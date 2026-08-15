import corridorHotspotCatalog from '../../../backend/data/corridor_hotspots.json';

export const BATAM_HOTSPOT_LIMIT = 30;

export type HotspotWatchPriority = 'CRITICAL' | 'HEAVY';

export interface BatamLocationPhoto {
  id: string;
  imageUrl: string;
  sourceUrl: string;
  author: string;
  license: string;
  licenseUrl: string;
  alt: string;
  caption: string;
  capturedYear: number;
}

export interface BatamHotspotReference {
  id: string;
  name: string;
  category: string;
  lat: number;
  lng: number;
  radiusM: number;
  planningScore: number;
  priority: HotspotWatchPriority;
  photo: BatamLocationPhoto;
  photoContext: 'Location photo' | 'Nearby area photo' | 'Corridor reference photo';
  observed: false;
  basis: 'modelled-planning-priority';
}

const commonsImage = (filename: string): string =>
  `https://commons.wikimedia.org/wiki/Special:Redirect/file/${encodeURIComponent(filename)}?width=640`;

const commonsPage = (filename: string): string =>
  `https://commons.wikimedia.org/wiki/File:${encodeURIComponent(filename)}`;

/**
 * Reusable, licensed Batam location references. These are deliberately not
 * described as traffic-camera images: each photo is a historical orientation
 * aid, with its Commons source and licence kept next to it in the UI.
 */
export const BATAM_LOCATION_PHOTOS = {
  sudirman: {
    id: 'photo-sudirman',
    imageUrl: commonsImage('Jalan Panglima Besar Sudirman, Batam, Riau Islands.jpg'),
    sourceUrl: commonsPage('Jalan Panglima Besar Sudirman, Batam, Riau Islands.jpg'),
    author: 'Firzafp',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
    alt: 'Jalan Panglima Besar Sudirman in Batam after road renovation',
    caption: 'Jalan Panglima Besar Sudirman, Batam',
    capturedYear: 2018,
  },
  bungaRaya: {
    id: 'photo-bunga-raya',
    imageUrl: commonsImage('Jalan Bunga Raya, Batam, Riau Islands.jpg'),
    sourceUrl: commonsPage('Jalan Bunga Raya, Batam, Riau Islands.jpg'),
    author: 'Firzafp',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
    alt: 'Jalan Bunga Raya near Batam City Square in Batam',
    caption: 'Jalan Bunga Raya, Lubuk Baja',
    capturedYear: 2018,
  },
  pembangunan: {
    id: 'photo-pembangunan',
    imageUrl: commonsImage('Jalan Pembangunan Kota Batam.jpg'),
    sourceUrl: commonsPage('Jalan Pembangunan Kota Batam.jpg'),
    author: 'Cun Cun',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
    alt: 'Jalan Pembangunan in Batu Selicin, Lubuk Baja, Batam',
    caption: 'Jalan Pembangunan, Batu Selicin',
    capturedYear: 2018,
  },
  nagoya: {
    id: 'photo-nagoya',
    imageUrl: commonsImage('Nagoya, Batam, Indonesia (4022041840).jpg'),
    sourceUrl: commonsPage('Nagoya, Batam, Indonesia (4022041840).jpg'),
    author: 'Kok Leng Yeo (yeowatzup)',
    license: 'CC BY 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/2.0/',
    alt: 'Street scene in Nagoya, Batam',
    caption: 'Nagoya, Lubuk Baja',
    capturedYear: 2009,
  },
  batamCentre: {
    id: 'photo-batam-centre',
    imageUrl: commonsImage('Batam center.JPG'),
    sourceUrl: commonsPage('Batam center.JPG'),
    author: 'Masgatotkaca',
    license: 'GFDL 1.2+',
    licenseUrl: 'https://www.gnu.org/licenses/old-licenses/fdl-1.2.html',
    alt: 'Batam Centre International Ferry Terminal building',
    caption: 'Batam Centre Ferry Terminal',
    capturedYear: 2008,
  },
  harbourBay: {
    id: 'photo-harbour-bay',
    imageUrl: commonsImage('Harbour Bay Batam.jpg'),
    sourceUrl: commonsPage('Harbour Bay Batam.jpg'),
    author: 'Cun Cun',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
    alt: 'Harbour Bay ferry terminal in Batam',
    caption: 'Harbour Bay Ferry Terminal',
    capturedYear: 2018,
  },
  sekupang: {
    id: 'photo-sekupang',
    imageUrl: commonsImage('Sekupang Ferry Terminal.jpg'),
    sourceUrl: commonsPage('Sekupang Ferry Terminal.jpg'),
    author: 'alantankenghoe',
    license: 'CC BY 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/2.0/',
    alt: 'Sekupang Ferry Terminal in Batam',
    caption: 'Sekupang Ferry Terminal',
    capturedYear: 2011,
  },
  hangTuah: {
    id: 'photo-hang-tuah',
    imageUrl: commonsImage("Jalan Hang Tuah - Batam, KR (13 Juli '24) 01.jpg"),
    sourceUrl: commonsPage("Jalan Hang Tuah - Batam, KR (13 Juli '24) 01.jpg"),
    author: 'Firzafp',
    license: 'CC BY 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/4.0/',
    alt: 'Expanded Jalan Hang Tuah in Batam',
    caption: 'Jalan Hang Tuah, Batam',
    capturedYear: 2024,
  },
  barelang: {
    id: 'photo-barelang',
    imageUrl: commonsImage('Barelang Bridge, Batam.jpg'),
    sourceUrl: commonsPage('Barelang Bridge, Batam.jpg'),
    author: 'Soham Banerjee',
    license: 'CC BY 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/2.0/',
    alt: 'Barelang Bridge in Batam',
    caption: 'Barelang Bridge, southern Batam',
    capturedYear: 2008,
  },
} as const satisfies Record<string, BatamLocationPhoto>;

const commonFields = {
  observed: false,
  basis: 'modelled-planning-priority',
} as const;

/**
 * Thirty representative Batam mobility pressure points for planning coverage.
 * The 20/10 split is an explicit watch-list priority, not an observed claim
 * about current road conditions. Live/provider telemetry remains a separate
 * layer on the map.
 */
const HOTSPOT_MEDIA_REFERENCES: readonly BatamHotspotReference[] = [
  { id: 'zone-simpang-jam', name: 'Simpang Jam / Laluan Madani', category: 'Urban junction · Sudirman', lat: 1.122424, lng: 104.019539, radiusM: 650, planningScore: 92, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-panbil', name: 'Simpang Panbil / Muka Kuning', category: 'Industrial junction', lat: 1.072252, lng: 104.025136, radiusM: 700, planningScore: 91, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-nagoya', name: 'Nagoya / Lubuk Baja Centre', category: 'Commercial district', lat: 1.1445, lng: 104.013, radiusM: 700, planningScore: 89, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.nagoya, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-kepri-mall', name: 'Kepri Mall / Pandan Wangi', category: 'Commercial junction', lat: 1.097364, lng: 104.036254, radiusM: 600, planningScore: 88, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-batam-centre', name: 'Batam Centre Ferry / Engku Putri', category: 'Ferry and civic access', lat: 1.1318, lng: 104.0554, radiusM: 650, planningScore: 87, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.batamCentre, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-batu-ampar', name: 'Batu Ampar / Yos Sudarso', category: 'Freight and port access', lat: 1.163, lng: 104.0025, radiusM: 750, planningScore: 86, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.harbourBay, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-batamindo', name: 'Batamindo Industrial Gate', category: 'Industrial access', lat: 1.06048, lng: 104.030321, radiusM: 750, planningScore: 85, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-kabil', name: 'Simpang Kabil / Plamo Garden', category: 'Urban junction', lat: 1.105097, lng: 104.039121, radiusM: 650, planningScore: 84, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-kara', name: 'Simpang Kara / Duta Mas', category: 'Urban junction', lat: 1.1112, lng: 104.0421, radiusM: 550, planningScore: 83, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-kda', name: 'Simpang KDA / Raja Isa', category: 'Urban junction', lat: 1.101449, lng: 104.076077, radiusM: 650, planningScore: 82, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-cikitsu', name: 'Simpang Cikitsu / Tengku Sulung', category: 'Residential junction', lat: 1.124686, lng: 104.098174, radiusM: 700, planningScore: 81, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-basecamp', name: 'Simpang Basecamp / Batu Aji', category: 'Transit junction', lat: 1.0519, lng: 103.9509, radiusM: 650, planningScore: 80, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-fanindo', name: 'Fanindo / Sagulung', category: 'Residential access', lat: 1.047372, lng: 103.937295, radiusM: 600, planningScore: 79, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-sei-panas', name: 'Sei Panas / Sudirman', category: 'Arterial junction', lat: 1.1292, lng: 104.0348, radiusM: 600, planningScore: 78, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-baloi', name: 'Baloi / Bunga Raya', category: 'Commercial junction', lat: 1.1311, lng: 104.0062, radiusM: 550, planningScore: 77, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.bungaRaya, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-batu-selicin', name: 'Batu Selicin / Pembangunan', category: 'Commercial access', lat: 1.1378, lng: 104.0112, radiusM: 550, planningScore: 76, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.pembangunan, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-jodoh', name: 'Jodoh / Ali Haji', category: 'Commercial junction', lat: 1.1491, lng: 104.0085, radiusM: 550, planningScore: 75, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.nagoya, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-harbour-bay', name: 'Harbour Bay / Duyung', category: 'Ferry and port access', lat: 1.15396, lng: 103.997234, radiusM: 650, planningScore: 74, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.harbourBay, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-batu-besar', name: 'Batu Besar / Hang Tuah', category: 'Airport corridor', lat: 1.1425, lng: 104.117, radiusM: 650, planningScore: 73, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-bengkong', name: 'Bengkong / Hang Jebat', category: 'Urban junction', lat: 1.1515, lng: 104.0385, radiusM: 600, planningScore: 72, priority: 'CRITICAL', photo: BATAM_LOCATION_PHOTOS.sudirman, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-sekupang', name: 'Sekupang Ferry Terminal', category: 'Ferry and port access', lat: 1.125, lng: 103.925, radiusM: 650, planningScore: 69, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.sekupang, photoContext: 'Location photo', ...commonFields },
  { id: 'zone-vitka-tiban', name: 'Vitka / Tiban Centre', category: 'Residential access', lat: 1.111246, lng: 103.978309, radiusM: 650, planningScore: 68, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.bungaRaya, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-nongsa-pura', name: 'Nongsa Pura Ferry Terminal', category: 'Ferry and resort access', lat: 1.1963, lng: 104.0997, radiusM: 650, planningScore: 67, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-hang-nadim', name: 'Hang Nadim Airport Access', category: 'Airport access', lat: 1.1211, lng: 104.1147, radiusM: 650, planningScore: 66, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-telaga-punggur', name: 'Telaga Punggur Ferry Terminal', category: 'Domestic ferry access', lat: 1.0436, lng: 104.1323, radiusM: 650, planningScore: 65, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.batamCentre, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-batu-aji', name: 'Batu Aji Transit Hub', category: 'Transit and residential access', lat: 1.050919, lng: 103.964956, radiusM: 650, planningScore: 64, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-marina-city', name: 'Marina City / Tanjung Riau', category: 'Coastal residential access', lat: 1.082167, lng: 103.931768, radiusM: 600, planningScore: 63, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.sekupang, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-botania', name: 'Botania / Batam Centre East', category: 'Commercial junction', lat: 1.118, lng: 104.0875, radiusM: 600, planningScore: 62, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Nearby area photo', ...commonFields },
  { id: 'zone-kabil-industrial', name: 'Kabil Industrial Estate Access', category: 'Industrial and port access', lat: 1.094875, lng: 104.118329, radiusM: 750, planningScore: 61, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.hangTuah, photoContext: 'Corridor reference photo', ...commonFields },
  { id: 'zone-tanjung-piayu', name: 'Tanjung Piayu / Sei Beduk', category: 'Residential and industrial access', lat: 1.0255, lng: 104.0575, radiusM: 650, planningScore: 60, priority: 'HEAVY', photo: BATAM_LOCATION_PHOTOS.barelang, photoContext: 'Corridor reference photo', ...commonFields },
] as const;

const hotspotMediaById = new Map(
  HOTSPOT_MEDIA_REFERENCES.map(reference => [reference.id, reference]),
);

/**
 * Identity, location and coverage now come from the backend-owned catalogue.
 * The frontend retains only licensed photo presentation data keyed by zone id.
 */
export const BATAM_HOTSPOT_REFERENCES: readonly BatamHotspotReference[] =
  corridorHotspotCatalog.candidates.map((candidate, index) => {
    const media = hotspotMediaById.get(candidate.zone_id);
    if (!media) {
      throw new Error(`Missing photo metadata for ${candidate.zone_id}.`);
    }
    return {
      id: candidate.zone_id,
      name: candidate.name,
      category: candidate.category,
      lat: candidate.lat,
      lng: candidate.lng,
      radiusM: candidate.radius_m,
      planningScore: media.planningScore,
      priority: index < 20 ? 'CRITICAL' : 'HEAVY',
      photo: media.photo,
      photoContext: media.photoContext,
      ...commonFields,
    };
  });

export const BATAM_HOTSPOT_WATCH_DISCLAIMER =
  'Watch priority is backend-weighted from corridor pressure, recurrence, network role, demand exposure, and evidence confidence. It is modelled, not an observed live condition.';
