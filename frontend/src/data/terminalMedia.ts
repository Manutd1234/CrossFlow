export interface TerminalMedia {
  imageUrl: string;
  alt: string;
  author: string;
  sourceUrl: string;
  license: string;
  licenseUrl: string;
  capturedYear: number;
  context: string;
  officialUrl: string;
}

const commonsImage = (filename: string): string =>
  `https://commons.wikimedia.org/wiki/Special:FilePath/${encodeURIComponent(filename)}?width=960`;

const commonsPage = (filename: string): string =>
  `https://commons.wikimedia.org/wiki/File:${encodeURIComponent(filename)}`;

/** Reusable Commons photography with source and licence details shown in the UI. */
export const TERMINAL_MEDIA: Record<string, TerminalMedia> = {
  'Batam Centre': {
    imageUrl: commonsImage('Terminal Ferry Batam Centre.JPG'),
    alt: 'Passenger ferries beside Batam Centre Ferry Terminal',
    author: 'Masgatotkaca',
    sourceUrl: commonsPage('Terminal Ferry Batam Centre.JPG'),
    license: 'CC BY-SA 3.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/3.0/',
    capturedYear: 2007,
    context: 'Terminal photo',
    officialUrl: 'https://batamport.bpbatam.go.id/batam-centre/',
  },
  HarbourBay: {
    imageUrl: commonsImage('Harbour Bay Ferry Terminal.jpg'),
    alt: 'Harbour Bay Ferry Terminal building in Batam',
    author: 'Exbeing',
    sourceUrl: commonsPage('Harbour Bay Ferry Terminal.jpg'),
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
    capturedYear: 2018,
    context: 'Terminal photo',
    officialUrl: 'https://batamport.bpbatam.go.id/harbour-bay/',
  },
  Sekupang: {
    imageUrl: commonsImage('Sekupang Ferry Terminal.jpg'),
    alt: 'Sekupang International Ferry Terminal seen from the water',
    author: 'alantankenghoe',
    sourceUrl: commonsPage('Sekupang Ferry Terminal.jpg'),
    license: 'CC BY 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/2.0/',
    capturedYear: 2011,
    context: 'Terminal photo',
    officialUrl: 'https://batamport.bpbatam.go.id/sekupang/',
  },
  'Nongsa Pura': {
    imageUrl: commonsImage('Jl. Hang Lekiu, Sambau, Nongsa, Kota Batam, Kepulauan Riau 29465, Indonesia - panoramio.jpg'),
    alt: 'View near Jalan Hang Lekiu in the Nongsa area of Batam',
    author: 'Lobster1',
    sourceUrl: commonsPage('Jl. Hang Lekiu, Sambau, Nongsa, Kota Batam, Kepulauan Riau 29465, Indonesia - panoramio.jpg'),
    license: 'CC BY-SA 3.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/3.0/',
    capturedYear: 2014,
    context: 'Nongsa area reference',
    officialUrl: 'https://batamport.bpbatam.go.id/nongsapura/',
  },
};
