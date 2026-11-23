"""Push notification WebSocket messages.

The client first fetches a bootstrap record ({fqdn, retryIntervalMin/Max,
keepAliveStatus}), then connects to wss://{fqdn}/np/pushNotification and sends
text JSON messages carrying `version`, `method` and `tranId`.
"""

import json

CHANGE_USER_STATUS = 1003
CHANGE_KEEP_ALIVE_STATUS_TYPE = 1007


def socket_url(fqdn: str) -> str:
    return f"wss://{fqdn}/np/pushNotification"


def change_user_status(version: str, tran_id: str, account_id: str, access_token: str, active: bool = True) -> str:
    return json.dumps({
        "version": version,
        "method": CHANGE_USER_STATUS,
        "tranId": tran_id,
        "accountId": int(account_id),
        "status": "active" if active else "",
        "auth": {"accessToken": access_token},
    })


def change_keep_alive_status_type(version: str, tran_id: str, status_type: int) -> str:
    if status_type not in (3, 6):
        raise ValueError("the app only sends keepAliveStatusType 3 or 6")
    return json.dumps({
        "version": version,
        "method": CHANGE_KEEP_ALIVE_STATUS_TYPE,
        "tranId": tran_id,
        "keepAliveStatusType": status_type,
    })


def parse_packet(text: str) -> dict:
    """Return {version, dataType, contexts, body, payload} from a push packet."""
    msg = json.loads(text)
    return {key: msg.get(key) for key in ("version", "dataType", "contexts", "body", "payload")}
