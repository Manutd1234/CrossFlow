import React, { useEffect, useId, useState } from 'react';
import { createPortal } from 'react-dom';
import { Activity, ChartNoAxesColumn, Clock, RefreshCw, TrendingUp, TriangleAlert } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import {
  BrowserHistoryMetadata, Corridor, Fetched, HistoricalProfile, HourlyBucket,
} from '../../types';
import { fetchHistoricalCongestion } from '../../services/api';
import { batamParts } from '../../utils/batamTime';
import { formatScheduleVerifiedAt } from '../../utils/format';

interface CongestionHeatmapProps {
  corridors: Corridor[];
}

const HOUR_LABELS = [
  '12am', '1am', '2am', '3am', '4am', '5am', '6am', '7am', '8am', '9am', '10am', '11am',
  '12pm', '1pm', '2pm', '3pm', '4pm', '5pm', '6pm', '7pm', '8pm', '9pm', '10pm', '11pm',
];

const TREND_VIEWBOX_WIDTH = 100;
const TREND_VIEWBOX_HEIGHT = 100;
const TREND_PLOT_BOTTOM = 100;
const CHART_MAX_DATA_HEIGHT_RATIO = 0.8;
const HOURLY_MAX_BAR_HEIGHT_PX = 160;
const TOOLTIP_CURSOR_OFFSET_PX = 10;
const TOOLTIP_VIEWPORT_WIDTH_PX = 210;
const TOOLTIP_VIEWPORT_HEIGHT_PX = 74;

interface HourlyTooltipState {
  bucket: HourlyBucket;
  x: number;
  y: number;
}

function scoreToTone(score: number | null): string {
  if (score === null) return 'is-empty';
  if (score >= 70) return 'is-critical';
  if (score >= 40) return 'is-heavy';
  return 'is-smooth';
}

function scoreToHeight(score: number | null, maxScore: number, maxPx: number): number {
  if (score === null) return 4;
  return Math.max(4, Math.round((score / maxScore) * maxPx));
}

function isBrowserHistoryMetadata(
  metadata: HistoricalProfile['history_metadata'],
): metadata is BrowserHistoryMetadata {
  return 'source' in metadata && metadata.source === 'browser_modelled_baseline';
}

export const CongestionHeatmap: React.FC<CongestionHeatmapProps> = ({ corridors }) => {
  const trendGradientId = `operations-history-trend-${useId().replace(/:/g, '')}`;
  const [selectedCorridor, setSelectedCorridor] = useState<string>(
    corridors[0]?.id ?? 'corridor-1',
  );
  const [historyResult, setHistoryResult] = useState<Fetched<HistoricalProfile> | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(7);
  const [error, setError] = useState<string | null>(null);
  const [hourlyTooltip, setHourlyTooltip] = useState<HourlyTooltipState | null>(null);

  const updateHourlyTooltip = (
    event: React.MouseEvent<HTMLDivElement>,
    bucket: HourlyBucket,
  ) => {
    setHourlyTooltip({
      bucket,
      x: Math.max(8, Math.min(
        event.clientX + TOOLTIP_CURSOR_OFFSET_PX,
        window.innerWidth - TOOLTIP_VIEWPORT_WIDTH_PX,
      )),
      y: Math.max(8, Math.min(
        event.clientY + TOOLTIP_CURSOR_OFFSET_PX,
        window.innerHeight - TOOLTIP_VIEWPORT_HEIGHT_PX,
      )),
    });
  };

  const activeCorridorId = corridors.some(c => c.id === selectedCorridor)
    ? selectedCorridor
    : corridors[0]?.id ?? selectedCorridor;

  useEffect(() => {
    let cancelled = false;
    const loadProfile = async () => {
      setLoading(true);
      try {
        const res = await fetchHistoricalCongestion(activeCorridorId, days);
        if (cancelled) return;
        setHistoryResult(res);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Historical congestion could not be loaded.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void loadProfile();
    return () => {
      cancelled = true;
    };
  }, [activeCorridorId, days]);

  const profile = historyResult?.data ?? null;
  const displayedCorridorId = profile?.corridor_id ?? activeCorridorId;
  const displayedDays = profile?.days_requested ?? days;
  const corridor = corridors.find(c => c.id === displayedCorridorId);
  const hourly = profile?.hourly_profile ?? [];
  // The API currently returns up to 30 daily points for every request, while
  // the history filter controls `days_requested`. Trim the plotted series so
  // the Daily Trend always represents the selected window as well.
  const weekly = (profile?.weekly_trend ?? []).slice(-displayedDays);
  const hasHourlyData = hourly.some(bucket => bucket.avg_score !== null);
  const peakHour = hourly.reduce(
    (best, b) => (b.avg_score ?? 0) > (best.avg_score ?? 0) ? b : best,
    hourly[0] ?? { hour: 0, avg_score: 0 },
  );
  const quietHour = hourly.reduce(
    (best, b) => ((b.avg_score ?? 100) < (best.avg_score ?? 100) && (b.avg_score !== null)) ? b : best,
    hourly[0] ?? { hour: 0, avg_score: 0 },
  );

  const maxHourlyScore = Math.max(...hourly.map(bucket => bucket.avg_score ?? 0), 1);
  const maxWeeklyScore = Math.max(...weekly.map(day => day.avg_score), 1);
  const trendPoints = weekly.map((day, index) => {
    const x = weekly.length <= 1
      ? TREND_VIEWBOX_WIDTH / 2
      : (index / (weekly.length - 1)) * TREND_VIEWBOX_WIDTH;
    const normalizedScore = Math.max(0, Math.min(1, day.avg_score / maxWeeklyScore));
    const y = TREND_PLOT_BOTTOM
      - normalizedScore * TREND_VIEWBOX_HEIGHT * CHART_MAX_DATA_HEIGHT_RATIO;
    return { x, y };
  });
  const trendLinePath = trendPoints.map((point, index) => (
    `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`
  )).join(' ');
  const trendAreaPath = trendPoints.length > 0
    ? `${trendLinePath} L ${TREND_VIEWBOX_WIDTH} ${TREND_PLOT_BOTTOM} L 0 ${TREND_PLOT_BOTTOM} Z`
    : '';
  const currentBatamHour = batamParts(new Date()).hour;
  const historyMetadata = profile?.history_metadata;
  const browserMetadata = historyMetadata && isBrowserHistoryMetadata(historyMetadata)
    ? historyMetadata
    : null;
  const apiMetadata = historyMetadata && !isBrowserHistoryMetadata(historyMetadata)
    ? historyMetadata
    : null;
  const apiSampleCount = apiMetadata
    ? Object.values(apiMetadata.source_counts).reduce((total, count) => total + count, 0)
    : 0;
  const observedSampleCount = apiMetadata
    ? Object.values(apiMetadata.sources).reduce(
      (total, item) => total + (item.observed ? item.sample_count : 0),
      0,
    )
    : 0;
  const historyBadge = !historyResult
    ? (loading ? 'LOADING HISTORY' : 'HISTORY UNAVAILABLE')
    : browserMetadata
      ? 'BROWSER-MODELLED BASELINE'
      : apiMetadata?.observed
        ? 'OBSERVED HISTORY'
        : apiMetadata?.contains_observed_samples
          ? 'MIXED HISTORY'
          : 'API-MODELLED HISTORY';
  const dataSourceValue = browserMetadata
    ? 'Browser model'
    : apiMetadata?.observed
      ? 'Observed'
      : apiMetadata?.contains_observed_samples
        ? 'Mixed'
        : apiMetadata
          ? 'API modelled'
          : 'Loading';
  const sampleSummary = browserMetadata
    ? '0 observed samples'
    : apiMetadata?.observed
      ? `${apiSampleCount} observed samples`
      : apiMetadata?.contains_observed_samples
        ? `${observedSampleCount} observed of ${apiSampleCount} stored samples`
        : apiMetadata
          ? `${apiSampleCount} modelled stored samples`
          : 'Checking provenance';

  return (
    <section className="operations-history" aria-labelledby="history-panel-title" aria-busy={loading}>
      {/* Header + corridor selector */}
      <div className="operations-history__header">
        <div className="operations-history__title-row">
          <ChartNoAxesColumn size={ICON_SIZE.large} color="var(--accent-indigo)" />
          <h3 id="history-panel-title" className="operations-history__title">
            Historical Congestion Analysis
          </h3>
        </div>
        <div className="operations-history__status">
          {loading ? <RefreshCw className="operations-spinner" aria-label="Loading history" size={ICON_SIZE.big} color="var(--text-muted)" /> : null}
          <span className="badge badge-neutral">
            {historyBadge}
          </span>
        </div>
      </div>

      {error ? (
        <div role="alert" className="operations-inline-alert">
          <TriangleAlert aria-hidden="true" size={ICON_SIZE.big} /> {error}
        </div>
      ) : null}

      <div className="operations-history__filters">
        {/* Corridor picker tabs */}
        <div className="operations-history__corridor-controls" role="group" aria-label="Corridor history">
          {corridors.map(c => (
            <button
              key={c.id}
              type="button"
              aria-pressed={activeCorridorId === c.id}
              onClick={() => {
                if (c.id === activeCorridorId) return;
                setLoading(true);
                setSelectedCorridor(c.id);
              }}
              className="ui-sand-interactive operations-history__corridor-button"
            >
              {c.name.split('->')[0].trim().split(' ').slice(0, 2).join(' ')}
            </button>
          ))}
        </div>

        <div className="operations-history__window-controls" role="group" aria-label="History window">
          {[1, 7, 14, 30].map(d => (
            <button
              key={d}
              type="button"
              aria-pressed={days === d}
              aria-label={d === 1 ? 'Show today' : `Show the last ${d} days`}
              onClick={() => {
                if (d === days) return;
                setLoading(true);
                setDays(d);
              }}
              className="ui-sand-interactive operations-history__window-button"
            >
              {d === 1 ? 'Today' : `${d}d`}
            </button>
          ))}
        </div>
      </div>

      {/* Summary stats */}
      {profile && hasHourlyData ? (
        <div className="operations-history__summary">
          {[
            {
              icon: <TrendingUp size={ICON_SIZE.medium} color="var(--accent-rose)" />,
              label: 'Peak Hour',
              value: HOUR_LABELS[peakHour?.hour ?? 0],
              sub: `avg ${peakHour?.avg_score?.toFixed(0) ?? '—'}`,
              tone: 'rose',
            },
            {
              icon: <Clock size={ICON_SIZE.medium} color="var(--accent-emerald)" />,
              label: 'Quietest',
              value: HOUR_LABELS[quietHour?.hour ?? 0],
              sub: `avg ${quietHour?.avg_score?.toFixed(0) ?? '—'}`,
              tone: 'emerald',
            },
            {
              icon: <Activity size={ICON_SIZE.medium} color="var(--accent-cyan)" />,
              label: 'Data Source',
              value: dataSourceValue,
              sub: sampleSummary,
              tone: 'cyan',
            },
          ].map(stat => (
            <div key={stat.label} className="operations-history-stat">
              <div className="operations-history-stat__label">
                {stat.icon}{stat.label}
              </div>
              <div className={`operations-history-stat__value operations-history-stat__value--${stat.tone}`}>{stat.value}</div>
              <div className="operations-history-stat__detail">{stat.sub}</div>
            </div>
          ))}
        </div>
      ) : null}

      {historyMetadata ? (
        <div className="operations-history__provenance" role="note" aria-label="History provenance">
          {browserMetadata ? (
            <>
              <strong>Browser continuity model — no observations.</strong>{' '}
              {browserMetadata.methodology}{' '}
              Reference time{' '}
              <time dateTime={browserMetadata.reference_time}>
                {formatScheduleVerifiedAt(browserMetadata.reference_time)}
              </time>.
              <ul className="operations-history__limitations">
                {browserMetadata.limitations.map(limitation => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </>
          ) : apiMetadata ? (
            <>
              <strong>
                {apiMetadata.observed
                  ? 'Observed API history.'
                  : apiMetadata.contains_observed_samples
                    ? 'Mixed observed and modelled API history.'
                    : 'API history contains modelled samples only.'}
              </strong>{' '}
              Freshness: {apiMetadata.freshness} ({apiMetadata.freshness_basis}).{' '}
              {apiMetadata.latest_sample_at ? (
                <>
                  Latest stored sample{' '}
                  <time dateTime={apiMetadata.latest_sample_at}>
                    {formatScheduleVerifiedAt(apiMetadata.latest_sample_at)}
                  </time>.{' '}
                </>
              ) : 'No stored sample timestamp is available. '}
              Storage: {apiMetadata.storage.durability.split('_').join(' ')};
              {apiMetadata.storage.durable ? ' durable on this API instance' : ' not durable across restarts'};
              {' '}not shared across instances.
            </>
          ) : null}
        </div>
      ) : null}

      <div className="operations-history__charts">
        {/* Hourly bar chart */}
        <div className="operations-history-chart-card">
        <div className="operations-history__chart-title">
          <ChartNoAxesColumn size={ICON_SIZE.medium} /> 24-Hour Average Profile
        </div>
        {hasHourlyData ? (
          <>
            <div
              className={`operations-history-chart-plot operations-history-bars${loading ? ' is-loading' : ''}`}
              role="img"
              aria-label={`24-hour congestion profile for ${corridor?.name}`}
            >
              {hourly.map(bucket => {
                const height = scoreToHeight(
                  bucket.avg_score,
                  maxHourlyScore,
                  HOURLY_MAX_BAR_HEIGHT_PX,
                );
                const tone = scoreToTone(bucket.avg_score);
                const isNow = currentBatamHour === bucket.hour;
                return (
                  <div
                    key={bucket.hour}
                    className="operations-history-bars__column"
                    onMouseEnter={event => updateHourlyTooltip(event, bucket)}
                    onMouseMove={event => updateHourlyTooltip(event, bucket)}
                    onMouseLeave={() => setHourlyTooltip(null)}
                  >
                    <div
                      className={`operations-history-bars__bar ${tone}${isNow ? ' is-current' : ''}`}
                      style={{ height: `${height}px` }}
                    />
                  </div>
                );
              })}
            </div>
            <div className="operations-history-bars__axis" aria-hidden="true">
              {HOUR_LABELS.map((label, hour) => (
                <span key={label}>{hour % 3 === 0 ? label : ''}</span>
              ))}
            </div>
            <div className="operations-history-legend">
              {[
                { tone: 'smooth', label: 'Smooth (<40)' },
                { tone: 'heavy', label: 'Heavy (40–70)' },
                { tone: 'critical', label: 'Critical (≥70)' },
              ].map(item => (
                <div key={item.label} className="operations-history-legend__item">
                  <span className={`operations-history-legend__swatch is-${item.tone}`} aria-hidden="true" />
                  {item.label}
                </div>
              ))}
              <div className="operations-history-legend__current">
                <span className="operations-history-legend__swatch is-current" aria-hidden="true" />
                Current hour
              </div>
            </div>
            {hourlyTooltip ? createPortal((
              <div
                className="operations-chart-tooltip operations-history-chart-tooltip"
                aria-hidden="true"
                style={{ left: hourlyTooltip.x, top: hourlyTooltip.y }}
              >
                <div className="operations-chart-tooltip__title">
                  {HOUR_LABELS[hourlyTooltip.bucket.hour]}
                </div>
                <div className="operations-chart-tooltip__body">
                  Average congestion index: {hourlyTooltip.bucket.avg_score === null
                    ? 'No data'
                    : `${hourlyTooltip.bucket.avg_score.toFixed(1)} / 100`}
                </div>
              </div>
            ), document.body) : null}
          </>
        ) : (
          <div role="status" className="operations-history__empty-chart">
            {loading ? 'Loading hourly congestion samples…' : 'No historical samples are available for this corridor and time window.'}
          </div>
        )}
        </div>

        {/* Weekly trend line */}
        {weekly.length >= 2 ? (
          <div className="operations-history-chart-card">
            <div className="operations-history__chart-title operations-history__chart-title--compact">
              <TrendingUp size={ICON_SIZE.medium} /> Daily Trend
            </div>
            <div className="operations-history-chart-plot operations-history-trend">
              <svg
                className="operations-history-trend__svg"
                role="img"
                aria-label={`Daily congestion trend for ${corridor?.name}`}
                viewBox={`0 0 ${TREND_VIEWBOX_WIDTH} ${TREND_VIEWBOX_HEIGHT}`}
                preserveAspectRatio="none"
              >
                <defs>
                  <linearGradient
                    id={trendGradientId}
                    gradientUnits="userSpaceOnUse"
                    x1="0"
                    y1={TREND_VIEWBOX_HEIGHT * (1 - CHART_MAX_DATA_HEIGHT_RATIO)}
                    x2="0"
                    y2={TREND_PLOT_BOTTOM}
                  >
                    <stop offset="0%" stopColor="var(--accent-cyan)" stopOpacity="0.28" />
                    <stop offset="100%" stopColor="var(--accent-cyan)" stopOpacity="0.03" />
                  </linearGradient>
                </defs>
                <path d={trendAreaPath} fill={`url(#${trendGradientId})`} />
                <path
                  d={trendLinePath}
                  fill="none"
                  stroke="var(--accent-cyan)"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
            </div>
            <div className="operations-history-trend__labels">
              <span>{weekly[0]?.date?.slice(5)}</span>
              <span>avg {(weekly.reduce((s, d) => s + d.avg_score, 0) / weekly.length).toFixed(0)}</span>
              <span>{weekly[weekly.length - 1]?.date?.slice(5)}</span>
            </div>
            <div className="operations-history-legend" aria-label="Daily trend legend">
              <div className="operations-history-legend__item">
                <span className="operations-history-legend__swatch is-trend" aria-hidden="true" />
                Daily average congestion index
              </div>
            </div>
          </div>
        ) : !loading && hasHourlyData ? (
          <div className="operations-history__trend-placeholder">
            A daily trend will appear after at least two days of samples are available.
          </div>
        ) : null}
      </div>

    </section>
  );
};
