"""REST request specs for the party and RTC bridge APIs.

Each function returns a `RequestSpec` describing method, API group, path,
query, extra headers and JSON body. Sending is left to a transport, which must
add `Authorization: Bearer <access token>` and resolve the API group to a base
URL (bundle default: https://m.np.playstation.com/api/<group>).
"""

from dataclasses import dataclass, field

RTC_BRIDGE = "rtcBridge"
SESSION_MANAGER = "sessionManager"

DEFAULT_BASE_URLS = {
    RTC_BRIDGE: "https://m.np.playstation.com/api/rtcBridge",
    SESSION_MANAGER: "https://m.np.playstation.com/api/sessionManager",
}

JSON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",  # exact literal
    "Accept": "application/json",
}


@dataclass
class RequestSpec:
    method: str
    group: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: dict | None = None

    def url(self, base_urls: dict = DEFAULT_BASE_URLS) -> str:
        from urllib.parse import urlencode

        url = base_urls[self.group] + self.path
        return url + ("?" + urlencode(self.query) if self.query else "")


def media_types(celt24: bool = True, celt40: bool = True) -> dict:
    def entry(bitrate: int) -> dict:
        return {"codec": "CELT", "bitrate": bitrate, "channels": 1, "ptime": 40, "samplingRates": [16000]}

    opus = ([entry(24)] if celt24 else []) + ([entry(40)] if celt40 else [])
    return {"audio": {"isRequired": True, "opus": opus}, "application": {"isRequired": True}}


def _bridge_headers(title_id: str | None, bridge_token: str | None = None) -> dict:
    # The title-id header is omitted when empty; its value is unknown.
    headers = dict(JSON_HEADERS)
    if title_id:
        headers["X-PSN-RTC-TITLE-ID"] = title_id
    if bridge_token is not None:
        headers["X-PSN-BRIDGE-TOKEN"] = bridge_token
    return headers


# RTC bridge

def create_bridge(title_id: str) -> RequestSpec:
    return RequestSpec("POST", RTC_BRIDGE, "/v1/bridges", headers=_bridge_headers(title_id), body={})


def create_peer(bridge_id: str, bridge_token: str, title_id: str | None, allow_duid_duplication: bool = False) -> RequestSpec:
    return RequestSpec(
        "POST",
        RTC_BRIDGE,
        f"/v1/bridges/{bridge_id}/peers",
        query={"allow_duid_duplication": str(allow_duid_duplication).lower(), "g": "2"},
        headers=_bridge_headers(title_id, bridge_token),
        body={"mediaTypes": media_types()},
    )


def request_offer(bridge_id: str, peer_id: str, bridge_token: str, title_id: str | None) -> RequestSpec:
    return RequestSpec(
        "POST",
        RTC_BRIDGE,
        f"/v1/bridges/{bridge_id}/peers/{peer_id}/offer",
        headers=_bridge_headers(title_id, bridge_token),
        body={"mediaTypes": media_types()},
    )


def send_answer(bridge_id: str, peer_id: str, bridge_token: str, title_id: str | None, sdp: str) -> RequestSpec:
    return RequestSpec(
        "POST",
        RTC_BRIDGE,
        f"/v1/bridges/{bridge_id}/peers/{peer_id}/answer",
        headers=_bridge_headers(title_id, bridge_token),
        body={"answer": {"sdp": sdp}},
    )


def get_bridge(bridge_id: str, bridge_token: str, title_id: str | None) -> RequestSpec:
    # Bridge GET; seeds the peers that were on the bridge before us.
    return RequestSpec("GET", RTC_BRIDGE, f"/v1/bridges/{bridge_id}",
                       query={"fields": "bridgeId,peer.peerId,peer.msids,peer.dataChannels"},
                       headers=_bridge_headers(title_id, bridge_token))


def bridge_peer_ids(body: dict) -> list[str]:
    bridge = body.get("bridge", body)
    return [str(p["peerId"]) for p in bridge.get("peers", []) if p.get("peerId")]


def delete_peer(bridge_id: str, peer_id: str, title_id: str | None = None) -> RequestSpec:
    # Leaving deletes only our own peer; the bridge belongs to its creator.
    return RequestSpec("DELETE", RTC_BRIDGE, f"/v1/bridges/{bridge_id}/peers/{peer_id}",
                       headers=_bridge_headers(title_id))


# Party sessions

def new_context_id() -> str:
    """Client-generated push context GUID (the app uses xg::newGuid)."""
    import uuid

    return str(uuid.uuid4())


def member_object(push_context_id: str, voice_chat: bool = True) -> dict:
    # The server expects literal "me" for both identity fields.
    # The app sends its context GUID only in pushContexts; the desktop's extra
    # create-level contextId is tolerated, not required.
    return {
        "accountId": "me",
        "platform": "me",
        "voiceChatActivated": voice_chat,
        "isOnShareScreen": False,
        "pushContexts": [{"contextId": push_context_id}],
    }


def create_party(context_id: str, member: dict, joinable: str = "FRIENDS",
                 visibility: str = "FRIENDS_ONLY", max_members: int = 8,
                 session_name: str = "Party") -> RequestSpec:
    # v2PartySessions is an ARRAY, not an object.
    # The app also successfully sends an empty sessionName for an open party.
    return RequestSpec(
        "POST",
        SESSION_MANAGER,
        "/v2/partySessions",
        headers=dict(JSON_HEADERS),
        body={
            "v2PartySessions": [{
                "contextId": context_id,
                "sessionName": session_name,
                "sessionType": "ROOM",
                "maxMembers": max_members,
                "members": [member],
                "joinableUserType": joinable,
                "visibilityType": visibility,
            }]
        },
    )


def join_party(session_id: str, member: dict) -> RequestSpec:
    # Join body (HTTP 201): only a members array at the root. The context
    # belongs inside the member's pushContexts; no top-level contextId was sent.
    return RequestSpec(
        "POST",
        SESSION_MANAGER,
        f"/v2/partySessions/{session_id}/members",
        headers=dict(JSON_HEADERS),
        body={"members": [member]},
    )


def invite_to_party(session_id: str, account_ids: list[str]) -> RequestSpec:
    # invitationsToGroups omitted: no group invites.
    return RequestSpec("POST", SESSION_MANAGER, f"/v2/partySessions/{session_id}/invitations",
                       headers=dict(JSON_HEADERS), body={
                           "invitations": [{"to": {"accountId": str(a)}} for a in account_ids],
                           "invitationType": "NORMAL",
                       })


def get_party(session_id: str) -> RequestSpec:
    # Runtime: collection route + ID header; view=v3 includes owner and the
    # current member schema. Without it, the server returns a reduced view.
    return RequestSpec("GET", SESSION_MANAGER, "/v2/partySessions", query={"view": "v3"}, headers={
        **JSON_HEADERS, "X-PSN-SESSION-MANAGER-SESSION-IDS": session_id,
    })


def attach_bridge(session_id: str, bridge_id: str, bridge_token: str) -> RequestSpec:
    return RequestSpec(
        "POST",
        SESSION_MANAGER,
        f"/v2/partySessions/{session_id}/bridges",
        headers=dict(JSON_HEADERS),
        body={"bridges": [{"bridgeId": bridge_id, "bridgeToken": bridge_token}]},
    )


def publish_bridge_peer(session_id: str, account_id: str, bridge_id: str, peer_id: str) -> RequestSpec:
    # Member PATCH bodies hold one property. Bridge info goes in customData4[8].
    from .custom_data import format_bridge_info, metadata

    return RequestSpec(
        "PATCH",
        SESSION_MANAGER,
        f"/v2/partySessions/{session_id}/members/{account_id}",
        headers=dict(JSON_HEADERS),
        body={"customData4": metadata(format_bridge_info(bridge_id, peer_id))},
    )


def leave_party(session_id: str, account_id: str) -> RequestSpec:
    return RequestSpec("DELETE", SESSION_MANAGER, f"/v2/partySessions/{session_id}/members/{account_id}")


def custom_message(session_id: str, account_id: str, payload: str,
                   platform: str = "MOBILE_APP") -> RequestSpec:
    return RequestSpec("POST", SESSION_MANAGER,
                       f"/v2/partySessions/{session_id}/customMessage",
                       headers=dict(JSON_HEADERS), body={
                           "channel": "miranda:12", "payload": payload,
                           "to": [{"accountId": account_id, "platform": platform}],
                           "withoutSequenceNumber": True,
                       })


def patch_member(session_id: str, property_name: str, value) -> RequestSpec:
    if property_name not in ("voiceChatActivated", "customData3", "customData4"):
        raise ValueError("Unsupported member property")
    return RequestSpec("PATCH", SESSION_MANAGER,
                       f"/v2/partySessions/{session_id}/members/me",
                       headers=dict(JSON_HEADERS), body={property_name: value})


# Response helpers

def parse_peer_response(body: dict) -> tuple[str, dict | None]:
    """Return (peerId, offer or None) from a POST /peers response."""
    peer = body["peer"]
    offer = peer.get("offer")
    if offer and "sdp" in offer and "mids" in offer:
        return peer["peerId"], offer
    return peer["peerId"], None


def bridge_from_session(session: dict) -> tuple[str, str] | None:
    """Return (bridgeId, bridgeToken) from a party-session GET body, if present."""
    bridges = session.get("bridges") or []
    if not bridges:
        return None
    return bridges[0]["bridgeId"], bridges[0]["bridgeToken"]
