/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { FERRY_SEA_ROUTE } from '../../data/mockData';
import type { PlanningTrafficSnapshot, RoadRouteOption } from '../../types';
import { MAP_PALETTE } from '../../theme/mapPalette';
import { RoutePreviewMap } from './RoutePreviewMap';

const leafletSpies = vi.hoisted(() => ({
  circle: vi.fn(),
  circleMarker: vi.fn(),
  divIcon: vi.fn(),
  inactiveRouteClicks: [] as Array<() => void>,
  map: vi.fn(),
  marker: vi.fn(),
  polyline: vi.fn(),
  zoomControl: vi.fn(),
}));

vi.mock('leaflet', () => {
  const makeBounds = () => {
    const bounds = {
      extend: vi.fn(),
      pad: vi.fn(),
    };
    bounds.extend.mockReturnValue(bounds);
    bounds.pad.mockReturnValue(bounds);
    return bounds;
  };

  const makeLayer = () => {
    const layer = {
      addTo: vi.fn(),
      bindTooltip: vi.fn(),
      getBounds: vi.fn(() => makeBounds()),
      on: vi.fn(),
    };
    layer.addTo.mockReturnValue(layer);
    layer.bindTooltip.mockReturnValue(layer);
    layer.on.mockImplementation((event: string, handler: () => void) => {
      if (event === 'click') leafletSpies.inactiveRouteClicks.push(handler);
      return layer;
    });
    return layer;
  };

  leafletSpies.polyline.mockImplementation(() => makeLayer());
  leafletSpies.divIcon.mockImplementation(options => options);
  leafletSpies.circle.mockImplementation(() => makeLayer());
  leafletSpies.circleMarker.mockImplementation(() => makeLayer());
  leafletSpies.marker.mockImplementation(() => makeLayer());
  leafletSpies.zoomControl.mockImplementation(() => makeLayer());

  const map = {
    fitBounds: vi.fn(),
    invalidateSize: vi.fn(),
    remove: vi.fn(),
    setView: vi.fn(),
  };
  map.setView.mockReturnValue(map);
  leafletSpies.map.mockImplementation(() => map);

  const layerGroup = () => {
    const group = { addTo: vi.fn(), clearLayers: vi.fn() };
    group.addTo.mockReturnValue(group);
    return group;
  };

  return {
    default: {
      circle: leafletSpies.circle,
      circleMarker: leafletSpies.circleMarker,
      control: { zoom: leafletSpies.zoomControl },
      divIcon: leafletSpies.divIcon,
      layerGroup,
      map: leafletSpies.map,
      marker: leafletSpies.marker,
      polyline: leafletSpies.polyline,
      tileLayer: vi.fn(() => makeLayer()),
    },
  };
});

let root: Root | undefined;

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

const primaryGeometry: [number, number][] = [
  [1.06, 104.03],
  [1.09, 104.04],
  [1.13, 104.055],
];
const alternativeGeometry: [number, number][] = [
  [1.06, 104.03],
  [1.08, 104.07],
  [1.13, 104.055],
];

const routes: RoadRouteOption[] = [{
  id: 'primary',
  name: 'Recommended route',
  description: 'Lowest routing cost.',
  route_geometry: primaryGeometry,
  distance_km: 8.2,
  estimated_travel_time_mins: 17,
  co2_emissions_kg: 1.7,
  co2_saved_kg: 0.4,
  route_data_source: 'bundled_client_openstreetmap',
  routing_cost_breakdown: {
    free_flow_mins: 14,
    congestion_delay_mins: 1.2,
    weather_delay_mins: 0,
    maneuver_delay_mins: 1.8,
    road_suitability_penalty_mins: 1.1,
    modeled_travel_time_mins: 17,
    generalized_cost_mins: 18.1,
  },
  navigation: {
    landmarks_along_route: [],
    traffic_lights_count: 0,
    route_narrative_words: 'Follow the recommended corridor to the ferry terminal.',
    maneuvers: [
      { step: 1, type: 'continue', modifier: 'straight', instruction: 'Continue', street: 'Road', distance_m: 100, icon: 'continue', coords: [1.065, 104.035] },
      { step: 2, type: 'turn', modifier: 'slight_left', instruction: 'Bear left', street: 'Road', distance_m: 100, icon: 'turn_left', coords: [1.07, 104.036] },
      { step: 3, type: 'turn', modifier: 'sharp_right', instruction: 'Turn sharply right', street: 'Road', distance_m: 100, icon: 'turn_right', coords: [1.08, 104.038] },
      { step: 4, type: 'roundabout', modifier: 'roundabout', instruction: 'Enter roundabout', street: 'Road', distance_m: 100, icon: 'turn_right', coords: [1.09, 104.04] },
      { step: 5, type: 'TAKE_RAMP', modifier: 'take_ramp', instruction: 'Take the ramp', street: 'Ramp', distance_m: 100, icon: 'turn_right', coords: [1.1, 104.045] },
      { step: 6, type: 'U_TURN', modifier: 'u_turn', instruction: 'Make a U-turn', street: 'Road', distance_m: 100, icon: 'turn_left', coords: [1.11, 104.05] },
    ],
  },
}, {
  id: 'osm-alternative-1',
  name: 'Alternative road route 1',
  description: 'Distinct eastern corridor.',
  route_geometry: alternativeGeometry,
  distance_km: 9.6,
  estimated_travel_time_mins: 24,
  co2_emissions_kg: 2.4,
  co2_saved_kg: 0.1,
  route_data_source: 'bundled_client_openstreetmap',
}];

const planningTrafficSnapshot: PlanningTrafficSnapshot = {
  schema_version: 1,
  effective_at: '2026-08-10T14:00:00+07:00',
  weather: 0,
  source: 'modelled_spatial_hotspots',
  observed: false,
  applied_to_returned_route: true,
  zone_count: 3,
  congestion_level_counts: { SMOOTH: 1, HEAVY: 1, SUPER_CONGESTED: 1 },
  zones: [
    { zone_id: 'smooth', name: 'Smooth area', lat: 1.1, lng: 104.01, radius_m: 600, congestion_index: 30, level: 'SMOOTH', color: '#10b981', avoid_recommended: false, modeled_emissions_pressure: { index: 9, queue_pressure_factor: 0.09, level: 'LOW', metric: 'relative_queue_emissions_pressure', unit: 'index_0_100', observed: false } },
    { zone_id: 'heavy', name: 'Heavy area', lat: 1.11, lng: 104.02, radius_m: 650, congestion_index: 55, level: 'HEAVY', color: '#f59e0b', avoid_recommended: false, modeled_emissions_pressure: { index: 30.3, queue_pressure_factor: 0.3025, level: 'ELEVATED', metric: 'relative_queue_emissions_pressure', unit: 'index_0_100', observed: false } },
    { zone_id: 'critical', name: 'Critical area', lat: 1.12, lng: 104.03, radius_m: 700, congestion_index: 82, level: 'SUPER_CONGESTED', color: '#ef4444', avoid_recommended: true, modeled_emissions_pressure: { index: 67.2, queue_pressure_factor: 0.6724, level: 'HIGH', metric: 'relative_queue_emissions_pressure', unit: 'index_0_100', observed: false } },
  ],
  emissions_pressure_model: {
    schema_version: 1,
    methodology_version: 'crossflow-zone-pressure-v1',
    formula: 'pressure_index=100*(congestion_index/100)^2',
    thresholds: { ELEVATED: 16, HIGH: 49 },
    traffic_input: 'selected route planning conditions',
    source: 'crossflow_congestion_delay_model',
    observed: false,
    aggregate_mass_available: false,
    limitations: 'Relative proxy; not measured area emissions.',
  },
  routing_effect: 'Positive scores increase local A* edge cost with radial decay.',
  limitations: 'Modelled planning areas, not measured area emissions.',
};

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  vi.unstubAllGlobals();
  document.body.replaceChildren();
  leafletSpies.divIcon.mockClear();
  leafletSpies.circle.mockClear();
  leafletSpies.circleMarker.mockClear();
  leafletSpies.marker.mockClear();
  leafletSpies.map.mockClear();
  leafletSpies.polyline.mockClear();
  leafletSpies.zoomControl.mockClear();
  leafletSpies.inactiveRouteClicks.length = 0;
});

describe('route preview alternatives', () => {
  it.each([
    { layout: 'small-width', smallWidth: true, smallHeight: false },
    { layout: 'small-height', smallWidth: false, smallHeight: true },
  ])('disables wheel zoom in the $layout layout', ({ smallWidth, smallHeight }) => {
    const mediaQueries = {
      '(max-width: 1000px)': {
        matches: smallWidth,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
      '(max-height: 999px)': {
        matches: smallHeight,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    };
    vi.stubGlobal('matchMedia', vi.fn((query: keyof typeof mediaQueries) => mediaQueries[query]));

    renderIntoDom(
      <RoutePreviewMap
        routes={routes}
        selectedRouteId="primary"
        onSelectRoute={vi.fn()}
      />,
    );

    expect(leafletSpies.map).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({ scrollWheelZoom: false }),
    );
    expect(mediaQueries['(max-width: 1000px)'].addEventListener).toHaveBeenCalledWith(
      'change',
      expect.any(Function),
    );
    expect(mediaQueries['(max-height: 999px)'].addEventListener).toHaveBeenCalledWith(
      'change',
      expect.any(Function),
    );
  });

  it('draws active and inactive road geometry and exposes both map selection paths', async () => {
    const onSelectRoute = vi.fn();
    const container = renderIntoDom(
      <RoutePreviewMap
        routes={routes}
        selectedRouteId="primary"
        onSelectRoute={onSelectRoute}
        origin={{ id: 'origin', name: 'Batamindo Industrial Park', category: 'Test', lat: 1.06, lng: 104.03 }}
        destination={{ id: 'destination', name: 'Batam Centre Ferry Terminal', category: 'Test', lat: 1.13, lng: 104.055 }}
        planningTrafficSnapshot={planningTrafficSnapshot}
      />,
    );

    await act(async () => Promise.resolve());

    expect(leafletSpies.zoomControl).toHaveBeenCalledWith({ position: 'topright' });
    expect(leafletSpies.map).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({
        maxZoom: 18,
        minZoom: 10,
        scrollWheelZoom: true,
        zoomControl: false,
      }),
    );
    expect(container.querySelector<HTMLElement>('#route-preview-map')?.classList)
      .toContain('route-preview-map-canvas');
    expect(container.querySelector('figure')?.classList)
      .toContain('route-preview-map-figure');
    expect(container.querySelector('[aria-label="Show a larger route map"]')).toBeNull();
    const radarCard = container.querySelector('.map-radar-card.route-map-radar-card');
    expect(radarCard).not.toBeNull();
    expect(radarCard?.textContent).not.toContain('Batamindo Industrial Park');
    expect(radarCard?.textContent).not.toContain('Batam Centre Ferry Terminal');
    expect(radarCard?.textContent).toContain('8.2 km');
    expect(radarCard?.textContent).toContain('17 min');
    expect(radarCard?.textContent).not.toContain('Follow the recommended corridor');
    expect(radarCard?.querySelector('summary')?.textContent)
      .toContain('How this route was weighted');
    expect(radarCard?.querySelector('details')?.open).toBe(false);

    const drawnPaths = leafletSpies.polyline.mock.calls.map(call => call[0]);
    expect(drawnPaths.filter(path => path === primaryGeometry)).toHaveLength(2);
    expect(drawnPaths.filter(path => path === alternativeGeometry)).toHaveLength(2);
    const primaryStyles = leafletSpies.polyline.mock.calls
      .filter(call => call[0] === primaryGeometry)
      .map(call => call[1]);
    expect(primaryStyles).toEqual(expect.arrayContaining([
      expect.objectContaining({ color: MAP_PALETTE.route.casing, weight: 11 }),
      expect.objectContaining({ color: MAP_PALETTE.route.selected, weight: 6 }),
    ]));
    const alternativeStyles = leafletSpies.polyline.mock.calls
      .filter(call => call[0] === alternativeGeometry)
      .map(call => call[1]);
    expect(alternativeStyles.every(style => style?.dashArray === '9 7')).toBe(true);
    expect(alternativeStyles).toEqual(expect.arrayContaining([
      expect.objectContaining({ color: MAP_PALETTE.route.casing }),
      expect.objectContaining({ color: MAP_PALETTE.route.alternatives[1] }),
    ]));

    const routeLegend = container.querySelector('[role="group"][aria-label="Select a route shown on the map"]');
    expect(routeLegend).not.toBeNull();
    expect(routeLegend?.classList.contains('route-map-route-tags')).toBe(true);
    expect(container.querySelector('figcaption')).toBeNull();
    const routeButtons = Array.from(routeLegend?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    expect(routeButtons).toHaveLength(2);
    expect(routeButtons.every(button => button.type === 'button')).toBe(true);
    expect(routeButtons.every(button => button.classList.contains('badge'))).toBe(true);
    expect(routeButtons.every(button => !button.classList.contains('ui-button-choice'))).toBe(true);
    expect(routeButtons.every(button => button.classList.contains('route-map-route-option'))).toBe(true);
    expect(routeButtons.every(button => (
      button.querySelector('.route-map-route-swatch')?.getAttribute('style')?.includes('4px')
    ))).toBe(true);
    expect(routeButtons.map(button => button.textContent)).toEqual([
      'Recommended route',
      'Alternative 1',
    ]);
    expect(routeButtons[0].getAttribute('aria-pressed')).toBe('true');

    act(() => routeButtons[1].click());
    expect(onSelectRoute).toHaveBeenCalledWith('osm-alternative-1');

    expect(leafletSpies.inactiveRouteClicks).toHaveLength(1);
    act(() => leafletSpies.inactiveRouteClicks[0]());
    expect(onSelectRoute).toHaveBeenLastCalledWith('osm-alternative-1');

    const maneuverIcons = leafletSpies.divIcon.mock.calls
      .map(call => call[0])
      .filter(options => options?.className === 'gmaps-turn-badge')
      .map(options => options?.html.querySelector('svg'));
    const maneuverIconClasses = maneuverIcons.map(icon => icon?.getAttribute('class'));
    expect(maneuverIconClasses).toEqual(expect.arrayContaining([
      expect.stringContaining('lucide-move-up'),
      expect.stringContaining('lucide-corner-up-left'),
      expect.stringContaining('lucide-corner-up-right'),
      expect.stringContaining('lucide-rotate-cw'),
      expect.stringContaining('lucide-redo'),
    ]));
    const uTurnIcon = maneuverIcons.find(icon => icon?.classList.contains('lucide-redo'));
    expect(uTurnIcon?.style.transform).toBe('rotate(90deg)');
    const summaryBadgeOptions = leafletSpies.divIcon.mock.calls
      .map(call => call[0])
      .find(options => options?.className === 'gmaps-floating-badge');
    expect(summaryBadgeOptions?.iconSize).toEqual([0, 0]);
    expect(summaryBadgeOptions?.html.style.width).toBe('max-content');
    expect(summaryBadgeOptions?.html.style.transform).toBe('translate(-50%, -50%)');
    expect(Array.from(summaryBadgeOptions?.html.children ?? []).map(child => (
      (child as HTMLElement).style.fontSize
    ))).toEqual(['12px', '12px']);
    const summaryMarkerCall = leafletSpies.marker.mock.calls.find(call => (
      call[1]?.icon?.className === 'gmaps-floating-badge'
    ));
    expect(summaryMarkerCall?.[0][0]).toBeCloseTo(1.11);
    expect(summaryMarkerCall?.[0][1]).toBeCloseTo(104.0475);
    expect(summaryMarkerCall?.[1]).toEqual(expect.objectContaining({ interactive: false }));
    const endpointPins = leafletSpies.divIcon.mock.calls
      .map(call => call[0]?.html)
      .filter((html): html is HTMLElement => (
        html instanceof HTMLElement && html.dataset.routeEndpoint !== undefined
      ));
    expect(endpointPins.map(pin => pin.textContent)).toEqual(['A', 'B']);
    expect(endpointPins.map(pin => pin.dataset.markerColor)).toEqual([
      MAP_PALETTE.endpoint.origin,
      MAP_PALETTE.endpoint.destination,
    ]);
    expect(leafletSpies.marker).toHaveBeenCalledWith(
      primaryGeometry[0],
      expect.objectContaining({ zIndexOffset: 1000 }),
    );
    expect(leafletSpies.marker).toHaveBeenCalledWith(
      primaryGeometry[primaryGeometry.length - 1],
      expect.objectContaining({ zIndexOffset: 1000 }),
    );
    expect(leafletSpies.circle).toHaveBeenCalledTimes(4);
    expect(leafletSpies.circleMarker).toHaveBeenCalledTimes(3);
    expect(leafletSpies.circleMarker.mock.calls.map(call => call[1]?.fillColor))
      .toEqual(['#10b981', '#f59e0b', '#ef4444']);
  });

  it('renders road and ferry legs distinctly with transfer-terminal markers', async () => {
    const roadOne: [number, number][] = [
      [1.284, 103.8513], [1.2644, 103.8206],
    ];
    const ferry: [number, number][] = [
      [1.2644, 103.8206], [1.20, 103.91], [1.1318, 104.0554],
    ];
    const roadTwo: [number, number][] = [
      [1.1318, 104.0554], [1.06, 104.0303],
    ];
    const multimodal: RoadRouteOption = {
      ...routes[0],
      id: 'multimodal',
      name: 'Cross-border journey',
      route_geometry: [...roadOne, ...ferry.slice(1), ...roadTwo.slice(1)],
      route_data_source: 'multimodal_offline_estimate',
      route_legs: [
        {
          mode: 'ROAD', from_name: 'Raffles Place', to_name: 'HarbourFront SG',
          geometry: roadOne, distance_km: 4, duration_mins: 12,
          data_source: 'offline_access_estimate', is_estimate: true,
          limitations: 'Estimated road access.',
        },
        {
          mode: 'FERRY', from_name: 'HarbourFront SG', to_name: 'Batam Centre',
          geometry: ferry, distance_km: 28, duration_mins: 55,
          data_source: 'official_reverse_corridor_duration_reference', is_estimate: true,
          limitations: 'Published reverse-corridor duration reference.',
          schedule_status: 'CROSSING_DURATION_REFERENCE_ONLY',
        },
        {
          mode: 'ROAD', from_name: 'Batam Centre', to_name: 'Batamindo',
          geometry: roadTwo, distance_km: 10, duration_mins: 22,
          data_source: 'batam_bundled_openstreetmap', is_estimate: false,
          limitations: 'Bundled Batam OSM route.',
        },
      ],
    };

    const container = renderIntoDom(
      <RoutePreviewMap routes={[multimodal]} selectedRouteId="multimodal" />,
    );
    await act(async () => Promise.resolve());

    const ferryStyles = leafletSpies.polyline.mock.calls
      .filter(call => call[0] === FERRY_SEA_ROUTE)
      .map(call => call[1]);
    expect(ferryStyles).toEqual(expect.arrayContaining([
      expect.objectContaining({ dashArray: '5 9', weight: 9 }),
      expect.objectContaining({ color: '#0284c7', dashArray: '5 9', weight: 5 }),
    ]));
    const estimatedRoadStyles = leafletSpies.polyline.mock.calls
      .filter(call => call[0] === roadOne)
      .map(call => call[1]);
    expect(estimatedRoadStyles).toEqual(expect.arrayContaining([
      expect.objectContaining({ dashArray: '12 9' }),
      expect.objectContaining({ color: '#d97706', dashArray: '12 9' }),
    ]));
    expect(leafletSpies.marker).toHaveBeenCalledWith(
      FERRY_SEA_ROUTE[0],
      expect.objectContaining({ icon: expect.anything() }),
    );
    expect(leafletSpies.marker).toHaveBeenCalledWith(
      FERRY_SEA_ROUTE[FERRY_SEA_ROUTE.length - 1],
      expect.objectContaining({ icon: expect.anything() }),
    );
    const summaryMarkerCall = leafletSpies.marker.mock.calls.find(call => (
      call[1]?.icon?.className === 'gmaps-floating-badge'
    ));
    expect(summaryMarkerCall?.[0][0]).toBeCloseTo(1.2225);
    expect(summaryMarkerCall?.[0][1]).toBeCloseTo(103.9165);
    expect(container.querySelector('figcaption')).toBeNull();
  });
});
