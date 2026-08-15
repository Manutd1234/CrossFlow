import { useEffect, useMemo, useRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import L from 'leaflet';
import {
  CircleDot,
  Clock,
  Coffee,
  CornerUpLeft,
  CornerUpRight,
  Flag,
  MapPin,
  MoveUp,
  Redo,
  RotateCw,
  Route,
  Ship,
  type LucideIcon,
} from 'lucide-react';
import type {
  CongestionZone,
  PlanningTrafficSnapshot,
  RoadRouteOption,
  RouteLeg,
  RouteLocation,
} from '../../types';
import { ICON_SIZE } from '../../theme/iconSizes';
import { MAP_PALETTE } from '../../theme/mapPalette';
import { FERRY_SEA_ROUTE } from '../../data/mockData';

interface RoutePreviewMapProps {
  routes: ReadonlyArray<RoadRouteOption>;
  selectedRouteId: string;
  onSelectRoute?: (routeId: string) => void;
  origin?: RouteLocation;
  destination?: RouteLocation;
  planningTrafficSnapshot?: PlanningTrafficSnapshot;
}

const JOURNEY_MAP_MIN_ZOOM = 10;
const JOURNEY_MAP_MAX_ZOOM = 18;

function routeColor(index: number, isSelected: boolean): string {
  return isSelected
    ? MAP_PALETTE.route.selected
    : MAP_PALETTE.route.alternatives[index % MAP_PALETTE.route.alternatives.length];
}

function routeTagLabel(name: string): string {
  return name.replace(/\broad route\b/gi, '').replace(/\s+/g, ' ').trim();
}

function journeyMapLeg(leg: RouteLeg): RouteLeg {
  if (leg.mode !== 'FERRY') return leg;

  const fromName = leg.from_name.toLowerCase();
  const toName = leg.to_name.toLowerCase();
  const travelsHarbourFrontToBatamCentre = fromName.includes('harbourfront')
    && toName.includes('batam centre');
  const travelsBatamCentreToHarbourFront = fromName.includes('batam centre')
    && toName.includes('harbourfront');

  if (!travelsHarbourFrontToBatamCentre && !travelsBatamCentreToHarbourFront) {
    return leg;
  }

  return {
    ...leg,
    geometry: travelsHarbourFrontToBatamCentre
      ? FERRY_SEA_ROUTE
      : [...FERRY_SEA_ROUTE].reverse(),
  };
}

function tooltipContent(prefix: string, name: string): HTMLSpanElement {
  const element = document.createElement('span');
  const label = document.createElement('strong');
  label.textContent = `${prefix}: `;
  element.append(label, document.createTextNode(name));
  return element;
}

function plainTooltip(text: string): HTMLSpanElement {
  const element = document.createElement('span');
  element.textContent = text;
  return element;
}

function trafficTooltip(zone: CongestionZone): HTMLSpanElement {
  const element = document.createElement('span');
  const heading = document.createElement('strong');
  heading.textContent = `${zone.level.replace(/_/g, ' ')} hotspot: `;
  const pressure = zone.modeled_emissions_pressure;
  element.append(
    heading,
    document.createTextNode(`${zone.name} (score ${zone.congestion_index})`),
    document.createElement('br'),
    document.createTextNode(`Modeled emissions pressure: ${pressure.level} (${pressure.index})`),
    document.createElement('br'),
    document.createTextNode('Relative queue proxy · not measured area CO₂'),
  );
  return element;
}

function applyStyles(element: HTMLElement, styles: Partial<CSSStyleDeclaration>): void {
  Object.assign(element.style, styles);
}

function endpointPin(label: 'A' | 'B', color: string, endpoint: 'origin' | 'destination'): HTMLDivElement {
  const pin = document.createElement('div');
  const text = document.createElement('span');
  text.textContent = label;
  pin.dataset.routeEndpoint = endpoint;
  pin.dataset.markerColor = color;
  pin.setAttribute('aria-hidden', 'true');
  pin.append(text);

  applyStyles(pin, {
    alignItems: 'center',
    background: color,
    border: `3px solid ${MAP_PALETTE.route.casing}`,
    borderRadius: '50% 50% 50% 8px',
    boxShadow: '0 3px 10px rgba(7, 59, 76, 0.28)',
    display: 'flex',
    height: '34px',
    justifyContent: 'center',
    transform: 'rotate(-45deg)',
    width: '34px',
  });
  applyStyles(text, {
    color: MAP_PALETTE.route.casing,
    fontFamily: 'Inter, sans-serif',
    fontSize: '13px',
    fontWeight: '800',
    lineHeight: '1',
    transform: 'rotate(45deg)',
  });
  return pin;
}

function maneuverBadge(Icon: LucideIcon, color: string, rotation = 0): HTMLDivElement {
  const badge = document.createElement('div');
  badge.innerHTML = renderToStaticMarkup(
    <Icon
      aria-hidden="true"
      size={ICON_SIZE.medium}
      strokeWidth={2.25}
      style={rotation ? { transform: `rotate(${rotation}deg)` } : undefined}
    />,
  );
  badge.setAttribute('aria-hidden', 'true');
  applyStyles(badge, {
    alignItems: 'center',
    background: '#ffffff',
    border: `2px solid ${color}`,
    borderRadius: '50%',
    boxShadow: '0 2px 6px rgba(15, 23, 42, 0.28)',
    color,
    display: 'flex',
    height: '28px',
    justifyContent: 'center',
    width: '28px',
  });
  return badge;
}

function longestSegmentMidpoint(
  geometry: ReadonlyArray<readonly [number, number]>,
): [number, number] | undefined {
  if (geometry.length === 0) return undefined;
  if (geometry.length === 1) return [...geometry[0]];

  let longestLengthSquared = -1;
  let midpoint: [number, number] = [...geometry[0]];
  const radiansPerDegree = Math.PI / 180;

  for (let index = 1; index < geometry.length; index += 1) {
    const start = geometry[index - 1];
    const end = geometry[index];
    const meanLatitudeRadians = ((start[0] + end[0]) / 2) * radiansPerDegree;
    const latitudeDelta = (end[0] - start[0]) * radiansPerDegree;
    const longitudeDelta = (end[1] - start[1]) * radiansPerDegree
      * Math.cos(meanLatitudeRadians);
    const lengthSquared = latitudeDelta ** 2 + longitudeDelta ** 2;

    if (lengthSquared <= longestLengthSquared) continue;
    longestLengthSquared = lengthSquared;
    midpoint = [
      (start[0] + end[0]) / 2,
      (start[1] + end[1]) / 2,
    ];
  }

  return midpoint;
}

function routeSummaryBadge(durationMins: number, distanceKm?: number): HTMLDivElement {
  const badge = document.createElement('div');
  const duration = document.createElement('strong');
  duration.textContent = `${durationMins} min`;
  applyStyles(duration, {
    color: MAP_PALETTE.route.selected,
    fontSize: '12px',
    fontWeight: '700',
    lineHeight: '1',
  });
  badge.append(duration);

  if (distanceKm !== undefined) {
    const distance = document.createElement('span');
    distance.textContent = `${distanceKm} km`;
    applyStyles(distance, {
      color: MAP_PALETTE.route.connector,
      fontSize: '12px',
      fontWeight: '700',
      lineHeight: '1',
    });
    badge.append(distance);
  }

  applyStyles(badge, {
    alignItems: 'center',
    background: '#ffffff',
    border: `1.5px solid ${MAP_PALETTE.route.selected}`,
    borderRadius: '999px',
    boxSizing: 'border-box',
    boxShadow: '0 4px 14px rgba(15, 23, 42, 0.22)',
    display: 'flex',
    fontSize: '12px',
    gap: '7px',
    height: '32px',
    justifyContent: 'center',
    padding: '6px 9px',
    transform: 'translate(-50%, -50%)',
    whiteSpace: 'nowrap',
    width: 'max-content',
  });
  return badge;
}

export function RoutePreviewMap({
  routes,
  selectedRouteId,
  onSelectRoute,
  origin,
  destination,
  planningTrafficSnapshot,
}: RoutePreviewMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const routeLayersRef = useRef<L.LayerGroup | null>(null);
  const trafficLayersRef = useRef<L.LayerGroup | null>(null);
  const routeBoundsRef = useRef<L.LatLngBounds | null>(null);

  const drawableRoutes = useMemo(
    () => routes.filter(route => route.route_geometry.length >= 2),
    [routes],
  );
  const selectedRoute = drawableRoutes.find(route => route.id === selectedRouteId)
    ?? drawableRoutes[0];
  const isStraightLineFallback = selectedRoute?.route_data_source === 'offline_straight_line';
  const selectedLegs = useMemo(
    () => selectedRoute?.route_legs?.map(journeyMapLeg) ?? [],
    [selectedRoute],
  );
  const hasEstimatedRoadLeg = selectedLegs.some(
    leg => leg.mode === 'ROAD' && leg.is_estimate,
  );
  const isEstimatedFallback = isStraightLineFallback
    || selectedRoute?.route_data_source === 'offline_access_estimate'
    || selectedRoute?.route_data_source === 'multimodal_offline_estimate'
    || hasEstimatedRoadLeg;
  const originLat = origin?.lat;
  const originLng = origin?.lng;
  const originName = origin?.name;
  const destinationLat = destination?.lat;
  const destinationLng = destination?.lng;
  const destinationName = destination?.name;
  const planningZones = planningTrafficSnapshot?.zones;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const smallWidthQuery = window.matchMedia?.('(max-width: 1000px)');
    const smallHeightQuery = window.matchMedia?.('(max-height: 999px)');
    const isCompactLayout = () => Boolean(
      smallWidthQuery?.matches || smallHeightQuery?.matches,
    );
    const map = L.map(containerRef.current, {
      maxZoom: JOURNEY_MAP_MAX_ZOOM,
      minZoom: JOURNEY_MAP_MIN_ZOOM,
      zoomControl: false,
      scrollWheelZoom: !isCompactLayout(),
    }).setView([1.12, 104.02], 11);
    mapRef.current = map;
    L.control.zoom({ position: 'topright' }).addTo(map);

    const syncWheelZoom = () => {
      if (isCompactLayout()) map.scrollWheelZoom.disable();
      else map.scrollWheelZoom.enable();
    };
    smallWidthQuery?.addEventListener('change', syncWheelZoom);
    smallHeightQuery?.addEventListener('change', syncWheelZoom);

    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
      maxZoom: JOURNEY_MAP_MAX_ZOOM,
    }).addTo(map);

    trafficLayersRef.current = L.layerGroup().addTo(map);
    routeLayersRef.current = L.layerGroup().addTo(map);

    return () => {
      smallWidthQuery?.removeEventListener('change', syncWheelZoom);
      smallHeightQuery?.removeEventListener('change', syncWheelZoom);
      map.remove();
      mapRef.current = null;
      routeLayersRef.current = null;
      trafficLayersRef.current = null;
      routeBoundsRef.current = null;
    };
  }, []);

  useEffect(() => {
    const layers = trafficLayersRef.current;
    if (!layers) return;

    layers.clearLayers();
    (planningZones ?? []).forEach((zone) => {
      const isCritical = zone.level === 'SUPER_CONGESTED';
      const isHeavy = zone.level === 'HEAVY';
      if (zone.modeled_emissions_pressure.level === 'HIGH') {
        L.circle([zone.lat, zone.lng], {
          radius: zone.radius_m + 110,
          color: MAP_PALETTE.emissionsPressure.high,
          weight: 3,
          fillOpacity: 0,
          dashArray: '2, 7',
        })
          .bindTooltip(trafficTooltip(zone), { sticky: true })
          .addTo(layers);
      }
      L.circle([zone.lat, zone.lng], {
        radius: zone.radius_m,
        color: zone.color,
        weight: isCritical ? 2.5 : 1.5,
        fillColor: zone.color,
        fillOpacity: isCritical ? 0.3 : isHeavy ? 0.2 : 0.14,
        dashArray: isCritical ? '4, 4' : isHeavy ? '8, 5' : undefined,
      })
        .bindTooltip(trafficTooltip(zone), { sticky: true })
        .addTo(layers);
      L.circleMarker([zone.lat, zone.lng], {
        radius: isCritical ? 7 : 6,
        color: MAP_PALETTE.route.casing,
        fillColor: zone.color,
        fillOpacity: 1,
        weight: 2,
      })
        .bindTooltip(trafficTooltip(zone), { sticky: true })
        .addTo(layers);
    });
  }, [planningZones]);

  useEffect(() => {
    const map = mapRef.current;
    const layers = routeLayersRef.current;
    if (!map || !layers || !selectedRoute) return;

    layers.clearLayers();

    drawableRoutes.forEach((route, index) => {
      if (route.id === selectedRoute.id) return;
      const color = routeColor(index, false);
      L.polyline(route.route_geometry, {
        color: MAP_PALETTE.route.casing,
        dashArray: '9 7',
        weight: 7,
        opacity: 0.82,
      }).addTo(layers);
      const inactiveLine = L.polyline(route.route_geometry, {
        bubblingMouseEvents: false,
        color,
        dashArray: '9 7',
        lineCap: 'round',
        lineJoin: 'round',
        opacity: 0.78,
        weight: 4,
      })
        .bindTooltip(
          plainTooltip(`Select ${route.name} · ${route.distance_km} km · ${route.estimated_travel_time_mins} min`),
          { sticky: true },
        )
        .addTo(layers);
      if (onSelectRoute) inactiveLine.on('click', () => onSelectRoute(route.id));
    });

    const geometry = selectedRoute.route_geometry;
    const drawableLegs = selectedLegs.filter(leg => leg.geometry.length >= 2);
    const displayedGeometry = drawableLegs.length > 0
      ? drawableLegs.flatMap((leg, index) => (
          index === 0 ? leg.geometry : leg.geometry.slice(1)
        ))
      : geometry;
    let line: L.Polyline | null = null;
    drawableLegs.forEach((leg) => {
      const isFerry = leg.mode === 'FERRY';
      const isEstimate = leg.mode === 'ROAD' && leg.is_estimate;
      const color = isFerry
        ? '#0284c7'
        : isEstimate ? '#d97706' : MAP_PALETTE.route.selected;
      const dashArray = isFerry ? '5 9' : isEstimate ? '12 9' : undefined;
      L.polyline(leg.geometry, {
        color: MAP_PALETTE.route.casing,
        weight: isFerry ? 9 : 11,
        opacity: 0.94,
        dashArray,
      }).addTo(layers);
      const legLine = L.polyline(leg.geometry, {
        color,
        weight: isFerry ? 5 : 6,
        opacity: 1,
        dashArray,
        lineCap: 'round',
        lineJoin: 'round',
      })
        .bindTooltip(
          plainTooltip(`${leg.mode === 'FERRY' ? 'Ferry' : 'Road'} · ${leg.from_name} → ${leg.to_name} · ${leg.duration_mins} min`),
          { sticky: true },
        )
        .addTo(layers);
      line ??= legLine;

      if (isFerry) {
        const terminalIcon = L.divIcon({
          className: 'route-terminal-badge',
          html: maneuverBadge(Ship, '#0284c7'),
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        });
        L.marker(leg.geometry[0], { icon: terminalIcon })
          .bindTooltip(tooltipContent('Ferry from', leg.from_name), { direction: 'top' })
          .addTo(layers);
        L.marker(leg.geometry[leg.geometry.length - 1], { icon: terminalIcon })
          .bindTooltip(tooltipContent('Ferry to', leg.to_name), { direction: 'top' })
          .addTo(layers);
      }
    });

    if (!line) {
      L.polyline(geometry, {
        color: MAP_PALETTE.route.casing,
        weight: 11,
        opacity: 0.96,
        dashArray: isEstimatedFallback ? '12 12' : undefined,
      }).addTo(layers);
      line = L.polyline(geometry, {
        color: MAP_PALETTE.route.selected,
        weight: 6,
        opacity: 1,
        dashArray: isEstimatedFallback ? '12 12' : undefined,
        lineCap: 'round',
        lineJoin: 'round',
      })
        .bindTooltip(
          plainTooltip(`${selectedRoute.name} · ${selectedRoute.distance_km} km · ${selectedRoute.estimated_travel_time_mins} min`),
          { sticky: true },
        )
        .addTo(layers);
    }

    const start = geometry[0];
    const end = geometry[geometry.length - 1];
    const requestedStart: [number, number] = originLat !== undefined && originLng !== undefined
      ? [originLat, originLng]
      : start;
    const requestedEnd: [number, number] = destinationLat !== undefined && destinationLng !== undefined
      ? [destinationLat, destinationLng]
      : end;
    const summaryBadgePosition = longestSegmentMidpoint(displayedGeometry);

    if (requestedStart[0] !== start[0] || requestedStart[1] !== start[1]) {
      L.polyline([requestedStart, start], {
        color: MAP_PALETTE.route.connector, weight: 2, opacity: 0.85, dashArray: '5 6',
      }).bindTooltip(plainTooltip('Connector to the nearest routable road')).addTo(layers);
    }
    if (requestedEnd[0] !== end[0] || requestedEnd[1] !== end[1]) {
      L.polyline([end, requestedEnd], {
        color: MAP_PALETTE.route.connector, weight: 2, opacity: 0.85, dashArray: '5 6',
      }).bindTooltip(plainTooltip('Connector from the nearest routable road')).addTo(layers);
    }

    const originIcon = L.divIcon({
      className: 'route-endpoint-pin route-endpoint-pin--origin',
      html: endpointPin('A', MAP_PALETTE.endpoint.origin, 'origin'),
      iconAnchor: [17, 34],
      iconSize: [34, 34],
      tooltipAnchor: [0, -30],
    });
    L.marker(requestedStart, { icon: originIcon, zIndexOffset: 1000 })
      .bindTooltip(tooltipContent('From', originName ?? 'Origin'), { direction: 'top' })
      .addTo(layers);

    const destinationIcon = L.divIcon({
      className: 'route-endpoint-pin route-endpoint-pin--destination',
      html: endpointPin('B', MAP_PALETTE.endpoint.destination, 'destination'),
      iconAnchor: [17, 34],
      iconSize: [34, 34],
      tooltipAnchor: [0, -30],
    });
    L.marker(requestedEnd, { icon: destinationIcon, zIndexOffset: 1000 })
      .bindTooltip(tooltipContent('To', destinationName ?? 'Destination'), { direction: 'top' })
      .addTo(layers);

    selectedRoute.navigation?.maneuvers?.forEach((maneuver) => {
      if (!maneuver.coords || maneuver.coords.length < 2) return;
      const maneuverKey = maneuver.modifier && maneuver.modifier !== 'straight'
        ? maneuver.modifier
        : maneuver.icon;
      const markerStyles: Record<string, { icon: LucideIcon; color: string; rotation?: number }> = {
        turn_left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        turn_right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        slight_left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        turn_slight_left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        slight_right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        turn_slight_right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        sharp_left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        turn_sharp_left: { icon: CornerUpLeft, color: MAP_PALETTE.route.selected },
        sharp_right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        turn_sharp_right: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        roundabout: { icon: RotateCw, color: MAP_PALETTE.maneuver.roundabout },
        take_ramp: { icon: CornerUpRight, color: MAP_PALETTE.route.selected },
        u_turn: { icon: Redo, color: MAP_PALETTE.traffic.critical, rotation: 90 },
        continue: { icon: MoveUp, color: MAP_PALETTE.route.selected },
        depart: { icon: MoveUp, color: MAP_PALETTE.endpoint.origin },
        arrive: { icon: Flag, color: MAP_PALETTE.endpoint.origin },
        traffic_light: { icon: CircleDot, color: MAP_PALETTE.maneuver.trafficLight },
        cafe: { icon: Coffee, color: MAP_PALETTE.maneuver.roundabout },
        landmark: { icon: MapPin, color: MAP_PALETTE.node.batam },
      };
      const markerStyle = markerStyles[maneuverKey];
      if (!markerStyle) return;

      const turnIcon = L.divIcon({
        className: 'gmaps-turn-badge',
        html: maneuverBadge(markerStyle.icon, markerStyle.color, markerStyle.rotation),
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      });
      L.marker(maneuver.coords, { icon: turnIcon })
        .bindTooltip(plainTooltip(`Step ${maneuver.step}: ${maneuver.instruction}`), { direction: 'top' })
        .addTo(layers);
    });

    if (summaryBadgePosition) {
      const badgeIcon = L.divIcon({
        className: 'gmaps-floating-badge',
        html: routeSummaryBadge(
          selectedRoute.estimated_travel_time_mins,
          selectedRoute.distance_km,
        ),
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      });
      L.marker(summaryBadgePosition, { icon: badgeIcon, interactive: false }).addTo(layers);
    }

    const bounds = line.getBounds();
    drawableRoutes.forEach(route => {
      route.route_geometry.forEach(point => bounds.extend(point));
    });
    displayedGeometry.forEach(point => bounds.extend(point));
    bounds.extend(requestedStart);
    bounds.extend(requestedEnd);
    routeBoundsRef.current = bounds;
    map.fitBounds(bounds.pad(0.14), { maxZoom: 14 });
    const resizeTimer = window.setTimeout(() => map.invalidateSize(), 50);

    return () => {
      window.clearTimeout(resizeTimer);
    };
  }, [
    destinationLat,
    destinationLng,
    destinationName,
    drawableRoutes,
    isEstimatedFallback,
    onSelectRoute,
    originLat,
    originLng,
    originName,
    selectedLegs,
    selectedRoute,
  ]);

  const routeLabel = `${selectedRoute?.name ?? 'Journey route'} from ${originName ?? 'origin'} to ${destinationName ?? 'destination'}`;
  const selectedCostBreakdown = selectedRoute?.routing_cost_breakdown;

  return (
    <figure
      aria-label="Interactive route preview"
      className="route-preview-map-figure"
    >
      <div
        id="route-preview-map"
        className="route-preview-map-canvas"
        ref={containerRef}
        role="region"
        aria-label={routeLabel}
      />

      {selectedRoute ? (
        <aside className="map-radar-card route-map-radar-card" aria-label="Selected route radar">
          <div className="map-radar-card__heading route-map-radar-heading">
            <Route aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-cyan)" />
            <div>
              <strong>{routeTagLabel(selectedRoute.name)}</strong>
            </div>
          </div>

          <div
            className="map-radar-card__legend route-map-radar-metrics"
            aria-label={`${selectedRoute.distance_km} kilometres, ${selectedRoute.estimated_travel_time_mins} minutes`}
          >
            <span className="map-radar-card__legend-item route-map-radar-metric-distance">
              <Route aria-hidden="true" size={ICON_SIZE.small} /> {selectedRoute.distance_km} km
            </span>
            <span className="map-radar-card__legend-item route-map-radar-metric-time">
              <Clock aria-hidden="true" size={ICON_SIZE.small} /> {selectedRoute.estimated_travel_time_mins} min
            </span>
          </div>

          {selectedCostBreakdown ? (
            <details className="route-map-radar-weighting">
              <summary>How this route was weighted</summary>
              <div>
                <dl>
                  <dt>Free-flow road time</dt>
                  <dd>{selectedCostBreakdown.free_flow_mins} min</dd>
                  <dt>Congestion delay</dt>
                  <dd>+{selectedCostBreakdown.congestion_delay_mins} min</dd>
                  <dt>Weather delay</dt>
                  <dd>+{selectedCostBreakdown.weather_delay_mins} min</dd>
                  <dt>Turns, short segments &amp; signals</dt>
                  <dd>+{selectedCostBreakdown.maneuver_delay_mins} min</dd>
                  <dt>Road-suitability preference</dt>
                  <dd>+{selectedCostBreakdown.road_suitability_penalty_mins} cost min</dd>
                </dl>
              </div>
            </details>
          ) : null}
        </aside>
      ) : null}

      {drawableRoutes.length > 1 && (
        <span
          aria-label="Select a route shown on the map"
          className="route-map-route-tags"
          role="group"
        >
          {drawableRoutes.map((route, index) => {
            const isSelected = route.id === selectedRoute?.id;
            return (
              <button
                key={route.id}
                type="button"
                className="badge route-map-route-option"
                aria-pressed={isSelected}
                onClick={() => onSelectRoute?.(route.id)}
              >
                <span
                  aria-hidden="true"
                  className="route-map-route-swatch"
                  style={{
                    borderTop: `4px ${isSelected ? 'solid' : 'dashed'} ${routeColor(index, isSelected)}`,
                  }}
                />
                {routeTagLabel(route.name)}
              </button>
            );
          })}
        </span>
      )}
    </figure>
  );
}
