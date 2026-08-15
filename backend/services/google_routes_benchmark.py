"""Opt-in, text-only Google Routes v2 benchmark.

This adapter is deliberately isolated from production routing and learning.
It requests only duration, distance, and route labels: no provider geometry,
polylines, steps, or navigation text may enter the application response.
"""

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List

from services import tls


COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_MAPS_POLICY_URL = (
    "https://developers.google.com/maps/documentation/routes/policies"
)
FIELD_MASK = "routes.duration,routes.distanceMeters,routes.routeLabels"
TIMEOUT_S = 4.0
MAX_RESPONSE_BYTES = 128_000
MAX_ROUTES = 3
MAX_DISTANCE_METERS = 2_000_000.0
MAX_DURATION_SECONDS = 172_800.0
_DURATION_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?s$")
_ROUTE_PREFERENCES = {"BALANCED", "FASTEST", "SHORTEST", "EASY", "LOCAL"}


class GoogleBenchmarkUnavailable(RuntimeError):
    """The optional benchmark could not produce a safe metrics response."""


def is_enabled() -> bool:
    """Require an explicit opt-in rather than treating any truthy value as on."""
    return os.environ.get("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK", "").strip().lower() == "true"


def get_api_key() -> str:
    """Read the benchmark-only server key; never inspect browser/legacy keys."""
    return os.environ.get("CROSSFLOW_GOOGLE_ROUTES_API_KEY", "").strip()


def _request_body(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    route_preference: str,
) -> Dict[str, Any]:
    if route_preference not in _ROUTE_PREFERENCES:
        raise ValueError(f"Unknown route preference: {route_preference}.")
    body: Dict[str, Any] = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": origin_lat,
                    "longitude": origin_lng,
                },
            },
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": destination_lat,
                    "longitude": destination_lng,
                },
            },
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
    }
    if route_preference == "SHORTEST":
        body["requestedReferenceRoutes"] = ["SHORTER_DISTANCE"]
    return body


def _duration_seconds(value: Any) -> float:
    if not isinstance(value, str) or not _DURATION_RE.fullmatch(value):
        raise ValueError("Invalid Google duration.")
    seconds = float(value[:-1])
    if not math.isfinite(seconds) or not 0.0 < seconds <= MAX_DURATION_SECONDS:
        raise ValueError("Google duration is outside the benchmark bounds.")
    return seconds


def _distance_meters(value: Any) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= MAX_DISTANCE_METERS
    ):
        raise ValueError("Google distance is outside the benchmark bounds.")
    return float(value)


def parse_metrics(payload: Any) -> List[Dict[str, Any]]:
    """Parse at most three finite metric-only routes from a provider payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("routes"), list):
        raise GoogleBenchmarkUnavailable("Google Routes returned no route list.")

    parsed: List[Dict[str, Any]] = []
    for route in payload["routes"][:MAX_ROUTES]:
        if not isinstance(route, dict):
            continue
        try:
            seconds = _duration_seconds(route.get("duration"))
            distance_m = _distance_meters(route.get("distanceMeters"))
        except ValueError:
            continue
        raw_labels = route.get("routeLabels", [])
        labels = (
            [label for label in raw_labels[:8] if isinstance(label, str) and 0 < len(label) <= 64]
            if isinstance(raw_labels, list)
            else []
        )
        parsed.append({
            "id": f"google-benchmark-{len(parsed) + 1}",
            "duration_seconds": round(seconds, 3),
            "duration_mins": round(seconds / 60.0, 2),
            "distance_meters": round(distance_m, 1),
            "distance_km": round(distance_m / 1000.0, 3),
            "route_labels": labels,
            "summary": f"{distance_m / 1000.0:.1f} km · {seconds / 60.0:.1f} min",
        })
    if not parsed:
        raise GoogleBenchmarkUnavailable("Google Routes returned no valid metrics.")
    return parsed


def _preference_details(route_preference: str) -> Dict[str, Any]:
    if route_preference == "FASTEST":
        return {
            "requested": route_preference,
            "honored": True,
            "experimental": False,
            "provider_translation": "TRAFFIC_AWARE",
            "note": "Google traffic-aware travel time is comparable to the fastest objective.",
        }
    if route_preference == "SHORTEST":
        return {
            "requested": route_preference,
            "honored": True,
            "experimental": True,
            "provider_translation": "SHORTER_DISTANCE reference route",
            "requested_reference_routes": ["SHORTER_DISTANCE"],
            "note": (
                "Experimental Google reference-route benchmark; it does not "
                "guarantee an absolute shortest path."
            ),
        }
    return {
        "requested": route_preference,
        "honored": False,
        "experimental": False,
        "provider_translation": "TRAFFIC_AWARE",
        "note": (
            "Google does not receive CrossFlow's balanced, easy or local-road "
            "component weights; these metrics are comparison-only."
        ),
    }


def benchmark_routes(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    route_preference: str = "BALANCED",
) -> Dict[str, Any]:
    """Call Google once and return non-cacheable metric text only."""
    if not is_enabled():
        raise GoogleBenchmarkUnavailable("Google benchmark is disabled.")
    api_key = get_api_key()
    if not api_key:
        raise GoogleBenchmarkUnavailable("Google benchmark is not configured.")

    body = _request_body(
        origin_lat, origin_lng, destination_lat, destination_lng,
        route_preference,
    )
    request = urllib.request.Request(
        COMPUTE_ROUTES_URL,
        data=json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_S, context=tls.default_context(),
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GoogleBenchmarkUnavailable("Google Routes response exceeded the size limit.")
        payload = json.loads(raw.decode("utf-8"))
        routes = parse_metrics(payload)
    except GoogleBenchmarkUnavailable:
        raise
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        raise GoogleBenchmarkUnavailable("Google Routes benchmark is unavailable.") from exc

    preference = _preference_details(route_preference)
    return {
        "benchmark_type": "google_routes_v2_text_metrics",
        "provider": "google_routes_v2",
        "attribution": "Google Maps",
        "policy_url": GOOGLE_MAPS_POLICY_URL,
        "route_preference": route_preference,
        "preference_honored": preference["honored"],
        "preference_honored_details": preference,
        "routes": routes,
        "cacheable": False,
        "persisted": False,
        "training_eligible": False,
        "map_overlay_allowed": False,
    }
