/** Where the data on screen actually came from. */
export type DataSource = 'live' | 'simulated' | 'offline';

/** Stable API identifiers shared by the controls, backend and browser router. */
export type VehicleType =
  | 'COMMUTER'
  | 'ELECTRIC_CAR'
  | 'MOTORCYCLE'
  | 'EXPRESS_VAN'
  | 'MINIBUS'
  | 'CITY_BUS'
  | 'LIGHT_TRUCK'
  | 'CARGO_TRUCK';

export interface VehicleProfileSnapshot {
  id: VehicleType;
  name: string;
  max_speed_kph: number;
  speed_factor: number;
  congestion_sensitivity: number;
  weather_sensitivity: number;
  road_preferences: {
    residential: number;
    unclassified: number;
    tertiary: number;
    link: number;
  };
  turn_penalty_s: number;
  short_maneuver_penalty_s: number;
  signal_delay_s: number;
  customs_buffer_mins: number;
  emissions_kg_per_km: number;
  idle_emissions_kg_per_hour: number;
  assumptions_source?: string;
  legal_restrictions_note?: string;
}

/**
 * Provenance attached to every API response.
 *
 * Optional throughout, so the app still builds and runs against an older
 * backend. Anything missing is treated as 'simulated' — the honest default for
 * a synthetic telemetry engine.
 */
export interface Provenance {
  road_network?: string;
  road_network_license?: string;
  routing?: string;
  traffic?: string;
  ferry_schedule?: string;
  operations?: string;
}

export interface Envelope {
  generated_at?: string;
  data_source?: string;
  provenance?: Provenance;
  engine?: string;
  model?: string;
}

export interface Corridor {
  id: string;
  name: string;
  origin?: string;
  destination?: string;
  distance_km: number;
  base_time_mins: number;
  live_congestion_score: number;
  delay_mins: number;
  status: 'SMOOTH' | 'HEAVY' | 'CRITICAL';
  risk_level: 'LOW' | 'MODERATE' | 'HIGH';
  forecast_30m: number;
  forecast_60m?: number;
  trend?: 'UPWARD' | 'DOWNWARD' | 'STABLE';
  ferry_surge?: number;
  surge_source?: FerrySchedule | null;
  key_checkpoints: string[];
  is_weekend?: boolean;
}

/** A named place that can be used as either end of a planned road trip. */
export interface RouteLocation {
  id: string;
  name: string;
  category: string;
  lat: number;
  lng: number;
  ferry_port?: string | null;
}

export type FerryTimezone = 'Asia/Jakarta' | 'Asia/Singapore';

export interface FerrySchedule {
  sailing_id?: string;
  ferry_name: string;
  operator?: string;
  departure_port: string;
  arrival_port: string;
  /** ISO 8601 with explicit offset. Render via formatTime, never raw. */
  departure_time: string;
  arrival_time?: string;
  departure_timezone?: FerryTimezone;
  arrival_timezone?: FerryTimezone;
  estimated_crossing_mins?: number;
  arrival_time_is_estimate?: boolean;
  minutes_until_departure?: number;
  status: 'SCHEDULED' | 'ON_TIME' | 'BOARDING' | 'FINAL_CALL' | 'DEPARTED' | 'DELAYED_10M';
  gate_status?: 'GATE_SCHEDULED' | 'GATE_OPEN' | 'BOARDING' | 'FINAL_CALL' | 'GATE_CLOSED';
  berth?: string;
  speed_knots?: number;
  available_seats: number | null;
  capacity?: number | null;
  live_status_available?: boolean;
  data_source?: 'official_timetable_snapshot';
  schedule_source_id?: string;
  schedule_source_url?: string;
  booking_url?: string;
  schedule_effective_from?: string | null;
  schedule_last_verified_at?: string;
  schedule_calendar_note?: string;
}

export interface FerryTimetableSource {
  source_id: string;
  operator: string;
  schedule_url: string;
  booking_url: string;
  effective_from: string | null;
  last_verified_at: string;
  calendar_note: string;
}

export interface FerryTimetableService {
  service_id: string;
  operator: string;
  departure_port: string;
  arrival_port: string;
  departure_timezone?: FerryTimezone;
  arrival_timezone?: FerryTimezone;
  source_id: string;
  estimated_crossing_mins: number;
  daily_departures: string[];
  weekend_additions: string[];
}

export interface FerryTimetableMetadata {
  schema_version: 1;
  snapshot_id: string;
  timezone: 'Asia/Jakarta';
  last_verified_at: string;
  status: 'published_schedule_snapshot';
  live_board_url: string;
  limitations: string;
  sources: FerryTimetableSource[];
}

export interface FerryTimetableSnapshot extends FerryTimetableMetadata {
  services: FerryTimetableService[];
}

export interface PortStatus {
  port_name: string;
  terminal_code: string;
  passenger_queue_mins: number | null;
  customs_processing_mins: number | null;
  freight_clearance_mins: number | null;
  active_berths: number | null;
  total_berths: number | null;
  status: 'NORMAL' | 'BUSY' | 'CONGESTED' | null;
  next_sailing_in_mins?: number | null;
  next_vessel?: string | null;
  next_operator?: string | null;
  data_source?: 'schedule_informed_planning_estimate' | 'browser_schedule_informed_estimate';
  observed?: false;
  estimate_basis?: string;
  official_reference_url?: string;
  limitations?: string;
}


export interface ModelMetrics {
  is_trained: boolean;
  retraining_enabled?: boolean;
  total_samples: number;
  r2_score: number;
  mae: number;
  rmse: number;
  last_trained_at: string | null;
  feature_importances: Record<string, number>;
}




export interface ShortcutItem {
  id: string;
  name: string;
  badge: string;
  time_saved_mins: number;
  co2_saved_kg?: number;
  description: string;
}

export interface RoutingCostBreakdown {
  free_flow_mins: number;
  congestion_delay_mins: number;
  weather_delay_mins: number;
  maneuver_delay_mins: number;
  road_suitability_penalty_mins: number;
  modeled_travel_time_mins: number;
  generalized_cost_mins: number;
}

export type RoutePreference = 'BALANCED' | 'FASTEST' | 'SHORTEST' | 'EASY' | 'LOCAL';

export interface RoutePreferenceWeights {
  distance_proxy_s: number;
  free_flow_s: number;
  congestion_delay_s: number;
  weather_delay_s: number;
  maneuver_delay_s: number;
  road_suitability_penalty_s: number;
}

export interface RoutePreferenceProfile {
  id: RoutePreference;
  name: string;
  description: string;
  component_weights: RoutePreferenceWeights;
  objective_cost_unit: 'weighted_seconds';
  eligible_vehicle_types: readonly VehicleType[];
  road_scope: 'STANDARD' | 'MAPPED_PUBLIC_LOCAL';
  road_scope_note: string;
  distance_proxy_note: string;
}

export interface LocalRoadSegment {
  id: string;
  name: string;
  highway: 'residential' | 'living_street';
  source_node: number | string;
  target_node: number | string;
  edge_count: number;
  distance_km: number;
}

export interface LocalRoadAudit {
  requested: boolean;
  segment_count: number;
  metadata_scope: 'mapped_osm_residential_motor_roads';
  width_clearance_verified: false;
  note: string;
}

export interface RouteBenchmarkRoute {
  id: string;
  duration_seconds: number;
  duration_mins: number;
  distance_meters: number;
  distance_km: number;
  route_labels: string[];
  summary: string;
}

export interface RouteBenchmarkPreferenceDetails {
  requested: RoutePreference;
  honored: boolean;
  experimental: boolean;
  provider_translation: string;
  requested_reference_routes?: string[];
  note: string;
}

export interface RouteBenchmarkResult {
  generated_at: string;
  data_source: 'google_routes_v2_text_benchmark';
  provenance: {
    benchmark: 'Google Routes API v2';
    attribution: 'Google Maps';
    policy_url: string;
    external_route_content_persisted: false;
  };
  benchmark_type: 'google_routes_v2_text_metrics';
  provider: 'google_routes_v2';
  attribution: 'Google Maps';
  policy_url: string;
  route_preference: RoutePreference;
  preference_honored: boolean;
  preference_honored_details: RouteBenchmarkPreferenceDetails;
  routes: RouteBenchmarkRoute[];
  cacheable: false;
  persisted: false;
  training_eligible: false;
  map_overlay_allowed: false;
}

export interface LocalRoutingModelSummary {
  version: number;
  cost_unit: 'seconds';
  objective_cost_unit?: 'weighted_seconds';
  objective: string;
  selected_preference?: RoutePreference;
  road_scope?: 'STANDARD' | 'MAPPED_PUBLIC_LOCAL';
  component_weights?: RoutePreferenceWeights;
  objective_cost_s?: number;
  network_congestion_score: number;
  weather: number;
  spatial_zone_count: number;
  distance_is_raw_edge_sum: boolean;
  traffic_provenance: string;
  limitations: string;
}

export interface ExternalRoutingModelSummary {
  version: 'external';
  objective: string;
  selected_preference?: RoutePreference;
  preference_honored?: boolean;
  component_weights?: RoutePreferenceWeights;
  limitations: string;
}

export type RoutingModelSummary = LocalRoutingModelSummary | ExternalRoutingModelSummary;

/** A complete, selectable road path returned alongside the recommended path. */
export interface RoadRouteOption {
  id: string;
  name: string;
  /** Human-readable reason to choose this path; never a navigation claim. */
  description: string;
  route_geometry: [number, number][];
  distance_km: number;
  estimated_travel_time_mins: number;
  total_eta_mins?: number;
  co2_emissions_kg: number;
  co2_saved_kg: number;
  route_type?: string;
  route_data_source?: string;
  avoided_congested_zones?: string[];
  /** Fraction of physical road length shared with the recommended route. */
  overlap_ratio?: number;
  /** Internal congestion-weighted routing cost; not a physical distance. */
  congestion_cost?: number | null;
  objective_cost_s?: number | null;
  route_preference?: RoutePreference;
  route_preference_profile?: RoutePreferenceProfile;
  free_flow_time_mins?: number;
  estimated_travel_time_after_30_mins?: number;
  congestion_delay_after_30_mins?: number;
  routing_cost_breakdown?: RoutingCostBreakdown | null;
  routing_model?: RoutingModelSummary | null;
  local_road_distance_km?: number;
  local_road_segments?: LocalRoadSegment[];
  local_road_audit?: LocalRoadAudit;
  navigation?: NavigationData;
  next_matching_ferries?: FerrySchedule[];
  /** Ordered road/ferry legs for a composed Singapore-Batam journey. */
  route_legs?: RouteLeg[];
  road_distance_km?: number;
  ferry_distance_km?: number;
  emissions_scope?: string;
  access_distance_km?: number;
  access_time_mins?: number;
  snap_info?: {
    origin_snap_m: number;
    destination_snap_m: number;
    origin_access_time_mins?: number;
    destination_access_time_mins?: number;
    total_access_distance_km?: number;
    total_access_time_mins?: number;
    assumed_access_speed_kph?: number;
    included_in_road_distance?: boolean;
  };
}

export type AlternativeRouteOption = RoadRouteOption;

export interface ManeuverItem {
  step: number;
  type: string;
  /** Directional qualifier supplied by the navigation schema (for example slight_left). */
  modifier?: string;
  instruction: string;
  street: string;
  road_ref?: string | null;
  distance_m: number;
  cumulative_distance_m?: number;
  icon: string;
  coords: [number, number];
  bearing_before?: number | null;
  bearing_after?: number | null;
  exit_number?: number;
  landmark?: string | null;
}

export interface LandmarkItem {
  name: string;
  type: string;
  lat: number;
  lng: number;
  details?: string;
  distance_m?: number;
}

export interface NavigationData {
  schema_version?: number;
  data_source?: string;
  maneuvers: ManeuverItem[];
  landmarks_along_route: LandmarkItem[];
  traffic_lights_count: number;
  route_narrative_words?: string;
}

export type RouteLegMode = 'ROAD' | 'FERRY';

/** One independently sourced leg in a local or cross-border journey. */
export interface RouteLeg {
  mode: RouteLegMode;
  from_name: string;
  to_name: string;
  geometry: [number, number][];
  distance_km: number;
  duration_mins: number;
  wait_mins?: number;
  data_source: string;
  is_estimate: boolean;
  limitations: string;
  geometry_note?: string;
  schedule_status?: 'PUBLISHED_DEPARTURE_SELECTED' | 'CROSSING_DURATION_REFERENCE_ONLY' | 'NO_MATCHING_PUBLISHED_DEPARTURE';
  selected_sailing?: FerrySchedule | null;
  vehicle_role?: 'FIRST_LAST_MILE_ACCESS';
  vehicle_carried_onboard?: false;
  navigation?: NavigationData;
}

/** Endpoint snapshot returned with a route. Older/newer APIs use either name key. */
export interface RouteEndpointSnapshot {
  lat: number;
  lng: number;
  name?: string;
  display_name?: string;
  node_id?: number | null;
  region?: 'BATAM' | 'SINGAPORE';
}

/** Optional wall-clock scheduling constraint for route requests.
 *
 * Values should be ISO-8601 strings with an explicit offset (for example
 * `2026-08-16T09:30:00+07:00`). The legacy `hour` argument remains supported
 * when neither field is supplied.
 */
export interface RouteScheduleOptions {
  departure_at?: string;
  arrive_by?: string;
}

export interface RouteSchedulingMetadata {
  mode: 'HOUR' | 'DEPART_AT' | 'ARRIVE_BY';
  requested_departure_at?: string | null;
  requested_arrive_by?: string | null;
  deadline_slack_mins?: number | null;
}

export interface RouteOptimizationResult {
  /** Content-addressed identifier returned by the authoritative route API. */
  route_id?: string;
  /** Short seven-character code suitable for driver handoff. */
  route_code?: string;
  route_type?: 'ROAD_ROUTE' | 'MULTIMODAL_FERRY_ROUTE' | 'FASTEST_BYPASS' | 'ECO_EFFICIENT' | 'PORT_SYNC';
  corridor: {
    id: string;
    name: string;
    origin?: string;
    destination?: string;
    distance_km: number;
    base_time_mins: number;
    straight_line_km?: number;
    detour_ratio?: number | null;
  };
  vehicle_type: VehicleType;
  vehicle_profile?: VehicleProfileSnapshot;
  route_preference?: RoutePreference;
  route_preference_profile?: RoutePreferenceProfile;
  objective_cost_s?: number | null;
  routing_cost_breakdown?: RoutingCostBreakdown | null;
  routing_model?: RoutingModelSummary | null;
  local_road_distance_km?: number;
  local_road_segments?: LocalRoadSegment[];
  local_road_audit?: LocalRoadAudit;
  planning_traffic_snapshot?: PlanningTrafficSnapshot;
  generalized_cost_mins?: number | null;
  planned_departure?: string;
  /** Scheduling constraint echoed by newer route APIs. */
  scheduling?: RouteSchedulingMetadata;
  schedule_mode?: 'departure' | 'arrive_by';
  requested_departure_at?: string;
  requested_arrive_by?: string;
  deadline_slack_mins?: number | null;
  congestion_prediction: {
    current_score: number;
    predicted_30min: number;
    predicted_60min: number;
    estimated_delay_mins: number;
    status?: string;
    risk_level: string;
    trend: string;
  };
  estimated_travel_time_mins: number;
  customs_buffer_mins: number;
  total_eta_mins: number;
  access_distance_km?: number;
  access_time_mins?: number;
  co2_emissions_kg: number;
  co2_saved_kg: number;
  ferry_surge?: number;
  optimal_departure: {
    recommended: 'DEPART_NOW' | 'DEFER_30_MINS';
    time_saved_mins: number;
    reason: string;
  };
  next_matching_ferries: FerrySchedule[];
  route_legs?: RouteLeg[];
  vehicle_transfer_policy?: 'FIRST_LAST_MILE_ONLY' | 'ROAD_JOURNEY';
  vehicle_transfer_note?: string | null;
  road_distance_km?: number;
  ferry_distance_km?: number;
  emissions_scope?: string;
  ferry_connection_note?: string;
  route_geometry?: [number, number][];
  route_data_source?: string;
  snap_info?: {
    origin_snap_m: number;
    destination_snap_m: number;
    origin_access_time_mins?: number;
    destination_access_time_mins?: number;
    total_access_distance_km?: number;
    total_access_time_mins?: number;
    assumed_access_speed_kph?: number;
    included_in_road_distance?: boolean;
  };
  shortcuts_used?: ShortcutItem[];
  local_tips?: string[];
  avoided_congested_zones?: string[];
  alternative_routes?: AlternativeRouteOption[];
  /** Explains why topology/quality constraints yielded fewer than three paths. */
  alternatives_note?: string | null;
  navigation?: NavigationData;
  /** Immutable request snapshot used to keep a persisted result correctly labelled. */
  requested_origin?: RouteEndpointSnapshot;
  requested_destination?: RouteEndpointSnapshot;
}


export interface AlertItem {
  id: string;
  severity: 'WARNING' | 'INFO' | 'CRITICAL';
  corridor_id?: string;
  title: string;
  message: string;
  /** ISO 8601 with explicit offset. */
  timestamp: string;
}

export interface Co2Methodology {
  advised_trips_per_corridor_per_hour: number;
  avoidable_delay_fraction: number;
  idle_burn_kg_per_hour: number;
  basis: string;
}

export interface OperationsScenarioAssumption {
  response_key: string;
  classification: 'illustrative_scenario_assumption';
  observed: false;
  live: false;
  measured: false;
  formula?: string;
  description?: string;
  shares?: Record<string, number>;
}

export interface OperationsMethodology {
  observed: false;
  source: string;
  model: {
    id: string;
    congestion: string;
    emissions: string;
  };
  scopes: {
    network: string;
    emissions: string;
    ferries: string;
  };
  assumptions: {
    current_network_kg_per_hour: OperationsScenarioAssumption;
    fixed_fleet_split: OperationsScenarioAssumption;
    hourly_curves: OperationsScenarioAssumption;
  };
}

export interface OperationsSummary extends Envelope {
  overall_network_status: string;
  average_congestion_index: number;
  active_bottlenecks: number;
  bottleneck_corridors?: { id: string; name: string; score: number }[];
  bottleneck_threshold?: number;
  /** Compatibility key: a modelled scenario opportunity, never a measured reduction. */
  total_co2_reduced_today_kg: number;
  modeled_avoidable_emissions_opportunity_kg_today?: number;
  projected_full_day_co2_kg?: number;
  modeled_projected_full_day_avoidable_emissions_kg?: number;
  co2_by_corridor_kg?: Record<string, number>;
  co2_by_vehicle_type?: Record<string, number>;
  hourly_co2_distribution?: { hour: string; baseline_co2: number; optimized_co2: number }[];
  /** Compatibility key for a modelled illustrative scenario rate; it is not live. */
  live_co2_rate_kg_hr?: number;
  api_source?: string;
  co2_methodology?: Co2Methodology;
  operations_methodology?: OperationsMethodology;
  active_ferry_sailings: number;
  scheduled_ferry_departures_next_12h?: number;
  alerts: AlertItem[];
}


/** Road geometry for one corridor, from A* over the OSM graph. */
export interface CorridorRoute {
  id: string;
  name: string;
  distance_km: number;
  straight_line_km: number;
  detour_ratio: number | null;
  geometry: [number, number][];
}

/** Demo freight consignments. Not a live feed — no public TMS API exists. */
export interface Shipment {
  id: string;
  origin: string;
  destination: string;
  carrier: string;
  vessel: string;
  status: 'IN_TRANSIT' | 'CUSTOMS_CLEARANCE' | 'SCHEDULED';
  progress: number;
  eta: string;
  co2_saved: string;
}

/** A fetch result plus where it came from, so the UI can label itself. */
export interface Fetched<T> {
  data: T;
  source: DataSource;
  fetchedAt: string;
  provenance?: Provenance;
}

export interface FetchedFerrySchedule extends Fetched<FerrySchedule[]> {
  timetable: FerryTimetableMetadata;
}

export type FerryRefreshStatus =
  | 'checked'
  | 'partial'
  | 'failed_using_last_known_good'
  | 'cached';

export type FerryRefreshSourceStatus =
  | 'verified_structure'
  | 'unavailable_or_invalid'
  | 'skipped_permission_required';

/** Outcome for one reviewed source in the fixed official refresh allowlist. */
export interface FerryRefreshSourceResult {
  source_id: string;
  authority: string;
  kind: string;
  url: string;
  permission_status: string;
  status: FerryRefreshSourceStatus;
  checked_at: string;
  http_status: number | null;
  final_url?: string;
  parser_version?: string;
  observed_time_value_count?: number;
  changed_since_previous_check?: boolean | null;
  warning?: string;
  note: string;
}

export interface FerryRefreshReport {
  refresh_id: string;
  status: FerryRefreshStatus;
  started_at: string;
  finished_at: string;
  refresh_scope: 'fixed_official_allowlist';
  source_results: FerryRefreshSourceResult[];
  summary: {
    verified: number;
    failed: number;
    permission_gated: number;
  };
  schedule_applied: boolean;
  last_known_good_active: boolean;
  promotion_requirement: string;
  data_changed: boolean;
  limitations: string;
  cache_age_seconds?: number;
}

/** Atomic result of a user-requested source check and planning-data refresh. */
export interface FetchedFerryRefresh extends Fetched<FerrySchedule[]> {
  timetable: FerryTimetableMetadata;
  ports: PortStatus[];
  refresh: FerryRefreshReport;
}

/** A geocoding result from Nominatim + nearest OSM graph node. */
export interface GeocodedLocation {
  display_name: string;
  type: string;
  lat: number;
  lng: number;
  node_id: number | null;
  snap_distance_m: number;
  snapped_lat: number;
  snapped_lng: number;
  importance: number;
  supported_region?: 'BATAM' | 'SINGAPORE' | null;
}

/** One bucket in a historical hourly profile. */
export interface HourlyBucket {
  hour: number;
  avg_score: number | null;
  sample_count: number;
}

/** One day in a weekly congestion trend. */
export interface DailyTrend {
  date: string;
  avg_score: number;
  sample_count: number;
}

export interface HistoricalSourceMetadata {
  sample_count: number;
  observed: boolean;
}

export interface HistoricalStorageMetadata {
  engine: string;
  durability: 'process_memory' | 'ephemeral_instance_file' | 'persistent_file' | string;
  durable: boolean;
  shared_across_instances: boolean;
  fallback_to_memory: boolean;
}

export interface HistoricalSyntheticSeedMetadata {
  source: string;
  version: number;
  days: number;
  timezone: string;
  generated_for_date: string | null;
  observed: false;
}

/** Provenance for persisted history returned by the backend. */
export interface ApiHistoryMetadata {
  window_days: number;
  /** True only when every sample in the returned window is observed. */
  observed: boolean;
  /** True when at least one observed sample is present, including mixed windows. */
  contains_observed_samples: boolean;
  source_counts: Record<string, number>;
  sources: Record<string, HistoricalSourceMetadata>;
  latest_sample_at: string | null;
  latest_sample_age_seconds: number | null;
  freshness: 'current' | 'recent' | 'stale' | 'empty';
  freshness_basis: string;
  storage: HistoricalStorageMetadata;
  synthetic_seed: HistoricalSyntheticSeedMetadata;
}

/** Provenance for the deterministic browser continuity profile. */
export interface BrowserHistoryMetadata {
  observed: false;
  source: 'browser_modelled_baseline';
  model: 'crossflow_local_forecast_equation_v1';
  methodology: string;
  limitations: string[];
  reference_time: string;
}

export type HistoryMetadata = ApiHistoryMetadata | BrowserHistoryMetadata;

/** Full historical profile for one corridor from /api/historical-congestion. */
export interface HistoricalProfile {
  corridor_id: string;
  hourly_profile: HourlyBucket[];
  weekly_trend: DailyTrend[];
  days_requested: number;
  history_metadata: HistoryMetadata;
}

/** Live traffic data for one corridor segment. */
export interface LiveTrafficSegment {
  corridor_id: string;
  lat: number;
  lng: number;
  congestion_index: number;
  current_speed_kmh: number | null;
  free_flow_speed_kmh: number | null;
  provider_confidence?: number | null;
  source: 'tomtom_live' | 'simulated';
}

export interface ModeledEmissionsPressure {
  index: number;
  queue_pressure_factor: number;
  level: 'LOW' | 'ELEVATED' | 'HIGH';
  metric: 'relative_queue_emissions_pressure';
  unit: 'index_0_100';
  observed: false;
}

export interface EmissionsPressureModel {
  schema_version: 1;
  methodology_version: 'crossflow-zone-pressure-v1';
  formula: string;
  thresholds: {
    ELEVATED: number;
    HIGH: number;
  };
  traffic_input: string;
  source: 'crossflow_congestion_delay_model';
  observed: false;
  aggregate_mass_available: false;
  limitations: string;
}

export interface CongestionZone {
  zone_id: string;
  name: string;
  lat: number;
  lng: number;
  radius_m: number;
  congestion_index: number;
  level: 'SMOOTH' | 'HEAVY' | 'SUPER_CONGESTED';
  color: string;
  avoid_recommended: boolean;
  category?: string;
  corridor_ids?: string[];
  peak_active?: boolean;
  source?: 'modelled_spatial_hotspot';
  observed?: false;
  routing_enabled?: boolean;
  base_score?: number;
  network_criticality?: number;
  demand_exposure?: number;
  /** Relative backend watch tier; separate from the dynamic congestion level. */
  watch_priority?: 'CRITICAL' | 'HEAVY';
  selection_rank?: number;
  selection_score?: number;
  score_breakdown?: Record<string, unknown>;
  selection_breakdown?: Record<string, unknown>;
  modeled_emissions_pressure: ModeledEmissionsPressure;
}

/** Exact modelled spatial conditions evaluated for a returned road route. */
export interface PlanningTrafficSnapshot {
  schema_version: 1;
  effective_at: string;
  weather: 0 | 1 | 2;
  source: 'modelled_spatial_hotspots';
  observed: false;
  applied_to_returned_route: boolean;
  zone_count: number;
  congestion_level_counts: {
    SMOOTH: number;
    HEAVY: number;
    SUPER_CONGESTED: number;
  };
  zones: CongestionZone[];
  emissions_pressure_model: EmissionsPressureModel;
  routing_effect: string;
  limitations: string;
}

export interface TrafficCoverage {
  hotspot_count: number;
  level_counts: {
    SMOOTH: number;
    HEAVY: number;
    SUPER_CONGESTED: number;
  };
  emissions_pressure_level_counts: {
    LOW: number;
    ELEVATED: number;
    HIGH: number;
  };
  emissions_pressure_model: EmissionsPressureModel;
  method: string;
  methodology?: Record<string, unknown>;
  catalog_version?: string;
}

/** Full live traffic response from /api/live-traffic. */
export interface LiveTrafficData {
  segments: LiveTrafficSegment[];
  zones: CongestionZone[];
  coverage?: TrafficCoverage;
  overall_source: string;
  tomtom_key_configured: boolean;
}

/** A free-form location chosen from geocode results or map click. */
export interface FreeLocation {
  lat: number;
  lng: number;
  display_name: string;
  node_id?: number | null;
  snap_distance_m?: number;
  snapped_lat?: number;
  snapped_lng?: number;
  supported_region?: 'BATAM' | 'SINGAPORE' | null;
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

/** Supabase credentials held by the browser. Never sent to the CrossFlow API except as a Bearer token. */
export interface StoredSession {
  accessToken: string;
  refreshToken: string;
  expiresAtMs: number;
}

/** `/api/auth/status` — whether signing in is possible at all. */
export interface AuthStatus {
  mode: string;
  enabled: boolean;
  configured: boolean;
  project_origin: string | null;
  sign_in: string;
  notes: string;
}

/** `/api/auth/session` — the identity the server resolved, not one the client claimed. */
export interface AuthSession {
  user_id: string;
  role: string;
  display_name: string | null;
  expires_at: number | null;
  role_source: string;
}
