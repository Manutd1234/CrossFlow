"""Deterministic identities for route requests.

Route identities are deliberately based on the validated request, rather than
on generated timestamps or provider output.  This makes a route replayable and
gives callers an opaque, content-addressed handle for a persisted response.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Mapping


ROUTE_ID_VERSION = "route-v1"
SHORT_ROUTE_CODE_LENGTH = 7
SHORT_ROUTE_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _canonical_value(value: Any) -> Any:
    """Return a JSON-safe value with stable ordering and time formatting."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical_value(item) for item in value), key=_sort_key)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        # Offset-equivalent instants should have one identity.  Naive values
        # are retained as-is because they are invalid for scheduled requests
        # and changing them here would hide a caller validation error.
        normalized = (
            value.astimezone(timezone.utc)
            if value.tzinfo is not None and value.utcoffset() is not None
            else value
        )
        return normalized.isoformat(timespec="microseconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Route identity values must be finite JSON numbers.")
        # JSON has one representation for zero in our identity contract.
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported route identity value: {type(value).__name__}")


def _sort_key(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonicalize_route_request(request: Any, *, route_kind: str = "optimize-route") -> str:
    """Serialize a validated request into the canonical identity document."""
    if not isinstance(route_kind, str) or not route_kind.strip():
        raise ValueError("route_kind must be a non-empty string")
    document = {
        "version": ROUTE_ID_VERSION,
        "route_kind": route_kind.strip(),
        "request": _canonical_value(request),
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def route_id(request: Any, *, route_kind: str = "optimize-route") -> str:
    """Return the lowercase SHA-256 digest of a canonical route request."""
    canonical = canonicalize_route_request(request, route_kind=route_kind)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_id_for_request(request: Any, *, route_kind: str = "optimize-route") -> str:
    """Descriptive alias for callers that handle several identity types."""
    return route_id(request, route_kind=route_kind)


def short_route_code(route_key: str, attempt: int = 0) -> str:
    """Return a human-friendly seven-character code for a full route ID.

    The database enforces uniqueness and can request a later deterministic
    candidate if the first seven-character projection ever collides.
    """
    if (
        not isinstance(route_key, str)
        or len(route_key) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in route_key)
    ):
        raise ValueError("route_key must be a 64-character hexadecimal SHA-256 ID")
    if not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")
    material = bytes.fromhex(route_key.lower())
    if attempt:
        material = hashlib.sha256(material + attempt.to_bytes(2, "big")).digest()
    value = int.from_bytes(material[:8], "big")
    base = len(SHORT_ROUTE_CODE_ALPHABET)
    characters = []
    for _ in range(SHORT_ROUTE_CODE_LENGTH):
        value, remainder = divmod(value, base)
        characters.append(SHORT_ROUTE_CODE_ALPHABET[remainder])
    return "".join(reversed(characters))


# Short aliases keep the identity contract convenient for storage adapters and
# callers that do not need to distinguish it from other canonical documents.
canonicalize = canonicalize_route_request
compute_route_id = route_id


__all__ = [
    "ROUTE_ID_VERSION",
    "canonicalize_route_request",
    "canonicalize",
    "route_id",
    "route_id_for_request",
    "SHORT_ROUTE_CODE_LENGTH",
    "SHORT_ROUTE_CODE_ALPHABET",
    "short_route_code",
    "compute_route_id",
]
