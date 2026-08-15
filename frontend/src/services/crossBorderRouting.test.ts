import { describe, expect, it } from 'vitest';
import { FERRY_SEA_ROUTE } from '../data/mockData';
import {
  isSupportedLocation,
  locationRegion,
  offlineCrossBorderOptimize,
  PASSENGER_FERRY_TRANSFER_NOTE,
  PASSENGER_FERRY_TRUCK_MESSAGE,
} from './crossBorderRouting';

describe('cross-border continuity routing', () => {
  it('classifies Singapore and Batam without treating unrelated cities as local', () => {
    expect(locationRegion(1.2840, 103.8513)).toBe('SINGAPORE');
    expect(locationRegion(1.1318, 104.0554)).toBe('BATAM');
    expect(locationRegion(3.1390, 101.6869)).toBeNull();
    expect(isSupportedLocation(1.3644, 103.9915)).toBe(true);
  });

  it('builds road-ferry-road legs from Singapore using a published outbound slot', () => {
    const route = offlineCrossBorderOptimize(
      { lat: 1.2840, lng: 103.8513, display_name: 'Raffles Place Singapore' },
      { lat: 1.0605, lng: 104.0303, display_name: 'Batamindo Industrial Park' },
      'COMMUTER',
      9,
      'BALANCED',
      new Date('2026-08-07T00:30:00Z'),
    );

    expect(route.route_type).toBe('MULTIMODAL_FERRY_ROUTE');
    expect(route.route_legs?.map(leg => leg.mode)).toEqual(['ROAD', 'FERRY', 'ROAD']);
    const ferry = route.route_legs?.[1];
    expect(ferry?.geometry.length).toBeGreaterThan(4);
    expect(ferry?.geometry).toEqual(FERRY_SEA_ROUTE);
    expect(ferry?.geometry_note).toContain('not an observed');
    expect(ferry?.schedule_status).toBe('PUBLISHED_DEPARTURE_SELECTED');
    expect(ferry?.geometry[0]).toEqual([1.2644, 103.8206]);
    expect(ferry?.geometry[ferry.geometry.length - 1]).toEqual([1.1318, 104.0554]);
    expect(ferry?.vehicle_carried_onboard).toBe(false);
    expect(route.next_matching_ferries).toHaveLength(1);
    expect(route.next_matching_ferries[0]).toMatchObject({
      departure_port: 'HarbourFront SG',
      arrival_port: 'Batam Centre',
      departure_timezone: 'Asia/Singapore',
      arrival_timezone: 'Asia/Jakarta',
    });
    expect(route.next_matching_ferries[0].departure_time).toMatch(/\+08:00$/);
    expect(route.ferry_connection_note).toBeUndefined();
    expect(route.vehicle_transfer_policy).toBe('FIRST_LAST_MILE_ONLY');
    expect(route.vehicle_transfer_note).toBe(PASSENGER_FERRY_TRANSFER_NOTE);
    expect(route.route_legs?.[0].vehicle_role).toBe('FIRST_LAST_MILE_ACCESS');
    expect(route.route_legs?.[2].vehicle_role).toBe('FIRST_LAST_MILE_ACCESS');
    expect(route.planned_departure).toMatch(/T09:00:00\.000\+08:00$/);
    expect(route.total_eta_mins).toBeGreaterThan(route.estimated_travel_time_mins);
    expect(route.emissions_scope).toContain('road legs only');
  });

  it('keeps trucks on local roads and rejects passenger-ferry crossings', () => {
    const singapore = {
      lat: 1.2840, lng: 103.8513, display_name: 'Raffles Place',
    };
    const batam = {
      lat: 1.0605, lng: 104.0303, display_name: 'Batamindo',
    };
    expect(() => offlineCrossBorderOptimize(
      singapore, batam, 'CARGO_TRUCK', 9, 'BALANCED',
      new Date('2026-08-07T00:30:00Z'),
    )).toThrow(PASSENGER_FERRY_TRUCK_MESSAGE);

    const local = offlineCrossBorderOptimize(
      batam,
      { lat: 1.1318, lng: 104.0554, display_name: 'Batam Centre' },
      'CARGO_TRUCK',
      9,
      'BALANCED',
      new Date('2026-08-07T00:30:00Z'),
    );
    expect(local.route_type).toBe('ROAD_ROUTE');
    expect(local.vehicle_type).toBe('CARGO_TRUCK');
    expect(local.route_legs?.map(leg => leg.mode)).toEqual(['ROAD']);
  });

  it('preserves a same-island Singapore trip as road-only continuity routing', () => {
    const route = offlineCrossBorderOptimize(
      { lat: 1.2840, lng: 103.8513, display_name: 'Raffles Place' },
      { lat: 1.3644, lng: 103.9915, display_name: 'Changi Airport' },
      'ELECTRIC_CAR',
      11,
      'FASTEST',
      new Date('2026-08-07T00:30:00Z'),
    );

    expect(route.route_type).toBe('ROAD_ROUTE');
    expect(route.route_legs?.map(leg => leg.mode)).toEqual(['ROAD']);
    expect(route.ferry_distance_km).toBe(0);
    expect(route.route_data_source).toBe('offline_access_estimate');
    expect(route.navigation?.maneuvers).toHaveLength(2);
  });
});
