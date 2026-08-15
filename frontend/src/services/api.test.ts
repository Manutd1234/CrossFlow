import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { planWithBundledRoadGraph } from './offlineRoadRouter';
import { routePreferenceProfile } from '../data/routePreferences';
import {
  MOCK_OPERATIONS,
  PUBLISHED_FERRY_TIMETABLE_METADATA,
  offlineFerries,
} from '../data/mockData';
import {
  fetchFerries,
  fetchHistoricalCongestion,
  fetchLiveTraffic,
  fetchModelStatus,
  fetchOperationsSummary,
  fetchPortIntelligence,
  refreshOfficialFerrySources,
  requestFreeRouteOptimization,
  requestModelRetrain,
  requestRouteBenchmark,
  requestRouteOptimization,
  scheduleInformedPortSeed,
} from './api';

vi.mock('./offlineRoadRouter', () => ({
  planWithBundledRoadGraph: vi.fn(),
}));

const planBundledRoute = vi.mocked(planWithBundledRoadGraph);
const balancedPreference = routePreferenceProfile('BALANCED');
const localPreference = routePreferenceProfile('LOCAL');
const emptyLocalRoadAudit = {
  requested: false,
  segment_count: 0,
  metadata_scope: 'mapped_osm_residential_motor_roads' as const,
  width_clearance_verified: false as const,
  note: 'No mapped residential road sections selected.',
};
const bundledRoutingModel = (objectiveCostSeconds: number) => ({
  version: 5 as const,
  cost_unit: 'seconds' as const,
  objective_cost_unit: 'weighted_seconds' as const,
  objective: balancedPreference.description,
  selected_preference: 'BALANCED' as const,
  road_scope: 'STANDARD' as const,
  component_weights: balancedPreference.component_weights,
  objective_cost_s: objectiveCostSeconds,
  network_congestion_score: 20,
  weather: 0,
  spatial_zone_count: 0 as const,
  distance_is_raw_edge_sum: true as const,
  traffic_provenance: 'bundled local forecast; no live spatial edge zones',
  limitations: 'OSM planning limitations apply.',
});

const origin = { lat: 1.1039, lng: 104.0175, display_name: 'Selected Location' };
const destination = {
  lat: 1.1318,
  lng: 104.0554,
  display_name: 'Batam Centre Ferry Terminal',
};

const navigation = {
  schema_version: 1 as const,
  data_source: 'openstreetmap_edge_metadata' as const,
  maneuvers: [
    {
      step: 1,
      type: 'depart',
      modifier: 'straight',
      instruction: 'Head onto Jalan Ahmad Yani',
      street: 'Jalan Ahmad Yani',
      distance_m: 7000,
      cumulative_distance_m: 0,
      icon: 'continue',
      coords: [1.1039, 104.0175] as [number, number],
    },
    {
      step: 2,
      type: 'arrive',
      modifier: 'straight',
      instruction: 'Arrive at Batam Centre Ferry Terminal',
      street: 'Jalan Engku Putri',
      distance_m: 0,
      cumulative_distance_m: 7000,
      icon: 'arrive',
      coords: [1.1318, 104.0554] as [number, number],
    },
  ],
  landmarks_along_route: [] as [],
  traffic_lights_count: 0 as const,
  route_narrative_words: 'Follow Jalan Ahmad Yani and Jalan Engku Putri.',
};

const bundledPlan = {
  origin_snap_m: 12.34,
  destination_snap_m: 8.76,
  routes: [
    {
      id: 'fastest',
      name: 'Fastest route',
      description: 'Shortest available route on the bundled OpenStreetMap road graph.',
      distance_km: 8.2,
      geometry: [
        [1.1039, 104.0175],
        [1.1100, 104.0250],
        [1.1200, 104.0400],
        [1.1318, 104.0554],
      ] as [number, number][],
      navigation,
      path: ['1', '2', '3', '4'],
      free_flow_time_mins: 14,
      estimated_travel_time_mins: 18,
      estimated_travel_time_after_30_mins: 18,
      routing_cost_mins: 18.6,
      objective_cost_s: 1116,
      route_preference: 'BALANCED' as const,
      route_preference_profile: balancedPreference,
      local_road_distance_km: 0,
      local_road_segments: [],
      local_road_audit: emptyLocalRoadAudit,
      routing_model: bundledRoutingModel(1116),
      congestion_delay_after_30_mins: 2,
      routing_cost_breakdown: {
        free_flow_mins: 14,
        congestion_delay_mins: 2,
        weather_delay_mins: 1,
        maneuver_delay_mins: 1,
        road_suitability_penalty_mins: 0.6,
        modeled_travel_time_mins: 18,
        generalized_cost_mins: 18.6,
      },
    },
    {
      id: 'alternative-1',
      name: 'Alternative 1',
      description: 'A genuinely different road path with limited overlap and detour.',
      distance_km: 9.1,
      geometry: [
        [1.1039, 104.0175],
        [1.1050, 104.0350],
        [1.1250, 104.0500],
        [1.1318, 104.0554],
      ] as [number, number][],
      navigation,
      path: ['1', '5', '6', '4'],
      overlap_ratio: 0.44,
      free_flow_time_mins: 16,
      estimated_travel_time_mins: 21,
      estimated_travel_time_after_30_mins: 21,
      routing_cost_mins: 22.4,
      objective_cost_s: 1344,
      route_preference: 'BALANCED' as const,
      route_preference_profile: balancedPreference,
      local_road_distance_km: 0,
      local_road_segments: [],
      local_road_audit: emptyLocalRoadAudit,
      routing_model: bundledRoutingModel(1344),
      congestion_delay_after_30_mins: 2.5,
      routing_cost_breakdown: {
        free_flow_mins: 16,
        congestion_delay_mins: 2.5,
        weather_delay_mins: 1.2,
        maneuver_delay_mins: 1.3,
        road_suitability_penalty_mins: 1.4,
        modeled_travel_time_mins: 21,
        generalized_cost_mins: 22.4,
      },
    },
  ],
};

const liveApiRoute = {
  data_source: 'live',
  generated_at: '2026-08-09T14:00:00+07:00',
  route_type: 'ROAD_ROUTE',
  corridor: {
    id: 'live-road',
    name: 'Live road route',
    distance_km: 8,
    base_time_mins: 15,
  },
  vehicle_type: 'COMMUTER',
  congestion_prediction: {
    current_score: 20,
    predicted_30min: 20,
    predicted_60min: 20,
    estimated_delay_mins: 1,
    risk_level: 'LOW',
    trend: 'STABLE',
  },
  estimated_travel_time_mins: 16,
  customs_buffer_mins: 0,
  total_eta_mins: 16,
  co2_emissions_kg: 1.8,
  co2_saved_kg: 0,
  optimal_departure: {
    recommended: 'DEPART_NOW',
    time_saved_mins: 0,
    reason: 'Live route available.',
  },
  next_matching_ferries: [],
  route_geometry: [[1.1039, 104.0175], [1.1318, 104.0554]],
  route_data_source: 'openstreetmap',
  navigation,
};

beforeEach(() => {
  planBundledRoute.mockRejectedValue(new Error('worker unavailable'));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('opt-in route benchmark', () => {
  const benchmarkPayload = {
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
    route_preference: 'LOCAL',
    preference_honored: false,
    preference_honored_details: {
      requested: 'LOCAL',
      honored: false,
      experimental: false,
      provider_translation: 'TRAFFIC_AWARE',
      note: 'Google does not receive CrossFlow local-road weights.',
    },
    routes: [{
      id: 'google-benchmark-1',
      duration_seconds: 960,
      duration_mins: 16,
      distance_meters: 8200,
      distance_km: 8.2,
      route_labels: ['DEFAULT_ROUTE'],
      summary: '8.2 km · 16.0 min',
    }],
    cacheable: false,
    persisted: false,
    training_eligible: false,
    map_overlay_allowed: false,
  };

  it('posts only endpoint metrics and the selected preference without browser caching', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify(benchmarkPayload),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ));
    vi.stubGlobal('fetch', fetchMock);

    const response = await requestRouteBenchmark(origin, destination, 'LOCAL');

    const url = String(fetchMock.mock.calls[0]?.[0]);
    const requestInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(url).toContain('/api/route-benchmark');
    expect(requestInit.cache).toBe('no-store');
    expect(JSON.parse(String(requestInit.body))).toEqual({
      origin_lat: origin.lat,
      origin_lng: origin.lng,
      destination_lat: destination.lat,
      destination_lng: destination.lng,
      route_preference: 'LOCAL',
    });
    expect(response.routes[0]).toMatchObject({
      summary: '8.2 km · 16.0 min',
      route_labels: ['DEFAULT_ROUTE'],
    });
  });

  it('surfaces disabled and temporarily unavailable server responses without fallback data', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    for (const [status, detail] of [
      [404, 'Route benchmark is disabled.'],
      [503, 'Route benchmark is temporarily unavailable.'],
    ] as const) {
      fetchMock.mockResolvedValueOnce(new Response(
        JSON.stringify({ detail }),
        { status, headers: { 'Content-Type': 'application/json' } },
      ));
      await expect(requestRouteBenchmark(origin, destination, 'BALANCED'))
        .rejects.toMatchObject({ status, message: detail });
    }
  });
});

describe('free-route API fallback', () => {
  it('keeps Singapore-Batam planning available without downloading the Batam-only graph', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    const singapore = {
      lat: 1.2840,
      lng: 103.8513,
      display_name: 'Raffles Place Singapore',
    };

    const response = await requestFreeRouteOptimization(
      singapore,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.source).toBe('offline');
    expect(response.data.route_type).toBe('MULTIMODAL_FERRY_ROUTE');
    expect(response.data.route_data_source).toBe('multimodal_offline_estimate');
    expect(response.data.route_legs?.map(leg => leg.mode)).toEqual([
      'ROAD', 'FERRY', 'ROAD',
    ]);
    expect(response.data.requested_origin?.region).toBe('SINGAPORE');
    expect(response.data.requested_destination?.region).toBe('BATAM');
    expect(response.data.route_geometry?.length).toBeGreaterThan(8);
    expect(response.data.navigation?.maneuvers.length).toBeGreaterThanOrEqual(6);
    expect(response.data.next_matching_ferries).toHaveLength(1);
    expect(response.data.next_matching_ferries[0]).toMatchObject({
      departure_port: 'HarbourFront SG',
      arrival_port: 'Batam Centre',
      departure_timezone: 'Asia/Singapore',
    });
    expect(response.data.ferry_connection_note).toBeUndefined();
    expect(response.data.vehicle_transfer_policy).toBe('FIRST_LAST_MILE_ONLY');
    expect(response.data.vehicle_transfer_note).toContain('not carried onboard');
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('returns an ApiRequestError explaining why trucks need a cargo feed', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestFreeRouteOptimization(
      { lat: 1.2840, lng: 103.8513, display_name: 'Raffles Place Singapore' },
      destination,
      'CARGO_TRUCK',
      14,
      0,
    )).rejects.toMatchObject({
      name: 'ApiRequestError',
      status: 400,
      message: expect.stringContaining('cargo port'),
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('rejects points outside both supported islands before making a request', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestFreeRouteOptimization(
      { lat: 3.1390, lng: 101.6869, display_name: 'Kuala Lumpur' },
      destination,
      'COMMUTER',
    )).rejects.toMatchObject({
      status: 400,
      message: 'Both route points must be within Singapore or Batam.',
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('does not download the browser graph when the backend responds inside the hedge window', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify(liveApiRoute),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )));

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.source).toBe('live');
    expect(response.data.route_data_source).toBe('openstreetmap');
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('returns bundled routing after 1.2 seconds without waiting for a slow backend timeout', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>(() => undefined)));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const pendingResponse = requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );
    await vi.advanceTimersByTimeAsync(1_199);
    expect(planBundledRoute).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(planBundledRoute).toHaveBeenCalledOnce();

    const response = await pendingResponse;
    expect(response.data.route_data_source).toBe('bundled_client_openstreetmap');
  });

  it('does not let the browser hedge abort an exact-time backend route', async () => {
    vi.useFakeTimers();
    const departureAt = '2026-08-20T09:30:00+07:00';
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      setTimeout(() => resolve(new Response(JSON.stringify({
        ...liveApiRoute,
        planned_departure: departureAt,
        scheduling: {
          mode: 'DEPART_AT',
          requested_departure_at: departureAt,
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })), 5_000);
    })));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const pendingResponse = requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      9,
      0,
      'BALANCED',
      { departure_at: departureAt },
    );
    await vi.advanceTimersByTimeAsync(1_200);
    expect(planBundledRoute).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(3_800);

    const response = await pendingResponse;
    expect(response.source).toBe('live');
    expect(response.data.planned_departure).toBe(departureAt);
    expect(response.data.scheduling).toMatchObject({
      mode: 'DEPART_AT',
      requested_departure_at: departureAt,
    });
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('uses the bundled road graph with navigation and genuine alternatives when the API is offline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.source).toBe('offline');
    expect(response.data.corridor.name).toBe('Selected Location -> Batam Centre Ferry Terminal');
    expect(response.data.corridor.distance_km).toBe(8.2);
    expect(response.data.route_geometry).toEqual(bundledPlan.routes[0].geometry);
    expect(response.data.route_data_source).toBe('bundled_client_openstreetmap');
    expect(response.data.navigation?.maneuvers[0].street).toBe('Jalan Ahmad Yani');
    expect(response.data.alternative_routes).toHaveLength(1);
    expect(response.data.alternative_routes?.[0]).toMatchObject({
      id: 'alternative-1',
      distance_km: 9.1,
      route_geometry: bundledPlan.routes[1].geometry,
      route_data_source: 'bundled_client_openstreetmap',
      overlap_ratio: 0.44,
    });
    expect(response.data.alternative_routes?.[0].estimated_travel_time_mins)
      .toBeGreaterThan(response.data.estimated_travel_time_mins);
    expect(response.data.alternative_routes?.[0].co2_emissions_kg)
      .toBeGreaterThan(response.data.co2_emissions_kg);
    expect(response.data.snap_info).toMatchObject({
      origin_snap_m: 12.3,
      destination_snap_m: 8.8,
      total_access_distance_km: 0.021,
      total_access_time_mins: 0.3,
    });
    expect(response.data.planned_departure).toMatch(/T14:00:00\.000\+07:00$/);
    const alternative = response.data.alternative_routes?.[0];
    const alternativeArrival = new Date(response.data.planned_departure ?? 0).getTime()
      + (alternative?.total_eta_mins ?? 0) * 60_000;
    expect(alternative?.next_matching_ferries?.length).toBeGreaterThan(0);
    expect(new Date(alternative?.next_matching_ferries?.[0].departure_time ?? 0).getTime())
      .toBeGreaterThanOrEqual(alternativeArrival + 15 * 60_000);
  });

  it('uses vehicle handling and customs metrics for every bundled road option', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'CARGO_TRUCK',
      14,
      2,
    );

    expect(response.data.customs_buffer_mins).toBe(25);
    expect(response.data.total_eta_mins).toBe(
      response.data.estimated_travel_time_mins
        + 25
        + (response.data.snap_info?.total_access_time_mins ?? 0),
    );
    expect(response.data.alternative_routes?.[0].total_eta_mins).toBe(
      (response.data.alternative_routes?.[0].estimated_travel_time_mins ?? 0)
        + 25
        + (response.data.snap_info?.total_access_time_mins ?? 0),
    );
    expect(response.data.vehicle_profile).toMatchObject({
      id: 'CARGO_TRUCK',
      max_speed_kph: 55,
      customs_buffer_mins: 25,
      emissions_kg_per_km: 1.05,
    });
    expect(response.data.co2_emissions_kg).toBeGreaterThan(8);
  });

  it('does not add terminal handling time to an ordinary road destination', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      { lat: 1.1465, lng: 104.0125, display_name: 'Nagoya Hill' },
      'CARGO_TRUCK',
      14,
      0,
    );

    expect(response.data.customs_buffer_mins).toBe(0);
    expect(response.data.total_eta_mins).toBe(
      response.data.estimated_travel_time_mins
        + (response.data.snap_info?.total_access_time_mins ?? 0),
    );
  });

  it('sends an expanded vehicle ID and forecast context to both road engines', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('offline'));
    vi.stubGlobal('fetch', fetchMock);
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'CITY_BUS',
      17,
      2,
      'EASY',
    );

    const requestInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(requestInit.body))).toMatchObject({
      vehicle_type: 'CITY_BUS',
      hour: 17,
      weather: 2,
      route_preference: 'EASY',
    });
    expect(planBundledRoute).toHaveBeenCalledWith(
      [origin.lat, origin.lng],
      [destination.lat, destination.lng],
      destination.display_name,
      expect.objectContaining({
        vehicleType: 'CITY_BUS',
        hour: 17,
        weather: 2,
        routePreference: 'EASY',
        networkCongestionScore: expect.any(Number),
        networkCongestionScoreAfter30: expect.any(Number),
      }),
    );
    expect(response.data.vehicle_type).toBe('CITY_BUS');
    expect(response.data.vehicle_profile?.road_preferences.residential).toBe(1.7);
  });

  it('uses the later edge timing once deferral is recommended without double-counting delay', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    planBundledRoute.mockResolvedValue({
      ...bundledPlan,
      routes: [{
        ...bundledPlan.routes[0],
        estimated_travel_time_mins: 30,
        estimated_travel_time_after_30_mins: 20,
        congestion_delay_after_30_mins: 1,
        routing_cost_breakdown: {
          ...bundledPlan.routes[0].routing_cost_breakdown,
          congestion_delay_mins: 5,
          modeled_travel_time_mins: 30,
          generalized_cost_mins: 30.6,
        },
      }],
    });

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      18,
      0,
    );

    expect(response.data.optimal_departure.recommended).toBe('DEFER_30_MINS');
    expect(response.data.estimated_travel_time_mins).toBe(20);
    expect(response.data.routing_cost_breakdown).toMatchObject({
      congestion_delay_mins: 1,
      modeled_travel_time_mins: 20,
      generalized_cost_mins: 20.6,
    });
    expect(response.data.congestion_prediction.estimated_delay_mins).toBe(1);
    expect(response.data.co2_emissions_kg).toBeCloseTo(1.75, 2);
  });

  it('ignores a stale straight-line 200 response and uses the bundled road graph', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        ...liveApiRoute,
        route_data_source: 'offline_straight_line',
        navigation: undefined,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.data.route_data_source).toBe('bundled_client_openstreetmap');
    expect(response.data.navigation?.maneuvers.length).toBeGreaterThan(1);
  });

  it('falls back to bundled roads when an older backend has no free-route endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, {
      status: 404,
      statusText: 'Not Found',
    })));
    planBundledRoute.mockResolvedValue(bundledPlan);

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.data.route_data_source).toBe('bundled_client_openstreetmap');
  });

  it('returns an explicitly estimated local route when both road engines are unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const response = await requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    );

    expect(response.source).toBe('offline');
    expect(response.data.route_type).toBe('ROAD_ROUTE');
    expect(response.data.route_data_source).toBe('offline_access_estimate');
    expect(response.data.route_legs?.map(leg => leg.mode)).toEqual(['ROAD']);
    expect(response.data.navigation?.maneuvers).toHaveLength(2);
    expect(response.data.route_geometry?.length).toBeGreaterThan(2);
  });

  it('surfaces a backend validation error instead of substituting another route', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'Origin is outside the supported Batam road network.' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } },
    )));

    await expect(requestFreeRouteOptimization(
      origin,
      destination,
      'COMMUTER',
      14,
      0,
    )).rejects.toMatchObject({
      status: 400,
      message: 'Origin is outside the supported Batam road network.',
    });
    expect(planBundledRoute).not.toHaveBeenCalled();
  });

  it('keeps endpoint order when the requested direction is reversed', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    const reversedGeometry: [number, number][] = [
      [destination.lat, destination.lng],
      [1.12, 104.035],
      [origin.lat, origin.lng],
    ];
    planBundledRoute.mockResolvedValue({
      origin_snap_m: 8.76,
      destination_snap_m: 12.34,
      routes: [{
        ...bundledPlan.routes[0],
        geometry: reversedGeometry,
        path: ['4', '3', '1'],
      }],
    });

    const response = await requestFreeRouteOptimization(
      destination,
      origin,
      'COMMUTER',
      14,
      0,
    );

    expect(response.data.route_geometry).toEqual(reversedGeometry);
    expect(response.data.requested_origin?.display_name).toBe(destination.display_name);
    expect(response.data.requested_destination?.display_name).toBe(origin.display_name);
  });
});

describe('named-route API fallback', () => {
  it('routes named Batam locations through the same bundled road planner', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('offline'));
    vi.stubGlobal('fetch', fetchMock);
    const localRoadSegment = {
      id: 'local-road:1:2:4',
      name: 'Jalan Cendana',
      highway: 'residential' as const,
      source_node: '1',
      target_node: '2',
      edge_count: 1,
      distance_km: 0.8,
    };
    planBundledRoute.mockResolvedValue({
      ...bundledPlan,
      routes: bundledPlan.routes.map(route => ({
        ...route,
        route_preference: 'LOCAL' as const,
        route_preference_profile: localPreference,
        local_road_distance_km: 0.8,
        local_road_segments: [localRoadSegment],
        local_road_audit: {
          ...emptyLocalRoadAudit,
          requested: true,
          segment_count: 1,
        },
        routing_model: {
          ...route.routing_model,
          objective: localPreference.description,
          selected_preference: 'LOCAL' as const,
          road_scope: 'MAPPED_PUBLIC_LOCAL' as const,
          component_weights: localPreference.component_weights,
        },
      })),
    });

    const response = await requestRouteOptimization(
      'mukakuning',
      'batam_centre',
      'COMMUTER',
      14,
      0,
      'LOCAL',
    );

    const requestInit = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(requestInit.body))).toMatchObject({
      route_preference: 'LOCAL',
    });

    expect(planBundledRoute).toHaveBeenCalledWith(
      [1.0605, 104.0303],
      [1.1318, 104.0554],
      'Batam Centre Ferry Terminal',
      expect.objectContaining({
        vehicleType: 'COMMUTER',
        hour: 14,
        weather: 0,
        routePreference: 'LOCAL',
        networkCongestionScore: expect.any(Number),
        networkCongestionScoreAfter30: expect.any(Number),
      }),
    );
    expect(response.data.route_data_source).toBe('bundled_client_openstreetmap');
    expect(response.data.route_geometry).toEqual(bundledPlan.routes[0].geometry);
    expect(response.data.alternative_routes?.[0].route_geometry)
      .toEqual(bundledPlan.routes[1].geometry);
    expect(response.data.local_road_distance_km).toBe(0.8);
    expect(response.data.local_road_segments).toEqual([localRoadSegment]);
    expect(response.data.local_road_audit?.requested).toBe(true);
    expect(response.data.routing_model).toMatchObject({
      version: 5,
      selected_preference: 'LOCAL',
      road_scope: 'MAPPED_PUBLIC_LOCAL',
    });
    expect(response.data.alternative_routes?.[0].local_road_audit?.requested)
      .toBe(true);
  });
});

describe('model telemetry fallback', () => {
  it('returns the reproducible bundled RF validation manifest without enabling retraining', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const status = await fetchModelStatus();
    expect(status.source).toBe('offline');
    expect(status.data).toMatchObject({
      is_trained: true,
      total_samples: 4000,
      r2_score: 0.9486,
      mae: 2.88,
      rmse: 3.62,
    });
    await expect(requestModelRetrain()).rejects.toMatchObject({ status: 503 });
  });

  it('rejects malformed successful telemetry and uses the versioned bundled manifest', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T12:00:00+07:00',
      data_source: 'simulated',
      metrics: {
        is_trained: true,
        total_samples: 'many',
        r2_score: 0.99,
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const status = await fetchModelStatus();

    expect(status.source).toBe('offline');
    expect(status.data).toMatchObject({
      is_trained: true,
      total_samples: 4000,
      feature_importances: expect.objectContaining({
        'Time of Day (Cyclical)': 0.559,
      }),
    });
  });
});

describe('historical congestion continuity', () => {
  it('returns a populated, explicitly unobserved browser baseline when the API fails', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-10T05:00:00.000Z'));
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const response = await fetchHistoricalCongestion('corridor-1', 7);

    expect(response.source).toBe('offline');
    expect(response.data.hourly_profile).toHaveLength(24);
    expect(response.data.hourly_profile.every(bucket => (
      bucket.avg_score !== null && bucket.sample_count === 0
    ))).toBe(true);
    expect(response.data.weekly_trend).toHaveLength(7);
    expect(response.data.history_metadata).toMatchObject({
      observed: false,
      source: 'browser_modelled_baseline',
      model: 'crossflow_local_forecast_equation_v1',
    });
  });

  it('preserves mixed API provenance instead of presenting the window as observed', async () => {
    const historyMetadata = {
      window_days: 7,
      observed: false,
      contains_observed_samples: true,
      source_counts: { synthetic: 168, tomtom_live: 2 },
      sources: {
        synthetic: { sample_count: 168, observed: false },
        tomtom_live: { sample_count: 2, observed: true },
      },
      latest_sample_at: '2026-08-10T12:00:00+07:00',
      latest_sample_age_seconds: 0,
      freshness: 'current',
      freshness_basis: 'latest sample in the requested history window',
      storage: {
        engine: 'sqlite',
        durability: 'ephemeral_instance_file',
        durable: false,
        shared_across_instances: false,
        fallback_to_memory: false,
      },
      synthetic_seed: {
        source: 'synthetic',
        version: 3,
        days: 14,
        timezone: 'WIB (UTC+07:00)',
        generated_for_date: '2026-08-10',
        observed: false,
      },
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T12:00:00+07:00',
      data_source: 'simulated',
      corridor_id: 'corridor-1',
      days_requested: 7,
      hourly_profile: Array.from({ length: 24 }, (_, hour) => ({
        hour,
        avg_score: 30 + hour,
        sample_count: 7,
      })),
      weekly_trend: [{ date: '2026-08-10', avg_score: 42, sample_count: 170 }],
      history_metadata: historyMetadata,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const response = await fetchHistoricalCongestion('corridor-1', 7);

    expect(response.source).toBe('simulated');
    expect(response.data.history_metadata).toEqual(historyMetadata);
    expect(response.data.history_metadata.observed).toBe(false);
    expect('contains_observed_samples' in response.data.history_metadata
      && response.data.history_metadata.contains_observed_samples).toBe(true);
  });
});

describe('operations provenance continuity', () => {
  it('rejects a malformed success payload and returns the browser-modelled fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T12:00:00+07:00',
      data_source: 'simulated',
      overall_network_status: 'OPTIMAL',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const response = await fetchOperationsSummary();

    expect(response.source).toBe('offline');
    expect(response.data).toEqual(MOCK_OPERATIONS);
  });
});

describe('published ferry schedule continuity', () => {
  it('accepts an atomic official-source refresh response and posts without caller URLs', async () => {
    const fixedNow = new Date('2026-08-09T22:30:00.000Z');
    const sailings = offlineFerries(fixedNow, fixedNow, 12).slice(0, 2);
    const ports = scheduleInformedPortSeed(fixedNow);
    const sourceResults = [
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
    ];
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T05:30:00+07:00',
      data_source: 'published_schedule',
      ferries: sailings,
      ports,
      timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
      refresh: {
        refresh_id: 'official-source-check-20260810T053000+0700',
        status: 'checked',
        started_at: '2026-08-10T05:30:00+07:00',
        finished_at: '2026-08-10T05:30:00+07:00',
        refresh_scope: 'fixed_official_allowlist',
        source_results: sourceResults,
        summary: { verified: 1, failed: 0, permission_gated: 1 },
        schedule_applied: false,
        last_known_good_active: true,
        promotion_requirement: 'Calendar-aware validation is required.',
        data_changed: false,
        limitations: 'Only reviewed official public pages are checked.',
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    const response = await refreshOfficialFerrySources();

    expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/ferry-refresh$/), expect.objectContaining({
      method: 'POST',
    }));
    expect(response.data).toEqual(sailings);
    expect(response.timetable).toEqual(PUBLISHED_FERRY_TIMETABLE_METADATA);
    expect(response.ports).toHaveLength(4);
    expect(response.refresh.status).toBe('checked');
    expect(response.refresh.source_results).toEqual(sourceResults);
  });

  it('rejects a malformed coordinated official-source refresh response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T05:30:00+07:00',
      data_source: 'published_schedule',
      ferries: [],
      ports: [],
      timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    await expect(refreshOfficialFerrySources()).rejects.toThrow(
      'Official-source refresh returned no valid coordinated planning update.',
    );
  });

  it('accepts only the published timetable contract and preserves its metadata', async () => {
    const fixedNow = new Date('2026-08-09T22:30:00.000Z');
    const sailing = offlineFerries(fixedNow, fixedNow, 12)[0];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T05:30:00+07:00',
      data_source: 'published_schedule',
      provenance: {
        ferry_schedule: 'Official operator timetable snapshot; not live operations',
      },
      ferries: [sailing],
      timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const response = await fetchFerries();

    expect(response.source).toBe('simulated');
    expect(response.timetable).toEqual(PUBLISHED_FERRY_TIMETABLE_METADATA);
    expect(response.data).toEqual([sailing]);
  });

  it('falls back to the same published snapshot without fabricated live details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const response = await fetchFerries();

    expect(response.source).toBe('offline');
    expect(response.timetable.status).toBe('published_schedule_snapshot');
    expect(response.data.length).toBeGreaterThan(0);
    expect(response.data.every(ferry => (
      ferry.status === 'SCHEDULED'
      && ferry.available_seats === null
      && ferry.live_status_available === false
      && ferry.data_source === 'official_timetable_snapshot'
    ))).toBe(true);
  });

  it('derives schedule-informed terminal planning estimates without claiming observations', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-09T22:30:00.000Z'));
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const response = await fetchPortIntelligence();
    const harbourBay = response.data.find(port => port.port_name === 'HarbourBay');

    expect(response.source).toBe('offline');
    expect(harbourBay).toMatchObject({
      passenger_queue_mins: 11,
      customs_processing_mins: 9,
      freight_clearance_mins: 23,
      active_berths: 1,
      total_berths: 4,
      status: 'NORMAL',
      next_operator: 'Horizon Fast Ferry',
      data_source: 'browser_schedule_informed_estimate',
      observed: false,
      estimate_basis: 'Published departure density × Batam time-of-day planning profile',
      official_reference_url: 'https://batamport.bpbatam.go.id/harbour-bay/',
    });
    expect(harbourBay?.next_sailing_in_mins).toBe(30);
    expect(harbourBay?.next_vessel).toBeUndefined();
  });

  it('exports an immediate complete terminal seed with numeric planning estimates', () => {
    const seed = scheduleInformedPortSeed(
      new Date('2026-08-09T22:30:00.000Z'),
    );

    expect(seed.map(port => port.port_name)).toEqual([
      'Batam Centre', 'HarbourBay', 'Sekupang', 'Nongsa Pura',
    ]);
    expect(seed.every(port => (
      typeof port.passenger_queue_mins === 'number'
      && typeof port.customs_processing_mins === 'number'
      && port.observed === false
    ))).toBe(true);
  });

  it('merges partial API terminals and fills null queue fields from the seed', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-09T22:30:00.000Z'));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      generated_at: '2026-08-10T05:30:00+07:00',
      data_source: 'simulated',
      ports: [
        {
          port_name: 'Batam Centre',
          terminal_code: 'BCT',
          passenger_queue_mins: null,
          customs_processing_mins: null,
          freight_clearance_mins: 44,
          active_berths: null,
          total_berths: 6,
          status: 'BUSY',
          data_source: 'schedule_informed_planning_estimate',
          observed: false,
          estimate_basis: 'API schedule profile',
          limitations: 'API planning estimate, not observed.',
        },
        {
          port_name: 'Unknown Terminal',
          passenger_queue_mins: 1,
          customs_processing_mins: 1,
        },
      ],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const response = await fetchPortIntelligence();
    const batamCentre = response.data.find(port => port.port_name === 'Batam Centre');

    expect(response.data).toHaveLength(4);
    expect(response.data.some(port => port.port_name === 'Unknown Terminal')).toBe(false);
    expect(batamCentre).toMatchObject({
      passenger_queue_mins: expect.any(Number),
      customs_processing_mins: expect.any(Number),
      freight_clearance_mins: 44,
      status: 'BUSY',
      data_source: 'browser_schedule_informed_estimate',
      observed: false,
    });
    expect(batamCentre?.estimate_basis).toContain('browser continuity');
    expect(response.data.every(port => (
      port.passenger_queue_mins !== null
      && port.customs_processing_mins !== null
    ))).toBe(true);
  });
});

describe('traffic continuity fallback', () => {
  it('keeps five corridor points and all hotspot areas in the offline layer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const response = await fetchLiveTraffic();

    expect(response.source).toBe('offline');
    expect(response.data.overall_source).toBe('local_model');
    expect(response.data.segments).toHaveLength(5);
    expect(response.data.segments.every(segment => Number.isFinite(segment.congestion_index)))
      .toBe(true);
    expect(response.data.zones).toHaveLength(30);
    expect(response.data.coverage).toMatchObject({ hotspot_count: 30 });
    expect(response.data.zones?.every(zone => Number.isFinite(zone.congestion_index)))
      .toBe(true);
    expect(response.data.zones?.every(zone => (
      Number.isFinite(zone.modeled_emissions_pressure.index)
      && zone.modeled_emissions_pressure.observed === false
    ))).toBe(true);
    expect(response.data.coverage?.emissions_pressure_model).toMatchObject({
      methodology_version: 'crossflow-zone-pressure-v1',
      observed: false,
      aggregate_mass_available: false,
    });
    expect(Object.values(
      response.data.coverage?.emissions_pressure_level_counts ?? {},
    ).reduce((total, count) => total + count, 0)).toBe(30);
  });
});
