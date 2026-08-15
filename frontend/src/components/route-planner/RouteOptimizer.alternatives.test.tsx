/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ROUTE_LOCATIONS, offlineFerries } from '../../data/mockData';
import { routePreferenceProfile } from '../../data/routePreferences';
import type { RoadRouteOption, RouteOptimizationResult } from '../../types';
import { formatTime } from '../../utils/format';
import { RouteOptimizer } from './RouteOptimizer';

vi.mock('./RoutePreviewMap', () => ({
  RoutePreviewMap: ({
    routes,
    selectedRouteId,
    onSelectRoute,
  }: {
    routes: RoadRouteOption[];
    selectedRouteId: string;
    onSelectRoute?: (routeId: string) => void;
  }) => {
    const selected = routes.find(route => route.id === selectedRouteId);
    return (
      <div data-testid="route-map-stub">
        <aside className="map-radar-card route-map-radar-card">
          <span data-testid="map-selected-route">{selected?.name}</span>
          <span data-testid="map-selected-source">{selected?.route_data_source}</span>
          <span>{selected?.distance_km} km</span>
          <span>{selected?.estimated_travel_time_mins} min</span>
          {selected?.routing_cost_breakdown ? (
            <details>
              <summary>How this route was weighted</summary>
              <span>{selected.routing_cost_breakdown.congestion_delay_mins} min</span>
            </details>
          ) : null}
        </aside>
        <button type="button" onClick={() => onSelectRoute?.('primary')}>
          Select recommended route on map
        </button>
      </div>
    );
  },
}));

let root: Root | undefined;

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

const result: RouteOptimizationResult = {
  route_type: 'ROAD_ROUTE',
  corridor: {
    id: 'corridor-1',
    name: 'Batamindo to Batam Centre',
    distance_km: 8.2,
    base_time_mins: 15,
  },
  vehicle_type: 'COMMUTER',
  planned_departure: '2026-08-09T14:00:00+07:00',
  congestion_prediction: {
    current_score: 42,
    predicted_30min: 39,
    predicted_60min: 35,
    estimated_delay_mins: 2,
    risk_level: 'LOW',
    trend: 'DOWNWARD',
  },
  estimated_travel_time_mins: 17,
  customs_buffer_mins: 5,
  total_eta_mins: 22,
  co2_emissions_kg: 1.7,
  co2_saved_kg: 0.4,
  optimal_departure: {
    recommended: 'DEPART_NOW',
    time_saved_mins: 3,
    reason: 'Road conditions are currently favourable.',
  },
  next_matching_ferries: [],
  route_geometry: [
    [1.0605, 104.0303],
    [1.09, 104.04],
    [1.1318, 104.0554],
  ],
  route_data_source: 'bundled_client_openstreetmap',
  generalized_cost_mins: 18.1,
  routing_cost_breakdown: {
    free_flow_mins: 14,
    congestion_delay_mins: 1.2,
    weather_delay_mins: 0,
    maneuver_delay_mins: 1.8,
    road_suitability_penalty_mins: 1.1,
    modeled_travel_time_mins: 17,
    generalized_cost_mins: 18.1,
  },
  routing_model: {
    version: 2,
    cost_unit: 'seconds',
    objective: 'vehicle-specific generalized travel time',
    network_congestion_score: 42,
    weather: 0,
    spatial_zone_count: 0,
    distance_is_raw_edge_sum: true,
    traffic_provenance: 'simulated',
    limitations: 'OSM planning limits apply.',
  },
  alternatives_note: 'Only two sufficiently different road paths satisfy the route-quality limits.',
  requested_origin: {
    lat: 1.0605,
    lng: 104.0303,
    display_name: 'Batamindo Industrial Park',
  },
  requested_destination: {
    lat: 1.1318,
    lng: 104.0554,
    display_name: 'Batam Centre Ferry Terminal',
  },
  navigation: {
    traffic_lights_count: 1,
    landmarks_along_route: [],
    route_narrative_words: 'Follow the recommended road corridor north.',
    maneuvers: [{
      step: 1,
      type: 'continue',
      instruction: 'Continue on Primary Road',
      street: 'Primary Road',
      distance_m: 8200,
      icon: 'continue',
      coords: [1.0605, 104.0303],
    }],
  },
  alternative_routes: [{
    id: 'osm-alternative-1',
    name: 'Alternative road route 1',
    description: 'Uses a distinct eastern road corridor.',
    route_geometry: [
      [1.0605, 104.0303],
      [1.08, 104.07],
      [1.1318, 104.0554],
    ],
    distance_km: 9.6,
    estimated_travel_time_mins: 24,
    total_eta_mins: 29,
    co2_emissions_kg: 2.4,
    co2_saved_kg: 0.1,
    overlap_ratio: 0.44,
    routing_cost_breakdown: {
      free_flow_mins: 18,
      congestion_delay_mins: 2.1,
      weather_delay_mins: 0,
      maneuver_delay_mins: 3.9,
      road_suitability_penalty_mins: 1.4,
      modeled_travel_time_mins: 24,
      generalized_cost_mins: 25.4,
    },
    navigation: {
      traffic_lights_count: 0,
      landmarks_along_route: [],
      route_narrative_words: 'Use the eastern corridor and rejoin near the terminal.',
      maneuvers: [{
        step: 1,
        type: 'roundabout',
        instruction: 'Enter the roundabout toward Eastern Road',
        street: 'Eastern Road',
        road_ref: 'AH 2',
        distance_m: 9600,
        cumulative_distance_m: 7000,
        icon: 'roundabout',
        coords: [1.08, 104.07],
      }],
    },
  }],
};

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('selectable road alternatives', () => {
  it('updates the map, metrics, source, overview, and directions from a route card', () => {
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={result}
        setResult={vi.fn()}
        resultSource="offline"
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

    const resultTabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Route result sections"] [role="tab"]'),
    );
    const [summaryTab, journeyTab, directionsTab, connectionsTab] = resultTabs;
    expect(resultTabs.map(tab => tab.textContent)).toEqual([
      'Summary',
      'Journey map',
      'Directions',
      'Comparison',
    ]);
    expect(summaryTab.getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('[data-testid="route-map-stub"]')).toBeNull();
    expect(container.querySelector('[class*="route-ui-"]')).toBeNull();

    const sourceBadge = Array.from(container.querySelectorAll<HTMLSpanElement>('span'))
      .find(element => element.textContent === 'Offline fallback');
    expect(sourceBadge?.classList.contains('badge')).toBe(true);
    expect(sourceBadge?.classList.contains('badge-heavy')).toBe(true);
    expect(sourceBadge?.className).not.toContain('route-source-status');

    const choices = container.querySelector('[aria-label="Choose a road route"]');
    const buttons = Array.from(choices?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    expect(buttons).toHaveLength(2);
    expect(buttons.every(button => button.type === 'button')).toBe(true);
    expect(buttons[0].getAttribute('aria-pressed')).toBe('true');
    expect(container.textContent).not.toContain('Compare online');
    expect(container.querySelector('[aria-label="Route summary metrics"]')?.textContent)
      .toContain('17 min');

    const alternativeButton = buttons.find(button => button.textContent?.includes('Alternative 1'));
    expect(alternativeButton).toBeDefined();
    expect(alternativeButton?.textContent).not.toContain('road route');
    act(() => alternativeButton?.click());

    expect(alternativeButton?.getAttribute('aria-pressed')).toBe('true');
    expect(buttons[0].getAttribute('aria-pressed')).toBe('false');
    expect(container.querySelector('.route-options-selection-status')?.textContent)
      .toBe('Showing Alternative 1');
    expect(container.querySelector('[data-testid="route-map-stub"]')).toBeNull();
    const metrics = container.querySelector('[aria-label="Route summary metrics"]')?.textContent;
    expect(metrics).toContain('24 min');
    expect(metrics).toContain('9.6 km');
    expect(metrics).toContain('29 min');
    expect(metrics).toContain('2.4 kg');
    expect(container.textContent).toContain('Enter the roundabout toward Eastern Road');
    expect(container.textContent).toContain('Road ref: AH 2');
    expect(container.textContent).toContain('9.6 km');
    expect(container.textContent).not.toContain('from start');
    expect(container.textContent).not.toContain('Continue on Primary Road');
    expect(container.textContent).not.toContain('Bundled OpenStreetMap road graph');
    expect(container.textContent).toContain('Only two sufficiently different road paths');

    act(() => journeyTab.click());
    expect(journeyTab.getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('[data-testid="map-selected-route"]')?.textContent)
      .toBe('Alternative road route 1');
    expect(container.querySelector('[data-testid="map-selected-source"]')?.textContent)
      .toBe('bundled_client_openstreetmap');
    expect(container.querySelector('.route-overview-card')?.textContent)
      .toContain('Use the eastern corridor and rejoin near the terminal.');
    const weightingDetails = container.querySelector<HTMLDetailsElement>('details');
    expect(weightingDetails?.closest('.map-radar-card')).not.toBeNull();
    expect(weightingDetails?.open).toBe(false);
    expect(weightingDetails?.querySelector('summary')?.textContent)
      .toContain('How this route was weighted');
    expect(weightingDetails?.textContent).toContain('2.1 min');
    expect(weightingDetails?.textContent).not.toContain('not added to ETA');

    act(() => directionsTab.click());
    expect(directionsTab.getAttribute('aria-selected')).toBe('true');
    expect(container.textContent).toContain('Enter the roundabout toward Eastern Road');
    expect(container.querySelector('.route-direction-icon .lucide-rotate-cw')).not.toBeNull();

    act(() => connectionsTab.click());
    const comparison = container.querySelector('[role="region"][aria-label="Vehicle profile comparison"]');
    expect(comparison?.querySelectorAll('tbody tr')).toHaveLength(8);
    expect(comparison?.querySelectorAll('tbody tr[aria-current="true"]')).toHaveLength(1);

    act(() => journeyTab.click());
    const mapSelectButton = container.querySelector<HTMLButtonElement>(
      '[data-testid="route-map-stub"] button',
    );
    act(() => mapSelectButton?.click());
    expect(container.querySelector('[data-testid="map-selected-route"]')?.textContent)
      .toBe('Recommended');

    act(() => directionsTab.click());
    expect(container.textContent).toContain('Continue on Primary Road');
    expect(container.querySelector('.route-direction-icon .lucide-move-up')).not.toBeNull();
  });

  it('labels exact-time planning when it uses the committed timetable simulation', () => {
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={{
          ...result,
          schedule_provenance: {
            source: 'committed_timetable_simulation',
            freshness_durability: 'supabase_table_unavailable',
            shared_freshness: false,
            live: false,
          },
        }}
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

    expect(container.textContent).toContain('committed timetable simulation');
    expect(container.textContent).toContain('Verify the departure and book with the operator');
  });

  it('renders ferry connections as sourced schedule slots without live claims', () => {
    const fixedNow = new Date('2026-08-09T22:30:00.000Z');
    const publishedSailing = offlineFerries(fixedNow, fixedNow, 12)[0];
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={{ ...result, next_matching_ferries: [publishedSailing] }}
        setResult={vi.fn()}
        resultSource="offline"
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

    expect(container.textContent).toContain('Published ferry connections after arrival');
    expect(container.textContent).toContain('SCHEDULED');
    expect(container.textContent).toContain('not live operating status');
    expect(container.textContent).toContain('verify with the operator before travel');
    const ferryDetails = container.querySelector('.route-ferry-itinerary');
    expect(Array.from(ferryDetails?.children ?? []).map(child => child.textContent)).toEqual([
      `${publishedSailing.departure_port} → ${publishedSailing.arrival_port}`,
      `Estimated arrival: ${formatTime(publishedSailing.arrival_time!)}`,
    ]);
    const scheduleDisclaimer = Array.from(container.querySelectorAll<HTMLParagraphElement>('p'))
      .find(paragraph => paragraph.textContent?.includes('not live operating status'));
    expect(scheduleDisclaimer?.classList.contains('route-section-note')).toBe(true);
    expect(container.textContent).toContain('Last verified 13 Aug 2026, 00:30 WIB');
    expect(container.textContent).not.toContain('seats open');
    const sourceLink = Array.from(container.querySelectorAll<HTMLAnchorElement>('a'))
      .find(link => link.textContent?.includes('Official operator timetable'));
    expect(sourceLink?.href).toBe(publishedSailing.schedule_source_url);
    expect(sourceLink?.target).toBe('_blank');
  });

  it('shows that the selected vehicle is only for first and last-mile access', () => {
    const transferNote = 'The selected vehicle profile applies only to first- and last-mile road access; it is not carried onboard the passenger ferry.';
    const multimodalResult: RouteOptimizationResult = {
      ...result,
      route_type: 'MULTIMODAL_FERRY_ROUTE',
      congestion_prediction: {
        ...result.congestion_prediction,
        status: 'NOT_MODELLED',
      },
      vehicle_transfer_policy: 'FIRST_LAST_MILE_ONLY',
      vehicle_transfer_note: transferNote,
      alternative_routes: [],
      route_legs: [
        {
          mode: 'ROAD', from_name: 'Raffles Place', to_name: 'HarbourFront SG',
          geometry: [[1.284, 103.8513], [1.2644, 103.8206]],
          distance_km: 4, duration_mins: 12,
          data_source: 'offline_access_estimate', is_estimate: true,
          limitations: 'Estimated road access.',
          vehicle_role: 'FIRST_LAST_MILE_ACCESS',
        },
        {
          mode: 'FERRY', from_name: 'HarbourFront SG', to_name: 'Batam Centre',
          geometry: [[1.2644, 103.8206], [1.2, 103.91], [1.1318, 104.0554]],
          distance_km: 28, duration_mins: 60,
          data_source: 'official_timetable_snapshot', is_estimate: true,
          limitations: 'Published schedule, not live status.',
          schedule_status: 'PUBLISHED_DEPARTURE_SELECTED',
          vehicle_carried_onboard: false,
        },
        {
          mode: 'ROAD', from_name: 'Batam Centre', to_name: 'Batamindo',
          geometry: [[1.1318, 104.0554], [1.0605, 104.0303]],
          distance_km: 10, duration_mins: 22,
          data_source: 'offline_access_estimate', is_estimate: true,
          limitations: 'Estimated road access.',
          vehicle_role: 'FIRST_LAST_MILE_ACCESS',
        },
      ],
    };
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={multimodalResult}
        setResult={vi.fn()}
        resultSource="live"
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

    const itinerary = container.querySelector('section[aria-label="Cross-border itinerary"]');
    expect(itinerary).not.toBeNull();
    const itineraryLegs = itinerary?.querySelectorAll('.route-itinerary-leg') ?? [];
    expect(itineraryLegs).toHaveLength(3);
    itineraryLegs.forEach((leg) => {
      const metadata = leg.querySelector('.route-itinerary-leg-meta');
      expect(metadata?.querySelector('.route-itinerary-status')).not.toBeNull();
      expect(metadata?.querySelector('.route-itinerary-leg-duration')).not.toBeNull();
    });
    expect(itinerary?.textContent).toContain('Road · 4 km');
    expect(itinerary?.textContent).toContain('Ferry · 28 km');
    expect(container.textContent).not.toContain(transferNote);
    expect(container.textContent).not.toContain('Cross-border traffic not modelled');
    expect(container.textContent).not.toContain('Indicative vehicle comparison');
  });

  it('shows exact mapped local-road sections without the audit disclaimer', () => {
    const localResult: RouteOptimizationResult = {
      ...result,
      route_preference: 'LOCAL',
      route_preference_profile: routePreferenceProfile('LOCAL'),
      local_road_distance_km: 1.24,
      local_road_segments: [{
        id: 'local-road:1:3:8',
        name: 'Jalan Cendana',
        highway: 'residential',
        source_node: 1,
        target_node: 3,
        edge_count: 2,
        distance_km: 1.24,
      }],
      local_road_audit: {
        requested: true,
        segment_count: 1,
        metadata_scope: 'mapped_osm_residential_motor_roads',
        width_clearance_verified: false,
        note: 'Mapped OSM residential motor roads only; drains and footpaths are absent, and lane width or vehicle clearance is not verified.',
      },
      alternative_routes: [],
    };
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={localResult}
        setResult={vi.fn()}
        resultSource="live"
        setResultSource={vi.fn()}
        vehicleType="COMMUTER"
        setVehicleType={vi.fn()}
        routePreference="LOCAL"
        setRoutePreference={vi.fn()}
        weather={0}
        setWeather={vi.fn()}
        hour={14}
        setHour={vi.fn()}
      />,
    );

    const audit = container.querySelector('.route-local-road-audit');
    const auditHeader = audit?.querySelector('.route-local-road-audit__header');
    expect(auditHeader?.textContent).toContain('Mapped Local Road Coverage');
    expect(auditHeader?.querySelector('svg')?.getAttribute('width')).toBe('16');
    expect(auditHeader?.querySelector('svg')?.getAttribute('height')).toBe('16');
    expect(auditHeader?.nextElementSibling?.textContent).toContain('This path selects');
    expect(auditHeader?.nextElementSibling?.nextElementSibling?.tagName).toBe('UL');
    expect(audit?.textContent).toContain('1.24 km');
    expect(audit?.textContent).toContain('Jalan Cendana');
    expect(audit?.textContent).not.toContain('drains and footpaths are absent');
    expect(audit?.textContent).not.toContain('clearance is not verified');
  });

  it('keeps the opt-in Google comparison text-only, attributed, and resettable', async () => {
    vi.stubEnv('VITE_ENABLE_GOOGLE_BENCHMARK', 'true');
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T14:00:00+07:00',
      data_source: 'google_routes_v2_text_benchmark',
      provenance: {
        benchmark: 'Google Routes API v2',
        attribution: 'Google Maps',
        policy_url: 'https://developers.google.com/maps/terms',
        external_route_content_persisted: false,
      },
      benchmark_type: 'google_routes_v2_text_metrics',
      provider: 'google_routes_v2',
      attribution: 'Google Maps',
      policy_url: 'https://developers.google.com/maps/terms',
      route_preference: 'BALANCED',
      preference_honored: false,
      preference_honored_details: {
        requested: 'BALANCED',
        honored: false,
        experimental: false,
        provider_translation: 'TRAFFIC_AWARE',
        note: 'Google does not receive CrossFlow balanced weights.',
      },
      routes: [{
        id: 'google-benchmark-1',
        duration_seconds: 960,
        duration_mins: 16,
        distance_meters: 8100,
        distance_km: 8.1,
        route_labels: ['DEFAULT_ROUTE'],
        summary: '8.1 km · 16.0 min',
      }],
      cacheable: false,
      persisted: false,
      training_eligible: false,
      map_overlay_allowed: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    const container = renderIntoDom(
      <RouteOptimizer
        locations={ROUTE_LOCATIONS}
        originId="mukakuning"
        setOriginId={vi.fn()}
        destinationId="batam_centre"
        setDestinationId={vi.fn()}
        result={result}
        setResult={vi.fn()}
        resultSource="offline"
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

    const compareButton = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Compare online'));
    expect(compareButton).toBeDefined();
    await act(async () => {
      compareButton?.click();
      await new Promise<void>(resolve => window.setTimeout(() => resolve(), 0));
    });

    const benchmark = container.querySelector('#route-online-benchmark');
    expect(benchmark?.textContent).toContain('8.1 km · 16.0 min');
    expect(benchmark?.textContent).toContain('Labels: DEFAULT_ROUTE');
    expect(benchmark?.textContent).toContain('not drawn on this map, not saved');
    expect(benchmark?.textContent).toContain('not used for shortcut training');
    const attribution = benchmark?.querySelector<HTMLAnchorElement>('.route-benchmark-attribution');
    expect(attribution?.textContent).toBe('Google Maps');
    expect(attribution?.getAttribute('translate')).toBe('no');
    expect(attribution?.href).toBe('https://developers.google.com/maps/terms');
    const journeyTab = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Route result sections"] [role="tab"]'),
    ).find(tab => tab.textContent === 'Journey map');
    act(() => journeyTab?.click());
    expect(container.querySelector('[data-testid="route-map-stub"]')?.textContent)
      .not.toContain('Google');
    const requestInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(requestInit.body))).toMatchObject({
      route_preference: 'BALANCED',
      origin_lat: result.requested_origin?.lat,
      destination_lat: result.requested_destination?.lat,
    });

    // A benchmark computed for one set of inputs must not stay on screen once
    // those inputs change; a stale comparison read as current would be worse
    // than none. The redesigned panel no longer exposes a route-preference
    // control, so this drives the same invalidation through the swap action.
    const swapButton = container.querySelector<HTMLButtonElement>(
      '[aria-label="Swap origin and destination"]',
    ) ?? Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => /swap/i.test(button.getAttribute('aria-label') ?? ''));
    expect(swapButton).toBeTruthy();
    act(() => swapButton?.click());
    expect(container.querySelector('#route-online-benchmark')).toBeNull();
  });
});
