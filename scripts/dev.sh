#!/usr/bin/env bash
#
# Start CrossFlow AI: installs anything missing, then runs the FastAPI backend
# and the Vite frontend together. Ctrl+C stops both.
#
#   ./scripts/dev.sh                 # install if needed, run both
#   ./scripts/dev.sh --install-only  # set up dependencies and exit
#   ./scripts/dev.sh --backend-only  # API on :8000
#   ./scripts/dev.sh --frontend-only # UI on :3000 (falls back to mock data)
#   ./scripts/dev.sh --clean         # force a dependency reinstall
#   ./scripts/dev.sh --force-restart # kill whatever holds :8000/:3000, no prompt
#
# Calling this again while CrossFlow is already up is safe: a port already
# serving a healthy instance is reused rather than treated as an error, so
# re-running with no flags is idempotent.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
BACKEND_PORT=8000
FRONTEND_PORT=3000

RUN_BACKEND=1
RUN_FRONTEND=1
INSTALL_ONLY=0
CLEAN=0
FORCE_RESTART=0

for arg in "$@"; do
  case "$arg" in
    --install-only)   INSTALL_ONLY=1 ;;
    --backend-only)   RUN_FRONTEND=0 ;;
    --frontend-only)  RUN_BACKEND=0 ;;
    --clean)          CLEAN=1 ;;
    --force-restart)  FORCE_RESTART=1 ;;
    -h|--help)        sed -n '3,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; DIM=""; G=""; Y=""; R=""; N=""
fi
say()  { printf '%s\n' "${B}▸${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
die()  { printf '%s\n' "${R}✗${N} $*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------

if [ "$RUN_BACKEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ]; then
  command -v python3 >/dev/null || die "python3 not found. Install Python 3.12+."
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' || \
    die "Python 3.12+ is required (found $(python3 --version 2>&1))."
fi
if [ "$RUN_FRONTEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ]; then
  command -v node >/dev/null || die "node not found. Install Node.js 20.19+ or 22.12+."
  command -v npm >/dev/null || die "npm not found. Install Node.js 20.19+ or 22.12+."
  node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit((major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22 ? 0 : 1)' || \
    die "Node.js 20.19+ or 22.12+ is required (found $(node --version))."
fi

# `[ -r /dev/tty ]` only checks permission bits on the device node — it stays
# true even with no controlling terminal (e.g. run from an agent's non-pty
# shell), so it doesn't tell us whether a `read </dev/tty` would actually
# work. Try to open it for real: that's what fails with "Device not
# configured" when there's nothing to read from, and we want to know that
# *before* printing a prompt nobody can answer, not after.
HAVE_TTY=0
if ( : < /dev/tty ) 2>/dev/null; then
  HAVE_TTY=1
fi

# -sTCP:LISTEN matters: plain `lsof -ti :$port` also matches sockets that
# merely reference the port without listening on it, e.g. another process's
# CLOSED connection that happened to use it as an ephemeral local port. Without
# the filter, `head -1` can pick an unrelated PID — and --force-restart would
# kill it. Filtering to LISTEN gets the actual port owner every time.
port_pid() { lsof -ti :"$1" -sTCP:LISTEN 2>/dev/null | head -1; }
backend_healthy()  { curl -sf "http://localhost:$BACKEND_PORT/api/corridors" >/dev/null 2>&1; }
frontend_healthy() { curl -sf "http://localhost:$FRONTEND_PORT/" >/dev/null 2>&1; }

check_port() {
  local port="$1" label="$2" pid
  pid="$(port_pid "$port")" || true
  [ -z "$pid" ] && return 0
  warn "Port $port is already in use by PID $pid ($label), and it isn't answering as CrossFlow."
  # Reusing the port silently would mean demoing against a stale process
  # running old code, which is worse than failing here.
  if [ "$FORCE_RESTART" = 1 ]; then
    say "Killing PID $pid (--force-restart)"
  elif [ "$HAVE_TTY" = 1 ]; then
    printf '  Kill it and continue? [y/N] '
    read -r reply </dev/tty 2>/dev/null || reply=n
    case "$reply" in
      [yY]*) : ;;
      *) die "Port $port busy — stop that process or free the port first." ;;
    esac
  else
    die "Port $port busy (PID $pid) and no terminal to ask on. Re-run with --force-restart to kill it automatically, or: kill $pid"
  fi
  kill "$pid" 2>/dev/null || true; sleep 1
  [ -n "$(port_pid "$port")" ] && kill -9 "$pid" 2>/dev/null || true
  sleep 0.5
}

# --- dependencies ----------------------------------------------------------

if [ "$CLEAN" = 1 ]; then
  say "Removing existing dependencies (--clean)"
  [ "$RUN_BACKEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ] && rm -rf "$VENV"
  [ "$RUN_FRONTEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ] && rm -rf "$ROOT/frontend/node_modules"
fi

fingerprint() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

if [ "$RUN_BACKEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    say "Creating Python virtualenv"
    python3 -m venv "$VENV"
  fi

  requirements_hash="$(fingerprint "$ROOT/backend/requirements.txt")"
  requirements_stamp="$VENV/.crossflow-requirements.sha256"
  installed_requirements="$(sed -n '1p' "$requirements_stamp" 2>/dev/null || true)"
  if [ "$requirements_hash" != "$installed_requirements" ] || \
     ! "$VENV/bin/python" -c "import fastapi, uvicorn, sklearn, numpy" 2>/dev/null; then
    say "Installing backend dependencies"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$ROOT/backend/requirements.txt"
    printf '%s\n' "$requirements_hash" > "$requirements_stamp"
  else
    printf '%s\n' "${DIM}  backend dependencies present${N}"
  fi

  [ -f "$ROOT/backend/data/batam_graph.json" ] || warn \
    "backend/data/batam_graph.json missing — run: $VENV/bin/python scripts/build_graph.py"
fi

if [ "$RUN_FRONTEND" = 1 ] || [ "$INSTALL_ONLY" = 1 ]; then
  package_hash="$(fingerprint "$ROOT/frontend/package-lock.json")"
  package_stamp="$ROOT/frontend/node_modules/.crossflow-package-lock.sha256"
  installed_package_hash="$(sed -n '1p' "$package_stamp" 2>/dev/null || true)"
  if [ ! -d "$ROOT/frontend/node_modules" ] || [ "$package_hash" != "$installed_package_hash" ]; then
    say "Installing frontend dependencies"
    (cd "$ROOT/frontend" && npm ci --no-fund --no-audit)
    printf '%s\n' "$package_hash" > "$package_stamp"
  else
    printf '%s\n' "${DIM}  frontend dependencies present${N}"
  fi
fi

if [ "$INSTALL_ONLY" = 1 ]; then
  say "${G}Dependencies ready.${N} Run ./scripts/dev.sh to start."
  exit 0
fi

# --- launch ----------------------------------------------------------------

BACKEND_ALREADY_RUNNING=0
FRONTEND_ALREADY_RUNNING=0

if [ "$RUN_BACKEND" = 1 ]; then
  if [ "$FORCE_RESTART" != 1 ] && backend_healthy; then
    printf '%s\n' "${DIM}  backend already running and healthy on :$BACKEND_PORT — reusing it${N}"
    BACKEND_ALREADY_RUNNING=1
  else
    check_port "$BACKEND_PORT" "backend"
  fi
fi

if [ "$RUN_FRONTEND" = 1 ]; then
  if [ "$FORCE_RESTART" != 1 ] && frontend_healthy; then
    printf '%s\n' "${DIM}  frontend already running and healthy on :$FRONTEND_PORT — reusing it${N}"
    FRONTEND_ALREADY_RUNNING=1
  else
    check_port "$FRONTEND_PORT" "frontend"
  fi
fi

# Job control, so each background job becomes its own process-group leader
# (pgid == pid). Without this we can only signal the direct child: `npm run dev`
# forks vite, which forks esbuild, and killing the npm wrapper alone leaves vite
# holding port 3000 — verified by watching an orphan survive Ctrl+C.
set -m

PIDS=()

cleanup() {
  trap - EXIT INT TERM          # don't re-enter while tearing down
  [ "${#PIDS[@]}" -eq 0 ] && return 0   # nothing spawned (e.g. all sides reused) — nothing to tear down
  printf '\n%s\n' "${B}▸${N} Shutting down"

  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid:-}" ] || continue
    # Negative PID targets the whole process group: child and grandchildren.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done

  # Give them a moment to exit gracefully, then insist.
  for _ in 1 2 3 4 5 6; do
    still_running=0
    for pid in "${PIDS[@]:-}"; do
      [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null && still_running=1
    done
    [ "$still_running" = 0 ] && break
    sleep 0.5
  done

  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid:-}" ] || continue
    kill -0 "$pid" 2>/dev/null || continue
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done

  wait 2>/dev/null || true
}
trap cleanup EXIT
# A signal handler must terminate the supervisor after cleanup. Letting the
# interrupted polling loop resume can make it `wait` on an already-reaped PID.
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$RUN_BACKEND" = 1 ] && [ "$BACKEND_ALREADY_RUNNING" != 1 ]; then
  say "Starting backend on http://localhost:$BACKEND_PORT"
  # Must run from inside backend/: main.py uses bare imports such as
  # `from models.congestion_model import ...`, which only resolve if that
  # directory is the working directory.
  (cd "$ROOT/backend" && exec "$VENV/bin/uvicorn" main:app --port "$BACKEND_PORT") &
  PIDS+=($!)

  for _ in $(seq 1 60); do
    curl -sf "http://localhost:$BACKEND_PORT/api/corridors" >/dev/null 2>&1 && break
    sleep 0.5
  done
  if curl -sf "http://localhost:$BACKEND_PORT/api/corridors" >/dev/null 2>&1; then
    printf '%s\n' "  ${G}✓${N} API responding  ${DIM}(docs: http://localhost:$BACKEND_PORT/docs)${N}"
  else
    warn "Backend did not respond in time — the UI will fall back to demo data."
  fi
fi

if [ "$RUN_FRONTEND" = 1 ] && [ "$FRONTEND_ALREADY_RUNNING" != 1 ]; then
  say "Starting frontend on http://localhost:$FRONTEND_PORT"
  (cd "$ROOT/frontend" && exec npm run dev) &
  PIDS+=($!)
fi

printf '\n%s\n' "${G}${B}CrossFlow AI is running.${N}"
[ "$RUN_FRONTEND" = 1 ] && printf '%s\n' "  Dashboard  ${B}http://localhost:$FRONTEND_PORT${N}"
[ "$RUN_BACKEND"  = 1 ] && printf '%s\n' "  API docs   ${B}http://localhost:$BACKEND_PORT/docs${N}"

if [ "${#PIDS[@]}" -eq 0 ]; then
  # Everything requested was already up and healthy before we started —
  # nothing was spawned, so there's nothing to hold the terminal open for.
  printf '%s\n\n' "${DIM}Nothing to start — reused the existing instance(s).${N}"
  exit 0
fi

printf '%s\n\n' "${DIM}Press Ctrl+C to stop both.${N}"

# Bash 3.2 (the version shipped with macOS) has no `wait -n`. Poll the process
# groups instead so a failed half of the stack immediately tears down the
# survivor on every supported shell.
while :; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      if wait "$pid"; then
        exit 0
      else
        exit $?
      fi
    fi
  done
  sleep 0.5
done
