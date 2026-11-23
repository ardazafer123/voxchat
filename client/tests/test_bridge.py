import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

from live import bridge
from aiortc.rtcsctptransport import RTCSctpTransport
from live.bridge import BridgeLink, member_bridge_peer


class FakeBridgeSctp(RTCSctpTransport):
    """Like the live bridge: never sends INIT, and as DTLS server uses odd stream ids."""

    @property
    def is_server(self):
        return True

    async def start(self, remoteCaps, remotePort):
        await super().start(remoteCaps, remotePort)
        self._data_channel_id = 1
from psclient import custom_data
from psclient.audio import AudioInput, AudioOutput
from psclient.opus16k import install

BID = '0b5e2f4c-1111-4222-8333-944455556666'




class OfferFixTest(unittest.TestCase):
    def test_swaps_bridge_connection_line(self):
        from live.bridge import fix_offer_sdp
        self.assertEqual(fix_offer_sdp('v=0\r\nc=IN 192.0.2.7 IP4\r\na=rtcp:9 IN IP4 0.0.0.0\r\n'),
                         'v=0\r\nc=IN IP4 192.0.2.7\r\na=rtcp:9 IN IP4 0.0.0.0\r\n')
        self.assertEqual(fix_offer_sdp('c=IN IP4 192.0.2.7\r\n'), 'c=IN IP4 192.0.2.7\r\n')


def cd4(fields):
    return {'customData4': base64.b64encode(json.dumps(fields).encode()).decode()}


class BridgeInfoTest(unittest.TestCase):
    def test_live_phone_value(self):
        # Real-world shape (ids replaced).
        member = cd4(['', 'AQAAAA==', '1', '0', '6.2.0', '1', '0', '0', f'{BID}\nPEER-UUID', ''])
        self.assertEqual(member_bridge_peer(member, BID), 'PEER-UUID')

    def test_round_trip_and_rejects(self):
        self.assertEqual(member_bridge_peer({'customData4': custom_data.metadata(f'{BID}\n42')}, BID), '42')
        self.assertIsNone(member_bridge_peer({'customData4': custom_data.metadata(f'{BID}\n42')}, 'other-bridge'))
        self.assertIsNone(member_bridge_peer({'customData4': custom_data.metadata()}, BID))
        self.assertIsNone(member_bridge_peer({'customData4': '!!'}, BID))
        self.assertIsNone(member_bridge_peer({'customData3': base64.b64encode(f'{BID}\n42'.encode()).decode()}, BID))
        self.assertIsNone(member_bridge_peer({}, BID))


class FakeBridgeTest(unittest.IsolatedAsyncioTestCase):
    """A local aiortc peer plays the bridge: it sends a remote member's audio on mid 0,
    receives our upstream on mid 1, and reads the DcApi requests from the `bridge` channel."""

    async def test_join_answers_subscribes_and_carries_audio_both_ways(self):
        install()
        remote, mine = AudioInput(mode='tone'), AudioInput(mode='tone')
        heard, bridge_out = AudioOutput(enabled=False), AudioOutput(enabled=False)
        fake = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        prefs = [c for c in RTCRtpSender.getCapabilities('audio').codecs
                 if c.mimeType == 'audio/opus' and c.clockRate == 16000]
        for t in (fake.addTransceiver(remote, direction='sendonly'), fake.addTransceiver('audio', direction='recvonly')):
            t.setCodecPreferences(prefs)
        fake.createDataChannel('probe')
        fake.sctp.__class__ = FakeBridgeSctp
        requests, tasks = [], []
        fake.on('track', lambda track: tasks.append(asyncio.create_task(bridge_out.consume(track))))
        fake.on('datachannel', lambda ch: ch.on('message', lambda m: requests.append(json.loads(m))))
        await fake.setLocalDescription(await fake.createOffer())
        mids = [t.mid for t in fake.getTransceivers()]
        offer = {'sdp': fake.localDescription.sdp,
                 'mids': [{'mid': mids[0], 'peerId': 55}, {'mid': mids[1], 'peerId': 77, 'upstream': True}]}
        sent = []

        def send_spec(spec, token):
            sent.append(spec)
            if spec.method == 'POST' and spec.path.endswith('/peers'):
                return SimpleNamespace(status=200, body={'peer': {'peerId': 77, 'offer': offer}})
            if spec.method == 'GET':
                return SimpleNamespace(status=200, body={'bridge': {'peers': [{'peerId': '55'}, {'peerId': '77'}]}})
            return SimpleNamespace(status=204, body=None)

        link = BridgeLink('tok', 'sid', BID, 'btok', mine, heard, MediaRelay(), lambda _: None,
                          {'55': 'acct-phone'}.get, ice_servers=[])
        try:
            with patch.object(bridge, 'send_spec', send_spec):
                await link.join([])  # the bridge GET seeds peer 55
                self.assertEqual(custom_data.metadata_fields(sent[2].body['customData4'])[8], f'{BID}\n77')
                answer = sent[3].body['answer']['sdp']
                self.assertTrue(sent[3].path.endswith('/peers/77/answer'))
                lines = {m.split('a=mid:')[1].split()[0]: m for m in answer.split('m=')[1:]}
                self.assertIn('a=sendonly', lines[mids[1]])
                self.assertIn('a=recvonly', lines[mids[0]])
                await fake.setRemoteDescription(RTCSessionDescription(answer, 'answer'))
                async with asyncio.timeout(10):
                    while len(requests) < 3 or heard.frames_received < 10 or bridge_out.frames_received < 10:
                        await asyncio.sleep(.05)
                self.assertTrue(link.connected())
                self.assertEqual([r['type'] for r in requests[:3]],
                                 ['setFloorControlParameters', 'setReceivingCapability', 'setMediaPubSubPeers'])
                self.assertIn('acct-phone', heard.levels)
                self.assertEqual(link.accounts(), {'acct-phone'})
                self.assertEqual(len(sent), 4)  # create, peer list, bridge info, answer
                link._message(json.dumps({'type': 'peerCreated', 'payload': {'peer': {'peerId': '88'}}}))
                async with asyncio.timeout(5):
                    while len(requests) < 4:
                        await asyncio.sleep(.05)
                self.assertEqual(requests[3]['type'], 'setMediaPubSubPeers')
                await link.close()
                self.assertEqual((sent[-1].method, sent[-1].path), ('DELETE', f'/v1/bridges/{BID}/peers/77'))
        finally:
            await fake.close()
            await link.peer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            remote.stop()
            mine.stop()


class JoinDecisionTest(unittest.IsolatedAsyncioTestCase):
    def session(self, phone_data):
        return {'sessionId': 'sid', 'bridges': [{'bridgeId': BID, 'bridgeToken': 'btok'}],
                'members': [{'accountId': 'me'}, {'accountId': 'phone', **phone_data}]}

    async def test_joins_once_when_a_member_moved_to_the_relay(self):
        from live.voice import PartyVoice
        session = self.session({'customData4': custom_data.metadata(f'{BID}\n55')})
        voice = PartyVoice('tok', 'me', session, None, None, lambda _: None)
        with patch('live.voice.BridgeLink') as create:
            create.return_value.join = unittest.mock.AsyncMock(side_effect=RuntimeError('HTTP 403'))
            create.return_value.close = unittest.mock.AsyncMock()
            await voice.join_bridge(session)
            await voice.join_bridge(session)
        create.return_value.join.assert_awaited_once()
        self.assertEqual(list(create.return_value.join.await_args.args[0]), ['55'])
        self.assertIsNone(voice.bridge)

    async def test_joins_after_bridge_fallback_message(self):
        from live.voice import PartyVoice
        session = self.session({})
        voice = PartyVoice('tok', 'me', session, None, None, lambda _: None)
        voice.fallback = True
        with patch('live.voice.BridgeLink') as create:
            create.return_value.join = unittest.mock.AsyncMock()
            create.return_value.bridge_id = BID
            await voice.join_bridge(session)
        create.return_value.join.assert_awaited_once()
        self.assertIsNone(voice.bridge_peer_account('99'))
        voice.members['phone']['customData4'] = custom_data.metadata(f'{BID}\n99')
        self.assertEqual(voice.bridge_peer_account('99'), 'phone')

    async def test_stays_on_p2p_without_a_relay_member(self):
        from live.voice import PartyVoice
        session = self.session({})
        voice = PartyVoice('tok', 'me', session, None, None, lambda _: None)
        with patch('live.voice.BridgeLink') as create:
            await voice.join_bridge(session)
        create.assert_not_called()


class TopologyMessageTest(unittest.TestCase):
    def test_bridge_fallback_message(self):
        from live.voice import topology_type
        from psclient import signaling
        body = b'3/1/1\n{"global_seq_num":0,"p2p_error_code":-2123525363,"retry_count":0,"type":"bridge_fallback"}'
        raw = signaling.HEADER.pack(1, 1, 1, 0, 7, 100001, 0, 0, len(body)) + body
        self.assertEqual(topology_type(signaling.PREFIX + base64.b64encode(raw).decode()), 'bridge_fallback')
        self.assertIsNone(topology_type('unknown-control-format'))


class UserTableTest(unittest.TestCase):
    def test_matches_live_phone_shape(self):
        from live.bridge import user_table
        msg = json.loads(user_table(3, 'BRIDGE-PEER', 'DUID', '123'))
        self.assertEqual(msg['type'], 'user_table_updated')
        self.assertEqual(msg['global_seq_num'], 3)
        peer = msg['peer']
        self.assertEqual((peer['bridge_peer_id'], peer['peer_id'], peer['miranda_signaling_version']), ('BRIDGE-PEER', 'DUID', 2))
        self.assertEqual(peer['users'], [{'account_id': '123', 'platform': 100001}])
        self.assertEqual(peer['medias'][0]['media_records'], [{'media_identifier': 'voice_chat', 'mid': ''}])
