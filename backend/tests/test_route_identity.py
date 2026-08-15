from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

import sys
from pathlib import Path

# Required by `unittest discover -s backend/tests`, which makes this directory
# the top level and so runs neither a package init nor conftest.py. See
# tests/conftest.py for the full explanation.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from main import MultiStopRouteRequest
from services.route_identity import (
    canonicalize_route_request,
    route_id,
    short_route_code,
)
from services.route_store import RouteStore


def test_route_identity_is_order_independent_and_sha256_content_addressed():
    first = {"vehicle_type": "COMMUTER", "weather": 0, "coords": {"b": 2, "a": 1}}
    second = {"coords": {"a": 1, "b": 2}, "weather": 0, "vehicle_type": "COMMUTER"}
    canonical = canonicalize_route_request(first, route_kind="optimize-route")
    assert canonical == canonicalize_route_request(second, route_kind="optimize-route")
    assert route_id(first) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(route_id(first)) == 64
    assert route_id(first, route_kind="optimize-free-route") != route_id(first)


def test_route_store_round_trip_and_identity_validation(tmp_path):
    store = RouteStore(str(tmp_path / "routes.sqlite"))
    request = {"vehicle_type": "COMMUTER", "weather": 0}
    identity = route_id(request)
    response = {"route_id": identity, "route_data_source": "openstreetmap"}
    code = store.save(identity, response, request=request, route_kind="optimize-route")
    assert len(code) == 7
    assert store.get(identity) == response
    assert store.get(code) == response
    try:
        store.save("0" * 64, response, request=request, route_kind="optimize-route")
    except ValueError:
        pass
    else:
        raise AssertionError("forged route identity was accepted")
    store.close()


def test_short_route_code_is_deterministic_and_human_safe():
    identity = route_id({"route": "one"})
    assert short_route_code(identity) == short_route_code(identity)
    assert len(short_route_code(identity)) == 7
    assert all(character in "23456789ABCDEFGHJKLMNPQRSTUVWXYZ" for character in short_route_code(identity))


def test_multi_stop_schedule_fields_require_one_timezone_aware_mode():
    stops = [
        {"lat": 1.1, "lng": 104.0},
        {"lat": 1.11, "lng": 104.01},
        {"lat": 1.12, "lng": 104.02},
    ]
    request = MultiStopRouteRequest(
        stops=stops,
        departure_at=datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc),
    )
    assert request.departure_at is not None

    with pytest.raises(ValueError, match="either departure_at or arrive_by"):
        MultiStopRouteRequest(
            stops=stops,
            departure_at=datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc),
            arrive_by=datetime(2026, 8, 16, 10, 30, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError, match="departure_at must include a timezone"):
        MultiStopRouteRequest(
            stops=stops,
            departure_at=datetime(2026, 8, 16, 9, 30),
        )
