"""Singapore/Batam multimodal journey composition.

Batam road-only requests continue to use :mod:`services.route_solver` and the
committed Batam OSM graph. This module handles journeys that include Singapore.
It prefers OSRM geometry derived from OpenStreetMap for Singapore road legs,
retains the committed graph for Batam road legs, and always has a deterministic,
explicitly approximate connector fallback.

Ferry terminal pairs and crossing durations are derived from the committed
snapshot of official operator timetables. The sea polylines are planning
visualisations of the published terminal pair, not observed vessel tracks.
"""

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from services import ferry_schedule, router, tls
from services.service_contracts import ApprovedGraphOverrideSnapshot


Coordinate = Tuple[float, float]
_USER_AGENT = "CrossFlowAI/3.1 (batam-singapore-hackathon-2026)"
_OSRM_BASE_URL = os.environ.get(
    "CROSSFLOW_OSRM_BASE_URL", "https://router.project-osrm.org",
).strip().rstrip("/")
_MAX_BATAM_GRAPH_SNAP_M = 1_000.0
_BOARDING_BUFFER_MINS = ferry_schedule.BOARDING_CUTOFF_MINS
_REGION_TIMEZONES = {
    "BATAM": ZoneInfo("Asia/Jakarta"),
    "SINGAPORE": ZoneInfo("Asia/Singapore"),
}


class TerminalPair(NamedTuple):
    singapore_name: str
    singapore_coords: Coordinate
    batam_name: str
    batam_coords: Coordinate
    crossing_mins: int
    sea_geometry: Tuple[Coordinate, ...]


_SINGAPORE_TERMINALS: Dict[str, Coordinate] = {
    "HarbourFront SG": (1.2644, 103.8206),
    "Tanah Merah SG": (1.3143, 103.9886),
}
_BATAM_TERMINALS: Dict[str, Coordinate] = {
    "Batam Centre": (1.1318, 104.0554),
    "HarbourBay": (1.15396, 103.997234),
    "Sekupang": (1.1250, 103.9250),
    "Nongsa Pura": (1.1890, 104.1020),
}

# Coarse channel-aware display geometry. It identifies the terminal corridor
# without claiming to be a tracked or navigational vessel path.
_SEA_GEOMETRIES: Dict[Tuple[str, str], Tuple[Coordinate, ...]] = {
    ("HarbourFront SG", "Batam Centre"): (
        (1.2644, 103.8206), (1.2440, 103.8310), (1.2160, 103.8610),
        (1.1880, 103.9070), (1.1610, 103.9660), (1.1430, 104.0170),
        (1.1318, 104.0554),
    ),
    ("HarbourFront SG", "HarbourBay"): (
        (1.2644, 103.8206), (1.2430, 103.8300), (1.2160, 103.8670),
        (1.1900, 103.9140), (1.1710, 103.9610), (1.15396, 103.997234),
    ),
    ("HarbourFront SG", "Sekupang"): (
        (1.2644, 103.8206), (1.2410, 103.8190), (1.2110, 103.8390),
        (1.1740, 103.8730), (1.1450, 103.9050), (1.1250, 103.9250),
    ),
    ("Tanah Merah SG", "Batam Centre"): (
        (1.3143, 103.9886), (1.2820, 103.9980), (1.2450, 104.0060),
        (1.2060, 104.0200), (1.1660, 104.0410), (1.1318, 104.0554),
    ),
    ("Tanah Merah SG", "Nongsa Pura"): (
        (1.3143, 103.9886), (1.2830, 103.9990), (1.2500, 104.0150),
        (1.2200, 104.0460), (1.2040, 104.0770), (1.1890, 104.1020),
    ),
}


def location_region(lat: float, lng: float) -> Optional[str]:
    """Classify coordinates within the supported Singapore/Batam corridor."""
    # The islands' broad boxes overlap around Singapore's southern islands.
    # Batam's routable places are south of 1.215; points farther north are SG.
    if 0.88 <= lat < 1.215 and 103.75 <= lng <= 104.30:
        return "BATAM"
    if 1.215 <= lat <= 1.50 and 103.55 <= lng <= 104.15:
        return "SINGAPORE"
    return None


def supported_location(lat: float, lng: float) -> bool:
    return location_region(lat, lng) is not None


def _haversine_km(start: Coordinate, end: Coordinate) -> float:
    return router.haversine_m(start, end) / 1000.0


def _geometry_length_km(geometry: Sequence[Coordinate]) -> float:
    return sum(
        _haversine_km(start, end)
        for start, end in zip(geometry, geometry[1:])
    )


def _terminal_pairs() -> List[TerminalPair]:
    pairs: Dict[Tuple[str, str], TerminalPair] = {}
    for service in ferry_schedule.FERRY_ROUTES:
        if service.departure_port in _SINGAPORE_TERMINALS:
            singapore_name = service.departure_port
            batam_name = service.arrival_port
        else:
            singapore_name = service.arrival_port
            batam_name = service.departure_port
        sg_coords = _SINGAPORE_TERMINALS.get(singapore_name)
        batam_coords = _BATAM_TERMINALS.get(batam_name)
        sea_geometry = _SEA_GEOMETRIES.get((singapore_name, batam_name))
        if sg_coords is None or batam_coords is None or sea_geometry is None:
            continue
        key = (singapore_name, batam_name)
        previous = pairs.get(key)
        crossing_mins = min(
            service.estimated_crossing_mins,
            previous.crossing_mins if previous else service.estimated_crossing_mins,
        )
        pairs[key] = TerminalPair(
            singapore_name, sg_coords,
            batam_name, batam_coords,
            crossing_mins, sea_geometry,
        )
    return sorted(
        pairs.values(),
        key=lambda pair: (pair.singapore_name, pair.batam_name),
    )


def _choose_terminal_pair(
    origin: Coordinate,
    destination: Coordinate,
    origin_region: str,
) -> TerminalPair:
    candidates = _terminal_pairs()
    if not candidates:
        raise ValueError("No published Singapore-Batam ferry corridor is configured.")
    directional_candidates = [
        pair for pair in candidates
        if any(
            (
                service.departure_port == pair.singapore_name
                and service.arrival_port == pair.batam_name
            ) if origin_region == "SINGAPORE" else (
                service.departure_port == pair.batam_name
                and service.arrival_port == pair.singapore_name
            )
            for service in ferry_schedule.FERRY_ROUTES
        )
    ]
    if directional_candidates:
        candidates = directional_candidates

    def score(pair: TerminalPair) -> float:
        if origin_region == "SINGAPORE":
            first = _haversine_km(origin, pair.singapore_coords)
            last = _haversine_km(pair.batam_coords, destination)
        else:
            first = _haversine_km(origin, pair.batam_coords)
            last = _haversine_km(pair.singapore_coords, destination)
        # Approximate access-road minutes plus published crossing duration.
        return ((first + last) * 1.22 / 35.0 * 60.0) + pair.crossing_mins

    return min(candidates, key=lambda pair: (score(pair), pair))


def _decode_osrm_step(
    step: Dict[str, Any], index: int, cumulative_m: float,
) -> Dict[str, Any]:
    maneuver = step.get("maneuver") if isinstance(step.get("maneuver"), dict) else {}
    location = maneuver.get("location", [0.0, 0.0])
    maneuver_type = str(maneuver.get("type", "continue")).replace(" ", "_").upper()
    modifier = str(maneuver.get("modifier", "straight")).replace(" ", "_")
    road = str(step.get("name") or step.get("ref") or "Mapped road")
    distance_m = max(0.0, float(step.get("distance", 0.0)))
    verb = {
        "DEPART": "Head onto",
        "ARRIVE": "Arrive via",
        "TURN": "Turn onto",
        "CONTINUE": "Continue on",
        "MERGE": "Merge onto",
        "ON_RAMP": "Take the ramp onto",
        "OFF_RAMP": "Exit onto",
        "ROUNDABOUT": "Use the roundabout towards",
    }.get(maneuver_type, "Continue on")
    return {
        "step": index,
        "type": maneuver_type,
        "modifier": modifier,
        "instruction": f"{verb} {road}",
        "street": road,
        "road_ref": step.get("ref"),
        "distance_m": round(distance_m),
        "cumulative_distance_m": round(cumulative_m),
        "icon": modifier if modifier != "straight" else maneuver_type.lower(),
        "coords": [float(location[1]), float(location[0])],
    }


def _osrm_route(
    origin: Coordinate,
    destination: Coordinate,
) -> Optional[Dict[str, Any]]:
    """Return an OSM/OSRM road leg, or ``None`` for continuity fallback."""
    if not _OSRM_BASE_URL:
        return None
    coordinates = (
        f"{origin[1]:.6f},{origin[0]:.6f};"
        f"{destination[1]:.6f},{destination[0]:.6f}"
    )
    query = urllib.parse.urlencode({
        "overview": "full", "geometries": "geojson", "steps": "true",
        "alternatives": "false",
    })
    url = f"{_OSRM_BASE_URL}/route/v1/driving/{coordinates}?{query}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(
            request, timeout=4, context=tls.default_context(),
        ) as response:
            payload = json.loads(response.read())
    except Exception as error:  # noqa: BLE001 - provider failure is non-fatal
        print(f"[multimodal] OSRM unavailable: {error}")
        return None
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("code") != "Ok" \
            or not isinstance(routes, list) or not routes:
        return None
    candidate = routes[0]
    raw_coords = candidate.get("geometry", {}).get("coordinates", [])
    geometry = [
        [float(point[1]), float(point[0])]
        for point in raw_coords
        if isinstance(point, list) and len(point) >= 2
    ]
    if len(geometry) < 2:
        return None
    raw_steps = [
        step
        for leg in candidate.get("legs", [])
        for step in leg.get("steps", [])
        if isinstance(step, dict)
    ]
    maneuvers: List[Dict[str, Any]] = []
    cumulative = 0.0
    for index, step in enumerate(raw_steps, start=1):
        maneuvers.append(_decode_osrm_step(step, index, cumulative))
        cumulative += max(0.0, float(step.get("distance", 0.0)))
    if len(maneuvers) < 2:
        maneuvers = _estimate_navigation(
            origin, destination, "Destination",
        )["maneuvers"]
    return {
        "geometry": geometry,
        "distance_km": round(float(candidate.get("distance", 0.0)) / 1000.0, 2),
        "duration_mins": round(max(1.0, float(candidate.get("duration", 0.0)) / 60.0), 1),
        "navigation": {
            "schema_version": 1,
            "data_source": "osrm_openstreetmap",
            "maneuvers": maneuvers,
            "landmarks_along_route": [],
            "traffic_lights_count": 0,
            "route_narrative_words": "Road geometry and steps returned by OSRM over OpenStreetMap data.",
        },
        "data_source": "osrm_openstreetmap",
        "is_estimate": False,
        "limitations": "OSRM road geometry; obey posted access and vehicle restrictions.",
    }


def _curved_connector(origin: Coordinate, destination: Coordinate) -> List[List[float]]:
    """Deterministic visual connector that is never described as a road path."""
    lat_delta = destination[0] - origin[0]
    lng_delta = destination[1] - origin[1]
    bend = min(0.012, max(0.0015, math.hypot(lat_delta, lng_delta) * 0.06))
    magnitude = max(0.000001, math.hypot(lat_delta, lng_delta))
    lat_perp = -lng_delta / magnitude * bend
    lng_perp = lat_delta / magnitude * bend
    return [
        [round(origin[0], 6), round(origin[1], 6)],
        [round(origin[0] + lat_delta * 0.25 + lat_perp, 6),
         round(origin[1] + lng_delta * 0.25 + lng_perp, 6)],
        [round(origin[0] + lat_delta * 0.50 + lat_perp * 0.5, 6),
         round(origin[1] + lng_delta * 0.50 + lng_perp * 0.5, 6)],
        [round(origin[0] + lat_delta * 0.75, 6),
         round(origin[1] + lng_delta * 0.75, 6)],
        [round(destination[0], 6), round(destination[1], 6)],
    ]


def _estimate_navigation(
    origin: Coordinate,
    destination: Coordinate,
    destination_name: str,
) -> Dict[str, Any]:
    distance_m = round(_haversine_km(origin, destination) * 1_000 * 1.22)
    return {
        "schema_version": 1,
        "data_source": "offline_access_estimate",
        "maneuvers": [
            {
                "step": 1, "type": "DEPART", "modifier": "straight",
                "instruction": "Start the estimated road-access leg",
                "street": "Road access estimate", "distance_m": distance_m,
                "cumulative_distance_m": 0, "icon": "depart",
                "coords": [origin[0], origin[1]],
            },
            {
                "step": 2, "type": "ARRIVE", "modifier": "arrive",
                "instruction": f"Arrive at {destination_name}",
                "street": destination_name, "distance_m": 0,
                "cumulative_distance_m": distance_m, "icon": "arrive",
                "coords": [destination[0], destination[1]],
            },
        ],
        "landmarks_along_route": [],
        "traffic_lights_count": 0,
        "route_narrative_words": (
            "Continuity estimate between the selected point and terminal; "
            "not turn-by-turn road navigation."
        ),
    }


def _estimated_road_leg(
    origin: Coordinate,
    destination: Coordinate,
    origin_name: str,
    destination_name: str,
) -> Dict[str, Any]:
    straight_km = _haversine_km(origin, destination)
    distance_km = round(straight_km * 1.22, 2)
    return {
        "mode": "ROAD",
        "from_name": origin_name,
        "to_name": destination_name,
        "geometry": _curved_connector(origin, destination),
        "distance_km": distance_km,
        "duration_mins": round(max(2.0, distance_km / 32.0 * 60.0), 1),
        "data_source": "offline_access_estimate",
        "is_estimate": True,
        "navigation": _estimate_navigation(origin, destination, destination_name),
        "limitations": (
            "Deterministic access-time and display connector estimate; it is "
            "not a turn-by-turn road path."
        ),
    }


def _road_leg(
    region: str,
    origin: Coordinate,
    destination: Coordinate,
    origin_name: str,
    destination_name: str,
    vehicle_type: str,
    route_preference: str,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Dict[str, Any]:
    if _haversine_km(origin, destination) < 0.08:
        leg = _estimated_road_leg(origin, destination, origin_name, destination_name)
        leg.update({"distance_km": 0.0, "duration_mins": 1.0})
        return leg

    if region == "BATAM":
        source, source_snap = router.snap_to_graph(
            *origin, vehicle_type, approved_override_snapshot,
        )
        target, target_snap = router.snap_to_graph(
            *destination, vehicle_type, approved_override_snapshot,
        )
        if (
            source is not None and target is not None
            and source_snap <= _MAX_BATAM_GRAPH_SNAP_M
            and target_snap <= _MAX_BATAM_GRAPH_SNAP_M
        ):
            route = router.route_between_nodes(
                source, target,
                origin_name=origin_name,
                destination_name=destination_name,
                include_alternatives=False,
                vehicle_type=vehicle_type,
                route_preference=route_preference,
                approved_override_snapshot=approved_override_snapshot,
            )
            if route and len(route.get("geometry", [])) >= 2:
                route_breakdown = route.get("routing_cost_breakdown") or {}
                leg = {
                    "mode": "ROAD",
                    "from_name": origin_name,
                    "to_name": destination_name,
                    "geometry": route["geometry"],
                    "distance_km": round(float(route["distance_km"]), 2),
                    "duration_mins": round(float(
                        route.get("modeled_travel_time_mins")
                        or route_breakdown.get("modeled_travel_time_mins")
                        or max(2.0, float(route["distance_km"]) / 32.0 * 60.0)
                    ), 1),
                    "data_source": "batam_bundled_openstreetmap",
                    "is_estimate": False,
                    "navigation": route.get("navigation"),
                    "limitations": (
                        "A* over the committed Batam OpenStreetMap extract; "
                        "lane-level restrictions are not represented."
                    ),
                }
                approved_overrides = route.get("approved_graph_overrides_used")
                if approved_overrides:
                    # Retain the exact reviewed identities that influenced this
                    # selected access leg.  Snapshot-level provenance alone is
                    # not enough to audit whether a crowd-sourced connector was
                    # actually traversed.
                    leg["approved_graph_overrides_used"] = [
                        dict(item) for item in approved_overrides
                    ]
                return leg

    osrm = _osrm_route(origin, destination)
    if osrm:
        return {
            "mode": "ROAD", "from_name": origin_name,
            "to_name": destination_name, **osrm,
        }
    return _estimated_road_leg(origin, destination, origin_name, destination_name)


def _published_sailing(
    pair: TerminalPair,
    earliest_terminal_arrival: datetime,
    now: datetime,
    origin_region: str,
    schedule_verified_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cutoff = earliest_terminal_arrival + timedelta(minutes=_BOARDING_BUFFER_MINS)
    sailings = ferry_schedule.generate_sailings(
        now,
        horizon_hours=48,
        schedule_verified_at=schedule_verified_at,
    )
    departure_port = (
        pair.singapore_name if origin_region == "SINGAPORE"
        else pair.batam_name
    )
    arrival_port = (
        pair.batam_name if origin_region == "SINGAPORE"
        else pair.singapore_name
    )
    matching = [
        sailing for sailing in sailings
        if sailing["departure_port"] == departure_port
        and sailing["arrival_port"] == arrival_port
        and datetime.fromisoformat(sailing["departure_time"]) >= cutoff
    ]
    return matching[0] if matching else None


def _ferry_leg(
    pair: TerminalPair,
    origin_region: str,
    earliest_terminal_arrival: datetime,
    now: datetime,
    schedule_verified_at: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], float]:
    batam_to_sg = origin_region == "BATAM"
    geometry = list(
        reversed(pair.sea_geometry) if batam_to_sg else pair.sea_geometry
    )
    sailing = _published_sailing(
        pair,
        earliest_terminal_arrival,
        now,
        origin_region,
        schedule_verified_at,
    )
    from_name = pair.batam_name if batam_to_sg else pair.singapore_name
    to_name = pair.singapore_name if batam_to_sg else pair.batam_name
    if sailing:
        departure = datetime.fromisoformat(sailing["departure_time"])
        wait_mins = max(0.0, (departure - earliest_terminal_arrival).total_seconds() / 60.0)
        schedule_status = "PUBLISHED_DEPARTURE_SELECTED"
        source = "official_timetable_snapshot"
        limitations = (
            "Published operator departure slot and estimated crossing duration; "
            "not live vessel, cancellation, gate, berth, capacity or seat status."
        )
        sailings = [sailing]
    else:
        wait_mins = float(_BOARDING_BUFFER_MINS)
        schedule_status = "NO_MATCHING_PUBLISHED_DEPARTURE"
        source = "official_corridor_duration_reference"
        limitations = (
            "No matching departure was found within the committed 48-hour "
            "official schedule snapshot. Crossing duration is a planning "
            "reference only; verify and book with the operator."
        )
        sailings = []
    crossing_mins = (
        int(sailing["estimated_crossing_mins"])
        if sailing else pair.crossing_mins
    )
    leg = {
        "mode": "FERRY",
        "from_name": from_name,
        "to_name": to_name,
        "geometry": [[point[0], point[1]] for point in geometry],
        "distance_km": round(_geometry_length_km(geometry), 2),
        "duration_mins": crossing_mins,
        "wait_mins": round(wait_mins, 1),
        "data_source": source,
        "is_estimate": True,
        "schedule_status": schedule_status,
        "selected_sailing": sailing,
        "limitations": limitations,
        "geometry_note": (
            "Channel-aware terminal corridor visualisation; not an observed or "
            "navigational vessel track."
        ),
    }
    return leg, sailings, wait_mins


def _combine_geometry(legs: Sequence[Dict[str, Any]]) -> List[List[float]]:
    combined: List[List[float]] = []
    for leg in legs:
        for point in leg.get("geometry", []):
            normalized = [float(point[0]), float(point[1])]
            if not combined or combined[-1] != normalized:
                combined.append(normalized)
    return combined


def _combine_navigation(legs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    maneuvers: List[Dict[str, Any]] = []
    cumulative_m = 0.0
    for leg in legs:
        if leg["mode"] == "FERRY":
            first = leg["geometry"][0]
            last = leg["geometry"][-1]
            maneuvers.extend([
                {
                    "type": "BOARD_FERRY", "modifier": "transfer",
                    "instruction": f"Board the ferry at {leg['from_name']}",
                    "street": leg["from_name"], "distance_m": 0,
                    "cumulative_distance_m": round(cumulative_m),
                    "icon": "ferry", "coords": first,
                },
                {
                    "type": "DISEMBARK_FERRY", "modifier": "transfer",
                    "instruction": f"Disembark at {leg['to_name']}",
                    "street": leg["to_name"], "distance_m": 0,
                    "cumulative_distance_m": round(
                        cumulative_m + float(leg["distance_km"]) * 1000,
                    ),
                    "icon": "ferry", "coords": last,
                },
            ])
        else:
            navigation = leg.get("navigation") or {}
            for item in navigation.get("maneuvers", []):
                copied = dict(item)
                copied["cumulative_distance_m"] = round(
                    cumulative_m + float(item.get("cumulative_distance_m", 0)),
                )
                maneuvers.append(copied)
        cumulative_m += float(leg["distance_km"]) * 1000
    for index, item in enumerate(maneuvers, start=1):
        item["step"] = index
    return {
        "schema_version": 1,
        "data_source": "composed_multimodal_journey",
        "maneuvers": maneuvers,
        "landmarks_along_route": [],
        "traffic_lights_count": sum(
            int((leg.get("navigation") or {}).get("traffic_lights_count", 0))
            for leg in legs if leg["mode"] == "ROAD"
        ),
        "route_narrative_words": " ".join(
            f"{leg['from_name']} to {leg['to_name']} by {leg['mode'].lower()}."
            for leg in legs
        ),
    }


def _approved_overrides_used_by_legs(
    legs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return deterministic, de-duplicated reviewed overrides on selected legs."""
    selected: Dict[Tuple[str, str, int, int], Dict[str, Any]] = {}
    for leg in legs:
        for raw in leg.get("approved_graph_overrides_used") or ():
            if not isinstance(raw, dict):
                continue
            override_id = raw.get("override_id")
            candidate_sha256 = raw.get("candidate_sha256")
            source_node = raw.get("source_node")
            target_node = raw.get("target_node")
            if (
                not isinstance(override_id, str)
                or not isinstance(candidate_sha256, str)
                or not isinstance(source_node, int)
                or not isinstance(target_node, int)
            ):
                continue
            key = (override_id, candidate_sha256, source_node, target_node)
            selected[key] = dict(raw)
    return [selected[key] for key in sorted(selected)]


def _planned_departure(hour: int, now: datetime, origin_region: str) -> datetime:
    local = now.astimezone(_REGION_TIMEZONES[origin_region])
    planned = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return planned + timedelta(days=1) if planned < local else planned


def optimize_journey(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    vehicle_type: str,
    hour: int,
    weather: int,
    now: datetime,
    origin_name: Optional[str] = None,
    destination_name: Optional[str] = None,
    route_preference: str = "BALANCED",
    schedule_verified_at: Optional[str] = None,
    approved_override_snapshot: Optional[ApprovedGraphOverrideSnapshot] = None,
) -> Dict[str, Any]:
    """Compose an SG/Batam local or cross-border journey."""
    del weather  # Cross-border traffic is not inferred from the Batam-only model.
    origin = (origin_lat, origin_lng)
    destination = (destination_lat, destination_lng)
    origin_region = location_region(*origin)
    destination_region = location_region(*destination)
    if origin_region is None or destination_region is None:
        raise ValueError("Both points must be within Singapore or Batam.")
    if origin == destination:
        raise ValueError("Origin and destination must be different locations.")
    profile = router.vehicle_profile(vehicle_type)
    preference = router.route_preference(route_preference)
    origin_display = origin_name or f"{origin_lat:.5f}, {origin_lng:.5f}"
    destination_display = destination_name or (
        f"{destination_lat:.5f}, {destination_lng:.5f}"
    )
    departure = _planned_departure(hour, now, origin_region)
    legs: List[Dict[str, Any]] = []
    next_ferries: List[Dict[str, Any]] = []
    ferry_note: Optional[str] = None
    terminal_wait_mins = 0.0

    if origin_region == destination_region:
        legs.append(_road_leg(
            origin_region, origin, destination,
            origin_display, destination_display,
            vehicle_type, preference.key,
            approved_override_snapshot,
        ))
        route_type = "ROAD_ROUTE"
    else:
        pair = _choose_terminal_pair(origin, destination, origin_region)
        if origin_region == "SINGAPORE":
            departure_terminal_name = pair.singapore_name
            departure_terminal_coords = pair.singapore_coords
            arrival_terminal_name = pair.batam_name
            arrival_terminal_coords = pair.batam_coords
        else:
            departure_terminal_name = pair.batam_name
            departure_terminal_coords = pair.batam_coords
            arrival_terminal_name = pair.singapore_name
            arrival_terminal_coords = pair.singapore_coords

        first_road = _road_leg(
            origin_region, origin, departure_terminal_coords,
            origin_display, departure_terminal_name,
            vehicle_type, preference.key,
            approved_override_snapshot,
        )
        terminal_arrival = departure + timedelta(minutes=float(first_road["duration_mins"]))
        ferry_leg, next_ferries, terminal_wait_mins = _ferry_leg(
            pair,
            origin_region,
            terminal_arrival,
            now,
            schedule_verified_at,
        )
        final_road = _road_leg(
            destination_region, arrival_terminal_coords, destination,
            arrival_terminal_name, destination_display,
            vehicle_type, preference.key,
            approved_override_snapshot,
        )
        first_road["vehicle_role"] = "FIRST_LAST_MILE_ACCESS"
        final_road["vehicle_role"] = "FIRST_LAST_MILE_ACCESS"
        ferry_leg["vehicle_carried_onboard"] = False
        legs.extend([first_road, ferry_leg, final_road])
        route_type = "MULTIMODAL_FERRY_ROUTE"
        if not next_ferries:
            ferry_note = ferry_leg["limitations"]

    geometry = _combine_geometry(legs)
    road_distance_km = round(sum(
        float(leg["distance_km"]) for leg in legs if leg["mode"] == "ROAD"
    ), 2)
    ferry_distance_km = round(sum(
        float(leg["distance_km"]) for leg in legs if leg["mode"] == "FERRY"
    ), 2)
    movement_mins = round(sum(float(leg["duration_mins"]) for leg in legs), 1)
    total_eta_mins = round(movement_mins + terminal_wait_mins, 1)
    estimated = any(
        bool(leg.get("is_estimate"))
        for leg in legs if leg["mode"] == "ROAD"
    )
    source = (
        "multimodal_offline_estimate" if estimated
        else "multimodal_openstreetmap_official_timetable"
    ) if route_type == "MULTIMODAL_FERRY_ROUTE" else (
        "offline_access_estimate" if estimated else "openstreetmap_osrm"
    )
    road_co2 = round(road_distance_km * profile.emissions_kg_per_km, 2)
    navigation = _combine_navigation(legs)

    result: Dict[str, Any] = {
        "route_type": route_type,
        "corridor": {
            "id": f"free:{origin_region.lower()}:{destination_region.lower()}",
            "name": f"{origin_display} → {destination_display}",
            "origin": "free-origin", "destination": "free-destination",
            "distance_km": round(road_distance_km + ferry_distance_km, 2),
            "base_time_mins": movement_mins,
            "straight_line_km": round(_haversine_km(origin, destination), 2),
            "detour_ratio": None,
        },
        "requested_origin": {
            "name": origin_display, "lat": origin_lat, "lng": origin_lng,
            "region": origin_region,
        },
        "requested_destination": {
            "name": destination_display,
            "lat": destination_lat, "lng": destination_lng,
            "region": destination_region,
        },
        "vehicle_type": profile.key,
        "vehicle_profile": router.vehicle_profile_payload(profile),
        "route_preference": preference.key,
        "route_preference_profile": router.route_preference_payload(preference),
        "planned_departure": departure.isoformat(timespec="seconds"),
        "congestion_prediction": {
            "current_score": 0, "predicted_30min": 0,
            "predicted_60min": 0, "estimated_delay_mins": 0,
            "status": "NOT_MODELLED", "risk_level": "UNKNOWN", "trend": "STABLE",
        },
        "estimated_travel_time_mins": movement_mins,
        "customs_buffer_mins": round(terminal_wait_mins, 1),
        "total_eta_mins": total_eta_mins,
        "road_distance_km": road_distance_km,
        "ferry_distance_km": ferry_distance_km,
        "co2_emissions_kg": road_co2,
        "co2_saved_kg": 0.0,
        "emissions_scope": (
            "Selected road legs only; ferry emissions are not estimated without "
            "vessel and occupancy data."
        ),
        "optimal_departure": {
            "recommended": "DEPART_NOW", "time_saved_mins": 0.0,
            "reason": (
                "Journey composed from available road routing and the published "
                "ferry corridor; no cross-border live-traffic forecast is asserted."
            ),
        },
        "route_geometry": geometry,
        "route_data_source": source,
        "route_legs": legs,
        "vehicle_transfer_policy": (
            "FIRST_LAST_MILE_ONLY"
            if route_type == "MULTIMODAL_FERRY_ROUTE" else "ROAD_JOURNEY"
        ),
        "vehicle_transfer_note": (
            "The selected vehicle profile applies only to first- and last-mile "
            "road access; it is not carried onboard the passenger ferry."
            if route_type == "MULTIMODAL_FERRY_ROUTE" else None
        ),
        "navigation": navigation,
        "alternative_routes": [],
        "alternatives_note": (
            "Terminal pair selected by estimated access time plus published "
            "crossing duration; road-provider alternatives are not mixed across modes."
            if route_type == "MULTIMODAL_FERRY_ROUTE" else
            "No sufficiently distinct local alternative was returned."
        ),
        "next_matching_ferries": next_ferries,
    }
    if ferry_note:
        result["ferry_connection_note"] = ferry_note
    approved_overrides_used = _approved_overrides_used_by_legs(legs)
    if approved_overrides_used:
        result["approved_graph_overrides_used"] = approved_overrides_used
    return result
