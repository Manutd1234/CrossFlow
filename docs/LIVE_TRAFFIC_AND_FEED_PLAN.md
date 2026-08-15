# CrossFlow live traffic and live feed plan

Updated 10 August 2026.

## Outcome

Use TomTom as the first production traffic source for Batam, keep the bundled
current-time model as a clearly labelled continuity layer, and introduce Batam
government CCTV as links before attempting embeds. The dashboard must never
describe modelled, stale, or unreachable data as live.

## Current readiness

- `/api/live-traffic` already has a server-side TomTom Flow Segment adapter for
  five verified points on the routed Batam corridors.
- Provider calls are concurrent, bounded to two seconds, cached for two minutes,
  and fall back per corridor if TomTom is unavailable.
- The frontend refreshes one shared traffic snapshot every 30 seconds. Provider
  caching prevents this UI cadence from multiplying billable upstream calls.
- When the backend is unavailable, the map now shows five current-time local
  model points instead of the former empty `0 segments` state.
- A real `TOMTOM_API_KEY` is not present in the current local environment. Until
  one is configured and a request succeeds, `Current traffic model` or
  `Local current-time model` is the only truthful status.

## Phase 1 — activate live Batam traffic

Target: same day once a TomTom account and deployment access are available.

1. Create a restricted TomTom key and confirm the account's Traffic API quota.
2. Add `TOMTOM_API_KEY` as a server-side secret in local, preview, and production
   environments. Never expose this key through a `VITE_` variable.
3. Smoke-test `GET /api/live-traffic` during a busy Batam period. Acceptance:
   `data_source` is `live`, `overall_source` is `tomtom_live`, five segments are
   returned, at least one segment has current/free-flow speeds, and response time
   remains below the frontend's six-second timeout.
4. Verify the header reads `Live road traffic`, the map adds a `TomTom` provider
   badge, and the timestamp advances without resetting the map viewport.
5. Add a deployment health check that alerts when no successful live response is
   seen for five minutes. Degrade to the model with a visible last-live timestamp.

TomTom officially lists Indonesia for detailed flow and incidents, and documents
approximately minute-level flow updates. Relevant official documentation:

- [Traffic market coverage](https://docs.tomtom.com/traffic-api/documentation/tomtom-orbis-maps/v2/product-information/market-coverage)
- [Traffic Flow service](https://docs.tomtom.com/traffic-api/documentation/tomtom-orbis-maps/v2/traffic-flow/traffic-flow-service)
- [Raster flow tiles](https://docs.tomtom.com/traffic-api/documentation/tomtom-orbis-maps/v2/traffic-flow/raster-flow-tiles)
- [Platform allowance and rate-limit FAQ](https://developer.tomtom.com/platform/documentation/status-and-support/faqs)

## Phase 1.5 — make the whole map and route solver traffic-aware

Target: one to two implementation days after Phase 1 is stable.

| Capability | Source | Update pattern | CrossFlow change |
|---|---|---|---|
| Full-road congestion overlay | TomTom raster flow tiles | Browser revalidation about every 60 seconds | Add a Leaflet tile layer with a separate domain/product-restricted browser key. |
| Incidents | TomTom Incident Details bbox API | Backend poll every 60 seconds | Normalize closures, crashes, and roadworks into a provider-neutral incident schema. |
| Route ETA | TomTom Calculate Route | On solve; refresh an active route every 60–120 seconds | Request `traffic=true`, `departAt=now`, and the selected travel mode. |
| Freight restrictions | TomTom routing parameters | On solve | Pass supported truck dimensions and restrictions; retain the existing safety disclaimer. |

Official implementation references:

- [Incident Details API](https://docs.tomtom.com/traffic-api/documentation/tomtom-orbis-maps/v2/traffic-incidents/incident-details)
- [Vector incident tiles](https://docs.tomtom.com/traffic-api/documentation/tomtom-orbis-maps/v2/traffic-incidents/vector-incident-tiles)
- [Calculate Route](https://developer.tomtom.com/routing-api/documentation/tomtom-maps/v1/calculate-route)
- [Common routing parameters](https://developer.tomtom.com/routing-api/documentation/tomtom-maps/v1/common-routing-parameters)

Non-tile calls stay behind the backend. Browser tiles use a different restricted
key. Cache behavior must follow provider headers and the
[TomTom terms](https://developer.tomtom.com/terms-and-conditions).

## Phase 2 — Batam live camera feed

Target: link-based pilot in one day; embedding only after permission and a
technical check.

Batam's official [Matanya Batam](https://matanya.batam.go.id/) portal exposes
publicly viewable intersection and port cameras. The city's
[application catalogue](https://batam.go.id/aplikasi/) describes it as a
24-hour public CCTV service. No official public camera API, stream contract,
uptime SLA, or redistribution licence was found.

1. Add a top-level camera subview with government-source cards and links to the
   corresponding Matanya page.
2. Label each card `Government CCTV · availability not guaranteed` and show a
   camera-health state separate from road-traffic status.
3. Test iframe embedding only after checking response headers and player behavior.
4. Ask Diskominfo/Dishub for written embedding/reuse terms, camera metadata,
   stream health, and supported HLS/MJPEG endpoints.
5. Do not scrape playlist URLs, proxy, restream, record, or run computer vision
   on camera frames until that permission is explicit.

## Phase 3 — local public-data partnership

Ask Dishub/Diskominfo for Matanya/ATCS metadata, signal and incident data, and a
documented Trans Batam vehicle-position or GTFS-Realtime feed. Dishub describes
SIP TB as showing nearby live buses and routes, but no public API contract was
found: [official SIP TB announcement](https://dishub.batam.go.id/sosialisasi-pembayaran-non-tunai-dan-sip-tb-sistem-informasi-penumpang-trans-batam-di-smpn-3-batam-oleh-upt-jasa-pelayanan-transportasi-dishub-batam/).

[Satu Data Batam](https://satudata.batam.go.id/web/) remains useful for static
context, not as a substitute for live traffic.

## Status contract and acceptance criteria

Use four explicit modes:

- `LIVE`: a provider response succeeded inside its freshness window.
- `DEGRADED`: a recent permitted cached response is shown with age and warning.
- `MODELLED`: the backend is reachable but the current conditions are estimated.
- `LOCAL`: the API is unavailable and the bundled current-time model is active.

Phase 1 is complete only when a production request proves live speeds for Batam,
the UI exposes source and age, stale/provider failures downgrade automatically,
no secret reaches the browser bundle, and quota/latency monitoring is enabled.

## Alternatives

Google Routes supports traffic-aware routes and traffic-aware polylines, but its
map-bound content and caching rules make it a poor fit for the existing Leaflet
map unless the map is migrated to Google:
[traffic-aware routing](https://developers.google.com/maps/documentation/routes/config_trade_offs),
[traffic polylines](https://developers.google.com/maps/documentation/routes/traffic_on_polylines),
[policies](https://developers.google.com/maps/documentation/routes/policies).

HERE Traffic v7 supports flow and incidents, but its published Indonesia
vector-tile coverage should be tested for Batam before adoption:
[API introduction](https://docs.here.com/traffic-api/docs/introduction-to-here-traffic-api-v7),
[coverage](https://docs.here.com/traffic-api/docs/traffic-vector-tile-traffic).
