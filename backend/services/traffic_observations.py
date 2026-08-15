"""Typed spatial traffic observations and congestion-cost primitives.

Only numeric, geocoded, timezone-aware observations accepted by this module
may enter the historical congestion pipeline.  Scraped prose and unreviewed
community tips are intentionally not valid training data; a separate review
step must first turn them into a ``reviewed_community_observation``.

The module has no storage or web-framework dependency.  That keeps validation,
idempotency, decay weighting, and penalty formulas reusable by SQLite today and
a shared time-series store later.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Tuple


MAX_INGEST_BATCH_SIZE = 2_000
MAX_ESTIMATOR_HISTORY_SIZE = 100_000
MAX_FUTURE_SKEW_SECONDS = 300
MIN_OBSERVATION_YEAR = 2000
BATAM_TIMEZONE_OFFSET_MINUTES = 7 * 60
# Spatial training is scoped to the Batam road network.  Keeping this broader
# than the current graph extract allows a future audited rebuild without
# silently accepting coordinates from another city or timezone.
BATAM_SPATIAL_BOUNDS = (0.88, 103.75, 1.215, 104.30)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Trust and provenance assigned by source type, never by a caller."""

    provenance: str
    confidence: float
    observed: bool
    review_required: bool = False


SOURCE_POLICIES: Mapping[str, SourcePolicy] = MappingProxyType({
    "loop_sensor": SourcePolicy("verified_sensor", 1.00, True),
    "probe_gps": SourcePolicy("verified_gps", 0.95, True),
    "tomtom_live": SourcePolicy("verified_provider", 0.90, True),
    "verified_traffic_observation": SourcePolicy(
        "verified_observation", 0.85, True,
    ),
    "reviewed_community_observation": SourcePolicy(
        "reviewed_community", 0.55, True, review_required=True,
    ),
    # These are useful cold-start/model inputs but must never be described as
    # measurements.  Their lower weights keep them from overwhelming sensors.
    "modelled": SourcePolicy("modelled", 0.25, False),
    "simulated": SourcePolicy("simulated", 0.20, False),
    "synthetic": SourcePolicy("synthetic", 0.15, False),
})

ROAD_CLASSES = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "track",
    "road", "ferry",
})


class ObservationValidationError(ValueError):
    """An observation cannot safely enter training or history storage."""


class ObservationConflictError(ValueError):
    """An upstream id was replayed with a different immutable payload."""


def source_policy(source: str) -> SourcePolicy:
    """Return the fixed trust policy for a supported source."""
    try:
        return SOURCE_POLICIES[source]
    except KeyError as error:
        supported = ", ".join(sorted(SOURCE_POLICIES))
        raise ObservationValidationError(
            f"Unsupported traffic source {source!r}; expected one of {supported}."
        ) from error


def source_confidence(source: str) -> float:
    """Confidence weight for model fitting and historical decay."""
    return source_policy(source).confidence


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool):
        raise ObservationValidationError(f"{name} must be numeric, not boolean.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ObservationValidationError(f"{name} must be numeric.") from error
    if not math.isfinite(numeric):
        raise ObservationValidationError(f"{name} must be finite.")
    return numeric


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ObservationValidationError("observed_at must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservationValidationError(
            "observed_at must include an explicit timezone offset."
        )
    normalized = value.astimezone(timezone.utc)
    if normalized.year < MIN_OBSERVATION_YEAR:
        raise ObservationValidationError(
            f"observed_at must be in year {MIN_OBSERVATION_YEAR} or later."
        )
    return normalized


def _identifier(name: str, value: str) -> str:
    normalized = str(value).strip()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ObservationValidationError(
            f"{name} must be 1-128 identifier characters."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class SpatialTrafficObservation:
    """One validated, geocoded speed observation.

    ``observation_key`` is deterministic.  When an upstream event id exists,
    its source/corridor identity is the idempotency boundary and a changed
    payload is a conflict.  Otherwise all immutable measurement fields form
    the key, making exact batch replays harmless.
    """

    observation_key: str
    corridor_id: str
    observed_at: datetime
    latitude: float
    longitude: float
    actual_speed_kph: float
    free_flow_speed_kph: float
    source: str
    provenance: str
    confidence: float
    observed: bool
    reviewed: bool
    road_class: str
    capacity_vph: Optional[float]
    terminal_distance_km: Optional[float]
    local_timezone_offset_minutes: int
    upstream_event_id: Optional[str]

    @classmethod
    def create(
        cls,
        *,
        corridor_id: str,
        observed_at: datetime,
        latitude: float,
        longitude: float,
        actual_speed_kph: float,
        free_flow_speed_kph: float,
        source: str,
        reviewed: bool = False,
        road_class: str = "unclassified",
        capacity_vph: Optional[float] = None,
        terminal_distance_km: Optional[float] = None,
        local_timezone_offset_minutes: int = BATAM_TIMEZONE_OFFSET_MINUTES,
        upstream_event_id: Optional[str] = None,
        validation_now: Optional[datetime] = None,
    ) -> "SpatialTrafficObservation":
        corridor = _identifier("corridor_id", corridor_id)
        timestamp = _aware_utc(observed_at)
        effective_now = _aware_utc(validation_now or datetime.now(timezone.utc))
        if (timestamp - effective_now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
            raise ObservationValidationError(
                "observed_at cannot be more than five minutes in the future."
            )

        lat = _finite("latitude", latitude)
        lng = _finite("longitude", longitude)
        if not -90.0 <= lat <= 90.0:
            raise ObservationValidationError("latitude must be between -90 and 90.")
        if not -180.0 <= lng <= 180.0:
            raise ObservationValidationError(
                "longitude must be between -180 and 180."
            )
        south, west, north, east = BATAM_SPATIAL_BOUNDS
        if not (south <= lat < north and west <= lng <= east):
            raise ObservationValidationError(
                "Traffic observation must be inside the supported Batam region."
            )

        actual = _finite("actual_speed_kph", actual_speed_kph)
        free_flow = _finite("free_flow_speed_kph", free_flow_speed_kph)
        if not 0.0 < actual <= 250.0:
            raise ObservationValidationError(
                "actual_speed_kph must be greater than 0 and at most 250."
            )
        if not 0.0 < free_flow <= 250.0:
            raise ObservationValidationError(
                "free_flow_speed_kph must be greater than 0 and at most 250."
            )
        ratio = actual / free_flow
        if not 0.05 <= ratio <= 1.50:
            raise ObservationValidationError(
                "actual/free-flow speed ratio must be between 0.05 and 1.50."
            )

        normalized_source = str(source).strip()
        policy = source_policy(normalized_source)
        if policy.review_required and reviewed is not True:
            raise ObservationValidationError(
                f"{normalized_source} requires explicit human review."
            )

        normalized_road_class = str(road_class).strip().casefold()
        if normalized_road_class not in ROAD_CLASSES:
            raise ObservationValidationError(
                f"Unsupported road_class {road_class!r}."
            )

        capacity = None
        if capacity_vph is not None:
            capacity = _finite("capacity_vph", capacity_vph)
            if not 0.0 < capacity <= 100_000.0:
                raise ObservationValidationError(
                    "capacity_vph must be greater than 0 and at most 100000."
                )

        terminal_distance = None
        if terminal_distance_km is not None:
            terminal_distance = _finite(
                "terminal_distance_km", terminal_distance_km,
            )
            if not 0.0 <= terminal_distance <= 500.0:
                raise ObservationValidationError(
                    "terminal_distance_km must be between 0 and 500."
                )

        if isinstance(local_timezone_offset_minutes, bool) or not isinstance(
            local_timezone_offset_minutes, int,
        ):
            raise ObservationValidationError(
                "local_timezone_offset_minutes must be an integer."
            )
        if local_timezone_offset_minutes != BATAM_TIMEZONE_OFFSET_MINUTES:
            raise ObservationValidationError(
                "Batam traffic observations must use the server-owned WIB "
                "timezone offset (+420 minutes)."
            )

        upstream_id = None
        if upstream_event_id is not None:
            upstream_id = _identifier("upstream_event_id", upstream_event_id)

        canonical_timestamp = timestamp.isoformat(timespec="microseconds")
        if upstream_id is not None:
            identity = (
                f"v1|upstream|{normalized_source}|{corridor}|{upstream_id}"
            )
        else:
            identity = "|".join((
                "v1", corridor, canonical_timestamp, f"{lat:.7f}",
                f"{lng:.7f}", f"{actual:.4f}", f"{free_flow:.4f}",
                normalized_source, normalized_road_class,
                "" if capacity is None else f"{capacity:.3f}",
                "" if terminal_distance is None else f"{terminal_distance:.4f}",
                str(local_timezone_offset_minutes),
                "1" if reviewed else "0",
            ))
        observation_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()

        return cls(
            observation_key=observation_key,
            corridor_id=corridor,
            observed_at=timestamp,
            latitude=lat,
            longitude=lng,
            actual_speed_kph=actual,
            free_flow_speed_kph=free_flow,
            source=normalized_source,
            provenance=policy.provenance,
            confidence=policy.confidence,
            observed=policy.observed,
            reviewed=bool(reviewed),
            road_class=normalized_road_class,
            capacity_vph=capacity,
            terminal_distance_km=terminal_distance,
            local_timezone_offset_minutes=local_timezone_offset_minutes,
            upstream_event_id=upstream_id,
        )

    @property
    def speed_ratio(self) -> float:
        return self.actual_speed_kph / self.free_flow_speed_kph

    @property
    def timestamp_epoch_us(self) -> int:
        return int(round(self.observed_at.timestamp() * 1_000_000))

    def immutable_payload(self) -> Tuple[object, ...]:
        """Tuple used to reject same-key/different-payload replays."""
        return (
            self.corridor_id, self.timestamp_epoch_us, self.latitude,
            self.longitude, self.actual_speed_kph, self.free_flow_speed_kph,
            self.source, self.provenance, self.confidence, self.observed,
            self.reviewed, self.road_class, self.capacity_vph,
            self.terminal_distance_km, self.local_timezone_offset_minutes,
            self.upstream_event_id,
        )

    @property
    def local_observed_at(self) -> datetime:
        """Observation timestamp in its explicitly persisted civil timezone."""
        return self.observed_at.astimezone(timezone(timedelta(
            minutes=self.local_timezone_offset_minutes,
        )))


def validate_spatial_observation(
    observation: SpatialTrafficObservation,
) -> SpatialTrafficObservation:
    """Rebuild and authenticate one observation at the ingestion boundary.

    ``SpatialTrafficObservation`` remains a plain transport dataclass so rows
    can be reconstructed without hidden I/O. Callers are therefore not trusted
    merely because they supplied an instance: every value is run through the
    factory again, policy-controlled provenance fields are compared, and the
    deterministic key is recomputed. The returned value is the canonical copy
    that storage may safely persist.

    Wall-clock future/retention checks remain the store's responsibility,
    because only that boundary has the authoritative ingestion time.
    """
    if not isinstance(observation, SpatialTrafficObservation):
        raise ObservationValidationError(
            "Every batch item must be a SpatialTrafficObservation."
        )
    if not isinstance(observation.observed, bool):
        raise ObservationValidationError("observed must be a boolean policy value.")
    if not isinstance(observation.reviewed, bool):
        raise ObservationValidationError("reviewed must be a boolean policy value.")

    canonical = SpatialTrafficObservation.create(
        corridor_id=observation.corridor_id,
        observed_at=observation.observed_at,
        latitude=observation.latitude,
        longitude=observation.longitude,
        actual_speed_kph=observation.actual_speed_kph,
        free_flow_speed_kph=observation.free_flow_speed_kph,
        source=observation.source,
        reviewed=observation.reviewed,
        road_class=observation.road_class,
        capacity_vph=observation.capacity_vph,
        terminal_distance_km=observation.terminal_distance_km,
        local_timezone_offset_minutes=(
            observation.local_timezone_offset_minutes
        ),
        upstream_event_id=observation.upstream_event_id,
        # This validates awareness/year while deferring clock-relative future
        # skew to HistoryStore.ingest_spatial_batch(now=...).
        validation_now=observation.observed_at,
    )
    if observation.observation_key != canonical.observation_key:
        raise ObservationValidationError(
            "observation_key does not match the deterministic canonical identity."
        )
    if observation.immutable_payload() != canonical.immutable_payload():
        raise ObservationValidationError(
            "Observation contains noncanonical or forged provenance fields."
        )
    return canonical


def validate_observation_batch(
    observations: Iterable[SpatialTrafficObservation],
    *,
    max_size: int = MAX_INGEST_BATCH_SIZE,
) -> Tuple[SpatialTrafficObservation, ...]:
    """Materialize a bounded batch and reject key conflicts within it."""
    if not isinstance(max_size, int) or not 1 <= max_size <= MAX_INGEST_BATCH_SIZE:
        raise ObservationValidationError(
            f"max_size must be between 1 and {MAX_INGEST_BATCH_SIZE}."
        )
    # Consume at most one item beyond the accepted limit. This keeps validation
    # safe for streams and infinite generators while still detecting overflow
    # before inspecting or hashing any item payloads.
    materialized = tuple(islice(iter(observations), max_size + 1))
    if not materialized:
        raise ObservationValidationError("Observation batch must not be empty.")
    if len(materialized) > max_size:
        raise ObservationValidationError(
            f"Observation batch exceeds the {max_size}-record limit."
        )

    canonical_batch = []
    by_key: Dict[str, SpatialTrafficObservation] = {}
    for observation in materialized:
        canonical = validate_spatial_observation(observation)
        previous = by_key.get(canonical.observation_key)
        if previous is not None and (
            previous.immutable_payload() != canonical.immutable_payload()
        ):
            raise ObservationConflictError(
                f"Conflicting payloads for {canonical.observation_key}."
            )
        by_key[canonical.observation_key] = canonical
        canonical_batch.append(canonical)
    return tuple(canonical_batch)


@dataclass(frozen=True, slots=True)
class CongestionEstimatorConfig:
    """Decay controls for comparable hour/day historical observations."""

    history_half_life_days: float = 45.0
    hour_distance_scale: float = 2.5
    weekday_distance_scale: float = 1.5
    minimum_history_weight: float = 1e-9

    def __post_init__(self) -> None:
        values = (
            self.history_half_life_days, self.hour_distance_scale,
            self.weekday_distance_scale, self.minimum_history_weight,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("All congestion estimator decay values must be positive.")


@dataclass(frozen=True, slots=True)
class SpatialBatchIngestionResult:
    """Outcome of one atomic idempotent history write."""

    received: int
    unique: int
    inserted: int
    duplicates: int
    observation_keys: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpeedRatioCongestionEstimate:
    """Strict provider-neutral estimate consumed by routing orchestration."""

    expected_speed_ratio: float
    current_speed_ratio: float
    historical_speed_ratio: Optional[float]
    speed_ratio_std: float
    congestion_score: float
    history_sample_count: int
    observed_history_count: int
    source_counts: Mapping[str, int]
    source_weight_totals: Mapping[str, float]
    provenance: str

    def router_payload(self, *, free_flow_travel_seconds: float) -> Dict[str, object]:
        """Map to ``router.EdgeCongestionEstimate`` field semantics."""
        seconds = _finite("free_flow_travel_seconds", free_flow_travel_seconds)
        if seconds < 0.0:
            raise ValueError("free_flow_travel_seconds cannot be negative.")
        p90_slow_ratio = max(
            0.05,
            self.expected_speed_ratio - 1.2815515655446004 * self.speed_ratio_std,
        )
        upper_delay = seconds * (1.0 / p90_slow_ratio - 1.0)
        return {
            "expected_speed_ratio": min(1.0, self.expected_speed_ratio),
            "speed_ratio_std": self.speed_ratio_std,
            "p90_speed_ratio": min(1.0, p90_slow_ratio),
            "p90_delay_s": max(0.0, upper_delay),
            "source": self.provenance,
        }


def _circular_distance(left: float, right: float, period: float) -> float:
    direct = abs(left - right) % period
    return min(direct, period - direct)


def estimate_speed_ratio_congestion(
    current: SpatialTrafficObservation,
    history: Iterable[SpatialTrafficObservation],
    *,
    config: CongestionEstimatorConfig = CongestionEstimatorConfig(),
    max_history_size: int = MAX_ESTIMATOR_HISTORY_SIZE,
) -> SpeedRatioCongestionEstimate:
    """Blend current Vactual/Vfree-flow with decayed hour/day history.

    History is limited to the same corridor at or before the current sample.
    Weight decays independently by age, circular hour-of-day distance, and
    circular day-of-week distance, then by the fixed source confidence.
    """
    if (
        not isinstance(max_history_size, int)
        or isinstance(max_history_size, bool)
        or not 1 <= max_history_size <= MAX_ESTIMATOR_HISTORY_SIZE
    ):
        raise ObservationValidationError(
            "max_history_size must be between 1 and "
            f"{MAX_ESTIMATOR_HISTORY_SIZE}."
        )
    canonical_current = validate_spatial_observation(current)
    current_ratio = min(1.0, max(0.05, canonical_current.speed_ratio))
    current_local = canonical_current.local_observed_at
    weighted: list[Tuple[float, float, str, bool]] = []
    source_counts: Dict[str, int] = {}
    source_weights: Dict[str, float] = {}

    bounded_history = islice(iter(history), max_history_size + 1)
    for history_index, sample in enumerate(bounded_history):
        if history_index == max_history_size:
            raise ObservationValidationError(
                "Congestion history exceeds the "
                f"{max_history_size}-record estimator limit."
            )
        canonical_sample = validate_spatial_observation(sample)
        if canonical_sample.corridor_id != canonical_current.corridor_id:
            continue
        age_seconds = (
            canonical_current.observed_at - canonical_sample.observed_at
        ).total_seconds()
        if (
            age_seconds < 0.0
            or canonical_sample.observation_key == canonical_current.observation_key
        ):
            continue
        age_days = age_seconds / 86_400.0
        sample_local = canonical_sample.local_observed_at
        hour_distance = _circular_distance(
            current_local.hour + current_local.minute / 60.0,
            sample_local.hour + sample_local.minute / 60.0,
            24.0,
        )
        weekday_distance = _circular_distance(
            float(current_local.weekday()), float(sample_local.weekday()), 7.0,
        )
        weight = canonical_sample.confidence
        weight *= math.exp(
            -math.log(2.0) * age_days / config.history_half_life_days
        )
        weight *= math.exp(-hour_distance / config.hour_distance_scale)
        weight *= math.exp(-weekday_distance / config.weekday_distance_scale)
        if weight < config.minimum_history_weight:
            continue
        ratio = min(1.0, max(0.05, canonical_sample.speed_ratio))
        weighted.append((
            ratio, weight, canonical_sample.source, canonical_sample.observed,
        ))
        source_counts[canonical_sample.source] = (
            source_counts.get(canonical_sample.source, 0) + 1
        )
        source_weights[canonical_sample.source] = (
            source_weights.get(canonical_sample.source, 0.0) + weight
        )

    if weighted:
        total_weight = sum(weight for _, weight, _, _ in weighted)
        historical_ratio = sum(
            ratio * weight for ratio, weight, _, _ in weighted
        ) / total_weight
        variance = sum(
            weight * (ratio - historical_ratio) ** 2
            for ratio, weight, _, _ in weighted
        ) / total_weight
        history_blend = min(0.65, 0.25 + total_weight / (total_weight + 8.0))
        # More trusted current observations should displace history faster.
        history_blend *= 1.0 - 0.35 * canonical_current.confidence
        expected_ratio = (
            (1.0 - history_blend) * current_ratio
            + history_blend * historical_ratio
        )
        speed_ratio_std = math.sqrt(max(0.0, variance))
    else:
        historical_ratio = None
        expected_ratio = current_ratio
        speed_ratio_std = 0.0

    observed_count = sum(1 for _, _, _, observed in weighted if observed)
    includes_reviewed_community = (
        canonical_current.source == "reviewed_community_observation"
        or source_counts.get("reviewed_community_observation", 0) > 0
    )
    if canonical_current.observed and observed_count == len(weighted):
        provenance = (
            "observational_includes_reviewed_community"
            if includes_reviewed_community
            else "verified_observed"
        )
    elif canonical_current.observed or observed_count:
        provenance = "mixed_observed_and_modelled"
    else:
        provenance = "modelled"

    return SpeedRatioCongestionEstimate(
        expected_speed_ratio=round(expected_ratio, 6),
        current_speed_ratio=round(current_ratio, 6),
        historical_speed_ratio=(
            round(historical_ratio, 6) if historical_ratio is not None else None
        ),
        speed_ratio_std=round(speed_ratio_std, 6),
        congestion_score=round(100.0 * (1.0 - expected_ratio), 2),
        history_sample_count=len(weighted),
        observed_history_count=observed_count,
        source_counts=MappingProxyType(dict(sorted(source_counts.items()))),
        source_weight_totals=MappingProxyType({
            source: round(weight, 6)
            for source, weight in sorted(source_weights.items())
        }),
        provenance=provenance,
    )


def peak_penalty(when: datetime, *, strength: float = 0.65) -> float:
    """Return a nonlinear peak-hour travel-time multiplier (at least 1)."""
    aware = _aware_utc(when).astimezone(when.tzinfo)
    strength = _finite("strength", strength)
    if strength < 0.0:
        raise ValueError("strength cannot be negative.")
    hour = aware.hour + aware.minute / 60.0
    morning = math.exp(-((hour - 8.0) ** 2) / (2.0 * 1.15 ** 2))
    evening = math.exp(-((hour - 18.0) ** 2) / (2.0 * 1.30 ** 2))
    intensity = min(1.0, morning + evening)
    if aware.weekday() >= 5:
        intensity *= 0.45
    return 1.0 + strength * intensity ** 2


def border_crossing_penalty(
    queue_minutes: float,
    *,
    utilization_ratio: float = 0.0,
) -> float:
    """Return nonlinear border delay minutes as utilization approaches 1+."""
    queue = _finite("queue_minutes", queue_minutes)
    utilization = _finite("utilization_ratio", utilization_ratio)
    if queue < 0.0 or utilization < 0.0:
        raise ValueError("Border queue and utilization cannot be negative.")
    saturation = min(2.0, utilization)
    return queue * (1.0 + 1.5 * saturation ** 2)


def ferry_wait_penalty(
    wait_minutes: float,
    *,
    missed_boarding_cutoff: bool = False,
) -> float:
    """Return convex wait cost, including a missed-connection disutility."""
    wait = _finite("wait_minutes", wait_minutes)
    if wait < 0.0:
        raise ValueError("Ferry wait cannot be negative.")
    missed_connection = 30.0 if missed_boarding_cutoff else 0.0
    return wait + wait ** 2 / 120.0 + missed_connection


def ferry_surge_factor(
    minutes_until_departure: Optional[float],
    *,
    amplitude: float = 0.35,
    decay_minutes: float = 45.0,
) -> float:
    """Exponential terminal-approach multiplier, 1 when no sailing applies."""
    if minutes_until_departure is None:
        return 1.0
    minutes = _finite("minutes_until_departure", minutes_until_departure)
    amplitude = _finite("amplitude", amplitude)
    decay = _finite("decay_minutes", decay_minutes)
    if minutes < 0.0 or amplitude < 0.0 or decay <= 0.0:
        raise ValueError("Ferry surge inputs must be nonnegative with positive decay.")
    return 1.0 + amplitude * math.exp(-minutes / decay)
