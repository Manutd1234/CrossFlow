import logging
import os
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple


# Vercel loads this entry point as ``backend.main`` from the repository root,
# while the backend service modules intentionally use ``models`` and
# ``services`` as their import roots. Add this file's directory explicitly so
# those imports resolve the same way in serverless and local executions.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


from fastapi import FastAPI, Header, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from auth import identity
from auth.routes import router as auth_router
from models.congestion_model import forecaster
from services import clock, ferry_freshness_store, ferry_refresh, ferry_schedule, router
from services import geocoder, google_routes_benchmark, live_traffic, historical_store
from services import routing_intelligence_store, shortcut_ingestion
from services import route_run_store, route_store
from services.route_identity import route_id as make_route_id, short_route_code
from services.service_contracts import ApprovedGraphOverrideSnapshot
from services.traffic_observations import (
    ObservationValidationError,
    SpatialTrafficObservation,
)
from services.route_learning_store import (
    DEFAULT_ROUTE_LEARNING_STORE,
    MAX_CLEAR_CONGESTION_SCORE,
    MIN_MAP_MATCH_CONFIDENCE,
    OBSERVATION_RETENTION_DAYS,
    VerifiedTraversalObservation,
)
from services.route_solver import (
    CORRIDORS,
    MAX_FREE_ROUTE_SNAP_M,
    MAX_MULTI_STOP_COUNT,
    MAX_MULTI_STOP_DWELL_MINS,
    ROUTE_LOCATIONS,
    optimize_route,
)
from services.route_solver import optimize_free_route, optimize_multi_stop_route
from services.simulator import get_live_corridor_telemetry, get_operations_summary


route_learning_store = DEFAULT_ROUTE_LEARNING_STORE
_CORRIDOR_IDS = frozenset(corridor["id"] for corridor in CORRIDORS)
_SHARED_ROUTING_INTELLIGENCE = (
    routing_intelligence_store.SupabaseRoutingIntelligenceStore()
)
_LOCAL_SHORTCUT_REVIEW_QUEUE = shortcut_ingestion.InMemoryShortcutReviewQueue()
_SHORTCUT_RUNTIME_LOCK = threading.RLock()
_SHORTCUT_POLICY_CACHE_KEY: Optional[str] = None
_SHORTCUT_POLICY_CACHE: Optional[shortcut_ingestion.SourcePolicy] = None
_SHORTCUT_POLICY_ERROR: Optional[str] = None
_SHORTCUT_GRAPH_INDEX: Optional[shortcut_ingestion.GraphIndex] = None
_ROUTE_STORE = route_store.DEFAULT_ROUTE_STORE

_LOGGER = logging.getLogger(__name__)

# Set to "0" for a serverless deployment, where a background thread cannot
# outlive the invocation that started it and would only steal its CPU.
_WARM_ROUTING_CACHES = os.environ.get("CROSSFLOW_WARM_ROUTING_CACHES", "1") == "1"


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """Build the per-vehicle routing caches before the first request needs them.

    Cold, a single route request spends ten-plus seconds building this profile's
    edge features and reachable core, which outlasts the browser's own request
    timeout: the route arrives correctly but far too late to be rendered. The
    build is pure CPU over the committed graph, so doing it on a daemon thread
    keeps startup instant and lets early requests fall back to building their
    own profile as before.
    """
    if _WARM_ROUTING_CACHES:
        def _warm() -> None:
            # Resolve the snapshot exactly as the route handlers do; it is part
            # of both cache keys, so warming any other value warms nothing.
            approved_snapshot, _ = _approved_override_snapshot_for_route()
            warmed = router.warm_profile_caches(
                approved_override_snapshot=approved_snapshot,
            )
            _LOGGER.info(
                "[warmup] %d routing profiles ready in %.1fs",
                len(warmed), sum(warmed.values()),
            )

        threading.Thread(
            target=_warm, name="crossflow-routing-warmup", daemon=True,
        ).start()
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="CrossFlow AI API",
    description=(
        "Smart mobility and cross-border logistics API for the Batam-Singapore "
        "corridor. Road geometry and distances come from OpenStreetMap; traffic "
        "telemetry uses TomTom flow when configured and an explicitly labelled "
        "Batam-shaped model otherwise."
    ),
    version="3.0.0",
)

# Enable CORS for the Vite frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # The API does not use cookies. Browsers do not permit credentialed CORS
    # together with a wildcard origin, so advertising both was contradictory.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Human sign-in for admins and drivers. Every corridor, ferry, model and
# routing endpoint below stays public and unauthenticated: auth is a per-route
# dependency, never middleware, so an unreachable Supabase cannot blank the app.
app.include_router(auth_router)




VehicleType = Literal[
    "COMMUTER", "ELECTRIC_CAR", "MOTORCYCLE", "EXPRESS_VAN",
    "MINIBUS", "CITY_BUS", "LIGHT_TRUCK", "CARGO_TRUCK",
]
RoutePreference = Literal["BALANCED", "FASTEST", "SHORTEST", "EASY", "LOCAL"]
VerificationMethod = Literal[
    "FIRST_PARTY_GPS_MAP_MATCH",
    "SIGNED_FLEET_TELEMETRY",
]
TrafficRoadClass = Literal[
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "track",
    "road", "ferry",
]


def _validate_schedule_fields(
    departure_at: Optional[datetime],
    arrive_by: Optional[datetime],
) -> None:
    """Validate the mutually exclusive full-timestamp scheduling inputs."""
    if departure_at is not None and arrive_by is not None:
        raise ValueError("Provide either departure_at or arrive_by, not both.")
    for field_name, value in (("departure_at", departure_at), ("arrive_by", arrive_by)):
        if value is not None and value.tzinfo is None:
            raise ValueError(f"{field_name} must include a timezone offset.")


class RouteRequest(BaseModel):
    """Named-corridor route request.

    ``departure_at`` and ``arrive_by`` are timezone-aware ISO-8601 timestamps
    and are mutually exclusive.  ``hour`` remains available as the legacy
    local-hour fallback when neither full timestamp is supplied.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "corridor_id": "batam-centre-to-nongsa",
                    "vehicle_type": "LIGHT_TRUCK",
                    "departure_at": "2026-08-15T08:00:00+07:00",
                    "weather": 0,
                    "route_preference": "BALANCED",
                },
                {
                    "corridor_id": "batam-centre-to-nongsa",
                    "vehicle_type": "LIGHT_TRUCK",
                    "arrive_by": "2026-08-15T10:00:00+07:00",
                },
            ],
        },
    )

    corridor_id: Optional[str] = None
    origin_id: Optional[str] = None
    destination_id: Optional[str] = None
    vehicle_type: VehicleType
    hour: Optional[int] = Field(
        default=14,
        ge=0,
        le=23,
        description=(
            "Legacy local departure hour (0-23). Used only when departure_at "
            "and arrive_by are omitted."
        ),
        examples=[14],
    )
    departure_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Requested departure as a timezone-aware ISO-8601 timestamp. "
            "Mutually exclusive with arrive_by."
        ),
        examples=["2026-08-15T08:00:00+07:00"],
    )
    arrive_by: Optional[datetime] = Field(
        default=None,
        description=(
            "Required arrival deadline as a timezone-aware ISO-8601 timestamp. "
            "The API searches for the latest feasible departure and is mutually "
            "exclusive with departure_at."
        ),
        examples=["2026-08-15T10:00:00+07:00"],
    )
    weather: Optional[int] = Field(default=0, ge=0, le=2)
    route_preference: RoutePreference = "BALANCED"

    @model_validator(mode="after")
    def validate_schedule(self):
        _validate_schedule_fields(self.departure_at, self.arrive_by)
        return self


class FreeRouteRequest(BaseModel):
    """Free-form route request within Singapore and/or Batam."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "origin_lat": 1.1308,
                    "origin_lng": 104.053,
                    "destination_lat": 1.145,
                    "destination_lng": 104.02,
                    "vehicle_type": "LIGHT_TRUCK",
                    "departure_at": "2026-08-15T08:00:00+07:00",
                },
                {
                    "origin_lat": 1.29,
                    "origin_lng": 103.85,
                    "destination_lat": 1.13,
                    "destination_lng": 104.05,
                    "vehicle_type": "COMMUTER",
                    "arrive_by": "2026-08-15T12:00:00+08:00",
                },
            ],
        },
    )

    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    destination_lat: float = Field(ge=-90, le=90)
    destination_lng: float = Field(ge=-180, le=180)
    origin_name: Optional[str] = Field(default=None, max_length=200)
    destination_name: Optional[str] = Field(default=None, max_length=200)
    vehicle_type: VehicleType = "COMMUTER"
    hour: Optional[int] = Field(
        default=14,
        ge=0,
        le=23,
        description=(
            "Legacy local departure hour (0-23). Used only when departure_at "
            "and arrive_by are omitted."
        ),
        examples=[14],
    )
    departure_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Requested departure as a timezone-aware ISO-8601 timestamp. "
            "Mutually exclusive with arrive_by."
        ),
        examples=["2026-08-15T08:00:00+07:00"],
    )
    arrive_by: Optional[datetime] = Field(
        default=None,
        description=(
            "Required arrival deadline as a timezone-aware ISO-8601 timestamp. "
            "The API searches for the latest feasible departure and is mutually "
            "exclusive with departure_at."
        ),
        examples=["2026-08-15T12:00:00+08:00"],
    )
    weather: Optional[int] = Field(default=0, ge=0, le=2)
    route_preference: RoutePreference = "BALANCED"

    @model_validator(mode="after")
    def validate_schedule(self):
        _validate_schedule_fields(self.departure_at, self.arrive_by)
        return self


class RouteStop(BaseModel):
    """One stop on a multi-destination journey."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    name: Optional[str] = Field(default=None, max_length=200)
    # Time spent at this stop before departing for the next one. Ignored on the
    # origin and the final destination, neither of which is waited at.
    dwell_mins: float = Field(default=0.0, ge=0, le=MAX_MULTI_STOP_DWELL_MINS)


class MultiStopRouteRequest(BaseModel):
    """Ordered stops for one scheduled multi-destination journey."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "stops": [
                        {"lat": 1.1308, "lng": 104.053, "name": "Origin"},
                        {
                            "lat": 1.145,
                            "lng": 104.02,
                            "name": "Checkpoint 1",
                            "dwell_mins": 15,
                        },
                        {"lat": 1.11, "lng": 104.06, "name": "Destination"},
                    ],
                    "vehicle_type": "LIGHT_TRUCK",
                    "departure_at": "2026-08-15T08:00:00+07:00",
                },
                {
                    "stops": [
                        {"lat": 1.1308, "lng": 104.053, "name": "Origin"},
                        {"lat": 1.145, "lng": 104.02, "name": "Checkpoint 1"},
                        {"lat": 1.11, "lng": 104.06, "name": "Destination"},
                    ],
                    "arrive_by": "2026-08-15T12:00:00+07:00",
                },
            ],
        },
    )

    stops: List[RouteStop] = Field(min_length=3, max_length=MAX_MULTI_STOP_COUNT)
    vehicle_type: VehicleType = "COMMUTER"
    hour: Optional[int] = Field(
        default=14,
        ge=0,
        le=23,
        description=(
            "Legacy local departure hour (0-23). Used only when departure_at "
            "and arrive_by are omitted."
        ),
        examples=[14],
    )
    departure_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Requested departure as a timezone-aware ISO-8601 timestamp. "
            "Mutually exclusive with arrive_by."
        ),
        examples=["2026-08-15T08:00:00+07:00"],
    )
    arrive_by: Optional[datetime] = Field(
        default=None,
        description=(
            "Required arrival deadline as a timezone-aware ISO-8601 timestamp. "
            "The API searches for the latest feasible departure and is mutually "
            "exclusive with departure_at."
        ),
        examples=["2026-08-15T12:00:00+07:00"],
    )
    weather: Optional[int] = Field(default=0, ge=0, le=2)
    route_preference: RoutePreference = "BALANCED"
    # Off by default: the requested order is the caller's itinerary, and
    # reordering it silently would change where the driver actually goes.
    optimize_order: bool = False

    @model_validator(mode="after")
    def validate_schedule(self):
        _validate_schedule_fields(self.departure_at, self.arrive_by)
        return self


class RouteSchedulingMetadata(BaseModel):
    """Scheduling interpretation returned with every calculated route."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    mode: Optional[str] = Field(
        default=None,
        description=(
            "Scheduling mode: HOUR for the legacy hour input, DEPART_AT for an "
            "explicit departure, or ARRIVE_BY for a deadline search."
        ),
    )
    requested_departure_at: Optional[datetime] = Field(
        default=None,
        description="Normalized requested departure timestamp, when supplied.",
    )
    requested_arrive_by: Optional[datetime] = Field(
        default=None,
        description="Normalized requested arrival deadline, when supplied.",
    )
    deadline_slack_mins: Optional[float] = Field(
        default=None,
        description=(
            "Minutes between the calculated arrival and arrive_by; positive "
            "means the route arrives before the deadline."
        ),
    )


class RouteResponse(BaseModel):
    """Common route response envelope returned by the optimization APIs.

    The route engines intentionally expose additional provider- and corridor-
    specific fields.  ``extra='allow'`` keeps those fields in the response and
    in persisted route retrievals while documenting the stable fields that
    drivers can rely on across route types.
    """

    model_config = ConfigDict(
        extra="allow",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "generated_at": "2026-08-15T00:00:00+00:00",
                    "data_source": "simulated",
                    "route_id": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "route_code": "7K3M9PQ",
                    "route_type": "ROAD_ROUTE",
                    "planned_departure": "2026-08-15T08:00:00+07:00",
                    "estimated_arrival": "2026-08-15T08:42:00+07:00",
                    "estimated_travel_time_mins": 42.0,
                    "total_eta_mins": 42.0,
                    "scheduling": {
                        "mode": "DEPART_AT",
                        "requested_departure_at": "2026-08-15T08:00:00+07:00",
                        "requested_arrive_by": None,
                        "deadline_slack_mins": None,
                    },
                    "route_geometry": [[104.053, 1.1308], [104.02, 1.145]],
                    "legs": [],
                    "provenance": {"routing": "A* over OpenStreetMap"},
                }
            ],
        },
    )

    generated_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the response envelope was generated.",
    )
    data_source: Optional[str] = Field(
        default=None,
        description="Data-source label for traffic and routing provenance.",
    )
    provenance: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Road, routing, traffic, and licensing provenance metadata.",
    )
    route_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Stable 64-character lowercase SHA-256 route identity. Drivers "
            "may use it to retrieve the persisted route."
        ),
    )
    route_code: Optional[str] = Field(
        default=None,
        min_length=7,
        max_length=7,
        pattern=r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$",
        description=(
            "Seven-character human-safe route code for driver retrieval; it is "
            "a lookup alias for route_id, not an access credential."
        ),
    )
    route_type: Optional[str] = Field(
        default=None,
        description="ROAD_ROUTE, MULTIMODAL_FERRY_ROUTE, or a multi-stop variant.",
    )
    corridor: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Selected corridor summary, including distance and endpoints.",
    )
    stops: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Ordered stop cards returned for a multi-stop route.",
    )
    legs: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Per-leg route results for multimodal and multi-stop journeys.",
    )
    requested_origin: Optional[Dict[str, Any]] = None
    requested_destination: Optional[Dict[str, Any]] = None
    vehicle_type: Optional[str] = None
    route_preference: Optional[str] = None
    planned_departure: Optional[datetime] = Field(
        default=None,
        description="Concrete departure used by the solver, with timezone offset.",
    )
    estimated_arrival: Optional[datetime] = Field(
        default=None,
        description="Estimated destination arrival, including modeled waits/dwell.",
    )
    estimated_travel_time_mins: Optional[float] = Field(
        default=None,
        description="Moving travel time in minutes, excluding route-level waits.",
    )
    total_eta_mins: Optional[float] = Field(
        default=None,
        description="End-to-end ETA in minutes, including modeled access/wait time.",
    )
    scheduling: Optional[RouteSchedulingMetadata] = Field(
        default=None,
        description="How hour, departure_at, or arrive_by was applied.",
    )
    route_geometry: Optional[Any] = Field(
        default=None,
        description="Combined route geometry as provider-native coordinate arrays.",
    )
    route_data_source: Optional[str] = None
    navigation: Optional[Any] = None
    next_matching_ferries: Optional[List[Dict[str, Any]]] = None


class RouteBenchmarkRequest(BaseModel):
    """Strict coordinates for an ephemeral, text-only provider comparison."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    origin_lat: float = Field(ge=-90, le=90)
    origin_lng: float = Field(ge=-180, le=180)
    destination_lat: float = Field(ge=-90, le=90)
    destination_lng: float = Field(ge=-180, le=180)
    route_preference: RoutePreference = "BALANCED"


class RouteLearningObservation(BaseModel):
    """Provider-free input for one verified, map-matched edge traversal."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    observation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    graph_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    source_node: int
    target_node: int
    road_index: int = Field(ge=-1)
    vehicle_type: VehicleType
    moving_duration_s: float = Field(gt=0)
    observed_at: datetime
    verification_method: VerificationMethod
    map_match_confidence: float = Field(
        ge=MIN_MAP_MATCH_CONFIDENCE,
        le=1.0,
    )
    weather: Literal[0] = 0
    network_congestion_score: float = Field(
        ge=0,
        le=MAX_CLEAR_CONGESTION_SCORE,
    )
    local_congestion_score: float = Field(
        ge=0,
        le=MAX_CLEAR_CONGESTION_SCORE,
    )

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset.")
        return value


class RouteLearningBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[RouteLearningObservation] = Field(
        min_length=1,
        max_length=500,
    )


class SpatialTrafficObservationInput(BaseModel):
    """Measurement fields only; source trust is selected by server config."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    corridor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    observed_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    actual_speed_kph: float = Field(gt=0, le=250)
    free_flow_speed_kph: float = Field(gt=0, le=250)
    road_class: TrafficRoadClass = "unclassified"
    capacity_vph: Optional[float] = Field(default=None, gt=0, le=100_000)
    terminal_distance_km: Optional[float] = Field(default=None, ge=0, le=500)
    upstream_event_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )

    @field_validator("observed_at")
    @classmethod
    def require_observation_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset.")
        return value


class SpatialTrafficObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[SpatialTrafficObservationInput] = Field(
        min_length=1,
        max_length=2_000,
    )


class ShortcutDocumentInput(BaseModel):
    """Untrusted content associated only with a server-pinned source URL."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    pinned_url_index: int = Field(default=0, ge=0, le=49)
    content_type: Literal["application/json", "text/plain", "text/html"]
    content: str = Field(min_length=1, max_length=262_144)
    document_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_document_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone offset.")
        return value


class ShortcutDocumentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[ShortcutDocumentInput] = Field(
        min_length=1,
        max_length=20,
    )


class ShortcutFetchRequest(BaseModel):
    """Select configured sources, never caller-provided network URLs."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] = Field(min_length=1, max_length=5)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item or len(item) > 64 for item in value
        ):
            raise ValueError("source_ids must be unique bounded identifiers.")
        return value


class ShortcutApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


def _configured_shortcut_reviewer() -> str:
    reviewer = os.environ.get("CROSSFLOW_SHORTCUT_REVIEWER_ID", "").strip()
    if (
        not reviewer
        or len(reviewer) > 128
        or not all(char.isalnum() or char in "._:@-" for char in reviewer)
    ):
        raise _routing_intelligence_http_error(
            503,
            "A server-owned shortcut reviewer identity is not configured.",
        )
    return reviewer


def _require_admin(
    x_crossflow_admin_token: Optional[str],
    *,
    detail: str = "Administrator authorization is required.",
) -> None:
    """Authorize mutation/diagnostic endpoints without timing-leaky equality."""
    expected = os.environ.get("CROSSFLOW_ADMIN_TOKEN", "")
    if not expected or not x_crossflow_admin_token or not secrets.compare_digest(
        x_crossflow_admin_token,
        expected,
    ):
        raise HTTPException(status_code=403, detail=detail)


_ROUTING_INTELLIGENCE_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}
MAX_SHORTCUT_FETCH_URLS_PER_REQUEST = 8


def _routing_intelligence_http_error(
    status_code: int,
    detail: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_ROUTING_INTELLIGENCE_NO_STORE_HEADERS,
    )


def _shortcut_runtime() -> tuple[
    shortcut_ingestion.SourcePolicy,
    shortcut_ingestion.GraphIndex,
]:
    """Load server-owned source policy and immutable graph index lazily."""
    global _SHORTCUT_POLICY_CACHE_KEY
    global _SHORTCUT_POLICY_CACHE
    global _SHORTCUT_POLICY_ERROR
    global _SHORTCUT_GRAPH_INDEX

    raw_policy = os.environ.get(shortcut_ingestion.SOURCE_POLICY_ENV, "")
    with _SHORTCUT_RUNTIME_LOCK:
        if raw_policy != _SHORTCUT_POLICY_CACHE_KEY:
            try:
                policy = shortcut_ingestion.source_policy_from_environment()
            except shortcut_ingestion.SourceContractError:
                _SHORTCUT_POLICY_CACHE = None
                _SHORTCUT_POLICY_ERROR = "invalid_server_source_policy"
            else:
                _SHORTCUT_POLICY_CACHE = policy
                _SHORTCUT_POLICY_ERROR = None
            _SHORTCUT_POLICY_CACHE_KEY = raw_policy
        if _SHORTCUT_GRAPH_INDEX is None:
            bounds = (
                router.GRAPH_META.get("actual_bounds")
                or router.GRAPH_META.get("bbox")
            )
            _SHORTCUT_GRAPH_INDEX = shortcut_ingestion.GraphIndex.from_runtime_nodes(
                router.NODES,
                bounds=bounds,
                graph_revision=router.GRAPH_REVISION,
            )
        if _SHORTCUT_POLICY_CACHE is None:
            raise _routing_intelligence_http_error(
                503,
                "The server-owned shortcut source policy is invalid.",
            )
        return _SHORTCUT_POLICY_CACHE, _SHORTCUT_GRAPH_INDEX


def _shared_routing_store_required(
) -> routing_intelligence_store.SupabaseRoutingIntelligenceStore:
    if not routing_intelligence_store.shared_store_ready():
        raise _routing_intelligence_http_error(
            503,
            "Shared routing-intelligence persistence is not configured.",
        )
    return _SHARED_ROUTING_INTELLIGENCE


def _approved_override_snapshot_for_route() -> tuple[
    ApprovedGraphOverrideSnapshot,
    str,
]:
    """Return an immutable reviewed snapshot, or a safe local-graph fallback."""
    empty = ApprovedGraphOverrideSnapshot(
        graph_revision=router.GRAPH_REVISION,
        override_revision=0,
        overrides=(),
    )
    if not routing_intelligence_store.shared_store_enabled():
        return empty, "local_graph_no_shared_override_store"
    if not routing_intelligence_store.configured():
        return empty, "local_graph_shared_override_credentials_unavailable"
    try:
        snapshot = _SHARED_ROUTING_INTELLIGENCE.approved_snapshot(
            graph_revision=router.GRAPH_REVISION,
        )
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        # The exception value is a stable internal reason code, never a URL,
        # response body, or credential.
        print(
            "[routing_intelligence] approved_snapshot_fallback "
            f"reason={error}",
        )
        return empty, "local_graph_shared_override_store_unavailable"
    return snapshot, "shared_reviewed_override_snapshot"


def _routing_intelligence_audit(
    result: Dict[str, Any],
    snapshot: ApprovedGraphOverrideSnapshot,
    snapshot_source: str,
) -> None:
    audit = result.setdefault("service_audit", {})
    if not isinstance(audit, dict):
        audit = {}
        result["service_audit"] = audit
    used_by_identity: Dict[tuple[str, str], Dict[str, str]] = {}
    for raw in result.get("approved_graph_overrides_used") or ():
        if not isinstance(raw, dict):
            continue
        override_id = raw.get("override_id")
        candidate_sha256 = raw.get("candidate_sha256")
        if not isinstance(override_id, str) or not isinstance(candidate_sha256, str):
            continue
        used_by_identity[(override_id, candidate_sha256)] = {
            "override_id": override_id,
            "candidate_sha256": candidate_sha256,
        }
    used_overrides = [
        used_by_identity[key] for key in sorted(used_by_identity)
    ]
    audit.update({
        "routing_contract_version": 1,
        "graph_revision": router.GRAPH_REVISION,
        "approved_override_revision": snapshot.override_revision,
        "approved_override_count": len(snapshot.overrides),
        "approved_override_used_count": len(used_overrides),
        "approved_graph_overrides_used": used_overrides,
        "approved_override_snapshot_source": snapshot_source,
        "fallback_provider": "local_openstreetmap",
    })


def envelope(
    payload: Dict[str, Any],
    now: datetime,
    *,
    data_source: str = "simulated",
    provenance_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    tomtom_active = data_source == "live"
    graph_nodes = router.GRAPH_META.get("node_count")
    route_source = payload.get("route_data_source")
    if route_source == "google_maps_directions_api":
        routing_provenance = "Google Maps Directions API native route"
    elif route_source == "supabase_pgrouting":
        routing_provenance = "Supabase pgRouting provider"
    elif route_source in {
        "multimodal_openstreetmap_official_timetable",
        "multimodal_offline_estimate",
    }:
        routing_provenance = (
            "Composed OpenStreetMap road access and official operator ferry "
            "timetable corridor; per-leg limitations are included"
        )
    elif route_source == "openstreetmap_osrm":
        routing_provenance = "OSRM routing over OpenStreetMap road data"
    elif route_source == "offline_access_estimate":
        routing_provenance = (
            "Deterministic access estimate; not turn-by-turn navigation"
        )
    elif route_source and route_source not in {"openstreetmap", "bundled_openstreetmap"}:
        routing_provenance = f"{route_source} route provider"
    else:
        routing_provenance = "A* over the committed OpenStreetMap road graph"
        if isinstance(graph_nodes, int):
            if router.GRAPH_META.get("runtime_vehicle_cores_required"):
                routing_provenance += (
                    f" ({graph_nodes:,} union-retention nodes; a mutually "
                    "reachable vehicle-specific core is selected per request)"
                )
            else:
                routing_provenance += f" ({graph_nodes:,} mutually reachable nodes)"
    is_multimodal = route_source in {
        "multimodal_openstreetmap_official_timetable",
        "multimodal_offline_estimate",
    }
    road_network_provenance = (
        "OpenStreetMap via OSRM plus committed Batam extract"
        if is_multimodal else "OpenStreetMap Batam Extract"
    )
    traffic_provenance = (
        "No cross-border live traffic applied; per-leg provider durations only"
        if is_multimodal
        else "TomTom Traffic Flow API (Live Flow)" if tomtom_active
        else "Historical & Telemetry Model"
    )
    return {
        "generated_at": clock.iso(now),
        "data_source": data_source,
        "provenance": {
            "road_network": road_network_provenance,
            "road_network_license": "ODbL",
            "routing": routing_provenance,
            "traffic": traffic_provenance,
            **(provenance_overrides or {}),
        },
        **payload,
    }


_ROUTE_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}


def _optional_user_id(authorization: Optional[str]) -> Optional[str]:
    """Resolve the caller's account id, or None for a guest.

    Route planning is public, so an absent, expired or malformed credential
    must never fail the request; it only means the run is recorded without
    attribution.
    """
    if not authorization or not identity.auth_enabled():
        return None
    try:
        return identity.require_user(authorization).id
    except Exception:  # noqa: BLE001 - attribution is never worth a 4xx here
        return None


def _persist_route_response(
    request: BaseModel,
    route_kind: str,
    result: Dict[str, Any],
    now: datetime,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach a stable route identity and best-effort persist the response.

    The request alone is not enough to identify a route: a graph revision or
    selected path can change while the caller sends the same request.  Include
    those stable route facts in the content-addressed identity, while leaving
    generated timestamps and other presentation-only fields out.
    """
    request_payload = request.model_dump(mode="json")
    result = dict(result)
    identity_payload = {
        "request": request_payload,
        "graph_revision": router.GRAPH_REVISION,
        "route_type": result.get("route_type"),
        "corridor": result.get("corridor"),
        "stops": result.get("stops"),
        "route_geometry": result.get("route_geometry"),
        "legs": [
            {
                key: leg.get(key)
                for key in ("from", "to", "route_geometry", "route_type")
            }
            for leg in (result.get("legs") or [])
            if isinstance(leg, dict)
        ],
        "approved_graph_overrides_used": result.get(
            "approved_graph_overrides_used", [],
        ),
        "schedule_provenance": result.get("schedule_provenance"),
    }
    identity = make_route_id(identity_payload, route_kind=route_kind)
    # The route calculation is complete before this helper runs. Persistence is
    # intentionally non-critical so a read-only/serverless filesystem cannot
    # turn a valid route into a 5xx response.
    try:
        route_code = _ROUTE_STORE.route_code_for(identity)
    except Exception:  # pragma: no cover - defensive storage boundary
        route_code = short_route_code(identity)
    result["route_id"] = identity
    result["route_code"] = route_code
    schedule_provenance = result.get("schedule_provenance")
    provenance_overrides = None
    if isinstance(schedule_provenance, dict):
        if schedule_provenance.get("source") == "committed_timetable_simulation":
            provenance_overrides = {
                "ferry_schedule": (
                    "Committed published timetable used for simulated exact-time "
                    "planning; no shared freshness or live ferry operations."
                ),
            }
        elif schedule_provenance.get("source") == "published_schedule":
            provenance_overrides = {
                "ferry_schedule": (
                    "Published operator timetable with shared Supabase freshness; "
                    "not live ferry operations."
                ),
            }
    payload = envelope(
        result,
        now,
        provenance_overrides=provenance_overrides,
    )
    try:
        _ROUTE_STORE.save(
            identity,
            payload,
            request=identity_payload,
            route_kind=route_kind,
            route_code=route_code,
        )
    except Exception as error:  # pragma: no cover - defensive storage boundary
        print(f"[route_store] persistence skipped ({type(error).__name__})")
    # Mirror the run into the shared table when one is configured. SQLite alone
    # is per-instance on serverless, so without this a code issued by one
    # worker cannot be retrieved from another. Failures here are logged inside
    # the store and never affect the response.
    try:
        route_run_store.save(
            identity,
            route_code,
            route_kind,
            identity_payload,
            payload,
            origin_name=request_payload.get("origin_name"),
            destination_name=request_payload.get("destination_name"),
            vehicle_type=request_payload.get("vehicle_type"),
            created_by=created_by,
        )
    except Exception as error:  # pragma: no cover - defensive storage boundary
        print(f"[route_runs] persistence skipped ({type(error).__name__})")
    return payload


@app.get("/")
def read_root():
    return {
        "status": "online",
        "system": "CrossFlow AI Engine",
        "corridor": "Batam-Singapore Corridor",
        "graph": router.GRAPH_META,
        "docs_url": "/docs",
    }


@app.get("/api/corridors")
@app.get("/corridors")
def get_corridors():
    """Corridor telemetry with 30 and 60 minute forecasts."""
    now = clock.now()
    telemetry = get_live_corridor_telemetry(now)
    # Record each observation to the historical store.
    for c in telemetry:
        historical_store.record(
            c["id"],
            c["live_congestion_score"],
            "simulated",
            now=now,
        )
    return envelope({"corridors": telemetry}, now)


@app.get("/api/corridor-routes")
@app.get("/corridor-routes")
def get_corridor_routes():
    """Road geometry for each corridor, from A* over the OSM network."""
    now = clock.now()
    routes = [
        {
            "id": c["id"],
            "name": c["name"],
            **(router.route_between(c["origin"], c["destination"]) or {}),
        }
        for c in CORRIDORS
    ]
    return envelope({"routes": routes}, now)


@app.get("/api/route-locations")
@app.get("/route-locations")
def get_route_locations():
    """Named origins and destinations available to the route planner."""
    now = clock.now()
    return envelope({"locations": ROUTE_LOCATIONS}, now)


@app.get("/api/vehicle-profiles")
@app.get("/vehicle-profiles")
def get_vehicle_profiles():
    """Auditable vehicle assumptions used by routing, ETA and emissions."""
    now = clock.now()
    return envelope({
        "vehicle_profiles": [
            router.vehicle_profile_payload(profile)
            for profile in router.VEHICLE_PROFILES.values()
        ],
    }, now)


@app.get("/api/route-preferences")
@app.get("/route-preferences")
def get_route_preferences():
    """Auditable nonnegative component weights for each A* objective."""
    now = clock.now()
    return envelope({
        "route_preferences": [
            router.route_preference_payload(preference)
            for preference in router.ROUTE_PREFERENCES.values()
        ],
        "default_route_preference": "BALANCED",
    }, now)


_BENCHMARK_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}


def _benchmark_http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_BENCHMARK_NO_STORE_HEADERS,
    )


@app.post("/api/route-benchmark")
def api_route_benchmark(req: RouteBenchmarkRequest, response: Response):
    """Return ephemeral Google route metrics without geometry or persistence."""
    for header, value in _BENCHMARK_NO_STORE_HEADERS.items():
        response.headers[header] = value

    if not google_routes_benchmark.is_enabled():
        raise _benchmark_http_error(404, "Route benchmark is disabled.")
    if not google_routes_benchmark.get_api_key():
        raise _benchmark_http_error(503, "Route benchmark is unavailable.")

    origin_node, origin_snap_m = router.snap_to_graph(
        req.origin_lat, req.origin_lng,
    )
    destination_node, destination_snap_m = router.snap_to_graph(
        req.destination_lat, req.destination_lng,
    )
    if origin_node is None or origin_snap_m > MAX_FREE_ROUTE_SNAP_M:
        raise _benchmark_http_error(
            400, "Origin is outside the supported Batam road network.",
        )
    if destination_node is None or destination_snap_m > MAX_FREE_ROUTE_SNAP_M:
        raise _benchmark_http_error(
            400, "Destination is outside the supported Batam road network.",
        )
    if origin_node == destination_node:
        raise _benchmark_http_error(
            400, "Origin and destination must snap to different road nodes.",
        )

    try:
        benchmark = google_routes_benchmark.benchmark_routes(
            req.origin_lat,
            req.origin_lng,
            req.destination_lat,
            req.destination_lng,
            req.route_preference,
        )
    except google_routes_benchmark.GoogleBenchmarkUnavailable as exc:
        raise _benchmark_http_error(
            503, "Route benchmark is temporarily unavailable.",
        ) from exc

    now = clock.now()
    return {
        "generated_at": clock.iso(now),
        "data_source": "google_routes_v2_text_benchmark",
        "provenance": {
            "benchmark": "Google Routes API v2",
            "attribution": "Google Maps",
            "policy_url": google_routes_benchmark.GOOGLE_MAPS_POLICY_URL,
            "external_route_content_persisted": False,
        },
        **benchmark,
    }


@app.post(
    "/api/optimize-route",
    response_model=RouteResponse,
    summary="Optimize a named corridor route",
    response_description=(
        "Persisted route envelope with a 64-character route_id and seven-character route_code."
    ),
)
@app.post("/optimize-route", response_model=RouteResponse, include_in_schema=False)
def api_optimize_route(
    req: RouteRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Calculate a named-corridor route and persist its response.

    Send exactly one of ``departure_at`` or ``arrive_by`` for full timestamp
    scheduling. With neither field, ``hour`` is interpreted as the next local
    departure hour (Batam time). The response includes ``planned_departure``,
    ``estimated_arrival``, a ``scheduling`` explanation, and stable route
    identifiers for later driver retrieval.
    """
    now = clock.now()
    schedule_verified_at, schedule_provenance = _route_schedule_authority()
    approved_snapshot, snapshot_source = _approved_override_snapshot_for_route()
    try:
        result = optimize_route(
            corridor_id=req.corridor_id,
            origin_id=req.origin_id,
            destination_id=req.destination_id,
            vehicle_type=req.vehicle_type,
            hour=req.hour if req.hour is not None else 14,
            weather=req.weather or 0,
            departure_at=req.departure_at,
            arrive_by=req.arrive_by,
            route_preference=req.route_preference,
            now=now,
            schedule_verified_at=schedule_verified_at,
            approved_override_snapshot=approved_snapshot,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    result["schedule_provenance"] = schedule_provenance
    _routing_intelligence_audit(result, approved_snapshot, snapshot_source)
    return _persist_route_response(
        req, "optimize-route", result, now,
        created_by=_optional_user_id(authorization),
    )


@app.post(
    "/api/optimize-free-route",
    response_model=RouteResponse,
    summary="Optimize a free-form route",
    response_description=(
        "Persisted route envelope with a 64-character route_id and seven-character route_code."
    ),
)
@app.post("/optimize-free-route", response_model=RouteResponse, include_in_schema=False)
def api_optimize_free_route(
    req: FreeRouteRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Route local or ferry-linked journeys within Singapore and Batam.

    Batam-only requests use the committed OSM A* graph. Singapore-involved
    requests compose road-access legs with an official timetable terminal
    corridor and retain a labelled continuity fallback when online OSRM is not
    available. Use exactly one of ``departure_at`` or ``arrive_by`` for
    timestamp scheduling, or use the legacy ``hour`` field. The response
    contains ``estimated_arrival``, ``scheduling``, ``route_id``, and
    ``route_code``.
    """
    now = clock.now()
    schedule_verified_at, schedule_provenance = _route_schedule_authority()
    approved_snapshot, snapshot_source = _approved_override_snapshot_for_route()
    try:
        result = optimize_free_route(
            origin_lat=req.origin_lat,
            origin_lng=req.origin_lng,
            destination_lat=req.destination_lat,
            destination_lng=req.destination_lng,
            vehicle_type=req.vehicle_type,
            hour=req.hour if req.hour is not None else 14,
            weather=req.weather or 0,
            departure_at=req.departure_at,
            arrive_by=req.arrive_by,
            now=now,
            origin_name=req.origin_name,
            destination_name=req.destination_name,
            route_preference=req.route_preference,
            schedule_verified_at=schedule_verified_at,
            approved_override_snapshot=approved_snapshot,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    result["schedule_provenance"] = schedule_provenance
    _routing_intelligence_audit(result, approved_snapshot, snapshot_source)
    return _persist_route_response(
        req, "optimize-free-route", result, now,
        created_by=_optional_user_id(authorization),
    )


@app.post(
    "/api/optimize-multi-stop-route",
    response_model=RouteResponse,
    summary="Optimize a multi-stop route",
    response_description=(
        "Persisted multi-stop route envelope with route identifiers and per-leg results."
    ),
)
@app.post(
    "/optimize-multi-stop-route",
    response_model=RouteResponse,
    include_in_schema=False,
)
def api_optimize_multi_stop_route(
    req: MultiStopRouteRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Schedule one journey through three or more ordered stops.

    Each consecutive pair is solved by the same engines as a two-point route —
    the committed Batam OSM graph, or the multimodal composer when a leg
    touches Singapore — and departs when the previous leg arrives plus that
    stop's dwell. Every leg runs a full A* search, so response time grows with
    the stop count. Use exactly one of ``departure_at`` or ``arrive_by`` for
    timestamp scheduling, or use the legacy ``hour`` field. The response
    includes the ordered ``stops``, per-leg ``legs``, ``estimated_arrival``,
    ``scheduling``, ``route_id``, and ``route_code``.
    """
    now = clock.now()
    schedule_verified_at, schedule_provenance = _route_schedule_authority()
    approved_snapshot, snapshot_source = _approved_override_snapshot_for_route()
    try:
        result = optimize_multi_stop_route(
            stops=[stop.model_dump() for stop in req.stops],
            vehicle_type=req.vehicle_type,
            hour=req.hour if req.hour is not None else 14,
            weather=req.weather or 0,
            departure_at=req.departure_at,
            arrive_by=req.arrive_by,
            now=now,
            route_preference=req.route_preference,
            optimize_order=req.optimize_order,
            schedule_verified_at=schedule_verified_at,
            approved_override_snapshot=approved_snapshot,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    result["schedule_provenance"] = schedule_provenance
    _routing_intelligence_audit(result, approved_snapshot, snapshot_source)
    return _persist_route_response(
        req, "optimize-multi-stop-route", result, now,
        created_by=_optional_user_id(authorization),
    )


@app.get(
    "/api/routes/{route_id}",
    response_model=RouteResponse,
    summary="Retrieve a persisted route",
    response_description=(
        "The same persisted route envelope returned by the optimization endpoint."
    ),
)
@app.get("/routes/{route_id}", response_model=RouteResponse, include_in_schema=False)
def api_get_route(
    route_id: str = Path(
        ...,
        min_length=7,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{64}|[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7})$",
    ),
    response: Response = None,
):
    """Retrieve a persisted route by the code dispatch issued for it.

    Drivers are deliberately unauthenticated: dispatch hands out a code and
    anyone holding it can load that journey, so this endpoint is public and the
    code itself is the capability. It grants read access to exactly one route
    and nothing else -- no listing, no account data, no other journeys. Route
    history, which does span accounts, stays behind sign-in on ``GET
    /api/routes``.

    Codes are seven characters from a 32-symbol alphabet, so guessing one is a
    1-in-34-billion draw per attempt; put a rate limit in front of this route
    if that margin ever needs to be wider.

    The path accepts either the full 64-character ``route_id`` or its
    seven-character ``route_code`` alias and returns the persisted route
    envelope with ``estimated_arrival``, ``scheduling``, and route identifiers.
    """
    if response is not None:
        for header, value in _ROUTE_NO_STORE_HEADERS.items():
            response.headers[header] = value
    stored = _ROUTE_STORE.get(route_id)
    if stored is None:
        # The local SQLite file is per-instance on serverless, so a code issued
        # by another worker is only findable in the shared table.
        stored = route_run_store.get(route_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Route not found.")
    return stored


@app.get(
    "/api/routes",
    summary="List recent route-solver runs",
    response_description=(
        "Recent run summaries, newest first. Summaries only: fetch a route "
        "code from /api/routes/{route_id} for the full envelope."
    ),
)
@app.get("/routes", include_in_schema=False)
def api_route_history(
    response: Response = None,
    limit: int = Query(default=20, ge=1, le=route_run_store.MAX_HISTORY_LIMIT),
    mine: bool = Query(
        default=False,
        description="Restrict to runs produced by the calling account.",
    ),
    authorization: Optional[str] = Header(default=None),
):
    """Return recent route-solver runs from the shared history table.

    History is only meaningful across instances, so it is served solely from
    the shared table; the per-instance SQLite file is deliberately not merged
    in, which would make the same list differ per worker. An administrator
    sees every account's runs, any signed-in account sees its own.
    """
    if not identity.auth_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Route history requires user authentication; set "
                "CROSSFLOW_AUTH_MODE=supabase."
            ),
        )
    user = identity.require_user(authorization)
    if not route_run_store.configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Shared route history is not configured; set SUPABASE_URL and "
                "SUPABASE_SECRET_KEY and create the crossflow_route_runs table."
            ),
        )
    # Only an administrator may see other accounts' runs.
    created_by = None if user.is_admin and not mine else user.id
    runs = route_run_store.recent(limit, created_by=created_by)
    # The store fails soft, so a project that is configured but whose table is
    # missing returns an empty list indistinguishable from "nothing planned
    # yet". Report that as the setup gap it is, naming the migration to run.
    if not runs and route_run_store.available() is False:
        raise HTTPException(
            status_code=503,
            detail=(
                "Shared route history is unreachable "
                f"(reason: {route_run_store.failure_reason()}). If this is "
                "http_404 the crossflow_route_runs table does not exist yet; "
                "run backend/data/route_runs.sql in the Supabase SQL editor."
            ),
        )
    if response is not None:
        for header, value in _ROUTE_NO_STORE_HEADERS.items():
            response.headers[header] = value
    return envelope(
        {
            "runs": runs,
            "scope": "all_accounts" if created_by is None else "own_account",
        },
        clock.now(),
    )


def _route_schedule_authority() -> Tuple[str, Dict[str, Any]]:
    """Load a route's timetable authority without ever implying live status.

    Exact departure and arrive-by calculations can be reproducible from the
    committed departure-slot snapshot.  In Vercel, that fallback is allowed
    only when explicitly enabled, because it does not provide shared freshness
    across function instances.  The response always carries the authority
    that selected its ferry cards so callers can distinguish the two cases.
    """
    try:
        # Both route solvers attach `next_matching_ferries`, including local
        # plans. The UI labels their timestamp "Last verified", so it must come
        # from the same durable row as the Ferry tab on every serverless worker.
        timetable = ferry_schedule.timetable_metadata()
    except ferry_freshness_store.FreshnessStoreUnavailable as error:
        if ferry_schedule.committed_snapshot_fallback_enabled():
            timetable = ferry_schedule.committed_timetable_metadata()
            return _committed_route_schedule_authority(timetable)
        raise HTTPException(
            status_code=503,
            detail=(
                "Current ferry verification freshness is temporarily "
                "unavailable; the route was not generated."
            ),
            headers={
                "Cache-Control": "private, no-store",
                "Pragma": "no-cache",
                "Retry-After": "5",
            },
        ) from error

    # Local single-instance development has always used the committed snapshot
    # when no shared store is configured. Preserve that mode, but make it
    # explicit on every route response instead of calling its timestamp live.
    if timetable.get("freshness_durability") != "shared_supabase":
        return _committed_route_schedule_authority(
            ferry_schedule.committed_timetable_metadata(),
        )

    return timetable["last_verified_at"], {
        "source": "published_schedule",
        "snapshot_id": timetable["snapshot_id"],
        "snapshot_verified_at": timetable["snapshot_verified_at"],
        "last_verified_at": timetable["last_verified_at"],
        "latest_checked_at": timetable["latest_checked_at"],
        "freshness_durability": timetable["freshness_durability"],
        "shared_freshness": True,
        "live": False,
        "limitations": timetable["limitations"],
    }


def _committed_route_schedule_authority(
    timetable: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """Describe deterministic planning against the bundled timetable only."""
    provenance: Dict[str, Any] = {
        "source": "committed_timetable_simulation",
        "snapshot_id": timetable["snapshot_id"],
        "snapshot_verified_at": timetable["snapshot_verified_at"],
        "last_verified_at": timetable["last_verified_at"],
        "latest_checked_at": None,
        "freshness_durability": timetable["freshness_durability"],
        "shared_freshness": False,
        "live": False,
        "limitations": timetable["limitations"],
    }
    return timetable["last_verified_at"], provenance


@app.get("/api/geocode")
@app.get("/geocode")
def api_geocode(
    q: str = Query(
        ..., min_length=1, max_length=160,
        description="Free-text place name or address within Singapore or Batam",
    ),
    limit: int = Query(5, ge=1, le=10),
):
    """Geocode a query within the Singapore-Batam corridor.

    Uses Nominatim (OpenStreetMap's public geocoder), bounded to both islands.
    Batam results include the nearest local graph node; Singapore results are
    routed by the multimodal service. Results are cached for 1 hour. Returns an empty list when
    Nominatim is unreachable (the frontend falls back to the named-location
    dropdown in that case).
    """
    now = clock.now()
    results = geocoder.geocode(q, limit=limit)
    return envelope({"results": results, "query": q}, now)


@app.get("/api/reverse-geocode")
@app.get("/reverse-geocode")
def api_reverse_geocode(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
):
    """Return the display name and nearest graph node for a lat/lng coordinate."""
    now = clock.now()
    result = geocoder.reverse_geocode(lat, lng)
    return envelope({"result": result}, now)


@app.get("/api/live-traffic")
@app.get("/live-traffic")
def api_live_traffic():
    """Per-corridor speed and congestion index from TomTom or the simulator.

    When TOMTOM_API_KEY is set, returns real traffic data. Otherwise, the
    simulator provides synthetic speeds consistent with the congestion model.
    Responses are cached for 2 minutes to stay within free-tier rate limits.
    """
    now = clock.now()
    data = live_traffic.get_live_traffic()
    source = "live" if data.get("overall_source") == "tomtom_live" else "simulated"
    return envelope(data, now, data_source=source)


@app.get("/api/historical-congestion")
@app.get("/historical-congestion")
def api_historical_congestion(
    corridor_id: str = Query(..., description="e.g. corridor-1"),
    days: int = Query(7, ge=1, le=30, description="History window in days"),
):
    """Historical congestion profile for one corridor.

    Returns:
      - hourly_profile: avg score by hour-of-day over the past `days` days.
      - weekly_trend: daily avg score for the last 30 days.
      - history_metadata: explicit provenance, freshness, and storage durability.
    The initial Batam-time seed and collected model snapshots are non-observed.
    `history_metadata.observed` is true only when every sample in the requested
    window is observed; `contains_observed_samples` also identifies mixed data.
    """
    if corridor_id not in _CORRIDOR_IDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown corridor_id: {corridor_id}",
        )

    now = clock.now()
    hourly = historical_store.get_hourly_profile(corridor_id, days=days, now=now)
    weekly = historical_store.get_weekly_trend(corridor_id, days=30, now=now)
    metadata = historical_store.get_history_metadata(
        corridor_id,
        days=days,
        now=now,
    )
    return envelope({
        "corridor_id": corridor_id,
        "hourly_profile": hourly,
        "weekly_trend": weekly,
        "days_requested": days,
        "history_metadata": metadata,
    }, now)


def _canonical_spatial_observation(
    item: SpatialTrafficObservationInput,
    now: datetime,
    source: str,
) -> SpatialTrafficObservation:
    return SpatialTrafficObservation.create(
        corridor_id=item.corridor_id,
        observed_at=item.observed_at,
        latitude=item.latitude,
        longitude=item.longitude,
        actual_speed_kph=item.actual_speed_kph,
        free_flow_speed_kph=item.free_flow_speed_kph,
        source=source,
        reviewed=False,
        road_class=item.road_class,
        capacity_vph=item.capacity_vph,
        terminal_distance_km=item.terminal_distance_km,
        # Batam/WIB is a server-owned invariant, not caller-provided metadata.
        local_timezone_offset_minutes=7 * 60,
        upstream_event_id=item.upstream_event_id,
        validation_now=now,
    )


def _configured_traffic_ingest_source() -> str:
    """Resolve the authenticated integration identity from server config.

    A request body must never be able to label itself as a verified sensor or
    provider. The deployment owner binds this admin-only endpoint to one
    audited integration by setting ``CROSSFLOW_TRAFFIC_INGEST_SOURCE``.
    """
    value = os.environ.get("CROSSFLOW_TRAFFIC_INGEST_SOURCE", "").strip()
    trusted_sources = {
        "loop_sensor", "probe_gps", "tomtom_live",
        "verified_traffic_observation",
    }
    if value not in trusted_sources:
        raise _routing_intelligence_http_error(
            503,
            "A trusted traffic-observation source is not configured.",
        )
    return value


def _routing_ingestion_payload(result: Any) -> Dict[str, Any]:
    return {
        "received": result.received,
        "unique": result.unique,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
        "observation_keys": list(result.observation_keys),
    }


@app.post("/api/routing-intelligence/traffic-observations")
def api_ingest_spatial_traffic_observations(
    req: SpatialTrafficObservationBatch,
    response: Response,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Persist a bounded batch of typed speed/free-flow observations."""
    _require_admin(x_crossflow_admin_token)
    response.headers.update(_ROUTING_INTELLIGENCE_NO_STORE_HEADERS)
    now = clock.now()
    source = _configured_traffic_ingest_source()
    try:
        observations = tuple(
            _canonical_spatial_observation(item, now, source)
            for item in req.observations
        )
        if routing_intelligence_store.shared_store_enabled():
            result = _shared_routing_store_required().ingest_spatial_batch(
                observations,
                now=now,
            )
            durability = "shared_supabase"
        else:
            result = historical_store.ingest_spatial_batch(
                observations,
                now=now,
            )
            durability = "local_sqlite"
    except routing_intelligence_store.RoutingIntelligenceStoreConflict as error:
        raise _routing_intelligence_http_error(
            409,
            "The spatial observation conflicts with durable history.",
        ) from error
    except (ObservationValidationError, ValueError) as error:
        raise _routing_intelligence_http_error(400, str(error)) from error
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        raise _routing_intelligence_http_error(
            503,
            "Shared spatial-history persistence is temporarily unavailable.",
        ) from error
    return envelope(
        {
            "ingestion": _routing_ingestion_payload(result),
            "durability": durability,
            "training_eligible": True,
        },
        now,
        data_source="validated_spatial_observations",
        provenance_overrides={
            "traffic_observations": (
                "Typed speed/free-flow ratios with fixed source trust policy"
            ),
        },
    )


def _shortcut_pipeline() -> tuple[
    shortcut_ingestion.SourcePolicy,
    shortcut_ingestion.ShortcutTipPipeline,
]:
    policy, graph = _shortcut_runtime()
    queue = (
        _shared_routing_store_required()
        if routing_intelligence_store.shared_store_enabled()
        else _LOCAL_SHORTCUT_REVIEW_QUEUE
    )
    return policy, shortcut_ingestion.ShortcutTipPipeline(
        policy,
        graph,
        review_queue=queue,
    )


def _server_pinned_document(
    item: ShortcutDocumentInput,
    policy: shortcut_ingestion.SourcePolicy,
) -> shortcut_ingestion.SourceDocument:
    registration = policy.registration(item.source_id)
    try:
        source_url = registration.pinned_urls[item.pinned_url_index]
    except IndexError as error:
        raise shortcut_ingestion.SourceContractError(
            "pinned_url_index does not identify a configured source URL."
        ) from error
    return shortcut_ingestion.SourceDocument(
        source_id=item.source_id,
        source_url=source_url,
        content_type=item.content_type,
        content=item.content,
        document_id=item.document_id,
        retrieved_at=item.retrieved_at,
    )


@app.post("/api/routing-intelligence/shortcut-documents")
def api_ingest_shortcut_documents(
    req: ShortcutDocumentBatch,
    response: Response,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Parse supplied content only against server-pinned source identities."""
    _require_admin(x_crossflow_admin_token)
    response.headers.update(_ROUTING_INTELLIGENCE_NO_STORE_HEADERS)
    now = clock.now()
    try:
        policy, pipeline = _shortcut_pipeline()
        result = pipeline.ingest(
            _server_pinned_document(item, policy) for item in req.documents
        )
    except routing_intelligence_store.RoutingIntelligenceStoreConflict as error:
        raise _routing_intelligence_http_error(
            409,
            "The shortcut candidate conflicts with durable review state.",
        ) from error
    except shortcut_ingestion.ShortcutIngestionError as error:
        raise _routing_intelligence_http_error(400, str(error)) from error
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        raise _routing_intelligence_http_error(
            503,
            "The durable shortcut review queue is temporarily unavailable.",
        ) from error
    return envelope(
        {
            "shortcut_ingestion": result.to_dict(),
            "durability": (
                "shared_supabase" if routing_intelligence_store.shared_store_enabled()
                else "process_local_review_queue"
            ),
            "activation_policy": "review_required_no_automatic_activation",
        },
        now,
        data_source="review_gated_crowd_shortcut_candidates",
    )


@app.post("/api/routing-intelligence/shortcut-fetch")
def api_fetch_shortcut_sources(
    req: ShortcutFetchRequest,
    response: Response,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Fetch selected server-pinned sources with bounded SSRF-safe transport."""
    _require_admin(x_crossflow_admin_token)
    response.headers.update(_ROUTING_INTELLIGENCE_NO_STORE_HEADERS)
    now = clock.now()
    try:
        policy, pipeline = _shortcut_pipeline()
        fetcher = shortcut_ingestion.AllowlistedSourceFetcher(policy)
        documents = []
        fetch_results = []
        targets: list[tuple[str, str]] = []
        for source_id in req.source_ids:
            registration = policy.registration(source_id)
            if not registration.pinned_urls:
                raise shortcut_ingestion.SourceContractError(
                    "Selected shortcut source has no server-pinned URL."
                )
            targets.extend(
                (source_id, source_url)
                for source_url in registration.pinned_urls
            )
        if len(targets) > MAX_SHORTCUT_FETCH_URLS_PER_REQUEST:
            raise shortcut_ingestion.SourceContractError(
                "Selected sources exceed the per-request pinned URL limit."
            )
        with ThreadPoolExecutor(
            max_workers=min(4, len(targets)),
            thread_name_prefix="shortcut-api-fetch",
        ) as executor:
            future_targets = {
                executor.submit(fetcher.fetch, source_id, source_url): source_id
                for source_id, source_url in targets
            }
            for future in as_completed(future_targets):
                source_id = future_targets[future]
                try:
                    document = future.result()
                except shortcut_ingestion.ShortcutIngestionError as error:
                    fetch_results.append({
                        "source_id": source_id,
                        "status": "failed_safely",
                        "reason": type(error).__name__,
                    })
                else:
                    documents.append(document)
                    fetch_results.append({
                        "source_id": source_id,
                        "status": "fetched",
                        "document_id": document.document_id,
                    })
        fetch_results.sort(key=lambda item: (
            item["source_id"], item["status"], item.get("document_id", ""),
        ))
        result = pipeline.ingest(documents)
    except routing_intelligence_store.RoutingIntelligenceStoreConflict as error:
        raise _routing_intelligence_http_error(
            409,
            "The shortcut candidate conflicts with durable review state.",
        ) from error
    except shortcut_ingestion.ShortcutIngestionError as error:
        raise _routing_intelligence_http_error(400, str(error)) from error
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        raise _routing_intelligence_http_error(
            503,
            "The durable shortcut review queue is temporarily unavailable.",
        ) from error
    return envelope(
        {
            "fetch_results": fetch_results,
            "shortcut_ingestion": result.to_dict(),
            "activation_policy": "review_required_no_automatic_activation",
        },
        now,
        data_source="server_allowlisted_shortcut_sources",
    )


@app.post("/api/routing-intelligence/shortcut-approve")
def api_approve_shortcut_candidate(
    req: ShortcutApprovalRequest,
    response: Response,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Atomically promote one exact persisted candidate after human review."""
    _require_admin(x_crossflow_admin_token)
    response.headers.update(_ROUTING_INTELLIGENCE_NO_STORE_HEADERS)
    now = clock.now()
    store = _shared_routing_store_required()
    approved_by = _configured_shortcut_reviewer()
    try:
        approved, revision = store.approve_candidate(
            req.override_id,
            approved_by=approved_by,
            approved_at=now,
            active_graph_revision=router.GRAPH_REVISION,
        )
    except ValueError as error:
        raise _routing_intelligence_http_error(409, str(error)) from error
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        raise _routing_intelligence_http_error(
            503,
            "Shortcut approval persistence is temporarily unavailable.",
        ) from error
    return envelope(
        {
            "approval": {
                "override_id": approved.override_id,
                "graph_revision": approved.graph_revision,
                "override_revision": revision,
                "approved_by": approved.approved_by,
                "approved_at": approved.approved_at.isoformat(),
                "candidate_sha256": approved.candidate_sha256,
            },
            "activation_policy": "immutable_reviewed_snapshot_only",
        },
        now,
        data_source="human_reviewed_graph_override",
    )


@app.get("/api/routing-intelligence/status")
def api_routing_intelligence_status(
    response: Response,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Return protected, secret-free service and persistence status."""
    _require_admin(x_crossflow_admin_token)
    response.headers.update(_ROUTING_INTELLIGENCE_NO_STORE_HEADERS)
    now = clock.now()
    policy, _graph = _shortcut_runtime()
    cache_info = router._route_between_nodes_cached.cache_info()
    shared = routing_intelligence_store.shared_store_enabled()
    local_storage = historical_store.get_storage_metadata()
    store_status = (
        _SHARED_ROUTING_INTELLIGENCE.status(probe=True)
        if shared else {
            **local_storage,
            "enabled": False,
            "configured": False,
            "implementation": "local_sqlite_and_process_review_queue",
            "durable_spatial_history": local_storage["durable"],
            "durable_inactive_review_queue": False,
            "schema_health": "not_applicable_local_mode",
        }
    )
    return envelope({
        "routing_intelligence": {
            "contract_version": 1,
            "graph_revision": router.GRAPH_REVISION,
            "graph_schema_version": router.GRAPH_META.get("schema_version"),
            "vehicle_core_node_counts": {
                mode: len(router._main_routing_core(profile))
                for mode, profile in (
                    ("MOTORCYCLE", "MOTORCYCLE"),
                    ("PASSENGER_CAR", "COMMUTER"),
                    ("FREIGHT_TRUCK", "CARGO_TRUCK"),
                )
            },
            "shortcut_sources": {
                "configured_count": len(policy.registrations),
                "source_ids": [
                    registration.source_id
                    for registration in policy.registrations
                ],
                "network_fetch_enabled": bool(policy.registrations),
            },
            "persistence": store_status,
            "route_cache": {
                "implementation": "bounded_lru_request_snapshot",
                "max_entries": cache_info.maxsize,
                "entries": cache_info.currsize,
                "hits": cache_info.hits,
                "misses": cache_info.misses,
            },
            "fallback_order": [
                "reviewed_override_snapshot_plus_local_osm",
                "local_osm_without_overrides",
            ],
        },
    }, now)


@app.get("/api/model-status")
@app.get("/model-status")
def api_model_status():
    """Return metrics plus the model's declared training and holdout scope."""
    now = clock.now()
    metrics = {
        **forecaster.metrics,
        "retraining_enabled": bool(os.environ.get("CROSSFLOW_ADMIN_TOKEN", "")),
    }
    return envelope({"metrics": metrics}, now)


def _route_learning_envelope(payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    return {
        "generated_at": clock.iso(now),
        "data_source": "verified_actual_traversal",
        "provenance": {
            "observations": "Allowlisted, map-matched first-party traversal telemetry",
            "edge_identity": "Exact directed edges in the committed OpenStreetMap graph",
            "external_route_provider_content_persisted": False,
            "google_routes_content_persisted": False,
        },
        **payload,
    }


def _canonical_learning_observation(
    item: RouteLearningObservation,
    now: datetime,
) -> VerifiedTraversalObservation:
    if item.graph_revision != router.GRAPH_REVISION:
        raise HTTPException(
            status_code=409,
            detail="Observation graph_revision does not match the active OSM graph.",
        )
    edge = router.road_edge_by_key(
        item.source_node,
        item.target_node,
        item.road_index,
    )
    if edge is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Observation does not identify an exact directed edge in the "
                "active OSM graph."
            ),
        )
    observed_at = item.observed_at.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    if observed_at > now_utc + timedelta(minutes=5):
        raise HTTPException(
            status_code=400,
            detail="Observation timestamp is more than five minutes in the future.",
        )
    if observed_at < now_utc - timedelta(days=OBSERVATION_RETENTION_DAYS):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Observation is older than the {OBSERVATION_RETENTION_DAYS}-day "
                "learning window."
            ),
        )

    profile = router.vehicle_profile(item.vehicle_type)
    physical_floor_s = edge.distance_m / profile.max_speed_kph * 3.6
    # Moving time deliberately excludes stopped/turn dwell. Two km/h plus a
    # 30-second allowance for very short GPS segments is a conservative upper
    # bound for a clear-road observation.
    plausible_ceiling_s = max(30.0, edge.distance_m / 2.0 * 3.6)
    if item.moving_duration_s + 1e-6 < physical_floor_s:
        raise HTTPException(
            status_code=400,
            detail="Moving duration exceeds the vehicle's physical maximum speed.",
        )
    if item.moving_duration_s > plausible_ceiling_s:
        raise HTTPException(
            status_code=400,
            detail="Moving duration is implausibly slow for a clear-road edge sample.",
        )
    return VerifiedTraversalObservation(
        observation_id=item.observation_id,
        graph_revision=item.graph_revision,
        source_node=item.source_node,
        target_node=item.target_node,
        road_index=item.road_index,
        vehicle_type=item.vehicle_type,
        moving_duration_s=round(item.moving_duration_s, 3),
        observed_at_epoch=int(observed_at.timestamp()),
        verification_method=item.verification_method,
        map_match_confidence=round(item.map_match_confidence, 4),
        weather=item.weather,
        network_congestion_score=round(item.network_congestion_score, 2),
        local_congestion_score=round(item.local_congestion_score, 2),
        edge_distance_m=round(edge.distance_m, 3),
    )


@app.post("/api/route-learning/observations")
@app.post("/route-learning/observations")
def api_ingest_route_learning_observations(
    req: RouteLearningBatch,
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Ingest verified actual traversals; never route-provider responses."""
    _require_admin(x_crossflow_admin_token)
    now = clock.now()
    observations = [
        _canonical_learning_observation(item, now)
        for item in req.observations
    ]
    result = route_learning_store.ingest(
        observations,
        current_graph_revision=router.GRAPH_REVISION,
    )
    return _route_learning_envelope({
        "ingestion": {
            "accepted": result.accepted,
            "duplicates": result.duplicates,
            "rejected_stale_graph": result.rejected_stale_graph,
            "learning_revision": result.revision,
            "qualifying_edge_count": result.qualifying_edge_count,
        },
    }, now)


@app.get("/api/route-learning/status")
@app.get("/route-learning/status")
def api_route_learning_status(
    x_crossflow_admin_token: Optional[str] = Header(default=None),
):
    """Return protected, audit-safe route-learning persistence status."""
    _require_admin(x_crossflow_admin_token)
    now = clock.now()
    return _route_learning_envelope({
        "route_learning": route_learning_store.status(router.GRAPH_REVISION),
    }, now)


@app.post("/api/retrain-model")
@app.post("/retrain-model")
def api_retrain_model(x_crossflow_admin_token: Optional[str] = Header(default=None)):
    """Retrain from one bounded, canonical spatial-history snapshot."""
    _require_admin(
        x_crossflow_admin_token,
        detail="Model retraining is admin-only.",
    )
    now = clock.now()
    try:
        if routing_intelligence_store.shared_store_enabled():
            observations = (
                _shared_routing_store_required().get_spatial_training_dataset()
            )
            durability = "shared_supabase"
        else:
            observations = historical_store.get_spatial_training_dataset(now=now)
            durability = "local_sqlite"
        updated_metrics = forecaster.retrain_from_observations(observations)
    except routing_intelligence_store.RoutingIntelligenceStoreUnavailable as error:
        raise _routing_intelligence_http_error(
            503,
            "The shared spatial training snapshot is temporarily unavailable.",
        ) from error
    except (ObservationValidationError, ValueError) as error:
        raise _routing_intelligence_http_error(
            409,
            "The spatial training snapshot failed validation.",
        ) from error
    return envelope({
        "metrics": {**updated_metrics, "retraining_enabled": True},
        "training_snapshot": {
            "durability": durability,
            "candidate_rows": len(observations),
            "bounded_max_rows": 100_000,
            "validation": "canonical_spatial_observations",
        },
        "message": (
            "Model retraining evaluated the validated spatial history snapshot; "
            "inspect last_retrain_skipped_reason and validation metrics before "
            "interpreting the result."
        ),
    }, now)


@app.get("/api/ferries")
@app.get("/ferries")
def get_ferries(response: Response):
    """Upcoming published departure slots, always ahead of current Batam time."""
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    now = clock.now()
    try:
        timetable = ferry_schedule.timetable_metadata()
    except ferry_freshness_store.FreshnessStoreUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "Current ferry verification freshness is temporarily unavailable; "
                "an older timestamp will not be presented as latest."
            ),
            headers={
                "Cache-Control": "private, no-store",
                "Pragma": "no-cache",
                "Retry-After": "5",
            },
        ) from error
    sailings = ferry_schedule.generate_sailings(
        now,
        horizon_hours=12,
        schedule_verified_at=timetable["last_verified_at"],
    )
    return envelope(
        {
            "ferries": sailings,
            "total_active": len(sailings),
            "timetable": timetable,
        },
        now,
        data_source="published_schedule",
        provenance_overrides={
            "ferry_schedule": (
                "Official operator timetable snapshot; last verified "
                f"{timetable['last_verified_at']}; not live operations"
            ),
        },
    )


@app.post("/api/ferry-refresh")
def refresh_ferry_sources(response: Response):
    """Recheck the fixed official-source allowlist and return current planning output.

    No caller-controlled URL is accepted. All six reviewed official sources are
    checked, and a failed or ambiguous check never replaces the committed
    last-known-good timetable.
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    now = clock.now()
    refresh = ferry_refresh.refresh_official_sources(now, completed_now=clock.now)
    try:
        ferry_schedule.record_refresh_result(
            refresh["finished_at"],
            refresh.get("schedule_verified_at")
            if refresh.get("schedule_verified") else None,
        )
    except ferry_freshness_store.FreshnessStoreUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "The source check completed, but its latest verification time "
                "could not be durably published. Please retry."
            ),
            headers={
                "Cache-Control": "private, no-store",
                "Pragma": "no-cache",
                "Retry-After": "5",
            },
        ) from error
    timetable = ferry_schedule.timetable_metadata(load_durable=False)
    # Keep the report and timetable views of the most recent successful
    # semantic verification identical, including after a failed check on a
    # freshly started instance that recovered the durable prior value.
    refresh["schedule_verified_at"] = timetable["last_verified_at"]
    sailings = ferry_schedule.generate_sailings(
        now,
        horizon_hours=12,
        schedule_verified_at=timetable["last_verified_at"],
    )
    ports = ferry_schedule.get_port_intelligence(now)
    return envelope(
        {
            "refresh": refresh,
            "ferries": sailings,
            "total_active": len(sailings),
            "ports": ports,
            "timetable": timetable,
        },
        now,
        data_source="published_schedule",
        provenance_overrides={
            "ferry_schedule": (
                "Official-source refresh check with committed last-known-good "
                "timetable; per-source outcomes and restrictions included"
            ),
            "operations": (
                "Schedule-informed planning estimates; not observed passenger queues"
            ),
        },
    )


@app.get("/api/port-intelligence")
@app.get("/port-intelligence")
def get_port_intelligence():
    """Schedule-informed terminal planning estimates with official references."""
    now = clock.now()
    ports = ferry_schedule.get_port_intelligence(now)
    return envelope(
        {"ports": ports},
        now,
        data_source="simulated",
        provenance_overrides={
            "operations": (
                "Schedule-informed planning estimate; not an observed passenger queue"
            ),
            "ferry_schedule": "Source-dated official operator timetable snapshot",
        },
    )


@app.get("/api/operations")
@app.get("/operations")
def get_ops():
    """Modelled operator analytics, bottlenecks and dispatch alerts."""
    now = clock.now()
    return envelope(
        get_operations_summary(now),
        now,
        data_source="simulated",
        provenance_overrides={
            "operations": (
                "CrossFlow modelled operations scenario; not observed or measured"
            ),
            "traffic": "CrossFlow synthetic Batam-shaped congestion model",
            "ferry_schedule": (
                "Source-dated published timetable snapshot; scheduled "
                "departures only"
            ),
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
