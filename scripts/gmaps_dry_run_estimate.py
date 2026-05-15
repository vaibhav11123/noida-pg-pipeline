#!/usr/bin/env python3
"""
Dry-run stats for gmaps-queries.txt + gosom/google-maps-scraper.

- Counts non-comment lines (actual jobs in query-list mode).
- Estimates grid jobs = (cells in bbox) × (query lines) when using -grid-bbox
  (gosom creates one job per query × cell — see runner/jobs.go CreateGridSeedJobs).

Run from repo root:
  python3 scripts/gmaps_dry_run_estimate.py
  python3 scripts/gmaps_dry_run_estimate.py --print-docker-estimate
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUERIES_PATH = ROOT / "gmaps-queries.txt"
# Match scripts/run_gmaps_scraper.sh default concurrency (macOS vs Linux).
_DEFAULT_SCRAPER_C = 2 if sys.platform == "darwin" else 4

# Noida bbox from gmaps-queries.txt / user docs (minLat,minLon,maxLat,maxLon)
DEFAULT_BBOX = (28.482, 77.300, 28.660, 77.430)
DEFAULT_GRID_CELL_KM = 0.3


def load_queries(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def approx_bbox_area_km2(
    min_lat: float, min_lon: float, max_lat: float, max_lon: float
) -> float:
    mid_lat = (min_lat + max_lat) / 2.0
    rad = math.radians(mid_lat)
    km_per_lat = 111.0
    km_per_lon = 111.0 * math.cos(rad)
    d_lat_km = abs(max_lat - min_lat) * km_per_lat
    d_lon_km = abs(max_lon - min_lon) * km_per_lon
    return d_lat_km * d_lon_km


def estimate_grid_cells(area_km2: float, cell_km: float) -> int:
    """Lower bound-ish: area / cell^2 (gosom uses proper tiling; use Docker for exact)."""
    if cell_km <= 0:
        return 0
    return max(1, int(math.ceil(area_km2 / (cell_km * cell_km))))


def query_prefix_stats(queries: list[str]) -> dict[str, int]:
    pref: dict[str, int] = {}
    for q in queries:
        m = re.match(r"^([a-z ]+?)\s+Sector\b", q, re.I)
        key = m.group(1).strip().lower() if m else "(other)"
        pref[key] = pref.get(key, 0) + 1
    return dict(sorted(pref.items(), key=lambda kv: -kv[1]))


def docker_cell_count() -> int | None:
    """Run gosom once to print `grid scraping: ~N cells` (needs Docker)."""
    one = ROOT / "gmaps-output" / "_dry_one.txt"
    one.parent.mkdir(parents=True, exist_ok=True)
    one.write_text("PG in Sector 62 Noida\n", encoding="utf-8")
    min_lat, min_lon, max_lat, max_lon = DEFAULT_BBOX
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{one}:/q.txt:ro",
        "gosom/google-maps-scraper",
        "-input",
        "/q.txt",
        "-results",
        "/dev/null",
        "-grid-bbox",
        f"{min_lat},{min_lon},{max_lat},{max_lon}",
        "-grid-cell",
        str(DEFAULT_GRID_CELL_KM),
        "-zoom",
        "16",
        "-depth",
        "1",
        "-c",
        "1",
        "-exit-on-inactivity",
        "2s",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"grid scraping:\s*~(\d+)\s*cells", text)
    if not m:
        return None
    return int(m.group(1))


def main() -> int:
    ap = argparse.ArgumentParser(description="Gosom gmaps query dry-run / time hints")
    ap.add_argument(
        "--print-docker-estimate",
        action="store_true",
        help="Run Docker once to print official grid cell count (slower, needs Docker)",
    )
    args = ap.parse_args()

    if not QUERIES_PATH.exists():
        print(f"Missing {QUERIES_PATH}", file=sys.stderr)
        return 1

    queries = load_queries(QUERIES_PATH)
    nq = len(queries)
    area = approx_bbox_area_km2(*DEFAULT_BBOX)
    rough_cells = estimate_grid_cells(area, DEFAULT_GRID_CELL_KM)

    print("=== gmaps-queries.txt ===")
    print(f"Path: {QUERIES_PATH}")
    print(f"Active query lines: {nq}")
    print("\nPrefix mix:")
    for k, v in list(query_prefix_stats(queries).items())[:12]:
        print(f"  {k!r}: {v}")

    print("\n=== Query-list mode (no -grid-bbox) ===")
    print("Jobs = one Playwright search per query line.")
    dc = _DEFAULT_SCRAPER_C
    print(
        f"  → {nq} jobs (run_gmaps_scraper.sh defaults here: -c {dc}, -depth 10; "
        f"-exit-on-inactivity omitted by default; -geo only if GMAPS_USE_GEO=yes)"
    )
    for sec in (45, 90, 150):
        wall = nq * sec / dc
        print(f"  heuristic: if ~{sec}s amortized per job slot → ~{wall/60:.0f} min wall-clock")

    print("\n=== Grid mode (-grid-bbox + -grid-cell km) ===")
    print(f"BBox {DEFAULT_BBOX[0]},{DEFAULT_BBOX[1]},{DEFAULT_BBOX[2]},{DEFAULT_BBOX[3]}  cell={DEFAULT_GRID_CELL_KM} km")
    print(f"Rough area ~{area:.0f} km² → naive cell count ~{rough_cells} (use Docker line for authoritative count)")
    cells: int | None = rough_cells
    if args.print_docker_estimate:
        print("Running Docker for official cell estimate (may take ~1–2 min) …")
        cells = docker_cell_count()
        if cells is None:
            print("  (Docker unavailable or could not parse cell count — using naive estimate.)")
            cells = rough_cells
        else:
            print(f"  gosom stderr: ~{cells} cells")
    else:
        print("  Tip: re-run with --print-docker-estimate for gosom's exact ~cell count.")

    if cells is not None:
        total = nq * cells
        print(
            f"\n  gosom pairs EACH query line with EACH grid cell "
            f"(runner/jobs.go CreateGridSeedJobs).\n"
            f"  → ~{cells} × {nq} ≈ {total:,} jobs with your current file + this bbox/cell."
        )
        for sec in (30, 60, 120):
            wall = total * sec / dc
            print(
                f"  if ~{sec}s per job @ -c {dc}: ~{wall/3600:.0f} h "
                f"({wall/86400:.1f} days) — usually not practical; use fewer queries, "
                f"larger -grid-cell, or grid with a 1-line broad query only."
            )

    print("\n=== Practical guidance ===")
    print(
        "  • Full-area coverage: prefer query-list mode (this file) OR grid with a tiny query file,\n"
        "    not both at full line count.\n"
        "  • For a time-boxed dry run: copy 3–5 lines to a temp file and docker -input that file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
