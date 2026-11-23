"""Framing for party session messages.

Frame: "<type>/<part>/<total>\\n" + payload chunk. Parts count from 1. Senders
reserve 22 bytes for the header, so a chunk holds at most `max_size - 22` bytes.

Types: 1 = P2P offer/answer/sdp_updated, 2 = bridge user_table_updated,
3 = topology (bridge fallback, start reconnection).
"""

HEADER_RESERVE = 22


def encode(msg_type: int, payload: bytes, max_size: int) -> list[bytes]:
    chunk = max_size - HEADER_RESERVE
    if chunk <= 0:
        raise ValueError("max_size too small")
    parts = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
    total = len(parts)
    return [f"{msg_type}/{n}/{total}\n".encode() + part for n, part in enumerate(parts, 1)]


def decode(frame: bytes) -> tuple[int, int, int, bytes]:
    header, sep, payload = frame.partition(b"\n")
    if not sep:
        raise ValueError("missing newline")
    msg_type, part, total = (int(x) for x in header.decode().split("/"))
    if not 1 <= part <= total:
        raise ValueError("bad part numbers")
    return msg_type, part, total, payload


class Reassembler:
    """Rebuild messages per sender; parts must arrive in order, as in the app."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[int, int, int, bytearray]] = {}

    def feed(self, sender: str, frame: bytes) -> tuple[int, bytes] | None:
        msg_type, part, total, payload = decode(frame)
        if part == 1:
            self._pending[sender] = (msg_type, 1, total, bytearray(payload))
        else:
            state = self._pending.get(sender)
            if not state or state[0] != msg_type or state[2] != total or state[1] + 1 != part:
                self._pending.pop(sender, None)
                return None
            state[3].extend(payload)
            self._pending[sender] = (msg_type, part, total, state[3])
        if part == total:
            _, _, _, data = self._pending.pop(sender)
            return msg_type, bytes(data)
        return None
