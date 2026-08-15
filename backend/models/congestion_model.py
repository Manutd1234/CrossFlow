from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
from itertools import islice
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from services.traffic_observations import (
    SpatialTrafficObservation,
    ferry_surge_factor,
    validate_spatial_observation,
)

# ---------------------------------------------------------------------------
# Shared classification rules.
# ---------------------------------------------------------------------------

CRITICAL_THRESHOLD = 70.0
HEAVY_THRESHOLD = 40.0
DELAY_MINS_AT_FULL_CONGESTION = 28.0
OBSERVED_HISTORY_SOURCES = frozenset({
    "loop_sensor", "probe_gps", "tomtom_live",
    "verified_traffic_observation", "reviewed_community_observation",
})

SPATIAL_ENSEMBLE_MODEL_ID = "crossflow_spatial_quantile_rf_v1"
CALIBRATION_INTERVAL_TARGET = 0.80
HIGH_UNCERTAINTY_STD_THRESHOLD = 15.0
FERRY_SURGE_AMPLITUDE_POINTS = 15.0
FERRY_SURGE_DECAY_MINUTES = 45.0
FERRY_SURGE_FEATURE_INDEX = 5
MAX_SPATIAL_RETRAIN_SAMPLES = 100_000

_ROAD_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service",
    "track", "road", "ferry",
)
_ROAD_CLASS_INDEX = {name: index for index, name in enumerate(_ROAD_CLASSES)}

# Corridor-level assumptions preserve the legacy API, which has no edge
# metadata. They are explicit fallbacks, not claims of lane-level precision.
LEGACY_CORRIDOR_SPATIAL_DEFAULTS: Mapping[int, Mapping[str, Any]] = {
    0: {"road_class": "primary", "capacity_vph": 1_800.0,
        "terminal_distance_km": 0.8, "free_flow_speed_kph": 50.0},
    1: {"road_class": "primary", "capacity_vph": 1_700.0,
        "terminal_distance_km": 1.0, "free_flow_speed_kph": 48.0},
    2: {"road_class": "secondary", "capacity_vph": 1_400.0,
        "terminal_distance_km": 1.2, "free_flow_speed_kph": 45.0},
    3: {"road_class": "secondary", "capacity_vph": 1_200.0,
        "terminal_distance_km": 1.5, "free_flow_speed_kph": 42.0},
    4: {"road_class": "tertiary", "capacity_vph": 900.0,
        "terminal_distance_km": 1.8, "free_flow_speed_kph": 38.0},
}


@dataclass(frozen=True, slots=True)
class SpatialPredictionContext:
    """Typed feature contract for one edge/corridor congestion prediction."""

    hour_float: float
    day_of_week: int
    weather: int
    corridor_idx: int
    road_class: str
    capacity_vph: float
    terminal_distance_km: float
    free_flow_speed_kph: float
    minutes_until_ferry_departure: Optional[float] = None
    risk_aversion: float = 0.0
    spatial_source: str = "explicit_edge_features"
    defaults_applied: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        numeric = (
            self.hour_float, self.capacity_vph, self.terminal_distance_km,
            self.free_flow_speed_kph, self.risk_aversion,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("Spatial prediction features must be finite.")
        if not 0.0 <= self.hour_float < 24.0:
            raise ValueError("hour_float must be in [0, 24).")
        if not 0 <= self.day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6.")
        if self.weather not in (0, 1, 2):
            raise ValueError("weather must be 0, 1, or 2.")
        if self.corridor_idx not in LEGACY_CORRIDOR_SPATIAL_DEFAULTS:
            raise ValueError("corridor_idx must identify one of five corridors.")
        if self.road_class not in _ROAD_CLASS_INDEX:
            raise ValueError(f"Unsupported road_class {self.road_class!r}.")
        if self.capacity_vph <= 0.0 or self.free_flow_speed_kph <= 0.0:
            raise ValueError("Capacity and free-flow speed must be positive.")
        if self.terminal_distance_km < 0.0 or self.risk_aversion < 0.0:
            raise ValueError("Distance and risk aversion cannot be negative.")
        if self.minutes_until_ferry_departure is not None and (
            not math.isfinite(float(self.minutes_until_ferry_departure))
            or self.minutes_until_ferry_departure < 0.0
        ):
            raise ValueError("Ferry departure minutes cannot be negative.")

    @classmethod
    def from_legacy(
        cls,
        hour_float: float,
        is_weekend: int,
        weather: int,
        ferry_surge: int,
        corridor_idx: int,
        *,
        risk_aversion: float = 0.0,
    ) -> "SpatialPredictionContext":
        defaults = LEGACY_CORRIDOR_SPATIAL_DEFAULTS.get(corridor_idx)
        if defaults is None:
            raise ValueError("corridor_idx must identify one of five corridors.")
        # The legacy contract knows only weekend/not-weekend. Wednesday and
        # Saturday are declared representative encodings, never inferred dates.
        day_of_week = 5 if is_weekend else 2
        return cls(
            hour_float=hour_float % 24.0,
            day_of_week=day_of_week,
            weather=weather,
            corridor_idx=corridor_idx,
            road_class=str(defaults["road_class"]),
            capacity_vph=float(defaults["capacity_vph"]),
            terminal_distance_km=float(defaults["terminal_distance_km"]),
            free_flow_speed_kph=float(defaults["free_flow_speed_kph"]),
            minutes_until_ferry_departure=(0.0 if ferry_surge else None),
            risk_aversion=risk_aversion,
            spatial_source="legacy_corridor_defaults",
            defaults_applied=(
                "representative_day_of_week", "road_class", "capacity_vph",
                "terminal_distance_km", "free_flow_speed_kph",
            ),
        )

    @classmethod
    def from_observation(
        cls,
        observation: SpatialTrafficObservation,
        *,
        corridor_idx: int,
        weather: int = 0,
        minutes_until_ferry_departure: Optional[float] = None,
        risk_aversion: float = 0.0,
    ) -> "SpatialPredictionContext":
        local = observation.local_observed_at
        defaults = LEGACY_CORRIDOR_SPATIAL_DEFAULTS[corridor_idx]
        missing = []
        capacity = observation.capacity_vph
        if capacity is None:
            capacity = float(defaults["capacity_vph"])
            missing.append("capacity_vph")
        terminal_distance = observation.terminal_distance_km
        if terminal_distance is None:
            terminal_distance = float(defaults["terminal_distance_km"])
            missing.append("terminal_distance_km")
        return cls(
            hour_float=local.hour + local.minute / 60.0 + local.second / 3600.0,
            day_of_week=local.weekday(),
            weather=weather,
            corridor_idx=corridor_idx,
            road_class=observation.road_class,
            capacity_vph=capacity,
            terminal_distance_km=terminal_distance,
            free_flow_speed_kph=observation.free_flow_speed_kph,
            minutes_until_ferry_departure=minutes_until_ferry_departure,
            risk_aversion=risk_aversion,
            spatial_source=observation.provenance,
            defaults_applied=tuple(missing),
        )


@dataclass(frozen=True, slots=True)
class SpatialCongestionPrediction:
    """Quantile ensemble output with a direct risk-aversion routing hook."""

    mean_score: float
    std_score: float
    p10_score: float
    p90_score: float
    upper_bound_delay_mins: float
    risk_adjusted_score: float
    expected_speed_ratio: float
    speed_ratio_std: float
    status: str
    risk_level: str
    model_id: str
    spatial_source: str
    defaults_applied: Tuple[str, ...]
    ferry_surge_adjustment_points: float
    ferry_surge_adjustment_method: str

    def router_payload(self, *, free_flow_travel_seconds: float) -> Dict[str, Any]:
        if not math.isfinite(free_flow_travel_seconds) \
                or free_flow_travel_seconds < 0.0:
            raise ValueError("free_flow_travel_seconds must be finite and nonnegative.")
        # A congestion P90 is a downside (slower) route bound. Keep this public
        # adapter defensive even for a directly constructed prediction whose
        # quantile is inconsistent with its mean.
        p90_speed_ratio = min(
            self.expected_speed_ratio,
            max(0.05, 1.0 - max(self.mean_score, self.p90_score) / 100.0),
        )
        return {
            "expected_speed_ratio": self.expected_speed_ratio,
            "speed_ratio_std": self.speed_ratio_std,
            "p90_speed_ratio": p90_speed_ratio,
            "p90_delay_s": max(
                0.0,
                free_flow_travel_seconds * (1.0 / p90_speed_ratio - 1.0),
            ),
            "source": f"{self.model_id}:{self.spatial_source}",
        }


def classify_status(score: float) -> str:
    if score >= CRITICAL_THRESHOLD:
        return "CRITICAL"
    if score >= HEAVY_THRESHOLD:
        return "HEAVY"
    return "SMOOTH"


def risk_from_status(status: str) -> str:
    return {"CRITICAL": "HIGH", "HEAVY": "MODERATE"}.get(status, "LOW")


def uncertainty_aware_risk(status: str, std_score: float) -> str:
    """Escalate routing risk when ensemble disagreement is itself material."""
    if not math.isfinite(std_score) or std_score < 0.0:
        raise ValueError("std_score must be finite and nonnegative.")
    if std_score > HIGH_UNCERTAINTY_STD_THRESHOLD:
        return "HIGH"
    return risk_from_status(status)


def delay_from_score(score: float) -> float:
    return round((score / 100.0) * DELAY_MINS_AT_FULL_CONGESTION, 1)


def history_training_provenance(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Describe whether stored training rows are observed, mixed or modelled."""
    source_counts = dict(Counter(
        str(record.get("source") or "undeclared")
        for record in records
    ))
    observed_training_rows = sum(
        count for source, count in source_counts.items()
        if source in OBSERVED_HISTORY_SOURCES
    )
    if records and observed_training_rows == len(records):
        training_data_source = "history_store_observed"
        validation_scope = "history_holdout_observed"
    elif observed_training_rows > 0:
        training_data_source = "history_store_mixed"
        validation_scope = "history_holdout_mixed"
    else:
        training_data_source = "history_store_non_observed"
        validation_scope = "history_holdout_non_observed"
    return {
        "training_data_source": training_data_source,
        "validation_scope": validation_scope,
        "training_source_counts": source_counts,
        "observed_training_rows": observed_training_rows,
    }


class CongestionForecaster:
    """Random-forest spatial quantile ensemble with a legacy API facade.

    Tree dispersion supplies empirical P10/P90 bounds without another runtime
    dependency. It is not a GNN and does not claim topology-level learning.
    """

    def __init__(self):
        self._model_lock = threading.RLock()
        self._retrain_lock = threading.Lock()
        self.model = RandomForestRegressor(n_estimators=100, random_state=42)
        self.is_trained = False
        self.metrics: Dict[str, Any] = {
            "is_trained": False,
            "total_samples": 0,
            "r2_score": 0.0,
            "mae": 0.0,
            "rmse": 0.0,
            "last_trained_at": None,
            "training_data_source": "synthetic_profile_generator",
            "validation_scope": "synthetic_holdout",
            "training_source_counts": {},
            "training_source_weight_totals": {},
            "observed_training_rows": 0,
            "calibration": {},
            "feature_importances": {},
        }
        self._train_baseline_model()

    @staticmethod
    def _context_features(context: SpatialPredictionContext) -> np.ndarray:
        hour_angle = 2.0 * np.pi * context.hour_float / 24.0
        day_angle = 2.0 * np.pi * context.day_of_week / 7.0
        surge_pressure = (
            ferry_surge_factor(context.minutes_until_ferry_departure) - 1.0
        ) / 0.35
        road_class = np.zeros(len(_ROAD_CLASSES), dtype=float)
        road_class[_ROAD_CLASS_INDEX[context.road_class]] = 1.0
        # Corridors are categorical. A scalar idx/4 encoding invented an
        # ordering and distance relationship between unrelated road areas.
        corridor = np.zeros(len(LEGACY_CORRIDOR_SPATIAL_DEFAULTS), dtype=float)
        corridor[context.corridor_idx] = 1.0
        return np.concatenate((np.array([
            np.sin(hour_angle),
            np.cos(hour_angle),
            np.sin(day_angle),
            np.cos(day_angle),
            float(context.weather),
            surge_pressure,
        ], dtype=float), corridor, road_class, np.array([
            np.log1p(context.capacity_vph) / np.log1p(5_000.0),
            np.log1p(context.terminal_distance_km) / np.log1p(20.0),
            min(1.0, context.free_flow_speed_kph / 120.0),
        ], dtype=float)))

    def _extract_features(
        self,
        hour_float: float,
        is_weekend: int,
        weather: int,
        ferry_surge: int,
        corridor_idx: int,
    ) -> np.ndarray:
        """Backward-compatible feature adapter with declared spatial defaults."""
        return self._context_features(SpatialPredictionContext.from_legacy(
            hour_float, is_weekend, weather, ferry_surge, corridor_idx,
        ))

    @staticmethod
    def _feature_importance_payload(importances: np.ndarray) -> Dict[str, float]:
        corridor_start = 6
        corridor_end = corridor_start + len(LEGACY_CORRIDOR_SPATIAL_DEFAULTS)
        road_start = corridor_end
        road_end = road_start + len(_ROAD_CLASSES)
        return {
            "Time of Day (Cyclical)": round(
                float(importances[0] + importances[1]), 3,
            ),
            "Day of Week (Cyclical)": round(
                float(importances[2] + importances[3]), 3,
            ),
            "Weather Condition": round(float(importances[4]), 3),
            "Ferry Surge Proximity (Post-model Exponential)": round(
                float(importances[FERRY_SURGE_FEATURE_INDEX]), 3,
            ),
            "Corridor Identity (One-hot)": round(
                float(np.sum(importances[corridor_start:corridor_end])), 3,
            ),
            "Road Class (One-hot)": round(
                float(np.sum(importances[road_start:road_end])), 3,
            ),
            "Capacity": round(float(importances[road_end]), 3),
            "Terminal Distance": round(float(importances[road_end + 1]), 3),
            "Free-flow Speed": round(float(importances[road_end + 2]), 3),
        }

    def _tree_predictions(self, features: np.ndarray) -> np.ndarray:
        matrix = np.atleast_2d(features)
        with self._model_lock:
            return np.array([
                estimator.predict(matrix) for estimator in self.model.estimators_
            ], dtype=float)

    def _new_unfitted_model(self) -> RandomForestRegressor:
        """Clone only estimator configuration, never mutable fitted state."""
        with self._model_lock:
            parameters = self.model.get_params(deep=False)
        return RandomForestRegressor(**parameters)

    @staticmethod
    def _calibration_metrics(
        y_true: np.ndarray,
        tree_predictions: np.ndarray,
        *,
        holdout_order: str,
    ) -> Dict[str, Any]:
        means = np.mean(tree_predictions, axis=0)
        p10 = np.quantile(tree_predictions, 0.10, axis=0)
        p90 = np.quantile(tree_predictions, 0.90, axis=0)
        coverage = float(np.mean((y_true >= p10) & (y_true <= p90)))
        interval_width = float(np.mean(p90 - p10))

        # Empirical ensemble CRPS approximation:
        # E|X-y| - 0.5 E|X-X'|, with each RF tree an ensemble draw.
        first_term = np.mean(np.abs(tree_predictions - y_true[None, :]), axis=0)
        # For sorted draws x_i, 0.5 E|X-X'| equals
        # sum((2*i-n+1)*x_i) / n^2. This is algebraically identical to the
        # pairwise expression but avoids O(trees^2 * holdout) memory/work.
        sorted_trees = np.sort(tree_predictions, axis=0)
        tree_count = sorted_trees.shape[0]
        coefficients = (
            2.0 * np.arange(tree_count, dtype=float) - tree_count + 1.0
        )[:, None]
        second_term = np.sum(
            coefficients * sorted_trees, axis=0,
        ) / float(tree_count ** 2)
        empirical_crps = float(np.mean(first_term - second_term))
        return {
            "interval": "P10-P90 empirical tree quantiles",
            "interval_coverage_target": CALIBRATION_INTERVAL_TARGET,
            "interval_coverage_observed": round(coverage, 4),
            "mean_interval_width_points": round(interval_width, 3),
            "empirical_crps": round(empirical_crps, 3),
            "empirical_crps_method": (
                "mean|tree_prediction-y| - 0.5*"
                "mean|tree_prediction_i-tree_prediction_j|"
            ),
            "holdout_order": holdout_order,
            "holdout_samples": int(len(y_true)),
            "mean_prediction": round(float(np.mean(means)), 3),
        }

    def _train_baseline_model(self):
        """Generate synthetic Batam-shaped traffic profile and fit baseline."""
        np.random.seed(42)
        n = 4000

        hours = np.random.uniform(0, 24, n)
        # Keep the established synthetic draw sequence stable so adding a
        # feature cannot silently recalibrate all downstream scenario metrics.
        weekends = np.random.choice([0, 1], p=[0.7, 0.3], size=n)
        weathers = np.random.choice([0, 1, 2], p=[0.7, 0.2, 0.1], size=n)
        ferry_surges = np.random.choice([0, 1], p=[0.75, 0.25], size=n)
        corridors = np.random.randint(0, 5, n)
        day_rng = np.random.default_rng(4_242)
        days_of_week = np.where(
            weekends == 1,
            day_rng.integers(5, 7, n),
            day_rng.integers(0, 5, n),
        )

        diurnal = 0.5 * (1 - np.cos(2 * np.pi * (hours - 4) / 24.0))
        base = 12.0 + 20.0 * diurnal

        def bump(h, centre, width):
            return np.exp(-((h - centre) ** 2) / (2 * width * width))

        corridor_factors = np.array([1.25, 1.05, 0.90, 0.65, 0.45])
        c_scale = corridor_factors[corridors]

        peaks = (28.0 * bump(hours, 8, 1.1) + 33.0 * bump(hours, 18, 1.2)) * c_scale
        peaks *= np.where(weekends == 1, 0.45, 1.0)

        congestion = (
            base
            + peaks
            + weathers * 12.0
            + ferry_surges * 15.0
        )
        congestion = np.clip(congestion + np.random.normal(0, 3.0, n), 5, 96)

        X = np.array([
            self._context_features(SpatialPredictionContext.from_legacy(
                float(hours[index]),
                int(weekends[index]),
                int(weathers[index]),
                int(ferry_surges[index]),
                int(corridors[index]),
            ))
            for index in range(n)
        ])
        # Preserve the generated day rather than the representative legacy day.
        for index, day_of_week in enumerate(days_of_week):
            day_angle = 2.0 * np.pi * int(day_of_week) / 7.0
            X[index, 2] = np.sin(day_angle)
            X[index, 3] = np.cos(day_angle)
        
        # Train-test split
        split = int(0.8 * n)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = congestion[:split], congestion[split:]

        with self._model_lock:
            self.model.fit(
                X_train,
                y_train,
                sample_weight=np.full(len(X_train), 0.15),
            )
            self.is_trained = True
            y_pred = self.model.predict(X_test)
            importances = self.model.feature_importances_.copy()
            tree_predictions = np.array([
                estimator.predict(X_test) for estimator in self.model.estimators_
            ], dtype=float)
        # This is a holdout score against the synthetic profile generator, not
        # measured Batam accuracy. Report the computed value exactly; flooring
        # it at 0.85 made a weak model look successful by construction.
        r2 = float(r2_score(y_test, y_pred))
        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        self.metrics = {
            "is_trained": True,
            "total_samples": n,
            "r2_score": round(r2, 4),
            "mae": round(mae, 2),
            "rmse": round(rmse, 2),
            "last_trained_at": datetime.now(timezone.utc).isoformat(),
            "training_data_source": "synthetic_profile_generator",
            "validation_scope": "synthetic_holdout",
            "training_source_counts": {"synthetic_profile_generator": n},
            "training_source_weight_totals": {
                "synthetic_profile_generator": round(n * 0.15, 2),
            },
            "fit_source_counts": {"synthetic_profile_generator": len(X_train)},
            "fit_source_weight_totals": {
                "synthetic_profile_generator": round(len(X_train) * 0.15, 2),
            },
            "holdout_source_counts": {
                "synthetic_profile_generator": len(X_test),
            },
            "observed_training_rows": 0,
            "observed_fit_rows": 0,
            "spatial_feature_scope": (
                "Synthetic corridor-level defaults; not measured edge geometry."
            ),
            "ferry_surge_adjustment": {
                "method": "post_model_exponential_tree_shift",
                "amplitude_points": FERRY_SURGE_AMPLITUDE_POINTS,
                "decay_minutes": FERRY_SURGE_DECAY_MINUTES,
                "reason": (
                    "Typed history does not yet persist ferry proximity; the "
                    "published timetable signal remains a transparent, capped "
                    "post-model adjustment."
                ),
            },
            "calibration": self._calibration_metrics(
                y_test,
                tree_predictions,
                holdout_order="deterministic_generator_order",
            ),
            "feature_importances": self._feature_importance_payload(importances),
        }

    def retrain_from_history(self) -> Dict[str, Any]:
        """Retrain only from validated typed spatial observations."""
        with self._retrain_lock:
            return self._retrain_from_history_locked()

    def retrain_from_observations(
        self,
        records: Iterable[SpatialTrafficObservation],
    ) -> Dict[str, Any]:
        """Fit a bounded, canonical snapshot supplied by a persistence port."""
        materialized = tuple(islice(
            iter(records), MAX_SPATIAL_RETRAIN_SAMPLES + 1,
        ))
        if len(materialized) > MAX_SPATIAL_RETRAIN_SAMPLES:
            raise ValueError(
                f"Spatial retraining is limited to {MAX_SPATIAL_RETRAIN_SAMPLES} rows."
            )
        canonical = tuple(
            validate_spatial_observation(record) for record in materialized
        )
        with self._retrain_lock:
            return self._retrain_from_observations_locked(canonical)

    def _retrain_from_history_locked(self) -> Dict[str, Any]:
        try:
            from services.historical_store import get_spatial_training_dataset
            records = get_spatial_training_dataset()
        except Exception as err:
            print(f"[congestion_model] Failed to load history dataset: {err}")
            return self.metrics

        return self._retrain_from_observations_locked(records)

    def _retrain_from_observations_locked(
        self,
        records: Iterable[SpatialTrafficObservation],
    ) -> Dict[str, Any]:
        records = tuple(records)

        if len(records) < 100:
            return {
                **self.metrics,
                "last_retrain_skipped_reason": (
                    "fewer_than_100_validated_spatial_observations"
                ),
                "candidate_samples": len(records),
            }

        corridor_indexes = {
            f"corridor-{index + 1}": index for index in range(5)
        }
        accepted = [
            item for item in sorted(records, key=lambda item: item.observed_at)
            if item.corridor_id in corridor_indexes
        ]
        if len(accepted) < 100:
            return {
                **self.metrics,
                "last_retrain_skipped_reason": (
                    "fewer_than_100_supported_corridor_observations"
                ),
                "candidate_samples": len(records),
                "supported_samples": len(accepted),
            }

        contexts = [
            SpatialPredictionContext.from_observation(
                item,
                corridor_idx=corridor_indexes[item.corridor_id],
            )
            for item in accepted
        ]
        X_list = [self._context_features(context) for context in contexts]
        y_list = [
            100.0 * (1.0 - min(1.0, max(0.05, item.speed_ratio)))
            for item in accepted
        ]
        sample_weights = np.array([item.confidence for item in accepted])

        X = np.array(X_list)
        y = np.array(y_list)

        n = len(X)
        split = int(0.8 * n)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]
        weights_train = sample_weights[:split]

        # Fit a private candidate. Predictions continue using the last complete
        # model, and a failed fit cannot partially mutate the live estimator.
        candidate_model = self._new_unfitted_model()
        candidate_model.fit(X_train, y_train, sample_weight=weights_train)
        y_pred = candidate_model.predict(X_test)
        importances = candidate_model.feature_importances_.copy()
        tree_predictions = np.array([
            estimator.predict(X_test) for estimator in candidate_model.estimators_
        ], dtype=float)
        
        r2 = float(r2_score(y_test, y_pred))
        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        source_counts = dict(Counter(item.source for item in accepted))
        source_weight_totals = {
            source: round(sum(
                item.confidence for item in accepted if item.source == source
            ), 3)
            for source in sorted(source_counts)
        }
        fit_records = accepted[:split]
        holdout_records = accepted[split:]
        fit_source_counts = dict(Counter(item.source for item in fit_records))
        fit_source_weight_totals = {
            source: round(sum(
                item.confidence for item in fit_records if item.source == source
            ), 3)
            for source in sorted(fit_source_counts)
        }
        holdout_source_counts = dict(Counter(
            item.source for item in holdout_records
        ))
        observed_rows = sum(1 for item in accepted if item.observed)
        observed_fit_rows = sum(1 for item in fit_records if item.observed)
        if observed_rows == n:
            training_data_source = "validated_spatial_observed"
            validation_scope = "chronological_spatial_holdout_observed"
        elif observed_rows:
            training_data_source = "validated_spatial_mixed"
            validation_scope = "chronological_spatial_holdout_mixed"
        else:
            training_data_source = "validated_spatial_modelled"
            validation_scope = "chronological_spatial_holdout_modelled"
        updated_metrics = {
            "is_trained": True,
            "total_samples": n,
            "r2_score": round(r2, 4),
            "mae": round(mae, 2),
            "rmse": round(rmse, 2),
            "last_trained_at": datetime.now(timezone.utc).isoformat(),
            "training_data_source": training_data_source,
            "validation_scope": validation_scope,
            "training_source_counts": source_counts,
            "training_source_weight_totals": source_weight_totals,
            "fit_source_counts": fit_source_counts,
            "fit_source_weight_totals": fit_source_weight_totals,
            "holdout_source_counts": holdout_source_counts,
            "observed_training_rows": observed_rows,
            "observed_fit_rows": observed_fit_rows,
            "spatial_defaults_applied_counts": dict(Counter(
                field for context in contexts for field in context.defaults_applied
            )),
            "spatial_feature_scope": (
                "Validated geocoded observations; absent capacity/terminal "
                "distance fields use declared corridor defaults."
            ),
            "ferry_surge_adjustment": {
                "method": "post_model_exponential_tree_shift",
                "amplitude_points": FERRY_SURGE_AMPLITUDE_POINTS,
                "decay_minutes": FERRY_SURGE_DECAY_MINUTES,
                "reason": (
                    "Typed history does not yet persist ferry proximity; the "
                    "published timetable signal remains a transparent, capped "
                    "post-model adjustment."
                ),
            },
            "calibration": self._calibration_metrics(
                y_test,
                tree_predictions,
                holdout_order="chronological_observed_at",
            ),
            "feature_importances": self._feature_importance_payload(importances),
        }

        # Model and its matching audit metrics become visible as one revision.
        with self._model_lock:
            self.model = candidate_model
            self.metrics = updated_metrics
            self.is_trained = True
        return updated_metrics

    def predict_spatial(
        self,
        context: SpatialPredictionContext,
    ) -> SpatialCongestionPrediction:
        """Return tree-dispersion quantiles and a risk-adjusted route score."""
        features = self._context_features(context)
        # Historical observations do not persist ferry proximity. Always ask
        # the forest for its no-sailing baseline, then apply the same published
        # timetable adjustment regardless of whether the forest is the bundled
        # synthetic baseline or has been retrained from real spatial history.
        features[FERRY_SURGE_FEATURE_INDEX] = 0.0
        raw_tree_predictions = self._tree_predictions(features)[:, 0]
        surge_pressure = (
            ferry_surge_factor(
                context.minutes_until_ferry_departure,
                amplitude=0.35,
                decay_minutes=FERRY_SURGE_DECAY_MINUTES,
            ) - 1.0
        ) / 0.35
        requested_adjustment = FERRY_SURGE_AMPLITUDE_POINTS * surge_pressure
        tree_predictions = np.clip(
            raw_tree_predictions + requested_adjustment,
            0.0,
            100.0,
        )
        mean = float(np.mean(tree_predictions))
        raw_mean = float(np.mean(raw_tree_predictions))
        std = float(np.std(tree_predictions))
        p10 = float(np.quantile(tree_predictions, 0.10))
        p90 = float(np.quantile(tree_predictions, 0.90))
        mean = min(100.0, max(0.0, mean))
        # Empirical quantiles can straddle the arithmetic mean asymmetrically.
        # Published lower/upper risk bounds must still contain the mean, or the
        # derived P90 speed ratio can incorrectly be faster than expectation.
        p10 = min(mean, min(100.0, max(0.0, p10)))
        p90 = max(mean, min(100.0, max(p10, p90)))
        risk_adjusted = min(
            100.0,
            max(0.0, mean + context.risk_aversion * max(0.0, p90 - mean)),
        )
        status = classify_status(mean)
        return SpatialCongestionPrediction(
            mean_score=round(mean, 3),
            std_score=round(std, 3),
            p10_score=round(p10, 3),
            p90_score=round(p90, 3),
            upper_bound_delay_mins=delay_from_score(p90),
            risk_adjusted_score=round(risk_adjusted, 3),
            expected_speed_ratio=round(max(0.05, 1.0 - mean / 100.0), 6),
            speed_ratio_std=round(std / 100.0, 6),
            status=status,
            risk_level=uncertainty_aware_risk(status, std),
            model_id=SPATIAL_ENSEMBLE_MODEL_ID,
            spatial_source=context.spatial_source,
            defaults_applied=context.defaults_applied,
            ferry_surge_adjustment_points=round(
                max(0.0, mean - raw_mean), 3,
            ),
            ferry_surge_adjustment_method=(
                "post_model_exponential_tree_shift_capped_0_100"
            ),
        )

    def _score(self, hour_float: float, is_weekend: int, weather: int,
               ferry_surge: int, corridor_idx: int) -> float:
        context = SpatialPredictionContext.from_legacy(
            hour_float, is_weekend, weather, ferry_surge, corridor_idx,
        )
        return self.predict_spatial(context).mean_score

    def predict(self, hour: int, is_weekend: int, weather: int,
                ferry_surge: int, corridor_idx: int) -> Dict[str, Any]:
        return self.predict_continuous(
            float(hour), is_weekend, weather, ferry_surge, corridor_idx
        )

    def predict_continuous(self, hour_float: float, is_weekend: int, weather: int,
                           ferry_surge: int, corridor_idx: int) -> Dict[str, Any]:
        current = self._score(hour_float, is_weekend, weather, ferry_surge, corridor_idx)
        predicted_30 = self._score(hour_float + 0.5, is_weekend, weather, ferry_surge, corridor_idx)
        predicted_60 = self._score(hour_float + 1.0, is_weekend, weather, ferry_surge, corridor_idx)

        status = classify_status(current)

        return {
            "current_score": round(current, 1),
            "predicted_30min": round(predicted_30, 1),
            "predicted_60min": round(predicted_60, 1),
            "estimated_delay_mins": delay_from_score(current),
            "status": status,
            "risk_level": risk_from_status(status),
            "trend": (
                "UPWARD" if predicted_30 > current + 3
                else "DOWNWARD" if predicted_30 < current - 3
                else "STABLE"
            ),
        }


# Global singleton
forecaster = CongestionForecaster()
