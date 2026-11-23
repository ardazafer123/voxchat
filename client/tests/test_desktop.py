import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch
import asyncio
import tempfile
import time

from PySide6.QtWidgets import QApplication, QMessageBox
import desktop
from desktop import PartyWindow
from live import auth
from live.voice import SessionControl


def temp_store(case):
    """Point the account store at a fresh temp dir for one test."""
    root = case.enterContext(tempfile.TemporaryDirectory())
    for module, name, value in ((auth, 'ACCOUNTS_DIR', root + '/accounts'), (auth, 'ACTIVE_PATH', root + '/active'),
                                (auth, 'DEFAULT_NPSSO_PATH', root + '/npsso'), (desktop, 'DEFAULT_NPSSO_PATH', root + '/npsso')):
        case.enterContext(patch.object(module, name, value))
    case.enterContext(patch.dict(os.environ, {'PSN_NPSSO': ''}))
    return root


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_store(self)

    def test_save_switch_and_sign_out(self):
        auth.save_account('111', 'alpha', 'npsso-a')
        auth.save_account('222', 'beta', 'npsso-b')
        self.assertEqual(auth.active_account(), '222')
        self.assertEqual(auth.load_npsso(), 'npsso-b')
        self.assertEqual(os.stat(auth._account_path('111')).st_mode & 0o777, 0o600)
        self.assertEqual([a['online_id'] for a in auth.list_accounts()], ['alpha', 'beta'])
        self.assertNotIn('npsso', auth.list_accounts()[0])
        auth.set_active('111')
        self.assertEqual(auth.load_npsso(), 'npsso-a')
        auth.remove_account('111')
        self.assertIsNone(auth.active_account())
        self.assertFalse(auth.is_acknowledged('111'))
        self.assertTrue(auth.is_acknowledged('222'))
        with self.assertRaises(auth.AuthError):
            auth.load_npsso()

    def test_rejects_path_tricks_and_adopts_legacy_file(self):
        with self.assertRaises(auth.AuthError):
            auth._account_path('../x')
        self.assertFalse(auth.is_acknowledged('../x'))
        with open(auth.DEFAULT_NPSSO_PATH, 'w') as fh:
            fh.write('legacy\n')
        self.assertTrue(auth.adopt_legacy_npsso('333', 'old'))
        self.assertFalse(os.path.exists(auth.DEFAULT_NPSSO_PATH))
        self.assertEqual((auth.active_account(), auth.load_npsso()), ('333', 'legacy'))


class SocialParseTest(unittest.TestCase):
    def test_group_and_message_shapes(self):
        from live import social
        group = {'groupId': 'g', 'groupName': {'value': ''}, 'existsNewArrival': True,
                 'members': [{'accountId': '1', 'onlineId': 'me'}, {'accountId': '2', 'onlineId': 'pal'}],
                 'mainThread': {'threadId': 't', 'latestMessage': {'body': 'hi'}}}
        conversation = social.group_to_conversation(group, '1')
        self.assertEqual((conversation['name'], conversation['thread'], conversation['unread']), ('pal', 't', 1))
        row = social.message_row({'sender': {'accountId': '2'}, 'body': 'yo', 'createdTimestamp': '1795356493348'}, conversation['names'])
        self.assertEqual((row['sender'], row['body']), ('pal', 'yo'))
        self.assertEqual(social.message_row({}, {})['body'], '(unsupported message)')
        self.assertTrue(social.message_row({'messageType': 2006, 'body': 'x'}, {})['system'])
        self.assertFalse(row['system'])


class SocialRequestTest(unittest.TestCase):
    """Writes must match the mobile app's requests exactly."""

    def test_write_requests_match_mobile_app(self):
        from live import social
        sent = []

        class Recorder(social.Social):
            def call(self, method, path, query=None, body=None, headers=None):
                sent.append((method, path, body))
                return {'groupId': 'g', 'mainThread': {'threadId': 't'}}

        client = Recorder()
        client.send({'id': 'g', 'thread': 't'}, 'hi')
        chat = client.open_direct({'account_id': '9', 'online_id': 'pal'})
        client.send_request('9')
        client.answer_request('9', True)
        client.answer_request('9', False)
        self.assertEqual(sent, [
            ('POST', 'gamingLoungeGroups/v1/groups/g/threads/t/messages', {'messageType': 1, 'body': 'hi'}),
            ('POST', 'gamingLoungeGroups/v1/groups', {'invitees': [{'accountId': '9'}]}),
            ('POST', 'userProfile/v1/internal/users/me/friends/9', {}),
            ('PUT', 'userProfile/v1/internal/users/me/friends/9', None),
            ('DELETE', 'userProfile/v1/internal/users/me/friends/9', None)])
        self.assertEqual((chat['id'], chat['thread'], chat['members']), ('g', 't', ['pal']))

    def test_party_invite_body(self):
        from psclient import rest
        spec = rest.invite_to_party('s1', ['9'])
        self.assertEqual((spec.method, spec.path), ('POST', '/v2/partySessions/s1/invitations'))
        self.assertEqual(spec.body, {'invitations': [{'to': {'accountId': '9'}}], 'invitationType': 'NORMAL'})


class ProfilePictureTest(unittest.TestCase):
    def test_picks_large_https_avatar_and_skips_broken_downloads(self):
        from live import social
        self.assertEqual(social.avatar_url({'avatars': [{'size': 's', 'url': 'https://x/s.png'},
                                                        {'size': 'l', 'url': 'https://x/l.png'}]}), 'https://x/l.png')
        self.assertIsNone(social.avatar_url({'avatars': [{'size': 'l', 'url': 'file:///etc/passwd'}]}))
        self.assertEqual(social.avatar_url({'avatars': [{'size': 'm', 'url': 'http://x/m.png'}]}), 'https://x/m.png')
        self.assertIsNone(social.avatar_url({}))

        class Profiles(social.Social):
            def call(self, method, path, query=None, body=None, headers=None):
                return {'profiles': [{'onlineId': 'pal', 'avatars': [{'size': 'l', 'url': 'https://x/pal.png'}]},
                                     {'onlineId': 'bad', 'avatars': [{'size': 'l', 'url': 'https://x/bad.png'}]},
                                     {'onlineId': 'none'}]}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return b'png'

        def urlopen(url, timeout):
            if 'bad' in url:
                raise OSError('broken')
            return Response()

        with patch('urllib.request.urlopen', urlopen):
            faces = Profiles().faces({'1': 'pal', '2': 'bad', '3': 'none'})
        self.assertEqual(faces, {'pal': b'png'})

        class Down(social.Social):
            def call(self, *args, **kwargs):
                raise OSError('lookup down')

        client = Down()
        client.avatar_urls['known'] = 'https://x/known.png'  # e.g. from a search row
        with patch('urllib.request.urlopen', urlopen):
            self.assertEqual(client.faces({'1': 'known', '2': 'other'}), {'known': b'png'})


class DesktopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['voxchat-test'])

    def setUp(self):
        temp_store(self)
        auth.save_account('444', 'tester', 'synthetic')
        self.window = PartyWindow()

    def test_switch_and_sign_out_accounts(self):
        window = self.window
        auth.save_account('555', 'second', 'synthetic-2')
        window.reload_account()
        self.assertEqual(window.me, 'second')
        window.switch_account({'account_id': '444', 'online_id': 'tester'})
        self.assertEqual(window.me, 'tester')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            window.sign_out({'account_id': '444', 'online_id': 'tester'})
        self.assertEqual(window.me, 'second')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            window.sign_out({'account_id': '555', 'online_id': 'second'})
        self.assertIsNone(window.me)
        self.assertFalse(window.refresh_button.isEnabled())
        self.assertFalse(window.sign_in_button.isEnabled())
        window.npsso_input.setText('x')
        window.risk_box.setChecked(True)
        self.assertTrue(window.sign_in_button.isEnabled())

    def wait_for(self, check):
        deadline = time.monotonic() + 2
        while not check() and time.monotonic() < deadline:
            time.sleep(.01)
            self.window.poll()
        self.assertTrue(check())

    def test_messages_and_friend_requests_use_social_client(self):
        window = self.window
        calls = []

        class FakeSocial:
            def conversations(self):
                return [{'id': 'c1', 'name': 'pal', 'members': ['pal'], 'last': 'hi', 'unread': 1}]

            def messages(self, conversation):
                return [{'sender': 'pal', 'body': '<b>not html</b>'}]

            def send(self, conversation, text):
                calls.append(('send', conversation['id'], text))
                return None

            def friends(self):
                return [{'account_id': '9', 'online_id': 'pal', 'presence': 'online'}]

            def requests(self):
                answered = any(call[0] == 'answer' for call in calls)
                return [] if answered else [{'account_id': '8', 'online_id': 'asker'}]

            def answer_request(self, account_id, accept):
                calls.append(('answer', account_id, accept))

            def search(self, query):
                return [{'account_id': '7', 'online_id': query, 'relation': 'none'}]

            def send_request(self, account_id):
                calls.append(('request', account_id))

        window.social_client = FakeSocial()
        window.rail.select(desktop.MESSAGES)
        self.wait_for(lambda: window.chat_list.count() == 1)
        window.chat_list.setCurrentRow(0)
        self.wait_for(lambda: window.thread_list.count() == 1)
        bubble = window.thread_list.itemWidget(window.thread_list.item(0)).findChildren(desktop.QLabel)[-1]
        self.assertEqual(bubble.textFormat(), desktop.Qt.TextFormat.PlainText)
        window.chat_input.setText('hello')
        window.send_message()
        self.wait_for(lambda: window.thread_list.count() == 2)
        self.assertIn(('send', 'c1', 'hello'), calls)

        window.rail.select(desktop.FRIENDS)
        self.wait_for(lambda: window.friend_list.count() == 1)
        self.assertTrue(window.requests_card.isVisibleTo(window))
        window.answer_request(window.requests[0], True)
        self.wait_for(lambda: ('answer', '8', True) in calls and not window.requests)

        window.friends_tab(1)
        window.search_input.setText('ab')
        self.assertFalse(window.search_button.isEnabled())
        window.search_input.setText('newpal')
        window.search_players()
        self.wait_for(lambda: window.result_list.count() == 1)
        window.send_friend_request(window.results[0], desktop.Button(text='x'))
        self.wait_for(lambda: window.results[0]['relation'] == 'requested')
        self.assertIn(('request', '7'), calls)

    def test_social_results_from_old_account_are_dropped(self):
        window = self.window
        window.events.put(('social', (window.generation - 1, 'results', [{'account_id': '1', 'online_id': 'x'}])))
        window.poll()
        self.assertEqual(window.results, [])

    def tearDown(self):
        self.window.busy = False
        self.window.close()

    def test_select_mute_leave_and_close_wait_for_cleanup(self):
        window = self.window
        self.assertFalse(window.join_button.isEnabled())
        window.events.put(('parties', [{'sessionId': 's', 'owner': {'onlineId': 'host'}, 'members': [{}]}]))
        window.poll()
        window.update_buttons()
        self.assertTrue(window.join_button.isEnabled())
        window.busy = True
        window.control = SessionControl()
        window.update_buttons()
        self.assertFalse(window.join_button.isEnabled())
        window.mute()
        self.assertTrue(window.control.muted.is_set())
        window.mute()
        self.assertFalse(window.control.muted.is_set())
        window.show()
        window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(window.control.stop.is_set())
        window.events.put(('done', None))
        window.poll()
        self.assertFalse(window.isVisible())

    def test_rail_settings_and_avatar_accounts(self):
        window = self.window
        window.rail.select(desktop.SETTINGS)
        self.assertEqual(window.pages.currentIndex(), desktop.SETTINGS)
        window.rail_avatar.on_click()
        self.assertEqual(window.pages.currentIndex(), desktop.ACCOUNTS)
        self.assertFalse(any(b.isChecked() for b in window.rail.buttons))

    def test_member_connection_mute_and_local_silence(self):
        window = self.window
        window.me = 'tester'
        window.busy, window.hosting, window.offline = True, True, True
        control = window.control = SessionControl()
        control.members = ['tester', 'pal', 'slow', 'gone']
        control.states = {'pal': 'connected', 'slow': 'connecting', 'gone': 'disconnected'}
        control.remote_muted = {'pal'}
        window.tick()
        status = {n: l.text() for n, l in window.member_status.items()}
        self.assertEqual(status, {'tester': '', 'pal': 'Muted', 'slow': 'Connecting…',
                                  'gone': 'Disconnected · no audio'})
        self.assertTrue(window.avatars['pal'].muted)
        self.assertEqual(window.avatars['slow'].state, 'connecting')
        window.avatars['pal'].on_click()
        self.assertEqual(control.silenced, {'pal'})
        window.tick()
        self.assertEqual(window.member_status['pal'].text(), 'Muted for you')
        self.assertTrue(window.avatars['pal'].silenced)
        window.avatars['pal'].on_click()
        self.assertEqual(control.silenced, set())
        window.avatars['pal'].on_click()
        window.events.put(('done', None))
        window.poll()
        self.assertEqual(window.silenced, set())

    def test_call_end_reloads_invitations_quietly(self):
        window = self.window
        window.me, window.busy, window.control = 'tester', True, SessionControl()
        with patch.object(window, 'refresh') as refresh:
            window.events.put(('error', 'This account was removed from the party'))
            window.events.put(('done', None))
            window.poll()
            refresh.assert_called_once_with(quiet=True)
            window.events.put(('done', None))  # a plain refresh finishing must not loop
            window.poll()
            refresh.assert_called_once()

    def test_peer_state_mapping(self):
        from live.voice import peer_state

        def peer(state):
            return type('P', (), {'pc': type('PC', (), {'connectionState': state})()})()
        self.assertEqual(peer_state(None), 'connecting')
        self.assertEqual(peer_state(peer('new')), 'connecting')
        self.assertEqual(peer_state(peer('connected')), 'connected')
        self.assertEqual(peer_state(peer('failed')), 'disconnected')

    def test_start_party_and_invite_friend(self):
        window = self.window
        window.offline = True
        window.friends = [{'account_id': '9', 'online_id': 'pal', 'presence': 'online', 'status': 'Online'}]
        window.loaded.add('friends')
        seen = []

        async def fake_call(options, control, log):
            seen.append(options.create)
            log('WebRTC: connected')
            while not control.stop.is_set():
                while not control.invites.empty():
                    friend = control.invites.get()
                    seen.append(friend['account_id'])
                    log(f"INVITE {friend['online_id']} -> HTTP 204")
                await asyncio.sleep(.01)
            log('LEAVE -> HTTP 204')

        with patch('desktop.run', new=fake_call):
            window.start_button.click()
            self.wait_for(lambda: window.status.text() == 'Connected · voice is live')
            self.assertEqual(window.party_title.text(), 'Your party')
            self.assertEqual(window.side_pages.currentIndex(), 1)
            invite = window.invite_list.itemWidget(window.invite_list.item(0)).findChildren(desktop.Button)[0]
            invite.click()
            self.wait_for(lambda: window.status.text() == 'Invite sent to pal')
            self.assertEqual(seen, [True, '9'])
            window.leave_button.click()
            window.worker.join(2)
            window.poll()
        self.assertFalse(window.hosting)
        self.assertEqual(window.side_pages.currentIndex(), 0)

    def test_start_party_from_chat_invites_its_members(self):
        window = self.window
        window.offline = True
        window.me = 'tester'
        window.loaded.add('friends')
        window.thread = {'id': 'g', 'names': {'444': 'tester', '9': 'pal', '7': 'buddy'}}
        window.update_social()
        self.assertTrue(window.chat_party.isEnabled())
        seen = []

        async def fake_call(options, control, log):
            seen.append(options.create)
            while not control.stop.is_set():
                while not control.invites.empty():
                    seen.append(control.invites.get()['online_id'])
                await asyncio.sleep(.01)

        with patch('desktop.run', new=fake_call):
            window.chat_party.click()
            self.assertEqual(window.pages.currentIndex(), desktop.PARTIES)
            self.wait_for(lambda: len(seen) == 3)
            self.assertEqual(seen, [True, 'pal', 'buddy'])
            self.assertEqual(window.invited, {'9', '7'})
            self.assertFalse(window.chat_party.isEnabled())
            window.leave_button.click()
            window.worker.join(2)
            window.poll()

    def test_invitation_poll_keeps_selection_and_skips_when_busy(self):
        window = self.window
        a = {'sessionId': 'a', 'owner': {'onlineId': 'x'}, 'members': [{'onlineId': 'x'}]}
        b = {'sessionId': 'b', 'owner': {'onlineId': 'y'}, 'members': [{'onlineId': 'y'}]}
        window.set_parties([a, b])
        window.party_list.setCurrentRow(1)
        first = window.party_list.itemWidget(window.party_list.item(0))
        window.polling = True
        window.social_result('parties-poll', [a, b])
        self.assertFalse(window.polling)
        self.assertIs(window.party_list.itemWidget(window.party_list.item(0)), first)  # no rebuild
        window.social_result('parties-poll', [{**b, 'members': [{'onlineId': 'y'}, {'onlineId': 'z'}]}, a])
        self.assertEqual(window.selected_party()['sessionId'], 'b')
        window.busy = True
        window.social_result('parties-poll', [])
        self.assertEqual(len(window.parties), 2)
        window.busy = False
        window.polling = True
        window.social_failed('parties-poll', 'HTTP 429')
        self.assertFalse(window.polling)
        window.offline = True
        with patch.object(window, 'social') as call:
            window.poll_invitations()
            call.assert_not_called()

    def test_worker_connects_and_leaves_without_touching_psn(self):
        window = self.window
        window.offline = True  # joining loads friends for the invite list
        window.events.put(('parties', [{'sessionId': 'synthetic', 'owner': {'onlineId': 'test-host'}, 'members': [{}]}]))
        window.poll()
        received = []

        async def fake_call(options, control, log):
            received.append((options.session, options.owner))
            log('WebRTC: connected')
            while not control.stop.is_set():
                await asyncio.sleep(.01)
            log('LEAVE -> HTTP 204')

        with patch('desktop.run', new=fake_call):
            window.join_button.click()
            deadline = time.monotonic() + 2
            while window.status.text() != 'Connected · voice is live' and time.monotonic() < deadline:
                time.sleep(.01)
                window.poll()
            self.assertEqual(window.status.text(), 'Connected · voice is live')
            self.assertEqual(received, [('synthetic', 'test-host')])
            window.leave_button.click()
            window.worker.join(2)
            window.poll()
        self.assertFalse(window.busy)
        self.assertEqual(window.status.text(), 'Left party')

    def test_profile_pictures_replace_initials_quietly(self):
        from PySide6.QtCore import QBuffer, QByteArray
        from PySide6.QtGui import QImage
        window = self.window
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0x3366cc)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        window.social_result('faces', {'tester': bytes(data), 'junk': b'not an image'})
        self.addCleanup(desktop.FACES.clear)
        self.assertIn('tester', desktop.FACES)
        self.assertNotIn('junk', desktop.FACES)
        window.rail_avatar.grab()  # paints the picture path
        hint = window.result_hint.text()
        window.social_failed('faces', 'boom')
        self.assertEqual(window.result_hint.text(), hint)
