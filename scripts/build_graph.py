"""Build a routable Batam road graph from OpenStreetMap.

Run once at build time; the output is committed so the demo never depends on
Overpass (or venue wifi) at runtime:

    python scripts/build_graph.py

Uses only the standard library so it adds no dependency to the project.
Overpass mirrors are frequently congested, so we rotate across them with
backoff rather than failing on the first timeout.
"""

import json
import hashlib
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Batam island plus margin; covers every landmark in MAP_NODES except the
# Singapore side (HarbourFront is reached by ferry, not road).
BBOX = (1.02, 103.88, 1.23, 104.17)  # south, west, north, east

# Landmarks that must be routable. Keys match MAP_NODES in the frontend.
#
# These were verified against OSM/Nominatim rather than trusted from the
# original hardcoded values, four of which were materially wrong — Muka Kuning
# was 1.6 km north of Batamindo Industrial Park, which put the busiest corridor
# in the demo on entirely the wrong roads.
LANDMARKS = {
    # amenity=ferry_terminal "Batam Centre"
    "batam_centre": (1.1318, 104.0554),
    # landuse=industrial "Batamindo Industrial Park" (was 1.0747,104.0326)
    "mukakuning": (1.0605, 104.0303),
    # highway=service "Pelabuhan Batu Ampar" (was 1.1558,103.9984)
    "batu_ampar": (1.1630, 104.0025),
    # Airport road access node (rather than the closer outbound-only service
    # lane, which cannot be reached from the rest of Batam).
    "hang_nadim": (1.121524, 104.113022),
    # Nagoya Hill commercial centre (was 1.1444,104.0090)
    "nagoya": (1.1465, 104.0125),
    # amenity=ferry_terminal "Sekupang Ferry Terminal" (was 1.1258,103.9318)
    "sekupang": (1.1250, 103.9250),
    "nongsa": (1.1822, 104.1030),
    "harbour_bay": (1.15396, 103.997234),
    "panbil_mall": (1.07210, 104.02355),
    "kabil_industrial": (1.094875, 104.118329),
    "batu_aji": (1.051, 103.965),
    "tiban": (1.099, 103.961),
    "kepri_mall": (1.101, 104.038),
}

DRIVABLE = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential"
    "|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"
    "|living_street|service|track"
)

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend", "data", "batam_graph.json",
)

GRAPH_SCHEMA_VERSION = 3

# Keep one explicit ordering so router.py can decode future fields while still
# accepting the four-field v2 artifact already committed to the repository.
ROAD_FIELDS = (
    "name", "ref", "highway", "junction", "access", "vehicle",
    "motor_vehicle", "motorcar", "motorcycle", "hgv", "ferry", "surface",
    "smoothness", "width", "maxweight", "maxheight", "service",
    "maxspeed", "lanes",
)


def build_query() -> str:
    south, west, north, east = BBOX
    return (
        f'[out:json][timeout:180];'
        f'way["highway"~"^({DRIVABLE})$"]({south},{west},{north},{east});'
        # Node tags are needed for sourced traffic-signal/stop metadata.  The
        # previous ``out skel`` discarded every referenced-node tag.
        f'out body;>;out body qt;'
    )


def fetch(max_rounds: int = 4) -> dict:
    """Query Overpass, rotating mirrors and backing off on congestion."""
    query = build_query()
    body = urllib.parse.urlencode({"data": query}).encode()
    last_err = None

    for round_no in range(max_rounds):
        for mirror in MIRRORS:
            try:
                print(f"  [{round_no + 1}/{max_rounds}] {mirror} ...", flush=True)
                req = urllib.request.Request(
                    mirror, data=body,
                    headers={"User-Agent": "CrossFlowAI-graph-builder/1.0"},
                )
                with urllib.request.urlopen(req, timeout=240) as resp:
                    raw = resp.read()
                # Congested mirrors return an HTML error page with a 200.
                if raw[:1] != b"{":
                    raise ValueError("non-JSON response (mirror busy)")
                data = json.loads(raw)
                if not data.get("elements"):
                    raise ValueError("empty element list")
                print(f"  -> {len(data['elements'])} elements", flush=True)
                return data
            except Exception as err:  # noqa: BLE001 - any failure means try next
                last_err = err
                print(f"     failed: {type(err).__name__}: {err}", flush=True)
        if round_no < max_rounds - 1:
            wait = 20 * (round_no + 1)
            print(f"  all mirrors busy; waiting {wait}s", flush=True)
            time.sleep(wait)

    raise SystemExit(f"Overpass unreachable after {max_rounds} rounds: {last_err}")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _oneway_direction(tags: dict) -> int:
    """Return 1 for forward, -1 for reverse, and 0 for both directions."""
    raw = str(tags.get("oneway", "")).strip().lower()
    if raw in ("-1", "reverse"):
        return -1
    if raw in ("yes", "true", "1"):
        return 1
    if raw in ("no", "false", "0"):
        return 0
    # OSM implies one-way travel for roundabouts/circular junctions and
    # motorways unless explicitly tagged otherwise.
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    if tags.get("highway") == "motorway":
        return 1
    return 0


def _compact_way_metadata(element: dict, direction: int) -> dict:
    tags = element.get("tags", {})
    metadata = {
        "name": tags.get("name"),
        "ref": tags.get("ref"),
        "highway": tags.get("highway"),
        "junction": tags.get("junction"),
        "destination": tags.get("destination"),
        "maxspeed": tags.get("maxspeed"),
        "lanes": tags.get("lanes"),
        "access": tags.get("access"),
        "vehicle": tags.get("vehicle"),
        "motor_vehicle": tags.get("motor_vehicle"),
        "motorcar": tags.get("motorcar"),
        "motorcycle": tags.get("motorcycle"),
        "hgv": tags.get("hgv"),
        "ferry": tags.get("ferry"),
        "surface": tags.get("surface"),
        "smoothness": tags.get("smoothness"),
        "width": tags.get("width"),
        "maxweight": tags.get("maxweight"),
        "maxheight": tags.get("maxheight"),
        "service": tags.get("service"),
        "oneway": direction,
    }
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _specific_mode_access(tags: dict, mode: str):
    """Return an explicit access decision for one supported motor mode."""
    specific = {
        "MOTORCYCLE": "motorcycle",
        "PASSENGER_CAR": "motorcar",
        "FREIGHT_TRUCK": "hgv",
    }[mode]
    for key in (specific, "motor_vehicle", "vehicle", "access"):
        raw = str(tags.get(key, "")).strip().lower()
        if raw in {"yes", "permissive", "designated", "official"}:
            return True
        if raw in {"no", "private"}:
            return False
    return None


def _allows_supported_vehicle(tags: dict) -> bool:
    """Return whether at least one supported vehicle mode may use a way.

    Preserve motorcycle-only and freight-specific ways for runtime profile
    filtering. A broad ban still removes the edge unless a more-specific mode
    explicitly grants public motor access.
    """
    values = {
        key: str(tags.get(key, "")).strip().lower()
        for key in (
            "motorcycle", "motorcar", "hgv", "motor_vehicle", "vehicle",
        )
    }
    allowed = {"yes", "permissive", "designated", "official"}
    explicit_public_allow = any(value in allowed for value in values.values())
    if explicit_public_allow:
        return True
    if values["motor_vehicle"] in {"no", "private"}:
        return False
    if values["vehicle"] in {"no", "private"}:
        return False
    access = str(tags.get("access", "")).strip().lower()
    if access in {"no", "private"}:
        return False
    # OSM track is predominantly agricultural/forestry access; absent a mode
    # grant it must never be assumed to be a public shortcut.
    if tags.get("highway") == "track":
        return False
    # These service subtypes are assumed-private or non-through facilities.
    # Ordinary alleys and generic service roads remain eligible.
    if tags.get("service") in {
        "driveway", "parking_aisle", "emergency_access",
    }:
        return False
    return True


def _node_allows_supported_vehicle(tags: dict) -> bool:
    """Keep a node when at least one supported mode can traverse it.

    Mode-specific filtering happens at runtime. This build-time union prevents
    a motorcycle=yes bollard from deleting a legitimate motorcycle connector
    while still dropping an unqualified physical barrier for every mode.
    """
    decisions = tuple(
        _specific_mode_access(tags, mode)
        for mode in ("MOTORCYCLE", "PASSENGER_CAR", "FREIGHT_TRUCK")
    )
    if any(decision is True for decision in decisions):
        return True
    if all(decision is False for decision in decisions):
        return False
    barrier = str(tags.get("barrier", "")).strip().lower()
    return barrier not in {
        "block", "bollard", "bus_trap", "cycle_barrier", "jersey_barrier",
        "sump_buster",
    }


def build_adjacency(data: dict):
    coords: dict[int, tuple[float, float]] = {}
    node_meta: dict[int, dict] = {}
    for el in data["elements"]:
        if el["type"] == "node":
            coords[el["id"]] = (el["lat"], el["lon"])
            tags = el.get("tags", {})
            metadata = {
                key: tags[key]
                for key in (
                    "highway", "crossing", "barrier", "access", "vehicle",
                    "motor_vehicle", "motorcar", "motorcycle", "hgv",
                )
                if tags.get(key)
            }
            if metadata:
                node_meta[el["id"]] = metadata

    blocked_nodes = {
        node_id for node_id, metadata in node_meta.items()
        if not _node_allows_supported_vehicle(metadata)
    }

    # Keep parallel arcs and their OSM way identity.  Collapsing by endpoint
    # loses which named/classified road A* actually chose.
    adj: dict[int, list[tuple[int, float, int]]] = {}
    ways: dict[int, dict] = {}

    def link(a: int, b: int, way_id: int) -> None:
        if a not in coords or b not in coords or a in blocked_nodes or b in blocked_nodes:
            return
        d = haversine_m(*coords[a], *coords[b])
        adj.setdefault(a, []).append((b, d, way_id))
        adj.setdefault(b, [])

    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        if tags.get("highway") not in DRIVABLE.split("|"):
            continue
        if not _allows_supported_vehicle(tags):
            continue
        direction = _oneway_direction(tags)
        ways[el["id"]] = _compact_way_metadata(el, direction)
        nodes = el.get("nodes", [])
        for a, b in zip(nodes, nodes[1:]):
            if direction == -1:
                link(b, a, el["id"])
            else:
                link(a, b, el["id"])
                if direction == 0:
                    link(b, a, el["id"])

    return coords, adj, ways, node_meta


def largest_strongly_connected_component(
    adj: dict[int, list[tuple[int, float, int]]],
) -> set[int]:
    """Return the largest set whose nodes are mutually reachable."""
    nodes = set(adj)
    reverse: dict[int, list[int]] = {node: [] for node in nodes}
    for source, edges in adj.items():
        for target, _, _ in edges:
            nodes.add(target)
            reverse.setdefault(target, []).append(source)

    # Iterative Kosaraju avoids Python's recursion limit on the 84k-node graph.
    seen: set[int] = set()
    order: list[int] = []
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, 0)]
        while stack:
            node, edge_index = stack[-1]
            edges = adj.get(node, ())
            if edge_index < len(edges):
                target = edges[edge_index][0]
                stack[-1] = (node, edge_index + 1)
                if target not in seen:
                    seen.add(target)
                    stack.append((target, 0))
            else:
                order.append(node)
                stack.pop()

    assigned: set[int] = set()
    best: set[int] = set()
    for start in reversed(order):
        if start in assigned:
            continue
        component: set[int] = set()
        stack = [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.add(node)
            for previous in reverse.get(node, ()):
                if previous not in assigned:
                    assigned.add(previous)
                    stack.append(previous)
        if len(component) > len(best):
            best = component
    return best


def snap(coords, keep: set[int], lat: float, lng: float):
    best_id, best_d = None, float("inf")
    for nid in keep:
        d = haversine_m(lat, lng, *coords[nid])
        if d < best_d:
            best_id, best_d = nid, d
    return best_id, best_d


def serialized_edge_distance_m(
    serialized_nodes: dict[int, tuple[float, float]], source: int, target: int,
) -> float:
    """Encode an edge without understating the serialized-node heuristic."""
    distance = haversine_m(*serialized_nodes[source], *serialized_nodes[target])
    return max(0.1, math.ceil(distance * 10.0) / 10.0)


def main() -> int:
    print("Fetching Batam drivable network from OpenStreetMap...")
    data = fetch()

    print("Building adjacency...")
    coords, adj, ways, node_meta = build_adjacency(data)
    print(f"  raw: {len(adj)} nodes")

    keep = largest_strongly_connected_component(adj)
    dropped = len(adj) - len(keep)
    print(f"  largest component: {len(keep)} nodes ({dropped} dropped as disconnected)")

    print("Snapping landmarks...")
    landmarks = {}
    worst = 0.0
    for name, (lat, lng) in LANDMARKS.items():
        nid, dist = snap(coords, keep, lat, lng)
        landmarks[name] = nid
        worst = max(worst, dist)
        flag = "  <-- CHECK" if dist > 200 else ""
        print(f"  {name:<14} -> node {nid} ({dist:.0f} m){flag}")

    used_way_ids = {
        way_id
        for node in keep
        for target, _, way_id in adj[node]
        if target in keep
    }
    road_keys = sorted({
        tuple(ways[way_id].get(field) for field in ROAD_FIELDS)
        for way_id in used_way_ids
    }, key=lambda road: tuple(value or "" for value in road))
    road_index = {road: index for index, road in enumerate(road_keys)}
    way_road_index = {
        way_id: road_index[
            tuple(ways[way_id].get(field) for field in ROAD_FIELDS)
        ]
        for way_id in used_way_ids
    }
    query = build_query()
    serialized_nodes = {
        node: (round(coords[node][0], 6), round(coords[node][1], 6))
        for node in keep
    }
    all_lats = [point[0] for point in serialized_nodes.values()]
    all_lngs = [point[1] for point in serialized_nodes.values()]

    out = {
        "meta": {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "builder_version": "3.0",
            "source": "OpenStreetMap via Overpass API",
            "license": "ODbL 1.0",
            "bbox": list(BBOX),
            "actual_bounds": [min(all_lats), min(all_lngs), max(all_lats), max(all_lngs)],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "osm_base_timestamp": data.get("osm3s", {}).get("timestamp_osm_base"),
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "node_count": len(keep),
            "edge_count": sum(
                1 for node in keep for target, _, _ in adj[node] if target in keep
            ),
            "way_count": len(used_way_ids),
            "road_count": len(road_keys),
            "road_fields": list(ROAD_FIELDS),
            # This artifact is a union retention core. Runtime routing computes
            # a separate strongly-connected snap core for each vehicle after
            # applying mode/access/clearance constraints.
            "mutually_reachable": False,
            "connectivity_scope": "union_retention_core_only",
            "runtime_vehicle_cores_required": True,
            "turn_restrictions_included": False,
            "max_snap_distance_m": round(worst, 1),
        },
        # Round to ~0.1 m; full float precision doubles the file for no gain.
        "nodes": {
            str(node): list(serialized_nodes[node])
            for node in sorted(keep)
        },
        "adj": {
            str(n): [
                [str(m), serialized_edge_distance_m(serialized_nodes, n, m), way_road_index[way_id]]
                for m, _, way_id in adj[n] if m in keep
            ]
            for n in sorted(keep)
        },
        "roads": [list(road) for road in road_keys],
        "node_meta": {
            str(node_id): metadata
            for node_id, metadata in sorted(node_meta.items())
            if node_id in keep
        },
        "landmarks": {k: str(v) for k, v in landmarks.items()},
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))

    size_mb = os.path.getsize(OUT_PATH) / 1e6
    print(f"\nWrote {OUT_PATH} ({size_mb:.1f} MB)")
    if worst > 200:
        print(f"WARNING: worst landmark snap is {worst:.0f} m — verify on a map.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
