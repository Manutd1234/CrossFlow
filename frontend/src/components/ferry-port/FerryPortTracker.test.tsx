/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import {
  PUBLISHED_FERRY_TIMETABLE_METADATA,
  offlineFerries,
} from '../../data/mockData';
import { scheduleInformedPortSeed } from '../../services/api';
import type { FerryRefreshReport } from '../../types';
import { toBatamIso } from '../../utils/batamTime';
import { FerryPortTracker } from './FerryPortTracker';

let root: Root | undefined;

const FIXED_NOW = new Date('2026-08-09T22:30:00.000Z');
const PORTS = scheduleInformedPortSeed(FIXED_NOW);
const REFRESH_REPORT: FerryRefreshReport = {
  refresh_id: 'official-source-check-20260810T053000+0700',
  status: 'partial',
  started_at: '2026-08-10T05:30:00+07:00',
  finished_at: '2026-08-10T05:30:00+07:00',
  refresh_scope: 'fixed_official_allowlist',
  source_results: [
    {
      source_id: 'batamfast-public-timetable',
      authority: 'BatamFast',
      kind: 'published_timetable',
      url: 'https://www.batamfast.com/tripschedule/index.ashx',
      permission_status: 'public_official_page',
      status: 'verified_structure',
      checked_at: '2026-08-10T05:30:00+07:00',
      http_status: 200,
      note: 'Public operator timetable.',
    },
    {
      source_id: 'scc-live-board-permission-gated',
      authority: 'Singapore Cruise Centre',
      kind: 'same_day_operations_board',
      url: 'https://singaporecruise.com.sg/schedule/ferries/',
      permission_status: 'written_permission_required',
      status: 'skipped_permission_required',
      checked_at: '2026-08-10T05:30:00+07:00',
      http_status: null,
      note: 'Permission required.',
    },
  ],
  summary: { verified: 1, failed: 1, permission_gated: 1 },
  schedule_applied: false,
  last_known_good_active: true,
  promotion_requirement: 'Calendar-aware validation is required.',
  data_changed: false,
  limitations: 'Only reviewed official pages are checked.',
};

const baseProps = {
  ferries: offlineFerries(FIXED_NOW, FIXED_NOW, 12),
  dataSource: 'offline' as const,
  timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
  ports: PORTS,
  portSource: 'offline' as const,
  portsLoading: false,
  portsError: null,
  isRefreshingOfficialSources: false,
  onRefreshOfficialSources: async () => REFRESH_REPORT,
};

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe('published ferry schedule view', () => {
  it('labels the browser fallback as a sourced snapshot without live details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    const container = renderIntoDom(
      <FerryPortTracker {...baseProps} />,
    );

    const layout = container.querySelector<HTMLElement>('.ferry-port-layout');
    const tabsLayout = container.querySelector<HTMLElement>('.ferry-port-tabs');
    expect(layout).not.toBeNull();
    expect(layout?.classList.contains('app-screen-layout')).toBe(true);
    expect(layout?.style.padding).toBe('');
    expect(tabsLayout).not.toBeNull();
    expect(tabsLayout?.querySelector(':scope > .workspace-subtabs__rail')).not.toBeNull();
    expect(tabsLayout?.querySelector(':scope > .workspace-subtabs__panel')).not.toBeNull();

    // The planning estimates are seeded synchronously, before the API request
    // settles, so a slow or offline backend never produces empty terminal cards.
    expect(container.querySelectorAll('.terminal-intelligence-card')).toHaveLength(4);
    expect(container.textContent).toContain('Passenger Queue');
    expect(container.textContent).toContain('Processing');
    expect(container.textContent).not.toContain('Unavailable');
    expect(container.textContent).not.toContain('Loading terminal queue and berth data');

    await act(async () => new Promise(resolve => window.setTimeout(resolve, 0)));

    const controls = container.querySelector('[aria-label="Ferry data controls"]');
    expect(controls).not.toBeNull();
    expect(container.textContent).not.toContain('PUBLISHED SCHEDULE SNAPSHOT');
    const refreshButton = container.querySelector('.ferry-port-overview__refresh-button');
    const overviewFooter = container.querySelector('.ferry-port-overview__footer');
    expect(refreshButton?.textContent).toContain('Check Schedules');
    expect(refreshButton?.classList.contains('ui-button-primary')).toBe(true);
    expect(refreshButton?.querySelector('svg')?.getAttribute('width')).toBe('18');
    expect(refreshButton?.parentElement).toBe(overviewFooter);
    expect(overviewFooter?.querySelector('[aria-label="Filter by departure port"]')).not.toBeNull();
    expect(container.textContent).not.toContain('OFFLINE FERRY FALLBACK');
    expect(container.textContent).toContain('Last verified 13 Aug 2026, 00:30 WIB');
    expect(controls?.querySelector('.ferry-port-overview__verified-badge')).not.toBeNull();
    expect(container.querySelector('.ferry-port-schedule-note')).toBeNull();
    expect(container.textContent).not.toContain('verify with the operator before travel');

    expect(container.textContent).not.toContain('Official live ferry board');

    const sailings = container.querySelector('#sailings-title')?.closest('section');
    expect(sailings?.textContent).toContain('SCHEDULED');
    expect(sailings?.textContent).toContain('Official operator timetable');
    expect(sailings?.textContent).not.toContain('seats');
    expect(sailings?.querySelector('.ferry-departure-card__verified-at')).toBeNull();
    expect(sailings?.querySelector('.badge-critical')).toBeNull();

    expect(container.textContent).toContain('SCHEDULE-INFORMED CONTINUITY');
    expect(container.textContent).not.toContain('OFFLINE PORT FALLBACK');
    expect(container.textContent).toContain('Passenger Queue');
    expect(container.textContent).not.toContain('Unavailable');
    expect(container.textContent).not.toContain('Planning estimate, not a sensor observation');
    expect(container.textContent).toContain('BP Batam Terminal Reference');
    const terminalPhotos = Array.from(
      container.querySelectorAll<HTMLImageElement>('.terminal-card-media img'),
    );
    expect(terminalPhotos).toHaveLength(4);
    expect(Array.from(container.querySelectorAll('.terminal-card-heading'))
      .every(heading => heading.querySelector('.terminal-card-media'))).toBe(true);
    expect(container.querySelectorAll('.terminal-card-code')).toHaveLength(4);
    expect(container.querySelector('.terminal-card-photo-context')).toBeNull();
    expect(terminalPhotos.some(photo => photo.src.includes('bpbatam.go.id'))).toBe(false);
    expect(container.textContent).toContain('Masgatotkaca');
    expect(container.textContent).toContain('Exbeing');
    expect(container.textContent).toContain('alantankenghoe');
    expect(container.textContent).toContain('Lobster1');
    expect(container.textContent).toContain('Nongsa area reference, 2014.');
    expect(container.querySelectorAll('.terminal-card-photo-license')).toHaveLength(4);

    const nongsaPhoto = terminalPhotos.find(photo => photo.alt.includes('Nongsa area'));
    expect(nongsaPhoto).toBeDefined();
    act(() => nongsaPhoto?.dispatchEvent(new Event('error', { bubbles: true })));
    expect(container.textContent).toContain('Location photo unavailable');
    expect(container.querySelector('[aria-label="Nongsa Pura location photo unavailable"]')).not.toBeNull();
    expect(container.querySelectorAll('.terminal-card-media img')).toHaveLength(3);
    expect(container.textContent).toContain('Next departure');
    expect(container.textContent).not.toContain('Batam Fast 502');
  });

  it('shows one workspace panel and preserves terminal filter and photo state across tabs', () => {
    const container = renderIntoDom(<FerryPortTracker {...baseProps} />);
    const tabs = Array.from(container.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const panels = Array.from(container.querySelectorAll<HTMLElement>('[role="tabpanel"]'));
    const terminalsTab = tabs.find(tab => tab.textContent?.includes('Terminals'));
    const departuresTab = tabs.find(tab => tab.textContent?.includes('Departures'));
    const cargoTab = tabs.find(tab => tab.textContent?.includes('Cargo'));

    expect(tabs).toHaveLength(3);
    expect(panels.filter(panel => !panel.hidden)).toHaveLength(1);
    expect(terminalsTab?.getAttribute('aria-selected')).toBe('true');
    expect(panels.find(panel => !panel.hidden)?.querySelector(
      '[aria-label="Terminal access and processing outlook"]',
    )).not.toBeNull();
    expect(container.querySelector('#terminal-status-title')).toBeNull();

    const harbourBayFilter = Array.from(container.querySelectorAll<HTMLButtonElement>(
      '[aria-label="Filter by departure port"] button',
    )).find(button => button.textContent === 'HarbourBay');
    expect(harbourBayFilter?.classList.contains('ui-button-choice')).toBe(true);
    expect(harbourBayFilter?.classList.contains('ui-sand-interactive')).toBe(true);
    act(() => harbourBayFilter?.click());

    const terminalsPanel = container.querySelector<HTMLElement>('#ferry-workspace-panel-0');
    expect(harbourBayFilter?.getAttribute('aria-pressed')).toBe('true');
    expect(terminalsPanel?.querySelectorAll('.terminal-intelligence-card')).toHaveLength(1);
    expect(container.querySelector('.ferry-port-overview')?.classList)
      .toContain('ui-white-card-hover');
    const terminalCard = terminalsPanel?.querySelector('.terminal-intelligence-card');
    expect(terminalCard?.classList).toContain('terminal-card-content');
    expect(terminalCard?.classList).toContain('ui-white-card-hover');
    expect(terminalCard?.querySelector('.terminal-card-content')).toBeNull();
    expect(terminalsPanel?.querySelector('[role="progressbar"]')?.classList)
      .toContain('ui-progress-track');
    expect(terminalsPanel?.querySelector('[role="progressbar"]')?.firstElementChild?.classList)
      .toContain('ui-progress-fill');
    const harbourBayPhoto = terminalsPanel?.querySelector<HTMLImageElement>(
      '.terminal-card-media img',
    );
    expect(harbourBayPhoto?.alt).toContain('Harbour Bay');
    act(() => harbourBayPhoto?.dispatchEvent(new Event('error', { bubbles: true })));
    expect(terminalsPanel?.textContent).toContain('Location photo unavailable');

    act(() => departuresTab?.click());
    expect(departuresTab?.getAttribute('aria-selected')).toBe('true');
    expect(panels.filter(panel => !panel.hidden)).toHaveLength(1);
    expect(container.querySelector<HTMLElement>('#ferry-workspace-panel-1')?.hidden).toBe(false);
    expect(container.querySelector('#ferry-workspace-panel-1')?.textContent).toContain(
      'Upcoming Departures',
    );

    act(() => cargoTab?.click());
    expect(cargoTab?.getAttribute('aria-selected')).toBe('true');
    expect(panels.filter(panel => !panel.hidden)).toHaveLength(1);
    expect(container.querySelector('#ferry-workspace-panel-2')?.textContent).toContain(
      'Batam-SG Cargo Monitor',
    );
    expect(container.querySelector('#ferry-workspace-panel-2 .cargo-monitor__description')).toBeNull();
    expect(container.querySelector('#ferry-workspace-panel-2 [role="progressbar"]')?.classList)
      .toContain('ui-progress-track');
    expect(container.querySelector('#ferry-workspace-panel-2 [role="progressbar"]')?.firstElementChild?.classList)
      .toContain('ui-progress-fill');

    act(() => terminalsTab?.click());
    expect(terminalsTab?.getAttribute('aria-selected')).toBe('true');
    expect(harbourBayFilter?.getAttribute('aria-pressed')).toBe('true');
    expect(terminalsPanel?.querySelectorAll('.terminal-intelligence-card')).toHaveLength(1);
    expect(terminalsPanel?.textContent).toContain('Location photo unavailable');
  });

  it('colors the verification badge by Batam calendar-day freshness', () => {
    const currentVerification = toBatamIso(new Date());
    const staleVerification = toBatamIso(new Date(Date.now() - 48 * 60 * 60 * 1000));
    const container = renderIntoDom(
      <FerryPortTracker
        {...baseProps}
        timetable={{ ...baseProps.timetable, last_verified_at: currentVerification }}
      />,
    );
    const verificationBadge = container.querySelector('.ferry-port-overview__verified-badge');

    expect(verificationBadge?.classList.contains('badge-smooth')).toBe(true);
    expect(verificationBadge?.classList.contains('badge-heavy')).toBe(false);

    act(() => root?.render(
      <FerryPortTracker
        {...baseProps}
        timetable={{ ...baseProps.timetable, last_verified_at: staleVerification }}
      />,
    ));

    expect(verificationBadge?.classList.contains('badge-smooth')).toBe(false);
    expect(verificationBadge?.classList.contains('badge-heavy')).toBe(true);
  });

  it('runs one coordinated refresh and reports verified and gated sources', async () => {
    let resolveRefresh: ((report: FerryRefreshReport) => void) | undefined;
    const onRefresh = vi.fn(() => new Promise<FerryRefreshReport>((resolve) => {
      resolveRefresh = resolve;
    }));
    const container = renderIntoDom(
      <FerryPortTracker {...baseProps} onRefreshOfficialSources={onRefresh} />,
    );
    const button = container.querySelector<HTMLButtonElement>(
      '.ferry-port-overview__refresh-button',
    );

    expect(button).not.toBeNull();
    act(() => button?.click());
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(button?.disabled).toBe(true);
    expect(button?.getAttribute('aria-busy')).toBe('true');
    expect(container.textContent).toContain(
      'Checking reviewed official ferry and terminal sources',
    );

    await act(async () => {
      resolveRefresh?.(REFRESH_REPORT);
      await Promise.resolve();
    });

    expect(button?.disabled).toBe(false);
    expect(container.textContent).toContain(
      'Checked 1 official source page; 1 could not be validated',
    );
    expect(container.textContent).toContain('BatamFast');
    expect(container.textContent).toContain('Singapore Cruise Centre');
    expect(container.textContent).toContain('Permission required');
    expect(container.querySelectorAll('.ferry-port-source-results .badge')).toHaveLength(0);
    expect(container.querySelectorAll('.ferry-port-source-results .ferry-port-source-status-icon'))
      .toHaveLength(2);
    expect(container.querySelectorAll('.ferry-port-source-legend__item')).toHaveLength(3);
    expect(container.querySelector('svg[aria-label="Page checked"]')).not.toBeNull();
    expect(container.querySelector('svg[aria-label="Permission required"]')).not.toBeNull();
    const gatedSourceLink = Array.from(container.querySelectorAll<HTMLAnchorElement>('a'))
      .find(link => link.textContent === 'Singapore Cruise Centre');
    expect(gatedSourceLink).toBeUndefined();
  });
});
