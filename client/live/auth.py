"""Sign-in for the desktop client.

Everything in `psclient/` stays offline; code that talks to the network lives in `live/`.

Flow (the public web sign-in flow, also used by open-source libraries such as psn-api):

  1. You sign in on the account website in a browser and copy the `npsso`
     cookie value into the app (or the PSN_NPSSO env var).
  2. exchange_npsso_for_code(): GET the authorize endpoint with that cookie and
     `prompt=none`; the server answers with a redirect holding a short-lived `?code=`.
  3. exchange_code_for_token(): POST that code to the token endpoint, which
     returns an access token (and refresh token).

The client id, redirect URI, scope and Basic-auth string below are the public
web-flow values that open-source libraries already ship.

Unofficial clients break the network's terms of service and can get an account
banned. The NPSSO is a credential: keep it local, never share or commit it.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar

# Public web-flow client registration (same values as psn-api).
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
SCOPE = "psn:mobile.v2.core psn:clientapp"
SERVICE_ENTITY = "urn:service-entity:psn"
BASIC_AUTH = "MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="

# Device id sent on authorize: fixed prefix plus random hex, 122 hex characters.
import secrets as _secrets
DUID = "00000007000801a8" + _secrets.token_hex(53)

AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"

# PSCLIENT_CONFIG_DIR lets a test copy start with empty sign-ins without changing HOME.
CONFIG_DIR = os.environ.get("PSCLIENT_CONFIG_DIR") or os.path.expanduser("~/.config/psclient")
DEFAULT_NPSSO_PATH = os.path.join(CONFIG_DIR, "npsso")


class AuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


ACCOUNTS_DIR = os.path.join(CONFIG_DIR, "accounts")
ACTIVE_PATH = os.path.join(CONFIG_DIR, "active")


def _account_path(account_id: str) -> str:
    if not account_id.isdigit():
        raise AuthError("Invalid account id")
    return os.path.join(ACCOUNTS_DIR, account_id + ".json")


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _read_account(account_id: str) -> dict | None:
    try:
        with open(_account_path(account_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, AuthError):
        return None


def list_accounts() -> list[dict]:
    """Saved accounts as {account_id, online_id}; NPSSO values stay on disk."""
    accounts = []
    if os.path.isdir(ACCOUNTS_DIR):
        for name in os.listdir(ACCOUNTS_DIR):
            account_id = name.removesuffix(".json")
            data = _read_account(account_id) if name.endswith(".json") else None
            if data:
                accounts.append({"account_id": account_id, "online_id": data.get("online_id", account_id)})
    return sorted(accounts, key=lambda a: a["online_id"].lower())


def active_account() -> str | None:
    try:
        with open(ACTIVE_PATH, encoding="utf-8") as fh:
            account_id = fh.read().strip()
    except OSError:
        return None
    return account_id if _read_account(account_id) else None


def set_active(account_id: str) -> None:
    if not _read_account(account_id):
        raise AuthError("Account is not saved on this computer")
    _write_private(ACTIVE_PATH, account_id)


def save_account(account_id: str, online_id: str, npsso: str) -> None:
    """Store a verified sign-in. Saving records the ban-risk acknowledgement."""
    _write_private(_account_path(account_id), json.dumps(
        {"online_id": online_id, "npsso": npsso, "acknowledged": True}))
    set_active(account_id)


def remove_account(account_id: str) -> None:
    """Sign out locally: delete the saved NPSSO. It does not revoke it on the server."""
    try:
        os.remove(_account_path(account_id))
    except FileNotFoundError:
        pass
    if active_account() is None:
        try:
            os.remove(ACTIVE_PATH)
        except FileNotFoundError:
            pass


def is_acknowledged(account_id: str) -> bool:
    data = _read_account(account_id) if account_id else None
    return bool(data and data.get("acknowledged"))


def adopt_legacy_npsso(account_id: str, online_id: str) -> bool:
    """Move the old single-file NPSSO into the account store, once verified."""
    if os.environ.get("PSN_NPSSO") or not os.path.exists(DEFAULT_NPSSO_PATH) or active_account():
        return False
    with open(DEFAULT_NPSSO_PATH, encoding="utf-8") as fh:
        npsso = fh.read().strip()
    save_account(account_id, online_id, npsso)
    os.remove(DEFAULT_NPSSO_PATH)
    return True


def load_npsso() -> str:
    """Read the NPSSO from PSN_NPSSO, the active saved account, else the legacy file."""
    val = os.environ.get("PSN_NPSSO")
    active = None if val else active_account()
    if active:
        val = (_read_account(active) or {}).get("npsso", "")
    if not val and os.path.exists(DEFAULT_NPSSO_PATH):
        with open(DEFAULT_NPSSO_PATH, encoding="utf-8") as fh:
            val = fh.read().strip()
    if not val:
        raise AuthError(
            "No NPSSO found. Put it in PSN_NPSSO or " + DEFAULT_NPSSO_PATH
        )
    return val.strip()


def exchange_npsso_for_code(npsso: str) -> str:
    """Return the short-lived authorization code from the authorize redirect."""
    params = {
        "access_type": "offline",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "service_entity": SERVICE_ENTITY,
        "duid": DUID,
    }
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Cookie": f"npsso={npsso}"})

    # Do NOT follow the redirect; the code is in the Location header.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPCookieProcessor(CookieJar()))
    try:
        resp = opener.open(req, timeout=30)
        location = resp.headers.get("Location", "")
    except urllib.error.HTTPError as err:
        location = err.headers.get("Location", "") if err.headers else ""
        if not location:
            raise AuthError(f"authorize failed: HTTP {err.code}") from err

    query = urllib.parse.urlparse(location).query
    code = urllib.parse.parse_qs(query).get("code", [None])[0]
    if not code:
        raise AuthError(
            "No code in redirect. NPSSO is likely expired or wrong "
            "(they last ~2 months). Location=" + location[:120]
        )
    return code


def exchange_code_for_token(code: str) -> Tokens:
    """Exchange the authorization code for an access token."""
    body = urllib.parse.urlencode({
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "token_format": "jwt",
        "duid": DUID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": "Basic " + BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:200]
        raise AuthError(f"token failed: HTTP {err.code} {detail}") from err

    return Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_in=int(data.get("expires_in", 0)),
        token_type=data.get("token_type", "bearer"),
    )


def sign_in(npsso: str | None = None) -> Tokens:
    """Full flow: NPSSO (given, else local) -> code -> tokens."""
    return exchange_code_for_token(exchange_npsso_for_code(npsso or load_npsso()))
