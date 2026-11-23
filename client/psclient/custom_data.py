"""Party member metadata in `customData4`: a base64 JSON list of ten strings.

Index 5 is "1" while the mic is muted, and index 8 holds the bridge info
`bridgeId\npeerId` once a member is on the RTC bridge.
"""
import base64
import json

BRIDGE_INDEX = 8


def format_bridge_info(bridge_id: str, peer_id: str) -> str:
    return f"{bridge_id}\n{peer_id}"


def parse_bridge_info(value: str) -> tuple[str, str] | None:
    bridge_id, sep, peer_id = value.partition("\n")
    if not sep or not bridge_id or not peer_id:
        return None
    return bridge_id, peer_id


def metadata(bridge_info: str = "") -> str:
    """The initial customData4, with optional bridge info at index 8."""
    fields = ["", "", "1", "0", "6.2.0", "0", "0", "0", bridge_info, ""]
    return base64.b64encode(json.dumps(fields, separators=(",", ":")).encode()).decode()


def metadata_fields(value: str | None) -> list:
    try:
        fields = json.loads(base64.b64decode(value or ""))
    except ValueError:
        return []
    return fields if isinstance(fields, list) else []
