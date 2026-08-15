/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Header } from './Header';

vi.mock('./Navigation', () => ({
  Navigation: () => <nav aria-label="Primary workspace navigation" />,
}));

let root: Root | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe('header Batam clock', () => {
  it('rolls forward once per second from the latest backend timestamp', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-09T07:30:00Z'));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    act(() => root?.render(
      <Header
        onOpenPitch={vi.fn()}
        activeTab="map"
        setActiveTab={vi.fn()}
        dataSource="simulated"
        lastUpdated="2026-08-09T14:30:00+07:00"
      />,
    ));
    const clock = container.querySelector<HTMLTimeElement>(
      '[aria-label="Telemetry status and Batam clock"] time',
    );

    expect(container.textContent).toContain('Batam time 14:30:00 WIB');
    act(() => vi.advanceTimersByTime(2_000));
    expect(clock?.textContent).toBe('14:30:02');
    expect(clock?.dateTime).toBe('2026-08-09T14:30:02.000+07:00');
  });
});
