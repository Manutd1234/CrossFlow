import { useEffect, useState } from 'react';
import type { DataSource, Provenance } from '../../types';
import { toBatamIso } from '../../utils/batamTime';
import { formatClock } from '../../utils/format';
import { Navigation, type AppTab } from './Navigation';

interface HeaderProps {
  activeTab: AppTab;
  setActiveTab: (tab: AppTab) => void;
  dataSource: DataSource;
  provenance?: Provenance;
  lastUpdated?: string;
}

const STATUS = {
  live: {
    label: 'Live',
    className: 'app-data-status-live',
    fallback: 'The current road traffic provider is connected.',
  },
  simulated: {
    label: 'Model Estimate',
    className: 'app-data-status-simulated',
    fallback: 'Current conditions are estimated by the Batam traffic model.',
  },
  offline: {
    label: 'Local Estimate',
    className: 'app-data-status-offline',
    fallback: 'The API is reconnecting; current-time local estimates remain available.',
  },
} as const;

function RollingBatamClock({ anchor }: { anchor: string }) {
  const anchorTime = Date.parse(anchor);
  const [currentTime, setCurrentTime] = useState(anchor);

  useEffect(() => {
    const startedAt = Date.now();
    const baseTime = Number.isNaN(anchorTime) ? startedAt : anchorTime;
    const timer = window.setInterval(() => {
      setCurrentTime(toBatamIso(new Date(baseTime + Date.now() - startedAt)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [anchorTime]);

  return <time dateTime={currentTime}>{formatClock(currentTime)}</time>;
}

export function Header({
  activeTab,
  setActiveTab,
  dataSource,
  provenance,
  lastUpdated,
}: HeaderProps) {
  const status = STATUS[dataSource];
  const sourceSummary = [
    provenance?.road_network &&
      `Roads: ${provenance.road_network} (${provenance.road_network_license ?? 'ODbL'})`,
    provenance?.routing && `Routing: ${provenance.routing}`,
    provenance?.traffic && `Traffic: ${provenance.traffic}`,
  ].filter(Boolean).join('. ');
  const statusDescription = sourceSummary || status.fallback;
  return (
    <header className="app-header">
      <div className="app-header-inner">
        <div className="app-brand">
          <div className="app-brand-copy">
            <h1 className="app-brand-name">Cross<span>Flow</span></h1>
          </div>
        </div>

        <section className="app-topbar app-header-topbar" aria-label="Batam mobility workspace controls">
          <div className="app-topbar-place">
            <span className="app-topbar-place-mark" aria-hidden="true">BTM</span>
            <span>
              <small>Island network</small>
              <strong>Batam ↔ Singapore</strong>
            </span>
          </div>
          <Navigation activeTab={activeTab} setActiveTab={setActiveTab} />
          <div className="app-topbar-scope">
            <span className="app-topbar-scope-dot" aria-hidden="true" />
            <span>
              <small>Planning watch</small>
              <strong>30 hotspot areas</strong>
            </span>
          </div>
        </section>

        <div className="app-header-actions">
          <div
            className={`app-data-status ${status.className}`}
            role="group"
            title={statusDescription}
            aria-label="Telemetry status and clock"
          >
            <span className="app-data-status-dot" aria-hidden="true" />
            <span className="app-data-status-copy">
              <strong role="status" aria-label={`${status.label}. ${statusDescription}`}>
                {status.label}
              </strong>
              <span>
                {lastUpdated ? (
                  <><RollingBatamClock key={lastUpdated} anchor={lastUpdated} /> WIB</>
                ) : 'Waiting for sync'}
              </span>
            </span>
          </div>

        </div>
      </div>
    </header>
  );
}
