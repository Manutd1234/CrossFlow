import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertItem, Corridor, Fetched, OperationsSummary } from '../../types';
import {
  ChartColumn, Check, ChevronDown, ExternalLink, Fuel, Landmark, Leaf, RadioTower,
  ShieldAlert, Ship, Sparkles, TrendingDown, UsersRound,
} from 'lucide-react';

import { OFFICIAL_OPERATIONS_FACTS } from '../../data/officialOperations';
import { ICON_SIZE } from '../../theme/iconSizes';
import { formatTime, shortCorridorName } from '../../utils/format';
import {
  Area as RechartsArea, AreaChart as RechartsAreaChart, Bar, BarChart as RechartsBarChart,
  CartesianGrid, Legend, Pie, PieChart, PieSectorShapeProps, ResponsiveContainer, Sector,
  Tooltip, type TooltipContentProps, XAxis, YAxis,
} from 'recharts';

import { CongestionHeatmap } from './CongestionHeatmap';
import { AIModelPanel } from './AIModelPanel';
import { OPERATIONS_TRENDS_ANIMATION } from './constants';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';


// Shared light-theme chart chrome
const AXIS_COLOR = '#94a3b8';
const AXIS_TICK = { fill: '#475569', fontSize: 12 };
const BAR_HOVER_FILL = 'rgba(6, 182, 212, 0.08)';
const UNMANAGED_COLOR = '#dc2626';
const MANAGED_COLOR = '#059669';
const PIE_COLORS = ['#6366f1', '#06b6d4', '#10b981'];
const OFFICIAL_FACT_ICONS = [UsersRound, UsersRound, Ship, RadioTower] as const;

const VehiclePieSector = (props: PieSectorShapeProps) => (
  <Sector {...props} fill={PIE_COLORS[props.index % PIE_COLORS.length]} />
);

type OperationsTooltipVariant = 'planning-index' | 'series' | 'share';

interface OperationsChartTooltipProps extends Pick<
  TooltipContentProps<number | string, number | string>,
  'active' | 'label' | 'payload'
> {
  variant: OperationsTooltipVariant;
}

const OperationsChartTooltip: React.FC<OperationsChartTooltipProps> = ({
  active,
  label,
  payload,
  variant,
}) => {
  const entries = payload?.filter(entry => entry.value !== undefined) ?? [];
  if (!active || entries.length === 0) return null;

  const title = variant === 'share' ? entries[0]?.name : label;

  return (
    <div className="operations-chart-tooltip">
      <div className="operations-chart-tooltip__title">{title}</div>
      {entries.map((entry, index) => {
        const bodyLabel = variant === 'share'
          ? 'Relative planning share'
          : variant === 'planning-index'
            ? 'Planning index'
            : entry.name;
        const valueSuffix = variant === 'share'
          ? '%'
          : variant === 'planning-index'
            ? ' / 100'
            : entry.unit ? ` ${entry.unit}` : '';

        return (
          <div
            key={`${String(entry.dataKey ?? entry.name)}-${index}`}
            className="operations-chart-tooltip__body"
          >
            {bodyLabel}: {String(entry.value)}{valueSuffix}
          </div>
        );
      })}
    </div>
  );
};

interface OperationsAlertCardProps {
  alert: AlertItem;
  acknowledged?: boolean;
  dismissing?: boolean;
  onAcknowledge?: () => void;
}

const OperationsAlertCard: React.FC<OperationsAlertCardProps> = ({
  alert,
  acknowledged = false,
  dismissing = false,
  onAcknowledge,
}) => (
  <article
    className={`operations-alert operations-alert--${alert.severity.toLowerCase()}${acknowledged ? ' is-acknowledged' : ''}`}
  >
    <div className="operations-alert__content">
      <div className="operations-alert__heading">
        <strong className="operations-alert__title">{alert.title}</strong>
        <span className="operations-alert__metadata">
          <span className="operations-alert__timestamp">{formatTime(alert.timestamp)}</span>
          <span className={`operations-alert__severity operations-alert__severity--${alert.severity.toLowerCase()}`}>
            {alert.severity}
          </span>
        </span>
      </div>
      <p className="operations-alert__message">{alert.message}</p>
    </div>

    {acknowledged ? (
      <span className="operations-alert__acknowledged-status">
        <Check aria-hidden="true" size={ICON_SIZE.big} /> Acknowledged
      </span>
    ) : (
      <button
        type="button"
        className="glass-button operations-alert__action"
        aria-label={`Acknowledge planning alert: ${alert.title}`}
        disabled={dismissing}
        onClick={onAcknowledge}
      >
        {dismissing ? 'Acknowledging…' : 'Acknowledge'}
      </button>
    )}
  </article>
);

interface OperationsAnalyticsProps {
  operations: OperationsSummary;
  operationsSnapshot: Fetched<OperationsSummary> | null;
  corridors: Corridor[];
  ferryAndPortsContent?: React.ReactNode;
}

/** Modelled congestion index with no departure management, for comparison. */
const UNMANAGED_BASELINE = 63.0;

const HOURLY_SCENARIO = [
  { hour: '06:00', baseline: 32, optimized: 24 },
  { hour: '08:00', baseline: 84, optimized: 46 },
  { hour: '10:00', baseline: 58, optimized: 35 },
  { hour: '12:00', baseline: 62, optimized: 38 },
  { hour: '14:00', baseline: 50, optimized: 30 },
  { hour: '16:00', baseline: 76, optimized: 42 },
  { hour: '18:00', baseline: 92, optimized: 48 },
  { hour: '20:00', baseline: 44, optimized: 25 },
];

const FALLBACK_VEHICLE_PIE = [
  { name: 'Freight Trucks', value: 55 },
  { name: 'Express Vans', value: 25 },
  { name: 'Commuter Cars', value: 20 },
];

const FALLBACK_PRESSURE_INDEX = [
  { hour: '06:00', baseline_index: 43, managed_index: 21 },
  { hour: '08:00', baseline_index: 90, managed_index: 40 },
  { hour: '10:00', baseline_index: 57, managed_index: 29 },
  { hour: '12:00', baseline_index: 62, managed_index: 30 },
  { hour: '14:00', baseline_index: 52, managed_index: 27 },
  { hour: '16:00', baseline_index: 81, managed_index: 38 },
  { hour: '18:00', baseline_index: 100, managed_index: 46 },
  { hour: '20:00', baseline_index: 48, managed_index: 24 },
];

type OperationsWorkspaceTab = 'overview' | 'ferry-ports' | 'trends' | 'alerts' | 'history' | 'model';

export const OperationsAnalytics: React.FC<OperationsAnalyticsProps> = ({
  operations,
  operationsSnapshot,
  corridors,
  ferryAndPortsContent,
}) => {
  // Acknowledged alerts. Ids are stable across polls, so a dismissal sticks.
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const [activeWorkspaceTab, setActiveWorkspaceTab] =
    useState<OperationsWorkspaceTab>('overview');
  const [historyVisited, setHistoryVisited] = useState(false);
  const [modelVisited, setModelVisited] = useState(false);
  const [dismissingAlertIds, setDismissingAlertIds] = useState<Set<string>>(new Set());
  const acknowledgementTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const beginAcknowledge = useCallback((alertId: string) => {
    if (acknowledgementTimersRef.current.has(alertId)) return;

    setDismissingAlertIds(prev => new Set(prev).add(alertId));
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    const timer = setTimeout(() => {
      setAcknowledged(prev => new Set(prev).add(alertId));
      setDismissingAlertIds(prev => {
        const next = new Set(prev);
        next.delete(alertId);
        return next;
      });
      acknowledgementTimersRef.current.delete(alertId);
    }, reducedMotion ? 0 : 220);
    acknowledgementTimersRef.current.set(alertId, timer);
  }, []);

  useEffect(() => () => {
    acknowledgementTimersRef.current.forEach(timer => clearTimeout(timer));
    acknowledgementTimersRef.current.clear();
  }, []);

  const handleWorkspaceTabChange = (tab: OperationsWorkspaceTab) => {
    setActiveWorkspaceTab(tab);
    if (tab === 'history') setHistoryVisited(true);
    if (tab === 'model') setModelVisited(true);
  };

  /**
   * Per-corridor scenario opportunity from the API, so the bars provably sum
   * to the headline tile above them.
   */
  const corridorComparison = useMemo(() => {
    const byCorridor = operations.co2_by_corridor_kg;
    if (!byCorridor) return [];
    return corridors.map(c => ({
      name: shortCorridorName(c.name),
      opportunity_kg: byCorridor[c.id] ?? 0,
    }));
  }, [operations.co2_by_corridor_kg, corridors]);

  // Same >60 rule the backend counts with, so the caption can never name a
  // bottleneck while the number above it reads zero.
  const bottleneckNames = (
    operations.bottleneck_corridors?.map(b => shortCorridorName(b.name)) ?? []
  ).join(' & ');

  const congestionDelta = operations.average_congestion_index - UNMANAGED_BASELINE;
  const congestionDeltaPct = (congestionDelta / UNMANAGED_BASELINE) * 100;

  const visibleAlerts = operations.alerts ?? [];
  const activeAlerts = visibleAlerts.filter(alert => !acknowledged.has(alert.id));
  const acknowledgedAlerts = visibleAlerts.filter(alert => acknowledged.has(alert.id));

  const hasApiOperations = operationsSnapshot !== null
    && operationsSnapshot.source !== 'offline';
  const operationsSourceLabel = !operationsSnapshot
    ? 'LOADING OPERATIONS'
    : hasApiOperations
      ? 'PLANNING MODEL API CONNECTED'
      : 'LOCAL PLANNING CONTINUITY';
  const avoidableEmissionsOpportunity = operations
    .modeled_avoidable_emissions_opportunity_kg_today
    ?? operations.total_co2_reduced_today_kg;
  const projectedAvoidableEmissions = operations
    .modeled_projected_full_day_avoidable_emissions_kg
    ?? operations.projected_full_day_co2_kg;

  const vehiclePieData = useMemo(() => {
    const byType = hasApiOperations ? operations.co2_by_vehicle_type : undefined;
    const raw = byType
      ? Object.entries(byType).map(([name, value]) => ({ name, value }))
      : FALLBACK_VEHICLE_PIE;
    const total = raw.reduce((sum, item) => sum + item.value, 0);
    return raw.map((item, index) => ({
      ...item,
      value: total > 0 ? Number((item.value / total * 100).toFixed(1)) : 0,
      fill: PIE_COLORS[index % PIE_COLORS.length],
    }));
  }, [hasApiOperations, operations.co2_by_vehicle_type]);

  const histogramData = useMemo(() => {
    const apiRows = hasApiOperations ? operations.hourly_co2_distribution : undefined;
    if (!apiRows?.length) return FALLBACK_PRESSURE_INDEX;
    const peak = Math.max(
      1,
      ...apiRows.flatMap(row => [row.baseline_co2, row.optimized_co2]),
    );
    return apiRows.map(row => ({
      hour: row.hour,
      baseline_index: Number((row.baseline_co2 / peak * 100).toFixed(1)),
      managed_index: Number((row.optimized_co2 / peak * 100).toFixed(1)),
    }));
  }, [hasApiOperations, operations.hourly_co2_distribution]);

  const emissionsPressureIndex = Number((
    (Math.max(0, Math.min(100, operations.average_congestion_index)) / 100) ** 2
    * 100
  ).toFixed(1));

  return (
    <div className="app-screen-layout operations-layout">
      <WorkspaceSubtabs
        activeTab={activeWorkspaceTab}
        onActiveTabChange={handleWorkspaceTabChange}
        ariaLabel="Operations analytics sections"
        className="operations-tabs"
        idPrefix="operations-workspace"
        tabs={[
          {
            id: 'overview',
            label: 'Overview',
            content: activeWorkspaceTab === 'overview' ? (
              <div className="operations-section-stack">
      <section className="ui-card-shadow-hover official-evidence-panel" aria-labelledby="official-evidence-title">
        <div className="official-evidence-heading">
          <h3 id="official-evidence-title">Batam Authority Operating Context</h3>
          <span><Landmark aria-hidden="true" size={ICON_SIZE.medium} /> Verified public records</span>
        </div>
        <div className="official-evidence-grid">
          {OFFICIAL_OPERATIONS_FACTS.map((fact, index) => {
            const FactIcon = OFFICIAL_FACT_ICONS[index % OFFICIAL_FACT_ICONS.length];
            return (
              <article key={fact.id}>
                <div className="official-evidence-label">
                  <FactIcon aria-hidden="true" size={ICON_SIZE.medium} /> {fact.label}
                </div>
                <strong>{fact.value}</strong>
                <p>{fact.detail}</p>
                <div className="operations-alert__content">
                  <span>{fact.period}</span>
                  <a href={fact.sourceUrl} target="_blank" rel="noreferrer">
                    {fact.publisher} <ExternalLink aria-hidden="true" size={ICON_SIZE.medium} />
                  </a>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {/* Metrics Row */}
      <section className="operations-metrics" aria-label="Operations headline metrics">
        <article className="glass-panel operations-metric-card">
          <div className="operations-metric-card__label">
            <ChartColumn size={ICON_SIZE.medium} color="var(--accent-cyan)" /> Network Congestion Index
          </div>
          <div className="operations-metric-card__value">
            {operations.average_congestion_index} <span className="operations-metric-card__unit">/ 100</span>
          </div>
          <div className={`operations-metric-card__detail operations-metric-card__detail--trend ${congestionDelta <= 0 ? 'is-positive' : 'is-negative'}`}>
            <TrendingDown className={congestionDelta > 0 ? 'is-rising' : undefined} size={ICON_SIZE.big} />
            {congestionDeltaPct >= 0 ? '+' : ''}{congestionDeltaPct.toFixed(1)}% vs modelled baseline ({UNMANAGED_BASELINE})
          </div>
        </article>

        <article className="glass-panel operations-metric-card">
          <div className="operations-metric-card__label">
            <ShieldAlert size={ICON_SIZE.medium} color="var(--accent-rose)" /> Active Corridor Bottlenecks
          </div>
          <div className="operations-metric-card__value operations-metric-card__value--rose">
            {operations.active_bottlenecks} <span className="operations-metric-card__unit">locations</span>
          </div>
          <div className="operations-metric-card__detail">
            {bottleneckNames || 'No corridors above threshold'}
          </div>
        </article>

        <article className="glass-panel operations-metric-card">
          <div className="operations-metric-card__label">
            <Leaf size={ICON_SIZE.medium} color="var(--accent-emerald)" /> Avoidable Emissions
          </div>
          <div
            className="operations-metric-card__value operations-metric-card__value--emerald"
            title={operations.co2_methodology?.basis}
          >
            {avoidableEmissionsOpportunity} <span className="operations-metric-card__unit">kg CO2</span>
          </div>
          <div className="operations-metric-card__detail">
            {projectedAvoidableEmissions
              ? `Modelled scenario to current hour · ~${projectedAvoidableEmissions} kg full-day opportunity`
              : 'Modelled scenario to current hour; no full-day projection is available'}
          </div>
        </article>

        <article className="glass-panel operations-metric-card">
          <div className="operations-metric-card__label">
            <Fuel size={ICON_SIZE.medium} color="var(--accent-cyan)" /> Queue Emissions Pressure
          </div>
          <div className="operations-metric-card__value operations-metric-card__value--cyan">
            {emissionsPressureIndex}{' '}
            <span className="operations-metric-card__unit">/ 100</span>
          </div>
          <div className="operations-metric-card__detail operations-metric-card__detail--inline">
            Unitless congestion-derived planning proxy; not measured CO2 or air quality.
          </div>
        </article>
      </section>

              </div>
            ) : null,
          },
          {
            id: 'ferry-ports',
            label: 'Ports',
            content: activeWorkspaceTab === 'ferry-ports' ? ferryAndPortsContent : null,
          },
          {
            id: 'trends',
            label: 'Trends',
            content: activeWorkspaceTab === 'trends' ? (
              <div className="operations-section-stack">

      {/* Charts Tier 1: unitless planning shares and pressure indices */}
      <div className="operations-chart-grid operations-chart-grid--wide">
        {/* Pie / Donut Chart: normalized vehicle planning share */}
        <section className="glass-panel operations-chart-card" aria-labelledby="fleet-chart-title">
          <h3 id="fleet-chart-title" className="operations-chart-card__title">
            <Fuel size={ICON_SIZE.large} color="var(--accent-indigo)" /> Illustrative Fleet Emissions-Pressure Share
            <span className="badge badge-neutral">
              {hasApiOperations && operations.co2_by_vehicle_type ? 'MODELLED API · NORMALIZED' : 'PLANNING PROFILE'}
            </span>
          </h3>

          <div className="operations-chart operations-chart--centered" role="img" aria-label="Donut chart of unitless modelled emissions-pressure share by vehicle type" aria-describedby="fleet-chart-summary">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  {...OPERATIONS_TRENDS_ANIMATION}
                  data={vehiclePieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={60}
                  outerRadius={90}
                  paddingAngle={5}
                  dataKey="value"
                  shape={VehiclePieSector}
                />
                <Tooltip
                  content={props => <OperationsChartTooltip {...props} variant="share" />}
                  isAnimationActive={false}
                />
                <Legend iconSize={10} iconType="circle" />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <ul id="fleet-chart-summary" className="visually-hidden">
            {vehiclePieData.map(item => (
              <li key={item.name}>{item.name}: {item.value}% relative planning share.</li>
            ))}
          </ul>
        </section>

        {/* Hourly unitless emissions-pressure planning index */}
        <section className="glass-panel operations-chart-card" aria-labelledby="emissions-chart-title">
          <h3 id="emissions-chart-title" className="operations-chart-card__title">
            <ChartColumn size={ICON_SIZE.large} color="var(--accent-cyan)" /> Hourly Emissions-Pressure Planning Index
            <span className="badge badge-neutral">
              {hasApiOperations && operations.hourly_co2_distribution ? 'MODELLED API · NORMALIZED' : 'UNITLESS SCENARIO'}
            </span>
          </h3>

          <div className="operations-chart" role="img" aria-label="Bar chart comparing unitless baseline and managed hourly emissions-pressure indices" aria-describedby="emissions-chart-summary">
            <ResponsiveContainer width="100%" height="100%">
              <RechartsBarChart data={histogramData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="hour" stroke={AXIS_COLOR} tick={AXIS_TICK} />
                <YAxis stroke={AXIS_COLOR} tick={AXIS_TICK} domain={[0, 100]} />
                <Tooltip
                  content={props => <OperationsChartTooltip {...props} variant="planning-index" />}
                  cursor={{ fill: BAR_HOVER_FILL }}
                  isAnimationActive={false}
                />
                <Legend iconSize={10} iconType="circle" />
                <Bar {...OPERATIONS_TRENDS_ANIMATION} dataKey="baseline_index" name="Unmanaged planning index" fill={UNMANAGED_COLOR} radius={[4, 4, 0, 0]} />
                <Bar {...OPERATIONS_TRENDS_ANIMATION} dataKey="managed_index" name="Managed planning index" fill={MANAGED_COLOR} radius={[4, 4, 0, 0]} />
              </RechartsBarChart>
            </ResponsiveContainer>
          </div>
          <ul id="emissions-chart-summary" className="visually-hidden">
            {histogramData.map(item => (
              <li key={item.hour}>{item.hour}: unmanaged index {item.baseline_index} of 100; managed index {item.managed_index} of 100.</li>
            ))}
          </ul>
        </section>
      </div>

      {/* Charts Tier 2: daily congestion and modelled emissions opportunity */}
      <div className="operations-chart-grid">
        {/* Recharts Area Chart: Baseline vs CrossFlow AI */}
        <section className="glass-panel operations-chart-card" aria-labelledby="scenario-chart-title">
          <h3 id="scenario-chart-title" className="operations-chart-card__title">
            <Sparkles size={ICON_SIZE.large} color="var(--accent-cyan)" /> Daily Congestion Profile: Unmanaged vs. CrossFlow AI
            <span className="badge badge-neutral">ILLUSTRATIVE SCENARIO</span>
          </h3>

          <div className="operations-chart" role="img" aria-label="Area chart comparing illustrative unmanaged and CrossFlow AI congestion scenarios" aria-describedby="scenario-chart-summary">
            <ResponsiveContainer width="100%" height="100%">
              <RechartsAreaChart data={HOURLY_SCENARIO}>
                <defs>
                  <linearGradient id="colorBaseline" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={UNMANAGED_COLOR} stopOpacity={0.4}/>
                    <stop offset="95%" stopColor={UNMANAGED_COLOR} stopOpacity={0}/>
                  </linearGradient>
                  <linearGradient id="colorOptimized" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={MANAGED_COLOR} stopOpacity={0.5}/>
                    <stop offset="95%" stopColor={MANAGED_COLOR} stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="hour" stroke={AXIS_COLOR} tick={AXIS_TICK} />
                <YAxis stroke={AXIS_COLOR} tick={AXIS_TICK} domain={[0, 100]} />
                <Tooltip
                  content={props => <OperationsChartTooltip {...props} variant="series" />}
                  cursor={{ stroke: '#cbd5e1' }}
                  isAnimationActive={false}
                />
                <Legend iconSize={10} iconType="circle" />
                <RechartsArea {...OPERATIONS_TRENDS_ANIMATION} type="monotone" dataKey="baseline" name="Unmanaged Baseline" stroke={UNMANAGED_COLOR} strokeWidth={2} fillOpacity={1} fill="url(#colorBaseline)" />
                <RechartsArea {...OPERATIONS_TRENDS_ANIMATION} type="monotone" dataKey="optimized" name="CrossFlow AI Optimized" stroke={MANAGED_COLOR} strokeWidth={2} fillOpacity={1} fill="url(#colorOptimized)" />
              </RechartsAreaChart>
            </ResponsiveContainer>
          </div>
          <ul id="scenario-chart-summary" className="visually-hidden">
            {HOURLY_SCENARIO.map(item => (
              <li key={item.hour}>{item.hour}: unmanaged congestion index {item.baseline} of 100; CrossFlow planning index {item.optimized} of 100.</li>
            ))}
          </ul>
        </section>

        {/* Recharts Bar Chart: modelled emissions opportunity by corridor */}
        <section className="glass-panel operations-chart-card" aria-labelledby="corridor-carbon-title">
          <h3 id="corridor-carbon-title" className="operations-chart-card__title">
            <Leaf size={ICON_SIZE.large} color="var(--accent-emerald)" /> Modelled Avoidable Emissions Opportunity by Corridor (kg)
            <span className="badge badge-neutral">
              {hasApiOperations ? 'MODELLED API' : operationsSourceLabel}
            </span>
          </h3>

          {corridorComparison.length > 0 ? <>
            <div className="operations-chart" role="img" aria-label="Bar chart of modelled avoidable-emissions opportunity by corridor" aria-describedby="corridor-carbon-summary">
              <ResponsiveContainer width="100%" height="100%">
                <RechartsBarChart data={corridorComparison}>
                  <defs>
                    <linearGradient id="colorBars" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#06b6d4" />
                      <stop offset="100%" stopColor="#10b981" />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="name" stroke={AXIS_COLOR} tick={AXIS_TICK} />
                  <YAxis stroke={AXIS_COLOR} tick={AXIS_TICK} />
                  <Tooltip
                    content={props => <OperationsChartTooltip {...props} variant="series" />}
                    cursor={{ fill: BAR_HOVER_FILL }}
                    isAnimationActive={false}
                  />
                  <Bar {...OPERATIONS_TRENDS_ANIMATION} dataKey="opportunity_kg" name="Scenario Opportunity (kg CO2)" fill="url(#colorBars)" radius={[6, 6, 0, 0]} />
                </RechartsBarChart>
              </ResponsiveContainer>
            </div>
            <ul id="corridor-carbon-summary" className="visually-hidden">
              {corridorComparison.map(item => (
                <li key={item.name}>{item.name}: modelled scenario opportunity {item.opportunity_kg} kg CO2.</li>
              ))}
            </ul>
          </> : <div role="status" className="operations-chart operations-chart--empty">Corridor-level carbon estimates are not available.</div>}
        </section>
      </div>

              </div>
            ) : null,
          },
          {
            id: 'alerts',
            label: 'Alerts',
            content: activeWorkspaceTab === 'alerts' ? (
      <section className="glass-panel operations-alerts" aria-labelledby="operator-alerts-title">
        <h3 id="operator-alerts-title" className="operations-alerts__title">
          <ShieldAlert size={ICON_SIZE.large} color="var(--accent-amber)" /> Dispatch Planning Alerts
          <span className="badge badge-neutral">
            {activeAlerts.length} {activeAlerts.length === 1 ? 'alert' : 'alerts'}
          </span>
        </h3>
        <div className="operations-alerts__scroll-region">
          <div className="operations-alerts__list" aria-label="Active planning alerts">
            {activeAlerts.map(alert => {
              const dismissing = dismissingAlertIds.has(alert.id);
              return (
                <div
                  key={alert.id}
                  className={`operations-alerts__item${dismissing ? ' is-dismissing' : ''}`}
                >
                  <div className="operations-alerts__item-inner">
                    <OperationsAlertCard
                      alert={alert}
                      dismissing={dismissing}
                      onAcknowledge={() => beginAcknowledge(alert.id)}
                    />
                  </div>
                </div>
              );
            })}
            {activeAlerts.length === 0 ? (
              <div role="status" className="operations-alerts__empty">
                No unacknowledged model-generated planning alerts are active.
              </div>
            ) : null}
          </div>

          <details className="operations-alerts__acknowledged-menu">
            <summary className="ui-sand-interactive operations-alerts__acknowledged-summary">
              <span>Acknowledged alerts</span>
              <span className="operations-alerts__acknowledged-count">{acknowledgedAlerts.length}</span>
              <ChevronDown className="operations-alerts__acknowledged-chevron" aria-hidden="true" size={ICON_SIZE.medium} />
            </summary>
            <div className="operations-alerts__acknowledged-list">
              {acknowledgedAlerts.length > 0 ? acknowledgedAlerts.map(alert => (
                <div key={alert.id} className="operations-alerts__acknowledged-item">
                  <OperationsAlertCard alert={alert} acknowledged />
                </div>
              )) : (
                <p className="operations-alerts__acknowledged-empty">No alerts have been acknowledged.</p>
              )}
            </div>
          </details>
        </div>
      </section>

            ) : null,
          },
          {
            id: 'history',
            label: 'History',
            content: activeWorkspaceTab === 'history' || historyVisited
              ? corridors.length > 0
                ? (
                    <div className="glass-panel operations-history-panel">
                      <CongestionHeatmap corridors={corridors} />
                    </div>
                  )
                : (
                    <div role="status" className="glass-panel operations-history-panel operations-history-panel--empty">
                      Historical congestion analysis will appear when corridor telemetry is available.
                    </div>
                  )
              : null,
          },
          {
            id: 'model',
            label: 'Model',
            content: activeWorkspaceTab === 'model' || modelVisited ? <AIModelPanel /> : null,
          },
        ]}
      />
    </div>
  );
};
