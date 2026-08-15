import type { RoutePreference, RoutePreferenceProfile, VehicleType } from '../types';

const DISTANCE_PROXY_NOTE = 'Physical metres normalized to seconds at the vehicle maximum speed for objective comparison only.';
const STANDARD_ROAD_NOTE = 'Uses the standard bundled public motor-road graph.';
const ALL_VEHICLES: readonly VehicleType[] = [
  'COMMUTER', 'ELECTRIC_CAR', 'MOTORCYCLE', 'EXPRESS_VAN',
  'MINIBUS', 'CITY_BUS', 'LIGHT_TRUCK', 'CARGO_TRUCK',
];
export const LOCAL_ROUTE_VEHICLES: readonly VehicleType[] = [
  'COMMUTER', 'ELECTRIC_CAR', 'MOTORCYCLE',
];

export const ROUTE_PREFERENCES: Readonly<Record<RoutePreference, RoutePreferenceProfile>> = {
  BALANCED: {
    id: 'BALANCED',
    name: 'Balanced',
    description: 'Balance modeled ETA and road suitability.',
    component_weights: {
      distance_proxy_s: 0,
      free_flow_s: 1,
      congestion_delay_s: 1,
      weather_delay_s: 1,
      maneuver_delay_s: 1,
      road_suitability_penalty_s: 1,
    },
    objective_cost_unit: 'weighted_seconds',
    eligible_vehicle_types: ALL_VEHICLES,
    road_scope: 'STANDARD',
    road_scope_note: STANDARD_ROAD_NOTE,
    distance_proxy_note: DISTANCE_PROXY_NOTE,
  },
  FASTEST: {
    id: 'FASTEST',
    name: 'Fastest',
    description: 'Minimize modeled travel time.',
    component_weights: {
      distance_proxy_s: 0,
      free_flow_s: 1,
      congestion_delay_s: 1,
      weather_delay_s: 1,
      maneuver_delay_s: 1,
      road_suitability_penalty_s: 0,
    },
    objective_cost_unit: 'weighted_seconds',
    eligible_vehicle_types: ALL_VEHICLES,
    road_scope: 'STANDARD',
    road_scope_note: STANDARD_ROAD_NOTE,
    distance_proxy_note: DISTANCE_PROXY_NOTE,
  },
  SHORTEST: {
    id: 'SHORTEST',
    name: 'Shortest',
    description: 'Minimize physical road distance.',
    component_weights: {
      distance_proxy_s: 1,
      free_flow_s: 0,
      congestion_delay_s: 0,
      weather_delay_s: 0,
      maneuver_delay_s: 0,
      road_suitability_penalty_s: 0,
    },
    objective_cost_unit: 'weighted_seconds',
    eligible_vehicle_types: ALL_VEHICLES,
    road_scope: 'STANDARD',
    road_scope_note: STANDARD_ROAD_NOTE,
    distance_proxy_note: DISTANCE_PROXY_NOTE,
  },
  EASY: {
    id: 'EASY',
    name: 'Easy',
    description: 'Prefer through roads with fewer difficult maneuvers.',
    component_weights: {
      distance_proxy_s: 0,
      free_flow_s: 1,
      congestion_delay_s: 1,
      weather_delay_s: 1,
      maneuver_delay_s: 4,
      road_suitability_penalty_s: 2.5,
    },
    objective_cost_unit: 'weighted_seconds',
    eligible_vehicle_types: ALL_VEHICLES,
    road_scope: 'STANDARD',
    road_scope_note: STANDARD_ROAD_NOTE,
    distance_proxy_note: DISTANCE_PROXY_NOTE,
  },
  LOCAL: {
    id: 'LOCAL',
    name: 'Local Shortcuts',
    description: 'Seek compact routes over mapped public residential roads.',
    component_weights: {
      distance_proxy_s: 1,
      free_flow_s: 0.15,
      congestion_delay_s: 0.65,
      weather_delay_s: 0.3,
      maneuver_delay_s: 0.15,
      road_suitability_penalty_s: 0,
    },
    objective_cost_unit: 'weighted_seconds',
    eligible_vehicle_types: LOCAL_ROUTE_VEHICLES,
    road_scope: 'MAPPED_PUBLIC_LOCAL',
    road_scope_note: 'Mapped OSM residential motor roads may be selected; service alleys, drains and footpaths are absent, and lane width or vehicle clearance is not verified.',
    distance_proxy_note: DISTANCE_PROXY_NOTE,
  },
};

export const ROUTE_PREFERENCE_OPTIONS = Object.values(ROUTE_PREFERENCES);

export function routePreferenceProfile(preference: RoutePreference): RoutePreferenceProfile {
  return ROUTE_PREFERENCES[preference];
}

export function isRoutePreferenceAvailable(
  preference: RoutePreference,
  vehicleType: VehicleType,
): boolean {
  return ROUTE_PREFERENCES[preference].eligible_vehicle_types.includes(vehicleType);
}
