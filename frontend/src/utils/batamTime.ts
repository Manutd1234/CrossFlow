export type CorridorTimezone = 'Asia/Jakarta' | 'Asia/Singapore';

const CORRIDOR_OFFSET_MINUTES: Record<CorridorTimezone, number> = {
  'Asia/Jakarta': 7 * 60,
  'Asia/Singapore': 8 * 60,
};

export interface BatamDateParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  weekday: number;
}

/** Calendar fields for an instant in either corridor terminal timezone. */
export function corridorParts(
  value: Date,
  timezone: CorridorTimezone,
): BatamDateParts {
  const shifted = new Date(
    value.getTime() + CORRIDOR_OFFSET_MINUTES[timezone] * 60_000,
  );
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth(),
    day: shifted.getUTCDate(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
    weekday: shifted.getUTCDay(),
  };
}

/** Calendar fields for an instant in Batam (UTC+7, with no daylight saving). */
export function batamParts(value: Date): BatamDateParts {
  return corridorParts(value, 'Asia/Jakarta');
}

/** Build an instant from local calendar fields in a supported terminal zone. */
export function fromCorridorParts(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute = 0,
  timezone: CorridorTimezone = 'Asia/Jakarta',
): Date {
  return new Date(
    Date.UTC(year, month, day, hour, minute)
      - CORRIDOR_OFFSET_MINUTES[timezone] * 60_000,
  );
}

/** Build an instant from Batam-local calendar fields. */
export function fromBatamParts(
  year: number,
  month: number,
  day: number,
  hour: number,
  minute = 0,
): Date {
  return fromCorridorParts(year, month, day, hour, minute, 'Asia/Jakarta');
}

/** The next occurrence of a selected departure hour in a terminal timezone. */
export function nextCorridorHour(
  hour: number,
  timezone: CorridorTimezone,
  now: Date = new Date(),
): Date {
  const parts = corridorParts(now, timezone);
  let candidate = fromCorridorParts(
    parts.year, parts.month, parts.day, hour, 0, timezone,
  );
  if (candidate.getTime() < now.getTime()) {
    candidate = new Date(candidate.getTime() + 24 * 60 * 60 * 1000);
  }
  return candidate;
}

/** The next occurrence of a selected Batam departure hour. */
export function nextBatamHour(hour: number, now: Date = new Date()): Date {
  return nextCorridorHour(hour, 'Asia/Jakarta', now);
}

/** ISO 8601 string that retains the selected terminal's local wall clock. */
export function toCorridorIso(
  value: Date,
  timezone: CorridorTimezone,
): string {
  const offsetMinutes = CORRIDOR_OFFSET_MINUTES[timezone];
  const shifted = new Date(value.getTime() + offsetMinutes * 60_000);
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const absoluteOffset = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(absoluteOffset / 60)).padStart(2, '0')}:${String(absoluteOffset % 60).padStart(2, '0')}`;
  return `${shifted.toISOString().slice(0, -1)}${offset}`;
}

/** ISO 8601 string that retains Batam's own +07:00 wall-clock fields. */
export function toBatamIso(value: Date): string {
  return toCorridorIso(value, 'Asia/Jakarta');
}

/** A scheduled local minute-of-day relative to a reference instant. */
export function corridorScheduleInstant(
  reference: Date,
  dayOffset: number,
  minuteOfDay: number,
  timezone: CorridorTimezone,
): Date {
  const parts = corridorParts(reference, timezone);
  const midnight = fromCorridorParts(
    parts.year, parts.month, parts.day, 0, 0, timezone,
  );
  return new Date(
    midnight.getTime() + (dayOffset * 1440 + minuteOfDay) * 60_000,
  );
}

/** A scheduled Batam-local minute-of-day relative to a reference instant. */
export function batamScheduleInstant(reference: Date, dayOffset: number, minuteOfDay: number): Date {
  return corridorScheduleInstant(
    reference, dayOffset, minuteOfDay, 'Asia/Jakarta',
  );
}
