"""Durable ferry freshness metadata over Supabase REST.

Production Vercel deployments fail closed when the shared store is unavailable,
so a cold instance can never present an older timestamp as "latest". Local
development keeps the committed snapshot fallback unless durability is
explicitly required.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


TABLE_NAME = "crossflow_ferry_freshness"
TIMEOUT_SECONDS = 2
FAILURE_BACKOFF_SECONDS = 60
_last_backend_available: Optional[bool] = None
_retry_after_monotonic = 0.0
_last_failure_reason: Optional[str] = None
_LOGGER = logging.getLogger(__name__)


class FreshnessStoreUnavailable(RuntimeError):
    """The required shared freshness record cannot be read or written."""


def required() -> bool:
    """Whether serving a process-local freshness value would be misleading."""
    # Preview and production can both fan out across short-lived function
    # instances. Never let an explicit false value weaken that deployment
    # invariant when Vercel exposes its environment classification.
    if os.environ.get("VERCEL_ENV", "").strip().casefold() in {
        "preview", "production",
    }:
        return True

    explicit = os.environ.get("CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS")
    if explicit is None or not explicit.strip():
        return False
    normalized = explicit.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise FreshnessStoreUnavailable(
        "CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS must be 1 or 0."
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never forward a service-role credential to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _remember_failure(reason: str) -> None:
    """Record one non-secret diagnostic code and log only state changes."""
    global _last_failure_reason
    if reason != _last_failure_reason:
        _LOGGER.warning("[ferry_freshness] unavailable reason=%s", reason)
    _last_failure_reason = reason


def _config() -> Optional[Dict[str, str]]:
    url = (
        os.environ.get("SUPABASE_URL", "")
        or os.environ.get("SUPABASE_PROJECT_URL", "")
    ).strip().rstrip("/")
    # This state controls public provenance. Never accept an anonymous/browser
    # key for writes even if one exists elsewhere in the app. Supabase's modern
    # secret key replaces the legacy service-role JWT and also bypasses RLS.
    key = (
        os.environ.get("SUPABASE_SECRET_KEY", "")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    ).strip()
    if not url:
        _remember_failure("missing_project_url")
        return None
    if not key:
        _remember_failure("missing_server_key")
        return None
    parsed = urlparse(url)
    explicit_host = os.environ.get(
        "CROSSFLOW_SUPABASE_ALLOWED_HOST", "",
    ).strip().casefold()
    hostname = (parsed.hostname or "").casefold()
    official_host = hostname.endswith(".supabase.co")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not (official_host or hostname == explicit_host)
    ):
        _remember_failure("invalid_project_url")
        return None
    return {"url": f"https://{hostname}", "key": key}


def _mark_failure(reason: str) -> None:
    global _last_backend_available, _retry_after_monotonic
    _last_backend_available = False
    _retry_after_monotonic = time.monotonic() + FAILURE_BACKOFF_SECONDS
    _remember_failure(reason)


def _mark_success() -> None:
    global _last_backend_available, _retry_after_monotonic, _last_failure_reason
    _last_backend_available = True
    _retry_after_monotonic = 0.0
    _last_failure_reason = None


def _headers(key: str, *, prefer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
        "User-Agent": "CrossFlow-AI/1.0 ferry-freshness",
    }
    # Modern sb_secret keys are opaque rather than JWTs. Supabase explicitly
    # requires them on the apikey header only; treating one as a Bearer JWT
    # returns "Invalid JWT". Legacy service-role keys still use both headers.
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    if prefer is not None:
        headers["Prefer"] = prefer
    return headers


def load(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """Return one stored freshness row, or ``None`` when unavailable."""
    config = _config()
    if config is None:
        return None
    if time.monotonic() < _retry_after_monotonic:
        _remember_failure("request_backoff")
        return None
    query = quote(snapshot_id, safe="")
    url = (
        f"{config['url']}/rest/v1/{TABLE_NAME}"
        f"?snapshot_id=eq.{query}"
        "&select=snapshot_id,latest_checked_at,last_verified_at"
        "&limit=1"
    )
    request = Request(url, headers=_headers(config["key"]), method="GET")
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        _mark_failure(f"http_{error.code}")
        return None
    except TimeoutError:
        _mark_failure("timeout")
        return None
    except (URLError, OSError):
        _mark_failure("network_error")
        return None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _mark_failure("invalid_response")
        return None
    if not isinstance(payload, list):
        _mark_failure("invalid_response")
        return None
    _mark_success()
    if not payload:
        return None
    if not isinstance(payload[0], dict):
        _mark_failure("invalid_response")
        return None
    return payload[0]


def save(
    snapshot_id: str,
    latest_checked_at: str,
    last_verified_at: str,
) -> Optional[Dict[str, Any]]:
    """Upsert and return the database-monotonic freshness row."""
    config = _config()
    if config is None:
        return None
    url = (
        f"{config['url']}/rest/v1/{TABLE_NAME}"
        "?on_conflict=snapshot_id"
    )
    payload = json.dumps({
        "snapshot_id": snapshot_id,
        "latest_checked_at": latest_checked_at,
        "last_verified_at": last_verified_at,
    }).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers=_headers(
            config["key"],
            prefer="resolution=merge-duplicates,return=representation",
        ),
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not 200 <= int(getattr(response, "status", 200)) < 300:
                _mark_failure(f"http_{int(getattr(response, 'status', 0))}")
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        _mark_failure(f"http_{error.code}")
        return None
    except TimeoutError:
        _mark_failure("timeout")
        return None
    except (URLError, OSError):
        _mark_failure("network_error")
        return None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        _mark_failure("invalid_response")
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        _mark_failure("invalid_response")
        return None
    _mark_success()
    return payload[0]


def configured() -> bool:
    return _config() is not None


def available() -> Optional[bool]:
    """Whether the configured table answered the most recent store operation."""
    return _last_backend_available


def failure_reason() -> Optional[str]:
    """Return the latest non-secret diagnostic code for runtime logging."""
    return _last_failure_reason
