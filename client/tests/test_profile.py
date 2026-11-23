import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
import urllib.error
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
import desktop
from live import auth, social
from tests.test_desktop import temp_store


class ProfileParseTest(unittest.TestCase):
    def test_rows_and_helpers(self):
        self.assertEqual(social.play_time('PT334H10M40S'), '334 h 10 min')
        self.assertEqual(social.play_time('P1DT2H'), '26 h 0 min')
        self.assertEqual(social.play_time('PT45M'), '45 min')
        self.assertEqual(social.play_time(None), '')
        game = social.game_row({'titleId': 'CUSA1', 'name': 'Raw', 'localizedName': 'Game', 'category': 'ps5_native_game',
                                'imageUrl': 'https://i/x.png', 'playDuration': 'PT2H5M'})
        self.assertEqual((game['name'], game['platform'], game['play_time']), ('Game', 'PS5', '2 h 5 min'))
        title = social.title_row({'npCommunicationId': 'NPWR1', 'trophyTitleName': 'T', 'progress': 50,
                                  'earnedTrophies': {'gold': 2}, 'definedTrophies': {'gold': 4, 'bronze': 9}})
        self.assertEqual((title['service'], title['earned']['gold'], title['defined']['platinum']), ('trophy', 2, 0))
        self.assertEqual([desktop.tier_name(t) for t in (1, 3, 4, 7, 10)], ['Bronze', 'Bronze', 'Silver', 'Gold', 'Platinum'])
        self.assertEqual(desktop.ago(None), '')
        self.assertEqual(desktop.ago('2020-01-02T03:04:05.123456Z'), 'Jan 2, 2020')

    def test_merge_trophies_hides_secrets_and_lists_newest_earned_first(self):
        defs = [{'trophyId': 0, 'trophyName': 'Plat', 'trophyType': 'platinum', 'trophyIconUrl': 'https://i/0'},
                {'trophyId': 1, 'trophyName': 'Secret', 'trophyDetail': 'Spoiler', 'trophyHidden': True,
                 'trophyType': 'gold', 'trophyIconUrl': 'https://i/1'},
                {'trophyId': 2, 'trophyName': 'Old', 'trophyType': 'bronze'},
                {'trophyId': 3, 'trophyName': 'New', 'trophyType': 'bronze'}]
        progress = [{'trophyId': 2, 'earned': True, 'earnedDateTime': '2024-01-01T00:00:00Z', 'trophyEarnedRate': '50'},
                    {'trophyId': 3, 'earned': True, 'earnedDateTime': '2025-01-01T00:00:00Z'}]
        rows = social.merge_trophies(defs, progress)
        self.assertEqual([r['name'] for r in rows], ['New', 'Old', 'Plat', 'Hidden trophy'])
        self.assertNotIn('Spoiler', rows[3]['detail'])
        self.assertIsNone(rows[3]['icon'])
        self.assertEqual(rows[1]['rate'], '50')


class ProfileRequestTest(unittest.TestCase):
    def test_private_sections_are_none_and_relation_comes_from_friend_list(self):
        class Fake(social.Social):
            def me(self):
                return '1'

            def presences(self, ids):
                return {'2': {'presence': 'online', 'status': 'Online on PS5', 'last_online': None}}

            def call(self, method, path, query=None, body=None, headers=None):
                if path.endswith('/profiles'):
                    return {'onlineId': 'pal', 'aboutMe': 'hi', 'isPlus': True, 'personalDetail': {'firstName': 'A', 'lastName': 'B'},
                            'avatars': [{'size': 'l', 'url': 'http://a/l.png'}]}
                if path.startswith('trophy/'):
                    raise urllib.error.HTTPError(path, 403, 'private', {}, None)
                if path == 'userProfile/v2/internal/users/1/friends':
                    return {'friends': ['2']}
                if path.endswith('/friends'):
                    return {'friends': ['x'], 'totalItemCount': 7}
                if path.startswith('gamelist/'):
                    return {'titles': [{'name': 'Old', 'lastPlayedDateTime': '2020-01-01T00:00:00Z'},
                                       {'name': 'New', 'lastPlayedDateTime': '2025-01-01T00:00:00Z'}], 'totalItemCount': 2}
                return {}

        data = Fake().profile('2')
        self.assertEqual((data['online_id'], data['name'], data['relation'], data['friends_count']), ('pal', 'A B', 'friend', 7))
        self.assertEqual(data['avatar'], 'https://a/l.png')
        self.assertIsNone(data['trophies'])
        self.assertIsNone(data['titles'])
        self.assertEqual([g['name'] for g in data['games']], ['New', 'Old'])
        self.assertEqual(data['status'], 'Online on PS5')


class ProfilePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(['voxchat-test'])

    def test_profile_page_shows_sections_and_trophy_list(self):
        temp_store(self)
        auth.save_account('444', 'tester', 'synthetic')
        window = desktop.PartyWindow()
        window.offline = True  # no PSN calls; results are fed in directly
        window.rail.select(desktop.FRIENDS)
        window.open_profile('2', 'pal')
        self.assertEqual(window.pages.currentIndex(), desktop.PROFILE)
        self.assertEqual(window.profile_status.text(), 'Loading profile…')
        title = {'id': 'NPWR1', 'service': 'trophy', 'name': 'Game Trophies', 'icon': None, 'platform': 'PS5',
                 'progress': 40, 'earned': social.counts({'gold': 1}), 'defined': social.counts({'gold': 2}), 'updated': None}
        window.social_result('profile', {
            'account_id': '2', 'online_id': 'pal', 'name': '', 'about': '<b>plain</b>', 'avatar': None, 'plus': True,
            'verified': False, 'languages': [], 'relation': 'none', 'presence': 'offline', 'status': 'Offline',
            'last_online': None, 'trophies': {'level': 12, 'progress': 40, 'tier': 1, **social.counts({'gold': 1})},
            'friends_count': None, 'games': [], 'games_total': 0, 'titles': [title], 'titles_total': 1})
        self.assertEqual(window.profile_name.text(), 'pal')
        self.assertEqual(window.level_label.text(), 'Level 12 · Bronze')
        self.assertEqual(window.profile_friends.text(), 'Private')
        self.assertEqual(window.profile_about.textFormat(), desktop.Qt.TextFormat.PlainText)
        self.assertEqual(window.profile_hint.text(), 'No games played yet.')
        self.assertIn('Add friend', [b.text() for b in window.findChildren(desktop.Button) if b.isVisibleTo(window)])
        window.profile_tab(1)
        self.assertEqual(window.titles_list.count(), 1)
        window.title_clicked(window.titles_list.item(0))
        window.social_result('title-trophies', ('2', 'NPWR1', social.merge_trophies(
            [{'trophyId': 0, 'trophyName': 'Win', 'trophyType': 'gold'}], [{'trophyId': 0, 'earned': True}])))
        self.assertEqual((window.trophy_list.count(), window.profile_hint.text()), (1, '1 of 1 trophies earned'))
        window.social_result('profile', {'account_id': '9', 'online_id': 'other'})  # stale result is ignored
        self.assertEqual(window.profile_name.text(), 'pal')
        window.rail.select(window.profile_return)
        self.assertEqual(window.pages.currentIndex(), desktop.FRIENDS)


if __name__ == '__main__':
    unittest.main()
