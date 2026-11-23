import unittest

from live.voice import created_session_id


class CreatedSessionTest(unittest.TestCase):
    def test_created_session_id(self):
        response = {"v2PartySessions": [{"sessionId": "new-session", "members": []}]}
        self.assertEqual(created_session_id(response), "new-session")
        for invalid in (None, {}, {"v2PartySessions": []}, {"v2PartySessions": [None]}):
            self.assertIsNone(created_session_id(invalid))


if __name__ == "__main__":
    unittest.main()
