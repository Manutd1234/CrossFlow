# Routing Performance and ALT Implementation Plan

## Status and scope

This plan improves the Batam backend road router without weakening the current
vehicle, access, weather, congestion, route-preference, learning, or approved
shortcut contracts. It targets the stateful A* implementation in
`backend/services/router.py` and the duplicate route orchestration in
`backend/services/route_solver.py`.

The first production algorithmic upgrade is directed ALT (A* with Landmarks
and Triangle inequality). Learned multi-step guidance is an optional later
experiment, after the deterministic search path is measured and optimized.
MLD or Customizable Contraction Hierarchies are explicitly out of scope until
ALT results provide evidence that a larger index and customization pipeline is
needed.

Implementation status in this workspace: benchmark harness, canonical routing
view/cache, static edge features, directed ALT artifacts/runtime fallback,
primary-search reuse, and bounded synchronous alternative search are
implemented. Asynchronous delivery and learned guidance remain follow-up work.

The user-supplied local measurements are the initial reference points, not CI
assertions:

- Muka Kuning to Batam Centre: about 2.6 seconds from a cold process.
- Sekupang to Hang Nadim: about 11.6 seconds with the routing core warm.
- Coordinate snapping: about 0.17 seconds.

## Correctness invariants

Every delivery slice must preserve these properties:

1. The returned primary path minimizes the selected nonnegative objective in
   exact mode. Tests compare objective cost, not node sequence, because equal-
   cost paths may legitimately differ.
2. Search state remains `(node, previous_source, incoming_road_index)` because
   maneuver cost depends on the incoming arc. ALT may be node-based, but it
   must never collapse the search state to a node-only best score.
3. Vehicle access, node barriers, dimensions, weights, destination-only access,
   one-way edges, and approved-override applicability remain enforced.
4. Congestion, weather, turn, uncertainty, road-quality, avoidance, and
   alternative penalties remain nonnegative additions. They are not included
   in the landmark lower-bound metric.
5. The reported physical distance, modeled duration, generalized cost,
   navigation, provenance, and override audit continue to be computed from the
   exact selected `RoadEdge` sequence.
6. A missing, stale, corrupt, or inapplicable ALT artifact degrades to the
   existing haversine heuristic; it must not make routing unavailable.
7. Initially, any nonempty approved shortcut snapshot uses haversine. Added or
   cheaper edges can invalidate a committed-graph ALT bound. A later
   snapshot-versioned landmark index may remove this fallback.
8. Learned predictions may prioritize work or supply a valid incumbent route,
   but exact mode may not prune an edge solely because a model did not predict
   it.

## Delivery 0: repeatable baseline and search diagnostics

Add `scripts/benchmark_router.py` as a deterministic local benchmark. It should
run primary-only and primary-plus-alternatives cases separately and emit both a
human-readable table and JSON. Include at least:

- Muka Kuning to Batam Centre;
- Sekupang to Hang Nadim;
- one short urban route;
- `COMMUTER`, `MOTORCYCLE`, and `CARGO_TRUCK` coverage;
- cold process, warm routing view, and warm result-cache measurements; and
- clear conditions plus one congested/weather case.

Add opt-in `SearchDiagnostics` counters around `astar_detailed`:

- heap pushes and pops;
- settled states;
- vehicle edge and node eligibility decisions;
- full edge-cost evaluations;
- heuristic evaluations;
- ALT landmark terms used or skipped;
- ALT fallback reason; and
- alternative A* attempts and accepted candidates.

Diagnostics must be absent or near-zero-overhead in normal requests. Wall-clock
thresholds should not run as hard CI checks; correctness and operation-count
checks can run in CI, while benchmark JSON is used for before/after review.

Acceptance:

- The benchmark is reproducible from a documented command.
- A primary-only measurement cannot accidentally include alternative searches.
- Baseline results capture graph SHA-256, Python version, scenario, vehicle,
  preference, and cold/warm state.

## Delivery 1: canonical cache key and reusable vehicle routing view

### 1.1 Canonicalize the core cache

The current `lru_cache` sees `_main_routing_core("COMMUTER")` and
`_main_routing_core("COMMUTER", None)` as different calls. Introduce one
noncached normalization boundary and one private cached builder. The cached key
must always contain:

- canonical API vehicle-profile key; and
- one canonical override identity (`None` for no active overrides, otherwise
  the immutable revision-bound snapshot).

Keep a compatible `_main_routing_core(...)` facade for current diagnostics and
tests, including an explicit way to clear the underlying cache in graph-fixture
tests.

### 1.2 Retain the work already done while building the core

Replace the discarded filtered adjacency with an immutable `RoutingView`
cached by the same canonical key. The view should contain:

- the mutually reachable snap core;
- normally eligible outgoing `RoadEdge` objects;
- reverse adjacency needed by core construction and ALT preprocessing;
- normally eligible outgoing-choice counts for turn decisions; and
- the static edge features introduced in Delivery 2.

`astar_detailed` and `snap_to_graph` should consume the same view. This removes
per-expansion traversal checks for ordinary edges and prevents snapping/search
topology drift.

Destination-only endpoint access is query-dependent and must remain a narrow
exception. Preserve a raw/rejected-edge path for edges within
`ENDPOINT_DESTINATION_ACCESS_RADIUS_M`; do not place destination-only edges in
the globally eligible adjacency. Approved snapshot edges must be present only
in views built for that exact snapshot.

Acceptance:

- Equivalent one-argument and explicit-`None` calls produce one cache miss.
- Every existing vehicle-core, destination-access, override, and route-cache
  test passes.
- On a matrix of seeded routes, the new view and the current implementation
  have equal optimal objective cost and physical validity.
- Warm primary routing performs no repeated ordinary-edge eligibility checks;
  only endpoint exceptions may invoke dynamic checks.

## Delivery 2: precompute static edge and turn inputs

Add an immutable per-vehicle `StaticEdgeFeatures` record to each routing view.
Precompute values that do not depend on the request:

- physical lower-bound seconds at vehicle maximum speed;
- class/maxspeed planned free-flow seconds;
- distance proxy;
- road-quality score and provenance;
- ordinary and `prefer_through_roads` suitability multipliers;
- congestion-class and weather-class exposure;
- directed-edge bearing; and
- source-node signal flag and normally eligible outgoing-choice count.

Keep request-dependent work in `_edge_cost_components`: learning-snapshot
adjustment, local/global congestion, empirical speed ratios, weather level,
incoming/outgoing maneuver combination, uncertainty, and route-preference
weighting. Refactor `_turn_cost_s` to use precomputed bearings and the active
routing view rather than rescanning global `ROAD_ADJ` for every expansion.

Before switching the search, add parity tests that compare old and new edge
component dictionaries for representative road classes, vehicles, weather,
learning entries, congestion estimates, turn shapes, and approved edges.

Acceptance:

- Component values and final path objective costs match within a documented
  floating-point tolerance.
- The benchmark shows at least a 30% reduction in warm primary-search time or
  full edge-cost CPU time. If it does not, retain only changes justified by the
  counters and profile.

## Delivery 3: directed ALT landmark index

### 3.1 Offline artifact

Add `scripts/build_alt_index.py`. Build a deterministic index from the exact
committed graph and the ordinary eligible adjacency for each distinct vehicle-
eligibility topology. Map API vehicle profiles to a shared topology only when
their eligible nodes and directed edges are provably identical; otherwise give
the profile its own index. Start with 8 landmarks; benchmark 8, 12, and 16
before choosing the committed default.

Choose landmarks deterministically with a well-spaced/farthest-point strategy,
starting from the Batam Centre core anchor. For every landmark `L`, compute
directed shortest physical road distance in metres:

- `d(L, v)` on forward adjacency; and
- `d(v, L)` by running from `L` on reverse adjacency.

The artifact must contain:

- format and builder versions;
- exact `GRAPH_REVISION` SHA-256;
- graph schema version;
- eligibility-topology ID and its mapped API vehicle-profile keys;
- ordered node-ID table and landmark node IDs; and
- forward and reverse distance arrays.

Use a representation whose rounding cannot overstate the lower bound. Begin
with float64 arrays and measure load time, compressed size, and resident memory
before considering a smaller representation. Build-time validation must reject
unknown nodes, negative distances, inconsistent shapes, and a graph-revision
mismatch.

### 3.2 Runtime heuristic

For current node `v` and target `t`, calculate a physical-distance bound:

```text
h_alt_m(v, t) = max over usable landmarks L of
    d(L, t) - d(L, v)
    d(v, L) - d(t, L)

h_m(v, t) = max(0, haversine_m(v, t), h_alt_m(v, t))
```

Skip a term if any required directed distance is unreachable. Convert `h_m`
to the selected objective's units using the same maximum-speed and lower-bound
coefficient currently used by `heuristic`. This keeps the bound valid for
turn-state A*, congestion, weather, avoid nodes, edge penalties, and
learned free-flow values because the latter remain clamped to the physical
maximum-speed floor.

Load the artifact lazily once per process and validate its graph revision. The
runtime fallback reasons should distinguish: disabled, missing, stale,
corrupt, unsupported vehicle policy, and active approved shortcut snapshot.

### 3.3 Exactness oracle

Add a test-only zero-heuristic mode (Dijkstra over the same turn-state and edge
objective). For deterministic random node pairs and every route preference:

- verify the unscaled distance bound never exceeds exact shortest physical
  road distance on reachable samples;
- compare ALT A* final objective cost with zero-heuristic search;
- exercise one-way asymmetry and unreachable landmark terms;
- exercise congestion, weather, avoidance, and edge penalties; and
- verify stale/corrupt indexes and active shortcuts fall back to haversine.

Acceptance:

- No exact-mode objective mismatch against the oracle.
- Median settled states on long primary-only scenarios fall by at least 60%,
  or median primary-search wall time improves by at least 2x over the Delivery
  2 baseline.
- Index loading does not erase the cold-start gain. Record artifact size,
  one-time load time, and process memory in the benchmark report.

## Delivery 4: reuse the selected primary and decouple alternatives

### 4.1 Split primary computation from presentation

The current result cache includes `include_alternatives`, so requesting the
same selected route again with alternatives repeats primary work. Introduce an
internal immutable `RouteComputation` containing the exact `PathResult`, query
context, and primary payload. Layer caches as:

```text
normalized route query -> primary RouteComputation
primary RouteComputation + alternative policy -> alternatives
presentation metadata -> deep-copied API payload
```

Names and other presentation-only fields should not force a graph search cache
miss. Keep the public dictionary response copy-safe.

Update both `optimize_route` and `optimize_free_route` to retain `route_now` and
`route_later` computations, select one, and attach alternatives to that exact
selected primary. Do not invoke a third primary A* search.

Acceptance:

- Instrumented solver tests prove two primary searches for now/later and zero
  additional primary searches when alternatives are attached. `leg_mode`
  remains one primary search.
- Selected geometry, ETA, emissions, provenance, and alternative baselines are
  unchanged apart from allowed equal-cost path ties.

### 4.2 Give alternatives a separate latency contract

First add a synchronous search budget: maximum attempts, settled states, and
wall time. Return deterministic accepted candidates found within the budget.
Do not change the current response contract until the frontend supports a
partial state.

Status: implemented. Alternative generation defaults to three A* attempts, a
10-second wall-clock budget, and 250,000 settled states. Deployments can tune
these through `CROSSFLOW_ALTERNATIVE_MAX_SEARCHES`,
`CROSSFLOW_ALTERNATIVE_TIME_BUDGET_MS`, and
`CROSSFLOW_ALTERNATIVE_MAX_SETTLED_STATES`; direct route APIs also accept
per-request overrides. Near-duplicate candidates stop after two successive
similarity rejections. The primary search remains outside these limits.

Then, if product latency still requires it, add a two-stage API:

1. return the primary route with an `alternatives_status` token; and
2. fetch or stream alternatives using the immutable primary/query identity.

The frontend must distinguish `pending`, `complete`, `partial`, and
`unavailable`; an empty pending result must not be presented as proof that no
alternative exists. Add backend contract tests and
`RouteOptimizer.alternatives.test.tsx` coverage before enabling asynchronous
behavior.

Acceptance:

- Primary response latency is independent of the seven-value penalty schedule.
- Alternative work cannot delay or alter the already selected primary.
- Existing diversity, overlap, physical-edge, maneuver, and deterministic-
  ordering tests continue to pass for completed alternative searches.

## Delivery 5: optional learned multi-step guidance experiment

Do not begin this delivery until ALT and static preprocessing benchmarks are
recorded. Keep it behind a disabled feature flag and out of the default exact
path until it demonstrates incremental value over ALT.

### 5.1 Training data

Add an offline generator that samples origin/destination pairs and runs the
zero-heuristic/exact solver. Store graph revision, vehicle policy, route
preference, traffic/weather context, state, exact remaining objective, correct
next edge, next 3-5 edges, and corridor/waypoint labels. Split train/validation
by geographic corridors or destination regions, not random states from the
same routes, to reduce leakage.

### 5.2 First safe integration

Prefer a candidate-route policy first:

- predict a short edge/corridor sequence;
- validate it against the active routing view and complete it if possible;
- calculate its exact objective to obtain an incumbent upper bound; and
- let ALT A* prune only when `g + admissible_alt_h >= incumbent_cost`.

A bad prediction then costs inference time but cannot change the optimum.
Measure candidate validity, incumbent gap, expansions saved, inference time,
and end-to-end latency.

Policy probabilities or a learned cost-to-go may be secondary ordering inputs.
They may not be placed after `anchor_f` in a purely lexicographic tuple and then
claimed as a major search change: in that form they only break equal-`anchor_f`
ties. Any auxiliary-queue Multi-Heuristic A* implementation must state and test
its queue-selection, termination, and suboptimality bound. A weighted or
bounded mode must be explicit in the API and must never be labelled exact.

Acceptance for enabling any learned guidance:

- exact objective parity with guidance disabled/enabled;
- at least 20% additional p50 latency improvement over ALT on held-out long
  routes after model inference is included;
- no material p95 regression on short routes;
- model and dataset graph revisions are validated at load time; and
- automatic fallback to plain ALT on low confidence, invalid output, model
  error, or revision mismatch.

## Pull-request sequence

Keep the rollout reviewable and reversible:

1. **PR 1 - Measurement and cache identity:** benchmark harness, diagnostics,
   canonical `_main_routing_core` key, and regression tests.
2. **PR 2 - Routing view and static features:** retained eligible adjacency,
   endpoint exception path, edge feature parity, and turn-input precomputation.
3. **PR 3 - Directed ALT:** offline builder, versioned artifact loader,
   heuristic integration, oracle tests, and fallback telemetry.
4. **PR 4 - Primary reuse and alternatives budget:** layered computation cache,
   solver reuse, synchronous limits, and optionally the two-stage API/UI.
5. **PR 5 - Learned-guidance experiment:** dataset generator, candidate-route
   incumbent, feature flag, and benchmark report. Do not merge into the default
   path unless its acceptance gate is met.

After each PR, run the focused backend routing tests, the full backend suite,
and relevant frontend alternative tests. Attach benchmark JSON from the same
machine and Python build to the PR. If a phase misses its performance gate,
use the diagnostic counters to keep only independently valuable changes rather
than stacking unmeasured complexity.

## Decision gate after ALT

Stop after PR 3 and review:

- cold and warm p50/p95 latency;
- settled states and edge-cost evaluations;
- memory and landmark-index load cost;
- primary versus alternatives latency; and
- behavior with approved shortcuts.

Proceed to learned guidance only if primary search still dominates after ALT.
Consider MLD or Customizable Contraction Hierarchies instead when latency is
still insufficient across many dynamic queries and the team is ready to own a
larger preprocessing/customization subsystem.
