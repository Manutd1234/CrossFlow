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

  it('offers route preferences and adds intermediate location fields', () => {
    const container = renderIntoDom(<VehicleHarness />);
    // All five audited objectives stay selectable: the solver honours each of
    // them, so hiding the control would strand a documented capability.
    const preferences = Array.from(
      container.querySelectorAll('.route-preference-option'),
    ).map((option) => option.textContent);
    expect(preferences).toHaveLength(5);
    expect(preferences.join(' ')).toContain('Balanced');
    expect(preferences.join(' ')).toContain('Local');

    const swap = container.querySelector<HTMLButtonElement>('.route-swap-button');
    const add = container.querySelector<HTMLButtonElement>('.route-add-stop-button');
    expect(swap).not.toBeNull();
    expect(add?.getAttribute('aria-label')).toBe('Add an intermediate stop');

    act(() => add?.click());
    const waypoint = container.querySelector('.route-waypoint-row');
    expect(waypoint).not.toBeNull();
    expect(waypoint?.textContent).toContain('Stop 1');
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Remove stop 1"]')).not.toBeNull();
  });
});
