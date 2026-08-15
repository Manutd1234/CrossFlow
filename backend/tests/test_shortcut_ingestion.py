"""Security and contract tests for quarantined shortcut-tip ingestion.

Run with:
    python -m unittest backend.tests.test_shortcut_ingestion
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from services import shortcut_ingestion as shortcut_module  # noqa: E402
from services.shortcut_ingestion import (  # noqa: E402
    AllowlistedSourceFetcher,
    FetchResponse,
    FetchSafetyError,
    GraphIndex,
    InMemoryShortcutReviewQueue,
    SourceContractError,
    SourceDocument,
    SourcePolicy,
    SourceRegistration,
    ShortcutTipPipeline,
    haversine_m,
    source_policy_from_environment,
)


# Intentionally historical so tests never depend on the machine clock.
NOW = datetime(2020, 8, 13, 4, 30, tzinfo=timezone.utc)


def graph_index() -> GraphIndex:
    return GraphIndex.from_graph_payload(
        {
            "meta": {"bbox": [1.0, 103.9, 1.3, 104.3]},
            "nodes": {
                "101": [1.100, 104.000],
                "102": [1.101, 104.001],
                "103": [1.102, 104.002],
                "104": [1.105, 104.005],
            },
        },
        graph_revision="graph-revision-001",
    )


def registration(**changes) -> SourceRegistration:
    values = {
        "source_id": "sanitized_forum",
        "allowed_hosts": ("tips.example.org",),
        "pinned_urls": ("https://tips.example.org/routes/batam.json",),
        "confidence_ceiling": 0.65,
    }
    values.update(changes)
    return SourceRegistration(**values)


def policy(**changes) -> SourcePolicy:
    values = {"registrations": (registration(),)}
    values.update(changes)
    return SourcePolicy(**values)


def supplied_document(
    content: bytes | str,
    *,
    content_type: str = "application/json",
    document_id: str = "fixture-doc-1",
    source_url: str = "https://tips.example.org/routes/batam.json",
) -> SourceDocument:
    return SourceDocument(
        source_id="sanitized_forum",
        source_url=source_url,
        content_type=content_type,
        content=content,
        document_id=document_id,
        retrieved_at=NOW,
    )


def fixture_document() -> SourceDocument:
    return supplied_document(
        (FIXTURES / "shortcut_tips_sanitized.json").read_bytes(),
    )


def json_document(tips: list[dict], *, document_id: str = "fixture-json") -> SourceDocument:
    return supplied_document(
        json.dumps({"schema_version": 1, "tips": tips}),
        document_id=document_id,
    )


def base_tip(**changes) -> dict:
    value = {
        "tip_id": "tip-001",
        "start": {"lat": 1.100, "lng": 104.000},
        "end": {"lat": 1.101, "lng": 104.001},
        "vehicle_modes": ["motorcycle"],
        "confidence": 0.60,
        "claimed_distance_m": 160,
        "claimed_duration_minutes": 1.0,
        "snippet": "Sanitized report of a possible informal connector between two road nodes.",
    }
    value.update(changes)
    return value


class SourcePolicyTests(unittest.TestCase):
    def test_server_environment_policy_is_strict_and_disabled_by_default(self) -> None:
        self.assertEqual(source_policy_from_environment({}).registrations, ())
        configured = source_policy_from_environment({
            "CROSSFLOW_SHORTCUT_SOURCE_POLICY": json.dumps({
                "schema_version": 1,
                "sources": [{
                    "source_id": "audited_blog",
                    "pinned_urls": ["https://blog.example.org/audited/tips.json"],
                    "allowed_content_types": ["application/json"],
                    "confidence_ceiling": 0.55,
                }],
                "limits": {"max_tips_per_batch": 12},
            }),
        })
        self.assertEqual(configured.max_tips_per_batch, 12)
        self.assertEqual(configured.registrations[0].allowed_hosts, ("blog.example.org",))
        with self.assertRaisesRegex(SourceContractError, "malformed JSON"):
            source_policy_from_environment({"CROSSFLOW_SHORTCUT_SOURCE_POLICY": "{"})
        with self.assertRaisesRegex(SourceContractError, "unknown fields"):
            source_policy_from_environment({
                "CROSSFLOW_SHORTCUT_SOURCE_POLICY": json.dumps({
                    "schema_version": 1, "sources": [], "caller_urls": True,
                }),
            })
        with self.assertRaisesRegex(SourceContractError, "pinned_urls"):
            source_policy_from_environment({
                "CROSSFLOW_SHORTCUT_SOURCE_POLICY": json.dumps({
                    "schema_version": 1,
                    "sources": [{"source_id": "open_crawler", "pinned_urls": []}],
                }),
            })

    def test_source_contract_is_exact_https_and_server_owned(self) -> None:
        configured = policy()
        target = configured.validate_target(
            "sanitized_forum",
            "https://tips.example.org/routes/batam.json",
            require_pinned_url=True,
        )
        self.assertEqual(target.hostname, "tips.example.org")
        with self.assertRaisesRegex(SourceContractError, "HTTPS"):
            configured.validate_target(
                "sanitized_forum",
                "http://tips.example.org/routes/batam.json",
                require_pinned_url=False,
            )
        with self.assertRaisesRegex(SourceContractError, "hostname"):
            configured.validate_target(
                "sanitized_forum",
                "https://tips.example.org.attacker.test/routes/batam.json",
                require_pinned_url=False,
            )
        with self.assertRaisesRegex(SourceContractError, "credentials"):
            configured.validate_target(
                "sanitized_forum",
                "https://user@tips.example.org/routes/batam.json",
                require_pinned_url=False,
            )
        with self.assertRaisesRegex(SourceContractError, "port"):
            configured.validate_target(
                "sanitized_forum",
                "https://tips.example.org:8443/routes/batam.json",
                require_pinned_url=False,
            )
        for unsafe_url in (
            "https://tips.example.org/routes/batam.json\r\nX-Header: injected",
            "https://tips.example.org/routes\\batam.json",
            "https://tips.example.org/routes%2f..%2fadmin",
        ):
            with self.subTest(unsafe_url=unsafe_url):
                with self.assertRaisesRegex(SourceContractError, "unsafe"):
                    configured.validate_target(
                        "sanitized_forum",
                        unsafe_url,
                        require_pinned_url=False,
                    )

    def test_empty_allowlist_disables_all_fetching(self) -> None:
        fetcher = AllowlistedSourceFetcher(SourcePolicy())
        with self.assertRaisesRegex(SourceContractError, "not server-allowlisted"):
            fetcher.fetch("anything", "https://example.org/tips")

    def test_unpinned_path_cannot_be_fetched_even_on_allowed_host(self) -> None:
        called = False

        def transport(*_args):
            nonlocal called
            called = True
            raise AssertionError("transport must not run")

        fetcher = AllowlistedSourceFetcher(
            policy(),
            resolver=lambda _host, _port: ("8.8.8.8",),
            transport=transport,
        )
        with self.assertRaisesRegex(FetchSafetyError, "pinned"):
            fetcher.fetch(
                "sanitized_forum",
                "https://tips.example.org/routes/arbitrary.json",
            )
        self.assertFalse(called)

        # Supplied content cannot claim an unpinned provenance URL either when
        # that source has a server-pinned catalog.
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([
            supplied_document(
                json.dumps({"schema_version": 1, "tips": [base_tip()]}),
                source_url="https://tips.example.org/routes/arbitrary.json",
            ),
        ])
        self.assertEqual(result.accepted, 0)
        self.assertEqual(result.rejections[0].code, "SOURCE_CONTRACT")


class SafeFetcherTests(unittest.TestCase):
    def test_tls_handshake_failure_closes_the_raw_socket(self) -> None:
        class RawSocket:
            closed = False

            def close(self):
                self.closed = True

        raw = RawSocket()
        connection = shortcut_module._PinnedHTTPSConnection(
            "tips.example.org", "203.0.113.10", timeout=1.0,
        )
        with patch.object(
            shortcut_module.socket,
            "create_connection",
            return_value=raw,
        ), patch.object(
            connection._context,
            "wrap_socket",
            side_effect=shortcut_module.ssl.SSLError("bad certificate"),
        ):
            with self.assertRaises(shortcut_module.ssl.SSLError):
                connection.connect()
        self.assertTrue(raw.closed)

    def test_stuck_deadline_workers_are_globally_bounded_and_release(self) -> None:
        release_worker = threading.Event()
        calls = 0
        slots = threading.BoundedSemaphore(1)

        def stalled_resolver(_host, _port):
            nonlocal calls
            calls += 1
            release_worker.wait(timeout=1.0)
            return ("8.8.8.8",)

        fetcher = AllowlistedSourceFetcher(
            policy(),
            resolver=stalled_resolver,
            transport=lambda *_args: self.fail("transport must not run"),
            timeout_s=0.1,
        )
        with patch.object(shortcut_module, "_DEADLINE_WORKER_SLOTS", slots):
            with self.assertRaisesRegex(FetchSafetyError, "wall-clock deadline"):
                fetcher.fetch(
                    "sanitized_forum",
                    "https://tips.example.org/routes/batam.json",
                )
            with self.assertRaisesRegex(FetchSafetyError, "capacity.*saturated"):
                fetcher.fetch(
                    "sanitized_forum",
                    "https://tips.example.org/routes/batam.json",
                )
            self.assertEqual(calls, 1, "saturation spawned another stuck worker")

            release_worker.set()
            acquired = False
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                if slots.acquire(blocking=False):
                    acquired = True
                    slots.release()
                    break
                time.sleep(0.005)
            self.assertTrue(acquired, "completed timed-out worker did not release its slot")
            self.assertEqual(
                shortcut_module._run_with_deadline(lambda: "released", 0.1),
                "released",
            )
            with self.assertRaisesRegex(FetchSafetyError, "failed safely"):
                shortcut_module._run_with_deadline(
                    lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
                    0.1,
                )
            self.assertEqual(
                shortcut_module._run_with_deadline(lambda: "released-again", 0.1),
                "released-again",
            )

    def test_fetcher_pins_public_ip_and_builds_auditable_document(self) -> None:
        observed = {}

        def resolver(host: str, port: int):
            observed["resolve"] = (host, port)
            return ("8.8.8.8",)

        body = (FIXTURES / "shortcut_tips_sanitized.json").read_bytes()

        def transport(target, address, timeout, max_bytes):
            observed["transport"] = (
                target.hostname,
                target.request_target,
                address,
                timeout,
                max_bytes,
            )
            return FetchResponse(
                200,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                    "Content-Encoding": "identity",
                },
                body,
            )

        fetched = AllowlistedSourceFetcher(
            policy(),
            resolver=resolver,
            transport=transport,
            now_provider=lambda: NOW,
        ).fetch("sanitized_forum", "https://tips.example.org/routes/batam.json")
        self.assertEqual(observed["resolve"], ("tips.example.org", 443))
        self.assertEqual(observed["transport"][2], "8.8.8.8")
        self.assertEqual(fetched.content, body)
        self.assertEqual(fetched.retrieved_at, NOW)
        self.assertTrue(fetched.document_id.startswith("fetch-"))

    def test_fetcher_rejects_private_dns_even_from_injected_resolver(self) -> None:
        fetcher = AllowlistedSourceFetcher(
            policy(),
            resolver=lambda _host, _port: ("127.0.0.1",),
            transport=lambda *_args: self.fail("transport must not run"),
        )
        with self.assertRaisesRegex(FetchSafetyError, "non-public"):
            fetcher.fetch("sanitized_forum", "https://tips.example.org/routes/batam.json")

    def test_fetcher_rejects_redirect_mime_compression_and_size(self) -> None:
        cases = (
            (FetchResponse(302, {"location": "https://evil.test"}, b""), "Redirects"),
            (FetchResponse(200, {"content-type": "image/png"}, b"png"), "MIME"),
            (FetchResponse(200, {
                "content-type": "application/json", "content-encoding": "gzip",
            }, b"zip"), "Compressed"),
            (FetchResponse(200, {
                "content-type": "application/json", "content-length": "9999",
            }, b"{}"), "size cap"),
            (FetchResponse(200, {"content-type": "application/json"}, b"x" * 65), "size cap"),
        )
        limited_policy = SourcePolicy(
            registrations=(registration(),),
            max_document_bytes=64,
        )
        for response, message in cases:
            with self.subTest(message=message):
                fetcher = AllowlistedSourceFetcher(
                    limited_policy,
                    resolver=lambda _host, _port: ("8.8.8.8",),
                    transport=lambda *_args, response=response: response,
                )
                with self.assertRaisesRegex(FetchSafetyError, message):
                    fetcher.fetch(
                        "sanitized_forum",
                        "https://tips.example.org/routes/batam.json",
                    )

    def test_fetcher_has_one_sanitized_wall_clock_deadline(self) -> None:
        def stalled_transport(*_args):
            time.sleep(0.5)
            raise RuntimeError("internal upstream detail must not leak")

        fetcher = AllowlistedSourceFetcher(
            policy(),
            resolver=lambda _host, _port: ("8.8.8.8",),
            transport=stalled_transport,
            timeout_s=0.1,
        )
        started = time.monotonic()
        with self.assertRaisesRegex(FetchSafetyError, "wall-clock deadline") as context:
            fetcher.fetch("sanitized_forum", "https://tips.example.org/routes/batam.json")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)
        self.assertNotIn("internal upstream", str(context.exception))


class ShortcutPipelineTests(unittest.TestCase):
    def test_document_batch_bounds_generator_consumption(self) -> None:
        consumed = 0

        def infinite_documents():
            nonlocal consumed
            while True:
                consumed += 1
                yield fixture_document()

        pipeline = ShortcutTipPipeline(
            SourcePolicy(
                registrations=(registration(),),
                max_documents_per_batch=3,
            ),
            graph_index(),
        )
        with self.assertRaisesRegex(SourceContractError, "too many"):
            pipeline.ingest(infinite_documents())
        self.assertEqual(consumed, 4)
        self.assertEqual(pipeline.review_queue.snapshot(), ())

    def test_empty_batch_is_a_safe_noop(self) -> None:
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([])
        self.assertEqual(result.records, ())
        self.assertEqual(result.rejections, ())
        self.assertEqual(result.documents_processed, 0)
        self.assertEqual(result.tips_parsed, 0)
        self.assertEqual(result.inserted, 0)

    def test_malformed_document_item_fails_with_contract_error(self) -> None:
        pipeline = ShortcutTipPipeline(policy(), graph_index())
        with self.assertRaisesRegex(SourceContractError, "SourceDocument"):
            pipeline.ingest([object()])  # type: ignore[list-item]
        self.assertEqual(pipeline.review_queue.snapshot(), ())

    def test_review_queue_capacity_is_validated_and_atomic(self) -> None:
        for invalid in (False, 0, 100_001):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(SourceContractError, "max_entries"):
                    InMemoryShortcutReviewQueue(max_entries=invalid)

        queue = InMemoryShortcutReviewQueue(max_entries=1)
        pipeline = ShortcutTipPipeline(policy(), graph_index(), review_queue=queue)
        document = json_document([
            base_tip(tip_id="capacity-motorcycle"),
            base_tip(
                tip_id="capacity-car",
                vehicle_modes=["passenger_car"],
                snippet=(
                    "A separate sanitized claim describes the same endpoints "
                    "for passenger cars."
                ),
            ),
        ], document_id="queue-capacity")
        with self.assertRaisesRegex(SourceContractError, "capacity"):
            pipeline.ingest([document])
        self.assertEqual(queue.snapshot(), (), "capacity failure partially mutated queue")

    def test_graph_spatial_index_is_exact_and_bounded(self) -> None:
        runtime_nodes = {
            index: (1.0 + (index // 100) * 0.01, 103.9 + (index % 100) * 0.001)
            for index in range(2_000)
        }
        index = GraphIndex.from_runtime_nodes(
            runtime_nodes,
            bounds=(0.9, 103.8, 1.3, 104.1),
            graph_revision="runtime-revision-1",
        )
        coordinate = (1.1001, 103.9501)
        brute_id = min(
            runtime_nodes,
            key=lambda node_id: haversine_m(coordinate, runtime_nodes[node_id]),
        )
        with patch(
            "services.shortcut_ingestion.haversine_m",
            wraps=haversine_m,
        ) as measured:
            snapped = index.snap(coordinate)
        self.assertEqual(snapped.node_id, brute_id)
        self.assertLess(measured.call_count, len(runtime_nodes) // 10)

    def test_sanitized_json_becomes_inactive_typed_override(self) -> None:
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([fixture_document()])
        self.assertEqual((result.accepted, result.inserted, result.duplicates), (1, 1, 0))
        self.assertFalse(result.rejections)
        record = result.records[0]
        self.assertEqual((record.source_node, record.target_node), (101, 102))
        self.assertEqual(
            record.applicable_vehicle_modes,
            ("MOTORCYCLE", "PASSENGER_CAR"),
        )
        self.assertEqual(record.review_state, "REVIEW_REQUIRED")
        self.assertFalse(record.activation_allowed)
        self.assertEqual(record.graph_revision, "graph-revision-001")
        self.assertLessEqual(record.confidence, 0.65)
        self.assertFalse(record.road_quality_is_default)
        self.assertEqual(record.provenance[0].source_id, "sanitized_forum")
        self.assertEqual(len(record.provenance[0].content_sha256), 64)
        self.assertIn("UNTRUSTED_CROWD_CLAIM", record.validation_flags)
        self.assertFalse(result.to_dict()["summary"]["activation_allowed"])

    def test_html_visible_text_parser_ignores_script_and_parses_tip(self) -> None:
        html = (FIXTURES / "shortcut_blog_sanitized.html").read_bytes()
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([
            supplied_document(html, content_type="text/html", document_id="blog-doc"),
        ])
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.records[0].applicable_vehicle_modes, ("MOTORCYCLE",))
        self.assertEqual(result.records[0].provenance[0].parser, "geocoded_text_v1")
        self.assertNotIn("0,0", result.records[0].provenance[0].excerpt)

    def test_coordinates_are_normalized_and_unrated_quality_has_audited_fallback(self) -> None:
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([
            json_document([base_tip(
                start={"lat": "1.1000004", "lon": "104.0000004"},
                end=[1.1010004, 104.0010004],
                road_quality=None,
            )]),
        ])
        record = result.records[0]
        self.assertEqual(record.geometry, ((1.1, 104.0), (1.101, 104.001)))
        self.assertEqual(record.road_quality, 0.5)
        self.assertTrue(record.road_quality_is_default)

    def test_deterministic_identity_and_queue_upsert_are_idempotent(self) -> None:
        queue = InMemoryShortcutReviewQueue()
        pipeline = ShortcutTipPipeline(policy(), graph_index(), review_queue=queue)
        first = pipeline.ingest([fixture_document()])
        second = pipeline.ingest([fixture_document()])
        self.assertEqual(first.records[0].override_id, second.records[0].override_id)
        self.assertEqual((first.inserted, first.duplicates), (1, 0))
        self.assertEqual((second.inserted, second.duplicates), (0, 1))
        self.assertEqual(len(queue.snapshot()), 1)

    def test_duplicate_spatial_claims_merge_provenance_not_trust(self) -> None:
        first_tip = base_tip(tip_id="source-a", confidence=0.45)
        second_tip = base_tip(
            tip_id="source-b",
            confidence=0.95,
            claimed_duration_minutes=2.0,
            snippet="A second sanitized account mentions the same possible shortcut endpoint pair.",
        )
        result = ShortcutTipPipeline(policy(), graph_index()).ingest([
            json_document([first_tip], document_id="doc-a"),
            json_document([second_tip], document_id="doc-b"),
        ])
        self.assertEqual((result.accepted, result.duplicates), (1, 1))
        record = result.records[0]
        self.assertEqual(len(record.provenance), 2)
        self.assertLessEqual(record.confidence, 0.65)
        self.assertEqual(record.claimed_duration_s, 120.0)
        self.assertFalse(record.activation_allowed)

    def test_geometry_identity_and_merges_are_independent_of_document_order(self) -> None:
        documents = [
            json_document([base_tip(
                tip_id="merge-a",
                confidence=0.45,
                claimed_duration_minutes=1.2,
                road_quality=0.75,
            )], document_id="merge-doc-a"),
            json_document([base_tip(
                tip_id="merge-b",
                via=[[1.1004, 104.0004]],
                confidence=0.60,
                claimed_duration_minutes=1.8,
                road_quality=0.35,
                snippet="Another sanitized source describes the same endpoints with a rougher connector.",
            )], document_id="merge-doc-b"),
            json_document([base_tip(
                tip_id="merge-c",
                confidence=0.50,
                claimed_duration_minutes=1.5,
                road_quality=0.55,
                snippet="A third sanitized source describes the same candidate endpoint connection.",
            )], document_id="merge-doc-c"),
        ]
        forward = ShortcutTipPipeline(policy(), graph_index()).ingest(documents)
        reverse = ShortcutTipPipeline(policy(), graph_index()).ingest(reversed(documents))
        self.assertEqual(
            [record.to_dict() for record in forward.records],
            [record.to_dict() for record in reverse.records],
        )
        self.assertEqual(forward.accepted, 2)
        self.assertEqual(len({record.override_id for record in forward.records}), 2)
        straight = next(record for record in forward.records if len(record.geometry) == 2)
        via = next(record for record in forward.records if len(record.geometry) == 3)
        self.assertEqual(len(straight.provenance), 2)
        self.assertEqual(straight.claimed_duration_s, 90.0)
        self.assertEqual(via.road_quality, 0.35)
        self.assertEqual(via.claimed_duration_s, 108.0)
        self.assertNotEqual(straight.override_id, via.override_id)

    def test_validation_quarantines_outside_same_node_and_impossible_speed(self) -> None:
        cases = (
            (base_tip(tip_id="outside", start={"lat": 0, "lng": 0}), "OUTSIDE_GRAPH_BOUNDS"),
            (base_tip(tip_id="bad-wgs84", start={"lat": 91, "lng": 104}), "INVALID_COORDINATE"),
            (base_tip(
                tip_id="same-node",
                start={"lat": 1.100, "lng": 104.000},
                end={"lat": 1.10008, "lng": 104.00008},
                claimed_distance_m=None,
            ), "SAME_GRAPH_NODE"),
            (base_tip(
                tip_id="too-fast",
                claimed_distance_m=600,
                claimed_duration_minutes=0.05,
            ), "IMPLAUSIBLE_SPEED"),
        )
        for tip, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                result = ShortcutTipPipeline(policy(), graph_index()).ingest([
                    json_document([tip], document_id=f"doc-{expected_code.lower()}"),
                ])
                self.assertEqual(result.accepted, 0)
                self.assertEqual(result.rejections[0].code, expected_code)

    def test_unknown_fields_mime_utf8_and_document_caps_fail_closed(self) -> None:
        strict_result = ShortcutTipPipeline(policy(), graph_index()).ingest([
            json_document([base_tip(unexpected="claim")]),
        ])
        self.assertEqual(strict_result.accepted, 0)
        self.assertEqual(strict_result.rejections[0].code, "INVALID_TIP_SCHEMA")

        wrong_mime = ShortcutTipPipeline(policy(), graph_index()).ingest([
            supplied_document("anything", content_type="image/png", document_id="wrong-mime"),
        ])
        self.assertEqual(wrong_mime.rejections[0].code, "SOURCE_CONTRACT")

        invalid_utf8 = ShortcutTipPipeline(policy(), graph_index()).ingest([
            supplied_document(b"\xff\xfe", document_id="bad-utf8"),
        ])
        self.assertEqual(invalid_utf8.rejections[0].code, "SOURCE_CONTRACT")

        with self.assertRaisesRegex(SourceContractError, "too many"):
            ShortcutTipPipeline(
                SourcePolicy(registrations=(registration(),), max_documents_per_batch=1),
                graph_index(),
            ).ingest([fixture_document(), fixture_document()])

    def test_total_tip_cap_is_checked_before_review_queue_mutation(self) -> None:
        queue = InMemoryShortcutReviewQueue()
        pipeline = ShortcutTipPipeline(
            SourcePolicy(
                registrations=(registration(),),
                max_tips_per_batch=1,
            ),
            graph_index(),
            review_queue=queue,
        )
        with self.assertRaisesRegex(SourceContractError, "too many shortcut tips"):
            pipeline.ingest([
                json_document([base_tip(tip_id="first")], document_id="cap-a"),
                json_document([base_tip(tip_id="second")], document_id="cap-b"),
            ])
        self.assertEqual(queue.snapshot(), ())

    def test_record_invariant_forbids_activation(self) -> None:
        record = ShortcutTipPipeline(policy(), graph_index()).ingest([fixture_document()]).records[0]
        with self.assertRaisesRegex(SourceContractError, "cannot be activated"):
            replace(record, activation_allowed=True)
        with self.assertRaisesRegex(SourceContractError, "cannot be activated"):
            replace(record, review_state="APPROVED")

    def test_future_retrieval_timestamp_is_rejected_as_provenance(self) -> None:
        future = replace(fixture_document(), retrieved_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        result = ShortcutTipPipeline(
            policy(),
            graph_index(),
            now_provider=lambda: NOW,
        ).ingest([future])
        self.assertEqual(result.accepted, 0)
        self.assertEqual(result.rejections[0].code, "SOURCE_CONTRACT")
        self.assertIn("future", result.rejections[0].detail)


if __name__ == "__main__":
    unittest.main()
