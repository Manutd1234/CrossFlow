"""Focused vehicle-aware graph routing tests.

Run with:
    python -m unittest backend.tests.test_vehicle_routing
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
BACKEND_DIR = os.path.join(PROJECT_DIR, "backend")
for path in (BACKEND_DIR, PROJECT_DIR):
    if path not in sys.path:
        sys.path.append(path)

from scripts import build_graph  # noqa: E402
from services import router  # noqa: E402
from services.service_contracts import (  # noqa: E402
    ApprovedGraphOverride,
    ApprovedGraphOverrideSnapshot,
)


def _edge(target: int, distance_m: float, road_index: int, **metadata):
    return router.RoadEdge(target, distance_m, road_index, **metadata)


class VehicleRoutingPolicyTests(unittest.TestCase):
    def test_local_preference_reports_v3_constraint_scope_truthfully(self):
        payload = router.route_preference_payload(
            router.route_preference("LOCAL"),
        )
        note = payload["road_scope_note"]
        self.assertIn("eligible service roads", note)
        self.assertIn("tags are enforced", note)
        self.assertIn("missing physical tags remain unknown", note)

    def test_explicit_access_and_mode_specific_overrides(self):
        private = _edge(2, 100.0, 1, highway="residential", access="private")
        denied = router.edge_traversal_decision(private, "COMMUTER")
        self.assertFalse(denied.allowed)
        self.assertIn("access=private", denied.reason)

        motorcycle_only = _edge(
            2,
            100.0,
            2,
            highway="service",
            motor_vehicle="no",
            motorcycle="yes",
        )
        self.assertTrue(
            router.edge_traversal_decision(
                motorcycle_only, "MOTORCYCLE",
            ).allowed
        )
        self.assertFalse(
            router.edge_traversal_decision(motorcycle_only, "COMMUTER").allowed
        )

    def test_destination_access_requires_endpoint_context(self):
        local_access = _edge(
            2,
            100.0,
            20,
            highway="residential",
            motor_vehicle="destination",
        )
        self.assertFalse(
            router.edge_traversal_decision(local_access, "COMMUTER").allowed
        )
        self.assertTrue(router.edge_traversal_decision(
            local_access,
            "COMMUTER",
            allow_destination_access=True,
        ).allowed)
        private = _edge(
            2, 100.0, 21, highway="residential", motor_vehicle="private",
        )
        self.assertFalse(router.edge_traversal_decision(
            private,
            "COMMUTER",
            allow_destination_access=True,
        ).allowed)

    def test_narrow_lane_accepts_motorcycle_but_not_passenger_car(self):
        lane = _edge(
            2, 80.0, 3, highway="service", service="alley", width_m=1.4,
        )
        self.assertTrue(
            router.edge_traversal_decision(lane, "MOTORCYCLE").allowed
        )
        car = router.edge_traversal_decision(lane, "COMMUTER")
        self.assertFalse(car.allowed)
        self.assertIn("vehicle width", car.reason)

    def test_truck_height_and_weight_clearances_are_enforced(self):
        restricted = _edge(
            2,
            100.0,
            4,
            highway="primary",
            maxheight_m=3.8,
            maxweight_t=18.0,
        )
        truck = router.edge_traversal_decision(restricted, "CARGO_TRUCK")
        self.assertFalse(truck.allowed)
        self.assertIn("maxheight", truck.reason)
        self.assertTrue(
            router.edge_traversal_decision(restricted, "COMMUTER").allowed
        )

        weight_only = _edge(
            2, 100.0, 5, highway="primary", maxweight_t=18.0,
        )
        heavy = router.edge_traversal_decision(weight_only, "CARGO_TRUCK")
        self.assertFalse(heavy.allowed)
        self.assertIn("maxweight", heavy.reason)

    def test_unrated_quality_fallback_is_nonzero_and_profile_audited(self):
        unknown = _edge(2, 100.0, 6, highway="mystery_road")
        car_profile = router.vehicle_profile("COMMUTER")
        score, source = router._edge_road_quality_score(unknown, car_profile)
        policy = router.vehicle_routing_policy(car_profile)
        self.assertEqual(score, policy.unrated_road_quality)
        self.assertEqual(source, "vehicle_profile_fallback")
        self.assertGreater(score, 0.0)

        poor = _edge(2, 100.0, 7, highway="residential", surface="mud")
        components = router._edge_cost_components(1, poor, car_profile)
        self.assertGreater(components["road_quality_penalty_s"], 0.0)
        self.assertGreaterEqual(
            components["generalized_cost_s"],
            components["expected_objective_cost_s"],
        )

    def test_legacy_four_field_road_metadata_is_compatible(self):
        edge = router._road_edge_from_metadata(
            2,
            125.0,
            9,
            ["Jalan Test", "R1", "secondary", None],
            ("name", "ref", "highway", "junction"),
        )
        self.assertEqual(edge.name, "Jalan Test")
        self.assertEqual(edge.highway, "secondary")
        self.assertIsNone(edge.surface)
        self.assertIsNone(edge.width_m)
        self.assertTrue(
            router.edge_traversal_decision(edge, "COMMUTER").allowed
        )

    def test_osm_number_parser_supports_common_units(self):
        self.assertAlmostEqual(
            router._optional_osm_number("10 ft", quantity="height"),
            3.048,
        )
        self.assertAlmostEqual(
            router._optional_osm_number("3500 kg", quantity="weight"),
            3.5,
        )
        self.assertIsNone(
            router._optional_osm_number("default", quantity="height")
        )

    def test_astar_filters_restricted_edges_without_changing_distance(self):
        nodes = {
            1: (0.0, 0.0),
            2: (0.0, 0.001),
            3: (0.001, 0.001),
            4: (0.0, 0.002),
        }
        adjacency = {
            1: [
                _edge(
                    2, 120.0, 1, highway="service", width_m=1.4,
                    road_quality=0.95,
                ),
                _edge(3, 500.0, 2, highway="primary"),
            ],
            2: [_edge(
                4, 120.0, 1, highway="service", width_m=1.4,
                road_quality=0.95,
            )],
            3: [_edge(4, 500.0, 2, highway="primary")],
            4: [],
        }
        with patch.multiple(
            router,
            NODES=nodes,
            ROAD_ADJ=adjacency,
            ADJ={
                source: [(edge.target, edge.distance_m) for edge in edges]
                for source, edges in adjacency.items()
            },
            NODE_META={},
        ):
            motorcycle = router.astar_detailed(
                1, 4, vehicle_type="MOTORCYCLE",
            )
            car = router.astar_detailed(1, 4, vehicle_type="COMMUTER")
        self.assertIsNotNone(motorcycle)
        self.assertIsNotNone(car)
        self.assertEqual(motorcycle.nodes, [1, 2, 4])
        self.assertEqual(car.nodes, [1, 3, 4])
        self.assertEqual(motorcycle.distance_m, 240.0)
        self.assertEqual(car.distance_m, 1000.0)

    def test_uncertainty_penalty_is_separate_nonnegative_and_risk_weighted(self):
        edge = _edge(2, 200.0, 8, highway="secondary", road_quality=0.8)
        estimate = {
            (1, 2, 8): router.EdgeCongestionEstimate(
                expected_speed_ratio=0.65,
                speed_ratio_std=0.12,
                p90_delay_s=30.0,
                source="historical-hour-of-week",
            ),
        }
        car = router._edge_cost_components(
            1,
            edge,
            router.vehicle_profile("COMMUTER"),
            congestion_estimates=estimate,
        )
        truck = router._edge_cost_components(
            1,
            edge,
            router.vehicle_profile("CARGO_TRUCK"),
            congestion_estimates=estimate,
        )
        self.assertGreater(car["expected_objective_cost_s"], 0.0)
        self.assertGreater(car["uncertainty_penalty_s"], 0.0)
        self.assertGreater(truck["uncertainty_penalty_s"], car["uncertainty_penalty_s"])

        no_uncertainty = router._edge_cost_components(
            1,
            edge,
            router.vehicle_profile("COMMUTER"),
            congestion_estimates={
                (1, 2, 8): router.EdgeCongestionEstimate(
                    expected_speed_ratio=0.65,
                ),
            },
        )
        self.assertEqual(no_uncertainty["uncertainty_penalty_s"], 0.0)

    def test_empirical_p90_ratio_drives_exact_downside_cost(self):
        edge = _edge(2, 200.0, 9, highway="secondary", road_quality=1.0)
        components = router._edge_cost_components(
            1,
            edge,
            router.vehicle_profile("COMMUTER"),
            congestion_estimates={
                (1, 2, 9): router.EdgeCongestionEstimate(
                    expected_speed_ratio=0.8,
                    speed_ratio_std=0.01,
                    p90_speed_ratio=0.5,
                    source="calibrated_tree_quantile",
                ),
            },
        )
        expected_spread = components["free_flow_s"] * (1.0 / 0.5 - 1.0 / 0.8)
        self.assertAlmostEqual(
            components["congestion_uncertainty_s"],
            expected_spread,
            places=9,
        )
        policy = router.vehicle_routing_policy("COMMUTER")
        self.assertAlmostEqual(
            components["uncertainty_penalty_s"],
            expected_spread
            * policy.cost_weights.congestion
            * policy.risk_aversion,
            places=9,
        )

    def test_default_congestion_estimate_applies_without_edge_expansion(self):
        edge = _edge(2, 200.0, 8, highway="secondary", road_quality=1.0)
        path = router.PathResult([1, 2], [edge], 200.0, 0.0)
        profile = router.vehicle_profile("COMMUTER")
        corridor_default = router.EdgeCongestionEstimate(
            expected_speed_ratio=0.7,
            speed_ratio_std=0.1,
            source="corridor-hour-of-week",
        )
        default_cost = router.path_cost_breakdown(
            path,
            profile,
            default_congestion_estimate=corridor_default,
        )
        clear_cost = router.path_cost_breakdown(path, profile)
        self.assertGreater(
            default_cost["congestion_delay_s"], clear_cost["congestion_delay_s"],
        )
        self.assertGreater(default_cost["uncertainty_penalty_s"], 0.0)

        exact_clear = router.path_cost_breakdown(
            path,
            profile,
            congestion_estimates={
                (1, 2, 8): router.EdgeCongestionEstimate(
                    expected_speed_ratio=1.0,
                    source="realtime-edge",
                ),
            },
            default_congestion_estimate=corridor_default,
        )
        self.assertEqual(exact_clear["congestion_delay_s"], 0.0)
        self.assertEqual(exact_clear["uncertainty_penalty_s"], 0.0)

    def test_balanced_heuristic_never_exceeds_direct_edge_cost(self):
        profile = router.vehicle_profile("COMMUTER")
        policy = router.vehicle_routing_policy(profile)
        edge = _edge(2, 100.0, 10, highway="primary", road_quality=1.0)
        components = router._edge_cost_components(1, edge, profile)
        cost = router._edge_objective_cost_s(
            edge,
            components,
            profile,
            router.route_preference("BALANCED"),
        )
        heuristic = (
            edge.distance_m / profile.max_speed_kph * 3.6
            * (policy.cost_weights.time + policy.cost_weights.distance)
        )
        self.assertLessEqual(heuristic, cost + 1e-9)
        self.assertTrue(math.isfinite(cost))

    def test_extended_metadata_caps_speed_and_audits_lanes(self):
        edge = router._road_edge_from_metadata(
            2, 100.0, 11,
            [None, None, "primary", None, "30 mph", "2"],
            ("name", "ref", "highway", "junction", "maxspeed", "lanes"),
        )
        self.assertAlmostEqual(edge.maxspeed_kph, 48.28032)
        self.assertEqual(edge.lanes, 2.0)
        self.assertAlmostEqual(
            router._edge_speed_kph(edge, router.vehicle_profile("COMMUTER")),
            48.28032,
        )
        audit = router._path_sourced_capacity_audit(
            router.PathResult([1, 2], [edge], 100.0, 0.0),
        )
        self.assertEqual(audit["lane_tagged_edge_count"], 1)
        self.assertIsNone(audit["capacity_inference"])

    def test_reviewed_overlay_recomputes_admissible_distance_and_geometry(self):
        nodes = {1: (1.0, 104.0), 2: (1.0, 104.005), 3: (1.0, 104.01)}
        adjacency = {
            1: [_edge(2, 2_000.0, 1, highway="primary")],
            2: [_edge(3, 2_000.0, 2, highway="primary")],
            3: [],
        }
        approved = ApprovedGraphOverride(
            override_id="shortcut-" + "1" * 32,
            graph_revision="a" * 64,
            source_node=1,
            target_node=3,
            geometry=((1.0, 104.0005), (1.0002, 104.0095)),
            applicable_vehicle_modes=("PASSENGER_CAR",),
            road_quality=0.8,
            distance_m=1_010.0,
            duration_s=90.0,
            approved_by="reviewer",
            approved_at=datetime.now(timezone.utc),
            candidate_sha256="b" * 64,
        )
        snapshot = ApprovedGraphOverrideSnapshot(
            "a" * 64, 1, (approved,),
        )
        with patch.multiple(
            router, NODES=nodes, ROAD_ADJ=adjacency,
            ADJ={source: [(edge.target, edge.distance_m) for edge in edges]
                 for source, edges in adjacency.items()},
            NODE_META={}, GRAPH_REVISION="a" * 64,
        ):
            result = router.astar_detailed(
                1, 3, vehicle_type="COMMUTER",
                approved_override_snapshot=snapshot,
            )
            self.assertIsNotNone(result)
            self.assertGreaterEqual(
                result.distance_m, router.haversine_m(nodes[1], nodes[3]),
            )
            payload = router._path_payload(
                result, 1, 3, "Origin", "Destination",
                vehicle_type="COMMUTER",
            )
        self.assertEqual(
            payload["approved_graph_overrides_used"][0]["override_id"],
            approved.override_id,
        )
        self.assertIn([1.0002, 104.0095], payload["geometry"])

    def test_forged_durable_override_endpoints_are_rejected(self):
        nodes = {1: (1.0, 104.0), 2: (1.0, 104.01)}
        approved = ApprovedGraphOverride(
            override_id="shortcut-" + "2" * 32,
            graph_revision="c" * 64,
            source_node=1, target_node=2,
            geometry=((35.0, -120.0), (35.001, -120.001)),
            applicable_vehicle_modes=("PASSENGER_CAR",), road_quality=0.8,
            distance_m=160.0, duration_s=20.0,
            approved_by="reviewer", approved_at=datetime.now(timezone.utc),
            candidate_sha256="d" * 64,
        )
        snapshot = ApprovedGraphOverrideSnapshot("c" * 64, 1, (approved,))
        with patch.multiple(
            router, NODES=nodes, ROAD_ADJ={1: (), 2: ()},
            ADJ={1: (), 2: ()}, NODE_META={}, GRAPH_REVISION="c" * 64,
        ):
            with self.assertRaisesRegex(ValueError, "not bound"):
                router.astar_detailed(
                    1, 2, vehicle_type="COMMUTER",
                    approved_override_snapshot=snapshot,
                )

    def test_mode_specific_snap_core_does_not_cross_motorcycle_bridge(self):
        nodes = {
            1: (1.0, 104.0), 2: (1.0, 104.001),
            3: (1.0, 104.01), 4: (1.0, 104.011),
        }
        car = dict(highway="residential")
        motorcycle = dict(
            highway="service", motorcar="no", motorcycle="yes",
        )
        adjacency = {
            1: [_edge(2, 120.0, 1, **car)],
            2: [_edge(1, 120.0, 1, **car), _edge(3, 1_000.0, 2, **motorcycle)],
            3: [_edge(4, 120.0, 3, **car), _edge(2, 1_000.0, 2, **motorcycle)],
            4: [_edge(3, 120.0, 3, **car)],
        }
        with patch.multiple(
            router, NODES=nodes, ROAD_ADJ=adjacency,
            ADJ={source: [(edge.target, edge.distance_m) for edge in edges]
                 for source, edges in adjacency.items()},
            NODE_META={}, LANDMARKS={"batam_centre": 1},
        ):
            router._main_routing_core.cache_clear()
            car_core = router._main_routing_core("COMMUTER")
            motorcycle_core = router._main_routing_core("MOTORCYCLE")
            router._main_routing_core.cache_clear()
        self.assertEqual(car_core, frozenset({1, 2}))
        self.assertEqual(motorcycle_core, frozenset(nodes))


class GraphBuilderVehicleMetadataTests(unittest.TestCase):
    @staticmethod
    def _way(way_id: int, highway: str, **tags):
        return {
            "type": "way",
            "id": way_id,
            "nodes": [1, 2],
            "tags": {"highway": highway, **tags},
        }

    def test_builder_quarantines_tracks_and_private_service_subtypes(self):
        elements = [
            {"type": "node", "id": 1, "lat": 1.0, "lon": 104.0},
            {"type": "node", "id": 2, "lat": 1.0, "lon": 104.001},
            self._way(10, "track"),
            self._way(11, "track", motorcycle="yes"),
            self._way(12, "service", service="driveway"),
            self._way(
                13, "service", service="driveway", motor_vehicle="permissive",
            ),
            self._way(14, "service", service="alley"),
        ]
        _coords, adjacency, metadata, _node_meta = build_graph.build_adjacency(
            {"elements": elements},
        )
        included_way_ids = {
            way_id
            for edges in adjacency.values()
            for _target, _distance, way_id in edges
        }
        self.assertNotIn(10, included_way_ids)
        self.assertIn(11, included_way_ids)
        self.assertNotIn(12, included_way_ids)
        self.assertIn(13, included_way_ids)
        self.assertIn(14, included_way_ids)
        self.assertEqual(metadata[11]["motorcycle"], "yes")
        self.assertEqual(metadata[13]["service"], "driveway")

    def test_builder_retains_motorcycle_only_way_for_runtime_filtering(self):
        elements = [
            {"type": "node", "id": 1, "lat": 1.0, "lon": 104.0},
            {"type": "node", "id": 2, "lat": 1.0, "lon": 104.001},
            self._way(
                30,
                "residential",
                motor_vehicle="yes",
                motorcar="no",
                motorcycle="yes",
            ),
        ]
        _coords, adjacency, metadata, _node_meta = build_graph.build_adjacency(
            {"elements": elements},
        )
        self.assertIn(30, metadata)
        self.assertTrue(any(
            way_id == 30
            for edges in adjacency.values()
            for _target, _distance, way_id in edges
        ))
        self.assertEqual(metadata[30]["motorcar"], "no")
        self.assertEqual(metadata[30]["motorcycle"], "yes")

    def test_compact_metadata_preserves_vehicle_constraints(self):
        element = self._way(
            20,
            "service",
            service="alley",
            motorcar="no",
            motorcycle="yes",
            surface="compacted",
            smoothness="bad",
            width="1.5",
            maxweight="3.5",
            maxheight="2.2",
            maxspeed="30 mph",
            lanes="2",
        )
        metadata = build_graph._compact_way_metadata(element, direction=0)
        for field in (
            "motorcar", "motorcycle", "surface", "smoothness", "width",
            "maxweight", "maxheight", "service", "maxspeed", "lanes",
        ):
            self.assertIn(field, build_graph.ROAD_FIELDS)
            self.assertIn(field, metadata)


class CommittedGraphVehicleIntegrationTests(unittest.TestCase):
    def test_public_core_endpoints_never_unlock_destination_only_shortcuts(self):
        source, target = 6130845457, 13038555596
        core = router._main_routing_core("COMMUTER")
        self.assertIn(source, core)
        self.assertIn(target, core)
        route = router.astar_detailed(
            source,
            target,
            vehicle_type="COMMUTER",
            route_preference="FASTEST",
        )
        self.assertIsNotNone(route)
        self.assertTrue(all(
            router.edge_traversal_decision(edge, "COMMUTER").allowed
            for edge in route.edges
        ))

    def test_seeded_corridors_route_for_every_vehicle_profile(self):
        pairs = (
            ("mukakuning", "batam_centre"),
            ("batu_ampar", "batam_centre"),
            ("hang_nadim", "nagoya"),
            ("sekupang", "mukakuning"),
            ("nongsa", "batam_centre"),
        )
        for vehicle in router.VEHICLE_PROFILES:
            for origin, destination in pairs:
                with self.subTest(
                    vehicle=vehicle,
                    origin=origin,
                    destination=destination,
                ):
                    result = router.astar_detailed(
                        router.LANDMARKS[origin],
                        router.LANDMARKS[destination],
                        vehicle_type=vehicle,
                    )
                    self.assertIsNotNone(result)

    def test_real_vehicle_cores_reflect_committed_access_metadata(self):
        motorcycle = router._main_routing_core("MOTORCYCLE")
        passenger_car = router._main_routing_core("COMMUTER")
        freight = router._main_routing_core("CARGO_TRUCK")
        self.assertNotEqual(motorcycle, passenger_car)
        self.assertNotEqual(passenger_car, freight)
        self.assertGreater(len(motorcycle), len(passenger_car))
        self.assertGreater(len(passenger_car), len(freight))


if __name__ == "__main__":
    unittest.main()
