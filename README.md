# ⚡ CrossFlow AI — Smart Mobility & Cross-Border Logistics Platform

> **Batam-Singapore Hackathon 2026 Submission**  
> **Track 2: Ease of Living & Sustainability — Smart Mobility Flow**

CrossFlow AI is a smart-mobility and cross-border logistics planning platform
for the **Batam-Singapore Corridor**. It combines road routing, planning-grade
congestion forecasts and published ferry schedules in a door-to-door journey
view. The product distinguishes official reference data, optional observations
and modelled estimates; it does not present a timetable, queue estimate or
forecast as live unless a connected source actually supplies a fresh
observation.


---

## 🌟 Key Features

1. **Vehicle-Aware Road Routing (OpenStreetMap + A\*)**
   - Batam road-following paths are computed over a 115,320-node schema-v3 union-retention graph of mapped motor roads. Each request selects a mutually reachable core for its vehicle mode; the committed metadata preserves access, motorcycle/HGV rules, surface, width, height, weight, speed and lane tags when OSM publishes them. A stateful A\* supports five audited objectives: **Balanced**, **Fastest**, **Shortest**, **Easy**, and **Local shortcuts**, with vehicle-specific time, distance, congestion and road-quality weights. Missing physical tags use declared conservative fallbacks rather than invented measurements. Singapore and cross-border road-access legs request OSRM routes over OpenStreetMap; an unavailable road service produces an explicitly labelled continuity estimate, not turn-by-turn navigation.
2. **Congestion Forecasting & Telemetry**
   - 30- and 60-minute planning forecasts cover **30 representative Batam mobility hotspots: 20 critical-priority and 10 heavy-priority locations**. The split is a product watch-list taxonomy, not an official Batam classification or a claim about current conditions. Scores respond to time of day, weekday/weekend, weather, local baselines and ferry-departure density. Each hotspot is accompanied by a dated reference photograph and is labelled modelled unless a successful point observation supplies its own source and timestamp.
3. **Singapore ↔ Batam Door-to-Door Journey Solver**
   - Free-text or map-picked endpoints can be anywhere in the supported Singapore and Batam bounds. A cross-border plan is composed as **Singapore road access → published ferry terminal corridor → Batam road access**, or the reverse. Candidate crossings are restricted to published terminal pairs through HarbourFront or Tanah Merah and Batam Centre, Harbour Bay, Sekupang or Nongsa Pura; the solver does not invent informal small-channel crossings. Every leg carries its own provider, geometry and limitation. Ferry lines are channel-aware planning geometry, not an observed vessel track, while the selected departure is asserted only when the bundled operator snapshot contains a matching sailing. Same-island Batam journeys retain the full local A\* objectives and alternatives.
4. **Ferry & Port Intelligence**
   - Source-dated operator timetable snapshots cover published Batam-Singapore sailings, alongside official BP Batam terminal reference information. Passenger-queue and processing values are **schedule-informed planning estimates** derived from departure density and a Batam time-of-day profile. They are always marked non-observed because no documented public live Batam terminal queue API is connected. The UI retains the operator or terminal link and asks travellers to verify and book before departure.
5. **Operations & Carbon Analytics Dashboard**
   - Bottleneck detection and dispatch alerts grounded in modelled corridor state, plus a modelled avoidable-emissions opportunity with published illustrative assumptions. These are scenario outputs, not observed or measured operational performance.

---

## 🔍 Data & Model Provenance

Every operational card is intended to answer three questions: **where did this
come from, when was it valid, and is it observed or estimated?** API responses
carry provenance fields, and the UI reads them instead of inferring that a
successful request must be live.

| Component | Source | Status |
|---|---|---|
| Batam road network | OpenStreetMap via Overpass (ODbL 1.0) | **Real** |
| Batam route geometry, distances, alternatives and manoeuvres | Computed from the committed OSM graph | Mapped road plan |
| Singapore/cross-border road-access legs | OSRM over OpenStreetMap when reachable | Mapped road plan; labelled continuity estimate when unavailable |
| Vehicle, route-objective, congestion and weather weights | Published planning assumptions over OSM edges | Modelled |
| Route-learning observation store | Admin-authorized, map-matched first-party traversals keyed to the exact committed OSM graph | Optional; empty until verified telemetry is ingested |
| Landmark coordinates | Verified against OSM features | **Real** |
| Point traffic observations | TomTom Traffic Flow only when a server-side key returns a fresh response | Optional commercial observation; not Batam-government telemetry |
| 30 Batam planning hotspots (20 critical / 10 heavy) | Representative locations, local-area profiles and synthetic Random Forest inputs | Modelled watch-list priorities; not official severity labels |
| Legacy congestion charts | Rolling, versioned 14-day Batam-time synthetic seed plus five-minute snapshots of modelled corridor telemetry | Non-observed; API reports source counts, freshness and storage durability |
| Typed spatial speed history | Numeric, geocoded, timezone-aware `V_actual` and `V_free_flow` records with fixed source policy | Optional; idempotent and provenance-labelled, stored in local SQLite or the server-only Supabase adapter when explicitly configured |
| Operations bottlenecks and emissions opportunity | Synthetic corridor scenario, deterministic emissions assumptions and published ferry timetable snapshot | Modelled/illustrative; not observed, live or measured |
| Ferry departure slots | Source-dated published timetables from BatamFast, Sindo, Majestic and Horizon; official source links retained | Last-known-good schedule snapshot, not live operations |
| Passenger queue and processing time | Published departure density × Batam time-of-day planning profile | Schedule-informed estimate; not observed and not an official wait time |
| Terminal identity, location and facilities | BP Batam official passenger-port and terminal pages | Official reference information; not live occupancy |
| Ferry vessels, seats, gates, berths, cancellations | No licensed public machine feed is connected | Unavailable; never fabricated |
| Cargo consignments | Illustrative demo dataset | Simulated |

The traffic score models network demand and queue conditions, while the
separately reported weather term models safe-speed loss on wet roads. They are
kept separate in each route's cost breakdown so the estimate can be audited.
Historical metadata sets `observed` only when every sample in the requested
window is observed; `contains_observed_samples` identifies mixed windows.

**On the model:** the bundled `RandomForestRegressor` remains a reproducible
synthetic cold-start model. The richer typed path can retrain it only from
validated spatial speed observations, using server-owned source-confidence
weights and a chronological holdout. Tree dispersion supplies empirical mean,
standard deviation, P10 and P90 outputs. Calibration metadata publishes the
P10–P90 coverage target and observed coverage, mean interval width, and an
empirical ensemble CRPS approximation. These are random-forest ensemble
statistics—not Bayesian intervals, a GNN, or evidence of learned road
topology.

The bundled R², MAE, RMSE, coverage and CRPS values measure how well the model
reproduces its synthetic generator; they are not real-world Batam accuracy.
Every retrain publishes source counts, source-weight totals, observed-row
counts, defaults used and validation scope. An imminent published sailing is a
separate, capped 15-point post-model adjustment with 45-minute exponential
decay because ferry proximity is not persisted as an observed traffic feature.
A commercial TomTom adapter can provide a current point speed when configured,
but it does not turn the hotspot catalogue or other forecasts into observed
ground truth.

Likewise, the queue cards deliberately remain estimates. Public official pages
provide terminal descriptions, aggregate passenger volumes, schedules and
process guidance, but no documented public feed of Batam passenger wait,
immigration processing, berth occupancy, gates, seats or cancellations was
found. A successful page scrape would not itself make a value authoritative or
grant redistribution rights.

### Authoritative references and integration boundaries

- **Batam traffic:** [Dishub Batam traffic cameras](https://dishub.batam.go.id/cctv-lalu-lintas-kota-batam/), the [2024 Dishub performance report](https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_LAKIP_2024.pdf) and the [2021–2026 strategic plan](https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_RENSTRA2021-2026.pdf). The 2024 report records the official ATCS/intersection inventory; it does not publish current congestion labels. The camera page has no documented public machine API, so a camera can be called current only after a successful timestamped fetch.
- **Batam open data:** [Satu Data Kota Batam](https://satudata.batam.go.id/data/) publishes downloadable civic datasets, and [BP Batam Open Data](https://data.bpbatam.go.id/dataset/?groups=transportasi) publishes agency datasets. The available road and infrastructure series are useful context, not a live segment-speed feed.
- **Ferry terminals and port totals:** [BP Batam passenger ports](https://batamport.bpbatam.go.id/pelabuhan-penumpang/) and its official pages for [Batam Centre](https://batamport.bpbatam.go.id/batam-centre/), [Sekupang](https://batamport.bpbatam.go.id/sekupang/), [Harbour Bay](https://batamport.bpbatam.go.id/harbour-bay/) and [Nongsapura](https://batamport.bpbatam.go.id/nongsapura/) provide terminal identity, facilities, routes and published aggregates. [B-SIMS](https://b-sims.bpbatam.go.id/) is an account-based port-service system for authorized users, not a public passenger-status API.
- **Ferry schedules:** “Check Schedules” validates six reviewed official pages: recurring operator timetables from [BatamFast](https://www.batamfast.com/tripschedule/index.ashx), [Sindo Ferry](https://app.sindoferry.com.sg/schedule/), [Majestic Fast Ferry](https://www.majesticfastferry.com.sg/) and [Horizon Fast Ferry](https://horizonfastferry.com.sg/), the [BP Batam passenger-terminal catalogue](https://batamport.bpbatam.go.id/pelabuhan-penumpang/), and the date-bound [Singapore Cruise Centre ferry board](https://singaporecruise.com.sg/schedule/ferries/). BP Batam supplies terminal context rather than departure slots. SCC rows are validated as same-day operations and never overwrite recurring operator timetables. Deployments remain responsible for the SCC site's stated linking/reuse terms.
- **Passenger processing:** the [Singapore Cruise Centre departure guide](https://singaporecruise.com.sg/departure-arrival/ferry/) publishes check-in and gate windows, while [Singapore ICA checkpoint information](https://www.ica.gov.sg/about-us/our-checkpoints) describes the official checkpoints. Neither source publishes a Batam-terminal live wait-time API.
- **Singapore road data:** [SLA OneMap](https://www.onemap.gov.sg/apidocs/) supports authoritative Singapore search and routing, and [LTA DataMall](https://datamall.lta.gov.sg/content/datamall/en/dynamic-data.html) offers key-gated traffic speed bands, incidents and images. These are integration options rather than evidence that the current browser fallback is live; OneMap routing also does not extend into Batam.
- **Maritime logistics:** [MPA OCEANS-X](https://oceans-x.mpa.gov.sg/) and the [Singapore Maritime Data Hub vessel-arrivals API](https://sg-mdh.mpa.gov.sg/vessel-arrivals/apis) provide subscribed maritime data. They can enrich cargo and vessel planning, but they do not provide Batam passenger queue minutes.
- **Reference photographs:** hotspot and terminal photographs are historical orientation aids, never congestion observations. Reusable examples include [Jalan Sudirman](https://commons.wikimedia.org/wiki/File:Jalan_Panglima_Besar_Sudirman,_Batam,_Riau_Islands.jpg), [Batam Centre](https://commons.wikimedia.org/wiki/File:Terminal_Ferry_Batam_Centre.JPG), [Sekupang](https://commons.wikimedia.org/wiki/File:Sekupang_Ferry_Terminal.jpg) and [Harbour Bay](https://commons.wikimedia.org/wiki/File:Harbour_Bay_Ferry_terminal.jpg). The hotspot catalogue retains the Commons author, licence, capture year and source. BP Batam page imagery has no clear reuse licence and requires permission before redistribution.

Published sailing times use the terminal's local timezone: Singapore is SGT
(UTC+8), while Batam is WIB (UTC+7). The optimizer normalizes timestamps for
calculation and should always display the local zone on a cross-border leg.

The protected route-learning store accepts only clear-weather, low-congestion
actual traversal observations from allowlisted map-matching pipelines. Inputs
are exact directed OSM edge keys tied to the SHA-256 of the committed graph;
stale graph observations are isolated automatically. Its API schema has no
route geometry, polyline, provider route, or provider duration fields. Google
Routes content is never accepted, cached, or persisted. A\* captures one
immutable snapshot per calculation and applies it only on clear-weather edges
whose network and local congestion scores are at most 25. Every response
publishes the baseline-versus-learned adjustment, model revision, gate state,
and selected-edge audit; external provider geometry strips those local claims.

An optional `POST /api/route-benchmark` comparison is disabled by default. It
uses a dedicated server-only Google Routes v2 key and sends coordinates to
Google only when the explicit enable flag is set. The field mask permits only
duration, distance and route labels—never geometry, polylines or steps—and the
response is `private, no-store`, cannot be persisted or used for training, and
cannot be drawn over the OpenStreetMap/CARTO map. Shortest-distance reference
routes are clearly labelled experimental. The frontend action is separately
opted in only when `VITE_ENABLE_GOOGLE_BENCHMARK=true`; it renders attributed
distance/duration text in its own card and never sends provider content to the
Leaflet map or browser storage.

The staged activation and camera-feed path is documented in
[`docs/LIVE_TRAFFIC_AND_FEED_PLAN.md`](docs/LIVE_TRAFFIC_AND_FEED_PLAN.md).

### Routing-intelligence backend

The refactored backend keeps graph search, traffic validation, shortcut review,
model inference, and persistence behind typed service boundaries. The local
Batam solver uses vehicle-constrained, turn-aware A* and publishes the
four-component edge objective
`time + distance + congestion + road-quality penalty` with per-vehicle weights
and explicit fallbacks for unrated roads. Crowd-sourced route tips are parsed
only from a server-owned pinned-source policy and remain inactive until a human
review produces an immutable approval for the current graph revision.

Typed spatial history stores timezone-aware `V_actual / V_free_flow` records
with fixed provenance and idempotent keys. The probabilistic random-forest
contract publishes empirical quantiles and calibration metadata, while a
transparent ferry-schedule adjustment stays separate from observed traffic.
Local SQLite remains the development default; optional server-only Supabase
durability is available through
[`backend/data/routing_intelligence.sql`](backend/data/routing_intelligence.sql).
See [Routing Intelligence Architecture](docs/ROUTING_INTELLIGENCE_ARCHITECTURE.md)
for the cost units, vehicle constraints, source-policy schema, retention,
review/promotion flow, RLS boundary, cache, and fallback behavior.

### Authentication and the credential boundary

Human sign-in (admins and drivers) is separate from the machine secret that
guards ingestion and retraining. `CROSSFLOW_ADMIN_TOKEN` remains a shared
deployment credential for backend-to-backend operations; it is never a login,
and presenting it as a user token is rejected.

Identity comes from Supabase Auth. Clients authenticate with Supabase
**directly**, so this API never receives a password and inherits Supabase's rate
limiting, lockout and password-reset handling. The API receives only the
resulting access token, verifies it, and resolves the caller's role from
`crossflow_profiles` — never from the request body, and never from the token's
own `user_metadata`, which users can write themselves.

Three credentials, three purposes. Confusing them is the one mistake that
disables every access-control policy while leaving the application apparently
working:

| Credential | Used by | Subject to row-level security |
|---|---|---|
| `SUPABASE_SECRET_KEY` | Backend-owned tables, ingestion, background jobs | **No — bypasses it entirely** |
| `SUPABASE_PUBLISHABLE_KEY` | Identifies the project on user-scoped calls | n/a — grants nothing alone |
| The caller's access token | Every admin and driver read or write | **Yes — this is the boundary** |

The separation is structural rather than conventional:
[`backend/auth/transport.py`](backend/auth/transport.py) never reads a secret
key from the environment, and a test asserts that it cannot. Apply
[`backend/auth/schema.sql`](backend/auth/schema.sql) and set
`CROSSFLOW_AUTH_MODE=supabase` enables the server-side auth routes; the browser
also needs matching `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
Until the server mode is enabled, every public corridor, ferry and model
endpoint keeps serving unauthenticated, and only the `/api/auth/*` routes report
that sign-in is unavailable. Auth is a per-route dependency, never middleware,
so an unreachable Supabase cannot blank the app.
See [Auth Backend Roadmap](docs/AUTH_BACKEND_ROADMAP.md).

---

## 🏗️ Technology Stack

| Component | Technology | Purpose |
|---|---|---|
| **Frontend UI** | React 18, Vite, TypeScript | Modern responsive dashboard |
| **Styling & Design** | Vanilla CSS Glassmorphism, Outfit & Inter Fonts | Bright light-mode aesthetic, micro-animations |
| **Interactive Maps** | Leaflet JS + CartoDB Voyager Tiles | Singapore-road, ferry-corridor and Batam-road leg visualization |
| **Road Graph & Routing** | Committed OpenStreetMap Batam graph + stateful A\*; OSRM/OSM for online access legs | Per-leg road geometry with source-labelled continuity fallback |
| **Multimodal Composition** | FastAPI journey composer + source-dated operator timetable snapshot | Legal terminal-pair selection, transfer timing and per-leg provenance |
| **Data Visualization** | Recharts | Congestion trend charts & emissions reduction bars |
| **Backend & AI Engine** | Python 3.12 baseline, FastAPI, uvicorn | High-performance REST API |
| **Machine Learning** | scikit-learn Random Forest, NumPy | Synthetic cold start plus optional typed spatial retraining, empirical tree quantiles and calibration metadata |

---

## 📁 Project Structure

```
├── backend/      # FastAPI + scikit-learn API and backend regression runner
│   └── auth/     # Human sign-in: schema, user-scoped transport, boundary tests
├── frontend/     # React 18 + Vite + TypeScript dashboard and Vitest checks
├── scripts/      # Local launcher, graph tooling, and optional training jobs
├── .github/      # GitHub Actions CI
└── docs/         # Pitch outline, hackathon reference, and the standalone
                  # auth sign-in demo page (docs/auth-signin-demo.html)
```

---

## 🚀 Quick Start

### Prerequisites
- Node.js v20.19+ (or v22.12+)
- Python 3.12+

### One command

```bash
./scripts/dev.sh
```

Installs anything missing (Python venv + backend requirements, and
`npm ci`), then runs the backend on **:8000** and the frontend on
**:3000** together. Ctrl+C stops both.

| | |
|---|---|
| Dashboard | <http://localhost:3000> |
| API docs | <http://localhost:8000/docs> |

Options: `--install-only`, `--backend-only`, `--frontend-only`, `--clean`,
and `--force-restart`.

---

## FastAPI route-planning contract

The interactive API is available at `http://localhost:8000` (OpenAPI UI at
`/docs`). Route planning has three POST endpoints:

| Endpoint | Request shape |
|---|---|
| `POST /api/optimize-route` | A named `corridor_id`, or both `origin_id` and `destination_id`, plus the required `vehicle_type`. |
| `POST /api/optimize-free-route` | Required `origin_lat`, `origin_lng`, `destination_lat`, and `destination_lng`; optional display names and route settings. |
| `POST /api/optimize-multi-stop-route` | An ordered `stops` array with 3–5 stops (origin, destination, and up to 3 intermediate stops). Each stop has `lat`, `lng`, optional `name`, and optional `dwell_mins`; `optimize_order` defaults to `false`. |

All three request types also accept `weather` (`0` clear, `1` rain, `2`
storm), `route_preference` (`BALANCED`, `FASTEST`, `SHORTEST`, `EASY`, or
`LOCAL`), and the legacy `hour` field (`0`–`23`, default `14`). For a precise
schedule, send exactly one of `departure_at` or `arrive_by` as a timezone-aware
ISO-8601 timestamp. They are mutually exclusive, and a timestamp without an
offset is rejected. `+07:00` (Batam) and `+08:00` (Singapore) are accepted;
the solver normalizes them for calculation and returns timezone-aware times.
If neither timestamp is provided, `hour` remains the backwards-compatible
hour-of-day mode. An `arrive_by` request searches for the latest feasible
departure in a bounded 48-hour window and returns HTTP 400 when no feasible
departure is found.

Example departure-time request:

```json
{
  "origin_lat": 1.0605,
  "origin_lng": 104.0303,
  "destination_lat": 1.1318,
  "destination_lng": 104.0554,
  "vehicle_type": "COMMUTER",
  "departure_at": "2026-08-15T08:30:00+07:00",
  "route_preference": "BALANCED"
}
```

To request an arrival deadline, replace `departure_at` with (not in addition
to) an `arrive_by` value, for example
`"arrive_by": "2026-08-15T10:00:00+07:00"`. A multi-stop body uses the same
scheduling fields:

```json
{
  "stops": [
    {"lat": 1.1630, "lng": 104.0025, "name": "Batu Ampar"},
    {"lat": 1.1465, "lng": 104.0125, "name": "Nagoya Hill", "dwell_mins": 15},
    {"lat": 1.1318, "lng": 104.0554, "name": "Batam Centre"}
  ],
  "vehicle_type": "LIGHT_TRUCK",
  "arrive_by": "2026-08-15T13:00:00+07:00"
}
```

Successful responses are flat envelopes: `generated_at`, `data_source`, and
`provenance` are returned alongside the route result. The route identity fields
are:

- `route_id`: the full 64-character lowercase SHA-256 content hash.
- `route_code`: a deterministic, driver-friendly 7-character code. It is an
  identifier only; it is not an access credential.

The route result includes `route_type`, `planned_departure`,
`estimated_arrival`, `estimated_travel_time_mins`, `total_eta_mins`,
`route_geometry` (latitude/longitude pairs), `legs` where applicable, and a
`scheduling` object. The scheduling object reports `mode` (`HOUR`, `DEPART_AT`,
or `ARRIVE_BY`), the requested timestamp, and `deadline_slack_mins` when an
arrival deadline was used. Representative response excerpt:

```json
{
  "generated_at": "2026-08-15T01:30:04+00:00",
  "data_source": "simulated",
  "provenance": {"road_network": "OpenStreetMap Batam Extract", "traffic": "Historical & Telemetry Model"},
  "route_id": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "route_code": "7KQ4MNP",
  "route_type": "ROAD_ROUTE",
  "planned_departure": "2026-08-15T08:30:00+07:00",
  "estimated_arrival": "2026-08-15T08:56:00+07:00",
  "estimated_travel_time_mins": 26.0,
  "total_eta_mins": 26,
  "route_geometry": [[1.0605, 104.0303], [1.1318, 104.0554]],
  "scheduling": {
    "mode": "DEPART_AT",
    "requested_departure_at": "2026-08-15T08:30:00+07:00",
    "requested_arrive_by": null,
    "deadline_slack_mins": null
  }
}
```

The excerpt is shortened; multimodal and multi-stop responses add their
normal `legs`, ferry, stop, and navigation fields. Use the actual
`route_id` or `route_code` to retrieve a persisted response:

```http
GET /api/routes/7KQ4MNP
X-CrossFlow-Route-Token: <CROSSFLOW_ROUTE_READ_TOKEN>
```

The equivalent `/routes/{route_id}` path is also available. Retrieval accepts
either the 64-character hash or 7-character code and authorizes a valid
configured `X-CrossFlow-Route-Token`; deployments may also use the existing
`X-CrossFlow-Admin-Token` compatibility path (and the admin path is used when
no route-read token is configured). Route IDs/codes alone do not authorize
access. Responses are marked `Cache-Control: private, no-store`.

---

## 🔧 Manual Setup

### 1. Frontend Web Application Setup
```bash
cd frontend

# Install the locked Node dependencies
npm ci

# Launch frontend dev server on http://localhost:3000
npm run dev
```

### 2. Python AI Backend Setup
```bash
# Create & activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install backend dependencies
pip install -r backend/requirements.txt

# Run backend API server on http://localhost:8000
python backend/main.py
```

### 3. Run the primary local verification

The commands below mirror the frontend and backend checks in GitHub Actions.
The backend function-style tests require the CI-only `pytest` package, so install
it into the virtual environment if it is not already present. The commands use
POSIX paths; on Windows, use the equivalent `.venv\\Scripts\\python.exe` paths
from PowerShell, or run them under WSL/Git Bash.

```bash
# Frontend typecheck/build, lint, and regression tests
cd frontend
npm run lint
npm test
npm run build
cd ..

# Backend regression checks and launcher validation
.venv/bin/python -m compileall -q backend scripts
.venv/bin/python backend/test_backend.py
.venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py'
# Once, if needed, install the CI-only function-test runner
.venv/bin/pip install pytest
(cd backend && ../.venv/bin/python -m pytest tests -q)
.venv/bin/python -m unittest discover -s backend/auth/tests -t . -p 'test_*.py'
bash -n scripts/dev.sh
```

The verification suite covers A\* path validity and preference-aware admissibility, all
eight vehicle profiles, all five route-objective catalogs and API validation,
physical-distance versus weighted-objective and modeled-ETA units, requested-hour and
weather weighting, spatial congestion decay and caching, all named planner
pairs, free-point routing and browser fallback, road alternatives, manoeuvres,
roundabouts, access barriers, verified traversal ingestion isolation,
idempotency, graph revisioning, immutable learning snapshots, clear-road gates,
physical speed clamping, learned-path cache invalidation and selected-edge-only
shortcut audits, the opt-in metric-only Google benchmark request/parser,
server-key isolation and no-store failure behavior, timezone-safe Batam schedules, ferry boarding
cutoffs, determinism, and dashboard consistency invariants. GitHub Actions runs
these commands for every push and pull request.

### 4. Optional configuration

The application runs with simulated traffic and local OSM routing when no
secrets are configured. Set only the integrations you use:

| Variable | Purpose |
|---|---|
| `VITE_API_BASE_URL` | API origin for a split frontend/backend deployment; local Vite and the bundled Vercel config use same-origin `/api` by default. |
| `VITE_SUPABASE_URL` | Supabase project URL used by the React sign-in flow. It must identify the same project as the server-side `SUPABASE_URL`. |
| `VITE_SUPABASE_PUBLISHABLE_KEY` (or legacy `VITE_SUPABASE_ANON_KEY`) | Browser-safe Supabase key for direct sign-in and token refresh. Never put `SUPABASE_SECRET_KEY` or `SUPABASE_SERVICE_ROLE_KEY` in a `VITE_` variable. |
| `VITE_TEST_ADMIN_EMAIL` + `VITE_TEST_ADMIN_PASSWORD` | Optional shared test administrator shown as a one-click sign-in. These values are embedded in the public browser bundle by design. Use them only with a disposable demo Supabase project containing no production data; leave either blank to hide the button. The account must already exist and have `role = 'admin'` in `crossflow_profiles`. |
| `VITE_ENABLE_GOOGLE_BENCHMARK` | Must equal `true` to show the optional text-only “Compare online” action. This is a UI gate, never an API key; the server gate and dedicated server key below are also required. |
| `CROSSFLOW_AUTH_MODE` | `disabled` by default locally; set to `supabase` after applying `backend/auth/schema.sql` to enable the per-route auth checks. Vercel pins this to `supabase`. |
| `TOMTOM_API_KEY` | Optional TomTom flow-segment layer used by `/api/live-traffic`. |
| `SUPABASE_URL` + `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`) | Server-only Supabase credentials. Apply `backend/data/ferry_freshness.sql` for shared ferry verification, and optionally `backend/data/routing_intelligence.sql` for durable typed traffic observations and shortcut review/approval records. Ferry freshness fails closed in production when required durability is unavailable; routing intelligence remains optional and must report its actual storage boundary. Never expose the secret/service-role key. Official `*.supabase.co` hosts are accepted; set `CROSSFLOW_SUPABASE_ALLOWED_HOST` only for an audited self-hosted HTTPS origin. |
| `CROSSFLOW_REQUIRE_DURABLE_FERRY_FRESHNESS` | Durability gate. `vercel.json` pins it to `1` for Preview and Production so shared freshness cannot silently degrade to process memory. Use `0` only for local committed-snapshot fallback. |
| `CROSSFLOW_HISTORY_DB` | Writable SQLite path for legacy congestion history and typed spatial speed observations. API metadata distinguishes a persistent file from serverless `/tmp` or memory fallback. |
| `CROSSFLOW_SPATIAL_HISTORY_RETENTION_DAYS` | Typed spatial-observation retention. Defaults to `1825` (five years); accepted values are `365` through `7300`. |
| `CROSSFLOW_ROUTING_INTELLIGENCE_STORE` | `local` by default. Set to `supabase` only after applying `backend/data/routing_intelligence.sql`; ordinary ferry Supabase credentials never implicitly probe this optional schema. |
| `CROSSFLOW_TRAFFIC_INGEST_SOURCE` | Server-owned identity for the admin-only spatial-observation endpoint (`loop_sensor`, `probe_gps`, `tomtom_live`, or `verified_traffic_observation`). Request bodies cannot self-assign verified provenance. |
| `CROSSFLOW_SHORTCUT_REVIEWER_ID` | Server-owned reviewer identity recorded on immutable shortcut approvals. The request body cannot spoof this audit field. |
| `CROSSFLOW_SHORTCUT_SOURCE_POLICY` | Server-owned JSON allowlist of exact pinned HTTPS sources, MIME types, confidence ceilings, and resource limits. Blank disables source fetching. Parsed tips always enter `REVIEW_REQUIRED`; this variable cannot enable activation. |
| `CROSSFLOW_ROUTE_PROVIDER` | `local` by default. The legacy `supabase` value fails fast because its RPC cannot prove vehicle constraints. Only `supabase_v2_constrained` may be enabled after a replacement RPC returns the complete constraint, navigation and alternative-route contract; all failures retain the local OSM route. |
| `CROSSFLOW_ALTERNATIVE_MAX_SEARCHES` | Optional upper bound for synchronous alternative-route A* attempts; default `3`. |
| `CROSSFLOW_ALTERNATIVE_TIME_BUDGET_MS` | Optional wall-clock budget for alternative-route search; default `10000` ms. |
| `CROSSFLOW_ALTERNATIVE_MAX_SETTLED_STATES` | Optional settled-state cap for alternative-route search; default `250000`. |
| `CROSSFLOW_ROUTE_LEARNING_DB` | Writable SQLite path for verified traversal observations. A configured persistent volume is durable; serverless `/tmp` is explicitly reported as ephemeral. |
| `CROSSFLOW_ADMIN_TOKEN` | Server-only token for protected ingestion, review/promotion, persistence status and retraining operations through the `X-CrossFlow-Admin-Token` header. An unset token denies access; do not reuse a Supabase credential. |
| `CROSSFLOW_ROUTE_DB` | SQLite path for persisted, content-addressed route responses. Configure a durable volume when drivers must retrieve routes across restarts. |
| `CROSSFLOW_ROUTE_READ_TOKEN` | Optional deployment-scoped token accepted by `X-CrossFlow-Route-Token` for driver route retrieval. Route IDs are identifiers, not credentials. |
| `CROSSFLOW_ENABLE_GOOGLE_BENCHMARK` | Must equal `true` to expose the optional, text-only `/api/route-benchmark` comparison. Disabled by default. |
| `CROSSFLOW_GOOGLE_ROUTES_API_KEY` | Dedicated server-only Google Routes v2 key for the opt-in benchmark. Browser-prefixed and legacy Google keys are never read. |
| `SUPABASE_DB_URL` | Optional PostgreSQL URL used only by ingestion/training scripts. Never expose it to the frontend. |

When using `scripts/dev.sh`, non-empty server variables are read from a root
`.env` file (or an already-exported shell variable). Vite browser variables
must be placed in `frontend/.env.local` or configured in the frontend deploy
environment; restart Vite after changing them. Keep the browser publishable key
and the server `SUPABASE_URL` pointed at the same Supabase project.

### 5. Optional training tooling

Training dependencies are deliberately separate from the production API so
Pandas, Modal, and PostgreSQL drivers do not bloat serverless deployments:

```bash
pip install -r scripts/requirements-training.txt
python scripts/train_local_mac.py

# Optional Modal jobs
modal run scripts/train_traffic_model.py
modal run scripts/tune_hyperparams.py
```

With no database URL, training writes a clearly named synthetic demo artifact.
The hourly database synchronizer only consumes a model trained from Supabase.
Hyperparameter tuning uses CPU workers and defaults to 12 trials (hard-capped at
24 via `CROSSFLOW_TUNING_TRIALS`) to prevent accidental large GPU fan-outs.

### 6. Rebuilding the road graph (optional)

The graph is committed, so this is only needed to refresh OSM data:
```bash
.venv/bin/python scripts/build_graph.py
```

### 7. Navigation scope

CrossFlow provides a corridor-planning preview, not certified live navigation.
The committed Batam graph filters explicit prohibited/private motor access and
hard barriers and retains mode, width, height, weight, surface and smoothness
tags for runtime checks when OSM supplies them. Missing tags and
turn-restriction relations remain unknown and cannot certify legal clearance.
Online Singapore and cross-border road legs depend on OSRM; the offline
continuity connector is explicitly not turn-by-turn road geometry. Vehicle
selection changes road-leg preferences, modelled speed, congestion/weather
sensitivity, terminal handling and emissions, but does not certify permission
to carry the vehicle on a passenger ferry.

Cross-border results use only listed terminal pairs and published sailing
evidence. Their sea geometry is a corridor visualization, not a navigational
track, and the schedule snapshot does not establish a current gate, seat,
cancellation, immigration wait or vehicle/freight acceptance. Heavy freight,
dangerous goods and accompanied vehicles require a separate authorized cargo
or operator workflow. Always verify the sailing and obey posted road, port and
border requirements.

---

## 📊 Judging Criteria Alignment

- **Problem Understanding & Relevance**: Covers 30 representative Batam mobility pressure points, including Simpang Kabil, Mukakuning Industrial and Batu Ampar Port, while connecting Singapore origins and destinations through the ferry interface. The priority labels are planning outputs, not claims of current official severity.
- **Technical Execution & Engineering Quality**: TypeScript React frontend + FastAPI service, stateful vehicle- and preference-aware A\* over a committed Batam OSM graph, per-leg SG–ferry–Batam journey composition, and a browser fallback that preserves source and limitation labels when online routing is unavailable.
- **Innovation & Creativity**: Cross-modal departure planning couples road access with a matching published ferry window while keeping schedule evidence distinct from live operating status.
- **Impact & Feasibility**: Models roughly **540 kg CO2 per day** of avoidable idle emissions across five corridors, under assumptions published in the API response (40 advised trips/corridor/hour, 35% of queue delay avoidable, 1.8 kg/h idle burn). These are modelled projections from a simulated traffic layer, not measured outcomes.
- **Presentation & Demo**: Responsive light-theme UI and honest data-provenance labelling throughout; the stage pitch remains documented as an outline in [`docs/PITCH_DECK_OUTLINE.md`](docs/PITCH_DECK_OUTLINE.md).
