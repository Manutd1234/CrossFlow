import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import L from 'leaflet';
import { Check, Crosshair, LoaderCircle, MapPin, Search } from 'lucide-react';
import { FreeLocation } from '../../types';
import { geocodeQuery, reverseGeocode } from '../../services/api';
import { ICON_SIZE } from '../../theme/iconSizes';
import {
  CORRIDOR_MAP_BOUNDS,
  isSupportedLocation,
  locationRegion,
} from '../../services/crossBorderRouting';

interface WorldMapPickerModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSelectLocation: (loc: FreeLocation) => void;
  title?: string;
  initialLat?: number;
  initialLng?: number;
  initialName?: string;
}

const CORRIDOR_BOUNDS: L.LatLngBoundsExpression = [
  [CORRIDOR_MAP_BOUNDS.south, CORRIDOR_MAP_BOUNDS.west],
  [CORRIDOR_MAP_BOUNDS.north, CORRIDOR_MAP_BOUNDS.east],
];

function corridorPoint(lat: number, lng: number): { lat: number; lng: number } {
  return {
    lat: Math.min(CORRIDOR_MAP_BOUNDS.north, Math.max(CORRIDOR_MAP_BOUNDS.south, lat)),
    lng: Math.min(CORRIDOR_MAP_BOUNDS.east, Math.max(CORRIDOR_MAP_BOUNDS.west, lng)),
  };
}

function pickerPin(): HTMLDivElement {
  const pin = document.createElement('div');
  pin.textContent = '📍';
  pin.setAttribute('aria-hidden', 'true');
  Object.assign(pin.style, {
    alignItems: 'center',
    background: 'linear-gradient(135deg, #06b6d4, #2563eb)',
    border: '3px solid #ffffff',
    borderRadius: '50%',
    boxShadow: '0 5px 16px rgba(15, 23, 42, 0.36)',
    color: '#ffffff',
    display: 'flex',
    fontSize: '16px',
    height: '36px',
    justifyContent: 'center',
    width: '36px',
  } satisfies Partial<CSSStyleDeclaration>);
  return pin;
}

export const WorldMapPickerModal: React.FC<WorldMapPickerModalProps> = ({
  isOpen,
  onClose,
  onSelectLocation,
  title = 'Pick a Singapore or Batam location',
  initialLat = 1.12,
  initialLng = 104.02,
  initialName,
}) => {
  const dialogRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const markerRef = useRef<L.Marker | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  const searchInputId = useId();

  const [selectedCoords, setSelectedCoords] = useState<{ lat: number; lng: number }>({
    lat: initialLat,
    lng: initialLng,
  });
  const [placeName, setPlaceName] = useState<string>('Selected Location');
  const [loading, setLoading] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [isRoutable, setIsRoutable] = useState(true);
  const [pickerReady, setPickerReady] = useState(false);
  const reverseRequestRef = useRef(0);
  const searchRequestRef = useRef(0);

  useEffect(() => {
    if (!isOpen) return;

    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const dialog = dialogRef.current;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }

      if (event.key !== 'Tab' || !dialog) return;

      const focusableElements = Array.from(dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));

      if (focusableElements.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }

      const firstElement = focusableElements[0];
      const lastElement = focusableElements[focusableElements.length - 1];
      const activeElement = document.activeElement;
      const focusIsOutside = !activeElement || !dialog.contains(activeElement);

      if (event.shiftKey && (activeElement === firstElement || focusIsOutside)) {
        event.preventDefault();
        lastElement.focus();
      } else if (!event.shiftKey && (activeElement === lastElement || focusIsOutside)) {
        event.preventDefault();
        firstElement.focus();
      }
    };

    const focusFrame = window.requestAnimationFrame(() => dialog?.focus());
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, [isOpen, onClose]);

  const fetchReverseName = useCallback(async (lat: number, lng: number) => {
    const requestId = ++reverseRequestRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await reverseGeocode(lat, lng);
      if (requestId !== reverseRequestRef.current) return;
      const region = res.supported_region ?? locationRegion(lat, lng);
      // ``undefined`` means reverse-geocoding itself was offline; the route
      // endpoint still has its deterministic browser fallback. Explicit null
      // means the backend measured the point outside Batam graph coverage.
      const batamRoutable = region === 'BATAM' && res.node_id !== null;
      if (region !== 'SINGAPORE' && !batamRoutable) {
        setIsRoutable(false);
        setPlaceName(`Unroutable point (${lat.toFixed(5)}, ${lng.toFixed(5)})`);
        setError('Choose a supported Singapore point or a point closer to a drivable Batam road.');
        return;
      }
      // Keep the clicked/search coordinate as the requested endpoint. The
      // route result visualizes any short connector to the routable OSM node.
      setSelectedCoords({ lat, lng });
      markerRef.current?.setLatLng([lat, lng]);
      setIsRoutable(true);
      setPlaceName(res.display_name || `Road point (${lat.toFixed(5)}, ${lng.toFixed(5)})`);
    } catch {
      if (requestId !== reverseRequestRef.current) return;
      setPlaceName(`Road point (${lat.toFixed(5)}, ${lng.toFixed(5)})`);
      setIsRoutable(true);
      setError('The address could not be resolved, but these supported corridor coordinates are still usable.');
    } finally {
      if (requestId === reverseRequestRef.current) setLoading(false);
    }
  }, []);

  // Initialize Leaflet map inside modal
  useEffect(() => {
    if (!isOpen || !containerRef.current) return;

    const initialPoint = isSupportedLocation(initialLat, initialLng)
      ? { lat: initialLat, lng: initialLng }
      : { lat: 1.12, lng: 104.02 };
    setSelectedCoords(initialPoint);
    setPlaceName(
      (isSupportedLocation(initialLat, initialLng) && initialName?.trim())
        || `Road point (${initialPoint.lat.toFixed(5)}, ${initialPoint.lng.toFixed(5)})`,
    );
    setSearchQuery('');
    setError(null);
    setLoading(false);
    setIsSearching(false);
    setIsRoutable(true);
    setPickerReady(false);

    const map = L.map(containerRef.current, {
      zoomControl: false,
      scrollWheelZoom: true,
      minZoom: 9,
      maxBounds: CORRIDOR_BOUNDS,
      maxBoundsViscosity: 1,
    }).setView([initialPoint.lat, initialPoint.lng], 12);
    L.control.zoom({ position: 'topright' }).addTo(map);

    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
      maxZoom: 19,
    }).addTo(map);

    // Create draggable pin marker
    const pinIcon = L.divIcon({
      className: 'world-picker-pin',
      html: pickerPin(),
      iconSize: [36, 36],
      iconAnchor: [18, 18],
    });

    const marker = L.marker([initialPoint.lat, initialPoint.lng], {
      icon: pinIcon,
      draggable: true,
    }).addTo(map);

    mapRef.current = map;
    markerRef.current = marker;
    setPickerReady(true);

    // Handle map click
    map.on('click', (e: L.LeafletMouseEvent) => {
      const { lat, lng } = e.latlng;
      searchRequestRef.current += 1;
      setIsSearching(false);
      marker.setLatLng([lat, lng]);
      setSelectedCoords({ lat, lng });
      setPlaceName(`Road point (${lat.toFixed(5)}, ${lng.toFixed(5)})`);
      setIsRoutable(false);
      void fetchReverseName(lat, lng);
    });

    // Handle marker drag
    marker.on('dragend', () => {
      const pos = marker.getLatLng();
      const point = corridorPoint(pos.lat, pos.lng);
      searchRequestRef.current += 1;
      setIsSearching(false);
      marker.setLatLng([point.lat, point.lng]);
      setSelectedCoords(point);
      setPlaceName(`Road point (${point.lat.toFixed(5)}, ${point.lng.toFixed(5)})`);
      setIsRoutable(false);
      void fetchReverseName(point.lat, point.lng);
    });

    const resizeTimer = window.setTimeout(() => map.invalidateSize(), 200);
    return () => {
      window.clearTimeout(resizeTimer);
      reverseRequestRef.current += 1;
      searchRequestRef.current += 1;
      setPickerReady(false);
      map.remove();
      mapRef.current = null;
      markerRef.current = null;
    };
  }, [fetchReverseName, initialLat, initialLng, initialName, isOpen]);

  const handleSearchSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;
    const requestId = ++searchRequestRef.current;
    reverseRequestRef.current += 1;
    setLoading(false);
    setIsSearching(true);
    setError(null);
    try {
      const res = await geocodeQuery(searchQuery, 1);
      if (requestId !== searchRequestRef.current) return;
      if (res && res.length > 0) {
        const item = res[0];
        if (
          !isSupportedLocation(item.lat, item.lng)
          || (
            (item.supported_region ?? locationRegion(item.lat, item.lng)) === 'BATAM'
            && typeof item.node_id !== 'number'
          )
        ) {
          setError('That result is outside the supported Singapore-Batam corridor.');
          return;
        }
        const lat = item.lat;
        const lng = item.lng;
        setSelectedCoords({ lat, lng });
        setIsRoutable(true);
        setPlaceName(item.display_name);
        if (mapRef.current) mapRef.current.setView([lat, lng], 14);
        if (markerRef.current) markerRef.current.setLatLng([lat, lng]);
      } else {
        setError('No Singapore or Batam place matched that search.');
      }
    } catch (err) {
      if (requestId !== searchRequestRef.current) return;
      console.error('Picker search failed:', err);
      setError('The Singapore-Batam place search is temporarily unavailable.');
    } finally {
      if (requestId === searchRequestRef.current) setIsSearching(false);
    }
  };

  const handleConfirm = () => {
    if (!pickerReady || loading || isSearching || !isRoutable) return;
    onSelectLocation({
      lat: selectedCoords.lat,
      lng: selectedCoords.lng,
      display_name: placeName,
      supported_region: locationRegion(selectedCoords.lat, selectedCoords.lng),
    });
    onClose();
  };

  if (!isOpen) return null;

  return createPortal((
    <div
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      zIndex: 9999,
      background: 'rgba(15, 23, 42, 0.75)',
      backdropFilter: 'blur(8px)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 'clamp(8px, 2.5vw, 24px)',
    }}
    >
      <div
        ref={dialogRef}
        className="glass-panel world-map-picker__dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        style={{
        width: '100%',
        maxWidth: '920px',
        height: 'min(88vh, 780px)',
        maxHeight: 'calc(100vh - 16px)',
        display: 'flex',
        flexDirection: 'column',
        gap: '16px',
        padding: '16px',
        borderRadius: 'var(--radius-lg)',
        overflow: 'hidden',
        boxShadow: '0 25px 50px -12px rgba(0,0,0,0.5)',
        background: 'var(--warm-card)',
        border: '1px solid var(--border-color)',
        }}
      >
        <header className="world-map-picker__header" style={{
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
        }}>
          <div style={{ alignItems: 'center', display: 'flex', gap: '10px', minWidth: 0 }}>
            <span aria-hidden="true" style={{ alignItems: 'center', background: 'var(--tint-cyan-strong)', borderRadius: '10px', display: 'flex', flex: '0 0 auto', height: '38px', justifyContent: 'center', width: '38px' }}>
              <MapPin size={ICON_SIZE.big} color="var(--accent-cyan)" />
            </span>
            <div style={{ minWidth: 0 }}>
              <h3 id={titleId} style={{ fontSize: '1.05rem', fontWeight: 750, color: 'var(--text-primary)', margin: 0 }}>
                {title}
              </h3>
              <p id={descriptionId} style={{ color: 'var(--text-muted)', fontSize: '0.72rem', margin: '2px 0 0' }}>
                Search, click, or drag the pin anywhere in Singapore or near a routable Batam road.
              </p>
            </div>
          </div>
        </header>

        <form className="world-map-picker__search-form" role="search" onSubmit={handleSearchSubmit}>
            <div style={{ flex: 1, position: 'relative', display: 'flex', alignItems: 'center' }}>
              <label htmlFor={searchInputId} style={{ position: 'absolute', width: '1px', height: '1px', padding: 0, margin: '-1px', overflow: 'hidden', clip: 'rect(0, 0, 0, 0)', whiteSpace: 'nowrap', border: 0 }}>
                Search for a Singapore or Batam place
              </label>
              {isSearching
                ? <LoaderCircle className="world-map-picker__search-spinner" size={ICON_SIZE.medium} aria-hidden="true" color="var(--text-muted)" style={{ position: 'absolute', left: '12px' }} />
                : <Search size={ICON_SIZE.medium} aria-hidden="true" color="var(--text-muted)" style={{ position: 'absolute', left: '12px' }} />}
              <input
                className="world-map-picker__search-input"
                id={searchInputId}
                type="text"
                aria-busy={isSearching}
                value={searchQuery}
                onChange={(e) => {
                  searchRequestRef.current += 1;
                  setIsSearching(false);
                  setSearchQuery(e.target.value);
                  setError(null);
                }}
                onKeyDown={(event) => {
                  if (event.key !== 'Enter' || event.nativeEvent.isComposing) return;
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }}
                placeholder="Try Raffles Place, Changi, Nagoya Hill, or Sekupang…"
                style={{
                  width: '100%',
                  minHeight: '44px',
                  minWidth: '220px',
                  padding: '10px 12px 10px 36px',
                  borderRadius: '8px',
                  border: '1px solid var(--border-color)',
                  color: 'var(--text-primary)',
                  fontSize: '0.85rem',
                  outline: 'none',
                }}
              />
            </div>
            <span className="visually-hidden" role="status" aria-live="polite">
              {isSearching ? 'Searching Singapore and Batam places' : ''}
            </span>
          {error && (
            <p role="alert" style={{ margin: '8px 0 0', color: 'var(--accent-rose)', fontSize: '0.75rem' }}>
              {error}
            </p>
          )}
        </form>

        <div className="world-map-picker__map" style={{ flex: 1, minHeight: '260px', position: 'relative', width: '100%' }}>
          <div className="world-map-picker__map-canvas" ref={containerRef} role="region" aria-label="Interactive Singapore and Batam map. Click or drag the marker to choose a route endpoint." style={{ width: '100%', height: '100%' }} />

          {/* Floating instructions pill */}
          <div style={{
            position: 'absolute',
            top: '12px',
            left: '12px',
            zIndex: 1000,
            background: 'rgba(255, 255, 255, 0.94)',
            backdropFilter: 'blur(6px)',
            padding: '8px 11px',
            borderRadius: '10px',
            fontSize: '0.78rem',
            fontWeight: 600,
            color: 'var(--text-primary)',
            boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}>
            <Crosshair size={ICON_SIZE.big} aria-hidden="true" color="var(--accent-cyan)" />
            Click the map or drag the pin
          </div>
        </div>

        <footer className="world-map-picker__footer" style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: '12px',
        }}>
          <dl aria-live="polite" style={{ margin: 0, minWidth: 0 }}>
            <dt style={{ fontSize: '0.68rem', fontWeight: 750, color: 'var(--text-muted)', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              Selected route point
            </dt>
            <dd style={{ fontSize: '0.88rem', fontWeight: 700, color: 'var(--text-primary)', margin: '2px 0 0', maxWidth: '520px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {loading ? 'Resolving the nearest road…' : placeName}
            </dd>
            <dd style={{ fontFamily: 'monospace', fontSize: '0.7rem', color: 'var(--accent-cyan)', margin: '2px 0 0' }}>
              {selectedCoords.lat.toFixed(6)}, {selectedCoords.lng.toFixed(6)}
            </dd>
          </dl>

          <div className="world-map-picker__actions">
            <button
              type="button"
              className="world-map-picker__cancel"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="ui-button-primary world-map-picker__confirm"
              onClick={handleConfirm}
              disabled={!pickerReady || loading || isSearching || !isRoutable}
            >
              <Check size={ICON_SIZE.large} aria-hidden="true" /> {loading ? 'Resolving road…' : 'Use this location'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  ), document.body);
};
