"""Google Maps Directions response parser retained for isolated evaluation.

Queries Google Maps Directions API when a Google Maps API Key is configured in
environment variables (GOOGLE_MAPS_API_KEY, GOOGLE_MAPS_KEY, or GOOGLE_API_KEY).
Decodes the encoded polyline into high-precision street-level [lat, lng] coordinates.

The production route solver does not call this adapter: Google Directions
geometry cannot be rendered on CrossFlow's CARTO/OpenStreetMap map under
Google's display policy. Runtime routing therefore remains on the OSM graph.
"""

import json
import html
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_USER_AGENT = "CrossFlowAI/3.0 (batam-singapore-hackathon-2026)"

def get_api_key() -> str:
    return (
        os.environ.get("GOOGLE_MAPS_API_KEY", "") or
        os.environ.get("GOOGLE_MAPS_KEY", "") or
        os.environ.get("GOOGLE_API_KEY", "")
    ).strip()

def decode_polyline(polyline_str: str) -> List[List[float]]:
    """Decode Google Maps Encoded Polyline algorithm into [[lat, lng], ...]."""
    index, lat, lng = 0, 0, 0
    coordinates: List[List[float]] = []
    length = len(polyline_str)

    while index < length:
        # Decode latitude
        shift, result = 0, 0
        while True:
            if index >= length:
                break
            byte = ord(polyline_str[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        # Decode longitude
        shift, result = 0, 0
        while True:
            if index >= length:
                break
            byte = ord(polyline_str[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coordinates.append([round(lat / 1e5, 6), round(lng / 1e5, 6)])

    return coordinates


def _plain_instruction(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _normalized_google_maneuver(value: Any) -> tuple[str, str, str]:
    raw = str(value or "straight").strip().lower().replace("-", "_")
    mapped = {
        "turn_left": ("TURN_LEFT", "left", "turn_left"),
        "turn_right": ("TURN_RIGHT", "right", "turn_right"),
        "turn_slight_left": ("SLIGHT_LEFT", "slight_left", "turn_left"),
        "turn_slight_right": ("SLIGHT_RIGHT", "slight_right", "turn_right"),
        "turn_sharp_left": ("SHARP_LEFT", "sharp_left", "turn_left"),
        "turn_sharp_right": ("SHARP_RIGHT", "sharp_right", "turn_right"),
        "uturn_left": ("U_TURN", "u_turn", "u_turn"),
        "uturn_right": ("U_TURN", "u_turn", "u_turn"),
        "ramp_left": ("TAKE_RAMP", "left", "turn_left"),
        "ramp_right": ("TAKE_RAMP", "right", "turn_right"),
        "fork_left": ("SLIGHT_LEFT", "slight_left", "turn_left"),
        "fork_right": ("SLIGHT_RIGHT", "slight_right", "turn_right"),
        "roundabout_left": ("ROUNDABOUT", "roundabout", "roundabout"),
        "roundabout_right": ("ROUNDABOUT", "roundabout", "roundabout"),
        "merge": ("MERGE", "merge", "straight"),
        "straight": ("CONTINUE", "straight", "straight"),
    }
    return mapped.get(raw, (raw.upper(), raw, "straight"))


def _google_navigation(
    leg: Dict[str, Any], destination_name: str,
) -> Optional[Dict[str, Any]]:
    raw_steps = leg.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    maneuvers = []
    cumulative = 0.0
    for index, step in enumerate(raw_steps, start=1):
        raw_instruction = str(step.get("html_instructions", "Continue"))
        bold_values = re.findall(r"<b>(.*?)</b>", raw_instruction, flags=re.IGNORECASE)
        street = _plain_instruction(bold_values[-1]) if bold_values else "Road"
        maneuver_type, modifier, icon = _normalized_google_maneuver(
            step.get("maneuver"),
        )
        distance_m = float(step.get("distance", {}).get("value", 0.0))
        start = step.get("start_location", {})
        coords = [float(start.get("lat", 0.0)), float(start.get("lng", 0.0))]
        maneuvers.append({
            "step": index,
            "type": maneuver_type,
            "modifier": modifier,
            "instruction": _plain_instruction(raw_instruction),
            "street": street,
            "road_ref": None,
            "distance_m": round(distance_m),
            "cumulative_distance_m": round(cumulative),
            "icon": icon,
            "coords": coords,
            "bearing_before": None,
            "bearing_after": None,
            "landmark": None,
        })
        cumulative += distance_m
    end = leg.get("end_location", {})
    maneuvers.append({
        "step": len(maneuvers) + 1,
        "type": "ARRIVE",
        "modifier": "arrive",
        "instruction": f"Arrive at {destination_name}",
        "street": destination_name,
        "road_ref": None,
        "distance_m": 0,
        "cumulative_distance_m": round(cumulative),
        "icon": "arrive",
        "coords": [float(end.get("lat", 0.0)), float(end.get("lng", 0.0))],
        "bearing_before": None,
        "bearing_after": None,
        "landmark": destination_name,
    })
    return {
        "schema_version": 1,
        "data_source": "google_maps_directions_api",
        "maneuvers": maneuvers,
        "landmarks_along_route": [],
        "traffic_lights_count": 0,
        "route_narrative_words": " ".join(
            maneuver["instruction"] + "." for maneuver in maneuvers
        ),
    }


def _parse_google_route(route: Dict[str, Any], fallback_origin: str, fallback_destination: str) -> Optional[Dict[str, Any]]:
    legs = route.get("legs")
    if not isinstance(legs, list) or not legs:
        return None
    leg = legs[0]
    overview_polyline = route.get("overview_polyline", {}).get("points", "")
    geometry = decode_polyline(overview_polyline) if overview_polyline else []
    end_address = leg.get("end_address", fallback_destination)
    navigation = _google_navigation(leg, end_address)
    if len(geometry) < 2 or navigation is None:
        return None
    return {
        "distance_km": round(leg.get("distance", {}).get("value", 0) / 1000.0, 2),
        "duration_mins": round(leg.get("duration", {}).get("value", 0) / 60.0, 1),
        "geometry": geometry,
        "navigation": navigation,
        "start_address": leg.get("start_address", fallback_origin),
        "end_address": end_address,
        "data_source": "google_maps_directions_api",
    }

def get_google_route(origin: str, destination: str) -> Optional[Dict[str, Any]]:
    """Query Google Maps Directions API for driving route between origin and destination."""
    key = get_api_key()
    if not key:
        return None

    url = (
        "https://maps.googleapis.com/maps/api/directions/json"
        f"?origin={urllib.parse.quote(origin)}&destination={urllib.parse.quote(destination)}"
        f"&mode=driving&alternatives=true&key={key}"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "OK" or not data.get("routes"):
            print(f"[google_maps] Status: {data.get('status')}, Error: {data.get('error_message')}")
            return None

        parsed = [
            candidate
            for raw_route in data["routes"]
            if (candidate := _parse_google_route(raw_route, origin, destination)) is not None
        ]
        if not parsed:
            return None
        primary = parsed[0]
        primary["alternatives"] = [
            {
                "id": f"google-alternative-{index}",
                "name": f"Google Maps alternative {index}",
                "description": "Alternative returned by the Google Maps Directions API.",
                **alternative,
            }
            for index, alternative in enumerate(parsed[1:3], start=1)
        ]
        return primary
    except Exception as err:
        print(f"[google_maps] Unreachable/Failed: {err}")
        return None
