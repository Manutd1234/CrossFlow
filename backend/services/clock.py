"""The single source of "now" for the whole backend.

Everything time-dependent takes an optional `now` argument and falls back to
this module, which gives the test suite one seam to freeze instead of trying to
monkeypatch datetime (a C type, so that doesn't work anyway).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

# Batam is WIB (UTC+7). Kept explicit because the demo laptop is often in
# Singapore (UTC+8) — an offset-naive timestamp would silently shift every
# ferry departure by an hour.
BATAM_TZ = timezone(timedelta(hours=7), name="WIB")

_frozen: Optional[datetime] = None


def now() -> datetime:
    """Current Batam time, always timezone-aware."""
    if _frozen is not None:
        return _frozen
    return datetime.now(BATAM_TZ)


def iso(dt: datetime) -> str:
    """Serialize to ISO 8601 with an explicit offset.

    This is the project's only time format on the wire. The frontend previously
    received 12-hour strings from its mock data and 24-hour strings from the
    API, so every timestamp on screen visibly rewrote itself whenever the
    backend connected or dropped.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BATAM_TZ)
    return dt.astimezone(BATAM_TZ).isoformat(timespec="seconds")


def minutes_between(start: datetime, end: datetime) -> int:
    return int(round((end - start).total_seconds() / 60.0))


def freeze(dt: datetime) -> None:
    global _frozen
    _frozen = dt


def unfreeze() -> None:
    global _frozen
    _frozen = None


@contextmanager
def frozen(dt: datetime):
    """Pin `now()` for the duration of the block."""
    previous = _frozen
    freeze(dt)
    try:
        yield dt
    finally:
        globals()["_frozen"] = previous
