"""Public auth endpoints.

There is deliberately no login endpoint. Clients obtain an access token from
Supabase Auth directly, so passwords never reach this server and Supabase's own
rate limiting, lockout and password-reset flows apply without us reimplementing
them. See docs/AUTH_BACKEND_ROADMAP.md decision D2.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Response

from auth import identity
from auth.identity import AuthenticatedUser


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/status")
def auth_status(response: Response) -> dict[str, Any]:
    """Whether signing in is possible. Intentionally requires no session.

    A client calls this to decide whether to show a sign-in form at all, which
    is what lets the public corridor views stay usable when Supabase is
    unreachable instead of the whole app appearing broken.
    """
    _no_store(response)
    return identity.status()


@router.get("/session")
def auth_session(
    response: Response,
    user: AuthenticatedUser = Depends(identity.require_user),
) -> dict[str, Any]:
    """Who the server thinks the caller is.

    ``role`` here is authoritative: it was read from crossflow_profiles with
    the caller's own token, not taken from the token's claims.
    """
    _no_store(response)
    return {
        "user_id": user.id,
        "role": user.role,
        "display_name": user.display_name,
        "expires_at": user.expires_at,
        "role_source": "crossflow_profiles",
    }


@router.get("/admin-check")
def auth_admin_check(
    response: Response,
    user: AuthenticatedUser = Depends(identity.require_admin_user),
) -> dict[str, Any]:
    """Smallest possible admin-guarded endpoint.

    It exists so the admin path is exercised end to end before WP9 has any
    admin resources to guard, and so the placeholder client can demonstrate the
    driver/admin split.
    """
    _no_store(response)
    return {"user_id": user.id, "role": user.role, "admin": True}
