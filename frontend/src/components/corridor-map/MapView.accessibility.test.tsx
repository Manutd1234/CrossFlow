/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import L from 'leaflet';
import { MapView } from './MapView';
import { corridorColor } from '../../utils/format';
import type { Corridor, Fetched, LiveTrafficData } from '../../types';

const leafletTestState = vi.hoisted(() => ({
  corridorClickHandlers: [] as Array<() => void>,
  corridorLayers: [] as Array<Record<string, ReturnType<typeof vi.fn>>>,
  hotspotClickHandlers: [] as Array<() => void>,
  map: vi.fn(),
  mapInstance: { fitBounds: vi.fn() } as { fitBounds: ReturnType<typeof vi.fn> },
}));

vi.mock('leaflet', () => {
  const makeLayer = (clickHandlers?: Array<() => void>) => {
    const layer = {
      addTo: vi.fn(),
      bindTooltip: vi.fn(),
      bringToFront: vi.fn(),
      getBounds: vi.fn(() => ({ pad: vi.fn(() => 'bounds') })),
      on: vi.fn(),
      openTooltip: vi.fn(),
      remove: vi.fn(),
      setStyle: vi.fn(),
    };
    layer.addTo.mockReturnValue(layer);
    layer.bindTooltip.mockReturnValue(layer);
    layer.on.mockImplementation((event: string, handler: () => void) => {
      if (event === 'click') clickHandlers?.push(handler);
      return layer;
    });
    return layer;
  };

  const map = {
    fitBounds: vi.fn(),
    getZoom: vi.fn(() => 11),
    remove: vi.fn(),
    scrollWheelZoom: {
      disable: vi.fn(),
      enable: vi.fn(),
    },
    setView: vi.fn(),
  };
  map.setView.mockReturnValue(map);
  leafletTestState.map.mockImplementation(() => map);
  leafletTestState.mapInstance = map;

  return {
    default: {
      circle: vi.fn(() => makeLayer(leafletTestState.hotspotClickHandlers)),
      circleMarker: vi.fn(() => makeLayer()),
      control: { zoom: vi.fn(() => makeLayer()) },
      map: leafletTestState.map,
      polyline: vi.fn(() => {
        const layer = makeLayer(leafletTestState.corridorClickHandlers);
        leafletTestState.corridorLayers.push(layer);
        return layer;
      }),
      tileLayer: vi.fn(() => makeLayer()),
    },
  };
});

let root: Root | undefined;

const TEST_CORRIDORS: Corridor[] = [
  {
    id: 'corridor-1',
    name: 'Mukakuning Industrial -> Batam Centre Terminal',
    distance_km: 9.91,
    base_time_mins: 18,
    live_congestion_score: 72,
    delay_mins: 8,
    status: 'CRITICAL',
    risk_level: 'HIGH',
    forecast_30m: 78,
    forecast_60m: 81,
    trend: 'UPWARD',
    key_checkpoints: ['Mukakuning Gate', 'Batam Centre Ferry'],
  },
];

function trafficSnapshot(selectionScore: number): Fetched<LiveTrafficData> {
  return {
    source: 'simulated',
    fetchedAt: '2026-08-13T12:00:00+07:00',
    data: {
      segments: [],
      zones: [{
        zone_id: 'zone-simpang-jam',
        name: 'Simpang Jam / Laluan Madani',
        category: 'Urban junction',
        lat: 1.122424,
        lng: 104.019539,
        radius_m: 650,
        congestion_index: 63.4,
        level: 'HEAVY',
        color: '#f59e0b',
        avoid_recommended: false,
        watch_priority: 'HEAVY',
        selection_rank: 1,
        selection_score: selectionScore,
        source: 'modelled_spatial_hotspot',
        modeled_emissions_pressure: {
          index: 40.2,
          queue_pressure_factor: 0.402,
          level: 'ELEVATED',
          metric: 'relative_queue_emissions_pressure',
          unit: 'index_0_100',
          observed: false,
        },
      }],
      overall_source: 'simulated',
      tomtom_key_configured: false,
    },
  };
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
  leafletTestState.corridorClickHandlers.length = 0;
  leafletTestState.corridorLayers.length = 0;
  leafletTestState.hotspotClickHandlers.length = 0;
  leafletTestState.map.mockClear();
  leafletTestState.mapInstance.fitBounds.mockClear();
  vi.mocked(L.polyline).mockClear();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe('MapView traffic overlay controls', () => {
  it('disables wheel zoom in the small-width layout', () => {
    const mediaQuery = {
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal('matchMedia', vi.fn(() => mediaQuery));

    renderIntoDom(
      <MapView
        corridors={[]}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    expect(leafletTestState.map).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({ scrollWheelZoom: false }),
    );
    expect(mediaQuery.addEventListener).toHaveBeenCalledWith(
      'change',
      expect.any(Function),
    );
  });

  it('names and reports the combined planning-watch and traffic overlays', () => {
    const container = renderIntoDom(
      <MapView
        corridors={[]}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    const mapPanel = container.querySelector<HTMLElement>('.map-canvas-panel');
    const mapLayout = container.querySelector('.map-view-layout');
    expect(mapLayout?.classList.contains(
      'app-screen-layout',
    )).toBe(true);
    expect(mapLayout?.querySelector(':scope > .workspace-subtabs__rail')).not.toBeNull();
    expect(mapLayout?.querySelector(':scope > .workspace-subtabs__panel')).not.toBeNull();
    expect(container.querySelector('.map-view-sidebar')).toBeNull();
    expect(mapPanel?.style.height).toBe('');
    expect(mapPanel?.closest('[role="tabpanel"]')).toBeNull();
    expect(container.querySelectorAll('.hotspot-card')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card.ui-sand-interactive')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card__credit-meta')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card__credit-meta a')).toHaveLength(60);
    expect(container.textContent).toContain('30-Area Congestion Watch');
    expect(container.textContent).toContain('20 critical');
    expect(container.textContent).toContain('10 heavy');
    expect(L.polyline).not.toHaveBeenCalled();
    expect(leafletTestState.corridorClickHandlers).toHaveLength(0);

    const toggle = container.querySelector<HTMLButtonElement>('.map-traffic-toggle');
    expect(toggle?.getAttribute('aria-pressed')).toBe('true');
    expect(toggle?.hasAttribute('aria-controls')).toBe(false);
    expect(toggle?.getAttribute('aria-label'))
      .toBe('Hide planning-watch overlays');
    expect(toggle?.textContent).toBe('');
    expect(toggle?.querySelector('svg')?.getAttribute('width')).toBe('18');
    expect(container.querySelector('.map-radar-card .map-traffic-toggle')).toBeNull();

    expect(container.querySelector('#traffic-overlay-layer-status')).toBeNull();

    act(() => toggle?.click());
    expect(toggle?.getAttribute('aria-pressed')).toBe('false');
    expect(toggle?.getAttribute('aria-label'))
      .toBe('Show planning-watch overlays');
    expect(toggle?.textContent).toBe('');
  });

  it('keeps the map visible while corridor details and feeds share one tab', () => {
    const container = renderIntoDom(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]'),
    );
    const mapPanel = container.querySelector<HTMLElement>('.map-canvas-panel');

    expect(tabs.map(tab => tab.textContent)).toEqual([
      'Hotspots',
      'Corridor',
    ]);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(mapPanel?.closest('[role="tabpanel"]')).toBeNull();

    act(() => tabs[1].click());
    const feedButton = container.querySelector<HTMLButtonElement>(
      '[aria-label^="Select Mukakuning Industrial"]',
    );
    expect(feedButton?.classList.contains('ui-sand-interactive')).toBe(true);
    act(() => feedButton?.click());

    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(tabs[1]);
    expect(container.querySelector('#selected-corridor-title')?.textContent)
      .toContain('Mukakuning Industrial');
    expect(container.querySelector('#corridor-feed-title')).not.toBeNull();
    expect(container.querySelector('.map-corridor-stack')?.children).toHaveLength(2);
    expect(mapPanel?.closest('[hidden]')).toBeNull();

    act(() => leafletTestState.hotspotClickHandlers[0]?.());

    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('.hotspot-card.is-focused')?.id)
      .toBe('hotspot-card-zone-simpang-jam');
  });

  it('draws corridor road geometry coloured by congestion on the Corridor tab', () => {
    const secondCorridor: Corridor = {
      id: 'corridor-2',
      name: 'Batu Ampar Freight Port -> Batam Centre Ferry',
      distance_km: 8.4,
      base_time_mins: 16,
      live_congestion_score: 31,
      delay_mins: 2,
      status: 'SMOOTH',
      risk_level: 'LOW',
      forecast_30m: 29,
      forecast_60m: 27,
      trend: 'STABLE',
      key_checkpoints: ['Batu Ampar Gate 2', 'Batam Centre'],
    };
    const container = renderIntoDom(
      <MapView
        corridors={[...TEST_CORRIDORS, secondCorridor]}
        routes={[{
          id: 'corridor-1',
          name: 'Mukakuning Industrial -> Batam Centre Terminal',
          distance_km: 9.91,
          straight_line_km: 7.2,
          detour_ratio: 1.38,
          geometry: [[1.0605, 104.0303], [1.1, 104.04], [1.1318, 104.0554]],
        }]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    // The Hotspots tab stays free of route lines; they belong to the panel
    // that actually reports a corridor.
    expect(L.polyline).not.toHaveBeenCalled();

    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]'),
    );
    act(() => tabs[1].click());

    // One casing plus one line for each of the five corridors.
    expect(L.polyline).toHaveBeenCalledTimes(10);
    const calls = vi.mocked(L.polyline).mock.calls;
    const geometry = [[1.0605, 104.0303], [1.1, 104.04], [1.1318, 104.0554]];
    // corridor-1 has real A* geometry: casing and stroke both trace the road.
    expect(calls[0][0]).toEqual(geometry);
    expect(calls[1][0]).toEqual(geometry);
    expect(calls[1][1]).not.toHaveProperty('dashArray', expect.any(String));
    // corridor-2 has none yet, so it falls back to a two-point placeholder and
    // is dashed rather than passed off as a surveyed road path.
    expect(calls[3][0]).toHaveLength(2);
    expect(calls[3][1]).toMatchObject({ dashArray: '8, 6' });

    // corridor-1 is selected, so it is emphasised and carries its own status
    // colour rather than a single flat stroke for the whole network.
    const styles = leafletTestState.corridorLayers.flatMap(
      layer => vi.mocked(layer.setStyle).mock.calls.map(call => call[0]),
    );
    const selected = styles.find(style => style.weight === 7);
    expect(selected?.color).toBe(corridorColor('CRITICAL'));
    expect(selected?.opacity).toBe(1);
    // The unselected corridor is subdued and carries its own status colour, so
    // the map cannot disagree with the badge in the feed beneath it.
    const unselected = styles.find(style => style.weight === 4);
    expect(unselected?.color).toBe(corridorColor('SMOOTH'));
    expect(unselected?.opacity).toBe(0.75);

    // Clicking the feed frames the corridor the panel is describing.
    act(() => container.querySelector<HTMLButtonElement>(
      '[aria-label^="Select Batu Ampar"]',
    )?.click());
    expect(leafletTestState.mapInstance.fitBounds).toHaveBeenCalled();
  });

  it('uses backend hotspot weights while retaining photo metadata', () => {
    const onSelectCorridor = vi.fn();
    const container = renderIntoDom(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={trafficSnapshot(71.3)}
        onSelectCorridor={onSelectCorridor}
      />,
    );
    const card = container.querySelector('#hotspot-card-zone-simpang-jam');

    expect(card?.querySelector('.hotspot-card__score')?.textContent).toBe('71.3');
    expect(card?.querySelector('.hotspot-card__priority')?.textContent).toContain('HEAVY');
    expect(card?.querySelector('img')?.getAttribute('src')).toContain('wikimedia.org');
    expect(container.querySelectorAll('.hotspot-card')).toHaveLength(30);

    act(() => root?.render(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={trafficSnapshot(77.8)}
        onSelectCorridor={onSelectCorridor}
      />,
    ));

    expect(container.querySelector('#hotspot-card-zone-simpang-jam .hotspot-card__score')?.textContent)
      .toBe('77.8');
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });
});
