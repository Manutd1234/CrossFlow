"""Security-boundary tests for human authentication.

These are acceptance criteria, not regression cover. The handoff is explicit
that delivery persistence must not begin until driver isolation is proven, and
this file is the proof. Several tests are written so that the *easy* mistake
makes them fail loudly rather than pass quietly.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from fastapi import HTTPException


PROJECT_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from auth import identity, transport  # noqa: E402


ADMIN_ID = "11111111-2222-3333-4444-555555555555"
DRIVER_ID = "66666666-7777-8888-9999-000000000000"

CONFIGURED_ENV = {
    "CROSSFLOW_AUTH_MODE": "supabase",
    "SUPABASE_URL": "https://project-ref.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_browser_safe",
}


def _segment(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(
    *,
    subject: str = DRIVER_ID,
    expires_in: int = 3_600,
    **extra_claims: Any,
) -> str:
    """A structurally valid compact JWS. The signature is never checked here.

    Supabase verifies the signature; this module only ever parses ``exp`` from
    an already-verified token, so a fake signature is the correct fixture.
    """
    claims: dict[str, Any] = {"sub": subject, "exp": int(time.time()) + expires_in}
    claims.update(extra_claims)
    return f"{_segment({'alg': 'ES256', 'typ': 'JWT'})}.{_segment(claims)}.c2ln"


class FakeSupabase:
    """Records every outbound call so tests can assert on credentials used."""

    def __init__(
        self,
        *,
        user: Optional[dict[str, Any]] = None,
        profile_rows: Optional[list[dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.user = user if user is not None else {"id": DRIVER_ID}
        self.profile_rows = (
            profile_rows
            if profile_rows is not None
            else [{"id": DRIVER_ID, "role": "driver", "display_name": "Driver One"}]
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, config, **kwargs: Any) -> Any:
        self.calls.append({"config": config, **kwargs})
        if self.error is not None:
            raise transport.SupabaseUserError(self.error)
        path = kwargs.get("path", "")
        if path == "/auth/v1/user":
            return dict(self.user)
        if path.startswith("/rest/v1/"):
            return [dict(row) for row in self.profile_rows]
        raise AssertionError(f"unexpected path {path!r}")


class AuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        identity.clear_cache()
        self.addCleanup(identity.clear_cache)
        patcher = patch.dict(os.environ, CONFIGURED_ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def authenticate(self, fake: FakeSupabase, token: str) -> identity.AuthenticatedUser:
        with patch.object(identity.transport, "request_json", fake):
            return identity.authenticate(token)


class CredentialBoundaryTests(AuthTestCase):
    """The failure mode that looks completely correct in testing."""

    def test_user_transport_cannot_load_a_service_role_key(self) -> None:
        env = {
            "SUPABASE_URL": "https://project-ref.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_privileged",
            "SUPABASE_SERVICE_ROLE_KEY": "legacy.service.role",
        }
        with patch.dict(os.environ, env, clear=True):
            # No publishable key is present, so there is nothing safe to send.
            # A module that silently fell back to the secret key would return a
            # config here, and every RLS policy would become inert.
            self.assertIsNone(transport.load_user_config())

    def test_user_transport_never_names_a_privileged_credential(self) -> None:
        source = Path(transport.__file__).read_text(encoding="utf-8")
        code = source.split('"""', 2)[-1]  # exclude the explanatory docstring
        for forbidden in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
            self.assertNotIn(forbidden, code)

    def test_profile_read_uses_the_callers_own_token(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        profile_calls = [
            call for call in fake.calls if call["path"].startswith("/rest/v1/")
        ]
        self.assertEqual(len(profile_calls), 1)
        # RLS applies only because this is the caller's token, not a shared one.
        self.assertEqual(profile_calls[0]["access_token"], token)

    def test_outbound_headers_pair_publishable_key_with_user_token(self) -> None:
        token = make_token()
        headers = transport.user_headers("sb_publishable_browser_safe", token)
        self.assertEqual(headers["apikey"], "sb_publishable_browser_safe")
        self.assertEqual(headers["Authorization"], f"Bearer {token}")

    def test_only_data_and_user_paths_are_reachable(self) -> None:
        config = transport.SupabaseUserConfig(
            "https://project-ref.supabase.co", "sb_publishable_browser_safe",
        )
        # Token issuance and admin user management must not be callable from
        # the module that holds user tokens.
        for path in (
            "/auth/v1/admin/users",
            "/auth/v1/token",
            "/rest/v1/../auth/v1/token",
            "/storage/v1/object",
        ):
            with self.subTest(path=path):
                with self.assertRaises(transport.SupabaseUserError):
                    transport.request_json(
                        config,
                        access_token=make_token(),
                        method="GET",
                        path=path,
                    )


class RoleResolutionTests(AuthTestCase):
    def test_role_comes_from_profiles_not_from_token_metadata(self) -> None:
        # user_metadata is writable by the user through the Supabase client, so
        # a role claimed there is a role the user granted themselves.
        fake = FakeSupabase(
            user={
                "id": DRIVER_ID,
                "role": "admin",
                "user_metadata": {"role": "admin"},
                "app_metadata": {"role": "admin"},
            },
            profile_rows=[
                {"id": DRIVER_ID, "role": "driver", "display_name": "Driver One"},
            ],
        )
        user = self.authenticate(fake, make_token(role="admin"))
        self.assertEqual(user.role, "driver")
        self.assertFalse(user.is_admin)

    def test_admin_dependency_rejects_a_driver(self) -> None:
        fake = FakeSupabase()
        with patch.object(identity.transport, "request_json", fake):
            with self.assertRaises(HTTPException) as raised:
                identity.require_admin_user(f"Bearer {make_token()}")
        self.assertEqual(raised.exception.status_code, 403)

    def test_admin_dependency_accepts_an_admin(self) -> None:
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[
                {"id": ADMIN_ID, "role": "admin", "display_name": "Ops Admin"},
            ],
        )
        with patch.object(identity.transport, "request_json", fake):
            user = identity.require_admin_user(
                f"Bearer {make_token(subject=ADMIN_ID)}",
            )
        self.assertTrue(user.is_admin)

    def test_missing_profile_is_refused_rather_than_defaulted(self) -> None:
        # Defaulting a profile-less account to 'driver' would grant access to
        # anyone who can reach the sign-up form.
        fake = FakeSupabase(profile_rows=[])
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, make_token())
        self.assertEqual(raised.exception.status_code, 403)

    def test_unknown_role_value_is_refused(self) -> None:
        fake = FakeSupabase(
            profile_rows=[
                {"id": DRIVER_ID, "role": "superuser", "display_name": "X"},
            ],
        )
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, make_token())
        self.assertEqual(raised.exception.status_code, 403)


class MechanismSeparationTests(AuthTestCase):
    """The machine secret and the human session must not authenticate each other."""

    def test_admin_token_presented_as_a_bearer_token_is_rejected(self) -> None:
        shared_secret = "crossflow-admin-shared-secret"
        with patch.dict(
            os.environ, {"CROSSFLOW_ADMIN_TOKEN": shared_secret}, clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                identity.require_user(f"Bearer {shared_secret}")
        self.assertEqual(raised.exception.status_code, 401)

    def test_a_user_token_does_not_open_the_machine_endpoints(self) -> None:
        import main as api_main

        with patch.dict(
            os.environ,
            {"CROSSFLOW_ADMIN_TOKEN": "crossflow-admin-shared-secret"},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                api_main._require_admin(make_token(subject=ADMIN_ID))
        self.assertEqual(raised.exception.status_code, 403)


class TokenHandlingTests(AuthTestCase):
    def test_malformed_tokens_are_401_never_500(self) -> None:
        for header in (
            None,
            "",
            "Bearer",
            "Basic dXNlcjpwYXNz",
            "Bearer not-a-jwt",
            "Bearer a.b",
            "Bearer a.b.c.d",
            "Bearer " + "a" * 9_000,
        ):
            with self.subTest(header=header):
                with self.assertRaises(HTTPException) as raised:
                    identity.require_user(header)
                self.assertEqual(raised.exception.status_code, 401)

    def test_header_injection_in_a_token_is_rejected_before_any_request(self) -> None:
        poisoned = "aaa.bbb.ccc\r\nX-Injected: 1"
        with self.assertRaises(transport.SupabaseUserError):
            transport.validate_access_token(poisoned)
        with self.assertRaises(transport.SupabaseUserError):
            transport.user_headers("sb_publishable_browser_safe", poisoned)

    def test_rejected_tokens_are_not_cached(self) -> None:
        token = make_token()
        failing = FakeSupabase(error="http_401")
        with self.assertRaises(HTTPException):
            self.authenticate(failing, token)
        succeeding = FakeSupabase()
        user = self.authenticate(succeeding, token)
        # A cached failure, or a cache write on the failed path, would mean the
        # second call never reached Supabase.
        self.assertTrue(succeeding.calls)
        self.assertEqual(user.role, "driver")

    def test_verification_is_cached_for_repeat_calls(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        first_call_count = len(fake.calls)
        self.authenticate(fake, token)
        self.assertEqual(len(fake.calls), first_call_count)

    def test_a_cache_entry_never_outlives_the_token(self) -> None:
        already_expired = make_token(expires_in=-10)
        fake = FakeSupabase()
        self.authenticate(fake, already_expired)
        calls_after_first = len(fake.calls)
        self.authenticate(fake, already_expired)
        self.assertGreater(len(fake.calls), calls_after_first)

    def test_different_tokens_do_not_share_a_cache_entry(self) -> None:
        fake = FakeSupabase()
        self.authenticate(fake, make_token())
        calls_after_first = len(fake.calls)
        self.authenticate(fake, make_token(expires_in=1_800))
        self.assertGreater(len(fake.calls), calls_after_first)


class DegradationTests(AuthTestCase):
    def test_auth_outage_is_503_not_a_crash(self) -> None:
        for code in ("network_error", "timeout", "http_500"):
            with self.subTest(code=code):
                identity.clear_cache()
                fake = FakeSupabase(error=code)
                with self.assertRaises(HTTPException) as raised:
                    self.authenticate(fake, make_token())
                self.assertEqual(raised.exception.status_code, 503)

    def test_public_endpoints_are_unaffected_by_an_auth_outage(self) -> None:
        import main as api_main

        with patch.object(
            identity.transport,
            "request_json",
            FakeSupabase(error="network_error"),
        ):
            payload = api_main.get_corridors()
        self.assertIn("corridors", payload)

    def test_auth_is_a_route_dependency_and_never_global_middleware(self) -> None:
        import main as api_main

        # Global auth middleware would take the public corridor views down with
        # Supabase, which is precisely what must not happen during a demo.
        middleware = " ".join(str(item.cls) for item in api_main.app.user_middleware)
        self.assertNotIn("auth", middleware.casefold())

    def test_disabled_mode_refuses_sessions_but_status_stays_public(self) -> None:
        with patch.dict(os.environ, {"CROSSFLOW_AUTH_MODE": "disabled"}, clear=False):
            with self.assertRaises(HTTPException) as raised:
                identity.require_user(f"Bearer {make_token()}")
            status = identity.status()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(status["enabled"])

    def test_status_never_reveals_a_credential(self) -> None:
        status = identity.status()
        rendered = json.dumps(status)
        self.assertNotIn("sb_publishable_browser_safe", rendered)
        self.assertNotIn("secret", rendered.casefold())


class ExistenceLeakTests(AuthTestCase):
    def test_absent_and_unowned_records_are_indistinguishable(self) -> None:
        # WP10's driver lookup must return this for both cases. Any wording or
        # status difference confirms which journey IDs exist.
        absent = identity.not_yours_error()
        unowned = identity.not_yours_error()
        self.assertEqual(absent.status_code, unowned.status_code)
        self.assertEqual(absent.detail, unowned.detail)
        self.assertEqual(absent.status_code, 404)

    def test_session_responses_are_not_cacheable(self) -> None:
        error = identity.auth_error(401, "nope")
        self.assertEqual(error.headers["Cache-Control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
