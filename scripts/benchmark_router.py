"""Deterministic local routing benchmark.

This script deliberately exercises the public named-landmark router instead of
private search implementation details.  Each case runs in a fresh worker
process so the first measurement includes a cold import/graph load.  The same
worker then performs a different-name cache miss (warm routing core) and an
exact repeat (warm result cache).  Primary-only and primary-plus-alternatives
are separate worker invocations, so the primary timing can never accidentally
include alternative searches.

Run from the repository root, for example::

    .venv\\Scripts\\python.exe scripts\\benchmark_router.py

The table is printed first and a machine-readable JSON report follows.  Use
``--json-out path`` to persist the report for before/after comparisons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = REPOSITORY_ROOT / "backend" / "data" / "batam_graph.json"

# Pin the optional-search budget for reproducible before/after measurements.
# These match the router defaults, but passing them explicitly prevents an
# operator's CROSSFLOW_ALTERNATIVE_* environment overrides from changing a
# benchmark run silently.
ALTERNATIVE_BUDGET = {
    "max_searches": 3,
    "time_budget_ms": 10_000.0,
    "max_settled_states": 250_000,
}


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    origin: str
    destination: str
    vehicle_type: str
    route_preference: str = "BALANCED"
    network_congestion_score: float = 0.0
    weather: int = 0


# Keep this set small enough for a local review while covering a cold long
# route, a congested/weather route, and a short urban route.  The vehicle mix
# also exercises the mode-specific graph filtering paths.
DEFAULT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="muka_kuning_to_batam_centre",
        origin="mukakuning",
        destination="batam_centre",
        vehicle_type="COMMUTER",
    ),
    BenchmarkCase(
        name="sekupang_to_hang_nadim_congested_rain",
        origin="sekupang",
        destination="hang_nadim",
        vehicle_type="MOTORCYCLE",
        network_congestion_score=75.0,
        weather=1,
    ),
    BenchmarkCase(
        name="short_urban_panbil_to_kepri",
        origin="panbil_mall",
        destination="kepri_mall",
        vehicle_type="CARGO_TRUCK",
    ),
)


def _cache_info(value: Any) -> Optional[Dict[str, int]]:
    """Convert functools.cache_info() output into stable JSON."""
    if value is None:
        return None
    return {
        key: int(getattr(value, key))
        for key in ("hits", "misses", "maxsize", "currsize")
        if hasattr(value, key)
    }


def _router_cache_state(router: Any) -> Dict[str, Optional[Dict[str, int]]]:
    names = (
        "_main_routing_core",
        "_routing_view",
        "snap_to_graph",
        "_static_edge_features",
        "_alt_index_for_profile",
        "_primary_paths_cached",
        "_route_between_nodes_cached",
    )
    state: Dict[str, Optional[Dict[str, int]]] = {}
    for name in names:
        cache_owner = getattr(router, name, None)
        state[name] = _cache_info(
            cache_owner.cache_info() if cache_owner is not None else None,
        )
    return state


def _call_router(case: BenchmarkCase, include_alternatives: bool, name_tag: str) -> Dict[str, Any]:
    # Imports stay inside the worker path: the parent process only launches a
    # clean worker and therefore does not contaminate cold-process results.
    from services import router  # type: ignore[import-not-found]

    with router.search_diagnostics() as diagnostics:
        result = router.route_between(
            case.origin,
            case.destination,
            include_alternatives=include_alternatives,
            origin_name=f"{case.name} origin {name_tag}",
            destination_name=f"{case.name} destination {name_tag}",
            vehicle_type=case.vehicle_type,
            network_congestion_score=case.network_congestion_score,
            weather=case.weather,
            route_preference=case.route_preference,
            alternative_max_searches=(
                ALTERNATIVE_BUDGET["max_searches"] if include_alternatives else None
            ),
            alternative_time_budget_ms=(
                ALTERNATIVE_BUDGET["time_budget_ms"] if include_alternatives else None
            ),
            alternative_max_settled_states=(
                ALTERNATIVE_BUDGET["max_settled_states"] if include_alternatives else None
            ),
        )
    if result is None:
        raise RuntimeError(
            f"No route for {case.origin} -> {case.destination} "
            f"({case.vehicle_type})."
        )
    alternatives = result.get("alternatives") or []
    return {
        "distance_km": result.get("distance_km"),
        "objective_cost_s": result.get("objective_cost_s"),
        "path_node_count": result.get("path_node_count"),
        "alternative_count": len(alternatives),
        "diagnostics": diagnostics.as_dict(),
    }


def _run_worker(case: BenchmarkCase, include_alternatives: bool) -> Dict[str, Any]:
    """Run one mode in a fresh process and return its JSON payload."""
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        json.dumps(asdict(case), separators=(",", ":")),
        "--include-alternatives" if include_alternatives else "--primary-only",
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT / "backend")},
        capture_output=True,
        text=True,
        check=False,
    )
    process_elapsed_ms = (time.perf_counter() - started) * 1000.0
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Benchmark worker failed ({completed.returncode}): {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Benchmark worker emitted invalid JSON: {completed.stdout[:500]}"
        ) from exc
    payload["process_elapsed_ms"] = round(process_elapsed_ms, 3)
    return payload


def _worker_main(case: BenchmarkCase, include_alternatives: bool) -> int:
    # This is intentionally a JSON-only process; all human presentation is in
    # the parent so callers can safely consume stdout from worker invocations.
    import time as _time

    from services import router  # type: ignore[import-not-found]

    cache_before = _router_cache_state(router)
    measurements: List[Dict[str, Any]] = []
    for state, name_tag in (
        ("cold_route", "cold"),
        ("warm_routing_view", "warm"),
        # Repeat the exact warm-view identity to measure the route-result
        # cache rather than creating a third cache miss.
        ("warm_result_cache", "warm"),
    ):
        started = _time.perf_counter()
        route = _call_router(case, include_alternatives, name_tag)
        elapsed_ms = (_time.perf_counter() - started) * 1000.0
        measurements.append({
            "state": state,
            "elapsed_ms": round(elapsed_ms, 3),
            **route,
            "cache_state": _router_cache_state(router),
        })

    payload = {
        "mode": "primary_plus_alternatives" if include_alternatives else "primary_only",
        "cache_before": cache_before,
        "measurements": measurements,
        "cache_after": _router_cache_state(router),
        "graph_sha256": getattr(router, "GRAPH_REVISION", None),
        "alternative_budget": ALTERNATIVE_BUDGET if include_alternatives else None,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _graph_sha256() -> str:
    digest = hashlib.sha256()
    with GRAPH_PATH.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _flatten_rows(report: Mapping[str, Any]) -> Iterable[Dict[str, Any]]:
    for case_report in report["cases"]:
        case = case_report["case"]
        for mode_report in case_report["modes"]:
            for measurement in mode_report["measurements"]:
                yield {
                    "case": case["name"],
                    "vehicle": case["vehicle_type"],
                    "mode": mode_report["mode"],
                    **measurement,
                    "process_elapsed_ms": mode_report["process_elapsed_ms"],
                    "diagnostic_searches": (
                        measurement.get("diagnostics", {}).get("searches_started", 0)
                    ),
                    "diagnostic_settled": (
                        measurement.get("diagnostics", {}).get("settled_states", 0)
                    ),
                    "diagnostic_alt_attempts": (
                        measurement.get("diagnostics", {}).get("alternative_attempts", 0)
                    ),
                }


def _print_table(report: Mapping[str, Any]) -> None:
    columns = (
        "case", "vehicle", "mode", "state", "elapsed_ms",
        "process_elapsed_ms", "alternative_count", "diagnostic_searches",
        "diagnostic_settled", "diagnostic_alt_attempts",
    )
    rows = list(_flatten_rows(report))
    values = []
    for row in rows:
        values.append({
            "case": row["case"],
            "vehicle": row["vehicle"],
            "mode": row["mode"],
            "state": row["state"],
            "elapsed_ms": f"{row['elapsed_ms']:.1f}",
            "process_elapsed_ms": f"{row['process_elapsed_ms']:.1f}",
            "alternative_count": str(row["alternative_count"]),
            "diagnostic_searches": str(row["diagnostic_searches"]),
            "diagnostic_settled": str(row["diagnostic_settled"]),
            "diagnostic_alt_attempts": str(row["diagnostic_alt_attempts"]),
        })
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in values))
        for column in columns
    }
    print("Routing benchmark (each mode is a fresh process; times are wall-clock ms)")
    print("  " + "  ".join(column.ljust(widths[column]) for column in columns))
    print("  " + "  ".join("-" * widths[column] for column in columns))
    for row in values:
        print("  " + "  ".join(str(row[column]).ljust(widths[column]) for column in columns))
    print()


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, help="Also write the JSON report to this path.")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--include-alternatives", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--primary-only", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if args.worker is not None:
        case = BenchmarkCase(**json.loads(args.worker))
        return _worker_main(case, bool(args.include_alternatives))

    cases = DEFAULT_CASES
    report: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "graph_path": str(GRAPH_PATH),
        "graph_sha256": _graph_sha256(),
        "conditions": {
            "network": "offline committed OSM graph",
            "provider": os.environ.get("CROSSFLOW_ROUTE_PROVIDER", "local"),
            "cold_process": "fresh worker process per mode/case",
            "warm_routing_view": "same route with distinct cache identity after cold call",
            "warm_result_cache": "exact repeat of the warm-routing-view request",
            "alternative_budget": ALTERNATIVE_BUDGET,
        },
        "cases": [],
    }
    for case in cases:
        case_report = {
            "case": asdict(case),
            "modes": [_run_worker(case, False), _run_worker(case, True)],
        }
        report["cases"].append(case_report)

    _print_table(report)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
