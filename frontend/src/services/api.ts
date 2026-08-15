import {
  ApiHistoryMetadata, Corridor, CorridorRoute, DataSource, Envelope, Fetched,
  FerryRefreshReport, FerryRefreshSourceResult, FerrySchedule, FerryTimetableMetadata,
  FetchedFerryRefresh, FetchedFerrySchedule, FreeLocation, GeocodedLocation, HistoricalProfile,
  LiveTrafficData, ModelMetrics, NavigationData, OperationsSummary, PortStatus, RouteLocation,
  RouteBenchmarkResult, RouteEndpointSnapshot, RouteOptimizationResult, RoutePreference,
  RouteScheduleOptions,
  RoutingCostBreakdown, VehicleType,
} from '../types';

import {
  INITIAL_CORRIDORS, MOCK_OPERATIONS, CORRIDOR_INDEX, ROUTE_LOCATIONS,
  EMISSIONS_PRESSURE_MODEL, PUBLISHED_FERRY_TIMETABLE_METADATA,
  offlineCongestionZones, offlineFerries,
} from '../data/mockData';
import { browserModeledHistoricalProfile } from '../data/offlineHistory';
import { BUNDLED_RF_VALIDATION } from '../data/modelManifest';
import { CORRIDOR_STREET_GEOMETRIES } from '../data/corridorGeometries';
import { predictLocal } from './localForecast';
import { planWithBundledRoadGraph } from './offlineRoadRouter';
import {
  isPassengerFerryIncompatibleVehicle,
  locationRegion,
  offlineCrossBorderOptimize,
  PASSENGER_FERRY_TRUCK_MESSAGE,
} from './crossBorderRouting';
import type { OfflineRoadPlan, OfflineRoutingContext } from '../workers/offlineRouterCore';
import { vehicleProfile, vehicleProfileSnapshot } from '../data/vehicleCatalog';
import { batamParts, nextBatamHour, toBatamIso } from '../utils/batamTime';


/** Same-origin by default (Vite proxies /api in development); configurable for split deployments. */
const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '');
const API_BASE = configuredApiBase ?? '';


const CORRIDOR_TIMEOUT_MS = 6000;
const ROUTE_TIMEOUT_MS = 15000;
const FERRY_REFRESH_TIMEOUT_MS = 20_000;
const FERRY_TERMINAL_MATCH_KM = 0.5;

/** Serialize only the new scheduling fields that are actually selected. */
function scheduleRequestFields(schedule?: RouteScheduleOptions): RouteScheduleOptions {
  if (!schedule) return {};
  const departureAt = schedule.departure_at?.trim();
  const arriveBy = schedule.arrive_by?.trim();
  // The backend enforces mutual exclusivity too; keeping this guard here
  // avoids sending an ambiguous request if a caller changes modes mid-flight.
  if (departureAt) return { departure_at: departureAt };
  if (arriveBy) return { arrive_by: arriveBy };
  return {};
}

function hourFromSchedule(schedule?: RouteScheduleOptions): number | undefined {
  const value = schedule?.departure_at;
  if (!value) return undefined;
  const match = value.match(/T(\d{2})(?::\d{2})?/);
  if (!match) return undefined;
  const hour = Number(match[1]);
  return Number.isInteger(hour) && hour >= 0 && hour <= 23 ? hour : undefined;
}

function requireServerSchedule(schedule?: RouteScheduleOptions): void {
  if (schedule?.arrive_by) {
    throw new ApiRequestError(
      'Arrive-by planning requires the scheduling API; it is unavailable right now.',
      503,
    );
  }
  if (schedule?.departure_at) {
    throw new ApiRequestError(
      'Exact departure-time planning requires the scheduling API; it is unavailable right now.',
      503,
    );
  }
}

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
  }
}

/**
 * One request path for every endpoint.
 *
 * Each fetcher previously wrote `if (res.ok) { ... }` with no `else`, so an
 * HTTP 500 or 404 fell through to the mock return without even logging — a
 * broken-but-reachable backend was completely invisible.
 */
async function getJSON<T>(
  path: string,
  timeoutMs: number,
  init?: RequestInit,
  throwClientErrors = false,
): Promise<T | null> {
  try {
    const timeoutSignal = AbortSignal.timeout(timeoutMs);
    const signal = init?.signal
      ? AbortSignal.any([init.signal, timeoutSignal])
      : timeoutSignal;
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal,
    });
    if (!res.ok) {
      console.warn(`[crossflow] ${path} -> HTTP ${res.status} ${res.statusText}`);
      // Only semantic validation failures are authoritative. A 404/405 from
      // an older deployment, plus rate limits and gateway timeouts, should
      // still permit the bundled road engine to plan the trip.
      if (throwClientErrors && (res.status === 400 || res.status === 422)) {
        let message = `The route request was rejected (${res.status}).`;
        try {
          const body = await res.json() as { detail?: unknown };
          if (typeof body.detail === 'string' && body.detail.trim()) message = body.detail;
        } catch {
          // The status-specific fallback above is still useful for non-JSON errors.
        }
        throw new ApiRequestError(message, res.status);
      }
      return null;
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiRequestError) throw err;
    console.warn(`[crossflow] ${path} unreachable:`, err);
    return null;
  }
}

function sourceOf(payload: Envelope | null): DataSource {
  if (!payload) return 'offline';
  // Absent provenance means an older backend; 'simulated' is the honest default.
  return payload.data_source === 'live' ? 'live' : 'simulated';
}

function stampOf(payload: Envelope | null): string {
  const raw = payload?.generated_at;
  if (raw && !Number.isNaN(new Date(raw).getTime())) return raw;
  return toBatamIso(new Date());
}

function wrap<T>(payload: Envelope | null, data: T): Fetched<T> {
  return {
    data,
    source: sourceOf(payload),
    fetchedAt: stampOf(payload),
    provenance: payload?.provenance,
  };
}

function hasFiniteRoadGeometry(geometry: unknown): geometry is [number, number][] {
  return Array.isArray(geometry)
    && geometry.length >= 2
    && geometry.every((point) => Array.isArray(point)
      && point.length === 2
      && point.every((coordinate) => typeof coordinate === 'number' && Number.isFinite(coordinate)));
}

/** Reject stale/malformed 200 responses before they can suppress local A*. */
function validatedRoadPayload<T extends RouteOptimizationResult>(payload: T | null): T | null {
  const validPrimary = payload
    && payload.route_data_source !== 'offline_straight_line'
    && typeof payload.route_data_source === 'string'
    && Number.isFinite(payload.corridor?.distance_km)
    && payload.corridor.distance_km > 0
    && hasFiniteRoadGeometry(payload.route_geometry)
    && Array.isArray(payload.navigation?.maneuvers)
    && payload.navigation.maneuvers.length >= 2;
  if (!validPrimary || !payload) {
    if (payload) console.warn('[crossflow] ignored a non-drivable or incomplete route response');
    return null;
  }
  const alternativeRoutes = (payload.alternative_routes ?? []).filter((route) => (
    Number.isFinite(route.distance_km)
    && route.distance_km > 0
    && hasFiniteRoadGeometry(route.route_geometry)
    && Array.isArray(route.navigation?.maneuvers)
    && route.navigation.maneuvers.length >= 2
  ));
  return { ...payload, alternative_routes: alternativeRoutes };
}

export async function fetchCorridors(): Promise<Fetched<Corridor[]>> {
  const payload = await getJSON<Envelope & { corridors: Corridor[] }>(
    '/api/corridors', CORRIDOR_TIMEOUT_MS);
  if (!payload?.corridors?.length) return wrap(null, INITIAL_CORRIDORS);
  return wrap(payload, payload.corridors);
}

function isPublishedFerrySchedule(value: unknown): value is FerrySchedule {
  if (!value || typeof value !== 'object') return false;
  const ferry = value as Partial<FerrySchedule>;
  return ferry.status === 'SCHEDULED'
    && ferry.data_source === 'official_timetable_snapshot'
    && ferry.live_status_available === false
    && ferry.available_seats === null
    && (ferry.capacity === null || ferry.capacity === undefined)
    && typeof ferry.operator === 'string'
    && ferry.ferry_name === ferry.operator
    && typeof ferry.departure_time === 'string'
    && typeof ferry.departure_port === 'string'
    && typeof ferry.arrival_port === 'string'
    && typeof ferry.schedule_source_url === 'string'
    && typeof ferry.schedule_last_verified_at === 'string';
}

function isFerryTimetableMetadata(value: unknown): value is FerryTimetableMetadata {
  if (!value || typeof value !== 'object') return false;
  const timetable = value as Partial<FerryTimetableMetadata>;
  return timetable.schema_version === 1
    && timetable.status === 'published_schedule_snapshot'
    && timetable.timezone === 'Asia/Jakarta'
    && typeof timetable.snapshot_id === 'string'
    && typeof timetable.last_verified_at === 'string'
    && typeof timetable.live_board_url === 'string'
    && typeof timetable.limitations === 'string'
    && Array.isArray(timetable.sources);
}

export async function fetchFerries(): Promise<FetchedFerrySchedule> {
  const payload = await getJSON<Envelope & {
    ferries: FerrySchedule[];
    timetable: FerryTimetableMetadata;
  }>(
    '/api/ferries', CORRIDOR_TIMEOUT_MS);
  if (payload?.data_source !== 'published_schedule'
    || !payload.ferries?.length
    || !payload.ferries.every(isPublishedFerrySchedule)
    || !isFerryTimetableMetadata(payload.timetable)) {
    return {
      ...wrap(null, offlineFerries()),
      timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
    };
  }
  return {
    ...wrap(payload, payload.ferries),
    timetable: payload.timetable,
  };
}

const FERRY_REFRESH_STATUSES = new Set<FerryRefreshReport['status']>([
  'checked', 'partial', 'failed_using_last_known_good', 'cached',
]);

const FERRY_REFRESH_SOURCE_STATUSES = new Set<FerryRefreshSourceResult['status']>([
  'verified_structure', 'unavailable_or_invalid', 'skipped_permission_required',
]);

function isFerryRefreshSourceResult(value: unknown): value is FerryRefreshSourceResult {
  if (!isRecord(value)) return false;
  return typeof value.source_id === 'string'
    && typeof value.authority === 'string'
    && typeof value.kind === 'string'
    && typeof value.url === 'string'
    && typeof value.permission_status === 'string'
    && FERRY_REFRESH_SOURCE_STATUSES.has(value.status as FerryRefreshSourceResult['status'])
    && typeof value.checked_at === 'string'
    && (value.http_status === null || isNonNegativeInteger(value.http_status))
    && typeof value.note === 'string';
}

function isFerryRefreshReport(value: unknown): value is FerryRefreshReport {
  if (!isRecord(value) || !isRecord(value.summary)) return false;
  return typeof value.refresh_id === 'string'
    && FERRY_REFRESH_STATUSES.has(value.status as FerryRefreshReport['status'])
    && typeof value.started_at === 'string'
    && typeof value.finished_at === 'string'
    && value.refresh_scope === 'fixed_official_allowlist'
    && Array.isArray(value.source_results)
    && value.source_results.length > 0
    && value.source_results.every(isFerryRefreshSourceResult)
    && isNonNegativeInteger(value.summary.verified)
    && isNonNegativeInteger(value.summary.failed)
    && isNonNegativeInteger(value.summary.permission_gated)
    && typeof value.schedule_applied === 'boolean'
    && typeof value.last_known_good_active === 'boolean'
    && typeof value.promotion_requirement === 'string'
    && typeof value.data_changed === 'boolean'
    && typeof value.limitations === 'string';
}

/**
 * Recheck the reviewed official-source allowlist and accept the backend's
 * ferry, timetable and terminal planning outputs as one atomic response.
 */
export async function refreshOfficialFerrySources(): Promise<FetchedFerryRefresh> {
  const payload = await getJSON<Envelope & {
    refresh: FerryRefreshReport;
    ferries: FerrySchedule[];
    ports: PortStatus[];
    timetable: FerryTimetableMetadata;
  }>('/api/ferry-refresh', FERRY_REFRESH_TIMEOUT_MS, {
    method: 'POST',
    headers: { Accept: 'application/json' },
  });

  if (payload?.data_source !== 'published_schedule'
    || !Array.isArray(payload.ferries)
    || payload.ferries.length === 0
    || !payload.ferries.every(isPublishedFerrySchedule)
    || !Array.isArray(payload.ports)
    || !isFerryTimetableMetadata(payload.timetable)
    || !isFerryRefreshReport(payload.refresh)) {
    throw new Error('Official-source refresh returned no valid coordinated planning update.');
  }

  const seed = scheduleInformedPortSeed();
  const mergedPorts = mergePortIntelligence(payload.ports, seed);
  const hasKnownTerminal = payload.ports.some(port => (
    isRecord(port)
    && typeof port.port_name === 'string'
    && BROWSER_PORT_CONFIG.some(known => known.port_name === port.port_name)
  ));
  if (!hasKnownTerminal) {
    throw new Error('Official-source refresh did not return a recognised terminal update.');
  }

  return {
    ...wrap(payload, payload.ferries),
    timetable: payload.timetable,
    ports: mergedPorts,
    refresh: payload.refresh,
  };
}

const BROWSER_PORT_CONFIG = [
  { port_name: 'Batam Centre', terminal_code: 'BCT', baseQueue: 20, baseProcessing: 15, totalBerths: 6, officialUrl: 'https://batamport.bpbatam.go.id/batam-centre/' },
  { port_name: 'HarbourBay', terminal_code: 'HBT', baseQueue: 12, baseProcessing: 10, totalBerths: 4, officialUrl: 'https://batamport.bpbatam.go.id/harbour-bay/' },
  { port_name: 'Sekupang', terminal_code: 'SKP', baseQueue: 15, baseProcessing: 12, totalBerths: 4, officialUrl: 'https://batamport.bpbatam.go.id/sekupang/' },
  { port_name: 'Nongsa Pura', terminal_code: 'NPT', baseQueue: 10, baseProcessing: 8, totalBerths: 2, officialUrl: 'https://batamport.bpbatam.go.id/nongsapura/' },
] as const;

export function scheduleInformedPortSeed(at: Date = new Date()): PortStatus[] {
  const publishedDepartures = offlineFerries(at, at, 24);
  const hour = batamParts(at).hour;
  const peakFactor = ((hour >= 7 && hour <= 9) || (hour >= 16 && hour <= 19)) ? 1.35 : 0.85;
  return BROWSER_PORT_CONFIG.map((terminal) => {
    const terminalDepartures = publishedDepartures.filter(
      ferry => ferry.departure_port === terminal.port_name,
    );
    const nextDeparture = terminalDepartures[0];
    const departuresWithinHour = terminalDepartures.filter(
      ferry => (ferry.minutes_until_departure ?? Number.POSITIVE_INFINITY) <= 60,
    ).length;
    const sailingFactor = 1 + Math.min(0.3, departuresWithinHour * 0.08);
    const estimateFactor = peakFactor * sailingFactor;
    return {
      port_name: terminal.port_name,
      terminal_code: terminal.terminal_code,
      passenger_queue_mins: Math.max(5, Math.round(terminal.baseQueue * estimateFactor)),
      customs_processing_mins: Math.max(4, Math.round(terminal.baseProcessing * estimateFactor)),
      freight_clearance_mins: Math.max(15, Math.round((terminal.baseProcessing + 15) * estimateFactor)),
      active_berths: Math.max(1, Math.min(terminal.totalBerths, departuresWithinHour || 1)),
      total_berths: terminal.totalBerths,
      status: estimateFactor >= 1.35 ? 'BUSY' : 'NORMAL',
      next_sailing_in_mins: nextDeparture?.minutes_until_departure ?? null,
      next_operator: nextDeparture?.operator ?? null,
      data_source: 'browser_schedule_informed_estimate' as const,
      observed: false as const,
      estimate_basis: 'Published departure density × Batam time-of-day planning profile',
      official_reference_url: terminal.officialUrl,
      limitations: (
        'Planning estimate, not a sensor observation. No public official live queue feed was available; '
        + 'verify conditions with the terminal or operator before travel.'
      ),
    };
  });
}

function mergePortIntelligence(
  payloadPorts: unknown[],
  seed: PortStatus[],
): PortStatus[] {
  const knownNames = new Set(BROWSER_PORT_CONFIG.map(port => port.port_name));
  const byName = new Map<string, Record<string, unknown>>();
  payloadPorts.forEach((candidate) => {
    if (!isRecord(candidate)
      || typeof candidate.port_name !== 'string'
      || !knownNames.has(candidate.port_name as typeof BROWSER_PORT_CONFIG[number]['port_name'])) {
      return;
    }
    byName.set(candidate.port_name, candidate);
  });

  const planningMinutes = (value: unknown, fallback: number | null): number | null => (
    isFiniteNonNegative(value) ? value : fallback
  );
  const nullableInteger = (value: unknown, fallback: number | null): number | null => (
    value === null ? null : isNonNegativeInteger(value) ? value : fallback
  );
  const optionalText = (value: unknown, fallback?: string): string | undefined => (
    typeof value === 'string' && value.trim() ? value : fallback
  );
  const validStatuses = new Set<PortStatus['status']>([
    'NORMAL', 'BUSY', 'CONGESTED', null,
  ]);

  return seed.map((fallback) => {
    const candidate = byName.get(fallback.port_name);
    if (!candidate) return fallback;
    const queueFromApi = isFiniteNonNegative(candidate.passenger_queue_mins);
    const processingFromApi = isFiniteNonNegative(candidate.customs_processing_mins);
    const usedBrowserEstimate = !queueFromApi || !processingFromApi;
    const status = validStatuses.has(candidate.status as PortStatus['status'])
      ? candidate.status as PortStatus['status']
      : fallback.status;
    const apiSource = candidate.data_source === 'schedule_informed_planning_estimate'
      || candidate.data_source === 'browser_schedule_informed_estimate'
      ? candidate.data_source
      : fallback.data_source;
    const apiLimitations = optionalText(candidate.limitations, fallback.limitations);
    return {
      port_name: fallback.port_name,
      terminal_code: typeof candidate.terminal_code === 'string'
        && candidate.terminal_code.trim()
        ? candidate.terminal_code : fallback.terminal_code,
      passenger_queue_mins: planningMinutes(
        candidate.passenger_queue_mins, fallback.passenger_queue_mins,
      ),
      customs_processing_mins: planningMinutes(
        candidate.customs_processing_mins, fallback.customs_processing_mins,
      ),
      freight_clearance_mins: planningMinutes(
        candidate.freight_clearance_mins, fallback.freight_clearance_mins,
      ),
      active_berths: nullableInteger(candidate.active_berths, fallback.active_berths),
      total_berths: nullableInteger(candidate.total_berths, fallback.total_berths),
      status,
      next_sailing_in_mins: nullableInteger(
        candidate.next_sailing_in_mins, fallback.next_sailing_in_mins ?? null,
      ),
      next_vessel: candidate.next_vessel === null
        ? null : optionalText(candidate.next_vessel, fallback.next_vessel ?? undefined),
      next_operator: candidate.next_operator === null
        ? null : optionalText(candidate.next_operator, fallback.next_operator ?? undefined),
      data_source: usedBrowserEstimate
        ? 'browser_schedule_informed_estimate' : apiSource,
      observed: false,
      estimate_basis: usedBrowserEstimate
        ? `${fallback.estimate_basis}; browser continuity supplied missing API queue/processing fields`
        : optionalText(candidate.estimate_basis, fallback.estimate_basis),
      official_reference_url: optionalText(
        candidate.official_reference_url, fallback.official_reference_url,
      ),
      limitations: usedBrowserEstimate
        ? `${apiLimitations ?? ''} Missing API queue/processing values were filled from the published-schedule planning profile.`.trim()
        : apiLimitations,
    };
  });
}

export async function fetchPortIntelligence(): Promise<Fetched<PortStatus[]>> {
  const seed = scheduleInformedPortSeed();
  const payload = await getJSON<Envelope & { ports: PortStatus[] }>(
    '/api/port-intelligence', CORRIDOR_TIMEOUT_MS);
  if (!Array.isArray(payload?.ports)) {
    return wrap(null, seed);
  }
  const merged = mergePortIntelligence(payload.ports, seed);
  const hasKnownTerminal = payload.ports.some(port => (
    isRecord(port)
    && typeof port.port_name === 'string'
    && BROWSER_PORT_CONFIG.some(known => known.port_name === port.port_name)
  ));
  return wrap(hasKnownTerminal ? payload : null, merged);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isFiniteNonNegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function isOperationsSummaryPayload(value: unknown): value is OperationsSummary {
  if (!isRecord(value)
    || typeof value.overall_network_status !== 'string'
    || !isFiniteNonNegative(value.average_congestion_index)
    || value.average_congestion_index > 100
    || !isNonNegativeInteger(value.active_bottlenecks)
    || !isFiniteNonNegative(value.total_co2_reduced_today_kg)
    || !isNonNegativeInteger(value.active_ferry_sailings)
    || !Array.isArray(value.alerts)
    || !value.alerts.every(alert => (
      isRecord(alert)
      && typeof alert.id === 'string'
      && ['WARNING', 'INFO', 'CRITICAL'].includes(String(alert.severity))
      && typeof alert.title === 'string'
      && typeof alert.message === 'string'
      && typeof alert.timestamp === 'string'
    ))) return false;

  const methodology = value.operations_methodology;
  if (!isRecord(methodology)
    || methodology.observed !== false
    || typeof methodology.source !== 'string'
    || !isRecord(methodology.scopes)
    || typeof methodology.scopes.network !== 'string'
    || typeof methodology.scopes.emissions !== 'string'
    || typeof methodology.scopes.ferries !== 'string') return false;

  const optionalNumbers = [
    value.modeled_avoidable_emissions_opportunity_kg_today,
    value.projected_full_day_co2_kg,
    value.modeled_projected_full_day_avoidable_emissions_kg,
    value.live_co2_rate_kg_hr,
  ];
  if (optionalNumbers.some(item => item !== undefined && !isFiniteNonNegative(item))) return false;

  for (const candidate of [value.co2_by_corridor_kg, value.co2_by_vehicle_type]) {
    if (candidate !== undefined && (
      !isRecord(candidate)
      || Object.values(candidate).some(item => !isFiniteNonNegative(item))
    )) return false;
  }
  return value.hourly_co2_distribution === undefined
    || (Array.isArray(value.hourly_co2_distribution)
      && value.hourly_co2_distribution.every(item => (
        isRecord(item)
        && typeof item.hour === 'string'
        && isFiniteNonNegative(item.baseline_co2)
        && isFiniteNonNegative(item.optimized_co2)
      )));
}


export async function fetchOperationsSummary(): Promise<Fetched<OperationsSummary>> {
  const payload = await getJSON<OperationsSummary>('/api/operations', CORRIDOR_TIMEOUT_MS);
  if (!isOperationsSummaryPayload(payload)) return wrap(null, MOCK_OPERATIONS);
  return wrap(payload, payload);
}

export async function fetchCorridorRoutes(): Promise<Fetched<CorridorRoute[]>> {
  const payload = await getJSON<Envelope & { routes: CorridorRoute[] }>(
    '/api/corridor-routes', ROUTE_TIMEOUT_MS);
  if (!payload?.routes?.length) return wrap(null, []);
  return wrap(payload, payload.routes);
}

export async function fetchRouteLocations(): Promise<Fetched<RouteLocation[]>> {
  const payload = await getJSON<Envelope & { locations: RouteLocation[] }>(
    '/api/route-locations', CORRIDOR_TIMEOUT_MS);
  if (!payload?.locations?.length) return wrap(null, ROUTE_LOCATIONS);
  return wrap(payload, payload.locations);
}

export function googleRouteBenchmarkEnabled(): boolean {
  return import.meta.env.VITE_ENABLE_GOOGLE_BENCHMARK === 'true';
}

function isRouteBenchmarkResult(value: unknown): value is RouteBenchmarkResult {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<RouteBenchmarkResult>;
  return candidate.data_source === 'google_routes_v2_text_benchmark'
    && candidate.attribution === 'Google Maps'
    && typeof candidate.policy_url === 'string'
    && candidate.policy_url.startsWith('https://')
    && candidate.cacheable === false
    && candidate.persisted === false
    && candidate.training_eligible === false
    && candidate.map_overlay_allowed === false
    && Array.isArray(candidate.routes)
    && candidate.routes.length > 0
    && candidate.routes.every((route) => (
      typeof route?.id === 'string'
      && Number.isFinite(route.duration_mins)
      && Number.isFinite(route.distance_km)
      && typeof route.summary === 'string'
      && Array.isArray(route.route_labels)
    ));
}

export async function requestRouteBenchmark(
  origin: Pick<RouteEndpointSnapshot, 'lat' | 'lng'>,
  destination: Pick<RouteEndpointSnapshot, 'lat' | 'lng'>,
  routePreference: RoutePreference,
): Promise<RouteBenchmarkResult> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/route-benchmark`, {
      method: 'POST',
      cache: 'no-store',
      signal: AbortSignal.timeout(ROUTE_TIMEOUT_MS),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        origin_lat: origin.lat,
        origin_lng: origin.lng,
        destination_lat: destination.lat,
        destination_lng: destination.lng,
        route_preference: routePreference,
      }),
    });
  } catch {
    throw new ApiRequestError('Route benchmark is temporarily unavailable.', 503);
  }

  if (!response.ok) {
    let detail = response.status === 404
      ? 'Route benchmark is disabled.'
      : response.status === 503
        ? 'Route benchmark is temporarily unavailable.'
        : `Route benchmark request failed (${response.status}).`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail;
    } catch {
      // Keep the status-specific, provider-neutral message for non-JSON errors.
    }
    throw new ApiRequestError(detail, response.status);
  }

  const payload = await response.json() as unknown;
  if (!isRouteBenchmarkResult(payload)) {
    throw new ApiRequestError('Route benchmark returned an invalid metric-only response.', 503);
  }
  return payload;
}

export async function requestRouteOptimization(
  originId: string,
  destinationId: string,
  vehicleType: VehicleType,
  hour: number = 14,
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
  schedule?: RouteScheduleOptions,
): Promise<Fetched<RouteOptimizationResult>> {
  const origin = ROUTE_LOCATIONS.find(location => location.id === originId) ?? ROUTE_LOCATIONS[0];
  const destination = ROUTE_LOCATIONS.find(location => location.id === destinationId)
    ?? ROUTE_LOCATIONS[1];
  const seededCorridor = INITIAL_CORRIDORS.find(corridor => (
    (corridor.origin === originId && corridor.destination === destinationId)
      || (corridor.origin === destinationId && corridor.destination === originId)
  ));
  const corridorIdx = seededCorridor
    ? (CORRIDOR_INDEX[seededCorridor.id] ?? 0)
    : Math.abs(Math.round((origin.lat * 1000) + (origin.lng * 100))) % 5;
  const effectiveHour = hourFromSchedule(schedule) ?? hour;
  const roadHedge = bundledRoadHedge(
    [origin.lat, origin.lng],
    [destination.lat, destination.lng],
    destination.name,
    browserRoutingContext(vehicleType, effectiveHour, weather, corridorIdx, routePreference),
  );
  const backendAbort = new AbortController();
  const sources = await raceRoadRouteSources(
    getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        signal: backendAbort.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin_id: originId, destination_id: destinationId,
          vehicle_type: vehicleType, hour: effectiveHour, weather,
          route_preference: routePreference, ...scheduleRequestFields(schedule),
        }),
      },
      true,
    ).then(validatedRoadPayload),
    roadHedge,
    () => backendAbort.abort(),
  );

  if (sources.payload) {
    return wrap(sources.payload, sources.payload);
  }
  requireServerSchedule(schedule);
  const fallback = offlineOptimize(originId, destinationId, vehicleType, effectiveHour, weather);
  if (!sources.roadPlan) throw roadRoutingUnavailable();
  return wrap(null, applyBundledRoadPlan(
    fallback,
    sources.roadPlan,
    vehicleType,
    effectiveHour,
    weather,
    corridorIdx,
    routePreference,
  ));
}

/** Retrieve the immutable route assigned to a driver by its seven-character code. */
export async function requestPersistedRoute(
  routeCode: string,
  accessToken: string | null,
): Promise<Fetched<RouteOptimizationResult>> {
  const normalizedCode = routeCode.trim().toUpperCase();
  if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(normalizedCode)) {
    throw new ApiRequestError('Enter the seven-character route code supplied by dispatch.', 400);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/routes/${normalizedCode}`, {
      signal: AbortSignal.timeout(ROUTE_TIMEOUT_MS),
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : undefined,
    });
  } catch {
    throw new ApiRequestError('The assigned route could not be reached. Try again.', 503);
  }
  if (!response.ok) {
    let detail = response.status === 404
      ? 'No assigned route matches that code.'
      : 'Your account is not authorized to retrieve this assigned route.';
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail;
    } catch {
      // Keep the status-specific message for non-JSON responses.
    }
    throw new ApiRequestError(detail, response.status);
  }
  const payload = validatedRoadPayload(await response.json() as RouteOptimizationResult);
  if (!payload) throw new ApiRequestError('The assigned route response is incomplete.', 503);
  return wrap(payload as RouteOptimizationResult & Envelope, payload);
}
const FERRY_BOARDING_CUTOFF_MINS = 15;

function browserRoutingContext(
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  routePreference: RoutePreference,
): OfflineRoutingContext {
  const departure = nextBatamHour(hour);
  const weekday = batamParts(departure).weekday;
  const weekend = weekday === 0 || weekday === 6 ? 1 : 0;
  const current = predictLocal(hour, weekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, weekend, weather, 0, corridorIdx);
  return {
    vehicleType,
    hour,
    weather,
    routePreference,
    networkCongestionScore: current.current_score,
    networkCongestionScoreAfter30: later.current_score,
  };
}

type ComputedRoadMetrics = Omit<Pick<
  RouteOptimizationResult,
  | 'planned_departure'
  | 'congestion_prediction'
  | 'estimated_travel_time_mins'
  | 'customs_buffer_mins'
  | 'total_eta_mins'
  | 'co2_emissions_kg'
  | 'co2_saved_kg'
  | 'optimal_departure'
>, 'planned_departure'> & { baseTimeMins: number; planned_departure: string };

async function bundledRoadPlan(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
  context: OfflineRoutingContext,
): Promise<OfflineRoadPlan | null> {
  try {
    const plan = await planWithBundledRoadGraph(origin, destination, destinationName, context);
    return plan.routes.length ? plan : null;
  } catch (error) {
    console.warn('[crossflow] bundled browser road routing unavailable:', error);
    return null;
  }
}

interface BundledRoadHedge {
  cancel: () => void;
  delayed: Promise<OfflineRoadPlan | null>;
  result: () => Promise<OfflineRoadPlan | null>;
}

/**
 * Start the heavier browser graph only when the backend has not answered
 * quickly. A successful warm API request therefore avoids the graph download,
 * while a slow transport failure overlaps its timeout with local A* work.
 */
function bundledRoadHedge(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
  context: OfflineRoutingContext,
): BundledRoadHedge {
  let timer: ReturnType<typeof globalThis.setTimeout> | null = null;
  let planPromise: Promise<OfflineRoadPlan | null> | null = null;
  let delayedSettled = false;
  let settleDelayed: (plan: OfflineRoadPlan | null) => void = () => undefined;
  const start = () => {
    if (!planPromise) {
      planPromise = bundledRoadPlan(origin, destination, destinationName, context);
    }
    return planPromise;
  };
  const settleOnce = (plan: OfflineRoadPlan | null) => {
    if (delayedSettled) return;
    delayedSettled = true;
    settleDelayed(plan);
  };
  const delayed = new Promise<OfflineRoadPlan | null>((resolve) => {
    settleDelayed = resolve;
    timer = globalThis.setTimeout(() => {
      timer = null;
      // bundledRoadPlan handles worker/asset errors and always settles to null.
      void start().then(settleOnce);
    }, 1_200);
  });

  return {
    cancel: () => {
      if (timer !== null) globalThis.clearTimeout(timer);
      timer = null;
      settleOnce(null);
    },
    delayed,
    result: () => {
      if (timer !== null) globalThis.clearTimeout(timer);
      timer = null;
      const result = start();
      void result.then(settleOnce);
      return result;
    },
  };
}

type BackendRouteOutcome<T> =
  | { kind: 'backend'; payload: T | null }
  | { kind: 'backend-error'; error: unknown };

type LocalRouteOutcome = { kind: 'local'; roadPlan: OfflineRoadPlan | null };

/**
 * Return whichever valid road engine finishes first after the short warm-API
 * grace period. Rejections become values before the race, so a late backend
 * validation error or timeout cannot become an unhandled rejection.
 */
async function raceRoadRouteSources<T>(
  backendRequest: Promise<T | null>,
  roadHedge: BundledRoadHedge,
  cancelBackend: () => void,
): Promise<{ payload: T | null; roadPlan: OfflineRoadPlan | null }> {
  const backendOutcome: Promise<BackendRouteOutcome<T>> = backendRequest.then(
    (payload) => ({ kind: 'backend', payload }),
    (error: unknown) => ({ kind: 'backend-error', error }),
  );
  const localOutcome: Promise<LocalRouteOutcome> = roadHedge.delayed.then(
    (roadPlan) => ({ kind: 'local', roadPlan }),
  );
  const first = await Promise.race([backendOutcome, localOutcome]);

  if (first.kind === 'backend-error') {
    roadHedge.cancel();
    throw first.error;
  }
  if (first.kind === 'backend') {
    if (first.payload) {
      roadHedge.cancel();
      return { payload: first.payload, roadPlan: null };
    }
    return { payload: null, roadPlan: await roadHedge.result() };
  }
  if (first.roadPlan) {
    cancelBackend();
    return { payload: null, roadPlan: first.roadPlan };
  }

  const backend = await backendOutcome;
  if (backend.kind === 'backend-error') throw backend.error;
  return { payload: backend.payload, roadPlan: null };
}

function roadRoutingUnavailable(): ApiRequestError {
  return new ApiRequestError(
    'A drivable road route could not be calculated. Check the selected Batam road points and try again.',
    503,
  );
}

function routeMetrics(
  distanceKm: number,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  navigation?: NavigationData,
  accessTimeMins = 0,
  terminalBufferMins = 0,
  routedTiming?: {
    free_flow_time_mins?: number;
    estimated_travel_time_mins?: number;
    estimated_travel_time_after_30_mins?: number;
    congestion_delay_after_30_mins?: number;
    routing_cost_breakdown?: { congestion_delay_mins?: number };
  },
): ComputedRoadMetrics {
  const maneuverDelayMins = (navigation?.maneuvers ?? []).reduce((delay, maneuver) => {
    if (maneuver.type === 'turn') return delay + 0.2;
    if (maneuver.type === 'roundabout') return delay + 0.35;
    if (maneuver.type !== 'depart' && maneuver.type !== 'arrive') return delay + 0.05;
    return delay;
  }, 0);
  const fallbackBaseTimeMins = Math.max(
    1,
    Math.round(((distanceKm / 35 * 60) + maneuverDelayMins) * 10) / 10,
  );
  const baseTimeMins = routedTiming?.free_flow_time_mins ?? fallbackBaseTimeMins;
  const profile = vehicleProfile(vehicleType);
  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;
  const now = predictLocal(hour, isWeekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, corridorIdx);
  const fallbackTimeFactor = 1 / profile.speedFactor;
  const travelNow = routedTiming?.estimated_travel_time_mins ?? Math.round(
    (baseTimeMins + now.estimated_delay_mins) * fallbackTimeFactor * 10,
  ) / 10;
  const travelLater = routedTiming?.estimated_travel_time_after_30_mins ?? Math.round(
    (baseTimeMins + later.estimated_delay_mins) * fallbackTimeFactor * 10,
  ) / 10;
  const defer = travelLater <= travelNow - 0.5 && later.current_score < now.current_score;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const forecastPrediction = defer ? later : now;
  const estimatedTravel = defer ? travelLater : travelNow;
  const currentCongestionDelay = routedTiming?.routing_cost_breakdown?.congestion_delay_mins
    ?? now.estimated_delay_mins;
  const laterCongestionDelay = routedTiming?.congestion_delay_after_30_mins
    ?? later.estimated_delay_mins;
  const modeledDelay = Math.max(
    0,
    Math.round((defer ? laterCongestionDelay : currentCongestionDelay) * 10) / 10,
  );
  const avoidedQueueMins = defer
    ? Math.max(0, currentCongestionDelay - laterCongestionDelay)
    : 0;

  return {
    baseTimeMins,
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: forecastPrediction.current_score,
      predicted_30min: forecastPrediction.predicted_30min,
      predicted_60min: forecastPrediction.predicted_60min,
      estimated_delay_mins: modeledDelay,
      status: forecastPrediction.status,
      risk_level: forecastPrediction.risk_level,
      trend: forecastPrediction.trend,
    },
    estimated_travel_time_mins: estimatedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: Math.round(
      (estimatedTravel + terminalBufferMins + accessTimeMins) * 10,
    ) / 10,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm + (modeledDelay / 60) * profile.idleCo2KgPerHour)
        .toFixed(2),
    ),
    co2_saved_kg: Number(
      ((avoidedQueueMins / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Current road traffic is within optimal flow parameters.',
    },
  };
}

function destinationFerryPort(
  destination: { lat: number; lng: number },
): string | null {
  const nearestTerminal = ROUTE_LOCATIONS
    .filter(location => location.ferry_port)
    .map(location => ({
      location,
      distanceKm: haversineKm(destination.lat, destination.lng, location.lat, location.lng),
    }))
    .sort((a, b) => a.distanceKm - b.distanceKm)[0];
  return nearestTerminal && nearestTerminal.distanceKm < FERRY_TERMINAL_MATCH_KM
    ? nearestTerminal.location.ferry_port ?? null
    : null;
}

function offlineFerryConnection(
  destination: { lat: number; lng: number },
  plannedDeparture: string,
  totalEtaMins: number,
): { ferries: FerrySchedule[]; note?: string } {
  const destinationPort = destinationFerryPort(destination);
  const departure = new Date(plannedDeparture);
  const arrival = new Date(departure.getTime() + totalEtaMins * 60_000);
  const earliestBoardable = new Date(arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000);
  const ferries = offlineFerries(earliestBoardable, new Date())
    .filter(ferry => ferry.departure_port === (destinationPort ?? 'Batam Centre'))
    .slice(0, 3);
  return {
    ferries,
    note: destinationPort
      ? undefined
      : 'Destination is not a ferry port; showing Batam Centre departures for reference.',
  };
}

function breakdownForRecommendedDeparture(
  route: OfflineRoadPlan['routes'][number],
  metrics: ComputedRoadMetrics,
): RoutingCostBreakdown {
  if (metrics.optimal_departure.recommended !== 'DEFER_30_MINS') {
    return route.routing_cost_breakdown;
  }
  const congestionDelay = route.congestion_delay_after_30_mins;
  const modeledTravel = route.estimated_travel_time_after_30_mins;
  const suitability = route.routing_cost_breakdown.road_suitability_penalty_mins;
  return {
    ...route.routing_cost_breakdown,
    congestion_delay_mins: congestionDelay,
    modeled_travel_time_mins: modeledTravel,
    generalized_cost_mins: Math.round((modeledTravel + suitability) * 100) / 100,
  };
}

function applyBundledRoadPlan(
  fallback: RouteOptimizationResult,
  plan: OfflineRoadPlan,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  routePreference: RoutePreference,
): RouteOptimizationResult {
  const primary = plan.routes[0];
  const totalAccessDistanceKm = (plan.origin_snap_m + plan.destination_snap_m) / 1000;
  const totalAccessTimeMins = totalAccessDistanceKm / 5 * 60;
  const roundedAccessDistanceKm = Math.round(totalAccessDistanceKm * 1000) / 1000;
  const roundedAccessTimeMins = Math.round(totalAccessTimeMins * 10) / 10;
  const snapInfo = {
    origin_snap_m: Math.round(plan.origin_snap_m * 10) / 10,
    destination_snap_m: Math.round(plan.destination_snap_m * 10) / 10,
    origin_access_time_mins: Math.round((plan.origin_snap_m / 1000 / 5 * 60) * 10) / 10,
    destination_access_time_mins: Math.round(
      (plan.destination_snap_m / 1000 / 5 * 60) * 10,
    ) / 10,
    total_access_distance_km: roundedAccessDistanceKm,
    total_access_time_mins: roundedAccessTimeMins,
    assumed_access_speed_kph: 5,
    included_in_road_distance: false,
  };
  const destination = fallback.requested_destination ?? {
    lat: primary.geometry[primary.geometry.length - 1][0],
    lng: primary.geometry[primary.geometry.length - 1][1],
  };
  const terminalBufferMins = destinationFerryPort(destination)
    ? vehicleProfile(vehicleType).terminalBufferMins
    : 0;
  const metrics = routeMetrics(
    primary.distance_km,
    vehicleType,
    hour,
    weather,
    corridorIdx,
    primary.navigation,
    totalAccessTimeMins,
    terminalBufferMins,
    primary,
  );
  const primaryFerries = offlineFerryConnection(
    destination,
    metrics.planned_departure,
    metrics.total_eta_mins,
  );
  const routingModelFor = (
    routeMetricsResult: ComputedRoadMetrics,
    route: OfflineRoadPlan['routes'][number],
  ) => ({
    ...route.routing_model,
    network_congestion_score: routeMetricsResult.congestion_prediction.current_score,
    weather,
  });
  const routingModel = routingModelFor(metrics, primary);
  const primaryBreakdown = breakdownForRecommendedDeparture(primary, metrics);
  const alternatives = plan.routes.slice(1).map((route) => {
    const optionMetrics = routeMetrics(
      route.distance_km,
      vehicleType,
      hour,
      weather,
      corridorIdx,
      route.navigation,
      totalAccessTimeMins,
      terminalBufferMins,
      route,
    );
    const optionFerries = offlineFerryConnection(
      destination,
      optionMetrics.planned_departure,
      optionMetrics.total_eta_mins,
    );
    const optionBreakdown = breakdownForRecommendedDeparture(route, optionMetrics);
    return {
      id: route.id,
      route_type: 'ROAD_ALTERNATIVE' as const,
      name: route.name,
      description: route.description,
      distance_km: route.distance_km,
      estimated_travel_time_mins: optionMetrics.estimated_travel_time_mins,
      total_eta_mins: optionMetrics.total_eta_mins,
      co2_emissions_kg: optionMetrics.co2_emissions_kg,
      co2_saved_kg: optionMetrics.co2_saved_kg,
      route_geometry: route.geometry,
      route_data_source: 'bundled_client_openstreetmap',
      overlap_ratio: route.overlap_ratio,
      congestion_cost: optionBreakdown.generalized_cost_mins,
      objective_cost_s: route.objective_cost_s,
      route_preference: route.route_preference,
      route_preference_profile: route.route_preference_profile,
      free_flow_time_mins: route.free_flow_time_mins,
      estimated_travel_time_after_30_mins: route.estimated_travel_time_after_30_mins,
      congestion_delay_after_30_mins: route.congestion_delay_after_30_mins,
      routing_cost_breakdown: optionBreakdown,
      routing_model: routingModelFor(optionMetrics, route),
      local_road_distance_km: route.local_road_distance_km,
      local_road_segments: route.local_road_segments,
      local_road_audit: route.local_road_audit,
      navigation: route.navigation,
      next_matching_ferries: optionFerries.ferries,
      access_distance_km: roundedAccessDistanceKm,
      access_time_mins: roundedAccessTimeMins,
      snap_info: snapInfo,
    };
  });

  return {
    ...fallback,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    route_preference: primary.route_preference ?? routePreference,
    route_preference_profile: primary.route_preference_profile,
    objective_cost_s: primary.objective_cost_s,
    generalized_cost_mins: primaryBreakdown.generalized_cost_mins,
    routing_cost_breakdown: primaryBreakdown,
    routing_model: routingModel,
    local_road_distance_km: primary.local_road_distance_km,
    local_road_segments: primary.local_road_segments,
    local_road_audit: primary.local_road_audit,
    corridor: {
      ...fallback.corridor,
      distance_km: primary.distance_km,
      base_time_mins: metrics.baseTimeMins,
      detour_ratio: fallback.corridor.straight_line_km
        ? Math.round((primary.distance_km / fallback.corridor.straight_line_km) * 100) / 100
        : null,
    },
    planned_departure: metrics.planned_departure,
    congestion_prediction: metrics.congestion_prediction,
    estimated_travel_time_mins: metrics.estimated_travel_time_mins,
    customs_buffer_mins: metrics.customs_buffer_mins,
    total_eta_mins: metrics.total_eta_mins,
    access_distance_km: roundedAccessDistanceKm,
    access_time_mins: roundedAccessTimeMins,
    co2_emissions_kg: metrics.co2_emissions_kg,
    co2_saved_kg: metrics.co2_saved_kg,
    optimal_departure: metrics.optimal_departure,
    next_matching_ferries: primaryFerries.ferries,
    ferry_connection_note: primaryFerries.note,
    route_geometry: primary.geometry,
    route_data_source: 'bundled_client_openstreetmap',
    snap_info: snapInfo,
    navigation: primary.navigation,
    alternative_routes: alternatives,
  };
}

/**
 * Offline route computation.
 *
 * Mirrors backend/services/route_solver.py::optimize_route, driven by the same
 * hour and weather the user selected.
 */
function offlineOptimize(
  originId: string,
  destinationId: string,
  vehicleType: VehicleType,
  hour: number,
  weather: number
): RouteOptimizationResult {
  const origin = ROUTE_LOCATIONS.find(location => location.id === originId) ?? ROUTE_LOCATIONS[0];
  const destination = ROUTE_LOCATIONS.find(location => location.id === destinationId)
    ?? ROUTE_LOCATIONS[1];
  const directSeeded = INITIAL_CORRIDORS.find(
    corridor => corridor.origin === origin.id && corridor.destination === destination.id,
  );
  const reverseSeeded = INITIAL_CORRIDORS.find(
    corridor => corridor.origin === destination.id && corridor.destination === origin.id,
  );
  const seeded = directSeeded ?? reverseSeeded;
  const key1 = `${origin.id}:${destination.id}`;
  const key2 = `${destination.id}:${origin.id}`;
  const bundledRoadGeom = CORRIDOR_STREET_GEOMETRIES[key1]
    ?? (CORRIDOR_STREET_GEOMETRIES[key2] ? [...CORRIDOR_STREET_GEOMETRIES[key2]].reverse() : null);
  const straightLineKm = haversineKm(origin.lat, origin.lng, destination.lat, destination.lng);
  const distanceKm = seeded?.distance_km ?? Math.round(straightLineKm * 100) / 100;
  const baseTimeMins = seeded?.base_time_mins ?? Math.max(
    5, Math.round(distanceKm / 35 * 60 * 10) / 10,
  );
  const corridorId = seeded?.id ?? `route:${origin.id}:${destination.id}`;
  const corridorName = directSeeded?.name ?? `${origin.name} -> ${destination.name}`;
  const idx = seeded
    ? (CORRIDOR_INDEX[seeded.id] ?? 0)
    : ROUTE_LOCATIONS.findIndex(location => location.id === origin.id) % 5;
  const profile = vehicleProfile(vehicleType);

  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;

  const now = predictLocal(hour, isWeekend, weather, 0, idx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, idx);

  const travelNow = Math.round(
    (baseTimeMins + now.estimated_delay_mins) / profile.speedFactor * 10) / 10;
  const travelLater = Math.round(
    (baseTimeMins + later.estimated_delay_mins) / profile.speedFactor * 10) / 10;

  const defer = later.current_score < now.current_score - 10;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const recommendedPrediction = defer ? later : now;
  const recommendedTravel = defer ? travelLater : travelNow;
  const terminalBufferMins = destination.ferry_port ? profile.terminalBufferMins : 0;
  const totalEta = Math.round(
    (recommendedTravel + terminalBufferMins) * 10,
  ) / 10;

  const arrival = new Date(recommendedDeparture.getTime() + totalEta * 60_000);
  const earliestBoardable = new Date(
    arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000,
  );
  const ferries = offlineFerries(earliestBoardable, new Date());
  const matchingFerries = destination.ferry_port
    ? ferries.filter(ferry => ferry.departure_port === destination.ferry_port).slice(0, 3)
    : ferries.filter(ferry => ferry.departure_port === 'Batam Centre').slice(0, 3);

  const roadGeom: [number, number][] = bundledRoadGeom
    ?? [[origin.lat, origin.lng], [destination.lat, destination.lng]];

  return {
    route_type: 'ROAD_ROUTE',
    corridor: {
      id: corridorId,
      name: corridorName,
      origin: origin.id,
      destination: destination.id,
      distance_km: distanceKm,
      base_time_mins: baseTimeMins,
      straight_line_km: Math.round(straightLineKm * 100) / 100,
      detour_ratio: straightLineKm > 0
        ? Math.round((distanceKm / straightLineKm) * 100) / 100
        : null,
    },
    vehicle_type: vehicleType,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: recommendedPrediction.current_score,
      predicted_30min: recommendedPrediction.predicted_30min,
      predicted_60min: recommendedPrediction.predicted_60min,
      estimated_delay_mins: recommendedPrediction.estimated_delay_mins,
      status: recommendedPrediction.status,
      risk_level: recommendedPrediction.risk_level,
      trend: recommendedPrediction.trend,
    },
    estimated_travel_time_mins: recommendedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: totalEta,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm
        + (recommendedPrediction.estimated_delay_mins / 60) * profile.idleCo2KgPerHour)
        .toFixed(2)),
    co2_saved_kg: Number(
      (((defer ? savedMins : 0) / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Current corridor traffic is within optimal flow parameters.',
    },
    next_matching_ferries: matchingFerries,
    ferry_connection_note: destination.ferry_port
      ? undefined
      : 'This route does not terminate at a ferry port; showing Batam Centre departures for reference.',
    route_geometry: roadGeom,
    route_data_source: bundledRoadGeom ? 'bundled_openstreetmap' : 'offline_straight_line',
    requested_origin: {
      lat: origin.lat,
      lng: origin.lng,
      display_name: origin.name,
    },
    requested_destination: {
      lat: destination.lat,
      lng: destination.lng,
      display_name: destination.name,
    },
  };
}



function haversineKm(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const earthRadiusKm = 6371.0088;
  const toRadians = (degrees: number) => degrees * Math.PI / 180;
  const phi1 = toRadians(lat1);
  const phi2 = toRadians(lat2);
  const deltaPhi = phi2 - phi1;
  const deltaLambda = toRadians(lng2 - lng1);
  const a = Math.sin(deltaPhi / 2) ** 2
    + Math.cos(phi1) * Math.cos(phi2) * Math.sin(deltaLambda / 2) ** 2;
  return 2 * earthRadiusKm * Math.asin(Math.sqrt(a));
}

// ---------------------------------------------------------------------------
// Free-form geocode + Batam road-network routing
// ---------------------------------------------------------------------------

export async function geocodeQuery(
  query: string,
  limit = 5,
): Promise<GeocodedLocation[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const payload = await getJSON<{ results: GeocodedLocation[] }>(
    `/api/geocode?${params}`, ROUTE_TIMEOUT_MS,
  );
  return payload?.results ?? [];
}

export async function reverseGeocode(lat: number, lng: number): Promise<FreeLocation> {
  const params = new URLSearchParams({ lat: String(lat), lng: String(lng) });
  const payload = await getJSON<{ result: FreeLocation }>(
    `/api/reverse-geocode?${params}`, CORRIDOR_TIMEOUT_MS,
  );
  return payload?.result ?? {
    lat, lng,
    display_name: `${lat.toFixed(5)}, ${lng.toFixed(5)}`,
    supported_region: locationRegion(lat, lng),
  };
}

export async function requestFreeRouteOptimization(
  origin: FreeLocation,
  destination: FreeLocation,
  vehicleType: VehicleType,
  hour: number = 14,
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
  schedule?: RouteScheduleOptions,
): Promise<Fetched<RouteOptimizationResult>> {
  const originRegion = locationRegion(origin.lat, origin.lng);
  const destinationRegion = locationRegion(destination.lat, destination.lng);
  if (!originRegion || !destinationRegion) {
    throw new ApiRequestError('Both route points must be within Singapore or Batam.', 400);
  }
  if (
    originRegion !== destinationRegion
    && isPassengerFerryIncompatibleVehicle(vehicleType)
  ) {
    throw new ApiRequestError(PASSENGER_FERRY_TRUCK_MESSAGE, 400);
  }
  const corridorIdx = Math.abs(Math.round((origin.lat * 1000 + origin.lng * 100))) % 5;
  const effectiveHour = hourFromSchedule(schedule) ?? hour;
  const requestBody = JSON.stringify({
    origin_lat: origin.lat,
    origin_lng: origin.lng,
    destination_lat: destination.lat,
    destination_lng: destination.lng,
    origin_name: origin.display_name,
    destination_name: destination.display_name,
    vehicle_type: vehicleType,
    hour: effectiveHour,
    weather,
    route_preference: routePreference,
    ...scheduleRequestFields(schedule),
  });

  // The bundled graph covers Batam only. Singapore local and cross-border
  // requests go to the multimodal backend directly, then fall back to a
  // deterministic, per-leg-labelled continuity plan instead of failing.
  if (originRegion === 'SINGAPORE' || destinationRegion === 'SINGAPORE') {
    const payload = await getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-free-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload);
    if (payload) return wrap(payload, payload);
    requireServerSchedule(schedule);
    return wrap(null, offlineCrossBorderOptimize(
      origin, destination, vehicleType, effectiveHour, routePreference,
    ));
  }

  const roadHedge = bundledRoadHedge(
    [origin.lat, origin.lng],
    [destination.lat, destination.lng],
    destination.display_name,
    browserRoutingContext(vehicleType, effectiveHour, weather, corridorIdx, routePreference),
  );
  const backendAbort = new AbortController();
  const sources = await raceRoadRouteSources(
    getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-free-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        signal: backendAbort.signal,
        headers: { 'Content-Type': 'application/json' },
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload),
    roadHedge,
    () => backendAbort.abort(),
  );
  if (sources.payload) {
    return wrap(sources.payload, sources.payload);
  }
  requireServerSchedule(schedule);
  const fallback = offlineOptimizeFree(origin, destination, vehicleType, effectiveHour, weather);
  if (!sources.roadPlan) {
    return wrap(null, offlineCrossBorderOptimize(
      origin, destination, vehicleType, effectiveHour, routePreference,
    ));
  }
  return wrap(null, applyBundledRoadPlan(
    fallback,
    sources.roadPlan,
    vehicleType,
    effectiveHour,
    weather,
    corridorIdx,
    routePreference,
  ));
}

export async function requestMultiStopRouteOptimization(
  stops: Array<FreeLocation & { dwell_mins?: number }>,
  vehicleType: VehicleType,
  schedule: { departureAt?: string; arriveBy?: string },
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
): Promise<Fetched<RouteOptimizationResult>> {
  const payload = await getJSON<Envelope & RouteOptimizationResult>(
    '/api/optimize-multi-stop-route', ROUTE_TIMEOUT_MS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        stops: stops.map((stop) => ({
          lat: stop.lat, lng: stop.lng, name: stop.display_name,
          dwell_mins: stop.dwell_mins ?? 0,
        })),
        vehicle_type: vehicleType,
        ...(schedule.departureAt ? { departure_at: schedule.departureAt } : {}),
        ...(schedule.arriveBy ? { arrive_by: schedule.arriveBy } : {}),
        weather,
        route_preference: routePreference, optimize_order: false,
      }),
    }, true,
  ).then(validatedRoadPayload);
  if (!payload) throw new ApiRequestError('The backend returned an invalid multi-stop route.', 503);
  if (typeof payload.route_code !== 'string' || !/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(payload.route_code)) {
    throw new ApiRequestError('The backend returned no valid seven-character route code.', 503);
  }
  return wrap(payload, payload);
}

/**
 * Metric scaffold for the bundled free-form road planner. This straight-line
 * geometry is never returned to the UI: if both road engines fail, the caller
 * switches to the explicitly limited access-leg continuity route.
 */
function offlineOptimizeFree(
  origin: FreeLocation,
  destination: FreeLocation,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
): RouteOptimizationResult {
  const distanceKm = Math.round(
    haversineKm(origin.lat, origin.lng, destination.lat, destination.lng) * 100,
  ) / 100;
  const baseTimeMins = Math.max(1, Math.round(distanceKm / 35 * 60 * 10) / 10);
  const profile = vehicleProfile(vehicleType);

  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;
  const corridorIdx = Math.abs(Math.round((origin.lat * 1000 + origin.lng * 100))) % 5;
  const now = predictLocal(hour, isWeekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, corridorIdx);
  const travelNow = Math.round(
    (baseTimeMins + now.estimated_delay_mins) / profile.speedFactor * 10,
  ) / 10;
  const travelLater = Math.round(
    (baseTimeMins + later.estimated_delay_mins) / profile.speedFactor * 10,
  ) / 10;
  const defer = later.current_score < now.current_score - 10;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const recommendedPrediction = defer ? later : now;
  const recommendedTravel = defer ? travelLater : travelNow;
  const destinationPort = destinationFerryPort(destination);
  const terminalBufferMins = destinationPort ? profile.terminalBufferMins : 0;
  const totalEta = Math.round(
    (recommendedTravel + terminalBufferMins) * 10,
  ) / 10;
  const arrival = new Date(recommendedDeparture.getTime() + totalEta * 60_000);
  const earliestBoardable = new Date(
    arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000,
  );
  const ferries = offlineFerries(earliestBoardable, new Date());
  const matchingFerries = ferries
    .filter(ferry => ferry.departure_port === (destinationPort ?? 'Batam Centre'))
    .slice(0, 3);

  return {
    route_type: 'ROAD_ROUTE',
    corridor: {
      id: 'free:offline',
      name: `${origin.display_name} -> ${destination.display_name}`,
      origin: 'free-origin',
      destination: 'free-destination',
      distance_km: distanceKm,
      base_time_mins: baseTimeMins,
      straight_line_km: distanceKm,
      detour_ratio: 1,
    },
    vehicle_type: vehicleType,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: recommendedPrediction.current_score,
      predicted_30min: recommendedPrediction.predicted_30min,
      predicted_60min: recommendedPrediction.predicted_60min,
      estimated_delay_mins: recommendedPrediction.estimated_delay_mins,
      status: recommendedPrediction.status,
      risk_level: recommendedPrediction.risk_level,
      trend: recommendedPrediction.trend,
    },
    estimated_travel_time_mins: recommendedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: totalEta,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm
        + (recommendedPrediction.estimated_delay_mins / 60) * profile.idleCo2KgPerHour)
        .toFixed(2),
    ),
    co2_saved_kg: Number(
      (((defer ? savedMins : 0) / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Live road routing is unavailable; traffic timing is an offline estimate.',
    },
    next_matching_ferries: matchingFerries,
    ferry_connection_note: destinationPort
      ? undefined
      : 'Destination is not a ferry port; showing Batam Centre departures for reference.',
    route_geometry: [[origin.lat, origin.lng], [destination.lat, destination.lng]],
    route_data_source: 'offline_straight_line',
    requested_origin: origin,
    requested_destination: destination,
  };
}

// ---------------------------------------------------------------------------
// Live traffic
// ---------------------------------------------------------------------------

export async function fetchLiveTraffic(): Promise<Fetched<LiveTrafficData>> {
  const payload = await getJSON<Envelope & LiveTrafficData>(
    '/api/live-traffic', CORRIDOR_TIMEOUT_MS,
  );
  if (payload) return wrap(payload, payload);
  // Local continuity fallback: keep the overlay useful and honest when the
  // API cannot be reached. The previous fallback claimed to derive corridor
  // traffic but returned an empty array, producing "Traffic ON · 0 segments".
  const fallbackPoints: Record<string, [number, number]> = {
    'corridor-1': [1.103860, 104.039342],
    'corridor-2': [1.149066, 104.023257],
    'corridor-3': [1.119412, 104.020678],
    'corridor-4': [1.105226, 103.955302],
    'corridor-5': [1.116191, 104.099020],
  };
  const zones = offlineCongestionZones();
  const levelCounts = {
    SMOOTH: zones.filter(zone => zone.level === 'SMOOTH').length,
    HEAVY: zones.filter(zone => zone.level === 'HEAVY').length,
    SUPER_CONGESTED: zones.filter(zone => zone.level === 'SUPER_CONGESTED').length,
  };
  const emissionsPressureLevelCounts = {
    LOW: zones.filter(zone => zone.modeled_emissions_pressure.level === 'LOW').length,
    ELEVATED: zones.filter(zone => zone.modeled_emissions_pressure.level === 'ELEVATED').length,
    HIGH: zones.filter(zone => zone.modeled_emissions_pressure.level === 'HIGH').length,
  };
  return wrap(null, {
    segments: INITIAL_CORRIDORS.flatMap((corridor) => {
      const point = fallbackPoints[corridor.id];
      return point ? [{
        corridor_id: corridor.id,
        lat: point[0],
        lng: point[1],
        congestion_index: corridor.live_congestion_score,
        current_speed_kmh: null,
        free_flow_speed_kmh: null,
        source: 'simulated' as const,
      }] : [];
    }),
    zones,
    coverage: {
      hotspot_count: zones.length,
      level_counts: levelCounts,
      emissions_pressure_level_counts: emissionsPressureLevelCounts,
      emissions_pressure_model: EMISSIONS_PRESSURE_MODEL,
      method: 'local model areas blended with nearby corridor telemetry',
    },
    overall_source: 'local_model',
    tomtom_key_configured: false,
  });
}

// ---------------------------------------------------------------------------
// Historical congestion
// ---------------------------------------------------------------------------

function hasApiHistoryMetadata(value: unknown): value is ApiHistoryMetadata {
  if (!isRecord(value)
    || !Number.isInteger(value.window_days)
    || Number(value.window_days) < 1
    || Number(value.window_days) > 30
    || typeof value.observed !== 'boolean'
    || typeof value.contains_observed_samples !== 'boolean'
    || !isRecord(value.source_counts)
    || !isRecord(value.sources)
    || !(typeof value.latest_sample_at === 'string' || value.latest_sample_at === null)
    || !(isNonNegativeInteger(value.latest_sample_age_seconds)
      || value.latest_sample_age_seconds === null)
    || !['current', 'recent', 'stale', 'empty'].includes(String(value.freshness))
    || typeof value.freshness_basis !== 'string') return false;

  const sourceCounts = Object.entries(value.source_counts);
  const sources = Object.entries(value.sources);
  const sourceCountMap = value.source_counts;
  const observedFlags = sources
    .map(([, details]) => isRecord(details) && details.observed === true);
  if (sourceCounts.some(([, count]) => !isNonNegativeInteger(count))
    || sources.length !== sourceCounts.length
    || sources.some(([source, details]) => (
      !isRecord(details)
      || !isNonNegativeInteger(details.sample_count)
      || typeof details.observed !== 'boolean'
      || sourceCountMap[source] !== details.sample_count
    ))
    || value.contains_observed_samples !== observedFlags.some(Boolean)
    || value.observed !== (observedFlags.length > 0 && observedFlags.every(Boolean))) return false;

  const storage = value.storage;
  if (!isRecord(storage)
    || typeof storage.engine !== 'string'
    || typeof storage.durability !== 'string'
    || typeof storage.durable !== 'boolean'
    || typeof storage.shared_across_instances !== 'boolean'
    || typeof storage.fallback_to_memory !== 'boolean') return false;

  const syntheticSeed = value.synthetic_seed;
  return isRecord(syntheticSeed)
    && typeof syntheticSeed.source === 'string'
    && isNonNegativeInteger(syntheticSeed.version)
    && isNonNegativeInteger(syntheticSeed.days)
    && syntheticSeed.days > 0
    && typeof syntheticSeed.timezone === 'string'
    && (typeof syntheticSeed.generated_for_date === 'string'
      || syntheticSeed.generated_for_date === null)
    && syntheticSeed.observed === false;
}

function isHistoricalProfilePayload(
  payload: (Envelope & HistoricalProfile) | null,
  corridorId: string,
  days: number,
): payload is Envelope & HistoricalProfile {
  if (!payload
    || payload.corridor_id !== corridorId
    || payload.days_requested !== days
    || !Array.isArray(payload.hourly_profile)
    || payload.hourly_profile.length !== 24
    || !Array.isArray(payload.weekly_trend)
    || !hasApiHistoryMetadata(payload.history_metadata)
    || payload.history_metadata.window_days !== days) return false;

  const hours = new Set(payload.hourly_profile.map(bucket => bucket?.hour));
  return hours.size === 24 && payload.hourly_profile.every(bucket => (
    Number.isInteger(bucket?.hour)
    && bucket.hour >= 0
    && bucket.hour <= 23
    && (bucket.avg_score === null
      || (Number.isFinite(bucket.avg_score) && bucket.avg_score >= 0 && bucket.avg_score <= 100))
    && Number.isInteger(bucket.sample_count)
    && bucket.sample_count >= 0
  )) && payload.weekly_trend.every(day => (
    typeof day?.date === 'string'
    && Number.isFinite(day.avg_score)
    && day.avg_score >= 0
    && day.avg_score <= 100
    && Number.isInteger(day.sample_count)
    && day.sample_count >= 0
  ));
}

export async function fetchHistoricalCongestion(
  corridorId: string,
  days = 7,
): Promise<Fetched<HistoricalProfile>> {
  const params = new URLSearchParams({ corridor_id: corridorId, days: String(days) });
  const payload = await getJSON<Envelope & HistoricalProfile>(
    `/api/historical-congestion?${params}`, ROUTE_TIMEOUT_MS,
  );
  if (isHistoricalProfilePayload(payload, corridorId, days)) return wrap(payload, payload);
  return wrap(null, browserModeledHistoricalProfile(corridorId, days));
}

// ---------------------------------------------------------------------------
// AI Model Status & Retraining
// ---------------------------------------------------------------------------

function isModelMetricsPayload(value: unknown): value is ModelMetrics {
  if (!isRecord(value)
    || typeof value.is_trained !== 'boolean'
    || (value.retraining_enabled !== undefined
      && typeof value.retraining_enabled !== 'boolean')
    || !isNonNegativeInteger(value.total_samples)
    || typeof value.r2_score !== 'number'
    || !Number.isFinite(value.r2_score)
    || !isFiniteNonNegative(value.mae)
    || !isFiniteNonNegative(value.rmse)
    || !(typeof value.last_trained_at === 'string' || value.last_trained_at === null)
    || !isRecord(value.feature_importances)) return false;

  return Object.values(value.feature_importances).every(weight => (
    isFiniteNonNegative(weight) && weight <= 1
  ));
}

export async function fetchModelStatus(): Promise<Fetched<ModelMetrics>> {
  const payload = await getJSON<Envelope & { metrics: ModelMetrics }>(
    '/api/model-status', ROUTE_TIMEOUT_MS,
  );
  if (payload && isModelMetricsPayload(payload.metrics)) return wrap(payload, payload.metrics);
  return wrap(null, BUNDLED_RF_VALIDATION);
}

export async function requestModelRetrain(): Promise<Fetched<ModelMetrics>> {
  const payload = await getJSON<Envelope & { metrics: ModelMetrics }>(
    '/api/retrain-model',
    ROUTE_TIMEOUT_MS,
    { method: 'POST' },
    true,
  );
  if (payload && isModelMetricsPayload(payload.metrics)) return wrap(payload, payload.metrics);
  throw new ApiRequestError('The backend is unavailable; the model was not retrained.', 503);
}
