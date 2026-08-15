from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import sys

# The service modules import each other as ``services.*``, so ``backend/`` must
# be on sys.path. Every test module in this package does this for itself: with
# ``discover -s backend/tests`` the directory is the top level, so no package
# __init__ runs first, and relying on an alphabetically earlier test to have
# inserted the path makes the suite order-dependent.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import router
from services.alt_index import AltIndex


def test_directed_alt_uses_both_triangle_inequality_forms():
    adjacency = {
        1: ((2, 2.0), (3, 20.0)),
        2: ((3, 2.0),),
        3: ((4, 2.0),),
        4: ((1, 30.0),),
    }
    index = AltIndex.build(
        adjacency,
        (1, 4),
        graph_revision="graph-a",
        topology_id="cars",
    )

    # The directed lower bound is never greater than the exact physical
    # shortest path. The reverse form is required for this asymmetric graph.
    assert index.distance_lower_bound(1, 4) <= 6.0
    assert index.distance_lower_bound(4, 3) <= 34.0
    assert index.distance_lower_bound(2, 4) <= 4.0


def test_alt_index_round_trip_validates_identity(tmp_path: Path):
    adjacency = {1: ((2, 3.0),), 2: ()}
    index = AltIndex.build(
        adjacency,
        (1,),
        graph_revision="graph-a",
        topology_id="cars",
    )
    path = tmp_path / "alt.npz"
    index.save(path)
    loaded = AltIndex.load(
        path,
        expected_graph_revision="graph-a",
        expected_topology_id="cars",
    )
    assert loaded.distance_lower_bound(1, 2) == 3.0


def test_runtime_alt_matches_haversine_objective():
    index = router._alt_index_for_profile("COMMUTER")
    assert index is not None
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    with patch.object(router, "_alt_index_for_profile", return_value=None):
        baseline = router.astar_detailed(
            source, target, vehicle_type="COMMUTER", route_preference="FASTEST",
        )
    accelerated = router.astar_detailed(
        source, target, vehicle_type="COMMUTER", route_preference="FASTEST",
    )
    assert baseline is not None and accelerated is not None
    assert accelerated.distance_m == baseline.distance_m
    assert abs(accelerated.search_cost_s - baseline.search_cost_s) < 1e-6
