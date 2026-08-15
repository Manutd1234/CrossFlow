"""Bounded timetable validation for official Batam--Singapore ferry sources.

This module deliberately does not crawl arbitrary URLs.  A refresh checks a
small, reviewed allowlist of public operator/authority pages. Operator-specific
parsers retain route, direction, timezone and calendar semantics before an
unchanged snapshot can be reverified. Changed schedules remain last-known-good
until a separate atomic promotion path is available.
"""

from __future__ import annotations

import copy
import hashlib
import html
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from zoneinfo import ZoneInfo

from services import ferry_schedule, tls


REFRESH_COOLDOWN_SECONDS = 300
# Keep the complete six-source check below the client's 20 second deadline.
# A transiently unavailable source gets one additional bounded attempt; BP
# Batam's second attempt uses its official WordPress JSON representation.
SOURCE_TIMEOUT_SECONDS = 5
SOURCE_FETCH_ATTEMPTS = 2
MAX_SOURCE_BYTES = 2_000_000
PARSER_VERSION = "official-six-source-route-calendar-v4"

_TIME_PATTERN = re.compile(r"(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?!\d)")
_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")

_BATAMFAST_HEADERS = {
    "batamfast-hf-bct": "harbourfront to batam center spo time",
    "batamfast-bct-hf": "batam center to harbourfront indo time",
    "batamfast-tm-npt": "tanah merah to nongsapura spo time",
    "batamfast-npt-tm": "nongsapura to tanah merah indo time",
}

_SINDO_HEADERS = {
    "sindo-hf-bct": ("HARBOURFRONT", "BATAM CENTER", "(Singapore Time)"),
    "sindo-bct-hf": ("BATAM CENTER", "HARBOURFRONT", "(Indonesia Time)"),
    "sindo-hf-skp": ("HARBOURFRONT", "SEKUPANG", "(Singapore Time)"),
    "sindo-skp-hf": ("SEKUPANG", "HARBOURFRONT", "(Indonesia Time)"),
    "sindo-tm-bct": ("TANAH MERAH", "BATAM CENTER", "(Singapore Time)"),
    "sindo-bct-tm": ("BATAM CENTER", "TANAH MERAH", "(Indonesia Time)"),
}

_SINDO_SHIP_RULES = {
    1: "daily",
    2: "daily",
    3: "weekday_excluding_public_holidays",
    4: "weekend_and_public_holiday",
    5: "even_dates_and_weekends",
    6: "weekend",
    7: "sunday_only",
    8: "saturday_only",
    9: "weekend_via_sekupang",
}

_SINDO_LEGEND_MARKERS = {
    6: "Saturdays and Sundays Only",
    7: "Sunday Only and operated by Interlining Partner",
}

_MAJESTIC_SECTIONS = {
    "majestic-hf-bct": (
        "From HarbourFront Batam Centre SGP Time",
        "From Batam Centre HarbourFront IDN Time",
    ),
    "majestic-bct-hf": (
        "From Batam Centre HarbourFront IDN Time",
        "From HarbourFront Sekupang SGP Time",
    ),
    "majestic-hf-skp": (
        "From HarbourFront Sekupang SGP Time",
        "From Sekupang HarbourFront IDN Time",
    ),
    "majestic-skp-hf": (
        "From Sekupang HarbourFront IDN Time",
        "From Tanah Merah Batam Centre SGP Time",
    ),
    "majestic-tm-bct": (
        "From Tanah Merah Batam Centre SGP Time",
        "From Batam Centre Tanah Merah IDN Time",
    ),
    "majestic-bct-tm": (
        "From Batam Centre Tanah Merah IDN Time",
        "From Tanah Merah Tanjung Pinang SGP Time",
    ),
}

_HORIZON_SECTIONS = {
    "horizon-hf-hb": (
        "Singapore HarbourFront to Batam Harbour Bay (Singapore Time)",
        "Batam Harbour Bay to Singapore HarbourFront (Indo Time)",
    ),
    "horizon-hb-hf": (
        "Batam Harbour Bay to Singapore HarbourFront (Indo Time)",
        "Singapore | Nirup Ferry Schedules",
    ),
}

_SERVICE_IDENTITIES = {
    "batamfast-hf-bct": ("HarbourFront SG", "Batam Centre", "Asia/Singapore"),
    "batamfast-bct-hf": ("Batam Centre", "HarbourFront SG", "Asia/Jakarta"),
    "batamfast-tm-npt": ("Tanah Merah SG", "Nongsa Pura", "Asia/Singapore"),
    "batamfast-npt-tm": ("Nongsa Pura", "Tanah Merah SG", "Asia/Jakarta"),
    "sindo-hf-bct": ("HarbourFront SG", "Batam Centre", "Asia/Singapore"),
    "sindo-bct-hf": ("Batam Centre", "HarbourFront SG", "Asia/Jakarta"),
    "sindo-hf-skp": ("HarbourFront SG", "Sekupang", "Asia/Singapore"),
    "sindo-skp-hf": ("Sekupang", "HarbourFront SG", "Asia/Jakarta"),
    "sindo-tm-bct": ("Tanah Merah SG", "Batam Centre", "Asia/Singapore"),
    "sindo-bct-tm": ("Batam Centre", "Tanah Merah SG", "Asia/Jakarta"),
    "majestic-hf-bct": ("HarbourFront SG", "Batam Centre", "Asia/Singapore"),
    "majestic-bct-hf": ("Batam Centre", "HarbourFront SG", "Asia/Jakarta"),
    "majestic-hf-skp": ("HarbourFront SG", "Sekupang", "Asia/Singapore"),
    "majestic-skp-hf": ("Sekupang", "HarbourFront SG", "Asia/Jakarta"),
    "majestic-tm-bct": ("Tanah Merah SG", "Batam Centre", "Asia/Singapore"),
    "majestic-bct-tm": ("Batam Centre", "Tanah Merah SG", "Asia/Jakarta"),
    "horizon-hf-hb": ("HarbourFront SG", "HarbourBay", "Asia/Singapore"),
    "horizon-hb-hf": ("HarbourBay", "HarbourFront SG", "Asia/Jakarta"),
}


@dataclass(frozen=True)
class OfficialSource:
    source_id: str
    authority: str
    kind: str
    url: str
    permission_status: str
    fetch_enabled: bool
    schedule_operator: Optional[str] = None
    required_markers: Tuple[str, ...] = ()
    minimum_time_count: int = 0
    note: str = ""
    fallback_urls: Tuple[str, ...] = ()


# Only these exact public pages can ever be fetched.  Booking systems, private
# mobile APIs and user-provided/blog URLs are intentionally absent.
OFFICIAL_SOURCES: Tuple[OfficialSource, ...] = (
    OfficialSource(
        source_id="batamfast-public-timetable",
        authority="BatamFast",
        kind="published_timetable",
        url="https://www.batamfast.com/tripschedule/index.ashx",
        permission_status="public_official_page",
        fetch_enabled=True,
        schedule_operator="BatamFast",
        required_markers=(
            "Batamfast Ferry Schedule", "Harbourfront", "Batam Center",
            "Sekupang", "Nongsapura",
        ),
        minimum_time_count=30,
        note="Public operator timetable; conditional and suspended rows require review.",
    ),
    OfficialSource(
        source_id="sindo-public-timetable",
        authority="Sindo Ferry",
        kind="published_timetable",
        url="https://app.sindoferry.com.sg/schedule/",
        permission_status="public_official_page",
        fetch_enabled=True,
        schedule_operator="Sindo Ferry",
        required_markers=(
            "SINDO FERRY SCHEDULE", "HARBOURFRONT", "BATAM CENTER", "LEGENDS:",
        ),
        minimum_time_count=30,
        note="Public operator timetable; ship-icon calendar qualifiers are preserved for review.",
    ),
    OfficialSource(
        source_id="majestic-public-timetable",
        authority="Majestic Fast Ferry",
        kind="published_timetable",
        url="https://www.majesticfastferry.com.sg/",
        permission_status="public_official_page",
        fetch_enabled=True,
        schedule_operator="Majestic Fast Ferry",
        required_markers=(
            "Ferry Schedules", "HarbourFront", "Batam Centre", "Sekupang",
        ),
        minimum_time_count=30,
        note="Public operator timetable; weekday, weekend and holiday groups are kept distinct.",
    ),
    OfficialSource(
        source_id="bp-batam-passenger-ports",
        authority="BP Batam",
        kind="terminal_reference",
        url="https://batamport.bpbatam.go.id/pelabuhan-penumpang/",
        permission_status="public_official_page",
        fetch_enabled=True,
        required_markers=(
            "Pelabuhan Penumpang", "Terminal Ferry Internasional",
            "Singapura", "Malaysia",
        ),
        note=(
            "Official passenger-terminal reference; it validates terminal context "
            "but does not publish a recurring operator timetable or live queue feed."
        ),
        fallback_urls=(
            "https://batamport.bpbatam.go.id/wp-json/wp/v2/pages/14391",
        ),
    ),
    OfficialSource(
        source_id="horizon-public-timetable",
        authority="Horizon Fast Ferry",
        kind="published_timetable",
        url="https://horizonfastferry.com.sg/",
        permission_status="public_official_page",
        fetch_enabled=True,
        schedule_operator="Horizon Fast Ferry",
        required_markers=(
            "Singapore | Batam Ferry Daily Schedule", "Singapore HarbourFront",
            "Batam Harbour Bay", "Singapore Time", "Indo Time",
        ),
        minimum_time_count=26,
        note=(
            "Public operator timetable; the refresh validates the two daily "
            "HarbourFront--Harbour Bay route sections without republishing content."
        ),
    ),
    OfficialSource(
        source_id="scc-live-operations-board",
        authority="Singapore Cruise Centre",
        kind="same_day_operations_board",
        url="https://singaporecruise.com.sg/schedule/ferries/",
        permission_status="public_official_page",
        fetch_enabled=True,
        required_markers=(
            "FERRY OPERATOR", "TRIP ID", "FROM", "TO", "STATUS",
        ),
        minimum_time_count=1,
        note=(
            "Official same-day operations board supplied for this project. Its "
            "rows corroborate current operations but never replace the recurring "
            "operator timetable snapshot."
        ),
    ),
)

# Kept in the response for backwards compatibility. All six reviewed sources
# now appear directly in ``source_results`` so the existing UI renders them.
EXCLUDED_REFERENCES: Tuple[Dict[str, str], ...] = ()

_FETCHABLE_HOSTS = frozenset(
    urlparse(url).hostname
    for source in OFFICIAL_SOURCES
    if source.fetch_enabled
    for url in (source.url, *source.fallback_urls)
)


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    """Reject redirects away from the fixed official-host allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _require_fetchable_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(
    _AllowlistedRedirectHandler(),
    HTTPSHandler(context=tls.default_context()),
)

# BP Batam's official host currently requires legacy TLS renegotiation. Keep
# certificate and hostname verification enabled, and scope the compatibility
# option to that one reviewed hostname rather than weakening every source.
_BP_BATAM_HOST = "batamport.bpbatam.go.id"
_BP_TLS_CONTEXT = tls.new_context()
if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
    _BP_TLS_CONTEXT.options |= ssl.OP_LEGACY_SERVER_CONNECT
_BP_OPENER = build_opener(
    _AllowlistedRedirectHandler(),
    HTTPSHandler(context=_BP_TLS_CONTEXT),
)


@dataclass(frozen=True)
class FetchedSource:
    body: bytes
    status: int
    final_url: str
    headers: Mapping[str, str]
    requested_url: Optional[str] = None
    attempt_count: int = 1


FetchSource = Callable[[OfficialSource], FetchedSource]


class ScheduleMismatch(ValueError):
    """The official route/calendar content differs from the active snapshot."""

_refresh_lock = threading.Lock()
_last_report: Optional[Dict[str, Any]] = None
_last_refresh_monotonic: Optional[float] = None
_last_hashes: Dict[str, str] = {}
_last_schedule_verified_at: Optional[str] = ferry_schedule.timetable_metadata(
    load_durable=False,
)[
    "snapshot_verified_at"
]


def _require_fetchable_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _FETCHABLE_HOSTS:
        raise ValueError("Refresh URL is outside the reviewed official-source allowlist.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Refresh URL contains unsupported authority components.")


def _fetch_url(url: str, *, attempt_count: int) -> FetchedSource:
    _require_fetchable_url(url)
    opener = (
        _BP_OPENER
        if urlparse(url).hostname == _BP_BATAM_HOST
        else _OPENER
    )
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json",
            "User-Agent": "CrossFlow-AI/1.0 official-source-refresh",
        },
        method="GET",
    )
    with opener.open(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        _require_fetchable_url(final_url)
        body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError("Official source exceeded the refresh size limit.")
        return FetchedSource(
            body=body,
            status=int(getattr(response, "status", 200)),
            final_url=final_url,
            headers=dict(response.headers.items()),
            requested_url=url,
            attempt_count=attempt_count,
        )


def _fetch_source(
    source: OfficialSource,
    *,
    board_date: Optional[datetime] = None,
) -> FetchedSource:
    """Fetch one allowlisted source with a strict two-attempt total budget."""
    if source.source_id == "scc-live-operations-board":
        requested_day = board_date or datetime.now(ZoneInfo("Asia/Singapore"))
        if requested_day.tzinfo is None or requested_day.utcoffset() is None:
            raise ValueError("Singapore Cruise Centre board date must be timezone-aware.")
        singapore_date = requested_day.astimezone(
            ZoneInfo("Asia/Singapore")
        ).strftime("%Y%m%d")
        dated_url = f"{source.url}?{urlencode({
            'ferry-status': 'arrival',
            'date': singapore_date,
            'time': 'all',
            'origin': 'all',
            'destination': 'all',
            'ferry': 'all',
        })}"
        candidates = (dated_url, dated_url)
    else:
        candidates = (source.url, *source.fallback_urls)
    if len(candidates) < SOURCE_FETCH_ATTEMPTS:
        candidates = candidates + (source.url,) * (
            SOURCE_FETCH_ATTEMPTS - len(candidates)
        )
    last_error: Optional[Exception] = None
    for attempt_count, url in enumerate(
        candidates[:SOURCE_FETCH_ATTEMPTS], start=1,
    ):
        try:
            fetched = _fetch_url(url, attempt_count=attempt_count)
            if attempt_count < SOURCE_FETCH_ATTEMPTS:
                try:
                    # Retry/fallback decisions use the same deep validation as
                    # the final report. BP's JSON fallback is therefore tried
                    # even when a partial/interstitial page happens to contain
                    # the shallow source markers.
                    _validate_fetched_content(source, fetched)
                except ValueError as error:
                    last_error = error
                    continue
            return fetched
        except HTTPError as error:
            last_error = error
            if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
        except (TimeoutError, URLError, OSError) as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"No fetch candidate was configured for {source.source_id}.")


def _visible_text(body: bytes) -> str:
    decoded = body.decode("utf-8", errors="replace")
    without_tags = _TAG_PATTERN.sub(" ", decoded)
    return _SPACE_PATTERN.sub(" ", html.unescape(without_tags)).strip()


def _fragment_text(fragment: str) -> str:
    return _SPACE_PATTERN.sub(
        " ", html.unescape(_TAG_PATTERN.sub(" ", fragment)),
    ).strip()


def _services_by_id(operator: str) -> Dict[str, Dict[str, Any]]:
    return {
        service["service_id"]: service
        for service in ferry_schedule.published_services_for_operator(operator)
    }


def _calendar_exclusions(service: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    return {
        rule: tuple(values)
        for rule, values in service.get("calendar_exclusions", {}).items()
    }


def _require_service_identity(service: Mapping[str, Any]) -> None:
    service_id = str(service["service_id"])
    expected = _SERVICE_IDENTITIES.get(service_id)
    observed = (
        service["departure_port"],
        service["arrival_port"],
        service.get("departure_timezone", "Asia/Jakarta"),
    )
    if expected is None or observed != expected:
        raise ScheduleMismatch(
            f"Route identity mismatch for {service_id}; "
            f"expected {expected}, observed {observed}."
        )


def _require_exact_values(
    service_id: str,
    calendar_group: str,
    observed: Tuple[str, ...],
    expected: Tuple[str, ...],
) -> None:
    if observed == expected:
        return
    raise ScheduleMismatch(
        f"Route/calendar mismatch for {service_id} ({calendar_group}); "
        f"expected {list(expected)}, observed {list(observed)}."
    )


def _validate_service_groups(
    service: Mapping[str, Any],
    *,
    daily: Tuple[str, ...],
    weekend: Tuple[str, ...],
    exceptions: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> int:
    service_id = str(service["service_id"])
    _require_service_identity(service)
    _require_exact_values(
        service_id,
        "daily",
        daily,
        tuple(service["daily_departures"]),
    )
    _require_exact_values(
        service_id,
        "weekend_additions",
        weekend,
        tuple(service["weekend_additions"]),
    )
    observed_exceptions = dict(exceptions or {})
    expected_exceptions = _calendar_exclusions(service)
    if observed_exceptions != expected_exceptions:
        raise ScheduleMismatch(
            f"Route/calendar mismatch for {service_id} (audited exclusions); "
            f"expected {expected_exceptions}, observed {observed_exceptions}."
        )
    return (
        len(daily)
        + len(weekend)
        + sum(len(values) for values in observed_exceptions.values())
    )


def _validate_batamfast(body: bytes) -> Dict[str, Any]:
    decoded = body.decode("utf-8", errors="replace")
    services = _services_by_id("BatamFast")
    tables = []
    for table_match in re.finditer(
        r"<table\b[^>]*>(.*?)</table>", decoded, re.IGNORECASE | re.DOTALL,
    ):
        table_html = table_match.group(1)
        headers = tuple(
            _fragment_text(match.group(1)).casefold()
            for match in re.finditer(
                r"<th\b[^>]*>(.*?)</th>",
                table_html,
                re.IGNORECASE | re.DOTALL,
            )
        )
        rows = []
        for row_match in re.finditer(
            r"<tr\b[^>]*>(.*?)</tr>",
            table_html,
            re.IGNORECASE | re.DOTALL,
        ):
            cells = tuple(
                match.group(1)
                for match in re.finditer(
                    r"<td\b[^>]*>(.*?)</td>",
                    row_match.group(1),
                    re.IGNORECASE | re.DOTALL,
                )
            )
            if cells:
                rows.append(cells)
        if headers:
            tables.append((headers, rows))

    validated = []
    matched_slots = 0
    for service_id, expected_header in _BATAMFAST_HEADERS.items():
        service = services.get(service_id)
        if service is None:
            raise ValueError(f"Snapshot service {service_id} is missing.")
        matches = [
            (header_index, rows)
            for headers, rows in tables
            for header_index, header in enumerate(headers)
            if header == expected_header
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one BatamFast route column for {service_id}; "
                f"found {len(matches)}."
            )
        header_index, rows = matches[0]
        observed = []
        for row in rows:
            if header_index >= len(row):
                continue
            cell_text = _fragment_text(row[header_index])
            times = _TIME_PATTERN.findall(cell_text)
            if len(times) > 1:
                raise ValueError(
                    f"Multiple BatamFast times occupied one {service_id} row."
                )
            if not times:
                continue
            qualifier = _SPACE_PATTERN.sub(
                " ", _TIME_PATTERN.sub(" ", cell_text),
            ).strip()
            if qualifier:
                raise ValueError(
                    f"Unsupported BatamFast calendar/carrier qualifier for "
                    f"{service_id} at {times[0]}: {qualifier}."
                )
            observed.append(times[0])
        matched_slots += _validate_service_groups(
            service,
            daily=tuple(observed),
            weekend=(),
        )
        validated.append(service_id)

    if set(validated) != set(services):
        raise ValueError("BatamFast snapshot contains an unvalidated service.")
    return {
        "validated_service_ids": sorted(validated),
        "matched_departure_slot_count": matched_slots,
    }


def _sindo_card_departures(card_html: str) -> Tuple[Tuple[str, Optional[int]], ...]:
    departures = []
    pending_ship: Optional[int] = None
    for token in re.finditer(
        r"<img\b[^>]*>|<p\b[^>]*>.*?</p>",
        card_html,
        re.IGNORECASE | re.DOTALL,
    ):
        value = token.group(0)
        if value.casefold().startswith("<img"):
            ship_match = re.search(
                r"/ship([1-9])\.png", value, re.IGNORECASE,
            )
            if ship_match is not None:
                pending_ship = int(ship_match.group(1))
            continue
        text = _fragment_text(value)
        if _TIME_PATTERN.fullmatch(text):
            departures.append((text, pending_ship))
            pending_ship = None
    return tuple(departures)


def _validate_sindo(body: bytes) -> Dict[str, Any]:
    decoded = body.decode("utf-8", errors="replace")
    visible = _visible_text(body)
    services = _services_by_id("Sindo Ferry")
    cards: Dict[Tuple[str, str, str], str] = {}
    for card_html in re.split(
        r'<div class="[^"]*MuiCard-root[^"]*">',
        decoded,
        flags=re.IGNORECASE,
    )[1:]:
        headings = tuple(
            _fragment_text(match.group(1))
            for match in re.finditer(
                r"<p\b[^>]*>(.*?)</p>",
                card_html,
                re.IGNORECASE | re.DOTALL,
            )
        )
        if len(headings) < 3:
            continue
        key = (headings[0].upper(), headings[1].upper(), headings[2])
        if key in _SINDO_HEADERS.values():
            if key in cards:
                raise ValueError(f"Duplicate Sindo route card for {key}.")
            cards[key] = card_html

    validated = []
    matched_slots = 0
    used_ship_rules = set()
    for service_id, header in _SINDO_HEADERS.items():
        service = services.get(service_id)
        card_html = cards.get(header)
        if service is None or card_html is None:
            raise ValueError(f"Sindo route card for {service_id} is missing.")
        daily = []
        weekend = []
        exceptions: Dict[str, list[str]] = {}
        for departure, ship in _sindo_card_departures(card_html):
            if ship is None:
                rule = "daily"
            else:
                rule = _SINDO_SHIP_RULES.get(ship)
                if rule is None:
                    raise ValueError(
                        f"Unknown Sindo calendar icon ship{ship} on {service_id}."
                    )
                used_ship_rules.add(ship)
            if rule == "daily":
                daily.append(departure)
            elif rule == "weekend":
                weekend.append(departure)
            else:
                exceptions.setdefault(rule, []).append(departure)

        matched_slots += _validate_service_groups(
            service,
            daily=tuple(daily),
            weekend=tuple(weekend),
            exceptions={key: tuple(values) for key, values in exceptions.items()},
        )
        validated.append(service_id)

    for ship, marker in _SINDO_LEGEND_MARKERS.items():
        if ship in used_ship_rules and marker.casefold() not in visible.casefold():
            raise ValueError(f"Sindo legend for ship{ship} is missing or changed.")
    if set(validated) != set(services):
        raise ValueError("Sindo snapshot contains an unvalidated service.")
    return {
        "validated_service_ids": sorted(validated),
        "matched_departure_slot_count": matched_slots,
    }


def _section_between(text: str, start: str, end: str) -> str:
    folded = text.casefold()
    start_index = folded.find(start.casefold())
    if start_index < 0:
        raise ValueError(f"Schedule section is missing: {start}.")
    content_start = start_index + len(start)
    end_index = folded.find(end.casefold(), content_start)
    if end_index < 0:
        raise ValueError(f"Schedule boundary is missing: {end}.")
    return text[content_start:end_index]


def _sorted_times(values: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(sorted(values, key=lambda value: tuple(map(int, value.split(":")))))


def _validate_majestic(body: bytes) -> Dict[str, Any]:
    text = _visible_text(body)
    services = _services_by_id("Majestic Fast Ferry")
    validated = []
    matched_slots = 0
    label_rules = {"Mon": "monday_only", "Fri": "friday_only"}

    for service_id, (start, end) in _MAJESTIC_SECTIONS.items():
        service = services.get(service_id)
        if service is None:
            raise ValueError(f"Snapshot service {service_id} is missing.")
        section = _section_between(text, start, end)
        if "Weekday" not in section:
            matched_slots += _validate_service_groups(
                service,
                daily=tuple(_TIME_PATTERN.findall(section)),
                weekend=(),
            )
        else:
            weekday_index = section.find("Weekday") + len("Weekday")
            weekend_marker = "Sat, Sun & Public Holidays"
            weekend_index = section.find(weekend_marker, weekday_index)
            if weekend_index < 0:
                raise ValueError(
                    f"Majestic weekend/public-holiday group is missing for {service_id}."
                )
            weekday_text = section[weekday_index:weekend_index]
            weekend_text = section[weekend_index + len(weekend_marker):]
            observed_exceptions: Dict[str, list[str]] = {}
            labelled_times = []
            for departure, label in re.findall(
                r"((?:[01]\d|2[0-3]):[0-5]\d)\s+(Mon|Fri)\b",
                weekday_text,
            ):
                observed_exceptions.setdefault(label_rules[label], []).append(departure)
                labelled_times.append(departure)
            weekday_values = list(_TIME_PATTERN.findall(weekday_text))
            for departure in labelled_times:
                weekday_values.remove(departure)
            expected_weekend = _sorted_times(
                tuple(service["daily_departures"])
                + tuple(service["weekend_additions"])
            )
            observed_weekend = tuple(_TIME_PATTERN.findall(weekend_text))
            _require_exact_values(
                service_id,
                "weekend_and_public_holiday",
                observed_weekend,
                expected_weekend,
            )
            matched_slots += _validate_service_groups(
                service,
                daily=tuple(weekday_values),
                weekend=tuple(service["weekend_additions"]),
                exceptions={
                    key: tuple(values)
                    for key, values in observed_exceptions.items()
                },
            )
        validated.append(service_id)

    if set(validated) != set(services):
        raise ValueError("Majestic snapshot contains an unvalidated service.")
    return {
        "validated_service_ids": sorted(validated),
        "matched_departure_slot_count": matched_slots,
    }


def _validate_horizon(body: bytes) -> Dict[str, Any]:
    text = _visible_text(body)
    services = _services_by_id("Horizon Fast Ferry")
    daily_heading = "Singapore | Batam Ferry Daily Schedule"
    if daily_heading.casefold() not in text.casefold():
        raise ValueError("Horizon daily schedule heading is missing.")

    validated = []
    matched_slots = 0
    for service_id, (start, end) in _HORIZON_SECTIONS.items():
        service = services.get(service_id)
        if service is None:
            raise ValueError(f"Snapshot service {service_id} is missing.")
        section = _section_between(text, start, end)
        matched_slots += _validate_service_groups(
            service,
            daily=tuple(_TIME_PATTERN.findall(section)),
            weekend=(),
        )
        validated.append(service_id)

    if set(validated) != set(services):
        raise ValueError("Horizon snapshot contains an unvalidated service.")
    return {
        "validated_service_ids": sorted(validated),
        "matched_departure_slot_count": matched_slots,
    }


def _validate_schedule_snapshot(source: OfficialSource, body: bytes) -> Dict[str, Any]:
    validators = {
        "BatamFast": _validate_batamfast,
        "Sindo Ferry": _validate_sindo,
        "Majestic Fast Ferry": _validate_majestic,
        "Horizon Fast Ferry": _validate_horizon,
    }
    validator = validators.get(source.schedule_operator)
    if validator is None:
        raise ScheduleMismatch(
            f"No route/calendar validator exists for {source.schedule_operator}."
        )
    result = validator(body)
    result["schedule_validation_status"] = "matched_snapshot"
    result["schedule_validation_scope"] = "complete_route_calendar_match"
    return result


def _requested_scc_date(fetched: FetchedSource) -> date:
    requested_url = fetched.requested_url or fetched.final_url
    query_values = parse_qs(urlparse(requested_url).query).get("date", ())
    if len(query_values) != 1 or re.fullmatch(r"\d{8}", query_values[0]) is None:
        raise ValueError(
            "Singapore Cruise Centre request is missing one explicit board date."
        )
    try:
        return datetime.strptime(query_values[0], "%Y%m%d").date()
    except ValueError as error:
        raise ValueError(
            "Singapore Cruise Centre request has an invalid board date."
        ) from error


def _validate_scc_operations_board(
    body: bytes,
    *,
    requested_date: date,
) -> Dict[str, Any]:
    """Validate SCC's labelled same-day rows without treating them as a timetable."""
    decoded = body.decode("utf-8", errors="replace")
    expected_date_match = re.search(
        r'<input\b[^>]*name=["\']date["\'][^>]*value=["\'](\d{8})["\']',
        decoded,
        re.IGNORECASE,
    )
    if expected_date_match is None:
        raise ValueError("Singapore Cruise Centre board date marker is missing.")
    expected_date = datetime.strptime(
        expected_date_match.group(1), "%Y%m%d",
    ).date()
    if expected_date != requested_date:
        raise ValueError(
            "Singapore Cruise Centre returned a board for a different date."
        )
    visible = _visible_text(body)
    if (
        "FERRY arrival".casefold() not in visible.casefold()
        or "Last Updated:".casefold() not in visible.casefold()
        or "Singapore Time GMT +08".casefold() not in visible.casefold()
    ):
        raise ValueError(
            "Singapore Cruise Centre board heading or update time is missing."
        )
    required_columns = (
        "DATE", "TIME", "TRIP ID", "FERRY OPERATOR", "FROM", "TO", "STATUS",
    )
    matching_tables = []
    for table_match in re.finditer(
        r"<table\b[^>]*>(.*?)</table>", decoded, re.IGNORECASE | re.DOTALL,
    ):
        table_html = table_match.group(1)
        headers = tuple(
            _fragment_text(match.group(1)).upper()
            for match in re.finditer(
                r"<th\b[^>]*>(.*?)</th>",
                table_html,
                re.IGNORECASE | re.DOTALL,
            )
        )
        if all(column in headers for column in required_columns):
            matching_tables.append(table_html)
    if len(matching_tables) != 1:
        raise ValueError(
            "Expected one Singapore Cruise Centre ferry operations table; "
            f"found {len(matching_tables)}."
        )

    valid_rows = 0
    operators = set()
    for row_match in re.finditer(
        r"<tr\b[^>]*>(.*?)</tr>",
        matching_tables[0],
        re.IGNORECASE | re.DOTALL,
    ):
        labelled_cells: Dict[str, str] = {}
        for cell_match in re.finditer(
            r"<td\b([^>]*)>(.*?)</td>",
            row_match.group(1),
            re.IGNORECASE | re.DOTALL,
        ):
            label_match = re.search(
                r"data-label=[\"']([^\"']+)[\"']",
                cell_match.group(1),
                re.IGNORECASE,
            )
            if label_match is None:
                continue
            labelled_cells[label_match.group(1).strip().upper()] = (
                _fragment_text(cell_match.group(2))
            )
        if not labelled_cells:
            continue
        missing = [
            column for column in required_columns
            if not labelled_cells.get(column)
        ]
        if missing:
            raise ValueError(
                "Singapore Cruise Centre ferry row is missing labelled values: "
                + ", ".join(missing)
            )
        if re.fullmatch(r"(?:[01]\d|2[0-3]):?[0-5]\d", labelled_cells["TIME"]) is None:
            raise ValueError(
                "Singapore Cruise Centre ferry row has an invalid scheduled time."
            )
        try:
            row_date = datetime.strptime(
                labelled_cells["DATE"], "%a, %d %b %Y",
            ).date()
        except ValueError as error:
            raise ValueError(
                "Singapore Cruise Centre ferry row has an invalid date."
            ) from error
        if row_date != expected_date:
            raise ValueError(
                "Singapore Cruise Centre ferry row does not match the requested date."
            )
        if re.fullmatch(r"[A-Z0-9-]+", labelled_cells["TRIP ID"].upper()) is None:
            raise ValueError(
                "Singapore Cruise Centre ferry row has an invalid trip identifier."
            )
        operators.add(labelled_cells["FERRY OPERATOR"])
        valid_rows += 1
    if valid_rows == 0:
        raise ValueError("Singapore Cruise Centre ferry board contained no labelled rows.")
    return {
        "source_validation_status": "validated_same_day_operations_board",
        "matched_board_row_count": valid_rows,
        "matched_board_operator_count": len(operators),
        "matched_departure_slot_count": valid_rows,
    }


def _validate_non_schedule_source(
    source: OfficialSource,
    body: bytes,
    *,
    fetched: FetchedSource,
    requested_scc_date: Optional[date] = None,
) -> Dict[str, Any]:
    if source.source_id == "scc-live-operations-board":
        return _validate_scc_operations_board(
            body,
            requested_date=(
                requested_scc_date or _requested_scc_date(fetched)
            ),
        )
    if source.source_id == "bp-batam-passenger-ports":
        text = _visible_text(body)
        required_catalogue_markers = (
            "Pelabuhan Penumpang Internasional terdiri dari:",
            "Kinerja Operasional",
            "PENUMPANG INTERNASIONAL",
        )
        missing = [
            marker for marker in required_catalogue_markers
            if marker.casefold() not in text.casefold()
        ]
        if missing:
            raise ValueError(
                "BP Batam terminal-catalogue anchors were missing: "
                + ", ".join(missing)
            )
        terminal_aliases = (
            ("Batam Center", "Batam Centre"),
            ("Sekupang",),
            ("Teluk Senimba",),
            ("Nongsapura", "Nongsa Pura"),
            ("Harbour Bay", "HarbourBay"),
        )
        matched_terminals = [
            aliases[0] for aliases in terminal_aliases
            if any(alias.casefold() in text.casefold() for alias in aliases)
        ]
        # The official WordPress JSON fallback omits shortcode-rendered cards,
        # but still contains the catalogue identity and at least three terminal
        # choices. The primary HTML must retain all five authority entries.
        is_json_fallback = body.lstrip().startswith(b"{")
        required_terminal_count = 3 if is_json_fallback else 5
        if len(matched_terminals) < required_terminal_count:
            raise ValueError(
                "BP Batam terminal catalogue is incomplete; matched "
                f"{len(matched_terminals)} of {required_terminal_count} required entries."
            )
        return {
            "source_validation_status": "validated_terminal_reference",
            "matched_terminal_count": len(matched_terminals),
            "used_compact_official_representation": is_json_fallback,
            "matched_departure_slot_count": 0,
        }
    return {}


def _inspect_source(
    source: OfficialSource,
    fetched: FetchedSource,
    checked_at: str,
    *,
    requested_scc_date: Optional[date] = None,
) -> Dict[str, Any]:
    source_validation, text, observed_times = _validate_fetched_content(
        source,
        fetched,
        requested_scc_date=requested_scc_date,
    )

    digest = hashlib.sha256(fetched.body).hexdigest()
    previous_digest = _last_hashes.get(source.source_id)
    _last_hashes[source.source_id] = digest
    content_changed = (
        None if previous_digest is None else previous_digest != digest
    )

    result = {
        "source_id": source.source_id,
        "authority": source.authority,
        "kind": source.kind,
        "url": source.url,
        "permission_status": source.permission_status,
        "status": "verified_structure",
        "checked_at": checked_at,
        "http_status": fetched.status,
        "final_url": fetched.final_url,
        "requested_url": fetched.requested_url or source.url,
        "fetch_attempt_count": fetched.attempt_count,
        "used_official_fallback": (
            (fetched.requested_url or source.url) in source.fallback_urls
        ),
        "etag": fetched.headers.get("ETag") or fetched.headers.get("etag"),
        "last_modified": (
            fetched.headers.get("Last-Modified")
            or fetched.headers.get("last-modified")
        ),
        "content_sha256": digest,
        "parser_version": PARSER_VERSION,
        "observed_time_value_count": len(observed_times),
        "matched_published_time_value_count": source_validation.get(
            "matched_departure_slot_count", 0,
        ),
        "content_changed_since_previous_check": content_changed,
        "note": source.note,
    }
    result.update(source_validation)
    return result


def _validate_fetched_content(
    source: OfficialSource,
    fetched: FetchedSource,
    *,
    requested_scc_date: Optional[date] = None,
) -> Tuple[Dict[str, Any], str, Tuple[str, ...]]:
    """Run the complete source contract without mutating refresh state."""
    if fetched.status < 200 or fetched.status >= 300:
        raise ValueError(f"Official source returned HTTP {fetched.status}.")

    text = _visible_text(fetched.body)
    missing = [
        marker for marker in source.required_markers
        if marker.casefold() not in text.casefold()
    ]
    if missing:
        raise ValueError(
            "Expected official-page anchors were missing: " + ", ".join(missing)
        )

    observed_times = _TIME_PATTERN.findall(text)
    if len(observed_times) < source.minimum_time_count:
        raise ValueError(
            f"Only {len(observed_times)} timetable values were found; "
            f"expected at least {source.minimum_time_count}."
        )

    source_validation = (
        _validate_schedule_snapshot(source, fetched.body)
        if source.schedule_operator is not None
        else _validate_non_schedule_source(
            source,
            fetched.body,
            fetched=fetched,
            requested_scc_date=requested_scc_date,
        )
    )
    return source_validation, text, tuple(observed_times)


def _failed_result(
    source: OfficialSource,
    checked_at: str,
    error: Exception,
) -> Dict[str, Any]:
    if isinstance(error, ScheduleMismatch):
        message = str(error)
        http_status = None
        schedule_mismatch = True
    elif isinstance(error, HTTPError):
        message = f"Official source returned HTTP {error.code}."
        http_status: Optional[int] = error.code
        schedule_mismatch = False
    elif isinstance(error, (TimeoutError, URLError)):
        message = "Official source could not be reached within the refresh window."
        http_status = None
        schedule_mismatch = False
    else:
        message = str(error) or "Official source validation failed."
        http_status = None
        schedule_mismatch = False
    return {
        "source_id": source.source_id,
        "authority": source.authority,
        "kind": source.kind,
        "url": source.url,
        "permission_status": source.permission_status,
        "status": "unavailable_or_invalid",
        "schedule_mismatch": schedule_mismatch,
        "checked_at": checked_at,
        "http_status": http_status,
        "parser_version": PARSER_VERSION,
        "warning": message,
        "note": source.note,
    }


def _skipped_result(source: OfficialSource, checked_at: str) -> Dict[str, Any]:
    return {
        "source_id": source.source_id,
        "authority": source.authority,
        "kind": source.kind,
        "url": source.url,
        "permission_status": source.permission_status,
        "status": "skipped_permission_required",
        "checked_at": checked_at,
        "http_status": None,
        "note": source.note,
    }


def refresh_official_sources(
    now: datetime,
    *,
    fetch_source: Optional[FetchSource] = None,
    monotonic_now: Optional[float] = None,
    completed_now: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """Check every reviewed source and retain the timetable on any ambiguity.

    `fetch_source` exists for deterministic verification; the HTTP endpoint
    never accepts a URL or a fetcher from a caller.
    """
    global _last_report, _last_refresh_monotonic, _last_schedule_verified_at

    monotonic_value = time.monotonic() if monotonic_now is None else monotonic_now
    with _refresh_lock:
        if (
            _last_report is not None
            and _last_refresh_monotonic is not None
            and monotonic_value - _last_refresh_monotonic < REFRESH_COOLDOWN_SECONDS
        ):
            cached = copy.deepcopy(_last_report)
            cached["status"] = "cached"
            cached["cache_age_seconds"] = max(
                0, round(monotonic_value - _last_refresh_monotonic, 1),
            )
            return cached

        started_at = now.isoformat(timespec="seconds")
        fetcher = fetch_source or (
            lambda source: _fetch_source(source, board_date=now)
        )
        enabled_sources = [source for source in OFFICIAL_SOURCES if source.fetch_enabled]
        # Each source has its own bounded request and runs independently, so a
        # slow operator does not serialize the entire refresh into a long wait.
        with ThreadPoolExecutor(
            max_workers=max(1, min(6, len(enabled_sources))),
            thread_name_prefix="ferry-source",
        ) as executor:
            futures = {
                source.source_id: executor.submit(fetcher, source)
                for source in enabled_sources
            }
            results = []
            for source in OFFICIAL_SOURCES:
                if not source.fetch_enabled:
                    results.append(_skipped_result(source, started_at))
                    continue
                try:
                    fetched = futures[source.source_id].result()
                    results.append(_inspect_source(
                        source,
                        fetched,
                        started_at,
                        requested_scc_date=now.astimezone(
                            ZoneInfo("Asia/Singapore")
                        ).date(),
                    ))
                except Exception as error:  # noqa: BLE001 - each source is isolated
                    results.append(_failed_result(source, started_at, error))

        finished = now if completed_now is None else completed_now()
        if finished.tzinfo is None or finished.utcoffset() is None:
            raise ValueError("Refresh completion time must include a timezone offset.")
        finished_at = finished.isoformat(timespec="seconds")
        for result in results:
            result["checked_at"] = finished_at

        checked_results = [
            result for result in results
            if result["status"] != "skipped_permission_required"
        ]
        successful = sum(
            result["status"] == "verified_structure" for result in checked_results
        )
        if successful == len(checked_results):
            status = "checked"
        elif successful:
            status = "partial"
        else:
            status = "failed_using_last_known_good"

        schedule_source_ids = {
            source.source_id
            for source in OFFICIAL_SOURCES
            if source.schedule_operator is not None
        }
        schedule_results = [
            result for result in results
            if result["source_id"] in schedule_source_ids
        ]
        schedule_verified = (
            len(schedule_results) == len(schedule_source_ids)
            and all(
                result["status"] == "verified_structure"
                and result.get("schedule_validation_status") == "matched_snapshot"
                for result in schedule_results
            )
        )
        if schedule_verified:
            _last_schedule_verified_at = finished_at

        report: Dict[str, Any] = {
            "refresh_id": f"official-source-check-{now:%Y%m%dT%H%M%S%z}",
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "latest_checked_at": finished_at,
            "refresh_scope": "fixed_official_allowlist",
            "source_results": results,
            "summary": {
                "verified": successful,
                "failed": len(checked_results) - successful,
                "permission_gated": len(results) - len(checked_results),
            },
            "schedule_verified": schedule_verified,
            "schedule_verified_at": _last_schedule_verified_at,
            "schedule_validation_status": (
                "matched_snapshot" if schedule_verified else "incomplete"
            ),
            "schedule_applied": False,
            "schedule_unchanged": True if schedule_verified else None,
            "last_known_good_active": True,
            "promotion_requirement": (
                "Any changed route/calendar group requires schema validation and "
                "atomic durable promotion before it can replace the routing snapshot."
            ),
            "schedule_change_detected": any(
                result.get("schedule_mismatch") is True
                for result in results
            ),
            "data_changed": any(
                result.get("schedule_mismatch") is True
                for result in results
            ),
            "source_content_changed": any(
                result.get("content_changed_since_previous_check") is True
                for result in results
            ),
            "excluded_references": copy.deepcopy(EXCLUDED_REFERENCES),
            "limitations": (
                "This refresh checks all six reviewed official sources and compares "
                "every in-scope route, direction, timezone and calendar group with "
                "the committed snapshot. It does not scrape arbitrary blogs, "
                "private apps, or seat inventory. Singapore Cruise Centre rows are "
                "validated as a same-day operations board and do not replace the "
                "recurring operator timetable snapshot. "
                "Changed or unrepresentable calendars are not auto-promoted."
            ),
        }
        _last_report = copy.deepcopy(report)
        _last_refresh_monotonic = (
            time.monotonic() if monotonic_now is None else monotonic_value
        )
        return report


def _reset_refresh_state_for_tests() -> None:
    """Reset process-local cache; intentionally private to verification code."""
    global _last_report, _last_refresh_monotonic, _last_schedule_verified_at
    with _refresh_lock:
        _last_report = None
        _last_refresh_monotonic = None
        _last_schedule_verified_at = ferry_schedule.timetable_metadata(
            load_durable=False,
        )[
            "snapshot_verified_at"
        ]
        _last_hashes.clear()
