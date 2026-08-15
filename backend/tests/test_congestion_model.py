"""Spatial quantile-ensemble and calibration contract tests."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from models.congestion_model import (  # noqa: E402
    CALIBRATION_INTERVAL_TARGET,
    CongestionForecaster,
    SpatialPredictionContext,
    delay_from_score,
)
from services.route_solver import _forecast_ferry_minutes  # noqa: E402
from services.traffic_observations import SpatialTrafficObservation  # noqa: E402


WIB = timezone(timedelta(hours=7))


def training_observations(count: int = 120) -> list[SpatialTrafficObservation]:
    base = datetime(2026, 1, 1, 0, 0, tzinfo=WIB)
    validation_now = base + timedelta(days=10)
    return [SpatialTrafficObservation.create(
        corridor_id=f"corridor-{index % 5 + 1}",
        observed_at=base + timedelta(hours=index),
        latitude=1.10 + (index % 5) * 0.01,
        longitude=104.00 + (index % 5) * 0.01,
        actual_speed_kph=20.0 + index % 30,
        free_flow_speed_kph=60.0,
        source="probe_gps",
        road_class="primary",
        upstream_event_id=f"atomic-{index}",
        validation_now=validation_now,
    ) for index in range(count)]


def context(**overrides: object) -> SpatialPredictionContext:
    values = {
        "hour_float": 18.0,
        "day_of_week": 4,
        "weather": 0,
        "corridor_idx": 0,
        "road_class": "primary",
        "capacity_vph": 1_800.0,
        "terminal_distance_km": 0.8,
        "free_flow_speed_kph": 50.0,
    }
    values.update(overrides)
    return SpatialPredictionContext(**values)  # type: ignore[arg-type]


class SpatialQuantilePredictionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.forecaster = CongestionForecaster()

    def test_departed_ferry_does_not_become_maximum_future_surge(self) -> None:
        self.assertEqual(_forecast_ferry_minutes(40.0, 30.0), 10.0)
        self.assertEqual(_forecast_ferry_minutes(30.0, 30.0), 0.0)
        self.assertIsNone(_forecast_ferry_minutes(10.0, 30.0))
        self.assertIsNone(_forecast_ferry_minutes(None, 30.0))

    def test_quantiles_bounds_uncertainty_and_upper_delay(self) -> None:
        prediction = self.forecaster.predict_spatial(context())
        self.assertLessEqual(prediction.p10_score, prediction.p90_score)
        for score in (
            prediction.p10_score,
            prediction.mean_score,
            prediction.p90_score,
            prediction.risk_adjusted_score,
        ):
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(prediction.std_score, 0.0)
        self.assertGreaterEqual(prediction.upper_bound_delay_mins, 0.0)
        self.assertEqual(
            prediction.upper_bound_delay_mins,
            delay_from_score(prediction.p90_score),
        )

    def test_risk_aversion_is_monotonic(self) -> None:
        neutral = self.forecaster.predict_spatial(context(risk_aversion=0.0))
        cautious = self.forecaster.predict_spatial(context(risk_aversion=1.5))
        self.assertEqual(neutral.mean_score, cautious.mean_score)
        self.assertGreaterEqual(
            cautious.risk_adjusted_score,
            neutral.risk_adjusted_score,
        )

    def test_high_ensemble_uncertainty_escalates_risk(self) -> None:
        # Mean 20 is SMOOTH, but a 20-point std means the trees disagree enough
        # that a route consumer must not receive a LOW risk label.
        dispersed = np.array([[0.0], [40.0]] * 50)
        with patch.object(
            self.forecaster,
            "_tree_predictions",
            return_value=dispersed,
        ):
            prediction = self.forecaster.predict_spatial(context())
        self.assertEqual(prediction.status, "SMOOTH")
        self.assertGreater(prediction.std_score, 15.0)
        self.assertEqual(prediction.risk_level, "HIGH")

    def test_downside_p90_never_publishes_a_faster_than_mean_ratio(self) -> None:
        upper_tail = np.array([[70.0]] * 99 + [[100.0]])
        with patch.object(
            self.forecaster,
            "_tree_predictions",
            return_value=upper_tail,
        ):
            prediction = self.forecaster.predict_spatial(context())
        self.assertGreaterEqual(prediction.p90_score, prediction.mean_score)
        payload = prediction.router_payload(free_flow_travel_seconds=600.0)
        self.assertLessEqual(
            payload["p90_speed_ratio"],
            payload["expected_speed_ratio"],
        )

    def test_ferry_proximity_uses_exponential_pressure_feature(self) -> None:
        no_sailing = self.forecaster.predict_spatial(context(
            hour_float=14.0,
            minutes_until_ferry_departure=None,
        ))
        imminent = self.forecaster.predict_spatial(context(
            hour_float=14.0,
            minutes_until_ferry_departure=0.0,
        ))
        self.assertGreater(imminent.mean_score, no_sailing.mean_score)
        self.assertEqual(no_sailing.ferry_surge_adjustment_points, 0.0)
        self.assertGreater(imminent.ferry_surge_adjustment_points, 0.0)

    def test_day_of_week_is_cyclical_and_weekend_peak_differs(self) -> None:
        monday_features = self.forecaster._context_features(context(day_of_week=0))
        sunday_features = self.forecaster._context_features(context(day_of_week=6))
        self.assertFalse((monday_features[2:4] == sunday_features[2:4]).all())
        friday = self.forecaster.predict_spatial(context(day_of_week=4))
        saturday = self.forecaster.predict_spatial(context(day_of_week=5))
        self.assertLess(saturday.mean_score, friday.mean_score)

    def test_road_class_is_one_hot_not_an_ordinal(self) -> None:
        primary = self.forecaster._context_features(context(road_class="primary"))
        service = self.forecaster._context_features(context(road_class="service"))
        primary_encoding = primary[11:23]
        service_encoding = service[11:23]
        self.assertEqual(float(primary_encoding.sum()), 1.0)
        self.assertEqual(float(service_encoding.sum()), 1.0)
        self.assertFalse((primary_encoding == service_encoding).all())

    def test_corridor_identity_is_one_hot_not_an_ordinal(self) -> None:
        first = self.forecaster._context_features(context(corridor_idx=0))
        fifth = self.forecaster._context_features(context(corridor_idx=4))
        self.assertEqual(first[6:11].tolist(), [1.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(fifth[6:11].tolist(), [0.0, 0.0, 0.0, 0.0, 1.0])

    def test_legacy_predict_contract_is_unchanged(self) -> None:
        prediction = self.forecaster.predict(18, 0, 0, 1, 0)
        self.assertEqual(set(prediction), {
            "current_score", "predicted_30min", "predicted_60min",
            "estimated_delay_mins", "status", "risk_level", "trend",
        })


class SpatialCalibrationTests(unittest.TestCase):
    def test_external_retrain_snapshot_is_bounded_before_validation(self) -> None:
        consumed = 0

        def endless():
            nonlocal consumed
            while True:
                consumed += 1
                yield object()

        model = CongestionForecaster()
        with patch("models.congestion_model.MAX_SPATIAL_RETRAIN_SAMPLES", 3):
            with self.assertRaisesRegex(ValueError, "limited to 3 rows"):
                model.retrain_from_observations(endless())
        self.assertEqual(consumed, 4)

    def test_chronological_retrain_uses_source_confidence_and_calibrates(self) -> None:
        base = datetime(2026, 1, 1, 0, 0, tzinfo=WIB)
        validation_now = base + timedelta(days=10)
        observations = []
        for index in range(120):
            if index < 60:
                source = "synthetic"
                reviewed = False
            elif index < 100:
                source = "reviewed_community_observation"
                reviewed = True
            else:
                source = "probe_gps"
                reviewed = False
            observations.append(SpatialTrafficObservation.create(
                corridor_id=f"corridor-{index % 5 + 1}",
                observed_at=base + timedelta(hours=index),
                latitude=1.10 + (index % 5) * 0.01,
                longitude=104.00 + (index % 5) * 0.01,
                actual_speed_kph=20.0 + index % 30,
                free_flow_speed_kph=60.0,
                source=source,
                reviewed=reviewed,
                road_class=("primary" if index % 2 == 0 else "secondary"),
                capacity_vph=1_200 + index % 5 * 100,
                terminal_distance_km=0.5 + index % 5,
                upstream_event_id=f"cal-{index}",
                validation_now=validation_now,
            ))

        model = CongestionForecaster()
        metrics = model.retrain_from_observations(reversed(observations))

        self.assertEqual(
            metrics["validation_scope"],
            "chronological_spatial_holdout_mixed",
        )
        self.assertEqual(metrics["fit_source_counts"], {
            "synthetic": 60,
            "reviewed_community_observation": 36,
        })
        self.assertAlmostEqual(
            metrics["fit_source_weight_totals"]["synthetic"],
            60 * 0.15,
        )
        self.assertAlmostEqual(
            metrics["fit_source_weight_totals"][
                "reviewed_community_observation"
            ],
            36 * 0.55,
        )
        self.assertEqual(metrics["holdout_source_counts"], {
            "reviewed_community_observation": 4,
            "probe_gps": 20,
        })
        calibration = metrics["calibration"]
        self.assertEqual(calibration["holdout_order"], "chronological_observed_at")
        self.assertEqual(
            calibration["interval_coverage_target"],
            CALIBRATION_INTERVAL_TARGET,
        )
        self.assertGreaterEqual(calibration["interval_coverage_observed"], 0.0)
        self.assertLessEqual(calibration["interval_coverage_observed"], 1.0)
        self.assertGreaterEqual(calibration["mean_interval_width_points"], 0.0)
        self.assertGreaterEqual(calibration["empirical_crps"], 0.0)
        self.assertIn("mean|tree_prediction-y|", calibration["empirical_crps_method"])

        no_sailing = model.predict_spatial(context(
            hour_float=14.0,
            minutes_until_ferry_departure=None,
        ))
        imminent = model.predict_spatial(context(
            hour_float=14.0,
            minutes_until_ferry_departure=0.0,
        ))
        self.assertGreater(imminent.mean_score, no_sailing.mean_score)
        self.assertGreater(imminent.p10_score, no_sailing.p10_score)
        self.assertGreater(imminent.p90_score, no_sailing.p90_score)
        self.assertGreater(imminent.ferry_surge_adjustment_points, 0.0)
        self.assertEqual(
            metrics["ferry_surge_adjustment"]["method"],
            "post_model_exponential_tree_shift",
        )

    def test_failed_candidate_fit_keeps_live_model_and_metrics(self) -> None:
        model = CongestionForecaster()
        live_model = model.model
        live_metrics = model.metrics
        before = model.predict_spatial(context()).mean_score

        class FailedCandidate:
            def fit(self, *_args, **_kwargs):
                raise RuntimeError("candidate fit failed")

        with patch.object(
            model, "_new_unfitted_model", return_value=FailedCandidate(),
        ):
            with self.assertRaisesRegex(RuntimeError, "candidate fit failed"):
                model.retrain_from_observations(training_observations())
        self.assertIs(model.model, live_model)
        self.assertIs(model.metrics, live_metrics)
        self.assertEqual(model.predict_spatial(context()).mean_score, before)

    def test_predictions_continue_while_candidate_model_fits(self) -> None:
        model = CongestionForecaster()
        candidate = model._new_unfitted_model()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        failure: list[BaseException] = []
        original_fit = candidate.fit

        def delayed_fit(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release candidate fit")
            return original_fit(*args, **kwargs)

        candidate.fit = delayed_fit  # type: ignore[method-assign]

        def retrain() -> None:
            try:
                model.retrain_from_observations(training_observations())
            except BaseException as error:  # captured for the test thread
                failure.append(error)
            finally:
                finished.set()

        with patch.object(model, "_new_unfitted_model", return_value=candidate):
            worker = threading.Thread(target=retrain, daemon=True)
            worker.start()
            self.assertTrue(started.wait(timeout=3))
            # This call would block behind fit if retraining held _model_lock.
            prediction = model.predict_spatial(context())
            self.assertGreaterEqual(prediction.mean_score, 0.0)
            self.assertFalse(finished.is_set())
            release.set()
            self.assertTrue(finished.wait(timeout=10))
            worker.join(timeout=1)
        self.assertEqual(failure, [])
        self.assertIs(model.model, candidate)


if __name__ == "__main__":
    unittest.main()
