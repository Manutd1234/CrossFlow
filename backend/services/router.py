"""A* shortest-path routing over the real Batam road network.

The graph is OpenStreetMap data, baked to backend/data/batam_graph.json by
scripts/build_graph.py and committed. Nothing here touches the network at
runtime — a live Overpass call would put venue wifi on the demo's critical path.

A* is implemented directly rather than pulled from networkx: it keeps the
runtime dependency footprint small, and the heuristic below is the part worth
being able to explain.
"""

import copy
import hashlib
import heapq
import json
import math
import os
import re
from collections import ChainMap
from dataclasses import dataclass
from functools import lru_cache
from time import monotonic
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from services.alt_index import AltIndex
from services.route_learning_store import (
    DEFAULT_ROUTE_LEARNING_STORE,
    MAX_CLEAR_CONGESTION_SCORE,
    LearnedEdge,
    LearningSnapshot,
)
from services.service_contracts import ApprovedGraphOverrideSnapshot


route_learning_store = DEFAULT_ROUTE_LEARNING_STORE

_GRAPH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "batam_graph.json",
)
_ALT_INDEX_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "alt",
)

EARTH_RADIUS_M = 6371008.8
ENDPOINT_DESTINATION_ACCESS_RADIUS_M = 1_500.0

# Alternative routes are a presentation feature and must not be allowed to
# turn the primary route into an unbounded request.  These defaults are
# intentionally conservative; deployments can tune them without changing the
# route response contract, and callers can override them per request.
DEFAULT_ALTERNATIVE_MAX_SEARCHES = 3
# A route search itself may take several seconds on the full Batam graph;
# this budget still removes the old seven-attempt tail while preserving the
# existing expectation that ordinary routes can usually produce one option.
DEFAULT_ALTERNATIVE_TIME_BUDGET_MS = 10_000.0
DEFAULT_ALTERNATIVE_MAX_SETTLED_STATES = 250_000


def _bounded_int_setting(
    value: Optional[int], env_name: str, default: int,
) -> int:
    """Resolve a non-negative integer budget from a request or environment."""
    if value is None:
        raw = os.environ.get(env_name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
    return max(0, int(value))


def _bounded_float_setting(
    value: Optional[float], env_name: str, default: float,
) -> float:
    """Resolve a finite non-negative duration budget."""
    if value is None:
        raw = os.environ.get(env_name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
    value = float(value)
    return value if math.isfinite(value) and value >= 0.0 else default


def _alternative_budget(
    max_searches: Optional[int],
    time_budget_ms: Optional[float],
    max_settled_states: Optional[int],
) -> Tuple[int, float, int]:
    return (
        _bounded_int_setting(
            max_searches, "CROSSFLOW_ALTERNATIVE_MAX_SEARCHES",
            DEFAULT_ALTERNATIVE_MAX_SEARCHES,
        ),
        _bounded_float_setting(
            time_budget_ms, "CROSSFLOW_ALTERNATIVE_TIME_BUDGET_MS",
            DEFAULT_ALTERNATIVE_TIME_BUDGET_MS,
        ),
        _bounded_int_setting(
            max_settled_states, "CROSSFLOW_ALTERNATIVE_MAX_SETTLED_STATES",
            DEFAULT_ALTERNATIVE_MAX_SETTLED_STATES,
        ),
    )

# Additional named places snapped to the committed graph.  Keeping these ids
# here makes the expanded point-to-point planner available immediately without
# requiring a 9 MB graph rebuild during a demo.  scripts/build_graph.py carries
# the same locations so the mapping is regenerated when OSM data is refreshed.
#
# Hang Nadim deliberately uses the nearest *bidirectionally reachable* access
# node.  The old closest node sat on an outbound-only airport service lane,
# which made every route *to* the airport fail despite routes from it working.
_LANDMARK_OVERRIDES = {
    "hang_nadim": 5520644012,
    "harbour_bay": 7569279957,
    "panbil_mall": 702367827,
    "kabil_industrial": 10747602822,
    "batu_aji": 5985661280,
    "tiban": 702360604,
    "kepri_mall": 5254621192,
}


@dataclass(frozen=True)
class RoadEdge:
    """One directed graph traversal with optional OSM constraint metadata.

    The first eight fields are the legacy graph contract. The remaining fields
    are deliberately optional so the committed v2 graph and reviewed runtime
    shortcut overlays can share one traversal policy without inventing facts
    for older edges.
    """

    target: int
    distance_m: float
    road_index: int = -1
    name: Optional[str] = None
    ref: Optional[str] = None
    highway: Optional[str] = None
    junction: Optional[str] = None
    access: Optional[str] = None
    vehicle: Optional[str] = None
    motor_vehicle: Optional[str] = None
    motorcar: Optional[str] = None
    motorcycle: Optional[str] = None
    hgv: Optional[str] = None
    ferry: Optional[str] = None
    surface: Optional[str] = None
    smoothness: Optional[str] = None
    width_m: Optional[float] = None
    maxweight_t: Optional[float] = None
    maxheight_m: Optional[float] = None
    service: Optional[str] = None
    maxspeed_kph: Optional[float] = None
    lanes: Optional[float] = None
    road_quality: Optional[float] = None
    vehicle_modes: Tuple[str, ...] = ()
    approved_override_id: Optional[str] = None
    approved_duration_s: Optional[float] = None
    approved_geometry: Tuple[Tuple[float, float], ...] = ()
    approved_candidate_sha256: Optional[str] = None


@dataclass
class PathResult:
    """Exact unsimplified A* result used for geometry and navigation."""

    nodes: List[int]
    edges: List[RoadEdge]
    distance_m: float
    # The selected preference's objective cost is stored in weighted seconds,
    # never metres. Physical distance and modeled travel time are calculated
    # independently from the chosen edges.
    search_cost_s: float


@dataclass(frozen=True)
class VehicleProfile:
    """Auditable assumptions used by both path selection and ETA estimates.

    The committed OSM extract has road class but does not include lane-level
    speeds, height/weight limits, or turn-restriction relations.  Profiles
    therefore express preferences, not legal clearance for a particular
    vehicle. All profiles remain on the same public motor-road graph.
    """

    key: str
    name: str
    max_speed_kph: float
    speed_factor: float
    congestion_sensitivity: float
    weather_sensitivity: float
    residential_penalty: float
    unclassified_penalty: float
    tertiary_penalty: float
    link_penalty: float
    turn_penalty_s: float
    short_maneuver_penalty_s: float
    signal_delay_s: float
    customs_buffer_mins: float
    emissions_kg_per_km: float
    idle_emissions_kg_per_hour: float


@dataclass(frozen=True)
class MultiAttributeCostWeights:
    """Nonnegative profile weights for the generalized edge objective."""

    time: float
    distance: float
    congestion: float
    road_quality: float


@dataclass(frozen=True)
class VehicleRoutingPolicy:
    """Physical constraints and defaults shared by related vehicle profiles."""

    key: str
    name: str
    cost_weights: MultiAttributeCostWeights
    allowed_highways: Tuple[str, ...]
    min_width_m: Optional[float]
    vehicle_height_m: Optional[float]
    gross_weight_t: Optional[float]
    unrated_road_quality: float
    risk_aversion: float


@dataclass(frozen=True)
class EdgeCongestionEstimate:
    """Provider-neutral historical/realtime congestion input for one edge.

    Agent 4 or another caller can derive these values from multi-year history
    without coupling the graph core to a storage implementation. A value of
    ``1.0`` with zero uncertainty is the backward-compatible free-flow input.
    """

    expected_speed_ratio: float = 1.0
    speed_ratio_std: float = 0.0
    p90_speed_ratio: Optional[float] = None
    p90_delay_s: float = 0.0
    source: str = "unspecified"

    def __post_init__(self) -> None:
        values = (self.expected_speed_ratio, self.speed_ratio_std, self.p90_delay_s)
        if self.p90_speed_ratio is not None:
            values += (self.p90_speed_ratio,)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Congestion estimate values must be finite.")
        if not 0.0 < self.expected_speed_ratio <= 1.0:
            raise ValueError("expected_speed_ratio must be in (0, 1].")
        if self.p90_speed_ratio is not None and not (
            0.0 < self.p90_speed_ratio <= self.expected_speed_ratio
        ):
            raise ValueError(
                "p90_speed_ratio must be in (0, expected_speed_ratio]."
            )
        if self.speed_ratio_std < 0.0 or self.p90_delay_s < 0.0:
            raise ValueError("Congestion uncertainty cannot be negative.")


@dataclass(frozen=True)
class EdgeTraversalDecision:
    """Auditable vehicle-constraint result for one edge."""

    allowed: bool
    reason: str
    checked_constraints: Tuple[str, ...]


@dataclass(frozen=True)
class RoutingView:
    """Immutable vehicle-filtered view of one graph/override snapshot.

    ``adjacency`` contains only nodes and directed edges that pass the static
    vehicle policy.  Endpoint-only ``destination`` access is intentionally not
    included: searches that need that trip-specific exception fall back to the
    raw snapshot adjacency in :func:`astar_detailed`.
    """

    adjacency: Mapping[int, Tuple[RoadEdge, ...]]
    core: frozenset[int]


@dataclass(frozen=True)
class StaticEdgeFeatures:
    """Request-independent edge values reused by the hot cost loop."""

    physical_floor_s: float
    baseline_free_flow_s: float
    distance_proxy_s: float
    road_quality: float
    congestion_exposure: float
    weather_exposure: float
    suitability_multiplier: float
    through_suitability_multiplier: float


@dataclass(frozen=True)
class RoutePreferenceProfile:
    """Published nonnegative weights for one A* route objective.

    ``distance_proxy`` converts edge metres to seconds at the selected
    vehicle's maximum speed. It is only a search normalization: raw road
    distance and modeled ETA continue to be reported from their own units.
    """

    key: str
    name: str
    description: str
    distance_proxy: float
    free_flow: float
    congestion: float
    weather: float
    maneuver: float
    suitability: float
    eligible_vehicle_types: Tuple[str, ...] = ()
    road_scope: str = "STANDARD"


# Baseline speeds are transparent road-class proxies, not live observed speeds.
# They are intentionally below typical legal maxima for an urban planning ETA.
HIGHWAY_SPEED_KPH: Dict[str, float] = {
    "motorway": 80.0,
    "trunk": 65.0,
    "primary": 55.0,
    "secondary": 45.0,
    "tertiary": 38.0,
    "unclassified": 30.0,
    "residential": 25.0,
    "living_street": 12.0,
    "service": 18.0,
    "track": 14.0,
    "ferry": 35.0,
    "road": 25.0,
}


_PUBLIC_ROAD_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "track",
    "road",
)

# These four policies are the stable routing modes used by road graph builds,
# future reviewed shortcut overlays, and maritime-link adapters. API vehicle
# profiles map onto one of them without changing existing profile identifiers.
VEHICLE_ROUTING_POLICIES: Dict[str, VehicleRoutingPolicy] = {
    "MOTORCYCLE": VehicleRoutingPolicy(
        "MOTORCYCLE", "Motorcycle",
        MultiAttributeCostWeights(1.00, 0.12, 0.72, 0.45),
        _PUBLIC_ROAD_CLASSES, min_width_m=0.9, vehicle_height_m=1.9,
        gross_weight_t=0.4, unrated_road_quality=0.68, risk_aversion=0.35,
    ),
    "PASSENGER_CAR": VehicleRoutingPolicy(
        "PASSENGER_CAR", "Passenger car",
        MultiAttributeCostWeights(1.00, 0.05, 1.00, 1.00),
        _PUBLIC_ROAD_CLASSES, min_width_m=2.2, vehicle_height_m=2.1,
        gross_weight_t=3.5, unrated_road_quality=0.72, risk_aversion=0.75,
    ),
    "FREIGHT_TRUCK": VehicleRoutingPolicy(
        "FREIGHT_TRUCK", "Freight / truck",
        MultiAttributeCostWeights(1.00, 0.04, 1.25, 1.70),
        (
            "motorway", "trunk", "primary", "secondary", "tertiary",
            "unclassified", "residential", "service", "road",
        ),
        min_width_m=2.8, vehicle_height_m=4.2, gross_weight_t=25.0,
        unrated_road_quality=0.66, risk_aversion=1.25,
    ),
    "FERRY_MARITIME": VehicleRoutingPolicy(
        "FERRY_MARITIME", "Ferry / maritime link",
        MultiAttributeCostWeights(1.00, 0.03, 0.85, 0.40),
        ("ferry",), min_width_m=None, vehicle_height_m=None,
        gross_weight_t=None, unrated_road_quality=0.85, risk_aversion=0.90,
    ),
}


_ROUTING_MODE_BY_PROFILE = {
    "MOTORCYCLE": "MOTORCYCLE",
    "LIGHT_TRUCK": "FREIGHT_TRUCK",
    "CARGO_TRUCK": "FREIGHT_TRUCK",
}


_PROFILE_DIMENSIONS: Dict[str, Tuple[Optional[float], Optional[float], Optional[float]]] = {
    # width (m), height (m), gross weight (t); deliberately conservative
    "MOTORCYCLE": (0.9, 1.9, 0.4),
    "COMMUTER": (2.0, 2.1, 3.5),
    "ELECTRIC_CAR": (2.0, 2.1, 3.5),
    "EXPRESS_VAN": (2.2, 2.8, 5.0),
    "MINIBUS": (2.4, 3.2, 8.0),
    "CITY_BUS": (2.6, 3.8, 18.0),
    "LIGHT_TRUCK": (2.5, 3.5, 12.0),
    "CARGO_TRUCK": (2.8, 4.2, 25.0),
}


_HIGHWAY_QUALITY_DEFAULTS = {
    "motorway": 0.94, "trunk": 0.91, "primary": 0.88,
    "secondary": 0.83, "tertiary": 0.78, "unclassified": 0.70,
    "residential": 0.72, "living_street": 0.65, "service": 0.60,
    "track": 0.42, "ferry": 0.85,
}

_SURFACE_QUALITY = {
    "asphalt": 0.95, "concrete": 0.90, "concrete:plates": 0.84,
    "paving_stones": 0.78, "sett": 0.72, "compacted": 0.66,
    "fine_gravel": 0.62, "gravel": 0.52, "pebblestone": 0.46,
    "ground": 0.40, "dirt": 0.34, "earth": 0.32, "sand": 0.22,
    "mud": 0.12,
}

_SMOOTHNESS_QUALITY = {
    "excellent": 1.00, "good": 0.90, "intermediate": 0.75,
    "bad": 0.58, "very_bad": 0.42, "horrible": 0.28,
    "very_horrible": 0.15, "impassable": 0.0,
}


def _profile(
    key: str,
    name: str,
    *,
    max_speed_kph: float,
    speed_factor: float,
    congestion_sensitivity: float,
    weather_sensitivity: float,
    residential_penalty: float,
    unclassified_penalty: float,
    tertiary_penalty: float,
    link_penalty: float,
    turn_penalty_s: float,
    short_maneuver_penalty_s: float,
    signal_delay_s: float,
    customs_buffer_mins: float,
    emissions_kg_per_km: float,
    idle_emissions_kg_per_hour: float,
) -> VehicleProfile:
    return VehicleProfile(
        key, name, max_speed_kph, speed_factor, congestion_sensitivity,
        weather_sensitivity, residential_penalty, unclassified_penalty,
        tertiary_penalty, link_penalty, turn_penalty_s,
        short_maneuver_penalty_s, signal_delay_s, customs_buffer_mins,
        emissions_kg_per_km, idle_emissions_kg_per_hour,
    )


# One source of truth for API validation, A*, ETA, emissions and the frontend
# catalog. Values are planning assumptions and are returned with every result.
VEHICLE_PROFILES: Dict[str, VehicleProfile] = {
    "COMMUTER": _profile(
        "COMMUTER", "Car / taxi", max_speed_kph=80, speed_factor=1.00,
        congestion_sensitivity=1.00, weather_sensitivity=1.00,
        residential_penalty=1.08, unclassified_penalty=1.05,
        tertiary_penalty=1.02, link_penalty=1.03, turn_penalty_s=6,
        short_maneuver_penalty_s=8, signal_delay_s=18,
        customs_buffer_mins=0, emissions_kg_per_km=0.21,
        idle_emissions_kg_per_hour=1.8,
    ),
    "ELECTRIC_CAR": _profile(
        "ELECTRIC_CAR", "Electric car", max_speed_kph=80, speed_factor=1.00,
        congestion_sensitivity=0.92, weather_sensitivity=1.05,
        residential_penalty=1.08, unclassified_penalty=1.05,
        tertiary_penalty=1.02, link_penalty=1.03, turn_penalty_s=5,
        short_maneuver_penalty_s=7, signal_delay_s=17,
        customs_buffer_mins=0, emissions_kg_per_km=0.045,
        idle_emissions_kg_per_hour=0.10,
    ),
    "MOTORCYCLE": _profile(
        "MOTORCYCLE", "Motorcycle", max_speed_kph=75, speed_factor=1.03,
        congestion_sensitivity=0.62, weather_sensitivity=1.45,
        residential_penalty=1.00, unclassified_penalty=1.00,
        tertiary_penalty=1.00, link_penalty=1.00, turn_penalty_s=3,
        short_maneuver_penalty_s=2, signal_delay_s=10,
        customs_buffer_mins=5, emissions_kg_per_km=0.09,
        idle_emissions_kg_per_hour=0.45,
    ),
    "EXPRESS_VAN": _profile(
        "EXPRESS_VAN", "Express van", max_speed_kph=75, speed_factor=0.94,
        congestion_sensitivity=1.05, weather_sensitivity=1.08,
        residential_penalty=1.18, unclassified_penalty=1.12,
        tertiary_penalty=1.05, link_penalty=1.06, turn_penalty_s=8,
        short_maneuver_penalty_s=12, signal_delay_s=20,
        customs_buffer_mins=10, emissions_kg_per_km=0.27,
        idle_emissions_kg_per_hour=2.2,
    ),
    "MINIBUS": _profile(
        "MINIBUS", "Minibus / shuttle", max_speed_kph=70, speed_factor=0.90,
        congestion_sensitivity=1.08, weather_sensitivity=1.10,
        residential_penalty=1.25, unclassified_penalty=1.16,
        tertiary_penalty=1.08, link_penalty=1.10, turn_penalty_s=10,
        short_maneuver_penalty_s=15, signal_delay_s=22,
        customs_buffer_mins=0, emissions_kg_per_km=0.32,
        idle_emissions_kg_per_hour=2.4,
    ),
    "CITY_BUS": _profile(
        "CITY_BUS", "City bus", max_speed_kph=60, speed_factor=0.78,
        congestion_sensitivity=1.15, weather_sensitivity=1.12,
        residential_penalty=1.70, unclassified_penalty=1.45,
        tertiary_penalty=1.16, link_penalty=1.18, turn_penalty_s=14,
        short_maneuver_penalty_s=24, signal_delay_s=28,
        customs_buffer_mins=0, emissions_kg_per_km=0.72,
        idle_emissions_kg_per_hour=3.5,
    ),
    "LIGHT_TRUCK": _profile(
        "LIGHT_TRUCK", "Light cargo truck", max_speed_kph=68,
        speed_factor=0.86, congestion_sensitivity=1.12,
        weather_sensitivity=1.14, residential_penalty=1.42,
        unclassified_penalty=1.28, tertiary_penalty=1.12,
        link_penalty=1.14, turn_penalty_s=12,
        short_maneuver_penalty_s=19, signal_delay_s=25,
        customs_buffer_mins=18, emissions_kg_per_km=0.48,
        idle_emissions_kg_per_hour=2.8,
    ),
    "CARGO_TRUCK": _profile(
        "CARGO_TRUCK", "Heavy freight truck", max_speed_kph=55,
        speed_factor=0.72, congestion_sensitivity=1.22,
        weather_sensitivity=1.22, residential_penalty=2.20,
        unclassified_penalty=1.80, tertiary_penalty=1.28,
        link_penalty=1.25, turn_penalty_s=17,
        short_maneuver_penalty_s=32, signal_delay_s=32,
        customs_buffer_mins=25, emissions_kg_per_km=1.05,
        idle_emissions_kg_per_hour=4.0,
    ),
}


# Nonnegative component weights keep the straight-line A* lower bound
# admissible. BALANCED is exactly the generalized-time objective used before
# route preferences were exposed, preserving default route behavior.
ROUTE_PREFERENCES: Dict[str, RoutePreferenceProfile] = {
    "BALANCED": RoutePreferenceProfile(
        "BALANCED", "Balanced", "Balance modeled ETA and road suitability.",
        distance_proxy=0.0, free_flow=1.0, congestion=1.0, weather=1.0,
        maneuver=1.0, suitability=1.0,
    ),
    "FASTEST": RoutePreferenceProfile(
        "FASTEST", "Fastest", "Minimize modeled travel time.",
        distance_proxy=0.0, free_flow=1.0, congestion=1.0, weather=1.0,
        maneuver=1.0, suitability=0.0,
    ),
    "SHORTEST": RoutePreferenceProfile(
        "SHORTEST", "Shortest", "Minimize physical road distance.",
        distance_proxy=1.0, free_flow=0.0, congestion=0.0, weather=0.0,
        maneuver=0.0, suitability=0.0,
    ),
    "EASY": RoutePreferenceProfile(
        "EASY", "Easy", "Prefer through roads with fewer difficult maneuvers.",
        distance_proxy=0.0, free_flow=1.0, congestion=1.0, weather=1.0,
        maneuver=4.0, suitability=2.5,
    ),
    "LOCAL": RoutePreferenceProfile(
        "LOCAL", "Local shortcuts",
        "Seek compact routes over mapped public residential roads.",
        distance_proxy=2.0, free_flow=0.05, congestion=0.65, weather=0.30,
        maneuver=0.15, suitability=0.0,
        eligible_vehicle_types=("COMMUTER", "ELECTRIC_CAR", "MOTORCYCLE"),
        road_scope="MAPPED_PUBLIC_LOCAL",
    ),
}


def vehicle_profile(vehicle_type: str = "COMMUTER") -> VehicleProfile:
    try:
        return VEHICLE_PROFILES[vehicle_type]
    except KeyError as exc:
        allowed = ", ".join(VEHICLE_PROFILES)
        raise ValueError(
            f"Unknown vehicle type: {vehicle_type}. Expected one of: {allowed}."
        ) from exc


def vehicle_routing_policy(
    vehicle: VehicleProfile | str = "COMMUTER",
) -> VehicleRoutingPolicy:
    """Resolve an API profile or canonical mode to its routing policy."""
    if isinstance(vehicle, VehicleProfile):
        key = _ROUTING_MODE_BY_PROFILE.get(vehicle.key, "PASSENGER_CAR")
    elif vehicle in VEHICLE_ROUTING_POLICIES:
        key = vehicle
    else:
        profile = vehicle_profile(vehicle)
        key = _ROUTING_MODE_BY_PROFILE.get(profile.key, "PASSENGER_CAR")
    return VEHICLE_ROUTING_POLICIES[key]


def vehicle_routing_policy_payload(
    policy: VehicleRoutingPolicy,
) -> Dict[str, Any]:
    """Publish physical constraints and the four-term objective contract."""
    weights = policy.cost_weights
    return {
        "id": policy.key,
        "name": policy.name,
        "cost_function": (
            "C(e) = w_t*time + w_d*distance + w_c*congestion + "
            "w_r*road_quality_penalty"
        ),
        "cost_weights": {
            "time": weights.time,
            "distance": weights.distance,
            "congestion": weights.congestion,
            "road_quality": weights.road_quality,
        },
        "allowed_highways": list(policy.allowed_highways),
        "minimum_width_m": policy.min_width_m,
        "vehicle_height_m": policy.vehicle_height_m,
        "gross_weight_t": policy.gross_weight_t,
        "unrated_road_quality": policy.unrated_road_quality,
        "risk_aversion": policy.risk_aversion,
        "restriction_policy": (
            "Explicit access, mode, road-class, width, height, weight, and "
            "impassable-surface restrictions are enforced. Missing tags use "
            "the published profile defaults."
        ),
    }


def vehicle_profile_payload(profile: VehicleProfile) -> Dict[str, Any]:
    """JSON-safe assumptions published with each route result."""
    policy = vehicle_routing_policy(profile)
    dimensions = _PROFILE_DIMENSIONS[profile.key]
    policy_payload = vehicle_routing_policy_payload(policy)
    return {
        "id": profile.key,
        "name": profile.name,
        "max_speed_kph": profile.max_speed_kph,
        "speed_factor": profile.speed_factor,
        "congestion_sensitivity": profile.congestion_sensitivity,
        "weather_sensitivity": profile.weather_sensitivity,
        "road_preferences": {
            "residential": profile.residential_penalty,
            "unclassified": profile.unclassified_penalty,
            "tertiary": profile.tertiary_penalty,
            "link": profile.link_penalty,
        },
        "turn_penalty_s": profile.turn_penalty_s,
        "short_maneuver_penalty_s": profile.short_maneuver_penalty_s,
        "signal_delay_s": profile.signal_delay_s,
        "customs_buffer_mins": profile.customs_buffer_mins,
        "emissions_kg_per_km": profile.emissions_kg_per_km,
        "idle_emissions_kg_per_hour": profile.idle_emissions_kg_per_hour,
        "routing_mode": policy.key,
        "routing_policy": {
            **policy_payload,
            "vehicle_width_m": dimensions[0],
            "vehicle_height_m": dimensions[1],
            "gross_weight_t": dimensions[2],
        },
        "assumptions_source": "CrossFlow planning profile over OSM road classes",
        "legal_restrictions_note": (
            "Enforces explicit edge access, mode, width, height/weight and "
            "surface restrictions where tagged. Untagged clearance remains "
            "a planning fallback, not a guarantee; OSM turn-restriction "
            "relations are not included in the bundled graph."
        ),
    }


def _validated_route_preference(preference: str) -> RoutePreferenceProfile:
    try:
        return ROUTE_PREFERENCES[preference]
    except KeyError as exc:
        allowed = ", ".join(ROUTE_PREFERENCES)
        raise ValueError(
            f"Unknown route preference: {preference}. Expected one of: {allowed}."
        ) from exc


def route_preference(
    preference: str = "BALANCED",
) -> RoutePreferenceProfile:
    return _validated_route_preference(preference)


def route_preference_payload(
    preference: RoutePreferenceProfile,
) -> Dict[str, Any]:
    """Return the auditable route objective published by the API."""
    return {
        "id": preference.key,
        "name": preference.name,
        "description": preference.description,
        "component_weights": {
            "distance_proxy_s": preference.distance_proxy,
            "free_flow_s": preference.free_flow,
            "congestion_delay_s": preference.congestion,
            "weather_delay_s": preference.weather,
            "maneuver_delay_s": preference.maneuver,
            "road_suitability_penalty_s": preference.suitability,
        },
        "objective_cost_unit": "weighted_seconds",
        "eligible_vehicle_types": list(
            preference.eligible_vehicle_types or VEHICLE_PROFILES
        ),
        "road_scope": preference.road_scope,
        "road_scope_note": (
            "Mapped OSM residential and eligible service roads may be selected; "
            "drains and footpaths are excluded. Published access, width, height "
            "and weight tags are enforced, while missing physical tags remain "
            "unknown and use the vehicle policy's conservative fallback."
            if preference.road_scope == "MAPPED_PUBLIC_LOCAL"
            else "Uses the standard bundled public motor-road graph."
        ),
        "distance_proxy_note": (
            "Physical metres normalized to seconds at the vehicle maximum "
            "speed for objective comparison only."
        ),
    }


def _validate_preference_vehicle(
    preference: RoutePreferenceProfile,
    profile: VehicleProfile,
) -> None:
    """Reject a local-lane objective for vehicle classes without clearance."""
    if (
        preference.eligible_vehicle_types
        and profile.key not in preference.eligible_vehicle_types
    ):
        allowed = ", ".join(preference.eligible_vehicle_types)
        raise ValueError(
            f"{preference.name} is available only for {allowed}; "
            f"{profile.name} cannot be routed onto unverified narrow roads."
        )


def _serialized_haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance used while loading, before public helpers exist."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


_LEGACY_ROAD_FIELDS = ("name", "ref", "highway", "junction")


def _optional_osm_number(value: Any, *, quantity: str) -> Optional[float]:
    """Parse a simple OSM numeric tag into metres, tonnes, or unitless score."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0.0 else None

    text = str(value).strip().lower().replace(",", ".")
    if not text or text in {"none", "default", "unsigned", "unknown"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    parsed = float(match.group())
    if not math.isfinite(parsed) or parsed < 0.0:
        return None
    if quantity in {"width", "height"} and any(
        marker in text for marker in (" ft", "feet", "foot", "'")
    ):
        parsed *= 0.3048
    elif quantity == "weight" and "kg" in text:
        parsed /= 1000.0
    elif quantity == "weight" and any(
        marker in text for marker in (" lb", "pound")
    ):
        parsed *= 0.00045359237
    return parsed


def _optional_maxspeed_kph(value: Any) -> Optional[float]:
    """Parse a simple sourced OSM maxspeed without inventing a default.

    Conditional, variable and symbolic values remain ``None`` because this
    request contract has no date/vehicle context with which to interpret them.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().casefold()
    if (
        not text
        or any(token in text for token in (";", "@", "variable", "signals"))
        or text in {"none", "national", "urban", "rural", "walk"}
    ):
        return None
    parsed = _optional_osm_number(value, quantity="speed")
    if parsed is None or parsed <= 0.0:
        return None
    if "mph" in text:
        parsed *= 1.609344
    return parsed if parsed <= 180.0 else None


def _optional_lane_count(value: Any) -> Optional[float]:
    """Return only a finite sourced lane count suitable for capacity audit."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or any(marker in text for marker in (";", "|", "@")):
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0.5 <= parsed <= 20.0:
        return None
    return parsed


def _normalized_vehicle_modes(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set)) else re.split(
        r"[,;|]", str(value),
    )
    return tuple(sorted({
        str(item).strip().upper() for item in values if str(item).strip()
    }))


def _road_edge_from_metadata(
    target: int,
    distance_m: float,
    road_index: int,
    road: Sequence[Any],
    road_fields: Sequence[str],
) -> RoadEdge:
    """Decode both legacy four-field roads and extended v3 metadata."""
    metadata = dict(zip(road_fields, road))
    quality = _optional_osm_number(
        metadata.get("road_quality"), quantity="quality",
    )
    if quality is not None:
        quality = max(0.0, min(1.0, quality))
    return RoadEdge(
        target=target,
        distance_m=distance_m,
        road_index=road_index,
        name=metadata.get("name"),
        ref=metadata.get("ref"),
        highway=metadata.get("highway"),
        junction=metadata.get("junction"),
        access=metadata.get("access"),
        vehicle=metadata.get("vehicle"),
        motor_vehicle=metadata.get("motor_vehicle"),
        motorcar=metadata.get("motorcar"),
        motorcycle=metadata.get("motorcycle"),
        hgv=metadata.get("hgv"),
        ferry=metadata.get("ferry"),
        surface=metadata.get("surface"),
        smoothness=metadata.get("smoothness"),
        width_m=_optional_osm_number(metadata.get("width"), quantity="width"),
        maxweight_t=_optional_osm_number(
            metadata.get("maxweight"), quantity="weight",
        ),
        maxheight_m=_optional_osm_number(
            metadata.get("maxheight"), quantity="height",
        ),
        service=metadata.get("service"),
        maxspeed_kph=_optional_maxspeed_kph(metadata.get("maxspeed")),
        lanes=_optional_lane_count(metadata.get("lanes")),
        road_quality=quality,
        vehicle_modes=_normalized_vehicle_modes(metadata.get("vehicle_modes")),
    )


def _load():
    with open(_GRAPH_PATH, "rb") as fh:
        graph_bytes = fh.read()
    # The observation store is scoped to this exact committed artifact. OSM
    # node IDs can survive topology changes, so schema version or query hash
    # alone is not strong enough to prevent stale edge learning from leaking
    # into a rebuilt graph.
    graph_revision = hashlib.sha256(graph_bytes).hexdigest()
    raw = json.loads(graph_bytes)
    nodes = {int(k): (v[0], v[1]) for k, v in raw["nodes"].items()}
    roads = raw.get("roads", [])
    road_fields = tuple(
        raw.get("meta", {}).get("road_fields") or _LEGACY_ROAD_FIELDS
    )
    road_graph: Dict[int, List[RoadEdge]] = {}
    for source_text, records in raw["adj"].items():
        source = int(source_text)
        edges: List[RoadEdge] = []
        for record in records:
            target = int(record[0])
            # V2 builder ceilings distance computed from these exact serialized
            # coordinates. Clamp legacy v1 weights upward so the haversine A*
            # heuristic remains admissible there too.
            distance = max(
                float(record[1]),
                _serialized_haversine_m(nodes[source], nodes[target]),
            )
            road_index = int(record[2]) if len(record) >= 3 else -1
            road = roads[road_index] if 0 <= road_index < len(roads) else []
            edges.append(_road_edge_from_metadata(
                target, distance, road_index, road, road_fields,
            ))
        road_graph[source] = edges
    adj = {
        source: [(edge.target, edge.distance_m) for edge in edges]
        for source, edges in road_graph.items()
    }
    landmarks = {k: int(v) for k, v in raw["landmarks"].items()}
    # Only the original v1 artifact needs runtime additions. A v2 rebuild owns
    # all landmark snaps and must not be replaced by stale hardcoded node IDs.
    if int(raw.get("meta", {}).get("schema_version", 1)) < 2:
        landmarks.update({
            key: node_id for key, node_id in _LANDMARK_OVERRIDES.items()
            if node_id in nodes
        })
    return (
        nodes, road_graph, adj, landmarks, raw.get("meta", {}),
        {int(k): v for k, v in raw.get("node_meta", {}).items()}, roads,
        graph_revision,
    )


(
    NODES, ROAD_ADJ, ADJ, LANDMARKS, GRAPH_META, NODE_META, ROADS,
    GRAPH_REVISION,
) = _load()


def _road_adjacency_for_snapshot(
    snapshot: Optional[ApprovedGraphOverrideSnapshot],
) -> Mapping[int, Tuple[RoadEdge, ...]]:
    """Return a copy-on-write adjacency with reviewed, revision-bound edges."""
    if snapshot is not None and not isinstance(
        snapshot, ApprovedGraphOverrideSnapshot,
    ):
        raise TypeError(
            "approved_override_snapshot must be an ApprovedGraphOverrideSnapshot."
        )
    if snapshot is None or not snapshot.overrides:
        # The committed adjacency is immutable by application convention.
        # Returning it directly avoids copying ~75k entries per cold process
        # and also keeps isolated graph fixtures truthful in unit tests.
        return ROAD_ADJ
    if snapshot.graph_revision != GRAPH_REVISION:
        raise ValueError("Approved overrides target another graph revision.")
    additions: Dict[int, List[RoadEdge]] = {}
    for index, override in enumerate(snapshot.overrides):
        if override.source_node not in NODES or override.target_node not in NODES:
            raise ValueError("Approved override references an unknown graph node.")
        # Graph-bound activation authenticates durable rows again. Candidate
        # ingestion allows a bounded endpoint snap, so include both connector
        # legs in the routed weight instead of silently teleporting between a
        # graph node and the crowd-sourced polyline. This also preserves the
        # straight-line lower bound required by A* admissibility.
        geometry = tuple(
            (float(point[0]), float(point[1])) for point in override.geometry
        )
        source_coordinate = NODES[override.source_node]
        target_coordinate = NODES[override.target_node]
        forward_connectors = (
            haversine_m(source_coordinate, geometry[0]),
            haversine_m(geometry[-1], target_coordinate),
        )
        reverse_connectors = (
            haversine_m(source_coordinate, geometry[-1]),
            haversine_m(geometry[0], target_coordinate),
        )
        if sum(reverse_connectors) < sum(forward_connectors):
            geometry = tuple(reversed(geometry))
            connectors = reverse_connectors
        else:
            connectors = forward_connectors
        if max(connectors) > 400.0:
            raise ValueError(
                "Approved override geometry endpoints are not bound to its graph nodes."
            )
        geometry_distance_m = sum(
            haversine_m(first, second)
            for first, second in zip(geometry, geometry[1:])
        )
        if not 8.0 <= geometry_distance_m <= 10_000.0:
            raise ValueError("Approved road override geometry length is implausible.")
        declared_ratio = override.distance_m / geometry_distance_m
        if not 0.75 <= declared_ratio <= 4.0:
            raise ValueError(
                "Approved override distance is inconsistent with its geometry."
            )
        connector_distance_m = sum(connectors) + geometry_distance_m
        effective_distance_m = max(
            override.distance_m,
            connector_distance_m,
            haversine_m(source_coordinate, target_coordinate),
        )
        effective_duration_s = None
        if override.duration_s is not None:
            # Preserve the reviewed implied speed while accounting for the
            # graph-to-polyline connector legs introduced at activation.
            effective_duration_s = (
                override.duration_s * effective_distance_m / override.distance_m
            )
        additions.setdefault(override.source_node, []).append(RoadEdge(
            target=override.target_node,
            distance_m=effective_distance_m,
            road_index=-(index + 2),
            name=f"Reviewed shortcut {override.override_id}",
            highway="service",
            access="yes",
            road_quality=override.road_quality,
            vehicle_modes=override.applicable_vehicle_modes,
            approved_override_id=override.override_id,
            approved_duration_s=effective_duration_s,
            approved_geometry=geometry,
            approved_candidate_sha256=override.candidate_sha256,
        ))
    changed = {
        source: tuple(ROAD_ADJ.get(source, ())) + tuple(edges)
        for source, edges in additions.items()
    }
    return ChainMap(changed, ROAD_ADJ)


@lru_cache(maxsize=32)
def _routing_view(
    vehicle_type: str = "COMMUTER",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> RoutingView:
    """Return the mode-specific mutually reachable coordinate-snap core.

    The committed graph contains a handful of one-way service-lane tips.  They
    belong to the same geographic road component, but a car snapped to one can
    be unable to leave it (or unable to reach it from the rest of Batam).  The
    intersection of nodes reachable from Batam Centre and nodes that can reach
    Batam Centre is one strongly connected component, so any two snapped free-
    route coordinates are mutually routable.

    Named landmarks retain their exact configured node IDs; this set only
    governs free-coordinate and geocoder snapping.
    """
    profile = vehicle_profile(vehicle_type)
    road_adjacency = _road_adjacency_for_snapshot(approved_override_snapshot)
    eligible_nodes = frozenset(
        node_id for node_id in road_adjacency
        if node_traversal_decision(node_id, profile).allowed
    )
    filtered_edges: Dict[int, Tuple[RoadEdge, ...]] = {
        source: tuple(
            edge for edge in road_adjacency.get(source, ())
            if edge.target in eligible_nodes
            and edge_traversal_decision(edge, profile).allowed
        )
        for source in eligible_nodes
    }
    # MappingProxyType prevents a caller from mutating the cached view.  Tuples
    # above similarly make each outgoing edge list safe to share between A*
    # requests.
    filtered_adjacency: Mapping[int, Tuple[RoadEdge, ...]] = MappingProxyType(
        filtered_edges
    )
    filtered = {
        source: tuple(edge.target for edge in edges)
        for source, edges in filtered_adjacency.items()
    }
    anchor = LANDMARKS.get("batam_centre")
    if anchor is None or anchor not in filtered:
        return RoutingView(filtered_adjacency, eligible_nodes)

    forward = {anchor}
    stack = [anchor]
    while stack:
        node = stack.pop()
        for nxt in filtered.get(node, ()):
            if nxt not in forward:
                forward.add(nxt)
                stack.append(nxt)

    reverse: Dict[int, List[int]] = {node_id: [] for node_id in filtered}
    for node_id, edges in filtered.items():
        for nxt in edges:
            reverse.setdefault(nxt, []).append(node_id)

    backward = {anchor}
    stack = [anchor]
    while stack:
        node = stack.pop()
        for previous in reverse.get(node, ()):
            if previous not in backward:
                backward.add(previous)
                stack.append(previous)

    core = forward & backward
    return RoutingView(filtered_adjacency, frozenset(core or eligible_nodes))


def _main_routing_core(
    vehicle_type: str = "COMMUTER",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> frozenset[int]:
    """Compatibility facade returning the mode-specific routing core.

    Keep the historical function and its cache controls public while routing
    callers share the richer immutable :class:`RoutingView`.  Normalizing the
    optional snapshot here means omitted and explicit ``None`` calls use the
    same cached key in ``_routing_view``.
    """
    return _routing_view(
        vehicle_profile(vehicle_type).key,
        approved_override_snapshot,
    ).core


# Existing diagnostics and tests call ``_main_routing_core.cache_clear()``.
# Delegate the cache API to the canonical view cache so the compatibility
# facade cannot accidentally reintroduce a second cache identity. Clear the
# dependent feature/index caches too; graph-fixture tests can replace the
# module graph without retaining features from the committed graph.
def _clear_routing_view_cache() -> None:
    _routing_view.cache_clear()
    static_cache = globals().get("_static_edge_features")
    if static_cache is not None:
        static_cache.cache_clear()
    alt_cache = globals().get("_alt_index_for_profile")
    if alt_cache is not None:
        alt_cache.cache_clear()


_main_routing_core.cache_clear = _clear_routing_view_cache  # type: ignore[attr-defined]
_main_routing_core.cache_info = _routing_view.cache_info  # type: ignore[attr-defined]
_main_routing_core.cache_parameters = _routing_view.cache_parameters  # type: ignore[attr-defined]


@lru_cache(maxsize=16)
def _alt_index_for_profile(profile_key: str) -> Optional[AltIndex]:
    """Load a revision-bound ALT index, falling back safely when unavailable."""
    manifest_path = os.path.join(_ALT_INDEX_DIR, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
        if manifest.get("graph_revision") != GRAPH_REVISION:
            return None
        entry = next(
            (
                item for item in manifest.get("indexes", ())
                if profile_key in item.get("profiles", ())
            ),
            None,
        )
        if not entry:
            return None
        return AltIndex.load(
            os.path.join(_ALT_INDEX_DIR, str(entry["path"])),
            expected_graph_revision=GRAPH_REVISION,
            expected_topology_id=str(entry["topology_id"]),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


@lru_cache(maxsize=64)
def _radial_node_influence(
    lat: float, lng: float, radius_m: float,
) -> Dict[int, float]:
    """Score-independent 0..1 taper, cached for stable zone geometry."""
    centre = (lat, lng)
    influence: Dict[int, float] = {}
    lat_delta = radius_m / 111_195.0
    lon_delta = radius_m / (
        111_195.0 * max(0.1, math.cos(math.radians(lat)))
    )
    for node, point in NODES.items():
        if (
            abs(point[0] - lat) > lat_delta
            or abs(point[1] - lng) > lon_delta
        ):
            continue
        distance = haversine_m(point, centre)
        if distance < radius_m:
            normalized = distance / radius_m
            influence[node] = (1.0 - normalized * normalized) ** 2
    return influence


def _all_zone_node_sets(
    zones: List[Dict[str, Any]],
) -> List[Tuple[str, Dict[int, float], float]]:
    """Return node-level spatial scores with a smooth radial decay.

    A flat score at every point inside a circle creates an artificial cost
    cliff. The squared taper is full strength at the centre and reaches zero
    continuously at the configured radius.
    """
    zone_node_sets: List[Tuple[str, Dict[int, float], float]] = []
    for z in zones:
        z_pt = (z["lat"], z["lng"])
        radius = float(z.get("radius_m", 800))
        score = max(0.0, min(100.0, float(z.get("congestion_index", 0.0))))
        if score <= 0.0:
            continue
        radial = _radial_node_influence(
            round(float(z_pt[0]), 6), round(float(z_pt[1]), 6), round(radius, 1),
        )
        nodes = {node: score * influence for node, influence in radial.items()}
        display_name = f"{z['name']} (Score: {score:.0f})"
        zone_node_sets.append((display_name, nodes, score))
    return zone_node_sets


def _red_zone_node_sets(zones: List[Dict[str, Any]]) -> List[Tuple[str, set[int]]]:
    """Return display names and graph-node sets for active red zones."""
    return [
        (name, set(nodes)) for name, nodes, score in _all_zone_node_sets(zones)
        if score >= 70.0
    ]


def _zone_congestion_scores(
    zone_node_sets: Sequence[Tuple[str, Dict[int, float], float]],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for _, nodes, _ in zone_node_sets:
        for node, local_score in nodes.items():
            scores[node] = max(local_score, scores.get(node, 0.0))
    return scores


def find_nodes_in_zones(zones: List[Dict[str, Any]]) -> Tuple[set[int], List[str]]:
    """Find graph node IDs that fall inside active red super-congested zones."""
    zone_node_sets = _red_zone_node_sets(zones)
    blocked_nodes: set[int] = set()
    for _, nodes in zone_node_sets:
        blocked_nodes.update(nodes)
    return blocked_nodes, [name for name, _ in zone_node_sets]


def _path_distance_m(path: List[int]) -> float:
    """Sum physical road-edge lengths for a reconstructed graph path."""
    metres = 0.0
    for node, nxt in zip(path, path[1:]):
        for candidate, weight in ADJ.get(node, ()):
            if candidate == nxt:
                metres += weight
                break
        else:  # pragma: no cover - an A* predecessor must always be an edge
            raise ValueError(f"Path contains a non-edge: {node}->{nxt}")
    return metres


def _edge_key(source: int, edge: RoadEdge) -> Tuple[int, int, int]:
    return source, edge.target, edge.road_index


def road_edge_by_key(
    source: int, target: int, road_index: int,
) -> Optional[RoadEdge]:
    """Resolve the exact directed edge identity used by routing and learning."""
    return next(
        (
            edge for edge in ROAD_ADJ.get(source, ())
            if _edge_key(source, edge) == (source, target, road_index)
        ),
        None,
    )


def _undirected_edge_key(source: int, target: int) -> Tuple[int, int]:
    return (source, target) if source < target else (target, source)


def _highway_class(edge: RoadEdge) -> Tuple[str, bool]:
    highway = edge.highway or "road"
    is_link = highway.endswith("_link")
    return (highway[:-5] if is_link else highway), is_link


_ACCESS_ALLOWED = {"yes", "designated", "permissive", "official"}
_ACCESS_DENIED = {"no", "private"}


def _normalized_tag(value: Optional[str]) -> Optional[str]:
    normalized = str(value).strip().lower() if value is not None else ""
    return normalized or None


def _mode_access_tags(
    edge: RoadEdge, mode: str,
) -> Tuple[Tuple[str, Optional[str]], ...]:
    if mode == "MOTORCYCLE":
        return (
            ("motorcycle", edge.motorcycle),
            ("motor_vehicle", edge.motor_vehicle),
            ("vehicle", edge.vehicle),
            ("access", edge.access),
        )
    if mode == "PASSENGER_CAR":
        return (
            ("motorcar", edge.motorcar),
            ("motor_vehicle", edge.motor_vehicle),
            ("vehicle", edge.vehicle),
            ("access", edge.access),
        )
    if mode == "FREIGHT_TRUCK":
        return (
            ("hgv", edge.hgv),
            ("motor_vehicle", edge.motor_vehicle),
            ("vehicle", edge.vehicle),
            ("access", edge.access),
        )
    return (
        ("ferry", edge.ferry), ("vehicle", edge.vehicle),
        ("access", edge.access),
    )


def _mode_node_access_tags(
    metadata: Mapping[str, Any], mode: str,
) -> Tuple[Tuple[str, Optional[str]], ...]:
    specific = {
        "MOTORCYCLE": "motorcycle",
        "PASSENGER_CAR": "motorcar",
        "FREIGHT_TRUCK": "hgv",
        "FERRY_MARITIME": "ferry",
    }[mode]
    return (
        (specific, metadata.get(specific)),
        ("motor_vehicle", metadata.get("motor_vehicle")),
        ("vehicle", metadata.get("vehicle")),
        ("access", metadata.get("access")),
    )


def node_traversal_decision(
    node_id: int,
    vehicle: VehicleProfile | str = "COMMUTER",
    *,
    allow_destination_access: bool = False,
) -> EdgeTraversalDecision:
    """Apply OSM node barriers/access using the selected vehicle hierarchy."""
    policy = vehicle_routing_policy(vehicle)
    metadata = NODE_META.get(node_id, {})
    checked: List[str] = []
    explicit_allow = False
    for tag_name, raw_value in _mode_node_access_tags(metadata, policy.key):
        value = _normalized_tag(raw_value)
        if value is None:
            continue
        checked.append(tag_name)
        if value in _ACCESS_ALLOWED:
            explicit_allow = True
            break
        if value == "destination" and allow_destination_access:
            explicit_allow = True
            break
        if value in _ACCESS_DENIED:
            return EdgeTraversalDecision(
                False, f"explicit node {tag_name}={value} restriction",
                tuple(checked),
            )
        return EdgeTraversalDecision(
            False,
            f"node {tag_name}={value} requires trip-specific authorization",
            tuple(checked),
        )
    barrier = _normalized_tag(metadata.get("barrier"))
    if barrier is not None:
        checked.append("barrier")
    if barrier in {
        "block", "bollard", "bus_trap", "cycle_barrier", "jersey_barrier",
        "sump_buster",
    } and not explicit_allow:
        return EdgeTraversalDecision(
            False, f"physical node barrier {barrier}", tuple(checked),
        )
    return EdgeTraversalDecision(True, "eligible", tuple(checked))


def edge_traversal_decision(
    edge: RoadEdge,
    vehicle: VehicleProfile | str = "COMMUTER",
    *,
    allow_destination_access: bool = False,
) -> EdgeTraversalDecision:
    """Enforce only sourced constraints, with safe defaults for missing tags.

    An explicit mode-specific allow overrides a broader access restriction, as
    required by OSM's access hierarchy. Untagged width, surface and clearance
    never become fabricated hard restrictions.
    """
    if isinstance(vehicle, VehicleProfile):
        profile = vehicle
        policy = vehicle_routing_policy(profile)
        dimensions = _PROFILE_DIMENSIONS[profile.key]
    elif vehicle in VEHICLE_ROUTING_POLICIES:
        profile = None
        policy = vehicle_routing_policy(vehicle)
        dimensions = (
            policy.min_width_m, policy.vehicle_height_m, policy.gross_weight_t,
        )
    else:
        profile = vehicle_profile(vehicle)
        policy = vehicle_routing_policy(profile)
        dimensions = _PROFILE_DIMENSIONS[profile.key]

    checked: List[str] = []
    if edge.vehicle_modes:
        checked.append("vehicle_modes")
        if policy.key not in edge.vehicle_modes:
            return EdgeTraversalDecision(
                False, f"vehicle mode {policy.key} is not permitted",
                tuple(checked),
            )

    access_tags = _mode_access_tags(edge, policy.key)
    for tag_name, raw_value in access_tags:
        value = _normalized_tag(raw_value)
        if value is None:
            continue
        checked.append(tag_name)
        if value in _ACCESS_ALLOWED:
            break
        if value == "destination" and allow_destination_access:
            break
        if value in _ACCESS_DENIED:
            return EdgeTraversalDecision(
                False, f"explicit {tag_name}={value} restriction",
                tuple(checked),
            )
        # Conditional/destination/delivery/customer access needs trip context
        # this API does not carry. Fail closed instead of fabricating authority.
        return EdgeTraversalDecision(
            False,
            f"{tag_name}={value} requires trip-specific authorization",
            tuple(checked),
        )

    highway, _ = _highway_class(edge)
    if edge.highway is not None:
        checked.append("highway")
        if highway not in policy.allowed_highways:
            return EdgeTraversalDecision(
                False, f"road class {highway} is incompatible with {policy.key}",
                tuple(checked),
            )

    width_m, height_m, gross_weight_t = dimensions
    if edge.width_m is not None and width_m is not None:
        checked.append("width")
        if edge.width_m + 1e-9 < width_m:
            return EdgeTraversalDecision(
                False,
                f"edge width {edge.width_m:g}m is below vehicle width {width_m:g}m",
                tuple(checked),
            )
    if edge.maxheight_m is not None and height_m is not None:
        checked.append("maxheight")
        if edge.maxheight_m + 1e-9 < height_m:
            return EdgeTraversalDecision(
                False,
                f"maxheight {edge.maxheight_m:g}m is below vehicle height {height_m:g}m",
                tuple(checked),
            )
    if edge.maxweight_t is not None and gross_weight_t is not None:
        checked.append("maxweight")
        if edge.maxweight_t + 1e-9 < gross_weight_t:
            return EdgeTraversalDecision(
                False,
                f"maxweight {edge.maxweight_t:g}t is below vehicle weight {gross_weight_t:g}t",
                tuple(checked),
            )

    surface = _normalized_tag(edge.surface)
    smoothness = _normalized_tag(edge.smoothness)
    if surface is not None:
        checked.append("surface")
    if smoothness is not None:
        checked.append("smoothness")
    if surface in {"impassable", "unusable"} or smoothness == "impassable":
        return EdgeTraversalDecision(
            False, "surface is explicitly impassable", tuple(checked),
        )
    return EdgeTraversalDecision(True, "eligible", tuple(checked))


# Compatibility snapshot for diagnostics that historically imported this
# constant. New snapping always resolves the selected mode dynamically.
SNAP_NODE_IDS = _main_routing_core("COMMUTER")


def _edge_road_quality_score(
    edge: RoadEdge, profile: VehicleProfile,
) -> Tuple[float, str]:
    """Return 0..1 quality and provenance, never an unsupported zero default."""
    if edge.road_quality is not None:
        return max(0.0, min(1.0, edge.road_quality)), "edge_override"

    candidates: List[Tuple[float, str]] = []
    surface = _normalized_tag(edge.surface)
    if surface in _SURFACE_QUALITY:
        candidates.append((_SURFACE_QUALITY[surface], "surface"))
    smoothness = _normalized_tag(edge.smoothness)
    if smoothness in _SMOOTHNESS_QUALITY:
        candidates.append((_SMOOTHNESS_QUALITY[smoothness], "smoothness"))
    if candidates:
        score, source = min(candidates, key=lambda item: item[0])
        return score, source

    highway, _ = _highway_class(edge)
    policy = vehicle_routing_policy(profile)
    return (
        _HIGHWAY_QUALITY_DEFAULTS.get(
            highway, policy.unrated_road_quality,
        ),
        "road_class_fallback" if highway in _HIGHWAY_QUALITY_DEFAULTS
        else "vehicle_profile_fallback",
    )


def _edge_speed_kph(edge: RoadEdge, profile: VehicleProfile) -> float:
    highway, is_link = _highway_class(edge)
    class_speed = HIGHWAY_SPEED_KPH.get(highway, HIGHWAY_SPEED_KPH["road"])
    if is_link:
        class_speed *= 0.72
    planned_speed = class_speed * profile.speed_factor
    # A sourced OSM maxspeed is a ceiling, never a promise that traffic or road
    # quality permits that speed. Vehicle capability remains the other ceiling.
    if edge.maxspeed_kph is not None:
        planned_speed = min(planned_speed, edge.maxspeed_kph)
    return max(3.0, min(profile.max_speed_kph, planned_speed))


def _road_suitability_multiplier(
    edge: RoadEdge, profile: VehicleProfile, prefer_through_roads: bool = False,
) -> float:
    """Generalized operational preference; never reported as physical time."""
    highway, is_link = _highway_class(edge)
    multiplier = {
        "residential": profile.residential_penalty,
        "unclassified": profile.unclassified_penalty,
        "tertiary": profile.tertiary_penalty,
    }.get(highway, 1.0)
    if is_link:
        multiplier *= profile.link_penalty
    if prefer_through_roads:
        multiplier *= {
            "secondary": 1.03,
            "tertiary": 1.08,
            "unclassified": 1.12,
            "residential": 1.22,
        }.get(highway, 1.0)
    return max(1.0, multiplier)


def _congestion_class_exposure(edge: RoadEdge) -> float:
    """Relative exposure of each road class to the simulated traffic score."""
    highway, _ = _highway_class(edge)
    return {
        "motorway": 0.78,
        "trunk": 0.85,
        "primary": 0.95,
        "secondary": 1.00,
        "tertiary": 0.90,
        "unclassified": 0.72,
        "residential": 0.58,
        "living_street": 0.48,
    }.get(highway, 0.80)


def _weather_class_exposure(edge: RoadEdge) -> float:
    highway, _ = _highway_class(edge)
    return {
        "motorway": 0.90,
        "trunk": 0.92,
        "primary": 0.95,
        "secondary": 1.00,
        "tertiary": 1.07,
        "unclassified": 1.15,
        "residential": 1.12,
        "living_street": 1.20,
    }.get(highway, 1.10)


@lru_cache(maxsize=32)
def _static_edge_features(
    vehicle_type: str = "COMMUTER",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Mapping[Tuple[int, int, int], StaticEdgeFeatures]:
    """Build immutable edge features once per profile and graph snapshot."""
    profile = vehicle_profile(vehicle_type)
    adjacency = _road_adjacency_for_snapshot(approved_override_snapshot)
    features: Dict[Tuple[int, int, int], StaticEdgeFeatures] = {}
    for source, edges in adjacency.items():
        for edge in edges:
            physical_floor_s = edge.distance_m / profile.max_speed_kph * 3.6
            baseline_free_flow_s = (
                max(physical_floor_s, edge.approved_duration_s)
                if edge.approved_duration_s is not None
                else edge.distance_m / _edge_speed_kph(edge, profile) * 3.6
            )
            road_quality, _ = _edge_road_quality_score(edge, profile)
            features[_edge_key(source, edge)] = StaticEdgeFeatures(
                physical_floor_s=physical_floor_s,
                baseline_free_flow_s=baseline_free_flow_s,
                distance_proxy_s=physical_floor_s,
                road_quality=road_quality,
                congestion_exposure=_congestion_class_exposure(edge),
                weather_exposure=_weather_class_exposure(edge),
                suitability_multiplier=_road_suitability_multiplier(
                    edge, profile, False,
                ),
                through_suitability_multiplier=_road_suitability_multiplier(
                    edge, profile, True,
                ),
            )
    return MappingProxyType(features)


def _turn_cost_s(
    previous_source: Optional[int],
    node: int,
    previous_edge: Optional[RoadEdge],
    edge: RoadEdge,
    profile: VehicleProfile,
    eligible_adjacency: Optional[Mapping[int, Tuple[RoadEdge, ...]]] = None,
) -> Tuple[float, float, float]:
    """Return maneuver, short-maneuver and signal seconds at one junction."""
    signal = (
        profile.signal_delay_s
        if NODE_META.get(node, {}).get("highway") == "traffic_signals"
        else 0.0
    )
    if previous_source is None or previous_edge is None:
        return 0.0, 0.0, signal

    incoming = _bearing(NODES[previous_source], NODES[node])
    outgoing = _bearing(NODES[node], NODES[edge.target])
    magnitude = abs(_signed_turn(incoming, outgoing))
    road_changed = not _same_road(previous_edge, edge)
    candidates = (eligible_adjacency or ROAD_ADJ).get(node, ())
    legal_choices = {
        candidate.target for candidate in candidates
        if candidate.target != previous_source
        and (
            eligible_adjacency is not None
            or edge_traversal_decision(candidate, profile).allowed
        )
    }
    is_decision = len(legal_choices) > 1
    if not road_changed and not (is_decision and magnitude >= 35.0):
        return 0.0, 0.0, signal

    if magnitude >= 170:
        turn_factor = 3.0
    elif magnitude >= 135:
        turn_factor = 1.6
    elif magnitude >= 40:
        turn_factor = 1.0
    elif magnitude >= 15:
        turn_factor = 0.45
    else:
        turn_factor = 0.20 if road_changed else 0.0
    if previous_edge.junction in ("roundabout", "circular"):
        turn_factor = max(turn_factor, 0.65)
    maneuver = profile.turn_penalty_s * turn_factor
    short = (
        profile.short_maneuver_penalty_s
        if road_changed and edge.distance_m < 70.0
        else 0.0
    )
    return maneuver, short, signal


def _current_learning_snapshot() -> LearningSnapshot:
    """Capture the one immutable learning model used by a route calculation."""
    return route_learning_store.snapshot(GRAPH_REVISION)


def _edge_local_congestion_score(
    source: int,
    edge: RoadEdge,
    congestion_scores: Optional[Dict[int, float]],
) -> float:
    return max(
        float((congestion_scores or {}).get(source, 0.0)),
        float((congestion_scores or {}).get(edge.target, 0.0)),
    )


def _edge_learning_adjustment(
    source: int,
    edge: RoadEdge,
    profile: VehicleProfile,
    learning_snapshot: LearningSnapshot,
    *,
    local_congestion_score: float,
    network_congestion_score: float,
    weather: int,
    static: Optional[StaticEdgeFeatures] = None,
) -> Tuple[float, float, Optional[LearnedEdge]]:
    """Return baseline/effective free flow and the applied verified aggregate."""
    physical_floor_s = (
        static.physical_floor_s
        if static is not None
        else edge.distance_m / profile.max_speed_kph * 3.6
    )
    baseline_s = static.baseline_free_flow_s if static is not None else (
        max(physical_floor_s, edge.approved_duration_s)
        if edge.approved_duration_s is not None
        else edge.distance_m / _edge_speed_kph(edge, profile) * 3.6
    )
    gate_open = (
        weather == 0
        and network_congestion_score <= MAX_CLEAR_CONGESTION_SCORE
        and local_congestion_score <= MAX_CLEAR_CONGESTION_SCORE
        and learning_snapshot.graph_revision == GRAPH_REVISION
    )
    if not gate_open:
        return baseline_s, baseline_s, None
    learned = learning_snapshot.entries.get((
        source,
        edge.target,
        edge.road_index,
        profile.key,
    ))
    if learned is None:
        return baseline_s, baseline_s, None
    effective_s = max(physical_floor_s, learned.median_moving_duration_s)
    return baseline_s, effective_s, learned


def _edge_cost_components(
    source: int,
    edge: RoadEdge,
    profile: VehicleProfile,
    *,
    previous_source: Optional[int] = None,
    previous_edge: Optional[RoadEdge] = None,
    congestion_scores: Optional[Dict[int, float]] = None,
    speed_ratios: Optional[Dict[Tuple[int, int, int], float]] = None,
    congestion_estimates: Optional[
        Mapping[Tuple[int, int, int], EdgeCongestionEstimate]
    ] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    prefer_through_roads: bool = False,
    learning_snapshot: Optional[LearningSnapshot] = None,
    static_features: Optional[Mapping[Tuple[int, int, int], StaticEdgeFeatures]] = None,
    eligible_adjacency: Optional[Mapping[int, Tuple[RoadEdge, ...]]] = None,
) -> Dict[str, float]:
    """Return additive seconds used by routing and ETA calculation.

    Requested-hour network congestion supplies a background score. Inside a
    forecast spatial zone, its local score receives the larger share. This is
    one blended exposure term, so the solver must not add the forecaster's
    corridor delay again afterward.
    """
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    static = (static_features or {}).get(_edge_key(source, edge))
    local_score = _edge_local_congestion_score(source, edge, congestion_scores)
    global_score = max(0.0, min(100.0, float(network_congestion_score)))
    baseline_free_flow_s, free_flow_s, _ = _edge_learning_adjustment(
        source,
        edge,
        profile,
        learning_snapshot,
        local_congestion_score=local_score,
        network_congestion_score=global_score,
        weather=weather,
        static=static,
    )
    effective_score = (
        0.35 * global_score + 0.65 * local_score
        if local_score > 0.0 else 0.35 * global_score
    )
    congestion_delay_s = (
        free_flow_s
        * profile.congestion_sensitivity
        * (
            static.congestion_exposure
            if static is not None else _congestion_class_exposure(edge)
        )
        * 1.8
        * (effective_score / 100.0) ** 2
    )
    estimate = (congestion_estimates or {}).get(_edge_key(source, edge))
    if estimate is None:
        estimate = default_congestion_estimate
    if estimate is None:
        legacy_ratio = (speed_ratios or {}).get(_edge_key(source, edge))
        estimate = EdgeCongestionEstimate(
            expected_speed_ratio=max(0.05, min(1.0, float(legacy_ratio)))
            if legacy_ratio is not None else 1.0,
            source="legacy_speed_ratio" if legacy_ratio is not None else "clear_default",
        )
    observed_ratio = estimate.expected_speed_ratio
    speed_ratio_delay_s = free_flow_s * (1.0 / observed_ratio - 1.0)
    congestion_delay_s = max(congestion_delay_s, speed_ratio_delay_s)
    # Prefer a source-provided empirical P90 downside ratio. Standard
    # deviation remains a backward-compatible approximation for providers that
    # cannot publish a calibrated quantile yet.
    downside_ratio = (
        estimate.p90_speed_ratio
        if estimate.p90_speed_ratio is not None
        else max(0.05, observed_ratio - estimate.speed_ratio_std)
    )
    downside_delay_s = free_flow_s * (1.0 / downside_ratio - 1.0)
    uncertainty_spread_s = max(
        0.0,
        downside_delay_s - speed_ratio_delay_s,
        estimate.p90_delay_s - congestion_delay_s,
    )
    # The forecast score above represents network demand/queue conditions.
    # This separately published term represents safe-speed loss on wet roads,
    # so the two effects remain auditable instead of being one opaque factor.
    weather_base = (0.0, 0.12, 0.30)[weather]
    weather_delay_s = (
        free_flow_s * weather_base * profile.weather_sensitivity
        * (
            static.weather_exposure
            if static is not None else _weather_class_exposure(edge)
        )
    )
    maneuver_s, short_s, signal_s = _turn_cost_s(
        previous_source, source, previous_edge, edge, profile,
        eligible_adjacency,
    )
    modeled_s = (
        free_flow_s + congestion_delay_s + weather_delay_s
        + maneuver_s + short_s + signal_s
    )
    suitability_multiplier = (
        static.through_suitability_multiplier if prefer_through_roads
        else static.suitability_multiplier
    ) if static is not None else _road_suitability_multiplier(
        edge, profile, prefer_through_roads,
    )
    suitability_s = free_flow_s * (suitability_multiplier - 1.0)
    road_quality = (
        static.road_quality if static is not None
        else _edge_road_quality_score(edge, profile)[0]
    )
    road_quality_penalty_s = free_flow_s * max(0.0, 1.0 - road_quality)
    policy = vehicle_routing_policy(profile)
    weights = policy.cost_weights
    distance_proxy_s = (
        static.distance_proxy_s if static is not None
        else edge.distance_m / profile.max_speed_kph * 3.6
    )
    objective_time_s = free_flow_s + weather_delay_s + maneuver_s + short_s + signal_s
    expected_objective_s = (
        weights.time * objective_time_s
        + weights.distance * distance_proxy_s
        + weights.congestion * congestion_delay_s
        + weights.road_quality * road_quality_penalty_s
    )
    uncertainty_penalty_s = (
        weights.congestion * policy.risk_aversion * uncertainty_spread_s
    )
    return {
        "baseline_free_flow_s": baseline_free_flow_s,
        "free_flow_s": free_flow_s,
        "learned_free_flow_adjustment_s": free_flow_s - baseline_free_flow_s,
        "congestion_delay_s": congestion_delay_s,
        "weather_delay_s": weather_delay_s,
        "maneuver_delay_s": maneuver_s + short_s + signal_s,
        "suitability_penalty_s": suitability_s,
        "road_quality_penalty_s": road_quality_penalty_s,
        "congestion_uncertainty_s": uncertainty_spread_s,
        "uncertainty_penalty_s": uncertainty_penalty_s,
        "distance_proxy_s": distance_proxy_s,
        "objective_time_s": objective_time_s,
        "expected_objective_cost_s": expected_objective_s,
        "modeled_travel_s": modeled_s,
        "generalized_cost_s": (
            expected_objective_s + uncertainty_penalty_s + suitability_s
        ),
    }


def _edge_objective_cost_s(
    edge: RoadEdge,
    components: Dict[str, float],
    profile: VehicleProfile,
    preference: RoutePreferenceProfile,
) -> float:
    """Return C(e) in normalized seconds without changing physical metrics.

    ``distance`` is converted to a lower-bound seconds proxy at the vehicle's
    maximum speed. The time component excludes congestion so ``w_t`` and
    ``w_c`` remain independent rather than double-counting delay.
    """
    policy_weights = vehicle_routing_policy(profile).cost_weights
    distance_proxy_s = components["distance_proxy_s"]
    time_s = components["objective_time_s"]
    vehicle_objective_s = components["expected_objective_cost_s"]
    uncertainty_penalty_s = components["uncertainty_penalty_s"]
    # Preserve the route-preference contract as a second, nonnegative layer.
    # BALANCED now means the profile-calibrated four-term objective; FASTEST
    # ignores quality/distance while still modeling congestion and weather.
    if preference.key == "BALANCED":
        return (
            vehicle_objective_s + uncertainty_penalty_s
            + components["suitability_penalty_s"]
        )
    if preference.key == "FASTEST":
        return components["modeled_travel_s"] + uncertainty_penalty_s
    return (
        preference.distance_proxy * distance_proxy_s * policy_weights.distance
        + preference.free_flow * time_s * policy_weights.time
        + preference.congestion * components["congestion_delay_s"]
        * policy_weights.congestion
        + preference.weather * components["weather_delay_s"]
        + preference.maneuver * components["maneuver_delay_s"]
        + preference.suitability * components["suitability_penalty_s"]
        + preference.suitability * components["road_quality_penalty_s"]
        * policy_weights.road_quality
        + preference.congestion * uncertainty_penalty_s
    )


def path_cost_breakdown(
    path: PathResult,
    profile: VehicleProfile,
    *,
    congestion_scores: Optional[Dict[int, float]] = None,
    speed_ratios: Optional[Dict[Tuple[int, int, int], float]] = None,
    congestion_estimates: Optional[
        Mapping[Tuple[int, int, int], EdgeCongestionEstimate]
    ] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    learning_snapshot: Optional[LearningSnapshot] = None,
) -> Dict[str, float]:
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    estimate_snapshot = dict(congestion_estimates or {})
    speed_ratio_snapshot = dict(speed_ratios or {})
    if default_congestion_estimate is not None and not isinstance(
        default_congestion_estimate, EdgeCongestionEstimate,
    ):
        raise TypeError(
            "default_congestion_estimate must be an EdgeCongestionEstimate."
        )
    totals = {
        "baseline_free_flow_s": 0.0,
        "free_flow_s": 0.0,
        "learned_free_flow_adjustment_s": 0.0,
        "congestion_delay_s": 0.0,
        "weather_delay_s": 0.0,
        "maneuver_delay_s": 0.0,
        "suitability_penalty_s": 0.0,
        "road_quality_penalty_s": 0.0,
        "congestion_uncertainty_s": 0.0,
        "uncertainty_penalty_s": 0.0,
        "distance_proxy_s": 0.0,
        "objective_time_s": 0.0,
        "expected_objective_cost_s": 0.0,
        "modeled_travel_s": 0.0,
        "generalized_cost_s": 0.0,
    }
    previous_source: Optional[int] = None
    previous_edge: Optional[RoadEdge] = None
    for source, edge in zip(path.nodes, path.edges):
        components = _edge_cost_components(
            source, edge, profile, previous_source=previous_source,
            previous_edge=previous_edge, congestion_scores=congestion_scores,
            speed_ratios=speed_ratio_snapshot,
            congestion_estimates=estimate_snapshot,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            learning_snapshot=learning_snapshot,
        )
        for key in totals:
            totals[key] += components[key]
        previous_source, previous_edge = source, edge
    return {key: round(value, 3) for key, value in totals.items()}


def path_objective_cost_s(
    path: PathResult,
    profile: VehicleProfile,
    preference: RoutePreferenceProfile,
    *,
    congestion_scores: Optional[Dict[int, float]] = None,
    speed_ratios: Optional[Dict[Tuple[int, int, int], float]] = None,
    congestion_estimates: Optional[
        Mapping[Tuple[int, int, int], EdgeCongestionEstimate]
    ] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    learning_snapshot: Optional[LearningSnapshot] = None,
) -> float:
    """Recompute an unpenalized path objective for publication.

    Alternative generation temporarily penalizes shared edges. Recomputing
    here prevents those diversity-only penalties from leaking into the route's
    published objective cost.
    """
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    estimate_snapshot = dict(congestion_estimates or {})
    speed_ratio_snapshot = dict(speed_ratios or {})
    if default_congestion_estimate is not None and not isinstance(
        default_congestion_estimate, EdgeCongestionEstimate,
    ):
        raise TypeError(
            "default_congestion_estimate must be an EdgeCongestionEstimate."
        )
    total = 0.0
    previous_source: Optional[int] = None
    previous_edge: Optional[RoadEdge] = None
    for source, edge in zip(path.nodes, path.edges):
        components = _edge_cost_components(
            source, edge, profile, previous_source=previous_source,
            previous_edge=previous_edge, congestion_scores=congestion_scores,
            speed_ratios=speed_ratio_snapshot,
            congestion_estimates=estimate_snapshot,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            learning_snapshot=learning_snapshot,
        )
        total += _edge_objective_cost_s(edge, components, profile, preference)
        previous_source, previous_edge = source, edge
    return round(total, 3)


def astar_detailed(
    src: int,
    dst: int,
    avoid_nodes: Optional[set[int]] = None,
    banned_edges: Optional[set[Tuple[int, int, int]]] = None,
    edge_penalties: Optional[Dict[Tuple[int, int], float]] = None,
    prefer_through_roads: bool = False,
    vehicle_type: str = "COMMUTER",
    congestion_scores: Optional[Dict[int, float]] = None,
    speed_ratios: Optional[Dict[Tuple[int, int, int], float]] = None,
    congestion_estimates: Optional[
        Mapping[Tuple[int, int, int], EdgeCongestionEstimate]
    ] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    route_preference: str = "BALANCED",
    learning_snapshot: Optional[LearningSnapshot] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    deadline_monotonic: Optional[float] = None,
    max_settled_states: Optional[int] = None,
) -> Optional[PathResult]:
    """Lowest preference-weighted path with exact edges and raw road distance.

    The heuristic includes only distance-proxy and free-flow lower bounds.
    Each road edge is at least its straight-line distance, free-flow speed is
    capped by the profile maximum, and all remaining published weights and
    components are nonnegative. It therefore remains admissible for every
    preference, including with turn-dependent state.
    """
    profile = vehicle_profile(vehicle_type)
    policy = vehicle_routing_policy(profile)
    preference = _validated_route_preference(route_preference)
    _validate_preference_vehicle(preference, profile)
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    road_adjacency = _road_adjacency_for_snapshot(approved_override_snapshot)
    if weather not in (0, 1, 2):
        raise ValueError("Weather must be 0 (clear), 1 (rain), or 2 (storm).")
    if default_congestion_estimate is not None and not isinstance(
        default_congestion_estimate, EdgeCongestionEstimate,
    ):
        raise TypeError(
            "default_congestion_estimate must be an EdgeCongestionEstimate."
        )
    for edge_key, multiplier in (edge_penalties or {}).items():
        if (
            not isinstance(edge_key, tuple)
            or len(edge_key) != 2
            or any(not isinstance(node, int) for node in edge_key)
            or isinstance(multiplier, bool)
            or not isinstance(multiplier, (int, float))
            or not math.isfinite(float(multiplier))
            or float(multiplier) < 1.0
        ):
            raise ValueError(
                "edge_penalties must map integer node pairs to finite values >= 1."
            )
    if src not in road_adjacency or dst not in NODES:
        return None
    if src == dst:
        return PathResult([src], [], 0.0, 0.0)

    # `destination` is an endpoint exception, not a cheaper through-road class.
    # Free-coordinate endpoints already snap to this strict public core, so they
    # must never unlock nearby residential/customer-only shortcuts. A named
    # landmark outside the core may receive one bounded connector approach.
    strict_core = _routing_view(
        profile.key, approved_override_snapshot,
    ).core
    source_needs_destination_access = src not in strict_core
    target_needs_destination_access = dst not in strict_core
    if not node_traversal_decision(
        src,
        profile,
        allow_destination_access=source_needs_destination_access,
    ).allowed or not node_traversal_decision(
        dst,
        profile,
        allow_destination_access=target_needs_destination_access,
    ).allowed:
        return None

    # Most searches can share the immutable vehicle-filtered view and avoid
    # re-running static eligibility checks for every expansion.  A named
    # endpoint outside the public core is a deliberate exception: its bounded
    # destination-only approach may require an edge/node that the strict view
    # excludes, so retain the full snapshot adjacency for that trip.
    uses_filtered_view = (
        not source_needs_destination_access and not target_needs_destination_access
    )
    if uses_filtered_view:
        road_adjacency = _routing_view(
            profile.key, approved_override_snapshot,
        ).adjacency

    avoid_nodes = avoid_nodes or set()
    banned_edges = banned_edges or set()
    edge_penalties = edge_penalties or {}
    effective_avoid = avoid_nodes - {src, dst}
    # Take one request-level snapshot. A concurrent historical-data refresh can
    # update its source mapping, but can never alter costs halfway through A*.
    speed_ratio_snapshot = dict(speed_ratios or {})
    congestion_snapshot = dict(congestion_estimates or {})
    if not all(
        isinstance(value, EdgeCongestionEstimate)
        for value in congestion_snapshot.values()
    ):
        raise TypeError(
            "congestion_estimates values must be EdgeCongestionEstimate objects."
        )

    goal = NODES[dst]

    endpoint_access_cache: Dict[int, bool] = {}

    def is_endpoint_access_node(node_id: int) -> bool:
        """Limit OSM ``destination`` access to the trip's local approaches."""
        cached = endpoint_access_cache.get(node_id)
        if cached is not None:
            return cached
        point = NODES[node_id]
        allowed = (
            source_needs_destination_access
            and haversine_m(point, NODES[src])
            <= ENDPOINT_DESTINATION_ACCESS_RADIUS_M
        ) or (
            target_needs_destination_access
            and haversine_m(point, goal)
            <= ENDPOINT_DESTINATION_ACCESS_RADIUS_M
        )
        endpoint_access_cache[node_id] = allowed
        return allowed

    alt_index = (
        None
        if approved_override_snapshot is not None
        and approved_override_snapshot.overrides
        else _alt_index_for_profile(profile.key)
    )
    static_feature_map = _static_edge_features(
        profile.key, approved_override_snapshot,
    )

    def heuristic(node: int) -> float:
        straight_distance_m = haversine_m(NODES[node], goal)
        if alt_index is not None:
            straight_distance_m = max(
                straight_distance_m,
                alt_index.distance_lower_bound(node, dst),
            )
        straight_proxy_s = straight_distance_m / profile.max_speed_kph * 3.6
        if preference.key == "BALANCED":
            coefficient = policy.cost_weights.time + policy.cost_weights.distance
        elif preference.key == "FASTEST":
            coefficient = 1.0
        else:
            coefficient = (
                preference.distance_proxy * policy.cost_weights.distance
                + preference.free_flow * policy.cost_weights.time
            )
        return straight_proxy_s * coefficient

    # State includes the incoming arc because turns and short maneuvers have a
    # real cost. A node-only A* can discard a slightly dearer arrival that leads
    # to a much cheaper onward turn and is therefore not correct for this model.
    start_state = (src, -1, -1)
    open_heap = [(heuristic(src), 0.0, start_state)]
    came_from: Dict[Tuple[int, int, int], Tuple[Tuple[int, int, int], RoadEdge]] = {}
    incoming_edge: Dict[Tuple[int, int, int], RoadEdge] = {}
    best_g: Dict[Tuple[int, int, int], float] = {start_state: 0.0}
    closed: set[Tuple[int, int, int]] = set()

    while open_heap:
        if (
            deadline_monotonic is not None
            and monotonic() >= deadline_monotonic
        ):
            return None
        _, g, state = heapq.heappop(open_heap)
        if state in closed:
            continue
        node, previous_source_raw, _ = state
        if node == dst:
            states = [state]
            chosen_edges: List[RoadEdge] = []
            while states[-1] in came_from:
                previous, edge = came_from[states[-1]]
                chosen_edges.append(edge)
                states.append(previous)
            states.reverse()
            chosen_edges.reverse()
            return PathResult(
                nodes=[item[0] for item in states],
                edges=chosen_edges,
                distance_m=sum(edge.distance_m for edge in chosen_edges),
                search_cost_s=g,
            )
        closed.add(state)
        if (
            max_settled_states is not None
            and len(closed) >= max(0, int(max_settled_states))
        ):
            return None
        previous_source = previous_source_raw if previous_source_raw >= 0 else None
        previous_edge = incoming_edge.get(state)

        for edge in road_adjacency.get(node, ()):
            if (
                deadline_monotonic is not None
                and monotonic() >= deadline_monotonic
            ):
                return None
            nxt = edge.target
            if _edge_key(node, edge) in banned_edges:
                continue
            if not uses_filtered_view:
                edge_decision = edge_traversal_decision(edge, profile)
                if not edge_decision.allowed:
                    edge_decision = edge_traversal_decision(
                        edge,
                        profile,
                        allow_destination_access=(
                            is_endpoint_access_node(node)
                            and is_endpoint_access_node(nxt)
                        ),
                    )
                if not edge_decision.allowed:
                    continue
                node_decision = node_traversal_decision(nxt, profile)
                if not node_decision.allowed:
                    node_decision = node_traversal_decision(
                        nxt, profile,
                        allow_destination_access=is_endpoint_access_node(nxt),
                    )
                if not node_decision.allowed:
                    continue
            next_state = (nxt, node, edge.road_index)
            if next_state in closed:
                continue
            components = _edge_cost_components(
                node, edge, profile, previous_source=previous_source,
                previous_edge=previous_edge, congestion_scores=congestion_scores,
                speed_ratios=speed_ratio_snapshot,
                congestion_estimates=congestion_snapshot,
                default_congestion_estimate=default_congestion_estimate,
                network_congestion_score=network_congestion_score,
                weather=weather, prefer_through_roads=prefer_through_roads,
                learning_snapshot=learning_snapshot,
                static_features=static_feature_map,
                eligible_adjacency=(road_adjacency if uses_filtered_view else None),
            )
            # Alternative-generation and compatibility avoidance are search
            # penalties only. They never leak into distance or published ETA.
            cost = _edge_objective_cost_s(
                edge, components, profile, preference,
            )
            cost *= 15.0 if nxt in effective_avoid else 1.0
            cost *= edge_penalties.get(_undirected_edge_key(node, nxt), 1.0)
            tentative = g + cost
            if tentative < best_g.get(next_state, math.inf):
                best_g[next_state] = tentative
                came_from[next_state] = (state, edge)
                incoming_edge[next_state] = edge
                heapq.heappush(
                    open_heap, (tentative + heuristic(nxt), tentative, next_state),
                )

    return None


def astar(
    src: int,
    dst: int,
    avoid_nodes: Optional[set[int]] = None,
    *,
    route_preference: str = "BALANCED",
) -> Optional[Tuple[List[int], float]]:
    """Compatibility wrapper returning ``(node ids, physical metres)``."""
    result = astar_detailed(
        src, dst, avoid_nodes=avoid_nodes, route_preference=route_preference,
    )
    if result is None:
        return None
    return result.nodes, result.distance_m


def _perpendicular_m(pt, start, end) -> float:
    """Distance from `pt` to the segment start->end, in metres.

    Uses a local equirectangular projection, which is accurate to well under a
    metre across a single city and avoids trigonometry per candidate point.
    """
    lat_scale = EARTH_RADIUS_M * math.pi / 180.0
    lon_scale = lat_scale * math.cos(math.radians(start[0]))

    px, py = (pt[1] - start[1]) * lon_scale, (pt[0] - start[0]) * lat_scale
    ex, ey = (end[1] - start[1]) * lon_scale, (end[0] - start[0]) * lat_scale

    seg_sq = ex * ex + ey * ey
    if seg_sq < 1e-12:
        return math.hypot(px, py)
    # Project onto the segment, clamped to its endpoints.
    t = max(0.0, min(1.0, (px * ex + py * ey) / seg_sq))
    return math.hypot(px - t * ex, py - t * ey)


def _simplify(path: List[int], tolerance_m: float = 2.0) -> List[List[float]]:

    """Douglas-Peucker: drop vertices that don't change the drawn shape.

    An OSM path traces every surveyed vertex; most add nothing at map zoom.
    Note this must measure each point against the *retained* segment, not
    against its immediate neighbours — a chain of individually-small deviations
    along a curve otherwise sums to an unbounded error, which collapsed a 15 km
    route to a 2-point straight line.
    """
    pts = [NODES[n] for n in path]
    if len(pts) <= 2:
        return [[round(la, 6), round(ln, 6)] for la, ln in pts]

    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]

    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        worst_i, worst_d = -1, 0.0
        for i in range(lo + 1, hi):
            d = _perpendicular_m(pts[i], pts[lo], pts[hi])
            if d > worst_d:
                worst_i, worst_d = i, d
        if worst_d > tolerance_m:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))

    return [[round(pts[i][0], 6), round(pts[i][1], 6)]
            for i in range(len(pts)) if keep[i]]


def _path_geometry(path: PathResult) -> List[List[float]]:
    """Preserve reviewed connector geometry; simplify ordinary OSM paths."""
    if not any(edge.approved_geometry for edge in path.edges):
        return _simplify(path.nodes)
    points: List[Tuple[float, float]] = [NODES[path.nodes[0]]]
    for source, edge in zip(path.nodes, path.edges):
        geometry = list(edge.approved_geometry)
        if geometry:
            source_coordinate = NODES[source]
            if haversine_m(source_coordinate, geometry[-1]) < haversine_m(
                source_coordinate, geometry[0],
            ):
                geometry.reverse()
            for point in geometry:
                if not points or haversine_m(points[-1], point) > 0.1:
                    points.append(point)
        target_coordinate = NODES[edge.target]
        if haversine_m(points[-1], target_coordinate) > 0.1:
            points.append(target_coordinate)
    return [[round(lat, 6), round(lng, 6)] for lat, lng in points]


def _bearing(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    delta_lng = math.radians(b[1] - a[1])
    x = math.sin(delta_lng) * math.cos(lat2)
    y = (
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lng)
    )
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _window_bearing(nodes: Sequence[int], index: int, incoming: bool) -> float:
    """Bearing around a junction using a 20m window to suppress node jitter."""
    pivot = NODES[nodes[index]]
    cursor = index
    distance = 0.0
    if incoming:
        while cursor > 0 and distance < 20.0:
            previous = NODES[nodes[cursor - 1]]
            distance += haversine_m(previous, NODES[nodes[cursor]])
            cursor -= 1
        return _bearing(NODES[nodes[cursor]], pivot)
    while cursor < len(nodes) - 1 and distance < 20.0:
        following = NODES[nodes[cursor + 1]]
        distance += haversine_m(NODES[nodes[cursor]], following)
        cursor += 1
    return _bearing(pivot, NODES[nodes[cursor]])


def _signed_turn(incoming: float, outgoing: float) -> float:
    """Signed bearing delta in [-180, 180]; positive is clockwise/right."""
    return (outgoing - incoming + 540.0) % 360.0 - 180.0


def _road_label(edge: RoadEdge) -> str:
    if edge.name:
        return edge.name
    if edge.ref:
        return edge.ref
    if edge.highway:
        return f"Unnamed {edge.highway.replace('_', ' ')} road"
    return "Unnamed road"


def _same_road(first: RoadEdge, second: RoadEdge) -> bool:
    """Match a continuing road when either its OSM name or ref is stable."""
    first_name = first.name.casefold().strip() if first.name else None
    second_name = second.name.casefold().strip() if second.name else None
    if first_name and second_name and first_name == second_name:
        return True
    first_ref = first.ref.casefold().strip() if first.ref else None
    second_ref = second.ref.casefold().strip() if second.ref else None
    if first_ref and second_ref and first_ref == second_ref:
        return True
    if first.name or first.ref or second.name or second.ref:
        return False
    return (
        first.highway == second.highway
        and first.road_index == second.road_index
    )


def _compass_direction(bearing: float) -> str:
    directions = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
    return directions[int((bearing + 22.5) // 45.0) % 8]


def _turn_kind(delta: float) -> Tuple[str, str, str]:
    magnitude = abs(delta)
    side = "right" if delta > 0 else "left"
    icon = f"turn_{side}"
    if magnitude >= 170:
        return "U_TURN", "Make a U-turn", icon
    if magnitude >= 135:
        return f"SHARP_{side.upper()}", f"Make a sharp {side}", icon
    if magnitude >= 40:
        return f"TURN_{side.upper()}", f"Turn {side}", icon
    if magnitude >= 15:
        return f"SLIGHT_{side.upper()}", f"Keep slightly {side}", icon
    return "CONTINUE", "Continue straight", "straight"


def _roundabout_exit_count(path: PathResult, first_edge: int, last_edge: int) -> int:
    """Count legal non-roundabout exits passed, including the selected exit."""
    exits = 0
    for edge_index in range(first_edge, last_edge + 1):
        node_index = edge_index + 1
        node = path.nodes[node_index]
        previous = path.nodes[node_index - 1]
        choices = {
            edge.target
            for edge in ROAD_ADJ.get(node, ())
            if edge.target != previous and edge.junction not in ("roundabout", "circular")
        }
        exits += len(choices)
    return max(1, exits)


def generate_navigation(
    path: PathResult,
    origin_name: str = "Origin",
    destination_name: str = "Destination",
) -> Dict[str, Any]:
    """Generate sourced maneuvers from the exact unsimplified chosen edges."""
    if not path.edges:
        return {
            "schema_version": 1,
            "data_source": "openstreetmap_edge_metadata" if ROADS else "openstreetmap_geometry",
            "maneuvers": [],
            "landmarks_along_route": [],
            "traffic_lights_count": 0,
        }

    events: List[Dict[str, Any]] = []
    first_edge = path.edges[0]
    initial_bearing = _window_bearing(path.nodes, 0, incoming=False)
    first_road = _road_label(first_edge)
    events.append({
        "node_index": 0,
        "type": "DEPART",
        "modifier": "straight",
        "instruction": f"Head {_compass_direction(initial_bearing)} on {first_road}",
        "street": first_road,
        "road_ref": first_edge.ref,
        "icon": "depart",
        "coords": list(NODES[path.nodes[0]]),
        "bearing_before": None,
        "bearing_after": round(initial_bearing, 1),
        "landmark": origin_name,
    })

    skipped_nodes: set[int] = set()
    edge_index = 0
    while edge_index < len(path.edges):
        edge = path.edges[edge_index]
        if edge.junction not in ("roundabout", "circular"):
            edge_index += 1
            continue
        last_roundabout_edge = edge_index
        while (
            last_roundabout_edge + 1 < len(path.edges)
            and path.edges[last_roundabout_edge + 1].junction in ("roundabout", "circular")
        ):
            last_roundabout_edge += 1
        exit_edge_index = last_roundabout_edge + 1
        exit_edge = path.edges[exit_edge_index] if exit_edge_index < len(path.edges) else None
        exit_number = _roundabout_exit_count(path, edge_index, last_roundabout_edge)
        target_road = _road_label(exit_edge) if exit_edge else "your destination road"
        events.append({
            "node_index": edge_index,
            "type": "ROUNDABOUT",
            "modifier": "roundabout",
            "instruction": f"At the roundabout, take exit {exit_number} onto {target_road}",
            "street": target_road,
            "road_ref": exit_edge.ref if exit_edge else None,
            "icon": "roundabout",
            "coords": list(NODES[path.nodes[edge_index]]),
            "bearing_before": round(_window_bearing(path.nodes, edge_index, True), 1) if edge_index else None,
            "bearing_after": round(_window_bearing(path.nodes, min(exit_edge_index, len(path.nodes) - 1), False), 1) if exit_edge_index < len(path.nodes) - 1 else None,
            "exit_number": exit_number,
            "landmark": None,
        })
        skipped_nodes.update(range(edge_index + 1, min(exit_edge_index + 1, len(path.nodes) - 1)))
        edge_index = max(edge_index + 1, exit_edge_index)

    for node_index in range(1, len(path.nodes) - 1):
        if node_index in skipped_nodes:
            continue
        incoming_edge = path.edges[node_index - 1]
        outgoing_edge = path.edges[node_index]
        incoming_bearing = _window_bearing(path.nodes, node_index, incoming=True)
        outgoing_bearing = _window_bearing(path.nodes, node_index, incoming=False)
        delta = _signed_turn(incoming_bearing, outgoing_bearing)
        road_changed = not _same_road(incoming_edge, outgoing_edge)
        legal_choices = {
            (candidate.target, candidate.road_index)
            for candidate in ROAD_ADJ.get(path.nodes[node_index], ())
            if candidate.target != path.nodes[node_index - 1]
        }
        is_decision = len(legal_choices) > 1
        if not road_changed and not (is_decision and abs(delta) >= 35.0):
            continue

        maneuver_type, phrase, icon = _turn_kind(delta)
        road = _road_label(outgoing_edge)
        if outgoing_edge.highway and outgoing_edge.highway.endswith("_link"):
            maneuver_type = "TAKE_RAMP"
            phrase = "Take the ramp"
        instruction = f"{phrase} onto {road}" if road != "Unnamed road" else f"{phrase} at the next junction"
        events.append({
            "node_index": node_index,
            "type": maneuver_type,
            "modifier": maneuver_type.lower(),
            "instruction": instruction,
            "street": road,
            "road_ref": outgoing_edge.ref,
            "icon": icon,
            "coords": list(NODES[path.nodes[node_index]]),
            "bearing_before": round(incoming_bearing, 1),
            "bearing_after": round(outgoing_bearing, 1),
            "landmark": None,
        })

    events.append({
        "node_index": len(path.nodes) - 1,
        "type": "ARRIVE",
        "modifier": "arrive",
        "instruction": f"Arrive at {destination_name}",
        "street": destination_name,
        "road_ref": None,
        "icon": "arrive",
        "coords": list(NODES[path.nodes[-1]]),
        "bearing_before": round(_window_bearing(path.nodes, len(path.nodes) - 1, True), 1),
        "bearing_after": None,
        "landmark": destination_name,
    })

    # A road-name boundary and an angle boundary may identify the same node.
    # Prefer the roundabout instruction at ordinary junctions. Preserve both
    # DEPART and an immediate roundabout instruction when a landmark itself is
    # on a roundabout (notably Hang Nadim's public access node).
    events_by_index: Dict[int, List[Dict[str, Any]]] = {}
    def event_priority(event: Dict[str, Any]) -> int:
        # The route contract always begins with DEPART and ends with ARRIVE.
        # At intermediate duplicate indices, a sourced roundabout instruction
        # is more useful than a generic road-name/angle transition.
        if event["type"] in {"DEPART", "ARRIVE"}:
            return 0
        return 1 if event["type"] == "ROUNDABOUT" else 2

    for event in events:
        events_by_index.setdefault(event["node_index"], []).append(event)
    ordered: List[Dict[str, Any]] = []
    for node_index in sorted(events_by_index):
        at_node = sorted(events_by_index[node_index], key=event_priority)
        depart_events = [event for event in at_node if event["type"] == "DEPART"]
        arrive_events = [event for event in at_node if event["type"] == "ARRIVE"]
        roundabout_events = [event for event in at_node if event["type"] == "ROUNDABOUT"]
        if depart_events or arrive_events:
            ordered.extend(depart_events[:1])
            ordered.extend(roundabout_events[:1])
            ordered.extend(arrive_events[:1])
        elif roundabout_events:
            ordered.append(roundabout_events[0])
        else:
            ordered.append(at_node[0])
    cumulative = 0.0
    for index, event in enumerate(ordered):
        start = event["node_index"]
        end = ordered[index + 1]["node_index"] if index + 1 < len(ordered) else start
        distance = sum(edge.distance_m for edge in path.edges[start:end])
        event["step"] = index + 1
        event["distance_m"] = round(distance)
        event["cumulative_distance_m"] = round(cumulative)
        cumulative += distance
        event.pop("node_index", None)

    signal_count = sum(
        1 for node in path.nodes
        if NODE_META.get(node, {}).get("highway") == "traffic_signals"
    )
    return {
        "schema_version": 1,
        "data_source": "openstreetmap_edge_metadata" if ROADS else "openstreetmap_geometry",
        "maneuvers": ordered,
        "landmarks_along_route": [],
        "traffic_lights_count": signal_count,
        "route_narrative_words": " ".join(event["instruction"] + "." for event in ordered),
    }


def alternative_paths(
    primary: PathResult,
    avoid_nodes: Optional[set[int]] = None,
    limit: int = 2,
    *,
    vehicle_type: str = "COMMUTER",
    congestion_scores: Optional[Dict[int, float]] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    route_preference: str = "BALANCED",
    learning_snapshot: Optional[LearningSnapshot] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    max_searches: Optional[int] = None,
    time_budget_ms: Optional[float] = None,
    max_settled_states: Optional[int] = None,
) -> List[Tuple[PathResult, float]]:
    """Return bounded, meaningfully distinct alternatives via edge penalties."""
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    if len(primary.edges) < 4 or limit <= 0:
        return []
    max_searches, time_budget_ms, max_settled_states = _alternative_budget(
        max_searches, time_budget_ms, max_settled_states,
    )
    if max_searches <= 0 or max_settled_states <= 0:
        return []
    deadline = monotonic() + time_budget_ms / 1000.0

    # Increasing penalties progressively encourage a different corridor while
    # keeping the search bounded and deterministic. Penalties are undirected so
    # a route cannot fake diversity by traversing the same road in reverse.
    penalty_schedule = (1.50, 1.80, 2.0, 2.20, 2.80, 3.80, 5.50)
    candidates: List[Tuple[PathResult, float]] = []
    seen_paths = {tuple(primary.nodes)}
    accepted = [primary]

    def route_quality(path: PathResult) -> Tuple[int, int, int, float]:
        maneuvers = generate_navigation(path)["maneuvers"]
        decisive = {
            "TURN_LEFT", "TURN_RIGHT", "SHARP_LEFT", "SHARP_RIGHT",
            "U_TURN", "TAKE_RAMP", "ROUNDABOUT",
        }
        turn_count = sum(step["type"] in decisive for step in maneuvers)
        short_turn_count = sum(
            step["type"] in decisive and step["distance_m"] < 50
            for step in maneuvers
        )
        residential_metres = sum(
            edge.distance_m for edge in path.edges
            if edge.highway in {"residential", "unclassified"}
        )
        # Used only to report/compare route quality; physical distance remains
        # the exact edge sum and travel time is computed separately.
        quality_score = (
            path.distance_m + turn_count * 45.0 + short_turn_count * 90.0
            + residential_metres * 0.20
        )
        return len(maneuvers), turn_count, short_turn_count, quality_score

    primary_maneuvers, primary_turns, primary_short_turns, _ = route_quality(primary)
    profile = vehicle_profile(vehicle_type)
    primary_generalized_s = path_cost_breakdown(
        primary, profile, congestion_scores=congestion_scores,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        learning_snapshot=learning_snapshot,
    )["generalized_cost_s"]

    def overlap_ratio(candidate: PathResult, other: PathResult) -> float:
        other_keys = {
            _undirected_edge_key(source, edge.target)
            for source, edge in zip(other.nodes, other.edges)
        }
        shared_metres = sum(
            edge.distance_m
            for source, edge in zip(candidate.nodes, candidate.edges)
            if _undirected_edge_key(source, edge.target) in other_keys
        )
        return shared_metres / candidate.distance_m if candidate.distance_m else 1.0

    attempted_searches = 0
    similar_rejections = 0
    for penalty in penalty_schedule:
        if attempted_searches >= max_searches or monotonic() >= deadline:
            break
        # Penalize every already accepted route, not only the primary. This
        # avoids repeatedly rediscovering alternative 1 when searching for a
        # second genuinely different corridor.
        penalized_keys = {
            _undirected_edge_key(source, edge.target)
            for accepted_route in accepted
            for source, edge in zip(accepted_route.nodes, accepted_route.edges)
        }
        attempted_searches += 1
        candidate = astar_detailed(
            primary.nodes[0], primary.nodes[-1],
            avoid_nodes=avoid_nodes,
            edge_penalties={key: penalty for key in penalized_keys},
            prefer_through_roads=True,
            vehicle_type=vehicle_type,
            congestion_scores=congestion_scores,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score,
            weather=weather,
            route_preference=route_preference,
            learning_snapshot=learning_snapshot,
            approved_override_snapshot=approved_override_snapshot,
            deadline_monotonic=deadline,
            max_settled_states=max_settled_states,
        )
        if candidate is None:
            # A deadline/settled-state cutoff is terminal for this request:
            # later penalties cannot produce a complete candidate within the
            # same synchronous budget.
            break
        if tuple(candidate.nodes) in seen_paths:
            continue
        seen_paths.add(tuple(candidate.nodes))
        if candidate.distance_m > primary.distance_m * 1.65:
            continue
        candidate_generalized_s = path_cost_breakdown(
            candidate, profile, congestion_scores=congestion_scores,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            learning_snapshot=learning_snapshot,
        )["generalized_cost_s"]
        if candidate_generalized_s > primary_generalized_s * 1.85:
            continue
        overlaps = [overlap_ratio(candidate, route) for route in accepted]
        if any(overlap > 0.82 for overlap in overlaps):
            similar_rejections += 1
            # Once two successive bounded searches produce only near-duplicate
            # corridors, further penalties are unlikely to yield a useful
            # option. Stopping here only reduces optional alternatives; it can
            # never alter the already-computed primary route.
            if similar_rejections >= 2:
                break
            continue
        similar_rejections = 0
        maneuver_count, turn_count, short_turn_count, _ = route_quality(candidate)
        if maneuver_count > max(primary_maneuvers + 10, math.ceil(primary_maneuvers * 1.75)):
            continue
        if turn_count > max(primary_turns + 8, primary_turns * 2):
            continue
        if short_turn_count > max(primary_short_turns + 4, primary_short_turns * 3):
            continue
        primary_overlap = overlaps[0]
        candidates.append((candidate, round(primary_overlap, 3)))
        accepted.append(candidate)
        if len(candidates) >= limit:
            break

    return candidates


def _path_learning_audit(
    path: PathResult,
    profile: VehicleProfile,
    learning_snapshot: LearningSnapshot,
    *,
    congestion_scores: Optional[Dict[int, float]] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
) -> Tuple[List[Dict[str, Any]], int, float]:
    """Describe only qualifying learned edges that the selected path uses."""
    selected: List[Optional[Dict[str, Any]]] = []
    applied_count = 0
    max_local_score = 0.0
    global_score = max(0.0, min(100.0, float(network_congestion_score)))
    for source, edge in zip(path.nodes, path.edges):
        local_score = _edge_local_congestion_score(
            source,
            edge,
            congestion_scores,
        )
        max_local_score = max(max_local_score, local_score)
        baseline_s, effective_s, learned = _edge_learning_adjustment(
            source,
            edge,
            profile,
            learning_snapshot,
            local_congestion_score=local_score,
            network_congestion_score=global_score,
            weather=weather,
        )
        if learned is None:
            selected.append(None)
            continue
        applied_count += 1
        if effective_s > baseline_s * 0.95:
            # Slower/near-equal verified observations may improve ETA accuracy,
            # but they are not honestly described as shortcuts.
            selected.append(None)
            continue
        selected.append({
            "source_node": source,
            "target_node": edge.target,
            "road_index": edge.road_index,
            "road_name": edge.name,
            "road_ref": edge.ref,
            "highway": edge.highway,
            "baseline_s": baseline_s,
            "effective_s": effective_s,
            "sample_count": learned.sample_count,
            "confidence": learned.confidence,
        })

    shortcuts: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        first = current[0]
        last = current[-1]
        baseline_s = sum(float(item["baseline_s"]) for item in current)
        learned_s = sum(float(item["effective_s"]) for item in current)
        saved_s = max(0.0, baseline_s - learned_s)
        road_label = (
            first.get("road_name")
            or first.get("road_ref")
            or str(first.get("highway") or "Mapped road")
        )
        edge_keys = [
            {
                "source_node": int(item["source_node"]),
                "target_node": int(item["target_node"]),
                "road_index": int(item["road_index"]),
            }
            for item in current
        ]
        shortcuts.append({
            "id": (
                f"learned:{profile.key}:{first['source_node']}:"
                f"{last['target_node']}:{first['road_index']}"
            ),
            "name": str(road_label),
            "badge": "Verified clear-road shortcut",
            "time_saved_mins": round(saved_s / 60.0, 3),
            "description": (
                f"{len(current)} selected OSM edge"
                f"{'s' if len(current) != 1 else ''} learned from verified "
                "clear-road traversals; saving is relative to the published "
                "OSM road-class baseline."
            ),
            "baseline_time_mins": round(baseline_s / 60.0, 3),
            "learned_time_mins": round(learned_s / 60.0, 3),
            "confidence": round(
                min(float(item["confidence"]) for item in current),
                4,
            ),
            "sample_count": min(int(item["sample_count"]) for item in current),
            "edge_keys": edge_keys,
            "graph_revision": learning_snapshot.graph_revision,
            "learning_revision": learning_snapshot.revision,
            "source": "verified_actual_traversal",
        })
        current.clear()

    for item in selected:
        if item is None:
            flush()
            continue
        if current and (
            item["road_index"] != current[-1]["road_index"]
            or item["source_node"] != current[-1]["target_node"]
        ):
            flush()
        current.append(item)
    flush()
    return shortcuts, applied_count, max_local_score


def _path_local_road_audit(path: PathResult) -> Dict[str, Any]:
    """Summarize selected mapped residential roads without inferring width."""
    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    previous_edge: Optional[RoadEdge] = None
    total_distance_m = 0.0

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current["distance_km"] = round(
            float(current.pop("distance_m")) / 1000.0,
            3,
        )
        segments.append(current)
        current = None

    for source, edge in zip(path.nodes, path.edges):
        highway, _ = _highway_class(edge)
        if highway not in {"residential", "living_street"}:
            flush()
            previous_edge = None
            continue
        total_distance_m += edge.distance_m

        if (
            current is None
            or previous_edge is None
            or not _same_road(previous_edge, edge)
        ):
            flush()
            current = {
                "id": f"local-road:{source}:{edge.target}:{edge.road_index}",
                "name": _road_label(edge),
                "highway": highway,
                "source_node": source,
                "target_node": edge.target,
                "edge_count": 1,
                "distance_m": edge.distance_m,
            }
        else:
            current["target_node"] = edge.target
            current["edge_count"] = int(current["edge_count"]) + 1
            current["distance_m"] = (
                float(current["distance_m"]) + edge.distance_m
            )
        previous_edge = edge
    flush()

    return {
        "distance_km": round(total_distance_m / 1000.0, 3),
        "segment_count": len(segments),
        "segments": segments,
        "metadata_scope": "mapped_osm_residential_motor_roads",
        "width_clearance_verified": False,
    }


def _path_sourced_capacity_audit(path: PathResult) -> Dict[str, Any]:
    """Summarize sourced speed/lane metadata without fabricating capacity."""
    maxspeed_edges = [edge for edge in path.edges if edge.maxspeed_kph is not None]
    lane_edges = [edge for edge in path.edges if edge.lanes is not None]
    lane_distance = sum(edge.distance_m for edge in lane_edges)
    weighted_lanes = (
        sum(edge.distance_m * float(edge.lanes) for edge in lane_edges)
        / lane_distance
        if lane_distance > 0.0 else None
    )
    return {
        "edge_count": len(path.edges),
        "maxspeed_tagged_edge_count": len(maxspeed_edges),
        "minimum_sourced_maxspeed_kph": (
            round(min(float(edge.maxspeed_kph) for edge in maxspeed_edges), 2)
            if maxspeed_edges else None
        ),
        "lane_tagged_edge_count": len(lane_edges),
        "lane_tagged_distance_km": round(lane_distance / 1000.0, 3),
        "distance_weighted_sourced_lanes": (
            round(weighted_lanes, 2) if weighted_lanes is not None else None
        ),
        "capacity_inference": None,
        "note": (
            "Sourced maxspeed caps the free-flow planning speed. Sourced lane "
            "counts are audit metadata only and do not create legal access or "
            "an unsourced vehicles-per-hour capacity."
        ),
    }


def _path_payload(
    result: PathResult,
    src: int,
    dst: int,
    origin_name: str,
    destination_name: str,
    *,
    vehicle_type: str = "COMMUTER",
    congestion_scores: Optional[Dict[int, float]] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    spatial_zone_count: int = 0,
    route_preference: str = "BALANCED",
    learning_snapshot: Optional[LearningSnapshot] = None,
) -> Dict[str, Any]:
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    straight = haversine_m(NODES[src], NODES[dst])
    profile = vehicle_profile(vehicle_type)
    vehicle_policy = vehicle_routing_policy(profile)
    vehicle_policy_payload = vehicle_routing_policy_payload(vehicle_policy)
    preference = _validated_route_preference(route_preference)
    raw_breakdown = path_cost_breakdown(
        result, profile, congestion_scores=congestion_scores,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        learning_snapshot=learning_snapshot,
    )
    breakdown = {
        "baseline_free_flow_mins": round(
            raw_breakdown["baseline_free_flow_s"] / 60.0,
            2,
        ),
        "free_flow_mins": round(raw_breakdown["free_flow_s"] / 60.0, 2),
        "learned_free_flow_adjustment_mins": round(
            raw_breakdown["learned_free_flow_adjustment_s"] / 60.0,
            3,
        ),
        "verified_learning_time_saved_mins": round(
            max(0.0, -raw_breakdown["learned_free_flow_adjustment_s"]) / 60.0,
            3,
        ),
        "congestion_delay_mins": round(
            raw_breakdown["congestion_delay_s"] / 60.0, 2,
        ),
        "weather_delay_mins": round(
            raw_breakdown["weather_delay_s"] / 60.0, 2,
        ),
        "maneuver_delay_mins": round(
            raw_breakdown["maneuver_delay_s"] / 60.0, 2,
        ),
        "road_suitability_penalty_mins": round(
            raw_breakdown["suitability_penalty_s"] / 60.0, 2,
        ),
        "road_quality_penalty_mins": round(
            raw_breakdown["road_quality_penalty_s"] / 60.0, 2,
        ),
        "expected_objective_cost_mins": round(
            raw_breakdown["expected_objective_cost_s"] / 60.0, 2,
        ),
        "congestion_uncertainty_mins": round(
            raw_breakdown["congestion_uncertainty_s"] / 60.0, 2,
        ),
        "uncertainty_penalty_mins": round(
            raw_breakdown["uncertainty_penalty_s"] / 60.0, 2,
        ),
        "modeled_travel_time_mins": round(
            raw_breakdown["modeled_travel_s"] / 60.0, 2,
        ),
        "generalized_cost_mins": round(
            raw_breakdown["generalized_cost_s"] / 60.0, 2,
        ),
    }
    objective_cost_s = path_objective_cost_s(
        result, profile, preference, congestion_scores=congestion_scores,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        learning_snapshot=learning_snapshot,
    )
    shortcuts, applied_edge_count, max_local_score = _path_learning_audit(
        result,
        profile,
        learning_snapshot,
        congestion_scores=congestion_scores,
        network_congestion_score=network_congestion_score,
        weather=weather,
    )
    local_road_audit = _path_local_road_audit(result)
    sourced_capacity_audit = _path_sourced_capacity_audit(result)
    preference_payload = route_preference_payload(preference)
    payload = {
        "distance_km": round(result.distance_m / 1000.0, 2),
        "straight_line_km": round(straight / 1000.0, 2),
        "detour_ratio": round(result.distance_m / straight, 2) if straight > 0 else None,
        "path_node_count": len(result.nodes),
        "geometry": _path_geometry(result),
        "data_source": "openstreetmap",
        "navigation": generate_navigation(result, origin_name, destination_name),
        "modeled_travel_time_mins": breakdown["modeled_travel_time_mins"],
        "generalized_cost_mins": breakdown["generalized_cost_mins"],
        "objective_cost_s": objective_cost_s,
        "route_preference": preference.key,
        "route_preference_profile": preference_payload,
        "local_road_distance_km": local_road_audit["distance_km"],
        "local_road_segments": local_road_audit["segments"],
        "local_road_audit": {
            "requested": preference.key == "LOCAL",
            "segment_count": local_road_audit["segment_count"],
            "metadata_scope": local_road_audit["metadata_scope"],
            "width_clearance_verified": False,
            "note": (
                "These are mapped OSM residential motor roads selected by "
                "the route. Service alleys, drains and footpaths are absent; "
                "lane width and vehicle clearance are not verified."
            ),
        },
        "routing_cost_breakdown": breakdown,
        "routing_model": {
            "version": 6,
            # Retain the established transport envelope contract; the more
            # precise search-only unit is published alongside it.
            "cost_unit": "seconds",
            "objective_cost_unit": "weighted_seconds",
            "objective": preference.description,
            "selected_preference": preference.key,
            "road_scope": preference.road_scope,
            "component_weights": preference_payload["component_weights"],
            "vehicle_cost_function": {
                "formula": vehicle_policy_payload["cost_function"],
                "component_weights": vehicle_policy_payload["cost_weights"],
                "component_units": {
                    "time": "seconds",
                    "distance": (
                        "seconds proxy: metres / vehicle max speed"
                    ),
                    "congestion": "expected delay seconds",
                    "road_quality": "quality-loss seconds",
                    "uncertainty": "risk-adjusted delay seconds",
                },
                "risk_aversion": vehicle_policy.risk_aversion,
                "expected_objective_cost_s": round(
                    raw_breakdown["expected_objective_cost_s"], 3,
                ),
                "uncertainty_penalty_s": round(
                    raw_breakdown["uncertainty_penalty_s"], 3,
                ),
            },
            "objective_cost_s": objective_cost_s,
            "network_congestion_score": round(network_congestion_score, 1),
            "default_congestion_estimate": (
                {
                    "expected_speed_ratio": round(
                        default_congestion_estimate.expected_speed_ratio, 6,
                    ),
                    "speed_ratio_std": round(
                        default_congestion_estimate.speed_ratio_std, 6,
                    ),
                    "p90_speed_ratio": (
                        round(default_congestion_estimate.p90_speed_ratio, 6)
                        if default_congestion_estimate.p90_speed_ratio is not None
                        else None
                    ),
                    "p90_delay_s": round(
                        default_congestion_estimate.p90_delay_s, 3,
                    ),
                    "source": default_congestion_estimate.source,
                    "scope": "corridor_default_applied_to_each_selected_edge",
                }
                if default_congestion_estimate is not None else None
            ),
            "weather": weather,
            "spatial_zone_count": max(0, int(spatial_zone_count)),
            "learning": {
                "graph_revision": learning_snapshot.graph_revision,
                "revision": learning_snapshot.revision,
                "qualifying_edge_count": len(learning_snapshot.entries),
                "selected_applied_edge_count": applied_edge_count,
                "selected_shortcut_segment_count": len(shortcuts),
                "gate": {
                    "weather_clear": weather == 0,
                    "network_congestion_score": round(
                        max(0.0, min(100.0, network_congestion_score)),
                        1,
                    ),
                    "network_threshold": MAX_CLEAR_CONGESTION_SCORE,
                    "selected_path_max_local_congestion_score": round(
                        max_local_score,
                        1,
                    ),
                    "local_threshold": MAX_CLEAR_CONGESTION_SCORE,
                    "eligible": (
                        weather == 0
                        and network_congestion_score <= MAX_CLEAR_CONGESTION_SCORE
                        and max_local_score <= MAX_CLEAR_CONGESTION_SCORE
                    ),
                },
                "physical_floor": "edge_distance / vehicle_max_speed",
                "provenance": "verified_actual_traversal",
            },
            "sourced_road_capacity_audit": sourced_capacity_audit,
            "distance_is_raw_edge_sum": True,
            "traffic_provenance": "forecast/simulated unless response provenance says live",
            "limitations": (
                "Planning estimate from OSM road classes. Explicit edge "
                "access/clearance metadata is enforced when present; untagged "
                "clearance and OSM turn-restriction relations remain unverified."
            ),
        },
        "vehicle_profile": vehicle_profile_payload(profile),
    }
    approved_overrides = [
        {
            "override_id": edge.approved_override_id,
            "candidate_sha256": edge.approved_candidate_sha256,
            "source_node": source,
            "target_node": edge.target,
            "vehicle_modes": list(edge.vehicle_modes),
        }
        for source, edge in zip(result.nodes, result.edges)
        if edge.approved_override_id is not None
    ]
    if approved_overrides:
        payload["approved_graph_overrides_used"] = approved_overrides
    if shortcuts:
        payload["shortcuts_used"] = shortcuts
    return payload


def _with_alternatives(
    payload: Dict[str, Any],
    primary: PathResult,
    src: int,
    dst: int,
    origin_name: str,
    destination_name: str,
    avoid_nodes: Optional[set[int]] = None,
    *,
    vehicle_type: str = "COMMUTER",
    congestion_scores: Optional[Dict[int, float]] = None,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    network_congestion_score: float = 0.0,
    weather: int = 0,
    spatial_zone_count: int = 0,
    route_preference: str = "BALANCED",
    learning_snapshot: Optional[LearningSnapshot] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    alternative_max_searches: Optional[int] = None,
    alternative_time_budget_ms: Optional[float] = None,
    alternative_max_settled_states: Optional[int] = None,
) -> Dict[str, Any]:
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    alternatives = []
    for index, (candidate, overlap) in enumerate(
        alternative_paths(
            primary, avoid_nodes=avoid_nodes, vehicle_type=vehicle_type,
            congestion_scores=congestion_scores,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            route_preference=route_preference,
            learning_snapshot=learning_snapshot,
            approved_override_snapshot=approved_override_snapshot,
            max_searches=alternative_max_searches,
            time_budget_ms=alternative_time_budget_ms,
            max_settled_states=alternative_max_settled_states,
        ), start=1,
    ):
        alternative = _path_payload(
            candidate, src, dst, origin_name, destination_name,
            vehicle_type=vehicle_type, congestion_scores=congestion_scores,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            spatial_zone_count=spatial_zone_count,
            route_preference=route_preference,
            learning_snapshot=learning_snapshot,
        )
        alternatives.append({
            "id": f"osm-alternative-{index}",
            "name": f"Alternative road route {index}",
            "description": "A distinct drivable path computed over the same OpenStreetMap graph.",
            "route_geometry": alternative.pop("geometry"),
            "route_data_source": alternative.pop("data_source"),
            "overlap_ratio": overlap,
            **alternative,
        })
    payload["alternatives"] = alternatives
    return payload


def route_between(
    origin_key: str,
    dest_key: str,
    include_alternatives: bool = False,
    origin_name: Optional[str] = None,
    destination_name: Optional[str] = None,
    *,
    vehicle_type: str = "COMMUTER",
    avoid_zones: Optional[List[Dict[str, Any]]] = None,
    network_congestion_score: float = 0.0,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    weather: int = 0,
    route_preference: str = "BALANCED",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    alternative_max_searches: Optional[int] = None,
    alternative_time_budget_ms: Optional[float] = None,
    alternative_max_settled_states: Optional[int] = None,
) -> Optional[Dict]:
    """Route named landmarks under the same conditions as free-form routes."""
    src, dst = LANDMARKS.get(origin_key), LANDMARKS.get(dest_key)
    if src is None or dst is None:
        return None

    origin_name = origin_name or origin_key.replace("_", " ").title()
    destination_name = destination_name or dest_key.replace("_", " ").title()
    payload = route_between_nodes(
        src, dst, avoid_zones=avoid_zones, origin_name=origin_name,
        destination_name=destination_name,
        include_alternatives=include_alternatives,
        vehicle_type=vehicle_type,
        network_congestion_score=network_congestion_score,
        default_congestion_estimate=default_congestion_estimate,
        weather=weather,
        route_preference=route_preference,
        approved_override_snapshot=approved_override_snapshot,
        alternative_max_searches=alternative_max_searches,
        alternative_time_budget_ms=alternative_time_budget_ms,
        alternative_max_settled_states=alternative_max_settled_states,
    )
    if payload is None:
        return None
    return {
        "origin": origin_key,
        "destination": dest_key,
        **payload,
    }


@lru_cache(maxsize=512)
def snap_to_graph(
    lat: float,
    lng: float,
    vehicle_type: str = "COMMUTER",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Tuple[Optional[int], float]:
    """Return the nearest reachable graph node to any lat/lng coordinate.

    Searches across all nodes in the adjacency graph (i.e. only nodes that
    are part of the largest connected component). Returns (node_id, distance_m).
    Returns (None, inf) if the graph is empty.
    """
    best_id: Optional[int] = None
    best_d = math.inf
    target = (lat, lng)
    view = _routing_view(
        vehicle_profile(vehicle_type).key,
        approved_override_snapshot,
    )
    for node_id in view.core:
        d = haversine_m(NODES[node_id], target)
        if d < best_d:
            best_d = d
            best_id = node_id
    return best_id, best_d


def _route_between_nodes_uncached(
    src_node: int,
    dst_node: int,
    avoid_zones: Optional[List[Dict[str, Any]]] = None,
    origin_name: str = "Origin",
    destination_name: str = "Destination",
    include_alternatives: bool = True,
    vehicle_type: str = "COMMUTER",
    network_congestion_score: float = 0.0,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    weather: int = 0,
    route_preference: str = "BALANCED",
    learning_snapshot: Optional[LearningSnapshot] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    reuse_primary_cache: bool = False,
    alternative_max_searches: Optional[int] = None,
    alternative_time_budget_ms: Optional[float] = None,
    alternative_max_settled_states: Optional[int] = None,
) -> Optional[Dict]:
    """Route between two raw OSM node IDs, with optional super-congested red zone avoidance.

    Unlike route_between, this is not cached because the space of possible
    node pairs is enormous.
    """
    profile = vehicle_profile(vehicle_type)
    preference = _validated_route_preference(route_preference)
    _validate_preference_vehicle(preference, profile)
    learning_snapshot = learning_snapshot or _current_learning_snapshot()
    if src_node not in ADJ or dst_node not in NODES:
        return None
    if src_node == dst_node:
        active_zone_count = sum(
            float(zone.get("congestion_index", 0.0)) > 0.0
            for zone in (avoid_zones or [])
        )
        payload = _path_payload(
            PathResult([src_node], [], 0.0, 0.0), src_node, dst_node,
            origin_name, destination_name, vehicle_type=vehicle_type,
            network_congestion_score=network_congestion_score, weather=weather,
            default_congestion_estimate=default_congestion_estimate,
            spatial_zone_count=active_zone_count,
            route_preference=route_preference,
            learning_snapshot=learning_snapshot,
        )
        return {
            **payload,
            "detour_ratio": 1.0,
            "avoided_congested_zones": [],
            "alternatives": [],
        }

    primary_paths = _primary_paths_for_route(
        src_node,
        dst_node,
        _zone_cache_json(avoid_zones),
        vehicle_type,
        network_congestion_score,
        weather,
        route_preference,
        learning_snapshot,
        default_congestion_estimate,
        approved_override_snapshot,
        reuse_primary_cache=reuse_primary_cache,
    )
    if primary_paths is None:
        return None
    baseline, result, zone_node_sets, congestion_scores = primary_paths

    # Endpoint zones still affect modeled time. They are excluded only from the
    # list of zones we claim were bypassed, because a route cannot avoid a zone
    # containing its origin or destination.
    avoidable_zones = [
        (name, nodes, score) for name, nodes, score in zone_node_sets
        if src_node not in nodes and dst_node not in nodes
    ]

    baseline_path = baseline.nodes
    avoided_names = [
        name
        for name, nodes, score in avoidable_zones
        if score >= 70.0
        if any(node in nodes for node in baseline_path)
        and not any(node in nodes for node in result.nodes)
    ]

    payload = {
        **_path_payload(
            result, src_node, dst_node, origin_name, destination_name,
            vehicle_type=vehicle_type, congestion_scores=congestion_scores,
            default_congestion_estimate=default_congestion_estimate,
            network_congestion_score=network_congestion_score, weather=weather,
            spatial_zone_count=len(zone_node_sets),
            route_preference=route_preference,
            learning_snapshot=learning_snapshot,
        ),
        "avoided_congested_zones": avoided_names,
    }
    return _with_alternatives(
        payload, result, src_node, dst_node, origin_name, destination_name,
        vehicle_type=vehicle_type, congestion_scores=congestion_scores,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        spatial_zone_count=len(zone_node_sets),
        route_preference=route_preference,
        learning_snapshot=learning_snapshot,
        approved_override_snapshot=approved_override_snapshot,
        alternative_max_searches=alternative_max_searches,
        alternative_time_budget_ms=alternative_time_budget_ms,
        alternative_max_settled_states=alternative_max_settled_states,
    ) if include_alternatives else payload


def _zone_cache_json(zones: Optional[List[Dict[str, Any]]]) -> str:
    """Serialize only routing-relevant active-zone inputs for cache reuse."""
    active = []
    for zone in zones or []:
        score = float(zone.get("congestion_index", 0.0))
        if score <= 0.0:
            continue
        active.append({
            "name": str(zone.get("name", "Congested zone")),
            "lat": round(float(zone["lat"]), 6),
            "lng": round(float(zone["lng"]), 6),
            "radius_m": round(float(zone.get("radius_m", 800.0)), 1),
            "congestion_index": round(score, 1),
            "level": str(zone.get("level", "UNKNOWN")),
        })
    active.sort(key=lambda zone: (
        zone["name"], zone["lat"], zone["lng"], zone["radius_m"],
    ))
    return json.dumps(active, sort_keys=True, separators=(",", ":"))


PrimaryPaths = Tuple[
    PathResult,
    PathResult,
    List[Tuple[str, Dict[int, float], float]],
    Dict[int, float],
]


def _compute_primary_paths(
    src_node: int,
    dst_node: int,
    zones_json: str,
    vehicle_type: str,
    network_congestion_score: float,
    weather: int,
    route_preference: str,
    learning_snapshot: LearningSnapshot,
    default_congestion_estimate: Optional[EdgeCongestionEstimate],
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot],
) -> Optional[PrimaryPaths]:
    """Compute the baseline and zone-aware primary paths exactly once."""
    zones = json.loads(zones_json)
    baseline = astar_detailed(
        src_node, dst_node, vehicle_type=vehicle_type,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        route_preference=route_preference,
        learning_snapshot=learning_snapshot,
        approved_override_snapshot=approved_override_snapshot,
    )
    if baseline is None:
        return None
    zone_node_sets = _all_zone_node_sets(zones) if zones else []
    congestion_scores = _zone_congestion_scores(zone_node_sets)
    result = astar_detailed(
        src_node, dst_node, vehicle_type=vehicle_type,
        congestion_scores=congestion_scores,
        default_congestion_estimate=default_congestion_estimate,
        network_congestion_score=network_congestion_score, weather=weather,
        route_preference=route_preference,
        learning_snapshot=learning_snapshot,
        approved_override_snapshot=approved_override_snapshot,
    ) if congestion_scores else baseline
    if result is None:
        result = baseline
    return baseline, result, zone_node_sets, congestion_scores


@lru_cache(maxsize=128)
def _primary_paths_cached(
    src_node: int,
    dst_node: int,
    zones_json: str,
    vehicle_type: str,
    network_congestion_score: float,
    weather: int,
    route_preference: str,
    learning_snapshot: LearningSnapshot,
    default_congestion_estimate: Optional[EdgeCongestionEstimate],
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot],
) -> Optional[PrimaryPaths]:
    return _compute_primary_paths(
        src_node, dst_node, zones_json, vehicle_type,
        network_congestion_score, weather, route_preference,
        learning_snapshot, default_congestion_estimate,
        approved_override_snapshot,
    )


def _primary_paths_for_route(
    src_node: int,
    dst_node: int,
    zones_json: str,
    vehicle_type: str,
    network_congestion_score: float,
    weather: int,
    route_preference: str,
    learning_snapshot: LearningSnapshot,
    default_congestion_estimate: Optional[EdgeCongestionEstimate],
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot],
    *,
    reuse_primary_cache: bool,
) -> Optional[PrimaryPaths]:
    if reuse_primary_cache:
        return _primary_paths_cached(
            src_node, dst_node, zones_json, vehicle_type,
            network_congestion_score, weather, route_preference,
            learning_snapshot, default_congestion_estimate,
            approved_override_snapshot,
        )
    return _compute_primary_paths(
        src_node, dst_node, zones_json, vehicle_type,
        network_congestion_score, weather, route_preference,
        learning_snapshot, default_congestion_estimate,
        approved_override_snapshot,
    )


@lru_cache(maxsize=128)
def _route_between_nodes_cached(
    src_node: int,
    dst_node: int,
    zones_json: str,
    origin_name: str,
    destination_name: str,
    include_alternatives: bool,
    vehicle_type: str,
    network_congestion_score: float,
    weather: int,
    route_preference: str,
    learning_snapshot: LearningSnapshot,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    alternative_max_searches: Optional[int] = None,
    alternative_time_budget_ms: Optional[float] = None,
    alternative_max_settled_states: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    zones = json.loads(zones_json)
    return _route_between_nodes_uncached(
        src_node,
        dst_node,
        avoid_zones=zones,
        origin_name=origin_name,
        destination_name=destination_name,
        include_alternatives=include_alternatives,
        vehicle_type=vehicle_type,
        network_congestion_score=network_congestion_score,
        default_congestion_estimate=default_congestion_estimate,
        weather=weather,
        route_preference=route_preference,
        learning_snapshot=learning_snapshot,
        approved_override_snapshot=approved_override_snapshot,
        reuse_primary_cache=True,
        alternative_max_searches=alternative_max_searches,
        alternative_time_budget_ms=alternative_time_budget_ms,
        alternative_max_settled_states=alternative_max_settled_states,
    )


_route_between_nodes_cache_clear = _route_between_nodes_cached.cache_clear


def _clear_route_result_caches() -> None:
    _route_between_nodes_cache_clear()
    _primary_paths_cached.cache_clear()


_route_between_nodes_cached.cache_clear = _clear_route_result_caches  # type: ignore[attr-defined]


def route_between_nodes(
    src_node: int,
    dst_node: int,
    avoid_zones: Optional[List[Dict[str, Any]]] = None,
    origin_name: str = "Origin",
    destination_name: str = "Destination",
    include_alternatives: bool = True,
    vehicle_type: str = "COMMUTER",
    network_congestion_score: float = 0.0,
    default_congestion_estimate: Optional[EdgeCongestionEstimate] = None,
    weather: int = 0,
    route_preference: str = "BALANCED",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    alternative_max_searches: Optional[int] = None,
    alternative_time_budget_ms: Optional[float] = None,
    alternative_max_settled_states: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Route arbitrary snapped endpoints with a bounded, copy-safe cache."""
    learning_snapshot = _current_learning_snapshot()
    if default_congestion_estimate is not None and not isinstance(
        default_congestion_estimate, EdgeCongestionEstimate,
    ):
        raise TypeError(
            "default_congestion_estimate must be an EdgeCongestionEstimate."
        )
    result = _route_between_nodes_cached(
        src_node,
        dst_node,
        _zone_cache_json(avoid_zones),
        origin_name,
        destination_name,
        include_alternatives,
        vehicle_profile(vehicle_type).key,
        round(max(0.0, min(100.0, float(network_congestion_score))), 1),
        weather,
        _validated_route_preference(route_preference).key,
        learning_snapshot,
        default_congestion_estimate,
        approved_override_snapshot,
        *(
            _alternative_budget(
                alternative_max_searches,
                alternative_time_budget_ms,
                alternative_max_settled_states,
            )
            if include_alternatives
            else (None, None, None)
        ),
    )
    # The solver may replace provider geometry in its local result envelope;
    # never let that mutation corrupt a later cache hit.
    return copy.deepcopy(result)
