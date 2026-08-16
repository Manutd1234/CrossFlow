import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  DataSource,
  FreeLocation,
  RoadRouteOption,
  RouteBenchmarkResult,
  RouteLocation,
  RouteOptimizationResult,
  RoutePreference,
  VehicleType,
} from '../../types';
import {
  ApiRequestError,
  googleRouteBenchmarkEnabled,
  requestPersistedRoute,
  requestFreeRouteOptimization,
  requestMultiStopRouteOptimization,
  requestRouteBenchmark,
  requestRouteOptimization,
} from '../../services/api';
import { Anchor, ArrowDownUp, Bike, BusFront, Car, ChevronDown, CircleCheck, CircleDot, CircleSmall, Clock, CloudRain, CloudSun, Coffee, CornerUpLeft, CornerUpRight, Flag, Fuel, Globe, Info, KeyRound, Leaf, Lightbulb, LoaderCircle, MapPin, MoveUp, Navigation, Package, Plus, Redo, RotateCw, Route, ShieldCheck, Ship, Sparkles, TriangleAlert, Truck, Users, X, Zap, type LucideIcon } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';

import {
  ferryBadgeClass, formatScheduleVerifiedAt, formatTime, prettyStatus,
  relativeDeparture,
} from '../../utils/format';
import { RoutePreviewMap } from './RoutePreviewMap';
import { RouteRunHistoryPanel } from './RouteRunHistoryPanel';
import { LocationSearch } from './LocationSearch';
import { WorldMapPickerModal } from './WorldMapPickerModal';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';
import {
  VEHICLE_CATALOG,
  vehicleProfile,
  type VehicleIconKey,
} from '../../data/vehicleCatalog';


interface RouteOptimizerProps {
  locations: RouteLocation[];
  originId: string;
  setOriginId: (id: string) => void;
  destinationId: string;
  setDestinationId: (id: string) => void;
  result: RouteOptimizationResult | null;
  setResult: (r: RouteOptimizationResult | null) => void;
  resultSource: DataSource;
  setResultSource: (s: DataSource) => void;
  vehicleType: VehicleType;
  setVehicleType: (v: VehicleType) => void;
  weather: number;
  setWeather: (w: number) => void;
  hour: number;
  setHour: (h: number) => void;
  driverAccess?: boolean;
  accessToken?: string | null;
}

const WEATHER_OPTIONS = [
  { id: 0, label: 'Clear', icon: CloudSun },
  { id: 1, label: 'Rain', icon: CloudRain },
  { id: 2, label: 'Heavy storm', icon: TriangleAlert },
] as const;

function twelveHourValue(hour: number): number {
  return hour % 12 || 12;
}

function twentyFourHourValue(hour: number, period: 'AM' | 'PM'): number {
  return (hour % 12) + (period === 'PM' ? 12 : 0);
}

function nextSelectedTimeIso(hour: number, minute: number): string {
  const now = new Date();
  const selected = new Date(now);
  selected.setHours(hour, minute, 0, 0);
  if (selected.getTime() < now.getTime()) selected.setDate(selected.getDate() + 1);
  return selected.toISOString();
}

const VEHICLE_ICONS: Record<VehicleIconKey, LucideIcon> = {
  car: Car,
  electric: Zap,
  motorcycle: Bike,
  van: Package,
  minibus: Users,
  bus: BusFront,
  truck: Truck,
};
const VEHICLE_GROUPS = ['Passenger', 'Delivery', 'Public transport', 'Freight'] as const;
const VEHICLE_OPTIONS_BY_GROUP = VEHICLE_GROUPS.map((group) => ({
  group,
  profiles: VEHICLE_CATALOG.filter((profile) => profile.group === group),
}));

const PRIMARY_ROUTE_ID = 'primary';
const MAX_INTERMEDIATE_STOPS = 3;
type RouteResultTab = 'summary' | 'journey' | 'directions' | 'connections';

interface SelectableRoadRoute extends RoadRouteOption {
  isPrimary: boolean;
}

function routeOptionLabel(name: string): string {
  return name.replace(/\broad route\b/gi, '').replace(/\s+/g, ' ').trim();
}

function selectableRoutes(result: RouteOptimizationResult): SelectableRoadRoute[] {
  const osmSources = new Set([
    'openstreetmap',
    'bundled_openstreetmap',
    'bundled_client_openstreetmap',
  ]);
  const primaryDescription = result.avoided_congested_zones?.length
    ? 'Road route adjusted around the reported congestion zones.'
    : osmSources.has(result.route_data_source ?? '')
      ? 'Road route calculated on the OpenStreetMap network.'
      : 'Road route returned by the configured directions provider.';
  const primary: SelectableRoadRoute = {
    id: PRIMARY_ROUTE_ID,
    name: 'Recommended',
    description: primaryDescription,
    route_geometry: result.route_geometry ?? [],
    distance_km: result.corridor.distance_km,
    estimated_travel_time_mins: result.estimated_travel_time_mins,
    total_eta_mins: result.total_eta_mins,
    co2_emissions_kg: result.co2_emissions_kg,
    co2_saved_kg: result.co2_saved_kg,
    route_type: result.route_type,
    route_data_source: result.route_data_source,
    avoided_congested_zones: result.avoided_congested_zones,
    congestion_cost: result.generalized_cost_mins,
    objective_cost_s: result.objective_cost_s,
    route_preference: result.route_preference,
    route_preference_profile: result.route_preference_profile,
    routing_cost_breakdown: result.routing_cost_breakdown,
    routing_model: result.routing_model,
    local_road_distance_km: result.local_road_distance_km,
    local_road_segments: result.local_road_segments,
    local_road_audit: result.local_road_audit,
    navigation: result.navigation,
    next_matching_ferries: result.next_matching_ferries,
    route_legs: result.route_legs,
    road_distance_km: result.road_distance_km,
    ferry_distance_km: result.ferry_distance_km,
    emissions_scope: result.emissions_scope,
    isPrimary: true,
  };
  const seenIds = new Set([PRIMARY_ROUTE_ID]);
  const alternatives = (result.alternative_routes ?? []).flatMap((route) => {
    if (!route.id || seenIds.has(route.id) || route.route_geometry.length < 2) return [];
    seenIds.add(route.id);
    return [{
      ...route,
      route_data_source: route.route_data_source ?? result.route_data_source,
      isPrimary: false,
    }];
  });
  return [primary, ...alternatives];
}

function formatManeuverDistance(distanceM: number): string {
  if (!Number.isFinite(distanceM) || distanceM <= 0) return '';
  if (distanceM < 1000) return `${Math.round(distanceM)} m`;
  return `${(distanceM / 1000).toFixed(1)} km`;
}

export const RouteOptimizer: React.FC<RouteOptimizerProps> = ({
  locations, originId, setOriginId, destinationId, setDestinationId,
  result, setResult, resultSource, setResultSource,
  vehicleType, setVehicleType,
  weather, setWeather, hour, setHour,
  driverAccess = false, accessToken = null,
}) => {
  const [loading, setLoading] = useState<boolean>(false);
  const [routeError, setRouteError] = useState<string | null>(null);
  const [selectedRouteId, setSelectedRouteId] = useState(PRIMARY_ROUTE_ID);
  const [routeBenchmark, setRouteBenchmark] = useState<RouteBenchmarkResult | null>(null);
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null);
  const [benchmarkLoading, setBenchmarkLoading] = useState(false);
  const [activeResultTab, setActiveResultTab] = useState<RouteResultTab>('summary');
  const [vehicleMenuOpen, setVehicleMenuOpen] = useState(false);
  const [timeMode, setTimeMode] = useState<'departure' | 'arrival'>('departure');
  const [departureMinute, setDepartureMinute] = useState(() => new Date().getMinutes());
  const [arrivalHour, setArrivalHour] = useState(() => (new Date().getHours() + 1) % 24);
  const [arrivalMinute, setArrivalMinute] = useState(() => new Date().getMinutes());
  const [routeCode, setRouteCode] = useState('');
  const optimizationRequestRef = useRef(0);
  const benchmarkRequestRef = useRef(0);
  const vehicleSelectorRef = useRef<HTMLDivElement>(null);
  const vehicleTriggerRef = useRef<HTMLButtonElement>(null);
  const vehicleMenuRef = useRef<HTMLDivElement>(null);
  const vehicleOptionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // The preference selector was removed from the UI; every plan now uses the
  // balanced objective, which the request payloads below still send explicitly.
  const routePreference: RoutePreference = 'BALANCED';
  const selectedVehicleProfile = vehicleProfile(vehicleType);
  const SelectedVehicleIcon = VEHICLE_ICONS[selectedVehicleProfile.icon];

  useEffect(() => {
    if (!vehicleMenuOpen) return;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!vehicleSelectorRef.current?.contains(event.target as Node)) {
        setVehicleMenuOpen(false);
      }
    };

    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, [vehicleMenuOpen]);

  useEffect(() => {
    if (!vehicleMenuOpen) return;
    const selectedIndex = VEHICLE_CATALOG.findIndex((profile) => profile.id === vehicleType);
    const selectedOption = vehicleOptionRefs.current[selectedIndex];
    const menu = vehicleMenuRef.current;
    selectedOption?.focus({ preventScroll: true });
    if (selectedOption && menu) {
      const optionTop = selectedOption.offsetTop;
      const optionBottom = optionTop + selectedOption.offsetHeight;
      if (optionTop < menu.scrollTop) menu.scrollTop = optionTop;
      if (optionBottom > menu.scrollTop + menu.clientHeight) {
        menu.scrollTop = optionBottom - menu.clientHeight;
      }
    }
  }, [vehicleMenuOpen, vehicleType]);

  // Mode: 'named' uses the 13-location whitelist; 'free' uses geocode search.
  const resultIsFreeRoute = result?.corridor.id.startsWith('free:') ?? false;
  const [mode, setMode] = useState<'named' | 'free'>(
    result ? (resultIsFreeRoute ? 'free' : 'named') : 'free',
  );
  const [pickerTarget, setPickerTarget] = useState<'origin' | 'dest' | number | null>(null);
  const [waypoints, setWaypoints] = useState<Array<FreeLocation | null>>([]);

  // Named-mode derived state (unchanged from before)
  const originNamed = locations.find(l => l.id === originId) || locations[0];
  const destNamed = locations.find(l => l.id === destinationId) || locations[1];
  const sameLocationNamed = originId === destinationId;

  const [freeOrigin, setFreeOrigin] = useState<FreeLocation | null>(() => ({
    lat: resultIsFreeRoute ? (result?.requested_origin?.lat ?? 1.0605) : (originNamed?.lat ?? 1.0605),
    lng: resultIsFreeRoute ? (result?.requested_origin?.lng ?? 104.0303) : (originNamed?.lng ?? 104.0303),
    display_name: resultIsFreeRoute
      ? (result?.requested_origin?.display_name ?? result?.requested_origin?.name ?? 'Selected origin')
      : (originNamed?.name ?? 'Batamindo Industrial Park'),
  }));
  const [freeDest, setFreeDest] = useState<FreeLocation | null>(() => ({
    lat: resultIsFreeRoute ? (result?.requested_destination?.lat ?? 1.1318) : (destNamed?.lat ?? 1.1318),
    lng: resultIsFreeRoute ? (result?.requested_destination?.lng ?? 104.0554) : (destNamed?.lng ?? 104.0554),
    display_name: resultIsFreeRoute
      ? (result?.requested_destination?.display_name ?? result?.requested_destination?.name ?? 'Selected destination')
      : (destNamed?.name ?? 'Batam Centre Ferry Terminal'),
  }));
  // Free-mode derived state
  const sameLocationFree = !!(
    freeOrigin && freeDest &&
    Math.abs(freeOrigin.lat - freeDest.lat) < 0.0001 &&
    Math.abs(freeOrigin.lng - freeDest.lng) < 0.0001
  );
  const canOptimizeFree = !!(freeOrigin && freeDest && !sameLocationFree);
  const isNamedMode = mode === 'named';
  const canOptimizeEndpoints = isNamedMode
    ? !sameLocationNamed
    : canOptimizeFree;
  const canOptimize = canOptimizeEndpoints
    && waypoints.every((waypoint) => waypoint !== null);

  const clearRouteBenchmark = useCallback(() => {
    benchmarkRequestRef.current += 1;
    setRouteBenchmark(null);
    setBenchmarkError(null);
    setBenchmarkLoading(false);
  }, []);

  const invalidateResult = () => {
    optimizationRequestRef.current += 1;
    setLoading(false);
    setRouteError(null);
    setSelectedRouteId(PRIMARY_ROUTE_ID);
    clearRouteBenchmark();
    setActiveResultTab('summary');
    setResult(null);
  };

  const closePicker = useCallback(() => setPickerTarget(null), []);

  const swapLocations = () => {
    if (isNamedMode) {
      setOriginId(destinationId);
      setDestinationId(originId);
    } else {
      const tmp = freeOrigin;
      setFreeOrigin(freeDest);
      setFreeDest(tmp);
    }
    setWaypoints((current) => [...current].reverse());
    invalidateResult();
  };

  const addWaypoint = () => {
    setWaypoints((current) => (
      current.length < MAX_INTERMEDIATE_STOPS ? [...current, null] : current
    ));
    invalidateResult();
  };

  const handleOptimize = async () => {
    if (!canOptimize) return;
    clearRouteBenchmark();
    setActiveResultTab('summary');
    const requestId = ++optimizationRequestRef.current;
    setSelectedRouteId(PRIMARY_ROUTE_ID);
    setLoading(true);
    setRouteError(null);
    try {
      const schedule = timeMode === 'departure'
        ? { departure_at: nextSelectedTimeIso(hour, departureMinute) }
        : { arrive_by: nextSelectedTimeIso(arrivalHour, arrivalMinute) };
      const selectedRequestHour = timeMode === 'departure'
        ? hour
        : Math.floor((arrivalHour * 60 + arrivalMinute - (result?.total_eta_mins ?? 60) + 1440) % 1440 / 60);
      let res;
      let requestedOrigin: FreeLocation;
      let requestedDestination: FreeLocation;
      if (waypoints.length > 0) {
        if ((!isNamedMode && (!freeOrigin || !freeDest)) || waypoints.some((point) => !point)) return;
        requestedOrigin = isNamedMode
          ? { lat: originNamed.lat, lng: originNamed.lng, display_name: originNamed.name }
          : freeOrigin!;
        requestedDestination = isNamedMode
          ? { lat: destNamed.lat, lng: destNamed.lng, display_name: destNamed.name }
          : freeDest!;
        res = await requestMultiStopRouteOptimization(
          [requestedOrigin, ...waypoints.filter((point): point is FreeLocation => point !== null), requestedDestination],
          vehicleType,
          schedule.departure_at
            ? { departureAt: schedule.departure_at }
            : { arriveBy: schedule.arrive_by },
          weather,
          routePreference,
        );
      } else if (isNamedMode) {
        requestedOrigin = {
          lat: originNamed.lat,
          lng: originNamed.lng,
          display_name: originNamed.name,
        };
        requestedDestination = {
          lat: destNamed.lat,
          lng: destNamed.lng,
          display_name: destNamed.name,
        };
        res = await requestRouteOptimization(
          originId,
          destinationId,
          vehicleType,
          selectedRequestHour,
          weather,
          routePreference,
          schedule,
        );
      } else {
        if (!freeOrigin || !freeDest) return;
        requestedOrigin = freeOrigin;
        requestedDestination = freeDest;
        res = await requestFreeRouteOptimization(
          freeOrigin,
          freeDest,
          vehicleType,
          selectedRequestHour,
          weather,
          routePreference,
          schedule,
        );
      }
      if (requestId !== optimizationRequestRef.current) return;
      const backendOrigin = res.data.requested_origin;
      const backendDestination = res.data.requested_destination;
      setResult({
        ...res.data,
        requested_origin: {
          lat: backendOrigin?.lat ?? requestedOrigin.lat,
          lng: backendOrigin?.lng ?? requestedOrigin.lng,
          display_name: backendOrigin?.display_name ?? backendOrigin?.name ?? requestedOrigin.display_name,
          node_id: backendOrigin?.node_id ?? requestedOrigin.node_id,
          region: backendOrigin?.region ?? requestedOrigin.supported_region ?? undefined,
        },
        requested_destination: {
          lat: backendDestination?.lat ?? requestedDestination.lat,
          lng: backendDestination?.lng ?? requestedDestination.lng,
          display_name: backendDestination?.display_name ?? backendDestination?.name ?? requestedDestination.display_name,
          node_id: backendDestination?.node_id ?? requestedDestination.node_id,
          region: backendDestination?.region ?? requestedDestination.supported_region ?? undefined,
        },
      });
      setResultSource(res.source);
    } catch (err) {
      console.error('Optimization error:', err);
      if (requestId !== optimizationRequestRef.current) return;
      setRouteError(err instanceof ApiRequestError
        ? err.message
        : 'The journey could not be calculated. Choose points within Singapore or Batam and try again.');
      setResult(null);
    } finally {
      if (requestId === optimizationRequestRef.current) setLoading(false);
    }
  };

  // Shared by the driver code box and the history list, so a run opened from
  // either path lands in the result pane the same way.
  const handleAssignedRouteLookup = () => loadRouteByCode(routeCode);

  const loadRouteByCode = async (code: string) => {
    const normalizedCode = code.trim().toUpperCase();
    if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(normalizedCode)) return;
    clearRouteBenchmark();
    setActiveResultTab('summary');
    setSelectedRouteId(PRIMARY_ROUTE_ID);
    setLoading(true);
    setRouteError(null);
    try {
      const response = await requestPersistedRoute(normalizedCode, accessToken);
      setResult(response.data);
      setResultSource(response.source);
    } catch (error) {
      setResult(null);
      setRouteError(error instanceof ApiRequestError
        ? error.message
        : 'The assigned route could not be loaded.');
    } finally {
      setLoading(false);
    }
  };

  const handleRouteBenchmark = async () => {
    const benchmarkOrigin = result?.requested_origin;
    const benchmarkDestination = result?.requested_destination;
    if (!googleRouteBenchmarkEnabled() || !benchmarkOrigin || !benchmarkDestination) return;
    const requestId = ++benchmarkRequestRef.current;
    setRouteBenchmark(null);
    setBenchmarkError(null);
    setBenchmarkLoading(true);
    try {
      const benchmark = await requestRouteBenchmark(
        benchmarkOrigin,
        benchmarkDestination,
        routePreference,
      );
      if (requestId !== benchmarkRequestRef.current) return;
      setRouteBenchmark(benchmark);
    } catch (error) {
      if (requestId !== benchmarkRequestRef.current) return;
      setBenchmarkError(error instanceof ApiRequestError
        ? error.message
        : 'Route benchmark is temporarily unavailable.');
    } finally {
      if (requestId === benchmarkRequestRef.current) setBenchmarkLoading(false);
    }
  };

  const resultOrigin = result?.requested_origin;
  const resultDestination = result?.requested_destination;
  const resultOriginName = resultOrigin?.display_name ?? resultOrigin?.name ?? 'Origin';
  const resultDestinationName = resultDestination?.display_name ?? resultDestination?.name ?? 'Destination';
  const originForPreview: RouteLocation | undefined = resultOrigin
    ? { id: 'result-origin', name: resultOriginName, category: 'Route endpoint', lat: resultOrigin.lat, lng: resultOrigin.lng }
    : undefined;
  const destForPreview: RouteLocation | undefined = resultDestination
    ? { id: 'result-destination', name: resultDestinationName, category: 'Route endpoint', lat: resultDestination.lat, lng: resultDestination.lng }
    : undefined;
  const routeOptions = useMemo(() => result ? selectableRoutes(result) : [], [result]);
  const selectedRoute = routeOptions.find(route => route.id === selectedRouteId) ?? routeOptions[0];
  const selectedLegs = selectedRoute?.route_legs ?? [];
  const isMultimodal = selectedLegs.some(leg => leg.mode === 'FERRY');
  const trafficNotModelled = result?.congestion_prediction.status === 'NOT_MODELLED';
  const selectedNavigation = selectedRoute?.navigation;
  const selectedDistanceKm = selectedRoute?.distance_km ?? result?.corridor.distance_km ?? 0;
  const selectedTravelMins = selectedRoute?.estimated_travel_time_mins
    ?? result?.estimated_travel_time_mins
    ?? 0;
  const selectedTotalEtaMins = selectedRoute?.total_eta_mins
    ?? (result ? selectedTravelMins + result.customs_buffer_mins : 0);
  const selectedCo2EmissionsKg = selectedRoute?.co2_emissions_kg ?? result?.co2_emissions_kg ?? 0;
  const selectedFerries = selectedRoute?.isPrimary
    ? (result?.next_matching_ferries ?? [])
    : (selectedRoute?.next_matching_ferries ?? []);
  const selectedAvoidedZones = selectedRoute?.avoided_congested_zones
    ?? result?.avoided_congested_zones
    ?? [];
  const selectedCostBreakdown = selectedRoute?.routing_cost_breakdown;
  const selectedLocalRoadAudit = selectedRoute?.local_road_audit;
  const selectedLocalRoadDistanceKm = selectedRoute?.local_road_distance_km ?? 0;
  const selectedLocalRoadSegments = selectedRoute?.local_road_segments ?? [];
  const comparisonBaseProfile = vehicleProfile(result?.vehicle_type ?? vehicleType);
  const comparisonDistanceScale = result && result.corridor.distance_km > 0
    ? selectedDistanceKm / result.corridor.distance_km
    : 1;
  const comparisonBaseMins = selectedCostBreakdown?.free_flow_mins
    ?? ((result?.corridor.base_time_mins ?? 0) * comparisonDistanceScale);
  const comparisonCongestionMins = selectedCostBreakdown?.congestion_delay_mins
    ?? result?.congestion_prediction.estimated_delay_mins
    ?? 0;
  const comparisonWeatherMins = selectedCostBreakdown?.weather_delay_mins ?? 0;
  const comparisonManeuverMins = selectedCostBreakdown?.maneuver_delay_mins
    ?? Math.max(0, selectedTravelMins - comparisonBaseMins - comparisonCongestionMins);
  const comparisonManeuverWeight = comparisonBaseProfile.turnPenaltySeconds
    + comparisonBaseProfile.shortManeuverPenaltySeconds
    + comparisonBaseProfile.signalDelaySeconds;
  const handleSelectRoute = useCallback((routeId: string) => {
    setSelectedRouteId(routeId);
  }, []);

  return (
    <div className={`app-screen-layout route-planner-layout${driverAccess ? ' route-planner-layout--driver' : ''}`}>
      {!driverAccess ? <div className="workspace-subtabs__rail">
        <div role="group" aria-label="Location selection mode" className="workspace-subtabs__tablist route-location-mode-tabs">
          <button
            type="button"
            className="workspace-subtabs__tab"
            aria-pressed={mode === 'free'}
            onClick={() => {
              if (mode === 'free') return;
              setMode('free');
              setPickerTarget(null);
              invalidateResult();
            }}
          >
            <span className="workspace-subtabs__label route-location-mode-label">
              <Globe size={ICON_SIZE.medium} aria-hidden="true" /> Search
            </span>
          </button>
          <button
            type="button"
            className="workspace-subtabs__tab"
            aria-pressed={mode === 'named'}
            onClick={() => {
              if (mode === 'named') return;
              setMode('named');
              setPickerTarget(null);
              invalidateResult();
            }}
          >
            <span className="workspace-subtabs__label route-location-mode-label">
              <MapPin size={ICON_SIZE.medium} aria-hidden="true" /> Saved
            </span>
          </button>
        </div>
      </div> : null}

      <form
        className="glass-panel route-planner-sidebar"
        aria-label={driverAccess ? 'Assigned route lookup' : 'Route planning controls'}
        onSubmit={(event) => {
          event.preventDefault();
          void (driverAccess ? handleAssignedRouteLookup() : handleOptimize());
        }}
      >
        {driverAccess ? (
          <div className="route-sidebar-controls route-code-lookup">
            <div className="route-code-lookup__heading">
              <div className="route-code-lookup__title-row">
                <KeyRound size={ICON_SIZE.large} aria-hidden="true" />
                <h3>Assigned route</h3>
              </div>
              <p>Enter the code supplied by dispatch to load your journey.</p>
            </div>
            <label className="route-code-lookup__field" htmlFor="driver-route-code">
              <span>7-character route code</span>
              <input
                id="driver-route-code"
                type="text"
                inputMode="text"
                autoComplete="off"
                maxLength={7}
                pattern="[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}"
                placeholder="e.g. 4KF9X2P"
                value={routeCode}
                onChange={(event) => setRouteCode(
                  event.target.value.toUpperCase().replace(/[^23456789ABCDEFGHJKLMNPQRSTUVWXYZ]/g, '').slice(0, 7),
                )}
              />
            </label>
          </div>
        ) : (
        <div className="route-sidebar-controls">
        {/* Free search or ID-backed named location controls. */}
        <fieldset className="route-endpoint-section">
          <legend className="route-planner-fieldset-legend">Route</legend>
          <div className="route-location-picker">
            <div className="route-endpoint-rail" aria-hidden="true">
              <span className="route-endpoint-marker-row">
                <CircleSmall className="route-endpoint-icon route-endpoint-icon-origin" size={ICON_SIZE.medium} />
              </span>
              {waypoints.map((_, index) => (
                <span className="route-endpoint-marker-row" key={`waypoint-marker-${index}`}>
                  <CircleSmall className="route-endpoint-icon route-endpoint-icon-waypoint" size={ICON_SIZE.medium} />
                </span>
              ))}
              <span className="route-endpoint-marker-row">
                <MapPin className="route-endpoint-icon route-endpoint-icon-destination" size={ICON_SIZE.medium} />
              </span>
            </div>

            <div className="route-endpoint-fields">
              {isNamedMode ? (
                <LocationSearch
                  id="route-origin-named"
                  label="From"
                  value={originNamed}
                  onChange={() => undefined}
                  onNamedLocationSelect={(location) => {
                    setOriginId(location.id);
                    invalidateResult();
                  }}
                  namedLocations={locations}
                  markerColor="cyan"
                  showMapPickerButton={false}
                  showSearchButton={false}
                  showHelpText={false}
                  compactLayout
                  savedPlacesOnly
                  ariaDescribedBy={sameLocationNamed ? 'route-endpoint-validation' : undefined}
                  invalid={sameLocationNamed}
                />
              ) : (
                <LocationSearch
                  id="route-origin"
                  label="From"
                  value={freeOrigin}
                  onChange={loc => { setFreeOrigin(loc); invalidateResult(); }}
                  namedLocations={locations}
                  markerColor="cyan"
                  showMapPickerButton={false}
                  showSearchButton={false}
                  showHelpText={false}
                  compactLayout
                  placeholder="e.g. Raffles Place, HarbourFront, Nagoya Hill…"
                  onOpenMapPicker={() => setPickerTarget('origin')}
                />
              )}

              {waypoints.map((waypoint, index) => (
                <div className="route-waypoint-row" key={`waypoint-${index}`}>
                  <LocationSearch
                    id={`route-waypoint-${index}`}
                    label={`Stop ${index + 1}`}
                    value={waypoint}
                    onChange={(location) => {
                      setWaypoints((current) => current.map((item, itemIndex) => (
                        itemIndex === index ? location : item
                      )));
                      invalidateResult();
                    }}
                    onNamedLocationSelect={isNamedMode ? (location) => {
                      setWaypoints((current) => current.map((item, itemIndex) => (
                        itemIndex === index
                          ? { lat: location.lat, lng: location.lng, display_name: location.name }
                          : item
                      )));
                      invalidateResult();
                    } : undefined}
                    namedLocations={locations}
                    markerColor="cyan"
                    showMapPickerButton={false}
                    showSearchButton={false}
                    showHelpText={false}
                    compactLayout
                    savedPlacesOnly={isNamedMode}
                    placeholder="Add an intermediate stop"
                    onOpenMapPicker={() => setPickerTarget(index)}
                  />
                </div>
              ))}

              {isNamedMode ? (
                <LocationSearch
                  id="route-destination-named"
                  label="To"
                  value={destNamed}
                  onChange={() => undefined}
                  onNamedLocationSelect={(location) => {
                    setDestinationId(location.id);
                    invalidateResult();
                  }}
                  namedLocations={locations}
                  markerColor="rose"
                  showMapPickerButton={false}
                  showSearchButton={false}
                  showHelpText={false}
                  compactLayout
                  savedPlacesOnly
                  ariaDescribedBy={sameLocationNamed ? 'route-endpoint-validation' : undefined}
                  invalid={sameLocationNamed}
                />
              ) : (
                <LocationSearch
                  id="route-destination"
                  label="To"
                  value={freeDest}
                  onChange={loc => { setFreeDest(loc); invalidateResult(); }}
                  namedLocations={locations}
                  markerColor="rose"
                  showMapPickerButton={false}
                  showSearchButton={false}
                  showHelpText={false}
                  compactLayout
                  placeholder="e.g. Batam Centre, Changi Airport, Tuas Port…"
                  onOpenMapPicker={() => setPickerTarget('dest')}
                />
              )}
            </div>

            <div className="route-endpoint-actions">
              <button
                type="button"
                className="route-swap-button"
                onClick={swapLocations}
                aria-label="Swap origin and destination"
                title="Swap origin and destination"
              >
                <ArrowDownUp size={ICON_SIZE.large} aria-hidden="true" />
              </button>
              {waypoints.map((_, index) => (
                <button
                  type="button"
                  className="route-waypoint-remove"
                  key={`remove-waypoint-${index}`}
                  aria-label={`Remove stop ${index + 1}`}
                  onClick={() => {
                    setWaypoints((current) => current.filter((__, itemIndex) => itemIndex !== index));
                    invalidateResult();
                  }}
                >
                  <X size={ICON_SIZE.large} aria-hidden="true" />
                </button>
              ))}
              <button
                type="button"
                className="route-add-stop-button"
                onClick={addWaypoint}
                disabled={waypoints.length >= MAX_INTERMEDIATE_STOPS}
                aria-label="Add an intermediate stop"
                title={waypoints.length >= MAX_INTERMEDIATE_STOPS
                  ? 'Routes support up to 3 intermediate stops (5 locations total)'
                  : 'Add an intermediate stop'}
              >
                <Plus size={ICON_SIZE.large} aria-hidden="true" />
              </button>
            </div>

            {(isNamedMode ? sameLocationNamed : sameLocationFree) && (
              <p id="route-endpoint-validation" className="route-validation" role="alert" >
                Choose a different destination to calculate a route.
              </p>
            )}
          </div>
        </fieldset>


        <fieldset className="route-planner-fieldset">
          <legend className="route-planner-fieldset-legend">
            Road vehicle
          </legend>
          <div ref={vehicleSelectorRef} className="route-vehicle-selector">
            <button
              ref={vehicleTriggerRef}
              type="button"
              id="route-vehicle-type"
              className="ui-sand-interactive route-vehicle-trigger"
              aria-haspopup="listbox"
              aria-expanded={vehicleMenuOpen}
              aria-controls="route-vehicle-menu"
              onClick={() => setVehicleMenuOpen((open) => !open)}
              onKeyDown={(event) => {
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  event.preventDefault();
                  setVehicleMenuOpen(true);
                }
              }}
            >
              <SelectedVehicleIcon className="route-vehicle-icon" size={ICON_SIZE.large} aria-hidden="true" />
              <span className="route-vehicle-copy">
                <strong>{selectedVehicleProfile.label}</strong>
                <span id="route-vehicle-assumption">
                  {selectedVehicleProfile.routingAssumption}
                </span>
              </span>
              <ChevronDown className="route-vehicle-chevron" size={ICON_SIZE.large} aria-hidden="true" />
            </button>

            {vehicleMenuOpen ? (
              <div
                id="route-vehicle-menu"
                className="route-vehicle-menu"
                role="listbox"
                aria-label="Vehicle routing profile"
                onKeyDown={(event) => {
                  const currentIndex = vehicleOptionRefs.current.findIndex(
                    (option) => option === document.activeElement,
                  );
                  let nextIndex = currentIndex;
                  if (event.key === 'ArrowDown') nextIndex = Math.min(currentIndex + 1, VEHICLE_CATALOG.length - 1);
                  if (event.key === 'ArrowUp') nextIndex = Math.max(currentIndex - 1, 0);
                  if (event.key === 'Home') nextIndex = 0;
                  if (event.key === 'End') nextIndex = VEHICLE_CATALOG.length - 1;
                  if (nextIndex !== currentIndex) {
                    event.preventDefault();
                    vehicleOptionRefs.current[nextIndex]?.focus();
                  }
                  if (event.key === 'Escape') {
                    event.preventDefault();
                    setVehicleMenuOpen(false);
                    vehicleTriggerRef.current?.focus();
                  }
                  if (event.key === 'Tab') setVehicleMenuOpen(false);
                }}
              >
                <div ref={vehicleMenuRef} className="route-vehicle-option-list" role="presentation">
                  {VEHICLE_OPTIONS_BY_GROUP.map(({ group, profiles }) => (
                    <div key={group} className="route-vehicle-group" role="group" aria-label={group}>
                      <div className="route-vehicle-group-title">{group}</div>
                      {profiles.map((profile) => {
                        const VehicleIcon = VEHICLE_ICONS[profile.icon];
                        const optionIndex = VEHICLE_CATALOG.findIndex(({ id }) => id === profile.id);
                        const isSelected = profile.id === vehicleType;
                        return (
                          <button
                            key={profile.id}
                            ref={(element) => { vehicleOptionRefs.current[optionIndex] = element; }}
                            type="button"
                            id={`route-vehicle-option-${profile.id.toLowerCase()}`}
                            className="route-vehicle-option"
                            role="option"
                            aria-selected={isSelected}
                            onClick={() => {
                              setVehicleMenuOpen(false);
                              if (!isSelected) {
                                setVehicleType(profile.id);
                                invalidateResult();
                              }
                              vehicleTriggerRef.current?.focus();
                            }}
                          >
                            <VehicleIcon className="route-vehicle-option-icon" size={ICON_SIZE.large} aria-hidden="true" />
                            <span className="route-vehicle-option-copy">
                              <strong>{profile.label}</strong>
                            </span>
                            {isSelected ? <CircleCheck size={ICON_SIZE.medium} aria-hidden="true" /> : null}
                          </button>
                        );
                      })}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </fieldset>


        <fieldset className="route-planner-fieldset">
          <legend className="route-planner-fieldset-legend">
            Weather & Time
          </legend>
          <div className="route-weather-options">
            {WEATHER_OPTIONS.map((condition) => {
              const WeatherIcon = condition.icon;
              const isSelected = weather === condition.id;
              return (
                <button
                  type="button"
                  key={condition.id}
                  className="ui-button-choice ui-sand-interactive"
                  onClick={() => {
                    setWeather(condition.id);
                    invalidateResult();
                  }}
                  aria-pressed={isSelected}
                >
                  <span className="route-choice-option-heading">
                    <WeatherIcon size={ICON_SIZE.medium} aria-hidden="true" />
                    <strong className="route-choice-option-label">{condition.label}</strong>
                  </span>
                </button>
              );
            })}
          </div>
        </fieldset>

        <div className="route-time-card-grid" role="radiogroup" aria-label="Journey time preference">
          {([
            { id: 'departure' as const, label: 'Departure time', selectedHour: hour, selectedMinute: departureMinute },
            { id: 'arrival' as const, label: 'Arrival time', selectedHour: arrivalHour, selectedMinute: arrivalMinute },
          ]).map((option) => {
            const selected = timeMode === option.id;
            const selectMode = () => {
              setTimeMode(option.id);
              invalidateResult();
            };
            const setHourValue = (value: number) => {
              if (!Number.isInteger(value) || value < 1 || value > 12) return;
              const period = option.selectedHour >= 12 ? 'PM' : 'AM';
              const next = twentyFourHourValue(value, period);
              if (option.id === 'departure') setHour(next);
              else setArrivalHour(next);
              invalidateResult();
            };
            const setMinuteValue = (value: number) => {
              if (!Number.isInteger(value) || value < 0 || value > 59) return;
              if (option.id === 'departure') setDepartureMinute(value);
              else setArrivalMinute(value);
              invalidateResult();
            };
            return (
              <div
                key={option.id}
                className="ui-sand-interactive route-time-card"
                data-selected={selected}
                role="radio"
                aria-checked={selected}
                tabIndex={0}
                onClick={selectMode}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    selectMode();
                  }
                }}
              >
                <span className="route-time-card-label">{option.label}</span>
                <div className="route-time-selectors">
                  <label>
                    <input
                      type="number"
                      min="1"
                      max="12"
                      step="1"
                      inputMode="numeric"
                      value={twelveHourValue(option.selectedHour)}
                      aria-label={`${option.label} hour`}
                      onChange={(event) => {
                        selectMode();
                        setHourValue(Number(event.target.value));
                      }}
                      onPointerDown={(event) => { delete event.currentTarget.dataset.keyboardFocus; }}
                      onKeyDown={(event) => { event.currentTarget.dataset.keyboardFocus = 'true'; }}
                      onBlur={(event) => { delete event.currentTarget.dataset.keyboardFocus; }}
                      onWheel={(event) => {
                        if (document.activeElement !== event.currentTarget) return;
                        event.preventDefault();
                        const current = twelveHourValue(option.selectedHour);
                        setHourValue(event.deltaY < 0 ? (current % 12) + 1 : ((current + 10) % 12) + 1);
                      }}
                    />
                  </label>
                  <span className="route-time-unit" aria-hidden="true">:</span>
                  <label>
                    <input
                      type="number"
                      min="0"
                      max="59"
                      step="1"
                      inputMode="numeric"
                      value={String(option.selectedMinute).padStart(2, '0')}
                      aria-label={`${option.label} minute`}
                      onChange={(event) => {
                        selectMode();
                        setMinuteValue(Number(event.target.value));
                      }}
                      onPointerDown={(event) => { delete event.currentTarget.dataset.keyboardFocus; }}
                      onKeyDown={(event) => { event.currentTarget.dataset.keyboardFocus = 'true'; }}
                      onBlur={(event) => { delete event.currentTarget.dataset.keyboardFocus; }}
                      onWheel={(event) => {
                        if (document.activeElement !== event.currentTarget) return;
                        event.preventDefault();
                        const current = option.selectedMinute;
                        setMinuteValue(event.deltaY < 0 ? (current + 1) % 60 : (current + 59) % 60);
                      }}
                    />
                  </label>
                  <div className="route-time-period-selector" role="group" aria-label={`${option.label} AM or PM`}>
                    {(['AM', 'PM'] as const).map((period) => {
                      const periodSelected = (option.selectedHour >= 12 ? 'PM' : 'AM') === period;
                      return (
                        <button
                          type="button"
                          key={period}
                          aria-pressed={periodSelected}
                          onClick={() => {
                            selectMode();
                            const value = twentyFourHourValue(twelveHourValue(option.selectedHour), period);
                            if (option.id === 'departure') setHour(value);
                            else setArrivalHour(value);
                          }}
                        >
                          {period}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        </div>
        )}

        <button
          type="submit"
          className="ui-button-primary"
          aria-busy={loading}

          disabled={loading || (driverAccess
            ? !/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(routeCode)
            : !canOptimize)}
        >
          {loading ? <LoaderCircle size={ICON_SIZE.large} aria-hidden="true" className="route-loading-spinner" /> : <Route size={ICON_SIZE.large} aria-hidden="true" />}
          {loading ? (driverAccess ? 'Loading Route…' : 'Composing Journey…') : (driverAccess ? 'Load Route' : 'Plan Journey')}
        </button>
      </form>

      {/* Signed-in operators only: the server refuses history without a token,
          and the driver view is deliberately a single assigned journey. */}
      {accessToken && !driverAccess ? (
        <RouteRunHistoryPanel
          accessToken={accessToken}
          onSelectRun={(code) => void loadRouteByCode(code)}
          activeRouteCode={result?.route_code ?? null}
        />
      ) : null}

      <section
        className="glass-panel route-result-panel"
        aria-label="Route result"
        aria-busy={loading}
      >
        {loading ? (
          <div role="status" aria-live="polite" className="route-result-placeholder">
            <span aria-hidden="true" className="route-loading-icon">
              <LoaderCircle size={ICON_SIZE.massive} color="var(--accent-cyan)" className="route-loading-spinner" />
            </span>
            <h3 className="route-result-state-heading">Composing your journey</h3>
            <p className="route-loading-description">
              Matching local roads, viable ferry terminals, and the selected departure window.
            </p>
          </div>
        ) : routeError ? (
          <div role="alert" className="route-error-state">
            <span aria-hidden="true" className="route-error-icon">
              <TriangleAlert size={ICON_SIZE.massive} />
            </span>
            <h3 className="route-result-state-heading">Route unavailable</h3>
            <p className="route-error-message">{routeError}</p>
            <button type="button" className="glass-button route-retry-button" onClick={() => void handleOptimize()} disabled={!canOptimize} >
              Try again
            </button>
          </div>
        ) : !result ? (
          <div className="route-result-placeholder">
            <span aria-hidden="true" className="route-empty-state-icon">
              <Route size={ICON_SIZE.massive} color="var(--accent-cyan)" />
            </span>
            <h3 className="route-empty-state-heading">Your route will appear here</h3>
            <p className="route-empty-state-description">
              {isNamedMode
                ? 'Choose two saved Batam places and the conditions for this trip.'
                : 'Choose any two Singapore or Batam endpoints'}
            </p>
            <div className="route-empty-state-features">
              {['Road access', 'Ferry transfer', 'Departure advice'].map((label) => (
                <span key={label} className="route-empty-state-feature">{label}</span>
              ))}
            </div>
          </div>
        ) : (
          <>
            <header className="route-result-header">
              <div className="route-result-title-group">
                <div className="route-result-title-row">
                <h2 className="route-result-title">
                  {resultOriginName} <span aria-hidden="true" className="route-result-direction-arrow">→</span> {resultDestinationName}
                </h2>
                </div>
              </div>
              <div className="route-result-actions">
                {googleRouteBenchmarkEnabled() && resultOrigin && resultDestination ? (
                  <button
                    type="button"
                    className="route-benchmark-action"
                    aria-controls="route-online-benchmark"
                    aria-busy={benchmarkLoading}
                    disabled={benchmarkLoading}
                    onClick={() => {
                      setActiveResultTab('summary');
                      void handleRouteBenchmark();
                    }}
                  >
                    {benchmarkLoading
                      ? <LoaderCircle size={ICON_SIZE.big} aria-hidden="true" className="route-benchmark-spinner" />
                      : <Globe size={ICON_SIZE.big} aria-hidden="true" />}
                    {benchmarkLoading ? 'Comparing…' : 'Compare online'}
                  </button>
                ) : null}
                {trafficNotModelled && resultSource !== 'offline' ? null : (
                  <span className={`badge ${resultSource === 'offline' ? 'badge-heavy' : 'badge-smooth'}`}>
                    {resultSource === 'offline'
                      ? 'Offline fallback'
                      : resultSource === 'live' ? 'Live API response' : 'Simulated traffic'}
                  </span>
                )}
              </div>
            </header>

            <WorkspaceSubtabs<RouteResultTab>
              idPrefix="route-result"
              ariaLabel="Route result sections"
              className="route-result-tabs"
              activeTab={activeResultTab}
              onActiveTabChange={setActiveResultTab}
              tabs={[
                {
                  id: 'summary',
                  label: 'Summary',
                  content: (
                    <div className="route-result-tab-content">
            <section
              aria-labelledby="departure-recommendation-heading"
              className={`route-departure-recommendation route-departure-recommendation-${result.optimal_departure.recommended === 'DEFER_30_MINS' ? 'defer' : 'depart'}`}>
              <div className="route-departure-recommendation-copy">
                <span className={`badge ${result.optimal_departure.recommended === 'DEFER_30_MINS' ? 'badge-heavy' : 'badge-smooth'}`}>
                  {result.optimal_departure.recommended === 'DEFER_30_MINS'
                    ? 'Leave 30 minutes later'
                    : trafficNotModelled ? 'Journey plan' : 'Depart now'}
                </span>
                <h3 id="departure-recommendation-heading" className="route-departure-recommendation-title">
                  {result.optimal_departure.reason}
                </h3>
                <div className="route-departure-recommendation-metadata">
                  {result.planned_departure ? <span>Departure {formatTime(result.planned_departure)}</span> : null}
                  <span className="route-departure-recommendation-distance"><Route size={ICON_SIZE.medium} aria-hidden="true" /> {selectedDistanceKm} km</span>
                  <span>{selectedTravelMins} min {isMultimodal ? 'movement time' : 'road time'}</span>
                </div>
              </div>
              {result.optimal_departure.time_saved_mins > 0 ? (
                <div className="route-departure-savings">
                  <span className="route-departure-savings-label">Potential road-time reduction</span>
                  <strong className="route-departure-savings-value">{result.optimal_departure.time_saved_mins} min</strong>
                </div>
              ) : null}
            </section>

            {result.schedule_provenance?.source === 'committed_timetable_simulation' ? (
              <p role="note" className="route-alternatives-note">
                <TriangleAlert
                  size={ICON_SIZE.medium}
                  aria-hidden="true"
                  className="route-note-icon"
                />
                <span>
                  Exact times use the committed timetable simulation because shared schedule
                  freshness is unavailable. Verify the departure and book with the operator.
                </span>
              </p>
            ) : null}

            {result.route_code ? (
              <section className="route-code-card" aria-labelledby="route-code-card-heading">
                <div>
                  <span className="route-code-card-eyebrow">Driver route ID</span>
                  <h3 id="route-code-card-heading">Use this seven-character ID to retrieve the journey</h3>
                </div>
                <strong aria-label={`Route ID ${result.route_code}`}>{result.route_code}</strong>
              </section>
            ) : null}

            <dl className="route-metrics-grid" aria-label="Route summary metrics" >
              {[
                { label: isMultimodal ? 'Movement time' : 'Road travel', value: `${selectedTravelMins} min`, icon: Clock, color: 'var(--accent-cyan)' },
                { label: isMultimodal ? 'Journey distance' : 'Road distance', value: `${selectedDistanceKm} km`, icon: Route, color: 'var(--accent-indigo)' },
                { label: 'Total trip ETA', value: `${selectedTotalEtaMins} min`, icon: Navigation, color: 'var(--accent-emerald)' },
                { label: 'Estimated CO₂', value: `${selectedCo2EmissionsKg} kg`, icon: Fuel, color: 'var(--accent-emerald)' },
              ].map((metric) => {
                const MetricIcon = metric.icon;
                return (
                  <div key={metric.label} className="route-metric-card">
                    <dt className="route-metric-label">
                      <MetricIcon size={ICON_SIZE.medium} aria-hidden="true" color={metric.color} /> {metric.label}
                    </dt>
                    <dd className="route-metric-value">{metric.value}</dd>
                  </div>
                );
              })}
            </dl>

            {(benchmarkLoading || benchmarkError || routeBenchmark) ? (
              <section
                id="route-online-benchmark"
                className="route-online-benchmark"
                aria-labelledby="route-online-benchmark-heading"
                aria-busy={benchmarkLoading}
              >
                <header>
                  <div>
                    <p>Optional provider comparison</p>
                    <h3 id="route-online-benchmark-heading">Online route metrics</h3>
                  </div>
                  {routeBenchmark ? (
                    <a
                      className="route-benchmark-attribution"
                      href={routeBenchmark.policy_url}
                      target="_blank"
                      rel="noreferrer"
                      translate="no"
                    >
                      Google Maps
                    </a>
                  ) : null}
                </header>

                {benchmarkLoading ? (
                  <p role="status" aria-live="polite" className="route-benchmark-status">
                    Requesting current distance and duration metrics…
                  </p>
                ) : benchmarkError ? (
                  <div className="route-benchmark-error" role="alert">
                    <p>{benchmarkError}</p>
                    <button type="button" onClick={() => void handleRouteBenchmark()}>
                      Try comparison again
                    </button>
                  </div>
                ) : routeBenchmark ? (
                  <>
                    <ul className="route-benchmark-routes" aria-label="Google Maps route summaries">
                      {routeBenchmark.routes.map((benchmarkRoute) => (
                        <li key={benchmarkRoute.id}>
                          <strong>{benchmarkRoute.summary}</strong>
                          <span>
                            {benchmarkRoute.route_labels.length > 0
                              ? `Labels: ${benchmarkRoute.route_labels.join(', ')}`
                              : 'No provider route label'}
                          </span>
                        </li>
                      ))}
                    </ul>
                    <p className="route-benchmark-preference-note">
                      {routeBenchmark.preference_honored_details.note}
                    </p>
                    <p className="route-benchmark-policy-note">
                      Metric-only comparison: not drawn on this map, not saved, and not used for
                      {' '}shortcut training. Review the{' '}
                      <a href={routeBenchmark.policy_url} target="_blank" rel="noreferrer">
                        Google Maps content policy
                      </a>.
                    </p>
                  </>
                ) : null}
              </section>
            ) : null}

            {routeOptions.length > 1 && (
              <section aria-labelledby="route-options-heading">
                <div className="route-options-header">
                  <div>
                    <h3 id="route-options-heading" className="route-options-heading">
                      <Route size={ICON_SIZE.large} aria-hidden="true" color="var(--accent-cyan)" /> Choose a road route
                    </h3>
                    <p className="route-options-description">
                      Every option follows mapped roads. Select one to update the map, metrics, and directions.
                    </p>
                  </div>
                  <span aria-live="polite" className="route-options-selection-status">
                    Showing {selectedRoute ? routeOptionLabel(selectedRoute.name) : 'recommended route'}
                  </span>
                </div>
                <div role="group" aria-label="Choose a road route" className="route-options-grid">
                  {routeOptions.map((route) => {
                    const isSelected = route.id === selectedRoute?.id;
                    const hasLowerEmissions = route.co2_emissions_kg < result.co2_emissions_kg;
                    const RouteIcon = route.isPrimary ? Zap : hasLowerEmissions ? Leaf : Route;
                    return (
                      <button
                        key={route.id}
                        type="button"
                        aria-pressed={isSelected}
                        aria-controls="route-result-panel-1"
                        onClick={() => handleSelectRoute(route.id)}
                        className="ui-sand-interactive route-option-button">
                        <span className="route-option-header">
                          <span className="route-option-title">
                            <RouteIcon size={ICON_SIZE.medium} aria-hidden="true" color={isSelected ? 'var(--accent-cyan)' : 'var(--text-muted)'} />
                            <strong className="route-option-name">{routeOptionLabel(route.name)}</strong>
                          </span>
                          {isSelected && <CircleCheck size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-cyan)" />}
                        </span>
                        <span className="route-option-metrics">
                          <strong className="route-option-duration">{route.estimated_travel_time_mins} min</strong>
                          <span>{route.distance_km} km</span>
                          <span>{route.co2_emissions_kg} kg CO₂</span>
                          {route.local_road_audit?.requested ? (
                            <span>{route.local_road_distance_km ?? 0} km mapped local roads</span>
                          ) : null}
                          {route.overlap_ratio !== undefined && !route.isPrimary && (
                            <span title="Physical road length shared with the recommended route">
                              {Math.round(route.overlap_ratio * 100)}% shared
                            </span>
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </section>
            )}

            {result.alternatives_note && (
              <p role="note" className="route-alternatives-note">
                <Info size={ICON_SIZE.medium} aria-hidden="true" className="route-note-icon" />
                <span>{result.alternatives_note}</span>
              </p>
            )}

            {/* The journey is still planned when the requested arrival cannot
                be met, so say by how much rather than presenting the schedule
                as if it satisfied the request. */}
            {result.schedule_feasibility
              && !result.schedule_feasibility.meets_requested_arrival && (
              <p role="note" className="route-alternatives-note route-schedule-miss">
                <TriangleAlert size={ICON_SIZE.medium} aria-hidden="true" className="route-note-icon" />
                <span>{result.schedule_feasibility.note}</span>
              </p>
            )}

                    </div>
                  ),
                },
                {
                  id: 'journey',
                  label: 'Journey map',
                  content: (
                    <div className="route-result-tab-content">

            {selectedLegs.length > 1 && (
              <section aria-label="Cross-border itinerary">
                <ol className="route-itinerary-list">
                  {selectedLegs.map((leg, index) => (
                    <li key={`${leg.mode}-${leg.from_name}-${leg.to_name}-${index}`} className="route-itinerary-leg">
                      <span aria-hidden="true" className={`route-itinerary-icon route-itinerary-icon-${leg.mode === 'FERRY' ? 'ferry' : 'road'}`}>
                        {leg.mode === 'FERRY' ? <Anchor size={ICON_SIZE.medium} /> : <Route size={ICON_SIZE.medium} />}
                      </span>
                      <strong
                        className="route-itinerary-leg-route"
                        title={`${index + 1}. ${leg.from_name} → ${leg.to_name}`}
                      >
                        {index + 1}. {leg.from_name} → {leg.to_name}
                      </strong>
                      <span className="route-itinerary-leg-meta">
                        <span className={`route-itinerary-status route-itinerary-status-${leg.is_estimate ? 'estimate' : 'mapped'}`}>
                          {leg.is_estimate ? 'Estimate' : 'Mapped'}
                        </span>
                        <span
                          className="route-itinerary-leg-duration"
                          title={`${leg.duration_mins} min${leg.wait_mins ? ` + ${leg.wait_mins} min wait` : ''}`}
                        >
                          {leg.duration_mins} min{leg.wait_mins ? ` + ${leg.wait_mins} min wait` : ''}
                        </span>
                      </span>
                      <span
                        className="route-itinerary-leg-detail"
                        title={`${leg.mode === 'FERRY' ? 'Ferry' : 'Road'} · ${leg.distance_km} km`}
                      >
                        {leg.mode === 'FERRY' ? 'Ferry' : 'Road'} · {leg.distance_km} km
                      </span>
                    </li>
                  ))}
                </ol>
                {selectedVehicleProfile.group === 'Freight' ? (
                  <p role="note" className="route-itinerary-freight-note">
                    Freight note: the cited ferry sources are passenger-operator timetables. Cargo, dangerous-goods, and vehicle acceptance must be confirmed separately with the operator.
                  </p>
                ) : null}
              </section>
            )}

            {activeResultTab === 'journey' && (selectedRoute?.route_geometry && selectedRoute.route_geometry.length >= 2 ? (
              <div className="route-preview-shell">
                <RoutePreviewMap
                  routes={routeOptions}
                  selectedRouteId={selectedRoute.id}
                  onSelectRoute={handleSelectRoute}
                  origin={originForPreview}
                  destination={destForPreview}
                  planningTrafficSnapshot={result.planning_traffic_snapshot}
                />
              </div>
            ) : (
              <div role="note" className="route-map-unavailable-note">
                <TriangleAlert size={ICON_SIZE.large} aria-hidden="true" color="var(--accent-amber)" className="route-map-unavailable-icon" />
                Route metrics are available, but this response did not include map geometry.
              </div>
            ))}

            {selectedNavigation?.route_narrative_words && (
              <section aria-labelledby="route-overview-heading" className="route-overview-card">
                <h3 id="route-overview-heading" className="route-overview-heading">
                  <Sparkles size={ICON_SIZE.medium} aria-hidden="true" /> Route Overview
                </h3>
                <p className="route-overview-description">
                  {selectedNavigation.route_narrative_words}
                </p>
              </section>
            )}

            {selectedLocalRoadAudit?.requested ? (
              <section
                className="route-local-road-audit"
                aria-labelledby="route-local-road-heading"
              >
                <div className="route-local-road-audit__header">
                  <MapPin size={ICON_SIZE.medium} aria-hidden="true" />
                  <h3 id="route-local-road-heading">Mapped Local Road Coverage</h3>
                </div>
                <p>
                  This path selects <strong>{selectedLocalRoadDistanceKm} km</strong> across
                  {' '}{selectedLocalRoadAudit.segment_count} mapped residential road
                  {selectedLocalRoadAudit.segment_count === 1 ? '' : ' sections'}.
                </p>
                {selectedLocalRoadSegments.length > 0 ? (
                  <ul aria-label="Selected mapped local-road sections">
                    {selectedLocalRoadSegments.slice(0, 5).map((segment) => (
                      <li key={segment.id}>
                        <span>{segment.name}</span>
                        <strong>{segment.distance_km} km</strong>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {selectedLocalRoadSegments.length > 5 ? (
                  <p>Plus {selectedLocalRoadSegments.length - 5} more mapped sections.</p>
                ) : null}
              </section>
            ) : null}

            {selectedAvoidedZones.length > 0 && (
              <section aria-labelledby="avoided-zones-heading" className="route-avoided-zones-card">
                <ShieldCheck size={ICON_SIZE.big} aria-hidden="true" color="#dc2626" className="route-callout-icon" />
                <div>
                  <h3 id="avoided-zones-heading" className="route-avoided-zones-heading">
                    High-congestion zones avoided
                  </h3>
                  <p className="route-avoided-zones-description">
                    The returned road path avoids these reported bottlenecks:
                  </p>
                  <div className="route-avoided-zones-list">
                    {selectedAvoidedZones.map((zoneName) => (
                      <span key={zoneName} className="badge badge-heavy route-avoided-zone-badge">
                        {zoneName}
                      </span>
                    ))}
                  </div>
                </div>
              </section>
            )}

            {result.shortcuts_used && result.shortcuts_used.length > 0 && (
              <section aria-label="Route adjustments" className="route-adjustments-card">
                <h3 className="route-adjustments-heading">
                  <Zap size={ICON_SIZE.big} aria-hidden="true" /> Route adjustments applied
                </h3>
                <div className="route-badge-list">
                  {result.shortcuts_used.map(s => (
                    <span key={s.id} className="badge badge-smooth route-adjustment-badge">
                      {s.badge} (Saved {s.time_saved_mins} mins)
                    </span>
                  ))}
                </div>
              </section>
            )}

            {result.local_tips && result.local_tips.length > 0 && (
              <div className="route-local-tips-card">
                <Lightbulb size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-amber)" className="route-callout-icon" />
                <ul className="route-local-tips-list">
                  {result.local_tips.map((tip) => (
                    <li key={tip}>{tip}</li>
                  ))}
                </ul>
              </div>
            )}

                    </div>
                  ),
                },
                {
                  id: 'directions',
                  label: 'Directions',
                  content: (
                    <div className="route-result-tab-content">

            {/* Turn-by-Turn Step Guidance, Traffic Signals & Cafe Landmarks */}
            {selectedNavigation?.maneuvers && selectedNavigation.maneuvers.length > 0 ? (
              <section aria-labelledby="route-directions-heading" className="route-directions-card">
                <div className="route-directions-header">
                  <h3 id="route-directions-heading" className="route-directions-heading">
                    <Navigation size={ICON_SIZE.large} aria-hidden="true" color="var(--accent-cyan)" /> {isMultimodal ? 'Journey steps' : 'Road directions'}
                  </h3>
                  <div className="route-badge-list">
                    <span className="badge badge-smooth route-traffic-lights-badge">
                      {selectedNavigation.traffic_lights_count} Traffic Light Intersections
                    </span>
                    {selectedNavigation.landmarks_along_route && selectedNavigation.landmarks_along_route.length > 0 && (
                      <span className="badge badge-smooth route-landmarks-badge">
                        <Coffee size={ICON_SIZE.big} aria-hidden="true" />
                        {selectedNavigation.landmarks_along_route.length} Cafes &amp; Landmarks
                      </span>
                    )}
                  </div>
                </div>

                <ol className="route-directions-list">
                  {selectedNavigation.maneuvers.map((m) => {
                    const maneuverKey = m.modifier && m.modifier !== 'straight'
                      ? m.modifier
                      : m.icon;
                    let StepIcon: LucideIcon = MoveUp;
                    let stepIconRotation = 0;
                    let stepTone = 'default';
                    if (maneuverKey === 'turn_left' || maneuverKey === 'left') { StepIcon = CornerUpLeft; }
                    else if (maneuverKey === 'turn_right' || maneuverKey === 'right') { StepIcon = CornerUpRight; }
                    else if (maneuverKey === 'slight_left' || maneuverKey === 'turn_slight_left') { StepIcon = CornerUpLeft; }
                    else if (maneuverKey === 'slight_right' || maneuverKey === 'turn_slight_right') { StepIcon = CornerUpRight; }
                    else if (maneuverKey === 'sharp_left' || maneuverKey === 'turn_sharp_left') { StepIcon = CornerUpLeft; }
                    else if (maneuverKey === 'sharp_right' || maneuverKey === 'turn_sharp_right') { StepIcon = CornerUpRight; }
                    else if (maneuverKey === 'roundabout') { StepIcon = RotateCw; }
                    else if (maneuverKey === 'take_ramp') { StepIcon = CornerUpRight; }
                    else if (maneuverKey === 'u_turn') { StepIcon = Redo; stepIconRotation = 90; }
                    else if (maneuverKey === 'ferry' || maneuverKey === 'transfer') { StepIcon = Ship; stepTone = 'ferry'; }
                    else if (maneuverKey === 'continue' || maneuverKey === 'depart') { StepIcon = MoveUp; }
                    else if (maneuverKey === 'traffic_light') { StepIcon = CircleDot; stepTone = 'traffic-light'; }
                    else if (maneuverKey === 'cafe') { StepIcon = Coffee; stepTone = 'cafe'; }
                    else if (maneuverKey === 'landmark') { StepIcon = MapPin; stepTone = 'landmark'; }
                    else if (maneuverKey === 'arrive') { StepIcon = Flag; stepTone = 'arrive'; }

                    return (
                      <li key={`${selectedRoute?.id ?? PRIMARY_ROUTE_ID}-${m.step}`} className={`route-direction-step route-direction-step-${stepTone}`}>
                        <div aria-hidden="true" className="route-direction-icon">
                          <StepIcon
                            size={ICON_SIZE.medium}
                            strokeWidth={2.25}
                            style={stepIconRotation ? { transform: `rotate(${stepIconRotation}deg)` } : undefined}
                          />
                        </div>
                        <div className="route-direction-content">
                          <div className="route-direction-instruction">
                            Step {m.step}: {m.instruction}
                          </div>
                          <div className="route-direction-metadata">
                            <span>Street: <strong>{m.street}</strong></span>
                            {m.road_ref && <span>Road ref: <strong>{m.road_ref}</strong></span>}
                            {m.distance_m > 0 && <span>{formatManeuverDistance(m.distance_m)}</span>}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </section>
            ) : (
              <section aria-labelledby="route-directions-heading" role="note" className="route-directions-unavailable">
                <Navigation size={ICON_SIZE.large} aria-hidden="true" color="var(--text-muted)" className="route-note-icon" />
                <div>
                  <h3 id="route-directions-heading" className="route-directions-unavailable-heading">Detailed directions unavailable</h3>
                  <p className="route-directions-unavailable-description">
                    This road option includes verified geometry, but its source did not return manoeuvre-level guidance.
                  </p>
                </div>
              </section>
            )}

                    </div>
                  ),
                },
                {
                  id: 'connections',
                  label: 'Comparison',
                  content: (
                    <div className="route-result-tab-content">
            {/* All-Vehicle Mode Comparison Grid */}

            {!isMultimodal && (
            <section aria-labelledby="vehicle-comparison-heading" className="route-vehicle-comparison">
              <h3 id="vehicle-comparison-heading" className="route-vehicle-comparison-heading">
                <Route size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-cyan)" /> Indicative vehicle comparison
              </h3>
              <p className="route-section-note">
                Same-path planning estimates from the shared vehicle profiles. Re-plan after changing vehicle to optimize the road path itself.
              </p>

              <div
                aria-label="Vehicle profile comparison"
                role="region"
                tabIndex={0}
                className="route-vehicle-comparison-scroll">
                <table className="route-vehicle-comparison-table">
                  <thead className="route-vehicle-comparison-head">
                    <tr>
                      <th scope="col" className="route-vehicle-comparison-edge-heading">Vehicle</th>
                      <th scope="col" className="route-vehicle-comparison-metric-heading">Road time</th>
                      <th scope="col" className="route-vehicle-comparison-metric-heading">Terminal buffer</th>
                      <th scope="col" className="route-vehicle-comparison-metric-heading">Total</th>
                      <th scope="col" className="route-vehicle-comparison-edge-heading">CO₂ estimate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {VEHICLE_CATALOG.map((profile) => {
                      const Icon = VEHICLE_ICONS[profile.icon];
                      const isActive = result.vehicle_type === profile.id;
                      const baseMins = comparisonBaseMins
                        * comparisonBaseProfile.speedFactor / profile.speedFactor;
                      const congestionMins = comparisonCongestionMins
                        * profile.congestionSensitivity / comparisonBaseProfile.congestionSensitivity;
                      const weatherMins = comparisonWeatherMins
                        * profile.weatherSensitivity / comparisonBaseProfile.weatherSensitivity;
                      const profileManeuverWeight = profile.turnPenaltySeconds
                        + profile.shortManeuverPenaltySeconds
                        + profile.signalDelaySeconds;
                      const maneuverMins = comparisonManeuverMins
                        * profileManeuverWeight / comparisonManeuverWeight;
                      const roadTime = Math.round(
                        (baseMins + congestionMins + weatherMins + maneuverMins) * 10,
                      ) / 10;
                      const totalEta = Math.round(
                        (roadTime + profile.terminalBufferMins) * 10,
                      ) / 10;
                      const co2Kg = (
                        selectedDistanceKm * profile.co2KgPerKm
                          + (Math.max(0, congestionMins) / 60) * profile.idleCo2KgPerHour
                      ).toFixed(2);

                      return (
                        <tr
                          key={profile.id}
                          aria-current={isActive ? 'true' : undefined}
                          className={isActive ? 'route-vehicle-row route-vehicle-row-active' : 'route-vehicle-row'}>
                          <th scope="row" className="route-vehicle-cell route-vehicle-cell-label">
                            <span className={isActive ? 'route-vehicle-name route-vehicle-name-active' : 'route-vehicle-name'}>
                              <Icon size={ICON_SIZE.big} aria-hidden="true" /> {profile.label}
                              {isActive ? <span className="badge badge-smooth route-selected-vehicle-badge">Selected</span> : null}
                            </span>
                          </th>
                          <td className="route-vehicle-cell">{roadTime} min</td>
                          <td className="route-vehicle-cell">+{profile.terminalBufferMins} min</td>
                          <td className="route-vehicle-cell route-vehicle-cell-total">{totalEta} min</td>
                          <td className="route-vehicle-cell route-vehicle-cell-emissions">{co2Kg} kg</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
            )}


            <section aria-labelledby="connected-ferries-heading" hidden>
              <h3 id="connected-ferries-heading" className="route-ferry-connections-heading">
                <Anchor size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-cyan)" /> {isMultimodal ? 'Ferry schedule evidence' : 'Published ferry connections after arrival'}
              </h3>
              {result.ferry_connection_note && (
                <p className="route-ferry-connection-note">
                  {result.ferry_connection_note}
                </p>
              )}
              <p
                role="note"
                className="route-section-note">
                Published operator departure slots, not live operating status. Schedules may change; verify with the operator before travel.
              </p>
              {selectedFerries.length > 0 ? (
              <ul className="ferry-results-grid route-ferry-connections-list">
                {selectedFerries.map((f, i) => (
                  <li key={f.sailing_id ?? `${f.ferry_name}-${f.departure_time}-${i}`} className="route-ferry-connection-card">
                    <div className="route-ferry-connection-header">
                      <strong className="route-ferry-operator">{f.operator ?? f.ferry_name}</strong>
                      <span className={`badge ${ferryBadgeClass('SCHEDULED')} route-ferry-status`}>
                        {prettyStatus('SCHEDULED')}
                      </span>
                    </div>
                    <div className="route-ferry-departure">
                      {formatTime(f.departure_time)}
                      <span className="route-ferry-departure-relative">
                        {relativeDeparture(f.minutes_until_departure)}
                      </span>
                    </div>
                    <div className="route-ferry-itinerary">
                      <span>{f.departure_port} → {f.arrival_port}</span>
                      {f.arrival_time ? (
                        <span>Estimated arrival: {formatTime(f.arrival_time)}</span>
                      ) : null}
                    </div>
                    <div className="route-ferry-source-meta">
                      {f.schedule_source_url ? (
                        <a
                          href={f.schedule_source_url}
                          target="_blank"
                          rel="noreferrer"
                          className="route-ferry-source-link">
                          Official operator timetable
                        </a>
                      ) : null}
                      {f.schedule_last_verified_at ? (
                        <span className="route-ferry-verification-time">
                          Last verified {formatScheduleVerifiedAt(f.schedule_last_verified_at)}
                        </span>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
              ) : (
                <p className="route-ferry-empty-state">
                  {isMultimodal
                    ? 'No departure is asserted for this direction in the bundled official snapshot. The route remains usable as a corridor plan; verify and book the actual sailing with the operator.'
                    : 'No matching ferry sailing was returned for this arrival window.'}
                </p>
              )}
            </section>
                    </div>
                  ),
                },
              ]}
            />
          </>
        )}
      </section>

      {/* Singapore-Batam map picker modal */}
      <WorldMapPickerModal
        isOpen={pickerTarget !== null}
        onClose={closePicker}
        title={pickerTarget === 'origin' ? 'Select origin in Singapore or Batam' : pickerTarget === 'dest' ? 'Select destination in Singapore or Batam' : 'Select intermediate stop'}
        initialLat={pickerTarget === 'origin' ? (freeOrigin?.lat ?? 1.12) : pickerTarget === 'dest' ? (freeDest?.lat ?? 1.12) : (typeof pickerTarget === 'number' ? waypoints[pickerTarget]?.lat : undefined) ?? 1.12}
        initialLng={pickerTarget === 'origin' ? (freeOrigin?.lng ?? 104.02) : pickerTarget === 'dest' ? (freeDest?.lng ?? 104.02) : (typeof pickerTarget === 'number' ? waypoints[pickerTarget]?.lng : undefined) ?? 104.02}
        initialName={pickerTarget === 'origin' ? freeOrigin?.display_name : pickerTarget === 'dest' ? freeDest?.display_name : typeof pickerTarget === 'number' ? waypoints[pickerTarget]?.display_name : undefined}
        onSelectLocation={(loc) => {
          if (pickerTarget === 'origin') {
            setFreeOrigin(loc);
          } else if (pickerTarget === 'dest') {
            setFreeDest(loc);
          } else if (typeof pickerTarget === 'number') {
            setWaypoints((current) => current.map((item, index) => (
              index === pickerTarget ? loc : item
            )));
          }
          setPickerTarget(null);
          invalidateResult();
        }}
      />
    </div>
  );
};
