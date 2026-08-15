"""SQLite persistence for completed route responses.

The store is intentionally small and provider-neutral.  Route calculation does
not depend on it: a storage failure leaves the computed response available, and
the response metadata reports whether the local store is process-local.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from services.route_identity import (
    SHORT_ROUTE_CODE_ALPHABET,
    SHORT_ROUTE_CODE_LENGTH,
    route_id as make_route_id,
    short_route_code,
)


def database_path() -> str:
    configured = os.environ.get("CROSSFLOW_ROUTE_DB", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return os.path.join("/tmp", "crossflow-ai", "routes.db")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "routes.db",
    )


class RouteStore:
    """Thread-safe SQLite route store with a memory fallback."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or database_path()
        self._lock = threading.RLock()
        self._fallback_in_memory = False
        self._conn = self._connect(self.path)
        self._ensure_schema()

    def _connect(self, path: str) -> sqlite3.Connection:
        connection: Optional[sqlite3.Connection] = None
        try:
            if path != ":memory:":
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            connection = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            self._fallback_in_memory = True
            fallback = sqlite3.connect(":memory:", check_same_thread=False)
            fallback.row_factory = sqlite3.Row
            return fallback

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY,
                    route_code TEXT,
                    route_kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1]) for row in self._conn.execute("PRAGMA table_info(routes)")
            }
            if "route_code" not in columns:
                self._conn.execute("ALTER TABLE routes ADD COLUMN route_code TEXT")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_routes_route_code "
                "ON routes(route_code) WHERE route_code IS NOT NULL",
            )

    def route_code_for(self, route_key: str) -> str:
        """Reserve a deterministic available short code for a route ID."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT route_code FROM routes WHERE route_id = ?",
                (route_key,),
            ).fetchone()
            if existing is not None and existing[0]:
                return str(existing[0])
            for attempt in range(64):
                candidate = short_route_code(route_key, attempt)
                collision = self._conn.execute(
                    "SELECT route_id FROM routes WHERE route_code = ?",
                    (candidate,),
                ).fetchone()
                if collision is None or str(collision[0]) == route_key:
                    return candidate
        raise RuntimeError("Could not allocate a unique seven-character route code")

    @property
    def durable(self) -> bool:
        return not self._fallback_in_memory and self.path != ":memory:"

    @staticmethod
    def _json_value(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python")
        if isinstance(value, Mapping):
            return {str(k): RouteStore._json_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [RouteStore._json_value(v) for v in value]
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @classmethod
    def _dumps(cls, value: Any) -> str:
        return json.dumps(
            cls._json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def save(
        self,
        route_key: str,
        response: Mapping[str, Any],
        *,
        request: Any,
        route_kind: str,
        route_code: Optional[str] = None,
    ) -> str:
        """Idempotently persist one response under its validated identity."""
        if route_key != make_route_id(request, route_kind=route_kind):
            raise ValueError("route_id does not match the canonical route request")
        code = route_code or self.route_code_for(route_key)
        if (
            len(code) != SHORT_ROUTE_CODE_LENGTH
            or any(character not in SHORT_ROUTE_CODE_ALPHABET for character in code)
        ):
            raise ValueError("route_code must be a valid seven-character route code")
        request_json = self._dumps(request)
        response_json = self._dumps(response)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO routes
                    (route_id, route_code, route_kind, request_json, response_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id) DO UPDATE SET
                    route_code = COALESCE(routes.route_code, excluded.route_code),
                    response_json = excluded.response_json,
                    updated_at = excluded.updated_at
                """,
                (route_key, code, route_kind, request_json, response_json, now, now),
            )
        return code

    def get(self, route_key: str) -> Optional[dict[str, Any]]:
        if not isinstance(route_key, str):
            return None
        if len(route_key) == 64:
            query = "SELECT response_json FROM routes WHERE route_id = ?"
        elif len(route_key) == SHORT_ROUTE_CODE_LENGTH:
            query = "SELECT response_json FROM routes WHERE route_code = ?"
        else:
            return None
        with self._lock:
            row = self._conn.execute(
                query,
                (route_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


DEFAULT_ROUTE_STORE = RouteStore()


__all__ = ["RouteStore", "DEFAULT_ROUTE_STORE", "database_path"]
