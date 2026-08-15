"""Geocoding service for the CrossFlow AI free-form route planner.

Uses Nominatim (OpenStreetMap's public geocoder) to resolve places in Singapore
and Batam. Batam results are also snapped to the committed OSM graph; Singapore
results are handed to the multimodal road provider at route time.

Usage policy:
  - Maximum 1 request/second (enforced via a per-process rate limiter).
  - User-Agent header identifies this application.
  - Results are cached in memory (LRU, 512 entries, 1-hour TTL) to minimise
    repeat queries during a demo session.

No API key is required.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from services import multimodal_router, router, tls

# Corridor bounding box covering Singapore and Batam. Results outside the
# supported island classifier are retained with ``supported_region = None`` so
# callers never silently snap an unrelated city onto Batam's graph.
_BBOX_VIEWBOX = "103.55,0.88,104.30,1.50"   # west,south,east,north for Nominatim
_BBOX_BOUNDED = "1"
# A result farther than this from a routable Batam road is outside the service
# area. Preserve the measured distance, but do not manufacture a graph node.
_MAX_GRAPH_SNAP_M = 1000.0

_NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "CrossFlowAI/2.0 (batam-singapore-hackathon-2026)"

# Minimum seconds between outbound Nominatim requests.
_RATE_LIMIT_S = 1.1
_last_request_at: float = 0.0
_request_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class _TTLCache:
    """Simple dict-backed cache with per-entry expiry."""

    def __init__(self, maxsize: int = 512, ttl_s: float = 3600.0):
        self._store: Dict[str, Any] = {}
        self._expiry: Dict[str, float] = {}
        self._maxsize = maxsize
        self._ttl = ttl_s
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._store:
                return None
            if time.monotonic() > self._expiry[key]:
                del self._store[key], self._expiry[key]
                return None
            return self._store[key]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self._maxsize:
                # Evict the oldest entry.
                oldest = min(self._expiry, key=self._expiry.get)  # type: ignore[arg-type]
                del self._store[oldest], self._expiry[oldest]
            self._store[key] = value
            self._expiry[key] = time.monotonic() + self._ttl


_cache = _TTLCache()


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

def _nominatim_get(url: str) -> Optional[Any]:
    """Fetch a Nominatim URL with rate limiting, returning parsed JSON or None."""
    global _last_request_at  # noqa: PLW0603
    # FastAPI executes sync endpoints in a thread pool. Serialize the complete
    # delay/request sequence so two simultaneous explicit searches cannot both
    # pass the one-request-per-second gate.
    with _request_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _RATE_LIMIT_S:
            time.sleep(_RATE_LIMIT_S - elapsed)

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            )
            _last_request_at = time.monotonic()
            with urllib.request.urlopen(
                req, timeout=5, context=tls.default_context(),
            ) as resp:
                return json.loads(resp.read())
        except Exception as err:  # noqa: BLE001
            print(f"[geocoder] Nominatim unreachable: {err}")
            return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def geocode(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Resolve a free-text query within the Singapore-Batam corridor."""
    cache_key = f"geo:{query.lower().strip()}:{limit}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": limit,
        "bounded": _BBOX_BOUNDED,
        "viewbox": _BBOX_VIEWBOX,
        "addressdetails": "0",
        "extratags": "0",
    })
    url = f"{_NOMINATIM_BASE}?{params}"
    raw = _nominatim_get(url)

    if not raw:
        result: List[Dict[str, Any]] = []
        _cache.set(cache_key, result)
        return result

    out = []
    for item in raw:
        try:
            lat = float(item["lat"])
            lng = float(item["lon"])
        except (KeyError, ValueError):
            continue

        region = multimodal_router.location_region(lat, lng)
        node_id, snap_m = router.snap_to_graph(lat, lng)
        if (
            region == "BATAM" and node_id is not None
            and snap_m <= _MAX_GRAPH_SNAP_M
        ):
            snapped_lat, snapped_lng = router.NODES[node_id]
        else:
            snapped_lat, snapped_lng = lat, lng
            node_id = None

        out.append({
            "display_name": item.get("display_name", f"{lat:.5f}, {lng:.5f}"),
            "type": item.get("type", "place"),
            "lat": lat,
            "lng": lng,
            "node_id": node_id,
            "snap_distance_m": round(snap_m, 1),
            "snapped_lat": round(snapped_lat, 6),
            "snapped_lng": round(snapped_lng, 6),
            "importance": float(item.get("importance", 0)),
            "supported_region": region,
        })

    out.sort(key=lambda r: r["importance"], reverse=True)
    _cache.set(cache_key, out)
    return out



def reverse_geocode(lat: float, lng: float) -> Dict[str, Any]:
    """Return the display name and optional Batam graph node for a coordinate.

    Falls back gracefully: if Nominatim is unreachable, returns a placeholder
    name derived from the coordinates so the UI can still display something.
    """
    cache_key = f"rev:{lat:.5f},{lng:.5f}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    region = multimodal_router.location_region(lat, lng)
    node_id, snap_m = router.snap_to_graph(lat, lng)
    if (
        region == "BATAM" and node_id is not None
        and snap_m <= _MAX_GRAPH_SNAP_M
    ):
        snapped_lat, snapped_lng = router.NODES[node_id]
    else:
        snapped_lat, snapped_lng = lat, lng
        node_id = None

    params = urllib.parse.urlencode({
        "lat": lat, "lon": lng,
        "format": "jsonv2",
        "zoom": "16",
        "addressdetails": "0",
    })
    url = f"{_NOMINATIM_REVERSE}?{params}"
    raw = _nominatim_get(url)

    display = (
        raw.get("display_name", f"{lat:.5f}, {lng:.5f}")
        if isinstance(raw, dict) else f"{lat:.5f}, {lng:.5f}"
    )

    result = {
        "display_name": display,
        "lat": lat, "lng": lng,
        "node_id": node_id,
        "snap_distance_m": round(snap_m, 1),
        "snapped_lat": round(snapped_lat, 6),
        "snapped_lng": round(snapped_lng, 6),
        "supported_region": region,
    }
    _cache.set(cache_key, result)
    return result
