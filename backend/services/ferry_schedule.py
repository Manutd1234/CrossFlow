"""Published ferry timetable & modelled terminal intelligence.

Departure slots come from a versioned snapshot of official operator schedules.
The public sources do not provide a licensed machine feed for live vessel,
gate, berth, cancellation, capacity or seat availability, so those details are
never invented here. Terminal queue metrics remain explicitly modelled.
"""

import copy
import json
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from services import clock, ferry_freshness_store

BOARDING_CUTOFF_MINS = 15

@dataclass(frozen=True)
class FerryRoute:
    code: str
    operator: str
    departure_port: str
    arrival_port: str
    departure_timezone: str
    daily_departures: Tuple[time, ...]
    weekend_additions: Tuple[time, ...]
    estimated_crossing_mins: int
    source_id: str
    schedule_url: str
    booking_url: str
    effective_from: Optional[str]
    source_last_verified_at: str
    calendar_note: str


_TIMETABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "ferry_timetable.json"
with _TIMETABLE_PATH.open(encoding="utf-8") as timetable_file:
    _TIMETABLE = json.load(timetable_file)


def _parse_departure(value: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as err:
        raise RuntimeError(f"Invalid ferry timetable slot {value!r}") from err
    return parsed


def _load_routes() -> List[FerryRoute]:
    if _TIMETABLE.get("schema_version") != 1:
        raise RuntimeError("Unsupported ferry timetable schema")
    sources = {source["source_id"]: source for source in _TIMETABLE["sources"]}
    routes: List[FerryRoute] = []
    for service in _TIMETABLE["services"]:
        source = sources[service["source_id"]]
        routes.append(FerryRoute(
            code=service["service_id"],
            operator=service["operator"],
            departure_port=service["departure_port"],
            arrival_port=service["arrival_port"],
            departure_timezone=service.get(
                "departure_timezone", _TIMETABLE["timezone"],
            ),
            daily_departures=tuple(
                _parse_departure(value) for value in service["daily_departures"]
            ),
            weekend_additions=tuple(
                _parse_departure(value) for value in service["weekend_additions"]
            ),
            estimated_crossing_mins=int(service["estimated_crossing_mins"]),
            source_id=source["source_id"],
            schedule_url=source["schedule_url"],
            booking_url=source["booking_url"],
            effective_from=source["effective_from"],
            source_last_verified_at=source["last_verified_at"],
            calendar_note=source["calendar_note"],
        ))
    return routes


FERRY_ROUTES: List[FerryRoute] = _load_routes()

_verification_lock = threading.Lock()
_runtime_last_verified_at: Optional[str] = None
_runtime_latest_checked_at: Optional[str] = None

PORTS = sorted({r.departure_port for r in FERRY_ROUTES})


def published_services_for_operator(operator: str) -> Tuple[Dict[str, Any], ...]:
    """Defensive copies of every route/calendar group in the snapshot."""
    return tuple(
        copy.deepcopy(service)
        for service in _TIMETABLE["services"]
        if service["operator"] == operator
    )

# Port capacity and berth configuration metadata
PORT_METADATA: Dict[str, Dict[str, Any]] = {
    "Batam Centre": {
        "code": "BCT",
        "total_berths": 6,
        "base_customs_mins": 15,
        "base_queue_mins": 20,
        "official_reference_url": "https://batamport.bpbatam.go.id/batam-centre/",
    },
    "HarbourBay": {
        "code": "HBT",
        "total_berths": 4,
        "base_customs_mins": 10,
        "base_queue_mins": 12,
        "official_reference_url": "https://batamport.bpbatam.go.id/harbour-bay/",
    },
    "Sekupang": {
        "code": "SKP",
        "total_berths": 4,
        "base_customs_mins": 12,
        "base_queue_mins": 15,
        "official_reference_url": "https://batamport.bpbatam.go.id/sekupang/",
    },
    "Nongsa Pura": {
        "code": "NPT",
        "total_berths": 2,
        "base_customs_mins": 8,
        "base_queue_mins": 10,
        "official_reference_url": "https://batamport.bpbatam.go.id/nongsapura/",
    },
}


def _slots(route: FerryRoute, day: datetime.date) -> List[Tuple[int, datetime]]:
    """Every published departure slot for one route on one calendar day."""
    tz = ZoneInfo(route.departure_timezone)
    departures = list(route.daily_departures)
    if day.weekday() >= 5:
        departures.extend(route.weekend_additions)
    departures.sort()
    return [
        (slot, datetime.combine(day, departure, tzinfo=tz))
        for slot, departure in enumerate(departures)
    ]


def _parse_aware_timestamp(value: str, *, label: str) -> datetime:
    try:
        candidate = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be ISO 8601.") from error
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset.")
    return candidate


def _newer_timestamp(left: Optional[str], right: Optional[str]) -> Optional[str]:
    candidates = [value for value in (left, right) if value is not None]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda value: _parse_aware_timestamp(value, label="Freshness time"),
    )


def _durable_freshness() -> Tuple[Optional[str], Optional[str]]:
    stored = ferry_freshness_store.load(_TIMETABLE["snapshot_id"])
    if stored is None:
        if ferry_freshness_store.required():
            # A newly published snapshot has no row yet. Seed it through the
            # same monotonic upsert used by refreshes, so future snapshot IDs do
            # not require a data migration before their first read.
            stored = ferry_freshness_store.save(
                _TIMETABLE["snapshot_id"],
                _TIMETABLE["last_verified_at"],
                _TIMETABLE["last_verified_at"],
            )
            if stored is None:
                raise ferry_freshness_store.FreshnessStoreUnavailable(
                    "Shared ferry freshness is unavailable."
                )
        else:
            return None, None
    checked = stored.get("latest_checked_at")
    verified = stored.get("last_verified_at")
    stored_snapshot_id = stored.get("snapshot_id")
    if ferry_freshness_store.required() and (
        stored_snapshot_id != _TIMETABLE["snapshot_id"]
        or not isinstance(checked, str)
        or not isinstance(verified, str)
    ):
        raise ferry_freshness_store.FreshnessStoreUnavailable(
            "Shared ferry freshness is incomplete."
        )
    try:
        if checked is not None:
            _parse_aware_timestamp(checked, label="Stored source check time")
        if verified is not None:
            _parse_aware_timestamp(verified, label="Stored verification time")
    except ValueError as error:
        if ferry_freshness_store.required():
            raise ferry_freshness_store.FreshnessStoreUnavailable(
                "Shared ferry freshness contains invalid timestamps."
            ) from error
        return None, None
    if ferry_freshness_store.required():
        snapshot_verified = _parse_aware_timestamp(
            _TIMETABLE["last_verified_at"],
            label="Committed snapshot verification time",
        )
        stored_verified = _parse_aware_timestamp(
            verified,
            label="Stored verification time",
        )
        stored_checked = _parse_aware_timestamp(
            checked,
            label="Stored source check time",
        )
        if stored_verified < snapshot_verified or stored_checked < stored_verified:
            raise ferry_freshness_store.FreshnessStoreUnavailable(
                "Shared ferry freshness is older than the active snapshot."
            )
    return checked, verified


def record_refresh_result(
    checked_at: str,
    verified_at: Optional[str] = None,
) -> None:
    """Atomically publish one refresh's check and optional verification times.

    When shared durability is required, candidates remain private until the
    database returns the monotonic row. A failed write therefore cannot leak an
    unpersisted timestamp into a later response from this process.
    """
    checked = _parse_aware_timestamp(checked_at, label="Source check time")
    verified = (
        _parse_aware_timestamp(verified_at, label="Schedule verification time")
        if verified_at is not None
        else None
    )

    checked_iso = checked.isoformat(timespec="seconds")
    verified_iso = (
        verified.isoformat(timespec="seconds") if verified is not None else None
    )

    global _runtime_latest_checked_at, _runtime_last_verified_at
    with _verification_lock:
        merged_checked = _newer_timestamp(
            _runtime_latest_checked_at,
            checked_iso,
        )
        merged_verified = _newer_timestamp(
            _runtime_last_verified_at or _TIMETABLE["last_verified_at"],
            verified_iso,
        )

    durability_required = ferry_freshness_store.required()
    saved = ferry_freshness_store.save(
        _TIMETABLE["snapshot_id"],
        merged_checked or checked_iso,
        merged_verified or _TIMETABLE["last_verified_at"],
    )
    if saved is None:
        if durability_required:
            raise ferry_freshness_store.FreshnessStoreUnavailable(
                "The latest ferry verification could not be persisted."
            )
        saved_checked = None
        saved_verified = None
    else:
        saved_snapshot_id = saved.get("snapshot_id")
        saved_checked = saved.get("latest_checked_at")
        saved_verified = saved.get("last_verified_at")
        if (
            saved_snapshot_id != _TIMETABLE["snapshot_id"]
            or not isinstance(saved_checked, str)
            or not isinstance(saved_verified, str)
        ):
            if durability_required:
                raise ferry_freshness_store.FreshnessStoreUnavailable(
                    "The persisted ferry freshness response was incomplete."
                )
            saved_checked = None
            saved_verified = None
        try:
            if saved_checked is not None:
                _parse_aware_timestamp(saved_checked, label="Stored source check time")
            if saved_verified is not None:
                _parse_aware_timestamp(saved_verified, label="Stored verification time")
        except ValueError as error:
            if durability_required:
                raise ferry_freshness_store.FreshnessStoreUnavailable(
                    "The persisted ferry freshness response was invalid."
                ) from error
            saved_checked = None
            saved_verified = None

    if saved_checked is not None and saved_verified is not None:
        if _parse_aware_timestamp(
            saved_checked,
            label="Stored source check time",
        ) < _parse_aware_timestamp(
            saved_verified,
            label="Stored verification time",
        ):
            if durability_required:
                raise ferry_freshness_store.FreshnessStoreUnavailable(
                    "The persisted ferry freshness response was inconsistent."
                )
            saved_checked = None
            saved_verified = None

    if durability_required:
        if (
            _newer_timestamp(saved_checked, merged_checked) != saved_checked
            or _newer_timestamp(saved_verified, merged_verified) != saved_verified
        ):
            raise ferry_freshness_store.FreshnessStoreUnavailable(
                "The shared ferry freshness row did not retain the latest result."
            )

    # Only reach this point after a required shared write has been validated.
    # Optional/local operation still publishes the process-scoped candidate.
    with _verification_lock:
        _runtime_latest_checked_at = _newer_timestamp(
            _runtime_latest_checked_at,
            _newer_timestamp(merged_checked, saved_checked),
        )
        final_verified = _newer_timestamp(
            _runtime_last_verified_at or _TIMETABLE["last_verified_at"],
            _newer_timestamp(merged_verified, saved_verified),
        )
        _runtime_last_verified_at = (
            final_verified
            if final_verified != _TIMETABLE["last_verified_at"]
            else None
        )


def _sailing(
    route: FerryRoute,
    slot: int,
    departure: datetime,
    now: datetime,
    schedule_verified_at: str,
) -> Dict[str, Any]:
    sailing_id = f"{route.code}-{departure:%Y%m%d}-{slot:02d}"
    mins_until = clock.minutes_between(now, departure)
    arrival_timezone = (
        "Asia/Singapore" if route.arrival_port.endswith(" SG")
        else "Asia/Jakarta"
    )
    arrival = (
        departure + timedelta(minutes=route.estimated_crossing_mins)
    ).astimezone(ZoneInfo(arrival_timezone))

    return {
        "sailing_id": sailing_id,
        "ferry_name": route.operator,
        "operator": route.operator,
        "departure_port": route.departure_port,
        "arrival_port": route.arrival_port,
        # Preserve each terminal's local offset on the wire. In particular,
        # Singapore-origin slots must remain +08:00 rather than being silently
        # rewritten to Batam's +07:00 by the application's general clock.
        "departure_time": departure.isoformat(timespec="seconds"),
        "arrival_time": arrival.isoformat(timespec="seconds"),
        "departure_timezone": route.departure_timezone,
        "arrival_timezone": arrival_timezone,
        "estimated_crossing_mins": route.estimated_crossing_mins,
        "arrival_time_is_estimate": True,
        "minutes_until_departure": mins_until,
        "status": "SCHEDULED",
        "available_seats": None,
        "capacity": None,
        "live_status_available": False,
        "data_source": "official_timetable_snapshot",
        "schedule_source_id": route.source_id,
        "schedule_source_url": route.schedule_url,
        "booking_url": route.booking_url,
        "schedule_effective_from": route.effective_from,
        # Legacy/display field: always the latest full semantic verification.
        "schedule_last_verified_at": schedule_verified_at,
        # Immutable audit field: when the bundled source snapshot was approved.
        "schedule_snapshot_verified_at": route.source_last_verified_at,
        "schedule_calendar_note": route.calendar_note,
    }


def timetable_metadata(*, load_durable: bool = True) -> Dict[str, Any]:
    """Return a defensive copy of source and freshness metadata."""
    metadata = {
        key: copy.deepcopy(_TIMETABLE[key])
        for key in (
            "schema_version", "snapshot_id", "timezone", "last_verified_at",
            "status", "live_board_url", "limitations", "sources",
        )
    }
    durable_checked, durable_verified = (
        _durable_freshness() if load_durable else (None, None)
    )
    global _runtime_latest_checked_at, _runtime_last_verified_at
    with _verification_lock:
        if load_durable and ferry_freshness_store.required():
            # A required GET treats the shared row as the sole authority. This
            # also protects against a process whose configuration changed after
            # it accumulated optional, process-only freshness.
            verified_at = durable_verified or _TIMETABLE["last_verified_at"]
            latest_checked_at = durable_checked
            # Make internal sailings generated later in the same request carry
            # the exact shared value without issuing another database read.
            _runtime_latest_checked_at = _newer_timestamp(
                _runtime_latest_checked_at,
                latest_checked_at,
            )
            monotonic_runtime_verified = _newer_timestamp(
                _runtime_last_verified_at or _TIMETABLE["last_verified_at"],
                verified_at,
            )
            _runtime_last_verified_at = (
                monotonic_runtime_verified
                if monotonic_runtime_verified != _TIMETABLE["last_verified_at"]
                else None
            )
        else:
            verified_at = _newer_timestamp(
                _runtime_last_verified_at or _TIMETABLE["last_verified_at"],
                durable_verified,
            ) or _TIMETABLE["last_verified_at"]
            latest_checked_at = _newer_timestamp(
                _runtime_latest_checked_at,
                durable_checked,
            )
    metadata["snapshot_verified_at"] = _TIMETABLE["last_verified_at"]
    # Backwards-compatible alias from the first synchronization slice.
    metadata["snapshot_last_verified_at"] = _TIMETABLE["last_verified_at"]
    metadata["latest_checked_at"] = latest_checked_at
    metadata["last_verified_at"] = verified_at
    metadata["verification_scope"] = "complete_operator_route_calendar_match"
    store_available = ferry_freshness_store.available()
    if store_available is True:
        metadata["freshness_durability"] = "shared_supabase"
    elif ferry_freshness_store.configured():
        metadata["freshness_durability"] = "supabase_table_unavailable"
    else:
        metadata["freshness_durability"] = (
            "process_memory_with_committed_snapshot_fallback"
        )
    for source in metadata["sources"]:
        source_snapshot_verified_at = source["last_verified_at"]
        source["snapshot_verified_at"] = source_snapshot_verified_at
        source["last_verified_at"] = verified_at
        source["latest_successful_validation_at"] = (
            verified_at
            if verified_at != source_snapshot_verified_at
            else None
        )
    return metadata


def _reset_runtime_verification_for_tests() -> None:
    """Restore committed freshness; intentionally private to verification code."""
    global _runtime_last_verified_at, _runtime_latest_checked_at
    with _verification_lock:
        _runtime_last_verified_at = None
        _runtime_latest_checked_at = None


def generate_sailings(now: Optional[datetime] = None, horizon_hours: int = 24,
                      ports: Optional[List[str]] = None,
                      schedule_verified_at: Optional[str] = None) -> List[Dict[str, Any]]:
    """All sailings departing strictly after `now`, soonest first."""
    now = now or clock.now()
    schedule_verified_at = (
        schedule_verified_at
        or timetable_metadata(load_durable=False)["last_verified_at"]
    )
    horizon_end = now + timedelta(hours=horizon_hours)
    wanted = set(ports) if ports else None

    out: List[Dict[str, Any]] = []
    for route in FERRY_ROUTES:
        if wanted and route.departure_port not in wanted:
            continue
        route_now = now.astimezone(ZoneInfo(route.departure_timezone))
        for day_offset in range(3):
            day = (route_now + timedelta(days=day_offset)).date()
            for slot, departure in _slots(route, day):
                if departure <= now or departure > horizon_end:
                    continue
                out.append(_sailing(
                    route,
                    slot,
                    departure,
                    now,
                    schedule_verified_at,
                ))

    out.sort(key=lambda sailing: datetime.fromisoformat(sailing["departure_time"]))
    return out


def next_sailings_after(depart_after: datetime, limit: int = 3,
                         port: Optional[str] = None,
                         now: Optional[datetime] = None,
                         schedule_verified_at: Optional[str] = None) -> List[Dict[str, Any]]:
    """The next `limit` sailings a traveller could actually board."""
    now = now or clock.now()
    cutoff = depart_after + timedelta(minutes=BOARDING_CUTOFF_MINS)
    candidates = generate_sailings(
        now,
        horizon_hours=48,
        ports=[port] if port else None,
        schedule_verified_at=schedule_verified_at,
    )
    return [s for s in candidates
            if datetime.fromisoformat(s["departure_time"]) >= cutoff][:limit]


def ferry_surge_for_port(port: Optional[str], now: Optional[datetime] = None,
                          window_mins: int = 45,
                          schedule_verified_at: Optional[str] = None,
                          ) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Check if a sailing is imminent at this port."""
    if not port:
        return 0, None
    now = now or clock.now()
    upcoming = generate_sailings(
        now,
        horizon_hours=3,
        ports=[port],
        schedule_verified_at=schedule_verified_at,
    )
    for sailing in upcoming:
        if sailing["minutes_until_departure"] <= window_mins:
            return 1, sailing
    return 0, None


def get_port_intelligence(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Return explicitly modelled terminal metrics for major Batam ports."""
    now = now or clock.now()
    hour = now.hour
    is_peak = (7 <= hour <= 9) or (16 <= hour <= 19)

    out = []
    for port_name, meta in PORT_METADATA.items():
        upcoming = generate_sailings(now, horizon_hours=2, ports=[port_name])
        next_sailing = upcoming[0] if upcoming else None
        
        # A planning estimate derived from source-dated departure density and a
        # Batam time-of-day profile. It is never presented as an observation.
        departures_within_hour = sum(
            1 for sailing in upcoming
            if sailing["minutes_until_departure"] <= 60
        )
        load_factor = (1.35 if is_peak else 0.85) * (
            1 + min(0.3, departures_within_hour * 0.08)
        )
        queue_mins = int(meta["base_queue_mins"] * load_factor)
        customs_mins = int(meta["base_customs_mins"] * load_factor)
        
        active_berths = min(meta["total_berths"], len(upcoming))
        status = "BUSY" if load_factor >= 1.35 else "NORMAL"
        
        out.append({
            "port_name": port_name,
            "terminal_code": meta["code"],
            "passenger_queue_mins": queue_mins,
            "customs_processing_mins": customs_mins,
            "freight_clearance_mins": customs_mins + 15,
            "active_berths": max(1, active_berths),
            "total_berths": meta["total_berths"],
            "status": status,
            "next_sailing_in_mins": next_sailing["minutes_until_departure"] if next_sailing else None,
            "next_vessel": next_sailing["ferry_name"] if next_sailing else None,
            "next_operator": next_sailing["operator"] if next_sailing else None,
            "data_source": "schedule_informed_planning_estimate",
            "observed": False,
            "estimate_basis": "Published departure density × Batam time-of-day planning profile",
            "official_reference_url": meta["official_reference_url"],
            "limitations": (
                "Planning estimate, not a sensor observation. No public official "
                "live queue feed was available; verify conditions with the terminal "
                "or operator before travel."
            ),
        })
        
    return out
