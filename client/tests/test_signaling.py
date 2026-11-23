import json
import unittest

from psclient import signaling


class SignalingTest(unittest.TestCase):
    def test_header_layout(self):
        # Real-world header layout with a made-up account id; body synthetic.
        header = bytes.fromhex('010000000000000001000000000000004ef330a64b9bb601a1860100000000000000000000000000c8080000')
        import base64
        sender, platform, data = signaling.unwrap(signaling.PREFIX + base64.b64encode(header + b'x' * 2248).decode())
        self.assertEqual((sender, platform, len(data)), ('123456789012345678', 100001, 2248))
        self.assertEqual(signaling.wrap(sender, data), signaling.PREFIX + base64.b64encode(header + data).decode())

    def test_multipart_and_identity(self):
        message = signaling.description(signaling.identity('123'), signaling.identity('456'), 'v=0\r\n' * 100, 'answer', 1, 2)
        decoder = signaling.Decoder()
        parts = signaling.payloads(message, max_frame=80)
        self.assertGreater(len(parts), 1)
        for part in parts[:-1]:
            self.assertIsNone(decoder.feed(part))
        self.assertEqual(decoder.feed(parts[-1]), (1, message))
        bad = signaling.wrap('789', b'1/1/1\n' + json.dumps(message).encode())
        with self.assertRaisesRegex(ValueError, 'sender disagree'):
            decoder.feed(bad)

    def test_truncation_and_unknown_profile(self):
        import base64
        raw = signaling.HEADER.pack(2, 0, 1, 0, 123, 100001, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'profile'):
            signaling.unwrap(signaling.PREFIX + base64.b64encode(raw).decode())
        with self.assertRaises(ValueError):
            signaling.unwrap(signaling.PREFIX + 'AA==')

    def test_out_of_order_cannot_produce_message(self):
        decoder = signaling.Decoder()
        parts = signaling.payloads(signaling.description(signaling.identity('123'), signaling.identity('456'), 'x' * 100, 'offer', 1, 1), 80)
        for part in reversed(parts):
            self.assertIsNone(decoder.feed(part))
