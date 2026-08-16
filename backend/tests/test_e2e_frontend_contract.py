"""End-to-end tests over real HTTP against a live server process.

Every other suite calls handlers in-process, which cannot catch the failures
that actually reach the browser: a route registered at the wrong path, a
serialized field the TypeScript client does not expect, an auth gate that only
behaves correctly when FastAPI resolves the headers itself, or a payload the
frontend cannot parse. This boots uvicorn exactly as the dev server does and
speaks to it the way the frontend does.

The assertions mirror ``frontend/src/types.ts`` and the calls in
``frontend/src/services/api.ts``. If the API stops satisfying them, the
dashboard breaks even though the unit suites stay green.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
SERVER_BOOT_TIMEOUT_S = 180
ROUTE_READ_TOKEN = "e2e-route-read-token"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LiveServerTestCase(unittest.TestCase):
    """Boots one server for the whole class; startup dominates the runtime."""

    server: subprocess.Popen
    base_url: str
    #: Per-subclass server environment, so one suite can boot the deployed
    #: Supabase-auth configuration while another boots the open dev default.
    extra_env: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        port = free_port()
        cls.base_url = f"http://127.0.0.1:{port}"
        environment = {
            **os.environ,
            "CROSSFLOW_ROUTE_READ_TOKEN": ROUTE_READ_TOKEN,
            # Keep the run hermetic: no Supabase, and no background warm-up
            # thread competing with the very first request under test.
            "CROSSFLOW_AUTH_MODE": "disabled",
            "CROSSFLOW_WARM_ROUTING_CACHES": "0",
            "CROSSFLOW_ROUTE_DB": ":memory:",
            **cls.extra_env,
        }
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(BACKEND_DIR),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls._stop_server)
        cls._await_ready()

    @classmethod
    def _stop_server(cls) -> None:
        cls.server.terminate()
        try:
            cls.server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            cls.server.kill()

    @classmethod
    def _await_ready(cls) -> None:
        """Poll until the app answers; a cold graph load takes many seconds."""
        deadline = time.monotonic() + SERVER_BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                output = (cls.server.stdout.read() or b"").decode("utf-8", "replace")
                raise AssertionError(f"server exited during boot:\n{output}")
            try:
                with urllib.request.urlopen(f"{cls.base_url}/api/corridors", timeout=10):
                    return
            except (urllib.error.URLError, socket.timeout, ConnectionError):
                time.sleep(0.5)
        raise AssertionError("server did not become ready in time")

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: int = 120,
    ) -> tuple[int, Any]:
        """Return (status, decoded body), treating HTTP errors as results."""
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=payload, method=method,
        )
        request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raw = error.read() or b"null"
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw.decode("utf-8", "replace")


class FrontendRouteContractTests(LiveServerTestCase):
    """The planner's own request/response cycle, over the wire."""

    def plan_route(self) -> dict:
        status, body = self.call("/api/optimize-route", method="POST", body={
            "corridor_id": "corridor-1",
            "vehicle_type": "CARGO_TRUCK",
            "weather": 0,
            "hour": 8,
            "route_preference": "BALANCED",
        })
        self.assertEqual(status, 200, body)
        return body

    def test_planner_response_carries_every_field_the_ui_renders(self) -> None:
        body = self.plan_route()
        # Each of these is read directly by RouteOptimizer's summary panel.
        for field in (
            "route_id", "route_code", "corridor", "vehicle_type",
            "estimated_travel_time_mins", "total_eta_mins", "co2_emissions_kg",
            "congestion_prediction", "optimal_departure", "route_geometry",
            "route_data_source",
        ):
            self.assertIn(field, body, f"{field} missing from planner response")

        self.assertRegex(body["route_code"], r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$")
        self.assertRegex(body["route_id"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(body["route_geometry"], list)
        self.assertGreater(len(body["route_geometry"]), 1)
        corridor = body["corridor"]
        for field in ("id", "name", "distance_km", "base_time_mins"):
            self.assertIn(field, corridor)

    def test_balanced_objective_survives_the_round_trip(self) -> None:
        """The picker was removed, so the client always sends BALANCED."""
        body = self.plan_route()
        self.assertEqual(body.get("route_preference", "BALANCED"), "BALANCED")

    def test_planned_route_is_retrievable_by_its_dispatch_code(self) -> None:
        planned = self.plan_route()
        status, stored = self.call(f"/api/routes/{planned['route_code']}")
        self.assertEqual(status, 200, stored)
        self.assertEqual(stored["route_code"], planned["route_code"])
        self.assertEqual(stored["route_id"], planned["route_id"])

    def test_retrieval_needs_no_credential(self) -> None:
        """The driver client is a guest; the code alone is the capability."""
        planned = self.plan_route()
        status, _ = self.call(f"/api/routes/{planned['route_code']}")
        self.assertEqual(status, 200)

    def test_retrieval_is_not_cached_by_the_browser(self) -> None:
        planned = self.plan_route()
        request = urllib.request.Request(
            f"{self.base_url}/api/routes/{planned['route_code']}",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_unknown_code_is_a_clean_404_the_client_can_message(self) -> None:
        status, body = self.call("/api/routes/2222222")
        self.assertEqual(status, 404)
        self.assertIsInstance(body.get("detail"), str)

    def test_malformed_code_is_rejected_before_any_lookup(self) -> None:
        # Lowercase and ambiguous characters are outside the code alphabet.
        for bad_code in ("abcdefg", "0000000", "TOOLONGCODE"):
            status, _ = self.call(f"/api/routes/{bad_code}")
            self.assertEqual(status, 422, f"{bad_code} should fail validation")

    def test_identical_requests_resolve_to_the_same_route_identity(self) -> None:
        """Content addressing is what lets history dedupe a replanned journey."""
        self.assertEqual(self.plan_route()["route_id"], self.plan_route()["route_id"])


class FrontendHistoryContractTests(LiveServerTestCase):
    """History is the one route-facing endpoint that requires an account."""

    def test_history_refuses_service_when_auth_is_disabled(self) -> None:
        status, body = self.call("/api/routes")
        self.assertEqual(status, 503, body)
        self.assertIn("authentication", body["detail"].lower())

    def test_history_never_returns_runs_to_an_anonymous_caller(self) -> None:
        status, body = self.call("/api/routes?limit=5")
        self.assertNotEqual(status, 200)
        self.assertNotIn("runs", body if isinstance(body, dict) else {})

    def test_history_limit_is_validated_at_the_boundary(self) -> None:
        status, _ = self.call("/api/routes?limit=99999")
        self.assertEqual(status, 422)


class FrontendCorridorContractTests(LiveServerTestCase):
    """The views that render before anyone plans anything."""

    def test_corridors_match_the_shape_the_map_expects(self) -> None:
        """``fetchCorridors`` reads ``payload.corridors``; anything else falls
        back to the bundled offline list and silently hides live data."""
        status, body = self.call("/api/corridors")
        self.assertEqual(status, 200)
        self.assertIn("corridors", body)
        corridors = body["corridors"]
        self.assertIsInstance(corridors, list)
        self.assertGreater(len(corridors), 0)
        for field in ("id", "name", "distance_km", "base_time_mins"):
            self.assertIn(field, corridors[0])

    def test_route_locations_expose_coordinates_for_the_picker(self) -> None:
        """``fetchRouteLocations`` reads ``payload.locations``."""
        status, body = self.call("/api/route-locations")
        self.assertEqual(status, 200)
        self.assertIn("locations", body)
        locations = body["locations"]
        self.assertIsInstance(locations, list)
        self.assertGreater(len(locations), 0)
        for field in ("id", "name", "category", "lat", "lng"):
            self.assertIn(field, locations[0])

    def test_every_envelope_carries_the_provenance_badge_fields(self) -> None:
        """The header's source badge reads these off any enveloped response."""
        status, body = self.call("/api/corridors")
        self.assertEqual(status, 200)
        for field in ("generated_at", "data_source", "provenance"):
            self.assertIn(field, body)

    def test_cors_allows_the_vite_dev_origin(self) -> None:
        """Without this the browser blocks every call in local development."""
        request = urllib.request.Request(
            f"{self.base_url}/api/corridors",
            headers={"Origin": "http://localhost:5173"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            allowed = response.headers.get("Access-Control-Allow-Origin", "")
        self.assertIn(allowed, ("*", "http://localhost:5173"))


class DeployedAuthConfigurationTests(LiveServerTestCase):
    """The server as Vercel runs it, with Supabase auth switched on.

    Enabling auth must not close the driver's door. That regressed once
    already, and the failure only appears under the deployed configuration.
    """

    extra_env = {"CROSSFLOW_AUTH_MODE": "supabase"}

    def test_driver_retrieval_stays_open_when_auth_is_enabled(self) -> None:
        status, planned = self.call("/api/optimize-route", method="POST", body={
            "corridor_id": "corridor-1", "vehicle_type": "COMMUTER",
            "weather": 0, "hour": 8,
        })
        self.assertEqual(status, 200, planned)
        status, stored = self.call(f"/api/routes/{planned['route_code']}")
        self.assertEqual(status, 200, stored)
        self.assertEqual(stored["route_code"], planned["route_code"])

    def test_history_demands_a_token_rather_than_reporting_misconfiguration(self) -> None:
        status, body = self.call("/api/routes")
        self.assertEqual(status, 401, body)

    def test_history_never_honours_a_locally_minted_token(self) -> None:
        """A structurally valid JWS still has to survive Supabase.

        With no project credentials the server answers 503 rather than 401: it
        cannot verify the token, and saying "invalid" would be a claim it has
        no basis for. What matters either way is that nothing is handed back.
        """
        status, body = self.call(
            "/api/routes", headers={"Authorization": "Bearer forged.token.value"},
        )
        self.assertIn(status, (401, 503), body)
        self.assertNotIn("runs", body if isinstance(body, dict) else {})

    def test_planning_stays_public_with_auth_enabled(self) -> None:
        status, _ = self.call("/api/optimize-route", method="POST", body={
            "corridor_id": "corridor-2", "vehicle_type": "COMMUTER",
            "weather": 0, "hour": 9,
        })
        self.assertEqual(status, 200)

    def test_auth_status_tells_the_client_sign_in_is_possible(self) -> None:
        status, body = self.call("/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])


if __name__ == "__main__":
    unittest.main()
