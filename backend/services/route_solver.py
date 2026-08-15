import copy
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from models.congestion_model import (
    LEGACY_CORRIDOR_SPATIAL_DEFAULTS,
    SpatialPredictionContext,
    delay_from_score,
    forecaster,
)
from services import (
    clock, ferry_schedule, live_traffic, multimodal_router, router,
    supabase_pgrouting,
)
from services.service_contracts import ApprovedGraphOverrideSnapshot




# Corridor definitions. `origin`/`destination` are landmark keys in the OSM
# graph; distance and geometry come from A* over the real road network rather
# than from hardcoded figures (the previous literals were off by up to 6 km
# because four of the landmark coordinates were themselves wrong).
#
# `destination_port` is the ferry terminal a corridor actually feeds. Two
# corridors do not touch one, and say so rather than offering a connection
# that does not exist.
CORRIDORS: List[Dict[str, Any]] = [
    {
        "id": "corridor-1",
        "name": "Mukakuning Industrial -> Batam Centre Terminal",
        "origin": "mukakuning",
        "destination": "batam_centre",
        "destination_port": "Batam Centre",
        "base_time_mins": 18,
        "corridor_idx": 0,
        "key_checkpoints": ["Mukakuning Gate", "Simpang Kabil", "Batam Centre Ferry"],
    },
    {
        "id": "corridor-2",
        "name": "Batu Ampar Freight Port -> Batam Centre Ferry",
        "origin": "batu_ampar",
        "destination": "batam_centre",
        "destination_port": "Batam Centre",
        "base_time_mins": 16,
        "corridor_idx": 1,
        "key_checkpoints": ["Batu Ampar Gate 2", "Jalan Yos Sudarso", "Batam Centre"],
    },
    {
        "id": "corridor-3",
        "name": "Hang Nadim Airport -> Nagoya City Centre",
        "origin": "hang_nadim",
        "destination": "nagoya",
        "destination_port": None,
        "base_time_mins": 24,
        "corridor_idx": 2,
        "key_checkpoints": ["Airport Toll Access", "Simpang Jam", "Nagoya Hill"],
    },
    {
        "id": "corridor-4",
        "name": "Sekupang Ferry Terminal -> Mukakuning Industrial",
        "origin": "sekupang",
        "destination": "mukakuning",
        # Arriving direction: this corridor leaves the terminal, so an onward
        # sailing is not the relevant connection.
        "destination_port": None,
        "base_time_mins": 22,
        "corridor_idx": 3,
        "key_checkpoints": ["Sekupang Port", "Jalan Gajah Mada", "Mukakuning South"],
    },
    {
        "id": "corridor-5",
        "name": "Nongsa Digital Park -> Batam Centre Terminal",
        "origin": "nongsa",
        "destination": "batam_centre",
        "destination_port": "Batam Centre",
        "base_time_mins": 20,
        "corridor_idx": 4,
        "key_checkpoints": ["Nongsa Tech Hub", "Jalan Hang Tuah", "Batam Centre Ferry"],
    },
]

# Places exposed by the point-to-point planner.  The original UI presented the
# five corridors above as indivisible choices.  These locations turn the same
# offline OSM graph into a Maps-style from/to planner: every ordered pair is a
# valid route (13 x 12 = 156 combinations).
ROUTE_LOCATIONS: List[Dict[str, Any]] = [
    {"id": "batam_centre", "name": "Batam Centre Ferry Terminal", "category": "Ferry terminals", "lat": 1.1318, "lng": 104.0554, "ferry_port": "Batam Centre"},
    {"id": "harbour_bay", "name": "Harbour Bay Ferry Terminal", "category": "Ferry terminals", "lat": 1.15396, "lng": 103.997234, "ferry_port": "HarbourBay"},
    {"id": "sekupang", "name": "Sekupang Ferry Terminal", "category": "Ferry terminals", "lat": 1.1250, "lng": 103.9250, "ferry_port": "Sekupang"},
    {"id": "hang_nadim", "name": "Hang Nadim Airport", "category": "Transport hubs", "lat": 1.1211, "lng": 104.1147, "ferry_port": None},
    {"id": "batu_aji", "name": "Batu Aji Transit Hub", "category": "Transport hubs", "lat": 1.051, "lng": 103.965, "ferry_port": None},
    {"id": "tiban", "name": "Tiban Centre", "category": "Transport hubs", "lat": 1.099, "lng": 103.961, "ferry_port": None},
    {"id": "mukakuning", "name": "Batamindo Industrial Park", "category": "Industry & logistics", "lat": 1.0605, "lng": 104.0303, "ferry_port": None},
    {"id": "batu_ampar", "name": "Batu Ampar Freight Port", "category": "Industry & logistics", "lat": 1.1630, "lng": 104.0025, "ferry_port": None},
    {"id": "kabil_industrial", "name": "Kabil Industrial Estate", "category": "Industry & logistics", "lat": 1.094875, "lng": 104.118329, "ferry_port": None},
    {"id": "nongsa", "name": "Nongsa Digital Park", "category": "Business & shopping", "lat": 1.1822, "lng": 104.1030, "ferry_port": None},
    {"id": "nagoya", "name": "Nagoya Hill", "category": "Business & shopping", "lat": 1.1465, "lng": 104.0125, "ferry_port": None},
    {"id": "panbil_mall", "name": "Panbil Mall", "category": "Business & shopping", "lat": 1.07210, "lng": 104.02355, "ferry_port": None},
    {"id": "kepri_mall", "name": "Kepri Mall", "category": "Business & shopping", "lat": 1.101, "lng": 104.038, "ferry_port": None},
]

LOCATION_BY_ID = {location["id"]: location for location in ROUTE_LOCATIONS}

# Re-export the router's immutable catalog for existing callers. This is the
# single source of truth for API validation, path choice, ETA and emissions.
VEHICLE_PROFILES = router.VEHICLE_PROFILES
PASSENGER_FERRY_INCOMPATIBLE_VEHICLES = frozenset({
    "LIGHT_TRUCK", "CARGO_TRUCK",
})
ROUTE_PREFERENCES = router.ROUTE_PREFERENCES


# Fleet-average idle-burn factor used by the operations simulator. Individual
# route results use each selected vehicle profile's idle-emissions assumption.
IDLE_BURN_KG_PER_HOUR = 1.8

# A clicked/search result must actually be close to the committed Batam road
# extract.  Without this guard, Singapore, Jakarta, or even nonsensical points
# were silently snapped to an arbitrary edge of Batam and returned as a local
# road route.
MAX_FREE_ROUTE_SNAP_M = 1_000.0

# A free-form destination must be at the terminal itself, not merely in the
# same neighbourhood. The previous 2 km radius misclassified Nagoya Hill as
# Harbour Bay and added ferry handling time to an ordinary road trip.
FERRY_TERMINAL_MATCH_M = 500.0

# Endpoint connectors are rendered separately from graph-road geometry. Model
# them as conservative walking/access legs, never as extra road kilometres.
ACCESS_CONNECTOR_SPEED_KPH = 5.0


def _validated_profile(vehicle_type: str) -> router.VehicleProfile:
    return router.vehicle_profile(vehicle_type)


def _validated_preference(
    route_preference: str,
) -> router.RoutePreferenceProfile:
    return router.route_preference(route_preference)


def _validate_time_inputs(hour: int, weather: int) -> None:
    if not 0 <= hour <= 23:
        raise ValueError("Hour must be between 0 and 23.")
    if weather not in (0, 1, 2):
        raise ValueError("Weather must be 0 (clear), 1 (rain), or 2 (storm).")


def _forecast_ferry_minutes(
    minutes_until_departure: Optional[float],
    forecast_offset_mins: float,
) -> Optional[float]:
    """Return remaining proximity only while the referenced sailing is future.

    A sailing ten minutes away is not an imminent sailing twenty minutes after
    it departed.  The former clamp-to-zero behavior inverted that fact into
    maximum pressure for every later forecast.
    """
    if minutes_until_departure is None:
        return None
    remaining = minutes_until_departure - forecast_offset_mins
    return max(0.0, remaining) if remaining >= 0.0 else None


def _probabilistic_corridor_prediction(
    effective_at: datetime,
    *,
    weather: int,
    corridor_idx: int,
    ferry_surge: int,
    surge_source: Optional[Dict[str, Any]],
    profile: router.VehicleProfile,
) -> tuple[Dict[str, Any], router.EdgeCongestionEstimate]:
    """Adapt the spatial ensemble to the graph's provider-neutral contract.

    The estimate is a request snapshot.  It carries ratios rather than a
    route-total P90 delay because the router applies it independently to each
    selected edge.  That prevents a corridor-level delay from being counted
    once per edge while retaining the ensemble's downside uncertainty.
    """
    defaults = LEGACY_CORRIDOR_SPATIAL_DEFAULTS[corridor_idx]
    minutes_until = None
    if ferry_surge and isinstance(surge_source, dict):
        raw_minutes = surge_source.get("minutes_until_departure")
        if isinstance(raw_minutes, (int, float)) and not isinstance(
            raw_minutes, bool,
        ) and math.isfinite(float(raw_minutes)):
            minutes_until = max(0.0, float(raw_minutes))
    risk_aversion = router.vehicle_routing_policy(profile).risk_aversion

    def context(at: datetime, offset_mins: float) -> SpatialPredictionContext:
        local = at.astimezone(clock.BATAM_TZ)
        return SpatialPredictionContext(
            hour_float=(
                local.hour + local.minute / 60.0 + local.second / 3600.0
            ),
            day_of_week=local.weekday(),
            weather=weather,
            corridor_idx=corridor_idx,
            road_class=str(defaults["road_class"]),
            capacity_vph=float(defaults["capacity_vph"]),
            terminal_distance_km=float(defaults["terminal_distance_km"]),
            free_flow_speed_kph=float(defaults["free_flow_speed_kph"]),
            minutes_until_ferry_departure=_forecast_ferry_minutes(
                minutes_until,
                offset_mins,
            ),
            risk_aversion=risk_aversion,
            spatial_source="modelled_route_corridor_profile",
            defaults_applied=(
                "road_class", "capacity_vph", "terminal_distance_km",
                "free_flow_speed_kph",
            ),
        )

    current = forecaster.predict_spatial(context(effective_at, 0.0))
    after_30 = forecaster.predict_spatial(context(
        effective_at + timedelta(minutes=30), 30.0,
    ))
    after_60 = forecaster.predict_spatial(context(
        effective_at + timedelta(minutes=60), 60.0,
    ))
    prediction = {
        "current_score": round(current.mean_score, 1),
        "predicted_30min": round(after_30.mean_score, 1),
        "predicted_60min": round(after_60.mean_score, 1),
        "estimated_delay_mins": delay_from_score(current.mean_score),
        "status": current.status,
        "risk_level": current.risk_level,
        "trend": (
            "UPWARD" if after_30.mean_score > current.mean_score + 3
            else "DOWNWARD" if after_30.mean_score < current.mean_score - 3
            else "STABLE"
        ),
        "uncertainty_std": current.std_score,
        "p10_score": current.p10_score,
        "p90_score": current.p90_score,
        "upper_bound_delay_mins": current.upper_bound_delay_mins,
        "risk_adjusted_score": current.risk_adjusted_score,
        "expected_speed_ratio": current.expected_speed_ratio,
        "speed_ratio_std": current.speed_ratio_std,
        "model_id": current.model_id,
        "spatial_source": current.spatial_source,
        "defaults_applied": list(current.defaults_applied),
        "risk_aversion": risk_aversion,
        "provenance": "probabilistic_forecast",
        "observed": False,
        "training_data_source": forecaster.metrics.get("training_data_source"),
    }
    estimate = router.EdgeCongestionEstimate(**current.router_payload(
        free_flow_travel_seconds=0.0,
    ))
    return prediction, estimate


def _planning_traffic_snapshot(
    zones: List[Dict[str, Any]],
    effective_at: datetime,
    weather: int,
    *,
    applied_to_returned_route: bool,
) -> Dict[str, Any]:
    """Publish the exact spatial inputs evaluated for the returned route."""
    published_zones = copy.deepcopy(zones)
    return {
        "schema_version": 1,
        "effective_at": clock.iso(effective_at),
        "weather": weather,
        "source": "modelled_spatial_hotspots",
        "observed": False,
        "applied_to_returned_route": applied_to_returned_route,
        "zone_count": len(published_zones),
        "congestion_level_counts": {
            level: sum(zone.get("level") == level for zone in published_zones)
            for level in ("SMOOTH", "HEAVY", "SUPER_CONGESTED")
        },
        "zones": published_zones,
        "emissions_pressure_model": copy.deepcopy(
            live_traffic.EMISSIONS_PRESSURE_MODEL,
        ),
        "routing_effect": (
            "Positive zone scores increase local A* edge cost with radial "
            "decay; they are not hard road closures."
        ),
        "limitations": (
            "Modelled planning areas, not observed zone traffic or measured "
            "area emissions."
        ),
    }


def _access_connector_metrics(origin_snap_m: float, destination_snap_m: float) -> Dict[str, Any]:
    """Return separately-accounted endpoint distance and conservative time."""
    def access_minutes(distance_m: float) -> float:
        raw = max(0.0, distance_m) / 1000.0 / ACCESS_CONNECTOR_SPEED_KPH * 60.0
        return math.ceil(raw * 10.0) / 10.0

    origin_time = access_minutes(origin_snap_m)
    destination_time = access_minutes(destination_snap_m)
    return {
        "origin_snap_m": round(max(0.0, origin_snap_m), 1),
        "destination_snap_m": round(max(0.0, destination_snap_m), 1),
        "origin_access_time_mins": origin_time,
        "destination_access_time_mins": destination_time,
        "total_access_distance_km": round(
            (max(0.0, origin_snap_m) + max(0.0, destination_snap_m)) / 1000.0,
            3,
        ),
        "total_access_time_mins": round(origin_time + destination_time, 1),
        "assumed_access_speed_kph": ACCESS_CONNECTOR_SPEED_KPH,
        "included_in_road_distance": False,
    }


def _geometry_access_metrics(
    geometry: Any,
    requested_origin: tuple[float, float],
    requested_destination: tuple[float, float],
    fallback_origin_snap_m: float,
    fallback_destination_snap_m: float,
) -> Dict[str, Any]:
    if _valid_route_geometry(geometry):
        origin_snap_m = router.haversine_m(requested_origin, tuple(geometry[0]))
        destination_snap_m = router.haversine_m(
            requested_destination, tuple(geometry[-1]),
        )
    else:
        origin_snap_m = fallback_origin_snap_m
        destination_snap_m = fallback_destination_snap_m
    return _access_connector_metrics(origin_snap_m, destination_snap_m)


def _geometry(
    corridor: Dict[str, Any],
    *,
    vehicle_type: str = "COMMUTER",
    weather: int = 0,
    network_congestion_score: float = 0.0,
    default_congestion_estimate: Optional[router.EdgeCongestionEstimate] = None,
    zones: Optional[List[Dict[str, Any]]] = None,
    include_alternatives: bool = False,
    route_preference: str = "BALANCED",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Optional[Dict[str, Any]]:
    origin = LOCATION_BY_ID.get(corridor["origin"], {})
    destination = LOCATION_BY_ID.get(corridor["destination"], {})
    return router.route_between(
        corridor["origin"], corridor["destination"],
        include_alternatives=include_alternatives,
        origin_name=origin.get("name"), destination_name=destination.get("name"),
        vehicle_type=vehicle_type, weather=weather,
        network_congestion_score=network_congestion_score,
        default_congestion_estimate=default_congestion_estimate,
        avoid_zones=zones,
        route_preference=route_preference,
        approved_override_snapshot=approved_override_snapshot,
    )


def corridor_distance_km(corridor: Dict[str, Any]) -> float:
    route = _geometry(corridor)
    return route["distance_km"] if route else 0.0


def enrich(corridor: Dict[str, Any]) -> Dict[str, Any]:
    """Corridor metadata plus its real road distance."""
    route = _geometry(corridor)
    return {
        **{k: v for k, v in corridor.items() if k != "corridor_idx"},
        "distance_km": route["distance_km"] if route else 0.0,
        "straight_line_km": route["straight_line_km"] if route else 0.0,
        "detour_ratio": route["detour_ratio"] if route else None,
    }


def _navigation_turn_burden(navigation: Optional[Dict[str, Any]]) -> int:
    if not navigation:
        return 0
    decisive = {
        "TURN_LEFT", "TURN_RIGHT", "SHARP_LEFT", "SHARP_RIGHT",
        "U_TURN", "TAKE_RAMP", "ROUNDABOUT",
    }
    return sum(
        str(maneuver.get("type", "")).upper() in decisive
        for maneuver in navigation.get("maneuvers", [])
    )


def _modeled_route_time_mins(route: Optional[Dict[str, Any]]) -> float:
    if not route:
        return 0.0
    modeled = route.get("modeled_travel_time_mins")
    if isinstance(modeled, (int, float)) and modeled > 0:
        return float(modeled)
    # Defensive compatibility for a validated external provider or legacy
    # graph payload. New local OSM results always carry chosen-edge time.
    distance = float(route.get("distance_km") or 0.0)
    return max(1.0, distance / 35.0 * 60.0)


def _route_emissions_kg(
    route: Optional[Dict[str, Any]], profile: router.VehicleProfile,
) -> float:
    if not route:
        return 0.0
    distance = float(route.get("distance_km") or 0.0)
    breakdown = route.get("routing_cost_breakdown") or {}
    queue_mins = float(breakdown.get("congestion_delay_mins") or 0.0)
    return round(
        distance * profile.emissions_kg_per_km
        + queue_mins / 60.0 * profile.idle_emissions_kg_per_hour,
        2,
    )


def _prediction_for_selected_route(
    prediction: Dict[str, Any], route: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Keep forecast score/trend but publish delay from the selected edges."""
    breakdown = (route or {}).get("routing_cost_breakdown") or {}
    delay = breakdown.get("congestion_delay_mins")
    return {
        **prediction,
        "estimated_delay_mins": (
            round(float(delay), 1)
            if isinstance(delay, (int, float)) else prediction["estimated_delay_mins"]
        ),
        "delay_basis": (
            "selected_route_edges" if isinstance(delay, (int, float))
            else "provider_or_corridor_forecast"
        ),
    }


def _is_positive_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _valid_route_geometry(value: Any, *, max_points: int = 10_000) -> bool:
    return (
        isinstance(value, list)
        and 2 <= len(value) <= max_points
        and all(
            isinstance(point, (list, tuple))
            and len(point) == 2
            and all(
                isinstance(coordinate, (int, float))
                and not isinstance(coordinate, bool)
                and math.isfinite(float(coordinate))
                for coordinate in point
            )
            for point in value
        )
        and all(-90.0 <= float(point[0]) <= 90.0
                and -180.0 <= float(point[1]) <= 180.0 for point in value)
        and all(
            router.haversine_m(
                (float(first[0]), float(first[1])),
                (float(second[0]), float(second[1])),
            ) <= 5_000.0
            for first, second in zip(value, value[1:])
        )
    )


def _valid_navigation(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    maneuvers = value.get("maneuvers")
    if not isinstance(maneuvers, list) or not maneuvers:
        return False
    for maneuver in maneuvers:
        if (
            not isinstance(maneuver, dict)
            or not isinstance(maneuver.get("type"), str)
            or not maneuver["type"]
            or not isinstance(maneuver.get("instruction"), str)
            or not maneuver["instruction"]
            or not _valid_route_geometry([maneuver.get("coords"), maneuver.get("coords")])
        ):
            return False
    return str(maneuvers[-1]["type"]).upper() == "ARRIVE"


def _validated_provider_route(
    candidate: Any,
    *,
    requested_origin: tuple[float, float],
    requested_destination: tuple[float, float],
    vehicle_type: str,
) -> Optional[Dict[str, Any]]:
    """Validate one external provider envelope before it replaces local OSM."""
    proof = candidate.get("constraint_provenance") if isinstance(candidate, dict) else None
    if (
        not isinstance(candidate, dict)
        or not _valid_route_geometry(candidate.get("geometry"))
        or not _is_positive_finite(candidate.get("distance_km"))
        or not _valid_navigation(candidate.get("navigation"))
        or not isinstance(candidate.get("alternatives"), list)
        or not isinstance(proof, dict)
        or proof.get("vehicle_type") != vehicle_type
        or proof.get("access_constraints_honored") is not True
        or proof.get("clearance_constraints_honored") is not True
    ):
        return None
    def geometry_contract(geometry: Any, distance_km: Any) -> bool:
        if not _valid_route_geometry(geometry) or not _is_positive_finite(distance_km):
            return False
        if any(
            not 0.88 <= float(point[0]) < 1.215
            or not 103.75 <= float(point[1]) <= 104.30
            for point in geometry
        ):
            return False
        endpoints_match = (
            min(
                router.haversine_m(
                    requested_origin,
                    (float(geometry[0][0]), float(geometry[0][1])),
                ),
                router.haversine_m(
                    requested_origin,
                    (float(geometry[-1][0]), float(geometry[-1][1])),
                ),
            ) <= MAX_FREE_ROUTE_SNAP_M
            and min(
                router.haversine_m(
                    requested_destination,
                    (float(geometry[0][0]), float(geometry[0][1])),
                ),
                router.haversine_m(
                    requested_destination,
                    (float(geometry[-1][0]), float(geometry[-1][1])),
                ),
            ) <= MAX_FREE_ROUTE_SNAP_M
        )
        if not endpoints_match:
            return False
        geometry_km = sum(
            router.haversine_m(
                (float(first[0]), float(first[1])),
                (float(second[0]), float(second[1])),
            )
            for first, second in zip(geometry, geometry[1:])
        ) / 1_000.0
        return 0.8 <= float(distance_km) / max(geometry_km, 0.001) <= 2.0

    geometry = candidate["geometry"]
    if not geometry_contract(geometry, candidate["distance_km"]):
        return None
    valid_alternatives: List[Dict[str, Any]] = []
    for alternative in candidate["alternatives"]:
        if not isinstance(alternative, dict):
            continue
        geometry = alternative.get("route_geometry") or alternative.get("geometry")
        if (
            not geometry_contract(geometry, alternative.get("distance_km"))
            or not _valid_navigation(alternative.get("navigation"))
        ):
            continue
        # Provider geometry cannot carry local OSM learning, preference, or
        # residential-road audit claims. Copy only the navigation contract.
        sanitized: Dict[str, Any] = {
            "geometry": geometry,
            "distance_km": float(alternative["distance_km"]),
            "navigation": alternative["navigation"],
        }
        for key in ("id", "name", "description", "data_source"):
            if isinstance(alternative.get(key), str):
                sanitized[key] = alternative[key]
        if _is_positive_finite(alternative.get("duration_mins")):
            sanitized["duration_mins"] = float(alternative["duration_mins"])
        valid_alternatives.append(sanitized)

    validated = {
        "geometry": candidate["geometry"],
        "distance_km": float(candidate["distance_km"]),
        "navigation": candidate["navigation"],
        "alternatives": valid_alternatives,
        "constraint_provenance": {
            "vehicle_type": vehicle_type,
            "access_constraints_honored": True,
            "clearance_constraints_honored": True,
            "source": str(proof.get("source") or "provider_declared")[:128],
        },
    }
    if _is_positive_finite(candidate.get("duration_mins")):
        validated["duration_mins"] = float(candidate["duration_mins"])
    return validated


def _alternative_route_options(
    route: Optional[Dict[str, Any]],
    profile: router.VehicleProfile,
    prediction: Dict[str, Any],
    customs_buffer: float,
    primary_base_mins: float,
    primary_emissions_kg: float,
    recommended_departure: datetime,
    destination_port: Optional[str],
    now: datetime,
    requested_origin: tuple[float, float],
    requested_destination: tuple[float, float],
    fallback_origin_snap_m: float,
    fallback_destination_snap_m: float,
    schedule_verified_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert real path alternatives into complete, independently usable options."""
    if not route:
        return []
    primary_distance = float(route.get("distance_km") or 0.0)
    primary_turns = _navigation_turn_burden(route.get("navigation"))
    options: List[Dict[str, Any]] = []
    for index, alternative in enumerate(route.get("alternatives", []), start=1):
        geometry = alternative.get("route_geometry") or alternative.get("geometry") or []
        navigation = alternative.get("navigation")
        raw_distance = alternative.get("distance_km")
        if (
            not _valid_route_geometry(geometry)
            or not _is_positive_finite(raw_distance)
            or not _valid_navigation(navigation)
        ):
            continue
        distance_km = float(raw_distance)
        access = _geometry_access_metrics(
            geometry,
            requested_origin,
            requested_destination,
            fallback_origin_snap_m,
            fallback_destination_snap_m,
        )
        provider_duration = alternative.get("duration_mins")
        modeled_duration = alternative.get("modeled_travel_time_mins")
        if isinstance(provider_duration, (int, float)) and provider_duration > 0:
            base_mins = float(provider_duration)
            maneuver_delay = 0.0
        elif isinstance(modeled_duration, (int, float)) and modeled_duration > 0:
            base_mins = float(modeled_duration)
            maneuver_delay = float(
                (alternative.get("routing_cost_breakdown") or {}).get(
                    "maneuver_delay_mins", 0.0,
                )
            )
        else:
            ratio = distance_km / primary_distance if primary_distance > 0 else 1.0
            alternative_turns = _navigation_turn_burden(navigation)
            # A distance-only ETA made residential zigzags look equivalent to
            # through-road routes. Account for the extra deceleration/decision
            # burden without pretending this is provider traffic telemetry.
            maneuver_delay = max(0.0, (alternative_turns - primary_turns) * 0.25)
            base_mins = max(1.0, primary_base_mins * ratio + maneuver_delay)
        # Local route time already blends requested-hour congestion and
        # weather at edge level; adding prediction delay here would count the
        # same exposure twice.
        estimated = round(base_mins, 1)
        total_eta = round(
            estimated + customs_buffer + access["total_access_time_mins"], 1,
        )
        congestion_delay = float(
            (alternative.get("routing_cost_breakdown") or {}).get(
                "congestion_delay_mins", 0.0,
            )
        )
        emissions = round(
            distance_km * profile.emissions_kg_per_km
            + congestion_delay / 60.0 * profile.idle_emissions_kg_per_hour,
            2,
        )
        arrival = recommended_departure + timedelta(minutes=total_eta)
        option = {
            "id": alternative.get("id") or f"osm-alternative-{index}",
            "name": alternative.get("name") or f"Alternative road route {index}",
            "description": alternative.get("description") or "A distinct drivable road path.",
            "route_geometry": geometry,
            "distance_km": round(distance_km, 2),
            "estimated_travel_time_mins": estimated,
            "total_eta_mins": total_eta,
            "co2_emissions_kg": emissions,
            "co2_saved_kg": round(max(0.0, primary_emissions_kg - emissions), 2),
            "navigation": navigation,
            "route_data_source": alternative.get("route_data_source") or alternative.get("data_source") or route.get("data_source", "openstreetmap"),
            "avoided_congested_zones": alternative.get("avoided_congested_zones", []),
            "overlap_ratio": alternative.get("overlap_ratio"),
            "maneuver_delay_mins": round(maneuver_delay, 1),
            "generalized_cost_mins": alternative.get("generalized_cost_mins"),
            "objective_cost_s": alternative.get("objective_cost_s"),
            "route_preference": alternative.get(
                "route_preference", route.get("route_preference", "BALANCED"),
            ),
            "route_preference_profile": alternative.get(
                "route_preference_profile",
                route.get("route_preference_profile"),
            ),
            "routing_cost_breakdown": alternative.get("routing_cost_breakdown"),
            "routing_model": alternative.get("routing_model") or route.get("routing_model"),
            "vehicle_profile": alternative.get("vehicle_profile") or router.vehicle_profile_payload(profile),
            "access_distance_km": access["total_access_distance_km"],
            "access_time_mins": access["total_access_time_mins"],
            "snap_info": access,
            "next_matching_ferries": ferry_schedule.next_sailings_after(
                arrival,
                limit=3,
                port=destination_port or "Batam Centre",
                now=now,
                schedule_verified_at=schedule_verified_at,
            ),
        }
        if alternative.get("shortcuts_used"):
            option["shortcuts_used"] = alternative["shortcuts_used"]
        if alternative.get("approved_graph_overrides_used"):
            option["approved_graph_overrides_used"] = (
                alternative["approved_graph_overrides_used"]
            )
        if alternative.get("local_road_audit") is not None:
            option.update({
                "local_road_distance_km": alternative.get(
                    "local_road_distance_km", 0.0,
                ),
                "local_road_segments": alternative.get(
                    "local_road_segments", [],
                ),
                "local_road_audit": alternative["local_road_audit"],
            })
        options.append(option)
    return options


def corridor_for_locations(
    origin_id: str,
    destination_id: str,
    route_preference: str = "BALANCED",
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Dict[str, Any]:
    """Build a solver corridor for any two named locations."""
    if origin_id == destination_id:
        raise ValueError("Origin and destination must be different locations.")

    origin = LOCATION_BY_ID.get(origin_id)
    destination = LOCATION_BY_ID.get(destination_id)
    if not origin or not destination:
        raise ValueError("Unknown route location.")

    # Preserve the original corridor identity and calibrated base time when an
    # existing monitored direction is selected from the new from/to controls.
    seeded = next((
        corridor for corridor in CORRIDORS
        if corridor["origin"] == origin_id and corridor["destination"] == destination_id
    ), None)
    if seeded:
        return seeded

    route = router.route_between(
        origin_id, destination_id, route_preference=route_preference,
        approved_override_snapshot=approved_override_snapshot,
    )
    if not route:
        raise ValueError("No drivable route connects those locations.")

    # The ML model has five monitored corridor profiles.  Reuse the profile
    # associated with either endpoint, then fall back deterministically.  This
    # keeps predictions stable while making clear they remain simulated.
    related = next((c for c in CORRIDORS if c["origin"] == origin_id), None)
    related = related or next((c for c in CORRIDORS if c["destination"] == destination_id), None)
    corridor_idx = related["corridor_idx"] if related else (
        sum(ord(ch) for ch in f"{origin_id}:{destination_id}") % len(CORRIDORS)
    )

    # Class-speed free-flow estimate from the chosen OSM edges. The optimizer
    # will reroute with the selected vehicle/hour/weather conditions.
    base_time = round(max(1.0, _modeled_route_time_mins(route)), 1)
    return {
        "id": f"route:{origin_id}:{destination_id}",
        "name": f"{origin['name']} -> {destination['name']}",
        "origin": origin_id,
        "destination": destination_id,
        "destination_port": destination["ferry_port"],
        "base_time_mins": base_time,
        "corridor_idx": corridor_idx,
        "key_checkpoints": [origin["name"], destination["name"]],
    }


def _normalise_schedule_datetime(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset.")
    return value.astimezone(clock.BATAM_TZ)


def _validate_schedule_request(
    departure_at: Optional[datetime],
    arrive_by: Optional[datetime],
    now: datetime,
) -> tuple[Optional[datetime], Optional[datetime]]:
    if departure_at is not None and arrive_by is not None:
        raise ValueError("Provide either departure_at or arrive_by, not both.")
    departure_at = _normalise_schedule_datetime(departure_at, "departure_at")
    arrive_by = _normalise_schedule_datetime(arrive_by, "arrive_by")
    if arrive_by is not None and arrive_by < now.astimezone(clock.BATAM_TZ):
        raise ValueError("arrive_by must be in the future.")
    if departure_at is not None and departure_at < now.astimezone(clock.BATAM_TZ):
        raise ValueError("departure_at must be in the future.")
    return departure_at, arrive_by


def _schedule_metadata(
    mode: str,
    departure_at: Optional[datetime],
    arrive_by: Optional[datetime],
    planned_departure: datetime,
    estimated_arrival: datetime,
) -> Dict[str, Any]:
    slack = (
        round((arrive_by - estimated_arrival).total_seconds() / 60.0, 1)
        if arrive_by is not None else None
    )
    return {
        "mode": mode,
        "requested_departure_at": clock.iso(departure_at) if departure_at else None,
        "requested_arrive_by": clock.iso(arrive_by) if arrive_by else None,
        "deadline_slack_mins": slack,
    }


def _latest_feasible_departure(
    solve: Any,
    now: datetime,
    deadline: datetime,
    *,
    max_search_hours: int = 48,
    iterations: int = 8,
) -> Dict[str, Any]:
    """Bounded monotonic search for the latest departure meeting a deadline."""
    local_now = now.astimezone(clock.BATAM_TZ)
    deadline = deadline.astimezone(clock.BATAM_TZ)
    low = max(local_now, deadline - timedelta(hours=max_search_hours))
    high = deadline
    best: Optional[Dict[str, Any]] = None
    first = solve(low)
    first_arrival = datetime.fromisoformat(first["estimated_arrival"])
    if first_arrival > deadline:
        raise ValueError(
            "No feasible departure was found within the 48-hour arrive-by search window."
        )
    best = first
    for _ in range(iterations):
        candidate = low + (high - low) / 2
        result = solve(candidate)
        arrival = datetime.fromisoformat(result["estimated_arrival"])
        if arrival <= deadline:
            best = result
            low = candidate
        else:
            high = candidate
    return best


def resolve_planned_departure(hour: int, now: Optional[datetime] = None) -> datetime:
    """Turn the UI's hour slider into a concrete future departure."""
    now = now or clock.now()
    local = now.astimezone(clock.BATAM_TZ)
    planned = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if planned < local:
        planned += timedelta(days=1)
    return planned


def optimize_route(corridor_id: Optional[str], vehicle_type: str, hour: int = 14,
                   weather: int = 0, now: Optional[datetime] = None,
                   origin_id: Optional[str] = None,
                   destination_id: Optional[str] = None,
                   route_preference: str = "BALANCED",
                   schedule_verified_at: Optional[str] = None,
                   approved_override_snapshot: Optional[
                       ApprovedGraphOverrideSnapshot
                   ] = None,
                   departure_at: Optional[datetime] = None,
                   arrive_by: Optional[datetime] = None,
                   _schedule_search: bool = False) -> Dict[str, Any]:
    """Compute travel time, emissions and the best departure window."""
    now = now or clock.now()
    _validate_time_inputs(hour, weather)
    departure_at, arrive_by = _validate_schedule_request(
        departure_at, arrive_by, now,
    )
    if arrive_by is not None and not _schedule_search:
        def solve(candidate: datetime) -> Dict[str, Any]:
            return optimize_route(
                corridor_id=corridor_id,
                vehicle_type=vehicle_type,
                hour=candidate.hour,
                weather=weather,
                now=now,
                origin_id=origin_id,
                destination_id=destination_id,
                route_preference=route_preference,
                schedule_verified_at=schedule_verified_at,
                approved_override_snapshot=approved_override_snapshot,
                departure_at=candidate,
                _schedule_search=True,
            )
        result = _latest_feasible_departure(solve, now, arrive_by)
        planned = datetime.fromisoformat(result["planned_departure"])
        estimated = datetime.fromisoformat(result["estimated_arrival"])
        result["scheduling"] = _schedule_metadata(
            "ARRIVE_BY", None, arrive_by, planned, estimated,
        )
        return result
    preference = _validated_preference(route_preference)
    if origin_id is not None or destination_id is not None:
        if not origin_id or not destination_id:
            raise ValueError("Both origin_id and destination_id are required.")
        corridor = corridor_for_locations(
            origin_id, destination_id, route_preference=preference.key,
            approved_override_snapshot=approved_override_snapshot,
        )
    else:
        corridor = next((c for c in CORRIDORS if c["id"] == corridor_id), None)
        if corridor is None:
            raise ValueError(f"Unknown corridor: {corridor_id}.")
    profile = _validated_profile(vehicle_type)

    planned_departure = departure_at or resolve_planned_departure(hour, now)
    hour = planned_departure.hour

    surge, surge_source = ferry_schedule.ferry_surge_for_port(
        corridor["destination_port"],
        planned_departure,
        schedule_verified_at=schedule_verified_at,
    )

    prediction, congestion_estimate = _probabilistic_corridor_prediction(
        planned_departure,
        weather=weather,
        corridor_idx=corridor["corridor_idx"],
        ferry_surge=surge,
        surge_source=surge_source,
        profile=profile,
    )

    zones = live_traffic.get_congestion_zones(planned_departure, weather=weather)
    route_now = _geometry(
        corridor, vehicle_type=vehicle_type, weather=weather,
        network_congestion_score=prediction["current_score"], zones=zones,
        default_congestion_estimate=congestion_estimate,
        include_alternatives=False,
        route_preference=preference.key,
        approved_override_snapshot=approved_override_snapshot,
    )
    if not route_now:
        raise ValueError("No drivable route connects those locations.")
    travel_now = round(_modeled_route_time_mins(route_now), 1)
    customs_buffer = (
        profile.customs_buffer_mins if corridor["destination_port"] else 0.0
    )

    # Compare against departing half an hour later.
    later = planned_departure + timedelta(minutes=30)
    surge_later, surge_source_later = ferry_schedule.ferry_surge_for_port(
        corridor["destination_port"],
        later,
        schedule_verified_at=schedule_verified_at,
    )
    prediction_later, congestion_estimate_later = (
        _probabilistic_corridor_prediction(
            later,
            weather=weather,
            corridor_idx=corridor["corridor_idx"],
            ferry_surge=surge_later,
            surge_source=surge_source_later,
            profile=profile,
        )
    )
    zones_later = live_traffic.get_congestion_zones(later, weather=weather)
    route_later = _geometry(
        corridor, vehicle_type=vehicle_type, weather=weather,
        network_congestion_score=prediction_later["current_score"],
        default_congestion_estimate=congestion_estimate_later,
        zones=zones_later, include_alternatives=False,
        route_preference=preference.key,
        approved_override_snapshot=approved_override_snapshot,
    )
    if not route_later:
        route_later = route_now
    travel_later = round(_modeled_route_time_mins(route_later), 1)

    defer = (
        prediction_later["current_score"] < prediction["current_score"] - 10
        and travel_later < travel_now
    )
    # A full timestamp is an explicit user instruction; the legacy hour mode
    # may still recommend its 30-minute optimization window.
    if departure_at is not None:
        defer = False
    saved_mins = max(0.0, round(travel_now - travel_later, 1))
    recommended_departure = later if defer else planned_departure
    recommended_prediction = prediction_later if defer else prediction
    recommended_congestion_estimate = (
        congestion_estimate_later if defer else congestion_estimate
    )
    recommended_travel = travel_later if defer else travel_now
    recommended_surge = surge_later if defer else surge
    recommended_surge_source = surge_source_later if defer else surge_source
    recommended_zones = zones_later if defer else zones
    route = _geometry(
        corridor, vehicle_type=vehicle_type, weather=weather,
        network_congestion_score=recommended_prediction["current_score"],
        default_congestion_estimate=recommended_congestion_estimate,
        zones=recommended_zones, include_alternatives=True,
        route_preference=preference.key,
        approved_override_snapshot=approved_override_snapshot,
    )
    if not route:
        route = route_later if defer else route_now
    recommended_travel = round(_modeled_route_time_mins(route), 1)
    distance_km = float(route["distance_km"])
    co2_emissions = _route_emissions_kg(route, profile)
    co2_now = _route_emissions_kg(route_now, profile)
    co2_later = _route_emissions_kg(route_later, profile)
    co2_saved = round(max(0.0, co2_now - co2_later), 2) if defer else 0.0

    if defer:
        eased = prediction["current_score"] - prediction_later["current_score"]
        reason = (
            f"Congestion eases {eased:.0f} points over the next 30 minutes; "
            "the vehicle-weighted road ETA also improves."
        )
    else:
        reason = "Current corridor traffic is within optimal flow parameters."

    origin_loc = LOCATION_BY_ID.get(corridor["origin"], {})
    dest_loc = LOCATION_BY_ID.get(corridor["destination"], {})
    origin_lat = origin_loc.get("lat", 1.12)
    origin_lng = origin_loc.get("lng", 104.02)
    dest_lat = dest_loc.get("lat", 1.12)
    dest_lng = dest_loc.get("lng", 104.02)
    origin_node = router.LANDMARKS.get(corridor["origin"])
    destination_node = router.LANDMARKS.get(corridor["destination"])
    origin_snap_m = (
        router.haversine_m((origin_lat, origin_lng), router.NODES[origin_node])
        if origin_node in router.NODES else 0.0
    )
    destination_snap_m = (
        router.haversine_m((dest_lat, dest_lng), router.NODES[destination_node])
        if destination_node in router.NODES else 0.0
    )
    access = _geometry_access_metrics(
        route.get("geometry", []) if route else [],
        (origin_lat, origin_lng),
        (dest_lat, dest_lng),
        origin_snap_m,
        destination_snap_m,
    )
    total_eta = round(
        recommended_travel + customs_buffer + access["total_access_time_mins"], 1,
    )
    alternative_routes = _alternative_route_options(
        route,
        profile,
        recommended_prediction,
        customs_buffer,
        recommended_travel,
        co2_emissions,
        recommended_departure,
        corridor["destination_port"],
        now,
        (origin_lat, origin_lng),
        (dest_lat, dest_lng),
        origin_snap_m,
        destination_snap_m,
        schedule_verified_at,
    )

    result: Dict[str, Any] = {
        "route_type": "ROAD_ROUTE",
        "corridor": {
            **{k: v for k, v in corridor.items() if k != "corridor_idx"},
            "distance_km": distance_km,
            "straight_line_km": route.get("straight_line_km", 0.0),
            "detour_ratio": route.get("detour_ratio"),
        },
        "requested_origin": {
            "name": origin_loc.get("name", "Origin"),
            "lat": origin_lat,
            "lng": origin_lng,
        },
        "requested_destination": {
            "name": dest_loc.get("name", "Destination"),
            "lat": dest_lat,
            "lng": dest_lng,
        },
        "vehicle_type": vehicle_type,
        "vehicle_profile": route.get("vehicle_profile") or router.vehicle_profile_payload(profile),
        "route_preference": route.get("route_preference", preference.key),
        "route_preference_profile": route.get("route_preference_profile") or (
            router.route_preference_payload(preference)
        ),
        "planned_departure": clock.iso(recommended_departure),
        "planning_traffic_snapshot": _planning_traffic_snapshot(
            recommended_zones,
            recommended_departure,
            weather,
            applied_to_returned_route=True,
        ),
        "congestion_prediction": _prediction_for_selected_route(
            recommended_prediction, route,
        ),
        "estimated_travel_time_mins": recommended_travel,
        "customs_buffer_mins": customs_buffer,
        "access_distance_km": access["total_access_distance_km"],
        "access_time_mins": access["total_access_time_mins"],
        "total_eta_mins": total_eta,
        "co2_emissions_kg": co2_emissions,
        "co2_saved_kg": co2_saved,
        "ferry_surge": recommended_surge,
        "surge_source": recommended_surge_source,
        "optimal_departure": {
            "recommended": "DEFER_30_MINS" if defer else "DEPART_NOW",
            "time_saved_mins": saved_mins if defer else 0.0,
            "reason": reason,
        },
        "route_geometry": route["geometry"] if route else [],
        "route_data_source": route.get("data_source", "openstreetmap") if route else "openstreetmap",
        "navigation": route.get("navigation") if route else None,
        "generalized_cost_mins": route.get("generalized_cost_mins"),
        "objective_cost_s": route.get("objective_cost_s"),
        "routing_cost_breakdown": route.get("routing_cost_breakdown"),
        "routing_model": route.get("routing_model"),
        "snap_info": access,
        "avoided_congested_zones": route.get("avoided_congested_zones", []) if route else [],
        "alternative_routes": alternative_routes,
        "alternatives_note": (
            None if len(alternative_routes) >= 2
            else "Fewer than two sufficiently distinct routes satisfy the detour limit."
        ),
    }
    if route.get("shortcuts_used"):
        result["shortcuts_used"] = route["shortcuts_used"]
    if route.get("approved_graph_overrides_used"):
        result["approved_graph_overrides_used"] = (
            route["approved_graph_overrides_used"]
        )
    if route.get("local_road_audit") is not None:
        result.update({
            "local_road_distance_km": route.get("local_road_distance_km", 0.0),
            "local_road_segments": route.get("local_road_segments", []),
            "local_road_audit": route["local_road_audit"],
        })

    arrival = recommended_departure + timedelta(minutes=total_eta)
    port = corridor["destination_port"] or "Batam Centre"
    result["next_matching_ferries"] = ferry_schedule.next_sailings_after(
        arrival,
        limit=3,
        port=port,
        now=now,
        schedule_verified_at=schedule_verified_at,
    )
    if not corridor["destination_port"]:
        result["ferry_connection_note"] = (
            "This corridor does not terminate at a ferry port; showing the "
            "nearest Batam Centre departures for reference."
        )

    result["estimated_arrival"] = clock.iso(arrival)
    result["scheduling"] = _schedule_metadata(
        "DEPART_AT" if departure_at is not None else "HOUR",
        departure_at,
        None,
        recommended_departure,
        arrival,
    )

    return result



def optimize_free_route(
    origin_lat: float, origin_lng: float,
    destination_lat: float, destination_lng: float,
    vehicle_type: str,
    hour: int = 14,
    weather: int = 0,
    now: Optional[datetime] = None,
    origin_name: Optional[str] = None,
    destination_name: Optional[str] = None,
    route_preference: str = "BALANCED",
    schedule_verified_at: Optional[str] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    leg_mode: bool = False,
    departure_at: Optional[datetime] = None,
    arrive_by: Optional[datetime] = None,
    _schedule_search: bool = False,
) -> Dict[str, Any]:
    """Optimise a local or Singapore-Batam route from free coordinates.

    Batam-only journeys retain the committed OSM A* solver below. Journeys
    involving Singapore are delegated to the multimodal composer, which joins
    road-access legs with a published ferry terminal corridor.

    `origin_name` / `destination_name` are display strings from the geocoder;
    they are optional and fall back to formatted coordinates.

    `leg_mode` solves one leg of a longer multi-stop journey. A leg cannot
    choose its own departure — that belongs to the journey — so the +30 minute
    deferral comparison and the alternative-route set are skipped, cutting the
    A* work per leg from three searches to one. Every other field keeps the
    same meaning as a standalone route.
    """
    now = now or clock.now()
    _validate_time_inputs(hour, weather)
    departure_at, arrive_by = _validate_schedule_request(
        departure_at, arrive_by, now,
    )
    if arrive_by is not None and not _schedule_search:
        def solve(candidate: datetime) -> Dict[str, Any]:
            return optimize_free_route(
                origin_lat=origin_lat, origin_lng=origin_lng,
                destination_lat=destination_lat, destination_lng=destination_lng,
                vehicle_type=vehicle_type, hour=candidate.hour, weather=weather,
                now=now, origin_name=origin_name, destination_name=destination_name,
                route_preference=route_preference,
                schedule_verified_at=schedule_verified_at,
                approved_override_snapshot=approved_override_snapshot,
                leg_mode=leg_mode, departure_at=candidate,
                _schedule_search=True,
            )
        result = _latest_feasible_departure(solve, now, arrive_by)
        planned = datetime.fromisoformat(result["planned_departure"])
        estimated = datetime.fromisoformat(result["estimated_arrival"])
        result["scheduling"] = _schedule_metadata(
            "ARRIVE_BY", None, arrive_by, planned, estimated,
        )
        return result
    profile = _validated_profile(vehicle_type)
    preference = _validated_preference(route_preference)

    origin_region = multimodal_router.location_region(origin_lat, origin_lng)
    destination_region = multimodal_router.location_region(
        destination_lat, destination_lng,
    )
    if origin_region is None or destination_region is None:
        raise ValueError("Both points must be within Singapore or Batam.")
    if (
        origin_region != destination_region
        and profile.key in PASSENGER_FERRY_INCOMPATIBLE_VEHICLES
    ):
        raise ValueError(
            "Light and heavy trucks cannot be scheduled on the published "
            "passenger-ferry services used by this planner. Local road routing "
            "remains available; cross-border freight requires a cargo port, "
            "roll-on/roll-off operator, or an authorised logistics-partner feed."
        )
    if "SINGAPORE" in {origin_region, destination_region}:
        return multimodal_router.optimize_journey(
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            destination_lat=destination_lat,
            destination_lng=destination_lng,
            vehicle_type=profile.key,
            hour=hour,
            weather=weather,
            now=now,
            origin_name=origin_name,
            destination_name=destination_name,
            route_preference=preference.key,
            schedule_verified_at=schedule_verified_at,
            approved_override_snapshot=approved_override_snapshot,
            departure_at=departure_at,
        )

    src_node, src_snap_m = router.snap_to_graph(
        origin_lat, origin_lng, profile.key, approved_override_snapshot,
    )
    dst_node, dst_snap_m = router.snap_to_graph(
        destination_lat, destination_lng, profile.key, approved_override_snapshot,
    )

    if src_node is None or dst_node is None:
        raise ValueError("Could not snap coordinates to the road network.")
    if src_snap_m > MAX_FREE_ROUTE_SNAP_M:
        raise ValueError(
            f"Origin is outside the supported Batam road network "
            f"({src_snap_m / 1000.0:.1f} km from the nearest mapped road)."
        )
    if dst_snap_m > MAX_FREE_ROUTE_SNAP_M:
        raise ValueError(
            f"Destination is outside the supported Batam road network "
            f"({dst_snap_m / 1000.0:.1f} km from the nearest mapped road)."
        )
    if src_node == dst_node:
        raise ValueError("Origin and destination are on the same graph node; choose points further apart.")

    origin_display = origin_name or f"{origin_lat:.5f}, {origin_lng:.5f}"
    dest_display = destination_name or f"{destination_lat:.5f}, {destination_lng:.5f}"
    # Inherit the corridor profile from the nearest monitored corridor.
    related = next(
        (c for c in CORRIDORS if c["origin"] in LOCATION_BY_ID and
         router.haversine_m(
             (LOCATION_BY_ID[c["origin"]]["lat"], LOCATION_BY_ID[c["origin"]]["lng"]),
             (origin_lat, origin_lng),
         ) < 5000),
        None,
    )
    corridor_idx = related["corridor_idx"] if related else (
        int(abs(origin_lat * 1000 + origin_lng * 100)) % len(CORRIDORS)
    )

    # Find the nearest ferry port destination to snap ferry connections.
    ferry_terminals = [loc for loc in ROUTE_LOCATIONS if loc.get("ferry_port")]
    nearest_terminal = min(
        ferry_terminals,
        key=lambda loc: router.haversine_m((loc["lat"], loc["lng"]), (destination_lat, destination_lng)),
        default=None,
    )

    dest_ferry_port = (
        nearest_terminal["ferry_port"]
        if nearest_terminal and router.haversine_m(
            (nearest_terminal["lat"], nearest_terminal["lng"]),
            (destination_lat, destination_lng),
        ) < FERRY_TERMINAL_MATCH_M
        else None
    )

    planned_departure = departure_at or resolve_planned_departure(hour, now)
    hour = planned_departure.hour

    surge, surge_source = ferry_schedule.ferry_surge_for_port(
        dest_ferry_port,
        planned_departure,
        schedule_verified_at=schedule_verified_at,
    )

    prediction, congestion_estimate = _probabilistic_corridor_prediction(
        planned_departure,
        weather=weather,
        corridor_idx=corridor_idx,
        ferry_surge=surge,
        surge_source=surge_source,
        profile=profile,
    )

    zones = live_traffic.get_congestion_zones(planned_departure, weather=weather)
    route_now = router.route_between_nodes(
        src_node, dst_node, avoid_zones=zones,
        origin_name=origin_display, destination_name=dest_display,
        include_alternatives=False, vehicle_type=vehicle_type,
        network_congestion_score=prediction["current_score"], weather=weather,
        default_congestion_estimate=congestion_estimate,
        route_preference=preference.key,
        approved_override_snapshot=approved_override_snapshot,
    )
    if not route_now:
        raise ValueError("No drivable path connects those coordinates.")
    travel_now = round(_modeled_route_time_mins(route_now), 1)

    if leg_mode:
        # The journey owns the departure decision, so a leg reuses its own
        # conditions rather than searching a +30 minute alternative it has no
        # authority to act on.
        later = planned_departure
        surge_later, surge_source_later = surge, surge_source
        prediction_later, congestion_estimate_later = (
            prediction, congestion_estimate,
        )
        zones_later = zones
        route_later = route_now
    else:
        later = planned_departure + timedelta(minutes=30)
        surge_later, surge_source_later = ferry_schedule.ferry_surge_for_port(
            dest_ferry_port,
            later,
            schedule_verified_at=schedule_verified_at,
        )
        prediction_later, congestion_estimate_later = (
            _probabilistic_corridor_prediction(
                later,
                weather=weather,
                corridor_idx=corridor_idx,
                ferry_surge=surge_later,
                surge_source=surge_source_later,
                profile=profile,
            )
        )
        zones_later = live_traffic.get_congestion_zones(later, weather=weather)
        route_later = router.route_between_nodes(
            src_node, dst_node, avoid_zones=zones_later,
            origin_name=origin_display, destination_name=dest_display,
            include_alternatives=False, vehicle_type=vehicle_type,
            network_congestion_score=prediction_later["current_score"],
            weather=weather,
            default_congestion_estimate=congestion_estimate_later,
            route_preference=preference.key,
            approved_override_snapshot=approved_override_snapshot,
        ) or route_now
    travel_later = round(_modeled_route_time_mins(route_later), 1)

    defer = (
        prediction_later["current_score"] < prediction["current_score"] - 10
        and travel_later < travel_now
    )
    if departure_at is not None:
        defer = False
    saved_mins = max(0.0, round(travel_now - travel_later, 1))
    recommended_departure = later if defer else planned_departure
    recommended_prediction = prediction_later if defer else prediction
    recommended_congestion_estimate = (
        congestion_estimate_later if defer else congestion_estimate
    )
    recommended_travel = travel_later if defer else travel_now
    recommended_surge = surge_later if defer else surge
    recommended_surge_source = surge_source_later if defer else surge_source
    recommended_zones = zones_later if defer else zones
    if leg_mode:
        # Without a deferral or an alternative set to choose from, the
        # recommended route is by construction the one already solved above.
        route = route_now
    else:
        route = router.route_between_nodes(
            src_node, dst_node, avoid_zones=recommended_zones,
            origin_name=origin_display, destination_name=dest_display,
            include_alternatives=True, vehicle_type=vehicle_type,
            network_congestion_score=recommended_prediction["current_score"],
            default_congestion_estimate=recommended_congestion_estimate,
            weather=weather,
            route_preference=preference.key,
            approved_override_snapshot=approved_override_snapshot,
        ) or (route_later if defer else route_now)
    recommended_travel = round(_modeled_route_time_mins(route), 1)

    # Local OSM is the guaranteed critical path. A complete external provider
    # result may replace it only when explicitly configured.
    route_source_name = "openstreetmap"
    provider_route: Optional[Dict[str, Any]] = None
    provider_preference = os.environ.get(
        "CROSSFLOW_ROUTE_PROVIDER", "local",
    ).strip().lower()
    # The legacy RPC returns geometry/time only. It cannot prove the selected
    # vehicle's access/clearance or the required navigation/alternative
    # contract, so it remains fail-fast until a constrained v2 RPC exists.
    if provider_preference == "supabase_v2_constrained":
        provider_route = _validated_provider_route(
            supabase_pgrouting.query_supabase_pgrouting(
                origin_lat, origin_lng, destination_lat, destination_lng,
                vehicle_type=profile.key,
            ),
            requested_origin=(origin_lat, origin_lng),
            requested_destination=(destination_lat, destination_lng),
            vehicle_type=profile.key,
        )
        if provider_route:
            route_source_name = "supabase_pgrouting"
            route["geometry"] = provider_route["geometry"]
            route["distance_km"] = float(provider_route["distance_km"])
            route["navigation"] = provider_route["navigation"]
            route["alternatives"] = provider_route["alternatives"]
            route["data_source"] = route_source_name
            route["avoided_congested_zones"] = []
            # Learned metadata belongs to exact local OSM edges. Once provider
            # geometry replaces those edges, retaining it would be a false
            # claim even if the provider happens to draw a similar road.
            route.pop("shortcuts_used", None)
            route.pop("local_road_distance_km", None)
            route.pop("local_road_segments", None)
            route.pop("local_road_audit", None)
            for provider_alternative in route["alternatives"]:
                if isinstance(provider_alternative, dict):
                    provider_alternative.pop("shortcuts_used", None)
                    provider_alternative.pop("local_road_distance_km", None)
                    provider_alternative.pop("local_road_segments", None)
                    provider_alternative.pop("local_road_audit", None)
            duration = provider_route.get("duration_mins")
            if isinstance(duration, (int, float)) and duration > 0:
                recommended_travel = round(float(duration), 1)
            route["modeled_travel_time_mins"] = recommended_travel
            route["generalized_cost_mins"] = None
            route["objective_cost_s"] = None
            route["routing_cost_breakdown"] = None
            route["routing_model"] = {
                "version": "external",
                "objective": "provider-supplied route",
                "selected_preference": preference.key,
                "preference_honored": False,
                "component_weights": router.route_preference_payload(
                    preference,
                )["component_weights"],
                "limitations": "See provider provenance; local OSM weights not applied.",
            }
            straight = route.get("straight_line_km") or 0.0
            route["detour_ratio"] = (
                round(route["distance_km"] / straight, 2) if straight > 0 else None
            )

    distance_km = float(route["distance_km"])
    customs_buffer = profile.customs_buffer_mins if dest_ferry_port else 0.0
    access = _geometry_access_metrics(
        route.get("geometry", []), (origin_lat, origin_lng),
        (destination_lat, destination_lng), src_snap_m, dst_snap_m,
    )
    total_eta = round(
        recommended_travel + customs_buffer + access["total_access_time_mins"], 1,
    )
    co2_emissions = _route_emissions_kg(route, profile)
    co2_saved = (
        round(max(0.0, _route_emissions_kg(route_now, profile)
                  - _route_emissions_kg(route_later, profile)), 2)
        if defer else 0.0
    )

    reason = (
        f"Congestion eases {prediction['current_score'] - prediction_later['current_score']:.0f} "
        "points over the next 30 minutes; the vehicle-weighted road ETA also improves."
        if defer
        else "Current road traffic is within optimal flow parameters."
    )
    corridor: Dict[str, Any] = {
        "id": f"free:{src_node}:{dst_node}",
        "name": f"{origin_display} → {dest_display}",
        "origin": f"node:{src_node}",
        "destination": f"node:{dst_node}",
        "destination_port": dest_ferry_port,
        "base_time_mins": route.get("routing_cost_breakdown", {}).get(
            "free_flow_mins", recommended_travel,
        ) if route.get("routing_cost_breakdown") else recommended_travel,
        "corridor_idx": corridor_idx,
        "key_checkpoints": [origin_display, dest_display],
    }
    alternative_routes = _alternative_route_options(
        route,
        profile,
        recommended_prediction,
        customs_buffer,
        recommended_travel,
        co2_emissions,
        recommended_departure,
        dest_ferry_port,
        now,
        (origin_lat, origin_lng),
        (destination_lat, destination_lng),
        src_snap_m,
        dst_snap_m,
        schedule_verified_at,
    )

    result: Dict[str, Any] = {
        "route_type": "ROAD_ROUTE",
        "corridor": {
            **{k: v for k, v in corridor.items() if k not in ("corridor_idx",)},
            "distance_km": distance_km,
            "straight_line_km": route.get("straight_line_km", 0.0),
            "detour_ratio": route.get("detour_ratio"),
        },
        "requested_origin": {
            "name": origin_display,
            "lat": origin_lat,
            "lng": origin_lng,
        },
        "requested_destination": {
            "name": dest_display,
            "lat": destination_lat,
            "lng": destination_lng,
        },
        "vehicle_type": vehicle_type,
        "vehicle_profile": route.get("vehicle_profile") or router.vehicle_profile_payload(profile),
        "route_preference": route.get("route_preference", preference.key),
        "route_preference_profile": route.get("route_preference_profile") or (
            router.route_preference_payload(preference)
        ),
        "planned_departure": clock.iso(recommended_departure),
        "planning_traffic_snapshot": _planning_traffic_snapshot(
            recommended_zones,
            recommended_departure,
            weather,
            applied_to_returned_route=route_source_name == "openstreetmap",
        ),
        "congestion_prediction": _prediction_for_selected_route(
            recommended_prediction, route,
        ),
        "estimated_travel_time_mins": recommended_travel,
        "customs_buffer_mins": customs_buffer,
        "access_distance_km": access["total_access_distance_km"],
        "access_time_mins": access["total_access_time_mins"],
        "total_eta_mins": total_eta,
        "co2_emissions_kg": co2_emissions,
        "co2_saved_kg": co2_saved,
        "ferry_surge": recommended_surge,
        "surge_source": recommended_surge_source,
        "optimal_departure": {
            "recommended": "DEFER_30_MINS" if defer else "DEPART_NOW",
            "time_saved_mins": saved_mins if defer else 0.0,
            "reason": reason,
        },
        "route_geometry": route.get("geometry", []),
        "route_data_source": route_source_name,
        "navigation": route.get("navigation"),
        "generalized_cost_mins": route.get("generalized_cost_mins"),
        "objective_cost_s": route.get("objective_cost_s"),
        "routing_cost_breakdown": route.get("routing_cost_breakdown"),
        "routing_model": route.get("routing_model"),
        "snap_info": access,
        "avoided_congested_zones": route.get("avoided_congested_zones", []) if route else [],
        "alternative_routes": alternative_routes,
        "alternatives_note": (
            None if len(alternative_routes) >= 2
            else "Fewer than two sufficiently distinct routes satisfy the detour limit."
        ),
    }
    if route.get("shortcuts_used"):
        result["shortcuts_used"] = route["shortcuts_used"]
    if route.get("approved_graph_overrides_used"):
        result["approved_graph_overrides_used"] = (
            route["approved_graph_overrides_used"]
        )
    if route.get("local_road_audit") is not None:
        result.update({
            "local_road_distance_km": route.get("local_road_distance_km", 0.0),
            "local_road_segments": route.get("local_road_segments", []),
            "local_road_audit": route["local_road_audit"],
        })

    arrival = recommended_departure + timedelta(minutes=total_eta)
    port = dest_ferry_port or "Batam Centre"
    result["next_matching_ferries"] = ferry_schedule.next_sailings_after(
        arrival,
        limit=3,
        port=port,
        now=now,
        schedule_verified_at=schedule_verified_at,
    )
    if not dest_ferry_port:
        result["ferry_connection_note"] = (
            "Destination is not a ferry port; showing nearest Batam Centre departures for reference."
        )

    result["estimated_arrival"] = clock.iso(arrival)
    result["scheduling"] = _schedule_metadata(
        "DEPART_AT" if departure_at is not None else "HOUR",
        departure_at,
        None,
        recommended_departure,
        arrival,
    )

    return result


# ---------------------------------------------------------------------------
# Multi-stop journeys
# ---------------------------------------------------------------------------

# Every leg runs a full A* search over the committed 115k-node graph, so the
# request cost grows linearly with the stop count. The cap keeps the worst
# case bounded rather than letting one request occupy a worker indefinitely.
MAX_MULTI_STOP_COUNT = 8
MAX_MULTI_STOP_DWELL_MINS = 720.0


def _normalized_multi_stops(stops: Any) -> List[Dict[str, Any]]:
    """Validate the requested stop list and fill in display names."""
    if not isinstance(stops, (list, tuple)):
        raise ValueError("stops must be a list of coordinates.")
    if len(stops) < 3:
        raise ValueError(
            "A multi-stop journey needs at least three stops: an origin, one "
            "intermediate destination, and a final destination. Use the "
            "single-route solver for a two-point journey."
        )
    if len(stops) > MAX_MULTI_STOP_COUNT:
        raise ValueError(
            f"A multi-stop journey supports at most {MAX_MULTI_STOP_COUNT} "
            f"stops; {len(stops)} were requested."
        )

    normalized: List[Dict[str, Any]] = []
    for index, stop in enumerate(stops):
        if not isinstance(stop, dict):
            raise ValueError(f"Stop {index + 1} must be an object with lat and lng.")
        try:
            lat = float(stop["lat"])
            lng = float(stop["lng"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"Stop {index + 1} must carry numeric lat and lng."
            ) from error
        if not math.isfinite(lat) or not math.isfinite(lng):
            raise ValueError(f"Stop {index + 1} has non-finite coordinates.")
        region = multimodal_router.location_region(lat, lng)
        if region is None:
            raise ValueError(
                f"Stop {index + 1} is outside the supported Singapore-Batam "
                "service area."
            )
        dwell = stop.get("dwell_mins", 0.0) or 0.0
        try:
            dwell = float(dwell)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Stop {index + 1} has a non-numeric dwell_mins."
            ) from error
        if not math.isfinite(dwell) or dwell < 0:
            raise ValueError(f"Stop {index + 1} has a negative dwell_mins.")
        if dwell > MAX_MULTI_STOP_DWELL_MINS:
            raise ValueError(
                f"Stop {index + 1} exceeds the {MAX_MULTI_STOP_DWELL_MINS:.0f} "
                "minute dwell limit."
            )
        name = stop.get("name")
        normalized.append({
            "lat": lat,
            "lng": lng,
            "name": str(name) if name else f"{lat:.5f}, {lng:.5f}",
            "region": region,
            # The origin is departed from, not waited at; the final stop ends
            # the journey. Dwell on either would inflate the ETA.
            "dwell_mins": round(dwell, 1) if 0 < index < len(stops) - 1 else 0.0,
        })
    return normalized


def _two_opt_ordered(
    stops: List[Dict[str, Any]],
    cost: Any,
) -> List[Dict[str, Any]]:
    """Nearest-neighbour seed refined by 2-opt, with both ends pinned."""
    origin, destination = stops[0], stops[-1]
    remaining = list(stops[1:-1])

    ordered = [origin]
    while remaining:
        current = ordered[-1]
        nearest = min(remaining, key=lambda stop: cost(current, stop))
        remaining.remove(nearest)
        ordered.append(nearest)
    ordered.append(destination)

    def total(route: List[Dict[str, Any]]) -> float:
        return sum(cost(a, b) for a, b in zip(route, route[1:]))

    best = total(ordered)
    improved = True
    while improved:
        improved = False
        # Endpoints are fixed, so only the interior segment may be reversed.
        for i in range(1, len(ordered) - 2):
            for j in range(i + 1, len(ordered) - 1):
                candidate = ordered[:i] + ordered[i:j + 1][::-1] + ordered[j + 1:]
                candidate_cost = total(candidate)
                if candidate_cost < best - 1e-9:
                    ordered, best = candidate, candidate_cost
                    improved = True
    return ordered


def _ordered_multi_stops(
    stops: List[Dict[str, Any]],
    optimize_order: bool,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return the visiting order plus an honest description of how it was picked."""
    if not optimize_order or len(stops) < 4:
        return stops, {
            "requested": optimize_order,
            "applied": False,
            "method": "requested_order",
            "reason": (
                "Fewer than two intermediate stops, so the order is already "
                "determined." if optimize_order else
                "The requested stop order was used as given."
            ),
        }

    regions = {stop["region"] for stop in stops}
    if len(regions) > 1:
        # Reordering across the crossing would shuffle sailings, terminal
        # pairs and customs handling, none of which this heuristic models.
        return stops, {
            "requested": True,
            "applied": False,
            "method": "requested_order",
            "reason": (
                "Stops span Singapore and Batam. Reordering is not applied "
                "across a ferry crossing; the requested order was kept."
            ),
        }

    def straight_line_m(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        return router.haversine_m((a["lat"], a["lng"]), (b["lat"], b["lng"]))

    ordered = _two_opt_ordered(stops, straight_line_m)
    changed = [stop["name"] for stop in ordered] != [stop["name"] for stop in stops]
    return ordered, {
        "requested": True,
        "applied": changed,
        "method": "nearest_neighbour_2opt_straight_line",
        "reason": (
            "Intermediate stops were ordered by straight-line proximity."
            if changed else
            "The requested order already minimised straight-line travel."
        ),
        "limitations": (
            "Ordering uses straight-line distance between stops, which is "
            "cheap enough to run per request. It does not account for road "
            "distance, one-way streets, or congestion, so a different order "
            "may be faster on the road. Each leg's reported distance and time "
            "are solved on the road graph."
        ),
    }


def _combined_multi_stop_navigation(
    legs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Concatenate per-leg maneuvers into one continuously numbered list."""
    maneuvers: List[Dict[str, Any]] = []
    landmarks: List[Any] = []
    traffic_lights = 0
    narratives: List[str] = []
    data_sources: List[str] = []
    cumulative_m = 0.0

    for leg_index, leg in enumerate(legs):
        navigation = leg.get("navigation") or {}
        leg_maneuvers = navigation.get("maneuvers") or []
        leg_distance_m = float(leg.get("distance_km") or 0.0) * 1000.0
        is_last_leg = leg_index == len(legs) - 1
        for maneuver in leg_maneuvers:
            step = copy.deepcopy(maneuver)
            # An intermediate ARRIVE is a scheduled stop, not the journey end.
            if not is_last_leg and str(step.get("type", "")).upper() == "ARRIVE":
                step["type"] = "WAYPOINT"
                step["icon"] = "landmark"
                step["instruction"] = (
                    f"Arrive at stop {leg_index + 2}: {leg.get('to_name')}"
                )
            step["leg_index"] = leg_index
            step["cumulative_distance_m"] = round(
                cumulative_m + float(step.get("cumulative_distance_m") or 0.0), 1,
            )
            step["step"] = len(maneuvers) + 1
            maneuvers.append(step)
        cumulative_m += leg_distance_m
        landmarks.extend(navigation.get("landmarks_along_route") or [])
        traffic_lights += int(navigation.get("traffic_lights_count") or 0)
        narrative = navigation.get("route_narrative_words")
        if narrative:
            narratives.append(str(narrative))
        source = navigation.get("data_source")
        if source and source not in data_sources:
            data_sources.append(str(source))

    return {
        "schema_version": 1,
        "data_source": "+".join(data_sources) if data_sources else "openstreetmap",
        "maneuvers": maneuvers,
        "landmarks_along_route": landmarks,
        "traffic_lights_count": traffic_lights,
        "route_narrative_words": " ".join(narratives),
    }


def _combined_multi_stop_geometry(
    legs: List[Dict[str, Any]],
) -> List[List[float]]:
    """Join leg polylines, dropping the duplicated point at each stop."""
    combined: List[List[float]] = []
    for leg in legs:
        geometry = leg.get("route_geometry") or []
        if not geometry:
            continue
        if combined and combined[-1] == list(geometry[0]):
            combined.extend([list(point) for point in geometry[1:]])
        else:
            combined.extend([list(point) for point in geometry])
    return combined


def optimize_multi_stop_route(
    stops: Any,
    vehicle_type: str,
    hour: int = 14,
    weather: int = 0,
    now: Optional[datetime] = None,
    route_preference: str = "BALANCED",
    optimize_order: bool = False,
    schedule_verified_at: Optional[str] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
    departure_at: Optional[datetime] = None,
    arrive_by: Optional[datetime] = None,
    _schedule_search: bool = False,
) -> Dict[str, Any]:
    """Chain three or more stops into one scheduled journey.

    Each consecutive pair is solved by :func:`optimize_free_route` in leg mode,
    so a leg through Batam uses the committed OSM graph and a leg touching
    Singapore uses the multimodal composer — the same engines, contracts and
    limitations as a two-point route.

    Legs are solved in order because each one departs when the previous leg
    arrives plus its dwell, and the congestion model is evaluated at that
    departure hour. The journey-level totals are the sum of the legs; nothing
    here re-estimates a leg independently.
    """
    now = now or clock.now()
    _validate_time_inputs(hour, weather)
    departure_at, arrive_by = _validate_schedule_request(
        departure_at, arrive_by, now,
    )
    if arrive_by is not None and not _schedule_search:
        def solve(candidate: datetime) -> Dict[str, Any]:
            return optimize_multi_stop_route(
                stops=stops, vehicle_type=vehicle_type, hour=candidate.hour,
                weather=weather, now=now, route_preference=route_preference,
                optimize_order=optimize_order,
                schedule_verified_at=schedule_verified_at,
                approved_override_snapshot=approved_override_snapshot,
                departure_at=candidate, _schedule_search=True,
            )
        result = _latest_feasible_departure(solve, now, arrive_by)
        planned = datetime.fromisoformat(result["planned_departure"])
        estimated = datetime.fromisoformat(result["estimated_arrival"])
        result["scheduling"] = _schedule_metadata(
            "ARRIVE_BY", None, arrive_by, planned, estimated,
        )
        return result
    profile = _validated_profile(vehicle_type)
    preference = _validated_preference(route_preference)

    normalized = _normalized_multi_stops(stops)
    ordered, order_metadata = _ordered_multi_stops(normalized, optimize_order)

    if (
        len({stop["region"] for stop in ordered}) > 1
        and profile.key in PASSENGER_FERRY_INCOMPATIBLE_VEHICLES
    ):
        raise ValueError(
            "Light and heavy trucks cannot be scheduled on the published "
            "passenger-ferry services used by this planner. Local road routing "
            "remains available; cross-border freight requires a cargo port, "
            "roll-on/roll-off operator, or an authorised logistics-partner feed."
        )

    journey_departure = departure_at or resolve_planned_departure(hour, now)
    hour = journey_departure.hour
    leg_departure = journey_departure
    legs: List[Dict[str, Any]] = []
    shortcuts_used: List[Any] = []
    overrides_used: List[Any] = []
    total_travel_mins = 0.0
    total_dwell_mins = 0.0
    total_distance_km = 0.0
    total_access_km = 0.0
    total_emissions_kg = 0.0
    total_customs_mins = 0.0

    for index in range(len(ordered) - 1):
        start, end = ordered[index], ordered[index + 1]
        leg_result = optimize_free_route(
            origin_lat=start["lat"],
            origin_lng=start["lng"],
            destination_lat=end["lat"],
            destination_lng=end["lng"],
            vehicle_type=profile.key,
            hour=leg_departure.hour,
            weather=weather,
            now=now,
            origin_name=start["name"],
            destination_name=end["name"],
            route_preference=preference.key,
            schedule_verified_at=schedule_verified_at,
            approved_override_snapshot=approved_override_snapshot,
            leg_mode=True,
            departure_at=leg_departure,
        )

        travel_mins = float(leg_result.get("estimated_travel_time_mins") or 0.0)
        customs_mins = float(leg_result.get("customs_buffer_mins") or 0.0)
        access_mins = float(leg_result.get("access_time_mins") or 0.0)
        leg_elapsed = travel_mins + customs_mins + access_mins
        leg_arrival = leg_departure + timedelta(minutes=leg_elapsed)
        dwell_mins = float(end["dwell_mins"])

        legs.append({
            "leg_index": index,
            "from_name": start["name"],
            "to_name": end["name"],
            "from": {"lat": start["lat"], "lng": start["lng"]},
            "to": {"lat": end["lat"], "lng": end["lng"]},
            "route_type": leg_result.get("route_type"),
            "distance_km": float(leg_result["corridor"]["distance_km"]),
            "estimated_travel_time_mins": round(travel_mins, 1),
            "customs_buffer_mins": customs_mins,
            "access_time_mins": access_mins,
            "total_eta_mins": round(leg_elapsed, 1),
            "departure": clock.iso(leg_departure),
            "arrival": clock.iso(leg_arrival),
            "dwell_mins": dwell_mins,
            "co2_emissions_kg": float(leg_result.get("co2_emissions_kg") or 0.0),
            "route_geometry": leg_result.get("route_geometry", []),
            "route_data_source": leg_result.get("route_data_source"),
            "navigation": leg_result.get("navigation"),
            "congestion_prediction": leg_result.get("congestion_prediction"),
            "routing_cost_breakdown": leg_result.get("routing_cost_breakdown"),
            "snap_info": leg_result.get("snap_info"),
            "avoided_congested_zones": leg_result.get("avoided_congested_zones", []),
            "route_legs": leg_result.get("route_legs"),
        })
        # Learned-shortcut and approved-override provenance is per edge, so the
        # journey has to carry the union of what its legs actually traversed;
        # the service audit reads it off the top-level result.
        for shortcut in leg_result.get("shortcuts_used") or ():
            shortcuts_used.append(shortcut)
        for override in leg_result.get("approved_graph_overrides_used") or ():
            overrides_used.append(override)

        total_travel_mins += travel_mins
        total_customs_mins += customs_mins
        total_access_km += float(leg_result.get("access_distance_km") or 0.0)
        total_distance_km += float(leg_result["corridor"]["distance_km"])
        total_emissions_kg += float(leg_result.get("co2_emissions_kg") or 0.0)
        total_dwell_mins += dwell_mins
        leg_departure = leg_arrival + timedelta(minutes=dwell_mins)

    journey_arrival = leg_departure
    total_eta_mins = round(
        (journey_arrival - journey_departure).total_seconds() / 60.0, 1,
    )
    is_multimodal = any(
        leg["route_type"] == "MULTIMODAL_FERRY_ROUTE" for leg in legs
    )
    geometry = _combined_multi_stop_geometry(legs)
    leg_sources = []
    for leg in legs:
        source = leg.get("route_data_source")
        if source and source not in leg_sources:
            leg_sources.append(str(source))

    final_stop = ordered[-1]
    result: Dict[str, Any] = {
        "route_type": (
            "MULTIMODAL_MULTI_STOP_ROUTE" if is_multimodal
            else "MULTI_STOP_ROUTE"
        ),
        "corridor": {
            "id": "multi:" + ":".join(
                f"{stop['lat']:.5f},{stop['lng']:.5f}" for stop in ordered
            ),
            "name": " → ".join(stop["name"] for stop in ordered),
            "origin": f"{ordered[0]['lat']:.5f}, {ordered[0]['lng']:.5f}",
            "destination": f"{final_stop['lat']:.5f}, {final_stop['lng']:.5f}",
            "distance_km": round(total_distance_km, 2),
            "straight_line_km": round(
                sum(
                    router.haversine_m(
                        (a["lat"], a["lng"]), (b["lat"], b["lng"]),
                    )
                    for a, b in zip(ordered, ordered[1:])
                ) / 1000.0,
                2,
            ),
            "stop_count": len(ordered),
            "leg_count": len(legs),
            "key_checkpoints": [stop["name"] for stop in ordered],
        },
        "stops": [
            {
                "sequence": index + 1,
                "name": stop["name"],
                "lat": stop["lat"],
                "lng": stop["lng"],
                "region": stop["region"],
                "dwell_mins": stop["dwell_mins"],
                "role": (
                    "ORIGIN" if index == 0
                    else "DESTINATION" if index == len(ordered) - 1
                    else "WAYPOINT"
                ),
            }
            for index, stop in enumerate(ordered)
        ],
        "stop_order_optimization": order_metadata,
        "requested_origin": {
            "name": ordered[0]["name"],
            "lat": ordered[0]["lat"],
            "lng": ordered[0]["lng"],
        },
        "requested_destination": {
            "name": final_stop["name"],
            "lat": final_stop["lat"],
            "lng": final_stop["lng"],
        },
        "vehicle_type": profile.key,
        "vehicle_profile": router.vehicle_profile_payload(profile),
        "route_preference": preference.key,
        "route_preference_profile": router.route_preference_payload(preference),
        "planned_departure": clock.iso(journey_departure),
        "estimated_arrival": clock.iso(journey_arrival),
        "estimated_travel_time_mins": round(total_travel_mins, 1),
        "customs_buffer_mins": round(total_customs_mins, 1),
        "access_distance_km": round(total_access_km, 3),
        "dwell_time_mins": round(total_dwell_mins, 1),
        "total_eta_mins": total_eta_mins,
        "co2_emissions_kg": round(total_emissions_kg, 2),
        "co2_saved_kg": 0.0,
        "legs": legs,
        "route_geometry": geometry,
        "route_data_source": (
            "+".join(leg_sources) if leg_sources else "openstreetmap"
        ),
        "navigation": _combined_multi_stop_navigation(legs),
        "congestion_prediction": legs[0].get("congestion_prediction") if legs else None,
        "optimal_departure": {
            # Deferral is a whole-journey decision and every leg after the
            # first departs at a time this solver derived, not one the traveller
            # picks, so no per-leg deferral is offered.
            "recommended": "DEPART_NOW",
            "time_saved_mins": 0.0,
            "reason": (
                "Multi-stop journeys are scheduled from the requested "
                "departure; each leg is modelled at the hour it actually "
                "departs."
            ),
        },
        "alternative_routes": [],
        "alternatives_note": (
            "Alternative paths are offered for two-point routes only; a "
            "multi-stop journey is reported as the scheduled chain of legs."
        ),
        "limitations": (
            "Leg times come from the same modelled congestion used by the "
            "two-point solver. Dwell times are the caller's own inputs and are "
            "not validated against opening hours, loading bays, or driver "
            "break rules."
        ),
    }

    if shortcuts_used:
        result["shortcuts_used"] = shortcuts_used
    if overrides_used:
        result["approved_graph_overrides_used"] = overrides_used

    port_stop = final_stop
    arrival_port = None
    for location in ROUTE_LOCATIONS:
        if not location.get("ferry_port"):
            continue
        if router.haversine_m(
            (location["lat"], location["lng"]),
            (port_stop["lat"], port_stop["lng"]),
        ) < FERRY_TERMINAL_MATCH_M:
            arrival_port = location["ferry_port"]
            break
    result["next_matching_ferries"] = ferry_schedule.next_sailings_after(
        journey_arrival,
        limit=3,
        port=arrival_port or "Batam Centre",
        now=now,
        schedule_verified_at=schedule_verified_at,
    )
    if not arrival_port:
        result["ferry_connection_note"] = (
            "The final stop is not a ferry port; showing nearest Batam Centre "
            "departures for reference."
        )
    result["scheduling"] = _schedule_metadata(
        "DEPART_AT" if departure_at is not None else "HOUR",
        departure_at,
        None,
        journey_departure,
        journey_arrival,
    )
    return result
