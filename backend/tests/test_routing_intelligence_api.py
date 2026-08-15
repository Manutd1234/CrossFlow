"""API-boundary tests for server-owned routing-intelligence trust."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError


PROJECT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

import main as api_main  # noqa: E402
from services import routing_intelligence_store, shortcut_ingestion  # noqa: E402


WIB = timezone(timedelta(hours=7))
NOW = datetime(2026, 8, 13, 10, 0, tzinfo=WIB)


def observation_input() -> dict[str, object]:
    return {
        "corridor_id": "corridor-1",
        "observed_at": NOW - timedelta(minutes=1),
        "latitude": 1.1,
        "longitude": 104.0,
        "actual_speed_kph": 25.0,
        "free_flow_speed_kph": 50.0,
        "road_class": "primary",
        "upstream_event_id": "sensor-event-1",
    }


class RoutingIntelligenceApiTrustTests(unittest.TestCase):
    def test_observation_body_cannot_self_assign_verified_source(self) -> None:
        with self.assertRaises(ValidationError):
            api_main.SpatialTrafficObservationInput(
                **observation_input(), source="tomtom_live",
            )

    def test_server_config_owns_observation_provenance(self) -> None:
        item = api_main.SpatialTrafficObservationInput(**observation_input())
        with patch.dict(
            os.environ,
            {"CROSSFLOW_TRAFFIC_INGEST_SOURCE": "probe_gps"},
            clear=False,
        ):
            source = api_main._configured_traffic_ingest_source()
        record = api_main._canonical_spatial_observation(item, NOW, source)
        self.assertEqual(record.source, "probe_gps")
        self.assertEqual(record.provenance, "verified_gps")
        self.assertEqual(record.local_timezone_offset_minutes, 420)

    def test_unconfigured_observation_source_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                api_main._configured_traffic_ingest_source()
        self.assertEqual(raised.exception.status_code, 503)

    def test_supabase_credentials_do_not_implicitly_enable_optional_schema(self) -> None:
        env = {
            "SUPABASE_URL": "https://project-ref.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_backend",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(routing_intelligence_store.configured())
            self.assertFalse(routing_intelligence_store.shared_store_enabled())
            self.assertFalse(routing_intelligence_store.shared_store_ready())
            snapshot, source = api_main._approved_override_snapshot_for_route()
        self.assertEqual(snapshot.overrides, ())
        self.assertEqual(source, "local_graph_no_shared_override_store")

    def test_shortcut_fetch_has_a_total_pinned_url_cap(self) -> None:
        urls = tuple(
            f"https://tips.example.org/tip-{index}.json"
            for index in range(api_main.MAX_SHORTCUT_FETCH_URLS_PER_REQUEST + 1)
        )
        registration = shortcut_ingestion.SourceRegistration(
            source_id="audited",
            allowed_hosts=("tips.example.org",),
            pinned_urls=urls,
            allowed_content_types=("application/json",),
        )
        policy = shortcut_ingestion.SourcePolicy(registrations=(registration,))
        request = api_main.ShortcutFetchRequest(source_ids=["audited"])
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "token"}), \
                patch.object(api_main, "_shortcut_pipeline", return_value=(policy, object())):
            with self.assertRaises(HTTPException) as raised:
                api_main.api_fetch_shortcut_sources(
                    request, Response(), "token",
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_approval_body_cannot_spoof_reviewer_identity(self) -> None:
        request = api_main.ShortcutApprovalRequest(override_id="shortcut-abc")
        self.assertEqual(request.override_id, "shortcut-abc")
        with self.assertRaises(ValidationError):
            api_main.ShortcutApprovalRequest(
                override_id="shortcut-abc", approved_by="spoofed",
            )
        with patch.dict(
            os.environ,
            {"CROSSFLOW_SHORTCUT_REVIEWER_ID": "ops-reviewer-1"},
            clear=False,
        ):
            self.assertEqual(
                api_main._configured_shortcut_reviewer(), "ops-reviewer-1",
            )

    def test_retrain_uses_explicit_spatial_snapshot_port(self) -> None:
        metrics = {"is_trained": True, "total_samples": 123}
        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "token"}), \
                patch.object(
                    routing_intelligence_store,
                    "shared_store_ready",
                    return_value=False,
                ), patch.object(
                    api_main.historical_store,
                    "get_spatial_training_dataset",
                    return_value=[],
                ) as read_history, patch.object(
                    api_main.forecaster,
                    "retrain_from_observations",
                    return_value=metrics,
                ) as retrain:
            result = api_main.api_retrain_model("token")
        read_history.assert_called_once_with(now=ANY)
        retrain.assert_called_once_with([])
        self.assertEqual(result["training_snapshot"]["durability"], "local_sqlite")

    def test_durable_shortcut_conflict_maps_to_409(self) -> None:
        request = api_main.ShortcutDocumentBatch(documents=[{
            "source_id": "audited",
            "pinned_url_index": 0,
            "content_type": "application/json",
            "content": "{}",
            "document_id": "doc-1",
            "retrieved_at": NOW,
        }])
        registration = shortcut_ingestion.SourceRegistration(
            source_id="audited",
            allowed_hosts=("tips.example.org",),
            pinned_urls=("https://tips.example.org/tips.json",),
            allowed_content_types=("application/json",),
        )

        class ConflictPipeline:
            def ingest(self, _documents):
                raise routing_intelligence_store.RoutingIntelligenceStoreConflict()

        with patch.dict(os.environ, {"CROSSFLOW_ADMIN_TOKEN": "token"}), \
                patch.object(
                    api_main,
                    "_shortcut_pipeline",
                    return_value=(
                        shortcut_ingestion.SourcePolicy(
                            registrations=(registration,),
                        ),
                        ConflictPipeline(),
                    ),
                ):
            with self.assertRaises(HTTPException) as raised:
                api_main.api_ingest_shortcut_documents(
                    request, Response(), "token",
                )
        self.assertEqual(raised.exception.status_code, 409)

    def test_status_reports_all_vehicle_modes_without_policy_profile_mixup(self) -> None:
        with patch.dict(
            os.environ,
            {"CROSSFLOW_ADMIN_TOKEN": "token"},
            clear=False,
        ), patch.object(
            api_main,
            "_shortcut_runtime",
            return_value=(shortcut_ingestion.SourcePolicy(), object()),
        ):
            result = api_main.api_routing_intelligence_status(
                Response(), "token",
            )
        counts = result["routing_intelligence"]["vehicle_core_node_counts"]
        self.assertEqual(
            set(counts), {"MOTORCYCLE", "PASSENGER_CAR", "FREIGHT_TRUCK"},
        )
        self.assertTrue(all(value > 0 for value in counts.values()))

    def test_route_audit_names_only_reviewed_overrides_used_by_selected_path(self):
        result = {
            "approved_graph_overrides_used": [{
                "override_id": "shortcut-reviewed-1",
                "candidate_sha256": "a" * 64,
                "source_node": 101,
                "target_node": 102,
            }],
        }
        snapshot = api_main.ApprovedGraphOverrideSnapshot(
            graph_revision=api_main.router.GRAPH_REVISION,
            override_revision=7,
            overrides=(),
        )
        api_main._routing_intelligence_audit(
            result,
            snapshot,
            "shared_reviewed_override_snapshot",
        )
        audit = result["service_audit"]
        self.assertEqual(audit["approved_override_used_count"], 1)
        self.assertEqual(audit["approved_graph_overrides_used"], [{
            "override_id": "shortcut-reviewed-1",
            "candidate_sha256": "a" * 64,
        }])


if __name__ == "__main__":
    unittest.main()
