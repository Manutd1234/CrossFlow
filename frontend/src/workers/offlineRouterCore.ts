import { vehicleProfile } from '../data/vehicleCatalog';
import { routePreferenceProfile } from '../data/routePreferences';
import type {
  LocalRoadAudit,
  LocalRoadSegment,
  RoutePreference,
  RoutePreferenceProfile,
  VehicleType,
} from '../types';

export type Coordinate = [number, number];

export interface OfflineRoutingModel {
  version: 5;
  cost_unit: 'seconds';
  objective_cost_unit: 'weighted_seconds';
  objective: string;
  selected_preference: RoutePreference;
  road_scope: RoutePreferenceProfile['road_scope'];
  component_weights: RoutePreferenceProfile['component_weights'];
  objective_cost_s: number;
  network_congestion_score: number;
  weather: number;
  spatial_zone_count: 0;
  distance_is_raw_edge_sum: true;
  traffic_provenance: string;
  limitations: string;
}

type RawRoad = [
  name?: string | null,
  ref?: string | null,
  highway?: string | null,
  junction?: string | null,
];
type RawEdge = [target: string | number, distanceM: number, roadIndex?: number];

export interface OfflineGraph {
  nodes: Record<string, Coordinate>;
  adj: Record<string, RawEdge[]>;
  roads?: RawRoad[];
  node_meta?: Record<string, { highway?: string; crossing?: string; barrier?: string }>;
}

export interface OfflineManeuver {
  step: number;
  type: string;
  modifier: string;
  instruction: string;
  street: string;
  road_ref?: string;
  distance_m: number;
  cumulative_distance_m: number;
  icon: string;
  coords: Coordinate;
  exit_number?: number;
}

export interface OfflineNavigation {
  schema_version: 1;
  data_source: 'openstreetmap_edge_metadata' | 'openstreetmap_geometry';
  maneuvers: OfflineManeuver[];
  landmarks_along_route: [];
  traffic_lights_count: number;
  route_narrative_words: string;
}

export interface OfflineRoadOption {
  id: string;
  name: string;
  description: string;
  distance_km: number;
  geometry: Coordinate[];
  navigation: OfflineNavigation;
  path: string[];
  overlap_ratio?: number;
  /** Physical free-flow time from road-class speeds plus maneuver controls. */
  free_flow_time_mins: number;
  /** Vehicle-, hour-, congestion- and weather-aware time for this path. */
  estimated_travel_time_mins: number;
  /** Same physical path evaluated 30 minutes later. */
  estimated_travel_time_after_30_mins: number;
  /** Generalized planning cost retained separately from the selected search objective. */
  routing_cost_mins: number;
  /** Unpenalized preference-weighted A* objective for this physical path. */
  objective_cost_s: number;
  route_preference: RoutePreference;
  route_preference_profile: RoutePreferenceProfile;
  local_road_distance_km: number;
  local_road_segments: LocalRoadSegment[];
  local_road_audit: LocalRoadAudit;
  routing_model: OfflineRoutingModel;
  routing_cost_breakdown: {
    free_flow_mins: number;
    congestion_delay_mins: number;
    weather_delay_mins: number;
    maneuver_delay_mins: number;
    road_suitability_penalty_mins: number;
    modeled_travel_time_mins: number;
    generalized_cost_mins: number;
  };
  congestion_delay_after_30_mins: number;
}

export interface OfflineRoadPlan {
  routes: OfflineRoadOption[];
  origin_snap_m: number;
  destination_snap_m: number;
}

export interface OfflineRoutingContext {
  vehicleType: VehicleType;
  hour: number;
  weather: number;
  routePreference?: RoutePreference;
  /** 0–100 requested-hour network score from the local forecast model. */
  networkCongestionScore?: number;
  /** 0–100 score for the same network 30 minutes later. */
  networkCongestionScoreAfter30?: number;
}

interface SearchResult {
  path: string[];
  edges: RawEdge[];
  distanceM: number;
  routingCostSeconds: number;
}

const EARTH_RADIUS_M = 6_371_008.8;
const MAX_SNAP_M = 1_000;
const MAX_ALTERNATIVE_RATIO = 1.65;
const MAX_SHARED_DISTANCE_RATIO = 0.82;
const LOCAL_ROAD_AUDIT_NOTE = (
  'These are mapped OSM residential motor roads selected by the route. Service alleys, '
  + 'drains and footpaths are absent; lane width and vehicle clearance are not verified.'
);
const DEFAULT_ROUTING_CONTEXT: OfflineRoutingContext = {
  vehicleType: 'COMMUTER',
  hour: 14,
  weather: 0,
  routePreference: 'BALANCED',
};

function validatePreferenceVehicle(
  preference: RoutePreferenceProfile,
  vehicleType: VehicleType,
): void {
  if (preference.eligible_vehicle_types.includes(vehicleType)) return;
  const profile = vehicleProfile(vehicleType);
  throw new Error(
    `${preference.name} is available only for ${preference.eligible_vehicle_types.join(', ')}; `
    + `${profile.label} cannot be routed onto unverified narrow roads.`,
  );
}

const ROAD_SPEED_KPH: Record<string, number> = {
  motorway: 80,
  trunk: 65,
  primary: 55,
  secondary: 45,
  tertiary: 38,
  unclassified: 30,
  residential: 25,
  living_street: 12,
  road: 25,
};

function roadClass(road: RawRoad): { highway: string; isLink: boolean } {
  const raw = road[2] || 'road';
  const isLink = raw.endsWith('_link');
  return { highway: isLink ? raw.slice(0, -5) : raw, isLink };
}

function cyclicHourDistance(hour: number, centre: number): number {
  const normalized = ((hour % 24) + 24) % 24;
  const direct = Math.abs(normalized - centre);
  return Math.min(direct, 24 - direct);
}

/** Smooth bounded Batam demand prior used only when live edge telemetry is unavailable. */
export function trafficLoadAtHour(hour: number): number {
  const morning = Math.exp(-(cyclicHourDistance(hour, 7.5) ** 2) / (2 * 1.25 ** 2));
  const evening = Math.exp(-(cyclicHourDistance(hour, 17.5) ** 2) / (2 * 1.55 ** 2));
  const midday = Math.exp(-(cyclicHourDistance(hour, 12.5) ** 2) / (2 * 3.2 ** 2));
  return Math.min(0.82, 0.06 + 0.64 * Math.max(morning, evening) + 0.1 * midday);
}

/** 0–100 fallback forecast when the API layer cannot supply predictLocal's score. */
export function networkCongestionScoreAtHour(hour: number): number {
  return Math.round(12 + trafficLoadAtHour(hour) * 96);
}

function congestionExposure(highway: string): number {
  return {
    motorway: 0.78,
    trunk: 0.85,
    primary: 0.95,
    secondary: 1,
    tertiary: 0.9,
    unclassified: 0.72,
    residential: 0.58,
    living_street: 0.48,
  }[highway] ?? 0.8;
}

function weatherExposure(highway: string): number {
  return {
    motorway: 0.9,
    trunk: 0.92,
    primary: 0.95,
    secondary: 1,
    tertiary: 1.07,
    unclassified: 1.15,
    residential: 1.12,
    living_street: 1.2,
  }[highway] ?? 1.1;
}

function baseEdgeSeconds(graph: OfflineGraph, edge: RawEdge, context: OfflineRoutingContext): number {
  const profile = vehicleProfile(context.vehicleType);
  const { highway, isLink } = roadClass(roadForEdge(graph, edge));
  const classSpeed = (ROAD_SPEED_KPH[highway] ?? ROAD_SPEED_KPH.road) * (isLink ? 0.72 : 1);
  const speedKph = Math.max(8, Math.min(profile.maxSpeedKph, classSpeed * profile.speedFactor));
  return edge[1] / (speedKph / 3.6);
}

function timedEdgeSeconds(
  graph: OfflineGraph,
  edge: RawEdge,
  context: OfflineRoutingContext,
  networkCongestionScore: number,
): number {
  const profile = vehicleProfile(context.vehicleType);
  const { highway } = roadClass(roadForEdge(graph, edge));
  const freeFlowSeconds = baseEdgeSeconds(graph, edge, context);
  const boundedScore = Math.max(0, Math.min(100, networkCongestionScore));
  const effectiveScore = 0.35 * boundedScore;
  const congestionDelaySeconds = freeFlowSeconds
    * profile.congestionSensitivity
    * congestionExposure(highway)
    * 1.8
    * (effectiveScore / 100) ** 2;
  // Forecast congestion represents network demand/queues; this separately
  // itemized term represents safe-speed loss on wet roads.
  const weatherImpact = [0, 0.12, 0.3][Math.max(0, Math.min(2, Math.round(context.weather)))] ?? 0;
  const weatherDelaySeconds = freeFlowSeconds
    * weatherImpact
    * profile.weatherSensitivity
    * weatherExposure(highway);
  return freeFlowSeconds + congestionDelaySeconds + weatherDelaySeconds;
}

function suitabilityMultiplier(graph: OfflineGraph, edge: RawEdge, context: OfflineRoutingContext): number {
  const profile = vehicleProfile(context.vehicleType);
  const { highway, isLink } = roadClass(roadForEdge(graph, edge));
  const classPenalty = highway === 'residential'
    ? profile.residentialPenalty
    : highway === 'unclassified'
      ? profile.unclassifiedPenalty
      : highway === 'tertiary'
        ? profile.tertiaryPenalty
        : 1;
  return Math.max(1, classPenalty * (isLink ? profile.linkPenalty : 1));
}

function radians(degrees: number): number {
  return degrees * Math.PI / 180;
}

export function haversineM(a: Coordinate, b: Coordinate): number {
  const p1 = radians(a[0]);
  const p2 = radians(b[0]);
  const dp = p2 - p1;
  const dl = radians(b[1] - a[1]);
  const h = Math.sin(dp / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(h));
}

class MinHeap {
  private values: Array<[number, number, string]> = [];

  get size(): number {
    return this.values.length;
  }

  push(value: [number, number, string]): void {
    this.values.push(value);
    let index = this.values.length - 1;
    while (index > 0) {
      const parent = Math.floor((index - 1) / 2);
      if (this.values[parent][0] <= value[0]) break;
      this.values[index] = this.values[parent];
      index = parent;
    }
    this.values[index] = value;
  }

  pop(): [number, number, string] | undefined {
    if (!this.values.length) return undefined;
    const first = this.values[0];
    const last = this.values.pop();
    if (this.values.length && last) {
      let index = 0;
      while (true) {
        const left = index * 2 + 1;
        const right = left + 1;
        if (left >= this.values.length) break;
        const child = right < this.values.length
          && this.values[right][0] < this.values[left][0] ? right : left;
        if (this.values[child][0] >= last[0]) break;
        this.values[index] = this.values[child];
        index = child;
      }
      this.values[index] = last;
    }
    return first;
  }
}

function edgeKey(source: string, target: string, edge: RawEdge): string {
  const endpoints = source < target ? `${source}>${target}` : `${target}>${source}`;
  return `${endpoints}:${edge[2] ?? ''}`;
}

function routingStateKey(node: string, previousNode: string | null, incomingRoadIndex?: number): string {
  return `${node}|${previousNode ?? ''}|${incomingRoadIndex ?? ''}`;
}

function transitionDelaySeconds(
  graph: OfflineGraph,
  previousNode: string | null,
  node: string,
  incomingEdge: RawEdge | null,
  outgoingEdge: RawEdge,
  context: OfflineRoutingContext,
  includeSignal = true,
): number {
  const profile = vehicleProfile(context.vehicleType);
  const signalSeconds = includeSignal && graph.node_meta?.[node]?.highway === 'traffic_signals'
    ? profile.signalDelaySeconds
    : 0;
  if (!previousNode || !incomingEdge) return signalSeconds;
  const next = String(outgoingEdge[0]);
  const before = bearing(graph.nodes[previousNode], graph.nodes[node]);
  const after = bearing(graph.nodes[node], graph.nodes[next]);
  const absoluteTurn = Math.abs(turnDelta(before, after));
  const incomingRoad = roadForEdge(graph, incomingEdge);
  const outgoingRoad = roadForEdge(graph, outgoingEdge);
  const legalChoices = new Set(
    (graph.adj[node] ?? [])
      .filter((edge) => String(edge[0]) !== previousNode)
      .map((edge) => String(edge[0])),
  );
  const roadChanged = !sameRoad(incomingRoad, outgoingRoad);
  if (!roadChanged && !(legalChoices.size > 1 && absoluteTurn >= 35)) return signalSeconds;
  const turnFactor = absoluteTurn >= 170
    ? 3
    : absoluteTurn >= 135
      ? 1.6
      : absoluteTurn >= 40
        ? 1
        : absoluteTurn >= 15
          ? 0.45
          : roadChanged ? 0.2 : 0;
  const leavingRoundabout = ['roundabout', 'circular'].includes(incomingRoad[3] ?? '');
  const maneuverSeconds = profile.turnPenaltySeconds * Math.max(
    turnFactor,
    leavingRoundabout ? 0.65 : 0,
  );
  const shortManeuverSeconds = roadChanged && outgoingEdge[1] < 70
    ? profile.shortManeuverPenaltySeconds
    : 0;
  return maneuverSeconds + shortManeuverSeconds + signalSeconds;
}

interface RouteTiming {
  freeFlowSeconds: number;
  estimatedSeconds: number;
  estimatedAfter30Seconds: number;
  congestionDelaySeconds: number;
  congestionDelayAfter30Seconds: number;
  weatherDelaySeconds: number;
  maneuverDelaySeconds: number;
  suitabilityPenaltySeconds: number;
}

interface EdgeObjectiveComponents {
  distanceProxySeconds: number;
  freeFlowSeconds: number;
  congestionDelaySeconds: number;
  weatherDelaySeconds: number;
  maneuverDelaySeconds: number;
  suitabilityPenaltySeconds: number;
  timedRoadSeconds: number;
}

function preferenceObjectiveSeconds(
  components: EdgeObjectiveComponents,
  preference: RoutePreferenceProfile,
): number {
  // Preserve the established operation order for the two original objectives.
  if (preference.id === 'BALANCED') {
    return components.timedRoadSeconds
      + components.suitabilityPenaltySeconds
      + components.maneuverDelaySeconds;
  }
  if (preference.id === 'FASTEST') {
    return components.timedRoadSeconds + components.maneuverDelaySeconds;
  }
  const weights = preference.component_weights;
  return (
    weights.distance_proxy_s * components.distanceProxySeconds
    + weights.free_flow_s * components.freeFlowSeconds
    + weights.congestion_delay_s * components.congestionDelaySeconds
    + weights.weather_delay_s * components.weatherDelaySeconds
    + weights.maneuver_delay_s * components.maneuverDelaySeconds
    + weights.road_suitability_penalty_s * components.suitabilityPenaltySeconds
  );
}

function pathObjectiveSeconds(
  route: Pick<SearchResult, 'distanceM'>,
  timing: RouteTiming,
  context: OfflineRoutingContext,
): number {
  const profile = vehicleProfile(context.vehicleType);
  const preference = routePreferenceProfile(context.routePreference ?? 'BALANCED');
  return preferenceObjectiveSeconds({
    distanceProxySeconds: route.distanceM / profile.maxSpeedKph * 3.6,
    freeFlowSeconds: timing.freeFlowSeconds,
    congestionDelaySeconds: timing.congestionDelaySeconds,
    weatherDelaySeconds: timing.weatherDelaySeconds,
    maneuverDelaySeconds: timing.maneuverDelaySeconds,
    suitabilityPenaltySeconds: timing.suitabilityPenaltySeconds,
    timedRoadSeconds: timing.estimatedSeconds - timing.maneuverDelaySeconds,
  }, preference);
}

function objectiveHeuristicSeconds(
  straightLineM: number,
  maximumSpeedKph: number,
  preference: RoutePreferenceProfile,
): number {
  // Only components with a physical straight-line lower bound belong here.
  // LOCAL therefore uses exactly (distance proxy 1 + free-flow 0.15); its
  // congestion, weather and maneuver weights remain nonnegative edge costs.
  const lowerBoundWeight = preference.component_weights.distance_proxy_s
    + preference.component_weights.free_flow_s;
  return Math.max(0, straightLineM) / maximumSpeedKph * 3.6 * lowerBoundWeight;
}

export function offlineRouteHeuristicSeconds(
  straightLineM: number,
  vehicleType: VehicleType,
  routePreference: RoutePreference,
): number {
  const profile = vehicleProfile(vehicleType);
  const preference = routePreferenceProfile(routePreference);
  validatePreferenceVehicle(preference, profile.id);
  return objectiveHeuristicSeconds(
    straightLineM,
    profile.maxSpeedKph,
    preference,
  );
}

function timingForPath(
  graph: OfflineGraph,
  route: Pick<SearchResult, 'path' | 'edges'>,
  context: OfflineRoutingContext,
): RouteTiming {
  const currentScore = context.networkCongestionScore
    ?? networkCongestionScoreAtHour(context.hour);
  const laterScore = context.networkCongestionScoreAfter30
    ?? networkCongestionScoreAtHour(context.hour + 0.5);
  let freeFlowSeconds = 0;
  let estimatedSeconds = 0;
  let estimatedAfter30Seconds = 0;
  let congestionDelaySeconds = 0;
  let congestionDelayAfter30Seconds = 0;
  let weatherDelaySeconds = 0;
  let maneuverDelaySeconds = 0;
  let suitabilityPenaltySeconds = 0;
  route.edges.forEach((edge, index) => {
    const previousNode = index > 0 ? route.path[index - 1] : null;
    const node = route.path[index];
    const incomingEdge = index > 0 ? route.edges[index - 1] : null;
    const freeFlow = baseEdgeSeconds(graph, edge, context);
    const weatherAdjusted = timedEdgeSeconds(graph, edge, context, 0);
    const currentTimed = timedEdgeSeconds(graph, edge, context, currentScore);
    const laterTimed = timedEdgeSeconds(graph, edge, context, laterScore);
    const maneuver = transitionDelaySeconds(
      graph,
      previousNode,
      node,
      incomingEdge,
      edge,
      context,
    );
    freeFlowSeconds += freeFlow;
    weatherDelaySeconds += weatherAdjusted - freeFlow;
    congestionDelaySeconds += currentTimed - weatherAdjusted;
    congestionDelayAfter30Seconds += laterTimed - weatherAdjusted;
    maneuverDelaySeconds += maneuver;
    suitabilityPenaltySeconds += freeFlow * (suitabilityMultiplier(graph, edge, context) - 1);
    estimatedSeconds += currentTimed + maneuver;
    estimatedAfter30Seconds += laterTimed + maneuver;
  });
  return {
    freeFlowSeconds,
    estimatedSeconds,
    estimatedAfter30Seconds,
    congestionDelaySeconds,
    congestionDelayAfter30Seconds,
    weatherDelaySeconds,
    maneuverDelaySeconds,
    suitabilityPenaltySeconds,
  };
}

function aStar(
  graph: OfflineGraph,
  source: string,
  destination: string,
  penalties: ReadonlyMap<string, number>,
  context: OfflineRoutingContext,
): SearchResult | null {
  if (!graph.adj[source] || !graph.nodes[destination]) return null;
  const heap = new MinHeap();
  const sourceState = routingStateKey(source, null);
  const best = new Map<string, number>([[sourceState, 0]]);
  const stateData = new Map<string, {
    node: string;
    previousNode: string | null;
    incomingEdge: RawEdge | null;
  }>([[sourceState, { node: source, previousNode: null, incomingEdge: null }]]);
  const previous = new Map<string, {
    previousState: string;
    sourceNode: string;
    edge: RawEdge;
  }>();
  const closed = new Set<string>();
  const goal = graph.nodes[destination];
  const profile = vehicleProfile(context.vehicleType);
  const preference = routePreferenceProfile(context.routePreference ?? 'BALANCED');
  const currentCongestionScore = context.networkCongestionScore
    ?? networkCongestionScoreAtHour(context.hour);
  heap.push([
    objectiveHeuristicSeconds(
      haversineM(graph.nodes[source], goal),
      profile.maxSpeedKph,
      preference,
    ),
    0,
    sourceState,
  ]);

  while (heap.size) {
    const current = heap.pop();
    if (!current) break;
    const [, cost, state] = current;
    if (closed.has(state)) continue;
    const currentData = stateData.get(state);
    if (!currentData) continue;
    const { node, previousNode, incomingEdge } = currentData;
    if (node === destination) {
      const path = [node];
      const edges: RawEdge[] = [];
      let cursor = state;
      while (previous.has(cursor)) {
        const step = previous.get(cursor);
        if (!step) break;
        path.push(step.sourceNode);
        edges.push(step.edge);
        cursor = step.previousState;
      }
      path.reverse();
      edges.reverse();
      return {
        path,
        edges,
        distanceM: edges.reduce((total, edge) => total + edge[1], 0),
        routingCostSeconds: cost,
      };
    }
    closed.add(state);

    for (const edge of graph.adj[node] ?? []) {
      const next = String(edge[0]);
      const nextState = routingStateKey(next, node, edge[2]);
      if (closed.has(nextState)) continue;
      const freeFlowSeconds = baseEdgeSeconds(graph, edge, context);
      const weatherAdjustedSeconds = timedEdgeSeconds(graph, edge, context, 0);
      const timedRoadSeconds = timedEdgeSeconds(graph, edge, context, currentCongestionScore);
      const suitabilityPenaltySeconds = freeFlowSeconds
        * (suitabilityMultiplier(graph, edge, context) - 1);
      const maneuverSeconds = transitionDelaySeconds(
        graph,
        previousNode,
        node,
        incomingEdge,
        edge,
        context,
      );
      const edgeObjectiveSeconds = preferenceObjectiveSeconds({
        distanceProxySeconds: edge[1] / profile.maxSpeedKph * 3.6,
        freeFlowSeconds,
        congestionDelaySeconds: timedRoadSeconds - weatherAdjustedSeconds,
        weatherDelaySeconds: weatherAdjustedSeconds - freeFlowSeconds,
        maneuverDelaySeconds: maneuverSeconds,
        suitabilityPenaltySeconds,
        timedRoadSeconds,
      }, preference);
      const diversityPenalty = penalties.get(edgeKey(node, next, edge)) ?? 1;
      const tentative = cost + edgeObjectiveSeconds * diversityPenalty;
      if (tentative >= (best.get(nextState) ?? Number.POSITIVE_INFINITY)) continue;
      best.set(nextState, tentative);
      stateData.set(nextState, { node: next, previousNode: node, incomingEdge: edge });
      previous.set(nextState, { previousState: state, sourceNode: node, edge });
      const heuristic = objectiveHeuristicSeconds(
        haversineM(graph.nodes[next], goal),
        profile.maxSpeedKph,
        preference,
      );
      heap.push([tentative + heuristic, tentative, nextState]);
    }
  }
  return null;
}

function nearestNodes(
  graph: OfflineGraph,
  point: Coordinate,
  limit = 4,
  requireOutgoing = false,
): Array<[string, number]> {
  const nearest: Array<[string, number]> = [];
  for (const [nodeId, coordinate] of Object.entries(graph.nodes)) {
    if (requireOutgoing && !graph.adj[nodeId]?.length) continue;
    const distance = haversineM(point, coordinate);
    if (nearest.length < limit || distance < nearest[nearest.length - 1][1]) {
      nearest.push([nodeId, distance]);
      nearest.sort((a, b) => a[1] - b[1]);
      if (nearest.length > limit) nearest.pop();
    }
  }
  return nearest;
}

function sharedDistanceRatio(candidate: SearchResult, accepted: SearchResult): number {
  const acceptedEdges = new Set(accepted.edges.map(
    (edge, index) => edgeKey(accepted.path[index], accepted.path[index + 1], edge),
  ));
  let shared = 0;
  let candidateDistance = 0;
  candidate.edges.forEach((edge, index) => {
    const distance = edge[1];
    candidateDistance += distance;
    if (acceptedEdges.has(edgeKey(candidate.path[index], candidate.path[index + 1], edge))) {
      shared += distance;
    }
  });
  return candidateDistance > 0 ? shared / candidateDistance : 1;
}

function bearing(a: Coordinate, b: Coordinate): number {
  const p1 = radians(a[0]);
  const p2 = radians(b[0]);
  const dl = radians(b[1] - a[1]);
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2)
    - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function turnDelta(before: number, after: number): number {
  return ((after - before + 540) % 360) - 180;
}

function roadForEdge(graph: OfflineGraph, edge: RawEdge): RawRoad {
  const roadIndex = edge[2];
  return roadIndex === undefined ? [] : (graph.roads?.[roadIndex] ?? []);
}

function roadLabel(road: RawRoad): string {
  const name = road[0]?.trim();
  const ref = road[1]?.trim();
  if (name && ref && !name.includes(ref)) return `${name} (${ref})`;
  return name || ref || 'Unnamed road';
}

function normalizedRoadName(value?: string | null): string {
  return (value ?? '')
    .toLocaleLowerCase('id')
    .replace(/\bjendral\b/g, 'jenderal')
    .replace(/\bjl\.?\b/g, 'jalan')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function normalizedRoadRef(value?: string | null): string {
  return (value ?? '')
    .toLocaleLowerCase('id')
    .replace(/\b(nasional|national|route|rute)\b/g, '')
    .replace(/[^a-z0-9]+/g, '')
    .trim();
}

function sameRoad(first: RawRoad, second: RawRoad): boolean {
  const firstName = normalizedRoadName(first[0]);
  const secondName = normalizedRoadName(second[0]);
  if (firstName && secondName && firstName === secondName) return true;
  const firstRef = normalizedRoadRef(first[1]);
  const secondRef = normalizedRoadRef(second[1]);
  if (firstRef && secondRef && firstRef === secondRef) return true;
  if (firstName || firstRef || secondName || secondRef) return false;
  return first[2] === second[2];
}

function sameMappedLocalRoad(
  firstRoad: RawRoad,
  firstRoadIndex: number | undefined,
  secondRoad: RawRoad,
  secondRoadIndex: number | undefined,
): boolean {
  // Keep this intentionally narrower than navigation's spelling-tolerant
  // grouping: it mirrors backend `_same_road` for auditable OSM segments.
  const firstName = firstRoad[0]?.toLowerCase().trim();
  const secondName = secondRoad[0]?.toLowerCase().trim();
  if (firstName && secondName && firstName === secondName) return true;
  const firstRef = firstRoad[1]?.toLowerCase().trim();
  const secondRef = secondRoad[1]?.toLowerCase().trim();
  if (firstRef && secondRef && firstRef === secondRef) return true;
  if (firstRoad[0] || firstRoad[1] || secondRoad[0] || secondRoad[1]) return false;
  return firstRoad[2] === secondRoad[2] && firstRoadIndex === secondRoadIndex;
}

function mappedLocalRoadLabel(road: RawRoad): string {
  if (road[0]) return road[0];
  if (road[1]) return road[1];
  if (road[2]) return `Unnamed ${road[2].replace(/_/g, ' ')} road`;
  return 'Unnamed road';
}

function localRoadAuditForPath(
  graph: OfflineGraph,
  route: Pick<SearchResult, 'path' | 'edges'>,
): {
  distance_km: number;
  segment_count: number;
  segments: LocalRoadSegment[];
  metadata_scope: LocalRoadAudit['metadata_scope'];
  width_clearance_verified: false;
} {
  interface WorkingSegment extends Omit<LocalRoadSegment, 'distance_km'> {
    distance_m: number;
    road: RawRoad;
    roadIndex: number | undefined;
  }

  const segments: LocalRoadSegment[] = [];
  let current: WorkingSegment | null = null;
  let totalDistanceM = 0;

  const flush = () => {
    if (!current) return;
    segments.push({
      id: current.id,
      name: current.name,
      highway: current.highway,
      source_node: current.source_node,
      target_node: current.target_node,
      edge_count: current.edge_count,
      distance_km: Math.round(current.distance_m) / 1000,
    });
    current = null;
  };

  route.edges.forEach((edge, index) => {
    const source = route.path[index];
    const target = String(edge[0]);
    const road = roadForEdge(graph, edge);
    const { highway } = roadClass(road);
    if (highway !== 'residential' && highway !== 'living_street') {
      flush();
      return;
    }
    totalDistanceM += edge[1];

    if (
      !current
      || !sameMappedLocalRoad(current.road, current.roadIndex, road, edge[2])
    ) {
      flush();
      current = {
        id: `local-road:${source}:${target}:${edge[2] ?? -1}`,
        name: mappedLocalRoadLabel(road),
        highway,
        source_node: source,
        target_node: target,
        edge_count: 1,
        distance_m: edge[1],
        road,
        roadIndex: edge[2],
      };
      return;
    }

    current.target_node = target;
    current.edge_count += 1;
    current.distance_m += edge[1];
  });
  flush();

  return {
    distance_km: Math.round(totalDistanceM) / 1000,
    segment_count: segments.length,
    segments,
    metadata_scope: 'mapped_osm_residential_motor_roads',
    width_clearance_verified: false,
  };
}

function roundaboutExitCount(
  graph: OfflineGraph,
  path: string[],
  firstEdge: number,
  lastEdgeExclusive: number,
): number {
  let exits = 0;
  for (let edgeIndex = firstEdge; edgeIndex < lastEdgeExclusive; edgeIndex += 1) {
    const nodeIndex = edgeIndex + 1;
    const node = path[nodeIndex];
    const previous = path[nodeIndex - 1];
    const choices = new Set(
      (graph.adj[node] ?? [])
        .filter((edge) => {
          const road = roadForEdge(graph, edge);
          return String(edge[0]) !== previous
            && road[3] !== 'roundabout'
            && road[3] !== 'circular';
        })
        .map((edge) => String(edge[0])),
    );
    exits += choices.size;
  }
  return Math.max(1, exits);
}

function turnDescription(delta: number): { type: string; modifier: string; icon: string; verb: string } {
  const absolute = Math.abs(delta);
  const side = delta < 0 ? 'left' : 'right';
  if (absolute < 25) return { type: 'continue', modifier: 'straight', icon: 'continue', verb: 'Continue' };
  if (absolute < 55) return { type: 'turn', modifier: `slight_${side}`, icon: `turn_${side}`, verb: `Bear ${side}` };
  if (absolute < 135) return { type: 'turn', modifier: side, icon: `turn_${side}`, verb: `Turn ${side}` };
  return { type: 'turn', modifier: `sharp_${side}`, icon: `turn_${side}`, verb: `Make a sharp ${side}` };
}

function navigationForPath(graph: OfflineGraph, route: SearchResult, destinationName: string): OfflineNavigation {
  const { path } = route;
  const edges = route.edges.map((rawEdge, index) => {
    const source = path[index];
    const target = path[index + 1];
    const rawRoad = roadForEdge(graph, rawEdge);
    return {
      source,
      target,
      distance: rawEdge[1],
      road: roadLabel(rawRoad),
      ref: rawRoad[1]?.trim() || undefined,
      junction: rawRoad[3],
    };
  });
  const maneuvers: OfflineManeuver[] = [];
  let segmentStart = 0;
  let cumulative = 0;
  let mergeNextAfterRoundabout = false;

  const addSegment = (endExclusive: number, isFirst: boolean) => {
    const segment = edges.slice(segmentStart, endExclusive);
    if (!segment.length) return;
    const distance = Math.round(segment.reduce((sum, edge) => sum + edge.distance, 0));
    const first = segment[0];
    const isRoundabout = first.junction === 'roundabout' || first.junction === 'circular';
    if (mergeNextAfterRoundabout && !isRoundabout) {
      const previous = maneuvers[maneuvers.length - 1];
      previous.distance_m += distance;
      cumulative += distance;
      mergeNextAfterRoundabout = false;
      return;
    }
    let descriptor = { type: 'depart', modifier: 'straight', icon: 'continue', verb: 'Head' };
    if (!isFirst) {
      const beforeIndex = Math.max(0, segmentStart - 1);
      const before = bearing(
        graph.nodes[path[beforeIndex]],
        graph.nodes[path[beforeIndex + 1]],
      );
      const afterIndex = Math.min(path.length - 2, segmentStart);
      const after = bearing(graph.nodes[path[afterIndex]], graph.nodes[path[afterIndex + 1]]);
      descriptor = turnDescription(turnDelta(before, after));
    }
    const exitEdge = edges[endExclusive];
    const exitNumber = isRoundabout
      ? roundaboutExitCount(graph, path, segmentStart, endExclusive)
      : undefined;
    const targetRoad = isRoundabout ? (exitEdge?.road ?? first.road) : first.road;
    const instruction = isRoundabout
      ? `At the roundabout, take exit ${exitNumber} onto ${targetRoad}`
      : `${descriptor.verb} onto ${first.road}`;
    maneuvers.push({
      step: maneuvers.length + 1,
      type: isRoundabout ? 'roundabout' : descriptor.type,
      modifier: isRoundabout ? 'roundabout' : descriptor.modifier,
      instruction,
      street: targetRoad,
      road_ref: isRoundabout ? exitEdge?.ref : first.ref,
      distance_m: distance,
      cumulative_distance_m: Math.round(cumulative),
      icon: isRoundabout ? 'roundabout' : descriptor.icon,
      coords: graph.nodes[first.source],
      exit_number: exitNumber,
    });
    cumulative += distance;
    mergeNextAfterRoundabout = isRoundabout;
  };

  for (let index = 1; index < edges.length; index += 1) {
    const incomingRoad = roadForEdge(graph, route.edges[index - 1]);
    const outgoingRoad = roadForEdge(graph, route.edges[index]);
    const outgoingIsKnown = Boolean(outgoingRoad[0] || outgoingRoad[1]);
    const roadChanged = (outgoingIsKnown && !sameRoad(incomingRoad, outgoingRoad))
      || edges[index].junction !== edges[index - 1].junction;
    const before = bearing(graph.nodes[path[index - 1]], graph.nodes[path[index]]);
    const after = bearing(graph.nodes[path[index]], graph.nodes[path[index + 1]]);
    const meaningfulTurn = Math.abs(turnDelta(before, after)) >= 35;
    const legalChoices = (graph.adj[path[index]] ?? []).filter(
      (edge) => String(edge[0]) !== path[index - 1],
    );
    const isDecision = legalChoices.length > 1;
    const insideRoundabout = ['roundabout', 'circular'].includes(edges[index - 1].junction ?? '')
      && ['roundabout', 'circular'].includes(edges[index].junction ?? '');
    if (roadChanged || (!insideRoundabout && isDecision && meaningfulTurn)) {
      addSegment(index, segmentStart === 0);
      segmentStart = index;
    }
  }
  addSegment(edges.length, segmentStart === 0);

  maneuvers.push({
    step: maneuvers.length + 1,
    type: 'arrive',
    modifier: 'straight',
    instruction: `Arrive at ${destinationName}`,
    street: edges[edges.length - 1]?.road ?? 'Destination',
    distance_m: 0,
    cumulative_distance_m: Math.round(cumulative),
    icon: 'arrive',
    coords: graph.nodes[path[path.length - 1]],
  });

  const namedRoads = [...new Set(edges.map((edge) => edge.road).filter((road) => road !== 'Unnamed road'))];
  const narrative = namedRoads.length
    ? `Follow ${namedRoads.slice(0, 5).join(', ')}${namedRoads.length > 5 ? ', and connecting roads' : ''}.`
    : `Follow the mapped road network for ${(route.distanceM / 1000).toFixed(1)} km.`;
  const trafficLights = path.filter(
    (node) => graph.node_meta?.[node]?.highway === 'traffic_signals',
  ).length;
  return {
    schema_version: 1,
    data_source: graph.roads?.length ? 'openstreetmap_edge_metadata' : 'openstreetmap_geometry',
    maneuvers,
    landmarks_along_route: [],
    traffic_lights_count: trafficLights,
    route_narrative_words: narrative,
  };
}

function routeName(index: number): string {
  if (index === 0) return 'Recommended route';
  if (index === 1) return 'Alternative 1';
  return `Alternative ${index}`;
}

function roundedMinutes(seconds: number): number {
  return Math.round(seconds / 60 * 100) / 100;
}

export function planOfflineRoadRoutes(
  graph: OfflineGraph,
  origin: Coordinate,
  destination: Coordinate,
  destinationName = 'your destination',
  maximumRoutes = 3,
  requestedContext: OfflineRoutingContext = DEFAULT_ROUTING_CONTEXT,
): OfflineRoadPlan {
  const normalizedVehicleType = vehicleProfile(requestedContext.vehicleType).id;
  const requestedPreference = routePreferenceProfile(
    requestedContext.routePreference ?? 'BALANCED',
  );
  validatePreferenceVehicle(requestedPreference, normalizedVehicleType);
  const context: OfflineRoutingContext = {
    vehicleType: normalizedVehicleType,
    hour: Math.max(0, Math.min(23, requestedContext.hour)),
    weather: Math.max(0, Math.min(2, Math.round(requestedContext.weather))),
    routePreference: requestedContext.routePreference ?? 'BALANCED',
    networkCongestionScore: requestedContext.networkCongestionScore,
    networkCongestionScoreAfter30: requestedContext.networkCongestionScoreAfter30,
  };
  const origins = nearestNodes(graph, origin, 1, true);
  const destinations = nearestNodes(graph, destination, 1);
  if (!origins.length || !destinations.length) throw new Error('The offline road graph is empty.');
  if (origins[0][1] > MAX_SNAP_M || destinations[0][1] > MAX_SNAP_M) {
    throw new Error('The selected point is outside the supported Batam road network.');
  }

  const selectedOrigin = origins[0];
  const selectedDestination = destinations[0];
  if (selectedOrigin[0] === selectedDestination[0]) {
    throw new Error('Origin and destination snap to the same road point; choose points further apart.');
  }
  const primary = aStar(
    graph,
    selectedOrigin[0],
    selectedDestination[0],
    new Map(),
    context,
  );
  if (!primary) throw new Error('No drivable path connects the selected points.');

  const accepted: SearchResult[] = [primary];
  const primaryTiming = timingForPath(graph, primary, context);
  const primaryGeneralizedSeconds = primaryTiming.estimatedSeconds
    + primaryTiming.suitabilityPenaltySeconds;
  const primaryNavigation = navigationForPath(graph, primary, destinationName);
  const primaryTurns = primaryNavigation.maneuvers.filter(
    (maneuver) => maneuver.type === 'turn' || maneuver.type === 'roundabout',
  ).length;
  const primaryShortTurns = primaryNavigation.maneuvers.filter(
    (maneuver) => (maneuver.type === 'turn' || maneuver.type === 'roundabout')
      && maneuver.distance_m < 50,
  ).length;
  const penaltyMultipliers = [1.2, 1.35, 1.55, 1.8, 2.2, 2.8, 3.6];
  for (const multiplier of penaltyMultipliers) {
    if (accepted.length >= maximumRoutes) break;
    const penalties = new Map<string, number>();
    accepted.forEach((route) => {
      route.edges.forEach((edge, index) => {
        const node = route.path[index];
        const next = route.path[index + 1];
        penalties.set(edgeKey(node, next, edge), multiplier);
      });
    });
    const candidate = aStar(
      graph,
      selectedOrigin[0],
      selectedDestination[0],
      penalties,
      context,
    );
    if (!candidate || candidate.distanceM > primary.distanceM * MAX_ALTERNATIVE_RATIO) continue;
    const candidateTiming = timingForPath(graph, candidate, context);
    const candidateGeneralizedSeconds = candidateTiming.estimatedSeconds
      + candidateTiming.suitabilityPenaltySeconds;
    if (candidateGeneralizedSeconds > primaryGeneralizedSeconds * 1.85) continue;
    if (accepted.some((route) => route.path.join(',') === candidate.path.join(',')
      && route.edges.every((edge, index) => edge[2] === candidate.edges[index]?.[2]))) continue;
    if (accepted.some((route) => sharedDistanceRatio(candidate, route) > MAX_SHARED_DISTANCE_RATIO)) continue;
    const candidateNavigation = navigationForPath(graph, candidate, destinationName);
    const candidateTurns = candidateNavigation.maneuvers.filter(
      (maneuver) => maneuver.type === 'turn' || maneuver.type === 'roundabout',
    ).length;
    const candidateShortTurns = candidateNavigation.maneuvers.filter(
      (maneuver) => (maneuver.type === 'turn' || maneuver.type === 'roundabout')
        && maneuver.distance_m < 50,
    ).length;
    if (candidateNavigation.maneuvers.length > Math.max(
      primaryNavigation.maneuvers.length + 10,
      Math.ceil(primaryNavigation.maneuvers.length * 1.75),
    )) continue;
    if (candidateTurns > Math.max(primaryTurns + 8, primaryTurns * 2)) continue;
    if (candidateShortTurns > Math.max(primaryShortTurns + 4, primaryShortTurns * 3)) continue;
    accepted.push(candidate);
  }

  return {
    origin_snap_m: selectedOrigin[1],
    destination_snap_m: selectedDestination[1],
    routes: accepted.map((route, index) => {
      const timing = timingForPath(graph, route, context);
      const preference = routePreferenceProfile(context.routePreference ?? 'BALANCED');
      const modeledTravelMinutes = roundedMinutes(timing.estimatedSeconds);
      const suitabilityMinutes = roundedMinutes(timing.suitabilityPenaltySeconds);
      const generalizedMinutes = roundedMinutes(
        timing.estimatedSeconds + timing.suitabilityPenaltySeconds,
      );
      const objectiveCostSeconds = Math.round(
        pathObjectiveSeconds(route, timing, context) * 1000,
      ) / 1000;
      const localRoadAudit = localRoadAuditForPath(graph, route);
      const networkCongestionScore = Math.round(
        Math.max(0, Math.min(
          100,
          context.networkCongestionScore ?? networkCongestionScoreAtHour(context.hour),
        )) * 10,
      ) / 10;
      return {
        id: index === 0 ? 'recommended' : `alternative-${index}`,
        name: routeName(index),
        description: index === 0
          ? 'Optimized for the selected vehicle, departure time, traffic pattern, weather and maneuver burden.'
          : 'A genuinely different road path within the route-quality and detour limits.',
        distance_km: Math.round(route.distanceM / 10) / 100,
        geometry: route.path.map((node) => graph.nodes[node]),
        navigation: navigationForPath(graph, route, destinationName),
        path: route.path,
        free_flow_time_mins: roundedMinutes(timing.freeFlowSeconds),
        estimated_travel_time_mins: modeledTravelMinutes,
        estimated_travel_time_after_30_mins: roundedMinutes(timing.estimatedAfter30Seconds),
        congestion_delay_after_30_mins: roundedMinutes(
          timing.congestionDelayAfter30Seconds,
        ),
        routing_cost_mins: generalizedMinutes,
        objective_cost_s: objectiveCostSeconds,
        route_preference: preference.id,
        route_preference_profile: preference,
        local_road_distance_km: localRoadAudit.distance_km,
        local_road_segments: localRoadAudit.segments,
        local_road_audit: {
          requested: preference.id === 'LOCAL',
          segment_count: localRoadAudit.segment_count,
          metadata_scope: localRoadAudit.metadata_scope,
          width_clearance_verified: false,
          note: LOCAL_ROAD_AUDIT_NOTE,
        },
        routing_model: {
          version: 5,
          cost_unit: 'seconds',
          objective_cost_unit: 'weighted_seconds',
          objective: preference.description,
          selected_preference: preference.id,
          road_scope: preference.road_scope,
          component_weights: preference.component_weights,
          objective_cost_s: objectiveCostSeconds,
          network_congestion_score: networkCongestionScore,
          weather: context.weather,
          spatial_zone_count: 0,
          distance_is_raw_edge_sum: true,
          traffic_provenance: 'bundled local forecast; no live spatial edge zones',
          limitations: (
            'Planning estimate from OSM road classes; no lane speeds, OSM turn-restriction '
            + 'relations, or vehicle height/weight clearance.'
          ),
        },
        routing_cost_breakdown: {
          free_flow_mins: roundedMinutes(timing.freeFlowSeconds),
          congestion_delay_mins: roundedMinutes(timing.congestionDelaySeconds),
          weather_delay_mins: roundedMinutes(timing.weatherDelaySeconds),
          maneuver_delay_mins: roundedMinutes(timing.maneuverDelaySeconds),
          road_suitability_penalty_mins: suitabilityMinutes,
          modeled_travel_time_mins: modeledTravelMinutes,
          generalized_cost_mins: generalizedMinutes,
        },
        overlap_ratio: index === 0
          ? undefined
          : Math.round(sharedDistanceRatio(route, primary) * 100) / 100,
      };
    }),
  };
}
