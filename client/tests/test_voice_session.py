import argparse
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from live.voice import PartyVoice, SessionControl, run
ACCOUNT = '1000000000000000001'
from live.transport import Response


SESSION = {'sessionId': 'test-party', 'owner': {'onlineId': 'test-host'},
           'members': [{'accountId': '456', 'platform': 'MOBILE_APP'}], 'bridges': []}


class FakePush:
    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class VoiceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, join_error=False, existing=False, bridge=False):
        session = {**SESSION, 'members': [*SESSION['members']]}
        if bridge:
            session['bridges'] = [{'bridgeId': 'available-bridge', 'bridgeToken': 'fake'}]
        if existing:
            session['members'].append({'accountId': ACCOUNT})
        calls = []
        control = SessionControl()
        control.stop.set()

        def send(spec, token):
            calls.append(spec.method)
            if spec.method == 'GET':
                return Response(200, {'v2PartySessions': [session]})
            if spec.method == 'POST':
                if join_error:
                    raise TimeoutError('response lost after join may have succeeded')
                return Response(201, {})
            return Response(204, None)

        args = argparse.Namespace(session='test-party', owner='test-host', input_device=None,
                                  output_device=None, muted=True, silent=True, seconds=0, list=False)
        with patch('live.voice.sign_in', return_value=SimpleNamespace(access_token='fake', expires_in=3600)), \
             patch('live.voice.account_id_from_token', return_value=ACCOUNT), \
             patch('live.voice.account_allowed', return_value=True), \
             patch('live.voice.send_spec', side_effect=send), \
             patch('live.voice.PushConnection', FakePush):
            if existing:
                with self.assertRaisesRegex(RuntimeError, 'Already a member'):
                    await run(args, control, lambda _: None)
            elif join_error:
                with self.assertRaises(TimeoutError):
                    await run(args, control, lambda _: None)
            else:
                await run(args, control, lambda _: None)
        return calls

    async def test_leave_after_user_stop(self):
        self.assertEqual(await self.exercise(), ['GET', 'POST', 'GET', 'DELETE'])

    async def test_cleanup_after_uncertain_join(self):
        self.assertEqual(await self.exercise(join_error=True), ['GET', 'POST', 'DELETE'])

    async def test_preserve_membership_owned_by_other_app(self):
        self.assertEqual(await self.exercise(existing=True), ['GET'])

    async def test_existing_bridge_does_not_block_direct_join(self):
        self.assertEqual(await self.exercise(bridge=True), ['GET', 'POST', 'GET', 'DELETE'])

    async def test_ice_restart_replaces_transport(self):
        from psclient import signaling
        old = SimpleNamespace(pc=SimpleNamespace(connectionState='connecting'), close=AsyncMock())
        fresh = SimpleNamespace(
            pc=SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(kind='audio', mid='0')]),
            answer=AsyncMock(return_value='v=0\r\n'), close=AsyncMock())
        voice = PartyVoice('fake', ACCOUNT, SESSION, None, None, lambda _: None)
        voice.peers['456'] = old
        voice.remote_ice['456'] = (('old-ufrag', 'old-password'),)
        message = signaling.description(signaling.identity('456'), signaling.identity(ACCOUNT),
                                        'v=0\r\n', 'offer', 2, 10)
        event = {'body': {'data': {'sessionId': 'test-party',
            'customProperties': {'from': {'accountId': '456'}},
            'customMessage': {'channel': 'miranda:12', 'payload': signaling.payloads(message)[0]}}}}
        parsed = SimpleNamespace(media=[SimpleNamespace(ice=SimpleNamespace(usernameFragment='new', password='fresh'))])
        with patch('aiortc.sdp.SessionDescription.parse', return_value=parsed), \
             patch('live.voice.VoicePeer', return_value=fresh), \
             patch('live.voice.send_spec', return_value=Response(204, None)):
            await voice.process(event)
        old.close.assert_awaited_once()
        fresh.answer.assert_awaited_once()
        self.assertIs(voice.peers['456'], fresh)

    async def test_unsupported_control_does_not_disconnect_voice(self):
        log = []
        voice = PartyVoice('fake', ACCOUNT, SESSION, None, None, log.append)
        await voice.process({'body': {'data': {'sessionId': 'test-party',
            'customMessage': {'channel': 'miranda:12', 'payload': 'unknown-control-format'}}}})
        self.assertEqual(log, ['Ignored an unsupported signaling/control packet.'])

    async def test_ps5_offer_and_correlated_answer(self):
        from psclient import signaling
        session = {**SESSION, 'members': [{'accountId': '456', 'platform': 'PS5'}]}
        logs = []
        voice = PartyVoice('fake', ACCOUNT, session, None, None, logs.append)
        peer = SimpleNamespace(
            pc=SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(kind='audio', mid='0')]),
            offer=AsyncMock(return_value='fresh local offer'), accept_answer=AsyncMock(), close=AsyncMock())
        with patch('live.voice.VoicePeer', return_value=peer), \
             patch('live.voice.send_spec', return_value=Response(204, None)) as send:
            await voice.start_console_offers()
            await voice.start_console_offers()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(voice.pending_offers, {'456': 1})
        for sequence in (2, 1, 1):
            message = signaling.description(signaling.identity('456', 5), signaling.identity(ACCOUNT),
                                            'fresh remote answer', 'answer', sequence, 5)
            event = {'body': {'data': {'sessionId': 'test-party',
                'customProperties': {'from': {'accountId': '456'}},
                'customMessage': {'channel': 'miranda:12', 'payload': signaling.payloads(message)[0]}}}}
            await voice.process(event)
        peer.accept_answer.assert_awaited_once_with('fresh remote answer')
        self.assertFalse(voice.pending_offers)

    async def test_mobile_peer_keeps_passive_offer_role(self):
        voice = PartyVoice('fake', ACCOUNT, SESSION, None, None, lambda _: None)
        with patch('live.voice.VoicePeer') as create:
            await voice.start_console_offers()
        create.assert_not_called()
