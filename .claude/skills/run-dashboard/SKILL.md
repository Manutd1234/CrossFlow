---
name: run-dashboard
description: Install dependencies, build, and run the CrossFlow AI dashboard (Vite frontend + FastAPI backend) together, then drive and screenshot it in a real headless browser to verify a change
---

# Run the CrossFlow AI dashboard

CrossFlow AI is a Vite/React frontend talking to a FastAPI backend. Start both
with `./scripts/dev.sh`, then drive the running app headlessly with
`.claude/skills/run-dashboard/driver.mjs` (see "Verifying a change in the
browser" below) — that script is the harness, not just `npm run dev` and a
window opening.

One command sets up and starts everything:

```bash
./scripts/dev.sh
```

It installs anything missing (Python venv + `backend/requirements.txt`, plus
`npm install`), starts the FastAPI backend on **:8000** and the Vite frontend on
**:3000**, waits until the API answers, and prints both URLs. **Ctrl+C stops
both** — verified through a real pty, including grandchildren like the `vite`
process under `npm`.

| Flag | Purpose |
|---|---|
| *(none)* | Install if needed, run both |
| `--install-only` | Set up dependencies and exit |
| `--backend-only` | API only, on :8000 |
| `--frontend-only` | UI only, on :3000 (falls back to demo data) |
| `--clean` | Delete `.venv` and `node_modules`, reinstall |
| `--force-restart` | Kill whatever holds :8000/:3000, no prompt |

Calling this again while CrossFlow is already up is safe and the normal way
to invoke it from an agent: a port already serving a healthy instance is
reused, not treated as an error, so plain `./scripts/dev.sh` is idempotent —
run it first before assuming the dashboard needs (re)starting.

If a port is busy with something that **isn't** a healthy CrossFlow instance,
the script asks before killing it in an interactive terminal. Run from an
agent (no real tty), it can't ask, so it fails with a one-line message
instead of hanging — pass `--force-restart` to kill the stale occupant and
continue non-interactively.

Run it in the **foreground** when you want Ctrl+C to work. Bash sets SIGINT to
*ignored* for background jobs, and a signal inherited as ignored cannot be
trapped — so `./scripts/dev.sh &` will not clean up on Ctrl+C. To stop a
backgrounded run, send `SIGTERM` (that path is trapped and works).

## What you get

- Dashboard — <http://localhost:3000>
- API docs (Swagger) — <http://localhost:8000/docs>

The frontend polls the backend every 8s and **silently falls back to locally
generated demo data** if it is unreachable, so the app looks alive either way.
The header badge is the tell: `LIVE` / `SIMULATED TELEMETRY` / `OFFLINE DEMO
DATA`. If you are testing backend behaviour, confirm the badge does not say
OFFLINE before trusting what you see.

## Verifying a change in the browser (agent path)

`driver.mjs` drives the running dashboard with headless Chromium and screenshots
every tab, the solved route, and the pitch deck — this is the harness, use it
instead of hand-rolling a Playwright script:

```bash
node .claude/skills/run-dashboard/driver.mjs <outdir> [scenario]
```

| scenario | what it does |
|---|---|
| *(omit, or `all`)* | all five below, in order |
| `map` | screenshot the Live Corridor Map tab |
| `route` | switch to the solver tab, click `Compute AI Route Recommendation`, screenshot the result |
| `ferry` | screenshot Ferry & Port Intelligence |
| `analytics` | screenshot Operations & Carbon Analytics |
| `pitch` | open the `Stage Pitch Deck` modal and screenshot it |

Screenshots land at `<outdir>/<scenario>.png`. It prints the telemetry badge
text, the screenshot paths, and any console/page errors, then exits non-zero
if there were errors — so a CI-style check is just the exit code. It resolves
Playwright from the npx cache itself (`~/.npm/_npx/*/node_modules/playwright`)
since Playwright is not a project dependency; if that glob comes up empty,
`npx playwright install chromium` first. Point it at a different backend with
`CROSSFLOW_URL=http://localhost:3000 node ...` (default already localhost:3000).

**Look at every screenshot.** A blank frame means the launch failed; a plain
blue map panel means tiles have not loaded yet (wait longer, or you are offline).

Verified this session: all five scenarios against a live `dev.sh` instance —
real OSM road geometry on the map, a computed route with ferry connections,
25 ferry sailings, the carbon analytics charts, and the 2-slide pitch deck —
zero console errors.

## Checks worth running after a change

```bash
.venv/bin/python backend/test_backend.py    # 23 checks, exits non-zero on failure
cd frontend && npm run build                # tsc + vite; noUnusedLocals will fail on stray vars
```

Regressions that were real bugs here, worth re-checking if you touch the map,
polling, or the solver:

- Zoom into the map, wait ~20s (2+ poll cycles) — the viewport must hold.
- The detail card and the feed list beneath it must show the same numbers.
- Compute a route, switch tabs, switch back — the result must survive.
- Stop the backend mid-session — badge flips to OFFLINE and the hour slider and
  weather buttons must still change the answer.

## Gotchas

- `main.py` uses bare imports (`from models.congestion_model import ...`), so
  uvicorn **must** start from inside `backend/`. `dev.sh` handles this; doing it
  by hand from the repo root fails.
- `backend/data/batam_graph.json` (the OSM road graph) is committed. It is only
  rebuilt deliberately: `.venv/bin/python scripts/build_graph.py`. Nothing at
  runtime calls Overpass — venue wifi must not be on the demo's critical path.
- Map tiles come from `basemaps.cartocdn.com`; offline you get a plain panel
  rather than an error, but the corridor geometry still draws.
- `dev.sh`'s port check must filter to `-sTCP:LISTEN`. Plain `lsof -ti :$port`
  also matches sockets that merely reference the port without listening on
  it — e.g. a browser's `CLOSED` connection that happened to use it as an
  ephemeral local port. Hit this for real: `lsof -ti :8000` returned a Chrome
  helper PID (an old closed connection to the backend) ahead of the actual
  uvicorn listener, and `--force-restart` would have killed the browser
  instead of the stale server.
