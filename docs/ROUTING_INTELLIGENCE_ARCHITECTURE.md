# Routing Intelligence Architecture

This document describes the backend routing-intelligence refactor. It is an
implementation guide and trust-boundary reference, not a claim that CrossFlow
has a live Batam traffic feed or production-validated route accuracy.

## Component boundaries

| Boundary | Primary implementation | Contract |
|---|---|---|
| Road graph and path search | `backend/services/router.py` | Stateful, turn-aware A* over the committed OpenStreetMap graph, with one immutable congestion, learning, and approved-override snapshot per calculation. |
| Route orchestration | `backend/services/route_solver.py` | Validates a request, builds forecast inputs, compares departure windows, and keeps local OSM routing as the critical-path fallback. |
| Shortcut ingestion | `backend/services/shortcut_ingestion.py` | Parses only server-allowlisted, geocoded source documents into inactive `REVIEW_REQUIRED` candidates. It cannot activate an edge. |
| Traffic observation validation | `backend/services/traffic_observations.py` | Produces deterministic, typed speed observations and provider-neutral speed-ratio estimates. |
| Local history | `backend/services/historical_store.py` | SQLite storage for legacy dashboard history and the longer-lived typed spatial-observation table. |
| Congestion inference | `backend/models/congestion_model.py` | Random-forest tree ensemble with empirical quantiles, calibration metadata, and a legacy prediction facade. |
| Service ports and caching | `backend/services/service_contracts.py` | Protocols for observation persistence, shortcut review/promotion, routing, and a replaceable bounded cache. |
| Optional shared durability | `backend/services/routing_intelligence_store.py` and `backend/data/routing_intelligence.sql` | Server-only Supabase adapter for typed observations, review candidates, and approved graph overrides. |

The modules communicate through typed records rather than importing storage
details into the graph search. A persistence adapter can therefore change
without changing what counts as a valid observation or approved override.

## Vehicle-constrained A* and edge cost

The eight API vehicle profiles map to four stable routing policies:

| Routing policy | Time | Distance | Congestion | Road quality | Important constraints/defaults |
|---|---:|---:|---:|---:|---|
| Motorcycle | 1.00 | 0.12 | 0.72 | 0.45 | Public motor-road classes; 0.9 m width and 0.4 t planning dimensions; unrated quality 0.68. |
| Passenger car | 1.00 | 0.05 | 1.00 | 1.00 | Public motor-road classes; 2.2 m width and 3.5 t planning dimensions; unrated quality 0.72. |
| Freight/truck | 1.00 | 0.04 | 1.25 | 1.70 | Excludes living streets and tracks; 2.8 m width, 4.2 m height, and 25 t planning dimensions; unrated quality 0.66. |
| Ferry/maritime link | 1.00 | 0.03 | 0.85 | 0.40 | Ferry edges only; unrated quality 0.85. Ferry itinerary selection still uses the separate published-timetable composer. |

The dimensions in this table are canonical policy defaults. During normal API
routing, the selected profile's published dimensions take precedence—for
example, commuter cars, vans, buses, and trucks do not all share one width or
gross weight.

The balanced generalized objective for edge `e` is:

```text
C(e) = w_t * time
     + w_d * distance
     + w_c * congestion
     + w_r * road_quality_penalty
```

All four terms are normalized to seconds before weighting:

- `time` is effective free-flow time plus weather, manoeuvre, short-turn, and
  signal time. Congestion is deliberately excluded from this term so it is not
  counted twice.
- `distance` is physical edge length converted to a lower-bound seconds proxy
  at the vehicle profile's maximum speed. Raw distance is still reported in
  metres/kilometres.
- `congestion` is delay above free flow. The solver can consume an expected
  speed ratio, uncertainty, and a downside quantile through
  `EdgeCongestionEstimate`. A separate nonnegative uncertainty penalty is
  scaled by the policy's risk aversion.
- `road_quality_penalty` is `free_flow_seconds * (1 - quality)`. Quality comes
  from a reviewed override, OSM surface/smoothness, a published road-class
  default, or finally the vehicle policy's explicit unrated fallback.

Before an edge is costed, the traversal gate checks explicit access/mode tags,
allowed road class, width, height, weight, and impassable surface information.
The vehicle dimensions are published assumptions, while a missing edge width,
height, or weight tag remains unknown rather than becoming a fabricated hard
restriction. Missing data is not evidence of legal clearance. The A* heuristic
uses only admissible straight-line distance and free-flow lower bounds, while
turn-dependent state remains in the search.
The `FASTEST`, `SHORTEST`, `EASY`, and `LOCAL` preferences apply a second
nonnegative preference layer without changing physical distance or ETA units.

## Crowd shortcut ingestion and approval

`CROSSFLOW_SHORTCUT_SOURCE_POLICY` is a server-owned JSON allowlist. With the
variable unset, network fetching is disabled. A request may select a configured
source ID, but it cannot supply or widen a URL allowlist.

The pipeline performs the following steps:

1. Accept only a bounded document batch from exact pinned HTTPS URLs and
   allowed MIME types. Network fetches reject redirects, credentials in URLs,
   private/reserved IP addresses, DNS rebinding, oversized bodies, and deadline
   saturation.
2. Parse a narrow JSON, plain-text, or visible-HTML schema. Every candidate
   needs geocoded endpoints, explicit vehicle modes, and an auditable excerpt.
3. Validate WGS84 coordinates against the active graph bounds, snap endpoints
   to graph nodes, and reject implausible distance, duration, or implied speed.
4. Assign a deterministic identity containing the graph revision, endpoints,
   vehicle modes, and canonical geometry hash. Divergent geometries therefore
   do not silently collapse into one override.
5. Store the result as `REVIEW_REQUIRED` with `activation_allowed=false` in a
   bounded review queue.

Ingestion never edits the graph. Promotion is a separate human-reviewed action
that records the reviewer, approval time, candidate hash, graph revision, and
override revision. Routing can consume only an immutable approved snapshot for
the exact active graph revision. A stale approval is ineligible after a graph
rebuild. A blog calling a drain, footpath, or alley a “shortcut” does not make
it a motor edge; its geometry and explicit vehicle modes still require review
against physical and legal access evidence.

Scraped prose is also never model-training data. Only a numeric, geocoded,
timestamped `reviewed_community_observation` that passes the traffic contract
may enter traffic history, and it receives less source weight than verified
sensors or GPS.

### Source-policy example

Audit and replace the placeholder URL before enabling it:

```json
{
  "schema_version": 1,
  "sources": [
    {
      "source_id": "reviewed_batam_tips",
      "pinned_urls": [
        "https://tips.example.org/audited/batam-shortcuts.json"
      ],
      "allowed_content_types": ["application/json"],
      "confidence_ceiling": 0.6
    }
  ],
  "limits": {
    "max_document_bytes": 262144,
    "max_documents_per_batch": 20,
    "max_tips_per_document": 64,
    "max_tips_per_batch": 200,
    "max_excerpt_chars": 320
  }
}
```

Unknown fields and unsafe values fail closed. Keep this configuration in a
server environment variable, not in request payloads or frontend code.

## Typed spatial history and congestion estimation

A `SpatialTrafficObservation` contains a timezone-aware timestamp, Batam-bounds
latitude/longitude, actual and free-flow speeds, road class, optional capacity
and terminal distance, fixed provenance, and an optional upstream event ID.
The backend fixes the Batam civil offset at WIB (`UTC+07:00`) so 08:00 local
samples align across dates instead of accidentally aligning with 08:00 UTC.

Accepted source classes have server-owned provenance and confidence weights:
verified sensors/GPS/providers are highest, explicitly reviewed community
observations are lower, and modelled/simulated/synthetic fallbacks are lowest
and remain `observed=false`. The batch validator recomputes each deterministic
key and every policy-owned field, so constructing a dataclass directly cannot
forge provenance. Exact replays are idempotent; the same identity with a
different immutable payload is a conflict.

The speed-ratio estimator starts with:

```text
speed_ratio = V_actual / V_free_flow
```

For comparable records in the same corridor, historical weight is proportional
to source confidence multiplied by exponential decay for age, circular
hour-of-day distance, and circular day-of-week distance. It publishes source
counts, source-weight totals, observed counts, expected ratio, dispersion, and
provenance. This is a provider-neutral estimator contract; a stored row alone
does not make a route live. Route orchestration must explicitly provide its
result as an immutable `EdgeCongestionEstimate` snapshot, otherwise routing
uses its labelled forecast/free-flow fallbacks.

The module also exposes pure nonlinear helpers for peak-hour pressure, border
utilization, ferry wait, and ferry departure surge. They are orchestration
inputs, not independent observations.

### Retention and local durability

`CROSSFLOW_HISTORY_DB` selects the SQLite file. A normal file is durable for a
single host/process. Serverless `/tmp` and the in-memory safety fallback are
reported as ephemeral and must not be described as a shared archive.

The typed spatial table defaults to 1,825 days (five years). Set
`CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS` to an integer from 365 through 7,300
days when a different audited retention period is required. Batch ingestion is
bounded at 2,000 records and queries/training snapshots are also capped.

The legacy dashboard history remains separate: it uses a deterministic 14-day
synthetic seed and a shorter rolling modelled-snapshot window. Its synthetic
rows are never promoted to observed data.

## Probabilistic random-forest forecast

The congestion model is a dependency-light random-forest ensemble, not a GNN,
Bayesian model, or claim of learned road topology. The richer prediction
contract uses cyclical time/day features plus road class, capacity, terminal
distance, free-flow speed, weather, and explicitly declared corridor defaults
when older callers lack spatial fields.

Each tree is treated as an empirical ensemble draw. `predict_spatial` publishes
the mean, standard deviation, P10, P90, a P90-derived upper delay, and a
risk-adjusted score. Vehicle policy risk aversion raises the routing score
toward the downside bound; material tree disagreement also escalates the risk
label. These are empirical tree-dispersion quantities, not Bayesian credible
intervals, and the mean is not assumed to lie inside P10–P90.

Retraining accepts only validated typed observations, orders them by
`observed_at`, uses the earlier 80% for fitting and the later 20% for holdout,
and applies server-owned source confidence as sample weight. Metrics publish
fit/holdout source counts, source-weight totals, observed row counts, defaults
used, and validation scope. For the P10–P90 interval it reports:

- the nominal interval coverage target (`0.80`);
- observed prediction-interval coverage probability (PICP);
- mean interval width; and
- an empirical ensemble CRPS approximation:

```text
mean(|tree_prediction - y|)
  - 0.5 * mean(|tree_prediction_i - tree_prediction_j|)
```

The bundled cold-start model is trained and evaluated against a synthetic
Batam-shaped generator. Its R², MAE, RMSE, PICP, and CRPS describe reproduction
of that generator only. A deployment may claim observed validation only when
the published training/holdout metadata says the rows are observed.

Typed history does not persist a ferry-proximity feature. To keep that
limitation visible, an imminent published sailing applies a deterministic,
capped post-model adjustment to every tree prediction. It has a 15-point
amplitude and 45-minute exponential decay, and the response publishes both the
adjustment amount and method. It is a schedule-informed planning factor, not a
measured terminal queue.

## Persistence choices and Supabase boundary

### Local development

SQLite is the default history implementation. The shortcut module also has a
bounded in-process review queue for tests and local integration. Neither is a
multi-instance production queue by itself.

### Optional shared Supabase durability

Run [`backend/data/routing_intelligence.sql`](../backend/data/routing_intelligence.sql)
once in the Supabase SQL Editor. It creates:

- `crossflow_spatial_traffic_observations`, with deterministic keys and an
  idempotent, bounded batch-ingestion RPC;
- `crossflow_shortcut_review_candidates`, which can contain only inactive
  `REVIEW_REQUIRED` records and has an atomic deterministic batch-upsert RPC;
  and
- `crossflow_approved_graph_overrides`, keyed by graph and override identity
  with reviewer and revision metadata. Its advisory-lock-protected promotion
  RPC derives an append-only approval from the exact stored candidate.

The SQL enables Row Level Security, revokes table/function access from
`public`, `anon`, and `authenticated`, and grants the minimum declared access to
`service_role`. The backend still validates every domain record before writing;
database constraints are defense in depth, not a replacement for the typed
contracts. Supabase recommends combining explicit grants with RLS for exposed
Data API objects; see [Securing your API](https://supabase.com/docs/guides/api/securing-your-api).

`CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS` governs the local SQLite store. The
base Supabase migration intentionally does not create a scheduler or grant
browser-accessible deletion. If a deployment requires automatic deletion from
the shared archive, add an audited server-side retention job and a separately
reviewed least-privilege SQL migration; do not assume the local cleanup setting
applies remotely.

The matching adapter can ingest typed observations, read a bounded
chronological training snapshot, atomically upsert inactive candidates, approve
one persisted candidate by ID and reviewer, and read an approved snapshot for a
specific graph revision. Every row is reconstructed through the domain
validator on read. Approved snapshots have a separate bounded five-second
in-process cache; a successful approval invalidates that graph revision.

Configure `SUPABASE_URL` and the preferred `SUPABASE_SECRET_KEY`, or the legacy
`SUPABASE_SERVICE_ROLE_KEY`, only in the backend runtime. Secret/service-role
keys have elevated access and bypass RLS, so never place one in a `VITE_`,
`NEXT_PUBLIC_`, mobile, or other client-visible variable. See Supabase's
[API-key guidance](https://supabase.com/docs/guides/getting-started/api-keys).
The transport accepts only audited HTTPS project origins, rejects redirects,
bounds response sizes/timeouts, and avoids logging secret-bearing error text.

Applying the migration is a separate, explicit activation step. Set
`CROSSFLOW_ROUTING_INTELLIGENCE_STORE=supabase` only after the migration has
been applied and checked; the default `local` mode never probes these optional
tables merely because the ferry-freshness feature uses the same project.

Supabase durability is optional for routing intelligence. A missing or unsafe
configuration must be reported as unavailable; it must not be relabelled as a
successful shared write. Local OSM remains the route critical path, while
durability-sensitive admin operations should fail closed or explicitly report
their local/ephemeral fallback according to the calling endpoint's contract.

## Service contracts, cache, and fallbacks

The service layer exposes small ports rather than one implicit global service:

- `SpatialObservationPersistencePort` accepts bounded, idempotent typed batches.
- `ShortcutCandidatePersistencePort` stores quarantined candidates.
- `GraphOverridePromotionPort` returns an immutable, graph-revision-bound
  approved snapshot.
- `RoutePlanningPort` keeps the route API independent of the storage adapter.
- `CachePort` allows the in-process cache to be replaced by Redis or another
  bounded implementation without changing route semantics.

The deployed application currently exposes FastAPI REST endpoints and uses
bounded Supabase PostgREST/RPC calls where shared persistence is configured.
The Python protocols are transport-neutral, but no gRPC server is implemented
or claimed by this repository.

The service-contract `BoundedTTLCache` is thread-safe, copy-isolated, and
bounded; its tested facade keys entries by request, time bucket, graph,
dynamic-data and override revisions. The deployed low-level router uses a
separate bounded LRU whose identity includes the immutable override snapshot.
Neither cache stores secrets. Redis is an integration option, not a currently
claimed dependency. The Supabase approved-snapshot adapter uses its own
five-second, eight-graph-revision bound and also stores no secrets.

The local committed OSM graph is always calculated as the safe critical path.
The legacy `CROSSFLOW_ROUTE_PROVIDER=supabase` mode fails fast because its RPC
cannot prove vehicle access or return the complete navigation contract.
`supabase_v2_constrained` is reserved for an audited replacement envelope; it
may replace local geometry only after the backend validates vehicle-constraint
provenance, endpoints, distance, navigation and alternatives. Missing
credentials, network failure, malformed provider data, or another provider
setting leaves the local result in place. Provider geometry never inherits
exact-edge learning or shortcut claims from the local path.

Storage failures are converted to bounded, non-secret error codes, and invalid
or conflicting replays fail before partial mutation. Exact observation replays
and deterministic shortcut-candidate upserts are idempotent; the same identity
with a different immutable payload is an error rather than a silent overwrite.

## Administration and operations

Set a strong server-only `CROSSFLOW_ADMIN_TOKEN` and send it in
`X-CrossFlow-Admin-Token` for protected ingestion, review/promotion, persistence
status, and retraining operations exposed by the API. If the token is unset,
the authorization guard denies those operations; it does not create an open
development mode. Do not reuse the Supabase secret as this token.

Bind the spatial-ingestion endpoint to one audited producer with
`CROSSFLOW_TRAFFIC_INGEST_SOURCE`. That server variable—not a request field—
selects `loop_sensor`, `probe_gps`, `tomtom_live`, or
`verified_traffic_observation`, so an uploaded payload cannot assign itself
verified provenance or confidence.

Recommended operating sequence:

1. Pin and audit shortcut sources in `CROSSFLOW_SHORTCUT_SOURCE_POLICY`.
2. Ingest documents into the review queue and inspect each rejection and
   candidate's source excerpt, geometry, vehicle modes, confidence, and graph
   revision.
3. Approve through the protected promotion contract; never edit the active
   graph directly from scraped output.
4. Ingest typed traffic observations with timezone and provenance intact.
5. Inspect storage durability and source counts before retraining.
6. Retrain explicitly and review chronological holdout scope, PICP, interval
   width, CRPS, and source weights before promoting a model.
7. Monitor cache/store status and retain the local OSM fallback.

## Known boundaries

- The committed graph cannot prove missing lane width, clearance, weight, or
  every turn restriction. Vehicle constraints are planning safeguards, not a
  legal routing certificate.
- A crowd candidate is untrusted until reviewed; review establishes eligibility
  for a graph revision, not that the route is permanently safe.
- SQLite on serverless `/tmp` is ephemeral. Apply the Supabase schema and wire
  the server-only adapter when shared multi-instance durability is required.
- Model metrics must always be read with their source counts and validation
  scope. Synthetic or mixed history is not observed Batam accuracy.
- Ferry surge and border/peak penalties are planning functions. They do not
  establish a live vessel, gate, immigration queue, or road condition.
