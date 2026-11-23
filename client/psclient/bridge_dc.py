"""JSON protocol on the WebRTC data channel labelled "bridge".

Client requests:  {"category":"request","type":<api>,"id":N,"payload":{...}}
Responses:        {"category":"response","type":<api>,"id":N,"payload":{...}}
Errors:           {"category":"response","error":{"id":N,"code":str,"message":str}}
Server events:    {"type":<event>,"payload":{...}}
"""

import json
from dataclasses import dataclass, field

LABEL = "bridge"

CELT_24 = {"codec": "CELT", "bitrate": 24}
CELT_40 = {"codec": "CELT", "bitrate": 40}

EVENT_TYPES = (
    "bridgeDeleted",
    "peerCreated",
    "peerDeleted",
    "offerUpdated",
    "dataChannelCreated",
    "dataChannelDeleted",
    "maintenanceScheduled",
)


class RequestIds:
    """Per-connection counter; the app's first id is 1."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last += 1
        return self._last


def _request(api: str, request_id: int, payload: dict) -> str:
    return json.dumps(
        {"category": "request", "type": api, "id": request_id, "payload": payload},
        separators=(",", ":"),
    )


def set_media_pub_sub_peers(request_id: int, peer_ids: list[str]) -> str:
    # The app passes the same remote peer list for both keys.
    return _request(
        "setMediaPubSubPeers",
        request_id,
        {"mediaSubscriptionPeers": list(peer_ids), "mediaPublishmentPeers": list(peer_ids)},
    )


def set_floor_control_parameters(request_id: int, max_audio_streams: int = 8) -> str:
    return _request(
        "setFloorControlParameters",
        request_id,
        {
            "algorithm": "sieVadAudioLevelBased01",
            "parameters": {"maxAudioStreams": max(8, max_audio_streams)},
        },
    )


def set_receiving_capability(request_id: int, celt24: bool = True, celt40: bool = True) -> str:
    opus = ([CELT_24] if celt24 else []) + ([CELT_40] if celt40 else [])
    return _request(
        "setReceivingCapability",
        request_id,
        {"receivingCapability": {"audio": {"opus": opus}}},
    )


def set_all_muted_peer(request_id: int, muted: bool) -> str:
    return _request("setAllMutedPeer", request_id, {"muted": muted})


@dataclass
class Response:
    api: str
    request_id: int
    payload: dict


@dataclass
class ErrorResponse:
    request_id: int
    code: str
    message: str


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)

    @property
    def peer_id(self) -> str | None:
        return self.payload.get("peer", {}).get("peerId")


def parse_frame(text: str) -> Response | ErrorResponse | Event:
    """Classify one frame received on the bridge channel."""
    msg = json.loads(text)
    if msg.get("category") == "response":
        if isinstance(msg.get("type"), str):
            return Response(msg["type"], int(msg["id"]), msg.get("payload", {}))
        err = msg["error"]
        return ErrorResponse(int(err["id"]), str(err.get("code", "")), str(err.get("message", "")))
    if msg.get("type") in EVENT_TYPES:
        return Event(msg["type"], msg.get("payload", {}))
    raise ValueError(f"unknown bridge frame: {text[:200]}")
