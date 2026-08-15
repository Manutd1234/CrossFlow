"""Who is calling, and what may they do.

Two rules govern everything in this module.

*The role is never taken from the request.*  Not from the body, not from a
query parameter, and not from the token's own ``user_metadata`` -- users can
write their own metadata through the Supabase client SDK, so a role claimed
there is a role the user granted themselves.  It is read from
``crossflow_profiles`` on every request.

*The caller's own token does the reading.*  Profile lookups go through
:mod:`auth.transport`, so the row-level security policies in ``schema.sql``
decide what comes back.  The service-role key is never involved in a
user-scoped read; if it were, every policy would be inert and the failure would
be invisible in testing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Header, HTTPException

from auth import transport


AUTH_MODE_ENV = "CROSSFLOW_AUTH_MODE"
PROFILES_TABLE = "crossflow_profiles"

# Successful verifications are cached briefly so a chatty client does not force
# a network round trip per request. Kept short deliberately: the window is also
# how long a signed-out or deleted user keeps working.
VERIFICATION_TTL_SECONDS = 60.0
MAX_CACHE_ENTRIES = 512

VERIFY_TIMEOUT_SECONDS = 5.0
PROFILE_TIMEOUT_SECONDS = 5.0

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}

# One message for "no such journey" and "not your journey" alike. Any wording
# difference between the two tells an attacker which IDs exist.
NOT_YOURS_DETAIL = "No such record for this account."

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, "AuthenticatedUser"]] = {}


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """An identity the server resolved, never one the client asserted."""

    id: str
    role: str
    display_name: str
    access_token: str
    expires_at: Optional[int]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def auth_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=dict(_NO_STORE_HEADERS),
    )


def not_yours_error() -> HTTPException:
    """Identical response for a record that is absent and one that is another's.

    WP10's driver lookup must use this rather than distinguishing the cases.
    """
    return auth_error(404, NOT_YOURS_DETAIL)


def auth_enabled() -> bool:
    """Require an explicit opt-in, matching the other optional Supabase stores."""
    return (
        os.environ.get(AUTH_MODE_ENV, "disabled").strip().casefold() == "supabase"
    )


def auth_configured() -> bool:
    return auth_enabled() and transport.load_user_config() is not None


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _bearer_token(authorization: Optional[str]) -> str:
    """Extract a compact JWS from an Authorization header.

    CROSSFLOW_ADMIN_TOKEN is a shared machine secret, not a JWT, so presenting
    it here fails the format check below. That is the intended outcome: the two
    mechanisms must never authenticate each other.
    """
    if not authorization or not isinstance(authorization, str):
        raise auth_error(401, "An access token is required.")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        raise auth_error(401, "An access token is required.")
    try:
        return transport.validate_access_token(parts[1].strip())
    except transport.SupabaseUserError:
        raise auth_error(401, "The access token is not valid.") from None


def _token_expiry(access_token: str) -> Optional[int]:
    """Read ``exp`` from an already-verified token, only to bound the cache.

    This is not a verification step and must never be treated as one. Supabase
    has already validated the signature by the time this is called; the claim
    is read solely so a cache entry cannot outlive the token itself.
    """
    try:
        payload_segment = access_token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        claims = json.loads(decoded.decode("utf-8"))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        return None
    return expiry


def _cache_key(access_token: str) -> str:
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def _cached(key: str) -> Optional[AuthenticatedUser]:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        expires_at, user = entry
        if expires_at <= now:
            _CACHE.pop(key, None)
            return None
        return user


def _remember(key: str, user: AuthenticatedUser) -> None:
    ttl = VERIFICATION_TTL_SECONDS
    if user.expires_at is not None:
        remaining = user.expires_at - time.time()
        if remaining <= 0:
            return
        ttl = min(ttl, remaining)
    with _CACHE_LOCK:
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            now = time.monotonic()
            for stale_key, (expires_at, _user) in list(_CACHE.items()):
                if expires_at <= now:
                    _CACHE.pop(stale_key, None)
            if len(_CACHE) >= MAX_CACHE_ENTRIES:
                _CACHE.clear()
        _CACHE[key] = (time.monotonic() + ttl, user)


def _config() -> transport.SupabaseUserConfig:
    if not auth_enabled():
        raise auth_error(503, "Authentication is not enabled on this server.")
    config = transport.load_user_config()
    if config is None:
        raise auth_error(503, "Authentication is not configured on this server.")
    return config


def _translate(error: transport.SupabaseUserError) -> HTTPException:
    """Upstream rejection is the caller's fault; anything else is ours."""
    code = error.code
    if code in {"http_401", "http_403"}:
        return auth_error(401, "The access token is not valid.")
    if code == "invalid_access_token":
        return auth_error(401, "The access token is not valid.")
    return auth_error(503, "The authentication service is unavailable.")


def _verify_remote(
    config: transport.SupabaseUserConfig,
    access_token: str,
) -> dict[str, Any]:
    try:
        document = transport.request_json(
            config,
            access_token=access_token,
            method="GET",
            path="/auth/v1/user",
            timeout_seconds=VERIFY_TIMEOUT_SECONDS,
        )
    except transport.SupabaseUserError as error:
        raise _translate(error) from None
    if not isinstance(document, dict):
        raise auth_error(503, "The authentication service is unavailable.")
    user_id = document.get("id")
    if not isinstance(user_id, str) or not _UUID.fullmatch(user_id):
        raise auth_error(401, "The access token is not valid.")
    return document


def _read_profile(
    config: transport.SupabaseUserConfig,
    access_token: str,
    user_id: str,
) -> dict[str, Any]:
    """Read the caller's profile through their own token, so RLS applies."""
    try:
        rows = transport.request_json(
            config,
            access_token=access_token,
            method="GET",
            path=f"/rest/v1/{PROFILES_TABLE}",
            query={
                "select": "id,role,display_name",
                "id": f"eq.{user_id}",
                "limit": "1",
            },
            timeout_seconds=PROFILE_TIMEOUT_SECONDS,
        )
    except transport.SupabaseUserError as error:
        raise _translate(error) from None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        # A verified account with no profile row gets nothing. Defaulting to a
        # usable role here would grant access to anyone who can sign up.
        raise auth_error(403, "This account has no CrossFlow profile.")
    return rows[0]


def authenticate(access_token: str) -> AuthenticatedUser:
    """Verify a token and resolve its role. Cached briefly on success only."""
    config = _config()
    key = _cache_key(access_token)
    cached = _cached(key)
    if cached is not None:
        return cached

    document = _verify_remote(config, access_token)
    user_id = str(document["id"])
    profile = _read_profile(config, access_token, user_id)

    role = profile.get("role")
    if role not in {"admin", "driver"}:
        raise auth_error(403, "This account has no usable CrossFlow role.")
    display_name = profile.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = "CrossFlow user"

    user = AuthenticatedUser(
        id=user_id,
        role=role,
        display_name=display_name.strip()[:120],
        access_token=access_token,
        expires_at=_token_expiry(access_token),
    )
    _remember(key, user)
    return user


def require_user(
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    """FastAPI dependency: any signed-in CrossFlow account."""
    return authenticate(_bearer_token(authorization))


def require_admin_user(
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    """FastAPI dependency: a human administrator.

    Distinct from ``main._require_admin``, which authorizes machine ingestion
    with a shared deployment secret. Two mechanisms, two purposes; do not merge
    them.
    """
    user = require_user(authorization)
    if not user.is_admin:
        raise auth_error(403, "Administrator access is required.")
    return user


def status() -> dict[str, Any]:
    """Public description of whether signing in is possible right now."""
    mode = os.environ.get(AUTH_MODE_ENV, "disabled").strip().casefold() or "disabled"
    config = transport.load_user_config() if auth_enabled() else None
    return {
        "mode": "supabase" if auth_enabled() else mode,
        "enabled": auth_enabled(),
        "configured": config is not None,
        "project_origin": config.origin if config is not None else None,
        "sign_in": "supabase_auth_direct",
        "notes": (
            "Clients authenticate with Supabase Auth directly and send the "
            "resulting access token as a Bearer credential. This API never "
            "receives passwords."
        ),
    }
