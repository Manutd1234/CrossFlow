/**
 * Offline congestion estimate, used only when the backend is unreachable.
 *
 * This mirrors the *training equation* the backend's RandomForest is fitted to
 * (backend/models/congestion_model.py::_train_baseline_model) rather than
 * trying to reproduce the forest itself. The equation is the ground truth the
 * model approximates, so offline and online numbers stay in the same family
 * for a fraction of the complexity.
 *
 * The point of this file is that the hour slider and the weather buttons must
 * keep working when the backend is down. Previously the offline path ignored
 * both, so dragging the slider changed nothing on screen — which reads to an
 * audience as a dead demo.
 */

const CRITICAL_THRESHOLD = 70;
const HEAVY_THRESHOLD = 40;
const DELAY_MINS_AT_FULL_CONGESTION = 28;

export function classifyStatus(score: number): 'SMOOTH' | 'HEAVY' | 'CRITICAL' {
  if (score >= CRITICAL_THRESHOLD) return 'CRITICAL';
  if (score >= HEAVY_THRESHOLD) return 'HEAVY';
  return 'SMOOTH';
}

export function riskFromStatus(status: string): 'LOW' | 'MODERATE' | 'HIGH' {
  return status === 'CRITICAL' ? 'HIGH' : status === 'HEAVY' ? 'MODERATE' : 'LOW';
}

export const delayFromScore = (score: number): number =>
  Math.round((score / 100) * DELAY_MINS_AT_FULL_CONGESTION * 10) / 10;

/** Congestion score 5-96 for a given hour and conditions. */
export function simulateCongestion(
  hourFloat: number,
  isWeekend: number,
  weather: number,
  ferrySurge: number,
  corridorIdx: number
): number {
  const h = ((hourFloat % 24) + 24) % 24;

  // Smooth daily rhythm: quietest ~04:00, busiest late afternoon.
  const diurnal = 0.5 * (1 - Math.cos((2 * Math.PI * (h - 4)) / 24));
  const base = 12 + 20 * diurnal;

  const bump = (centre: number, width: number) =>
    Math.exp(-((h - centre) ** 2) / (2 * width * width));

  const peaks = (34 * bump(8, 1.1) + 42 * bump(18, 1.2)) * (isWeekend ? 0.45 : 1);

  const score = base + peaks + weather * 13 + ferrySurge * 16 + corridorIdx * 3.5;
  return Math.round(Math.min(96, Math.max(5, score)) * 10) / 10;
}

export interface LocalPrediction {
  current_score: number;
  predicted_30min: number;
  predicted_60min: number;
  estimated_delay_mins: number;
  status: 'SMOOTH' | 'HEAVY' | 'CRITICAL';
  risk_level: 'LOW' | 'MODERATE' | 'HIGH';
  trend: 'UPWARD' | 'DOWNWARD' | 'STABLE';
}

export function predictLocal(
  hourFloat: number,
  isWeekend: number,
  weather: number,
  ferrySurge: number,
  corridorIdx: number
): LocalPrediction {
  const current = simulateCongestion(hourFloat, isWeekend, weather, ferrySurge, corridorIdx);
  const in30 = simulateCongestion(hourFloat + 0.5, isWeekend, weather, ferrySurge, corridorIdx);
  const in60 = simulateCongestion(hourFloat + 1, isWeekend, weather, ferrySurge, corridorIdx);
  const status = classifyStatus(current);

  return {
    current_score: current,
    predicted_30min: in30,
    predicted_60min: in60,
    estimated_delay_mins: delayFromScore(current),
    status,
    risk_level: riskFromStatus(status),
    trend: in30 > current + 3 ? 'UPWARD' : in30 < current - 3 ? 'DOWNWARD' : 'STABLE',
  };
}
