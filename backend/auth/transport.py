"""User-scoped Supabase transport. Structurally cannot send the service key.

``services.supabase_server`` is the mirror image of this module: it speaks only
with ``SUPABASE_SECRET_KEY``, which bypasses row-level security entirely, and
is correct for backend-owned tables whose grants are restricted to
``service_role``.

This module is for the opposite case.  Admin and driver reads must be evaluated
*as that user*, so every request here carries the caller's own access token and
the public publishable key, and PostgreSQL enforces the policies in
``schema.sql``.

The separation is physical, not conventional.  This module never reads
``SUPABASE_SECRET_KEY`` or ``SUPABASE_SERVICE_ROLE_KEY`` from the environment,
so no future edit can accidentally promote a driver request to a
policy-bypassing one without deleting code that a test asserts is absent.  The
request hardening below is therefore duplicated rather than imported: keeping
the two credential boundaries in separate, independently readable modules is
worth more than sharing ninety lines.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from services import tls


DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
MAX_IN_FLIGHT_REQUEST_WORKERS = 8
MAX_ACCESS_TOKEN_CHARS = 8_192

_REQUEST_WORKER_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT_REQUEST_WORKERS)

# Only PostgREST data paths and the Auth user endpoint. Anything else -- token
# issuance, admin user management -- is deliberately unreachable from here.
_ALLOWED_PATH = re.compile(r"^/(?:rest|auth)/v1/[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*$")
_QUERY_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_OFFICIAL_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.supabase\.co$")
_ENCODED_CONTROL = re.compile(r"%(?:0[ad]|5c)", re.IGNORECASE)
# A JWS compact serialization: three base64url segments. Validated before the
# value is ever placed in an outbound header, so a token containing CR/LF can
# never split a request.
_ACCESS_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class SupabaseUserError(RuntimeError):
    """Safe, non-secret user-transport error identified by ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects rather than forwarding the caller's access token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# Unlike the server transport, this opener pins the verified trust store from
# services.tls, so an empty system CA bundle fails loudly instead of silently.
OPENER = build_opener(
    NoRedirectHandler(),
    HTTPSHandler(context=tls.default_context()),
)


def _validated_origin(value: str) -> str:
    """Accept only a bare HTTPS Supabase project origin."""
    raw = str(value).strip().rstrip("/")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        raise SupabaseUserError("invalid_project_url") from None
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
        raise SupabaseUserError("invalid_project_url")
    return f"https://{hostname}"


@dataclass(frozen=True, slots=True)
class SupabaseUserConfig:
    """Project origin plus the publishable key, which is safe to expose."""

    origin: str
    publishable_key: str

    def __post_init__(self) -> None:
        canonical = _validated_origin(self.origin)
        if (
            not isinstance(self.publishable_key, str)
            or not self.publishable_key
            or len(self.publishable_key) > 8_192
            or any(ord(char) < 33 or ord(char) > 126 for char in self.publishable_key)
        ):
            raise SupabaseUserError("invalid_publishable_key")
        object.__setattr__(self, "origin", canonical)


def load_user_config() -> Optional[SupabaseUserConfig]:
    """Load the browser-safe credential pair, or ``None`` when unconfigured.

    Note which variables are absent: this function cannot read a secret key.
    """
    raw_url = (
        os.environ.get("SUPABASE_URL", "")
        or os.environ.get("SUPABASE_PROJECT_URL", "")
    ).strip().rstrip("/")
    publishable = (
        os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
        or os.environ.get("SUPABASE_ANON_KEY", "")
    ).strip()
    if not raw_url or not publishable:
        return None
    try:
        return SupabaseUserConfig(raw_url, publishable)
    except SupabaseUserError:
        return None


def validate_access_token(access_token: str) -> str:
    """Reject anything that is not a compact JWS before it reaches a header."""
    if (
        not isinstance(access_token, str)
        or not 1 <= len(access_token) <= MAX_ACCESS_TOKEN_CHARS
        or not _ACCESS_TOKEN.fullmatch(access_token)
    ):
        raise SupabaseUserError("invalid_access_token")
    return access_token


def user_headers(
    publishable_key: str,
    access_token: str,
    *,
    prefer: Optional[str] = None,
    user_agent: str = "CrossFlow-AI/1.0 auth",
) -> dict[str, str]:
    """Identify the project with the publishable key, the caller with the JWT.

    PostgREST derives ``auth.uid()`` from the bearer token, so this pair is what
    makes the row-level security policies apply.
    """
    validate_access_token(access_token)
    if (
        not isinstance(publishable_key, str)
        or not publishable_key
        or any(ord(char) < 33 or ord(char) > 126 for char in publishable_key)
        or prefer is not None and (
            not isinstance(prefer, str)
            or not 1 <= len(prefer) <= 512
            or any(ord(char) < 32 or ord(char) > 126 for char in prefer)
        )
    ):
        raise SupabaseUserError("invalid_header")
    headers = {
        "apikey": publishable_key,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if prefer is not None:
        headers["Prefer"] = prefer
    return headers


def _normalized_query(query: Optional[Mapping[str, str]]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if not query:
        return normalized
    if len(query) > 50:
        raise SupabaseUserError("invalid_query")
    for raw_key, raw_value in query.items():
        if not isinstance(raw_key, str) or not _QUERY_KEY.fullmatch(raw_key):
            raise SupabaseUserError("invalid_query")
        if not isinstance(raw_value, str) or not 0 <= len(raw_value) <= 2_048:
            raise SupabaseUserError("invalid_query")
        if (
            any(ord(char) < 32 or char == "\\" for char in raw_value)
            or _ENCODED_CONTROL.search(raw_value)
        ):
            raise SupabaseUserError("invalid_query")
        normalized[raw_key] = raw_value
    return normalized


def request_json(
    config: SupabaseUserConfig,
    *,
    access_token: str,
    method: str,
    path: str,
    query: Optional[Mapping[str, str]] = None,
    payload: Any = None,
    prefer: Optional[str] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> Any:
    """Call one fixed-origin path as the token holder, within one time budget.

    ``urllib``'s timeout is an inactivity timeout, so a peer that slowly emits
    bytes could otherwise occupy a serverless worker indefinitely. A controller
    thread enforces the absolute budget and closes an opened response on
    expiry; the bounded worker semaphore caps abandoned connection attempts.
    """
    if not _ALLOWED_PATH.fullmatch(path) or any(
        segment in {"", ".", ".."} for segment in path.split("/")[3:]
    ):
        raise SupabaseUserError("invalid_path")
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        raise SupabaseUserError("invalid_method")
    if (
        isinstance(timeout_seconds, bool)
        or isinstance(max_response_bytes, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isinstance(max_response_bytes, int)
        or not 0.1 <= float(timeout_seconds) <= 30
        or not 1 <= max_response_bytes <= 4 * 1024 * 1024
    ):
        raise SupabaseUserError("invalid_resource_limit")

    origin = _validated_origin(config.origin)
    headers = user_headers(config.publishable_key, access_token, prefer=prefer)

    url = f"{origin}{path}"
    normalized_query = _normalized_query(query)
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
        raise SupabaseUserError("invalid_payload") from None
    if body is not None and len(body) > DEFAULT_MAX_REQUEST_BYTES:
        raise SupabaseUserError("request_too_large")

    request = Request(url, data=body, headers=headers, method=method)
    return _execute(
        request,
        timeout_seconds=float(timeout_seconds),
        max_response_bytes=max_response_bytes,
    )


def _execute(
    request: Request,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> Any:
    if not _REQUEST_WORKER_SLOTS.acquire(blocking=False):
        raise SupabaseUserError("request_capacity_saturated")
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    response_lock = threading.Lock()
    response_holder: dict[str, Any] = {}
    cancelled = threading.Event()
    deadline = time.monotonic() + timeout_seconds

    def perform_request() -> None:
        try:
            try:
                with OPENER.open(request, timeout=timeout_seconds) as response:
                    with response_lock:
                        response_holder["response"] = response
                    if cancelled.is_set():
                        response.close()
                        raise SupabaseUserError("timeout")
                    status = int(getattr(response, "status", 200))
                    if not 200 <= status < 300:
                        raise SupabaseUserError(f"http_{status}")
                    chunks: list[bytes] = []
                    total = 0
                    while total <= max_response_bytes:
                        if time.monotonic() >= deadline:
                            raise SupabaseUserError("timeout")
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
            except Exception as error:  # sanitized on the caller thread below
                outcome.put((False, error), block=False)
        finally:
            with response_lock:
                response_holder.clear()
            _REQUEST_WORKER_SLOTS.release()

    try:
        worker = threading.Thread(
            target=perform_request,
            name="supabase-user-request",
            daemon=True,
        )
        worker.start()
    except Exception:
        _REQUEST_WORKER_SLOTS.release()
        raise SupabaseUserError("network_error") from None
    try:
        remaining = max(0.0, deadline - time.monotonic())
        succeeded, value = outcome.get(timeout=remaining)
    except queue.Empty:
        cancelled.set()
        with response_lock:
            opened_response = response_holder.get("response")
        if opened_response is not None:
            try:
                opened_response.close()
            except Exception:
                pass
        raise SupabaseUserError("timeout") from None
    if not succeeded:
        if isinstance(value, SupabaseUserError):
            raise value
        if isinstance(value, HTTPError):
            raise SupabaseUserError(f"http_{value.code}") from None
        if isinstance(value, TimeoutError):
            raise SupabaseUserError("timeout") from None
        if isinstance(value, (URLError, OSError)):
            raise SupabaseUserError("network_error") from None
        raise SupabaseUserError("network_error") from None
    raw = value
    if len(raw) > max_response_bytes:
        raise SupabaseUserError("response_too_large")
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
        raise SupabaseUserError("invalid_json") from None
