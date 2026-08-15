"""Build revision-bound directed ALT indexes for the committed Batam graph.

Run from the repository root, for example:

    python scripts/build_alt_index.py --output-dir backend/data/alt

The builder groups API profiles only when their filtered directed topology is
identical. Objective scaling (maximum speed and preference coefficient) stays
in the runtime router.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import router
from services.alt_index import AltIndex


def _filtered_adjacency(vehicle_type: str) -> dict[int, tuple[tuple[int, float], ...]]:
    profile = router.vehicle_profile(vehicle_type)
    core = router._main_routing_core(profile.key)
    core_set = set(core)
    return {
        source: tuple(
            (edge.target, float(edge.distance_m))
            for edge in router.ROAD_ADJ.get(source, ())
            if edge.target in core_set
            and router.edge_traversal_decision(edge, profile).allowed
            and router.node_traversal_decision(edge.target, profile).allowed
        )
        for source in sorted(core_set)
    }


def _topology_id(adjacency: Mapping[int, Sequence[tuple[int, float]]]) -> str:
    digest = hashlib.sha256()
    for source in sorted(adjacency):
        digest.update(str(source).encode("ascii"))
        for target, distance in adjacency[source]:
            digest.update(f"{target}:{distance:.9f}".encode("ascii"))
    return digest.hexdigest()[:20]


def _select_landmarks(
    adjacency: Mapping[int, Sequence[tuple[int, float]]],
    count: int,
) -> tuple[int, ...]:
    nodes = tuple(sorted(adjacency))
    if count <= 0:
        raise ValueError("landmark count must be positive")
    if count >= len(nodes):
        return nodes
    anchor = router.LANDMARKS.get("batam_centre")
    selected = [anchor if anchor in adjacency else nodes[0]]
    while len(selected) < count:
        candidate = max(
            (node for node in nodes if node not in selected),
            key=lambda node: (
                min(
                    router.haversine_m(router.NODES[node], router.NODES[chosen])
                    for chosen in selected
                ),
                -node,
            ),
        )
        selected.append(candidate)
    return tuple(selected)


def build_indexes(
    output_dir: Path,
    *,
    landmark_count: int,
) -> dict:
    grouped: dict[str, tuple[dict[int, tuple[tuple[int, float], ...]], list[str]]] = {}
    for profile_key in sorted(router.VEHICLE_PROFILES):
        adjacency = _filtered_adjacency(profile_key)
        topology_id = _topology_id(adjacency)
        if topology_id not in grouped:
            grouped[topology_id] = (adjacency, [])
        grouped[topology_id][1].append(profile_key)

    output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for topology_id, (adjacency, profiles) in sorted(grouped.items()):
        landmarks = _select_landmarks(adjacency, landmark_count)
        index = AltIndex.build(
            adjacency,
            landmarks,
            graph_revision=router.GRAPH_REVISION,
            topology_id=topology_id,
        )
        filename = f"alt-{topology_id}.npz"
        index.save(output_dir / filename)
        entries.append({
            "topology_id": topology_id,
            "profiles": profiles,
            "landmarks": list(landmarks),
            "node_count": len(adjacency),
            "edge_count": sum(len(edges) for edges in adjacency.values()),
            "path": filename,
        })

    manifest = {
        "format_version": 1,
        "graph_revision": router.GRAPH_REVISION,
        "graph_schema_version": router.GRAPH_META.get("schema_version"),
        "landmark_count": landmark_count,
        "indexes": entries,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("backend/data/alt"),
    )
    parser.add_argument("--landmarks", type=int, default=8)
    args = parser.parse_args()
    manifest = build_indexes(args.output_dir, landmark_count=args.landmarks)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
