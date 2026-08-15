import type { HistoricalProfile } from '../types';
import { predictLocal } from '../services/localForecast';
import { batamParts, batamScheduleInstant, toBatamIso } from '../utils/batamTime';
import { CORRIDOR_INDEX } from './mockData';

export interface BrowserModeledHistoryMetadata {
  observed: false;
  source: 'browser_modelled_baseline';
  model: 'crossflow_local_forecast_equation_v1';
  methodology: string;
  limitations: string[];
  reference_time: string;
}

export interface BrowserModeledHistoricalProfile extends HistoricalProfile {
  history_metadata: BrowserModeledHistoryMetadata;
}

const HOURS_PER_DAY = 24;

const roundOneDecimal = (value: number): number => Math.round(value * 10) / 10;

/**
 * Builds a deterministic browser-only baseline when persisted history cannot
 * be reached. Values are model evaluations, never observed traffic samples.
 */
export function browserModeledHistoricalProfile(
  corridorId: string,
  days = 7,
  at: Date = new Date(),
): BrowserModeledHistoricalProfile {
  if (!Number.isInteger(days) || days < 1 || days > 30) {
    throw new RangeError('days must be an integer between 1 and 30');
  }
  if (!Object.prototype.hasOwnProperty.call(CORRIDOR_INDEX, corridorId)) {
    throw new RangeError(`Unknown corridor id: ${corridorId}`);
  }
  if (!(at instanceof Date) || !Number.isFinite(at.getTime())) {
    throw new TypeError('at must be a valid Date');
  }

  const corridorIndex = CORRIDOR_INDEX[corridorId];
  const modeledDays = Array.from({ length: days }, (_, index) => {
    const dayOffset = index - (days - 1);
    const dayStart = batamScheduleInstant(at, dayOffset, 0);
    const weekday = batamParts(dayStart).weekday;
    const isWeekend = weekday === 0 || weekday === 6 ? 1 : 0;
    const scores = Array.from({ length: HOURS_PER_DAY }, (_, hour) => (
      predictLocal(hour, isWeekend, 0, 0, corridorIndex).current_score
    ));

    return {
      date: toBatamIso(dayStart).slice(0, 10),
      scores,
    };
  });

  return {
    corridor_id: corridorId,
    days_requested: days,
    hourly_profile: Array.from({ length: HOURS_PER_DAY }, (_, hour) => ({
      hour,
      avg_score: roundOneDecimal(
        modeledDays.reduce((total, day) => total + day.scores[hour], 0) / days,
      ),
      // Backend sample counts represent persisted observations. Model
      // evaluations must remain zero so the UI cannot call them readings.
      sample_count: 0,
    })),
    weekly_trend: modeledDays.map(day => ({
      date: day.date,
      avg_score: roundOneDecimal(
        day.scores.reduce((total, score) => total + score, 0) / HOURS_PER_DAY,
      ),
      sample_count: 0,
    })),
    history_metadata: {
      observed: false,
      source: 'browser_modelled_baseline',
      model: 'crossflow_local_forecast_equation_v1',
      methodology: 'Deterministic 24-hour clear-weather baseline from the local congestion equation, averaged across Batam-local calendar days.',
      limitations: [
        'Modelled estimates only; no observed or persisted traffic measurements are included.',
        'Assumes clear weather and no ferry-arrival surge.',
        'Does not account for live traffic, incidents, roadworks, or public-holiday effects.',
      ],
      reference_time: toBatamIso(at),
    },
  };
}
