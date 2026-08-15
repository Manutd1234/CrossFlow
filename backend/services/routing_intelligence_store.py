"""Durable, server-only Supabase adapters for routing intelligence.

Every Data API call uses :mod:`services.supabase_server`; anonymous/browser
keys are never accepted. Candidate tips remain inactive. The only promotion
operation starts from a persisted candidate id and an identified reviewer,
then an atomic database function locks and approves the exact stored payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

from services import supabase_server
from services.service_contracts import (
    ApprovedGraphOverride,
    ApprovedGraphOverrideSnapshot,
    promote_reviewed_candidate,
)
from services.shortcut_ingestion import (
    CANONICAL_VEHICLE_MODES,
    DEFAULT_MAX_ENDPOINT_SNAP_M,
    GraphOverrideRecord,
    ProvenanceRecord,
    REVIEW_REQUIRED,
    SCHEMA_VERSION,
    ShortcutIngestionError,
    MAX_PROVENANCE_RECORDS_PER_CANDIDATE,
    haversine_m,
)
from services.traffic_observations import (
    MAX_FUTURE_SKEW_SECONDS,
    SpatialBatchIngestionResult,
    SpatialTrafficObservation,
    validate_observation_batch,
    validate_spatial_observation,
)


SPATIAL_HISTORY_RETENTION_DAYS = 5 * 365
MAX_SPATIAL_TRAINING_ROWS = 100_000
MAX_APPROVED_OVERRIDES_PER_GRAPH = 10_000
DEFAULT_CANDIDATE_PAGE_SIZE = 16
APPROVED_SNAPSHOT_PAGE_SIZE = 256
MAX_CANDIDATE_JSON_BYTES = 64 * 1024
MAX_ATOMIC_CANDIDATE_BATCH_BYTES = (
    supabase_server.DEFAULT_MAX_REQUEST_BYTES - 64 * 1024
)
DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS = 30.0
APPROVED_SNAPSHOT_TIMEOUT_SECONDS = 8.0
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
SHARED_STORE_MODE_ENV = "CROSSFLOW_ROUTING_INTELLIGENCE_STORE"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OVERRIDE_ID = re.compile(r"^shortcut-[0-9a-f]{32}$")
_REQUIRED_FLAGS = frozenset({
    "SOURCE_CONTRACT_VALID",
    "COORDINATES_NORMALIZED_WGS84",
    "GRAPH_BOUNDS_VALID",
    "ENDPOINTS_SNAPPED",
    "ROAD_PLAUSIBILITY_VALID",
    "UNTRUSTED_CROWD_CLAIM",
})


class RoutingIntelligenceStoreUnavailable(RuntimeError):
    """The optional shared routing-intelligence store did not answer safely."""


class RoutingIntelligenceStoreConflict(ShortcutIngestionError):
    """A durable mutation was rejected as a permanent domain conflict."""

    def __init__(self, code: str = "routing_intelligence_conflict") -> None:
        super().__init__(code)
        self.code = code


class _OperationBudget:
    """One monotonic deadline shared by every REST call in an operation."""

    def __init__(self, timeout_seconds: float) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.1 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("operation timeout must be between 0.1 and 30 seconds")
        self._deadline = time.monotonic() + float(timeout_seconds)

    def request_timeout(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining < 0.1:
            raise RoutingIntelligenceStoreUnavailable(
                "operation_deadline_exceeded",
            )
        return min(float(supabase_server.DEFAULT_TIMEOUT_SECONDS), remaining)


def _store_request(
    budget: _OperationBudget,
    *,
    mutation_conflicts: bool = False,
    **request: Any,
) -> Any:
    """Issue one safe call and preserve permanent-vs-transient semantics."""
    try:
        return supabase_server.request_json(
            _config(),
            timeout_seconds=budget.request_timeout(),
            **request,
        )
    except supabase_server.SupabaseServerError as error:
        if mutation_conflicts and error.code in {
            "http_400", "http_409", "http_422",
        }:
            raise RoutingIntelligenceStoreConflict() from None
        raise RoutingIntelligenceStoreUnavailable(error.code) from None


def configured() -> bool:
    """Return whether valid server credentials exist (not schema readiness)."""
    return supabase_server.load_server_config() is not None


def shared_store_enabled() -> bool:
    """Require an explicit opt-in before touching the optional schema."""
    return (
        os.environ.get(SHARED_STORE_MODE_ENV, "local").strip().casefold()
        == "supabase"
    )


def shared_store_ready() -> bool:
    return shared_store_enabled() and configured()


def _config() -> supabase_server.SupabaseServerConfig:
    if not shared_store_enabled():
        raise RoutingIntelligenceStoreUnavailable("shared_store_not_enabled")
    config = supabase_server.load_server_config()
    if config is None:
        raise RoutingIntelligenceStoreUnavailable("shared_store_not_configured")
    return config


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _observation_payload(item: SpatialTrafficObservation) -> dict[str, Any]:
    return {
        "observation_key": item.observation_key,
        "observed_at": item.observed_at.isoformat(timespec="microseconds"),
        "corridor_id": item.corridor_id,
        "latitude": item.latitude,
        "longitude": item.longitude,
        "actual_speed_kph": item.actual_speed_kph,
        "free_flow_speed_kph": item.free_flow_speed_kph,
        "source": item.source,
        "provenance": item.provenance,
        "confidence": item.confidence,
        "observed": item.observed,
        "reviewed": item.reviewed,
        "road_class": item.road_class,
        "capacity_vph": item.capacity_vph,
        "terminal_distance_km": item.terminal_distance_km,
        "local_timezone_offset_minutes": item.local_timezone_offset_minutes,
        "upstream_event_id": item.upstream_event_id,
    }


def _spatial_from_payload(payload: Any) -> SpatialTrafficObservation:
    if not isinstance(payload, dict):
        raise ValueError("spatial payload must be an object")
    try:
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        record = SpatialTrafficObservation(
            observation_key=str(payload["observation_key"]),
            corridor_id=str(payload["corridor_id"]),
            observed_at=observed_at,
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            actual_speed_kph=float(payload["actual_speed_kph"]),
            free_flow_speed_kph=float(payload["free_flow_speed_kph"]),
            source=str(payload["source"]),
            provenance=str(payload["provenance"]),
            confidence=float(payload["confidence"]),
            observed=payload["observed"],
            reviewed=payload["reviewed"],
            road_class=str(payload["road_class"]),
            capacity_vph=(
                float(payload["capacity_vph"])
                if payload.get("capacity_vph") is not None else None
            ),
            terminal_distance_km=(
                float(payload["terminal_distance_km"])
                if payload.get("terminal_distance_km") is not None else None
            ),
            local_timezone_offset_minutes=int(
                payload["local_timezone_offset_minutes"]
            ),
            upstream_event_id=(
                str(payload["upstream_event_id"])
                if payload.get("upstream_event_id") is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid spatial payload") from error
    return validate_spatial_observation(record)


def _candidate_identity(
    graph_revision: str,
    source_node: int,
    target_node: int,
    modes: Sequence[str],
    geometry: Sequence[tuple[float, float]],
) -> str:
    geometry_canonical = json.dumps(
        [[round(lat, 6), round(lng, 6)] for lat, lng in geometry],
        separators=(",", ":"),
    )
    canonical = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "graph_revision": graph_revision,
        "source_node": source_node,
        "target_node": target_node,
        "vehicle_modes": sorted(modes),
        "geometry_sha256": hashlib.sha256(
            geometry_canonical.encode("utf-8")
        ).hexdigest(),
    }, sort_keys=True, separators=(",", ":"))
    return "shortcut-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _candidate_from_payload(payload: Any) -> GraphOverrideRecord:
    """Strictly reconstruct an inactive candidate from an untrusted JSON row."""
    if not isinstance(payload, dict):
        raise ValueError("candidate payload must be an object")
    try:
        encoded_size = len(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("candidate payload is not JSON serializable") from error
    if encoded_size > MAX_CANDIDATE_JSON_BYTES:
        raise ValueError("candidate payload exceeds its resource bound")
    expected_keys = {
        "schema_version", "override_id", "graph_revision", "source_node",
        "target_node", "geometry", "applicable_vehicle_modes",
        "geometry_distance_m", "claimed_distance_m", "claimed_duration_s",
        "road_quality", "road_quality_is_default", "confidence",
        "endpoint_snap_m", "provenance", "validation_flags", "review_state",
        "activation_allowed",
    }
    if set(payload) != expected_keys:
        raise ValueError("candidate payload fields are invalid")
    try:
        geometry = tuple(
            (float(point[0]), float(point[1])) for point in payload["geometry"]
        )
        modes = tuple(str(mode) for mode in payload["applicable_vehicle_modes"])
        provenance_values = tuple(ProvenanceRecord(
            source_id=str(item["source_id"]),
            source_url=str(item["source_url"]),
            document_id=str(item["document_id"]),
            retrieved_at=str(item["retrieved_at"]),
            content_sha256=str(item["content_sha256"]),
            source_tip_id=str(item["source_tip_id"]),
            parser=str(item["parser"]),
            excerpt_sha256=str(item["excerpt_sha256"]),
            excerpt=str(item["excerpt"]),
        ) for item in payload["provenance"])
        if len(set(provenance_values)) != len(provenance_values):
            raise ValueError("candidate provenance contains duplicates")
        # Storage aggregation has its own deterministic JSON ordering. Domain
        # canonicalization is defined here by ProvenanceRecord field order.
        provenance = tuple(sorted(provenance_values))
        record = GraphOverrideRecord(
            schema_version=int(payload["schema_version"]),
            override_id=str(payload["override_id"]),
            graph_revision=str(payload["graph_revision"]),
            source_node=int(payload["source_node"]),
            target_node=int(payload["target_node"]),
            geometry=geometry,
            applicable_vehicle_modes=modes,
            geometry_distance_m=float(payload["geometry_distance_m"]),
            claimed_distance_m=(
                float(payload["claimed_distance_m"])
                if payload["claimed_distance_m"] is not None else None
            ),
            claimed_duration_s=(
                float(payload["claimed_duration_s"])
                if payload["claimed_duration_s"] is not None else None
            ),
            road_quality=float(payload["road_quality"]),
            road_quality_is_default=payload["road_quality_is_default"],
            confidence=float(payload["confidence"]),
            endpoint_snap_m=tuple(float(value) for value in payload["endpoint_snap_m"]),
            provenance=provenance,
            validation_flags=tuple(str(flag) for flag in payload["validation_flags"]),
            review_state=str(payload["review_state"]),
            activation_allowed=payload["activation_allowed"],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid candidate payload") from error

    numbers = (
        record.geometry_distance_m, record.road_quality, record.confidence,
        *record.endpoint_snap_m,
    )
    if record.claimed_distance_m is not None:
        numbers += (record.claimed_distance_m,)
    if record.claimed_duration_s is not None:
        numbers += (record.claimed_duration_s,)
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("candidate numeric fields must be finite")
    if (
        record.schema_version != SCHEMA_VERSION
        or not _OVERRIDE_ID.fullmatch(record.override_id)
        or not _DIGEST.fullmatch(record.graph_revision)
        or record.source_node == record.target_node
        or not 2 <= len(record.geometry) <= 64
        or any(
            not -90.0 <= point[0] <= 90.0
            or not -180.0 <= point[1] <= 180.0
            for point in record.geometry
        )
        or tuple(sorted(set(record.applicable_vehicle_modes)))
        != record.applicable_vehicle_modes
        or any(mode not in CANONICAL_VEHICLE_MODES for mode in record.applicable_vehicle_modes)
        or record.review_state != REVIEW_REQUIRED
        or record.activation_allowed is not False
        or not isinstance(record.road_quality_is_default, bool)
        or not 0.0 <= record.road_quality <= 1.0
        or not 0.0 <= record.confidence < 1.0
        or len(record.endpoint_snap_m) != 2
        or any(not 0.0 <= value <= DEFAULT_MAX_ENDPOINT_SNAP_M
               for value in record.endpoint_snap_m)
        or not record.provenance
        or len(record.provenance) > MAX_PROVENANCE_RECORDS_PER_CANDIDATE
        or not _REQUIRED_FLAGS.issubset(record.validation_flags)
    ):
        raise ValueError("candidate invariants are invalid")
    geometry_distance = sum(
        haversine_m(first, second)
        for first, second in zip(record.geometry, record.geometry[1:])
    )
    if not math.isclose(
        record.geometry_distance_m, geometry_distance, abs_tol=0.01,
    ):
        raise ValueError("candidate geometry distance is forged")
    effective_distance = record.claimed_distance_m or record.geometry_distance_m
    if record.claimed_distance_m is not None and not (
        0.75 <= record.claimed_distance_m / geometry_distance <= 4.0
    ):
        raise ValueError("candidate claimed distance is implausible")
    if record.claimed_duration_s is not None:
        implied_kph = effective_distance / record.claimed_duration_s * 3.6
        maximum = 90.0 if modes == ("FERRY_MARITIME",) else 130.0
        if not 1.0 <= implied_kph <= maximum:
            raise ValueError("candidate duration is implausible")
    if _candidate_identity(
        record.graph_revision, record.source_node, record.target_node,
        record.applicable_vehicle_modes, record.geometry,
    ) != record.override_id:
        raise ValueError("candidate identity does not match its immutable fields")
    for item in record.provenance:
        parsed = urlparse(item.source_url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or not _DIGEST.fullmatch(item.content_sha256)
            or not _DIGEST.fullmatch(item.excerpt_sha256)
            or not item.source_id or not item.document_id or not item.source_tip_id
            or not item.parser or not item.excerpt
        ):
            raise ValueError("candidate provenance is invalid")
        retrieved = datetime.fromisoformat(item.retrieved_at)
        _aware_utc(retrieved, "retrieved_at")
    return record


def _candidate_digest(record: GraphOverrideRecord) -> str:
    canonical = json.dumps(
        record.to_dict(), sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_candidate_upsert_summary(
    response: Any,
    expected_ids: Sequence[str],
) -> tuple[bool, ...]:
    """Validate the RPC's single bounded row and exact ordered identities."""
    if (
        not isinstance(response, list)
        or len(response) != 1
        or not isinstance(response[0], dict)
    ):
        raise ValueError("candidate summary envelope is invalid")
    summary = response[0]
    expected_keys = {
        "received", "inserted_count", "existing_count",
        "first_queue_revision", "last_queue_revision", "results",
    }
    if set(summary) != expected_keys or not isinstance(summary["results"], list):
        raise ValueError("candidate summary fields are invalid")
    received = summary["received"]
    inserted_count = summary["inserted_count"]
    existing_count = summary["existing_count"]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (
        received, inserted_count, existing_count,
        summary["first_queue_revision"], summary["last_queue_revision"],
    )):
        raise ValueError("candidate summary counters are invalid")
    items = summary["results"]
    if received != len(expected_ids) or len(items) != received:
        raise ValueError("candidate summary count is invalid")
    flags: list[bool] = []
    revisions: list[int] = []
    returned_ids: list[str] = []
    item_keys = {
        "override_id", "inserted", "queue_revision", "review_state",
        "candidate_sha256",
    }
    for item in items:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise ValueError("candidate summary result is invalid")
        override_id = item["override_id"]
        inserted = item["inserted"]
        revision = item["queue_revision"]
        state = item["review_state"]
        digest = item["candidate_sha256"]
        if (
            not isinstance(override_id, str)
            or not _OVERRIDE_ID.fullmatch(override_id)
            or not isinstance(inserted, bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
            or state not in {REVIEW_REQUIRED, "APPROVED_ARCHIVED"}
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise ValueError("candidate summary result values are invalid")
        returned_ids.append(override_id)
        flags.append(inserted)
        revisions.append(revision)
    if (
        returned_ids != list(expected_ids)
        or len(set(returned_ids)) != len(returned_ids)
        or len(set(revisions)) != len(revisions)
        or inserted_count != sum(flags)
        or existing_count != received - inserted_count
        or summary["first_queue_revision"] != min(revisions)
        or summary["last_queue_revision"] != max(revisions)
    ):
        raise ValueError("candidate summary does not bind the requested batch")
    return tuple(flags)


def _approved_payload(item: ApprovedGraphOverride) -> dict[str, Any]:
    return {
        "override_id": item.override_id,
        "graph_revision": item.graph_revision,
        "source_node": item.source_node,
        "target_node": item.target_node,
        "geometry": [list(point) for point in item.geometry],
        "applicable_vehicle_modes": list(item.applicable_vehicle_modes),
        "road_quality": item.road_quality,
        "distance_m": item.distance_m,
        "duration_s": item.duration_s,
        "approved_by": item.approved_by,
        "approved_at": item.approved_at.isoformat(),
        "candidate_sha256": item.candidate_sha256,
    }


def _approved_from_row(
    row: Any,
    expected_graph_revision: str,
    *,
    require_frozen_candidate: bool = True,
) -> ApprovedGraphOverride:
    if not isinstance(row, dict) or not isinstance(row.get("approved_payload"), dict):
        raise ValueError("invalid approved row")
    payload = row["approved_payload"]
    try:
        approved = ApprovedGraphOverride(
            override_id=str(payload["override_id"]),
            graph_revision=str(payload["graph_revision"]),
            source_node=int(payload["source_node"]),
            target_node=int(payload["target_node"]),
            geometry=tuple(
                (float(point[0]), float(point[1])) for point in payload["geometry"]
            ),
            applicable_vehicle_modes=tuple(
                str(mode) for mode in payload["applicable_vehicle_modes"]
            ),
            road_quality=float(payload["road_quality"]),
            distance_m=float(payload["distance_m"]),
            duration_s=(
                float(payload["duration_s"])
                if payload.get("duration_s") is not None else None
            ),
            approved_by=str(payload["approved_by"]),
            approved_at=datetime.fromisoformat(str(payload["approved_at"])),
            candidate_sha256=str(payload["candidate_sha256"]),
        )
        revision = int(row["override_revision"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid approved row") from error
    if (
        revision <= 0
        or approved.graph_revision != expected_graph_revision
        or str(row.get("graph_revision")) != approved.graph_revision
        or str(row.get("override_id")) != approved.override_id
        or str(row.get("candidate_sha256")) != approved.candidate_sha256
        or str(row.get("approved_by")) != approved.approved_by
        or datetime.fromisoformat(str(row.get("approved_at"))) != approved.approved_at
    ):
        raise ValueError("approved row audit columns do not match its payload")
    if not require_frozen_candidate:
        # Runtime snapshots read compact rows. Direct approval-table mutation is
        # denied by SQL grants; only the derivation-checking approval RPC can
        # create one. Exact candidate replays still use the strict branch below.
        return approved
    frozen_candidate = _candidate_from_payload(row.get("candidate_payload"))
    if (
        frozen_candidate.override_id != approved.override_id
        or frozen_candidate.graph_revision != approved.graph_revision
        or _candidate_digest(frozen_candidate) != approved.candidate_sha256
        or frozen_candidate.source_node != approved.source_node
        or frozen_candidate.target_node != approved.target_node
        or frozen_candidate.geometry != approved.geometry
        or frozen_candidate.applicable_vehicle_modes
        != approved.applicable_vehicle_modes
        or frozen_candidate.road_quality != approved.road_quality
        or (frozen_candidate.claimed_distance_m
            or frozen_candidate.geometry_distance_m) != approved.distance_m
        or frozen_candidate.claimed_duration_s != approved.duration_s
    ):
        raise ValueError("approval does not match its frozen candidate")
    return approved


class SupabaseRoutingIntelligenceStore:
    """Observation store, inactive review queue and approved snapshot port."""

    def __init__(self, *, approved_cache_ttl_seconds: float = 5.0) -> None:
        if not math.isfinite(approved_cache_ttl_seconds) or not (
            0.1 <= approved_cache_ttl_seconds <= 60.0
        ):
            raise ValueError("approved_cache_ttl_seconds must be 0.1..60")
        self._approved_cache_ttl_seconds = float(approved_cache_ttl_seconds)
        self._approved_cache: dict[
            str, tuple[float, ApprovedGraphOverrideSnapshot]
        ] = {}
        self._cache_lock = threading.RLock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._schema_health = "unknown_not_probed"
        self._schema_health_code: Optional[str] = None
        self._schema_checked_at: Optional[str] = None

    def ingest_spatial_batch(
        self,
        observations: Iterable[SpatialTrafficObservation],
        *,
        now: Optional[datetime] = None,
    ) -> SpatialBatchIngestionResult:
        batch = validate_observation_batch(observations)
        effective_now = _aware_utc(now or datetime.now(timezone.utc), "now")
        cutoff = effective_now - timedelta(days=SPATIAL_HISTORY_RETENTION_DAYS)
        future_limit = effective_now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
        if any(not cutoff <= item.observed_at <= future_limit for item in batch):
            raise ValueError("observations must be within retention and future-skew bounds")
        keys = tuple(sorted({item.observation_key for item in batch}))
        response = _store_request(
            _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS),
            method="POST",
            rest_path="/rest/v1/rpc/crossflow_ingest_spatial_observations",
            payload={"batch": [_observation_payload(item) for item in batch]},
            mutation_conflicts=True,
        )
        if not isinstance(response, list) or len(response) != 1 \
                or not isinstance(response[0], dict):
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
        try:
            row = response[0]
            result = SpatialBatchIngestionResult(
                received=int(row["received"]), unique=int(row["unique_count"]),
                inserted=int(row["inserted"]), duplicates=int(row["duplicates"]),
                observation_keys=keys,
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response") from None
        if (
            result.received != len(batch) or result.unique != len(keys)
            or not 0 <= result.inserted <= result.unique
            or result.duplicates != result.received - result.inserted
        ):
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
        return result

    def get_spatial_training_dataset(
        self,
        *,
        limit: int = MAX_SPATIAL_TRAINING_ROWS,
        page_size: int = 1_000,
    ) -> tuple[SpatialTrafficObservation, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_SPATIAL_TRAINING_ROWS:
            raise ValueError("limit must be 1..100000")
        if isinstance(page_size, bool) or not 1 <= page_size <= 2_000:
            raise ValueError("page_size must be 1..2000")
        budget = _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS)
        watermark_response = _store_request(
            budget,
            method="POST",
            rest_path=(
                "/rest/v1/rpc/crossflow_spatial_training_snapshot"
            ),
            payload={},
        )
        if not isinstance(watermark_response, list) or len(watermark_response) != 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
        try:
            watermark = int(watermark_response[0]["snapshot_revision"])
            cutoff = _aware_utc(
                datetime.fromisoformat(
                    str(watermark_response[0]["cutoff_observed_at"]),
                ),
                "cutoff_observed_at",
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response") from None
        local_now = datetime.now(timezone.utc)
        expected_cutoff = local_now - timedelta(
            days=SPATIAL_HISTORY_RETENTION_DAYS,
        )
        if (
            watermark < 0
            or abs((cutoff - expected_cutoff).total_seconds()) > 86_400
        ):
            raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
        records: list[SpatialTrafficObservation] = []
        cursor_observed_at: Optional[datetime] = None
        cursor_observation_key: Optional[str] = None
        keys: set[str] = set()
        while len(records) < limit and watermark > 0:
            page_limit = min(page_size, limit - len(records))
            response = _store_request(
                budget,
                method="POST",
                rest_path=(
                    "/rest/v1/rpc/"
                    "crossflow_read_spatial_training_page"
                ),
                payload={
                    "p_snapshot_revision": watermark,
                    "p_cutoff_observed_at": cutoff.isoformat(),
                    "p_before_observed_at": (
                        cursor_observed_at.isoformat()
                        if cursor_observed_at is not None else None
                    ),
                    "p_before_observation_key": cursor_observation_key,
                    "p_page_limit": page_limit,
                },
            )
            if not isinstance(response, list) or len(response) > page_limit:
                raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
            try:
                revisions = [int(row["ingestion_revision"]) for row in response]
                row_times = [
                    _aware_utc(
                        datetime.fromisoformat(str(row["observed_at"])),
                        "observed_at",
                    )
                    for row in response
                ]
                row_keys = [str(row["observation_key"]) for row in response]
                page = [_spatial_from_payload(row["immutable_payload"])
                        for row in response]
            except (KeyError, TypeError, ValueError):
                raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response") from None
            if not response:
                break
            order = list(zip(row_times, row_keys))
            if (
                len(set(revisions)) != len(revisions)
                or any(not 0 < revision <= watermark for revision in revisions)
                or order != sorted(set(order), reverse=True)
                or (
                    cursor_observed_at is not None
                    and order[0] >= (
                        cursor_observed_at, cursor_observation_key or "",
                    )
                )
                or any(item.observed_at != row_time
                       or item.observation_key != row_key
                       for item, row_time, row_key
                       in zip(page, row_times, row_keys))
                or any(not cutoff <= item.observed_at <= (
                    local_now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
                ) for item in page)
                or any(item.observation_key in keys for item in page)
            ):
                raise RoutingIntelligenceStoreUnavailable("invalid_spatial_response")
            keys.update(item.observation_key for item in page)
            records.extend(page)
            cursor_observed_at, cursor_observation_key = order[-1]
        # Model fitting owns chronological split semantics; persistence reads
        # the newest bounded ingestion window and returns it chronologically.
        return tuple(sorted(records, key=lambda item: (
            item.observed_at, item.observation_key,
        )))

    def upsert(self, record: GraphOverrideRecord) -> tuple[GraphOverrideRecord, bool]:
        return self.upsert_many((record,))[0]

    def upsert_many(
        self,
        records: Sequence[GraphOverrideRecord],
    ) -> tuple[tuple[GraphOverrideRecord, bool], ...]:
        materialized = tuple(islice(iter(records), 2_001))
        # Protocol parity with InMemoryShortcutReviewQueue: an ingestion run
        # whose documents all reject is a successful no-op, not a store error.
        if not materialized:
            return ()
        if len(materialized) > 2_000:
            raise ValueError("candidate batch must contain at most 2000 records")
        canonical = tuple(_candidate_from_payload(record.to_dict())
                          if isinstance(record, GraphOverrideRecord) else None
                          for record in materialized)
        if any(record is None for record in canonical):
            raise TypeError("candidate batch accepts GraphOverrideRecord values")
        candidate_payloads = [record.to_dict() for record in canonical]
        payload = {"batch": candidate_payloads}
        try:
            encoded_size = len(json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("candidate batch is not valid finite JSON") from None
        if encoded_size > MAX_ATOMIC_CANDIDATE_BATCH_BYTES:
            raise ValueError(
                "candidate batch exceeds the atomic persistence request bound",
            )
        response = _store_request(
            _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS),
            method="POST",
            rest_path="/rest/v1/rpc/crossflow_upsert_shortcut_candidates",
            payload=payload,
            mutation_conflicts=True,
        )
        try:
            flags = _validated_candidate_upsert_summary(
                response,
                tuple(record.override_id for record in canonical),
            )
        except ValueError:
            raise RoutingIntelligenceStoreUnavailable(
                "invalid_candidate_response",
            ) from None
        # Keep the queue port compact: this ingestion response represents the
        # validated evidence in this request. Durable merged evidence is read
        # by ID at the explicit approval boundary, where it is revalidated and
        # frozen. The SQL summary supplies the exact per-identity insert/replay
        # flags without echoing large candidate payloads.
        return tuple(zip(canonical, flags))

    def get(
        self,
        override_id: str,
        *,
        _budget: Optional[_OperationBudget] = None,
    ) -> Optional[GraphOverrideRecord]:
        if not isinstance(override_id, str) or not _OVERRIDE_ID.fullmatch(override_id):
            raise ValueError("invalid override_id")
        response = _store_request(
            _budget or _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS),
            method="GET",
            rest_path="/rest/v1/crossflow_shortcut_review_candidates",
            query={
                "override_id": f"eq.{override_id}",
                "review_state": "eq.REVIEW_REQUIRED",
                "select": (
                    "override_id,graph_revision,candidate_payload,"
                    "review_state,activation_allowed"
                ),
                "limit": "2",
            },
        )
        if not isinstance(response, list) or len(response) > 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response")
        if not response:
            return None
        row = response[0]
        try:
            candidate = _candidate_from_payload(row["candidate_payload"])
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response") from None
        if (
            row.get("override_id") != candidate.override_id
            or row.get("graph_revision") != candidate.graph_revision
            or row.get("review_state") != REVIEW_REQUIRED
            or row.get("activation_allowed") is not False
        ):
            raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response")
        return candidate

    def _get_approved_exact(
        self,
        override_id: str,
        graph_revision: str,
        *,
        budget: Optional[_OperationBudget] = None,
    ) -> Optional[tuple[ApprovedGraphOverride, int]]:
        response = _store_request(
            budget or _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS),
            method="GET",
            rest_path="/rest/v1/crossflow_approved_graph_overrides",
            query={
                "graph_revision": f"eq.{graph_revision}",
                "override_id": f"eq.{override_id}",
                "select": (
                    "graph_revision,override_id,override_revision,"
                    "approved_payload,candidate_payload,candidate_sha256,"
                    "approved_by,approved_at"
                ),
                "limit": "2",
            },
        )
        if not isinstance(response, list) or len(response) > 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
        if not response:
            return None
        try:
            return (
                _approved_from_row(response[0], graph_revision),
                int(response[0]["override_revision"]),
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response") from None

    def snapshot(self) -> tuple[GraphOverrideRecord, ...]:
        budget = _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS)
        watermark_response = _store_request(
            budget,
            method="GET",
            rest_path="/rest/v1/crossflow_shortcut_review_candidates",
            query={
                "review_state": "eq.REVIEW_REQUIRED",
                "select": "queue_revision",
                "order": "queue_revision.desc", "limit": "1",
            },
        )
        if not isinstance(watermark_response, list) or len(watermark_response) > 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response")
        try:
            watermark = (
                int(watermark_response[0]["queue_revision"])
                if watermark_response else 0
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response") from None
        records: list[GraphOverrideRecord] = []
        cursor = 0
        while len(records) < 2_000 and cursor < watermark:
            limit = min(DEFAULT_CANDIDATE_PAGE_SIZE, 2_000 - len(records))
            response = _store_request(
                budget,
                method="GET",
                rest_path="/rest/v1/crossflow_shortcut_review_candidates",
                query={
                    "select": "queue_revision,override_id,candidate_payload",
                    "review_state": "eq.REVIEW_REQUIRED",
                    "and": (
                        f"(queue_revision.gt.{cursor},"
                        f"queue_revision.lte.{watermark})"
                    ),
                    "order": "queue_revision.asc", "limit": str(limit),
                },
            )
            if not isinstance(response, list) or len(response) > limit:
                raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response")
            try:
                page = [_candidate_from_payload(row["candidate_payload"])
                        for row in response]
            except (KeyError, TypeError, ValueError):
                raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response") from None
            if not response:
                return tuple(records)
            ids = [item.override_id for item in page]
            try:
                revisions = [int(row["queue_revision"]) for row in response]
            except (KeyError, TypeError, ValueError):
                raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response") from None
            if (
                len(set(ids)) != len(ids)
                or revisions != sorted(set(revisions))
                or revisions[0] <= cursor or revisions[-1] > watermark
                or any(row.get("override_id") != item.override_id
                       for row, item in zip(response, page))
            ):
                raise RoutingIntelligenceStoreUnavailable("invalid_candidate_response")
            records.extend(page)
            cursor = revisions[-1]
            if len(response) < limit:
                return tuple(records)
        if cursor < watermark:
            # Rows may have been approved/archived after the watermark. That is
            # a safe disappearance from a pending-review snapshot.
            return tuple(records)
        return tuple(records)

    def approve_candidate(
        self,
        override_id: str,
        *,
        approved_by: str,
        approved_at: datetime,
        active_graph_revision: str,
    ) -> tuple[ApprovedGraphOverride, int]:
        reviewer = str(approved_by).strip()
        budget = _OperationBudget(DEFAULT_STORE_OPERATION_TIMEOUT_SECONDS)
        candidate = self.get(override_id, _budget=budget)
        if candidate is None:
            existing = self._get_approved_exact(
                override_id, active_graph_revision, budget=budget,
            )
            if existing is None:
                raise ValueError("unknown shortcut candidate")
            if existing[0].approved_by != reviewer:
                raise ValueError("shortcut candidate was already approved by another reviewer")
            with self._cache_lock:
                self._approved_cache.pop(active_graph_revision, None)
            return existing
        approved = promote_reviewed_candidate(
            candidate, approved_by=reviewer, approved_at=approved_at,
            active_graph_revision=active_graph_revision,
        )
        response = _store_request(
            budget,
            method="POST",
            rest_path="/rest/v1/rpc/crossflow_approve_shortcut_candidate",
            payload={
                "candidate_id": override_id,
                "expected_candidate": candidate.to_dict(),
                "approved": _approved_payload(approved),
            },
            mutation_conflicts=True,
        )
        if not isinstance(response, list) or len(response) != 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
        row = response[0]
        try:
            persisted = _approved_from_row(row, active_graph_revision)
            revision = int(row["override_revision"])
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response") from None
        if persisted.override_id != candidate.override_id \
                or persisted.candidate_sha256 != _candidate_digest(candidate):
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
        with self._cache_lock:
            self._approved_cache.pop(active_graph_revision, None)
        return persisted, revision

    def approved_snapshot(
        self, *, graph_revision: str,
    ) -> ApprovedGraphOverrideSnapshot:
        if not _DIGEST.fullmatch(str(graph_revision)):
            raise ValueError("graph_revision must be a SHA-256 digest")
        now = time.monotonic()
        with self._cache_lock:
            cached = self._approved_cache.get(graph_revision)
            if cached is not None and cached[0] > now:
                self._cache_hits += 1
                return cached[1]
            self._cache_misses += 1
        budget = _OperationBudget(APPROVED_SNAPSHOT_TIMEOUT_SECONDS)
        watermark_response = _store_request(
            budget,
            method="GET",
            rest_path="/rest/v1/crossflow_approved_graph_overrides",
            query={
                "graph_revision": f"eq.{graph_revision}",
                "select": "override_revision",
                "order": "override_revision.desc", "limit": "1",
            },
        )
        if not isinstance(watermark_response, list) or len(watermark_response) > 1:
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
        try:
            watermark = (
                int(watermark_response[0]["override_revision"])
                if watermark_response else 0
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response") from None
        if not 0 <= watermark <= MAX_APPROVED_OVERRIDES_PER_GRAPH:
            raise RoutingIntelligenceStoreUnavailable("approved_snapshot_limit_exceeded")
        rows: list[dict[str, Any]] = []
        cursor = 0
        while cursor < watermark:
            limit = min(
                APPROVED_SNAPSHOT_PAGE_SIZE,
                MAX_APPROVED_OVERRIDES_PER_GRAPH + 1 - len(rows),
            )
            response = _store_request(
                budget,
                method="GET",
                rest_path="/rest/v1/crossflow_approved_graph_overrides",
                query={
                    "graph_revision": f"eq.{graph_revision}",
                    "select": (
                        "graph_revision,override_id,override_revision,"
                        "approved_payload,candidate_sha256,approved_by,approved_at"
                    ),
                    # Keyset pagination is stable under concurrent appends:
                    # all pages are capped at the captured revision watermark.
                    "and": (
                        f"(override_revision.gt.{cursor},"
                        f"override_revision.lte.{watermark})"
                    ),
                    "order": "override_revision.asc", "limit": str(limit),
                },
            )
            if not isinstance(response, list) or len(response) > limit:
                raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
            if not response:
                raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
            try:
                page_revisions = [int(row["override_revision"]) for row in response]
            except (KeyError, TypeError, ValueError):
                raise RoutingIntelligenceStoreUnavailable("invalid_approval_response") from None
            if (
                page_revisions != sorted(set(page_revisions))
                or page_revisions[0] <= cursor
                or page_revisions[-1] > watermark
            ):
                raise RoutingIntelligenceStoreUnavailable("invalid_approval_response")
            rows.extend(response)
            cursor = page_revisions[-1]
        if len(rows) > MAX_APPROVED_OVERRIDES_PER_GRAPH:
            raise RoutingIntelligenceStoreUnavailable("approved_snapshot_limit_exceeded")
        try:
            overrides = tuple(sorted(
                (
                    _approved_from_row(
                        row, graph_revision, require_frozen_candidate=False,
                    )
                    for row in rows
                ),
                key=lambda item: item.override_id,
            ))
        except (KeyError, TypeError, ValueError):
            raise RoutingIntelligenceStoreUnavailable("invalid_approval_response") from None
        snapshot = ApprovedGraphOverrideSnapshot(
            graph_revision=graph_revision, override_revision=watermark,
            overrides=overrides,
        )
        with self._cache_lock:
            if len(self._approved_cache) >= 8:
                oldest = min(self._approved_cache, key=lambda key: self._approved_cache[key][0])
                self._approved_cache.pop(oldest, None)
            self._approved_cache[graph_revision] = (
                time.monotonic() + self._approved_cache_ttl_seconds, snapshot,
            )
        return snapshot

    def health(self) -> dict[str, Any]:
        """Probe the optional schema once without exposing response details."""
        checked_at = datetime.now(timezone.utc).isoformat()
        if not shared_store_enabled():
            state, code = "disabled", "shared_store_not_enabled"
        elif not configured():
            state, code = "unavailable", "shared_store_not_configured"
        else:
            try:
                response = _store_request(
                    _OperationBudget(HEALTH_PROBE_TIMEOUT_SECONDS),
                    method="POST",
                    rest_path=(
                        "/rest/v1/rpc/"
                        "crossflow_routing_intelligence_health"
                    ),
                    payload={},
                )
                if (
                    not isinstance(response, list)
                    or len(response) != 1
                    or not isinstance(response[0], dict)
                    or response[0].get("schema_version") != 1
                ):
                    raise RoutingIntelligenceStoreUnavailable(
                        "schema_health_response_invalid",
                    )
            except RoutingIntelligenceStoreUnavailable as error:
                state, code = "unavailable", str(error)
            else:
                state, code = "verified_available", None
        with self._cache_lock:
            self._schema_health = state
            self._schema_health_code = code
            self._schema_checked_at = checked_at
        return {
            "state": state,
            "verified_available": state == "verified_available",
            "code": code,
            "checked_at": checked_at,
        }

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        if probe:
            self.health()
        with self._cache_lock:
            verified = self._schema_health == "verified_available"
            return {
                "enabled": shared_store_enabled(),
                "credentials_configured": configured(),
                "configured": shared_store_ready(),
                "implementation": "supabase_service_role_rest",
                "schema_health": self._schema_health,
                "schema_health_code": self._schema_health_code,
                "schema_checked_at": self._schema_checked_at,
                "verified_available": verified,
                "durable_spatial_history": verified,
                "durable_inactive_review_queue": verified,
                "append_only_approval_rpc": verified,
                "approved_snapshot_cache": {
                    "implementation": "bounded_in_process_ttl",
                    "ttl_seconds": self._approved_cache_ttl_seconds,
                    "max_graph_revisions": 8,
                    "entries": len(self._approved_cache),
                    "hits": self._cache_hits,
                    "misses": self._cache_misses,
                    "stores_secrets": False,
                },
            }
