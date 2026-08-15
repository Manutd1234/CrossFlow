export interface OfficialOperationsFact {
  id: string;
  label: string;
  value: string;
  detail: string;
  period: string;
  publisher: string;
  sourceUrl: string;
}

/** Source-dated context only: these are not presented as live traffic values. */
export const OFFICIAL_OPERATIONS_FACTS: readonly OfficialOperationsFact[] = [
  {
    id: 'international-passengers-h1-2026',
    label: 'International Sea Passengers',
    value: '2,671,134',
    detail: 'Passenger movements through BP Batam-managed international terminals.',
    period: 'Jan–Jun 2026',
    publisher: 'BP Batam Port Business Entity',
    sourceUrl: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
  },
  {
    id: 'domestic-passengers-h1-2026',
    label: 'Domestic Sea Passengers',
    value: '2,295,165',
    detail: 'Passenger movements reported across BP Batam domestic terminals.',
    period: 'Jan–Jun 2026',
    publisher: 'BP Batam Port Business Entity',
    sourceUrl: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
  },
  {
    id: 'passenger-vessel-calls-h1-2026',
    label: 'Passenger-Vessel Calls',
    value: '38,369',
    detail: 'Aggregate passenger-vessel calls reported by the port authority.',
    period: 'Jan–Jun 2026',
    publisher: 'BP Batam Port Business Entity',
    sourceUrl: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
  },
  {
    id: 'atcs-intersections-2024',
    label: 'Maintained Signalised Intersections',
    value: '38 reported',
    detail: 'The report states 32 ATCS and 6 non-ATCS intersections.',
    period: 'Dec 2024 · PDF p. 64 (report p. 54)',
    publisher: 'Batam City Transportation Agency',
    sourceUrl: 'https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_LAKIP_2024.pdf',
  },
] as const;
