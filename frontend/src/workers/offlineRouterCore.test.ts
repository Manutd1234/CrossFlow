import { describe, expect, it } from 'vitest';
import batamGraph from '../../../backend/data/batam_graph.json';
import { VEHICLE_CATALOG } from '../data/vehicleCatalog';
import type { RoutePreference, VehicleType } from '../types';
import {
  offlineRouteHeuristicSeconds,
  planOfflineRoadRoutes,
  type OfflineGraph,
} from './offlineRouterCore';

const graph: OfflineGraph = {
  nodes: {
    a: [1, 104],
    b: [1, 104.001],
    c: [1.001, 104.001],
    d: [1.001, 104],
    e: [1.0005, 104.002],
  },
  roads: [
    ['Jalan Utama', 'J1', 'primary'],
    ['Jalan Timur', '', 'secondary'],
    ['Jalan Alternatif', '', 'residential'],
  ],
  adj: {
    a: [['b', 100, 0], ['d', 125, 2]],
    b: [['a', 100, 0], ['c', 100, 1], ['e', 130, 2]],
    c: [['b', 100, 1], ['d', 100, 1], ['e', 130, 2]],
    d: [['a', 125, 2], ['c', 100, 1]],
    e: [['b', 130, 2], ['c', 130, 2]],
  },
};

const vehicleChoiceGraph: OfflineGraph = {
  nodes: {
    a: [1, 104],
    b: [1.003, 104.00225],
    c: [1.0005, 104.00225],
    d: [1, 104.0045],
  },
  roads: [
    ['Main corridor', '', 'secondary'],
    ['Local shortcut', '', 'residential'],
  ],
  adj: {
    a: [['b', 500, 0], ['c', 270, 1]],
    b: [['d', 500, 0]],
    c: [['d', 270, 1]],
    d: [],
  },
};

const preferenceChoiceGraph: OfflineGraph = {
  nodes: {
    a: [1, 104],
    short: [1, 104.0015],
    fast1: [1.00035, 104.001],
    fast2: [0.99965, 104.002],
    easy: [1.002, 104.0015],
    destination: [1, 104.003],
  },
  roads: [
    ['Short Lane', '', 'residential'],
    ['Fast One', '', 'primary'],
    ['Fast Two', '', 'primary'],
    ['Fast Three', '', 'primary'],
    ['Easy Avenue', '', 'primary'],
  ],
  adj: {
    a: [['short', 180, 0], ['fast1', 160, 1], ['easy', 350, 4]],
    short: [['destination', 180, 0]],
    fast1: [['fast2', 160, 2]],
    fast2: [['destination', 160, 3]],
    easy: [['destination', 350, 4]],
    destination: [],
  },
};

describe('bundled road routing', () => {
  it('matches backend weighted objectives and publishes the selected preference', () => {
    const plan = (routePreference: RoutePreference) => (
      planOfflineRoadRoutes(
        preferenceChoiceGraph,
        preferenceChoiceGraph.nodes.a,
        preferenceChoiceGraph.nodes.destination,
        'Destination',
        1,
        {
          vehicleType: 'COMMUTER',
          hour: 2,
          weather: 0,
          networkCongestionScore: 0,
          routePreference,
        },
      ).routes[0]
    );

    const balanced = plan('BALANCED');
    const fastest = plan('FASTEST');
    const shortest = plan('SHORTEST');
    const easy = plan('EASY');
    const local = plan('LOCAL');

    expect(shortest.path).toEqual(['a', 'short', 'destination']);
    expect(fastest.path).toEqual(['a', 'fast1', 'fast2', 'destination']);
    expect(balanced.path).toEqual(fastest.path);
    expect(easy.path).toEqual(['a', 'easy', 'destination']);
    expect(local.path).toEqual(['a', 'short', 'destination']);
    expect(easy.route_preference).toBe('EASY');
    expect(easy.route_preference_profile.component_weights).toMatchObject({
      maneuver_delay_s: 4,
      road_suitability_penalty_s: 2.5,
    });
    expect(shortest.objective_cost_s).toBeCloseTo(
      shortest.distance_km * 1000 / 80 * 3.6,
      2,
    );
    expect(shortest.local_road_distance_km).toBe(0.36);
    expect(shortest.local_road_audit.requested).toBe(false);
    expect(shortest.routing_model).toMatchObject({
      version: 5,
      selected_preference: 'SHORTEST',
      road_scope: 'STANDARD',
    });
    expect(local.route_preference_profile.component_weights).toEqual({
      distance_proxy_s: 1,
      free_flow_s: 0.15,
      congestion_delay_s: 0.65,
      weather_delay_s: 0.3,
      maneuver_delay_s: 0.15,
      road_suitability_penalty_s: 0,
    });
    expect(Object.values(local.route_preference_profile.component_weights)
      .every(weight => weight >= 0)).toBe(true);
    expect(local.route_preference_profile.eligible_vehicle_types).toEqual([
      'COMMUTER', 'ELECTRIC_CAR', 'MOTORCYCLE',
    ]);
    expect(local.route_preference_profile.road_scope).toBe('MAPPED_PUBLIC_LOCAL');

    const straightLineM = 333;
    const heuristic = offlineRouteHeuristicSeconds(straightLineM, 'COMMUTER', 'LOCAL');
    expect(heuristic).toBeCloseTo(straightLineM / 80 * 3.6 * 1.15, 8);
    expect(heuristic).toBeLessThanOrEqual(local.objective_cost_s);

    expect(local.local_road_distance_km).toBe(0.36);
    expect(local.local_road_segments).toEqual([{
      id: 'local-road:a:short:0',
      name: 'Short Lane',
      highway: 'residential',
      source_node: 'a',
      target_node: 'destination',
      edge_count: 2,
      distance_km: 0.36,
    }]);
    expect(local.local_road_audit).toMatchObject({
      requested: true,
      segment_count: 1,
      metadata_scope: 'mapped_osm_residential_motor_roads',
      width_clearance_verified: false,
    });
    expect(local.local_road_audit.note)
      .toContain('lane width and vehicle clearance are not verified');
    expect(local.routing_model).toMatchObject({
      version: 5,
      selected_preference: 'LOCAL',
      road_scope: 'MAPPED_PUBLIC_LOCAL',
      objective_cost_s: local.objective_cost_s,
      distance_is_raw_edge_sum: true,
    });
  });

  it('limits local-road routing to the three clearance-eligible profiles', () => {
    const route = (vehicleType: VehicleType, origin = preferenceChoiceGraph.nodes.a) => (
      planOfflineRoadRoutes(
        preferenceChoiceGraph,
        origin,
        preferenceChoiceGraph.nodes.destination,
        'Destination',
        1,
        {
          vehicleType,
          hour: 2,
          weather: 0,
          networkCongestionScore: 0,
          routePreference: 'LOCAL',
        },
      )
    );

    (['COMMUTER', 'ELECTRIC_CAR', 'MOTORCYCLE'] as const).forEach((vehicleType) => {
      expect(() => route(vehicleType)).not.toThrow();
    });
    (['EXPRESS_VAN', 'MINIBUS', 'CITY_BUS', 'LIGHT_TRUCK', 'CARGO_TRUCK'] as const)
      .forEach((vehicleType) => {
        expect(() => route(vehicleType)).toThrow(/unverified narrow roads/);
      });

    expect(() => route('CARGO_TRUCK', preferenceChoiceGraph.nodes.destination))
      .toThrow(/unverified narrow roads/);
    expect(() => offlineRouteHeuristicSeconds(100, 'CARGO_TRUCK', 'LOCAL'))
      .toThrow(/unverified narrow roads/);
  });

  it('uses selected-hour congestion in generalized cost and can change the chosen path', () => {
    const offPeak = planOfflineRoadRoutes(
      vehicleChoiceGraph,
      vehicleChoiceGraph.nodes.a,
      vehicleChoiceGraph.nodes.d,
      'Destination',
      1,
      { vehicleType: 'COMMUTER', hour: 2, weather: 0 },
    );
    const peak = planOfflineRoadRoutes(
      vehicleChoiceGraph,
      vehicleChoiceGraph.nodes.a,
      vehicleChoiceGraph.nodes.d,
      'Destination',
      1,
      { vehicleType: 'COMMUTER', hour: 17, weather: 0 },
    );

    expect(offPeak.routes[0].path).toEqual(['a', 'b', 'd']);
    expect(peak.routes[0].path).toEqual(['a', 'c', 'd']);
    expect(peak.routes[0].estimated_travel_time_mins)
      .toBeGreaterThan(peak.routes[0].free_flow_time_mins);
    expect(peak.routes[0].routing_cost_mins)
      .toBeGreaterThanOrEqual(peak.routes[0].estimated_travel_time_mins);
    expect(peak.routes[0].distance_km).toBe(0.54);
  });

  it('uses vehicle road suitability without changing physical edge distance', () => {
    const motorcycle = planOfflineRoadRoutes(
      vehicleChoiceGraph,
      vehicleChoiceGraph.nodes.a,
      vehicleChoiceGraph.nodes.d,
      'Destination',
      1,
      { vehicleType: 'MOTORCYCLE', hour: 2, weather: 0 },
    );
    const cargoTruck = planOfflineRoadRoutes(
      vehicleChoiceGraph,
      vehicleChoiceGraph.nodes.a,
      vehicleChoiceGraph.nodes.d,
      'Destination',
      1,
      { vehicleType: 'CARGO_TRUCK', hour: 2, weather: 0 },
    );

    expect(motorcycle.routes[0].path).toEqual(['a', 'c', 'd']);
    expect(motorcycle.routes[0].distance_km).toBe(0.54);
    expect(cargoTruck.routes[0].path).toEqual(['a', 'b', 'd']);
    expect(cargoTruck.routes[0].distance_km).toBe(1);
  });

  it('follows graph edges and creates street-aware turn steps', () => {
    const result = planOfflineRoadRoutes(graph, [1, 104], [1.001, 104.001], 'Destination', 2);
    expect(result.routes[0].path).toEqual(['a', 'b', 'c']);
    expect(result.routes[0].geometry).toEqual([graph.nodes.a, graph.nodes.b, graph.nodes.c]);
    expect(result.routes[0].distance_km).toBe(0.2);
    expect(result.routes[0].navigation.maneuvers.map((step) => step.street)).toContain('Jalan Utama (J1)');
    const maneuvers = result.routes[0].navigation.maneuvers;
    expect(maneuvers[maneuvers.length - 1]?.type).toBe('arrive');
  });

  it('returns only genuinely different bounded alternatives', () => {
    const result = planOfflineRoadRoutes(graph, [1, 104], [1.001, 104.001], 'Destination', 3);
    expect(result.routes.length).toBeGreaterThanOrEqual(1);
    expect(new Set(result.routes.map((route) => route.path.join('>'))).size).toBe(result.routes.length);
    result.routes.slice(1).forEach((route) => {
      expect(route.distance_km).toBeLessThanOrEqual(result.routes[0].distance_km * 1.65);
    });
  });

  it('does not invent a turn when an unbranched road merely bends', () => {
    const curvedRoad: OfflineGraph = {
      nodes: { a: [1, 104], b: [1, 104.001], c: [1.001, 104.001] },
      roads: [['Jalan Melengkung', '', 'primary']],
      adj: {
        a: [['b', 100, 0]],
        b: [['c', 100, 0]],
        c: [],
      },
    };

    const result = planOfflineRoadRoutes(
      curvedRoad,
      curvedRoad.nodes.a,
      curvedRoad.nodes.c,
      'Destination',
      1,
    );
    expect(result.routes[0].navigation.maneuvers.map((maneuver) => maneuver.type))
      .toEqual(['depart', 'arrive']);
  });

  it('keeps one instruction across equivalent OSM name and reference boundaries', () => {
    const metadataBoundaries: OfflineGraph = {
      nodes: {
        a: [1, 104], b: [1, 104.001], c: [1, 104.002], d: [1, 104.003],
      },
      roads: [
        ['Jalan Jenderal Ahmad Yani', 'Nasional 39', 'primary'],
        [null, '39', 'primary'],
        ['Jalan Jendral Ahmad Yani', '39', 'primary'],
      ],
      adj: {
        a: [['b', 120, 0]],
        b: [['c', 120, 1]],
        c: [['d', 120, 2]],
        d: [],
      },
    };

    const result = planOfflineRoadRoutes(
      metadataBoundaries,
      metadataBoundaries.nodes.a,
      metadataBoundaries.nodes.d,
      'Destination',
      1,
    );
    expect(result.routes[0].navigation.maneuvers.map((maneuver) => maneuver.type))
      .toEqual(['depart', 'arrive']);
  });

  it('groups a roundabout and reports the selected exit', () => {
    const roundabout: OfflineGraph = {
      nodes: {
        a: [1, 104],
        b: [1, 104.001],
        c: [1.0005, 104.0015],
        d: [1.001, 104.001],
        e: [1.001, 104.002],
        side: [1.0005, 104.002],
      },
      roads: [
        ['Jalan Masuk', '', 'primary'],
        ['', '', 'primary', 'roundabout'],
        ['Jalan Keluar', 'K2', 'secondary'],
      ],
      adj: {
        a: [['b', 120, 0]],
        b: [['c', 80, 1]],
        c: [['d', 80, 1], ['side', 100, 2]],
        d: [['e', 120, 2]],
        e: [],
        side: [],
      },
    };

    const result = planOfflineRoadRoutes(
      roundabout,
      roundabout.nodes.a,
      roundabout.nodes.e,
      'Destination',
      1,
    );
    const maneuvers = result.routes[0].navigation.maneuvers;
    expect(maneuvers.map((maneuver) => maneuver.type))
      .toEqual(['depart', 'roundabout', 'arrive']);
    expect(maneuvers[1]).toMatchObject({
      exit_number: 2,
      street: 'Jalan Keluar (K2)',
    });
    expect(maneuvers[1].instruction).toContain('take exit 2');
  });

  it('keeps the exact parallel edge selected by A* for distance and street metadata', () => {
    const parallelEdges: OfflineGraph = {
      nodes: { a: [1, 104], b: [1, 104.0001], c: [1, 104.0002] },
      roads: [
        ['Long parallel road', '', 'primary'],
        ['Chosen parallel road', '', 'primary'],
      ],
      adj: {
        a: [['b', 100, 0], ['b', 50, 1]],
        b: [['c', 50, 1]],
        c: [],
      },
    };

    const result = planOfflineRoadRoutes(
      parallelEdges,
      parallelEdges.nodes.a,
      parallelEdges.nodes.c,
      'Destination',
      1,
    );
    expect(result.routes[0].distance_km).toBe(0.1);
    expect(result.routes[0].navigation.maneuvers[0].street).toBe('Chosen parallel road');
  });

  it('routes the reported Batam Centre to Batamindo failure on real roads', () => {
    const result = planOfflineRoadRoutes(
      batamGraph as unknown as OfflineGraph,
      [1.1318, 104.0554],
      [1.0605, 104.0303],
      'Batamindo Industrial Park',
      3,
    );

    expect(result.routes).toHaveLength(3);
    expect(result.routes[0].distance_km).toBeGreaterThan(9);
    expect(result.routes[0].distance_km).toBeLessThan(11);
    expect(result.routes[0].geometry.length).toBeGreaterThan(50);
    expect(result.routes[0].geometry).not.toEqual([
      [1.1318, 104.0554],
      [1.0605, 104.0303],
    ]);
    expect(result.routes[0].navigation.data_source).toBe('openstreetmap_edge_metadata');
    expect(result.routes[0].navigation.maneuvers.some(
      (maneuver) => maneuver.street !== 'Unnamed road',
    )).toBe(true);
    expect(result.routes[1].overlap_ratio).toBeLessThanOrEqual(0.82);
    expect(result.routes[2].overlap_ratio).toBeLessThanOrEqual(0.82);
  }, 20_000);

  it('plans a finite real-road route for every supported vehicle profile', () => {
    VEHICLE_CATALOG.forEach((profile) => {
      const result = planOfflineRoadRoutes(
        batamGraph as unknown as OfflineGraph,
        [1.1318, 104.0554],
        [1.0605, 104.0303],
        'Batamindo Industrial Park',
        1,
        { vehicleType: profile.id, hour: 17, weather: 1 },
      );
      expect(result.routes[0].geometry.length, profile.id).toBeGreaterThan(50);
      expect(result.routes[0].distance_km, profile.id).toBeGreaterThan(9);
      expect(result.routes[0].estimated_travel_time_mins, profile.id).toBeGreaterThan(0);
      expect(result.routes[0].routing_cost_mins, profile.id)
        .toBeGreaterThanOrEqual(result.routes[0].estimated_travel_time_mins);
    });
  }, 20_000);

  it('retains genuinely different alternatives for freight routing', () => {
    const result = planOfflineRoadRoutes(
      batamGraph as unknown as OfflineGraph,
      [1.1318, 104.0554],
      [1.0605, 104.0303],
      'Batamindo Industrial Park',
      3,
      {
        vehicleType: 'CARGO_TRUCK',
        hour: 17,
        weather: 1,
        networkCongestionScore: 78,
        networkCongestionScoreAfter30: 65,
      },
    );

    expect(result.routes.length).toBeGreaterThanOrEqual(2);
    result.routes.slice(1).forEach((route) => {
      expect(route.overlap_ratio).toBeLessThanOrEqual(0.82);
      expect(route.distance_km).toBeLessThanOrEqual(result.routes[0].distance_km * 1.65);
    });
  });
});
