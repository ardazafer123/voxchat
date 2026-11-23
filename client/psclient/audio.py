"""Bounded, real-time microphone and speaker streams; no audio is saved."""
import asyncio
from collections import deque
from fractions import Fraction
import math
import threading
import time
import uuid

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError


def level(samples):
    """RMS loudness of int16 samples, 0..1, for speaking indicators."""
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float32))))) / 32768 if samples.size else 0.0


class AudioInput(MediaStreamTrack):
    kind = 'audio'

    def __init__(self, device=None, mode='microphone'):
        super().__init__()
        self._id = f'{str(uuid.uuid4()).upper()}_1_CELT/40'
        self.mode = mode
        self.device = device
        self._stream = None
        self._queue = asyncio.Queue(maxsize=5)
        self._pts = 0
        self._started = None
        self.muted = False
        self.level = 0.0

    def start(self):
        if self.mode != 'microphone' or self.muted or self._stream is not None:
            return
        import sounddevice as sd
        loop = asyncio.get_running_loop()

        def enqueue(block):
            if self.readyState != 'live':
                return
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(block)

        def callback(indata, frames, time, status):
            loop.call_soon_threadsafe(enqueue, indata.copy())

        self._stream = sd.InputStream(device=self.device, channels=1, samplerate=48000,
                                      dtype='int16', blocksize=960, callback=callback)
        self._stream.start()

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        self.start()
        if self.mode == 'microphone' and not self.muted:
            block = await self._queue.get()
            if block is None:
                raise MediaStreamError
            self._started = asyncio.get_running_loop().time() - self._pts / 48000
        else:
            loop = asyncio.get_running_loop()
            if self._started is None:
                self._started = loop.time()
            await asyncio.sleep(max(0, self._started + self._pts / 48000 - loop.time()))
            if self.mode == 'tone':
                samples = np.arange(960) + self._pts
                block = (1800 * np.sin(2 * math.pi * 440 * samples / 48000)).astype(np.int16)
            else:
                block = np.zeros(960, dtype=np.int16)
        if self.muted:
            block = np.zeros_like(block)
        self.level = level(block)
        frame = av.AudioFrame.from_ndarray(block.reshape(1, -1), format='s16', layout='mono')
        frame.sample_rate, frame.time_base, frame.pts = 48000, Fraction(1, 48000), self._pts
        self._pts += 960
        return frame

    def set_muted(self, muted):
        if self.muted == muted:
            return
        self.muted = muted
        if muted and self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        while not self._queue.empty():
            self._queue.get_nowait()
        # Wake a recv() already waiting on the microphone queue.
        if muted:
            self._queue.put_nowait(np.zeros((960, 1), dtype=np.int16))

    def stop(self):
        super().stop()
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioOutput:
    def __init__(self, device=None, enabled=True):
        self.device, self.enabled = device, enabled
        self._stream = None
        self._buffers = {}
        self._lock = threading.Lock()
        self.frames_received = 0
        self.peak = 0
        self.levels = {}
        self.silenced = set()  # names muted for this listener only
        self.last_frame = {}

    def start(self):
        if not self.enabled or self._stream is not None:
            return
        import sounddevice as sd

        def callback(outdata, frames, time, status):
            mixed = np.zeros(frames, dtype=np.int32)
            with self._lock:
                for queue in self._buffers.values():
                    offset = 0
                    while queue and offset < frames:
                        data = queue[0]
                        count = min(len(data), frames - offset)
                        mixed[offset:offset + count] += data[:count]
                        offset += count
                        if count == len(data):
                            queue.popleft()
                        else:
                            queue[0] = data[count:]
            outdata[:, 0] = np.clip(mixed, -32768, 32767).astype(np.int16)

        self._stream = sd.OutputStream(device=self.device, channels=1, samplerate=48000,
                                       dtype='int16', blocksize=960, callback=callback)
        self._stream.start()

    async def consume(self, track, name=None):
        self.start()
        key = id(track)
        queue = deque(maxlen=12)
        with self._lock:
            self._buffers[key] = queue
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000, frame_size=960)
        try:
            while True:
                frame = await track.recv()
                self.frames_received += 1
                samples = frame.to_ndarray()
                self.peak = max(self.peak, int(np.max(np.abs(samples.astype(np.int32)))))
                if name is not None:
                    self.levels[name] = level(samples)
                    self.last_frame[name] = time.monotonic()
                if self.enabled and name not in self.silenced:
                    for output in resampler.resample(frame):
                        with self._lock:
                            queue.append(output.to_ndarray().reshape(-1))
        finally:
            with self._lock:
                self._buffers.pop(key, None)
            self.levels.pop(name, None)
            self.last_frame.pop(name, None)

    def close(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
