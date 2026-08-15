import { describe, expect, it } from 'vitest';
import { batamParts, batamScheduleInstant, nextBatamHour, toBatamIso } from './batamTime';

describe('Batam time helpers', () => {
  it('keeps Batam wall-clock hours independent of the test runner timezone', () => {
    const instant = new Date('2026-08-09T06:00:00.000Z');
    expect(batamParts(instant).hour).toBe(13);
    expect(toBatamIso(instant)).toBe('2026-08-09T13:00:00.000+07:00');
  });

  it('rolls a passed departure hour to the next Batam day', () => {
    const now = new Date('2026-08-09T08:30:00.000Z'); // 15:30 in Batam
    expect(toBatamIso(nextBatamHour(14, now))).toBe('2026-08-10T14:00:00.000+07:00');
  });

  it('constructs ferry schedule slots in Batam time', () => {
    const reference = new Date('2026-08-09T22:30:00.000Z'); // 05:30 in Batam
    expect(toBatamIso(batamScheduleInstant(reference, 0, 6 * 60)))
      .toBe('2026-08-10T06:00:00.000+07:00');
  });
});
