import type {
  FerrySchedule,
  FreeLocation,
  NavigationData,
  RouteLeg,
  RouteOptimizationResult,
  RoutePreference,
  VehicleType,
} from '../types';
import { FERRY_SEA_ROUTE, offlineFerries, PUBLISHED_FERRY_TIMETABLE } from '../data/mockData';
import { routePreferenceProfile } from '../data/routePreferences';
import { vehicleProfile, vehicleProfileSnapshot } from '../data/vehicleCatalog';
import { nextCorridorHour, toCorridorIso } from '../utils/batamTime';

export type LocationRegion = 'BATAM' | 'SINGAPORE';

export const CORRIDOR_MAP_BOUNDS = {
  south: 0.88,
  west: 103.55,
  north: 1.50,
  east: 104.30,
} as const;

interface TerminalPair {
  singaporeName: string;
  singapore: [number, number];
  batamName: string;
  batam: [number, number];
  crossingMins: number;
  seaGeometry: [number, number][];
}

const TERMINAL_PAIRS: TerminalPair[] = [
  {
    singaporeName: 'HarbourFront SG', singapore: [1.2644, 103.8206],
    batamName: 'Batam Centre', batam: [1.1318, 104.0554], crossingMins: 55,
    seaGeometry: FERRY_SEA_ROUTE,
  },
  {
    singaporeName: 'HarbourFront SG', singapore: [1.2644, 103.8206],
    batamName: 'HarbourBay', batam: [1.15396, 103.997234], crossingMins: 50,
    seaGeometry: [
      [1.2644, 103.8206], [1.243, 103.83], [1.216, 103.867],
      [1.19, 103.914], [1.171, 103.961], [1.15396, 103.997234],
    ],
  },
  {
    singaporeName: 'HarbourFront SG', singapore: [1.2644, 103.8206],
    batamName: 'Sekupang', batam: [1.125, 103.925], crossingMins: 50,
    seaGeometry: [
      [1.2644, 103.8206], [1.241, 103.819], [1.211, 103.839],
      [1.174, 103.873], [1.145, 103.905], [1.125, 103.925],
    ],
  },
  {
    singaporeName: 'Tanah Merah SG', singapore: [1.3143, 103.9886],
    batamName: 'Batam Centre', batam: [1.1318, 104.0554], crossingMins: 55,
    seaGeometry: [
      [1.3143, 103.9886], [1.282, 103.998], [1.245, 104.006],
      [1.206, 104.02], [1.166, 104.041], [1.1318, 104.0554],
    ],
  },
  {
    singaporeName: 'Tanah Merah SG', singapore: [1.3143, 103.9886],
    batamName: 'Nongsa Pura', batam: [1.189, 104.102], crossingMins: 45,
    seaGeometry: [
      [1.3143, 103.9886], [1.283, 103.999], [1.25, 104.015],
      [1.22, 104.046], [1.204, 104.077], [1.189, 104.102],
    ],
  },
];

const BOARDING_BUFFER_MINS = 15;
const PASSENGER_FERRY_INCOMPATIBLE_VEHICLES = new Set<VehicleType>([
  'LIGHT_TRUCK', 'CARGO_TRUCK',
]);

export const PASSENGER_FERRY_TRUCK_MESSAGE = (
  'Light and heavy trucks cannot be scheduled on the published passenger-ferry '
  + 'services used by this planner. Local road routing remains available; '
  + 'cross-border freight requires a cargo port, roll-on/roll-off operator, '
  + 'or an authorised logistics-partner feed.'
);

export const PASSENGER_FERRY_TRANSFER_NOTE = (
  'The selected vehicle profile applies only to first- and last-mile road access; '
  + 'it is not carried onboard the passenger ferry.'
);

export function isPassengerFerryIncompatibleVehicle(
  vehicleType: VehicleType,
): boolean {
  return PASSENGER_FERRY_INCOMPATIBLE_VEHICLES.has(vehicleType);
}

export function locationRegion(lat: number, lng: number): LocationRegion | null {
  if (lat >= 0.88 && lat < 1.215 && lng >= 103.75 && lng <= 104.30) return 'BATAM';
  if (lat >= 1.215 && lat <= 1.50 && lng >= 103.55 && lng <= 104.15) return 'SINGAPORE';
  return null;
}

export function isSupportedLocation(lat: number, lng: number): boolean {
  return locationRegion(lat, lng) !== null;
}

function toRadians(value: number): number {
  return value * Math.PI / 180;
}

function haversineKm(a: [number, number], b: [number, number]): number {
  const earthRadiusKm = 6371.0088;
  const phi1 = toRadians(a[0]);
  const phi2 = toRadians(b[0]);
  const dPhi = phi2 - phi1;
  const dLambda = toRadians(b[1] - a[1]);
  const h = Math.sin(dPhi / 2) ** 2
    + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  return 2 * earthRadiusKm * Math.asin(Math.sqrt(h));
}

function geometryLengthKm(geometry: [number, number][]): number {
  return geometry.slice(1).reduce(
    (sum, point, index) => sum + haversineKm(geometry[index], point),
    0,
  );
}

function chooseTerminalPair(
  origin: [number, number],
  destination: [number, number],
  originRegion: LocationRegion,
): TerminalPair {
  const directionalPairs = TERMINAL_PAIRS.filter(pair => (
    PUBLISHED_FERRY_TIMETABLE.services.some(service => (
      originRegion === 'SINGAPORE'
        ? service.departure_port === pair.singaporeName
          && service.arrival_port === pair.batamName
        : service.departure_port === pair.batamName
          && service.arrival_port === pair.singaporeName
    ))
  ));
  const candidates = directionalPairs.length > 0 ? directionalPairs : TERMINAL_PAIRS;
  const scored = candidates.map(pair => {
    const first = originRegion === 'SINGAPORE'
      ? haversineKm(origin, pair.singapore)
      : haversineKm(origin, pair.batam);
    const last = originRegion === 'SINGAPORE'
      ? haversineKm(pair.batam, destination)
      : haversineKm(pair.singapore, destination);
    return { pair, score: ((first + last) * 1.22 / 35 * 60) + pair.crossingMins };
  });
  scored.sort((a, b) => a.score - b.score
    || `${a.pair.singaporeName}:${a.pair.batamName}`.localeCompare(
      `${b.pair.singaporeName}:${b.pair.batamName}`,
    ));
  return scored[0].pair;
}

function curvedConnector(
  origin: [number, number],
  destination: [number, number],
): [number, number][] {
  const latDelta = destination[0] - origin[0];
  const lngDelta = destination[1] - origin[1];
  const magnitude = Math.max(0.000001, Math.hypot(latDelta, lngDelta));
  const bend = Math.min(0.012, Math.max(0.0015, magnitude * 0.06));
  const latPerp = -lngDelta / magnitude * bend;
  const lngPerp = latDelta / magnitude * bend;
  return [
    origin,
    [origin[0] + latDelta * 0.25 + latPerp, origin[1] + lngDelta * 0.25 + lngPerp],
    [origin[0] + latDelta * 0.5 + latPerp * 0.5, origin[1] + lngDelta * 0.5 + lngPerp * 0.5],
    [origin[0] + latDelta * 0.75, origin[1] + lngDelta * 0.75],
    destination,
  ].map(([lat, lng]) => [Number(lat.toFixed(6)), Number(lng.toFixed(6))]);
}

function estimatedNavigation(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
): NavigationData {
  const distanceM = Math.round(haversineKm(origin, destination) * 1_220);
  return {
    schema_version: 1,
    data_source: 'offline_access_estimate',
    maneuvers: [
      {
        step: 1, type: 'DEPART', modifier: 'straight',
        instruction: 'Start the estimated road-access leg',
        street: 'Road access estimate', distance_m: distanceM,
        cumulative_distance_m: 0, icon: 'depart', coords: origin,
      },
      {
        step: 2, type: 'ARRIVE', modifier: 'arrive',
        instruction: `Arrive at ${destinationName}`,
        street: destinationName, distance_m: 0,
        cumulative_distance_m: distanceM, icon: 'arrive', coords: destination,
      },
    ],
    landmarks_along_route: [],
    traffic_lights_count: 0,
    route_narrative_words: 'Continuity estimate only; not turn-by-turn road navigation.',
  };
}

function roadLeg(
  origin: [number, number], destination: [number, number],
  fromName: string, toName: string,
): RouteLeg {
  const distanceKm = Number((haversineKm(origin, destination) * 1.22).toFixed(2));
  return {
    mode: 'ROAD', from_name: fromName, to_name: toName,
    geometry: curvedConnector(origin, destination),
    distance_km: distanceKm,
    duration_mins: Number(Math.max(2, distanceKm / 32 * 60).toFixed(1)),
    data_source: 'offline_access_estimate', is_estimate: true,
    navigation: estimatedNavigation(origin, destination, toName),
    limitations: 'Deterministic access estimate and display connector; not a turn-by-turn road path.',
  };
}

function nextPublishedSailing(
  pair: TerminalPair,
  originRegion: LocationRegion,
  terminalArrival: Date,
  now: Date,
): FerrySchedule | null {
  const cutoff = new Date(terminalArrival.getTime() + BOARDING_BUFFER_MINS * 60_000);
  const departurePort = originRegion === 'SINGAPORE'
    ? pair.singaporeName : pair.batamName;
  const arrivalPort = originRegion === 'SINGAPORE'
    ? pair.batamName : pair.singaporeName;
  return offlineFerries(cutoff, now, 48).find(ferry => (
    ferry.departure_port === departurePort
      && ferry.arrival_port === arrivalPort
  )) ?? null;
}

function ferryLeg(
  pair: TerminalPair,
  originRegion: LocationRegion,
  terminalArrival: Date,
  now: Date,
): { leg: RouteLeg; sailings: FerrySchedule[]; waitMins: number; note?: string } {
  const batamToSingapore = originRegion === 'BATAM';
  const selectedSailing = nextPublishedSailing(
    pair, originRegion, terminalArrival, now,
  );
  const fromName = batamToSingapore ? pair.batamName : pair.singaporeName;
  const toName = batamToSingapore ? pair.singaporeName : pair.batamName;
  const geometry = batamToSingapore
    ? [...pair.seaGeometry].reverse()
    : pair.seaGeometry;
  const waitMins = selectedSailing
    ? Math.max(0, Math.round(
      (new Date(selectedSailing.departure_time).getTime() - terminalArrival.getTime()) / 60_000,
    ))
    : BOARDING_BUFFER_MINS;
  const limitations = selectedSailing
    ? 'Published operator slot and estimated crossing duration; not live cancellation, gate, capacity or seat status.'
    : 'No matching departure was found within the bundled 48-hour official schedule snapshot. Crossing duration is a planning reference only; verify and book with the operator.';
  const crossingMins = selectedSailing?.estimated_crossing_mins
    ?? pair.crossingMins;
  return {
    leg: {
      mode: 'FERRY', from_name: fromName, to_name: toName,
      geometry, distance_km: Number(geometryLengthKm(geometry).toFixed(2)),
      duration_mins: crossingMins, wait_mins: waitMins,
      data_source: selectedSailing
        ? 'official_timetable_snapshot'
        : 'official_corridor_duration_reference',
      is_estimate: true,
      schedule_status: selectedSailing
        ? 'PUBLISHED_DEPARTURE_SELECTED'
        : 'NO_MATCHING_PUBLISHED_DEPARTURE',
      selected_sailing: selectedSailing,
      vehicle_carried_onboard: false,
      limitations,
      geometry_note: 'Channel-aware terminal corridor visualisation; not an observed or navigational vessel track.',
    },
    sailings: selectedSailing ? [selectedSailing] : [],
    waitMins,
    note: selectedSailing ? undefined : limitations,
  };
}

function combinedGeometry(legs: RouteLeg[]): [number, number][] {
  return legs.flatMap((leg, legIndex) => (
    legIndex === 0 ? leg.geometry : leg.geometry.slice(1)
  ));
}

function combinedNavigation(legs: RouteLeg[]): NavigationData {
  let cumulativeM = 0;
  const maneuvers: NavigationData['maneuvers'] = [];
  legs.forEach((leg) => {
    if (leg.mode === 'FERRY') {
      const first = leg.geometry[0];
      const last = leg.geometry[leg.geometry.length - 1];
      maneuvers.push(
        {
          step: 0, type: 'BOARD_FERRY', modifier: 'transfer',
          instruction: `Board the ferry at ${leg.from_name}`,
          street: leg.from_name, distance_m: 0,
          cumulative_distance_m: cumulativeM, icon: 'ferry', coords: first,
        },
        {
          step: 0, type: 'DISEMBARK_FERRY', modifier: 'transfer',
          instruction: `Disembark at ${leg.to_name}`,
          street: leg.to_name, distance_m: 0,
          cumulative_distance_m: cumulativeM + leg.distance_km * 1_000,
          icon: 'ferry', coords: last,
        },
      );
    } else {
      maneuvers.push(...(leg.navigation?.maneuvers ?? []).map(item => ({
        ...item,
        cumulative_distance_m: cumulativeM + (item.cumulative_distance_m ?? 0),
      })));
    }
    cumulativeM += leg.distance_km * 1_000;
  });
  maneuvers.forEach((item, index) => { item.step = index + 1; });
  return {
    schema_version: 1,
    data_source: 'composed_multimodal_journey',
    maneuvers,
    landmarks_along_route: [],
    traffic_lights_count: 0,
    route_narrative_words: legs
      .map(leg => `${leg.from_name} to ${leg.to_name} by ${leg.mode.toLowerCase()}.`)
      .join(' '),
  };
}

/** Browser continuity route used only when the backend cannot answer. */
export function offlineCrossBorderOptimize(
  origin: FreeLocation,
  destination: FreeLocation,
  vehicleType: VehicleType,
  hour: number,
  routePreference: RoutePreference,
  now = new Date(),
): RouteOptimizationResult {
  const originRegion = locationRegion(origin.lat, origin.lng);
  const destinationRegion = locationRegion(destination.lat, destination.lng);
  if (!originRegion || !destinationRegion) {
    throw new Error('Both points must be within Singapore or Batam.');
  }
  if (
    originRegion !== destinationRegion
    && isPassengerFerryIncompatibleVehicle(vehicleType)
  ) {
    throw new Error(PASSENGER_FERRY_TRUCK_MESSAGE);
  }
  const originPoint: [number, number] = [origin.lat, origin.lng];
  const destinationPoint: [number, number] = [destination.lat, destination.lng];
  const departureTimezone = originRegion === 'SINGAPORE'
    ? 'Asia/Singapore' : 'Asia/Jakarta';
  const departure = nextCorridorHour(hour, departureTimezone, now);
  const legs: RouteLeg[] = [];
  let sailings: FerrySchedule[] = [];
  let ferryNote: string | undefined;
  let waitMins = 0;

  if (originRegion === destinationRegion) {
    legs.push(roadLeg(
      originPoint, destinationPoint,
      origin.display_name, destination.display_name,
    ));
  } else {
    const pair = chooseTerminalPair(originPoint, destinationPoint, originRegion);
    const departureTerminal = originRegion === 'SINGAPORE' ? pair.singapore : pair.batam;
    const departureTerminalName = originRegion === 'SINGAPORE'
      ? pair.singaporeName : pair.batamName;
    const arrivalTerminal = originRegion === 'SINGAPORE' ? pair.batam : pair.singapore;
    const arrivalTerminalName = originRegion === 'SINGAPORE'
      ? pair.batamName : pair.singaporeName;
    const firstRoad = roadLeg(
      originPoint, departureTerminal,
      origin.display_name, departureTerminalName,
    );
    const terminalArrival = new Date(
      departure.getTime() + firstRoad.duration_mins * 60_000,
    );
    const ferry = ferryLeg(pair, originRegion, terminalArrival, now);
    firstRoad.vehicle_role = 'FIRST_LAST_MILE_ACCESS';
    const finalRoad = roadLeg(
      arrivalTerminal, destinationPoint,
      arrivalTerminalName, destination.display_name,
    );
    finalRoad.vehicle_role = 'FIRST_LAST_MILE_ACCESS';
    legs.push(
      firstRoad,
      ferry.leg,
      finalRoad,
    );
    sailings = ferry.sailings;
    ferryNote = ferry.note;
    waitMins = ferry.waitMins;
  }

  const roadDistanceKm = Number(legs
    .filter(leg => leg.mode === 'ROAD')
    .reduce((sum, leg) => sum + leg.distance_km, 0).toFixed(2));
  const ferryDistanceKm = Number(legs
    .filter(leg => leg.mode === 'FERRY')
    .reduce((sum, leg) => sum + leg.distance_km, 0).toFixed(2));
  const movementMins = Number(legs
    .reduce((sum, leg) => sum + leg.duration_mins, 0).toFixed(1));
  const profile = vehicleProfile(vehicleType);
  const isMultimodal = legs.some(leg => leg.mode === 'FERRY');
  const result: RouteOptimizationResult = {
    route_type: isMultimodal ? 'MULTIMODAL_FERRY_ROUTE' : 'ROAD_ROUTE',
    corridor: {
      id: `free:${originRegion.toLowerCase()}:${destinationRegion.toLowerCase()}`,
      name: `${origin.display_name} → ${destination.display_name}`,
      origin: 'free-origin', destination: 'free-destination',
      distance_km: Number((roadDistanceKm + ferryDistanceKm).toFixed(2)),
      base_time_mins: movementMins,
      straight_line_km: Number(haversineKm(originPoint, destinationPoint).toFixed(2)),
      detour_ratio: null,
    },
    requested_origin: { ...origin, region: originRegion },
    requested_destination: { ...destination, region: destinationRegion },
    vehicle_type: vehicleType,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    route_preference: routePreference,
    route_preference_profile: routePreferenceProfile(routePreference),
    planned_departure: toCorridorIso(departure, departureTimezone),
    congestion_prediction: {
      current_score: 0, predicted_30min: 0, predicted_60min: 0,
      estimated_delay_mins: 0, status: 'NOT_MODELLED',
      risk_level: 'UNKNOWN', trend: 'STABLE',
    },
    estimated_travel_time_mins: movementMins,
    customs_buffer_mins: waitMins,
    total_eta_mins: Number((movementMins + waitMins).toFixed(1)),
    road_distance_km: roadDistanceKm,
    ferry_distance_km: ferryDistanceKm,
    co2_emissions_kg: Number((roadDistanceKm * profile.co2KgPerKm).toFixed(2)),
    co2_saved_kg: 0,
    emissions_scope: 'Selected road legs only; ferry emissions are not estimated without vessel and occupancy data.',
    optimal_departure: {
      recommended: 'DEPART_NOW', time_saved_mins: 0,
      reason: 'Backend unavailable; showing a deterministic cross-border continuity plan with per-leg limitations.',
    },
    next_matching_ferries: sailings,
    ferry_connection_note: ferryNote,
    route_geometry: combinedGeometry(legs),
    route_data_source: isMultimodal
      ? 'multimodal_offline_estimate' : 'offline_access_estimate',
    route_legs: legs,
    vehicle_transfer_policy: isMultimodal
      ? 'FIRST_LAST_MILE_ONLY' : 'ROAD_JOURNEY',
    vehicle_transfer_note: isMultimodal
      ? PASSENGER_FERRY_TRANSFER_NOTE : null,
    alternative_routes: [],
    alternatives_note: isMultimodal
      ? 'Terminal pair selected by estimated access time plus published crossing duration.'
      : 'No sufficiently distinct local alternative is available offline.',
    navigation: combinedNavigation(legs),
  };
  return result;
}
