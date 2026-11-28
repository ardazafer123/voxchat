"""Binary envelope (profile v1) for party session messages and P2P message construction.

Implements the signaling channel and data type only.
"""
import base64
import json
import struct

from .division import Reassembler, encode

PREFIX = 'ver=1.0, type=binary, body='
HEADER = struct.Struct('<IIIIQIIQI')
MOBILE = 100001
MAX_MESSAGE = 256 * 1024
MAX_FRAME = 0xf00  # Receiver's payload limit.


def wrap(sender: str, frame: bytes, platform: int = MOBILE) -> str:
    if len(frame) > MAX_FRAME:
        raise ValueError('Signaling frame too large')
    header = HEADER.pack(1, 0, 1, 0, int(sender), platform, 0, 0, len(frame))
    return PREFIX + base64.b64encode(header + frame).decode('ascii')


def share_request(sender: str, mode: int = 2, platform: int = MOBILE) -> str:
    """Ask a member who shares their screen to send the full stream (data type 4, u32 2 = start watching)."""
    header = HEADER.pack(1, 0, 4, 0, int(sender), platform, 0, 0, 4)
    return PREFIX + base64.b64encode(header + struct.pack('<I', mode)).decode('ascii')


def unwrap(payload: str) -> tuple[str, int, bytes]:
    if not isinstance(payload, str) or not payload.startswith(PREFIX):
        raise ValueError('Unsupported signaling envelope')
    if len(payload) > (MAX_MESSAGE + HEADER.size) * 4 // 3 + 100:
        raise ValueError('Signaling envelope too large')
    raw = base64.b64decode(payload[len(PREFIX):], validate=True)
    if len(raw) < HEADER.size:
        raise ValueError('Truncated signaling header')
    channel, sequence, kind, r0, sender, platform, r1, r2, size = HEADER.unpack_from(raw)
    if (channel, kind, r0, r1, r2) != (1, 1, 0, 0, 0):
        raise ValueError('Unsupported signaling header profile')
    if size > MAX_FRAME or size != len(raw) - HEADER.size or not sender or not platform:
        raise ValueError('Signaling payload length mismatch')
    return str(sender), platform, raw[HEADER.size:]


class Decoder:
    def __init__(self):
        self.parts = Reassembler()
        self._sizes = {}

    def feed(self, payload: str):
        sender, platform, frame = unwrap(payload)
        from .division import decode
        _, part, _, chunk = decode(frame)
        key = f'{sender}/{platform}'
        size = len(chunk) + (self._sizes.get(key, 0) if part != 1 else 0)
        if size > MAX_MESSAGE:
            self.parts._pending.pop(key, None)
            self._sizes.pop(key, None)
            raise ValueError('Reassembled signaling message too large')
        self._sizes[key] = size
        result = self.parts.feed(key, frame)
        if result is None:
            return None
        self._sizes.pop(key, None)
        kind, data = result
        message = json.loads(data)
        identity = message.get('from', {})
        if identity != {'account_id': sender, 'platform': platform}:
            raise ValueError('Envelope and message sender disagree')
        return kind, message


def identity(account_id: str, platform: int = MOBILE):
    return {'account_id': str(account_id), 'platform': platform}


def description(sender, recipient, sdp, kind, sequence, global_sequence, mid='0', msid='0'):
    if kind not in ('offer', 'answer'):
        raise ValueError('Expected offer or answer')
    return {
        'from': sender, 'to': recipient, 'users': [sender],
        'medias': [{'user': sender, 'msids': [msid],
                    'media_records': [{'media_identifier': 'voice_chat', 'mid': mid}]}],
        'global_seq_num': global_sequence, 'sdp_sequence_number': str(sequence),
        'type': kind, 'sdp': sdp,
    }


def payloads(message, max_frame=3000):
    data = json.dumps(message, separators=(',', ':')).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError('Signaling message too large')
    return [wrap(message['from']['account_id'], part, message['from']['platform'])
            for part in encode(1, data, max_frame)]
