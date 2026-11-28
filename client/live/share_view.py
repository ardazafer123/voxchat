"""Watch a party member's shared screen.

The stream server hands out a viewer id and a signaling ticket, then a protoo WebSocket
sets up a receive-only WebRTC call. The server offers several quality layers; the viewer
asks for one when it connects (changing quality reconnects).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import socket
import urllib.request

from aiortc import RTCPeerConnection, RTCSessionDescription
from websockets.asyncio.client import connect

from live.bridge import allow_rsa_suites

STREAM_API = 'https://bfe-prod.playstation-cloud.com'
# Layers the stream server offers (its profile list): label -> layer id.
QUALITIES = {'1080p': 's3', '720p': 's2', '540p': 's1', '360p': 's0'}
DEFAULT_QUALITY = '1080p'

# Receive-only offer the stream server accepts: audio then video, H264 only.
_AUDIO_EXT = ['1 urn:ietf:params:rtp-hdrext:ssrc-audio-level',
              '2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time',
              '3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01',
              '4 urn:ietf:params:rtp-hdrext:sdes:mid',
              '5 urn:uuid:84a86fab-a84f-4d61-b376-2ee79d2b67d2#sie:rtp-hdrext:rtp-payload-signature']
_VIDEO_EXT = ['14 urn:ietf:params:rtp-hdrext:toffset',
              '2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time',
              '13 urn:3gpp:video-orientation',
              '3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01',
              '12 http://www.webrtc.org/experiments/rtp-hdrext/playout-delay',
              '6 http://www.webrtc.org/experiments/rtp-hdrext/video-content-type',
              '7 http://www.webrtc.org/experiments/rtp-hdrext/video-timing',
              '8 http://www.webrtc.org/experiments/rtp-hdrext/color-space',
              '4 urn:ietf:params:rtp-hdrext:sdes:mid',
              '10 urn:ietf:params:rtp-hdrext:sdes:rtp-stream-id',
              '11 urn:ietf:params:rtp-hdrext:sdes:repaired-rtp-stream-id']
_AUDIO_CODECS = ['a=rtpmap:63 red/48000/2', 'a=fmtp:63 96/96', 'a=rtpmap:111 opus/48000/2',
                 'a=fmtp:111 minptime=10;stereo=0;useinbandfec=1', 'a=rtpmap:127 red/48000/2', 'a=fmtp:127 111/111',
                 'a=rtpmap:106 CN/32000', 'a=rtpmap:105 CN/16000', 'a=rtpmap:13 CN/8000',
                 'a=rtpmap:110 telephone-event/48000', 'a=rtpmap:113 telephone-event/16000', 'a=ptime:40']
# (H264 payload type, its rtx payload type, packetization-mode, profile-level-id)
_H264 = [(126, 97, 1, '42001f'), (98, 99, 0, '42001f'), (100, 101, 1, '42e01f'),
         (102, 103, 0, '42e01f'), (104, 125, 1, '4d001f'), (35, 36, 0, '4d001f')]
_FEEDBACK = ['goog-remb', 'transport-cc', 'ccm fir', 'nack', 'nack pli']


def _video_codecs():
    lines = []
    for pt, rtx, mode, level in _H264:
        lines.append(f'a=rtpmap:{pt} H264/90000')
        lines += [f'a=rtcp-fb:{pt} {fb}' for fb in _FEEDBACK]
        lines += [f'a=fmtp:{pt} level-asymmetry-allowed=1;packetization-mode={mode};profile-level-id={level}',
                  f'a=rtpmap:{rtx} rtx/90000', f'a=fmtp:{rtx} apt={pt}']
    return lines + ['a=rtpmap:124 red/90000', 'a=rtpmap:107 rtx/90000', 'a=fmtp:107 apt=124',
                    'a=rtpmap:108 ulpfec/90000', 'a=rtpmap:37 flexfec-03/90000', 'a=rtcp-fb:37 goog-remb',
                    'a=rtcp-fb:37 transport-cc', 'a=fmtp:37 repair-window=10000000']


def offer_sdp(ufrag: str, pwd: str, fingerprint: str, session_id: int | None = None) -> str:
    """The viewer offer with our ICE credentials and DTLS fingerprint ('sha-256 AA:BB:…')."""
    session_id = session_id or random.randrange(10 ** 17, 10 ** 18)

    def media(kind, mid, payloads, extensions, codecs):
        return [f'm={kind} 9 UDP/TLS/RTP/SAVPF {payloads}', 'c=IN IP4 0.0.0.0', 'a=rtcp:9 IN IP4 0.0.0.0',
                f'a=ice-ufrag:{ufrag}', f'a=ice-pwd:{pwd}', 'a=ice-options:trickle', f'a=fingerprint:{fingerprint}',
                'a=setup:actpass', f'a=mid:{mid}', *[f'a=extmap:{e}' for e in extensions],
                'a=recvonly', 'a=rtcp-mux', 'a=rtcp-rsize', *codecs]

    video_pts = ' '.join(str(p) for pair in _H264 for p in pair[:2]) + ' 124 107 108 37'
    lines = ['v=0', f'o=- {session_id} 2 IN IP4 127.0.0.1', 's=-', 't=0 0', 'a=group:BUNDLE 0 1',
             'a=extmap-allow-mixed', 'a=msid-semantic: WMS',
             *media('audio', 0, '96 63 111 127 106 105 13 110 113', _AUDIO_EXT, _AUDIO_CODECS),
             *media('video', 1, video_pts, _VIDEO_EXT, _video_codecs())]
    return '\r\n'.join(lines) + '\r\n'


def fix_remote_sdp(sdp: str, resolve=socket.gethostbyname) -> str:
    """Make the server's offer usable by aiortc.

    - Trailing spaces ('a=rtcp-fb:101 goog-remb ') make aiortc drop every feedback type, so it
      never reports bandwidth and the server stays on its lowest layer.
    - Ask for key frames (PLI) on loss; the server's offer leaves it out.
    - aiortc needs IP addresses in candidates; the server gives a host name.
    """
    sdp = re.sub(r'[ \t]+(?=\r?$)', '', sdp, flags=re.M)

    def add_pli(match):
        pt, line = match.group(1), match.group(0)
        return line if re.search(rf'^a=rtcp-fb:{pt} nack pli', sdp, re.M) else f'{line}\r\na=rtcp-fb:{pt} nack pli'
    sdp = re.sub(r'^a=rtcp-fb:(\d+) nack(?=\r?$)', add_pli, sdp, flags=re.M)

    def ip(match):
        host = match.group(2)
        return match.group(1) + (host if re.fullmatch(r'[\d.]+|[0-9a-fA-F:]+', host) else resolve(host)) + match.group(3)
    return re.sub(r'^(a=candidate:\S+ \d+ \S+ \d+ )(\S+)( \d+ typ .*)$', ip, sdp, flags=re.M)


def _http(method, url, body=None):
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b'{}')


class ShareViewer:
    """One viewing session. The UI reads `state`, `frame` (latest av.VideoFrame) and `size`."""

    def __init__(self, token, party_id, stream, quality=DEFAULT_QUALITY, output=None, name=None, log=print):
        self.token, self.party_id, self.stream = token, party_id, stream
        self.quality = quality if quality in QUALITIES else DEFAULT_QUALITY
        self.output, self.name, self.log = output, name, log
        self.state = 'connecting'  # connecting | live | ended | failed
        self.frame = None
        self.size = None

    async def run(self):
        try:
            await self._run()
            self.state = 'ended'
        except asyncio.CancelledError:
            self.state = 'ended'
            raise
        except Exception as exc:
            self.state = 'failed'
            # Library errors may carry tokens or SDP; log the type only.
            self.log(f'Screen view failed: {exc if isinstance(exc, RuntimeError) else type(exc).__name__}')

    async def _run(self):
        info = json.loads(self.stream['ConnectionInfo'])
        broadcast = 'nativeStreamer-' + self.stream['SessionId']
        viewer = (await asyncio.to_thread(_http, 'GET', STREAM_API + '/v1/broadcast/sessionId'))['sessionId']
        auth = await asyncio.to_thread(_http, 'POST', f'{STREAM_API}/v3/broadcast/sessions/{viewer}/authorize/view', {
            'broadcastInfo': info, 'npEnvironment': 'np', 'partySessionId': self.party_id,
            'platform': 'MOBILE_APP', 'sessionId': viewer, 'userToken': self.token})
        url = f"wss://{info['signalingServerAddress']}:{info['signalingServerPort']}/?data={auth['sessionData']}"
        pc = RTCPeerConnection()
        allow_rsa_suites(pc)  # the stream server's DTLS certificate is RSA
        tasks = set()
        try:
            async with connect(url, subprotocols=['protoo'], open_timeout=20, max_size=1 << 22) as ws:
                pending, counter = {}, 0

                async def read():
                    async for raw in ws:
                        message = json.loads(raw)
                        if message.get('response') and message.get('id') in pending:
                            pending.pop(message['id']).set_result(message)
                        elif message.get('request'):
                            await ws.send(json.dumps({'response': True, 'id': message['id'], 'ok': True, 'data': {}}))

                async def ask(data, method='mediasoup-request'):
                    nonlocal counter
                    counter += 1
                    future = asyncio.get_running_loop().create_future()
                    pending[counter] = future
                    await ws.send(json.dumps({'request': True, 'id': counter, 'method': method,
                                              'data': data, 'destination': info['sfuID']}))
                    reply = await asyncio.wait_for(future, 20)
                    if not reply.get('ok'):
                        raise RuntimeError(f"stream server refused {data['method']}: "
                                           f"{reply.get('name') or reply.get('errorReason') or 'no reason'}")
                    return reply.get('data') or {}

                reader = asyncio.create_task(read())
                tasks.add(reader)
                await ask({'method': 'getProfileInfo', 'broadcastId': broadcast})
                pc.addTransceiver('audio', direction='recvonly')
                pc.addTransceiver('video', direction='recvonly')
                ours = (await pc.createOffer()).sdp
                field = lambda key: re.search(rf'^a={key}:(.*?)\r?$', ours, re.M).group(1)
                offer = offer_sdp(field('ice-ufrag'), field('ice-pwd'),
                                  re.search(r'^a=fingerprint:(sha-256 .*?)\r?$', ours, re.M).group(1))
                layer = QUALITIES[self.quality]
                transport = await ask({'method': 'createTransport', 'broadcastId': broadcast, 'peerId': viewer,
                                       'kind': 'audio_video', 'localSDP': offer,
                                       'initProfile': layer, 'preferredProfile': layer})
                remote = await ask({'method': 'createRemoteSDPChrome', 'broadcastId': broadcast, 'peerId': viewer,
                                    'localSDP': offer, 'transportRemoteParameters': transport,
                                    'kinds': ['video', 'audio']})

                @pc.on('track')
                def on_track(track):
                    if track.kind == 'video':
                        tasks.add(asyncio.create_task(self._frames(track)))
                    elif self.output is not None:
                        tasks.add(asyncio.create_task(self.output.consume(track, self.name)))

                sdp = await asyncio.to_thread(fix_remote_sdp, remote['remoteSDP'])
                await pc.setRemoteDescription(RTCSessionDescription(sdp, 'offer'))
                await pc.setLocalDescription(await pc.createAnswer())
                await ask({'method': 'updateTransport', 'broadcastId': broadcast, 'peerId': viewer, 'localSDP': offer},
                          'mediasoup-notification')
                await ask({'method': 'enableConsumer', 'broadcastId': broadcast, 'kind': 'audio_video', 'peerId': viewer})
                self.log(f'Screen view: connected at {self.quality}.')
                await reader  # ends when the server closes the session (sharing stopped)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            await pc.close()

    async def _frames(self, track):
        while True:
            frame = await track.recv()
            self.frame, self.size = frame, (frame.width, frame.height)
            self.state = 'live'
