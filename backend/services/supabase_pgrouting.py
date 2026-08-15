"""Supabase pgRouting Integration for CrossFlow AI.

Queries the PostGIS pgRouting stored procedure `get_shortest_route` on Supabase
via the Supabase REST RPC API (or direct connection). Returns exact street-by-street
LineString geometries and edge travel costs.
"""

import math
from typing import Any, Dict, List, Optional

from services import supabase_server

_USER_AGENT = "CrossFlowAI/3.0 (batam-singapore-hackathon-2026)"
_EARTH_RADIUS_M = 6371008.8

def get_supabase_config() -> Optional[Dict[str, str]]:
    """Compatibility mapping backed only by strict server-owned credentials."""
    config = supabase_server.load_server_config()
    if config is None:
        return None
    return {"url": config.origin, "key": config.secret_key}

def parse_geojson_geometry(geom: Any) -> List[List[float]]:
    """Convert PostGIS GeoJSON or LineString geometry to [[lat, lng], ...]."""
    coords: List[List[float]] = []
    if isinstance(geom, dict):
        raw_coords = geom.get("coordinates", [])
        if geom.get("type") == "LineString":
            for pt in raw_coords:
                if len(pt) >= 2:
                    coords.append([round(pt[1], 6), round(pt[0], 6)])
    elif isinstance(geom, str):
        # Fallback for WKT LINESTRING(lng lat, lng lat)
        if "LINESTRING" in geom.upper():
            try:
                inner = geom.split("(")[1].split(")")[0]
                pairs = inner.split(",")
                for p in pairs:
                    parts = p.strip().split()
                    if len(parts) >= 2:
                        lng, lat = float(parts[0]), float(parts[1])
                        coords.append([round(lat, 6), round(lng, 6)])
            except Exception:
                pass
    return coords


def geometry_length_m(geometry: List[List[float]]) -> float:
    """Return the great-circle length of a ``[[lat, lng], ...]`` polyline."""
    total = 0.0
    for start, end in zip(geometry, geometry[1:]):
        lat1, lng1 = math.radians(start[0]), math.radians(start[1])
        lat2, lng2 = math.radians(end[0]), math.radians(end[1])
        dlat = lat2 - lat1
        dlng = lng2 - lng1
        h = (
            math.sin(dlat / 2.0) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2.0) ** 2
        )
        total += 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))
    return total

def query_supabase_pgrouting(
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
    *,
    vehicle_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Execute get_shortest_route PostGIS pgRouting function on Supabase."""
    # The deployed legacy function has no per-mode access/clearance proof and
    # no complete navigation envelope. Never make a slow request that cannot
    # safely replace the local vehicle-aware route.
    if vehicle_type is not None:
        return None
    cfg = get_supabase_config()
    if not cfg:
        return None

    coordinates = (origin_lat, origin_lng, dest_lat, dest_lng)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in coordinates
    ):
        return None
    if not (-90 <= origin_lat <= 90 and -90 <= dest_lat <= 90):
        return None
    if not (-180 <= origin_lng <= 180 and -180 <= dest_lng <= 180):
        return None

    payload = {
        "start_lng": origin_lng,
        "start_lat": origin_lat,
        "end_lng": dest_lng,
        "end_lat": dest_lat,
    }

    try:
        config = supabase_server.SupabaseServerConfig(
            cfg["url"], cfg["key"],
        )
        data = supabase_server.request_json(
            config,
            method="POST",
            rest_path="/rest/v1/rpc/get_shortest_route",
            payload=payload,
            timeout_seconds=5,
        )

        if not data or not isinstance(data, list) or len(data) > 20_000:
            return None

        all_geometry: List[List[float]] = []
        total_cost_s = 0.0

        for row in data:
            if not isinstance(row, dict):
                return None
            cost_s = float(row.get("cost_s", 0.0))
            if not math.isfinite(cost_s) or cost_s < 0.0:
                return None
            total_cost_s += cost_s
            
            geom_pts = parse_geojson_geometry(row.get("geom"))
            for pt in geom_pts:
                if not all_geometry or all_geometry[-1] != pt:
                    all_geometry.append(pt)

        if not all_geometry or len(all_geometry) < 2:
            return None

        distance_km = round(geometry_length_m(all_geometry) / 1000.0, 2)
        duration_mins = round(max(1.0, total_cost_s / 60.0), 1)

        return {
            "distance_km": distance_km,
            "duration_mins": duration_mins,
            "geometry": all_geometry,
            "data_source": "supabase_pgrouting",
        }
    except (supabase_server.SupabaseServerError, KeyError, TypeError, ValueError):
        # Local OSM remains the explicitly documented critical-path fallback.
        # Do not log exception strings: some urllib errors include full URLs.
        return None
