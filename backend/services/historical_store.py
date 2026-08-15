"""Versioned historical congestion storage for CrossFlow AI.

The history dashboard combines two explicitly non-observed inputs:

* a deterministic 14-day synthetic seed, used to make a new installation
  useful before it has collected samples; and
* periodic snapshots of CrossFlow's modelled corridor telemetry.

SQLite is durable for a single local process when it is backed by a normal
file. Serverless ``/tmp`` and the in-memory safety fallback are intentionally
reported as ephemeral. They must never be presented as a shared production
traffic archive.
"""

import math
import os
import random
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from services import clock
from services.traffic_observations import (
    MAX_FUTURE_SKEW_SECONDS,
    MAX_INGEST_BATCH_SIZE,
    ObservationConflictError,
    ObservationValidationError,
    SpatialBatchIngestionResult,
    SpatialTrafficObservation,
    validate_spatial_observation,
    validate_observation_batch,
)


CORRIDOR_INDEX = {
    "corridor-1": 0,
    "corridor-2": 1,
    "corridor-3": 2,
    "corridor-4": 3,
    "corridor-5": 4,
}
CORRIDOR_IDS = tuple(CORRIDOR_INDEX)

SYNTHETIC_SOURCE = "synthetic"
SYNTHETIC_SEED_VERSION = 2
SYNTHETIC_SEED_DAYS = 14
HISTORY_SCHEMA_VERSION = 2

SPATIAL_HISTORY_RETENTION_DAYS = 5 * 365
MAX_SPATIAL_QUERY_ROWS = 10_000
MAX_SPATIAL_TRAINING_ROWS = 100_000

_RECORD_INTERVAL_S = 300
_RETENTION_S = 60 * 86400
_CURRENT_SAMPLE_MAX_AGE_S = _RECORD_INTERVAL_S * 2
_RECENT_SAMPLE_MAX_AGE_S = 86400
_OBSERVED_SOURCES = frozenset({"tomtom_live", "verified_traffic_observation"})


def _database_path() -> str:
    """Return the configured path, or an honest runtime-specific default."""
    configured = os.environ.get("CROSSFLOW_HISTORY_DB", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return os.path.join("/tmp", "crossflow-ai", "congestion_history.db")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "congestion_history.db",
    )


_DB_PATH = _database_path()


def _normalise_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=clock.BATAM_TZ)
    return value.astimezone(clock.BATAM_TZ)


def _seed_rows(
    now: datetime,
    corridor_ids: Iterable[str],
) -> List[tuple[int, str, float, str]]:
    """Build a deterministic Batam-time seed for the requested corridors."""
    now_local = _normalise_now(now)
    requested = [corridor_id for corridor_id in CORRIDOR_IDS if corridor_id in corridor_ids]
    rows: List[tuple[int, str, float, str]] = []

    for day_offset in range(SYNTHETIC_SEED_DAYS, 0, -1):
        day = now_local - timedelta(days=day_offset)
        is_weekend = int(day.weekday() >= 5)

        for hour in range(24):
            local_sample = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            epoch = int(local_sample.astimezone(timezone.utc).timestamp())

            for corridor_id in requested:
                corridor_idx = CORRIDOR_INDEX[corridor_id]
                diurnal = 0.5 * (
                    1.0 - math.cos(2.0 * math.pi * (hour - 4) / 24.0)
                )
                base = 12.0 + 20.0 * diurnal
                morning = math.exp(-((hour - 8) ** 2) / (2 * 1.1 ** 2))
                evening = math.exp(-((hour - 18) ** 2) / (2 * 1.2 ** 2))
                corridor_factor = (1.25, 1.05, 0.90, 0.65, 0.45)[corridor_idx]
                peaks = (28.0 * morning + 33.0 * evening) * corridor_factor
                if is_weekend:
                    peaks *= 0.45

                # A per-sample seed makes a corridor seeded later byte-for-byte
                # consistent with the same corridor in a full initial seed.
                noise = random.Random(
                    f"{SYNTHETIC_SEED_VERSION}:{local_sample.date()}:"
                    f"{hour}:{corridor_id}"
                ).gauss(0, 2.5)
                score = max(5.0, min(96.0, base + peaks + noise))
                rows.append((epoch, corridor_id, round(score, 1), SYNTHETIC_SOURCE))

    return rows


class HistoryStore:
    """One SQLite history store with versioned synthetic-seed migration."""

    def __init__(
        self,
        path: str,
        *,
        now_provider: Callable[[], datetime] = clock.now,
        spatial_retention_days: Optional[int] = None,
    ) -> None:
        self.configured_path = path
        self.actual_path = path
        self._now_provider = now_provider
        self._write_lock = threading.Lock()
        self._last_cleanup_at = 0
        self._fallback_to_memory = False
        if spatial_retention_days is None:
            configured_retention = os.environ.get(
                "CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS", "",
            ).strip()
            try:
                spatial_retention_days = (
                    int(configured_retention)
                    if configured_retention
                    else SPATIAL_HISTORY_RETENTION_DAYS
                )
            except ValueError as error:
                raise ValueError(
                    "CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS must be an integer."
                ) from error
        if not 365 <= spatial_retention_days <= 20 * 365:
            raise ValueError(
                "Spatial history retention must be between 365 and 7300 days."
            )
        self.spatial_retention_days = spatial_retention_days
        self._conn: sqlite3.Connection
        self._open_and_initialize()

    def _open_connection(self, path: str) -> sqlite3.Connection:
        if path != ":memory:":
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=3000")
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _open_and_initialize(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open_connection(self.configured_path)
            self._conn = conn
            self._ensure_schema()
            self._migrate_synthetic_seed()
        except (OSError, sqlite3.Error) as err:
            if conn is not None:
                conn.close()
            if self.configured_path == ":memory:":
                raise
            print(
                f"[historical_store] {self.configured_path!r} unavailable "
                f"({err}); using process memory"
            )
            self.actual_path = ":memory:"
            self._fallback_to_memory = True
            self._conn = self._open_connection(":memory:")
            self._ensure_schema()
            self._migrate_synthetic_seed()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        INTEGER NOT NULL,
                    corridor  TEXT    NOT NULL,
                    score     REAL    NOT NULL,
                    source    TEXT    NOT NULL DEFAULT 'simulated'
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_corridor_ts "
                "ON observations(corridor, ts)"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS spatial_observations (
                    observation_key     TEXT PRIMARY KEY,
                    observed_at_us      INTEGER NOT NULL,
                    corridor            TEXT NOT NULL,
                    latitude            REAL NOT NULL,
                    longitude           REAL NOT NULL,
                    actual_speed_kph    REAL NOT NULL,
                    free_flow_speed_kph REAL NOT NULL,
                    source              TEXT NOT NULL,
                    provenance          TEXT NOT NULL,
                    confidence          REAL NOT NULL,
                    observed            INTEGER NOT NULL,
                    reviewed            INTEGER NOT NULL,
                    road_class          TEXT NOT NULL,
                    capacity_vph        REAL,
                    terminal_distance_km REAL,
                    local_timezone_offset_minutes INTEGER NOT NULL DEFAULT 420,
                    upstream_event_id   TEXT,
                    ingested_at_us      INTEGER NOT NULL,
                    CHECK (actual_speed_kph > 0),
                    CHECK (free_flow_speed_kph > 0),
                    CHECK (confidence > 0 AND confidence <= 1),
                    CHECK (observed IN (0, 1)),
                    CHECK (reviewed IN (0, 1))
                )
            """)
            spatial_columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(spatial_observations)"
                ).fetchall()
            }
            if "local_timezone_offset_minutes" not in spatial_columns:
                # Schema-v1 development databases stored UTC timestamps but
                # lacked their civil-time context. Existing Batam data gets
                # the honest regional default; new writes always persist it.
                self._conn.execute(
                    "ALTER TABLE spatial_observations ADD COLUMN "
                    "local_timezone_offset_minutes INTEGER NOT NULL DEFAULT 420"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spatial_corridor_time "
                "ON spatial_observations(corridor, observed_at_us)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spatial_source_time "
                "ON spatial_observations(source, observed_at_us)"
            )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS history_metadata (
                    key    TEXT PRIMARY KEY,
                    value  TEXT NOT NULL
                )
            """)
            self._set_metadata("history_schema_version", HISTORY_SCHEMA_VERSION)

    def _metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM history_metadata WHERE key = ?", (key,),
        ).fetchone()
        return str(row[0]) if row else None

    def _set_metadata(self, key: str, value: object) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO history_metadata (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    def _migrate_synthetic_seed(self) -> None:
        """Roll the synthetic window forward and fill missing corridors.

        Version 1 generated samples as UTC clock-hours and later interpreted
        them as WIB, shifting the intended peaks by seven hours. Version 2 is
        generated directly in Batam time. A cold start on a new Batam calendar
        date also regenerates the rolling 14-day window. Non-synthetic samples
        are never deleted by this migration.
        """
        try:
            current_version = int(self._metadata("synthetic_seed_version") or 0)
        except ValueError:
            current_version = 0

        now = _normalise_now(self._now_provider())
        seed_date = now.date().isoformat()
        generated_for_date = self._metadata("synthetic_seed_generated_for_date")
        if (
            current_version != SYNTHETIC_SEED_VERSION
            or generated_for_date != seed_date
        ):
            rows = _seed_rows(now, CORRIDOR_IDS)
            with self._conn:
                self._conn.execute(
                    "DELETE FROM observations WHERE source = ?",
                    (SYNTHETIC_SOURCE,),
                )
                self._conn.executemany(
                    "INSERT INTO observations (ts, corridor, score, source) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._set_metadata(
                    "synthetic_seed_version", SYNTHETIC_SEED_VERSION,
                )
                self._set_metadata(
                    "synthetic_seed_generated_for_date", seed_date,
                )
            return

        existing = {
            str(row[0])
            for row in self._conn.execute(
                "SELECT DISTINCT corridor FROM observations WHERE source = ?",
                (SYNTHETIC_SOURCE,),
            ).fetchall()
        }
        missing = [corridor_id for corridor_id in CORRIDOR_IDS if corridor_id not in existing]
        if not missing:
            return

        rows = _seed_rows(now, missing)
        placeholders = ", ".join("?" for _ in missing)
        with self._conn:
            # Deleting first makes two concurrent initializers idempotent even
            # if both observed the same missing corridor before the write lock.
            self._conn.execute(
                f"DELETE FROM observations WHERE source = ? "
                f"AND corridor IN ({placeholders})",
                (SYNTHETIC_SOURCE, *missing),
            )
            self._conn.executemany(
                "INSERT INTO observations (ts, corridor, score, source) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def close(self) -> None:
        self._conn.close()

    def _now(self, supplied: Optional[datetime] = None) -> datetime:
        return _normalise_now(supplied or self._now_provider())

    @staticmethod
    def _require_corridor(corridor_id: str) -> None:
        if corridor_id not in CORRIDOR_INDEX:
            raise ValueError(f"Unknown corridor_id: {corridor_id}")

    def storage_metadata(self) -> Dict[str, Any]:
        if self.actual_path == ":memory:":
            durability = "process_memory"
            durable = False
        else:
            absolute = os.path.realpath(self.actual_path)
            temporary_root = os.path.realpath(tempfile.gettempdir())
            is_temporary = (
                absolute == temporary_root
                or absolute.startswith(f"{temporary_root}{os.sep}")
            )
            durability = "ephemeral_instance_file" if is_temporary else "persistent_file"
            durable = not is_temporary
        return {
            "engine": "sqlite",
            "durability": durability,
            "durable": durable,
            "shared_across_instances": False,
            "fallback_to_memory": self._fallback_to_memory,
            "spatial_history": {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "typed": True,
                "retention_days": self.spatial_retention_days,
                "default_retention_years": 5,
                "max_ingest_batch_size": MAX_INGEST_BATCH_SIZE,
                "provenance_separates_observed_and_modelled": True,
            },
        }

    def record(
        self,
        corridor_id: str,
        score: float,
        source: str = "simulated",
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Sample at most once per corridor and source per five-minute bucket."""
        self._require_corridor(corridor_id)
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("History score must be a finite value from 0 to 100.")
        source = source.strip()
        if not source:
            raise ValueError("History source must not be empty.")

        epoch = int(self._now(now).timestamp())
        bucket_start = epoch - (epoch % _RECORD_INTERVAL_S)
        try:
            with self._write_lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO observations (ts, corridor, score, source)
                    SELECT ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM observations
                        WHERE corridor = ? AND source = ? AND ts >= ? AND ts < ?
                    )
                    """,
                    (
                        epoch, corridor_id, round(score, 1), source,
                        corridor_id, source,
                        bucket_start, bucket_start + _RECORD_INTERVAL_S,
                    ),
                )
                if epoch - self._last_cleanup_at >= 3600:
                    self._conn.execute(
                        "DELETE FROM observations WHERE ts < ?",
                        (epoch - _RETENTION_S,),
                    )
                    self._last_cleanup_at = epoch
        except sqlite3.Error as err:
            # Analytics history remains non-critical to route planning. A write
            # failure is visible in logs and freshness metadata without taking
            # the route API offline.
            print(f"[historical_store] write error: {err}")

    @staticmethod
    def _spatial_payload(
        observation: SpatialTrafficObservation,
    ) -> tuple[object, ...]:
        return (
            observation.observation_key,
            observation.timestamp_epoch_us,
            observation.corridor_id,
            observation.latitude,
            observation.longitude,
            observation.actual_speed_kph,
            observation.free_flow_speed_kph,
            observation.source,
            observation.provenance,
            observation.confidence,
            int(observation.observed),
            int(observation.reviewed),
            observation.road_class,
            observation.capacity_vph,
            observation.terminal_distance_km,
            observation.local_timezone_offset_minutes,
            observation.upstream_event_id,
        )

    @staticmethod
    def _stored_spatial_immutable(row: tuple[object, ...]) -> tuple[object, ...]:
        # Database rows start with the key; the observation contract starts
        # with corridor and timestamp. Keep this explicit so schema changes
        # cannot quietly weaken conflict detection.
        return (
            row[2], row[1], row[3], row[4], row[5], row[6], row[7], row[8],
            row[9], bool(row[10]), bool(row[11]), row[12], row[13], row[14],
            row[15], row[16],
        )

    def ingest_spatial_batch(
        self,
        observations: Iterable[SpatialTrafficObservation],
        *,
        now: Optional[datetime] = None,
    ) -> SpatialBatchIngestionResult:
        """Atomically insert a bounded batch with exact-replay idempotency.

        The five-year default applies only to this typed spatial table. Legacy
        dashboard samples retain their existing short window and APIs.
        """
        batch = validate_observation_batch(observations)
        effective_now = self._now(now).astimezone(timezone.utc)
        now_us = int(round(effective_now.timestamp() * 1_000_000))
        cutoff_us = now_us - self.spatial_retention_days * 86_400 * 1_000_000
        future_limit_us = now_us + MAX_FUTURE_SKEW_SECONDS * 1_000_000
        for observation in batch:
            if observation.timestamp_epoch_us < cutoff_us:
                raise ObservationValidationError(
                    "Observation falls outside the configured spatial-history "
                    f"retention window ({self.spatial_retention_days} days)."
                )
            if observation.timestamp_epoch_us > future_limit_us:
                raise ObservationValidationError(
                    "Observation is more than five minutes in the future."
                )

        unique_by_key = {
            observation.observation_key: observation for observation in batch
        }
        keys = tuple(sorted(unique_by_key))
        existing: Dict[str, tuple[object, ...]] = {}
        try:
            with self._write_lock, self._conn:
                # Stay below SQLite's common 999-variable limit.
                for offset in range(0, len(keys), 500):
                    chunk = keys[offset:offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = self._conn.execute(
                        "SELECT observation_key, observed_at_us, corridor, "
                        "latitude, longitude, actual_speed_kph, "
                        "free_flow_speed_kph, source, provenance, confidence, "
                        "observed, reviewed, road_class, capacity_vph, "
                        "terminal_distance_km, local_timezone_offset_minutes, "
                        "upstream_event_id "
                        f"FROM spatial_observations WHERE observation_key IN "
                        f"({placeholders})",
                        chunk,
                    ).fetchall()
                    existing.update({str(row[0]): row for row in rows})

                for key, row in existing.items():
                    candidate = unique_by_key[key]
                    if self._stored_spatial_immutable(row) != (
                        candidate.immutable_payload()
                    ):
                        raise ObservationConflictError(
                            f"Stored observation {key} has a different payload."
                        )

                new_observations = [
                    unique_by_key[key] for key in keys if key not in existing
                ]
                self._conn.executemany(
                    "INSERT INTO spatial_observations ("
                    "observation_key, observed_at_us, corridor, latitude, "
                    "longitude, actual_speed_kph, free_flow_speed_kph, source, "
                    "provenance, confidence, observed, reviewed, road_class, "
                    "capacity_vph, terminal_distance_km, "
                    "local_timezone_offset_minutes, upstream_event_id, "
                    "ingested_at_us) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [self._spatial_payload(item) + (now_us,)
                     for item in new_observations],
                )
                self._conn.execute(
                    "DELETE FROM spatial_observations WHERE observed_at_us < ?",
                    (cutoff_us,),
                )
        except (ObservationConflictError, ObservationValidationError):
            raise
        except sqlite3.Error as error:
            raise RuntimeError(
                "Spatial observation batch could not be durably stored."
            ) from error

        inserted = len(new_observations)
        return SpatialBatchIngestionResult(
            received=len(batch),
            unique=len(unique_by_key),
            inserted=inserted,
            duplicates=len(batch) - inserted,
            observation_keys=keys,
        )

    @staticmethod
    def _require_aware(value: datetime, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError(f"{name} must include an explicit timezone offset.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _spatial_observation_from_row(
        row: tuple[object, ...],
    ) -> SpatialTrafficObservation:
        observation = SpatialTrafficObservation(
            observation_key=str(row[0]),
            observed_at=datetime.fromtimestamp(
                int(row[1]) / 1_000_000.0, tz=timezone.utc,
            ),
            corridor_id=str(row[2]),
            latitude=float(row[3]),
            longitude=float(row[4]),
            actual_speed_kph=float(row[5]),
            free_flow_speed_kph=float(row[6]),
            source=str(row[7]),
            provenance=str(row[8]),
            confidence=float(row[9]),
            observed=bool(row[10]),
            reviewed=bool(row[11]),
            road_class=str(row[12]),
            capacity_vph=(float(row[13]) if row[13] is not None else None),
            terminal_distance_km=(
                float(row[14]) if row[14] is not None else None
            ),
            local_timezone_offset_minutes=int(row[15]),
            upstream_event_id=(str(row[16]) if row[16] is not None else None),
        )
        # Fail closed if a manually edited/corrupt database row attempts to
        # bypass the same provenance contract enforced on ingestion.
        return validate_spatial_observation(observation)

    def get_spatial_observations(
        self,
        corridor_id: str,
        *,
        start_at: datetime,
        end_at: datetime,
        sources: Optional[Iterable[str]] = None,
        limit: int = MAX_SPATIAL_QUERY_ROWS,
    ) -> List[SpatialTrafficObservation]:
        """Read typed history in chronological order for model calibration."""
        corridor_id = corridor_id.strip()
        if not corridor_id:
            raise ValueError("corridor_id must not be empty.")
        start = self._require_aware(start_at, "start_at")
        end = self._require_aware(end_at, "end_at")
        if start > end:
            raise ValueError("start_at cannot be after end_at.")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_SPATIAL_QUERY_ROWS:
            raise ValueError(
                f"limit must be between 1 and {MAX_SPATIAL_QUERY_ROWS}."
            )

        parameters: List[object] = [
            corridor_id,
            int(round(start.timestamp() * 1_000_000)),
            int(round(end.timestamp() * 1_000_000)),
        ]
        source_clause = ""
        if sources is not None:
            normalized_sources = tuple(sorted({str(source).strip()
                                               for source in sources}))
            if not normalized_sources or any(not item for item in normalized_sources):
                raise ValueError("sources cannot contain empty values.")
            source_clause = (
                " AND source IN ("
                + ",".join("?" for _ in normalized_sources)
                + ")"
            )
            parameters.extend(normalized_sources)
        parameters.append(limit)
        rows = self._conn.execute(
            "SELECT observation_key, observed_at_us, corridor, latitude, "
            "longitude, actual_speed_kph, free_flow_speed_kph, source, "
            "provenance, confidence, observed, reviewed, road_class, "
            "capacity_vph, terminal_distance_km, "
            "local_timezone_offset_minutes, upstream_event_id "
            "FROM spatial_observations WHERE corridor = ? "
            "AND observed_at_us >= ? AND observed_at_us <= ?"
            f"{source_clause} ORDER BY observed_at_us ASC LIMIT ?",
            parameters,
        ).fetchall()
        return [self._spatial_observation_from_row(row) for row in rows]

    def get_spatial_training_dataset(
        self,
        *,
        limit: int = MAX_SPATIAL_TRAINING_ROWS,
        now: Optional[datetime] = None,
    ) -> List[SpatialTrafficObservation]:
        """Return the latest in-retention records in chronological order."""
        if not isinstance(limit, int) or not 1 <= limit <= MAX_SPATIAL_TRAINING_ROWS:
            raise ValueError(
                f"limit must be between 1 and {MAX_SPATIAL_TRAINING_ROWS}."
            )
        effective_now = self._now(now).astimezone(timezone.utc)
        cutoff_us = int(round(
            (effective_now - timedelta(days=self.spatial_retention_days)).timestamp()
            * 1_000_000
        ))
        rows = self._conn.execute(
            "SELECT observation_key, observed_at_us, corridor, latitude, "
            "longitude, actual_speed_kph, free_flow_speed_kph, source, "
            "provenance, confidence, observed, reviewed, road_class, "
            "capacity_vph, terminal_distance_km, "
            "local_timezone_offset_minutes, upstream_event_id "
            "FROM (SELECT * FROM spatial_observations "
            "WHERE observed_at_us >= ? "
            "ORDER BY observed_at_us DESC LIMIT ?) "
            "ORDER BY observed_at_us ASC",
            (cutoff_us, limit),
        ).fetchall()
        return [self._spatial_observation_from_row(row) for row in rows]

    def get_hourly_profile(
        self,
        corridor_id: str,
        days: int = 7,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        self._require_corridor(corridor_id)
        cutoff = int(self._now(now).timestamp()) - days * 86400
        rows = self._conn.execute(
            """
            SELECT CAST(strftime('%H', ts, 'unixepoch', '+7 hours') AS INTEGER),
                   AVG(score), COUNT(*)
            FROM observations
            WHERE corridor = ? AND ts >= ?
            GROUP BY 1
            ORDER BY 1
            """,
            (corridor_id, cutoff),
        ).fetchall()
        by_hour = {row[0]: row for row in rows}
        return [
            {
                "hour": hour,
                "avg_score": round(by_hour[hour][1], 1) if hour in by_hour else None,
                "sample_count": by_hour[hour][2] if hour in by_hour else 0,
            }
            for hour in range(24)
        ]

    def get_weekly_trend(
        self,
        corridor_id: str,
        days: int = 30,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        self._require_corridor(corridor_id)
        cutoff = int(self._now(now).timestamp()) - days * 86400
        rows = self._conn.execute(
            """
            SELECT DATE(ts, 'unixepoch', '+7 hours'), AVG(score), COUNT(*)
            FROM observations
            WHERE corridor = ? AND ts >= ?
            GROUP BY 1
            ORDER BY 1
            """,
            (corridor_id, cutoff),
        ).fetchall()
        return [
            {"date": row[0], "avg_score": round(row[1], 1), "sample_count": row[2]}
            for row in rows
        ]

    def get_history_metadata(
        self,
        corridor_id: str,
        days: int = 7,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Describe the requested history window without overstating provenance."""
        self._require_corridor(corridor_id)
        effective_now = self._now(now)
        cutoff = int(effective_now.timestamp()) - days * 86400
        rows = self._conn.execute(
            """
            SELECT source, COUNT(*), MAX(ts)
            FROM observations
            WHERE corridor = ? AND ts >= ?
            GROUP BY source
            ORDER BY source
            """,
            (corridor_id, cutoff),
        ).fetchall()

        source_counts = {str(row[0]): int(row[1]) for row in rows}
        source_details = {
            source: {
                "sample_count": count,
                "observed": source in _OBSERVED_SOURCES,
            }
            for source, count in source_counts.items()
        }
        observed_flags = [item["observed"] for item in source_details.values()]
        contains_observed_samples = any(observed_flags)
        # `observed` describes the entire returned window, not merely whether
        # one observed source appears alongside synthetic/modelled samples.
        observed = bool(observed_flags) and all(observed_flags)
        latest_epoch = max((int(row[2]) for row in rows if row[2] is not None), default=None)

        if latest_epoch is None:
            latest_at = None
            latest_age_s = None
            freshness = "empty"
        else:
            latest = datetime.fromtimestamp(latest_epoch, tz=timezone.utc)
            latest_at = clock.iso(latest)
            latest_age_s = max(0, int(effective_now.timestamp()) - latest_epoch)
            if latest_age_s <= _CURRENT_SAMPLE_MAX_AGE_S:
                freshness = "current"
            elif latest_age_s <= _RECENT_SAMPLE_MAX_AGE_S:
                freshness = "recent"
            else:
                freshness = "stale"

        try:
            seed_version = int(
                self._metadata("synthetic_seed_version")
                or SYNTHETIC_SEED_VERSION
            )
        except ValueError:
            seed_version = SYNTHETIC_SEED_VERSION

        return {
            "window_days": days,
            "observed": observed,
            "contains_observed_samples": contains_observed_samples,
            "source_counts": source_counts,
            "sources": source_details,
            "latest_sample_at": latest_at,
            "latest_sample_age_seconds": latest_age_s,
            "freshness": freshness,
            "freshness_basis": "latest sample in the requested history window",
            "storage": self.storage_metadata(),
            "synthetic_seed": {
                "source": SYNTHETIC_SOURCE,
                "version": seed_version,
                "days": SYNTHETIC_SEED_DAYS,
                "timezone": "WIB (UTC+07:00)",
                "generated_for_date": self._metadata(
                    "synthetic_seed_generated_for_date"
                ),
                "observed": False,
            },
        }

    def get_training_dataset(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, corridor, score, source FROM observations ORDER BY ts ASC"
        ).fetchall()
        dataset = []
        for ts, corridor, score, source in rows:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(clock.BATAM_TZ)
            dataset.append({
                "ts": ts,
                "hour_float": dt.hour + dt.minute / 60.0 + dt.second / 3600.0,
                "day_of_week": dt.weekday(),
                "is_weekend": 1 if dt.weekday() >= 5 else 0,
                "corridor_idx": CORRIDOR_INDEX.get(corridor, 0),
                "score": score,
                "source": source,
            })
        return dataset


# The application uses one process-local store. Tests point CROSSFLOW_HISTORY_DB
# at a temporary path before importing this module.
_store = HistoryStore(_DB_PATH)


def close() -> None:
    _store.close()


def record(
    corridor_id: str,
    score: float,
    source: str = "simulated",
    *,
    now: Optional[datetime] = None,
) -> None:
    _store.record(corridor_id, score, source, now=now)


def get_hourly_profile(
    corridor_id: str,
    days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    return _store.get_hourly_profile(corridor_id, days, now=now)


def get_weekly_trend(
    corridor_id: str,
    days: int = 30,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    return _store.get_weekly_trend(corridor_id, days, now=now)


def get_history_metadata(
    corridor_id: str,
    days: int = 7,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    return _store.get_history_metadata(corridor_id, days, now=now)


def get_storage_metadata() -> Dict[str, Any]:
    """Expose the actual local storage boundary without leaking its path."""
    return _store.storage_metadata()


def get_all_corridors_today() -> Dict[str, Any]:
    return {
        corridor_id: get_hourly_profile(corridor_id, days=1)
        for corridor_id in CORRIDOR_IDS
    }


def get_training_dataset() -> List[Dict[str, Any]]:
    return _store.get_training_dataset()


def ingest_spatial_batch(
    observations: Iterable[SpatialTrafficObservation],
    *,
    now: Optional[datetime] = None,
) -> SpatialBatchIngestionResult:
    return _store.ingest_spatial_batch(observations, now=now)


def get_spatial_observations(
    corridor_id: str,
    *,
    start_at: datetime,
    end_at: datetime,
    sources: Optional[Iterable[str]] = None,
    limit: int = MAX_SPATIAL_QUERY_ROWS,
) -> List[SpatialTrafficObservation]:
    return _store.get_spatial_observations(
        corridor_id,
        start_at=start_at,
        end_at=end_at,
        sources=sources,
        limit=limit,
    )


def get_spatial_training_dataset(
    *,
    limit: int = MAX_SPATIAL_TRAINING_ROWS,
    now: Optional[datetime] = None,
) -> List[SpatialTrafficObservation]:
    return _store.get_spatial_training_dataset(limit=limit, now=now)
