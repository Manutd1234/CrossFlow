---
name: run-api
description: Install dependencies and run the CrossFlow AI FastAPI backend, then exercise its endpoints to verify a change
---

# Run the CrossFlow AI API

CrossFlow AI is a FastAPI service. This repository is backend-only — the Vite
dashboard was removed, so there is no UI to launch or screenshot.

One command sets up and starts everything:

```bash
./scripts/dev.sh
```

It installs anything missing (Python venv + `backend/requirements.txt`), starts
the API on **:8000**, and waits until it answers. **Ctrl+C stops it** — verified
through a real pty, including grandchildren.

| Flag | Purpose |
|---|---|
| *(none)* | Install if needed, run the API |
| `--install-only` | Set up dependencies and exit |
| `--clean` | Delete `.venv` and reinstall |
| `--force-restart` | Kill whatever holds :8000, no prompt |

Calling this again while CrossFlow is already up is safe and the normal way to
invoke it from an agent: a port already serving a healthy instance is reused,
not treated as an error, so plain `./scripts/dev.sh` is idempotent.

If :8000 is busy with something that **isn't** a healthy CrossFlow instance, the
script asks before killing it in an interactive terminal. Run from an agent (no
real tty), it can't ask, so it fails with a one-line message instead of hanging
— pass `--force-restart` to kill the stale occupant and continue.

Run it in the **foreground** when you want Ctrl+C to work. Bash sets SIGINT to
*ignored* for background jobs, and a signal inherited as ignored cannot be
trapped — so `./scripts/dev.sh &` will not clean up on Ctrl+C. To stop a
backgrounded run, send `SIGTERM` (that path is trapped and works).

## What you get

- API docs (Swagger) — <http://localhost:8000/docs>

## Verifying a change

Drive the endpoints directly. The solver is the slow part: every route runs a
full A\* over a 115k-node graph in pure Python, so a cold two-point route takes
seconds and a multi-stop journey takes roughly that per leg. Use generous
timeouts rather than assuming a hang.

```bash
# Two-point route
curl -s -m 120 -X POST http://localhost:8000/api/optimize-free-route \
  -H 'Content-Type: application/json' \
  -d '{"origin_lat":1.1465,"origin_lng":104.0125,
       "destination_lat":1.1318,"destination_lng":104.0554,
       "vehicle_type":"COMMUTER","hour":14,"weather":0}'

# Multi-stop journey (3-8 ordered stops, optional per-stop dwell)
curl -s -m 300 -X POST http://localhost:8000/api/optimize-multi-stop-route \
  -H 'Content-Type: application/json' \
  -d '{"stops":[{"lat":1.1480,"lng":104.0060,"name":"Batu Ampar"},
                {"lat":1.1465,"lng":104.0125,"name":"Nagoya Hill","dwell_mins":20},
                {"lat":1.1318,"lng":104.0554,"name":"Batam Centre"}],
       "vehicle_type":"COMMUTER","hour":9}'

# Corridor telemetry and corridor road geometry
curl -s -m 30  http://localhost:8000/api/corridors
curl -s -m 120 http://localhost:8000/api/corridor-routes
```

**Check `route_data_source` on every result.** A value of
`offline_access_estimate` or `multimodal_offline_estimate` means a road engine
was unreachable and the leg is an approximate connector, not a road path — the
response is still 200, so a passing request does not mean real routing happened.
For Singapore legs that means OSRM; confirm outbound TLS is healthy with:

```bash
cd backend && ../.venv/bin/python -c "from services import tls; print(tls.trust_store_status())"
```

`ca_certificate_count: 0` means every outbound HTTPS call will fail
verification and silently degrade to estimates.

## Checks worth running after a change

```bash
.venv/bin/python backend/test_backend.py    # 146 checks, exits non-zero on failure
```

Regressions that were real bugs here, worth re-checking if you touch the solver
or polling:

- Stop the backend mid-session — clients must degrade with labelled provenance,
  never present an estimate as a solved road route.
- A corridor's reported distance must come from A\* geometry, not a literal.
- Douglas-Peucker simplification must measure against the *retained* segment;
  measuring against immediate neighbours once collapsed a 15 km route to a
  2-point straight line.

## Gotchas

- `main.py` uses bare imports (`from models.congestion_model import ...`), so
  uvicorn **must** start from inside `backend/`. `dev.sh` handles this; doing it
  by hand from the repo root fails.
- `backend/data/batam_graph.json` (the OSM road graph) is committed. It is only
  rebuilt deliberately: `.venv/bin/python scripts/build_graph.py`. Nothing at
  runtime calls Overpass — venue wifi must not be on the demo's critical path.
- `dev.sh`'s port check must filter to `-sTCP:LISTEN`. Plain `lsof -ti :$port`
  also matches sockets that merely reference the port without listening on
  it — e.g. a browser's `CLOSED` connection that happened to use it as an
  ephemeral local port. Hit this for real: `lsof -ti :8000` returned a Chrome
  helper PID ahead of the actual uvicorn listener, and `--force-restart` would
  have killed the browser instead of the stale server.
