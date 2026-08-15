"""Opt-in routing search counters."""

import sys
from pathlib import Path

# Required by `unittest discover -s backend/tests`, which makes this directory
# the top level and so runs neither a package init nor conftest.py. See
# tests/conftest.py for the full explanation.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import router


def test_diagnostics_are_disabled_without_an_explicit_context():
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    assert router._ACTIVE_SEARCH_DIAGNOSTICS.get() is None
    result = router.astar_detailed(source, target, vehicle_type="COMMUTER")
    assert result is not None


def test_opt_in_diagnostics_capture_primary_search_work():
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    with router.search_diagnostics() as diagnostics:
        result = router.astar_detailed(
            source, target, vehicle_type="COMMUTER", route_preference="FASTEST",
        )
    assert result is not None
    counters = diagnostics.as_dict()
    assert counters["searches_started"] == 1
    assert counters["searches_succeeded"] == 1
    assert counters["heap_pushes"] >= counters["heap_pops"] >= 1
    assert counters["settled_states"] >= 1
    assert counters["edge_cost_evaluations"] >= 1
    assert counters["heuristic_evaluations"] >= 1
    assert counters["alt_terms_evaluated"] >= 1
    assert "diagnostics" not in router.route_between(
        "mukakuning", "batam_centre", include_alternatives=False,
    )


def test_cached_route_does_not_report_a_new_search():
    router._route_between_nodes_cached.cache_clear()
    router._primary_paths_cached.cache_clear()
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    router.route_between_nodes(source, target, include_alternatives=False)
    with router.search_diagnostics() as diagnostics:
        result = router.route_between_nodes(
            source, target, include_alternatives=False,
        )
    assert result is not None
    assert diagnostics.as_dict()["searches_started"] == 0
    router._route_between_nodes_cached.cache_clear()
    router._primary_paths_cached.cache_clear()


def test_alternative_searches_are_counted_separately():
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    primary = router.astar_detailed(source, target, vehicle_type="COMMUTER")
    assert primary is not None and len(primary.edges) >= 4
    with router.search_diagnostics() as diagnostics:
        router.alternative_paths(
            primary,
            vehicle_type="COMMUTER",
            limit=1,
            max_searches=1,
            time_budget_ms=2_000,
            max_settled_states=10_000,
        )
    counters = diagnostics.as_dict()
    assert counters["alternative_attempts"] == 1
    assert counters["searches_started"] == 1
