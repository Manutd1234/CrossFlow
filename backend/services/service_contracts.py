"""Strict ports and immutable contracts for CrossFlow backend services.

These interfaces keep routing, model inference, ingestion and persistence
replaceable without turning a web request into a network of implicit globals.
They are framework-neutral; ``main.py`` may expose them through protected REST
endpoints while tests and batch jobs call the same facade directly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Generic, Iterable, Mapping, Optional, Protocol, TypeVar

from services.shortcut_ingestion import GraphOverrideRecord, IngestionBatchResult, SourceDocument
from services.traffic_observations import SpatialBatchIngestionResult, SpatialTrafficObservation


T = TypeVar("T")


class SpatialObservationPersistencePort(Protocol):
    def ingest_spatial_batch(
        self,
        observations: Iterable[SpatialTrafficObservation],
        *,
        now: Optional[datetime] = None,
    ) -> SpatialBatchIngestionResult: ...


class ShortcutCandidatePersistencePort(Protocol):
    def upsert(
        self,
        record: GraphOverrideRecord,
    ) -> tuple[GraphOverrideRecord, bool]: ...

    def upsert_many(
        self,
        records: Iterable[GraphOverrideRecord],
    ) -> tuple[tuple[GraphOverrideRecord, bool], ...]: ...

    def get(self, override_id: str) -> Optional[GraphOverrideRecord]: ...

    def snapshot(self) -> tuple[GraphOverrideRecord, ...]: ...


class ShortcutIngestionPort(Protocol):
    def ingest(self, documents: Iterable[SourceDocument]) -> IngestionBatchResult: ...


@dataclass(frozen=True, slots=True)
class ApprovedGraphOverride:
    """One explicitly reviewed override safe to expose to a graph adapter."""

    override_id: str
    graph_revision: str
    source_node: int
    target_node: int
    geometry: tuple[tuple[float, float], ...]
    applicable_vehicle_modes: tuple[str, ...]
    road_quality: float
    distance_m: float
    duration_s: Optional[float]
    approved_by: str
    approved_at: datetime
    candidate_sha256: str

    def __post_init__(self) -> None:
        if (
            not self.override_id or not self.graph_revision
            or not self.approved_by or not self.candidate_sha256
        ):
            raise ValueError("Approved override audit fields cannot be empty.")
        if self.source_node == self.target_node:
            raise ValueError("Approved override endpoints must be distinct.")
        if len(self.geometry) < 2 or any(
            len(point) != 2
            or not all(math.isfinite(float(value)) for value in point)
            or not -90.0 <= float(point[0]) <= 90.0
            or not -180.0 <= float(point[1]) <= 180.0
            for point in self.geometry
        ):
            raise ValueError("Approved override geometry must be valid lat/lng.")
        # Maritime links remain in the multimodal service. Road overlays must
        # map to a concrete road-vehicle routing policy.
        canonical_modes = {"MOTORCYCLE", "PASSENGER_CAR", "FREIGHT_TRUCK"}
        if (
            not self.applicable_vehicle_modes
            or tuple(sorted(set(self.applicable_vehicle_modes)))
            != self.applicable_vehicle_modes
            or any(mode not in canonical_modes for mode in self.applicable_vehicle_modes)
        ):
            raise ValueError("Approved override vehicle modes are invalid.")
        if not 0.0 <= self.road_quality <= 1.0:
            raise ValueError("road_quality must be in [0, 1].")
        if not math.isfinite(self.distance_m) or self.distance_m <= 0.0:
            raise ValueError("distance_m must be positive and finite.")
        if self.duration_s is not None and (
            not math.isfinite(self.duration_s) or self.duration_s <= 0.0
        ):
            raise ValueError("duration_s must be positive and finite when present.")
        if self.duration_s is not None:
            implied_kph = self.distance_m / self.duration_s * 3.6
            if not 1.0 <= implied_kph <= 130.0:
                raise ValueError("Approved override duration implies an implausible speed.")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("approved_at must include an explicit timezone.")
        if len(self.candidate_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.candidate_sha256
        ):
            raise ValueError("candidate_sha256 must be a lowercase SHA-256 digest.")


@dataclass(frozen=True, slots=True)
class ApprovedGraphOverrideSnapshot:
    """Immutable, revision-bound promotion boundary consumed by routing."""

    graph_revision: str
    override_revision: int
    overrides: tuple[ApprovedGraphOverride, ...] = ()

    def __post_init__(self) -> None:
        if not self.graph_revision or self.override_revision < 0:
            raise ValueError("Override snapshot revision is invalid.")
        identities = [item.override_id for item in self.overrides]
        if identities != sorted(set(identities)):
            raise ValueError("Approved overrides must be unique and sorted by id.")
        if any(item.graph_revision != self.graph_revision for item in self.overrides):
            raise ValueError("Every approved override must match the snapshot graph.")


class GraphOverridePromotionPort(Protocol):
    def approved_snapshot(self, *, graph_revision: str) -> ApprovedGraphOverrideSnapshot: ...


def promote_reviewed_candidate(
    candidate: GraphOverrideRecord,
    *,
    approved_by: str,
    approved_at: datetime,
    active_graph_revision: str,
) -> ApprovedGraphOverride:
    """Explicit human-review boundary; never called by ingestion itself."""
    if not isinstance(candidate, GraphOverrideRecord):
        raise TypeError("candidate must be a quarantined GraphOverrideRecord.")
    if candidate.activation_allowed or candidate.review_state != "REVIEW_REQUIRED":
        raise ValueError("Only quarantined review candidates can be promoted.")
    if candidate.graph_revision != active_graph_revision:
        raise ValueError("Candidate targets another graph revision.")
    if "FERRY_MARITIME" in candidate.applicable_vehicle_modes:
        raise ValueError(
            "Maritime tips require the multimodal-link review workflow and "
            "cannot be promoted into the road graph."
        )
    reviewer = str(approved_by).strip()
    if not reviewer or len(reviewer) > 128:
        raise ValueError("approved_by must identify the reviewer.")
    canonical = json.dumps(
        candidate.to_dict(), sort_keys=True, separators=(",", ":"), default=str,
    )
    return ApprovedGraphOverride(
        override_id=candidate.override_id,
        graph_revision=candidate.graph_revision,
        source_node=candidate.source_node,
        target_node=candidate.target_node,
        geometry=candidate.geometry,
        applicable_vehicle_modes=candidate.applicable_vehicle_modes,
        road_quality=candidate.road_quality,
        distance_m=candidate.claimed_distance_m or candidate.geometry_distance_m,
        duration_s=candidate.claimed_duration_s,
        approved_by=reviewer,
        approved_at=approved_at,
        candidate_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class CorridorRouteRequest:
    corridor_id: Optional[str]
    vehicle_type: str
    hour: int = 14
    weather: int = 0
    origin_id: Optional[str] = None
    destination_id: Optional[str] = None
    route_preference: str = "BALANCED"
    schedule_verified_at: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FreeRouteRequest:
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    vehicle_type: str
    hour: int = 14
    weather: int = 0
    origin_name: Optional[str] = None
    destination_name: Optional[str] = None
    route_preference: str = "BALANCED"
    schedule_verified_at: Optional[str] = None


class RoutePlanningPort(Protocol):
    def plan_corridor(self, request: CorridorRouteRequest) -> Mapping[str, Any]: ...

    def plan_free(self, request: FreeRouteRequest) -> Mapping[str, Any]: ...


class CachePort(Protocol, Generic[T]):
    def get(self, key: str) -> Optional[T]: ...

    def put(self, key: str, value: T, *, ttl_seconds: float) -> None: ...

    def status(self) -> Mapping[str, Any]: ...


class BoundedTTLCache(Generic[T]):
    """Thread-safe copy-isolated cache for immutable, non-secret inputs only."""

    def __init__(self, *, max_entries: int = 128) -> None:
        if not isinstance(max_entries, int) or not 1 <= max_entries <= 10_000:
            raise ValueError("max_entries must be between 1 and 10000.")
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[T]:
        now = time.monotonic()
        with self._lock:
            stored = self._items.get(key)
            if stored is None or stored[0] <= now:
                if stored is not None:
                    self._items.pop(key, None)
                self._misses += 1
                return None
            self._items.move_to_end(key)
            self._hits += 1
            return copy.deepcopy(stored[1])

    def put(self, key: str, value: T, *, ttl_seconds: float) -> None:
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("Cache key must be a non-empty bounded string.")
        if not math.isfinite(ttl_seconds) or not 0.0 < ttl_seconds <= 86_400.0:
            raise ValueError("ttl_seconds must be in (0, 86400].")
        with self._lock:
            self._items[key] = (time.monotonic() + ttl_seconds, copy.deepcopy(value))
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "implementation": "bounded_in_process_ttl",
                "max_entries": self.max_entries,
                "entries": len(self._items),
                "hits": self._hits,
                "misses": self._misses,
                "stores_secrets": False,
            }


@dataclass(frozen=True, slots=True)
class RoutingServiceStatus:
    service_contract_version: int
    graph_revision: str
    override_revision: int
    approved_override_count: int
    cache: Mapping[str, Any]
    fallback_order: tuple[str, ...]
    shortcut_activation_policy: str


class RoutingServiceFacade:
    """Small orchestration boundary with explicit local fallback behavior."""

    def __init__(
        self,
        *,
        graph_revision: str,
        local_route: Callable[..., Mapping[str, Any]],
        local_free_route: Callable[..., Mapping[str, Any]],
        override_promoter: GraphOverridePromotionPort,
        cache: CachePort[Mapping[str, Any]],
        revision_identity: Optional[Callable[[], Mapping[str, Any]]] = None,
        cache_bucket_seconds: int = 10,
    ) -> None:
        if not 1 <= cache_bucket_seconds <= 60:
            raise ValueError("cache_bucket_seconds must be between 1 and 60.")
        self.graph_revision = graph_revision
        self._local_route = local_route
        self._local_free_route = local_free_route
        self._override_promoter = override_promoter
        self._cache = cache
        self._revision_identity = revision_identity or (lambda: {})
        self._cache_bucket_seconds = cache_bucket_seconds

    @staticmethod
    def _cache_key(
        operation: str,
        request: Mapping[str, Any],
        snapshot: ApprovedGraphOverrideSnapshot,
        dynamic_revision: Mapping[str, Any],
        time_bucket: int,
    ) -> str:
        payload = json.dumps(
            {
                "operation": operation,
                "request": request,
                "graph_revision": snapshot.graph_revision,
                "override_revision": snapshot.override_revision,
                "dynamic_revision": dynamic_revision,
                "time_bucket": time_bucket,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _snapshot(self) -> ApprovedGraphOverrideSnapshot:
        snapshot = self._override_promoter.approved_snapshot(
            graph_revision=self.graph_revision,
        )
        if snapshot.graph_revision != self.graph_revision:
            raise RuntimeError("Approved override snapshot targets another graph revision.")
        return snapshot

    def _execute(
        self,
        operation: str,
        function: Callable[..., Mapping[str, Any]],
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if "approved_override_snapshot" in request:
            raise ValueError(
                "approved_override_snapshot is service-owned and cannot be supplied."
            )
        snapshot = self._snapshot()
        dynamic_revision = dict(self._revision_identity())
        time_bucket = int(time.time()) // self._cache_bucket_seconds
        key = self._cache_key(
            operation, request, snapshot, dynamic_revision, time_bucket,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        response_value = function(
            **request,
            approved_override_snapshot=snapshot,
        )
        if response_value is None:
            raise RuntimeError("Local OpenStreetMap fallback produced no route.")
        response = dict(response_value)
        response.setdefault("service_audit", {})
        response["service_audit"].update({
            "contract_version": 1,
            "graph_revision": snapshot.graph_revision,
            "override_revision": snapshot.override_revision,
            "approved_override_count": len(snapshot.overrides),
            "approved_overrides_applied": bool(
                response.get("approved_graph_overrides_used")
                or any(
                    item.get("approved_graph_overrides_used")
                    for item in response.get("alternative_routes", ())
                    if isinstance(item, Mapping)
                )
            ),
            "fallback_provider": "local_openstreetmap",
            "dynamic_revision": dynamic_revision,
            "cache_time_bucket_seconds": self._cache_bucket_seconds,
        })
        self._cache.put(
            key, response, ttl_seconds=float(self._cache_bucket_seconds),
        )
        return copy.deepcopy(response)

    def plan_corridor(self, request: CorridorRouteRequest) -> Mapping[str, Any]:
        if not isinstance(request, CorridorRouteRequest):
            raise TypeError("request must be a CorridorRouteRequest.")
        return self._execute(
            "corridor", self._local_route,
            {field: getattr(request, field) for field in request.__dataclass_fields__},
        )

    def plan_free(self, request: FreeRouteRequest) -> Mapping[str, Any]:
        if not isinstance(request, FreeRouteRequest):
            raise TypeError("request must be a FreeRouteRequest.")
        return self._execute(
            "free", self._local_free_route,
            {field: getattr(request, field) for field in request.__dataclass_fields__},
        )

    def optimize_route(self, **request: Any) -> Mapping[str, Any]:
        return self._execute("corridor", self._local_route, request)

    def optimize_free_route(self, **request: Any) -> Mapping[str, Any]:
        return self._execute("free", self._local_free_route, request)

    def status(self) -> RoutingServiceStatus:
        snapshot = self._snapshot()
        return RoutingServiceStatus(
            service_contract_version=1,
            graph_revision=self.graph_revision,
            override_revision=snapshot.override_revision,
            approved_override_count=len(snapshot.overrides),
            cache=self._cache.status(),
            fallback_order=("local_openstreetmap", "explicit_supabase_pgrouting"),
            shortcut_activation_policy=(
                "review candidates are quarantined; only an immutable approved "
                "snapshot matching the active graph revision is eligible"
            ),
        )
