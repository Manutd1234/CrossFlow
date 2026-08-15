/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ROUTE_LOCATIONS } from '../../data/mockData';
import { Header } from './Header';
import { Navigation } from './Navigation';
import { RouteOptimizer } from '../route-planner/RouteOptimizer';

let root: Root | undefined;

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

describe('application shell DOM', () => {
  it('exposes the active workspace and routes clicks through semantic buttons', () => {
    const setActiveTab = vi.fn();
    const container = renderIntoDom(
      <Navigation activeTab="map" setActiveTab={setActiveTab} />,
    );

    const navigation = container.querySelector(
      'nav[aria-label="Primary workspace navigation"]',
    );
    expect(navigation).not.toBeNull();

    const buttons = Array.from(navigation?.querySelectorAll('button') ?? []);
    expect(buttons).toHaveLength(3);
    expect(navigation?.querySelector('[role="tablist"]')).not.toBeNull();
    expect(buttons.every(button => button.getAttribute('role') === 'tab')).toBe(true);
    expect(buttons.every(button => button.type === 'button')).toBe(true);
    expect(buttons.filter(button => button.getAttribute('aria-current') === 'page'))
      .toHaveLength(1);
    expect(buttons[0].textContent).toContain('Congestion');

    const routeButton = buttons.find(button => button.textContent?.includes('Route'));
    expect(routeButton).toBeDefined();
    const analyticsButton = buttons.find(button => button.textContent?.includes('Analytics'));
    expect(analyticsButton?.querySelector('.lucide-chart-column')).not.toBeNull();
    act(() => buttons[0].dispatchEvent(new KeyboardEvent('keydown', {
      key: 'ArrowRight',
      bubbles: true,
    })));
    expect(setActiveTab).toHaveBeenCalledWith('route');
    expect(document.activeElement).toBe(routeButton);

    setActiveTab.mockClear();
    act(() => routeButton?.click());
    expect(setActiveTab).toHaveBeenCalledWith('route');
  });

  it('renders honest telemetry status without a presentation action', () => {
    const setActiveTab = vi.fn();
    const container = renderIntoDom(
      <Header
        activeTab="analytics"
        setActiveTab={setActiveTab}
        dataSource="simulated"
        provenance={{
          road_network: 'OpenStreetMap Batam Extract',
          road_network_license: 'ODbL',
          routing: 'A* road graph',
          traffic: 'Synthetic forecast',
        }}
        lastUpdated="2026-08-09T14:30:00+07:00"
      />,
    );

    expect(container.querySelector('header')).not.toBeNull();
    expect(container.querySelector('.app-brand-mark')).toBeNull();
    expect(container.textContent).not.toContain('Current workspace');
    expect(container.textContent).not.toContain('Road intelligence');
    expect(container.querySelector('[aria-label="Batam mobility workspace controls"]'))
      .not.toBeNull();
    expect(container.querySelector('header nav[aria-label="Primary workspace navigation"]'))
      .not.toBeNull();

    const status = container.querySelector('[role="status"]');
    expect(status?.getAttribute('aria-label')).toContain('Model Estimate');
    const telemetryGroup = container.querySelector(
      '[role="group"][aria-label="Telemetry status and clock"]',
    );
    expect(telemetryGroup?.textContent).toContain('Model Estimate14:30:00 WIB');
    expect(telemetryGroup?.textContent).not.toContain('Batam time');

    expect(container.querySelector('button[aria-haspopup="dialog"]')).toBeNull();
  });

  it('renders a single semantic route form with labelled controls and an empty state', () => {
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={null}
        setResult={vi.fn()}
        resultSource="simulated"
        setResultSource={vi.fn()}
        vehicleType="COMMUTER"
        setVehicleType={vi.fn()}
        routePreference="BALANCED"
        setRoutePreference={vi.fn()}
        weather={0}
        setWeather={vi.fn()}
        hour={14}
        setHour={vi.fn()}
      />,
    );

    const planner = container.querySelector('form[aria-label="Route planning controls"]');
    expect(planner).not.toBeNull();
    expect(container.querySelector('.route-planner-layout')?.classList.contains(
      'app-screen-layout',
    )).toBe(true);
    expect(Array.from(
      container.querySelector('.workspace-subtabs__rail')?.classList ?? [],
    )).toEqual(['workspace-subtabs__rail']);
    expect(planner?.querySelectorAll('form')).toHaveLength(0);
    expect(planner?.querySelectorAll('fieldset')).toHaveLength(3);
    const submitButton = planner?.querySelector('button[type="submit"]');
    expect(submitButton?.textContent).toContain('Plan Journey');
    expect(submitButton?.classList.contains('ui-button-primary')).toBe(true);
    expect(submitButton?.classList.contains('route-plan-submit-button')).toBe(false);
    expect(submitButton?.querySelector('svg')?.getAttribute('width')).toBe('18');

    const result = container.querySelector('section[aria-label="Route result"]');
    expect(result?.textContent).toContain('Your route will appear here');

    const modeButtons = Array.from(
      container.querySelectorAll<HTMLButtonElement>(
        '[role="group"][aria-label="Location selection mode"] button',
      ),
    );
    expect(modeButtons).toHaveLength(2);
    expect(modeButtons.every(button => button.type === 'button')).toBe(true);
  });
});
