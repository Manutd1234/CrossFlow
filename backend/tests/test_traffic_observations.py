"""Focused tests for typed multi-year spatial congestion history."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from services.historical_store import HistoryStore  # noqa: E402
from services.traffic_observations import (  # noqa: E402
    CongestionEstimatorConfig,
    ObservationConflictError,
    ObservationValidationError,
    SpatialTrafficObservation,
    border_crossing_penalty,
    estimate_speed_ratio_congestion,
    ferry_surge_factor,
    ferry_wait_penalty,
    peak_penalty,
    validate_observation_batch,
)


WIB = timezone(timedelta(hours=7))
FIXED_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=WIB)


def observation(
    observed_at: datetime,
    *,
    source: str = "probe_gps",
    actual_speed_kph: float = 30.0,
    free_flow_speed_kph: float = 60.0,
    upstream_event_id: str | None = None,
    reviewed: bool = False,
    local_timezone_offset_minutes: int = 420,
) -> SpatialTrafficObservation:
    return SpatialTrafficObservation.create(
        corridor_id="corridor-1",
        observed_at=observed_at,
        latitude=1.1305,
        longitude=104.0535,
        actual_speed_kph=actual_speed_kph,
        free_flow_speed_kph=free_flow_speed_kph,
        source=source,
        reviewed=reviewed,
        road_class="primary",
        capacity_vph=1_800,
        terminal_distance_km=0.8,
        local_timezone_offset_minutes=local_timezone_offset_minutes,
        upstream_event_id=upstream_event_id,
        validation_now=FIXED_NOW,
    )


class ObservationValidationTests(unittest.TestCase):
    def test_batch_validation_bounds_generator_consumption(self) -> None:
        consumed = 0

        def infinite_items():
            nonlocal consumed
            while True:
                consumed += 1
                yield object()

        with self.assertRaisesRegex(ObservationValidationError, "3-record limit"):
            validate_observation_batch(infinite_items(), max_size=3)
        self.assertEqual(consumed, 4)

    def test_direct_constructor_cannot_forge_policy_or_numeric_fields(self) -> None:
        valid = observation(FIXED_NOW, upstream_event_id="forgery-check")
        for label, forged in (
            ("key", replace(valid, observation_key="0" * 64)),
            ("provenance", replace(valid, provenance="verified_sensor")),
            ("confidence", replace(valid, confidence=1.0)),
            ("observed", replace(valid, observed=False)),
            ("observed-type", replace(valid, observed=1)),
            ("reviewed-type", replace(valid, reviewed=1)),
            ("source", replace(valid, source="scraped_blog_tip")),
            ("boolean-coordinate", replace(valid, latitude=True)),
            ("latitude", replace(valid, latitude=float("nan"))),
            ("outside-batam", replace(valid, latitude=40.7128, longitude=-74.0060)),
            ("longitude", replace(valid, longitude=181.0)),
            ("actual-speed", replace(valid, actual_speed_kph=0.0)),
            ("free-flow", replace(valid, free_flow_speed_kph=float("inf"))),
            ("timestamp", replace(valid, observed_at=FIXED_NOW.replace(tzinfo=None))),
            ("road-class", replace(valid, road_class="secret_alley")),
            ("capacity", replace(valid, capacity_vph=-1.0)),
            ("terminal-distance", replace(valid, terminal_distance_km=-1.0)),
            ("timezone-offset", replace(
                valid, local_timezone_offset_minutes=480,
            )),
            ("upstream-id", replace(valid, upstream_event_id="bad id")),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ObservationValidationError):
                    validate_observation_batch([forged])

    def test_direct_constructor_cannot_bypass_community_review(self) -> None:
        reviewed = observation(
            FIXED_NOW,
            source="reviewed_community_observation",
            reviewed=True,
            upstream_event_id="review-policy",
        )
        with self.assertRaisesRegex(ObservationValidationError, "review"):
            validate_observation_batch([replace(reviewed, reviewed=False)])

    def test_requires_timezone_numeric_speed_and_approved_source(self) -> None:
        with self.assertRaisesRegex(ObservationValidationError, "timezone"):
            observation(FIXED_NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(ObservationValidationError, "ratio"):
            observation(FIXED_NOW, actual_speed_kph=2, free_flow_speed_kph=100)
        with self.assertRaisesRegex(ObservationValidationError, "Unsupported"):
            observation(FIXED_NOW, source="scraped_blog_tip")

    def test_reviewed_community_is_lower_trust_and_requires_review(self) -> None:
        with self.assertRaisesRegex(ObservationValidationError, "review"):
            observation(FIXED_NOW, source="reviewed_community_observation")
        reviewed = observation(
            FIXED_NOW,
            source="reviewed_community_observation",
            reviewed=True,
        )
        self.assertEqual(reviewed.provenance, "reviewed_community")
        self.assertTrue(reviewed.observed)
        self.assertLess(reviewed.confidence, observation(FIXED_NOW).confidence)


class SpatialHistoryStoreTests(unittest.TestCase):
    def test_store_rejects_direct_constructor_forgery_before_write(self) -> None:
        store = HistoryStore(":memory:", now_provider=lambda: FIXED_NOW)
        try:
            forged = replace(
                observation(FIXED_NOW, upstream_event_id="store-forgery"),
                confidence=1.0,
            )
            with self.assertRaisesRegex(ObservationValidationError, "forged"):
                store.ingest_spatial_batch([forged], now=FIXED_NOW)
            self.assertEqual(store._conn.execute(
                "SELECT COUNT(*) FROM spatial_observations"
            ).fetchone()[0], 0)
        finally:
            store.close()

    def test_batch_is_atomic_idempotent_and_detects_upstream_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                os.path.join(directory, "history.db"),
                now_provider=lambda: FIXED_NOW,
            )
            try:
                first = observation(
                    FIXED_NOW - timedelta(minutes=5),
                    upstream_event_id="gps-123",
                )
                result = store.ingest_spatial_batch([first, first], now=FIXED_NOW)
                self.assertEqual((result.received, result.unique), (2, 1))
                self.assertEqual((result.inserted, result.duplicates), (1, 1))

                replay = store.ingest_spatial_batch([first], now=FIXED_NOW)
                self.assertEqual((replay.inserted, replay.duplicates), (0, 1))
                rows = store.get_spatial_observations(
                    "corridor-1",
                    start_at=FIXED_NOW - timedelta(days=1),
                    end_at=FIXED_NOW,
                )
                self.assertEqual(rows, [first])

                conflict = observation(
                    FIXED_NOW - timedelta(minutes=5),
                    actual_speed_kph=20,
                    upstream_event_id="gps-123",
                )
                with self.assertRaises(ObservationConflictError):
                    store.ingest_spatial_batch([conflict], now=FIXED_NOW)
                self.assertEqual(
                    store._conn.execute(
                        "SELECT COUNT(*) FROM spatial_observations"
                    ).fetchone()[0],
                    1,
                )
            finally:
                store.close()

    def test_default_retention_accepts_multi_year_but_rejects_older(self) -> None:
        store = HistoryStore(":memory:", now_provider=lambda: FIXED_NOW)
        try:
            within_window = observation(FIXED_NOW - timedelta(days=4 * 365))
            self.assertEqual(
                store.ingest_spatial_batch([within_window], now=FIXED_NOW).inserted,
                1,
            )
            too_old = observation(FIXED_NOW - timedelta(days=5 * 365 + 1))
            with self.assertRaisesRegex(ObservationValidationError, "retention"):
                store.ingest_spatial_batch([too_old], now=FIXED_NOW)
        finally:
            store.close()

    def test_training_read_excludes_rows_that_aged_out_without_new_ingest(self) -> None:
        store = HistoryStore(":memory:", now_provider=lambda: FIXED_NOW)
        try:
            record = observation(
                FIXED_NOW - timedelta(days=4 * 365),
                upstream_event_id="ages-out",
            )
            store.ingest_spatial_batch([record], now=FIXED_NOW)
            self.assertEqual(
                store.get_spatial_training_dataset(now=FIXED_NOW), [record],
            )
            self.assertEqual(
                store.get_spatial_training_dataset(
                    now=FIXED_NOW + timedelta(days=2 * 365),
                ),
                [],
            )
        finally:
            store.close()


class CongestionEstimatorTests(unittest.TestCase):
    def test_estimator_bounds_history_and_authenticates_every_record(self) -> None:
        current = observation(FIXED_NOW, upstream_event_id="estimate-current")
        consumed = 0

        def infinite_history():
            nonlocal consumed
            while True:
                consumed += 1
                yield observation(
                    FIXED_NOW - timedelta(minutes=consumed),
                    upstream_event_id=f"history-{consumed}",
                )

        with self.assertRaisesRegex(ObservationValidationError, "3-record"):
            estimate_speed_ratio_congestion(
                current,
                infinite_history(),
                max_history_size=3,
            )
        self.assertEqual(consumed, 4)

        forged_history = replace(
            observation(
                FIXED_NOW - timedelta(hours=1),
                upstream_event_id="forged-history",
            ),
            confidence=1.0,
        )
        with self.assertRaisesRegex(ObservationValidationError, "forged"):
            estimate_speed_ratio_congestion(current, [forged_history])

        forged_current = replace(current, observation_key="0" * 64)
        with self.assertRaisesRegex(ObservationValidationError, "canonical"):
            estimate_speed_ratio_congestion(forged_current, [])

    def test_decay_matches_local_wib_hour_not_same_utc_clock_hour(self) -> None:
        current = observation(
            datetime(2026, 8, 14, 8, 0, tzinfo=WIB),
            actual_speed_kph=54,
        )
        aligned_0800_wib = observation(
            datetime(2026, 8, 7, 8, 0, tzinfo=WIB),
            actual_speed_kph=18,
            upstream_event_id="aligned",
        )
        same_utc_clock_but_1500_wib = observation(
            datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),
            actual_speed_kph=18,
            upstream_event_id="utc-clock",
        )
        config = CongestionEstimatorConfig(
            history_half_life_days=100,
            hour_distance_scale=1,
            weekday_distance_scale=10,
        )
        aligned = estimate_speed_ratio_congestion(
            current, [aligned_0800_wib], config=config,
        )
        misaligned = estimate_speed_ratio_congestion(
            current, [same_utc_clock_but_1500_wib], config=config,
        )
        self.assertLess(aligned.expected_speed_ratio, misaligned.expected_speed_ratio)
        self.assertGreater(aligned.congestion_score, misaligned.congestion_score)

    def test_provenance_and_router_contract_remain_explicit(self) -> None:
        current = observation(FIXED_NOW, source="simulated")
        history = [observation(
            FIXED_NOW - timedelta(days=7),
            source="reviewed_community_observation",
            reviewed=True,
        )]
        estimate = estimate_speed_ratio_congestion(current, history)
        self.assertEqual(estimate.provenance, "mixed_observed_and_modelled")
        self.assertEqual(estimate.observed_history_count, 1)
        payload = estimate.router_payload(free_flow_travel_seconds=600)
        self.assertEqual(
            set(payload),
            {
                "expected_speed_ratio", "speed_ratio_std",
                "p90_speed_ratio", "p90_delay_s", "source",
            },
        )
        self.assertGreaterEqual(payload["p90_delay_s"], 0)

    def test_nonlinear_penalties_and_ferry_surge_are_monotonic(self) -> None:
        peak = datetime(2026, 8, 14, 8, 0, tzinfo=WIB)
        off_peak = datetime(2026, 8, 14, 13, 0, tzinfo=WIB)
        self.assertGreater(peak_penalty(peak), peak_penalty(off_peak))
        self.assertGreater(
            border_crossing_penalty(20, utilization_ratio=1),
            border_crossing_penalty(20, utilization_ratio=0.5),
        )
        self.assertGreater(
            ferry_wait_penalty(30, missed_boarding_cutoff=True),
            ferry_wait_penalty(30),
        )
        self.assertGreater(ferry_surge_factor(5), ferry_surge_factor(60))


if __name__ == "__main__":
    unittest.main()
