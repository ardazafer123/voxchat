"""Minimal HTTPS transport for the web API (Bearer token).

Resolves the API group to the bundle
default base URL and attaches the access token. No ret/retry logic yet.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE_URLS = {
    "sessionManager": "https://m.np.playstation.com/api/sessionManager",
    "rtcBridge": "https://m.np.playstation.com/api/rtcBridge",
    "gamingLoungeGroups": "https://m.np.playstation.com/api/gamingLoungeGroups",
}


@dataclass
class Response:
    status: int
    body: object  # parsed JSON, or raw text if not JSON
    headers: dict = None  # response headers


def _send(method: str, url: str, token: str, body: bytes | None,
          extra_headers: dict | None) -> Response:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Accept-Language": "en-US",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
            hdrs = dict(resp.headers)
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", "replace")
        status = err.code
        hdrs = dict(err.headers) if err.headers else {}
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return Response(status=status, body=parsed, headers=hdrs)


def get(group: str, path: str, token: str, query: str = "",
        extra_headers: dict | None = None) -> Response:
    url = BASE_URLS[group] + path + (("?" + query) if query else "")
    return _send("GET", url, token, None, extra_headers)


def get_url(url: str, token: str, extra_headers: dict | None = None) -> Response:
    """GET an absolute URL (for ASM base-URL resolution, etc.)."""
    return _send("GET", url, token, None, extra_headers)


def send_spec(spec, token: str, extra_headers: dict | None = None) -> Response:
    """Send a psclient.rest.RequestSpec (method/group/path/query/headers/body)."""
    import json as _json

    url = BASE_URLS[spec.group] + spec.path
    if spec.query:
        url += "?" + urllib.parse.urlencode(spec.query)
    headers = dict(spec.headers or {})
    if extra_headers:
        headers.update(extra_headers)
    body = _json.dumps(spec.body).encode() if spec.body is not None else None
    return _send(spec.method, url, token, body, headers)


ASM_BASEURL = "https://asm.np.community.playstation.net/asm/v1/apps/me/baseUrls/"


def resolve_base_url(group: str, token: str) -> Response:
    return get_url(ASM_BASEURL + group, token)
