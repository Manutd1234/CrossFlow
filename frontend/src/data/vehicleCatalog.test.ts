import { describe, expect, it } from 'vitest';
import { VEHICLE_CATALOG, vehicleProfileSnapshot } from './vehicleCatalog';

describe('shared vehicle catalog', () => {
  it('keeps the public IDs unique and in backend contract order', () => {
    const ids = VEHICLE_CATALOG.map((profile) => profile.id);
    expect(ids).toEqual([
      'COMMUTER',
      'ELECTRIC_CAR',
      'MOTORCYCLE',
      'EXPRESS_VAN',
      'MINIBUS',
      'CITY_BUS',
      'LIGHT_TRUCK',
      'CARGO_TRUCK',
    ]);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('mirrors every numeric routing and emissions assumption published by the backend', () => {
    const tuples = VEHICLE_CATALOG.map((profile) => [
      profile.maxSpeedKph,
      profile.speedFactor,
      profile.congestionSensitivity,
      profile.weatherSensitivity,
      profile.residentialPenalty,
      profile.unclassifiedPenalty,
      profile.tertiaryPenalty,
      profile.linkPenalty,
      profile.turnPenaltySeconds,
      profile.shortManeuverPenaltySeconds,
      profile.signalDelaySeconds,
      profile.terminalBufferMins,
      profile.co2KgPerKm,
      profile.idleCo2KgPerHour,
    ]);
    expect(tuples).toEqual([
      [80, 1, 1, 1, 1.08, 1.05, 1.02, 1.03, 6, 8, 18, 0, 0.21, 1.8],
      [80, 1, 0.92, 1.05, 1.08, 1.05, 1.02, 1.03, 5, 7, 17, 0, 0.045, 0.1],
      [75, 1.03, 0.62, 1.45, 1, 1, 1, 1, 3, 2, 10, 5, 0.09, 0.45],
      [75, 0.94, 1.05, 1.08, 1.18, 1.12, 1.05, 1.06, 8, 12, 20, 10, 0.27, 2.2],
      [70, 0.9, 1.08, 1.1, 1.25, 1.16, 1.08, 1.1, 10, 15, 22, 0, 0.32, 2.4],
      [60, 0.78, 1.15, 1.12, 1.7, 1.45, 1.16, 1.18, 14, 24, 28, 0, 0.72, 3.5],
      [68, 0.86, 1.12, 1.14, 1.42, 1.28, 1.12, 1.14, 12, 19, 25, 18, 0.48, 2.8],
      [55, 0.72, 1.22, 1.22, 2.2, 1.8, 1.28, 1.25, 17, 32, 32, 25, 1.05, 4],
    ]);
  });

  it('serializes the catalog using the backend vehicle_profile field names', () => {
    expect(vehicleProfileSnapshot('LIGHT_TRUCK')).toMatchObject({
      id: 'LIGHT_TRUCK',
      max_speed_kph: 68,
      road_preferences: {
        residential: 1.42,
        unclassified: 1.28,
        tertiary: 1.12,
        link: 1.14,
      },
      customs_buffer_mins: 18,
      emissions_kg_per_km: 0.48,
    });
  });
});
