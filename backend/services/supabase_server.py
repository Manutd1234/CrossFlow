"""Server-only Supabase REST transport with a fixed credential boundary.

This module intentionally does not know about browser/anonymous keys.  It is
used only for backend-owned tables and RPC functions whose database grants are
restricted to ``service_role``.  Redirects are disabled so an opaque secret can
never be forwarded to a different origin.
"""

from __future__ import annotations

import json
import os
import queue
import re
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import (
    HTTPRedirectHandler, HTTPSHandler, Request, build_opener,
)


DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_IN_FLIGHT_REQUEST_WORKERS = 8
_REQUEST_WORKER_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT_REQUEST_WORKERS)


class SupabaseServerError(RuntimeError):
    """Safe, non-secret server integration error identified by ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SupabaseServerConfig:
    origin: str
    secret_key: str

    def __post_init__(self) -> None:
        canonical = _validated_origin(self.origin)
        if (
            not isinstance(self.secret_key, str)
            or not self.secret_key
            or len(self.secret_key) > 8_192
            or any(ord(char) < 33 or ord(char) > 126 for char in self.secret_key)
        ):
            raise SupabaseServerError("invalid_server_key")
        object.__setattr__(self, "origin", canonical)


class NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects rather than forwarding a privileged credential."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _verified_https_handler() -> Optional[HTTPSHandler]:
    """Verify TLS against certifi's bundle rather than the platform's.

    A stock python.org install on macOS ships no usable CA store until its
    ``Install Certificates`` script is run, so every request fails with
    CERTIFICATE_VERIFY_FAILED. Because callers flatten that to a generic
    network error, the symptom is indistinguishable from an unconfigured
    project. certifi is already a pinned dependency, so preferring it makes
    verification behave the same on a developer's laptop and in the deployed
    Linux runtime. Verification is never weakened: if certifi is somehow
    absent, fall back to the platform default rather than trusting anything.
    """
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is a pinned dependency
        return None
    return HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))


OPENER = build_opener(*(
    handler for handler in (NoRedirectHandler(), _verified_https_handler())
    if handler is not None
))

_REST_PATH = re.compile(
    r"^/rest/v1/[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*$",
)
_QUERY_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OFFICIAL_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co$")
_ENCODED_CONTROL = re.compile(r"%(?:0[ad]|5c)", re.IGNORECASE)


def _validated_origin(value: str) -> str:
    raw = str(value).strip().rstrip("/")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        raise SupabaseServerError("invalid_project_url") from None
    hostname = (parsed.hostname or "").casefold()
    explicitly_allowed = os.environ.get(
        "CROSSFLOW_SUPABASE_ALLOWED_HOST", "",
    ).strip().casefold()
    official = _OFFICIAL_HOST.fullmatch(hostname) is not None
    if (
        any(ord(char) < 33 or ord(char) > 126 or char == "\\" for char in raw)
        or parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not (official or hostname == explicitly_allowed)
    ):
        raise SupabaseServerError("invalid_project_url")
    return f"https://{hostname}"


def load_server_config() -> Optional[SupabaseServerConfig]:
    """Load a strict HTTPS project origin and a server-only privileged key."""
    raw_url = (
        os.environ.get("SUPABASE_URL", "")
        or os.environ.get("SUPABASE_PROJECT_URL", "")
    ).strip().rstrip("/")
    secret = (
        os.environ.get("SUPABASE_SECRET_KEY", "")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    ).strip()
    if not raw_url or not secret:
        return None

    try:
        return SupabaseServerConfig(raw_url, secret)
    except SupabaseServerError:
        return None


def privileged_headers(
    secret_key: str,
    *,
    prefer: Optional[str] = None,
    user_agent: str = "CrossFlow-AI/1.0 routing-intelligence",
) -> dict[str, str]:
    """Build headers correctly for modern opaque and legacy JWT secrets."""
    if (
        not isinstance(secret_key, str)
        or not secret_key
        or any(ord(char) < 33 or ord(char) > 126 for char in secret_key)
        or prefer is not None and (
            not isinstance(prefer, str)
            or not 1 <= len(prefer) <= 512
            or any(ord(char) < 32 or ord(char) > 126 for char in prefer)
        )
    ):
        raise SupabaseServerError("invalid_header")
    headers = {
        "apikey": secret_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    # Opaque sb_secret keys are not JWTs and must not be sent as Bearer tokens.
    if not secret_key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {secret_key}"
    if prefer is not None:
        headers["Prefer"] = prefer
    return headers


def request_json(
    config: SupabaseServerConfig,
    *,
    method: str,
    rest_path: str,
    query: Optional[Mapping[str, str]] = None,
    payload: Any = None,
    prefer: Optional[str] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> Any:
    """Call one fixed-origin REST path within one absolute wall-clock budget.

    ``urllib``'s timeout is an inactivity timeout, so a peer that slowly emits
    bytes can otherwise keep a serverless worker occupied indefinitely.  A
    controller thread enforces the total budget and closes an opened response
    on expiry; the bounded worker semaphore prevents abandoned connection
    attempts from growing without limit.
    """
    # Restrict paths to plain PostgREST/RPC identifiers. This rejects control
    # bytes, backslashes, authority-like ``//`` segments, dot traversal and
    # encoded CRLF/backslash tricks before URL construction.
    if not _REST_PATH.fullmatch(rest_path) or any(
        segment in {"", ".", ".."} for segment in rest_path.split("/")[3:]
    ):
        raise SupabaseServerError("invalid_rest_path")
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        raise SupabaseServerError("invalid_method")
    if (
        isinstance(timeout_seconds, bool)
        or isinstance(max_response_bytes, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isinstance(max_response_bytes, int)
        or not 0.1 <= float(timeout_seconds) <= 30
        or not 1 <= max_response_bytes <= 8 * 1024 * 1024
    ):
        raise SupabaseServerError("invalid_resource_limit")

    # Revalidate even manually-constructed/config-deserialized objects at the
    # request boundary rather than trusting dataclass type annotations.
    origin = _validated_origin(config.origin)
    if not isinstance(config.secret_key, str) or not config.secret_key:
        raise SupabaseServerError("invalid_server_key")
    normalized_query: dict[str, str] = {}
    if query:
        if len(query) > 50:
            raise SupabaseServerError("invalid_query")
        for raw_key, raw_value in query.items():
            if not isinstance(raw_key, str) or not _QUERY_KEY.fullmatch(raw_key):
                raise SupabaseServerError("invalid_query")
            if not isinstance(raw_value, str) or not 0 <= len(raw_value) <= 2_048:
                raise SupabaseServerError("invalid_query")
            if (
                any(ord(char) < 32 or char == "\\" for char in raw_value)
                or _ENCODED_CONTROL.search(raw_value)
            ):
                raise SupabaseServerError("invalid_query")
            normalized_query[raw_key] = raw_value

    url = f"{origin}{rest_path}"
    if normalized_query:
        url = f"{url}?{urlencode(normalized_query)}"
    try:
        body = None if payload is None else json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise SupabaseServerError("invalid_payload") from None
    if body is not None and len(body) > DEFAULT_MAX_REQUEST_BYTES:
        raise SupabaseServerError("request_too_large")
    request = Request(
        url,
        data=body,
        headers=privileged_headers(config.secret_key, prefer=prefer),
        method=method,
    )
    if not _REQUEST_WORKER_SLOTS.acquire(blocking=False):
        raise SupabaseServerError("request_capacity_saturated")
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    response_lock = threading.Lock()
    response_holder: dict[str, Any] = {}
    cancelled = threading.Event()
    deadline = time.monotonic() + float(timeout_seconds)

    def perform_request() -> None:
        try:
            try:
                with OPENER.open(request, timeout=float(timeout_seconds)) as response:
                    with response_lock:
                        response_holder["response"] = response
                    if cancelled.is_set():
                        response.close()
                        raise SupabaseServerError("timeout")
                    status = int(getattr(response, "status", 200))
                    if not 200 <= status < 300:
                        raise SupabaseServerError(f"http_{status}")
                    chunks: list[bytes] = []
                    total = 0
                    while total <= max_response_bytes:
                        if time.monotonic() >= deadline:
                            raise SupabaseServerError("timeout")
                        chunk = response.read(min(
                            64 * 1024,
                            max_response_bytes + 1 - total,
                        ))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    value = b"".join(chunks)
                outcome.put((True, value), block=False)
            except Exception as error:  # sanitized on caller thread below
                outcome.put((False, error), block=False)
        finally:
            with response_lock:
                response_holder.clear()
            _REQUEST_WORKER_SLOTS.release()

    try:
        worker = threading.Thread(
            target=perform_request,
            name="supabase-server-request",
            daemon=True,
        )
        worker.start()
    except Exception:
        _REQUEST_WORKER_SLOTS.release()
        raise SupabaseServerError("network_error") from None
    try:
        remaining = max(0.0, deadline - time.monotonic())
        succeeded, value = outcome.get(timeout=remaining)
    except queue.Empty:
        # ``close`` interrupts a slow-drip read in the standard HTTPS response
        # implementation.  Connection/DNS attempts which have not produced a
        # response remain bounded by the socket timeout and by the global
        # worker-slot cap.
        cancelled.set()
        with response_lock:
            opened_response = response_holder.get("response")
        if opened_response is not None:
            try:
                opened_response.close()
            except Exception:
                pass
        raise SupabaseServerError("timeout") from None
    if not succeeded:
        if isinstance(value, SupabaseServerError):
            raise value
        if isinstance(value, HTTPError):
            raise SupabaseServerError(f"http_{value.code}") from None
        if isinstance(value, TimeoutError):
            raise SupabaseServerError("timeout") from None
        if isinstance(value, (URLError, OSError)):
            raise SupabaseServerError("network_error") from None
        raise SupabaseServerError("network_error") from None
    raw = value
    if len(raw) > max_response_bytes:
        raise SupabaseServerError("response_too_large")
    if not raw:
        return None
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON constant")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SupabaseServerError("invalid_json") from None
