import { describe, expect, it } from 'vitest';
import {
  PUBLISHED_FERRY_TIMETABLE,
  PUBLISHED_FERRY_TIMETABLE_METADATA,
  offlineFerries,
} from './mockData';

function departureTimesFor(serviceId: string, after: Date): string[] {
  return offlineFerries(after, after, 24)
    .filter(ferry => ferry.sailing_id?.startsWith(`${serviceId}-`))
    .map(ferry => ferry.departure_time.match(/T(\d{2}:\d{2})/)?.[1] ?? '');
}

describe('published ferry timetable browser snapshot', () => {
  it('expands the exact weekday departure slots from the canonical JSON', () => {
    const mondayMidnight = new Date('2026-08-09T17:00:00.000Z');

    expect(departureTimesFor('majestic-bct-tm', mondayMidnight)).toEqual([
      '06:15', '08:25', '12:00', '15:30', '18:00',
    ]);
    expect(PUBLISHED_FERRY_TIMETABLE.services.find(
      service => service.service_id === 'majestic-bct-tm',
    )?.daily_departures).toEqual([
      '06:15', '08:25', '12:00', '15:30', '18:00',
    ]);
  });

  it('includes only the published weekend additions on Saturday and Sunday', () => {
    const sundayMidnight = new Date('2026-08-08T17:00:00.000Z');

    expect(departureTimesFor('majestic-bct-tm', sundayMidnight)).toEqual([
      '06:15', '08:25', '09:40', '12:00', '13:00', '15:30', '17:00', '18:00', '19:50',
    ]);
  });

  it('preserves Singapore departure time and Batam arrival time for Sindo', () => {
    const beforeFirstDeparture = new Date('2026-08-06T23:40:00.000Z');
    const sailings = offlineFerries(
      beforeFirstDeparture, beforeFirstDeparture, 3,
    );
    const harbourFrontToBatamCentre = sailings.find(
      ferry => ferry.sailing_id?.startsWith('sindo-hf-bct-'),
    );

    expect(harbourFrontToBatamCentre).toMatchObject({
      departure_port: 'HarbourFront SG',
      arrival_port: 'Batam Centre',
      departure_time: '2026-08-07T08:00:00+08:00',
      arrival_time: '2026-08-07T08:00:00+07:00',
      departure_timezone: 'Asia/Singapore',
      arrival_timezone: 'Asia/Jakarta',
      estimated_crossing_mins: 60,
      schedule_source_url: 'https://app.sindoferry.com.sg/schedule/',
      booking_url: 'https://app.sindoferry.com.sg/',
    });
    expect(sailings.map(ferry => Date.parse(ferry.departure_time))).toEqual(
      [...sailings]
        .map(ferry => Date.parse(ferry.departure_time))
        .sort((a, b) => a - b),
    );
  });

  it('publishes schedule provenance without inventing live operating details', () => {
    const after = new Date('2026-08-09T22:30:00.000Z');
    const sailings = offlineFerries(after, after, 12);

    expect(PUBLISHED_FERRY_TIMETABLE_METADATA).toMatchObject({
      snapshot_id: 'published-batam-singapore-timetables-calendar-audited-2026-08-13',
      status: 'published_schedule_snapshot',
      last_verified_at: '2026-08-13T00:30:50+07:00',
    });
    expect(sailings.length).toBeGreaterThan(0);
    expect(sailings.every(ferry => (
      ferry.status === 'SCHEDULED'
      && ferry.ferry_name === ferry.operator
      && ferry.available_seats === null
      && ferry.capacity === null
      && ferry.live_status_available === false
      && ferry.gate_status === undefined
      && ferry.berth === undefined
      && ferry.speed_knots === undefined
      && ferry.data_source === 'official_timetable_snapshot'
      && ferry.schedule_source_url?.startsWith('https://')
    ))).toBe(true);
  });
});
