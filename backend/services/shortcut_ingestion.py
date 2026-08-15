"""Quarantined ingestion of crowd-sourced shortcut tips.

This module deliberately stops before routing.  It accepts documents from a
server-owned source allowlist, parses narrowly-defined geocoded tips, validates
them against a read-only graph index, and emits deterministic *review queue*
records.  A record produced here is never an active road edge: every record is
marked ``REVIEW_REQUIRED`` and ``activation_allowed`` is always false.

The optional fetcher is similarly narrow.  It can fetch only explicitly pinned
HTTPS URLs, resolves and rejects non-public addresses, connects to the checked
IP address (avoiding a second DNS lookup), rejects redirects, and enforces MIME,
time and byte limits.  Callers cannot supply or widen the allowlist at request
time.
"""

from __future__ import annotations

import hashlib
import html.parser
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

from services import tls


SCHEMA_VERSION = 1
EARTH_RADIUS_M = 6_371_008.8

DEFAULT_MAX_DOCUMENT_BYTES = 256 * 1024
DEFAULT_MAX_DOCUMENTS_PER_BATCH = 20
DEFAULT_MAX_TIPS_PER_DOCUMENT = 64
DEFAULT_MAX_TIPS_PER_BATCH = 200
DEFAULT_MAX_EXCERPT_CHARS = 320
DEFAULT_FETCH_TIMEOUT_S = 5.0
DEFAULT_MAX_ENDPOINT_SNAP_M = 400.0
DEFAULT_ROAD_QUALITY = 0.50
DEFAULT_MAX_REVIEW_QUEUE_ENTRIES = 2_000
MAX_REVIEW_QUEUE_ENTRIES = 100_000
MAX_PROVENANCE_RECORDS_PER_CANDIDATE = 32
MAX_IN_FLIGHT_DEADLINE_WORKERS = 8
SOURCE_POLICY_ENV = "CROSSFLOW_SHORTCUT_SOURCE_POLICY"
MAX_SOURCE_POLICY_BYTES = 64 * 1024

MIN_CANDIDATE_LENGTH_M = 8.0
MAX_ROAD_CANDIDATE_LENGTH_M = 10_000.0
MAX_MARITIME_CANDIDATE_LENGTH_M = 80_000.0
MIN_CROWD_CONFIDENCE = 0.20
MAX_CROWD_CONFIDENCE = 0.80

REVIEW_REQUIRED = "REVIEW_REQUIRED"

CANONICAL_VEHICLE_MODES = frozenset({
    "MOTORCYCLE",
    "PASSENGER_CAR",
    "FREIGHT_TRUCK",
    "FERRY_MARITIME",
})

_MODE_ALIASES = {
    "motorcycle": "MOTORCYCLE",
    "motorbike": "MOTORCYCLE",
    "scooter": "MOTORCYCLE",
    "passenger_car": "PASSENGER_CAR",
    "passenger car": "PASSENGER_CAR",
    "car": "PASSENGER_CAR",
    "taxi": "PASSENGER_CAR",
    "freight_truck": "FREIGHT_TRUCK",
    "freight truck": "FREIGHT_TRUCK",
    "truck": "FREIGHT_TRUCK",
    "cargo": "FREIGHT_TRUCK",
    "ferry_maritime": "FERRY_MARITIME",
    "ferry maritime": "FERRY_MARITIME",
    "ferry": "FERRY_MARITIME",
    "maritime": "FERRY_MARITIME",
}

_ROAD_QUALITY_LABELS = {
    "impassable": 0.05,
    "very_poor": 0.15,
    "poor": 0.25,
    "rough": 0.40,
    "fair": 0.55,
    "good": 0.75,
    "excellent": 0.90,
}

_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_UNSAFE_RE = re.compile(r"[\x00-\x20\x7f\\]")
_URL_ENCODED_UNSAFE_RE = re.compile(r"(?i)%(?:0a|0d|2f|5c)")
_SPACE_RE = re.compile(r"\s+")


class ShortcutIngestionError(ValueError):
    """Base class for safe, caller-presentable ingestion failures."""


class SourceContractError(ShortcutIngestionError):
    """A source or document violated the server-owned source contract."""


class FetchSafetyError(SourceContractError):
    """A fetch was refused before untrusted content reached the parser."""


class CandidateValidationError(ShortcutIngestionError):
    """One parsed shortcut claim was not plausible enough to queue."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _normalise_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _normalise_host(value: str) -> str:
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as err:
        raise SourceContractError("Source hostname is not valid IDNA.") from err


def _canonical_https_url(value: str) -> tuple[str, SplitResult]:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise SourceContractError("Source URL must be a non-empty string under 2048 bytes.")
    if _URL_UNSAFE_RE.search(value) or _URL_ENCODED_UNSAFE_RE.search(value):
        raise SourceContractError("Source URL contains unsafe or ambiguous characters.")
    parts = urlsplit(value)
    if parts.scheme.lower() != "https":
        raise SourceContractError("Only HTTPS source URLs are permitted.")
    if parts.username is not None or parts.password is not None:
        raise SourceContractError("Source URLs must not contain credentials.")
    if not parts.hostname:
        raise SourceContractError("Source URL must contain a hostname.")
    try:
        port = parts.port
    except ValueError as err:
        raise SourceContractError("Source URL contains an invalid port.") from err
    if port not in (None, 443):
        raise SourceContractError("Source URLs may use only the standard HTTPS port.")
    if parts.fragment:
        raise SourceContractError("Source URLs must not contain fragments.")

    host = _normalise_host(parts.hostname)
    path = parts.path or "/"
    canonical_parts = SplitResult("https", host, path, parts.query, "")
    return urlunsplit(canonical_parts), canonical_parts


@dataclass(frozen=True)
class SourceRegistration:
    """Immutable server configuration for one auditable source."""

    source_id: str
    allowed_hosts: tuple[str, ...]
    pinned_urls: tuple[str, ...] = ()
    allowed_content_types: tuple[str, ...] = (
        "application/json",
        "text/plain",
        "text/html",
    )
    confidence_ceiling: float = 0.70

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not _SOURCE_ID_RE.fullmatch(self.source_id):
            raise SourceContractError(
                "source_id must use lowercase letters, digits, dot, dash or underscore."
            )
        hosts = tuple(sorted({_normalise_host(host) for host in self.allowed_hosts}))
        if not hosts:
            raise SourceContractError("Each registered source needs an exact hostname.")

        pinned: list[str] = []
        for value in self.pinned_urls:
            canonical, parts = _canonical_https_url(value)
            if parts.hostname not in hosts:
                raise SourceContractError("Pinned URL hostname is not allowlisted for its source.")
            pinned.append(canonical)

        content_types = tuple(sorted({
            _normalise_content_type(value) for value in self.allowed_content_types
        }))
        if not content_types or any(not value for value in content_types):
            raise SourceContractError("At least one valid source MIME type is required.")
        if not math.isfinite(self.confidence_ceiling) or not (
            MIN_CROWD_CONFIDENCE <= self.confidence_ceiling <= MAX_CROWD_CONFIDENCE
        ):
            raise SourceContractError("Crowd-source confidence ceiling must be 0.20..0.80.")

        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "pinned_urls", tuple(sorted(set(pinned))))
        object.__setattr__(self, "allowed_content_types", content_types)


@dataclass(frozen=True)
class ValidatedSourceTarget:
    source_id: str
    url: str
    hostname: str
    request_target: str


@dataclass(frozen=True)
class SourcePolicy:
    """Server-owned allowlist plus content and batch resource limits."""

    registrations: tuple[SourceRegistration, ...] = ()
    max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES
    max_documents_per_batch: int = DEFAULT_MAX_DOCUMENTS_PER_BATCH
    max_tips_per_document: int = DEFAULT_MAX_TIPS_PER_DOCUMENT
    max_tips_per_batch: int = DEFAULT_MAX_TIPS_PER_BATCH
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS
    _by_id: Mapping[str, SourceRegistration] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_id: dict[str, SourceRegistration] = {}
        for registration in self.registrations:
            if registration.source_id in by_id:
                raise SourceContractError("Duplicate source_id in source policy.")
            by_id[registration.source_id] = registration
        for name, value, upper in (
            ("max_document_bytes", self.max_document_bytes, 2 * 1024 * 1024),
            ("max_documents_per_batch", self.max_documents_per_batch, 100),
            ("max_tips_per_document", self.max_tips_per_document, 500),
            ("max_tips_per_batch", self.max_tips_per_batch, 200),
            ("max_excerpt_chars", self.max_excerpt_chars, 2_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= upper):
                raise SourceContractError(f"{name} must be an integer between 1 and {upper}.")
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))

    def registration(self, source_id: str) -> SourceRegistration:
        try:
            return self._by_id[source_id]
        except (KeyError, TypeError) as err:
            raise SourceContractError("Document source_id is not server-allowlisted.") from err

    def validate_target(
        self,
        source_id: str,
        url: str,
        *,
        require_pinned_url: bool,
    ) -> ValidatedSourceTarget:
        registration = self.registration(source_id)
        canonical, parts = _canonical_https_url(url)
        assert parts.hostname is not None
        if parts.hostname not in registration.allowed_hosts:
            raise SourceContractError("Source URL hostname is not allowlisted for this source.")
        if require_pinned_url:
            if not registration.pinned_urls or canonical not in registration.pinned_urls:
                raise FetchSafetyError("Network fetching is limited to server-pinned source URLs.")
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        return ValidatedSourceTarget(source_id, canonical, parts.hostname, target)


def source_policy_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> SourcePolicy:
    """Load a strict server-owned pinned-source policy, or disable fetching.

    Expected JSON::

        {"schema_version": 1, "sources": [{
          "source_id": "community_blog",
          "pinned_urls": ["https://example.org/audited/batam-tips.json"],
          "allowed_content_types": ["application/json"],
          "confidence_ceiling": 0.6
        }], "limits": {"max_document_bytes": 262144}}

    Unknown keys fail closed. With an unset/blank variable the returned policy
    has no sources, which makes network fetching impossible.
    """

    environment = os.environ if environ is None else environ
    raw = environment.get(SOURCE_POLICY_ENV, "")
    if not isinstance(raw, str):
        raise SourceContractError(f"{SOURCE_POLICY_ENV} must contain JSON text.")
    raw = raw.strip()
    if not raw:
        return SourcePolicy()
    if len(raw.encode("utf-8")) > MAX_SOURCE_POLICY_BYTES:
        raise SourceContractError("Shortcut source policy exceeds its size cap.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise SourceContractError("Shortcut source policy is malformed JSON.") from err
    if not isinstance(payload, Mapping):
        raise SourceContractError("Shortcut source policy must be a JSON object.")
    unknown = set(payload) - {"schema_version", "sources", "limits"}
    if unknown:
        raise SourceContractError(
            "Shortcut source policy contains unknown fields: "
            + ", ".join(sorted(map(str, unknown)))
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SourceContractError("Unsupported shortcut source policy schema version.")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) > 100:
        raise SourceContractError("Shortcut source policy sources must be a list of at most 100.")
    registrations: list[SourceRegistration] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping):
            raise SourceContractError("Each shortcut source policy entry must be an object.")
        unknown_source = set(raw_source) - {
            "source_id", "pinned_urls", "allowed_content_types", "confidence_ceiling",
        }
        if unknown_source:
            raise SourceContractError(
                "Shortcut source entry contains unknown fields: "
                + ", ".join(sorted(map(str, unknown_source)))
            )
        pinned_urls = raw_source.get("pinned_urls")
        if not isinstance(pinned_urls, list) or not pinned_urls or len(pinned_urls) > 50:
            raise SourceContractError("Each configured source requires 1..50 pinned_urls.")
        if any(not isinstance(value, str) for value in pinned_urls):
            raise SourceContractError("Pinned source URLs must be strings.")
        canonical_urls = [_canonical_https_url(value) for value in pinned_urls]
        allowed_content_types = raw_source.get(
            "allowed_content_types",
            ["application/json", "text/plain", "text/html"],
        )
        if not isinstance(allowed_content_types, list) or any(
            not isinstance(value, str) for value in allowed_content_types
        ):
            raise SourceContractError("allowed_content_types must be a string list.")
        registrations.append(SourceRegistration(
            source_id=raw_source.get("source_id"),
            allowed_hosts=tuple(parts.hostname or "" for _url, parts in canonical_urls),
            pinned_urls=tuple(url for url, _parts in canonical_urls),
            allowed_content_types=tuple(allowed_content_types),
            confidence_ceiling=raw_source.get("confidence_ceiling", 0.70),
        ))

    raw_limits = payload.get("limits", {})
    if not isinstance(raw_limits, Mapping):
        raise SourceContractError("Shortcut source policy limits must be an object.")
    allowed_limits = {
        "max_document_bytes", "max_documents_per_batch", "max_tips_per_document",
        "max_tips_per_batch", "max_excerpt_chars",
    }
    unknown_limits = set(raw_limits) - allowed_limits
    if unknown_limits:
        raise SourceContractError(
            "Shortcut source limits contain unknown fields: "
            + ", ".join(sorted(map(str, unknown_limits)))
        )
    return SourcePolicy(registrations=tuple(registrations), **dict(raw_limits))


@dataclass(frozen=True)
class SourceDocument:
    """A supplied or safely fetched source document; content remains untrusted."""

    source_id: str
    source_url: str
    content_type: str
    content: bytes | str
    document_id: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if not _DOCUMENT_ID_RE.fullmatch(self.document_id):
            raise SourceContractError("document_id contains unsupported characters.")
        if not isinstance(self.content, (bytes, str)):
            raise SourceContractError("Document content must be UTF-8 bytes or text.")
        if not isinstance(self.retrieved_at, datetime):
            raise SourceContractError("retrieved_at must be a timezone-aware datetime.")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise SourceContractError("retrieved_at must include a timezone.")


@dataclass(frozen=True)
class FetchResponse:
    """Injectable transport response used by ``AllowlistedSourceFetcher``."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Resolver = Callable[[str, int], Sequence[str]]
Transport = Callable[[ValidatedSourceTarget, str, float, int], FetchResponse]

# Timed-out daemon workers cannot be killed safely in CPython. Each keeps its
# slot until the underlying resolver/transport actually returns, so repeated
# stuck calls fail fast instead of creating an unbounded number of threads.
_DEADLINE_WORKER_SLOTS = threading.BoundedSemaphore(
    MAX_IN_FLIGHT_DEADLINE_WORKERS,
)


def _run_with_deadline(operation: Callable[[], Any], timeout_s: float) -> Any:
    """Return/raise within a wall-clock bound even if a resolver ignores timeouts.

    Python's system DNS API has no portable timeout argument. A daemon worker
    keeps the caller's deadline strict; a late result is discarded and can
    never reach parsing or the review queue.
    """

    worker_slots = _DEADLINE_WORKER_SLOTS
    if not worker_slots.acquire(blocking=False):
        raise FetchSafetyError(
            "Allowlisted source fetch capacity is temporarily saturated."
        )

    outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            try:
                outcomes.put((True, operation()), block=False)
            except Exception as err:  # noqa: BLE001 - sanitized below
                outcomes.put((False, err), block=False)
        finally:
            worker_slots.release()

    try:
        worker = threading.Thread(
            target=run,
            name="shortcut-source-fetch",
            daemon=True,
        )
        worker.start()
    except Exception:
        worker_slots.release()
        raise
    try:
        succeeded, value = outcomes.get(timeout=max(0.001, timeout_s))
    except queue.Empty as err:
        raise FetchSafetyError("Allowlisted source fetch exceeded its wall-clock deadline.") from err
    if succeeded:
        return value
    if isinstance(value, (FetchSafetyError, SourceContractError)):
        raise value
    raise FetchSafetyError("Allowlisted source fetch failed safely.") from value


def resolve_public_ips(hostname: str, port: int = 443) -> tuple[str, ...]:
    """Resolve a hostname and reject DNS answers that can reach local networks."""

    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as err:
        raise FetchSafetyError("Allowlisted source hostname did not resolve.") from err
    addresses = sorted({str(answer[4][0]) for answer in answers})
    if not addresses:
        raise FetchSafetyError("Allowlisted source hostname returned no addresses.")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as err:
            raise FetchSafetyError("DNS returned an invalid IP address.") from err
        if not address.is_global:
            raise FetchSafetyError("Source hostname resolved to a non-public address.")
    return tuple(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is the already-validated DNS answer."""

    def __init__(self, hostname: str, resolved_ip: str, *, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=tls.default_context())
        self._resolved_ip = resolved_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket, server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _default_transport(
    target: ValidatedSourceTarget,
    resolved_ip: str,
    timeout_s: float,
    max_bytes: int,
) -> FetchResponse:
    deadline = time.monotonic() + timeout_s
    connection = _PinnedHTTPSConnection(
        target.hostname,
        resolved_ip,
        timeout=timeout_s,
    )
    # Closing the connection from a timer makes the production transport
    # cancellable even when a peer keeps a socket active with a slow byte drip.
    # The outer worker deadline remains defense in depth for DNS/custom clients.
    cancel = threading.Timer(timeout_s, connection.close)
    cancel.daemon = True
    cancel.start()
    try:
        connection.request(
            "GET",
            target.request_target,
            headers={
                "Accept": "application/json, text/plain, text/html;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": "CrossFlowShortcutReviewBot/1.0",
            },
        )
        response = connection.getresponse()
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            if time.monotonic() >= deadline:
                raise FetchSafetyError(
                    "Allowlisted source fetch exceeded its wall-clock deadline."
                )
            size = min(64 * 1024, max_bytes + 1 - total)
            reader = getattr(response, "read1", response.read)
            chunk = reader(size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
        return FetchResponse(
            status=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            body=body,
        )
    except (OSError, ssl.SSLError, http.client.HTTPException) as err:
        raise FetchSafetyError("Allowlisted source fetch failed safely.") from err
    finally:
        cancel.cancel()
        connection.close()


class AllowlistedSourceFetcher:
    """Bounded fetcher for server-pinned sources; never a general URL client."""

    def __init__(
        self,
        policy: SourcePolicy,
        *,
        resolver: Resolver = resolve_public_ips,
        transport: Transport = _default_transport,
        timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not math.isfinite(timeout_s) or not (0.1 <= timeout_s <= 15.0):
            raise SourceContractError("Fetch timeout must be between 0.1 and 15 seconds.")
        self.policy = policy
        self._resolver = resolver
        self._transport = transport
        self._timeout_s = timeout_s
        self._now_provider = now_provider

    def fetch(self, source_id: str, url: str) -> SourceDocument:
        """Fetch one pinned document without following redirects."""

        deadline = time.monotonic() + self._timeout_s
        target = self.policy.validate_target(source_id, url, require_pinned_url=True)
        registration = self.policy.registration(source_id)
        addresses = tuple(_run_with_deadline(
            lambda: self._resolver(target.hostname, 443),
            deadline - time.monotonic(),
        ))
        if not addresses:
            raise FetchSafetyError("Allowlisted source hostname returned no addresses.")
        # Custom resolvers are untrusted too; repeat the public-address gate.
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as err:
                raise FetchSafetyError("Resolver returned an invalid IP address.") from err
            if not address.is_global:
                raise FetchSafetyError("Source hostname resolved to a non-public address.")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchSafetyError("Allowlisted source fetch exceeded its wall-clock deadline.")
        response = _run_with_deadline(
            lambda: self._transport(
                target,
                addresses[0],
                remaining,
                self.policy.max_document_bytes,
            ),
            remaining,
        )
        if 300 <= response.status < 400:
            raise FetchSafetyError("Redirects are disabled for source fetching.")
        if response.status != 200:
            raise FetchSafetyError("Allowlisted source returned a non-success status.")
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        if headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            raise FetchSafetyError("Compressed source responses are not accepted.")
        content_type = _normalise_content_type(headers.get("content-type", ""))
        if content_type not in registration.allowed_content_types:
            raise FetchSafetyError("Allowlisted source returned an unsupported MIME type.")
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as err:
                raise FetchSafetyError("Source returned an invalid Content-Length.") from err
            if declared_length < 0 or declared_length > self.policy.max_document_bytes:
                raise FetchSafetyError("Source response exceeds the configured size cap.")
        if not isinstance(response.body, bytes):
            raise FetchSafetyError("Source transport returned a non-byte body.")
        if len(response.body) > self.policy.max_document_bytes:
            raise FetchSafetyError("Source response exceeds the configured size cap.")

        fetched_at = self._now_provider()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise SourceContractError("Fetcher clock must return a timezone-aware datetime.")
        body_hash = hashlib.sha256(response.body).hexdigest()
        return SourceDocument(
            source_id=source_id,
            source_url=target.url,
            content_type=content_type,
            content=response.body,
            document_id=f"fetch-{body_hash[:24]}",
            retrieved_at=fetched_at.astimezone(timezone.utc),
        )


@dataclass(frozen=True)
class GraphNode:
    node_id: int
    lat: float
    lng: float


@dataclass(frozen=True)
class SnappedNode:
    node_id: int
    distance_m: float


def haversine_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lng1 = map(math.radians, first)
    lat2, lng2 = map(math.radians, second)
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    value = math.sin(dlat / 2.0) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2.0) ** 2
    )
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))


@dataclass(frozen=True)
class GraphIndex:
    """Minimal immutable graph view used only for bounds and endpoint snapping."""

    graph_revision: str
    bounds: tuple[float, float, float, float]
    nodes: tuple[GraphNode, ...]
    _grid: Mapping[tuple[int, int], tuple[GraphNode, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _grid_extents: tuple[int, int, int, int] = field(
        init=False,
        repr=False,
        compare=False,
    )

    GRID_CELL_DEGREES = 0.01

    def __post_init__(self) -> None:
        if not self.nodes:
            raise SourceContractError("Graph index cannot be empty.")
        buckets: dict[tuple[int, int], list[GraphNode]] = {}
        for node in self.nodes:
            key = self._bucket_key(node.lat, node.lng)
            buckets.setdefault(key, []).append(node)
        immutable = {
            key: tuple(sorted(values, key=lambda node: node.node_id))
            for key, values in buckets.items()
        }
        lat_cells = [key[0] for key in immutable]
        lng_cells = [key[1] for key in immutable]
        object.__setattr__(self, "_grid", MappingProxyType(immutable))
        object.__setattr__(self, "_grid_extents", (
            min(lat_cells), max(lat_cells), min(lng_cells), max(lng_cells),
        ))

    @classmethod
    def _bucket_key(cls, lat: float, lng: float) -> tuple[int, int]:
        return (
            math.floor(lat / cls.GRID_CELL_DEGREES),
            math.floor(lng / cls.GRID_CELL_DEGREES),
        )

    @classmethod
    def from_graph_file(cls, path: str | Path) -> "GraphIndex":
        graph_bytes = Path(path).read_bytes()
        try:
            payload = json.loads(graph_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise SourceContractError("Graph index file is not valid JSON.") from err
        return cls.from_graph_payload(
            payload,
            graph_revision=hashlib.sha256(graph_bytes).hexdigest(),
        )

    @classmethod
    def from_graph_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        graph_revision: str,
    ) -> "GraphIndex":
        if not isinstance(graph_revision, str) or len(graph_revision) < 8:
            raise SourceContractError("Graph revision must be a stable non-empty identifier.")
        metadata = payload.get("meta")
        raw_nodes = payload.get("nodes")
        if not isinstance(metadata, Mapping) or not isinstance(raw_nodes, Mapping):
            raise SourceContractError("Graph payload must contain meta and nodes mappings.")
        raw_bounds = metadata.get("bbox") or metadata.get("actual_bounds")
        if not isinstance(raw_bounds, list) or len(raw_bounds) != 4:
            raise SourceContractError("Graph payload must publish four coordinate bounds.")
        bounds = tuple(_finite_float(value, "graph bound") for value in raw_bounds)
        if not (-90 <= bounds[0] < bounds[2] <= 90):
            raise SourceContractError("Graph latitude bounds are invalid.")
        if not (-180 <= bounds[1] < bounds[3] <= 180):
            raise SourceContractError("Graph longitude bounds are invalid.")

        return cls.from_runtime_nodes(
            raw_nodes,
            bounds=bounds,
            graph_revision=graph_revision,
        )

    @classmethod
    def from_runtime_nodes(
        cls,
        nodes: Mapping[int | str, Sequence[float]] | Iterable[GraphNode],
        *,
        bounds: Sequence[float],
        graph_revision: str,
    ) -> "GraphIndex":
        """Build from an already-loaded router graph without rereading JSON."""

        if not isinstance(graph_revision, str) or len(graph_revision) < 8:
            raise SourceContractError("Graph revision must be a stable non-empty identifier.")
        if len(bounds) != 4:
            raise SourceContractError("Graph runtime bounds must contain four numbers.")
        normalised_bounds = tuple(_finite_float(value, "graph bound") for value in bounds)
        if not (-90 <= normalised_bounds[0] < normalised_bounds[2] <= 90):
            raise SourceContractError("Graph latitude bounds are invalid.")
        if not (-180 <= normalised_bounds[1] < normalised_bounds[3] <= 180):
            raise SourceContractError("Graph longitude bounds are invalid.")

        if isinstance(nodes, Mapping):
            items: Iterable[tuple[int | str, Any]] = nodes.items()
        else:
            materialized = tuple(nodes)
            if any(not isinstance(node, GraphNode) for node in materialized):
                raise SourceContractError("Runtime graph nodes must be GraphNode records.")
            ordered = tuple(sorted(materialized, key=lambda node: node.node_id))
            return cls(graph_revision, normalised_bounds, ordered)

        parsed_nodes: list[GraphNode] = []
        for raw_id, raw_coordinate in items:
            try:
                node_id = int(raw_id)
            except (TypeError, ValueError) as err:
                raise SourceContractError("Graph contains a non-integer node id.") from err
            lat, lng = _coordinate(raw_coordinate, order="lat_lng")
            parsed_nodes.append(GraphNode(node_id, lat, lng))
        if not parsed_nodes:
            raise SourceContractError("Graph index cannot be empty.")
        parsed_nodes.sort(key=lambda node: node.node_id)
        return cls(graph_revision, normalised_bounds, tuple(parsed_nodes))

    def contains(self, coordinate: tuple[float, float]) -> bool:
        lat, lng = coordinate
        return self.bounds[0] <= lat <= self.bounds[2] and self.bounds[1] <= lng <= self.bounds[3]

    def snap(self, coordinate: tuple[float, float]) -> SnappedNode:
        """Find the exact nearest node with grid-based branch-and-bound."""

        origin_lat_cell, origin_lng_cell = self._bucket_key(*coordinate)
        min_lat_cell, max_lat_cell, min_lng_cell, max_lng_cell = self._grid_extents
        max_ring = max(
            abs(origin_lat_cell - min_lat_cell),
            abs(origin_lat_cell - max_lat_cell),
            abs(origin_lng_cell - min_lng_cell),
            abs(origin_lng_cell - max_lng_cell),
        )
        nearest: Optional[GraphNode] = None
        nearest_distance = math.inf
        visited: set[tuple[int, int]] = set()
        for ring in range(max_ring + 1):
            low_lat = max(min_lat_cell, origin_lat_cell - ring)
            high_lat = min(max_lat_cell, origin_lat_cell + ring)
            low_lng = max(min_lng_cell, origin_lng_cell - ring)
            high_lng = min(max_lng_cell, origin_lng_cell + ring)
            for lat_cell in range(low_lat, high_lat + 1):
                for lng_cell in range(low_lng, high_lng + 1):
                    key = (lat_cell, lng_cell)
                    if key in visited:
                        continue
                    visited.add(key)
                    for node in self._grid.get(key, ()):
                        distance = haversine_m(coordinate, (node.lat, node.lng))
                        if distance < nearest_distance or (
                            math.isclose(distance, nearest_distance, abs_tol=1e-9)
                            and nearest is not None
                            and node.node_id < nearest.node_id
                        ):
                            nearest = node
                            nearest_distance = distance
            if nearest is not None and nearest_distance <= self._outside_grid_lower_bound_m(
                coordinate,
                low_lat,
                high_lat,
                low_lng,
                high_lng,
            ):
                break
        assert nearest is not None
        return SnappedNode(
            nearest.node_id,
            nearest_distance,
        )

    def _outside_grid_lower_bound_m(
        self,
        coordinate: tuple[float, float],
        low_lat_cell: int,
        high_lat_cell: int,
        low_lng_cell: int,
        high_lng_cell: int,
    ) -> float:
        """Conservative distance to any unsearched grid cell."""

        min_lat_cell, max_lat_cell, min_lng_cell, max_lng_cell = self._grid_extents
        lat, lng = coordinate
        bounds: list[float] = []
        if low_lat_cell > min_lat_cell:
            boundary = low_lat_cell * self.GRID_CELL_DEGREES
            bounds.append(EARTH_RADIUS_M * math.radians(max(0.0, lat - boundary)))
        if high_lat_cell < max_lat_cell:
            boundary = (high_lat_cell + 1) * self.GRID_CELL_DEGREES
            bounds.append(EARTH_RADIUS_M * math.radians(max(0.0, boundary - lat)))
        # A conservative longitude scale valid throughout the graph bounds.
        longitude_scale = math.cos(math.radians(max(abs(self.bounds[0]), abs(self.bounds[2]))))
        if low_lng_cell > min_lng_cell:
            boundary = low_lng_cell * self.GRID_CELL_DEGREES
            bounds.append(
                EARTH_RADIUS_M * math.radians(max(0.0, lng - boundary)) * longitude_scale
            )
        if high_lng_cell < max_lng_cell:
            boundary = (high_lng_cell + 1) * self.GRID_CELL_DEGREES
            bounds.append(
                EARTH_RADIUS_M * math.radians(max(0.0, boundary - lng)) * longitude_scale
            )
        return min(bounds, default=math.inf)


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise CandidateValidationError("INVALID_NUMBER", f"{label} must be numeric.")
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise CandidateValidationError("INVALID_NUMBER", f"{label} must be numeric.") from err
    if not math.isfinite(result):
        raise CandidateValidationError("INVALID_NUMBER", f"{label} must be finite.")
    return result


def _coordinate(value: Any, *, order: str = "lat_lng") -> tuple[float, float]:
    if isinstance(value, Mapping):
        allowed = {"lat", "latitude", "lng", "lon", "longitude"}
        if set(value) - allowed:
            raise CandidateValidationError("INVALID_COORDINATE", "Coordinate contains unknown fields.")
        lat_value = value.get("lat", value.get("latitude"))
        lng_value = value.get("lng", value.get("lon", value.get("longitude")))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        if order == "lng_lat":
            lng_value, lat_value = value
        else:
            lat_value, lng_value = value
    else:
        raise CandidateValidationError(
            "INVALID_COORDINATE",
            "Coordinate must be {lat,lng} or a two-number pair.",
        )
    lat = _finite_float(lat_value, "latitude")
    lng = _finite_float(lng_value, "longitude")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise CandidateValidationError("INVALID_COORDINATE", "Coordinate is outside WGS84 bounds.")
    return round(lat, 6), round(lng, 6)


def _normalise_modes(value: Any) -> tuple[str, ...]:
    raw_values: Sequence[Any]
    if isinstance(value, str):
        raw_values = [part for part in re.split(r"[,|/]", value) if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raise CandidateValidationError("INVALID_MODES", "vehicle_modes must be a list or string.")
    modes: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            raise CandidateValidationError("INVALID_MODES", "Vehicle modes must be strings.")
        key = _SPACE_RE.sub(" ", raw.strip().lower().replace("-", "_"))
        canonical = _MODE_ALIASES.get(key)
        if canonical is None and raw.strip().upper() in CANONICAL_VEHICLE_MODES:
            canonical = raw.strip().upper()
        if canonical is None:
            raise CandidateValidationError("INVALID_MODES", f"Unknown vehicle mode: {raw!r}.")
        if canonical == "FERRY_MARITIME":
            raise CandidateValidationError(
                "UNSUPPORTED_MARITIME_OVERLAY",
                "Maritime links require the multimodal timetable/terminal "
                "review workflow and cannot enter the road graph queue.",
            )
        modes.add(canonical)
    if not modes:
        raise CandidateValidationError("INVALID_MODES", "At least one vehicle mode is required.")
    return tuple(sorted(modes))


def _road_quality(value: Any) -> tuple[float, bool]:
    if value is None or value == "":
        return DEFAULT_ROAD_QUALITY, True
    if isinstance(value, str) and value.strip().lower() in _ROAD_QUALITY_LABELS:
        return _ROAD_QUALITY_LABELS[value.strip().lower()], False
    quality = _finite_float(value, "road_quality")
    if not 0.0 <= quality <= 1.0:
        raise CandidateValidationError("INVALID_ROAD_QUALITY", "road_quality must be 0..1.")
    return quality, False


def _safe_excerpt(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise CandidateValidationError("MISSING_EXCERPT", "Each tip needs a source excerpt.")
    cleaned = _SPACE_RE.sub(" ", _CONTROL_CHARS_RE.sub(" ", value)).strip()
    if len(cleaned) < 8:
        raise CandidateValidationError("MISSING_EXCERPT", "Source excerpt is too short to audit.")
    return cleaned[:limit]


@dataclass(frozen=True)
class ParsedShortcutTip:
    source_tip_id: str
    geometry: tuple[tuple[float, float], ...]
    vehicle_modes: tuple[str, ...]
    claimed_confidence: float
    claimed_distance_m: Optional[float]
    claimed_duration_s: Optional[float]
    road_quality: float
    road_quality_is_default: bool
    excerpt: str
    parser: str


@dataclass(frozen=True, order=True)
class ProvenanceRecord:
    source_id: str
    source_url: str
    document_id: str
    retrieved_at: str
    content_sha256: str
    source_tip_id: str
    parser: str
    excerpt_sha256: str
    excerpt: str


@dataclass(frozen=True)
class GraphOverrideRecord:
    """A deterministic inactive overlay candidate for an external review queue."""

    schema_version: int
    override_id: str
    graph_revision: str
    source_node: int
    target_node: int
    geometry: tuple[tuple[float, float], ...]
    applicable_vehicle_modes: tuple[str, ...]
    geometry_distance_m: float
    claimed_distance_m: Optional[float]
    claimed_duration_s: Optional[float]
    road_quality: float
    road_quality_is_default: bool
    confidence: float
    endpoint_snap_m: tuple[float, float]
    provenance: tuple[ProvenanceRecord, ...]
    validation_flags: tuple[str, ...]
    review_state: str = REVIEW_REQUIRED
    activation_allowed: bool = False

    def __post_init__(self) -> None:
        if self.review_state != REVIEW_REQUIRED or self.activation_allowed:
            raise SourceContractError("Crowd-sourced graph overrides cannot be activated here.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RejectedTip:
    document_id: str
    source_tip_id: Optional[str]
    code: str
    detail: str


@dataclass(frozen=True)
class IngestionBatchResult:
    records: tuple[GraphOverrideRecord, ...]
    rejections: tuple[RejectedTip, ...]
    documents_processed: int
    tips_parsed: int
    duplicates: int
    inserted: int

    @property
    def accepted(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
            "rejections": [asdict(rejection) for rejection in self.rejections],
            "summary": {
                "documents_processed": self.documents_processed,
                "tips_parsed": self.tips_parsed,
                "accepted": self.accepted,
                "duplicates": self.duplicates,
                "inserted": self.inserted,
                "activation_allowed": False,
            },
        }


class InMemoryShortcutReviewQueue:
    """Thread-safe reference queue with deterministic idempotent upserts.

    Production can replace this object with a durable adapter exposing the same
    atomic ``upsert_many`` contract. It is intentionally a review queue, not a
    route graph.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_REVIEW_QUEUE_ENTRIES,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) \
                or not 1 <= max_entries <= MAX_REVIEW_QUEUE_ENTRIES:
            raise SourceContractError(
                "Review queue max_entries must be an integer between 1 and "
                f"{MAX_REVIEW_QUEUE_ENTRIES}."
            )
        self.max_entries = max_entries
        self._records: dict[str, GraphOverrideRecord] = {}
        self._lock = threading.RLock()

    def upsert(self, record: GraphOverrideRecord) -> tuple[GraphOverrideRecord, bool]:
        return self.upsert_many((record,))[0]

    def upsert_many(
        self,
        records: Sequence[GraphOverrideRecord],
    ) -> tuple[tuple[GraphOverrideRecord, bool], ...]:
        """Atomically upsert a bounded candidate batch.

        Capacity is preflighted against every new identity before replacing the
        queue state. A rejected batch therefore cannot partially fill the
        process-local review queue.
        """
        if any(not isinstance(record, GraphOverrideRecord) for record in records):
            raise SourceContractError(
                "Review queue accepts only GraphOverrideRecord values."
            )
        with self._lock:
            new_ids = {
                record.override_id for record in records
                if record.override_id not in self._records
            }
            if len(self._records) + len(new_ids) > self.max_entries:
                raise SourceContractError(
                    "Shortcut review queue capacity would be exceeded."
                )

            proposed = dict(self._records)
            results: list[tuple[GraphOverrideRecord, bool]] = []
            for record in records:
                existing = proposed.get(record.override_id)
                if existing is None:
                    proposed[record.override_id] = record
                    results.append((record, True))
                else:
                    merged = _merge_records(existing, record)
                    proposed[record.override_id] = merged
                    results.append((merged, False))
            self._records = proposed
            return tuple(results)

    def get(self, override_id: str) -> Optional[GraphOverrideRecord]:
        with self._lock:
            return self._records.get(override_id)

    def snapshot(self) -> tuple[GraphOverrideRecord, ...]:
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))


def _merge_optional_measurement(first: Optional[float], second: Optional[float]) -> Optional[float]:
    if first is None:
        return second
    if second is None:
        return first
    # Values remain untrusted claims. Choosing the conservative maximum is
    # commutative, associative and stable regardless of source document order.
    return max(first, second)


def _merge_records(first: GraphOverrideRecord, second: GraphOverrideRecord) -> GraphOverrideRecord:
    if first.override_id != second.override_id:
        raise SourceContractError("Only identical override identities can be merged.")
    if first.geometry != second.geometry:
        raise SourceContractError(
            "Identical override identities cannot contain divergent geometry."
        )
    provenances = tuple(sorted(set(first.provenance + second.provenance)))
    if len(provenances) > MAX_PROVENANCE_RECORDS_PER_CANDIDATE:
        raise SourceContractError(
            "Shortcut candidate provenance capacity would be exceeded."
        )
    return replace(
        first,
        claimed_distance_m=_merge_optional_measurement(
            first.claimed_distance_m, second.claimed_distance_m,
        ),
        claimed_duration_s=_merge_optional_measurement(
            first.claimed_duration_s, second.claimed_duration_s,
        ),
        # Crowd reports cannot improve one another's quality estimate. The
        # conservative minimum is order-independent for any number of reports.
        road_quality=min(first.road_quality, second.road_quality),
        road_quality_is_default=(
            first.road_quality_is_default or second.road_quality_is_default
        ),
        # More reports do not manufacture trust. Keep the strongest individually
        # bounded claim, still below the activation boundary and review-gated.
        confidence=max(first.confidence, second.confidence),
        provenance=provenances,
    )


class _VisibleTextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag.lower() in {"br", "p", "li", "article", "section", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif tag.lower() in {"p", "li", "article", "section", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


_TEXT_TIP_RE = re.compile(
    r"(?i)\bshortcut(?:[-_ ]tip)?\b[^\n]{0,1600}?"
    r"\bfrom\s*[:=]?\s*(-?\d{1,3}(?:\.\d+)?)\s*[,/]\s*(-?\d{1,3}(?:\.\d+)?)"
    r"\s*(?:->|to)\s*[:=]?\s*(-?\d{1,3}(?:\.\d+)?)\s*[,/]\s*(-?\d{1,3}(?:\.\d+)?)"
    r"(?P<tail>[^\n]{0,1000})"
)


def _extract_field(tail: str, names: str) -> Optional[str]:
    match = re.search(
        rf"(?i)\b(?:{names})\s*[:=]\s*([^;|]+?)(?=\s+\w+\s*[:=]|[;|]|$)",
        tail,
    )
    return match.group(1).strip() if match else None


def _parse_text_tips(text: str, *, excerpt_limit: int) -> list[ParsedShortcutTip]:
    parsed: list[ParsedShortcutTip] = []
    for match in _TEXT_TIP_RE.finditer(text):
        tail = match.group("tail")
        modes_raw = _extract_field(tail, "modes?|vehicles?")
        if modes_raw is None:
            # Requiring an explicit mode prevents an ambiguous alley claim from
            # being silently applied to trucks or cars.
            raise CandidateValidationError("INVALID_MODES", "Text tips require an explicit modes field.")
        confidence_raw = _extract_field(tail, "confidence|conf")
        distance_raw = _extract_field(tail, "distance_m|metres|meters")
        duration_min_raw = _extract_field(tail, "duration_min|minutes|mins")
        quality_raw = _extract_field(tail, "road_quality|quality")
        tip_id = _extract_field(tail, "id|tip_id") or hashlib.sha256(
            match.group(0).encode("utf-8")
        ).hexdigest()[:16]
        quality, quality_default = _road_quality(quality_raw)
        confidence = _finite_float(confidence_raw if confidence_raw is not None else 0.35, "confidence")
        claimed_distance = (
            _finite_float(distance_raw, "distance_m") if distance_raw is not None else None
        )
        claimed_duration = (
            _finite_float(duration_min_raw, "duration_min") * 60.0
            if duration_min_raw is not None else None
        )
        parsed.append(ParsedShortcutTip(
            source_tip_id=str(tip_id)[:128],
            geometry=(
                _coordinate([match.group(1), match.group(2)]),
                _coordinate([match.group(3), match.group(4)]),
            ),
            vehicle_modes=_normalise_modes(modes_raw),
            claimed_confidence=confidence,
            claimed_distance_m=claimed_distance,
            claimed_duration_s=claimed_duration,
            road_quality=quality,
            road_quality_is_default=quality_default,
            excerpt=_safe_excerpt(match.group(0), excerpt_limit),
            parser="geocoded_text_v1",
        ))
    return parsed


def _parse_json_tip(raw: Any, *, excerpt_limit: int) -> ParsedShortcutTip:
    if not isinstance(raw, Mapping):
        raise CandidateValidationError("INVALID_TIP_SCHEMA", "Each JSON tip must be an object.")
    allowed = {
        "tip_id", "start", "end", "via", "vehicle_modes", "confidence",
        "claimed_distance_m", "claimed_duration_minutes", "road_quality", "snippet",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise CandidateValidationError(
            "INVALID_TIP_SCHEMA",
            "JSON tip contains unknown fields: " + ", ".join(sorted(map(str, unknown))),
        )
    tip_id = raw.get("tip_id")
    if not isinstance(tip_id, str) or not _DOCUMENT_ID_RE.fullmatch(tip_id):
        raise CandidateValidationError("INVALID_TIP_ID", "tip_id is required and malformed.")
    via = raw.get("via", [])
    if not isinstance(via, list) or len(via) > 62:
        raise CandidateValidationError("INVALID_GEOMETRY", "via must contain at most 62 points.")
    geometry = (
        _coordinate(raw.get("start")),
        *(_coordinate(point) for point in via),
        _coordinate(raw.get("end")),
    )
    confidence = _finite_float(raw.get("confidence", 0.35), "confidence")
    claimed_distance = raw.get("claimed_distance_m")
    claimed_duration = raw.get("claimed_duration_minutes")
    quality, quality_default = _road_quality(raw.get("road_quality"))
    return ParsedShortcutTip(
        source_tip_id=tip_id,
        geometry=geometry,
        vehicle_modes=_normalise_modes(raw.get("vehicle_modes")),
        claimed_confidence=confidence,
        claimed_distance_m=(
            _finite_float(claimed_distance, "claimed_distance_m")
            if claimed_distance is not None else None
        ),
        claimed_duration_s=(
            _finite_float(claimed_duration, "claimed_duration_minutes") * 60.0
            if claimed_duration is not None else None
        ),
        road_quality=quality,
        road_quality_is_default=quality_default,
        excerpt=_safe_excerpt(raw.get("snippet"), excerpt_limit),
        parser="geocoded_json_v1",
    )


def _decode_document(document: SourceDocument, max_bytes: int) -> tuple[str, bytes]:
    raw = document.content.encode("utf-8") if isinstance(document.content, str) else document.content
    if len(raw) > max_bytes:
        raise SourceContractError("Document exceeds the configured size cap.")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError as err:
        raise SourceContractError("Document content must be valid UTF-8.") from err


def _parse_document(
    document: SourceDocument,
    policy: SourcePolicy,
) -> tuple[list[ParsedShortcutTip], bytes]:
    registration = policy.registration(document.source_id)
    policy.validate_target(
        document.source_id,
        document.source_url,
        require_pinned_url=bool(registration.pinned_urls),
    )
    content_type = _normalise_content_type(document.content_type)
    if content_type not in registration.allowed_content_types:
        raise SourceContractError("Document MIME type is not allowed for this source.")
    text, raw = _decode_document(document, policy.max_document_bytes)
    if content_type == "application/json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise SourceContractError("Source JSON is malformed.") from err
        if not isinstance(payload, Mapping) or set(payload) - {"schema_version", "tips"}:
            raise SourceContractError("Source JSON must contain only schema_version and tips.")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SourceContractError("Unsupported shortcut source schema version.")
        raw_tips = payload.get("tips")
        if not isinstance(raw_tips, list):
            raise SourceContractError("Source JSON tips must be a list.")
        if len(raw_tips) > policy.max_tips_per_document:
            raise SourceContractError("Document contains too many shortcut tips.")
        parsed = [_parse_json_tip(tip, excerpt_limit=policy.max_excerpt_chars) for tip in raw_tips]
    else:
        if content_type == "text/html":
            html_parser = _VisibleTextParser()
            try:
                html_parser.feed(text)
                html_parser.close()
            except html.parser.HTMLParseError as err:
                raise SourceContractError("Source HTML could not be parsed safely.") from err
            # HTML presentation frequently wraps a single tip across elements
            # and lines. Collapse only layout whitespace; the source excerpt is
            # still content-addressed against the original document bytes.
            text = _SPACE_RE.sub(" ", html_parser.text())
        parsed = _parse_text_tips(text, excerpt_limit=policy.max_excerpt_chars)
        if len(parsed) > policy.max_tips_per_document:
            raise SourceContractError("Document contains too many shortcut tips.")
    return parsed, raw


def _polyline_distance_m(geometry: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_m(first, second) for first, second in zip(geometry, geometry[1:]))


def _record_identity(
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
    canonical = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_revision": graph_revision,
            "source_node": source_node,
            "target_node": target_node,
            "vehicle_modes": sorted(modes),
            "geometry_sha256": hashlib.sha256(
                geometry_canonical.encode("utf-8")
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "shortcut-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _validate_tip(
    tip: ParsedShortcutTip,
    *,
    document: SourceDocument,
    document_bytes: bytes,
    registration: SourceRegistration,
    graph: GraphIndex,
    max_endpoint_snap_m: float,
) -> GraphOverrideRecord:
    if not (MIN_CROWD_CONFIDENCE <= tip.claimed_confidence <= 1.0):
        raise CandidateValidationError(
            "INVALID_CONFIDENCE",
            "Claimed confidence must be between 0.20 and 1.0.",
        )
    if len(tip.geometry) < 2 or len(tip.geometry) > 64:
        raise CandidateValidationError("INVALID_GEOMETRY", "Geometry must contain 2..64 points.")
    if any(not graph.contains(point) for point in tip.geometry):
        raise CandidateValidationError(
            "OUTSIDE_GRAPH_BOUNDS",
            "Shortcut geometry is outside this graph extract.",
        )

    geometry_distance = _polyline_distance_m(tip.geometry)
    max_length = (
        MAX_MARITIME_CANDIDATE_LENGTH_M
        if tip.vehicle_modes == ("FERRY_MARITIME",)
        else MAX_ROAD_CANDIDATE_LENGTH_M
    )
    if not (MIN_CANDIDATE_LENGTH_M <= geometry_distance <= max_length):
        raise CandidateValidationError(
            "IMPLAUSIBLE_LENGTH",
            f"Candidate geometry must be {MIN_CANDIDATE_LENGTH_M:g}..{max_length:g} metres.",
        )

    source_snap = graph.snap(tip.geometry[0])
    target_snap = graph.snap(tip.geometry[-1])
    if source_snap.distance_m > max_endpoint_snap_m or target_snap.distance_m > max_endpoint_snap_m:
        raise CandidateValidationError(
            "NOT_NEAR_GRAPH",
            "Candidate endpoints are too far from this graph's routable nodes.",
        )
    if source_snap.node_id == target_snap.node_id:
        raise CandidateValidationError(
            "SAME_GRAPH_NODE",
            "Candidate endpoints snap to the same graph node.",
        )

    claimed_distance = tip.claimed_distance_m
    if claimed_distance is not None:
        if claimed_distance <= 0:
            raise CandidateValidationError("IMPLAUSIBLE_DISTANCE", "Claimed distance must be positive.")
        ratio = claimed_distance / geometry_distance
        if not 0.75 <= ratio <= 4.0:
            raise CandidateValidationError(
                "IMPLAUSIBLE_DISTANCE",
                "Claimed distance is inconsistent with geocoded geometry.",
            )
    effective_distance = claimed_distance or geometry_distance

    if tip.claimed_duration_s is not None:
        if tip.claimed_duration_s <= 0:
            raise CandidateValidationError("IMPLAUSIBLE_DURATION", "Claimed duration must be positive.")
        speed_kph = effective_distance / tip.claimed_duration_s * 3.6
        maximum_speed = 90.0 if tip.vehicle_modes == ("FERRY_MARITIME",) else 130.0
        if not 1.0 <= speed_kph <= maximum_speed:
            raise CandidateValidationError(
                "IMPLAUSIBLE_SPEED",
                "Claimed distance and duration imply an implausible speed.",
            )

    endpoint_quality = max(
        0.50,
        1.0 - ((source_snap.distance_m + target_snap.distance_m) / (2 * max_endpoint_snap_m)) * 0.5,
    )
    confidence = round(min(
        MAX_CROWD_CONFIDENCE,
        registration.confidence_ceiling,
        tip.claimed_confidence,
    ) * endpoint_quality, 4)
    retrieved = document.retrieved_at.astimezone(timezone.utc).isoformat()
    provenance = ProvenanceRecord(
        source_id=document.source_id,
        source_url=_canonical_https_url(document.source_url)[0],
        document_id=document.document_id,
        retrieved_at=retrieved,
        content_sha256=hashlib.sha256(document_bytes).hexdigest(),
        source_tip_id=tip.source_tip_id,
        parser=tip.parser,
        excerpt_sha256=hashlib.sha256(tip.excerpt.encode("utf-8")).hexdigest(),
        excerpt=tip.excerpt,
    )
    override_id = _record_identity(
        graph.graph_revision,
        source_snap.node_id,
        target_snap.node_id,
        tip.vehicle_modes,
        tip.geometry,
    )
    return GraphOverrideRecord(
        schema_version=SCHEMA_VERSION,
        override_id=override_id,
        graph_revision=graph.graph_revision,
        source_node=source_snap.node_id,
        target_node=target_snap.node_id,
        geometry=tip.geometry,
        applicable_vehicle_modes=tip.vehicle_modes,
        geometry_distance_m=round(geometry_distance, 3),
        claimed_distance_m=(round(claimed_distance, 3) if claimed_distance is not None else None),
        claimed_duration_s=(
            round(tip.claimed_duration_s, 3) if tip.claimed_duration_s is not None else None
        ),
        road_quality=round(tip.road_quality, 3),
        road_quality_is_default=tip.road_quality_is_default,
        confidence=confidence,
        endpoint_snap_m=(round(source_snap.distance_m, 3), round(target_snap.distance_m, 3)),
        provenance=(provenance,),
        validation_flags=(
            "SOURCE_CONTRACT_VALID",
            "COORDINATES_NORMALIZED_WGS84",
            "GRAPH_BOUNDS_VALID",
            "ENDPOINTS_SNAPPED",
            "ROAD_PLAUSIBILITY_VALID",
            "UNTRUSTED_CROWD_CLAIM",
        ),
    )


class ShortcutTipPipeline:
    """Parse, quarantine, validate and idempotently queue source documents."""

    def __init__(
        self,
        policy: SourcePolicy,
        graph: GraphIndex,
        *,
        review_queue: Optional[InMemoryShortcutReviewQueue] = None,
        max_endpoint_snap_m: float = DEFAULT_MAX_ENDPOINT_SNAP_M,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not math.isfinite(max_endpoint_snap_m) or not (10 <= max_endpoint_snap_m <= 2_000):
            raise SourceContractError("Endpoint snap limit must be 10..2000 metres.")
        self.policy = policy
        self.graph = graph
        self.review_queue = review_queue or InMemoryShortcutReviewQueue()
        self.max_endpoint_snap_m = max_endpoint_snap_m
        self._now_provider = now_provider

    def ingest(self, documents: Iterable[SourceDocument]) -> IngestionBatchResult:
        materialized = tuple(islice(
            iter(documents), self.policy.max_documents_per_batch + 1,
        ))
        if len(materialized) > self.policy.max_documents_per_batch:
            raise SourceContractError("Batch contains too many source documents.")
        if any(not isinstance(document, SourceDocument) for document in materialized):
            raise SourceContractError(
                "Every shortcut source item must be a SourceDocument."
            )
        records_by_id: dict[str, GraphOverrideRecord] = {}
        rejections: list[RejectedTip] = []
        parsed_count = 0
        duplicates = 0
        inserted = 0
        parsed_documents: list[tuple[SourceDocument, list[ParsedShortcutTip], bytes]] = []

        for document in materialized:
            # Future timestamps undermine provenance ordering and audit logs.
            now_utc = self._now_provider()
            if now_utc.tzinfo is None or now_utc.utcoffset() is None:
                raise SourceContractError("Pipeline clock must return a timezone-aware datetime.")
            if document.retrieved_at.astimezone(timezone.utc) > (
                now_utc.astimezone(timezone.utc) + timedelta(minutes=5)
            ):
                rejections.append(RejectedTip(
                    document.document_id,
                    None,
                    "SOURCE_CONTRACT",
                    "Document retrieved_at cannot be in the future.",
                ))
                continue
            try:
                tips, raw = _parse_document(document, self.policy)
            except (SourceContractError, CandidateValidationError) as err:
                code = err.code if isinstance(err, CandidateValidationError) else "SOURCE_CONTRACT"
                rejections.append(RejectedTip(
                    document.document_id,
                    None,
                    code,
                    str(err),
                ))
                continue
            parsed_count += len(tips)
            if parsed_count > self.policy.max_tips_per_batch:
                raise SourceContractError("Batch contains too many shortcut tips.")
            parsed_documents.append((document, tips, raw))

        # The review queue is not mutated until the entire batch passes its
        # aggregate resource cap, preserving idempotent retry semantics.
        for document, tips, raw in parsed_documents:
            registration = self.policy.registration(document.source_id)
            for tip in tips:
                try:
                    record = _validate_tip(
                        tip,
                        document=document,
                        document_bytes=raw,
                        registration=registration,
                        graph=self.graph,
                        max_endpoint_snap_m=self.max_endpoint_snap_m,
                    )
                except CandidateValidationError as err:
                    rejections.append(RejectedTip(
                        document.document_id,
                        tip.source_tip_id,
                        err.code,
                        err.detail,
                    ))
                    continue

                existing_batch = records_by_id.get(record.override_id)
                if existing_batch is not None:
                    records_by_id[record.override_id] = _merge_records(existing_batch, record)
                    duplicates += 1
                    continue
                records_by_id[record.override_id] = record

        ordered_ids = sorted(records_by_id)
        queue_results = self.review_queue.upsert_many(tuple(
            records_by_id[override_id] for override_id in ordered_ids
        ))
        for override_id, (stored, is_new) in zip(ordered_ids, queue_results):
            records_by_id[override_id] = stored
            if is_new:
                inserted += 1
            else:
                duplicates += 1

        records = tuple(records_by_id[key] for key in sorted(records_by_id))
        return IngestionBatchResult(
            records=records,
            rejections=tuple(rejections),
            documents_processed=len(materialized),
            tips_parsed=parsed_count,
            duplicates=duplicates,
            inserted=inserted,
        )


__all__ = [
    "AllowlistedSourceFetcher",
    "CandidateValidationError",
    "FetchResponse",
    "FetchSafetyError",
    "GraphIndex",
    "GraphOverrideRecord",
    "InMemoryShortcutReviewQueue",
    "IngestionBatchResult",
    "ProvenanceRecord",
    "RejectedTip",
    "SourceContractError",
    "SourceDocument",
    "SourcePolicy",
    "SourceRegistration",
    "ShortcutTipPipeline",
    "haversine_m",
    "resolve_public_ips",
    "source_policy_from_environment",
]
