from __future__ import annotations

from unittest.mock import patch

import sys
from pathlib import Path

# The service modules import each other as ``services.*``, so ``backend/`` must
# be on sys.path. Every test module in this package does this for itself: with
# ``discover -s backend/tests`` the directory is the top level, so no package
# __init__ runs first, and relying on an alphabetically earlier test to have
# inserted the path makes the suite order-dependent.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import router


def _path(nodes: list[int]) -> router.PathResult:
    edges = [
        router.RoadEdge(target, 100.0, road_index=index)
        for index, target in enumerate(nodes[1:], start=1)
    ]
    return router.PathResult(
        nodes=nodes,
        edges=edges,
        distance_m=float(len(edges) * 100),
        search_cost_s=float(len(edges)),
    )


def _flat_quality(*args, **kwargs):
    return {"generalized_cost_s": 1.0}


def _flat_navigation(*args, **kwargs):
    return {"maneuvers": []}


def test_alternatives_respect_max_searches_and_forward_deadline():
    primary = _path([1, 2, 3, 4, 5])
    candidate = _path([1, 2, 8, 9, 5])
    calls = []

    def fake_astar(*args, **kwargs):
        calls.append(kwargs)
        return candidate

    with patch.object(router, "astar_detailed", side_effect=fake_astar), \
            patch.object(router, "path_cost_breakdown", side_effect=_flat_quality), \
            patch.object(router, "generate_navigation", side_effect=_flat_navigation):
        alternatives = router.alternative_paths(
            primary,
            limit=2,
            max_searches=1,
            time_budget_ms=5_000,
            max_settled_states=100,
        )

    assert len(alternatives) == 1
    assert len(calls) == 1
    assert calls[0]["max_settled_states"] == 100
    assert calls[0]["deadline_monotonic"] > 0


def test_alternatives_stop_when_budget_is_already_expired():
    primary = _path([1, 2, 3, 4, 5])
    with patch.object(router, "monotonic", return_value=100.0), \
            patch.object(router, "astar_detailed") as search, \
            patch.object(router, "generate_navigation", side_effect=_flat_navigation), \
            patch.object(router, "path_cost_breakdown", side_effect=_flat_quality):
        alternatives = router.alternative_paths(
            primary,
            max_searches=3,
            time_budget_ms=0,
        )

    assert alternatives == []
    search.assert_not_called()


def test_similarity_rejection_stops_optional_searches_after_two_attempts():
    primary = _path(list(range(1, 15)))
    # Each candidate is distinct but shares over 82% of its physical metres
    # with the accepted primary route.
    candidates = [
        _path([*range(1, 14), 15, 14]),
        _path([*range(1, 14), 16, 14]),
        _path([*range(1, 14), 17, 14]),
    ]
    calls = []

    def fake_astar(*args, **kwargs):
        calls.append(kwargs)
        return candidates[len(calls) - 1]

    with patch.object(router, "astar_detailed", side_effect=fake_astar), \
            patch.object(router, "path_cost_breakdown", side_effect=_flat_quality), \
            patch.object(router, "generate_navigation", side_effect=_flat_navigation):
        alternatives = router.alternative_paths(
            primary,
            limit=3,
            max_searches=7,
            time_budget_ms=5_000,
            max_settled_states=100,
        )

    assert alternatives == []
    assert len(calls) == 2


def test_route_cache_separates_alternative_budget_variants():
    router._route_between_nodes_cached.cache_clear()
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    with patch.object(router, "alternative_paths", return_value=[]):
        first = router.route_between_nodes(
            source,
            target,
            include_alternatives=True,
            alternative_max_searches=1,
        )
        second = router.route_between_nodes(
            source,
            target,
            include_alternatives=True,
            alternative_max_searches=2,
        )
    assert first is not None and second is not None
    info = router._route_between_nodes_cached.cache_info()
    assert info.misses == 2
    router._route_between_nodes_cached.cache_clear()
