from __future__ import annotations

from unittest.mock import patch

from services import router


def test_alternative_request_reuses_cached_primary_search():
    router._route_between_nodes_cached.cache_clear()
    router._primary_paths_cached.cache_clear()
    source = router.LANDMARKS["mukakuning"]
    target = router.LANDMARKS["batam_centre"]
    with patch.object(
        router, "alternative_paths", return_value=[],
    ), patch.object(
        router, "astar_detailed", wraps=router.astar_detailed,
    ) as search:
        primary = router.route_between_nodes(
            source, target, include_alternatives=False,
        )
        first_count = search.call_count
        selected = router.route_between_nodes(
            source, target, include_alternatives=True,
        )
    assert primary is not None and selected is not None
    assert first_count == 1
    assert search.call_count == first_count
    router._route_between_nodes_cached.cache_clear()
    router._primary_paths_cached.cache_clear()

