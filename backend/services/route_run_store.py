"""Durable, shared storage for route-solver runs over Supabase REST.

The SQLite route store is durable for one long-lived process, but on Vercel it
lives in per-invocation ``/tmp``. A driver code issued by one worker is then
invisible to the next, so assigned-route retrieval fails even when the caller
presents a valid token. This module keeps the same runs in a shared table so
any instance can serve them, and so the history view has one source covering
every user.

It is strictly optional and always fails soft: without Supabase configured,
every call returns ``None``/empty and the caller keeps its SQLite behaviour.
Persisting a route must never be the reason a route response fails.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from services import supabase_server


TABLE_NAME = "crossflow_route_runs"
# The leading slash is required: supabase_server validates against
# ``^/rest/v1/...`` and rejects anything else as invalid_rest_path.
REST_PATH = f"/rest/v1/{TABLE_NAME}"
# A route envelope carries full geometry, so one row is far larger than the
# small-record defaults the other stores rely on.
SINGLE_ROW_MAX_BYTES = 4 * 1024 * 1024
MAX_HISTORY_LIMIT = 100

_SUMMARY_COLUMNS = (
    "route_id,route_code,route_kind,origin_name,destination_name,"
    "vehicle_type,created_by,created_at,updated_at"
)

_last_backend_available: Optional[bool] = None
_last_failure_reason: Optional[str] = None
_LOGGER = logging.getLogger(__name__)


def _config() -> Optional[supabase_server.SupabaseServerConfig]:
    return supabase_server.load_server_config()


def configured() -> bool:
    """Whether a shared route-run table could be reached at all."""
    return _config() is not None


def available() -> Optional[bool]:
    """Whether the table answered the most recent store operation."""
    return _last_backend_available


def failure_reason() -> Optional[str]:
    """Latest non-secret diagnostic code, for runtime logging."""
    return _last_failure_reason


def _mark_failure(reason: str) -> None:
    global _last_backend_available, _last_failure_reason
    _last_backend_available = False
    if reason != _last_failure_reason:
        _LOGGER.warning("[route_runs] unavailable reason=%s", reason)
    _last_failure_reason = reason


def _mark_success() -> None:
    global _last_backend_available, _last_failure_reason
    _last_backend_available = True
    _last_failure_reason = None


def _request(**kwargs: Any) -> Any:
    config = _config()
    if config is None:
        _mark_failure("not_configured")
        return None
    try:
        result = supabase_server.request_json(config, **kwargs)
    except supabase_server.SupabaseServerError as error:
        _mark_failure(error.code)
        return None
    _mark_success()
    return result


def save(
    route_id: str,
    route_code: str,
    route_kind: str,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    origin_name: Optional[str] = None,
    destination_name: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    created_by: Optional[str] = None,
) -> bool:
    """Upsert one run. Returns whether the shared table accepted it.

    A ``route_id`` is content-addressed, so replanning the same journey
    resolves to the same row; the table's trigger preserves the original
    ``created_at`` so history keeps first-seen ordering.
    """
    row = {
        "route_id": route_id,
        "route_code": route_code,
        "route_kind": route_kind,
        "request": dict(request),
        "response": dict(response),
        "origin_name": origin_name,
        "destination_name": destination_name,
        "vehicle_type": vehicle_type,
        "created_by": created_by,
    }
    result = _request(
        method="POST",
        rest_path=REST_PATH,
        query={"on_conflict": "route_id"},
        payload=[row],
        prefer="resolution=merge-duplicates,return=minimal",
        max_response_bytes=SINGLE_ROW_MAX_BYTES,
    )
    return result is not None


def get(route_key: str) -> Optional[Dict[str, Any]]:
    """Return one stored response envelope by route_id or route_code."""
    if not isinstance(route_key, str):
        return None
    if len(route_key) == 64:
        column = "route_id"
    elif len(route_key) == 7:
        column = "route_code"
    else:
        return None
    rows = _request(
        method="GET",
        rest_path=REST_PATH,
        query={column: f"eq.{route_key}", "select": "response", "limit": "1"},
        max_response_bytes=SINGLE_ROW_MAX_BYTES,
    )
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    envelope = rows[0].get("response")
    return envelope if isinstance(envelope, dict) else None


def recent(
    limit: int = 20,
    *,
    created_by: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent run summaries, newest first.

    Summaries only: the full envelope is deliberately excluded so listing a
    page of history never transfers megabytes of route geometry.
    """
    bounded = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    query = {
        "select": _SUMMARY_COLUMNS,
        "order": "created_at.desc",
        "limit": str(bounded),
    }
    if created_by:
        query["created_by"] = f"eq.{created_by}"
    rows = _request(method="GET", rest_path=REST_PATH, query=query)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


__all__ = [
    "TABLE_NAME",
    "available",
    "configured",
    "failure_reason",
    "get",
    "recent",
    "save",
]
