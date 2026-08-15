import React, { useMemo, useState } from 'react';
import {
  Anchor, CircleCheck, CircleMinus, CircleX, Clock, ExternalLink, ImageOff,
  PackageCheck, RefreshCw, ShieldCheck, Ship, TriangleAlert,
} from 'lucide-react';

import { DEMO_SHIPMENTS } from '../../data/mockData';
import { TERMINAL_MEDIA, type TerminalMedia } from '../../data/terminalMedia';
import { scheduleInformedPortSeed } from '../../services/api';
import { ICON_SIZE } from '../../theme/iconSizes';
import {
  DataSource, FerryRefreshReport, FerryRefreshSourceStatus, FerrySchedule,
  FerryTimetableMetadata, PortStatus,
} from '../../types';
import {
  ferryBadgeClass, formatScheduleVerifiedAt, formatTime, prettyStatus,
  relativeDeparture, shipmentBadgeClass,
} from '../../utils/format';
import { batamParts } from '../../utils/batamTime';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';


interface FerryPortTrackerProps {
  ferries: FerrySchedule[];
  dataSource: DataSource;
  timetable: FerryTimetableMetadata;
  ports: PortStatus[];
  portSource: DataSource;
  portsLoading: boolean;
  portsError: string | null;
  isRefreshingOfficialSources: boolean;
  onRefreshOfficialSources: () => Promise<FerryRefreshReport>;
}

const PORT_FILTERS = ['ALL', 'Batam Centre', 'HarbourBay', 'Sekupang', 'Nongsa Pura'] as const;
const INITIAL_PORT_ESTIMATES = scheduleInformedPortSeed();
type FerryWorkspaceTab = 'terminals' | 'departures' | 'cargo';

interface RefreshFeedback {
  tone: 'success' | 'partial' | 'failure';
  message: string;
  report?: FerryRefreshReport;
}

const SOURCE_STATUS_LABELS: Record<FerryRefreshSourceStatus, string> = {
  verified_structure: 'Page checked',
  unavailable_or_invalid: 'Needs review',
  skipped_permission_required: 'Permission required',
};

const SOURCE_STATUS_ORDER: FerryRefreshSourceStatus[] = [
  'verified_structure',
  'skipped_permission_required',
  'unavailable_or_invalid',
];

const SourceStatusIcon: React.FC<{
  status: FerryRefreshSourceStatus;
  labelled?: boolean;
}> = ({ status, labelled = false }) => {
  const Icon = status === 'verified_structure'
    ? CircleCheck
    : status === 'skipped_permission_required'
      ? CircleMinus
      : CircleX;
  const label = SOURCE_STATUS_LABELS[status];

  return (
    <span
      className={`ferry-port-source-status-icon ferry-port-source-status-icon--${status}`}
      title={labelled ? label : undefined}
    >
      <Icon aria-label={labelled ? label : undefined} size={ICON_SIZE.big} strokeWidth={2.2} />
    </span>
  );
};

function wasVerifiedToday(value?: string, now: Date = new Date()): boolean {
  if (!value) return false;
  const verifiedAt = new Date(value);
  if (Number.isNaN(verifiedAt.getTime())) return false;
  const verifiedDate = batamParts(verifiedAt);
  const currentDate = batamParts(now);
  return verifiedDate.year === currentDate.year
    && verifiedDate.month === currentDate.month
    && verifiedDate.day === currentDate.day;
}

function feedbackForRefresh(report: FerryRefreshReport): RefreshFeedback {
  const { verified, failed, permission_gated: permissionGated } = report.summary;
  if (report.status === 'checked') {
    return {
      tone: 'success',
      message: `Checked ${verified} official sources. ${permissionGated} ${permissionGated === 1 ? 'source was' : 'sources were'} skipped.`,
      report,
    };
  }
  if (report.status === 'cached') {
    return {
      tone: 'success',
      message: `Reused the recent official-source check (${verified} pages checked).`,
      report,
    };
  }
  if (report.status === 'partial') {
    return {
      tone: 'partial',
      message: `Checked ${verified} official source ${verified === 1 ? 'page' : 'pages'}; ${failed} could not be validated. The last-known-good planning schedule was retained.`,
      report,
    };
  }
  return {
    tone: 'failure',
    message: 'Official pages could not be validated. No timetable was replaced; the last-known-good planning schedule remains active.',
    report,
  };
}

interface TerminalPhotoProps {
  media: TerminalMedia;
  terminalName: string;
}

const TerminalPhoto: React.FC<TerminalPhotoProps> = ({ media, terminalName }) => {
  const [failed, setFailed] = useState(false);

  return (
    <figure className={`terminal-card-media${failed ? ' is-unavailable' : ''}`}>
      {failed ? (
        <div
          className="terminal-card-photo-fallback"
          role="img"
          aria-label={`${terminalName} location photo unavailable`}
        >
          <ImageOff aria-hidden="true" size={ICON_SIZE.big} />
          <span>Location photo unavailable</span>
        </div>
      ) : (
        <img
          src={media.imageUrl}
          alt={media.alt}
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      )}
    </figure>
  );
};

const TerminalPhotoCredit: React.FC<{ media: TerminalMedia }> = ({ media }) => (
  <p className="terminal-card-photo-credit">
    <span>{media.context}, {media.capturedYear}.</span>
    <span>
      Photo by{' '}
      <a href={media.sourceUrl} target="_blank" rel="noreferrer">{media.author}</a>
      {' · '}
      <a
        className="terminal-card-photo-license"
        href={media.licenseUrl}
        target="_blank"
        rel="noreferrer"
      >
        {media.license}
      </a>
    </span>
  </p>
);


export const FerryPortTracker: React.FC<FerryPortTrackerProps> = ({
  ferries,
  dataSource,
  timetable,
  ports,
  portSource,
  portsLoading,
  portsError,
  isRefreshingOfficialSources,
  onRefreshOfficialSources,
}) => {
  const [selectedPort, setSelectedPort] = useState<string>('ALL');
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<FerryWorkspaceTab>('terminals');
  const [refreshPending, setRefreshPending] = useState(false);
  const [refreshFeedback, setRefreshFeedback] = useState<RefreshFeedback | null>(null);
  const refreshBusy = refreshPending || isRefreshingOfficialSources;
  const verifiedToday = wasVerifiedToday(timetable.last_verified_at);
  const refreshFeedbackClassName = [
    'ferry-port-refresh-feedback',
    !refreshBusy && !refreshFeedback ? 'is-hidden' : '',
    refreshFeedback?.tone ? `is-${refreshFeedback.tone}` : '',
  ].filter(Boolean).join(' ');

  const handleOfficialRefresh = async () => {
    if (refreshBusy) return;
    setRefreshPending(true);
    setRefreshFeedback(null);
    try {
      const report = await onRefreshOfficialSources();
      setRefreshFeedback(feedbackForRefresh(report));
    } catch (error) {
      setRefreshFeedback({
        tone: 'failure',
        message: error instanceof Error
          ? error.message
          : 'Official ferry sources could not be refreshed.',
      });
    } finally {
      setRefreshPending(false);
    }
  };

  const filteredFerries = useMemo(
    () => ferries.filter(ferry => (
      selectedPort === 'ALL'
      || ferry.departure_port.toLowerCase().includes(selectedPort.toLowerCase())
    )),
    [ferries, selectedPort],
  );

  const filteredPorts = useMemo(
    () => ports.filter(port => (
      selectedPort === 'ALL'
      || port.port_name.toLowerCase().includes(selectedPort.toLowerCase())
    )),
    [ports, selectedPort],
  );

  return (
    <div className="app-screen-layout ferry-port-layout">
      <section className="glass-panel ui-white-card-hover ferry-port-overview" aria-labelledby="ferry-port-title">
        <div className="ferry-port-overview__header">
          <div className="ferry-port-overview__intro">
            <div className="ferry-port-overview__title-row">
              <Anchor aria-hidden="true" size={ICON_SIZE.big} />
              <h2 id="ferry-port-title" className="ferry-port-overview__title">
                Cross-Border Ferry &amp; Port Intelligence
              </h2>
            </div>
            <p className="ferry-port-overview__description">
              Official terminal references, schedule-informed processing estimates, and published cross-strait departures.
            </p>
          </div>

          <div className="ferry-port-overview__controls" aria-label="Ferry data controls">
            <span
              className="badge badge-neutral"
              title={dataSource === 'offline'
                ? 'Terminal estimates are using the bundled planning continuity data'
                : 'Terminal estimates were returned by the planning API'}
            >
              {portsLoading && ports.length === 0
                ? 'CHECKING TERMINAL DATA'
                : portSource === 'offline'
                  ? 'SCHEDULE-INFORMED CONTINUITY'
                  : 'SCHEDULE-INFORMED ESTIMATE'}
            </span>
            <span className={`badge ferry-port-overview__verified-badge ${verifiedToday ? 'badge-smooth' : 'badge-heavy'}`}>
              Last verified{' '}
              <time dateTime={timetable.last_verified_at}>
                {formatScheduleVerifiedAt(timetable.last_verified_at)}
              </time>
            </span>
          </div>
        </div>

        <div
          className={refreshFeedbackClassName}
          role={refreshFeedback?.tone === 'failure' ? 'alert' : 'status'}
          aria-live="polite"
          aria-atomic="true"
        >
          {refreshBusy
            ? 'Checking reviewed official ferry and terminal sources…'
            : refreshFeedback?.message ?? ''}
          {refreshFeedback?.report ? (
            <>
              <ul className="ferry-port-source-results" aria-label="Official source check results">
                {refreshFeedback.report.source_results.map(source => (
                  <li key={source.source_id} className="ferry-port-source-results__item">
                    {source.status === 'skipped_permission_required' ? (
                      <span className="ferry-port-source-results__authority">{source.authority}</span>
                    ) : (
                      <a className="ferry-port-source-results__link" href={source.url} target="_blank" rel="noreferrer">
                        {source.authority}
                      </a>
                    )}
                    <SourceStatusIcon status={source.status} labelled />
                  </li>
                ))}
              </ul>
              <div className="ferry-port-source-legend" aria-label="Source result legend">
                {SOURCE_STATUS_ORDER.map(status => (
                  <span key={status} className="ferry-port-source-legend__item">
                    <SourceStatusIcon status={status} />
                    {SOURCE_STATUS_LABELS[status]}
                  </span>
                ))}
              </div>
            </>
          ) : null}
        </div>

        <div className="ferry-port-overview__footer">
          <div className="ferry-port-filter-list" role="group" aria-label="Filter by departure port">
            {PORT_FILTERS.map(port => (
              <button
                key={port}
                type="button"
                aria-pressed={selectedPort === port}
                onClick={() => setSelectedPort(port)}
                className="ui-button-choice ui-sand-interactive"
              >
                {port === 'ALL' ? 'All terminals' : port}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="ui-button-primary ferry-port-overview__refresh-button"
            onClick={() => void handleOfficialRefresh()}
            disabled={refreshBusy}
            aria-busy={refreshBusy}
          >
            <RefreshCw
              aria-hidden="true"
              size={ICON_SIZE.large}
              className={refreshBusy ? 'ferry-port-spinner' : undefined}
            />
            {refreshBusy ? 'Checking Schedules…' : 'Check Schedules'}
          </button>
        </div>
      </section>

      {portsError ? (
        <div className="ferry-port-error" role="alert">
          <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {portsError}
        </div>
      ) : null}

      <WorkspaceSubtabs<FerryWorkspaceTab>
        className="ferry-port-tabs"
        activeTab={activeWorkspaceTab}
        onActiveTabChange={setActiveWorkspaceTab}
        ariaLabel="Ferry and port workspace sections"
        idPrefix="ferry-workspace"
        tabs={[
          {
            id: 'terminals',
            label: 'Terminals',
            description: 'Queues, processing and berths',
            content: (
      <section className="terminal-status" aria-label="Terminal access and processing outlook" aria-busy={portsLoading}>
        {portsLoading && ports.length === 0 ? (
          <div className="ferry-port-empty-state ferry-port-empty-state--terminals" role="status">
            Loading terminal queue and berth data…
          </div>
        ) : filteredPorts.length > 0 ? (
          <div className="terminal-card-grid">
            {filteredPorts.map(port => {
              const media = TERMINAL_MEDIA[port.port_name];
              const seed = INITIAL_PORT_ESTIMATES.find(
                estimate => estimate.port_name === port.port_name,
              );
              const passengerQueueMins = port.passenger_queue_mins
                ?? seed?.passenger_queue_mins
                ?? 5;
              const processingMins = port.customs_processing_mins
                ?? seed?.customs_processing_mins
                ?? 4;
              const activeBerths = port.active_berths;
              const hasBerthEstimate = activeBerths != null
                && port.total_berths != null
                && port.total_berths > 0;
              const berthPercent = hasBerthEstimate
                ? Math.round(((activeBerths ?? 0) / (port.total_berths ?? 1)) * 100)
                : 0;
              return (
                <article key={port.port_name} className="ui-white-card-hover terminal-intelligence-card terminal-card-content">
                  <header className="terminal-card-heading">
                    {media ? <TerminalPhoto media={media} terminalName={port.port_name} /> : null}
                    <div className="terminal-card-heading-copy">
                      <div className="terminal-card-heading-row">
                        <div>
                          <h4 className="terminal-card-title">{port.port_name}</h4>
                          <span className="terminal-card-code">Terminal {port.terminal_code}</span>
                        </div>
                        {port.status ? (
                          <span className={`badge terminal-card-status ${port.status === 'CONGESTED' ? 'badge-critical' : port.status === 'BUSY' ? 'badge-heavy' : 'badge-neutral'}`}>
                            EST. {port.status}
                          </span>
                        ) : (
                          <span className="badge badge-neutral terminal-card-status">
                            NOT OBSERVED
                          </span>
                        )}
                      </div>
                    </div>
                  </header>

                  <div className="terminal-card-metrics">
                    <div className="terminal-card-metric">
                      <div className="terminal-card-metric__label">
                        <Clock aria-hidden="true" size={ICON_SIZE.small} color="var(--accent-amber)" /> Passenger Queue
                      </div>
                      <strong className="terminal-card-metric__value terminal-card-metric__value--queue">
                        {passengerQueueMins} <span className="terminal-card-metric__unit">min</span>
                      </strong>
                    </div>
                    <div className="terminal-card-metric">
                      <div className="terminal-card-metric__label">
                        <ShieldCheck aria-hidden="true" size={ICON_SIZE.small} color="var(--accent-cyan)" /> Processing
                      </div>
                      <strong className="terminal-card-metric__value terminal-card-metric__value--processing">
                        {processingMins} <span className="terminal-card-metric__unit">min</span>
                      </strong>
                    </div>
                  </div>

                  {hasBerthEstimate ? <div className="terminal-card-berths">
                    <div className="terminal-card-berths__header">
                      <span>Estimated active berths</span>
                      <strong>{activeBerths}/{port.total_berths}</strong>
                    </div>
                    <div className="ui-progress-track" role="progressbar" aria-label={`${port.port_name} modelled active berth capacity`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={berthPercent}>
                      <div className="ui-progress-fill" style={{ width: `${berthPercent}%` }} />
                    </div>
                  </div> : (
                    <div className="terminal-card-berths__unavailable">
                      Berth configuration and active occupancy are unavailable.
                    </div>
                  )}

                  <div className="terminal-card-details">
                    {port.freight_clearance_mins != null ? (
                      <span>Freight clearance estimate: <strong className="terminal-card-details__value">{port.freight_clearance_mins} min</strong></span>
                    ) : null}
                    {port.next_sailing_in_mins != null ? (
                      <span>
                        Next departure: <strong className="terminal-card-details__value terminal-card-details__value--departure">{port.next_sailing_in_mins} min</strong>
                        {port.next_operator || port.next_vessel
                          ? ` · ${port.next_operator ?? port.next_vessel}`
                          : ''}
                      </span>
                    ) : null}
                  </div>
                  <div className="terminal-card-links">
                    <a
                      href={port.official_reference_url ?? media?.officialUrl}
                      target="_blank"
                      rel="noreferrer"
                    >
                      BP Batam Terminal Reference <ExternalLink aria-hidden="true" size={ICON_SIZE.small} />
                    </a>
                  </div>
                  {media ? <TerminalPhotoCredit media={media} /> : null}
                </article>
              );
            })}
          </div>
        ) : (
          <div className="ferry-port-empty-state" role="status">
            No terminal intelligence matches this port filter.
          </div>
        )}
      </section>
            ),
          },
          {
            id: 'departures',
            label: 'Departures',
            description: 'Published cross-strait sailings',
            content: (
        <section className="glass-panel ferry-departures" aria-labelledby="sailings-title">
          <div className="ferry-departures__header">
            <h3 id="sailings-title" className="ferry-departures__title">
              <Ship aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-cyan)" /> Upcoming Departures
            </h3>
            <span className="badge badge-neutral">{filteredFerries.length} shown</span>
          </div>

          {filteredFerries.length > 0 ? (
            <ul className="ferry-departures__list">
              {filteredFerries.map((ferry, index) => (
                <li key={ferry.sailing_id ?? `${ferry.ferry_name}-${ferry.departure_time}-${index}`}>
                  <article className="ferry-departure-card">
                    <div className="ferry-departure-card__details">
                      <div className="ferry-departure-card__operator-row">
                        <strong className="ferry-departure-card__operator">{ferry.operator ?? ferry.ferry_name}</strong>
                        <span className={`badge ${ferryBadgeClass(ferry.status)}`}>{prettyStatus(ferry.status)}</span>
                      </div>
                      <div className="ferry-departure-card__route">
                        <span>{ferry.departure_port} ➔ {ferry.arrival_port}</span>
                      </div>
                      <div className="ferry-departure-card__evidence">
                        {ferry.schedule_source_url ? (
                          <a
                            href={ferry.schedule_source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="ferry-departure-card__source-link"
                          >
                            Official operator timetable
                          </a>
                        ) : null}
                      </div>
                    </div>

                    <div className="ferry-departure-card__timing">
                      <time className="ferry-departure-card__departure-time" dateTime={ferry.departure_time}>
                        {formatTime(ferry.departure_time)}
                      </time>
                      <div className="ferry-departure-card__relative-time">{relativeDeparture(ferry.minutes_until_departure)}</div>
                      {ferry.arrival_time ? (
                        <div className="ferry-departure-card__arrival-time">
                          Est. arrival {formatTime(ferry.arrival_time)}
                        </div>
                      ) : null}
                      {ferry.available_seats != null ? (
                        <div className="ferry-departure-card__seat-count">
                          {ferry.available_seats} seats reported
                        </div>
                      ) : null}
                    </div>
                  </article>
                </li>
              ))}
            </ul>
          ) : (
            <div className="ferry-port-empty-state ferry-port-empty-state--departures" role="status">
              No upcoming sailings match this departure-port filter.
            </div>
          )}
        </section>
            ),
          },
          {
            id: 'cargo',
            label: 'Cargo',
            description: 'Demo logistics visibility',
            content: (
        <section className="glass-panel cargo-monitor" aria-labelledby="cargo-title">
          <div className="cargo-monitor__header">
            <h3 id="cargo-title" className="cargo-monitor__title">
              <PackageCheck aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-emerald)" /> Batam-SG Cargo Monitor
            </h3>
            <span className="badge badge-neutral cargo-monitor__dataset-badge">DEMO DATASET</span>
          </div>

          <div className="cargo-monitor__list">
            {DEMO_SHIPMENTS.map(shipment => (
              <article key={shipment.id} className="cargo-shipment-card">
                <div className="cargo-shipment-card__header">
                  <span className="cargo-shipment-card__id">{shipment.id}</span>
                  <span className={`badge ${shipmentBadgeClass(shipment.status)} cargo-shipment-card__status`}>
                    {prettyStatus(shipment.status)}
                  </span>
                </div>
                <h4 className="cargo-shipment-card__route">
                  {shipment.origin} ➔ {shipment.destination}
                </h4>
                <div className="cargo-shipment-card__progress">
                  <div className="cargo-shipment-card__details">
                    <span>{shipment.carrier} · {shipment.vessel}</span>
                    <span>ETA {shipment.eta}</span>
                  </div>
                  <div className="ui-progress-track" role="progressbar" aria-label={`${shipment.id} shipment progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={shipment.progress}>
                    <div className="ui-progress-fill" style={{ width: `${shipment.progress}%` }} />
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>
            ),
          },
        ]}
      />
    </div>
  );
};
