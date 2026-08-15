import type { VehicleProfileSnapshot, VehicleType } from '../types';

export type VehicleGroup = 'Passenger' | 'Delivery' | 'Public transport' | 'Freight';
export type VehicleIconKey = 'car' | 'electric' | 'motorcycle' | 'van' | 'minibus' | 'bus' | 'truck';

export interface VehicleProfile {
  id: VehicleType;
  label: string;
  shortLabel: string;
  description: string;
  routingAssumption: string;
  group: VehicleGroup;
  icon: VehicleIconKey;
  maxSpeedKph: number;
  /** Relative cruise speed applied to the road-class speed table. */
  speedFactor: number;
  /** Relative sensitivity to demand-related delay. */
  congestionSensitivity: number;
  /** Relative sensitivity to rain and storm speed reductions. */
  weatherSensitivity: number;
  /** Generalized-cost aversion to local/residential streets; always >= 1. */
  residentialPenalty: number;
  unclassifiedPenalty: number;
  tertiaryPenalty: number;
  linkPenalty: number;
  turnPenaltySeconds: number;
  shortManeuverPenaltySeconds: number;
  signalDelaySeconds: number;
  terminalBufferMins: number;
  co2KgPerKm: number;
  idleCo2KgPerHour: number;
}

/**
 * One catalog drives request IDs, route weighting, the selector and comparison.
 * Values are scenario assumptions—not legal access rules or live speed limits.
 */
export const VEHICLE_CATALOG = [
  {
    id: 'COMMUTER', label: 'Car / taxi', shortLabel: 'Car',
    description: 'Balanced everyday road routing',
    routingAssumption: 'Balanced speeds and public-road access; no restricted-road privileges.',
    group: 'Passenger', icon: 'car', maxSpeedKph: 80, speedFactor: 1,
    congestionSensitivity: 1, weatherSensitivity: 1, residentialPenalty: 1.08,
    unclassifiedPenalty: 1.05, tertiaryPenalty: 1.02, linkPenalty: 1.03,
    turnPenaltySeconds: 6, shortManeuverPenaltySeconds: 8, signalDelaySeconds: 18,
    terminalBufferMins: 0, co2KgPerKm: 0.21, idleCo2KgPerHour: 1.8,
  },
  {
    id: 'ELECTRIC_CAR', label: 'Electric car', shortLabel: 'EV',
    description: 'Passenger EV with lower operational CO₂',
    routingAssumption: 'Uses the same public roads as a car; charging stops are not modelled.',
    group: 'Passenger', icon: 'electric', maxSpeedKph: 80, speedFactor: 1,
    congestionSensitivity: 0.92, weatherSensitivity: 1.05, residentialPenalty: 1.08,
    unclassifiedPenalty: 1.05, tertiaryPenalty: 1.02, linkPenalty: 1.03,
    turnPenaltySeconds: 5, shortManeuverPenaltySeconds: 7, signalDelaySeconds: 17,
    terminalBufferMins: 0, co2KgPerKm: 0.045, idleCo2KgPerHour: 0.1,
  },
  {
    id: 'MOTORCYCLE', label: 'Motorcycle', shortLabel: 'Motorcycle',
    description: 'Agile two-wheeler, weather-sensitive',
    routingAssumption: 'Public-road routing only; lane filtering and motorcycle-only access are not modelled.',
    group: 'Passenger', icon: 'motorcycle', maxSpeedKph: 75, speedFactor: 1.03,
    congestionSensitivity: 0.62, weatherSensitivity: 1.45, residentialPenalty: 1,
    unclassifiedPenalty: 1, tertiaryPenalty: 1, linkPenalty: 1,
    turnPenaltySeconds: 3, shortManeuverPenaltySeconds: 2, signalDelaySeconds: 10,
    terminalBufferMins: 5, co2KgPerKm: 0.09, idleCo2KgPerHour: 0.45,
  },
  {
    id: 'EXPRESS_VAN', label: 'Express van', shortLabel: 'Van',
    description: 'Time-sensitive commercial delivery',
    routingAssumption: 'Prefers through roads while retaining normal public-road access.',
    group: 'Delivery', icon: 'van', maxSpeedKph: 75, speedFactor: 0.94,
    congestionSensitivity: 1.05, weatherSensitivity: 1.08, residentialPenalty: 1.18,
    unclassifiedPenalty: 1.12, tertiaryPenalty: 1.05, linkPenalty: 1.06,
    turnPenaltySeconds: 8, shortManeuverPenaltySeconds: 12, signalDelaySeconds: 20,
    terminalBufferMins: 10, co2KgPerKm: 0.27, idleCo2KgPerHour: 2.2,
  },
  {
    id: 'MINIBUS', label: 'Minibus / shuttle', shortLabel: 'Minibus',
    description: 'Passenger shuttle with wider turns',
    routingAssumption: 'Adds maneuver burden and favours through roads; stop schedules are not modelled.',
    group: 'Public transport', icon: 'minibus', maxSpeedKph: 70, speedFactor: 0.9,
    congestionSensitivity: 1.08, weatherSensitivity: 1.1, residentialPenalty: 1.25,
    unclassifiedPenalty: 1.16, tertiaryPenalty: 1.08, linkPenalty: 1.1,
    turnPenaltySeconds: 10, shortManeuverPenaltySeconds: 15, signalDelaySeconds: 22,
    terminalBufferMins: 0, co2KgPerKm: 0.32, idleCo2KgPerHour: 2.4,
  },
  {
    id: 'CITY_BUS', label: 'City bus', shortLabel: 'Bus',
    description: 'Large passenger vehicle',
    routingAssumption: 'Favours major roads and penalizes tight turns; transit stops and bus lanes are not modelled.',
    group: 'Public transport', icon: 'bus', maxSpeedKph: 60, speedFactor: 0.78,
    congestionSensitivity: 1.15, weatherSensitivity: 1.12, residentialPenalty: 1.7,
    unclassifiedPenalty: 1.45, tertiaryPenalty: 1.16, linkPenalty: 1.18,
    turnPenaltySeconds: 14, shortManeuverPenaltySeconds: 24, signalDelaySeconds: 28,
    terminalBufferMins: 0, co2KgPerKm: 0.72, idleCo2KgPerHour: 3.5,
  },
  {
    id: 'LIGHT_TRUCK', label: 'Light cargo truck', shortLabel: 'Light truck',
    description: 'Local commercial freight',
    routingAssumption: 'Prefers through roads and adds freight maneuver and terminal handling time.',
    group: 'Freight', icon: 'truck', maxSpeedKph: 68, speedFactor: 0.86,
    congestionSensitivity: 1.12, weatherSensitivity: 1.14, residentialPenalty: 1.42,
    unclassifiedPenalty: 1.28, tertiaryPenalty: 1.12, linkPenalty: 1.14,
    turnPenaltySeconds: 12, shortManeuverPenaltySeconds: 19, signalDelaySeconds: 25,
    terminalBufferMins: 18, co2KgPerKm: 0.48, idleCo2KgPerHour: 2.8,
  },
  {
    id: 'CARGO_TRUCK', label: 'Heavy freight truck', shortLabel: 'Cargo truck',
    description: 'Heavy freight and terminal handling',
    routingAssumption: 'Strongly favours major roads; height, weight and turn restrictions are not in the bundled graph.',
    group: 'Freight', icon: 'truck', maxSpeedKph: 55, speedFactor: 0.72,
    congestionSensitivity: 1.22, weatherSensitivity: 1.22, residentialPenalty: 2.2,
    unclassifiedPenalty: 1.8, tertiaryPenalty: 1.28, linkPenalty: 1.25,
    turnPenaltySeconds: 17, shortManeuverPenaltySeconds: 32, signalDelaySeconds: 32,
    terminalBufferMins: 25, co2KgPerKm: 1.05, idleCo2KgPerHour: 4,
  },
] as const satisfies readonly VehicleProfile[];

export const VEHICLE_PROFILE_BY_ID = new Map<VehicleType, VehicleProfile>(
  VEHICLE_CATALOG.map((profile) => [profile.id, profile]),
);

export function isVehicleType(value: string): value is VehicleType {
  return VEHICLE_PROFILE_BY_ID.has(value as VehicleType);
}

export function vehicleProfile(value: string): VehicleProfile {
  return VEHICLE_PROFILE_BY_ID.get(value as VehicleType) ?? VEHICLE_CATALOG[0];
}

export function vehicleProfileSnapshot(value: string): VehicleProfileSnapshot {
  const profile = vehicleProfile(value);
  return {
    id: profile.id,
    name: profile.label,
    max_speed_kph: profile.maxSpeedKph,
    speed_factor: profile.speedFactor,
    congestion_sensitivity: profile.congestionSensitivity,
    weather_sensitivity: profile.weatherSensitivity,
    road_preferences: {
      residential: profile.residentialPenalty,
      unclassified: profile.unclassifiedPenalty,
      tertiary: profile.tertiaryPenalty,
      link: profile.linkPenalty,
    },
    turn_penalty_s: profile.turnPenaltySeconds,
    short_maneuver_penalty_s: profile.shortManeuverPenaltySeconds,
    signal_delay_s: profile.signalDelaySeconds,
    customs_buffer_mins: profile.terminalBufferMins,
    emissions_kg_per_km: profile.co2KgPerKm,
    idle_emissions_kg_per_hour: profile.idleCo2KgPerHour,
    assumptions_source: 'CrossFlow planning profile over OSM road classes',
    legal_restrictions_note: 'Uses the same public motor-road graph for every profile; OSM turn restrictions and vehicle height/weight clearance are not modeled.',
  };
}
