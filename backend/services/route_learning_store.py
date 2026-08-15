"""Verified clear-road traversal persistence for local OSM route learning.

This store deliberately has no route-provider fields.  It accepts canonical
directed edge keys from the committed OpenStreetMap graph plus measurements
from allowlisted, map-matched first-party traversal telemetry.  Google Maps
routes, polylines, labels, distances, and durations are never accepted or
persisted here.

The immutable snapshot is consumed by the local A* router. This module remains
provider-agnostic and performs no routing itself.
"""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Sequence, Tuple


MAX_CLEAR_CONGESTION_SCORE = 25.0
MIN_MAP_MATCH_CONFIDENCE = 0.90
MIN_QUALIFYING_SAMPLES = 5
MIN_QUALIFYING_CONFIDENCE = 0.50
FULL_CONFIDENCE_SAMPLES = 8
OBSERVATION_RETENTION_DAYS = 90

VERIFICATION_METHODS = frozenset({
    "FIRST_PARTY_GPS_MAP_MATCH",
    "SIGNED_FLEET_TELEMETRY",
})

EdgeKey = Tuple[int, int, int]
LearnedEdgeKey = Tuple[int, int, int, str]


def database_path() -> str:
    """Return the configured durable path or a safe runtime default."""
    configured = os.environ.get("CROSSFLOW_ROUTE_LEARNING_DB", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return os.path.join("/tmp", "crossflow-ai", "route_learning.db")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "route_learning.db",
    )


def _is_ephemeral_path(path: str) -> bool:
    if path == ":memory:":
        return True
    absolute = os.path.realpath(path)
    return absolute == "/tmp" or absolute.startswith(("/tmp/", "/private/tmp/"))


@dataclass(frozen=True)
class VerifiedTraversalObservation:
    """One server-canonicalized, directed OSM-edge traversal measurement."""

    observation_id: str
    graph_revision: str
    source_node: int
    target_node: int
    road_index: int
    vehicle_type: str
    moving_duration_s: float
    observed_at_epoch: int
    verification_method: str
    map_match_confidence: float
    weather: int
    network_congestion_score: float
    local_congestion_score: float
    edge_distance_m: float


@dataclass(frozen=True)
class LearnedEdge:
    """Robust aggregate made only from qualifying verified observations."""

    source_node: int
    target_node: int
    road_index: int
    vehicle_type: str
    edge_distance_m: float
    median_moving_duration_s: float
    sample_count: int
    confidence: float
    median_map_match_confidence: float
    last_observed_at_epoch: int


@dataclass(frozen=True)
class LearningSnapshot:
    """An immutable, atomically replaceable lookup for one graph revision."""

    graph_revision: str
    revision: int
    # Cache identity is the persisted model revision, never a mutable/dynamic
    # mapping comparison. The MappingProxyType value itself remains immutable.
    entries: Mapping[LearnedEdgeKey, LearnedEdge] = field(
        compare=False,
        hash=False,
        repr=False,
    )


@dataclass(frozen=True)
class IngestionResult:
    accepted: int
    duplicates: int
    rejected_stale_graph: int
    revision: int
    qualifying_edge_count: int


def _empty_snapshot(graph_revision: str = "", revision: int = 0) -> LearningSnapshot:
    return LearningSnapshot(graph_revision, revision, MappingProxyType({}))


class RouteLearningStore:
    """Thread-safe SQLite store with an immutable in-process read snapshot."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or database_path()
        self._explicit_path = path is not None or bool(
            os.environ.get("CROSSFLOW_ROUTE_LEARNING_DB", "").strip()
        )
        self._lock = threading.RLock()
        self._fallback_in_memory = False
        self._storage_warning: Optional[str] = None
        self._conn = self._connect()
        self._ensure_schema()
        self._snapshot = _empty_snapshot(revision=self._revision_locked())

    def _connect(self) -> sqlite3.Connection:
        connection: Optional[sqlite3.Connection] = None
        try:
            if self.path != ":memory:":
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=5.0,
            )
            connection.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            connection.row_factory = sqlite3.Row
            return connection
        except (OSError, sqlite3.Error) as err:
            if connection is not None:
                connection.close()
            self._fallback_in_memory = True
            self._storage_warning = (
                f"Configured SQLite storage was unavailable; observations are "
                f"process-local only ({type(err).__name__})."
            )
            fallback = sqlite3.connect(":memory:", check_same_thread=False)
            fallback.row_factory = sqlite3.Row
            return fallback

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS learning_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    revision  INTEGER NOT NULL DEFAULT 0
                );

                INSERT OR IGNORE INTO learning_meta (singleton, revision)
                VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS edge_traversal_observations (
                    observation_id          TEXT PRIMARY KEY,
                    graph_revision          TEXT NOT NULL,
                    source_node             INTEGER NOT NULL,
                    target_node             INTEGER NOT NULL,
                    road_index              INTEGER NOT NULL,
                    vehicle_type            TEXT NOT NULL,
                    moving_duration_s       REAL NOT NULL CHECK (moving_duration_s > 0),
                    observed_at             INTEGER NOT NULL,
                    verification_method     TEXT NOT NULL
                                                   CHECK (verification_method IN (
                                                       'FIRST_PARTY_GPS_MAP_MATCH',
                                                       'SIGNED_FLEET_TELEMETRY'
                                                   )),
                    map_match_confidence    REAL NOT NULL
                                                   CHECK (map_match_confidence >= 0.9
                                                          AND map_match_confidence <= 1.0),
                    weather                 INTEGER NOT NULL CHECK (weather = 0),
                    network_congestion_score REAL NOT NULL
                                                    CHECK (network_congestion_score >= 0
                                                           AND network_congestion_score <= 25),
                    local_congestion_score  REAL NOT NULL
                                                   CHECK (local_congestion_score >= 0
                                                          AND local_congestion_score <= 25),
                    edge_distance_m         REAL NOT NULL CHECK (edge_distance_m > 0),
                    source_kind             TEXT NOT NULL DEFAULT 'verified_actual_traversal'
                                                   CHECK (source_kind = 'verified_actual_traversal'),
                    created_at              INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_edge_learning_group
                ON edge_traversal_observations (
                    graph_revision, source_node, target_node, road_index,
                    vehicle_type, observed_at
                );
            """)
            self._conn.commit()

    def _revision_locked(self) -> int:
        row = self._conn.execute(
            "SELECT revision FROM learning_meta WHERE singleton = 1",
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _aggregate(rows: Sequence[sqlite3.Row]) -> Optional[LearnedEdge]:
        if len(rows) < MIN_QUALIFYING_SAMPLES:
            return None
        durations = [float(row["moving_duration_s"]) for row in rows]
        duration_median = float(statistics.median(durations))
        deviations = [abs(value - duration_median) for value in durations]
        mad = float(statistics.median(deviations))
        relative_spread = 1.4826 * mad / duration_median
        consistency = max(0.0, 1.0 - min(1.0, relative_spread))
        map_confidence = float(statistics.median(
            float(row["map_match_confidence"]) for row in rows
        ))
        sample_strength = min(1.0, len(rows) / FULL_CONFIDENCE_SAMPLES)
        confidence = sample_strength * consistency * map_confidence
        if confidence < MIN_QUALIFYING_CONFIDENCE:
            return None
        first = rows[0]
        return LearnedEdge(
            source_node=int(first["source_node"]),
            target_node=int(first["target_node"]),
            road_index=int(first["road_index"]),
            vehicle_type=str(first["vehicle_type"]),
            edge_distance_m=float(statistics.median(
                float(row["edge_distance_m"]) for row in rows
            )),
            median_moving_duration_s=round(duration_median, 3),
            sample_count=len(rows),
            confidence=round(confidence, 4),
            median_map_match_confidence=round(map_confidence, 4),
            last_observed_at_epoch=max(int(row["observed_at"]) for row in rows),
        )

    def _build_snapshot_locked(self, graph_revision: str) -> LearningSnapshot:
        cutoff = int(time.time()) - OBSERVATION_RETENTION_DAYS * 86400
        rows = self._conn.execute(
            """
            SELECT source_node, target_node, road_index, vehicle_type,
                   moving_duration_s, map_match_confidence, edge_distance_m,
                   observed_at
            FROM edge_traversal_observations
            WHERE graph_revision = ? AND observed_at >= ?
            ORDER BY source_node, target_node, road_index, vehicle_type,
                     observed_at, observation_id
            """,
            (graph_revision, cutoff),
        ).fetchall()
        groups: Dict[LearnedEdgeKey, list[sqlite3.Row]] = {}
        for row in rows:
            key = (
                int(row["source_node"]),
                int(row["target_node"]),
                int(row["road_index"]),
                str(row["vehicle_type"]),
            )
            groups.setdefault(key, []).append(row)
        learned: Dict[LearnedEdgeKey, LearnedEdge] = {}
        for key, group in groups.items():
            aggregate = self._aggregate(group)
            if aggregate is not None:
                learned[key] = aggregate
        return LearningSnapshot(
            graph_revision=graph_revision,
            revision=self._revision_locked(),
            entries=MappingProxyType(learned),
        )

    def snapshot(self, graph_revision: str) -> LearningSnapshot:
        """Return one graph-scoped immutable snapshot, refreshing if needed."""
        with self._lock:
            revision = self._revision_locked()
            if (
                self._snapshot.graph_revision != graph_revision
                or self._snapshot.revision != revision
            ):
                self._snapshot = self._build_snapshot_locked(graph_revision)
            return self._snapshot

    def ingest(
        self,
        observations: Sequence[VerifiedTraversalObservation],
        *,
        current_graph_revision: str,
    ) -> IngestionResult:
        """Persist an idempotent verified batch and atomically refresh it."""
        stale = sum(
            item.graph_revision != current_graph_revision
            for item in observations
        )
        current = [
            item for item in observations
            if item.graph_revision == current_graph_revision
        ]
        for item in current:
            if item.verification_method not in VERIFICATION_METHODS:
                raise ValueError("Unsupported traversal verification method.")
            if item.weather != 0:
                raise ValueError("Route learning accepts clear-weather observations only.")
            if not (
                math.isfinite(item.map_match_confidence)
                and MIN_MAP_MATCH_CONFIDENCE <= item.map_match_confidence <= 1.0
            ):
                raise ValueError("Map-match confidence is below the learning threshold.")
            if not all(math.isfinite(score) and 0.0 <= score <= MAX_CLEAR_CONGESTION_SCORE for score in (
                item.network_congestion_score,
                item.local_congestion_score,
            )):
                raise ValueError("Congestion is above the clear-road learning threshold.")
            if not math.isfinite(item.moving_duration_s) or item.moving_duration_s <= 0:
                raise ValueError("Moving duration must be positive and finite.")
            if not math.isfinite(item.edge_distance_m) or item.edge_distance_m <= 0:
                raise ValueError("Canonical OSM edge distance must be positive and finite.")
        accepted = 0
        duplicates = 0
        now_epoch = int(time.time())
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for item in current:
                    cursor = self._conn.execute(
                        """
                        INSERT OR IGNORE INTO edge_traversal_observations (
                            observation_id, graph_revision, source_node,
                            target_node, road_index, vehicle_type,
                            moving_duration_s, observed_at, verification_method,
                            map_match_confidence, weather,
                            network_congestion_score, local_congestion_score,
                            edge_distance_m, source_kind, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'verified_actual_traversal', ?)
                        """,
                        (
                            item.observation_id,
                            item.graph_revision,
                            item.source_node,
                            item.target_node,
                            item.road_index,
                            item.vehicle_type,
                            item.moving_duration_s,
                            item.observed_at_epoch,
                            item.verification_method,
                            item.map_match_confidence,
                            item.weather,
                            item.network_congestion_score,
                            item.local_congestion_score,
                            item.edge_distance_m,
                            now_epoch,
                        ),
                    )
                    if cursor.rowcount == 1:
                        accepted += 1
                    else:
                        duplicates += 1
                if accepted:
                    self._conn.execute(
                        "UPDATE learning_meta SET revision = revision + 1 "
                        "WHERE singleton = 1",
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            self._snapshot = self._build_snapshot_locked(current_graph_revision)
            return IngestionResult(
                accepted=accepted,
                duplicates=duplicates,
                rejected_stale_graph=stale,
                revision=self._snapshot.revision,
                qualifying_edge_count=len(self._snapshot.entries),
            )

    def status(self, graph_revision: str) -> dict:
        """Return audit-safe persistence and qualification metadata."""
        snapshot = self.snapshot(graph_revision)
        with self._lock:
            current_count = int(self._conn.execute(
                "SELECT COUNT(*) FROM edge_traversal_observations "
                "WHERE graph_revision = ?",
                (graph_revision,),
            ).fetchone()[0])
            stale_count = int(self._conn.execute(
                "SELECT COUNT(*) FROM edge_traversal_observations "
                "WHERE graph_revision <> ?",
                (graph_revision,),
            ).fetchone()[0])
        ephemeral = self._fallback_in_memory or _is_ephemeral_path(self.path)
        if self._fallback_in_memory:
            location = "in_memory_fallback"
        elif self.path == ":memory:":
            location = "in_memory"
        elif ephemeral:
            location = "ephemeral_runtime_path"
        elif self._explicit_path:
            location = "configured_sqlite_path"
        else:
            location = "local_project_data"
        return {
            "enabled": True,
            "storage_backend": "sqlite",
            "storage_location": location,
            "durable": not ephemeral,
            "storage_warning": self._storage_warning,
            "graph_revision": graph_revision,
            "learning_revision": snapshot.revision,
            "current_graph_observation_count": current_count,
            "stale_graph_observation_count": stale_count,
            "qualifying_edge_count": len(snapshot.entries),
            "policy": {
                "weather": "clear_only",
                "maximum_network_congestion_score": MAX_CLEAR_CONGESTION_SCORE,
                "maximum_local_congestion_score": MAX_CLEAR_CONGESTION_SCORE,
                "minimum_map_match_confidence": MIN_MAP_MATCH_CONFIDENCE,
                "minimum_samples_per_edge_vehicle": MIN_QUALIFYING_SAMPLES,
                "minimum_aggregate_confidence": MIN_QUALIFYING_CONFIDENCE,
                "retention_days": OBSERVATION_RETENTION_DAYS,
            },
            "provenance": {
                "observations": "verified_actual_traversal",
                "edge_identity": "committed_openstreetmap_graph",
                "external_route_provider_content_persisted": False,
                "google_routes_content_persisted": False,
            },
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Main and router must consume the same process-local snapshot/connection.
# A single module-owned instance prevents two SQLite writers and divergent
# revisions when both modules import the learning service.
DEFAULT_ROUTE_LEARNING_STORE = RouteLearningStore()
