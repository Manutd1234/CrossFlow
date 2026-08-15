"""Focused tests for strict service, Supabase and routing facade boundaries."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
BACKEND_DIR = os.path.join(PROJECT_DIR, "backend")
for path in (BACKEND_DIR, PROJECT_DIR):
    if path not in sys.path:
        sys.path.append(path)

from services import multimodal_router, routing_intelligence_store, supabase_server  # noqa: E402
from services import shortcut_ingestion  # noqa: E402
from services.routing_intelligence_store import (  # noqa: E402
    SupabaseRoutingIntelligenceStore,
)
from services.service_contracts import (  # noqa: E402
    ApprovedGraphOverrideSnapshot,
    BoundedTTLCache,
    CorridorRouteRequest,
    RoutingServiceFacade,
)
from services.traffic_observations import SpatialTrafficObservation  # noqa: E402


def _valid_candidate():
    graph = shortcut_ingestion.GraphIndex.from_graph_payload(
        {
            "meta": {"bbox": [1.0, 103.9, 1.3, 104.3]},
            "nodes": {
                "101": [1.100, 104.000],
                "102": [1.101, 104.001],
            },
        },
        graph_revision="a" * 64,
    )
    registration = shortcut_ingestion.SourceRegistration(
        source_id="reviewed_blog",
        allowed_hosts=("tips.example.org",),
        pinned_urls=("https://tips.example.org/batam.json",),
        confidence_ceiling=0.65,
    )
    document = shortcut_ingestion.SourceDocument(
        source_id="reviewed_blog",
        source_url="https://tips.example.org/batam.json",
        content_type="application/json",
        content=(
            '{"schema_version":1,"tips":[{'
            '"tip_id":"tip-1",'
            '"start":{"lat":1.100,"lng":104.000},'
            '"end":{"lat":1.101,"lng":104.001},'
            '"vehicle_modes":["motorcycle"],'
            '"confidence":0.6,"claimed_distance_m":160,'
            '"claimed_duration_minutes":1.0,'
            '"snippet":"Reviewed possible local road connector."}]}'
        ),
        document_id="doc-1",
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    result = shortcut_ingestion.ShortcutTipPipeline(
        shortcut_ingestion.SourcePolicy(registrations=(registration,)),
        graph,
    ).ingest((document,))
    if len(result.records) != 1:
        raise AssertionError(result.to_dict())
    return result.records[0]


class _FakeResponse:
    status = 200

    def __init__(self, payload: bytes = b"[]") -> None:
        self._payload = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        self._payload.close()


class SupabaseServerTransportTests(unittest.TestCase):
    def test_config_accepts_only_server_credentials_and_official_origin(self):
        base = {
            "SUPABASE_URL": "https://project-ref.supabase.co",
            "NEXT_PUBLIC_SUPABASE_ANON_KEY": "public",
            "SUPABASE_ANON_KEY": "anon",
        }
        with patch.dict(os.environ, base, clear=True):
            self.assertIsNone(supabase_server.load_server_config())
        with patch.dict(
            os.environ,
            {**base, "SUPABASE_SECRET_KEY": "sb_secret_backend-only"},
            clear=True,
        ):
            config = supabase_server.load_server_config()
        self.assertIsNotNone(config)
        self.assertEqual(config.origin, "https://project-ref.supabase.co")
        with self.assertRaises(supabase_server.SupabaseServerError):
            supabase_server.SupabaseServerConfig(
                "https://project-ref.supabase.co.evil.example", "secret",
            )

    def test_modern_secret_is_not_sent_as_bearer_and_redirects_are_disabled(self):
        modern = supabase_server.privileged_headers("sb_secret_backend")
        legacy = supabase_server.privileged_headers("legacy.jwt")
        self.assertNotIn("Authorization", modern)
        self.assertEqual(legacy["Authorization"], "Bearer legacy.jwt")
        handler = supabase_server.NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://evil"))

    def test_request_rejects_path_query_injection_and_builds_bounded_request(self):
        config = supabase_server.SupabaseServerConfig(
            "https://project-ref.supabase.co", "sb_secret_backend",
        )
        for path in (
            "//evil/rest/v1/table", "/rest/v1//evil", "/rest/v1/../evil",
            "/rest/v1/table\\evil", "/rest/v1/table%0d%0aInjected",
        ):
            with self.assertRaises(supabase_server.SupabaseServerError):
                supabase_server.request_json(config, method="GET", rest_path=path)
        with self.assertRaises(supabase_server.SupabaseServerError):
            supabase_server.request_json(
                config, method="GET", rest_path="/rest/v1/table",
                query={"select": "x%0d%0aHeader"},
            )
        with patch.object(
            supabase_server.OPENER, "open", return_value=_FakeResponse(b'{"ok":true}'),
        ) as opened:
            result = supabase_server.request_json(
                config, method="POST", rest_path="/rest/v1/rpc/safe",
                payload={"value": 1}, timeout_seconds=3,
            )
        self.assertEqual(result, {"ok": True})
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://project-ref.supabase.co/rest/v1/rpc/safe")
        self.assertNotIn("Authorization", request.headers)

    def test_transport_rejects_non_finite_json_and_boolean_resource_limits(self):
        config = supabase_server.SupabaseServerConfig(
            "https://project-ref.supabase.co", "sb_secret_backend",
        )
        for kwargs in (
            {"payload": {"invalid": float("nan")}},
            {"timeout_seconds": True},
            {"max_response_bytes": True},
        ):
            with self.assertRaises(supabase_server.SupabaseServerError):
                supabase_server.request_json(
                    config, method="POST", rest_path="/rest/v1/rpc/safe",
                    **kwargs,
                )
        with patch.object(
            supabase_server.OPENER,
            "open",
            return_value=_FakeResponse(b'{"value":NaN}'),
        ):
            with self.assertRaisesRegex(
                supabase_server.SupabaseServerError, "invalid_json",
            ):
                supabase_server.request_json(
                    config, method="GET", rest_path="/rest/v1/table",
                )

    def test_absolute_timeout_closes_a_slow_response(self):
        class SlowResponse(_FakeResponse):
            def __init__(self):
                super().__init__(b'[]')
                self.closed = threading.Event()

            def read(self, size=-1):
                self.closed.wait(1.0)
                return b""

            def close(self):
                self.closed.set()

        config = supabase_server.SupabaseServerConfig(
            "https://project-ref.supabase.co", "sb_secret_backend",
        )
        response = SlowResponse()
        started = time.monotonic()
        with patch.object(supabase_server.OPENER, "open", return_value=response):
            with self.assertRaisesRegex(
                supabase_server.SupabaseServerError, "timeout",
            ):
                supabase_server.request_json(
                    config, method="GET", rest_path="/rest/v1/table",
                    timeout_seconds=0.1,
                )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(response.closed.wait(0.2))


class DurableRoutingIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SupabaseRoutingIntelligenceStore(
            approved_cache_ttl_seconds=5.0,
        )
        self.config = supabase_server.SupabaseServerConfig(
            "https://project-ref.supabase.co", "sb_secret_backend",
        )

    def test_empty_candidate_batch_is_protocol_compatible_noop(self):
        with patch.object(
            routing_intelligence_store, "_config", side_effect=AssertionError,
        ):
            self.assertEqual(self.store.upsert_many(()), ())

    def test_candidate_batch_is_transport_bounded_and_ordinals_are_exact(self):
        candidate = _valid_candidate()
        with patch.object(
            routing_intelligence_store,
            "MAX_ATOMIC_CANDIDATE_BATCH_BYTES",
            1,
        ), patch.object(
            routing_intelligence_store,
            "_store_request",
            side_effect=AssertionError("oversize request must not run"),
        ):
            with self.assertRaisesRegex(ValueError, "atomic persistence"):
                self.store.upsert_many((candidate,))

        bad_response = [{
            "received": 1,
            "inserted_count": 1,
            "existing_count": 0,
            "first_queue_revision": 1,
            "last_queue_revision": 1,
            "results": [{
                "override_id": "shortcut-" + "f" * 32,
                "inserted": True,
                "queue_revision": 1,
                "review_state": "REVIEW_REQUIRED",
                "candidate_sha256": "a" * 64,
            }],
        }]
        with patch.object(
            routing_intelligence_store,
            "_store_request",
            return_value=bad_response,
        ):
            with self.assertRaisesRegex(
                routing_intelligence_store.RoutingIntelligenceStoreUnavailable,
                "invalid_candidate_response",
            ):
                self.store.upsert_many((candidate,))

    def test_candidate_summary_is_one_row_bounded_past_postgrest_row_cap(self):
        ids = tuple(
            f"shortcut-{value:032x}" for value in range(1, 2_001)
        )
        results = [{
            "override_id": override_id,
            "inserted": True,
            "queue_revision": index,
            "review_state": "REVIEW_REQUIRED",
            "candidate_sha256": "a" * 64,
        } for index, override_id in enumerate(ids, 1)]
        response = [{
            "received": len(ids),
            "inserted_count": len(ids),
            "existing_count": 0,
            "first_queue_revision": 1,
            "last_queue_revision": len(ids),
            "results": results,
        }]
        flags = routing_intelligence_store._validated_candidate_upsert_summary(
            response, ids,
        )
        self.assertEqual(len(flags), 2_000)
        self.assertTrue(all(flags))
        self.assertLess(
            len(json.dumps(response, separators=(",", ":")).encode("utf-8")),
            supabase_server.DEFAULT_MAX_RESPONSE_BYTES,
        )

    def test_candidate_retry_preserves_exact_false_flag_without_followup_reads(self):
        candidate = _valid_candidate()
        summary = [{
            "received": 1,
            "inserted_count": 0,
            "existing_count": 1,
            "first_queue_revision": 17,
            "last_queue_revision": 17,
            "results": [{
                "override_id": candidate.override_id,
                "inserted": False,
                "queue_revision": 17,
                "review_state": "APPROVED_ARCHIVED",
                "candidate_sha256": "b" * 64,
            }],
        }]
        with patch.object(
            routing_intelligence_store, "_store_request", return_value=summary,
        ) as request:
            result = self.store.upsert_many((candidate,))
        self.assertEqual(result, ((candidate, False),))
        self.assertEqual(request.call_count, 1)

    def test_maritime_tip_is_explicitly_routed_out_of_road_overlay_queue(self):
        with self.assertRaises(shortcut_ingestion.CandidateValidationError) as raised:
            shortcut_ingestion._normalise_modes(["ferry_maritime"])
        self.assertEqual(raised.exception.code, "UNSUPPORTED_MARITIME_OVERLAY")

    def test_spatial_training_read_is_paginated_and_revalidated(self):
        record = SpatialTrafficObservation.create(
            corridor_id="corridor-1",
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            validation_now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            latitude=1.1,
            longitude=104.0,
            actual_speed_kph=30,
            free_flow_speed_kph=60,
            source="probe_gps",
            road_class="primary",
        )
        payload = routing_intelligence_store._observation_payload(record)
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=routing_intelligence_store.SPATIAL_HISTORY_RETENTION_DAYS)
        ).isoformat()
        responses = [
            [{"snapshot_revision": 1, "cutoff_observed_at": cutoff}],
            [{
                "ingestion_revision": 1,
                "observation_key": record.observation_key,
                "observed_at": record.observed_at.isoformat(),
                "immutable_payload": payload,
            }],
            [],
        ]
        with patch.object(routing_intelligence_store, "_config", return_value=self.config), \
                patch.object(
                    supabase_server, "request_json", side_effect=responses,
                ) as request:
            records = self.store.get_spatial_training_dataset(
                limit=10, page_size=1,
            )
        self.assertEqual(records, (record,))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            request.call_args_list[1].kwargs["rest_path"],
            "/rest/v1/rpc/crossflow_read_spatial_training_page",
        )
        payload = request.call_args_list[1].kwargs["payload"]
        self.assertEqual(payload["p_snapshot_revision"], 1)
        self.assertIsNone(payload["p_before_observed_at"])

    def test_spatial_training_limit_selects_newest_then_returns_chronological(self):
        older = SpatialTrafficObservation.create(
            corridor_id="corridor-1",
            observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            validation_now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            latitude=1.1, longitude=104.0,
            actual_speed_kph=25, free_flow_speed_kph=60,
            source="probe_gps", road_class="primary",
        )
        newer = SpatialTrafficObservation.create(
            corridor_id="corridor-1",
            observed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            validation_now=datetime(2026, 1, 3, tzinfo=timezone.utc),
            latitude=1.1, longitude=104.0,
            actual_speed_kph=30, free_flow_speed_kph=60,
            source="probe_gps", road_class="primary",
        )
        responses = [
            [{"snapshot_revision": 3, "cutoff_observed_at": (
                datetime.now(timezone.utc)
                - timedelta(
                    days=routing_intelligence_store.SPATIAL_HISTORY_RETENTION_DAYS,
                )
            ).isoformat()}],
            [
                {"ingestion_revision": 2,
                 "observation_key": newer.observation_key,
                 "observed_at": newer.observed_at.isoformat(),
                 "immutable_payload":
                 routing_intelligence_store._observation_payload(newer)},
            ],
        ]
        with patch.object(
            routing_intelligence_store, "_store_request", side_effect=responses,
        ) as request:
            records = self.store.get_spatial_training_dataset(
                limit=1, page_size=1,
            )
        # Revision 3 is a late, older backfill. The observed-time ordering in
        # the DB RPC must still spend the bounded training slot on revision 2.
        self.assertEqual(records, (newer,))
        self.assertEqual(request.call_count, 2)

    def test_approved_snapshot_cache_is_revision_scoped(self):
        with patch.object(routing_intelligence_store, "_config", return_value=self.config), \
                patch.object(supabase_server, "request_json", return_value=[]) as request:
            first = self.store.approved_snapshot(graph_revision="a" * 64)
            second = self.store.approved_snapshot(graph_revision="a" * 64)
            third = self.store.approved_snapshot(graph_revision="b" * 64)
        self.assertIs(first, second)
        self.assertNotEqual(first.graph_revision, third.graph_revision)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.store.status()["approved_snapshot_cache"]["hits"], 1)

    def test_schema_is_service_role_only_and_defines_every_adapter_rpc(self):
        schema_path = os.path.join(BACKEND_DIR, "data", "routing_intelligence.sql")
        with open(schema_path, encoding="utf-8") as handle:
            sql = handle.read().casefold()
        self.assertIn("enable row level security", sql)
        self.assertIn("from public, anon, authenticated", sql)
        self.assertIn(
            "revoke all on table public.crossflow_approved_graph_overrides "
            "from service_role", sql,
        )
        self.assertIn("grant select on table public.crossflow_approved", sql)
        self.assertNotIn(
            "grant select, insert on table public.crossflow_approved", sql,
        )
        for rpc in (
            "crossflow_ingest_spatial_observations",
            "crossflow_upsert_shortcut_candidates",
            "crossflow_approve_shortcut_candidate",
            "crossflow_spatial_training_snapshot",
            "crossflow_read_spatial_training_page",
            "crossflow_routing_intelligence_health",
        ):
            self.assertIn(f"create or replace function public.{rpc}", sql)
            self.assertIn(f"grant execute on function public.{rpc}", sql)
        self.assertIn("approved graph override capacity exceeded", sql)
        self.assertIn("approved shortcut candidate identity conflict", sql)
        self.assertIn("results jsonb", sql)
        self.assertNotIn("returns table(ordinal integer", sql)
        self.assertIn("crossflow_spatial_training_keyset_idx", sql)
        self.assertIn("crossflow_shortcut_pending_revision_idx", sql)
        self.assertIn("where review_state = 'review_required'", sql)
        self.assertIn("security definer", sql)
        self.assertIn("set search_path = ''", sql)

    def test_status_does_not_claim_unprobed_durability_and_health_is_bounded(self):
        status = self.store.status()
        self.assertEqual(status["schema_health"], "unknown_not_probed")
        self.assertFalse(status["durable_spatial_history"])
        with patch.dict(
            os.environ,
            {routing_intelligence_store.SHARED_STORE_MODE_ENV: "supabase"},
        ), patch.object(
            routing_intelligence_store, "configured", return_value=True,
        ), patch.object(
            routing_intelligence_store,
            "_store_request",
            return_value=[{"schema_version": 1}],
        ) as request:
            health = self.store.health()
        self.assertTrue(health["verified_available"])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(self.store.status()["durable_spatial_history"])


class RoutingServiceFacadeTests(unittest.TestCase):
    def test_cache_identity_includes_override_and_dynamic_revisions(self):
        class Promoter:
            revision = 1

            def approved_snapshot(self, *, graph_revision):
                return ApprovedGraphOverrideSnapshot(
                    graph_revision, self.revision, (),
                )

        calls = []

        def local(**request):
            calls.append(request)
            return {"route": "ok"}

        promoter = Promoter()
        model_revision = {"model": 1}
        facade = RoutingServiceFacade(
            graph_revision="a" * 64,
            local_route=local,
            local_free_route=local,
            override_promoter=promoter,
            cache=BoundedTTLCache(max_entries=8),
            revision_identity=lambda: model_revision,
            cache_bucket_seconds=60,
        )
        request = CorridorRouteRequest("corridor-1", "COMMUTER")
        facade.plan_corridor(request)
        facade.plan_corridor(request)
        self.assertEqual(len(calls), 1)
        promoter.revision = 2
        facade.plan_corridor(request)
        model_revision["model"] = 2
        facade.plan_corridor(request)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["approved_override_snapshot"].override_revision, 1)

    def test_reserved_override_snapshot_cannot_be_injected(self):
        facade = RoutingServiceFacade(
            graph_revision="a" * 64,
            local_route=lambda **request: request,
            local_free_route=lambda **request: request,
            override_promoter=type("P", (), {"approved_snapshot": lambda self, **_: ApprovedGraphOverrideSnapshot("a" * 64, 0, ())})(),
            cache=BoundedTTLCache(max_entries=1),
        )
        with self.assertRaisesRegex(ValueError, "service-owned"):
            facade.optimize_route(
                corridor_id="corridor-1", vehicle_type="COMMUTER",
                approved_override_snapshot="forged",
            )


class MultimodalOverrideProvenanceTests(unittest.TestCase):
    @staticmethod
    def _road_leg(*, approved=False):
        leg = {
            "mode": "ROAD",
            "from_name": "Origin",
            "to_name": "Destination",
            "geometry": [[1.10, 104.00], [1.11, 104.01]],
            "distance_km": 1.0,
            "duration_mins": 3.0,
            "data_source": "batam_bundled_openstreetmap",
            "is_estimate": False,
            "navigation": {"maneuvers": [], "traffic_lights_count": 0},
        }
        if approved:
            leg["approved_graph_overrides_used"] = [{
                "override_id": "shortcut-reviewed-1",
                "candidate_sha256": "a" * 64,
                "source_node": 101,
                "target_node": 102,
                "vehicle_modes": ["motorcycle"],
            }]
        return leg

    def test_selected_batam_road_leg_retains_exact_reviewed_identity(self):
        routed = {
            "geometry": [[1.10, 104.00], [1.11, 104.01]],
            "distance_km": 1.0,
            "modeled_travel_time_mins": 3.0,
            "navigation": {"maneuvers": []},
            "approved_graph_overrides_used": [{
                "override_id": "shortcut-reviewed-1",
                "candidate_sha256": "a" * 64,
                "source_node": 101,
                "target_node": 102,
                "vehicle_modes": ["motorcycle"],
            }],
        }
        with patch.object(
            multimodal_router.router,
            "snap_to_graph",
            side_effect=((101, 1.0), (102, 1.0)),
        ), patch.object(
            multimodal_router.router,
            "route_between_nodes",
            return_value=routed,
        ):
            leg = multimodal_router._road_leg(
                "BATAM",
                (1.10, 104.00),
                (1.11, 104.01),
                "Origin",
                "Destination",
                "MOTORCYCLE",
                "BALANCED",
            )
        self.assertEqual(
            leg["approved_graph_overrides_used"],
            routed["approved_graph_overrides_used"],
        )

    def test_same_region_and_ferry_access_legs_aggregate_used_overrides(self):
        reviewed_leg = self._road_leg(approved=True)
        plain_leg = self._road_leg()
        now = datetime(2026, 8, 13, 7, tzinfo=timezone.utc)

        with patch.object(
            multimodal_router,
            "_road_leg",
            return_value=reviewed_leg,
        ):
            local = multimodal_router.optimize_journey(
                1.10, 104.00, 1.11, 104.01,
                "MOTORCYCLE", 14, 0, now,
            )
        self.assertEqual(
            local["approved_graph_overrides_used"][0]["override_id"],
            "shortcut-reviewed-1",
        )

        pair = multimodal_router.TerminalPair(
            "HarbourFront SG", (1.2644, 103.8206),
            "Batam Centre", (1.1318, 104.0554),
            60,
            ((1.2644, 103.8206), (1.1318, 104.0554)),
        )
        ferry_leg = {
            "mode": "FERRY",
            "from_name": "Batam Centre",
            "to_name": "HarbourFront SG",
            "geometry": [[1.1318, 104.0554], [1.2644, 103.8206]],
            "distance_km": 30.0,
            "duration_mins": 60,
            "limitations": "fixture",
        }
        with patch.object(
            multimodal_router,
            "_choose_terminal_pair",
            return_value=pair,
        ), patch.object(
            multimodal_router,
            "_road_leg",
            side_effect=(reviewed_leg, plain_leg),
        ), patch.object(
            multimodal_router,
            "_ferry_leg",
            return_value=(ferry_leg, [], 0.0),
        ):
            cross_border = multimodal_router.optimize_journey(
                1.10, 104.00, 1.30, 103.90,
                "MOTORCYCLE", 14, 0, now,
            )
        self.assertEqual(
            cross_border["approved_graph_overrides_used"],
            reviewed_leg["approved_graph_overrides_used"],
        )


if __name__ == "__main__":
    unittest.main()
