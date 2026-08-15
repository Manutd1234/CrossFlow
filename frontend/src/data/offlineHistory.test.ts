import { describe, expect, it } from 'vitest';
import { predictLocal } from '../services/localForecast';
import { browserModeledHistoricalProfile } from './offlineHistory';

describe('browserModeledHistoricalProfile', () => {
  const mondayInBatam = new Date('2026-08-10T05:00:00.000Z');

  it('builds deterministic Batam-local hourly and daily modeled profiles', () => {
    const first = browserModeledHistoricalProfile('corridor-3', 2, mondayInBatam);
    const second = browserModeledHistoricalProfile('corridor-3', 2, mondayInBatam);

    expect(second).toEqual(first);
    expect(first.corridor_id).toBe('corridor-3');
    expect(first.days_requested).toBe(2);
    expect(first.hourly_profile).toHaveLength(24);
    expect(first.hourly_profile.map(bucket => bucket.hour)).toEqual(
      Array.from({ length: 24 }, (_, hour) => hour),
    );
    expect(first.weekly_trend.map(day => day.date)).toEqual([
      '2026-08-09',
      '2026-08-10',
    ]);

    const sundayEight = predictLocal(8, 1, 0, 0, 2).current_score;
    const mondayEight = predictLocal(8, 0, 0, 0, 2).current_score;
    expect(first.hourly_profile[8].avg_score)
      .toBe(Math.round(((sundayEight + mondayEight) / 2) * 10) / 10);
  });

  it('labels every value as an unobserved browser baseline', () => {
    const profile = browserModeledHistoricalProfile('corridor-1', 7, mondayInBatam);

    expect(profile.history_metadata).toMatchObject({
      observed: false,
      source: 'browser_modelled_baseline',
      model: 'crossflow_local_forecast_equation_v1',
      reference_time: '2026-08-10T12:00:00.000+07:00',
    });
    expect(profile.history_metadata.limitations.join(' ')).toContain('no observed');
    expect(profile.hourly_profile.every(bucket => bucket.sample_count === 0)).toBe(true);
    expect(profile.weekly_trend.every(day => day.sample_count === 0)).toBe(true);
  });

  it('validates the history window, corridor, and reference instant', () => {
    expect(browserModeledHistoricalProfile('corridor-1', 1, mondayInBatam).weekly_trend)
      .toHaveLength(1);
    expect(browserModeledHistoricalProfile('corridor-1', 30, mondayInBatam).weekly_trend)
      .toHaveLength(30);
    expect(() => browserModeledHistoricalProfile('corridor-1', 0, mondayInBatam))
      .toThrow(/between 1 and 30/);
    expect(() => browserModeledHistoricalProfile('corridor-1', 31, mondayInBatam))
      .toThrow(/between 1 and 30/);
    expect(() => browserModeledHistoricalProfile('corridor-1', 1.5, mondayInBatam))
      .toThrow(/between 1 and 30/);
    expect(() => browserModeledHistoricalProfile('unknown-corridor', 7, mondayInBatam))
      .toThrow(/Unknown corridor id/);
    expect(() => browserModeledHistoricalProfile('corridor-1', 7, new Date('invalid')))
      .toThrow(/valid Date/);
  });
});
