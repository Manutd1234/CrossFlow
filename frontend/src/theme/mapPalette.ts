/**
 * Shared map colors live here because Leaflet and Recharts receive literal
 * colors rather than resolving the application's CSS custom properties.
 * Backend-supplied hotspot colors intentionally remain authoritative.
 */
export const TRAFFIC_COLORS = {
  smooth: '#10B981',
  heavy: '#F59E0B',
  critical: '#EF4444',
} as const;

export const MAP_PALETTE = {
  route: {
    selected: '#1A73E8',
    casing: '#FFFFFF',
    connector: '#64748B',
    alternatives: ['#64748B', '#6138A8', '#A64B1A', '#116B4B'],
  },
  endpoint: {
    origin: '#116B4B',
    destination: '#B93E52',
  },
  node: {
    batam: '#006E78',
    singapore: '#6138A8',
    ferry: '#6138A8',
  },
  maneuver: {
    roundabout: '#6138A8',
    trafficLight: '#A64B1A',
  },
  traffic: TRAFFIC_COLORS,
  emissionsPressure: {
    high: '#7E225B',
  },
} as const;
