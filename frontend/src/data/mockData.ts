import ferryTimetableJson from './ferry_timetable.json';
import corridorHotspotCatalog from './corridor_hotspots.json';
import { AlertItem, CongestionZone, Corridor, EmissionsPressureModel, FerrySchedule, FerryTimetableMetadata, FerryTimetableSnapshot, ModeledEmissionsPressure, OperationsSummary, RouteLocation, Shipment } from '../types';
import { delayFromScore, predictLocal } from '../services/localForecast';
import {
  batamParts,
  corridorParts,
  corridorScheduleInstant,
  toBatamIso,
  toCorridorIso,
  type CorridorTimezone,
} from '../utils/batamTime';

/**
 * Offline fallback data, used only when the backend is unreachable.
 *
 * Formats match the API exactly — corridor names use "->", ferry times are ISO
 * 8601 — so nothing on screen visibly rewrites itself if the backend drops
 * mid-demo. The UI applies the nicer glyph and preserves Batam time at render.
 *
 * Distances are the real A* road distances from the OSM graph, not the
 * hardcoded estimates they replaced (several of which were wrong by kilometres
 * because the underlying landmark coordinates were wrong).
 */

/** Index into the model's corridor feature, matching the backend ordering. */
export const CORRIDOR_INDEX: Record<string, number> = {
  'corridor-1': 0,
  'corridor-2': 1,
  'corridor-3': 2,
  'corridor-4': 3,
  'corridor-5': 4,
};

const CORRIDOR_SEEDS = [
  {
    id: 'corridor-1',
    name: 'Mukakuning Industrial -> Batam Centre Terminal',
    origin: 'mukakuning',
    destination: 'batam_centre',
    distance_km: 9.91,
    base_time_mins: 18,
    key_checkpoints: ['Mukakuning Gate', 'Simpang Kabil', 'Batam Centre Ferry'],
  },
  {
    id: 'corridor-2',
    name: 'Batu Ampar Freight Port -> Batam Centre Ferry',
    origin: 'batu_ampar',
    destination: 'batam_centre',
    distance_km: 8.90,
    base_time_mins: 16,
    key_checkpoints: ['Batu Ampar Gate 2', 'Jalan Yos Sudarso', 'Batam Centre'],
  },
  {
    id: 'corridor-3',
    name: 'Hang Nadim Airport -> Nagoya City Centre',
    origin: 'hang_nadim',
    destination: 'nagoya',
    distance_km: 15.09,
    base_time_mins: 24,
    key_checkpoints: ['Airport Toll Access', 'Simpang Jam', 'Nagoya Hill'],
  },
  {
    id: 'corridor-4',
    name: 'Sekupang Ferry Terminal -> Mukakuning Industrial',
    origin: 'sekupang',
    destination: 'mukakuning',
    distance_km: 22.03,
    base_time_mins: 22,
    key_checkpoints: ['Sekupang Port', 'Jalan Gajah Mada', 'Mukakuning South'],
  },
  {
    id: 'corridor-5',
    name: 'Nongsa Digital Park -> Batam Centre Terminal',
    origin: 'nongsa',
    destination: 'batam_centre',
    distance_km: 17.43,
    base_time_mins: 20,
    key_checkpoints: ['Nongsa Tech Hub', 'Jalan Hang Tuah', 'Batam Centre Ferry'],
  },
];

/** Mirrors /api/route-locations so the planner still works if the backend is
 * unavailable during a demo. */
export const ROUTE_LOCATIONS: RouteLocation[] = [
  { id: 'batam_centre', name: 'Batam Centre Ferry Terminal', category: 'Ferry terminals', lat: 1.1318, lng: 104.0554, ferry_port: 'Batam Centre' },
  { id: 'harbour_bay', name: 'Harbour Bay Ferry Terminal', category: 'Ferry terminals', lat: 1.15396, lng: 103.997234, ferry_port: 'HarbourBay' },
  { id: 'sekupang', name: 'Sekupang Ferry Terminal', category: 'Ferry terminals', lat: 1.1250, lng: 103.9250, ferry_port: 'Sekupang' },
  { id: 'hang_nadim', name: 'Hang Nadim Airport', category: 'Transport hubs', lat: 1.1211, lng: 104.1147 },
  { id: 'batu_aji', name: 'Batu Aji Transit Hub', category: 'Transport hubs', lat: 1.051, lng: 103.965 },
  { id: 'tiban', name: 'Tiban Centre', category: 'Transport hubs', lat: 1.099, lng: 103.961 },
  { id: 'mukakuning', name: 'Batamindo Industrial Park', category: 'Industry & logistics', lat: 1.0605, lng: 104.0303 },
  { id: 'batu_ampar', name: 'Batu Ampar Freight Port', category: 'Industry & logistics', lat: 1.1630, lng: 104.0025 },
  { id: 'kabil_industrial', name: 'Kabil Industrial Estate', category: 'Industry & logistics', lat: 1.094875, lng: 104.118329 },
  { id: 'nongsa', name: 'Nongsa Digital Park', category: 'Business & shopping', lat: 1.1822, lng: 104.1030 },
  { id: 'nagoya', name: 'Nagoya Hill', category: 'Business & shopping', lat: 1.1465, lng: 104.0125 },
  { id: 'panbil_mall', name: 'Panbil Mall', category: 'Business & shopping', lat: 1.07210, lng: 104.02355 },
  { id: 'kepri_mall', name: 'Kepri Mall', category: 'Business & shopping', lat: 1.101, lng: 104.038 },
];

/** Corridor telemetry for the current time of day, so an offline demo still
 *  shows a plausible rush hour rather than frozen mid-afternoon numbers. */
function seedCorridors(at: Date = new Date()): Corridor[] {
  const batam = batamParts(at);
  const hourFloat = batam.hour + batam.minute / 60;
  const isWeekend = batam.weekday === 0 || batam.weekday === 6 ? 1 : 0;

  return CORRIDOR_SEEDS.map(seed => {
    const idx = CORRIDOR_INDEX[seed.id];
    const p = predictLocal(hourFloat, isWeekend, 0, 0, idx);
    return {
      ...seed,
      live_congestion_score: p.current_score,
      delay_mins: p.estimated_delay_mins,
      status: p.status,
      risk_level: p.risk_level,
      forecast_30m: p.predicted_30min,
      forecast_60m: p.predicted_60min,
      trend: p.trend,
      ferry_surge: 0,
      surge_source: null,
      is_weekend: Boolean(isWeekend),
    };
  });
}

export const INITIAL_CORRIDORS: Corridor[] = seedCorridors();

interface OfflineHotspotSeed {
  zone_id: string;
  name: string;
  category: string;
  lat: number;
  lng: number;
  radius_m: number;
  base_score: number;
  signal_mix: Record<string, number>;
  peak_windows: Array<[number, number]>;
  network_criticality: number;
  demand_exposure: number;
  routing_enabled: boolean;
}

const HOTSPOT_CORRIDOR_SIGNAL_WEIGHT = Math.min(
  0.85,
  corridorHotspotCatalog.methodology.source_confidence.simulated,
);
const HOTSPOT_BASELINE_WEIGHT = 1 - HOTSPOT_CORRIDOR_SIGNAL_WEIGHT;
const HOTSPOT_ACTIVE_PEAK_LIFT =
  corridorHotspotCatalog.methodology.dynamic_congestion.active_peak_lift;
const HOTSPOT_SELECTION_WEIGHTS = corridorHotspotCatalog.methodology.selection_weights;

export const EMISSIONS_PRESSURE_MODEL: EmissionsPressureModel = {
  schema_version: 1,
  methodology_version: 'crossflow-zone-pressure-v1',
  formula: 'queue_pressure_factor=(congestion_index/100)^2; index=100*factor',
  thresholds: { ELEVATED: 16, HIGH: 49 },
  traffic_input: 'simulated_corridor_forecast_plus_recurring_zone_baseline',
  source: 'crossflow_congestion_delay_model',
  observed: false,
  aggregate_mass_available: false,
  limitations: 'Relative queue pressure before road, route, vehicle and traffic-volume exposure; not measured CO2, air quality, or area kg/hour.',
};

function modeledEmissionsPressure(congestionIndex: number): ModeledEmissionsPressure {
  const queuePressureFactor = (Math.max(0, Math.min(100, congestionIndex)) / 100) ** 2;
  const index = Math.round(100 * queuePressureFactor * 10) / 10;
  return {
    index,
    queue_pressure_factor: Math.round(queuePressureFactor * 10_000) / 10_000,
    level: index >= EMISSIONS_PRESSURE_MODEL.thresholds.HIGH
      ? 'HIGH'
      : index >= EMISSIONS_PRESSURE_MODEL.thresholds.ELEVATED
        ? 'ELEVATED'
        : 'LOW',
    metric: 'relative_queue_emissions_pressure',
    unit: 'index_0_100',
    observed: false,
  };
}

/** The browser continuity layer reads the same backend-owned 30-place data. */
const OFFLINE_HOTSPOT_SEEDS = (
  corridorHotspotCatalog.candidates as unknown as OfflineHotspotSeed[]
);

function inPeakWindow(hour: number, windows: Array<[number, number]>): boolean {
  return windows.some(([start, end]) => (
    start <= end ? hour >= start && hour < end : hour >= start || hour < end
  ));
}

export function offlineCongestionZones(at: Date = new Date()): CongestionZone[] {
  const scores = new Map(
    seedCorridors(at).map(corridor => [corridor.id, corridor.live_congestion_score]),
  );
  const hour = batamParts(at).hour;
  const candidates = OFFLINE_HOTSPOT_SEEDS.map((seed): CongestionZone => {
    const peakActive = inPeakWindow(hour, seed.peak_windows);
    const corridorScore = Object.entries(seed.signal_mix).reduce(
      (total, [corridorId, weight]) => total + (scores.get(corridorId) ?? seed.base_score) * weight,
      0,
    );
    const recurringPressure = Math.max(0, Math.min(
      100,
      seed.base_score + (peakActive ? HOTSPOT_ACTIVE_PEAK_LIFT : 0),
    ));
    const score = Math.max(0, Math.min(
      100,
      Math.round((
        HOTSPOT_CORRIDOR_SIGNAL_WEIGHT * corridorScore
        + HOTSPOT_BASELINE_WEIGHT * recurringPressure
      ) * 10) / 10,
    ));
    const level = score >= 70 ? 'SUPER_CONGESTED' : score >= 40 ? 'HEAVY' : 'SMOOTH';
    const evidenceConfidence = 100
      * corridorHotspotCatalog.methodology.source_confidence.simulated;
    const selectionScore = Math.round((
      HOTSPOT_SELECTION_WEIGHTS.corridor_pressure * corridorScore
      + HOTSPOT_SELECTION_WEIGHTS.recurrence * recurringPressure
      + HOTSPOT_SELECTION_WEIGHTS.network_criticality * seed.network_criticality
      + HOTSPOT_SELECTION_WEIGHTS.demand_exposure * seed.demand_exposure
      + HOTSPOT_SELECTION_WEIGHTS.evidence_confidence * evidenceConfidence
    ) * 1_000) / 1_000;
    return {
      zone_id: seed.zone_id,
      name: seed.name,
      category: seed.category,
      lat: seed.lat,
      lng: seed.lng,
      radius_m: seed.radius_m,
      congestion_index: score,
      level,
      color: level === 'SUPER_CONGESTED' ? '#ef4444' : level === 'HEAVY' ? '#f59e0b' : '#10b981',
      avoid_recommended: level === 'SUPER_CONGESTED',
      corridor_ids: Object.keys(seed.signal_mix),
      peak_active: peakActive,
      source: 'modelled_spatial_hotspot',
      observed: false,
      routing_enabled: seed.routing_enabled,
      base_score: seed.base_score,
      network_criticality: seed.network_criticality,
      demand_exposure: seed.demand_exposure,
      selection_score: selectionScore,
      modeled_emissions_pressure: modeledEmissionsPressure(score),
    };
  });
  return candidates
    .sort((left, right) => (
      (right.selection_score ?? 0) - (left.selection_score ?? 0)
      || left.zone_id.localeCompare(right.zone_id)
    ))
    .map((zone, index) => ({
      ...zone,
      selection_rank: index + 1,
      watch_priority: index < 20 ? 'CRITICAL' : 'HEAVY',
    }));
}

// --- Ferries ---------------------------------------------------------------

/** The backend and browser read the same versioned published timetable file. */
export const PUBLISHED_FERRY_TIMETABLE = ferryTimetableJson as FerryTimetableSnapshot;

export const PUBLISHED_FERRY_TIMETABLE_METADATA: FerryTimetableMetadata = {
  schema_version: PUBLISHED_FERRY_TIMETABLE.schema_version,
  snapshot_id: PUBLISHED_FERRY_TIMETABLE.snapshot_id,
  timezone: PUBLISHED_FERRY_TIMETABLE.timezone,
  last_verified_at: PUBLISHED_FERRY_TIMETABLE.last_verified_at,
  status: PUBLISHED_FERRY_TIMETABLE.status,
  live_board_url: PUBLISHED_FERRY_TIMETABLE.live_board_url,
  limitations: PUBLISHED_FERRY_TIMETABLE.limitations,
  sources: PUBLISHED_FERRY_TIMETABLE.sources,
};

const FERRY_SOURCE_BY_ID = new Map(
  PUBLISHED_FERRY_TIMETABLE.sources.map(source => [source.source_id, source]),
);

function publishedSlotMinute(value: string): number {
  const [hourText, minuteText] = value.split(':');
  const hour = Number(hourText);
  const minute = Number(minuteText);
  if (!Number.isInteger(hour) || !Number.isInteger(minute)
    || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    throw new Error(`Invalid published ferry departure slot: ${value}`);
  }
  return hour * 60 + minute;
}

function publishedFerryIso(
  value: Date,
  timezone: CorridorTimezone,
): string {
  return toCorridorIso(value, timezone).replace('.000+', '+');
}

function terminalTimezone(port: string): CorridorTimezone {
  return port.endsWith(' SG') ? 'Asia/Singapore' : 'Asia/Jakarta';
}

/**
 * Expand the committed operator timetable snapshot forward from `after`.
 * This mirrors the backend without inventing a vessel, seat count, gate,
 * berth, capacity, cancellation, or live operating state.
 */
export function offlineFerries(
  after: Date = new Date(),
  relativeTo: Date = after,
  horizonHours = 12,
): FerrySchedule[] {
  const out: FerrySchedule[] = [];
  const horizonEnd = after.getTime() + horizonHours * 60 * 60_000;

  for (const service of PUBLISHED_FERRY_TIMETABLE.services) {
    const source = FERRY_SOURCE_BY_ID.get(service.source_id);
    if (!source) throw new Error(`Unknown ferry timetable source: ${service.source_id}`);
    const departureTimezone = service.departure_timezone
      ?? PUBLISHED_FERRY_TIMETABLE.timezone;
    const arrivalTimezone = service.arrival_timezone
      ?? terminalTimezone(service.arrival_port);

    for (let dayOffset = 0; dayOffset < 3; dayOffset++) {
      const day = corridorScheduleInstant(
        after, dayOffset, 0, departureTimezone,
      );
      const weekday = corridorParts(day, departureTimezone).weekday;
      const departureSlots = [
        ...service.daily_departures,
        ...(weekday === 0 || weekday === 6 ? service.weekend_additions : []),
      ].map(publishedSlotMinute).sort((a, b) => a - b);

      departureSlots.forEach((minuteOfDay, slot) => {
        const departure = corridorScheduleInstant(
          after, dayOffset, minuteOfDay, departureTimezone,
        );
        if (departure.getTime() <= after.getTime() || departure.getTime() > horizonEnd) return;

        const minutesUntil = Math.round((departure.getTime() - relativeTo.getTime()) / 60_000);
        const arrival = new Date(
          departure.getTime() + service.estimated_crossing_mins * 60_000,
        );
        const departureIso = publishedFerryIso(departure, departureTimezone);
        out.push({
          sailing_id: `${service.service_id}-${departureIso.slice(0, 10).replace(/-/g, '')}-${String(slot).padStart(2, '0')}`,
          ferry_name: service.operator,
          operator: service.operator,
          departure_port: service.departure_port,
          arrival_port: service.arrival_port,
          departure_time: departureIso,
          arrival_time: publishedFerryIso(arrival, arrivalTimezone),
          departure_timezone: departureTimezone,
          arrival_timezone: arrivalTimezone,
          estimated_crossing_mins: service.estimated_crossing_mins,
          arrival_time_is_estimate: true,
          minutes_until_departure: minutesUntil,
          status: 'SCHEDULED',
          available_seats: null,
          capacity: null,
          live_status_available: false,
          data_source: 'official_timetable_snapshot',
          schedule_source_id: source.source_id,
          schedule_source_url: source.schedule_url,
          booking_url: source.booking_url,
          schedule_effective_from: source.effective_from,
          schedule_last_verified_at: source.last_verified_at,
          schedule_calendar_note: source.calendar_note,
        });
      });
    }
  }

  return out.sort(
    (a, b) => Date.parse(a.departure_time) - Date.parse(b.departure_time),
  );
}

export const INITIAL_FERRIES: FerrySchedule[] = offlineFerries();

// --- Operations ------------------------------------------------------------

function seedOperations(at: Date = new Date()): OperationsSummary {
  const corridors = seedCorridors(at);
  const avg = Math.round(
    (corridors.reduce((s, c) => s + c.live_congestion_score, 0) / corridors.length) * 10) / 10;
  const bottlenecks = corridors.filter(c => c.live_congestion_score > 60);

  // Same integral the backend uses, so the offline figure is derived rather
  // than a constant that contradicts the pitch slide.
  const batam = batamParts(at);
  const elapsed = batam.hour + batam.minute / 60;
  const isWeekend = batam.weekday === 0 || batam.weekday === 6 ? 1 : 0;
  const byCorridor: Record<string, number> = {};
  let accrued = 0;
  let projected = 0;
  for (const seed of CORRIDOR_SEEDS) {
    const idx = CORRIDOR_INDEX[seed.id];
    let total = 0;
    for (let h = 0; h < 24; h++) {
      const kg = 40 * (delayFromScore(
        predictLocal(h, isWeekend, 0, 0, idx).current_score) / 60) * 0.35 * 1.8;
      projected += kg;
      total += kg * Math.min(1, Math.max(0, elapsed - h));
    }
    byCorridor[seed.id] = Math.round(total * 10) / 10;
    accrued += total;
  }

  const quietest = corridors.reduce((a, b) =>
    a.live_congestion_score <= b.live_congestion_score ? a : b);
  const stamp = (minsAgo: number) =>
    toBatamIso(new Date(at.getTime() - minsAgo * 60_000));

  // Same invariant the backend guarantees: an alert can only describe a
  // corridor that really is in the state claimed, so the feed can never name a
  // bottleneck while the counter above it reads zero.
  const alerts: AlertItem[] = corridors
    .filter(c => c.status === 'CRITICAL')
    .map(c => ({
      id: `alt-${c.id}-congestion`,
      severity: 'CRITICAL',
      corridor_id: c.id,
      title: `${c.key_checkpoints[1]} congestion critical`,
      message: `${c.name} at index ${c.live_congestion_score} (+${c.delay_mins}m delay).`,
      timestamp: stamp(6),
    }));

  if (!alerts.length) {
    alerts.push({
      id: `alt-${quietest.id}-clear`,
      severity: 'INFO',
      corridor_id: quietest.id,
      title: 'Optimal freight window open',
      message: `${quietest.name} running at index ${quietest.live_congestion_score}. Recommend dispatch now.`,
      timestamp: stamp(4),
    });
  }

  return {
    overall_network_status: avg > 65 ? 'CONGESTED' : avg > 40 ? 'MODERATE' : 'OPTIMAL',
    average_congestion_index: avg,
    active_bottlenecks: bottlenecks.length,
    bottleneck_corridors: bottlenecks.map(c => ({
      id: c.id, name: c.name, score: c.live_congestion_score,
    })),
    bottleneck_threshold: 60,
    total_co2_reduced_today_kg: Math.round(accrued * 10) / 10,
    projected_full_day_co2_kg: Math.round(projected * 10) / 10,
    co2_by_corridor_kg: byCorridor,
    co2_methodology: {
      advised_trips_per_corridor_per_hour: 40,
      avoidable_delay_fraction: 0.35,
      idle_burn_kg_per_hour: 1.8,
      basis: 'Offline estimate from the local forecast model, same assumptions as the API.',
    },
    active_ferry_sailings: offlineFerries(at).length,
    alerts,
  };
}

export const MOCK_OPERATIONS: OperationsSummary = seedOperations();

// --- Demo freight ----------------------------------------------------------

/**
 * Sample consignments. Explicitly labelled as a demo dataset in the UI — no
 * ferry operator or logistics provider on this route publishes an API.
 */
export const DEMO_SHIPMENTS: Shipment[] = [
  {
    id: 'SHP-8842',
    origin: 'Mukakuning Tech Park',
    destination: 'Singapore Tuas Logistics Hub',
    carrier: 'Sindo Logistics Lines',
    vessel: 'Sindo Cargo 04',
    status: 'IN_TRANSIT',
    progress: 68,
    eta: '14:45',
    co2_saved: '18.4 kg',
  },
  {
    id: 'SHP-9105',
    origin: 'Batu Ampar Port Gate 2',
    destination: 'Jurong Port SG',
    carrier: 'Batam Freight Line',
    vessel: 'Batam Express III',
    status: 'CUSTOMS_CLEARANCE',
    progress: 88,
    eta: '13:30',
    co2_saved: '24.1 kg',
  },
  {
    id: 'SHP-7721',
    origin: 'Nongsa Digital Hub',
    destination: 'Changi Airport Logistics Centre',
    carrier: 'CrossFlow Fast Air Cargo',
    vessel: 'Majestic Fast 302',
    status: 'SCHEDULED',
    progress: 15,
    eta: '16:15',
    co2_saved: '12.0 kg',
  },
];

// --- Map geometry ----------------------------------------------------------

/**
 * Landmark coordinates, corrected against OpenStreetMap.
 *
 * Four of the original values were wrong by more than a kilometre — Muka Kuning
 * was 1.6 km north of Batamindo Industrial Park, so the busiest corridor in the
 * demo was drawn along entirely the wrong roads. These now match the OSM
 * features the routing graph snaps to.
 */
export const MAP_NODES = {
  batam_centre: { lat: 1.1318, lng: 104.0554, name: 'Batam Centre Ferry Terminal' },
  mukakuning: { lat: 1.0605, lng: 104.0303, name: 'Mukakuning Industrial Park' },
  batu_ampar: { lat: 1.1630, lng: 104.0025, name: 'Batu Ampar Cargo Port' },
  hang_nadim: { lat: 1.1211, lng: 104.1147, name: 'Hang Nadim Int. Airport' },
  nagoya: { lat: 1.1465, lng: 104.0125, name: 'Nagoya Commercial District' },
  sekupang: { lat: 1.1250, lng: 103.9250, name: 'Sekupang International Port' },
  nongsa: { lat: 1.1822, lng: 104.1030, name: 'Nongsa Digital Park' },
  harbourfront_sg: { lat: 1.2644, lng: 103.8206, name: 'HarbourFront Terminal (SG)' },
};

// Hand-tuned sea-lane bends follow the HarbourFront departure channel,
// Singapore's southern edge, and the approach around northern Batam.
export const FERRY_SEA_ROUTE: [number, number][] = [
  [MAP_NODES.harbourfront_sg.lat, MAP_NODES.harbourfront_sg.lng],
  [1.2660, 103.8400],
  [1.2580, 103.8600],
  [1.2450, 103.8700],
  [1.2000, 103.9630],
  [1.2000, 104.0150],
  [1.2000, 104.0500],
  [1.1700, 104.0650],
  [1.1450, 104.0650],
  [MAP_NODES.batam_centre.lat, MAP_NODES.batam_centre.lng],
];

/** Corridor endpoints, matching backend/services/route_solver.py. */
export const CORRIDOR_ENDPOINTS: Record<string, [keyof typeof MAP_NODES, keyof typeof MAP_NODES]> = {
  'corridor-1': ['mukakuning', 'batam_centre'],
  'corridor-2': ['batu_ampar', 'batam_centre'],
  'corridor-3': ['hang_nadim', 'nagoya'],
  'corridor-4': ['sekupang', 'mukakuning'],
  'corridor-5': ['nongsa', 'batam_centre'],
};
