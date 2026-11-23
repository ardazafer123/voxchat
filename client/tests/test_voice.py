import asyncio
import unittest
from unittest.mock import Mock

import numpy as np
from aiortc import RTCPeerConnection, RTCConfiguration, RTCRtpSender
from aiortc.contrib.media import MediaRelay
from aiortc.jitterbuffer import JitterFrame

import socket
import threading

from live.voice_peer import VoicePeer, stun_answers
from psclient.audio import AudioInput, AudioOutput
from psclient.opus16k import Opus16kEncoder, Opus16kDecoder, install


class CodecTest(unittest.IsolatedAsyncioTestCase):
    async def test_mute_closes_microphone_but_keeps_silent_track_alive(self):
        source = AudioInput(mode='microphone')
        stream = Mock()
        source._stream = stream
        source.set_muted(True)
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        first, second = await source.recv(), await source.recv()
        self.assertEqual(source.readyState, 'live')
        self.assertFalse(np.any(first.to_ndarray()))
        self.assertEqual(second.pts - first.pts, 960)
        source.stop()

    async def test_silenced_member_is_metered_but_not_played(self):
        output = AudioOutput()
        output._stream = Mock()  # skip opening a real device
        output.silenced = {'quiet'}
        tone = AudioInput(mode='tone')
        frames = [await tone.recv(), await tone.recv()]

        class Track:
            async def recv(self):
                if frames:
                    return frames.pop()
                await asyncio.sleep(1)

        played = {}
        for name in ('loud', 'quiet'):
            frames[:] = [await tone.recv(), await tone.recv()]
            task = asyncio.create_task(output.consume(Track(), name))
            await asyncio.sleep(.05)
            played[name] = sum(len(q) for q in output._buffers.values())
            self.assertGreater(output.levels[name], 0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(played['loud'], 0)
        self.assertEqual(played['quiet'], 0)
        tone.stop()

    async def test_16k_packet_duration_and_signal(self):
        source = AudioInput(mode='tone')
        encoder, decoder = Opus16kEncoder(), Opus16kDecoder()
        timestamps = []
        try:
            for _ in range(10):
                packets, timestamp = encoder.encode(await source.recv())
                for packet in packets:
                    timestamps.append(timestamp)
                    frames = decoder.decode(JitterFrame(data=packet, timestamp=timestamp))
                    self.assertAlmostEqual(sum(f.samples / f.sample_rate for f in frames), .04)
                    self.assertGreater(np.abs(frames[0].to_ndarray().astype(float)).mean(), 50)
            self.assertGreaterEqual(len(timestamps), 3)
            self.assertTrue(all(b - a == 640 for a, b in zip(timestamps, timestamps[1:])))
        finally:
            source.stop()

    async def test_local_ice_dtls_srtp_and_audio_both_directions(self):
        install()
        a, b = AudioInput(mode='tone'), AudioInput(mode='tone')
        output_a, output_b = AudioOutput(enabled=False), AudioOutput(enabled=False)
        caller = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        responder = VoicePeer(b, output_b, MediaRelay(), on_state=lambda _: None, ice_servers=[])
        tasks = []

        @caller.on('track')
        def on_track(track):
            tasks.append(asyncio.create_task(output_a.consume(track)))

        try:
            caller.addTrack(a)
            prefs = [c for c in RTCRtpSender.getCapabilities('audio').codecs if c.mimeType == 'audio/opus' and c.clockRate == 16000]
            caller.getTransceivers()[0].setCodecPreferences(prefs)
            caller.createDataChannel('test')
            await caller.setLocalDescription(await caller.createOffer())
            self.assertIn('opus/16000/2', caller.localDescription.sdp)
            answer = await responder.answer(caller.localDescription.sdp)
            from aiortc import RTCSessionDescription
            await caller.setRemoteDescription(RTCSessionDescription(answer, 'answer'))
            async with asyncio.timeout(10):
                while min(output_a.frames_received, output_b.frames_received) < 10:
                    await asyncio.sleep(.05)
            self.assertEqual(caller.connectionState, 'connected')
            self.assertEqual(responder.pc.connectionState, 'connected')
        finally:
            await caller.close()
            await responder.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            a.stop()
            b.stop()

    async def test_voicepeer_initiates_and_accepts_answer_with_bidirectional_audio(self):
        a, b = AudioInput(mode='tone'), AudioInput(mode='tone')
        out_a, out_b = AudioOutput(enabled=False), AudioOutput(enabled=False)
        caller = VoicePeer(a, out_a, MediaRelay(), on_state=lambda _: None, ice_servers=[])
        responder = VoicePeer(b, out_b, MediaRelay(), on_state=lambda _: None, ice_servers=[])
        try:
            offer = await caller.offer()
            self.assertIn('opus/16000/2', offer)
            self.assertNotIn('opus/48000', offer)
            self.assertIn('a=ptime:40', offer)
            self.assertIn('m=application', offer)
            await caller.accept_answer(await responder.answer(offer))
            async with asyncio.timeout(10):
                while min(out_a.frames_received, out_b.frames_received) < 10:
                    await asyncio.sleep(.05)
            self.assertEqual(caller.pc.connectionState, 'connected')
            self.assertEqual(responder.pc.connectionState, 'connected')
            self.assertGreater(out_a.peak, 0)
            self.assertGreater(out_b.peak, 0)
        finally:
            await caller.close()
            await responder.close()
            a.stop()
            b.stop()
            out_a.close()
            out_b.close()


class StunProbeTest(unittest.TestCase):
    def test_probe_needs_a_matching_reply(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]

        def answer():
            data, addr = server.recvfrom(2048)
            server.sendto(b'\x01\x01\x00\x00' + data[4:20], addr)

        threading.Thread(target=answer, daemon=True).start()
        self.assertTrue(stun_answers('127.0.0.1', port))
        server.close()
        self.assertFalse(stun_answers('127.0.0.1', port, timeout=.2))


class MicMutedTest(unittest.TestCase):
    def test_app_mute_flag_and_voice_chat_off(self):
        import base64, json
        from live.voice import mic_muted

        def cd4(flag):
            return base64.b64encode(json.dumps(["", "AQAAAA==", "1", "0", "6.2.0", flag, "0", "0", "", ""]).encode()).decode()

        self.assertTrue(mic_muted({'customData4': cd4('1')}))
        self.assertFalse(mic_muted({'customData4': cd4('0')}))
        self.assertTrue(mic_muted({'voiceChatActivated': False, 'customData4': cd4('0')}))
        self.assertFalse(mic_muted({}))
        self.assertFalse(mic_muted({'customData4': 'bm90IGpzb24='}))  # bridge info or other text


class HostedPartiesTest(unittest.TestCase):
    def test_discover_lists_hosted_parties_and_forgets_ended_ones(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        import live.voice as voice

        def party(spec, token):
            return spec.headers['X-PSN-SESSION-MANAGER-SESSION-IDS']

        def parse(sid, wanted):
            if sid == 'ended':
                raise RuntimeError('Requested party missing from response')
            return {'sessionId': sid}

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(voice, 'CONFIG_DIR', tmp), \
                patch.object(voice, 'account_id_from_token', lambda t: '42'), \
                patch.object(voice, 'get', lambda *a: SimpleNamespace(status=200, body={'invitations': [{'sessionId': 'invited'}]})), \
                patch.object(voice, 'send_spec', party), \
                patch.object(voice, 'session_from_response', parse):
            voice.save_hosted('42', ['ended', 'mine'])
            found = asyncio.run(voice.discover('token'))
            self.assertEqual({p['sessionId'] for p in found}, {'invited', 'mine'})
            self.assertEqual(voice.hosted_ids('42'), ['mine'])
