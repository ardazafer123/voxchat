"""Sign in, then read your own profile. Nothing party-related.

Run:
    cd client
    python3 -m live.whoami

It prints your online ID and account id, or a clear error. One GET to the
profile endpoint; it does not create, join or touch any party.

The NPSSO stays in a local file / PSN_NPSSO; it is
never printed here, and the access token is only summarised, not shown.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request

from live.auth import AuthError, sign_in

# The "me" alias is rejected on this deployment ("path: accountId"); query by id.
PROFILE_URL = (
    "https://m.np.playstation.com/api/userProfile/v1/internal/users"
    "/{account_id}/profiles"
)


def account_id_from_token(access_token: str) -> str:
    """Read the `sub` (account id) claim from the JWT access token payload.

    No signature check — we only read our own token's claims locally.
    """
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as err:  # noqa: BLE001
        raise AuthError(f"token is not a readable JWT: {err}") from err
    account_id = claims.get("sub") or claims.get("account_id")
    if not account_id:
        raise AuthError("no account id (sub) in token claims")
    return str(account_id)


def get_own_profile(access_token: str, account_id: str) -> dict:
    req = urllib.request.Request(
        PROFILE_URL.format(account_id=account_id),
        headers={
            "Authorization": "Bearer " + access_token,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    try:
        tokens = sign_in()
    except AuthError as err:
        print("Sign-in failed:", err, file=sys.stderr)
        return 1

    print(f"Got an access token (expires in {tokens.expires_in}s). "
          f"Refresh token present: {bool(tokens.refresh_token)}.")

    account_id = account_id_from_token(tokens.access_token)
    print("Account ID (from token):", account_id)

    try:
        profile = get_own_profile(tokens.access_token, account_id)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        print(f"Profile request failed: HTTP {err.code} {detail}",
              file=sys.stderr)
        return 2

    prof = profile.get("profile", profile)
    print("Online ID:", prof.get("onlineId"))
    print("\nWorks. Sign-in and the web API both accept this token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
