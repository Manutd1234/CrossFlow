"""Backend verification suite.

Plain asserts with a pass/fail runner — no pytest dependency required. Run:

    .venv/bin/python backend/test_backend.py

Time is controlled by injecting `now=` (every public function accepts it) or
via clock.frozen() for code that reads the clock itself. Frozen datetimes carry
an explicit +07:00 so the suite passes regardless of the laptop's timezone.
"""

import json
import math
import os
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Response
from pydantic import ValidationError

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
sys.path.append(BACKEND_DIR)
sys.path.append(PROJECT_DIR)

# Importing the API initializes its process-local history store. Point that
# singleton at a disposable suite database before any service imports so this
# verification file can never seed or mutate backend/data/congestion_history.db.
_TEST_HISTORY_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="crossflow-history-tests-",
)
os.environ["CROSSFLOW_HISTORY_DB"] = os.path.join(
    _TEST_HISTORY_DIRECTORY.name,
    "suite_history.db",
)

from models.congestion_model import (  # noqa: E402
    classify_status, delay_from_score, forecaster, history_training_provenance,
    risk_from_status,
)
from services import (  # noqa: E402
    clock, drift, ferry_freshness_store, ferry_refresh, ferry_schedule,
    geocoder, google_maps_router,
    google_routes_benchmark, historical_store, live_traffic, multimodal_router,
    route_learning_store as learning_store, router, route_solver,
    supabase_pgrouting, tls,
)
from scripts import build_graph  # noqa: E402
from services.route_solver import (  # noqa: E402
    CORRIDORS, ROUTE_LOCATIONS, optimize_free_route, optimize_route,
)
from services.simulator import (  # noqa: E402
    BOTTLENECK_THRESHOLD, build_alerts, co2_accrued_today,
    get_live_corridor_telemetry, get_operations_summary,
)
import main as api_main  # noqa: E402

TZ = clock.BATAM_TZ
WEEKDAY_1400 = datetime(2026, 8, 7, 14, 0, tzinfo=TZ)   # Friday
WEEKEND_1800 = datetime(2026, 8, 8, 18, 0, tzinfo=TZ)   # Saturday
LATE_NIGHT = datetime(2026, 8, 7, 23, 50, tzinfo=TZ)
EVENING = datetime(2026, 8, 7, 18, 0, tzinfo=TZ)


# --------------------------------------------------------------------------
# Routing over the real OSM graph
# --------------------------------------------------------------------------

def test_astar_paths_are_real():
    for corridor in CORRIDORS:
        route = router.route_between(corridor["origin"], corridor["destination"])
        assert route, f"no route for {corridor['id']}"

        src = router.LANDMARKS[corridor["origin"]]
        dst = router.LANDMARKS[corridor["destination"]]
        path, metres = router.astar(src, dst)

        assert path[0] == src and path[-1] == dst, "path does not join the landmarks"

        # Every consecutive pair must be a real edge, or we are drawing a line
        # the road network does not support.
        for a, b in zip(path, path[1:]):
            assert b in {n for n, _ in router.ADJ[a]}, f"{a}->{b} is not an edge"

        # A ratio near 1.0 would mean we are cutting straight across country.
        assert 1.05 <= route["detour_ratio"] <= 3.0, (
            f"{corridor['id']} detour ratio {route['detour_ratio']} implausible")
        assert abs(metres / 1000.0 - route["distance_km"]) < 0.01
        assert len(route["geometry"]) >= 2


def test_graph_landmarks_snap_close():
    assert router.GRAPH_META["schema_version"] == 3
    assert router.GRAPH_META["mutually_reachable"] is False
    assert router.GRAPH_META["connectivity_scope"] == "union_retention_core_only"
    assert router.GRAPH_META["runtime_vehicle_cores_required"] is True
    assert router.GRAPH_META["max_snap_distance_m"] <= 300, (
        "a landmark snapped too far from its real location")


def test_v3_graph_edges_preserve_metadata_and_admissibility():
    """Stored edge lengths must dominate the same serialized coordinates."""
    with open(router._GRAPH_PATH) as graph_file:
        graph = json.load(graph_file)

    assert graph["meta"]["schema_version"] == 3
    assert graph["meta"]["road_fields"] == list(build_graph.ROAD_FIELDS)
    assert graph["meta"]["generated_at"]
    assert graph["meta"]["osm_base_timestamp"]
    assert len(graph["meta"]["query_sha256"]) == 64
    assert graph["roads"] and all(
        len(road) == len(build_graph.ROAD_FIELDS) for road in graph["roads"]
    )
    assert graph["meta"]["road_count"] == len(graph["roads"])
    assert router.LANDMARKS == {
        key: int(node_id) for key, node_id in graph["landmarks"].items()
    }
    assert "6503421953" not in graph["nodes"], "known bollard remained routable"
    assert any(
        metadata.get("highway") == "traffic_signals"
        for metadata in graph["node_meta"].values()
    )
    checked = 0
    for source, records in graph["adj"].items():
        source_point = tuple(graph["nodes"][source])
        for target, stored_metres, road_index in records:
            target_point = tuple(graph["nodes"][target])
            assert stored_metres + 1e-9 >= router.haversine_m(
                source_point, target_point,
            ), f"edge {source}->{target} understates its heuristic"
            assert 0 <= road_index < len(graph["roads"])
            checked += 1
    assert checked == graph["meta"]["edge_count"]


def test_graph_builder_direction_access_and_parallel_edges():
    nodes = [
        {"type": "node", "id": node, "lat": 1.0, "lon": 104.0 + node / 1000}
        for node in range(1, 11)
    ]
    nodes[8]["tags"] = {"barrier": "bollard"}
    ways = [
        {"type": "way", "id": 10, "nodes": [1, 2],
         "tags": {"highway": "primary", "oneway": "yes", "name": "Forward"}},
        {"type": "way", "id": 11, "nodes": [2, 3],
         "tags": {"highway": "primary", "oneway": "-1", "name": "Reverse"}},
        {"type": "way", "id": 12, "nodes": [3, 4],
         "tags": {"highway": "primary", "junction": "roundabout"}},
        # A second legal parallel way must not overwrite the first.
        {"type": "way", "id": 13, "nodes": [1, 2],
         "tags": {"highway": "secondary", "name": "Parallel"}},
        {"type": "way", "id": 14, "nodes": [5, 6],
         "tags": {"highway": "residential", "access": "private"}},
        {"type": "way", "id": 15, "nodes": [6, 7],
         "tags": {"highway": "residential", "access": "private",
                  "motor_vehicle": "permissive"}},
        {"type": "way", "id": 16, "nodes": [7, 8],
         "tags": {"highway": "residential", "motor_vehicle": "no"}},
        {"type": "way", "id": 17, "nodes": [9, 10],
         "tags": {"highway": "residential"}},
        {"type": "way", "id": 18, "nodes": [8, 10],
         "tags": {"highway": "residential", "motor_vehicle": "yes",
                  "motorcar": "no"}},
    ]
    _coords, adj, metadata, _node_meta = build_graph.build_adjacency(
        {"elements": nodes + ways},
    )

    assert {(target, way) for target, _distance, way in adj[1]} == {(2, 10), (2, 13)}
    assert not any(target == 1 and way == 10 for target, _distance, way in adj[2])
    assert any(target == 2 and way == 11 for target, _distance, way in adj[3])
    assert not any(target == 3 and way == 11 for target, _distance, way in adj[2])
    assert any(target == 4 and way == 12 for target, _distance, way in adj[3])
    assert not any(target == 3 and way == 12 for target, _distance, way in adj[4])
    assert 14 not in metadata and 16 not in metadata
    assert 15 in metadata
    # Keep a motorcycle-compatible way in the graph even when motorcars are
    # denied; the runtime vehicle policy filters it per request.
    assert 18 in metadata
    assert metadata[18]["motor_vehicle"] == "yes"
    assert metadata[18]["motorcar"] == "no"
    assert not any(target == 9 for edges in adj.values()
                   for target, _distance, _way in edges)

    rounded_nodes = {1: (1.000001, 104.000001), 2: (1.000002, 104.000002)}
    encoded = build_graph.serialized_edge_distance_m(rounded_nodes, 1, 2)
    assert encoded >= build_graph.haversine_m(*rounded_nodes[1], *rounded_nodes[2])
    assert round(encoded * 10) == encoded * 10


def test_graph_builder_uses_largest_strongly_connected_component():
    adjacency = {
        1: [(2, 1.0, 1)], 2: [(1, 1.0, 1), (3, 1.0, 1)],
        3: [(4, 1.0, 1)], 4: [(3, 1.0, 1)],
        5: [(6, 1.0, 1)], 6: [],
    }
    # {1,2} and {3,4} tie; either is valid, but the one-way bridge cannot
    # incorrectly merge them into a merely weakly connected four-node set.
    component = build_graph.largest_strongly_connected_component(adjacency)
    assert component in ({1, 2}, {3, 4})


def test_penalized_astar_reports_physical_distance():
    """Congestion search cost must never be published as kilometres."""
    src = router.LANDMARKS["nagoya"]
    dst = router.LANDMARKS["batam_centre"]
    destination_zone = [{
        "name": "Batam Centre Port Terminal",
        "lat": 1.1305,
        "lng": 104.0535,
        "radius_m": 700,
        "congestion_index": 90.0,
        "level": "SUPER_CONGESTED",
    }]
    blocked, _ = router.find_nodes_in_zones(destination_zone)
    routed = router.astar(src, dst, avoid_nodes=blocked)
    assert routed is not None
    path, reported_metres = routed

    raw_metres = 0.0
    penalized_cost = 0.0
    effective_avoid = blocked - {src, dst}
    for node, nxt in zip(path, path[1:]):
        weight = next(weight for candidate, weight in router.ADJ[node] if candidate == nxt)
        raw_metres += weight
        penalized_cost += weight * (15.0 if nxt in effective_avoid else 1.0)

    assert penalized_cost > raw_metres * 2, "fixture did not exercise a route penalty"
    assert abs(reported_metres - raw_metres) < 1e-6, (
        "A* returned congestion-weighted cost as physical road distance")


def test_vehicle_generalized_cost_changes_path_on_synthetic_graph():
    """A light agile vehicle and heavy freight must not be aliases."""
    original_nodes = router.NODES
    original_road_adj = router.ROAD_ADJ
    original_adj = router.ADJ
    original_meta = router.NODE_META
    try:
        router.NODES = {
            1: (0.0, 0.0), 2: (0.0, 0.001),
            3: (0.001, 0.001), 4: (0.0, 0.002),
        }

        def edge(target, distance, road_index, name, highway):
            return router.RoadEdge(
                target, distance, road_index, name=name, highway=highway,
            )

        router.ROAD_ADJ = {
            1: [edge(2, 120, 1, "Local Street", "residential"),
                edge(3, 280, 2, "Freight Arterial", "primary")],
            2: [edge(1, 120, 1, "Local Street", "residential"),
                edge(4, 120, 1, "Local Street", "residential")],
            3: [edge(1, 280, 2, "Freight Arterial", "primary"),
                edge(4, 280, 2, "Freight Arterial", "primary")],
            4: [edge(2, 120, 1, "Local Street", "residential"),
                edge(3, 280, 2, "Freight Arterial", "primary")],
        }
        router.ADJ = {
            source: [(edge.target, edge.distance_m) for edge in edges]
            for source, edges in router.ROAD_ADJ.items()
        }
        router.NODE_META = {}

        motorcycle = router.astar_detailed(1, 4, vehicle_type="MOTORCYCLE")
        freight = router.astar_detailed(1, 4, vehicle_type="CARGO_TRUCK")
        assert motorcycle is not None and motorcycle.nodes == [1, 2, 4]
        assert freight is not None and freight.nodes == [1, 3, 4]
        assert motorcycle.distance_m == 240
        assert freight.distance_m == 560
        assert motorcycle.search_cost_s != motorcycle.distance_m, (
            "seconds-based generalized cost was confused with metres")

        congested = router.astar_detailed(
            1, 4, vehicle_type="MOTORCYCLE",
            congestion_scores={2: 100.0}, network_congestion_score=80.0,
        )
        assert congested is not None and congested.nodes == [1, 3, 4], (
            "strong spatial exposure did not change the selected path")

        clear_cost = router.path_cost_breakdown(
            motorcycle, router.vehicle_profile("MOTORCYCLE"), weather=0,
            network_congestion_score=10.0,
        )
        peak_cost = router.path_cost_breakdown(
            motorcycle, router.vehicle_profile("MOTORCYCLE"), weather=0,
            network_congestion_score=90.0,
        )
        storm_cost = router.path_cost_breakdown(
            motorcycle, router.vehicle_profile("MOTORCYCLE"), weather=2,
            network_congestion_score=10.0,
        )
        assert peak_cost["generalized_cost_s"] > clear_cost["generalized_cost_s"]
        assert storm_cost["modeled_travel_s"] > clear_cost["modeled_travel_s"]
        assert clear_cost["generalized_cost_s"] >= clear_cost["modeled_travel_s"]
    finally:
        router.NODES = original_nodes
        router.ROAD_ADJ = original_road_adj
        router.ADJ = original_adj
        router.NODE_META = original_meta


def test_vehicle_profile_catalog_is_complete_and_auditable():
    expected = {
        "COMMUTER", "ELECTRIC_CAR", "MOTORCYCLE", "EXPRESS_VAN",
        "MINIBUS", "CITY_BUS", "LIGHT_TRUCK", "CARGO_TRUCK",
    }
    assert set(router.VEHICLE_PROFILES) == expected
    assert route_solver.VEHICLE_PROFILES is router.VEHICLE_PROFILES
    for vehicle_type, profile in router.VEHICLE_PROFILES.items():
        payload = router.vehicle_profile_payload(profile)
        assert payload["id"] == vehicle_type
        assert payload["max_speed_kph"] > 0
        assert payload["emissions_kg_per_km"] >= 0
        assert payload["road_preferences"]["residential"] >= 1.0
        assert "height/weight" in payload["legal_restrictions_note"]
        api_main.RouteRequest(vehicle_type=vehicle_type)
        api_main.FreeRouteRequest(
            origin_lat=1.06, origin_lng=104.03,
            destination_lat=1.13, destination_lng=104.05,
            vehicle_type=vehicle_type,
        )

    catalog = api_main.get_vehicle_profiles()["vehicle_profiles"]
    assert {profile["id"] for profile in catalog} == expected

    motorcycle_route = router.route_between(
        "mukakuning", "batam_centre", vehicle_type="MOTORCYCLE",
    )
    freight_route = router.route_between(
        "mukakuning", "batam_centre", vehicle_type="CARGO_TRUCK",
    )
    ev_route = router.route_between(
        "mukakuning", "batam_centre", vehicle_type="ELECTRIC_CAR",
    )
    assert motorcycle_route and freight_route and ev_route
    assert (
        freight_route["modeled_travel_time_mins"]
        > motorcycle_route["modeled_travel_time_mins"]
    )
    assert route_solver._route_emissions_kg(
        ev_route, router.vehicle_profile("ELECTRIC_CAR"),
    ) < route_solver._route_emissions_kg(
        freight_route, router.vehicle_profile("CARGO_TRUCK"),
    )

    # The committed Batam topology also has real (not merely synthetic)
    # profile divergence: heavy freight prefers the longer through-road path.
    agile_real = router.route_between(
        "batam_centre", "batu_ampar", vehicle_type="MOTORCYCLE",
    )
    freight_real = router.route_between(
        "batam_centre", "batu_ampar", vehicle_type="CARGO_TRUCK",
    )
    assert agile_real and freight_real
    assert agile_real["geometry"] != freight_real["geometry"]
    assert freight_real["distance_km"] > agile_real["distance_km"]


def test_invalid_router_profile_and_weather_are_rejected():
    src = router.LANDMARKS["mukakuning"]
    dst = router.LANDMARKS["batam_centre"]
    for kwargs, expected_word in (
        ({"vehicle_type": "BICYCLE"}, "vehicle"),
        ({"weather": 9}, "weather"),
    ):
        try:
            router.astar_detailed(src, dst, **kwargs)
        except ValueError as err:
            assert expected_word in str(err).lower()
        else:
            raise AssertionError(f"invalid routing input was accepted: {kwargs}")


def test_route_preference_catalog_and_api_validation():
    """All public objectives have nonnegative, auditable API weights."""
    from unittest.mock import patch

    expected = {"BALANCED", "FASTEST", "SHORTEST", "EASY", "LOCAL"}
    assert set(router.ROUTE_PREFERENCES) == expected
    assert route_solver.ROUTE_PREFERENCES is router.ROUTE_PREFERENCES
    for preference_id, preference in router.ROUTE_PREFERENCES.items():
        payload = router.route_preference_payload(preference)
        assert payload["id"] == preference_id
        assert payload["objective_cost_unit"] == "weighted_seconds"
        assert payload["component_weights"]
        assert payload["eligible_vehicle_types"]
        assert payload["road_scope"]
        if preference_id == "LOCAL":
            assert set(payload["eligible_vehicle_types"]) == {
                "COMMUTER", "ELECTRIC_CAR", "MOTORCYCLE",
            }
            assert payload["road_scope"] == "MAPPED_PUBLIC_LOCAL"
        else:
            assert set(payload["eligible_vehicle_types"]) == set(
                router.VEHICLE_PROFILES,
            )
            assert payload["road_scope"] == "STANDARD"
        assert all(
            isinstance(weight, (int, float)) and weight >= 0
            for weight in payload["component_weights"].values()
        )
        assert api_main.RouteRequest(
            vehicle_type="COMMUTER", route_preference=preference_id,
        ).route_preference == preference_id
        assert api_main.FreeRouteRequest(
            origin_lat=1.06, origin_lng=104.03,
            destination_lat=1.13, destination_lng=104.05,
            route_preference=preference_id,
        ).route_preference == preference_id

    assert api_main.RouteRequest(
        vehicle_type="COMMUTER",
    ).route_preference == "BALANCED"
    catalog = api_main.get_route_preferences()
    assert catalog["default_route_preference"] == "BALANCED"
    assert {
        preference["id"] for preference in catalog["route_preferences"]
    } == expected
    local_benchmark_body = google_routes_benchmark._request_body(
        1.1465, 104.0125, 1.1318, 104.0554, "LOCAL",
    )
    assert local_benchmark_body["routingPreference"] == "TRAFFIC_AWARE"
    assert "requestedReferenceRoutes" not in local_benchmark_body
    assert google_routes_benchmark._preference_details("LOCAL")["honored"] is False

    for request_factory in (
        lambda: api_main.RouteRequest(
            vehicle_type="COMMUTER", route_preference="SCENIC",
        ),
        lambda: api_main.FreeRouteRequest(
            origin_lat=1.06, origin_lng=104.03,
            destination_lat=1.13, destination_lng=104.05,
            route_preference="SCENIC",
        ),
    ):
        try:
            request_factory()
        except ValidationError:
            pass
        else:
            raise AssertionError("unknown route preference passed API validation")

    request = api_main.RouteRequest(
        corridor_id="corridor-1", vehicle_type="COMMUTER",
        route_preference="LOCAL",
    )
    with patch.object(
        api_main, "optimize_route", return_value={"route_preference": "LOCAL"},
    ) as optimize:
        response = api_main.api_optimize_route(request)
    assert optimize.call_args.kwargs["route_preference"] == "LOCAL"
    assert response["route_preference"] == "LOCAL"


def test_route_preferences_diverge_on_synthetic_graph():
    """Each objective chooses a legal path and local mode rejects large vehicles."""
    original_nodes = router.NODES
    original_road_adj = router.ROAD_ADJ
    original_adj = router.ADJ
    original_meta = router.NODE_META
    try:
        router.NODES = {
            1: (0.0, 0.0),
            2: (0.0, 0.0015),
            3: (0.00035, 0.001),
            4: (-0.00035, 0.002),
            5: (0.002, 0.0015),
            6: (0.0, 0.003),
        }

        def edge(target, distance, road_index, name, highway):
            return router.RoadEdge(
                target, distance, road_index, name=name, highway=highway,
            )

        router.ROAD_ADJ = {
            1: [
                edge(2, 180, 1, "Short Lane", "residential"),
                edge(3, 160, 2, "Fast One", "primary"),
                edge(5, 350, 5, "Easy Avenue", "primary"),
            ],
            2: [edge(6, 180, 1, "Short Lane", "residential")],
            3: [edge(4, 160, 3, "Fast Two", "primary")],
            4: [edge(6, 160, 4, "Fast Three", "primary")],
            5: [edge(6, 350, 5, "Easy Avenue", "primary")],
            6: [],
        }
        router.ADJ = {
            source: [(item.target, item.distance_m) for item in edges]
            for source, edges in router.ROAD_ADJ.items()
        }
        router.NODE_META = {}

        shortest = router.astar_detailed(1, 6, route_preference="SHORTEST")
        fastest = router.astar_detailed(1, 6, route_preference="FASTEST")
        easy = router.astar_detailed(1, 6, route_preference="EASY")
        local = router.astar_detailed(
            1, 6, route_preference="LOCAL", vehicle_type="MOTORCYCLE",
        )
        balanced = router.astar_detailed(1, 6)
        assert shortest and shortest.nodes == [1, 2, 6]
        assert fastest and fastest.nodes == [1, 3, 4, 6]
        assert easy and easy.nodes == [1, 5, 6]
        assert local and local.nodes == [1, 2, 6]
        assert balanced and balanced.nodes == fastest.nodes

        local_audit = router._path_local_road_audit(local)
        assert local_audit["distance_km"] == 0.36
        assert local_audit["segment_count"] == 1
        assert local_audit["segments"][0]["edge_count"] == 2
        assert local_audit["width_clearance_verified"] is False

        local_payload = router._path_payload(
            local,
            1,
            6,
            "Origin",
            "Destination",
            vehicle_type="MOTORCYCLE",
            route_preference="LOCAL",
        )
        assert local_payload["local_road_distance_km"] == 0.36
        assert local_payload["local_road_audit"]["requested"] is True
        assert local_payload["routing_model"]["road_scope"] == (
            "MAPPED_PUBLIC_LOCAL"
        )

        try:
            router.astar_detailed(
                1, 6, route_preference="LOCAL", vehicle_type="CARGO_TRUCK",
            )
        except ValueError as err:
            assert "unverified narrow roads" in str(err)
        else:
            raise AssertionError("local-road mode accepted a cargo truck")
        try:
            router._route_between_nodes_uncached(
                1,
                1,
                route_preference="LOCAL",
                vehicle_type="CARGO_TRUCK",
            )
        except ValueError as err:
            assert "unverified narrow roads" in str(err)
        else:
            raise AssertionError("zero-distance local mode accepted a cargo truck")

        shortest_eta = router.path_cost_breakdown(
            shortest, router.vehicle_profile("COMMUTER"),
        )["modeled_travel_s"]
        assert shortest.distance_m == 360
        assert shortest.search_cost_s != shortest.distance_m
        assert shortest.search_cost_s != shortest_eta
    finally:
        router.NODES = original_nodes
        router.ROAD_ADJ = original_road_adj
        router.ADJ = original_adj
        router.NODE_META = original_meta


def test_selected_route_preference_is_published_and_cached_separately():
    src = router.LANDMARKS["nagoya"]
    dst = router.LANDMARKS["batam_centre"]
    router._route_between_nodes_cached.cache_clear()
    balanced = router.route_between_nodes(
        src, dst, include_alternatives=False,
    )
    after_balanced = router._route_between_nodes_cached.cache_info()
    shortest = router.route_between_nodes(
        src, dst, include_alternatives=False, route_preference="SHORTEST",
    )
    after_shortest = router._route_between_nodes_cached.cache_info()
    shortest_again = router.route_between_nodes(
        src, dst, include_alternatives=False, route_preference="SHORTEST",
    )
    after_repeat = router._route_between_nodes_cached.cache_info()

    assert balanced and shortest and shortest_again
    assert after_shortest.misses == after_balanced.misses + 1
    assert after_repeat.hits == after_shortest.hits + 1
    assert balanced["route_preference"] == "BALANCED"
    assert shortest["route_preference"] == "SHORTEST"
    assert shortest["routing_model"]["selected_preference"] == "SHORTEST"
    assert shortest["routing_model"]["component_weights"] == (
        shortest["route_preference_profile"]["component_weights"]
    )
    assert shortest["objective_cost_s"] >= 0
    assert shortest["distance_km"] >= shortest["straight_line_km"]
    assert shortest["modeled_travel_time_mins"] > 0
    router._route_between_nodes_cached.cache_clear()


def _weighted_path_overlap(candidate, other):
    other_edges = {
        tuple(sorted((source, edge.target)))
        for source, edge in zip(other.nodes, other.edges)
    }
    shared = sum(
        edge.distance_m
        for source, edge in zip(candidate.nodes, candidate.edges)
        if tuple(sorted((source, edge.target))) in other_edges
    )
    return shared / candidate.distance_m


def test_navigation_comes_from_unsimplified_named_edges():
    src = router.LANDMARKS["mukakuning"]
    dst = router.LANDMARKS["batam_centre"]
    path = router.astar_detailed(src, dst)
    assert path is not None
    navigation = router.generate_navigation(
        path, "Batamindo Industrial Park", "Batam Centre Ferry Terminal",
    )
    maneuvers = navigation["maneuvers"]

    assert maneuvers[0]["type"] == "DEPART"
    assert maneuvers[-1]["type"] == "ARRIVE"
    assert tuple(maneuvers[0]["coords"]) == router.NODES[src]
    assert tuple(maneuvers[-1]["coords"]) == router.NODES[dst]
    assert all(tuple(step["coords"]) in {router.NODES[node] for node in path.nodes}
               for step in maneuvers)
    assert abs(sum(step["distance_m"] for step in maneuvers) - path.distance_m) <= len(maneuvers)
    assert any(step["type"] == "ROUNDABOUT" and step["exit_number"] >= 1
               for step in maneuvers)
    assert navigation["route_narrative_words"].endswith(".")
    assert "Arrive at Batam Centre Ferry Terminal" in navigation["route_narrative_words"]
    assert [step["step"] for step in maneuvers] == list(range(1, len(maneuvers) + 1))
    cumulative = [step["cumulative_distance_m"] for step in maneuvers]
    assert cumulative == sorted(cumulative)
    assert navigation["traffic_lights_count"] == sum(
        router.NODE_META.get(node, {}).get("highway") == "traffic_signals"
        for node in path.nodes
    )

    sourced_names = {value for road in router.ROADS for value in road[:2] if value}
    for step in maneuvers[1:-1]:
        street = step["street"]
        assert street in sourced_names or street.startswith("Unnamed "), (
            f"navigation invented road name {street!r}")

    assert router._turn_kind(10)[0] == "CONTINUE"
    assert router._turn_kind(20)[0] == "SLIGHT_RIGHT"
    assert router._turn_kind(-50)[0] == "TURN_LEFT"
    assert router._turn_kind(150)[0] == "SHARP_RIGHT"
    assert router._turn_kind(-175)[0] == "U_TURN"


def test_alternative_paths_are_bounded_diverse_and_deterministic():
    src = router.LANDMARKS["mukakuning"]
    dst = router.LANDMARKS["batam_centre"]
    primary = router.astar_detailed(src, dst)
    assert primary is not None
    first = router.alternative_paths(primary)
    second = router.alternative_paths(primary)
    assert 1 <= len(first) <= 2
    assert [route.nodes for route, _overlap in first] == [
        route.nodes for route, _overlap in second
    ]

    accepted = [primary]
    for candidate, published_primary_overlap in first:
        assert candidate.distance_m <= primary.distance_m * 1.65
        overlaps = [_weighted_path_overlap(candidate, other) for other in accepted]
        assert all(overlap <= 0.820001 for overlap in overlaps)
        assert abs(published_primary_overlap - overlaps[0]) <= 0.001
        for source, edge in zip(candidate.nodes, candidate.edges):
            assert any(
                actual.target == edge.target
                and actual.road_index == edge.road_index
                and actual.distance_m == edge.distance_m
                for actual in router.ROAD_ADJ[source]
            ), f"alternative contains fake edge {source}->{edge.target}"
        accepted.append(candidate)

    payload = router.route_between(
        "mukakuning", "batam_centre", include_alternatives=True,
    )
    assert payload is not None and len(payload["alternatives"]) == len(first)
    assert 1 <= len(payload["alternatives"]) <= 2
    assert payload["distance_km"] > 0
    assert all(
        alternative["distance_km"] > 0
        for alternative in payload["alternatives"]
    )
    for index, alternative in enumerate(payload["alternatives"], start=1):
        assert alternative["id"] == f"osm-alternative-{index}"
        assert alternative["name"]
        assert len(alternative["route_geometry"]) >= 2
        assert alternative["navigation"]["maneuvers"][0]["type"] == "DEPART"
        assert alternative["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"
        assert alternative["generalized_cost_mins"] <= (
            payload["generalized_cost_mins"] * 1.85 + 0.02
        )
        assert len(alternative["navigation"]["maneuvers"]) <= max(
            len(payload["navigation"]["maneuvers"]) + 10,
            math.ceil(len(payload["navigation"]["maneuvers"]) * 1.75),
        )


def test_route_avoidance_reports_only_genuine_bypasses():
    zones = [
        {
            "name": "Simpang Kabil Expressway",
            # Small stable fixture: the ordinary corridor crosses it, while a
            # nearby public-road bypass avoids every enclosed graph node.
            "lat": 1.063285, "lng": 104.024517, "radius_m": 150,
            "congestion_index": 90.0, "level": "SUPER_CONGESTED",
        },
        {
            # The route terminates inside this zone, so it cannot be bypassed.
            "name": "Batam Centre Port Terminal",
            "lat": 1.1305, "lng": 104.0535, "radius_m": 700,
            "congestion_index": 90.0, "level": "SUPER_CONGESTED",
        },
        {
            # Active but unrelated to the baseline path.
            "name": "Batu Ampar Freight Terminal",
            "lat": 1.1630, "lng": 104.0025, "radius_m": 800,
            "congestion_index": 90.0, "level": "SUPER_CONGESTED",
        },
    ]
    route = router.route_between_nodes(
        router.LANDMARKS["mukakuning"],
        router.LANDMARKS["batam_centre"],
        avoid_zones=zones,
    )
    assert route is not None
    assert route["routing_model"]["spatial_zone_count"] == len(zones)
    assert all(
        alternative["routing_model"]["spatial_zone_count"] == len(zones)
        for alternative in route["alternatives"]
    )
    # Soft, distance-decayed exposure must never claim a full bypass merely
    # because a path reduced its time inside a zone. This fixture's optimized
    # path still touches the red area, so the honest claim is empty.
    assert route["avoided_congested_zones"] == []
    assert route["distance_km"] < 20, "penalty leaked into physical distance"
    clear = router.route_between_nodes(
        router.LANDMARKS["mukakuning"],
        router.LANDMARKS["batam_centre"],
        avoid_zones=[],
        include_alternatives=False,
    )
    assert clear is not None
    assert clear["routing_model"]["spatial_zone_count"] == 0
    assert (
        route["routing_cost_breakdown"]["congestion_delay_mins"]
        > clear["routing_cost_breakdown"]["congestion_delay_mins"]
    )


def test_coordinate_snapping_uses_mutually_reachable_core():
    """Any coordinate snap must belong to its vehicle's mutually routable SCC."""
    assert router.GRAPH_META["runtime_vehicle_cores_required"] is True
    assert router.SNAP_NODE_IDS == router._main_routing_core("COMMUTER")
    assert router.LANDMARKS["batam_centre"] in router.SNAP_NODE_IDS

    # Probe arbitrary points rather than a stale OSM node ID: graph rebuilds
    # are allowed to remove old one-way service-lane tips entirely.
    terminal = router.LANDMARKS["batam_centre"]
    for point in ((1.1465, 104.0125), (1.0605, 104.0303), (1.10, 103.96)):
        snapped, snap_m = router.snap_to_graph(*point)
        assert snapped in router.SNAP_NODE_IDS and snap_m < 1_000
        assert router.astar(snapped, terminal)
        assert router.astar(terminal, snapped)


def test_free_path_cache_reuses_results_without_sharing_mutation():
    router.snap_to_graph.cache_clear()
    snapped_first = router.snap_to_graph(1.1465, 104.0125)
    snap_after_first = router.snap_to_graph.cache_info()
    snapped_second = router.snap_to_graph(1.1465, 104.0125)
    snap_after_second = router.snap_to_graph.cache_info()
    assert snapped_second == snapped_first
    assert snap_after_second.hits == snap_after_first.hits + 1
    assert snap_after_second.maxsize == 512

    src = router.LANDMARKS["nagoya"]
    dst = router.LANDMARKS["batam_centre"]
    router._route_between_nodes_cached.cache_clear()
    first = router.route_between_nodes(
        src, dst, origin_name="Nagoya Hill",
        destination_name="Batam Centre Ferry Terminal",
        include_alternatives=False,
    )
    after_first = router._route_between_nodes_cached.cache_info()
    assert first is not None
    first["geometry"].append([0.0, 0.0])

    second = router.route_between_nodes(
        src, dst, origin_name="Nagoya Hill",
        destination_name="Batam Centre Ferry Terminal",
        include_alternatives=False,
    )
    after_second = router._route_between_nodes_cached.cache_info()
    assert second is not None
    assert after_second.hits == after_first.hits + 1
    assert second["geometry"][-1] != [0.0, 0.0]
    assert router._route_between_nodes_cached.cache_info().maxsize == 128
    misses = after_second.misses
    router.route_between_nodes(
        src, dst, origin_name="Nagoya Hill",
        destination_name="Batam Centre Ferry Terminal",
        include_alternatives=False, vehicle_type="ELECTRIC_CAR",
    )
    router.route_between_nodes(
        src, dst, origin_name="Nagoya Hill",
        destination_name="Batam Centre Ferry Terminal",
        include_alternatives=False, weather=1,
    )
    router.route_between_nodes(
        src, dst, origin_name="Nagoya Hill",
        destination_name="Batam Centre Ferry Terminal",
        include_alternatives=False, network_congestion_score=75,
    )
    assert router._route_between_nodes_cached.cache_info().misses == misses + 3, (
        "vehicle/weather/hour-derived score missing from route cache key")
    router._route_between_nodes_cached.cache_clear()


def test_spatial_zone_radial_cache_is_bounded_and_reused():
    router._radial_node_influence.cache_clear()
    first = router._radial_node_influence(1.1, 104.04, 800.0)
    after_first = router._radial_node_influence.cache_info()
    second = router._radial_node_influence(1.1, 104.04, 800.0)
    after_second = router._radial_node_influence.cache_info()
    assert first == second and first
    assert after_second.hits == after_first.hits + 1
    assert after_second.maxsize == 64


def test_every_planner_location_pair_is_routable():
    assert len(ROUTE_LOCATIONS) >= 12, "route planner did not expand beyond the original places"
    expected = len(ROUTE_LOCATIONS) * (len(ROUTE_LOCATIONS) - 1)
    routable = 0
    for origin in ROUTE_LOCATIONS:
        for destination in ROUTE_LOCATIONS:
            if origin["id"] == destination["id"]:
                continue
            route = router.route_between(origin["id"], destination["id"])
            assert route, f"no route from {origin['id']} to {destination['id']}"
            routable += 1
    assert routable == expected


def test_geocoder_is_corridor_bounded_without_global_retry():
    calls = []
    original_get = geocoder._nominatim_get
    original_cache = geocoder._cache
    try:
        geocoder._cache = geocoder._TTLCache()

        def fake_get(url):
            calls.append(url)
            return []

        geocoder._nominatim_get = fake_get
        assert geocoder.geocode("place-that-does-not-exist", limit=3) == []
    finally:
        geocoder._nominatim_get = original_get
        geocoder._cache = original_cache

    assert len(calls) == 1, "empty corridor search unexpectedly retried worldwide"
    assert "bounded=1" in calls[0] and "viewbox=" in calls[0]


def test_geocoder_preserves_out_of_coverage_snap_distance():
    original_get = geocoder._nominatim_get
    original_cache = geocoder._cache
    try:
        geocoder._cache = geocoder._TTLCache()

        def fake_get(url):
            if "/reverse" in url:
                return {"display_name": "New York"}
            return [{
                "lat": "40.7128", "lon": "-74.0060",
                "display_name": "New York", "importance": 1.0,
            }]

        geocoder._nominatim_get = fake_get
        result = geocoder.geocode("forced-out-of-coverage-result", limit=1)[0]
        reverse = geocoder.reverse_geocode(40.7128, -74.0060)
    finally:
        geocoder._nominatim_get = original_get
        geocoder._cache = original_cache

    assert result["node_id"] is None
    assert result["snap_distance_m"] > 1_000_000
    assert result["snapped_lat"] == result["lat"]
    assert result["snapped_lng"] == result["lng"]
    assert reverse["node_id"] is None
    assert reverse["snap_distance_m"] > 1_000_000


def test_geocoder_marks_singapore_results_without_snapping_them_to_batam():
    original_get = geocoder._nominatim_get
    original_cache = geocoder._cache
    try:
        geocoder._cache = geocoder._TTLCache()
        geocoder._nominatim_get = lambda _url: [{
            "lat": "1.29027", "lon": "103.851959",
            "display_name": "Singapore CBD", "importance": 1.0,
        }]
        result = geocoder.geocode("Singapore CBD", limit=1)[0]
    finally:
        geocoder._nominatim_get = original_get
        geocoder._cache = original_cache

    assert result["supported_region"] == "SINGAPORE"
    assert result["node_id"] is None
    assert result["snapped_lat"] == result["lat"]
    assert result["snapped_lng"] == result["lng"]


def test_supabase_route_distance_comes_from_geometry():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'[{"cost_s":60,"geom":{"type":"LineString","coordinates":'
                b'[[104.0,1.0],[104.001,1.0],[104.001,1.001]]}}]'
            )

    original_config = supabase_pgrouting.get_supabase_config
    original_request_json = supabase_pgrouting.supabase_server.request_json
    try:
        supabase_pgrouting.get_supabase_config = lambda: {
            "url": "https://example.supabase.co", "key": "test",
        }
        supabase_pgrouting.supabase_server.request_json = (
            lambda *_args, **_kwargs: json.loads(
                FakeResponse().read().decode("utf-8"),
            )
        )
        result = supabase_pgrouting.query_supabase_pgrouting(1.0, 104.0, 1.001, 104.001)
    finally:
        supabase_pgrouting.get_supabase_config = original_config
        supabase_pgrouting.supabase_server.request_json = original_request_json

    assert result is not None
    assert result["distance_km"] == 0.22, (
        "distance was estimated from coordinate count instead of segment length")


def test_google_navigation_strips_html_and_appends_arrival():
    navigation = google_maps_router._google_navigation({
        "steps": [{
            "html_instructions": "Turn <b>left</b> onto <b>Jalan Raja</b>",
            "maneuver": "turn-left",
            "distance": {"value": 125},
            "start_location": {"lat": 1.1, "lng": 104.0},
        }],
        "end_location": {"lat": 1.2, "lng": 104.1},
    }, "Batam Centre Ferry Terminal")

    assert navigation is not None
    assert navigation["maneuvers"][0]["instruction"] == "Turn left onto Jalan Raja"
    assert navigation["maneuvers"][0]["street"] == "Jalan Raja"
    assert navigation["maneuvers"][0]["type"] == "TURN_LEFT"
    assert navigation["maneuvers"][0]["modifier"] == "left"
    assert navigation["maneuvers"][-1]["type"] == "ARRIVE"
    assert navigation["maneuvers"][-1]["coords"] == [1.2, 104.1]
    assert navigation["maneuvers"][-1]["cumulative_distance_m"] == 125
    assert "<" not in navigation["route_narrative_words"]
    assert navigation["route_narrative_words"].endswith(
        "Arrive at Batam Centre Ferry Terminal.",
    )
    assert google_maps_router._normalized_google_maneuver("turn-sharp-left") == (
        "SHARP_LEFT", "sharp_left", "turn_left",
    )
    assert google_maps_router._normalized_google_maneuver("ramp-right") == (
        "TAKE_RAMP", "right", "turn_right",
    )


def test_google_http_parser_keeps_native_steps_and_alternatives():
    seen_urls = []

    def raw_route(distance):
        return {
            "overview_polyline": {"points": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
            "legs": [{
                "distance": {"value": distance},
                "duration": {"value": 600},
                "start_address": "Origin",
                "end_address": "Destination",
                "end_location": {"lat": 1.2, "lng": 104.1},
                "steps": [{
                    "html_instructions": "Turn <b>right</b> onto <b>Jalan Test</b>",
                    "maneuver": "turn-right",
                    "distance": {"value": distance},
                    "start_location": {"lat": 1.1, "lng": 104.0},
                }],
            }],
        }

    payload = {"status": "OK", "routes": [raw_route(1000), raw_route(1200)]}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    original_key = google_maps_router.get_api_key
    original_urlopen = google_maps_router.urllib.request.urlopen
    try:
        google_maps_router.get_api_key = lambda: "test-key"

        def fake_urlopen(request, **_kwargs):
            seen_urls.append(request.full_url)
            return FakeResponse()

        google_maps_router.urllib.request.urlopen = fake_urlopen
        route = google_maps_router.get_google_route("1.1,104", "1.2,104.1")
    finally:
        google_maps_router.get_api_key = original_key
        google_maps_router.urllib.request.urlopen = original_urlopen

    assert route is not None
    assert "alternatives=true" in seen_urls[0]
    assert route["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"
    assert route["navigation"]["route_narrative_words"]
    assert len(route["alternatives"]) == 1
    assert route["alternatives"][0]["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"


def test_google_routes_benchmark_request_and_parser_are_metric_only():
    captured = {}
    payload = {
        "routes": [
            {
                "duration": "612.4s",
                "distanceMeters": 12_345,
                "routeLabels": ["DEFAULT_ROUTE"],
                "polyline": {"encodedPolyline": "must-never-be-read"},
            },
            {
                "duration": "700s",
                "distanceMeters": 13_500.5,
                "routeLabels": ["SHORTER_DISTANCE"],
            },
            {"duration": "NaNs", "distanceMeters": -1},
            {"duration": "800s", "distanceMeters": 15_000},
        ],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == google_routes_benchmark.MAX_RESPONSE_BYTES + 1
            return json.dumps(payload).encode("utf-8")

    original_urlopen = google_routes_benchmark.urllib.request.urlopen
    original_enabled = os.environ.get("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK")
    original_key = os.environ.get("CROSSFLOW_GOOGLE_ROUTES_API_KEY")
    try:
        os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = "true"
        os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = "server-benchmark-key"

        def fake_urlopen(request, timeout, **kwargs):
            captured["request"] = request
            captured["timeout"] = timeout
            captured["ssl_context"] = kwargs.get("context")
            return FakeResponse()

        google_routes_benchmark.urllib.request.urlopen = fake_urlopen
        result = google_routes_benchmark.benchmark_routes(
            1.1465, 104.0125, 1.1318, 104.0554, "SHORTEST",
        )
    finally:
        google_routes_benchmark.urllib.request.urlopen = original_urlopen
        if original_enabled is None:
            os.environ.pop("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK", None)
        else:
            os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = original_enabled
        if original_key is None:
            os.environ.pop("CROSSFLOW_GOOGLE_ROUTES_API_KEY", None)
        else:
            os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = original_key

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == google_routes_benchmark.COMPUTE_ROUTES_URL
    assert request.get_method() == "POST"
    assert captured["timeout"] == google_routes_benchmark.TIMEOUT_S
    assert headers == {
        "content-type": "application/json",
        "x-goog-api-key": "server-benchmark-key",
        "x-goog-fieldmask": (
            "routes.duration,routes.distanceMeters,routes.routeLabels"
        ),
    }
    assert body == {
        "origin": {"location": {"latLng": {
            "latitude": 1.1465, "longitude": 104.0125,
        }}},
        "destination": {"location": {"latLng": {
            "latitude": 1.1318, "longitude": 104.0554,
        }}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
        "requestedReferenceRoutes": ["SHORTER_DISTANCE"],
    }
    forbidden = ("geometry", "polyline", "steps")
    assert all(term not in google_routes_benchmark.FIELD_MASK.lower() for term in forbidden)
    assert all(term not in json.dumps(body).lower() for term in forbidden)
    assert all(term not in json.dumps(result).lower() for term in forbidden)
    assert len(result["routes"]) == 2
    assert result["routes"][0]["duration_seconds"] == 612.4
    assert result["routes"][0]["distance_km"] == 12.345
    assert result["preference_honored"] is True
    assert result["preference_honored_details"]["experimental"] is True
    assert result["cacheable"] is False
    assert result["persisted"] is False
    assert result["training_eligible"] is False
    assert result["map_overlay_allowed"] is False
    assert result["attribution"] == "Google Maps"


def test_google_routes_benchmark_uses_only_dedicated_server_key_and_gate():
    key_names = (
        "CROSSFLOW_GOOGLE_ROUTES_API_KEY",
        "VITE_GOOGLE_MAPS_API_KEY",
        "VITE_GOOGLE_ROUTES_API_KEY",
        "GOOGLE_MAPS_API_KEY",
        "GOOGLE_MAPS_KEY",
        "GOOGLE_API_KEY",
        "CROSSFLOW_ENABLE_GOOGLE_BENCHMARK",
    )
    originals = {key: os.environ.get(key) for key in key_names}
    try:
        for key in key_names:
            os.environ.pop(key, None)
        os.environ["VITE_GOOGLE_MAPS_API_KEY"] = "browser-key"
        os.environ["GOOGLE_MAPS_API_KEY"] = "legacy-server-key"
        assert google_routes_benchmark.get_api_key() == ""
        os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = "1"
        assert google_routes_benchmark.is_enabled() is False
        os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = " true "
        assert google_routes_benchmark.is_enabled() is True
        os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = "dedicated-key"
        assert google_routes_benchmark.get_api_key() == "dedicated-key"
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_route_benchmark_api_is_strict_coverage_bounded_and_disabled_by_default():
    valid = {
        "origin_lat": 1.1465,
        "origin_lng": 104.0125,
        "destination_lat": 1.1318,
        "destination_lng": 104.0554,
    }
    try:
        api_main.RouteBenchmarkRequest(**valid, provider_geometry=[])
    except ValidationError:
        pass
    else:
        raise AssertionError("route benchmark accepted an extra provider field")

    original_enabled = os.environ.get("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK")
    original_key = os.environ.get("CROSSFLOW_GOOGLE_ROUTES_API_KEY")
    try:
        os.environ.pop("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK", None)
        os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = "server-key"
        response = Response()
        try:
            api_main.api_route_benchmark(
                api_main.RouteBenchmarkRequest(**valid), response,
            )
        except HTTPException as err:
            assert err.status_code == 404
            assert err.headers["Cache-Control"] == "private, no-store"
            assert err.headers["Pragma"] == "no-cache"
        else:
            raise AssertionError("disabled Google benchmark endpoint was available")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["pragma"] == "no-cache"

        os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = "true"
        os.environ.pop("CROSSFLOW_GOOGLE_ROUTES_API_KEY", None)
        try:
            api_main.api_route_benchmark(
                api_main.RouteBenchmarkRequest(**valid), Response(),
            )
        except HTTPException as err:
            assert err.status_code == 503
        else:
            raise AssertionError("benchmark without its dedicated key was available")

        os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = "server-key"
        outside = api_main.RouteBenchmarkRequest(
            origin_lat=40.7128,
            origin_lng=-74.0060,
            destination_lat=1.1318,
            destination_lng=104.0554,
        )
        try:
            api_main.api_route_benchmark(outside, Response())
        except HTTPException as err:
            assert err.status_code == 400
            assert "Batam" in err.detail
        else:
            raise AssertionError("out-of-coverage benchmark coordinates were accepted")
    finally:
        if original_enabled is None:
            os.environ.pop("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK", None)
        else:
            os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = original_enabled
        if original_key is None:
            os.environ.pop("CROSSFLOW_GOOGLE_ROUTES_API_KEY", None)
        else:
            os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = original_key


def test_route_benchmark_api_is_no_store_and_maps_provider_errors_to_503():
    original_enabled = os.environ.get("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK")
    original_key = os.environ.get("CROSSFLOW_GOOGLE_ROUTES_API_KEY")
    original_urlopen = google_routes_benchmark.urllib.request.urlopen
    request = api_main.RouteBenchmarkRequest(
        origin_lat=1.1465,
        origin_lng=104.0125,
        destination_lat=1.1318,
        destination_lng=104.0554,
        route_preference="FASTEST",
    )
    try:
        os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = "true"
        os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = "server-key"

        def timed_out(*_args, **_kwargs):
            raise TimeoutError("provider timeout")

        google_routes_benchmark.urllib.request.urlopen = timed_out
        response = Response()
        try:
            api_main.api_route_benchmark(request, response)
        except HTTPException as err:
            assert err.status_code == 503
            assert err.detail == "Route benchmark is temporarily unavailable."
            assert err.headers["Cache-Control"] == "private, no-store"
        else:
            raise AssertionError("provider timeout did not become a bounded 503")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["pragma"] == "no-cache"
    finally:
        google_routes_benchmark.urllib.request.urlopen = original_urlopen
        if original_enabled is None:
            os.environ.pop("CROSSFLOW_ENABLE_GOOGLE_BENCHMARK", None)
        else:
            os.environ["CROSSFLOW_ENABLE_GOOGLE_BENCHMARK"] = original_enabled
        if original_key is None:
            os.environ.pop("CROSSFLOW_GOOGLE_ROUTES_API_KEY", None)
        else:
            os.environ["CROSSFLOW_GOOGLE_ROUTES_API_KEY"] = original_key


def test_road_continuity_matches_either_stable_name_or_reference():
    named = router.RoadEdge(
        2, 10, name="Jalan Jenderal Ahmad Yani", ref="39", highway="primary",
    )
    renamed_ref = router.RoadEdge(
        3, 10, name="Jalan Jenderal Ahmad Yani", ref="Nasional 39", highway="primary",
    )
    same_ref = router.RoadEdge(
        4, 10, name="Jalan Ahmad Yani", ref="39", highway="primary",
    )
    different = router.RoadEdge(
        5, 10, name="Jalan Engku Putri", ref="12", highway="primary",
    )
    assert router._same_road(named, renamed_ref)
    assert router._same_road(named, same_ref)
    assert not router._same_road(named, different)


def test_departure_roundabout_instruction_is_not_dropped():
    route = router.route_between("hang_nadim", "nagoya")
    assert route is not None
    maneuvers = route["navigation"]["maneuvers"]
    assert maneuvers[0]["type"] == "DEPART"
    assert any(maneuver["type"] == "ROUNDABOUT" for maneuver in maneuvers)
    assert maneuvers[-1]["type"] == "ARRIVE"

    original_nodes = router.NODES
    original_adjacency = router.ROAD_ADJ
    original_node_meta = router.NODE_META
    try:
        router.NODES = {
            1: (1.0000, 104.0000),
            2: (1.0001, 104.0001),
            3: (1.0002, 104.0002),
        }
        roundabout_edge = router.RoadEdge(
            2, 20, road_index=1, highway="tertiary", junction="roundabout",
        )
        exit_edge = router.RoadEdge(
            3, 30, road_index=2, name="Airport Road", highway="tertiary",
        )
        router.ROAD_ADJ = {2: [exit_edge], 3: []}
        router.NODE_META = {}
        synthetic = router.generate_navigation(
            router.PathResult([1, 2, 3], [roundabout_edge, exit_edge], 50, 50),
            "Airport", "City",
        )["maneuvers"]
    finally:
        router.NODES = original_nodes
        router.ROAD_ADJ = original_adjacency
        router.NODE_META = original_node_meta

    assert [step["type"] for step in synthetic] == ["DEPART", "ROUNDABOUT", "ARRIVE"]
    assert synthetic[0]["distance_m"] == 0
    assert synthetic[1]["distance_m"] == 50


def test_roundabout_parallel_arcs_count_as_one_exit():
    original_adjacency = router.ROAD_ADJ
    try:
        router.ROAD_ADJ = {
            2: [
                router.RoadEdge(3, 10, road_index=1, highway="primary"),
                router.RoadEdge(3, 10, road_index=2, highway="primary"),
                router.RoadEdge(4, 10, road_index=3, highway="secondary"),
                router.RoadEdge(
                    5, 10, road_index=4, highway="primary", junction="roundabout",
                ),
            ],
        }
        path = router.PathResult(
            [1, 2],
            [router.RoadEdge(2, 10, junction="roundabout")],
            10,
            10,
        )
        assert router._roundabout_exit_count(path, 0, 0) == 2
    finally:
        router.ROAD_ADJ = original_adjacency


# --------------------------------------------------------------------------
# Congestion model
# --------------------------------------------------------------------------

def test_model_does_not_saturate():
    """Evening peak used to clip every corridor to exactly 98.0."""
    scores = [forecaster.predict(h, 0, 0, 0, i)["current_score"]
              for h in (17, 18, 19) for i in range(5)]
    assert max(scores) < 95, f"still saturating at {max(scores)}"
    assert len({classify_status(s) for s in scores}) > 1, (
        "no status differentiation during the evening peak")


def test_evening_peak_stays_differentiated_end_to_end():
    """The map must not go uniformly red at the worst hour of the day.

    Checks the full pipeline rather than the model alone: the ferry surge adds
    ~16 points on top of the peak, which is what previously pushed every
    corridor over the CRITICAL threshold at once.
    """
    for moment in (EVENING, datetime(2026, 8, 7, 17, 30, tzinfo=TZ)):
        corridors = get_live_corridor_telemetry(moment)
        statuses = {c["status"] for c in corridors}
        assert len(statuses) > 1, (
            f"every corridor is {statuses.pop()} at {moment:%H:%M} — "
            "nothing left to differentiate on the map")


def test_weekend_materially_lowers_congestion():
    """The weekend feature used to be inert (importance 0.004)."""
    friday = sum(forecaster.predict(18, 0, 0, 0, i)["current_score"] for i in range(5)) / 5
    saturday = sum(forecaster.predict(18, 1, 0, 0, i)["current_score"] for i in range(5)) / 5
    assert saturday < 0.85 * friday, (
        f"weekend barely differs: {saturday:.1f} vs {friday:.1f}")


def test_forecast_horizons_ordered():
    """30-minute forecast used to be sampled a full hour ahead."""
    p = forecaster.predict_continuous(16.0, 0, 0, 0, 0)
    assert p["current_score"] < p["predicted_30min"] < p["predicted_60min"], (
        "forecast horizons out of order during the evening ramp")


def test_no_hour_boundary_cliff():
    before = forecaster.predict_continuous(16.983, 0, 0, 0, 0)["current_score"]
    after = forecaster.predict_continuous(17.0, 0, 0, 0, 0)["current_score"]
    assert abs(after - before) < 2.0, f"cliff of {abs(after - before):.1f} points"


def test_thresholds_consistent():
    assert classify_status(39.9) == "SMOOTH"
    assert classify_status(40.1) == "HEAVY"
    assert classify_status(69.9) == "HEAVY"
    assert classify_status(70.1) == "CRITICAL"
    for corridor in get_live_corridor_telemetry(WEEKDAY_1400):
        assert corridor["risk_level"] == risk_from_status(corridor["status"]), (
            "status and risk_level disagree in the same payload")
    assert delay_from_score(100.0) == 28.0


def test_history_training_provenance_counts_observed_and_modelled_rows():
    provenance = history_training_provenance([
        {"source": "synthetic"},
        {"source": "simulated"},
        {"source": "tomtom_live"},
        {"source": "verified_traffic_observation"},
    ])
    assert provenance == {
        "training_data_source": "history_store_mixed",
        "validation_scope": "history_holdout_mixed",
        "training_source_counts": {
            "synthetic": 1,
            "simulated": 1,
            "tomtom_live": 1,
            "verified_traffic_observation": 1,
        },
        "observed_training_rows": 2,
    }


def test_model_status_declares_baseline_training_provenance():
    metrics = api_main.api_model_status()["metrics"]
    assert metrics["training_data_source"] == "synthetic_profile_generator"
    assert metrics["validation_scope"] == "synthetic_holdout"
    assert metrics["training_source_counts"] == {
        "synthetic_profile_generator": metrics["total_samples"],
    }
    assert metrics["observed_training_rows"] == 0
    assert isinstance(metrics["retraining_enabled"], bool)


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------

def test_drift_deterministic_and_smooth():
    t = WEEKDAY_1400
    assert drift.corridor_drift("corridor-1", t) == drift.corridor_drift("corridor-1", t)

    a = drift.corridor_drift("corridor-1", t)
    b = drift.corridor_drift("corridor-1", t + timedelta(seconds=1))
    assert abs(a - b) < 0.15, f"jumps {abs(a - b):.2f} points in one second"

    spread = [drift.corridor_drift("corridor-1", t + timedelta(seconds=s))
              for s in range(0, 1800, 30)]
    assert max(spread) - min(spread) > 1.0, "drift is effectively static"
    assert all(abs(v) <= 4.0001 for v in spread), "drift exceeds its amplitude"


# --------------------------------------------------------------------------
# Ferry schedule
# --------------------------------------------------------------------------

def _fixture_services(operator):
    return {
        service["service_id"]: service
        for service in ferry_schedule.published_services_for_operator(operator)
    }


def _sorted_fixture_times(values):
    return sorted(values, key=lambda value: tuple(map(int, value.split(":"))))


def _batamfast_fixture_body(marker):
    services = _fixture_services("BatamFast")
    tables = []
    for service_id, header in ferry_refresh._BATAMFAST_HEADERS.items():
        rows = "".join(
            f"<tr><td>{departure}</td></tr>"
            for departure in services[service_id]["daily_departures"]
        )
        tables.append(
            f"<table><thead><th>{header}</th></thead><tbody>{rows}</tbody></table>"
        )
    return (
        "<html>Batamfast Ferry Schedule Sekupang "
        + "".join(tables)
        + f"<span data-fixture='{marker}'></span></html>"
    ).encode()


def _sindo_fixture_body(marker):
    services = _fixture_services("Sindo Ferry")
    cards = []
    exception_icons = {"sunday_only": 7}
    for service_id, (origin, destination, timezone_label) in (
        ferry_refresh._SINDO_HEADERS.items()
    ):
        service = services[service_id]
        departures = "".join(
            f"<p>{departure}</p>" for departure in service["daily_departures"]
        )
        departures += "".join(
            f'<img src="/assets/ship6.png"/><p>{departure}</p>'
            for departure in service["weekend_additions"]
        )
        for rule, values in service.get("calendar_exclusions", {}).items():
            ship = exception_icons[rule]
            departures += "".join(
                f'<img src="/assets/ship{ship}.png"/><p>{departure}</p>'
                for departure in values
            )
        cards.append(
            '<div class="MuiCard-root">'
            f"<p>{origin}</p><p>{destination}</p><p>{timezone_label}</p>"
            f"{departures}</div>"
        )
    return (
        "<html>SINDO FERRY SCHEDULE "
        + "".join(cards)
        + " LEGENDS: Saturdays and Sundays Only "
        + "Sunday Only and operated by Interlining Partner"
        + f"<span data-fixture='{marker}'></span></html>"
    ).encode()


def _majestic_fixture_body(marker):
    services = _fixture_services("Majestic Fast Ferry")
    sections = ["<html>Ferry Schedules"]
    labels = {"monday_only": "Mon", "friday_only": "Fri"}
    for service_id, (start, _) in ferry_refresh._MAJESTIC_SECTIONS.items():
        service = services[service_id]
        sections.append(start)
        exceptions = service.get("calendar_exclusions", {})
        if exceptions:
            labelled = {
                departure: labels[rule]
                for rule, values in exceptions.items()
                for departure in values
            }
            weekday = _sorted_fixture_times(
                service["daily_departures"] + list(labelled)
            )
            sections.append("Weekday")
            sections.extend(
                f"{departure} {labelled[departure]}"
                if departure in labelled else departure
                for departure in weekday
            )
            sections.append("Sat, Sun & Public Holidays")
            sections.extend(_sorted_fixture_times(
                service["daily_departures"] + service["weekend_additions"]
            ))
        else:
            sections.extend(service["daily_departures"])
    sections.extend([
        "From Tanah Merah Tanjung Pinang SGP Time",
        f"<span data-fixture='{marker}'></span></html>",
    ])
    return " ".join(sections).encode()


def _horizon_fixture_body(marker):
    services = _fixture_services("Horizon Fast Ferry")
    return (
        "<html>Singapore | Batam Ferry Daily Schedule "
        "Singapore HarbourFront to Batam Harbour Bay (Singapore Time) "
        + " ".join(services["horizon-hf-hb"]["daily_departures"])
        + " Batam Harbour Bay to Singapore HarbourFront (Indo Time) "
        + " ".join(services["horizon-hb-hf"]["daily_departures"])
        + " Singapore | Nirup Ferry Schedules "
        + f"<span data-fixture='{marker}'></span></html>"
    ).encode()


def _bp_batam_fixture_body(marker):
    return (
        "<html><h1>Pelabuhan Penumpang</h1> "
        "Terminal Ferry Internasional melayani Singapura dan Malaysia. "
        "<h2>Pelabuhan Penumpang Internasional terdiri dari:</h2> "
        "Pelabuhan Penumpang Internasional Batam Center "
        "Pelabuhan Penumpang Internasional Sekupang "
        "Pelabuhan Penumpang Internasional Teluk Senimba "
        "Pelabuhan Penumpang Internasional Nongsapura "
        "Pelabuhan Penumpang Internasional Harbour Bay "
        "<h2>Kinerja Operasional</h2><h4>PENUMPANG INTERNASIONAL</h4>"
        f"<span data-fixture='{marker}'></span></html>"
    ).encode()


def _scc_fixture_body(marker, board_day=WEEKDAY_1400.date()):
    input_date = board_day.strftime("%Y%m%d")
    row_date = board_day.strftime("%a, %d %b %Y")
    return (
        f"<html>FERRY arrival Last Updated: {row_date}, 09:00:00 "
        "(Singapore Time GMT +08) "
        f'<input type="hidden" name="date" value="{input_date}">'
        '<table class="schedule-table"><thead><tr>'
        "<th>DATE</th><th>TIME</th><th>TRIP ID</th>"
        "<th>FERRY OPERATOR</th><th>FROM</th><th>TO</th><th>STATUS</th>"
        "</tr></thead><tbody><tr><td>CONFIRMED</td>"
        f'<td data-label="DATE">{row_date}</td>'
        '<td data-label="TIME">0800</td>'
        '<td data-label="TRIP ID">ARPR0600</td>'
        '<td data-label="FERRY OPERATOR">HORIZON FAST FERRY PTE LTD</td>'
        '<td data-label="FROM">HARBOUR BAY</td>'
        '<td data-label="TO">HARBOURFRONT</td>'
        '<td data-label="STATUS">CONFIRMED</td>'
        f"</tr></tbody></table><span data-fixture='{marker}'></span></html>"
    ).encode()


def _official_source_fixture_body(
    source,
    minute="00",
    *,
    scc_day=WEEKDAY_1400.date(),
):
    fixtures = {
        "BatamFast": _batamfast_fixture_body,
        "Sindo Ferry": _sindo_fixture_body,
        "Majestic Fast Ferry": _majestic_fixture_body,
        "Horizon Fast Ferry": _horizon_fixture_body,
    }
    if source.schedule_operator is not None:
        return fixtures[source.schedule_operator](minute)
    references = {
        "bp-batam-passenger-ports": _bp_batam_fixture_body,
        "scc-live-operations-board": _scc_fixture_body,
    }
    if source.source_id == "scc-live-operations-board":
        return _scc_fixture_body(minute, scc_day)
    return references[source.source_id](minute)

def test_ferries_always_ahead():
    for moment in (WEEKDAY_1400, WEEKEND_1800, LATE_NIGHT,
                   datetime(2026, 8, 7, 2, 30, tzinfo=TZ)):
        sailings = ferry_schedule.generate_sailings(moment, horizon_hours=24)
        assert len(sailings) >= 3, f"only {len(sailings)} sailings at {moment}"
        times = [datetime.fromisoformat(s["departure_time"]) for s in sailings]
        assert all(t > moment for t in times), f"a past sailing was returned at {moment}"
        assert times == sorted(times), "sailings not in departure order"


def test_late_night_rolls_to_tomorrow():
    sailings = ferry_schedule.generate_sailings(LATE_NIGHT, horizon_hours=24)
    tomorrow = (LATE_NIGHT + timedelta(days=1)).date()
    assert any(datetime.fromisoformat(s["departure_time"]).date() == tomorrow
               for s in sailings), "no next-day sailings at 23:50"


def test_sailing_identity_stable_and_source_dated():
    a = ferry_schedule.generate_sailings(WEEKDAY_1400, horizon_hours=6)[0]
    b = ferry_schedule.generate_sailings(
        WEEKDAY_1400 + timedelta(seconds=1), horizon_hours=6)[0]
    assert a["sailing_id"] == b["sailing_id"]
    assert a["ferry_name"] == b["ferry_name"]
    assert a["status"] == b["status"]
    assert a["status"] == "SCHEDULED"
    assert a["data_source"] == "official_timetable_snapshot"
    assert a["live_status_available"] is False
    assert a["available_seats"] is None
    assert a["capacity"] is None
    assert a["arrival_time_is_estimate"] is True
    assert a["schedule_source_url"].startswith("https://")
    assert datetime.fromisoformat(a["schedule_last_verified_at"]).tzinfo is not None


def test_sindo_singapore_origin_schedule_preserves_singapore_local_time():
    expected_departures = {
        "sindo-hf-bct": [
            "08:00", "09:00", "10:20", "12:00", "13:20", "14:50",
            "16:10", "17:20", "18:30", "19:40", "20:45", "21:50",
        ],
        "sindo-hf-skp": [
            "08:30", "10:00", "11:10", "14:10", "17:30",
            "18:10", "19:30",
        ],
        "sindo-tm-bct": [
            "10:20", "12:30", "14:30", "16:30",
        ],
    }
    for code, expected in expected_departures.items():
        route = next(
            route for route in ferry_schedule.FERRY_ROUTES
            if route.code == code
        )
        slots = ferry_schedule._slots(route, datetime(2026, 8, 7).date())
        assert route.departure_timezone == "Asia/Singapore"
        assert [departure.strftime("%H:%M") for _, departure in slots] == expected
        assert all(
            departure.utcoffset() == timedelta(hours=8)
            for _, departure in slots
        )

    singapore_now = datetime(
        2026, 8, 7, 7, 40,
        tzinfo=timezone(timedelta(hours=8)),
    )
    sailing = next(
        item for item in ferry_schedule.generate_sailings(
            singapore_now, horizon_hours=2, ports=["HarbourFront SG"],
        )
        if item["arrival_port"] == "Batam Centre"
    )
    assert sailing["departure_time"] == "2026-08-07T08:00:00+08:00"
    assert sailing["arrival_time"] == "2026-08-07T08:00:00+07:00"
    assert sailing["departure_timezone"] == "Asia/Singapore"
    assert sailing["arrival_timezone"] == "Asia/Jakarta"
    assert sailing["schedule_source_url"] == (
        "https://app.sindoferry.com.sg/schedule/"
    )
    assert sailing["booking_url"] == (
        "https://app.sindoferry.com.sg/"
    )


def test_ferry_surge_is_grounded():
    surge, source = ferry_schedule.ferry_surge_for_port(None, WEEKDAY_1400)
    assert surge == 0 and source is None, "a corridor with no port reported surge"

    # Probe 20 minutes before a known departure. Note three operators serve
    # Batam Centre, so the surge is driven by whichever sailing is soonest at
    # the probe instant, not necessarily the one used to pick the probe.
    known = ferry_schedule.generate_sailings(
        WEEKDAY_1400, horizon_hours=6, ports=["Batam Centre"])[0]
    probe = datetime.fromisoformat(known["departure_time"]) - timedelta(minutes=20)

    surge, source = ferry_schedule.ferry_surge_for_port("Batam Centre", probe)
    assert surge == 1 and source is not None, "no surge 20 min before a sailing"
    assert source["departure_port"] == "Batam Centre"
    assert 0 <= source["minutes_until_departure"] <= 45, (
        "surge cited a distant sailing"
    )

    soonest = ferry_schedule.generate_sailings(
        probe, horizon_hours=3, ports=["Batam Centre"])[0]
    assert source["sailing_id"] == soonest["sailing_id"], (
        "surge did not cite the imminent sailing")

    # Quiet window: 03:00 has no service at all.
    surge, source = ferry_schedule.ferry_surge_for_port(
        "Batam Centre", datetime(2026, 8, 7, 3, 0, tzinfo=TZ))
    assert surge == 0 and source is None, "surge reported outside service hours"


def test_corridor_surge_reference_does_not_embed_stale_schedule_freshness():
    corridors = get_live_corridor_telemetry(
        datetime(2026, 8, 7, 5, 30, tzinfo=TZ),
    )
    sources = [
        corridor["surge_source"]
        for corridor in corridors
        if corridor["surge_source"] is not None
    ]
    assert sources
    for source in sources:
        assert set(source) == {
            "ferry_name", "departure_port", "minutes_until_departure",
        }


def test_ferry_snapshot_catalog_and_sailing_ids_are_auditable():
    sailings = ferry_schedule.generate_sailings(WEEKDAY_1400, horizon_hours=24)
    sailing_ids = [s["sailing_id"] for s in sailings]
    assert len(sailing_ids) == len(set(sailing_ids))

    metadata = ferry_schedule.timetable_metadata()
    assert metadata["schema_version"] == 1
    assert metadata["status"] == "published_schedule_snapshot"
    assert datetime.fromisoformat(metadata["last_verified_at"]).tzinfo is not None
    assert len(metadata["sources"]) == 4
    assert {source["operator"] for source in metadata["sources"]} == {
        "BatamFast", "Sindo Ferry", "Majestic Fast Ferry",
        "Horizon Fast Ferry",
    }
    assert "not a live" in metadata["limitations"]
    assert all(source["schedule_url"].startswith("https://")
               for source in metadata["sources"])

    weekday_tm = [
        departure.time() for _, departure in ferry_schedule._slots(
            next(route for route in ferry_schedule.FERRY_ROUTES
                 if route.code == "majestic-bct-tm"),
            datetime(2026, 8, 10, tzinfo=TZ).date(),
        )
    ]
    weekend_tm = [
        departure.time() for _, departure in ferry_schedule._slots(
            next(route for route in ferry_schedule.FERRY_ROUTES
                 if route.code == "majestic-bct-tm"),
            datetime(2026, 8, 15, tzinfo=TZ).date(),
        )
    ]
    weekend_only_slot = datetime(2026, 8, 15, 9, 40, tzinfo=TZ).time()
    assert weekend_only_slot not in weekday_tm
    assert weekend_only_slot in weekend_tm

    with clock.frozen(WEEKDAY_1400):
        response = api_main.get_ferries(Response())
    assert response["data_source"] == "published_schedule"
    assert response["timetable"]["snapshot_id"] == metadata["snapshot_id"]
    assert response["provenance"]["ferry_schedule"].endswith(
        "not live operations"
    )


def test_ferry_calendar_exclusions_are_audited_but_not_scheduled():
    expectations = {
        "sindo-tm-bct": (6, "17:40", False),
        "majestic-tm-bct": (0, "09:30", True),
        "majestic-bct-tm": (4, "19:50", True),
    }
    base_monday = datetime(2026, 8, 10).date()
    saturday = base_monday + timedelta(days=5)
    for service_id, (weekday, departure, has_weekend_slot) in expectations.items():
        route = next(
            item for item in ferry_schedule.FERRY_ROUTES
            if item.code == service_id
        )
        excluded_day = base_monday + timedelta(days=weekday)
        excluded_departures = {
            value.strftime("%H:%M")
            for _, value in ferry_schedule._slots(route, excluded_day)
        }
        weekend_departures = {
            value.strftime("%H:%M")
            for _, value in ferry_schedule._slots(route, saturday)
        }
        assert departure not in excluded_departures
        assert (departure in weekend_departures) is has_weekend_slot


def test_ferry_snapshot_service_identities_match_parser_contract():
    services = {
        service["service_id"]: service
        for operator in (
            "BatamFast", "Sindo Ferry", "Majestic Fast Ferry",
            "Horizon Fast Ferry",
        )
        for service in ferry_schedule.published_services_for_operator(operator)
    }
    assert set(services) == set(ferry_refresh._SERVICE_IDENTITIES)
    for service_id, expected in ferry_refresh._SERVICE_IDENTITIES.items():
        service = services[service_id]
        observed = (
            service["departure_port"],
            service["arrival_port"],
            service.get("departure_timezone", "Asia/Jakarta"),
        )
        assert observed == expected


def test_ferry_refresh_uses_only_fixed_official_allowlist_and_preserves_snapshot():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()
    fetched_ids = []

    def fake_fetch(source):
        fetched_ids.append(source.source_id)
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(source),
            status=200,
            final_url=source.url,
            headers={"ETag": '"fixture"'},
        )

    report = ferry_refresh.refresh_official_sources(
        WEEKDAY_1400,
        fetch_source=fake_fetch,
        monotonic_now=100.0,
    )
    assert set(fetched_ids) == {
        source.source_id
        for source in ferry_refresh.OFFICIAL_SOURCES
        if source.fetch_enabled
    }
    assert report["status"] == "checked"
    assert report["summary"] == {
        "verified": 6, "failed": 0, "permission_gated": 0,
    }
    assert len(report["source_results"]) == 6
    assert len({item["source_id"] for item in report["source_results"]}) == 6
    assert all(
        item["status"] == "verified_structure"
        for item in report["source_results"]
    )
    assert "horizon-public-timetable" in fetched_ids
    assert "bp-batam-passenger-ports" in fetched_ids
    assert "scc-live-operations-board" in fetched_ids
    assert report["schedule_verified"] is True
    assert report["schedule_verified_at"] == WEEKDAY_1400.isoformat()
    assert not report["excluded_references"]
    bp_result = next(
        item for item in report["source_results"]
        if item["source_id"] == "bp-batam-passenger-ports"
    )
    scc_result = next(
        item for item in report["source_results"]
        if item["source_id"] == "scc-live-operations-board"
    )
    assert bp_result["source_validation_status"] == "validated_terminal_reference"
    assert bp_result["matched_terminal_count"] == 5
    assert scc_result["source_validation_status"] == (
        "validated_same_day_operations_board"
    )
    assert scc_result["matched_board_row_count"] == 1
    assert report["schedule_validation_status"] == "matched_snapshot"
    assert report["schedule_unchanged"] is True
    assert report["schedule_applied"] is False
    assert report["last_known_good_active"] is True
    assert report["refresh_scope"] == "fixed_official_allowlist"
    assert "arbitrary blogs" in report["limitations"]

    cached = ferry_refresh.refresh_official_sources(
        WEEKDAY_1400 + timedelta(seconds=5),
        fetch_source=lambda source: (_ for _ in ()).throw(
            AssertionError(f"cached refresh fetched {source.source_id}")
        ),
        monotonic_now=105.0,
    )
    assert cached["status"] == "cached"
    assert cached["cache_age_seconds"] == 5.0
    assert cached["schedule_verified_at"] == report["schedule_verified_at"]


def test_ferry_refresh_uses_bp_official_json_fallback_after_bad_primary_page():
    source = next(
        item for item in ferry_refresh.OFFICIAL_SOURCES
        if item.source_id == "bp-batam-passenger-ports"
    )
    calls = []
    compact_fallback = (
        '{"content":"Pelabuhan Penumpang Terminal Ferry Internasional '
        'melayani Singapura dan Malaysia. Pelabuhan Penumpang Internasional '
        'terdiri dari: Batam Centre Sekupang Harbour Bay Kinerja Operasional '
        'PENUMPANG INTERNASIONAL"}'
    ).encode()

    def fake_fetch_url(url, *, attempt_count):
        calls.append((url, attempt_count))
        # The primary contains every shallow marker but is still an incomplete
        # terminal catalogue. Deep validation must trigger the JSON fallback.
        body = (
            b"<html>Pelabuhan Penumpang Terminal Ferry Internasional "
            b"Singapura Malaysia Kinerja Operasional PENUMPANG INTERNASIONAL"
            b"</html>"
        )
        if attempt_count == 2:
            body = compact_fallback
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=url,
            headers={},
            requested_url=url,
            attempt_count=attempt_count,
        )

    original_fetch_url = ferry_refresh._fetch_url
    try:
        ferry_refresh._fetch_url = fake_fetch_url
        fetched = ferry_refresh._fetch_source(source)
        result = ferry_refresh._inspect_source(
            source, fetched, WEEKDAY_1400.isoformat(),
        )
    finally:
        ferry_refresh._fetch_url = original_fetch_url

    assert calls == [
        (source.url, 1),
        (source.fallback_urls[0], 2),
    ]
    assert result["status"] == "verified_structure"
    assert result["used_official_fallback"] is True
    assert result["fetch_attempt_count"] == 2
    assert result["matched_terminal_count"] == 3
    assert result["used_compact_official_representation"] is True


def test_scc_refresh_rejects_a_board_for_a_different_requested_date():
    source = next(
        item for item in ferry_refresh.OFFICIAL_SOURCES
        if item.source_id == "scc-live-operations-board"
    )
    requested_day = datetime(
        2026, 8, 13, 8, 0, tzinfo=timezone(timedelta(hours=8)),
    )
    stale_day = requested_day.date() - timedelta(days=1)
    requested_url = (
        f"{source.url}?ferry-status=arrival&date="
        f"{requested_day:%Y%m%d}&time=all&origin=all&destination=all&ferry=all"
    )
    fetched = ferry_refresh.FetchedSource(
        body=_scc_fixture_body("stale", stale_day),
        status=200,
        final_url=requested_url,
        requested_url=requested_url,
        headers={},
    )

    try:
        ferry_refresh._inspect_source(
            source,
            fetched,
            requested_day.isoformat(),
        )
    except ValueError as error:
        assert "different date" in str(error)
    else:
        raise AssertionError("a stale SCC operations board was accepted as same-day")


def test_non_timetable_source_failure_does_not_block_operator_verification():
    ferry_refresh._reset_refresh_state_for_tests()

    def source_fetch(source):
        if source.source_id == "scc-live-operations-board":
            raise TimeoutError("fixture timeout")
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(source),
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            WEEKDAY_1400,
            fetch_source=source_fetch,
            monotonic_now=107.0,
        )
    finally:
        ferry_refresh._reset_refresh_state_for_tests()

    assert report["status"] == "partial"
    assert report["summary"] == {
        "verified": 5, "failed": 1, "permission_gated": 0,
    }
    assert report["schedule_verified"] is True
    assert report["schedule_verified_at"] == WEEKDAY_1400.isoformat()
    assert report["schedule_unchanged"] is True
    failed = next(
        item for item in report["source_results"]
        if item["source_id"] == "scc-live-operations-board"
    )
    assert failed["status"] == "unavailable_or_invalid"


def test_ferry_refresh_endpoint_advances_latest_verified_time_atomically():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()
    verification_time = datetime(2026, 8, 13, 10, 25, tzinfo=TZ)

    def complete_fetch(source, **_kwargs):
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(
                source,
                "30",
                scc_day=verification_time.date(),
            ),
            status=200,
            final_url=source.url,
            headers={},
        )

    original_fetch = ferry_refresh._fetch_source
    try:
        ferry_refresh._fetch_source = complete_fetch
        with clock.frozen(verification_time):
            response = api_main.refresh_ferry_sources(Response())

        expected = verification_time.isoformat()
        assert response["refresh"]["schedule_verified"] is True
        assert response["refresh"]["schedule_verified_at"] == expected
        assert response["refresh"]["latest_checked_at"] == expected
        assert response["timetable"]["last_verified_at"] == expected
        assert response["timetable"]["latest_checked_at"] == expected
        assert response["timetable"]["snapshot_verified_at"] == (
            "2026-08-13T00:30:50+07:00"
        )
        assert response["timetable"]["snapshot_last_verified_at"] == (
            "2026-08-13T00:30:50+07:00"
        )
        assert all(
            source["snapshot_verified_at"] == "2026-08-13T00:30:50+07:00"
            and source["last_verified_at"] == expected
            and source["latest_successful_validation_at"] == expected
            for source in response["timetable"]["sources"]
        )
        assert all(
            sailing["schedule_last_verified_at"] == expected
            and sailing["schedule_snapshot_verified_at"]
                == "2026-08-13T00:30:50+07:00"
            for sailing in response["ferries"]
        )

        with clock.frozen(verification_time + timedelta(seconds=5)):
            follow_up = api_main.get_ferries(Response())
            cached = api_main.refresh_ferry_sources(Response())
        assert follow_up["timetable"]["last_verified_at"] == expected
        assert cached["refresh"]["status"] == "cached"
        assert cached["timetable"]["last_verified_at"] == expected
    finally:
        ferry_refresh._fetch_source = original_fetch
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_ferry_refresh_uses_completion_time_for_check_and_verification():
    ferry_refresh._reset_refresh_state_for_tests()
    started = WEEKDAY_1400
    completed = started + timedelta(seconds=8)

    def complete_fetch(source, **_kwargs):
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(source),
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            started,
            fetch_source=complete_fetch,
            monotonic_now=110.0,
            completed_now=lambda: completed,
        )
        assert report["started_at"] == started.isoformat()
        assert report["finished_at"] == completed.isoformat()
        assert report["latest_checked_at"] == completed.isoformat()
        assert report["schedule_verified_at"] == completed.isoformat()
        assert all(
            result["checked_at"] == completed.isoformat()
            for result in report["source_results"]
        )
    finally:
        ferry_refresh._reset_refresh_state_for_tests()


def test_ferry_freshness_survives_process_reset_with_shared_store():
    stored = {}
    original_load = ferry_freshness_store.load
    original_save = ferry_freshness_store.save
    original_configured = ferry_freshness_store.configured
    original_available = ferry_freshness_store.available

    def fake_load(snapshot_id):
        assert snapshot_id == ferry_schedule.timetable_metadata(
            load_durable=False,
        )["snapshot_id"]
        return dict(stored) if stored else None

    def fake_save(snapshot_id, latest_checked_at, last_verified_at):
        current_checked = stored.get("latest_checked_at", latest_checked_at)
        current_verified = stored.get("last_verified_at", last_verified_at)
        stored.update({
            "snapshot_id": snapshot_id,
            "latest_checked_at": max(current_checked, latest_checked_at),
            "last_verified_at": max(current_verified, last_verified_at),
        })
        return dict(stored)

    try:
        ferry_freshness_store.load = fake_load
        ferry_freshness_store.save = fake_save
        ferry_freshness_store.configured = lambda: True
        ferry_freshness_store.available = lambda: True
        ferry_schedule._reset_runtime_verification_for_tests()
        checked = "2026-08-14T09:00:08+07:00"
        verified = "2026-08-14T09:00:08+07:00"
        ferry_schedule.record_refresh_result(checked, verified)

        # A reset models a fresh serverless process; metadata must reload the
        # shared monotonic row instead of falling back to the bundled date.
        ferry_schedule._reset_runtime_verification_for_tests()
        metadata = ferry_schedule.timetable_metadata()
        assert metadata["latest_checked_at"] == checked
        assert metadata["last_verified_at"] == verified
        assert metadata["freshness_durability"] == "shared_supabase"

        ferry_schedule.record_refresh_result(
            "2026-08-14T08:59:00+07:00",
            "2026-08-14T08:59:00+07:00",
        )
        assert stored["latest_checked_at"] == checked
        assert stored["last_verified_at"] == verified
    finally:
        ferry_freshness_store.load = original_load
        ferry_freshness_store.save = original_save
        ferry_freshness_store.configured = original_configured
        ferry_freshness_store.available = original_available
        ferry_schedule._reset_runtime_verification_for_tests()


def test_production_ferry_freshness_fails_closed_without_shared_store():
    env_names = (
        "VERCEL_ENV", "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS",
    )
    originals = {name: os.environ.get(name) for name in env_names}
    original_load = ferry_freshness_store.load
    original_save = ferry_freshness_store.save
    try:
        os.environ["VERCEL_ENV"] = "production"
        os.environ.pop("CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS", None)
        ferry_freshness_store.load = lambda snapshot_id: None
        ferry_freshness_store.save = lambda *args: None
        assert ferry_freshness_store.required() is True
        try:
            api_main.get_ferries(Response())
        except HTTPException as error:
            assert error.status_code == 503
            assert "older timestamp" in error.detail
        else:
            raise AssertionError("production served process-local ferry freshness")

        # A copied local fallback value must never weaken Preview/Production.
        os.environ["CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"] = "0"
        assert ferry_freshness_store.required() is True

        os.environ.pop("VERCEL_ENV")
        assert ferry_freshness_store.required() is False
        os.environ["CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"] = "typo"
        try:
            ferry_freshness_store.required()
        except ferry_freshness_store.FreshnessStoreUnavailable:
            pass
        else:
            raise AssertionError("an invalid durability setting was ignored")
    finally:
        ferry_freshness_store.load = original_load
        ferry_freshness_store.save = original_save
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_required_ferry_freshness_seeds_a_new_snapshot_monotonically():
    env_name = "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"
    original_env = os.environ.get(env_name)
    original_load = ferry_freshness_store.load
    original_save = ferry_freshness_store.save
    calls = []

    def seed(snapshot_id, latest_checked_at, last_verified_at):
        calls.append((snapshot_id, latest_checked_at, last_verified_at))
        return {
            "snapshot_id": snapshot_id,
            "latest_checked_at": latest_checked_at,
            "last_verified_at": last_verified_at,
        }

    try:
        os.environ[env_name] = "1"
        ferry_freshness_store.load = lambda snapshot_id: None
        ferry_freshness_store.save = seed
        ferry_schedule._reset_runtime_verification_for_tests()
        metadata = ferry_schedule.timetable_metadata()
        assert calls == [(
            metadata["snapshot_id"],
            metadata["snapshot_verified_at"],
            metadata["snapshot_verified_at"],
        )]
        assert metadata["last_verified_at"] == metadata["snapshot_verified_at"]
        assert metadata["latest_checked_at"] == metadata["snapshot_verified_at"]
    finally:
        ferry_freshness_store.load = original_load
        ferry_freshness_store.save = original_save
        ferry_schedule._reset_runtime_verification_for_tests()
        if original_env is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original_env


def test_required_ferry_freshness_rejects_a_row_older_than_active_snapshot():
    env_name = "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"
    original_env = os.environ.get(env_name)
    original_load = ferry_freshness_store.load
    try:
        os.environ[env_name] = "1"
        snapshot_id = ferry_schedule.timetable_metadata(
            load_durable=False,
        )["snapshot_id"]
        ferry_freshness_store.load = lambda candidate: {
            "snapshot_id": snapshot_id,
            "latest_checked_at": "2026-08-12T01:00:00+07:00",
            "last_verified_at": "2026-08-12T01:00:00+07:00",
        }
        try:
            ferry_schedule.timetable_metadata()
        except ferry_freshness_store.FreshnessStoreUnavailable as error:
            assert "older than the active snapshot" in str(error)
        else:
            raise AssertionError("an obsolete shared timestamp was served as latest")
    finally:
        ferry_freshness_store.load = original_load
        if original_env is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original_env


def test_required_ferry_refresh_returns_503_when_latest_cannot_be_persisted():
    env_name = "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"
    original_env = os.environ.get(env_name)
    original_save = ferry_freshness_store.save
    original_load = ferry_freshness_store.load
    original_fetch = ferry_refresh._fetch_source

    def complete_fetch(source, **_kwargs):
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(source),
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        os.environ[env_name] = "1"
        ferry_freshness_store.save = lambda *args: None
        durable_row = {
            "snapshot_id": ferry_schedule.timetable_metadata(
                load_durable=False,
            )["snapshot_id"],
            "latest_checked_at": "2026-08-13T00:30:50+07:00",
            "last_verified_at": "2026-08-13T00:30:50+07:00",
        }
        ferry_freshness_store.load = lambda snapshot_id: dict(durable_row)
        ferry_refresh._fetch_source = complete_fetch
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()
        with clock.frozen(WEEKDAY_1400):
            try:
                api_main.refresh_ferry_sources(Response())
            except HTTPException as error:
                assert error.status_code == 503
                assert "durably published" in error.detail
                assert error.headers["Cache-Control"] == "private, no-store"
                assert error.headers["Retry-After"] == "5"
            else:
                raise AssertionError("refresh claimed an unpersisted latest timestamp")

        # The rejected candidate must not survive in process memory or win over
        # the older authoritative row on the next request.
        metadata = ferry_schedule.timetable_metadata()
        assert metadata["latest_checked_at"] == durable_row["latest_checked_at"]
        assert metadata["last_verified_at"] == durable_row["last_verified_at"]
    finally:
        ferry_freshness_store.save = original_save
        ferry_freshness_store.load = original_load
        ferry_refresh._fetch_source = original_fetch
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()
        if original_env is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original_env


def test_required_ferry_refresh_rejects_an_inconsistent_saved_row():
    env_name = "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"
    original_env = os.environ.get(env_name)
    original_save = ferry_freshness_store.save
    try:
        os.environ[env_name] = "1"
        snapshot_id = ferry_schedule.timetable_metadata(
            load_durable=False,
        )["snapshot_id"]
        ferry_freshness_store.save = lambda *args: {
            "snapshot_id": snapshot_id,
            "latest_checked_at": "2026-08-14T08:59:59+07:00",
            "last_verified_at": "2026-08-14T09:00:00+07:00",
        }
        try:
            ferry_schedule.record_refresh_result(
                "2026-08-14T09:00:00+07:00",
                "2026-08-14T09:00:00+07:00",
            )
        except ferry_freshness_store.FreshnessStoreUnavailable as error:
            assert "inconsistent" in str(error)
        else:
            raise AssertionError("an impossible shared freshness row was accepted")
    finally:
        ferry_freshness_store.save = original_save
        ferry_schedule._reset_runtime_verification_for_tests()
        if original_env is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original_env


def test_ferry_freshness_store_supports_modern_and_legacy_server_keys():
    names = (
        "SUPABASE_URL",
        "SUPABASE_PROJECT_URL",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CROSSFLOW_SUPABASE_ALLOWED_HOST",
    )
    originals = {name: os.environ.get(name) for name in names}
    original_reason = ferry_freshness_store.failure_reason()
    modern_secret = "sb_secret_modern-server-only-value"
    legacy_service_role = "legacy-service-role-jwt"
    try:
        os.environ["SUPABASE_URL"] = "https://project.supabase.co"
        os.environ.pop("SUPABASE_PROJECT_URL", None)
        os.environ.pop("CROSSFLOW_SUPABASE_ALLOWED_HOST", None)

        # Modern Supabase secret keys are opaque API keys, not JWTs. Sending
        # one in an Authorization: Bearer header can cause PostgREST to reject
        # an otherwise valid server credential.
        os.environ["SUPABASE_SECRET_KEY"] = modern_secret
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = legacy_service_role
        modern_config = ferry_freshness_store._config()
        assert modern_config is not None
        assert modern_config["key"] == modern_secret
        modern_headers = ferry_freshness_store._headers(modern_config["key"])
        assert modern_headers["apikey"] == modern_secret
        assert "Authorization" not in modern_headers

        # Existing deployments using the legacy service-role JWT keep the
        # Bearer header required by that credential type.
        os.environ.pop("SUPABASE_SECRET_KEY")
        legacy_config = ferry_freshness_store._config()
        assert legacy_config is not None
        assert legacy_config["key"] == legacy_service_role
        legacy_headers = ferry_freshness_store._headers(legacy_config["key"])
        assert legacy_headers["apikey"] == legacy_service_role
        assert legacy_headers["Authorization"] == (
            f"Bearer {legacy_service_role}"
        )
    finally:
        ferry_freshness_store._last_failure_reason = original_reason
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_ferry_freshness_store_reports_safe_configuration_reasons():
    names = (
        "SUPABASE_URL",
        "SUPABASE_PROJECT_URL",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CROSSFLOW_SUPABASE_ALLOWED_HOST",
    )
    originals = {name: os.environ.get(name) for name in names}
    original_reason = ferry_freshness_store.failure_reason()
    secret = "sb_secret_never-include-in-diagnostics"
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["SUPABASE_SECRET_KEY"] = secret
        assert ferry_freshness_store.load("diagnostic-snapshot") is None
        assert ferry_freshness_store.failure_reason() == "missing_project_url"

        # A client credential is not cryptographically bound here to the
        # separate server key. Never let it select where that elevated key is
        # sent; the project URL must be configured explicitly.
        os.environ["SUPABASE_ANON_KEY"] = "client-key-from-another-project"
        assert ferry_freshness_store.load("diagnostic-snapshot") is None
        assert ferry_freshness_store.failure_reason() == "missing_project_url"

        os.environ["SUPABASE_URL"] = "https://project.supabase.co"
        os.environ.pop("SUPABASE_SECRET_KEY")
        assert ferry_freshness_store.load("diagnostic-snapshot") is None
        assert ferry_freshness_store.failure_reason() == "missing_server_key"

        os.environ["SUPABASE_SECRET_KEY"] = secret
        os.environ["SUPABASE_URL"] = "https://example.com/private-project"
        assert ferry_freshness_store.load("diagnostic-snapshot") is None
        assert ferry_freshness_store.failure_reason() == "invalid_project_url"

        reason = ferry_freshness_store.failure_reason() or ""
        assert secret not in reason
        assert "example.com" not in reason
        assert "private-project" not in reason
    finally:
        ferry_freshness_store._last_failure_reason = original_reason
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_ferry_freshness_store_reports_safe_request_failure_reasons():
    names = (
        "SUPABASE_URL",
        "SUPABASE_PROJECT_URL",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CROSSFLOW_SUPABASE_ALLOWED_HOST",
    )
    originals = {name: os.environ.get(name) for name in names}
    original_opener = ferry_freshness_store._OPENER
    original_retry = ferry_freshness_store._retry_after_monotonic
    original_available = ferry_freshness_store._last_backend_available
    original_reason = getattr(
        ferry_freshness_store, "_last_failure_reason", None,
    )
    secret = "sb_secret_http-body-must-not-leak"
    project_url = "https://project.supabase.co"

    class HttpFailureOpener:
        def open(self, request, timeout):  # noqa: ANN001, ARG002
            raise ferry_freshness_store.HTTPError(
                f"{project_url}/rest/v1/private?token={secret}",
                404,
                f"table missing; debug credential={secret}",
                {},
                None,
            )

    class InvalidResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                f"not-json url={project_url} credential={secret}"
            ).encode()

    class InvalidResponseOpener:
        def open(self, request, timeout):  # noqa: ANN001, ARG002
            return InvalidResponse()

    class NetworkFailureOpener:
        def open(self, request, timeout):  # noqa: ANN001, ARG002
            raise ferry_freshness_store.URLError(
                f"network detail url={project_url} credential={secret}",
            )

    class StatusFailureResponse:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return f"private response credential={secret}".encode()

    class StatusFailureOpener:
        def open(self, request, timeout):  # noqa: ANN001, ARG002
            return StatusFailureResponse()

    try:
        os.environ["SUPABASE_URL"] = project_url
        os.environ.pop("SUPABASE_PROJECT_URL", None)
        os.environ["SUPABASE_SECRET_KEY"] = secret
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        os.environ.pop("CROSSFLOW_SUPABASE_ALLOWED_HOST", None)

        ferry_freshness_store._retry_after_monotonic = 0.0
        ferry_freshness_store._OPENER = HttpFailureOpener()
        assert ferry_freshness_store.load("missing-table") is None
        assert ferry_freshness_store.failure_reason() == "http_404"

        # A second request inside the bounded retry window is also diagnosable
        # without reissuing a network request or exposing the prior exception.
        assert ferry_freshness_store.load("missing-table") is None
        assert ferry_freshness_store.failure_reason() == "request_backoff"

        ferry_freshness_store._retry_after_monotonic = 0.0
        ferry_freshness_store._OPENER = NetworkFailureOpener()
        assert ferry_freshness_store.load("network-failure") is None
        assert ferry_freshness_store.failure_reason() == "network_error"

        # A failed write reports only its status class; neither the row nor a
        # private response body can enter the operator-facing diagnostic.
        ferry_freshness_store._retry_after_monotonic = 0.0
        ferry_freshness_store._OPENER = StatusFailureOpener()
        assert ferry_freshness_store.save(
            "missing-table",
            "2026-08-13T12:00:00+07:00",
            "2026-08-13T12:00:00+07:00",
        ) is None
        assert ferry_freshness_store.failure_reason() == "http_503"

        ferry_freshness_store._retry_after_monotonic = 0.0
        ferry_freshness_store._OPENER = InvalidResponseOpener()
        assert ferry_freshness_store.load("invalid-json") is None
        assert ferry_freshness_store.failure_reason() == "invalid_response"

        reason = ferry_freshness_store.failure_reason() or ""
        assert secret not in reason
        assert project_url not in reason
        assert "not-json" not in reason
    finally:
        ferry_freshness_store._OPENER = original_opener
        ferry_freshness_store._retry_after_monotonic = original_retry
        ferry_freshness_store._last_backend_available = original_available
        if hasattr(ferry_freshness_store, "_last_failure_reason"):
            ferry_freshness_store._last_failure_reason = original_reason
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_ferry_freshness_store_rejects_unsafe_origins_and_redirects():
    names = (
        "SUPABASE_URL",
        "SUPABASE_PROJECT_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "CROSSFLOW_SUPABASE_ALLOWED_HOST",
    )
    originals = {name: os.environ.get(name) for name in names}
    try:
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
        os.environ.pop("SUPABASE_PROJECT_URL", None)
        os.environ.pop("CROSSFLOW_SUPABASE_ALLOWED_HOST", None)

        for unsafe in (
            "http://project.supabase.co",
            "https://example.com",
            "https://user:pass@project.supabase.co",
            "https://project.supabase.co/rest/v1",
            "https://project.supabase.co?redirect=example.com",
        ):
            os.environ["SUPABASE_URL"] = unsafe
            assert ferry_freshness_store._config() is None

        os.environ["SUPABASE_URL"] = "https://project.supabase.co/"
        assert ferry_freshness_store._config()["url"] == (
            "https://project.supabase.co"
        )

        os.environ["SUPABASE_URL"] = "https://db.crossflow.internal"
        assert ferry_freshness_store._config() is None
        os.environ["CROSSFLOW_SUPABASE_ALLOWED_HOST"] = (
            "db.crossflow.internal"
        )
        assert ferry_freshness_store._config()["url"] == (
            "https://db.crossflow.internal"
        )

        handler = ferry_freshness_store._NoRedirectHandler()
        assert handler.redirect_request(
            None, None, 302, "Found", {}, "https://example.com",
        ) is None
    finally:
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_ferry_refresh_isolates_source_failures_and_endpoint_refreshes_pipeline():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()

    def partial_fetch(source, **_kwargs):
        if source.source_id == "sindo-public-timetable":
            raise TimeoutError("fixture timeout")
        return ferry_refresh.FetchedSource(
            body=_official_source_fixture_body(source, "15"),
            status=200,
            final_url=source.url,
            headers={},
        )

    original_fetch = ferry_refresh._fetch_source
    try:
        ferry_refresh._fetch_source = partial_fetch
        with clock.frozen(WEEKDAY_1400):
            response = api_main.refresh_ferry_sources(Response())
    finally:
        ferry_refresh._fetch_source = original_fetch
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()

    refresh = response["refresh"]
    assert refresh["status"] == "partial"
    assert refresh["summary"] == {
        "verified": 5, "failed": 1, "permission_gated": 0,
    }
    assert refresh["schedule_verified"] is False
    assert refresh["schedule_verified_at"] == "2026-08-13T00:30:50+07:00"
    assert refresh["schedule_unchanged"] is None
    failed = next(
        result for result in refresh["source_results"]
        if result["source_id"] == "sindo-public-timetable"
    )
    assert failed["status"] == "unavailable_or_invalid"
    assert "within the refresh window" in failed["warning"]
    assert response["data_source"] == "published_schedule"
    assert response["ferries"]
    assert len(response["ports"]) == 4
    assert response["timetable"]["snapshot_id"] == (
        ferry_schedule.timetable_metadata()["snapshot_id"]
    )
    assert response["timetable"]["last_verified_at"] == (
        "2026-08-13T00:30:50+07:00"
    )
    assert response["timetable"]["latest_checked_at"] == WEEKDAY_1400.isoformat()


def test_ferry_refresh_rejects_operator_page_missing_committed_departure():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()

    def changed_horizon_fetch(source):
        body = _official_source_fixture_body(source)
        if source.source_id == "horizon-public-timetable":
            body = body.replace(b"21:35", b"21:34")
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            WEEKDAY_1400,
            fetch_source=changed_horizon_fetch,
            monotonic_now=200.0,
        )
        horizon = next(
            result for result in report["source_results"]
            if result["source_id"] == "horizon-public-timetable"
        )
        assert horizon["status"] == "unavailable_or_invalid"
        assert horizon["schedule_mismatch"] is True
        assert "21:35" in horizon["warning"]
        assert report["schedule_verified"] is False
        assert report["schedule_verified_at"] == "2026-08-13T00:30:50+07:00"
        assert report["schedule_unchanged"] is None
        assert report["schedule_change_detected"] is True
        assert report["data_changed"] is True
    finally:
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_ferry_refresh_rejects_same_times_swapped_between_directions():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()

    def swapped_horizon_fetch(source):
        body = _official_source_fixture_body(source)
        if source.source_id == "horizon-public-timetable":
            body = (
                body.replace(b"21:35", b"XX:XX")
                .replace(b"20:30", b"21:35")
                .replace(b"XX:XX", b"20:30")
            )
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            WEEKDAY_1400,
            fetch_source=swapped_horizon_fetch,
            monotonic_now=300.0,
        )
        horizon = next(
            result for result in report["source_results"]
            if result["source_id"] == "horizon-public-timetable"
        )
        assert horizon["status"] == "unavailable_or_invalid"
        assert horizon["schedule_mismatch"] is True
        assert "horizon-hf-hb (daily)" in horizon["warning"]
        assert report["schedule_verified"] is False
        assert report["schedule_validation_status"] == "incomplete"
    finally:
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_ferry_refresh_rejects_calendar_icon_change_with_same_times():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()

    def changed_calendar_fetch(source):
        body = _official_source_fixture_body(source)
        if source.source_id == "sindo-public-timetable":
            body = body.replace(b"ship6.png", b"ship2.png", 1)
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            WEEKDAY_1400,
            fetch_source=changed_calendar_fetch,
            monotonic_now=400.0,
        )
        sindo = next(
            result for result in report["source_results"]
            if result["source_id"] == "sindo-public-timetable"
        )
        assert sindo["status"] == "unavailable_or_invalid"
        assert sindo["schedule_mismatch"] is True
        assert "weekend_additions" in sindo["warning"] or "daily" in sindo["warning"]
        assert report["schedule_verified"] is False
    finally:
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_ferry_refresh_rejects_route_timezone_header_change():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()

    def changed_timezone_fetch(source):
        body = _official_source_fixture_body(source)
        if source.source_id == "sindo-public-timetable":
            body = body.replace(
                b"(Singapore Time)", b"(Indonesia Time)", 1,
            )
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=source.url,
            headers={},
        )

    try:
        report = ferry_refresh.refresh_official_sources(
            WEEKDAY_1400,
            fetch_source=changed_timezone_fetch,
            monotonic_now=450.0,
        )
        sindo = next(
            result for result in report["source_results"]
            if result["source_id"] == "sindo-public-timetable"
        )
        assert sindo["status"] == "unavailable_or_invalid"
        assert "sindo-hf-bct is missing" in sindo["warning"]
        assert report["schedule_verified"] is False
    finally:
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_failed_semantic_check_advances_checked_not_verified_timestamp():
    ferry_refresh._reset_refresh_state_for_tests()
    ferry_schedule._reset_runtime_verification_for_tests()
    check_time = datetime(2026, 8, 13, 11, 45, tzinfo=TZ)

    def changed_route_fetch(source, **_kwargs):
        body = _official_source_fixture_body(source)
        if source.source_id == "scc-live-operations-board":
            body = _official_source_fixture_body(
                source,
                scc_day=check_time.date(),
            )
        if source.source_id == "horizon-public-timetable":
            body = body.replace(b"21:35", b"21:36")
        return ferry_refresh.FetchedSource(
            body=body,
            status=200,
            final_url=source.url,
            headers={},
        )

    original_fetch = ferry_refresh._fetch_source
    try:
        ferry_refresh._fetch_source = changed_route_fetch
        with clock.frozen(check_time):
            response = api_main.refresh_ferry_sources(Response())
        assert response["refresh"]["latest_checked_at"] == check_time.isoformat()
        assert response["refresh"]["schedule_verified"] is False
        assert response["timetable"]["latest_checked_at"] == check_time.isoformat()
        assert response["timetable"]["last_verified_at"] == (
            "2026-08-13T00:30:50+07:00"
        )
        assert response["timetable"]["snapshot_verified_at"] == (
            "2026-08-13T00:30:50+07:00"
        )
    finally:
        ferry_refresh._fetch_source = original_fetch
        ferry_refresh._reset_refresh_state_for_tests()
        ferry_schedule._reset_runtime_verification_for_tests()


def test_ferry_refresh_rejects_redirects_outside_allowlist():
    try:
        ferry_refresh._require_fetchable_url("https://example.com/schedule")
    except ValueError as err:
        assert "allowlist" in str(err)
    else:
        raise AssertionError("an arbitrary refresh URL was accepted")


# --------------------------------------------------------------------------
# Telemetry, CO2, alerts
# --------------------------------------------------------------------------

def test_co2_is_derived_not_constant():
    morning = co2_accrued_today(datetime(2026, 8, 7, 9, 0, tzinfo=TZ))
    midday = co2_accrued_today(datetime(2026, 8, 7, 13, 0, tzinfo=TZ))
    evening = co2_accrued_today(EVENING)

    assert morning["accrued_kg"] != 428.5, "still the hardcoded constant"
    assert morning["accrued_kg"] < midday["accrued_kg"] < evening["accrued_kg"], (
        "accrued CO2 is not increasing through the day")

    just_after_midnight = co2_accrued_today(datetime(2026, 8, 7, 0, 1, tzinfo=TZ))
    assert just_after_midnight["accrued_kg"] < 15

    assert 400 <= evening["projected_full_day_kg"] <= 700, (
        f"full-day projection {evening['projected_full_day_kg']} outside sane range")
    assert abs(sum(evening["by_corridor_kg"].values()) - evening["accrued_kg"]) < 0.5, (
        "per-corridor CO2 does not sum to the headline figure")


def test_operations_aliases_and_methodology_do_not_claim_observed_data():
    summary = get_operations_summary(WEEKDAY_1400)

    assert summary["modeled_avoidable_emissions_opportunity_kg_today"] == (
        summary["total_co2_reduced_today_kg"]
    )
    assert summary["modeled_projected_full_day_avoidable_emissions_kg"] == (
        summary["projected_full_day_co2_kg"]
    )
    assert summary["scheduled_ferry_departures_next_12h"] == (
        summary["active_ferry_sailings"]
    )

    methodology = summary["operations_methodology"]
    assert methodology["observed"] is False
    assert methodology["source"]
    assert "tomtom" not in json.dumps(methodology).lower()
    assert methodology["model"]["id"] == "crossflow_operations_scenario_v1"
    assert set(methodology["scopes"]) == {"network", "emissions", "ferries"}
    assert set(methodology["assumptions"]) == {
        "current_network_kg_per_hour",
        "fixed_fleet_split",
        "hourly_curves",
    }
    for assumption in methodology["assumptions"].values():
        assert assumption["classification"] == "illustrative_scenario_assumption"
        assert assumption["observed"] is False
        assert assumption["live"] is False
        assert assumption["measured"] is False

    with clock.frozen(WEEKDAY_1400):
        response = api_main.get_ops()
    assert response["data_source"] == "simulated"
    assert response["api_source"] == "simulated"
    assert response["operations_methodology"]["observed"] is False
    assert "modelled" in response["provenance"]["operations"].lower()
    assert "synthetic" in response["provenance"]["traffic"].lower()
    assert "tomtom" not in json.dumps(response["provenance"]).lower()


def test_alerts_are_grounded():
    for moment in (WEEKDAY_1400, EVENING, LATE_NIGHT, WEEKEND_1800):
        corridors = get_live_corridor_telemetry(moment)
        alerts = build_alerts(corridors, moment)
        by_id = {c["id"]: c for c in corridors}

        assert alerts, f"no alerts at {moment}"
        for alert in alerts:
            assert alert["corridor_id"] in by_id, "alert names a corridor that does not exist"
            ts = datetime.fromisoformat(alert["timestamp"])
            assert ts.tzinfo is not None, "naive timestamp"
            assert moment - timedelta(hours=1) <= ts <= moment, "timestamp out of range"

        criticals = [a for a in alerts if a["severity"] == "CRITICAL"]
        for alert in criticals:
            assert by_id[alert["corridor_id"]]["status"] == "CRITICAL", (
                "CRITICAL alert for a non-critical corridor")

        summary = get_operations_summary(moment)
        assert len(criticals) <= summary["active_bottlenecks"], (
            "more critical alerts than the bottleneck count admits")
        if summary["active_bottlenecks"] == 0:
            assert not criticals, "critical alerts with zero bottlenecks"


def test_bottleneck_list_matches_count():
    for moment in (WEEKDAY_1400, EVENING, LATE_NIGHT):
        summary = get_operations_summary(moment)
        assert len(summary["bottleneck_corridors"]) == summary["active_bottlenecks"]
        for entry in summary["bottleneck_corridors"]:
            assert entry["score"] > BOTTLENECK_THRESHOLD


def test_weekend_flag_follows_calendar():
    assert all(c["is_weekend"] for c in get_live_corridor_telemetry(WEEKEND_1800))
    assert not any(c["is_weekend"] for c in get_live_corridor_telemetry(WEEKDAY_1400))


# --------------------------------------------------------------------------
# Route solver
# --------------------------------------------------------------------------

def test_route_ferries_are_boardable():
    result = optimize_route("corridor-1", "CARGO_TRUCK", hour=14, weather=1,
                            now=WEEKDAY_1400)
    departure = datetime.fromisoformat(result["planned_departure"])
    arrival = departure + timedelta(minutes=result["total_eta_mins"])

    assert result["next_matching_ferries"], "no onward connections offered"
    assert len(result["next_matching_ferries"]) <= 3
    for sailing in result["next_matching_ferries"]:
        sail_at = datetime.fromisoformat(sailing["departure_time"])
        assert sail_at >= arrival + timedelta(minutes=ferry_schedule.BOARDING_CUTOFF_MINS), (
            "offered a sailing that departs before the traveller can board")

    assert result["corridor"]["distance_km"] > 0
    assert result["route_geometry"], "no drawable geometry"
    assert result["route_data_source"] == "openstreetmap"
    assert result["route_type"] == "ROAD_ROUTE"
    traffic_snapshot = result["planning_traffic_snapshot"]
    assert traffic_snapshot["effective_at"] == result["planned_departure"]
    assert traffic_snapshot["weather"] == 1
    assert traffic_snapshot["observed"] is False
    assert traffic_snapshot["applied_to_returned_route"] is True
    assert traffic_snapshot["zone_count"] == 15
    assert len(traffic_snapshot["zones"]) == 15
    assert sum(traffic_snapshot["congestion_level_counts"].values()) == 15
    assert traffic_snapshot["emissions_pressure_model"] == (
        live_traffic.EMISSIONS_PRESSURE_MODEL
    )
    assert traffic_snapshot["zones"] == live_traffic.get_congestion_zones(
        departure,
        weather=1,
    )
    assert result["routing_model"]["spatial_zone_count"] == 15
    assert result["snap_info"]["origin_snap_m"] > 0
    assert result["snap_info"]["destination_snap_m"] > 0
    expected_access_km = round(
        (
            result["snap_info"]["origin_snap_m"]
            + result["snap_info"]["destination_snap_m"]
        ) / 1000.0,
        3,
    )
    exact_access_mins = expected_access_km / route_solver.ACCESS_CONNECTOR_SPEED_KPH * 60.0
    assert result["access_distance_km"] == expected_access_km
    assert result["snap_info"]["included_in_road_distance"] is False
    assert result["access_time_mins"] >= exact_access_mins
    assert result["access_time_mins"] < exact_access_mins + 0.21
    graph_route = router.route_between("mukakuning", "batam_centre")
    assert graph_route is not None
    assert result["corridor"]["distance_km"] == graph_route["distance_km"]
    assert result["total_eta_mins"] == round(
        result["estimated_travel_time_mins"]
        + result["customs_buffer_mins"]
        + result["access_time_mins"],
        1,
    )
    assert "shortcuts_used" not in result
    assert result["navigation"]["maneuvers"][0]["type"] == "DEPART"
    assert result["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"
    assert 1 <= len(result["alternative_routes"]) <= 2
    assert result["alternatives_note"] in {
        None,
        "Fewer than two sufficiently distinct routes satisfy the detour limit.",
    }
    for alternative in result["alternative_routes"]:
        assert alternative["id"] and alternative["name"]
        assert len(alternative["route_geometry"]) >= 2
        assert alternative["distance_km"] > 0
        assert alternative["access_distance_km"] == expected_access_km
        assert alternative["access_time_mins"] == result["access_time_mins"]
        assert alternative["total_eta_mins"] == round(
            alternative["estimated_travel_time_mins"]
            + result["customs_buffer_mins"]
            + alternative["access_time_mins"],
            1,
        )
        assert alternative["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"
        if len(alternative["navigation"]["maneuvers"]) > len(result["navigation"]["maneuvers"]):
            assert alternative["maneuver_delay_mins"] > 0
        alternative_arrival = departure + timedelta(
            minutes=alternative["total_eta_mins"],
        )
        for sailing in alternative["next_matching_ferries"]:
            sail_at = datetime.fromisoformat(sailing["departure_time"])
            assert sail_at >= alternative_arrival + timedelta(
                minutes=ferry_schedule.BOARDING_CUTOFF_MINS,
            )
    if result["optimal_departure"]["recommended"] == "DEPART_NOW":
        assert result["co2_saved_kg"] == 0


def test_corridor_without_port_is_labelled():
    result = optimize_route("corridor-3", "CARGO_TRUCK", hour=10, now=WEEKDAY_1400)
    assert "ferry_connection_note" in result, (
        "a corridor with no terminal silently offered a ferry connection")
    assert result["customs_buffer_mins"] == 0
    assert result["total_eta_mins"] == round(
        result["estimated_travel_time_mins"] + result["access_time_mins"], 1,
    )


def test_weather_and_hour_change_the_answer():
    clear = optimize_route("corridor-1", "CARGO_TRUCK", hour=14, weather=0, now=WEEKDAY_1400)
    storm = optimize_route("corridor-1", "CARGO_TRUCK", hour=14, weather=2, now=WEEKDAY_1400)
    assert storm["estimated_travel_time_mins"] > clear["estimated_travel_time_mins"], (
        "weather has no effect on the route")

    quiet = optimize_route("corridor-1", "CARGO_TRUCK", hour=3, weather=0, now=WEEKDAY_1400)
    assert quiet["estimated_travel_time_mins"] < clear["estimated_travel_time_mins"], (
        "departure hour has no effect on the route")
    assert quiet["generalized_cost_mins"] < clear["generalized_cost_mins"]
    assert (
        quiet["routing_model"]["network_congestion_score"]
        != clear["routing_model"]["network_congestion_score"]
    ), "requested hour never reached the path-weighting model"
    assert storm["routing_cost_breakdown"]["weather_delay_mins"] > 0
    assert (
        clear["congestion_prediction"]["estimated_delay_mins"]
        == round(clear["routing_cost_breakdown"]["congestion_delay_mins"], 1)
    ), "top-level delay contradicted the selected-edge delay"


def test_deferred_recommendation_uses_the_recommended_window():
    result = optimize_route(
        "corridor-1", "COMMUTER", hour=19, weather=0, now=WEEKDAY_1400,
    )
    departure = datetime.fromisoformat(result["planned_departure"])
    assert result["optimal_departure"]["recommended"] == "DEFER_30_MINS"
    assert (departure.hour, departure.minute) == (19, 30)
    assert result["optimal_departure"]["time_saved_mins"] > 0
    assert result["co2_saved_kg"] > 0

    arrival = departure + timedelta(minutes=result["total_eta_mins"])
    for sailing in result["next_matching_ferries"]:
        sail_at = datetime.fromisoformat(sailing["departure_time"])
        assert sail_at >= arrival + timedelta(minutes=ferry_schedule.BOARDING_CUTOFF_MINS)


def test_point_to_point_route_and_terminal_matching():
    result = optimize_route(
        None, "COMMUTER", hour=14, now=WEEKDAY_1400,
        origin_id="batu_aji", destination_id="harbour_bay",
    )
    assert result["corridor"]["origin"] == "batu_aji"
    assert result["corridor"]["destination"] == "harbour_bay"
    assert result["corridor"]["distance_km"] > 10
    assert len(result["route_geometry"]) >= 2
    assert result["next_matching_ferries"]
    assert all(
        ferry["departure_port"] == "HarbourBay"
        for ferry in result["next_matching_ferries"]
    )
    maneuvers = result["navigation"]["maneuvers"]
    assert maneuvers[0]["landmark"] == "Batu Aji Transit Hub"
    assert maneuvers[-1]["instruction"] == "Arrive at Harbour Bay Ferry Terminal"


def test_point_to_point_validation():
    try:
        optimize_route(
            None, "COMMUTER", origin_id="nagoya", destination_id="nagoya",
            now=WEEKDAY_1400,
        )
    except ValueError as err:
        assert "different" in str(err).lower()
    else:
        raise AssertionError("same-location route was accepted")

    try:
        optimize_route("corridor-1", "PASSENGER_FERRY", now=WEEKDAY_1400)
    except ValueError as err:
        assert "vehicle" in str(err).lower()
    else:
        raise AssertionError("a ferry was accepted as a Batam road vehicle")


def test_free_route_preserves_requested_points_and_geometry_order():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    try:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = lambda *_args: None
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Nagoya Hill",
            destination_name="Batam Centre Ferry Terminal",
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase

    assert result["requested_origin"]["name"] == "Nagoya Hill"
    assert result["requested_destination"]["name"] == "Batam Centre Ferry Terminal"
    assert result["route_data_source"] == "openstreetmap"
    assert result["planning_traffic_snapshot"]["effective_at"] == (
        result["planned_departure"]
    )
    assert result["planning_traffic_snapshot"]["applied_to_returned_route"] is True
    assert len(result["planning_traffic_snapshot"]["zones"]) == 15
    assert 5.0 < result["corridor"]["distance_km"] < 9.0
    assert len(result["route_geometry"]) > 10
    first = result["route_geometry"][0]
    last = result["route_geometry"][-1]
    assert router.haversine_m(tuple(first), (1.1465, 104.0125)) < 5_000
    assert router.haversine_m(tuple(last), (1.1318, 104.0554)) < 5_000
    assert result["snap_info"]["origin_snap_m"] == round(
        router.haversine_m(tuple(first), (1.1465, 104.0125)), 1,
    )
    assert result["snap_info"]["destination_snap_m"] == round(
        router.haversine_m(tuple(last), (1.1318, 104.0554)), 1,
    )
    assert result["access_distance_km"] > 0
    assert result["access_time_mins"] > 0
    assert result["total_eta_mins"] == round(
        result["estimated_travel_time_mins"]
        + result["customs_buffer_mins"]
        + result["access_time_mins"],
        1,
    )
    assert all(
        alternative["total_eta_mins"]
        >= round(
            alternative["estimated_travel_time_mins"]
            + alternative["access_time_mins"],
            1,
        )
        for alternative in result["alternative_routes"]
    )


def test_free_route_composes_singapore_to_batam_without_snapping_islands():
    original_osrm = multimodal_router._osrm_route
    try:
        multimodal_router._osrm_route = lambda *_args: None
        result = optimize_free_route(
            1.29027, 103.851959,
            1.1318, 104.0554,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Singapore CBD",
            destination_name="Batam Centre Ferry Terminal",
        )
    finally:
        multimodal_router._osrm_route = original_osrm

    assert result["route_type"] == "MULTIMODAL_FERRY_ROUTE"
    assert result["requested_origin"]["region"] == "SINGAPORE"
    assert result["requested_destination"]["region"] == "BATAM"
    assert [leg["mode"] for leg in result["route_legs"]] == [
        "ROAD", "FERRY", "ROAD",
    ]
    assert result["route_legs"][0]["to_name"].endswith("SG")
    assert result["route_legs"][1]["geometry_note"].startswith("Channel-aware")
    assert result["route_legs"][1]["schedule_status"] == (
        "PUBLISHED_DEPARTURE_SELECTED"
    )
    assert len(result["next_matching_ferries"]) == 1
    selected_sailing = result["next_matching_ferries"][0]
    assert selected_sailing["departure_port"] == "HarbourFront SG"
    assert selected_sailing["arrival_port"] == "Batam Centre"
    assert selected_sailing["departure_timezone"] == "Asia/Singapore"
    assert selected_sailing["departure_time"].endswith("+08:00")
    assert selected_sailing["schedule_source_url"] == (
        "https://app.sindoferry.com.sg/schedule/"
    )
    assert "ferry_connection_note" not in result
    ferry_geometry = result["route_legs"][1]["geometry"]
    assert router.haversine_m(tuple(ferry_geometry[0]), (1.2644, 103.8206)) < 10
    assert router.haversine_m(tuple(ferry_geometry[-1]), (1.1318, 104.0554)) < 10
    assert result["planned_departure"].endswith("+08:00")
    assert result["vehicle_transfer_policy"] == "FIRST_LAST_MILE_ONLY"
    assert "not carried onboard" in result["vehicle_transfer_note"]
    assert result["route_legs"][0]["vehicle_role"] == "FIRST_LAST_MILE_ACCESS"
    assert result["route_legs"][1]["vehicle_carried_onboard"] is False
    assert result["route_legs"][2]["vehicle_role"] == "FIRST_LAST_MILE_ACCESS"
    assert len(result["route_geometry"]) > 6
    assert len(result["navigation"]["maneuvers"]) >= 6
    assert result["total_eta_mins"] > result["estimated_travel_time_mins"]
    assert result["road_distance_km"] > 0
    assert result["ferry_distance_km"] > 0
    assert "road legs only" in result["emissions_scope"].lower()


def test_cross_border_route_api_uses_shared_latest_ferry_verification():
    env_name = "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS"
    original_env = os.environ.get(env_name)
    original_load = ferry_freshness_store.load
    original_osrm = multimodal_router._osrm_route
    latest = "2026-08-14T09:00:08+07:00"
    try:
        os.environ[env_name] = "1"
        snapshot_id = ferry_schedule.timetable_metadata(
            load_durable=False,
        )["snapshot_id"]
        ferry_freshness_store.load = lambda candidate: {
            "snapshot_id": snapshot_id,
            "latest_checked_at": latest,
            "last_verified_at": latest,
        }
        multimodal_router._osrm_route = lambda *_args: None
        ferry_schedule._reset_runtime_verification_for_tests()
        with clock.frozen(WEEKDAY_1400):
            response = api_main.api_optimize_free_route(
                api_main.FreeRouteRequest(
                    origin_lat=1.29027,
                    origin_lng=103.851959,
                    destination_lat=1.1318,
                    destination_lng=104.0554,
                    origin_name="Singapore CBD",
                    destination_name="Batam Centre Ferry Terminal",
                ),
            )
        assert response["next_matching_ferries"]
        assert all(
            sailing["schedule_last_verified_at"] == latest
            for sailing in response["next_matching_ferries"]
        )
        assert all(
            sailing["schedule_snapshot_verified_at"]
                == "2026-08-13T00:30:50+07:00"
            for sailing in response["next_matching_ferries"]
        )
    finally:
        ferry_freshness_store.load = original_load
        multimodal_router._osrm_route = original_osrm
        ferry_schedule._reset_runtime_verification_for_tests()
        if original_env is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = original_env


def test_cross_border_trucks_require_a_cargo_operator_feed():
    for vehicle_type in ("LIGHT_TRUCK", "CARGO_TRUCK"):
        try:
            optimize_free_route(
                1.29027, 103.851959,
                1.1318, 104.0554,
                vehicle_type,
                hour=14,
                now=WEEKDAY_1400,
                origin_name="Singapore CBD",
                destination_name="Batam Centre Ferry Terminal",
            )
            raise AssertionError(f"{vehicle_type} was placed on a passenger ferry")
        except ValueError as error:
            message = str(error)
            assert "cannot be scheduled" in message
            assert "cargo port" in message
            assert "Local road routing remains available" in message


def test_singapore_origin_chooses_a_pair_with_published_outbound_service():
    original_osrm = multimodal_router._osrm_route
    try:
        multimodal_router._osrm_route = lambda *_args: None
        result = optimize_free_route(
            1.2840, 103.8513,
            1.15396, 103.997234,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Raffles Place",
            destination_name="HarbourBay district",
        )
    finally:
        multimodal_router._osrm_route = original_osrm

    ferry_leg = result["route_legs"][1]
    assert ferry_leg["from_name"] == "HarbourFront SG"
    assert ferry_leg["to_name"] == "HarbourBay"
    assert ferry_leg["schedule_status"] == "PUBLISHED_DEPARTURE_SELECTED"
    assert len(result["next_matching_ferries"]) == 1
    selected = result["next_matching_ferries"][0]
    assert selected["departure_port"] == ferry_leg["from_name"]
    assert selected["arrival_port"] == ferry_leg["to_name"]


def test_local_batam_truck_route_remains_available():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    try:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = (
            lambda *_args: None
        )
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "CARGO_TRUCK",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Nagoya Hill",
            destination_name="Batam Centre",
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase
    assert result["route_type"] == "ROAD_ROUTE"
    assert result["vehicle_type"] == "CARGO_TRUCK"
    assert result["route_geometry"]


def test_free_route_keeps_local_singapore_journey_road_only_when_osrm_is_down():
    original_osrm = multimodal_router._osrm_route
    try:
        multimodal_router._osrm_route = lambda *_args: None
        result = optimize_free_route(
            1.29027, 103.851959,
            1.3521, 103.8198,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Singapore CBD",
            destination_name="Singapore Botanic Gardens",
        )
    finally:
        multimodal_router._osrm_route = original_osrm

    assert result["route_type"] == "ROAD_ROUTE"
    assert [leg["mode"] for leg in result["route_legs"]] == ["ROAD"]
    assert result["route_data_source"] == "offline_access_estimate"
    assert result["route_legs"][0]["is_estimate"] is True
    assert result["ferry_distance_km"] == 0
    assert result["next_matching_ferries"] == []


def test_free_route_still_rejects_points_outside_singapore_batam_corridor():
    try:
        optimize_free_route(
            3.1390, 101.6869,
            1.1318, 104.0554,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
            origin_name="Kuala Lumpur",
            destination_name="Batam Centre Ferry Terminal",
        )
    except ValueError as err:
        assert "singapore or batam" in str(err).lower()
    else:
        raise AssertionError("an out-of-corridor point was accepted")


def test_local_route_is_default_and_does_not_wait_for_external_providers():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    original_preference = os.environ.pop("CROSSFLOW_ROUTE_PROVIDER", None)
    calls = []
    try:
        def unexpected_provider(*_args):
            calls.append(True)
            raise AssertionError("default local route called an external provider")

        route_solver.supabase_pgrouting.query_supabase_pgrouting = unexpected_provider
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase
        if original_preference is not None:
            os.environ["CROSSFLOW_ROUTE_PROVIDER"] = original_preference

    assert not calls
    assert result["route_data_source"] == "openstreetmap"
    assert result["navigation"] and result["alternative_routes"]


def test_external_route_provider_metrics_use_complete_supabase_contract():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    original_preference = os.environ.get("CROSSFLOW_ROUTE_PROVIDER")
    provider_geometry = [
        [1.1465, 104.0125],
        [1.1400, 104.0300],
        [1.1318, 104.0554],
    ]
    provider_navigation = {
        "schema_version": 1,
        "data_source": "supabase_pgrouting",
        "maneuvers": [
            {
                "type": "DEPART", "instruction": "Head east",
                "coords": provider_geometry[0],
            },
            {
                "type": "ARRIVE", "instruction": "Arrive at destination",
                "coords": provider_geometry[-1],
            },
        ],
    }
    try:
        os.environ["CROSSFLOW_ROUTE_PROVIDER"] = "supabase_v2_constrained"
        route_solver.supabase_pgrouting.query_supabase_pgrouting = lambda *_args, **_kwargs: {
            "geometry": provider_geometry,
            "distance_km": 5.4,
            "duration_mins": 12.0,
            "navigation": provider_navigation,
            "constraint_provenance": {
                "vehicle_type": "COMMUTER",
                "access_constraints_honored": True,
                "clearance_constraints_honored": True,
                "source": "test_constrained_rpc",
            },
            "shortcuts_used": [{"id": "provider-must-not-claim-local-learning"}],
            "local_road_distance_km": 100.0,
            "local_road_segments": [{"name": "Injected primary local road"}],
            "local_road_audit": {"requested": True},
            "routing_model": {"learning": {"revision": 999}},
            "alternatives": [{
                "id": "supabase-alternative-1",
                "name": "Supabase alternative 1",
                "description": "Native provider alternative.",
                "geometry": provider_geometry,
                "distance_km": 5.8,
                "duration_mins": 13.0,
                "navigation": provider_navigation,
                "data_source": "supabase_pgrouting",
                "shortcuts_used": [{"id": "provider-alternative-learning"}],
                "local_road_distance_km": 105.0,
                "local_road_segments": [{"name": "Injected local road"}],
                "local_road_audit": {"requested": True},
                "route_preference": "LOCAL",
                "route_preference_profile": {"road_scope": "MAPPED_PUBLIC_LOCAL"},
                "routing_model": {
                    "version": 5,
                    "road_scope": "MAPPED_PUBLIC_LOCAL",
                    "learning": {"revision": 999},
                },
            }],
        }
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            hour=14,
            now=WEEKDAY_1400,
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase
        if original_preference is None:
            os.environ.pop("CROSSFLOW_ROUTE_PROVIDER", None)
        else:
            os.environ["CROSSFLOW_ROUTE_PROVIDER"] = original_preference

    assert result["route_data_source"] == "supabase_pgrouting"
    assert result["planning_traffic_snapshot"]["applied_to_returned_route"] is False
    assert result["route_geometry"] == provider_geometry
    assert result["corridor"]["distance_km"] == 5.4
    assert result["estimated_travel_time_mins"] >= 12.0
    assert result["co2_emissions_kg"] > 0.0
    assert result["avoided_congested_zones"] == []
    assert len(result["alternative_routes"]) == 1
    assert result["alternative_routes"][0]["navigation"] is provider_navigation
    assert "shortcuts_used" not in result
    assert "shortcuts_used" not in result["alternative_routes"][0]
    assert "local_road_audit" not in result
    assert "local_road_audit" not in result["alternative_routes"][0]
    assert "learning" not in result["routing_model"]
    assert result["alternative_routes"][0]["routing_model"]["version"] == "external"
    assert result["alternative_routes"][0]["route_preference"] == "BALANCED"
    assert "learning" not in result["alternative_routes"][0]["routing_model"]
    assert result["alternatives_note"] is not None


def test_supabase_without_native_steps_cannot_replace_local_contract():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    original_preference = os.environ.get("CROSSFLOW_ROUTE_PROVIDER")
    try:
        os.environ["CROSSFLOW_ROUTE_PROVIDER"] = "supabase_v2_constrained"
        route_solver.supabase_pgrouting.query_supabase_pgrouting = lambda *_args, **_kwargs: {
            "geometry": [[1.1465, 104.0125], [1.1318, 104.0554]],
            "distance_km": 100.0,
            "duration_mins": 120.0,
        }
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            now=WEEKDAY_1400,
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase
        if original_preference is None:
            os.environ.pop("CROSSFLOW_ROUTE_PROVIDER", None)
        else:
            os.environ["CROSSFLOW_ROUTE_PROVIDER"] = original_preference

    assert result["route_data_source"] == "openstreetmap"
    assert result["corridor"]["distance_km"] < 10


def test_legacy_supabase_provider_opt_in_fails_fast_without_network():
    original_supabase = route_solver.supabase_pgrouting.query_supabase_pgrouting
    original_preference = os.environ.get("CROSSFLOW_ROUTE_PROVIDER")
    calls = []
    try:
        os.environ["CROSSFLOW_ROUTE_PROVIDER"] = "supabase"
        route_solver.supabase_pgrouting.query_supabase_pgrouting = (
            lambda *_args, **_kwargs: calls.append(True)
        )
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            now=WEEKDAY_1400,
        )
    finally:
        route_solver.supabase_pgrouting.query_supabase_pgrouting = original_supabase
        if original_preference is None:
            os.environ.pop("CROSSFLOW_ROUTE_PROVIDER", None)
        else:
            os.environ["CROSSFLOW_ROUTE_PROVIDER"] = original_preference

    assert not calls
    assert result["route_data_source"] == "openstreetmap"


def test_google_route_replacement_is_disabled_for_map_license():
    original_google = google_maps_router.get_google_route
    original_preference = os.environ.get("CROSSFLOW_ROUTE_PROVIDER")
    called = []
    try:
        os.environ["CROSSFLOW_ROUTE_PROVIDER"] = "google"

        def forbidden_google_call(*_args):
            called.append(True)
            raise AssertionError("runtime requested Google geometry for an OSM/CARTO map")

        google_maps_router.get_google_route = forbidden_google_call
        result = optimize_free_route(
            1.1465, 104.0125,
            1.1318, 104.0554,
            "COMMUTER",
            now=WEEKDAY_1400,
        )
    finally:
        google_maps_router.get_google_route = original_google
        if original_preference is None:
            os.environ.pop("CROSSFLOW_ROUTE_PROVIDER", None)
        else:
            os.environ["CROSSFLOW_ROUTE_PROVIDER"] = original_preference

    assert not called
    assert result["route_data_source"] == "openstreetmap"
    assert len(result["route_geometry"]) >= 2
    assert result["navigation"]["maneuvers"][-1]["type"] == "ARRIVE"


def test_server_google_key_never_reads_vite_public_prefix():
    key_names = (
        "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_KEY", "GOOGLE_API_KEY",
        "VITE_GOOGLE_MAPS_API_KEY",
    )
    originals = {key: os.environ.get(key) for key in key_names}
    try:
        for key in key_names:
            os.environ.pop(key, None)
        os.environ["VITE_GOOGLE_MAPS_API_KEY"] = "browser-public-value"
        assert google_maps_router.get_api_key() == ""
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

def test_all_timestamps_are_tz_aware():
    with clock.frozen(WEEKDAY_1400):
        summary = get_operations_summary()
        for alert in summary["alerts"]:
            assert datetime.fromisoformat(alert["timestamp"]).tzinfo is not None
        for sailing in ferry_schedule.generate_sailings(horizon_hours=6):
            for field in ("departure_time", "arrival_time"):
                assert datetime.fromisoformat(sailing[field]).tzinfo is not None


def test_clock_freeze_restores():
    original = clock.now()
    with clock.frozen(WEEKDAY_1400):
        assert clock.now() == WEEKDAY_1400
    assert clock.now() != WEEKDAY_1400 and clock.now() >= original


def test_clock_always_serializes_batam_time():
    assert clock.now().utcoffset() == timedelta(hours=7)
    singapore = datetime(
        2026, 8, 9, 22, 15,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert clock.iso(singapore) == "2026-08-09T21:15:00+07:00"


def test_history_seed_migration_replaces_only_obsolete_synthetic_rows():
    fixed_now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
    with tempfile.TemporaryDirectory(prefix="crossflow-history-migration-") as directory:
        path = os.path.join(directory, "history.db")
        preserved_ts = int((fixed_now - timedelta(hours=1)).timestamp())
        with sqlite3.connect(path) as conn:
            conn.execute("""
                CREATE TABLE observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    corridor TEXT NOT NULL,
                    score REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'simulated'
                )
            """)
            conn.executemany(
                "INSERT INTO observations (ts, corridor, score, source) "
                "VALUES (?, ?, ?, ?)",
                [
                    (preserved_ts, "corridor-1", 99.9, "synthetic"),
                    (preserved_ts, "corridor-1", 33.3, "simulated"),
                ],
            )

        store = historical_store.HistoryStore(
            path,
            now_provider=lambda: fixed_now,
        )
        try:
            source_counts = dict(store._conn.execute(
                "SELECT source, COUNT(*) FROM observations GROUP BY source"
            ).fetchall())
            assert source_counts == {"simulated": 1, "synthetic": 1_680}
            assert store._conn.execute(
                "SELECT COUNT(*) FROM observations "
                "WHERE source = 'synthetic' AND score = 99.9"
            ).fetchone()[0] == 0
            assert store._conn.execute(
                "SELECT ts, corridor, score, source FROM observations "
                "WHERE source = 'simulated'"
            ).fetchone() == (
                preserved_ts,
                "corridor-1",
                33.3,
                "simulated",
            )
            corridor_counts = dict(store._conn.execute(
                "SELECT corridor, COUNT(*) FROM observations "
                "WHERE source = 'synthetic' GROUP BY corridor"
            ).fetchall())
            assert corridor_counts == {
                corridor_id: 336 for corridor_id in historical_store.CORRIDOR_IDS
            }

            profile = store.get_hourly_profile("corridor-1", days=30, now=fixed_now)
            by_hour = {bucket["hour"]: bucket["avg_score"] for bucket in profile}
            assert by_hour[8] > by_hour[1]
            assert by_hour[18] > by_hour[15]
        finally:
            store.close()


def test_history_current_seed_fills_missing_corridor_without_replacing_others():
    fixed_now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
    with tempfile.TemporaryDirectory(prefix="crossflow-history-repair-") as directory:
        path = os.path.join(directory, "history.db")
        original_store = historical_store.HistoryStore(
            path,
            now_provider=lambda: fixed_now,
        )
        corridor_one_rows = original_store._conn.execute(
            "SELECT ts, score FROM observations "
            "WHERE source = 'synthetic' AND corridor = 'corridor-1' ORDER BY ts"
        ).fetchall()
        original_store.close()

        with sqlite3.connect(path) as conn:
            conn.execute(
                "DELETE FROM observations "
                "WHERE source = 'synthetic' AND corridor = 'corridor-5'"
            )

        repaired_store = historical_store.HistoryStore(
            path,
            now_provider=lambda: fixed_now,
        )
        try:
            assert repaired_store._conn.execute(
                "SELECT ts, score FROM observations "
                "WHERE source = 'synthetic' AND corridor = 'corridor-1' ORDER BY ts"
            ).fetchall() == corridor_one_rows
            corridor_counts = dict(repaired_store._conn.execute(
                "SELECT corridor, COUNT(*) FROM observations "
                "WHERE source = 'synthetic' GROUP BY corridor"
            ).fetchall())
            assert corridor_counts == {
                corridor_id: 336 for corridor_id in historical_store.CORRIDOR_IDS
            }
        finally:
            repaired_store.close()


def test_history_seed_rolls_forward_on_next_batam_date_and_preserves_samples():
    first_now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
    next_day = first_now + timedelta(days=1)
    with tempfile.TemporaryDirectory(prefix="crossflow-history-rollover-") as directory:
        path = os.path.join(directory, "history.db")
        first_store = historical_store.HistoryStore(
            path,
            now_provider=lambda: first_now,
        )
        first_store.record("corridor-2", 47.5, "simulated", now=first_now)
        first_bounds = first_store._conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM observations WHERE source = 'synthetic'"
        ).fetchone()
        assert first_store._metadata(
            "synthetic_seed_generated_for_date"
        ) == first_now.date().isoformat()
        first_store.close()

        rolled_store = historical_store.HistoryStore(
            path,
            now_provider=lambda: next_day,
        )
        try:
            rolled_bounds = rolled_store._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM observations "
                "WHERE source = 'synthetic'"
            ).fetchone()
            assert rolled_bounds == tuple(epoch + 86400 for epoch in first_bounds)
            assert rolled_store._conn.execute(
                "SELECT COUNT(*) FROM observations WHERE source = 'synthetic'"
            ).fetchone()[0] == 1_680
            assert rolled_store._conn.execute(
                "SELECT ts, corridor, score, source FROM observations "
                "WHERE source = 'simulated'"
            ).fetchall() == [(
                int(first_now.timestamp()),
                "corridor-2",
                47.5,
                "simulated",
            )]
            assert rolled_store._metadata(
                "synthetic_seed_generated_for_date"
            ) == next_day.date().isoformat()
            metadata = rolled_store.get_history_metadata(
                "corridor-2",
                days=30,
                now=next_day,
            )
            assert metadata["synthetic_seed"]["generated_for_date"] == (
                next_day.date().isoformat()
            )
        finally:
            rolled_store.close()


def test_historical_api_reports_mixed_provenance_without_overclaiming():
    fixed_now = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)
    with tempfile.TemporaryDirectory(prefix="crossflow-history-api-") as directory:
        store = historical_store.HistoryStore(
            os.path.join(directory, "history.db"),
            now_provider=lambda: fixed_now,
        )
        store.record("corridor-1", 42.5, "simulated", now=fixed_now)
        non_observed = store.get_history_metadata(
            "corridor-1",
            days=7,
            now=fixed_now,
        )
        assert non_observed["observed"] is False
        assert non_observed["contains_observed_samples"] is False

        store.record("corridor-1", 38.5, "tomtom_live", now=fixed_now)
        suite_store = historical_store._store
        historical_store._store = store
        try:
            with clock.frozen(fixed_now):
                response = api_main.api_historical_congestion("corridor-1", 7)

            metadata = response["history_metadata"]
            assert len(response["hourly_profile"]) == 24
            assert metadata["observed"] is False
            assert metadata["contains_observed_samples"] is True
            assert metadata["source_counts"]["simulated"] == 1
            assert metadata["source_counts"]["tomtom_live"] == 1
            assert metadata["source_counts"]["synthetic"] > 0
            assert metadata["sources"]["synthetic"]["observed"] is False
            assert metadata["sources"]["simulated"]["observed"] is False
            assert metadata["sources"]["tomtom_live"]["observed"] is True
            assert metadata["latest_sample_at"] == clock.iso(fixed_now)
            assert metadata["latest_sample_age_seconds"] == 0
            assert metadata["freshness"] == "current"
            assert metadata["synthetic_seed"] == {
                "source": "synthetic",
                "version": historical_store.SYNTHETIC_SEED_VERSION,
                "days": historical_store.SYNTHETIC_SEED_DAYS,
                "timezone": "WIB (UTC+07:00)",
                "generated_for_date": fixed_now.date().isoformat(),
                "observed": False,
            }
            assert metadata["storage"]["engine"] == "sqlite"
            assert metadata["storage"]["durable"] is False
            assert metadata["storage"]["durability"] == "ephemeral_instance_file"
            assert metadata["storage"]["shared_across_instances"] is False
        finally:
            historical_store._store = suite_store
            store.close()


def test_historical_api_rejects_unknown_corridor_id():
    try:
        api_main.api_historical_congestion("corridor-does-not-exist", 7)
    except HTTPException as err:
        assert err.status_code == 404
        assert "Unknown corridor_id" in err.detail
    else:
        raise AssertionError("unknown historical corridor was accepted")


def test_api_request_models_reject_invalid_route_inputs():
    try:
        api_main.FreeRouteRequest(
            origin_lat=999,
            origin_lng=104.0,
            destination_lat=1.13,
            destination_lng=104.05,
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("an impossible latitude passed API validation")

    try:
        api_main.RouteRequest(vehicle_type="PASSENGER_FERRY")
    except ValidationError:
        pass
    else:
        raise AssertionError("a ferry was accepted as a Batam road vehicle")


def test_api_provenance_is_result_specific():
    original_key = os.environ.get("TOMTOM_API_KEY")
    try:
        os.environ["TOMTOM_API_KEY"] = "configured-but-not-used-by-this-response"
        assert api_main.envelope({}, WEEKDAY_1400)["data_source"] == "simulated"
        assert api_main.envelope(
            {}, WEEKDAY_1400, data_source="live",
        )["data_source"] == "live"
        local_routing = api_main.envelope(
            {"route_data_source": "openstreetmap"}, WEEKDAY_1400,
        )["provenance"]["routing"]
        google_routing = api_main.envelope(
            {"route_data_source": "google_maps_directions_api"}, WEEKDAY_1400,
        )["provenance"]["routing"]
        supabase_routing = api_main.envelope(
            {"route_data_source": "supabase_pgrouting"}, WEEKDAY_1400,
        )["provenance"]["routing"]
        multimodal = api_main.envelope(
            {"route_data_source": "multimodal_offline_estimate"},
            WEEKDAY_1400,
        )["provenance"]
        assert "OpenStreetMap" in local_routing
        assert "Google Maps Directions API" in google_routing
        assert "Supabase pgRouting" in supabase_routing
        assert "OpenStreetMap" not in google_routing
        assert "OpenStreetMap" not in supabase_routing
        assert "official operator ferry" in multimodal["routing"]
        assert "OSRM" in multimodal["road_network"]
        assert "No cross-border live traffic" in multimodal["traffic"]
    finally:
        if original_key is None:
            os.environ.pop("TOMTOM_API_KEY", None)
        else:
            os.environ["TOMTOM_API_KEY"] = original_key


def test_tomtom_flow_request_uses_supported_unit_and_runtime_key():
    """The provider contract is case-sensitive and keys may rotate at runtime."""
    import urllib.parse
    from unittest.mock import patch

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "flowSegmentData": {
                    "currentSpeed": 32,
                    "freeFlowSpeed": 64,
                    "confidence": 0.91,
                },
            }).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout, **kwargs):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["ssl_context"] = kwargs.get("context")
        return FakeResponse()

    original_key = os.environ.get("TOMTOM_API_KEY")
    live_traffic._tomtom_cache.clear()
    try:
        # live_traffic was imported before this assignment. A module-level key
        # snapshot would therefore miss it.
        os.environ["TOMTOM_API_KEY"] = "rotated-test-key"
        with patch.object(live_traffic.urllib.request, "urlopen", fake_urlopen):
            segment = live_traffic._tomtom_segment(1.1, 104.0)

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(captured["url"]).query,
        )
        assert query["unit"] == ["kmph"]
        assert query["key"] == ["rotated-test-key"]
        assert captured["timeout"] < 3
        assert segment == {
            "current_speed_kmh": 32.0,
            "free_flow_speed_kmh": 64.0,
            "confidence": 0.91,
        }
    finally:
        live_traffic._tomtom_cache.clear()
        if original_key is None:
            os.environ.pop("TOMTOM_API_KEY", None)
        else:
            os.environ["TOMTOM_API_KEY"] = original_key


def test_corridor_map_hotspot_catalog_is_complete_unique_and_graph_backed():
    """All 30 map candidates must represent a mapped part of Batam."""
    configs = live_traffic.CORRIDOR_MAP_HOTSPOTS_CONFIG
    assert live_traffic.CORRIDOR_HOTSPOT_CATALOG["schema_version"] == 1
    assert len(configs) == 30
    assert len({config["zone_id"] for config in configs}) == len(configs)
    assert sum(config["routing_enabled"] for config in configs) == 15
    assert "zone-tembesi" not in {config["zone_id"] for config in configs}

    by_id = {config["zone_id"]: config for config in configs}
    assert (by_id["zone-batu-aji"]["lat"], by_id["zone-batu-aji"]["lng"]) == (
        1.050919,
        103.964956,
    )
    assert (
        by_id["zone-marina-city"]["lat"],
        by_id["zone-marina-city"]["lng"],
    ) == (1.082167, 103.931768)

    selection_weights = live_traffic.HOTSPOT_SELECTION_WEIGHTS
    assert selection_weights == {
        "corridor_pressure": 0.45,
        "recurrence": 0.20,
        "network_criticality": 0.15,
        "demand_exposure": 0.10,
        "evidence_confidence": 0.10,
    }
    assert abs(sum(selection_weights.values()) - 1.0) < 1e-9

    for config in configs:
        weights = config["signal_mix"]
        assert weights and abs(sum(weights.values()) - 1.0) < 1e-9
        assert set(weights).issubset({f"corridor-{index}" for index in range(1, 6)})
        assert all(math.isfinite(weight) and weight >= 0 for weight in weights.values())
        assert 500 <= config["radius_m"] <= 1_000
        assert 0 <= config["base_score"] <= 100
        assert 0 <= config["network_criticality"] <= 100
        assert 0 <= config["demand_exposure"] <= 100
        radial = router._radial_node_influence(
            round(config["lat"], 6),
            round(config["lng"], 6),
            round(float(config["radius_m"]), 1),
        )
        assert radial, f"{config['zone_id']} has no graph-node coverage"
        nearest_covered_m = min(
            router.haversine_m(
                (config["lat"], config["lng"]),
                router.NODES[node_id],
            )
            for node_id in radial
        )
        assert nearest_covered_m <= 250, (
            f"{config['zone_id']} is {nearest_covered_m:.0f} m from a mapped road"
        )


def test_route_planning_hotspot_scope_remains_the_established_fifteen():
    """Map coverage expansion must not silently expand route cost zones."""
    configs = live_traffic.CONGESTION_ZONES_CONFIG
    assert len(configs) == 15
    assert all(config["routing_enabled"] for config in configs)
    expected_ids = {
        config["zone_id"]
        for config in live_traffic.CORRIDOR_MAP_HOTSPOTS_CONFIG
        if config["routing_enabled"]
    }
    assert {config["zone_id"] for config in configs} == expected_ids
    assert len(live_traffic.get_congestion_zones(WEEKDAY_1400)) == 15


def test_zone_scores_blend_local_baselines_instead_of_cloning_corridors():
    """Nearby corridor signals are mixed, while local baselines stay distinct."""
    from unittest.mock import patch

    telemetry = [
        {"id": f"corridor-{index}", "live_congestion_score": 50.0}
        for index in range(1, 6)
    ]
    with patch(
        "services.simulator.get_live_corridor_telemetry",
        return_value=telemetry,
    ):
        zones = live_traffic.get_congestion_zones(WEEKDAY_1400)

    by_id = {zone["zone_id"]: zone for zone in zones}
    panbil = by_id["zone-panbil"]
    batamindo = by_id["zone-batamindo"]
    assert panbil["congestion_index"] != batamindo["congestion_index"]
    expected = round(
        live_traffic.HOTSPOT_CORRIDOR_SIGNAL_WEIGHT * 50.0
        + live_traffic.HOTSPOT_BASELINE_WEIGHT * 68.0,
        1,
    )
    assert panbil["congestion_index"] == expected
    for zone in zones:
        if zone["congestion_index"] >= 70:
            assert zone["level"] == "SUPER_CONGESTED"
            assert zone["avoid_recommended"] is True
        elif zone["congestion_index"] >= 40:
            assert zone["level"] == "HEAVY"
            assert zone["avoid_recommended"] is False
        else:
            assert zone["level"] == "SMOOTH"
            assert zone["avoid_recommended"] is False
        pressure = zone["modeled_emissions_pressure"]
        expected_factor = (zone["congestion_index"] / 100.0) ** 2
        assert pressure["index"] == round(100.0 * expected_factor, 1)
        assert pressure["queue_pressure_factor"] == round(expected_factor, 4)
        assert pressure["level"] in {"LOW", "ELEVATED", "HIGH"}
        assert pressure["metric"] == "relative_queue_emissions_pressure"
        assert pressure["unit"] == "index_0_100"
        assert pressure["observed"] is False


def test_live_traffic_response_includes_complete_hotspot_coverage():
    """The current layer and offline-capable UI always receive all model areas."""
    from unittest.mock import patch

    with patch.object(live_traffic, "_tomtom_api_key", return_value=""):
        result = live_traffic.get_live_traffic()
    assert len(result["zones"]) == 30
    assert result["coverage"]["hotspot_count"] == 30
    assert sum(result["coverage"]["level_counts"].values()) == 30
    assert sum(
        result["coverage"]["emissions_pressure_level_counts"].values()
    ) == 30
    assert result["coverage"]["methodology"] == (
        live_traffic.CORRIDOR_MAP_HOTSPOT_METHODOLOGY
    )
    assert result["coverage"]["catalog_version"] == (
        live_traffic.CORRIDOR_HOTSPOT_CATALOG["catalog_version"]
    )
    pressure_model = result["coverage"]["emissions_pressure_model"]
    assert pressure_model == live_traffic.EMISSIONS_PRESSURE_MODEL
    assert pressure_model["observed"] is False
    assert pressure_model["aggregate_mass_available"] is False
    assert all(
        zone["source"] == "modelled_spatial_hotspot"
        and zone["observed"] is False
        and zone["category"]
        and zone["corridor_ids"]
        and zone["watch_priority"] in {"CRITICAL", "HEAVY"}
        and 1 <= zone["selection_rank"] <= 30
        and math.isfinite(zone["congestion_index"])
        and math.isfinite(zone["modeled_emissions_pressure"]["index"])
        and zone["modeled_emissions_pressure"]["observed"] is False
        for zone in result["zones"]
    )
    assert sum(zone["watch_priority"] == "CRITICAL" for zone in result["zones"]) == 20
    assert sum(zone["watch_priority"] == "HEAVY" for zone in result["zones"]) == 10


def test_corridor_telemetry_can_skip_static_distance_without_changing_scores():
    """The map's lean score feed must preserve the ordinary telemetry values."""
    from unittest.mock import patch

    with patch(
        "services.simulator.corridor_distance_km",
        return_value=12.34,
    ) as distance_lookup:
        ordinary = get_live_corridor_telemetry(WEEKDAY_1400)
        assert distance_lookup.call_count == len(CORRIDORS)
        distance_lookup.reset_mock()
        lean = get_live_corridor_telemetry(
            WEEKDAY_1400,
            include_route_distance=False,
        )
        distance_lookup.assert_not_called()

    assert all(corridor["distance_km"] == 12.34 for corridor in ordinary)
    assert all("distance_km" not in corridor for corridor in lean)
    assert [
        {key: value for key, value in corridor.items() if key != "distance_km"}
        for corridor in ordinary
    ] == lean


def test_live_traffic_never_solves_unused_corridor_distances():
    """A cold map request must not run five A* searches for discarded fields."""
    from unittest.mock import patch

    with patch(
        "services.simulator.corridor_distance_km",
        side_effect=AssertionError("live traffic requested an unused route distance"),
    ), patch.object(live_traffic, "_tomtom_api_key", return_value=""):
        result = live_traffic.get_live_traffic()

    assert len(result["segments"]) == 5
    assert len(result["zones"]) == 30


def test_corridor_map_hotspot_ranking_and_breakdowns_are_deterministic():
    corridor_scores = {
        corridor_id: {
            "congestion_index": 50.0,
            "source": "simulated",
            "provider_confidence": None,
        }
        for corridor_id in live_traffic._CORRIDOR_POINTS
    }
    first = live_traffic.get_corridor_map_hotspots(
        WEEKDAY_1400,
        corridor_scores=corridor_scores,
    )
    second = live_traffic.get_corridor_map_hotspots(
        WEEKDAY_1400,
        corridor_scores=corridor_scores,
    )

    assert [zone["zone_id"] for zone in first] == [
        zone["zone_id"] for zone in second
    ]
    assert [zone["selection_rank"] for zone in first] == list(range(1, 31))
    assert [zone["selection_score"] for zone in first] == sorted(
        (zone["selection_score"] for zone in first),
        reverse=True,
    )
    for previous, current in zip(first, first[1:]):
        if previous["selection_score"] == current["selection_score"]:
            assert previous["zone_id"] < current["zone_id"]

    for zone in first:
        dynamic = zone["score_breakdown"]
        reconstructed_dynamic = max(0.0, min(
            100.0,
            dynamic["corridor_contribution"]
            + dynamic["recurring_contribution"],
        ))
        assert round(reconstructed_dynamic, 1) == zone["congestion_index"]
        assert dynamic["published_congestion_index"] == zone["congestion_index"]
        assert abs(
            dynamic["corridor_weight"] + dynamic["recurring_weight"] - 1.0
        ) < 1e-6

        selection = zone["selection_breakdown"]
        assert selection["weights"] == live_traffic.HOTSPOT_SELECTION_WEIGHTS
        assert round(sum(selection["weighted_components"].values()), 3) == (
            zone["selection_score"]
        )


def test_live_hotspots_use_provider_scores_with_per_corridor_fallback():
    """Resolved TomTom flow must reach map zones without becoming a zone claim."""
    from unittest.mock import patch

    telemetry = [
        {"id": f"corridor-{index}", "live_congestion_score": 20.0}
        for index in range(1, 6)
    ]
    tomtom_segments = {
        "corridor-1": {
            "current_speed_kmh": 0.0,
            "free_flow_speed_kmh": 60.0,
            "confidence": 0.8,
        }
    }
    with clock.frozen(WEEKDAY_1400), patch.object(
        live_traffic,
        "_tomtom_api_key",
        return_value="provider-test-key",
    ), patch.object(
        live_traffic,
        "_tomtom_segments",
        return_value=tomtom_segments,
    ), patch(
        "services.simulator.get_live_corridor_telemetry",
        return_value=telemetry,
    ):
        result = live_traffic.get_live_traffic()

    segments = {segment["corridor_id"]: segment for segment in result["segments"]}
    assert segments["corridor-1"]["congestion_index"] == 95.0
    assert segments["corridor-1"]["source"] == "tomtom_live"
    assert segments["corridor-1"]["provider_confidence"] == 0.8
    assert segments["corridor-4"]["congestion_index"] == 20.0
    assert segments["corridor-4"]["source"] == "simulated"

    panbil = next(
        zone for zone in result["zones"] if zone["zone_id"] == "zone-panbil"
    )
    inputs = panbil["score_breakdown"]["input_signals"]
    assert inputs["corridor-1"] == {
        "affinity_weight": 0.55,
        "congestion_index": 95.0,
        "source": "tomtom_live",
        "confidence": 0.72,
    }
    assert inputs["corridor-4"] == {
        "affinity_weight": 0.45,
        "congestion_index": 20.0,
        "source": "simulated",
        "confidence": 0.2,
    }
    assert panbil["score_breakdown"]["corridor_pressure"] == 61.25
    assert panbil["source"] == "modelled_spatial_hotspot"
    assert panbil["observed"] is False


def test_live_traffic_fetches_corridor_points_concurrently():
    """Five provider calls must fit inside the frontend's three-second budget."""
    import threading
    from unittest.mock import patch

    original_key = os.environ.get("TOMTOM_API_KEY")
    barrier = threading.Barrier(len(live_traffic._CORRIDOR_POINTS))

    def fake_segment(_lat, _lng, _api_key):
        barrier.wait(timeout=1)
        return {
            "current_speed_kmh": 30.0,
            "free_flow_speed_kmh": 60.0,
            "confidence": 0.9,
        }

    try:
        os.environ["TOMTOM_API_KEY"] = "concurrency-test-key"
        with patch.object(live_traffic, "_tomtom_segment", fake_segment):
            result = live_traffic.get_live_traffic()

        assert result["overall_source"] == "tomtom_live"
        assert len(result["segments"]) == len(live_traffic._CORRIDOR_POINTS)
        assert all(segment["source"] == "tomtom_live" for segment in result["segments"])
    finally:
        if original_key is None:
            os.environ.pop("TOMTOM_API_KEY", None)
        else:
            os.environ["TOMTOM_API_KEY"] = original_key


def test_live_traffic_sample_points_follow_their_corridor_roads():
    """A live probe must not silently sample an unrelated nearby street."""
    corridor_by_id = {
        corridor["id"]: corridor for corridor in route_solver.CORRIDORS
    }
    assert set(live_traffic._CORRIDOR_POINTS) == set(corridor_by_id)

    for corridor_id, point in live_traffic._CORRIDOR_POINTS.items():
        corridor = corridor_by_id[corridor_id]
        route = router.route_between(
            corridor["origin"],
            corridor["destination"],
        )
        distance_to_route_m = min(
            router.haversine_m(point, route_point)
            for route_point in route["geometry"]
        )
        assert distance_to_route_m <= 250, (
            f"{corridor_id} traffic probe is {distance_to_route_m:.0f} m "
            "from its routed road"
        )


def test_tomtom_malformed_success_is_not_reported_as_live():
    """A JSON 200 without measurements must fall back instead of becoming index 95."""
    from unittest.mock import patch

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"flowSegmentData": {}}'

    live_traffic._tomtom_cache.clear()
    try:
        with patch.object(
            live_traffic.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: FakeResponse(),
        ):
            assert live_traffic._tomtom_segment(1.1, 104.0, "test-key") is None
    finally:
        live_traffic._tomtom_cache.clear()


def _learning_edge_fixture():
    source = next(node for node, edges in router.ROAD_ADJ.items() if edges)
    return source, router.ROAD_ADJ[source][0]


def _valid_learning_payload(observation_id="trip-verified:0"):
    source, edge = _learning_edge_fixture()
    profile = router.vehicle_profile("COMMUTER")
    physical_floor = edge.distance_m / profile.max_speed_kph * 3.6
    return {
        "observation_id": observation_id,
        "graph_revision": router.GRAPH_REVISION,
        "source_node": source,
        "target_node": edge.target,
        "road_index": edge.road_index,
        "vehicle_type": "COMMUTER",
        "moving_duration_s": max(0.1, physical_floor * 1.2),
        "observed_at": clock.now(),
        "verification_method": "FIRST_PARTY_GPS_MAP_MATCH",
        "map_match_confidence": 0.98,
        "weather": 0,
        "network_congestion_score": 10.0,
        "local_congestion_score": 8.0,
    }


def _stored_learning_observation(observation_id, *, graph_revision=None, duration=None):
    payload = _valid_learning_payload(observation_id)
    source, edge = _learning_edge_fixture()
    return learning_store.VerifiedTraversalObservation(
        observation_id=observation_id,
        graph_revision=graph_revision or router.GRAPH_REVISION,
        source_node=source,
        target_node=edge.target,
        road_index=edge.road_index,
        vehicle_type="COMMUTER",
        moving_duration_s=float(duration or payload["moving_duration_s"]),
        observed_at_epoch=int(time.time()),
        verification_method="FIRST_PARTY_GPS_MAP_MATCH",
        map_match_confidence=0.98,
        weather=0,
        network_congestion_score=10.0,
        local_congestion_score=8.0,
        edge_distance_m=edge.distance_m,
    )


def test_route_learning_request_contract_forbids_provider_content_and_bad_conditions():
    payload = _valid_learning_payload()
    invalid_variants = [
        {**payload, "provider": "google_routes"},
        {**payload, "polyline": "provider-geometry"},
        {**payload, "weather": 1},
        {**payload, "network_congestion_score": 25.1},
        {**payload, "local_congestion_score": 25.1},
        {**payload, "map_match_confidence": 0.899},
        {**payload, "observed_at": datetime(2026, 8, 10, 12, 0)},
        {**payload, "verification_method": "GOOGLE_ROUTE"},
    ]
    for candidate in invalid_variants:
        try:
            api_main.RouteLearningObservation(**candidate)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"invalid learning observation accepted: {candidate}")


def test_route_learning_endpoints_require_admin_authorization():
    original_token = os.environ.get("CROSSFLOW_ADMIN_TOKEN")
    os.environ["CROSSFLOW_ADMIN_TOKEN"] = "learning-test-token"
    batch = api_main.RouteLearningBatch(observations=[
        api_main.RouteLearningObservation(**_valid_learning_payload()),
    ])
    try:
        for call in (
            lambda: api_main.api_route_learning_status(None),
            lambda: api_main.api_route_learning_status("wrong-token"),
            lambda: api_main.api_ingest_route_learning_observations(batch, None),
        ):
            try:
                call()
            except HTTPException as err:
                assert err.status_code == 403
            else:
                raise AssertionError("unprotected route-learning endpoint")
    finally:
        if original_token is None:
            os.environ.pop("CROSSFLOW_ADMIN_TOKEN", None)
        else:
            os.environ["CROSSFLOW_ADMIN_TOKEN"] = original_token


def test_route_learning_api_rejects_stale_graph_and_unknown_directed_edge():
    original_token = os.environ.get("CROSSFLOW_ADMIN_TOKEN")
    original_store = api_main.route_learning_store
    os.environ["CROSSFLOW_ADMIN_TOKEN"] = "learning-test-token"
    with tempfile.TemporaryDirectory() as directory:
        store = learning_store.RouteLearningStore(
            os.path.join(directory, "route-learning.db"),
        )
        api_main.route_learning_store = store
        try:
            stale = api_main.RouteLearningBatch(observations=[
                api_main.RouteLearningObservation(**{
                    **_valid_learning_payload("stale-graph:0"),
                    "graph_revision": "0" * 64,
                }),
            ])
            unknown = api_main.RouteLearningBatch(observations=[
                api_main.RouteLearningObservation(**{
                    **_valid_learning_payload("unknown-edge:0"),
                    "target_node": -999999,
                }),
            ])
            for batch, status_code in ((stale, 409), (unknown, 400)):
                try:
                    api_main.api_ingest_route_learning_observations(
                        batch,
                        "learning-test-token",
                    )
                except HTTPException as err:
                    assert err.status_code == status_code
                else:
                    raise AssertionError("invalid edge observation was persisted")
            assert store.status(router.GRAPH_REVISION)[
                "current_graph_observation_count"
            ] == 0
        finally:
            api_main.route_learning_store = original_store
            store.close()
            if original_token is None:
                os.environ.pop("CROSSFLOW_ADMIN_TOKEN", None)
            else:
                os.environ["CROSSFLOW_ADMIN_TOKEN"] = original_token


def test_route_learning_api_rejects_implausible_duration_and_old_timestamp():
    payload = _valid_learning_payload("implausible-duration:0")
    source, edge = _learning_edge_fixture()
    profile = router.vehicle_profile(payload["vehicle_type"])
    physical_floor = edge.distance_m / profile.max_speed_kph * 3.6
    invalid = [
        {**payload, "moving_duration_s": physical_floor * 0.5},
        {
            **payload,
            "observation_id": "old-observation:0",
            "observed_at": clock.now() - timedelta(
                days=learning_store.OBSERVATION_RETENTION_DAYS + 1,
            ),
        },
        {
            **payload,
            "observation_id": "future-observation:0",
            "observed_at": clock.now() + timedelta(minutes=6),
        },
    ]
    for candidate in invalid:
        request = api_main.RouteLearningObservation(**candidate)
        try:
            api_main._canonical_learning_observation(request, clock.now())
        except HTTPException as err:
            assert err.status_code == 400
        else:
            raise AssertionError("implausible traversal observation accepted")
    assert source in router.ROAD_ADJ


def test_route_learning_store_is_idempotent_revisioned_and_reloadable():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "route-learning.db")
        observation = _stored_learning_observation("idempotent-trip:0")
        first_store = learning_store.RouteLearningStore(path)
        first = first_store.ingest(
            [observation],
            current_graph_revision=router.GRAPH_REVISION,
        )
        duplicate = first_store.ingest(
            [observation],
            current_graph_revision=router.GRAPH_REVISION,
        )
        assert first.accepted == 1 and first.duplicates == 0
        assert first.revision == 1
        assert duplicate.accepted == 0 and duplicate.duplicates == 1
        assert duplicate.revision == first.revision
        first_store.close()

        reopened = learning_store.RouteLearningStore(path)
        status = reopened.status(router.GRAPH_REVISION)
        assert status["learning_revision"] == 1
        assert status["current_graph_observation_count"] == 1
        assert status["qualifying_edge_count"] == 0
        assert status["provenance"]["google_routes_content_persisted"] is False
        reopened.close()


def test_route_learning_snapshot_qualifies_robust_samples_and_is_immutable():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "route-learning.db")
        store = learning_store.RouteLearningStore(path)
        base = _stored_learning_observation("qualifying-trip:0").moving_duration_s
        observations = [
            _stored_learning_observation(
                f"qualifying-trip:{index}",
                duration=base * (1.0 + (index - 2) * 0.01),
            )
            for index in range(5)
        ]
        result = store.ingest(
            observations,
            current_graph_revision=router.GRAPH_REVISION,
        )
        snapshot = store.snapshot(router.GRAPH_REVISION)
        assert result.accepted == 5
        assert result.qualifying_edge_count == 1
        assert len(snapshot.entries) == 1
        learned = next(iter(snapshot.entries.values()))
        assert learned.sample_count == 5
        assert learned.confidence >= learning_store.MIN_QUALIFYING_CONFIDENCE
        try:
            snapshot.entries[(0, 0, 0, "COMMUTER")] = learned
        except TypeError:
            pass
        else:
            raise AssertionError("route-learning snapshot mapping is mutable")
        store.close()

        reopened = learning_store.RouteLearningStore(path)
        assert len(reopened.snapshot(router.GRAPH_REVISION).entries) == 1
        reopened.close()


def test_route_learning_snapshot_is_scoped_to_exact_graph_revision():
    with tempfile.TemporaryDirectory() as directory:
        store = learning_store.RouteLearningStore(
            os.path.join(directory, "route-learning.db"),
        )
        observations = [
            _stored_learning_observation(f"graph-scope:{index}")
            for index in range(5)
        ]
        store.ingest(
            observations,
            current_graph_revision=router.GRAPH_REVISION,
        )
        assert len(store.snapshot(router.GRAPH_REVISION).entries) == 1
        other_revision = "f" * 64
        assert other_revision != router.GRAPH_REVISION
        assert len(store.snapshot(other_revision).entries) == 0
        other_status = store.status(other_revision)
        assert other_status["current_graph_observation_count"] == 0
        assert other_status["stale_graph_observation_count"] == 5
        store.close()


def _install_synthetic_learning_graph():
    originals = (router.NODES, router.ROAD_ADJ, router.ADJ, router.NODE_META)
    router.NODES = {
        1: (0.0, 0.0),
        2: (0.0, 0.0009),
        3: (0.00015, 0.00092),
        4: (0.0, 0.0018),
    }
    router.ROAD_ADJ = {
        1: [
            router.RoadEdge(
                2, 105.0, road_index=10, name="Baseline Road", highway="primary",
            ),
            router.RoadEdge(
                3, 112.0, road_index=20, name="Learned Lane", highway="primary",
            ),
        ],
        2: [router.RoadEdge(
            4, 105.0, road_index=10, name="Baseline Road", highway="primary",
        )],
        3: [router.RoadEdge(
            4, 112.0, road_index=20, name="Learned Lane", highway="primary",
        )],
        4: [],
    }
    router.ADJ = {
        source: [(edge.target, edge.distance_m) for edge in edges]
        for source, edges in router.ROAD_ADJ.items()
    }
    router.NODE_META = {}
    return originals


def _restore_synthetic_learning_graph(originals):
    router.NODES, router.ROAD_ADJ, router.ADJ, router.NODE_META = originals


def _synthetic_learning_observations(duration_s):
    now_epoch = int(time.time())
    observations = []
    for source, edge in (
        (1, router.ROAD_ADJ[1][1]),
        (3, router.ROAD_ADJ[3][0]),
    ):
        for sample in range(5):
            observations.append(learning_store.VerifiedTraversalObservation(
                observation_id=f"synthetic:{duration_s}:{source}:{sample}",
                graph_revision=router.GRAPH_REVISION,
                source_node=source,
                target_node=edge.target,
                road_index=edge.road_index,
                vehicle_type="COMMUTER",
                moving_duration_s=duration_s,
                observed_at_epoch=now_epoch - sample,
                verification_method="FIRST_PARTY_GPS_MAP_MATCH",
                map_match_confidence=0.99,
                weather=0,
                network_congestion_score=5.0,
                local_congestion_score=5.0,
                edge_distance_m=edge.distance_m,
            ))
    return observations


def test_route_learning_uses_one_shared_store_and_revisioned_cache_identity():
    assert api_main.route_learning_store is learning_store.DEFAULT_ROUTE_LEARNING_STORE
    assert router.route_learning_store is learning_store.DEFAULT_ROUTE_LEARNING_STORE
    first_snapshot = learning_store._empty_snapshot(  # noqa: SLF001
        router.GRAPH_REVISION,
        revision=9001,
    )
    same_revision = learning_store._empty_snapshot(  # noqa: SLF001
        router.GRAPH_REVISION,
        revision=9001,
    )
    next_revision = learning_store._empty_snapshot(  # noqa: SLF001
        router.GRAPH_REVISION,
        revision=9002,
    )
    assert first_snapshot == same_revision
    assert hash(first_snapshot) == hash(same_revision)

    src = router.LANDMARKS["nagoya"]
    dst = router.LANDMARKS["batam_centre"]
    args = (
        src, dst, "[]", "Origin", "Destination", False, "COMMUTER",
        0.0, 0, "BALANCED",
    )
    router._route_between_nodes_cached.cache_clear()
    router._route_between_nodes_cached(*args, first_snapshot)
    after_first = router._route_between_nodes_cached.cache_info()
    router._route_between_nodes_cached(*args, same_revision)
    after_same = router._route_between_nodes_cached.cache_info()
    router._route_between_nodes_cached(*args, next_revision)
    after_next = router._route_between_nodes_cached.cache_info()
    assert after_same.hits == after_first.hits + 1
    assert after_next.misses == after_same.misses + 1
    router._route_between_nodes_cached.cache_clear()


def test_verified_learning_changes_clear_path_but_all_three_gates_disable_it():
    originals = _install_synthetic_learning_graph()
    with tempfile.TemporaryDirectory() as directory:
        store = learning_store.RouteLearningStore(
            os.path.join(directory, "route-learning.db"),
        )
        try:
            store.ingest(
                _synthetic_learning_observations(6.0),
                current_graph_revision=router.GRAPH_REVISION,
            )
            snapshot = store.snapshot(router.GRAPH_REVISION)
            empty = learning_store._empty_snapshot(router.GRAPH_REVISION)  # noqa: SLF001
            baseline = router.astar_detailed(1, 4, learning_snapshot=empty)
            clear = router.astar_detailed(1, 4, learning_snapshot=snapshot)
            rain = router.astar_detailed(
                1, 4, weather=1, learning_snapshot=snapshot,
            )
            busy_network = router.astar_detailed(
                1, 4, network_congestion_score=25.1,
                learning_snapshot=snapshot,
            )
            busy_local = router.astar_detailed(
                1, 4, congestion_scores={3: 25.1},
                learning_snapshot=snapshot,
            )
            assert baseline and baseline.nodes == [1, 2, 4]
            assert clear and clear.nodes == [1, 3, 4]
            assert rain and rain.nodes == baseline.nodes
            assert busy_network and busy_network.nodes == baseline.nodes
            assert busy_local and busy_local.nodes == baseline.nodes
        finally:
            store.close()
            _restore_synthetic_learning_graph(originals)


def test_learned_edge_clamp_preserves_admissible_a_star_result():
    from unittest.mock import patch

    originals = _install_synthetic_learning_graph()
    with tempfile.TemporaryDirectory() as directory:
        store = learning_store.RouteLearningStore(
            os.path.join(directory, "route-learning.db"),
        )
        try:
            # Direct store ingestion simulates corrupted telemetry that the API
            # plausibility boundary rejects. The router must still clamp it.
            store.ingest(
                _synthetic_learning_observations(0.001),
                current_graph_revision=router.GRAPH_REVISION,
            )
            snapshot = store.snapshot(router.GRAPH_REVISION)
            profile = router.vehicle_profile("COMMUTER")
            learned_edge = router.ROAD_ADJ[1][1]
            components = router._edge_cost_components(
                1,
                learned_edge,
                profile,
                learning_snapshot=snapshot,
            )
            physical_floor = (
                learned_edge.distance_m / profile.max_speed_kph * 3.6
            )
            assert math.isclose(
                components["free_flow_s"],
                physical_floor,
                rel_tol=0,
                abs_tol=1e-9,
            )

            normal = router.astar_detailed(1, 4, learning_snapshot=snapshot)
            with patch.object(router, "haversine_m", lambda *_args: 0.0):
                zero_heuristic = router.astar_detailed(
                    1,
                    4,
                    learning_snapshot=snapshot,
                )
            assert normal and zero_heuristic
            assert normal.nodes == zero_heuristic.nodes
            assert math.isclose(
                normal.search_cost_s,
                zero_heuristic.search_cost_s,
                rel_tol=0,
                abs_tol=1e-9,
            )
        finally:
            store.close()
            _restore_synthetic_learning_graph(originals)


def test_shortcut_metadata_names_only_selected_edges_and_propagates_alternative():
    originals = _install_synthetic_learning_graph()
    with tempfile.TemporaryDirectory() as directory:
        store = learning_store.RouteLearningStore(
            os.path.join(directory, "route-learning.db"),
        )
        try:
            store.ingest(
                _synthetic_learning_observations(6.0),
                current_graph_revision=router.GRAPH_REVISION,
            )
            snapshot = store.snapshot(router.GRAPH_REVISION)
            selected = router.astar_detailed(1, 4, learning_snapshot=snapshot)
            unlearned = router.astar_detailed(
                1,
                4,
                learning_snapshot=learning_store._empty_snapshot(  # noqa: SLF001
                    router.GRAPH_REVISION,
                ),
            )
            assert selected and unlearned
            selected_payload = router._path_payload(
                selected,
                1,
                4,
                "Origin",
                "Destination",
                learning_snapshot=snapshot,
            )
            unlearned_payload = router._path_payload(
                unlearned,
                1,
                4,
                "Origin",
                "Destination",
                learning_snapshot=snapshot,
            )
            assert len(selected_payload["shortcuts_used"]) == 1
            shortcut = selected_payload["shortcuts_used"][0]
            assert shortcut["time_saved_mins"] > 0
            assert shortcut["sample_count"] == 5
            assert len(shortcut["edge_keys"]) == 2
            assert {edge["source_node"] for edge in shortcut["edge_keys"]} == {1, 3}
            assert "shortcuts_used" not in unlearned_payload
            breakdown = selected_payload["routing_cost_breakdown"]
            assert breakdown["learned_free_flow_adjustment_mins"] < 0
            assert selected_payload["routing_model"]["learning"]["revision"] == (
                snapshot.revision
            )

            alternative = {
                **selected_payload,
                "route_geometry": selected_payload["geometry"],
                "route_data_source": "openstreetmap",
            }
            route = {
                "distance_km": unlearned.distance_m / 1000.0,
                "navigation": router.generate_navigation(unlearned),
                "data_source": "openstreetmap",
                "alternatives": [alternative],
            }
            options = route_solver._alternative_route_options(
                route,
                router.vehicle_profile("COMMUTER"),
                {},
                0.0,
                1.0,
                1.0,
                WEEKDAY_1400,
                None,
                WEEKDAY_1400,
                router.NODES[1],
                router.NODES[4],
                0.0,
                0.0,
            )
            assert options[0]["shortcuts_used"] == selected_payload["shortcuts_used"]
        finally:
            store.close()
            _restore_synthetic_learning_graph(originals)


def test_retraining_endpoint_requires_admin_authorization():
    original_token = os.environ.pop("CROSSFLOW_ADMIN_TOKEN", None)
    try:
        try:
            api_main.api_retrain_model(None)
        except HTTPException as err:
            assert err.status_code == 403
        else:
            raise AssertionError("unauthenticated model retraining was accepted")
    finally:
        if original_token is not None:
            os.environ["CROSSFLOW_ADMIN_TOKEN"] = original_token


# --------------------------------------------------------------------------
# Outbound TLS trust store
# --------------------------------------------------------------------------

def test_tls_context_verifies_certificates_and_loads_ca_roots():
    """An empty trust store silently degraded every provider to an estimate."""
    context = tls.default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0, (
        "no CA roots are loaded, so every outbound HTTPS call will fail "
        "verification and fall back to an offline estimate"
    )


def test_tls_default_context_is_shared_and_new_context_is_independent():
    """Per-host options must not leak into every other caller's requests."""
    assert tls.default_context() is tls.default_context()
    separate = tls.new_context()
    assert separate is not tls.default_context()
    separate.options |= ssl.OP_LEGACY_SERVER_CONNECT
    assert not tls.default_context().options & ssl.OP_LEGACY_SERVER_CONNECT


def test_osrm_request_presents_a_verifying_tls_context():
    """The Singapore road legs are straight lines whenever this regresses."""
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "code": "Ok",
                "routes": [{
                    "distance": 8000.0,
                    "duration": 900.0,
                    "geometry": {"coordinates": [
                        [103.8198, 1.3521], [103.8300, 1.3400],
                        [103.8547, 1.2966],
                    ]},
                    "legs": [{"steps": [
                        {"distance": 4000.0, "maneuver": {
                            "type": "depart", "modifier": "straight",
                            "location": [103.8198, 1.3521],
                        }, "name": "Woodlands Road"},
                        {"distance": 4000.0, "maneuver": {
                            "type": "turn", "modifier": "left",
                            "location": [103.8300, 1.3400],
                        }, "name": "Bukit Timah Road"},
                    ]}],
                }],
            }).encode()

    def fake_urlopen(request, timeout, **kwargs):
        captured["timeout"] = timeout
        captured["context"] = kwargs.get("context")
        return FakeResponse()

    original_urlopen = multimodal_router.urllib.request.urlopen
    try:
        multimodal_router.urllib.request.urlopen = fake_urlopen
        leg = multimodal_router._osrm_route((1.3521, 103.8198), (1.2966, 103.8547))
    finally:
        multimodal_router.urllib.request.urlopen = original_urlopen

    assert leg is not None
    assert leg["data_source"] == "osrm_openstreetmap"
    assert leg["is_estimate"] is False
    assert len(leg["geometry"]) == 3
    context = captured["context"]
    assert context is tls.default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


# --------------------------------------------------------------------------
# Multi-stop journeys
# --------------------------------------------------------------------------

# Closely-spaced Batam stops keep each A* search cheap; the schedule and
# aggregation logic under test does not depend on leg length.
_MULTI_STOP_NAGOYA = {"lat": 1.1465, "lng": 104.0125, "name": "Nagoya Hill"}
_MULTI_STOP_CENTRE = {"lat": 1.1318, "lng": 104.0554, "name": "Batam Centre"}
_MULTI_STOP_AMPAR = {"lat": 1.1480, "lng": 104.0060, "name": "Batu Ampar"}


def _multi_stop_result(**overrides):
    parameters = {
        "stops": [
            _MULTI_STOP_AMPAR,
            {**_MULTI_STOP_NAGOYA, "dwell_mins": 20},
            _MULTI_STOP_CENTRE,
        ],
        "vehicle_type": "COMMUTER",
        "hour": 14,
        "now": WEEKDAY_1400,
    }
    parameters.update(overrides)
    return route_solver.optimize_multi_stop_route(**parameters)


def test_multi_stop_route_chains_legs_into_one_schedule():
    result = _multi_stop_result()

    assert result["route_type"] == "MULTI_STOP_ROUTE"
    assert result["corridor"]["stop_count"] == 3
    assert result["corridor"]["leg_count"] == 2
    assert len(result["legs"]) == 2
    assert [stop["role"] for stop in result["stops"]] == [
        "ORIGIN", "WAYPOINT", "DESTINATION",
    ]

    first, second = result["legs"]
    assert first["to_name"] == "Nagoya Hill"
    assert second["from_name"] == "Nagoya Hill"
    # The second leg departs only after the first arrives plus the dwell.
    first_arrival = datetime.fromisoformat(first["arrival"])
    second_departure = datetime.fromisoformat(second["departure"])
    assert second_departure - first_arrival == timedelta(minutes=20)


def test_multi_stop_totals_are_the_sum_of_their_legs():
    result = _multi_stop_result()
    legs = result["legs"]

    assert result["corridor"]["distance_km"] == round(
        sum(leg["distance_km"] for leg in legs), 2,
    )
    assert result["estimated_travel_time_mins"] == round(
        sum(leg["estimated_travel_time_mins"] for leg in legs), 1,
    )
    assert result["co2_emissions_kg"] == round(
        sum(leg["co2_emissions_kg"] for leg in legs), 2,
    )
    assert result["dwell_time_mins"] == 20.0

    departure = datetime.fromisoformat(result["planned_departure"])
    arrival = datetime.fromisoformat(result["estimated_arrival"])
    elapsed = round((arrival - departure).total_seconds() / 60.0, 1)
    assert result["total_eta_mins"] == elapsed
    # Dwell is real elapsed time, so the journey cannot finish in travel alone.
    assert result["total_eta_mins"] > result["estimated_travel_time_mins"]


def test_multi_stop_geometry_and_navigation_join_without_duplication():
    result = _multi_stop_result()
    legs = result["legs"]

    combined = result["route_geometry"]
    leg_points = sum(len(leg["route_geometry"]) for leg in legs)
    # Exactly one shared point is dropped at the intermediate stop.
    assert len(combined) == leg_points - 1
    assert combined[0] == list(legs[0]["route_geometry"][0])
    assert combined[-1] == list(legs[-1]["route_geometry"][-1])

    maneuvers = result["navigation"]["maneuvers"]
    assert [step["step"] for step in maneuvers] == list(
        range(1, len(maneuvers) + 1),
    )
    cumulative = [step["cumulative_distance_m"] for step in maneuvers]
    assert cumulative == sorted(cumulative), "distance from start must not reset"
    # The stop in the middle is a waypoint, not the end of the journey.
    assert [step["type"] for step in maneuvers].count("ARRIVE") == 1
    assert maneuvers[-1]["type"] == "ARRIVE"
    waypoints = [step for step in maneuvers if step["type"] == "WAYPOINT"]
    assert len(waypoints) == 1
    assert "Nagoya Hill" in waypoints[0]["instruction"]


def test_multi_stop_dwell_applies_only_to_intermediate_stops():
    result = _multi_stop_result(stops=[
        {**_MULTI_STOP_AMPAR, "dwell_mins": 45},
        {**_MULTI_STOP_NAGOYA, "dwell_mins": 10},
        {**_MULTI_STOP_CENTRE, "dwell_mins": 60},
    ])
    assert [stop["dwell_mins"] for stop in result["stops"]] == [0.0, 10.0, 0.0]
    assert result["dwell_time_mins"] == 10.0


def test_multi_stop_order_optimization_pins_both_endpoints():
    zigzag = [
        _MULTI_STOP_AMPAR,
        {"lat": 1.1211, "lng": 104.1147, "name": "Hang Nadim"},
        _MULTI_STOP_NAGOYA,
        _MULTI_STOP_CENTRE,
    ]
    optimized = route_solver.optimize_multi_stop_route(
        stops=zigzag, vehicle_type="COMMUTER", hour=14, now=WEEKDAY_1400,
        optimize_order=True,
    )
    metadata = optimized["stop_order_optimization"]
    assert metadata["applied"] is True
    assert metadata["method"] == "nearest_neighbour_2opt_straight_line"
    # A reordering heuristic that quietly moved the endpoints would send the
    # driver somewhere they never asked to start or finish.
    assert optimized["stops"][0]["name"] == "Batu Ampar"
    assert optimized["stops"][-1]["name"] == "Batam Centre"
    assert {stop["name"] for stop in optimized["stops"]} == {
        stop["name"] for stop in zigzag
    }
    assert [stop["name"] for stop in optimized["stops"]] != [
        stop["name"] for stop in zigzag
    ]
    assert "straight-line" in metadata["limitations"]


def test_multi_stop_order_is_not_optimized_across_a_ferry_crossing():
    original_osrm = multimodal_router._osrm_route
    try:
        # Keep the suite offline; the ordering decision under test is made
        # before any road engine is consulted.
        multimodal_router._osrm_route = lambda *_args: None
        result = route_solver.optimize_multi_stop_route(
            stops=[
                {"lat": 1.29027, "lng": 103.851959, "name": "Singapore CBD"},
                _MULTI_STOP_NAGOYA,
                _MULTI_STOP_CENTRE,
                _MULTI_STOP_AMPAR,
            ],
            vehicle_type="COMMUTER", hour=14, now=WEEKDAY_1400,
            optimize_order=True,
        )
    finally:
        multimodal_router._osrm_route = original_osrm
    metadata = result["stop_order_optimization"]
    assert metadata["requested"] is True
    assert metadata["applied"] is False
    assert "ferry crossing" in metadata["reason"]
    assert result["stops"][1]["name"] == "Nagoya Hill"
    assert result["route_type"] == "MULTIMODAL_MULTI_STOP_ROUTE"


def test_multi_stop_rejects_unusable_itineraries():
    cases = [
        ([_MULTI_STOP_AMPAR, _MULTI_STOP_NAGOYA], "at least three stops"),
        ([_MULTI_STOP_AMPAR] * 9, "at most 8 stops"),
        (
            [{"lat": -6.2, "lng": 106.8}, _MULTI_STOP_NAGOYA, _MULTI_STOP_CENTRE],
            "outside the supported",
        ),
        (
            [_MULTI_STOP_AMPAR, {"lng": 104.01}, _MULTI_STOP_CENTRE],
            "numeric lat and lng",
        ),
        (
            [
                _MULTI_STOP_AMPAR,
                {**_MULTI_STOP_NAGOYA, "dwell_mins": -5},
                _MULTI_STOP_CENTRE,
            ],
            "negative dwell_mins",
        ),
    ]
    for stops, expected in cases:
        try:
            route_solver.optimize_multi_stop_route(
                stops=stops, vehicle_type="COMMUTER", hour=14, now=WEEKDAY_1400,
            )
        except ValueError as error:
            assert expected in str(error), f"{expected!r} not in {error}"
        else:
            raise AssertionError(f"accepted an itinerary that {expected}")


def test_multi_stop_endpoint_returns_an_enveloped_journey():
    original_load = ferry_freshness_store.load
    try:
        latest = "2026-08-13T00:30:50+07:00"
        snapshot_id = ferry_schedule.timetable_metadata(
            load_durable=False,
        )["snapshot_id"]
        ferry_freshness_store.load = lambda candidate: {
            "snapshot_id": snapshot_id,
            "latest_checked_at": latest,
            "last_verified_at": latest,
        }
        ferry_schedule._reset_runtime_verification_for_tests()
        with clock.frozen(WEEKDAY_1400):
            response = api_main.api_optimize_multi_stop_route(
                api_main.MultiStopRouteRequest(
                    stops=[
                        api_main.RouteStop(**_MULTI_STOP_AMPAR),
                        api_main.RouteStop(**_MULTI_STOP_NAGOYA, dwell_mins=15),
                        api_main.RouteStop(**_MULTI_STOP_CENTRE),
                    ],
                ),
            )
    finally:
        ferry_freshness_store.load = original_load
        ferry_schedule._reset_runtime_verification_for_tests()

    assert response["route_type"] == "MULTI_STOP_ROUTE"
    assert response["corridor"]["leg_count"] == 2
    assert response["generated_at"]
    assert response["service_audit"]["routing_contract_version"] == 1


def test_multi_stop_request_model_rejects_a_two_point_itinerary():
    try:
        api_main.MultiStopRouteRequest(stops=[
            api_main.RouteStop(**_MULTI_STOP_AMPAR),
            api_main.RouteStop(**_MULTI_STOP_CENTRE),
        ])
    except ValidationError:
        pass
    else:
        raise AssertionError("a two-stop multi-destination request was accepted")


def test_leg_mode_matches_the_full_solver_geometry_without_alternatives():
    """Leg mode must save searches, not change which road is chosen."""
    parameters = {
        "origin_lat": 1.1465, "origin_lng": 104.0125,
        "destination_lat": 1.1318, "destination_lng": 104.0554,
        "vehicle_type": "COMMUTER", "hour": 14, "now": WEEKDAY_1400,
    }
    full = optimize_free_route(**parameters)
    leg = optimize_free_route(**parameters, leg_mode=True)

    assert leg["route_geometry"] == full["route_geometry"]
    assert leg["corridor"]["distance_km"] == full["corridor"]["distance_km"]
    assert leg["alternative_routes"] == []
    # A leg has no authority to defer; the journey owns that decision.
    assert leg["optimal_departure"]["recommended"] == "DEPART_NOW"
    assert leg["co2_saved_kg"] == 0.0


def test_vercel_package_import_bootstraps_backend_modules():
    """Vercel imports the function as backend.main from the repository root."""
    with tempfile.TemporaryDirectory(prefix="crossflow-vercel-import-") as temp_dir:
        environment = os.environ.copy()
        environment["CROSSFLOW_HISTORY_DB"] = os.path.join(
            temp_dir,
            "history.db",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import backend.main as deployed; assert deployed.app",
            ],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------

def main() -> int:
    try:
        tests = [(n, f) for n, f in sorted(globals().items())
                 if n.startswith("test_") and callable(f)]
        passed, failures = 0, []

        for name, fn in tests:
            try:
                fn()
                print(f"  ok   {name}")
                passed += 1
            except AssertionError as err:
                print(f"  FAIL {name}: {err}")
                failures.append(name)
            except Exception as err:  # noqa: BLE001
                print(f"  ERR  {name}: {type(err).__name__}: {err}")
                failures.append(name)

        print(f"\n{passed}/{len(tests)} passed")
        if failures:
            print("failed: " + ", ".join(failures))
            return 1
        print("ALL BACKEND CHECKS PASSED")
        return 0
    finally:
        historical_store.close()
        _TEST_HISTORY_DIRECTORY.cleanup()


if __name__ == "__main__":
    sys.exit(main())
