# 🎤 CrossFlow AI — Stage Pitch Deck & 3-Minute Demo Script

> This file is a presentation outline and demo script. The current React app
> does not include a built-in pitch-deck mode; use the dashboard alongside this
> outline for the live presentation.

> **Batam-Singapore Hackathon 2026**  
> **Track 2: Ease of Living & Sustainability — Smart Mobility Flow**

> **Presenter note:** this script deliberately does **not** quote specific
> on-screen numbers. Congestion values move with the clock and drift between
> polls, so any figure written here would contradict the screen by the time you
> read it. Describe what each number *means* and read the live value aloud.
> (The previous version of this script quoted 78.4 / 18.5 min / 3.4 kg — those
> were literals from the offline fallback path, so they only ever appeared when
> the backend was switched off.)

---

## 🎯 3-Minute Live Demo Walkthrough

### 0:00 – 0:45: The Problem & Regional Corridor
* **Visual**: Open the **Live Corridor Map**. Point out that the corridors trace
  the actual Batam road network — Mukakuning down through Simpang Kabil to
  Batam Centre — not straight lines between pins.
* **Script**: "Batam's growth as an industrial and logistics hub has created a
  cross-border bottleneck. Commuters and freight leaving Mukakuning get trapped
  in rush-hour delays, miss ferry connections at Batam Centre, and idle engines
  burn fuel while they wait. Existing tools treat road traffic and ferry
  timetables as separate problems."
* **If asked where the roads come from**: OpenStreetMap, 115,320 union-retention nodes, routed
  with a turn-aware A\* search and a haversine lower bound. The selected route
  is exact for its nonnegative objective; `SHORTEST` minimizes physical distance
  while the other preferences optimize their published weighted objectives.

### 0:45 – 1:45: The Departure Window Solver
* **Visual**: **Smart Route & Departure Solver**. Select the Mukakuning corridor
  with **Freight Truck**. Drag the departure hour into the evening peak (18:00)
  and click **Compute AI Route Recommendation**.
* **Script**: "Our model forecasts corridor congestion 30 and 60 minutes ahead.
  Watch what happens when I move departure into the evening peak — the index
  roughly doubles, and the solver switches its recommendation to defer. Read the
  time saved off the screen. Then it does the part mapping apps don't: it checks
  which ferry you can still board after that delay, including the 15-minute
  boarding cutoff."
* **Strong beat**: toggle **Heavy Storm** and let the recommendation change live.

### 1:45 – 2:30: Ferry & Operations Intelligence
* **Visual**: **Ferry & Port Intelligence**, then **Operations & Carbon Analytics**.
* **Script**: "For port and city operators this is one pane of glass. Sailings
  always look forward from right now across Batam Centre, Sekupang and
  HarbourBay. Dispatch alerts are generated from modelled corridor state; an
  optional server-side TomTom adapter can add a labelled live traffic
  observation when configured. And the
  carbon figure is accrued through the day from modelled queue delay — with the
  assumptions published in the API response, not baked into a slide."
* **Honesty beat, say it before you're asked**: "Road geometry and routing are
  real OpenStreetMap data. The default traffic layer is simulated — there is no
  documented public Batam segment-speed feed. When configured, the server-side
  TomTom adapter can provide labelled point-flow observations; ferry schedules
  remain published snapshots rather than live operator status."

### 2:30 – 3:00: Stage Pitch & Summary
* **Visual**: the dashboard alongside this outline; there is no built-in pitch
  deck button in the current React app.
* **Script**: "CrossFlow AI is built specifically for the Batam-Singapore
  corridor — real road network, real pathfinding, an honest default simulation
  layer, an optional labelled live-traffic adapter, and a clear line between
  the two. Thank you."

---

## 📌 Pitch Deck Slide Breakdown

### Slide 1: Unclogging the Batam-Singapore Corridor
- **Headline**: AI-Powered Mobility & Synchronized Cross-Border Logistics
- **Problem 1**: Mukakuning & Simpang Kabil suffer heavy rush-hour bottlenecks.
- **Problem 2**: Uncoordinated freight departure causes missed ferry connections
  at Batam Centre & HarbourFront SG.
- **Problem 3**: Idling freight burns fuel and increases carbon emissions.

### Slide 2: Architecture & Regional Impact
- **Road Network**: 115,320-node schema-v3 OpenStreetMap graph of Batam, routed with a
  hand-implemented, turn-aware A\* and an admissible haversine lower bound.
- **Forecasting**: scikit-learn Random Forest predicting 30/60-minute corridor
  congestion, trained on a synthetic Batam traffic profile by default, with an
  optional server-side TomTom point-flow adapter when configured.
  *(Not XGBoost — earlier drafts of this deck said so in error.)*
- **Route Engine**: Vehicle-aware departure window solver (Car, Freight Truck,
  Express Van) with customs buffers and ferry boarding cutoffs.
- **Modelled Impact** — projections under stated assumptions, not measurements:
  - **~540 kg CO2/day** of avoidable idle emissions across five corridors,
    at 40 advised trips/corridor/hour, 35% of queue delay avoided, 1.8 kg/h
    idle burn.
  - Departure deferral moves trips out of the modelled 08:00 and 18:00 peaks,
    where the congestion index roughly doubles.
  - Connections are only offered when reachable before boarding closes.

---

## 🛡️ Likely Judge Questions

**"Is this real data?"**
Road network and routing: yes, OpenStreetMap. The default forecast is simulated
and the UI says so. There is no documented municipal training feed; the optional
commercial TomTom point-flow adapter is labelled live only after a successful request.

**"What's the model's accuracy?"**
Unmeasured, deliberately. It's trained on a synthetic profile, so scoring it
against that generator would just measure the formula against itself. We'd need
real corridor telemetry to make an accuracy claim worth anything.

**"Why implement A\* instead of using a routing library?"**
The direct implementation keeps vehicle constraints, turn state, route
preferences, alternatives, caching, and provenance under one contract. The
great-circle lower bound is admissible, while exactness is defined against the
selected objective rather than a universal notion of shortest.

**"How hard is it to go live?"**
The default demo path is synthetic, but the backend already has a server-side
TomTom point-flow adapter behind `TOMTOM_API_KEY`. Any additional source still
has to preserve the same provenance and freshness contracts; ferry data remains
source-dated published schedule information.
