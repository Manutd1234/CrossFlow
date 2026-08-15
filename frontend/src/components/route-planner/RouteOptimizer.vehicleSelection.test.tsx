/* @vitest-environment jsdom */

import { act, useState, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ROUTE_LOCATIONS } from '../../data/mockData';
import type { RoutePreference, VehicleType } from '../../types';
import { RouteOptimizer } from './RouteOptimizer';

vi.mock('./RoutePreviewMap', () => ({ RoutePreviewMap: () => null }));

let root: Root | undefined;

function VehicleHarness() {
  const [vehicleType, setVehicleType] = useState<VehicleType>('COMMUTER');
  const [routePreference, setRoutePreference] = useState<RoutePreference>('BALANCED');
  return (
    <RouteOptimizer
      locations={ROUTE_LOCATIONS}
      originId="mukakuning"
      setOriginId={vi.fn()}
      destinationId="batam_centre"
      setDestinationId={vi.fn()}
      result={null}
      setResult={vi.fn()}
      resultSource="offline"
      setResultSource={vi.fn()}
      vehicleType={vehicleType}
      setVehicleType={setVehicleType}
      routePreference={routePreference}
      setRoutePreference={setRoutePreference}
      weather={0}
      setWeather={vi.fn()}
      hour={14}
      setHour={vi.fn()}
    />
  );
}

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
});

describe('vehicle profile selection', () => {
  it('offers all eight grouped profiles with details in one labelled control', () => {
    const container = renderIntoDom(<VehicleHarness />);
    const trigger = container.querySelector<HTMLButtonElement>('#route-vehicle-type');

    expect(trigger).not.toBeNull();
    expect(trigger?.classList.contains('ui-sand-interactive')).toBe(true);
    expect(trigger?.getAttribute('aria-haspopup')).toBe('listbox');
    expect(trigger?.getAttribute('aria-expanded')).toBe('false');
    expect(trigger?.textContent).toContain('Car / taxi');
    expect(trigger?.textContent).toContain('Balanced speeds and public-road access');

    act(() => trigger?.click());

    const menu = container.querySelector<HTMLElement>('#route-vehicle-menu');
    const options = Array.from(
      menu?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [],
    );
    expect(menu).not.toBeNull();
    expect(options).toHaveLength(8);
    expect(menu?.querySelectorAll('[role="group"]')).toHaveLength(4);
    expect(options.find(option => option.textContent?.includes('City bus'))?.textContent)
      .toBe('City bus');

    const cityBus = options.find(option => option.textContent?.includes('City bus'));
    act(() => cityBus?.click());

    expect(trigger?.textContent).toContain('City bus');
    expect(trigger?.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('#route-vehicle-assumption')?.textContent)
      .toContain('bus lanes are not modelled');
  });

  it('offers five accessible preferences and gates local roads by vehicle size', () => {
    const container = renderIntoDom(<VehicleHarness />);
    const fieldset = container.querySelector('.route-preference-fieldset');
    const buttons = Array.from(
      fieldset?.querySelectorAll<HTMLButtonElement>('button') ?? [],
    );

    expect(fieldset?.querySelector('legend')?.textContent).toContain('Route preference');
    expect(buttons).toHaveLength(5);
    expect(buttons.every(button => button.classList.contains('ui-sand-interactive'))).toBe(true);
    expect(buttons.map(button => button.textContent)).toEqual(expect.arrayContaining([
      expect.stringContaining('Balanced'),
      expect.stringContaining('Fastest'),
      expect.stringContaining('Shortest'),
      expect.stringContaining('Easy'),
      expect.stringMatching(/Local shortcuts/i),
    ]));
    expect(buttons.find(button => button.textContent?.includes('Balanced'))
      ?.getAttribute('aria-pressed')).toBe('true');

    const easy = buttons.find(button => button.textContent?.includes('Easy'));
    act(() => easy?.click());
    expect(easy?.getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelector('#route-preference-selected-description')?.textContent)
      .toContain('Prefer through roads with fewer difficult maneuvers');

    const local = buttons.find(button => /Local shortcuts/i.test(button.textContent ?? ''));
    expect(local?.disabled).toBe(false);
    act(() => local?.click());
    expect(local?.getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelector('#route-preference-selected-description')?.textContent)
      .toContain('Seek compact routes over mapped public residential roads');

    const vehicleTrigger = container.querySelector<HTMLButtonElement>('#route-vehicle-type');
    act(() => vehicleTrigger?.click());
    const cityBus = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[role="option"]'),
    ).find(option => option.textContent?.includes('City bus'));
    act(() => cityBus?.click());

    expect(local?.disabled).toBe(true);
    expect(local?.getAttribute('aria-describedby')).toBe('route-preference-local-unavailable');
    expect(container.querySelector('#route-preference-local-unavailable')?.textContent)
      .toContain('too large for unverified narrow-road clearance');
    expect(buttons.find(button => button.textContent?.includes('Balanced'))
      ?.getAttribute('aria-pressed')).toBe('true');
  });
});
