"""One WebRTC peer. Protocol metadata is handled by the party controller."""
import asyncio
import contextlib
import json
import os

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, RTCRtpSender
from aiortc.contrib.media import MediaRelay

from psclient.opus16k import install

# Logs member changes, push topics, audio gaps and every distinct data-channel packet (hex).
# Off by default: hex packets may carry peer identifiers.
DEBUG = os.environ.get('PARTY_VOICE_DEBUG') == '1'


class VoicePeer:
    # Phone answers use a public STUN: the network's own one can stall gathering long enough for
    # the phone to give up. Console offers always use the network STUN: with any other, the console
    # never answers the connection checks (seen live). Gathering waits at most 5 s for it.
    NETWORK_STUN = 'stun:stn.np.community.playstation.net'
    PUBLIC_STUN = 'stun:stun.l.google.com:19302'

    def __init__(self, source, output, relay: MediaRelay, on_state=print, ice_servers=None, name=None, send=True):
        """`name` labels received audio; a callable gets the track's SDP mid (bridge: one track per member).
        send=False leaves the microphone track unattached (`self.mic`) for the caller to place."""
        install()
        servers = [RTCIceServer(self.PUBLIC_STUN)] if ice_servers is None else ice_servers
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.mic = relay.subscribe(source)
        self.mic._id = source.id
        if send:
            sender = self.pc.addTrack(self.mic)
            # Match the app's stream-less track identifiers. The pinned aiortc
            # exposes stream_id only internally; confined to this adapter.
            sender._stream_id = '-'
        self.output = output
        self.tasks = set()
        self.channels = []
        self.on_state = on_state

        @self.pc.on('connectionstatechange')
        async def state():
            self.on_state('WebRTC: ' + self.pc.connectionState)

        @self.pc.on('iceconnectionstatechange')
        async def ice_state():
            self.on_state('ICE: ' + self.pc.iceConnectionState)

        @self.pc.on('track')
        def track_received(track):
            if track.kind == 'audio':
                who = name
                if callable(name):
                    mid = next((t.mid for t in self.pc.getTransceivers() if t.receiver.track is track), None)
                    who = name(mid)
                task = asyncio.create_task(output.consume(track, who))
                self.tasks.add(task)

        @self.pc.on('datachannel')
        def channel_received(channel):
            self.channels.append(channel)
            self.on_state('Data channel opened: ' + ('control' if channel.label == 'control' else 'peer channel'))
            seen = 0

            distinct = set()

            @channel.on('message')
            def message_received(data):
                nonlocal seen
                if DEBUG and data not in distinct and len(distinct) < 64:
                    distinct.add(data)
                    shown = data.hex() if isinstance(data, bytes) else repr(data[:120])
                    self.on_state(f'DC {channel.label} bytes={len(data)} {shown}')
                seen += 1
                if seen > 4:
                    return
                if data in ('hello', b'hello'):
                    self.on_state('Control heartbeat received.')
                else:
                    try:
                        packet = json.loads(data)
                        name = packet.get('type', 'JSON') if isinstance(packet, dict) else 'JSON'
                    except (ValueError, UnicodeDecodeError):
                        name = 'binary'
                    self.on_state(f'Data channel message: {name}; bytes={len(data)}')

    async def offer(self):
        # We initiate towards consoles.
        # Offer only the 16 kHz Opus profile we can encode, with fresh local ICE/DTLS.
        codecs = [c for c in RTCRtpSender.getCapabilities('audio').codecs
                  if c.mimeType.lower() == 'audio/opus' and c.clockRate == 16000]
        for transceiver in self.pc.getTransceivers():
            if transceiver.kind == 'audio':
                transceiver.setCodecPreferences(codecs)
        self.channels.append(self.pc.createDataChannel('control', ordered=True))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self._local_sdp()

    async def accept_answer(self, sdp):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='answer'))

    async def answer(self, sdp):
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='offer'))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return self._local_sdp()

    def _local_sdp(self):
        sdp = self.pc.localDescription.sdp
        # aiortc doesn't retain ptime attributes; the encoder emits 40 ms
        # packets, so advertise that duration on the signaling copy.
        lines = sdp.splitlines()
        for index, line in enumerate(lines):
            if line.startswith('m=audio '):
                lines.insert(index + 1, 'a=ptime:40')
                break
        return '\r\n'.join(lines) + '\r\n'

    async def close(self):
        # aioice0.10.2 can retain STUN retry timers across a transport close.
        # Cancel outstanding transactions before releasing their UDP sockets.
        transports = {t.sender.transport.transport for t in self.pc.getTransceivers()}
        if self.pc.sctp:
            transports.add(self.pc.sctp.transport.transport)
        for transport in transports:
            for protocol in transport._connection._protocols:
                for transaction in list(protocol.transactions.values()):
                    future = transaction._Transaction__future
                    if not future.done():
                        future.cancel()
        await asyncio.sleep(0)
        await self.pc.close()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
