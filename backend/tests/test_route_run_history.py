"""API-boundary tests for route-run history and driver route retrieval.

The two endpoints deliberately sit on opposite sides of the auth line: a single
route is readable by anyone holding its dispatch code, while history spans
accounts and must never be. These tests pin that split, and in particular that
a non-administrator cannot widen its own scope.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from auth import identity  # noqa: E402
from services import route_run_store  # noqa: E402


def user(user_id: str, role: str) -> identity.AuthenticatedUser:
    return identity.AuthenticatedUser(
        id=user_id,
        role=role,
        display_name=role.title(),
        access_token="token",
        expires_at=None,
    )


class RouteRunHistoryScopeTests(unittest.TestCase):
    """Who may read whose runs."""

    def call_history(self, caller: identity.AuthenticatedUser, mine: bool = False):
        """Invoke the endpoint with the shared table stubbed as reachable."""
        captured: dict[str, object] = {}

        def fake_recent(limit: int, *, created_by: str | None = None):
            captured["limit"] = limit
            captured["created_by"] = created_by
            return []

        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=caller), \
                patch.object(route_run_store, "configured", return_value=True), \
                patch.object(route_run_store, "recent", fake_recent):
            payload = api_main.api_route_history(
                response=None, limit=10, mine=mine, authorization="Bearer token",
            )
        body = payload.get("data", payload)
        return body, captured

    def test_administrator_sees_every_account(self) -> None:
        body, captured = self.call_history(user("admin-1", "admin"))
        self.assertEqual(body["scope"], "all_accounts")
        self.assertIsNone(captured["created_by"])

    def test_administrator_can_narrow_to_own_runs(self) -> None:
        body, captured = self.call_history(user("admin-1", "admin"), mine=True)
        self.assertEqual(body["scope"], "own_account")
        self.assertEqual(captured["created_by"], "admin-1")

    def test_driver_is_confined_to_own_runs(self) -> None:
        body, captured = self.call_history(user("driver-1", "driver"))
        self.assertEqual(body["scope"], "own_account")
        self.assertEqual(captured["created_by"], "driver-1")

    def test_driver_cannot_widen_scope_by_asking_for_everything(self) -> None:
        """`mine=False` is a narrowing hint for admins, never a privilege grant."""
        _, captured = self.call_history(user("driver-1", "driver"), mine=False)
        self.assertEqual(captured["created_by"], "driver-1")

    def test_history_requires_authentication(self) -> None:
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(
                    identity,
                    "require_user",
                    side_effect=identity.auth_error(401, "The access token is not valid."),
                ):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=10, mine=False, authorization=None,
                )
        self.assertEqual(caught.exception.status_code, 401)

    def test_history_reports_unconfigured_backend_rather_than_empty(self) -> None:
        """An empty list would read as "no runs"; the cause must stay visible."""
        with patch.object(identity, "auth_enabled", return_value=True), \
                patch.object(identity, "require_user", return_value=user("a", "admin")), \
                patch.object(route_run_store, "configured", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                api_main.api_route_history(
                    response=None, limit=10, mine=False, authorization="Bearer token",
                )
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("not configured", caught.exception.detail)


class SharedStoreTransportTests(unittest.TestCase):
    """The store's REST contract with supabase_server.

    A malformed path fails soft, exactly like an unreachable project, so the
    only symptom is history silently staying empty. That is indistinguishable
    from "no runs yet" in the UI, so it is pinned here instead.
    """

    def test_rest_path_satisfies_the_transport_validator(self) -> None:
        from services import supabase_server

        self.assertIsNotNone(
            supabase_server._REST_PATH.fullmatch(route_run_store.REST_PATH),
            f"{route_run_store.REST_PATH!r} would be rejected as invalid_rest_path",
        )

    def test_rest_path_targets_the_documented_table(self) -> None:
        self.assertEqual(
            route_run_store.REST_PATH, f"/rest/v1/{route_run_store.TABLE_NAME}",
        )

    def test_store_reports_unconfigured_rather_than_raising(self) -> None:
        """A missing Supabase project must never break route planning."""
        with patch.object(route_run_store, "_config", return_value=None):
            self.assertFalse(route_run_store.save(
                "a" * 64, "ABCDEFG", "optimize-route", {}, {},
            ))
            self.assertIsNone(route_run_store.get("a" * 64))
            self.assertEqual(route_run_store.recent(), [])

    def test_get_rejects_keys_that_are_neither_id_nor_code(self) -> None:
        for bad_key in ("", "short", "x" * 63, 12345):
            self.assertIsNone(route_run_store.get(bad_key))  # type: ignore[arg-type]


class DriverRouteRetrievalTests(unittest.TestCase):
    """A dispatch code is the capability, and grants exactly one route."""

    def planned_route_code(self) -> str:
        request = api_main.RouteRequest(
            corridor_id="corridor-1", vehicle_type="COMMUTER", weather=0, hour=8,
        )
        return api_main.api_optimize_route(request)["route_code"]

    def test_code_holder_may_read_without_any_credential(self) -> None:
        code = self.planned_route_code()
        stored = api_main.api_get_route(route_id=code, response=None)
        self.assertEqual(stored["route_code"], code)

    def test_unknown_code_is_not_found_rather_than_forbidden(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            api_main.api_get_route(route_id="2222222", response=None)
        self.assertEqual(caught.exception.status_code, 404)

    def test_shared_table_serves_a_code_this_instance_never_stored(self) -> None:
        """The per-instance SQLite file is empty on a fresh serverless worker."""
        envelope = {"route_code": "VQT4QX8", "route_id": "b" * 64}
        with patch.object(api_main._ROUTE_STORE, "get", return_value=None), \
                patch.object(route_run_store, "get", return_value=envelope) as shared:
            stored = api_main.api_get_route(route_id="VQT4QX8", response=None)
        shared.assert_called_once_with("VQT4QX8")
        self.assertEqual(stored["route_code"], "VQT4QX8")


if __name__ == "__main__":
    unittest.main()
