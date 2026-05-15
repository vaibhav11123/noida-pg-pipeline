#!/usr/bin/env python3
"""
Group gmaps-output/queries.csv by (lat,lng,zoom) and run stock Docker once per centroid.

Upstream gosom applies one -geo/-zoom per process; this tool materializes one query
file per distinct centroid, then runs docker sequentially (or only materializes).

  python3 scripts/gmaps_centroid_batches.py materialize --csv gmaps-output/queries.csv
  GMAPS_SCRAPE_APPROVED=yes python3 scripts/gmaps_centroid_batches.py run --depth 8

Outputs under gmaps-output/centroid_batches/:
  manifest.json   — batch metadata
  batch_000.txt … — one Maps query per line (no CSV header)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = ROOT / "gmaps-output" / "centroid_batches"
MANIFEST = BATCH_DIR / "manifest.json"


def _round_key(lat: str, lng: str, zoom: str) -> tuple[str, str, str]:
    return (f"{float(lat):.5f}", f"{float(lng):.5f}", str(int(float(zoom))))


def materialize(csv_path: Path) -> int:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            q = (row.get("query") or "").strip()
            lat, lng, z = (row.get("lat") or "").strip(), (row.get("lng") or "").strip(), (
                row.get("zoom") or "15"
            ).strip()
            if not q or not lat or not lng:
                continue
            key = _round_key(lat, lng, z)
            groups[key].append(q)

    if not groups:
        print("No valid rows — check CSV headers query,lat,lng,zoom", file=sys.stderr)
        return 1

    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str | int]] = []
    for i, ((lat_s, lng_s, z), queries) in enumerate(sorted(groups.items(), key=lambda x: x[0])):
        name = f"batch_{i:03d}.txt"
        p = BATCH_DIR / name
        p.write_text("\n".join(dict.fromkeys(queries)) + "\n", encoding="utf-8")
        manifest.append(
            {
                "id": name[:-4],
                "file": name,
                "geo": f"{lat_s},{lng_s}",
                "zoom": int(float(z)),
                "queries": len(queries),
            }
        )

    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {len(manifest)} batch file(s) under {BATCH_DIR}", flush=True)
    return 0


def run_batches(
    *,
    depth: int,
    no_email: bool,
    docker_image: str,
    platform: str | None,
    disable_telemetry: bool,
    concurrency: int,
    exit_inactivity: str | None,
) -> int:
    approved = os.environ.get("GMAPS_SCRAPE_APPROVED", "")
    if approved not in ("yes", "1", "true"):
        print(
            "Refusing to run Docker (set GMAPS_SCRAPE_APPROVED=yes).",
            file=sys.stderr,
        )
        return 3

    if not MANIFEST.is_file():
        print(f"Missing {MANIFEST} — run materialize first", file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    results_dir = ROOT / "gmaps-output" / "centroid_results"
    results_dir.mkdir(parents=True, exist_ok=True)

    telemetry = []
    if disable_telemetry:
        telemetry = ["-e", "DISABLE_TELEMETRY=1"]

    for entry in manifest:
        fname = str(entry["file"])
        geo = str(entry["geo"])
        zoom = int(entry["zoom"])
        batch_path = BATCH_DIR / fname
        out_csv = results_dir / f"{entry['id']}.csv"

        cmd = [
            "docker",
            "run",
            "--rm",
        ]
        if platform:
            cmd.extend(["--platform", platform])
        cmd.extend(telemetry)
        cmd.extend(
            [
                "-v",
                "gmaps-playwright-cache:/opt",
                "-v",
                f"{batch_path.resolve()}:/queries.txt:ro",
                "-v",
                f"{results_dir.resolve()}:/out",
                docker_image,
                "-input",
                "/queries.txt",
                "-results",
                f"/out/{out_csv.name}",
                "-geo",
                geo,
                "-zoom",
                str(zoom),
                "-depth",
                str(depth),
                "-c",
                str(concurrency),
                "-lang",
                "en",
            ]
        )
        if not no_email:
            cmd.append("-email")
        if exit_inactivity:
            cmd.extend(["-exit-on-inactivity", exit_inactivity])

        print(f"━━━ {entry['id']}  geo={geo} z={zoom}  n={entry['queries']} ━━━", flush=True)
        subprocess.run(cmd, check=True)

    print(
        f"Done. Merge ingest:\n"
        f"  python3 phase1_extract/gosom_gmaps.py gmaps-output/centroid_results/*.csv",
        flush=True,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("materialize", help="Split queries.csv into centroid batch .txt files")
    m.add_argument("--csv", type=Path, default=ROOT / "gmaps-output" / "queries.csv")

    r = sub.add_parser("run", help="Run docker once per batch (requires GMAPS_SCRAPE_APPROVED=yes)")
    r.add_argument("--depth", type=int, default=int(os.environ.get("GMAPS_DEPTH", "8")))
    r.add_argument("--no-email", action="store_true")
    r.add_argument("--docker-image", default=os.environ.get("GOSOM_DOCKER_IMAGE", "gosom/google-maps-scraper:latest"))
    r.add_argument("--platform", default=os.environ.get("GOSOM_DOCKER_PLATFORM") or None)
    r.add_argument("--no-disable-telemetry", action="store_true")
    r.add_argument("--c", type=int, default=int(os.environ.get("GMAPS_GOSOM_C", "2")))
    r.add_argument("--exit-on-inactivity", default=os.environ.get("GMAPS_EXIT_ON_INACTIVITY") or None)

    args = ap.parse_args()
    if args.cmd == "materialize":
        return materialize(args.csv)
    return run_batches(
        depth=args.depth,
        no_email=args.no_email,
        docker_image=args.docker_image,
        platform=args.platform,
        disable_telemetry=not args.no_disable_telemetry,
        concurrency=args.c,
        exit_inactivity=args.exit_on_inactivity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
