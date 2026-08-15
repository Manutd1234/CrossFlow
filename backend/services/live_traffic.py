"""Live traffic data integration for CrossFlow AI.

Tries the TomTom Traffic Flow API first (requires TOMTOM_API_KEY env var);
falls back to the existing simulator when the key is absent or the API is
unreachable.

TomTom Flow API:
  GET https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/{zoom}/json
      ?point={lat},{lng}&unit=kmph&key={key}
  Returns current speed and free-flow speed for the road segment nearest to
  the query point.

We query one representative point per corridor and compute a congestion ratio
(current / free-flow). A ratio near 1.0 is free-flow; near 0.0 is a standstill.
The ratio is converted to our 0–100 congestion index to blend with the ML model.

When simulated, we return the same structure with provenance="simulated".
"""

import copy
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from services import clock

_TOMTOM_FLOW_BASE = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/10/json"
)
_USER_AGENT = "CrossFlowAI/2.0 (batam-singapore-hackathon-2026)"
_TOMTOM_TIMEOUT_S = 2.0

# One representative lat/lng per corridor for the TomTom point query.
# These sit on the busiest segment of each named corridor.
_CORRIDOR_POINTS: Dict[str, tuple] = {
    "corridor-1": (1.103860, 104.039342),  # Jalan Jendral Ahmad Yani / Kabil
    "corridor-2": (1.149066, 104.023257),  # Jalan Yos Sudarso
    "corridor-3": (1.119412, 104.020678),  # Simpang Jam approach
    "corridor-4": (1.105226, 103.955302),  # Jalan Pangeran Diponegoro
    "corridor-5": (1.116191, 104.099020),  # Jalan Hang Tuah
}

# Simple in-process cache: re-use TomTom responses for up to 2 minutes. Briefly
# cache failures too; otherwise a provider outage makes every dashboard poll
# wait for the same failed requests again.
_tomtom_cache: Dict[str, tuple] = {}  # key -> (result_dict | None, expires_at)
_CACHE_TTL_S = 120
_FAILURE_CACHE_TTL_S = 30

_HOTSPOT_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "corridor_hotspots.json",
)
_KNOWN_CORRIDOR_IDS = frozenset(_CORRIDOR_POINTS)


def _load_hotspot_catalog() -> Dict[str, Any]:
    """Load and validate the backend-owned corridor-map hotspot catalogue."""
    with open(_HOTSPOT_CATALOG_PATH, encoding="utf-8") as source:
        catalog = json.load(source)

    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported corridor hotspot catalogue schema.")
    methodology = catalog.get("methodology")
    candidates = catalog.get("candidates")
    if not isinstance(methodology, dict) or not isinstance(candidates, list):
        raise ValueError("Corridor hotspot catalogue is missing methodology/data.")

    selection_limit = methodology.get("selection_limit")
    if selection_limit != 30 or len(candidates) != selection_limit:
        raise ValueError("Corridor hotspot catalogue must contain 30 candidates.")
    selection_weights = methodology.get("selection_weights")
    if not isinstance(selection_weights, dict) or not math.isclose(
        sum(float(value) for value in selection_weights.values()),
        1.0,
        abs_tol=1e-9,
    ):
        raise ValueError("Hotspot selection weights must sum to one.")

    seen_ids = set()
    routing_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Every hotspot candidate must be an object.")
        zone_id = candidate.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id or zone_id in seen_ids:
            raise ValueError("Hotspot candidate ids must be non-empty and unique.")
        seen_ids.add(zone_id)

        signal_mix = candidate.get("signal_mix")
        if not isinstance(signal_mix, dict) or not signal_mix:
            raise ValueError(f"{zone_id} must define corridor signal weights.")
        if not set(signal_mix).issubset(_KNOWN_CORRIDOR_IDS) or not math.isclose(
            sum(float(value) for value in signal_mix.values()),
            1.0,
            abs_tol=1e-9,
        ):
            raise ValueError(f"{zone_id} has invalid corridor signal weights.")
        if any(
            not math.isfinite(float(value)) or float(value) < 0.0
            for value in signal_mix.values()
        ):
            raise ValueError(f"{zone_id} has non-finite/negative signal weights.")

        numeric_fields = (
            "lat", "lng", "radius_m", "base_score",
            "network_criticality", "demand_exposure",
        )
        if any(
            isinstance(candidate.get(field), bool)
            or not isinstance(candidate.get(field), (int, float))
            or not math.isfinite(float(candidate[field]))
            for field in numeric_fields
        ):
            raise ValueError(f"{zone_id} has invalid numeric planning data.")
        if not 0.0 <= float(candidate["base_score"]) <= 100.0:
            raise ValueError(f"{zone_id} base_score must be in [0, 100].")
        if not 0.0 <= float(candidate["network_criticality"]) <= 100.0:
            raise ValueError(f"{zone_id} network_criticality must be in [0, 100].")
        if not 0.0 <= float(candidate["demand_exposure"]) <= 100.0:
            raise ValueError(f"{zone_id} demand_exposure must be in [0, 100].")
        if not isinstance(candidate.get("routing_enabled"), bool):
            raise ValueError(f"{zone_id} routing_enabled must be boolean.")
        routing_count += int(candidate["routing_enabled"])

    if routing_count != 15:
        raise ValueError("Exactly 15 established hotspots must remain routing-enabled.")
    return catalog


CORRIDOR_HOTSPOT_CATALOG = _load_hotspot_catalog()
CORRIDOR_MAP_HOTSPOTS_CONFIG = CORRIDOR_HOTSPOT_CATALOG["candidates"]
CORRIDOR_MAP_HOTSPOT_METHODOLOGY = CORRIDOR_HOTSPOT_CATALOG["methodology"]
HOTSPOT_SELECTION_WEIGHTS = CORRIDOR_MAP_HOTSPOT_METHODOLOGY[
    "selection_weights"
]

# Published model assumptions for local hotspot scores. These are calibrated
# planning weights, not trained accuracy claims or zone-specific observations.
HOTSPOT_CORRIDOR_SIGNAL_WEIGHT = 0.85
HOTSPOT_BASELINE_WEIGHT = 0.15
HOTSPOT_ACTIVE_PEAK_LIFT = 5.0

# Vehicle-neutral queue emissions pressure mirrors the quadratic congestion
# delay term used by A*. It deliberately stops at a relative index: without
# zone traffic volumes and fleet composition, publishing area kg/hour would be
# an unsupported claim.
EMISSIONS_PRESSURE_ELEVATED_THRESHOLD = 16.0
EMISSIONS_PRESSURE_HIGH_THRESHOLD = 49.0
EMISSIONS_PRESSURE_MODEL = {
    "schema_version": 1,
    "methodology_version": "crossflow-zone-pressure-v1",
    "formula": "queue_pressure_factor=(congestion_index/100)^2; index=100*factor",
    "thresholds": {
        "ELEVATED": EMISSIONS_PRESSURE_ELEVATED_THRESHOLD,
        "HIGH": EMISSIONS_PRESSURE_HIGH_THRESHOLD,
    },
    "traffic_input": "simulated_corridor_forecast_plus_recurring_zone_baseline",
    "source": "crossflow_congestion_delay_model",
    "observed": False,
    "aggregate_mass_available": False,
    "limitations": (
        "Relative queue pressure before road, route, vehicle and traffic-volume "
        "exposure; not measured CO2, air quality, or area kg/hour."
    ),
}


def _tomtom_api_key() -> str:
    """Read provider configuration at request time.

    Serverless environments normally inject variables before import, but a
    request-time read also makes local key rotation and tests work without
    restarting the Python process.
    """
    return os.environ.get("TOMTOM_API_KEY", "").strip()


def _tomtom_cache_key(lat: float, lng: float, api_key: str) -> str:
    """Keep cached responses separate across key rotation without storing a key."""
    key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"{key_fingerprint}:{lat:.4f},{lng:.4f}"


def _tomtom_segment(
    lat: float,
    lng: float,
    api_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Query TomTom for the road segment nearest to (lat, lng)."""
    api_key = _tomtom_api_key() if api_key is None else api_key.strip()
    if not api_key:
        return None

    cache_key = _tomtom_cache_key(lat, lng, api_key)
    cached = _tomtom_cache.get(cache_key)
    if cached is not None:
        cached_val, expires = cached
        if time.monotonic() < expires:
            return cached_val
        _tomtom_cache.pop(cache_key, None)

    # TomTom documents parameter values as case-sensitive. The supported
    # kilometre-per-hour value is `kmph` (not the former `KMPH`, which receives
    # an INVALID_REQUEST response).
    query = urllib.parse.urlencode({
        "point": f"{lat},{lng}",
        "unit": "kmph",
        "key": api_key,
    })
    url = f"{_TOMTOM_FLOW_BASE}?{query}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TOMTOM_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        segment = data.get("flowSegmentData")
        if not isinstance(segment, dict):
            raise ValueError("response did not contain flowSegmentData")

        current_speed = segment.get("currentSpeed")
        free_flow_speed = segment.get("freeFlowSpeed")
        confidence = segment.get("confidence", 0)
        numeric_values = (current_speed, free_flow_speed, confidence)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        ):
            raise ValueError("response contained invalid traffic measurements")
        if float(current_speed) < 0 or float(free_flow_speed) <= 0:
            raise ValueError("response contained invalid traffic speeds")

        result = {
            "current_speed_kmh": float(current_speed),
            "free_flow_speed_kmh": float(free_flow_speed),
            "confidence": float(confidence),
        }
        _tomtom_cache[cache_key] = (result, time.monotonic() + _CACHE_TTL_S)
        return result
    except Exception as err:  # noqa: BLE001
        print(f"[live_traffic] TomTom unreachable: {err}")
        _tomtom_cache[cache_key] = (
            None,
            time.monotonic() + _FAILURE_CACHE_TTL_S,
        )
        return None


def _tomtom_segments(api_key: str) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch every representative corridor point within one browser timeout.

    The frontend allows six seconds for `/api/live-traffic`. The former five
    sequential four-second calls could therefore never degrade gracefully
    during a slow provider response. Each request now has a two-second bound and
    all five run concurrently, leaving time for response assembly and transport.
    """
    if not api_key:
        return {}

    results: Dict[str, Optional[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(
        max_workers=len(_CORRIDOR_POINTS),
        thread_name_prefix="tomtom-flow",
    ) as executor:
        futures = {
            executor.submit(_tomtom_segment, lat, lng, api_key): corridor_id
            for corridor_id, (lat, lng) in _CORRIDOR_POINTS.items()
        }
        for future in as_completed(futures):
            corridor_id = futures[future]
            try:
                results[corridor_id] = future.result()
            except Exception as err:  # defensive: _tomtom_segment handles errors
                print(f"[live_traffic] TomTom worker failed: {err}")
                results[corridor_id] = None
    return results


def _congestion_from_speed_ratio(current: float, free_flow: float) -> float:
    """Convert a speed ratio to a 0-100 congestion index.

    Ratio 1.0 (free flow) → index 5.
    Ratio 0.0 (standstill) → index 95.
    """
    if free_flow <= 0:
        return 50.0
    ratio = max(0.0, min(1.0, current / free_flow))
    return round((1.0 - ratio) * 90.0 + 5.0, 1)


# Route planning deliberately retains the established 15 spatial areas. The
# corridor map uses all 30 candidates below through get_corridor_map_hotspots;
# expanding the map catalogue therefore cannot silently change A* behavior.
_ROUTING_ZONE_IDS = (
    "zone-simpang-jam",
    "zone-panbil",
    "zone-nagoya",
    "zone-kepri-mall",
    "zone-batam-centre",
    "zone-sekupang",
    "zone-batu-ampar",
    "zone-batamindo",
    "zone-kabil",
    "zone-kara",
    "zone-kda",
    "zone-cikitsu",
    "zone-vitka-tiban",
    "zone-basecamp",
    "zone-fanindo",
)
_MAP_HOTSPOT_BY_ID = {
    candidate["zone_id"]: candidate
    for candidate in CORRIDOR_MAP_HOTSPOTS_CONFIG
}
CONGESTION_ZONES_CONFIG = [
    _MAP_HOTSPOT_BY_ID[zone_id] for zone_id in _ROUTING_ZONE_IDS
]


def _peak_window_active(hour: int, windows: List[tuple]) -> bool:
    """Return whether a local hour falls inside one configured half-open window."""
    return any(
        start <= hour < end if start <= end else hour >= start or hour < end
        for start, end in windows
    )


def _level_payload(congestion_index: float) -> Dict[str, Any]:
    """Return the compatibility classification fields for a bounded score."""
    if congestion_index >= 70.0:
        return {
            "level": "SUPER_CONGESTED",
            "color": "#ef4444",
            "avoid_recommended": True,
        }
    if congestion_index >= 40.0:
        return {
            "level": "HEAVY",
            "color": "#f59e0b",
            "avoid_recommended": False,
        }
    return {
        "level": "SMOOTH",
        "color": "#10b981",
        "avoid_recommended": False,
    }


def _resolved_corridor_signals(
    corridor_scores: Optional[Mapping[str, Any]],
    telemetry: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Normalize provider/model corridor inputs for explainable zone scoring."""
    methodology = CORRIDOR_MAP_HOTSPOT_METHODOLOGY
    source_confidence = methodology["source_confidence"]
    resolved: Dict[str, Dict[str, Any]] = {}
    for corridor_id in _CORRIDOR_POINTS:
        fallback = telemetry.get(corridor_id, {})
        provided = (corridor_scores or {}).get(corridor_id)
        if isinstance(provided, Mapping):
            score = provided.get("congestion_index")
            source = str(provided.get("source", "simulated"))
            raw_provider_confidence = provided.get("provider_confidence")
        else:
            score = provided
            source = "simulated"
            raw_provider_confidence = None
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            score = fallback.get("live_congestion_score", 30.0)
            source = "simulated"
            raw_provider_confidence = None
        bounded_score = max(0.0, min(100.0, float(score)))

        if source == "tomtom_live":
            provider_confidence = (
                float(raw_provider_confidence)
                if isinstance(raw_provider_confidence, (int, float))
                and not isinstance(raw_provider_confidence, bool)
                and math.isfinite(float(raw_provider_confidence))
                else 0.0
            )
            provider_confidence = max(0.0, min(1.0, provider_confidence))
            confidence = (
                float(source_confidence["tomtom_live_policy"])
                * provider_confidence
            )
        else:
            source = "simulated"
            provider_confidence = None
            confidence = float(source_confidence["simulated"])

        resolved[corridor_id] = {
            "congestion_index": bounded_score,
            "source": source,
            "confidence": confidence,
            "provider_confidence": provider_confidence,
        }
    return resolved


def _score_map_hotspot(
    cfg: Mapping[str, Any],
    now: datetime,
    signals: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build one modelled map hotspot with auditable dynamic/ranking scores."""
    signal_mix = cfg["signal_mix"]
    corridor_pressure = sum(
        float(signals[corridor_id]["congestion_index"]) * float(weight)
        for corridor_id, weight in signal_mix.items()
    )
    weighted_signal_confidence = sum(
        float(signals[corridor_id]["confidence"]) * float(weight)
        for corridor_id, weight in signal_mix.items()
    )
    corridor_weight = min(0.85, max(0.0, weighted_signal_confidence))
    recurring_weight = 1.0 - corridor_weight
    peak_active = _peak_window_active(now.hour, cfg["peak_windows"])
    peak_lift = (
        float(CORRIDOR_MAP_HOTSPOT_METHODOLOGY["dynamic_congestion"][
            "active_peak_lift"
        ])
        if peak_active else 0.0
    )
    recurring_pressure = max(
        0.0,
        min(100.0, float(cfg["base_score"]) + peak_lift),
    )
    corridor_contribution = corridor_weight * corridor_pressure
    recurring_contribution = recurring_weight * recurring_pressure
    raw_congestion_index = max(
        0.0,
        min(100.0, corridor_contribution + recurring_contribution),
    )
    published_score = round(raw_congestion_index, 1)

    evidence_confidence = 100.0 * weighted_signal_confidence
    weights = HOTSPOT_SELECTION_WEIGHTS
    selection_components = {
        "corridor_pressure": float(weights["corridor_pressure"]) * corridor_pressure,
        "recurrence": float(weights["recurrence"]) * recurring_pressure,
        "network_criticality": (
            float(weights["network_criticality"])
            * float(cfg["network_criticality"])
        ),
        "demand_exposure": (
            float(weights["demand_exposure"])
            * float(cfg["demand_exposure"])
        ),
        "evidence_confidence": (
            float(weights["evidence_confidence"]) * evidence_confidence
        ),
    }
    selection_score = round(sum(selection_components.values()), 3)
    input_signals = {
        corridor_id: {
            "affinity_weight": float(weight),
            "congestion_index": round(
                float(signals[corridor_id]["congestion_index"]), 1,
            ),
            "source": signals[corridor_id]["source"],
            "confidence": round(float(signals[corridor_id]["confidence"]), 4),
        }
        for corridor_id, weight in signal_mix.items()
    }
    score_breakdown = {
        "methodology_version": CORRIDOR_MAP_HOTSPOT_METHODOLOGY[
            "methodology_version"
        ],
        "corridor_pressure": round(corridor_pressure, 6),
        "recurring_pressure": round(recurring_pressure, 6),
        "corridor_weight": round(corridor_weight, 6),
        "recurring_weight": round(recurring_weight, 6),
        "corridor_contribution": round(corridor_contribution, 6),
        "recurring_contribution": round(recurring_contribution, 6),
        "raw_congestion_index": round(raw_congestion_index, 6),
        "published_congestion_index": published_score,
        "peak_lift": peak_lift,
        "weighted_signal_confidence": round(weighted_signal_confidence, 6),
        "observed": False,
        "input_signals": input_signals,
    }
    selection_breakdown = {
        "weights": copy.deepcopy(weights),
        "inputs": {
            "corridor_pressure": round(corridor_pressure, 6),
            "recurrence": round(recurring_pressure, 6),
            "network_criticality": float(cfg["network_criticality"]),
            "demand_exposure": float(cfg["demand_exposure"]),
            "evidence_confidence": round(evidence_confidence, 6),
        },
        "weighted_components": {
            key: round(value, 6) for key, value in selection_components.items()
        },
    }

    return {
        "zone_id": cfg["zone_id"],
        "name": cfg["name"],
        "lat": cfg["lat"],
        "lng": cfg["lng"],
        "radius_m": cfg["radius_m"],
        "congestion_index": published_score,
        **_level_payload(published_score),
        "category": cfg["category"],
        "corridor_ids": list(signal_mix),
        "peak_active": peak_active,
        "source": "modelled_spatial_hotspot",
        "observed": False,
        "routing_enabled": bool(cfg["routing_enabled"]),
        "base_score": float(cfg["base_score"]),
        "network_criticality": float(cfg["network_criticality"]),
        "demand_exposure": float(cfg["demand_exposure"]),
        "selection_score": selection_score,
        "selection_breakdown": selection_breakdown,
        "score_breakdown": score_breakdown,
        **_emissions_pressure(published_score),
    }


def get_corridor_map_hotspots(
    at_time: Optional[datetime] = None,
    weather: int = 0,
    corridor_scores: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return the ranked 30-area modelled catalogue used by the corridor map."""
    from services.simulator import get_live_corridor_telemetry

    now = (at_time or clock.now()).astimezone(clock.BATAM_TZ)
    telemetry = {
        corridor["id"]: corridor
        for corridor in get_live_corridor_telemetry(
            now,
            weather=weather,
            include_route_distance=False,
        )
    }
    signals = _resolved_corridor_signals(corridor_scores, telemetry)
    candidates = [
        _score_map_hotspot(candidate, now, signals)
        for candidate in CORRIDOR_MAP_HOTSPOTS_CONFIG
    ]
    candidates.sort(key=lambda zone: (-zone["selection_score"], zone["zone_id"]))
    selected = candidates[:int(CORRIDOR_MAP_HOTSPOT_METHODOLOGY["selection_limit"])]
    for rank, zone in enumerate(selected, start=1):
        zone["selection_rank"] = rank
        zone["watch_priority"] = "CRITICAL" if rank <= 20 else "HEAVY"
    return selected


def _emissions_pressure(congestion_index: float) -> Dict[str, Any]:
    """Return the bounded queue-pressure indicator used by map overlays."""
    bounded_score = max(0.0, min(100.0, congestion_index))
    queue_pressure_factor = (bounded_score / 100.0) ** 2
    pressure_index = 100.0 * queue_pressure_factor
    if pressure_index >= EMISSIONS_PRESSURE_HIGH_THRESHOLD:
        level = "HIGH"
    elif pressure_index >= EMISSIONS_PRESSURE_ELEVATED_THRESHOLD:
        level = "ELEVATED"
    else:
        level = "LOW"
    return {
        "modeled_emissions_pressure": {
            "index": round(pressure_index, 1),
            "queue_pressure_factor": round(queue_pressure_factor, 4),
            "level": level,
            "metric": "relative_queue_emissions_pressure",
            "unit": "index_0_100",
            "observed": False,
        },
    }


def get_congestion_zones(
    at_time: Optional[datetime] = None,
    weather: int = 0,
) -> List[Dict[str, Any]]:
    """Return spatial congestion zones for a requested planning time.

    With no arguments this preserves the live-dashboard behavior. Route
    planning passes its requested future departure and weather so spatial
    weights are not accidentally based on the wall clock.
    
    Levels:
      - SUPER_CONGESTED (>= 70): Red zone (#ef4444). AI routing avoids these places!
      - HEAVY (40-69): Amber/Orange zone (#f59e0b).
      - SMOOTH (< 40): Green zone (#10b981).
    """
    from services.simulator import get_live_corridor_telemetry
    now = at_time or clock.now()
    telemetry = {
        c["id"]: c for c in get_live_corridor_telemetry(
            now,
            weather=weather,
            include_route_distance=False,
        )
    }
    zones = []

    for cfg in CONGESTION_ZONES_CONFIG:
        signal_mix = cfg["signal_mix"]
        corridor_score = sum(
            float(telemetry.get(cid, {}).get(
                "live_congestion_score", cfg["base_score"],
            )) * float(weight)
            for cid, weight in signal_mix.items()
        )

        peak_active = _peak_window_active(now.hour, cfg["peak_windows"])
        # Current corridor movement is the strongest signal, but each junction
        # retains its own recurring baseline. A small peak lift makes local
        # school/shift/retail patterns visible without inventing live sensors.
        live_score = (
            HOTSPOT_CORRIDOR_SIGNAL_WEIGHT * corridor_score
            + HOTSPOT_BASELINE_WEIGHT * float(cfg["base_score"])
        )
        if peak_active:
            live_score += HOTSPOT_ACTIVE_PEAK_LIFT
        live_score = max(0.0, min(100.0, live_score))
        
        published_score = round(live_score, 1)
        zones.append({
            "zone_id": cfg["zone_id"],
            "name": cfg["name"],
            "lat": cfg["lat"],
            "lng": cfg["lng"],
            "radius_m": cfg["radius_m"],
            "congestion_index": published_score,
            **_level_payload(published_score),
            "category": cfg["category"],
            "corridor_ids": list(signal_mix),
            "peak_active": peak_active,
            "source": "modelled_spatial_hotspot",
            **_emissions_pressure(published_score),
        })

    return zones


def get_live_traffic() -> Dict[str, Any]:
    """Return corridor-level speed and congestion data, plus spatial congestion zones.

    Tries TomTom for each corridor; fills remaining corridors (or all, if no
    key) from the simulator. Provenance is per-corridor.
    """
    from services.simulator import get_live_corridor_telemetry
    now = clock.now()
    sim_telemetry = {
        c["id"]: c for c in get_live_corridor_telemetry(
            now,
            include_route_distance=False,
        )
    }
    tomtom_key = _tomtom_api_key()
    tomtom_segments = _tomtom_segments(tomtom_key)

    segments: List[Dict[str, Any]] = []
    overall_source = "simulated"

    for corridor_id, (lat, lng) in _CORRIDOR_POINTS.items():
        sim = sim_telemetry.get(corridor_id, {})
        tt = tomtom_segments.get(corridor_id)

        if tt:
            index = _congestion_from_speed_ratio(
                tt["current_speed_kmh"], tt["free_flow_speed_kmh"]
            )
            source = "tomtom_live"
            provider_confidence = max(0.0, min(1.0, float(tt["confidence"])))
            overall_source = "tomtom_live"
        else:
            index = sim.get("live_congestion_score", 30.0)
            source = "simulated"
            provider_confidence = None

        segments.append({
            "corridor_id": corridor_id,
            "lat": lat,
            "lng": lng,
            "congestion_index": index,
            "current_speed_kmh": tt["current_speed_kmh"] if tt else None,
            "free_flow_speed_kmh": tt["free_flow_speed_kmh"] if tt else None,
            "provider_confidence": provider_confidence,
            "source": source,
        })

    resolved_scores = {
        segment["corridor_id"]: {
            "congestion_index": segment["congestion_index"],
            "source": segment["source"],
            "provider_confidence": segment["provider_confidence"],
        }
        for segment in segments
    }
    zones = get_corridor_map_hotspots(now, corridor_scores=resolved_scores)
    level_counts = {
        level: sum(zone["level"] == level for zone in zones)
        for level in ("SMOOTH", "HEAVY", "SUPER_CONGESTED")
    }
    emissions_pressure_level_counts = {
        level: sum(
            zone["modeled_emissions_pressure"]["level"] == level
            for zone in zones
        )
        for level in ("LOW", "ELEVATED", "HIGH")
    }

    return {
        "segments": segments,
        "zones": zones,
        "coverage": {
            "hotspot_count": len(zones),
            "level_counts": level_counts,
            "emissions_pressure_level_counts": emissions_pressure_level_counts,
            "method": (
                "30 backend-owned modelled areas ranked from corridor pressure, "
                "recurrence, network criticality, demand exposure and evidence "
                "confidence"
            ),
            "methodology": copy.deepcopy(CORRIDOR_MAP_HOTSPOT_METHODOLOGY),
            "catalog_version": CORRIDOR_HOTSPOT_CATALOG["catalog_version"],
            "emissions_pressure_model": EMISSIONS_PRESSURE_MODEL,
        },
        "overall_source": overall_source,
        "tomtom_key_configured": bool(tomtom_key),
    }
