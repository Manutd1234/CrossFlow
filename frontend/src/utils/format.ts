import { Corridor, FerrySchedule, Shipment } from '../types';
import { TRAFFIC_COLORS } from '../theme/mapPalette';

/**
 * Status -> badge class.
 *
 * Every ferry and shipment previously rendered with `badge-smooth`, so a
 * DELAYED_10M sailing and a consignment stuck in CUSTOMS_CLEARANCE both showed
 * up green, as though nothing was wrong.
 */
export const ferryBadgeClass = (status: FerrySchedule['status']): string =>
  status === 'SCHEDULED' ? 'badge-neutral'
    : status === 'ON_TIME' ? 'badge-smooth'
    : status === 'BOARDING' || status === 'FINAL_CALL' ? 'badge-heavy'
      : status === 'DEPARTED' ? 'badge-neutral'
        : 'badge-critical';

export const shipmentBadgeClass = (status: Shipment['status']): string =>
  status === 'IN_TRANSIT' ? 'badge-smooth'
    : status === 'CUSTOMS_CLEARANCE' ? 'badge-heavy'
      : 'badge-neutral';

export const corridorBadgeClass = (status: Corridor['status']): string =>
  `badge-${status.toLowerCase()}`;

export const corridorColor = (status: Corridor['status']): string =>
  status === 'CRITICAL'
    ? TRAFFIC_COLORS.critical
    : status === 'HEAVY'
      ? TRAFFIC_COLORS.heavy
      : TRAFFIC_COLORS.smooth;

/** Underscored enum -> readable text (CUSTOMS_CLEARANCE -> "CUSTOMS CLEARANCE"). */
export const prettyStatus = (status: string): string => status.replace(/_/g, ' ');

/**
 * The API uses "->" in corridor names; the UI shows a nicer glyph.
 *
 * Doing this at render time rather than in the data keeps one canonical format
 * on the wire. Previously the mock data used the glyph and the backend used the
 * arrow, so every corridor name visibly rewrote itself when the backend
 * connected or dropped.
 */
export const prettyArrow = (text: string): string => text.replace(/->/g, '➔');

/**
 * ISO 8601 timestamp -> "HH:MM" **in the timestamp's own offset**.
 *
 * Deliberately not converted to browser-local time. Ferry timestamps retain
 * the explicit local offset of their departure or arrival terminal (+08 for
 * Singapore, +07 for Batam); the UI therefore renders the wall clock encoded
 * by the source instead of silently shifting it to the viewer's timezone.
 *
 * Tolerates a bare "HH:MM" so a stale backend doesn't render "Invalid Date".
 */
export function formatTime(value: string): string {
  if (!value) return '--:--';
  if (!value.includes('T')) return value;

  // "2026-08-07T18:00:00+07:00" -> "18:00", without a timezone round-trip.
  const match = value.match(/T(\d{2}):(\d{2})/);
  if (match) return `${match[1]}:${match[2]}`;

  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** ISO 8601 -> "14:32:05" in the timestamp's own offset, for "updated" readouts. */
export function formatClock(value?: string): string {
  if (!value) return '--:--:--';
  const match = value.match(/T(\d{2}):(\d{2}):(\d{2})/);
  if (match) return `${match[1]}:${match[2]}:${match[3]}`;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '--:--:--';
  return d.toLocaleTimeString([], { hour12: false });
}

/** Published schedule verification timestamp, kept in Batam/WIB wall time. */
export function formatScheduleVerifiedAt(value?: string): string {
  if (!value) return 'verification date unavailable';
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T/);
  if (!match) return value;
  const month = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ][Number(match[2]) - 1];
  if (!month) return value;
  return `${Number(match[3])} ${month} ${match[1]}, ${formatTime(value)} WIB`;
}

/** "in 42 min" / "boarding now" / "departed". */
export function relativeDeparture(minutes?: number): string {
  if (minutes === undefined || minutes === null) return '';
  if (minutes < 0) return 'departed';
  if (minutes === 0) return 'departing now';
  if (minutes < 60) return `in ${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `in ${h}h ${m}m` : `in ${h}h`;
}

/** "Mukakuning Industrial -> Batam Centre Terminal" -> "Mukakuning Industrial". */
export const shortCorridorName = (name: string): string =>
  name.split(/\s*(?:->|➔)\s*/)[0].trim();
