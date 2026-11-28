import asyncio
import base64
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from live.share_view import QUALITIES, ShareViewer, fix_remote_sdp, offer_sdp
from live.transport import Response

FINGERPRINT = 'sha-256 ' + ':'.join(['AB'] * 32)

# Shape of the stream server's offer: ice-lite, BUNDLE, a host-name candidate and
# feedback lines that end in a space.
REMOTE = '\r\n'.join([
    'v=0', 'o=mediasoup-client 1 1 IN IP4 0.0.0.0', 's=-', 't=0 0', 'a=ice-lite',
    'a=fingerprint:sha-512 ' + ':'.join(['CD'] * 64), 'a=msid-semantic: WMS *', 'a=group:BUNDLE audio-32 video-33',
    'm=audio 7 RTP/SAVPF 100', 'c=IN IP4 127.0.0.1', 'a=rtpmap:100 opus/48000/2', 'a=fmtp:100 useinbandfec=1',
    'a=setup:actpass', 'a=mid:audio-32', 'a=sendonly', 'a=ice-ufrag:serverufrag', 'a=ice-pwd:serverpassword0123',
    'a=candidate:udpcandidate 1 udp 1076302079 stream.example.test 40000 typ host', 'a=end-of-candidates',
    'a=ssrc:11 cname:x', 'a=rtcp-mux', 'a=rtcp-rsize',
    'm=video 7 RTP/SAVPF 101 102', 'c=IN IP4 127.0.0.1', 'a=rtpmap:101 H264/90000',
    'a=fmtp:101 packetization-mode=1;profile-level-id=42e01f', 'a=rtcp-fb:101 goog-remb ', 'a=rtcp-fb:101 nack ',
    'a=rtpmap:102 rtx/90000', 'a=fmtp:102 apt=101', 'a=setup:actpass', 'a=mid:video-33', 'a=sendonly',
    'a=ice-ufrag:serverufrag', 'a=ice-pwd:serverpassword0123',
    'a=candidate:udpcandidate 1 udp 1076302079 stream.example.test 40000 typ host', 'a=end-of-candidates',
    'a=ssrc:21 cname:x', 'a=ssrc:22 cname:x', 'a=ssrc-group:FID 21 22', 'a=rtcp-mux', 'a=rtcp-rsize', ''])


class OfferTest(unittest.TestCase):
    def test_offer_carries_our_credentials_audio_first_h264_only(self):
        sdp = offer_sdp('ufragX', 'passwordY', FINGERPRINT, 1)
        self.assertEqual(re.findall(r'^m=(\w+)', sdp, re.M), ['audio', 'video'])
        self.assertIn('a=group:BUNDLE 0 1', sdp)
        self.assertEqual(sdp.count('a=ice-ufrag:ufragX'), 2)
        self.assertEqual(sdp.count('a=fingerprint:' + FINGERPRINT), 2)
        self.assertEqual(sdp.count('a=recvonly'), 2)
        video = sdp.split('m=video')[1]
        codecs = set(re.findall(r'^a=rtpmap:\d+ ([\w-]+)/', video, re.M))
        self.assertEqual(codecs, {'H264', 'rtx', 'red', 'ulpfec', 'flexfec-03'})
        # Every payload type in the m-line has an rtpmap.
        listed = video.split('\r\n')[0].split()[2:]  # after the port and protocol
        self.assertEqual(sorted(listed), sorted(re.findall(r'^a=rtpmap:(\d+)', video, re.M)))

    def test_qualities_match_server_layers(self):
        self.assertEqual(QUALITIES, {'1080p': 's3', '720p': 's2', '540p': 's1', '360p': 's0'})


class RemoteSdpTest(unittest.TestCase):
    def test_fixes_spaces_adds_pli_and_resolves_host(self):
        fixed = fix_remote_sdp(REMOTE, resolve=lambda host: '192.0.2.7')
        self.assertNotRegex(fixed, r'[ \t]\r?\n')
        self.assertEqual(fixed.count('a=rtcp-fb:101 nack pli'), 1)
        self.assertIn('udp 1076302079 192.0.2.7 40000 typ host', fixed)
        self.assertNotIn('stream.example.test', fixed)
        self.assertEqual(fix_remote_sdp(fixed, resolve=lambda host: 1 / 0), fixed)  # idempotent, IPs left alone

    def test_aiortc_negotiates_bandwidth_feedback(self):
        # Without the fix aiortc drops every feedback type, so the server never sees bandwidth reports.
        from aiortc import RTCPeerConnection, RTCSessionDescription

        async def feedback(sdp):
            pc = RTCPeerConnection()
            with patch.object(pc, '_RTCPeerConnection__connect', AsyncMock()):  # no network in tests
                await pc.setRemoteDescription(RTCSessionDescription(sdp, 'offer'))
                await pc.setLocalDescription(await pc.createAnswer())
            await pc.close()
            return set(re.findall(r'^a=rtcp-fb:\d+ (.*?)\r?$', pc.localDescription.sdp, re.M))

        self.assertIn('goog-remb', asyncio.run(feedback(fix_remote_sdp(REMOTE, resolve=lambda h: '192.0.2.7'))))


def streaming(info=True):
    stream = {'SessionId': 'broadcast1', **({'ConnectionInfo': json.dumps({'sfuID': 'x'})} if info else {})}
    return base64.b64encode(json.dumps({'Streaming': stream}).encode()).decode()


class WatchTest(unittest.IsolatedAsyncioTestCase):
    def voice(self, custom):
        from live.voice import PartyVoice
        session = {'sessionId': 'party1', 'members': [
            {'accountId': '1', 'onlineId': 'Me'},
            {'accountId': '2', 'onlineId': 'Sharer', 'platform': 'PS5', 'isOnShareScreen': True, 'customData1': custom}]}
        return PartyVoice('tok', '1', session, None, None, lambda _: None), session

    async def test_asks_for_the_stream_then_watches_at_the_chosen_quality(self):
        voice, _ = self.voice(streaming())
        sent = []
        with patch('live.voice.send_spec', side_effect=lambda spec, token: sent.append(spec) or Response(204, None)), \
                patch.object(ShareViewer, 'run', AsyncMock()) as run:
            voice.watch_screen('2', '720p')
            await voice.viewer_task
        self.assertEqual([s.method for s in sent], ['POST', 'PATCH'])
        self.assertEqual(voice.viewer.quality, '720p')
        self.assertEqual(voice.viewer.stream['SessionId'], 'broadcast1')
        run.assert_awaited_once()

    async def test_waits_for_stream_details_then_gives_up(self):
        voice, session = self.voice(streaming(info=False))
        reply = Response(200, {'v2PartySessions': [session]})
        with patch('live.voice.send_spec', side_effect=lambda spec, token: reply if spec.method == 'GET' else Response(204, None)), \
                patch('live.voice.asyncio.sleep', AsyncMock()), patch.object(ShareViewer, 'run', AsyncMock()) as run:
            voice.watch_screen('2', '1080p')
            await voice.viewer_task
        self.assertEqual(voice.viewer.state, 'failed')
        run.assert_not_awaited()

    async def test_stop_cancels_and_clears_the_watching_flag(self):
        voice, _ = self.voice(streaming())
        sent = []
        with patch('live.voice.send_spec', side_effect=lambda spec, token: sent.append(spec) or Response(204, None)), \
                patch.object(ShareViewer, 'run', AsyncMock(side_effect=asyncio.Event().wait)):
            voice.watch_screen('2', '1080p')
            task = voice.viewer_task
            await asyncio.sleep(0.05)
            voice.stop_watching()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.05)
        self.assertIsNone(voice.viewer)
        self.assertEqual(sent[-1].method, 'PATCH')
        self.assertEqual(base64.b64decode(sent[-1].body['customData1']), b'{}')


if __name__ == '__main__':
    unittest.main()
