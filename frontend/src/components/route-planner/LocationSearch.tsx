import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { ChevronDown, Globe, Loader, MapPin, Search, X } from 'lucide-react';
import { FreeLocation, GeocodedLocation, RouteLocation } from '../../types';
import { geocodeQuery } from '../../services/api';
import { isSupportedLocation, locationRegion } from '../../services/crossBorderRouting';
import { ICON_SIZE } from '../../theme/iconSizes';

interface LocationSearchProps {
  /** Label shown above the field (e.g. "FROM" / "TO") */
  label: string;
  /** Current value – either a free location or a named place */
  value: FreeLocation | RouteLocation | null;
  onChange: (loc: FreeLocation | null) => void;
  /** Named location fallback list (shown when API is offline) */
  namedLocations: RouteLocation[];
  /** Accent colour for the dot marker */
  markerColor?: 'cyan' | 'rose';
  placeholder?: string;
  id?: string;
  onOpenMapPicker?: () => void;
  showMapPickerButton?: boolean;
  showSearchButton?: boolean;
  showHelpText?: boolean;
  compactLayout?: boolean;
  savedPlacesOnly?: boolean;
  onNamedLocationSelect?: (loc: RouteLocation) => void;
  ariaDescribedBy?: string;
  invalid?: boolean;
}

type KeyboardOption =
  | { id: string; kind: 'map-picker'; disabled: false }
  | { id: string; kind: 'current-location'; disabled: boolean }
  | { id: string; kind: 'geocoded'; disabled: false; location: GeocodedLocation }
  | { id: string; kind: 'named'; disabled: false; location: RouteLocation };

function displayOf(v: FreeLocation | RouteLocation | null): string {
  if (!v) return '';
  if ('category' in v) return v.name;
  return v.display_name;
}

// Shorten a Nominatim display_name for compact rendering.
function shorten(displayName: string, maxParts = 3): string {
  return displayName.split(',').slice(0, maxParts).join(',').trim();
}

export const LocationSearch: React.FC<LocationSearchProps> = ({
  label, value, onChange, namedLocations,
  showMapPickerButton = true, showSearchButton = true, showHelpText = true,
  compactLayout = false, savedPlacesOnly = false,
  markerColor = 'cyan', placeholder = 'Search Singapore or Batam…', id, onOpenMapPicker,
  onNamedLocationSelect, ariaDescribedBy, invalid = false,
}) => {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<GeocodedLocation[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isOpen, setIsOpen] = useState(false);
  const [activeOptionId, setActiveOptionId] = useState<string | null>(null);
  const [draftValue, setDraftValue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const searchRequestRef = useRef(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const generatedId = useId();
  const inputId = id ?? `location-${generatedId}`;
  const listboxId = `${inputId}-options`;
  const labelId = `${inputId}-label`;
  const helpId = `${inputId}-help`;
  const statusId = `${inputId}-status`;

  const accentColor = markerColor === 'rose' ? 'var(--accent-rose)' : 'var(--accent-cyan)';
  const tintColor = markerColor === 'rose' ? 'rgba(244,63,94,0.12)' : 'var(--tint-cyan-strong)';

  const closeSuggestions = useCallback(() => {
    setIsOpen(false);
    setActiveOptionId(null);
  }, []);

  // Close dropdown when clicking outside.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        closeSuggestions();
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [closeSuggestions]);

  const inputValue = draftValue ?? displayOf(value);

  useEffect(() => () => {
    searchRequestRef.current += 1;
  }, []);

  const runSearch = useCallback(async (q: string) => {
    const cleaned = q.trim();
    setActiveOptionId(null);
    if (cleaned.length === 0) {
      setResults([]);
      setIsOpen(true);
      return;
    }
    const requestId = ++searchRequestRef.current;
    setIsLoading(true);
    setError(null);
    try {
      const res = await geocodeQuery(cleaned, 6);
      if (requestId !== searchRequestRef.current) return;
      const localResults = res.filter((loc) => {
        const region = loc.supported_region ?? locationRegion(loc.lat, loc.lng);
        if (region === 'SINGAPORE') return true;
        return region === 'BATAM'
          && typeof loc.node_id === 'number'
          && isSupportedLocation(loc.snapped_lat, loc.snapped_lng);
      });
      setResults(localResults);
      if (localResults.length === 0) {
        setError('No supported Singapore or Batam place matched that search.');
      }
      setIsOpen(true);
    } catch {
      if (requestId !== searchRequestRef.current) return;
      setResults([]);
      setError('Place search is temporarily unavailable. Choose a suggested place or use the map picker.');
      setIsOpen(true);
    } finally {
      if (requestId === searchRequestRef.current) setIsLoading(false);
    }
  }, []);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setDraftValue(q);
    setQuery(q);
    setError(null);
    setResults([]);
    setActiveOptionId(null);
    searchRequestRef.current += 1;
    setIsLoading(false);
    onChange(null);
  };

  const handleSelect = (loc: GeocodedLocation) => {
    const region = loc.supported_region ?? locationRegion(loc.lat, loc.lng);
    const batamRoutable = region === 'BATAM'
      && typeof loc.node_id === 'number'
      && isSupportedLocation(loc.snapped_lat, loc.snapped_lng);
    if (region !== 'SINGAPORE' && !batamRoutable) {
      setError('That result is outside the supported Singapore-Batam corridor.');
      return;
    }
    const free: FreeLocation = {
      // Preserve the place the user chose. The routing response separately
      // reports the access distance to its nearest graph node.
      lat: loc.lat,
      lng: loc.lng,
      display_name: shorten(loc.display_name),
      node_id: loc.node_id,
      supported_region: region,
    };
    onChange(free);
    setDraftValue(null);
    setQuery('');
    setError(null);
    closeSuggestions();
  };

  const handleNamedSelect = (loc: RouteLocation) => {
    if (onNamedLocationSelect) {
      onNamedLocationSelect(loc);
    } else {
      onChange({ lat: loc.lat, lng: loc.lng, display_name: loc.name });
    }
    setDraftValue(null);
    setQuery('');
    setError(null);
    closeSuggestions();
  };

  const handleUseCurrentLocation = () => {
    setActiveOptionId(null);
    if ('geolocation' in navigator) {
      setIsLoading(true);
      navigator.geolocation.getCurrentPosition(
        pos => {
          setIsLoading(false);
          const { latitude, longitude } = pos.coords;
          const region = locationRegion(latitude, longitude);
          if (!region) {
            setError('Your current location is outside the supported Singapore-Batam corridor.');
            return;
          }
          const displayName = 'My Current Location';
          onChange({
            lat: latitude,
            lng: longitude,
            display_name: displayName,
            supported_region: region,
          });
          setDraftValue(null);
          setQuery('');
          setError(null);
          closeSuggestions();
        },
        () => {
          setIsLoading(false);
          setError('Location permission was denied. Search for a Singapore or Batam place, or pick it on the map.');
        }
      );
    } else {
      setError('This browser does not provide location access. Search or use the map picker instead.');
    }
  };

  const handleClear = () => {
    setDraftValue(null);
    setQuery('');
    setResults([]);
    setError(null);
    searchRequestRef.current += 1;
    onChange(null);
    closeSuggestions();
  };

  const handleMapPickerSelect = () => {
    setDraftValue(null);
    setQuery('');
    setResults([]);
    setError(null);
    closeSuggestions();
    onOpenMapPicker?.();
  };

  // Memoize the local POI fallback so typing does not rebuild and deduplicate it.
  const uniquePlaces = useMemo(() => {
    if (savedPlacesOnly) return namedLocations;

    const extendedPlaces: RouteLocation[] = [
    ...namedLocations,
    // Cafes & Bakeries
    { id: 'morning_bakery_bc', name: 'Morning Bakery Batam Centre', category: '☕ Cafe & Bakery', lat: 1.1305, lng: 104.0535 },
    { id: 'morning_bakery_nagoya', name: 'Morning Bakery Nagoya Hill', category: '☕ Cafe & Bakery', lat: 1.1445, lng: 104.0140 },
    { id: 'morning_bakery_tiban', name: 'Morning Bakery Tiban Centre', category: '☕ Cafe & Bakery', lat: 1.0985, lng: 103.9605 },
    { id: 'anchor_cafe', name: 'Anchor Cafe & Roastery Sukajadi', category: '☕ Specialty Coffee', lat: 1.0810, lng: 104.0320 },
    { id: 'starbucks_nagoya', name: 'Starbucks Nagoya Hill Mall', category: '☕ Cafe & Coffee', lat: 1.1460, lng: 104.0130 },
    { id: 'starbucks_grand_batam', name: 'Starbucks Grand Batam Mall', category: '☕ Cafe & Coffee', lat: 1.1415, lng: 104.0090 },
    { id: 'common_grounds', name: 'Common Grounds Coffee Nagoya', category: '☕ Cafe & Coffee', lat: 1.1435, lng: 104.0115 },
    { id: 'smith_bakery', name: 'The Smith Bakery Batam Centre', category: '☕ Bakery & Pastry', lat: 1.1290, lng: 104.0515 },
    { id: 'socialite_kopi', name: 'Socialite Kopi & Bistro Sukajadi', category: '☕ Cafe & Bistro', lat: 1.0825, lng: 104.0335 },
    { id: 'fore_coffee', name: 'Fore Coffee Nagoya', category: '☕ Coffee Shop', lat: 1.1440, lng: 104.0120 },
    { id: 'excelso_mega_mall', name: 'Excelso Coffee Mega Mall', category: '☕ Cafe & Dining', lat: 1.1312, lng: 104.0538 },
    { id: 'la_kopi_penuin', name: 'La Kopi Penuin Kopitiam', category: '☕ Traditional Kopitiam', lat: 1.1405, lng: 104.0075 },

    // Shophouses (Ruko) & Commercial Districts
    { id: 'ruko_nagoya_hill', name: 'Ruko Nagoya Hill Commercial Complex', category: '🏬 Shophouse & Retail', lat: 1.1455, lng: 104.0135 },
    { id: 'ruko_batam_centre', name: 'Ruko Batam Centre City Avenue', category: '🏬 Shophouse & Office', lat: 1.1295, lng: 104.0525 },
    { id: 'ruko_panbil', name: 'Ruko Panbil Commercial Hub', category: '🏬 Shophouse & Banking', lat: 1.0725, lng: 104.0245 },
    { id: 'ruko_sukajadi', name: 'Ruko Sukajadi Business Park', category: '🏬 Shophouse & Dining', lat: 1.0830, lng: 104.0315 },
    { id: 'ruko_tiban_mas', name: 'Ruko Tiban Mas Commercial Centre', category: '🏬 Shophouse & Retail', lat: 1.0995, lng: 103.9620 },
    { id: 'ruko_harbour_bay', name: 'Ruko Harbour Bay Bayfront District', category: '🏬 Shophouse & Seafood', lat: 1.1542, lng: 103.9980 },
    { id: 'ruko_penuin', name: 'Ruko Penuin Market Square', category: '🏬 Shophouse & Market', lat: 1.1410, lng: 104.0080 },
    { id: 'ruko_botania2', name: 'Ruko Botania 2 Commercial Center', category: '🏬 Shophouse & Market', lat: 1.1230, lng: 104.0980 },

    // Supermarkets, Malls & Stores
    { id: 'grand_batam_mall', name: 'Grand Batam Mall Nagoya', category: '🛒 Shopping Mall & Retail', lat: 1.1410, lng: 104.0085 },
    { id: 'nagoya_hill_mall', name: 'Nagoya Hill Shopping Mall', category: '🛒 Shopping Mall & Retail', lat: 1.1465, lng: 104.0125 },
    { id: 'bcs_mall', name: 'BCS Mall (Batam City Square)', category: '🛒 Shopping Mall & Retail', lat: 1.1375, lng: 104.0105 },
    { id: 'mega_mall_bc', name: 'Mega Mall Batam Centre', category: '🛒 Shopping Mall & Retail', lat: 1.1310, lng: 104.0540 },
    { id: 'panbil_mall', name: 'Panbil Mall & Supermarket', category: '🛒 Mall & Supermarket', lat: 1.0721, lng: 104.0235 },
    { id: 'diamond_supermarket', name: 'Diamond Supermarket City Walk', category: '🛒 Supermarket & Grocery', lat: 1.1450, lng: 104.0120 },
    { id: 'top100_nagoya', name: 'Top 100 Supermarket Nagoya', category: '🛒 Supermarket & Grocery', lat: 1.1435, lng: 104.0145 },
    { id: 'top100_penuin', name: 'Top 100 Supermarket Penuin', category: '🛒 Supermarket & Grocery', lat: 1.1400, lng: 104.0070 },
    { id: 'indomaret_bc', name: 'Indomaret Batam Centre Terminal', category: '🛒 Store & Convenience', lat: 1.1315, lng: 104.0550 },
    { id: 'alfamart_sukajadi', name: 'Alfamart Sukajadi Boulevard', category: '🛒 Store & Convenience', lat: 1.0840, lng: 104.0325 },

    // Hotels & Resorts
    { id: 'marriott_harbour_bay', name: 'Marriott Hotel Harbour Bay', category: '🏨 5-Star Hotel & Resort', lat: 1.1550, lng: 103.9975 },
    { id: 'radisson_batam', name: 'Radisson Golf & Convention Center', category: '🏨 5-Star Hotel & Resort', lat: 1.0965, lng: 104.0340 },
    { id: 'best_western_panbil', name: 'Best Western Premier Panbil', category: '🏨 4-Star Hotel & Suites', lat: 1.0735, lng: 104.0240 },
    { id: 'swissbel_harbourbay', name: 'Swiss-Belhotel Harbour Bay', category: '🏨 4-Star Hotel', lat: 1.1535, lng: 103.9968 },
    { id: 'aston_batam', name: 'Aston Batam Hotel & Residence', category: '🏨 Hotel & Residence', lat: 1.1380, lng: 104.0160 },
    { id: 'montigo_resorts', name: 'Montigo Resorts Nongsa Villas', category: '🏨 Luxury Resort & Villas', lat: 1.1920, lng: 104.1080 },
    { id: 'turi_beach', name: 'Turi Beach Resort Nongsa', category: '🏨 Beach Resort & Spa', lat: 1.1870, lng: 104.1150 },
    { id: 'harris_barelang', name: 'Harris Resort Barelang Bridge 1', category: '🏨 Resort & Spa', lat: 1.0020, lng: 104.0410 },
    { id: 'harris_batam_centre', name: 'Harris Hotel Batam Centre', category: '🏨 Hotel & Convention', lat: 1.1300, lng: 104.0530 },

    // Hospitals & Clinics
    { id: 'awal_bros', name: 'RS Awal Bros Batam Hospital', category: '🏥 General Hospital', lat: 1.1180, lng: 104.0220 },
    { id: 'elisabeth_nagoya', name: 'RS Elisabeth Nagoya Hospital', category: '🏥 General Hospital', lat: 1.1440, lng: 104.0100 },
    { id: 'elisabeth_batam_kota', name: 'RS Elisabeth Batam Kota', category: '🏥 General Hospital', lat: 1.1210, lng: 104.0450 },
    { id: 'budi_kemuliaan', name: 'RS Budi Kemuliaan Seraya', category: '🏥 General Hospital', lat: 1.1500, lng: 104.0180 },
    { id: 'rsbp_sekupang', name: 'RS Otorita Batam Sekupang (RSBP)', category: '🏥 Regional Hospital', lat: 1.1190, lng: 103.9310 },
    { id: 'klinik_sukajadi', name: 'Klinik Utama Sukajadi', category: '🏥 Medical Clinic', lat: 1.0820, lng: 104.0310 },

    // Restaurants & Food Courts
    { id: 'sederhana_padang_bc', name: 'Restoran Sederhana Padang Batam Centre', category: '🍽️ Indonesian Restaurant', lat: 1.1250, lng: 104.0450 },
    { id: 'sei_enam_seafood', name: 'Restoran Sei Enam Seafood Nagoya', category: '🍽️ Seafood Restaurant', lat: 1.1420, lng: 104.0130 },
    { id: 'rezeki_seafood', name: 'Rezeki Seafood Beach Restaurant Nongsa', category: '🍽️ Coastal Seafood', lat: 1.1760, lng: 104.1350 },
    { id: 'wey_wey_seafood', name: 'Wey Wey Seafood Harbour Bay', category: '🍽️ Bayfront Seafood', lat: 1.1545, lng: 103.9970 },
    { id: 'golden_prawn_933', name: 'Golden Prawn 933 Seafood Bengkong', category: '🍽️ Iconic Seafood', lat: 1.1680, lng: 104.0320 },
    { id: 'pujasera_nagoya', name: 'Pujasera Nagoya Food Court', category: '🍽️ Food Court & Hawker', lat: 1.1430, lng: 104.0110 },
    { id: 'a2_food_court', name: 'A2 Food Court Nagoya Hill', category: '🍽️ Food Court & Hawker', lat: 1.1450, lng: 104.0140 },
    { id: 'tiga_putri', name: 'Restoran Tiga Putri Nagoya', category: '🍽️ Dining & Restaurant', lat: 1.1430, lng: 104.0110 },

    // Batam terminals and ports
    { id: 'tiban_centre', name: 'Tiban Centre District & Terminal', category: '🚢 District & Transit Hub', lat: 1.0990, lng: 103.9610 },
    { id: 'terminal_batam_centre', name: 'Terminal Ferry Batam Centre', category: '🚢 Ferry Terminal', lat: 1.1318, lng: 104.0554 },
    { id: 'terminal_sekupang', name: 'Terminal Ferry Sekupang', category: '🚢 Ferry Terminal', lat: 1.1250, lng: 103.9250 },
    { id: 'terminal_harbourbay', name: 'Terminal Ferry Harbour Bay', category: '🚢 Ferry Terminal', lat: 1.1539, lng: 103.9972 },
    { id: 'terminal_nongsapura', name: 'Terminal Ferry Nongsapura', category: '🚢 Ferry Terminal', lat: 1.1890, lng: 104.1020 },
    { id: 'tanjung_uncang', name: 'Tanjung Uncang Shipyard Port', category: '🚢 Industry & Cargo Port', lat: 1.0620, lng: 103.9050 },
    { id: 'batu_ampar_port', name: 'Batu Ampar Freight Container Port', category: '🚢 Main Freight Port', lat: 1.1630, lng: 104.0025 },

    // Singapore terminals and major trip anchors
    { id: 'sg_harbourfront', name: 'HarbourFront Ferry Terminal Singapore', category: '🇸🇬 Ferry Terminal', lat: 1.2644, lng: 103.8206 },
    { id: 'sg_tanah_merah', name: 'Tanah Merah Ferry Terminal Singapore', category: '🇸🇬 Ferry Terminal', lat: 1.3143, lng: 103.9886 },
    { id: 'sg_cbd', name: 'Raffles Place Singapore CBD', category: '🇸🇬 Business District', lat: 1.2840, lng: 103.8513 },
    { id: 'sg_changi', name: 'Singapore Changi Airport', category: '🇸🇬 Airport & Logistics', lat: 1.3644, lng: 103.9915 },
    { id: 'sg_jurong_port', name: 'Jurong Port Singapore', category: '🇸🇬 Freight & Logistics Port', lat: 1.3143, lng: 103.7216 },
    { id: 'sg_tuas', name: 'Tuas Mega Port Singapore', category: '🇸🇬 Freight & Logistics Port', lat: 1.2514, lng: 103.6278 },
    { id: 'sg_woodlands', name: 'Woodlands Regional Centre Singapore', category: '🇸🇬 District & Transit Hub', lat: 1.4382, lng: 103.7890 },

    // Parks & Landmarks
    { id: 'taman_kota', name: 'Taman Kota Batam Centre Park', category: '📍 Park & Civic Landmark', lat: 1.1270, lng: 104.0510 },
    { id: 'barelang_bridge_1', name: 'Barelang Bridge 1 (Tengku Fisabilillah)', category: '📍 Iconic Landmark Bridge', lat: 1.0015, lng: 104.0415 },
    ];

    return Array.from(
      new Map(
        extendedPlaces
        .filter(place => isSupportedLocation(place.lat, place.lng))
        .map(place => [place.name.toLowerCase(), place]),
      ).values(),
    );
  }, [namedLocations, savedPlacesOnly]);

  const qLower = query.trim().toLowerCase();
  const filteredNamed = useMemo(() => qLower
    ? [...uniquePlaces]
        .filter(l => l.name.toLowerCase().includes(qLower) || l.category.toLowerCase().includes(qLower))
        .sort((a, b) => {
          const aStarts = a.name.toLowerCase().startsWith(qLower);
          const bStarts = b.name.toLowerCase().startsWith(qLower);
          if (aStarts && !bStarts) return -1;
          if (!aStarts && bStarts) return 1;
          return a.name.localeCompare(b.name);
        })
    : uniquePlaces,
  [qLower, uniquePlaces]);
  const visibleNamed = useMemo(
    () => savedPlacesOnly ? filteredNamed : filteredNamed.slice(0, qLower ? 12 : 8),
    [filteredNamed, qLower, savedPlacesOnly],
  );

  const mapPickerOptionId = `${listboxId}-map-picker`;
  const currentLocationOptionId = `${listboxId}-current-location`;
  const geocodedOptionId = (index: number) => `${listboxId}-geocoded-${index}`;
  const namedOptionId = (index: number) => `${listboxId}-named-${index}`;
  const keyboardOptions: KeyboardOption[] = [];

  if (onOpenMapPicker) {
    keyboardOptions.push({ id: mapPickerOptionId, kind: 'map-picker', disabled: false });
  }
  if (!savedPlacesOnly) {
    keyboardOptions.push({ id: currentLocationOptionId, kind: 'current-location', disabled: isLoading });
  }
  results.forEach((location, index) => {
    keyboardOptions.push({ id: geocodedOptionId(index), kind: 'geocoded', disabled: false, location });
  });
  visibleNamed.forEach((location, index) => {
    keyboardOptions.push({ id: namedOptionId(index), kind: 'named', disabled: false, location });
  });
  const visibleActiveOptionId = keyboardOptions.some(
    option => option.id === activeOptionId && !option.disabled,
  ) ? activeOptionId : null;

  const moveActiveOption = (direction: 1 | -1) => {
    const enabledOptions = keyboardOptions.filter(option => !option.disabled);
    if (enabledOptions.length === 0) {
      setActiveOptionId(null);
      return;
    }

    const currentIndex = enabledOptions.findIndex(option => option.id === activeOptionId);
    const nextIndex = currentIndex === -1
      ? direction === 1 ? 0 : enabledOptions.length - 1
      : (currentIndex + direction + enabledOptions.length) % enabledOptions.length;
    setActiveOptionId(enabledOptions[nextIndex].id);
  };

  const selectKeyboardOption = (option: KeyboardOption) => {
    if (option.disabled) return;
    if (option.kind === 'map-picker') {
      handleMapPickerSelect();
    } else if (option.kind === 'current-location') {
      handleUseCurrentLocation();
    } else if (option.kind === 'geocoded') {
      handleSelect(option.location);
    } else {
      handleNamedSelect(option.location);
    }
  };

  useEffect(() => {
    if (!visibleActiveOptionId || !isOpen) return;
    const activeElement = document.getElementById(visibleActiveOptionId);
    if (typeof activeElement?.scrollIntoView === 'function') {
      activeElement.scrollIntoView({ block: 'nearest' });
    }
  }, [isOpen, visibleActiveOptionId]);

  return (
    <div
      ref={containerRef}
      role="group"
      aria-labelledby={labelId}
      className={compactLayout ? 'location-search location-search-compact' : 'location-search'}
      style={{ position: 'relative' }}
    >
      <div className="location-search-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
        <label
          id={labelId}
          htmlFor={inputId}
          style={{
            fontSize: '0.72rem',
            fontWeight: 700,
            color: 'var(--text-muted)',
            letterSpacing: '0.06em',
            display: 'block',
            textTransform: 'uppercase',
          }}
        >
          {label}
        </label>
        {onOpenMapPicker && showMapPickerButton && (
          <button
            type="button"
            onClick={handleMapPickerSelect}
            style={{
              background: 'var(--tint-cyan-strong)',
              border: '1px solid var(--accent-cyan)',
              color: 'var(--accent-cyan)',
              borderRadius: '12px',
              minHeight: '32px',
              padding: '5px 9px',
              fontSize: '0.7rem',
              fontWeight: 700,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <Globe size={ICON_SIZE.medium} aria-hidden="true" /> Pick on map
          </button>
        )}
      </div>

      <div
        role="search"
        style={{ display: 'flex', alignItems: 'stretch', gap: '8px' }}
      >
        <div style={{ position: 'relative', display: 'flex', alignItems: 'center', flex: 1, minWidth: 0 }}>
          <span
          className="location-search-input-marker"
          aria-hidden="true"
          style={{
            position: 'absolute',
            left: '12px',
            width: '10px',
            height: '10px',
            borderRadius: '50%',
            background: accentColor,
            boxShadow: `0 0 6px ${accentColor}`,
            flexShrink: 0,
            zIndex: 1,
          }}
          />

          <input
          id={inputId}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={isOpen}
          aria-controls={isOpen ? listboxId : undefined}
          aria-activedescendant={isOpen && visibleActiveOptionId ? visibleActiveOptionId : undefined}
          aria-describedby={[helpId, statusId, ariaDescribedBy].filter(Boolean).join(' ')}
          aria-invalid={error || invalid ? 'true' : undefined}
          aria-busy={isLoading}
          autoComplete="off"
          readOnly={savedPlacesOnly}
          value={inputValue}
          placeholder={placeholder}
          onFocus={() => {
            setIsOpen(true);
          }}
          onChange={handleInputChange}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              closeSuggestions();
              return;
            }
            if (event.key === 'ArrowDown') {
              event.preventDefault();
              setIsOpen(true);
              moveActiveOption(1);
              return;
            }
            if (event.key === 'ArrowUp') {
              event.preventDefault();
              setIsOpen(true);
              moveActiveOption(-1);
              return;
            }
            if (event.key === 'Enter') {
              const activeOption = keyboardOptions.find(option => option.id === activeOptionId && !option.disabled);
              if (activeOption) {
                event.preventDefault();
                selectKeyboardOption(activeOption);
                return;
              }
              if (savedPlacesOnly || isLoading || !inputValue.trim()) return;
              event.preventDefault();
              void runSearch(inputValue);
            }
          }}
          style={{
            width: '100%',
            minHeight: '46px',
            padding: '11px 44px 11px 32px',
            background: 'var(--surface-1)',
            border: `1px solid ${isOpen ? accentColor : 'var(--border-color)'}`,
            borderRadius: '10px',
            color: 'var(--text-primary)',
            fontSize: '0.88rem',
            outline: 'none',
            transition: 'border-color 0.2s',
          }}
          />

          <span style={{ position: 'absolute', right: '6px', color: 'var(--text-muted)', display: 'flex' }}>
            {savedPlacesOnly
              ? <span aria-hidden="true" style={{ alignItems: 'center', display: 'flex', height: '34px', justifyContent: 'center', width: '34px' }}><ChevronDown size={ICON_SIZE.medium} /></span>
              : inputValue
              ? <button type="button" onClick={handleClear} aria-label="Clear selected location" style={{ alignItems: 'center', background: 'none', border: 'none', borderRadius: '8px', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex', height: '34px', justifyContent: 'center', padding: 0, width: '34px' }}><X size={ICON_SIZE.medium} aria-hidden="true" /></button>
              : <span aria-hidden="true" style={{ alignItems: 'center', display: 'flex', height: '34px', justifyContent: 'center', width: '34px' }}><Search size={ICON_SIZE.large} /></span>
            }
          </span>
        </div>
        {showSearchButton && (
          <button
            type="button"
            className="ui-sand-interactive location-search-submit"
            onClick={() => void runSearch(inputValue)}
            disabled={isLoading || inputValue.trim().length === 0}
            aria-label="Search Singapore or Batam places"
            style={{
              minHeight: '46px', minWidth: '86px', padding: '0 12px', borderRadius: '10px',
              fontSize: '0.75rem', fontWeight: 700,
              cursor: isLoading || inputValue.trim().length === 0 ? 'not-allowed' : 'pointer',
              opacity: isLoading || inputValue.trim().length === 0 ? 0.55 : 1,
              display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '5px',
            }}
          >
          {isLoading ? <Loader size={ICON_SIZE.large} aria-hidden="true" style={{ animation: 'spin 1s linear infinite' }} /> : <Search size={ICON_SIZE.large} aria-hidden="true" />}
          {isLoading ? 'Finding…' : 'Search'}
        </button>
      )}
    </div>

    <p id={helpId} hidden={!showHelpText} style={{ color: 'var(--text-muted)', fontSize: '0.68rem', margin: '5px 0 0' }}>
      Singapore + Batam · road access and ferry transfer are composed automatically.
    </p>

    {error && (
      <p id={statusId} role="alert" style={{ margin: '5px 0 0', color: 'var(--accent-rose)', fontSize: '0.72rem', fontWeight: 600 }}>
        {error}
      </p>
    )}
    {!error && (
      <span id={statusId} role="status" aria-live="polite" style={{ position: 'absolute', width: '1px', height: '1px', padding: 0, margin: '-1px', overflow: 'hidden', clip: 'rect(0, 0, 0, 0)', whiteSpace: 'nowrap', border: 0 }}>
        {isLoading ? 'Searching Singapore and Batam places' : value ? `${displayOf(value)} selected` : 'No location selected'}
      </span>
    )}


    {/* Google Maps style Autocomplete Dropdown */}
    {isOpen && (
      <div
        id={listboxId}
        role="listbox"
        aria-label={`${label} suggestions`}
        aria-busy={isLoading}
        style={{
          position: 'absolute',
          top: 'calc(100% + 6px)',
          left: 0,
          right: 0,
          background: '#ffffff',
          border: '1px solid var(--border-color)',
          borderRadius: '12px',
          boxShadow: '0 10px 25px -5px rgba(0,0,0,0.12), 0 8px 10px -6px rgba(0,0,0,0.08)',
          zIndex: 200,
          overflow: 'hidden',
        }}
      >
          {isLoading && (
            <div role="status" style={{ alignItems: 'center', color: 'var(--text-secondary)', display: 'flex', fontSize: '0.78rem', gap: '8px', minHeight: '44px', padding: '9px 14px' }}>
              <Loader size={ICON_SIZE.big} aria-hidden="true" style={{ animation: 'spin 1s linear infinite' }} />
              Searching Singapore and Batam…
            </div>
          )}

          {/* Current Location & Map Picker options */}
          {onOpenMapPicker && (
            <button
              id={mapPickerOptionId}
              className="location-search-map-picker-option"
              type="button"
              role="option"
              aria-selected={activeOptionId === mapPickerOptionId}
              tabIndex={-1}
              onClick={handleMapPickerSelect}
              onMouseEnter={() => setActiveOptionId(mapPickerOptionId)}
              style={{
                width: '100%',
                minHeight: '44px',
                padding: '10px 14px',
                background: activeOptionId === mapPickerOptionId ? 'rgba(99, 102, 241, 0.16)' : 'rgba(99, 102, 241, 0.08)',
                border: 'none',
                borderBottom: '1px solid var(--border-color)',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                color: 'var(--accent-indigo)',
                fontSize: '0.82rem',
                fontWeight: 700,
                textAlign: 'left',
              }}
            >
              <Globe size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-indigo)" /> Choose a point in Singapore or Batam
            </button>
          )}

          {!savedPlacesOnly && <button
            id={currentLocationOptionId}
            type="button"
            role="option"
            aria-selected={activeOptionId === currentLocationOptionId}
            aria-disabled={isLoading}
            tabIndex={-1}
            onClick={handleUseCurrentLocation}
            onMouseEnter={() => {
              if (!isLoading) setActiveOptionId(currentLocationOptionId);
            }}
            disabled={isLoading}
            style={{
              width: '100%',
              minHeight: '44px',
              padding: '10px 14px',
              background: activeOptionId === currentLocationOptionId ? 'rgba(6, 182, 212, 0.14)' : 'rgba(6, 182, 212, 0.06)',
              border: 'none',
              borderBottom: '1px solid var(--border-color)',
              cursor: isLoading ? 'wait' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              color: 'var(--accent-cyan)',
              fontSize: '0.82rem',
              fontWeight: 600,
              textAlign: 'left',
            }}
          >
            <MapPin size={ICON_SIZE.medium} aria-hidden="true" color="var(--accent-cyan)" /> Use my current location
          </button>}

          {/* Live Geocoded Results */}
          {results.length > 0 && (
            <div role="group" aria-label="Matching addresses and places">
              <div style={{ padding: '6px 14px', fontSize: '0.65rem', fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.06em', background: '#f8fafc' }}>
                MATCHING ADDRESSES & PLACES
              </div>
              <div className="location-search-option-list" role="presentation">
                {results.map((loc, i) => (
                  <button
                    key={`geo-${loc.node_id ?? i}-${loc.lat}-${loc.lng}`}
                    id={geocodedOptionId(i)}
                    type="button"
                    role="option"
                    aria-selected={activeOptionId === geocodedOptionId(i)}
                    tabIndex={-1}
                    onClick={() => handleSelect(loc)}
                    onMouseEnter={() => setActiveOptionId(geocodedOptionId(i))}
                    style={{
                      width: '100%',
                      minHeight: '48px',
                      padding: '10px 14px',
                      background: activeOptionId === geocodedOptionId(i) ? tintColor : 'none',
                      border: 'none',
                      borderBottom: '1px solid #f1f5f9',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: '10px',
                      textAlign: 'left',
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: '0.85rem', color: 'var(--text-primary)', fontWeight: 600 }}>
                        {shorten(loc.display_name, 2)}
                      </div>
                      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                        {shorten(loc.display_name, 5).replace(shorten(loc.display_name, 2), '').replace(/^,\s*/, '')}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Named corridor locations */}
          {visibleNamed.length > 0 && (
            <div role="group" aria-label={savedPlacesOnly ? 'Saved places' : 'Popular Singapore and Batam places'}>
              <div style={{ padding: '6px 14px', fontSize: '0.65rem', fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.06em', background: '#f8fafc' }}>
                {savedPlacesOnly ? 'SAVED PLACES' : 'POPULAR SINGAPORE + BATAM PLACES'}
              </div>
              <div className="location-search-option-list" role="presentation">
                {visibleNamed.map((loc, i) => (
                  <button
                    key={`named-${loc.id}`}
                    id={namedOptionId(i)}
                    type="button"
                    className="location-search-named-option"
                    role="option"
                    aria-selected={activeOptionId === namedOptionId(i)}
                    tabIndex={-1}
                    onClick={() => handleNamedSelect(loc)}
                    onMouseEnter={() => setActiveOptionId(namedOptionId(i))}
                    style={{
                      width: '100%',
                      minHeight: '48px',
                      padding: '10px 14px',
                      background: activeOptionId === namedOptionId(i) ? tintColor : 'none',
                      border: 'none',
                      borderBottom: i < visibleNamed.length - 1 ? '1px solid #f1f5f9' : 'none',
                      cursor: 'pointer',
                      textAlign: 'left',
                    }}
                  >
                    <div className="location-search-option-name">
                      <span>
                        {loc.name}
                      </span>
                    </div>
                    <span className="badge badge-neutral location-search-option-badge" title={loc.category}>
                      {loc.category}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {!isLoading && qLower && results.length === 0 && visibleNamed.length === 0 && (
            <div role="status" style={{ color: 'var(--text-muted)', fontSize: '0.78rem', lineHeight: 1.5, padding: '18px 14px', textAlign: 'center' }}>
              No local suggestion matches “{query.trim()}”. Try a broader Singapore or Batam district, or use the map picker.
            </div>
          )}

          {!savedPlacesOnly && filteredNamed.length > visibleNamed.length && (
            <div style={{ background: '#f8fafc', borderTop: '1px solid #f1f5f9', color: 'var(--text-muted)', fontSize: '0.68rem', padding: '8px 14px', textAlign: 'center' }}>
              Showing {visibleNamed.length} of {filteredNamed.length} local suggestions · type more to narrow the list
            </div>
          )}
        </div>
      )}


      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
};
