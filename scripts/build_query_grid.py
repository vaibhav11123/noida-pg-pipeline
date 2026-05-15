#!/usr/bin/env python3
"""
Build gmaps-output/queries.csv with per-row lat/lng/zoom for Noida PG coverage.

Stock gosom/google-maps-scraper reads *one query per line* and applies a single
`-geo` to every job in a process, so this CSV is used to:

  1. Document / edit intended viewports.
  2. Feed `python scripts/gmaps_centroid_batches.py materialize` which groups
     rows by centroid and writes one .txt batch per distinct (lat,lng,zoom)
     for sequential Docker runs with matching `GMAPS_GEO`.

Modes:
  geocode  — Nominatim (1 req/s); cache centroids in gmaps-output/sector_centroids.json
  grid     — tile NOBROKER_BBOX–style bounds with STEP_M (no network)

Examples:
  python3 scripts/build_query_grid.py geocode --out gmaps-output/queries.csv
  python3 scripts/build_query_grid.py grid --out gmaps-output/queries.csv
  python3 scripts/build_query_grid.py geocode --only-thin data/raw/gosom_gmaps.coverage.json --zoom 14
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    NOBROKER_BBOX,
    TARGET_CITY,
    TARGET_SECTORS,
    TARGET_STATE,
)

# Mirrors config.py “dense” sectors for extra keyword variants.
DENSE_SECTORS = {
    15,
    16,
    18,
    22,
    34,
    41,
    50,
    51,
    52,
    57,
    58,
    62,
    63,
    66,
    70,
    73,
    74,
    75,
    100,
    119,
    120,
    121,
    122,
    125,
    128,
    131,
    134,
    137,
    142,
    144,
}

USER_AGENT_DEFAULT = "pg_pipeline/1.0 (gmaps query grid; contact local operator)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
CENTROIDS_CACHE = ROOT / "gmaps-output" / "sector_centroids.json"


def _sector_key(label: str) -> str:
    m = re.match(r"^Sector\s+(.+)$", label.strip(), re.I)
    return m.group(1).lower() if m else label.strip().lower()


def _dense_for_sector(label: str) -> bool:
    key = _sector_key(label)
    m = re.match(r"^(\d+)", key)
    if not m:
        return False
    try:
        n = int(m.group(1))
    except ValueError:
        return False
    return n in DENSE_SECTORS


def geocode_sector(
    q: str,
    *,
    user_agent: str,
) -> tuple[float | None, float | None]:
    params = urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "in"}
    )
    req = urllib.request.Request(
        f"{NOMINATIM}?{params}",
        headers={"User-Agent": user_agent},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        hits = json.loads(resp.read().decode("utf-8"))
    if not hits:
        return None, None
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def load_centroid_cache(path: Path) -> dict[str, dict[str, float]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, float]] = {}
    for k, v in raw.items():
        if isinstance(v, dict) and "lat" in v and "lon" in v:
            out[str(k).lower()] = {"lat": float(v["lat"]), "lon": float(v["lon"])}
    return out


def save_centroid_cache(path: Path, data: dict[str, dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def build_rows_geocode(
    *,
    zoom: int,
    lang: str,
    user_agent: str,
    sleep_s: float,
    only_sectors: set[str] | None,
    cache: dict[str, dict[str, float]],
) -> list[list[str]]:
    rows: list[list[str]] = []
    updated = dict(cache)

    for sector in TARGET_SECTORS:
        sk = _sector_key(sector)
        if only_sectors is not None and sk not in only_sectors:
            continue

        geo_q = f"{sector}, {TARGET_CITY}, {TARGET_STATE}, India"
        if sk in updated:
            lat, lon = updated[sk]["lat"], updated[sk]["lon"]
        else:
            lat, lon = geocode_sector(geo_q, user_agent=user_agent)
            if lat is None:
                print(f"[skip] no geocode hit for {geo_q}", flush=True)
                time.sleep(sleep_s)
                continue
            updated[sk] = {"lat": lat, "lon": lon}
            print(f"{sector}: {lat:.5f},{lon:.5f}", flush=True)
            time.sleep(sleep_s)

        def add_row(query: str) -> None:
            rows.append(
                [
                    query,
                    f"{lat:.6f}",
                    f"{lon:.6f}",
                    str(zoom),
                    lang,
                ]
            )

        add_row(f"PG in {sector} {TARGET_CITY}")
        add_row(f"paying guest {sector} {TARGET_CITY}")
        if _dense_for_sector(sector):
            add_row(f"boys PG {sector} {TARGET_CITY}")
            add_row(f"girls PG {sector} {TARGET_CITY}")
            add_row(f"hostel {sector} {TARGET_CITY}")

    save_centroid_cache(CENTROIDS_CACHE, updated)
    return rows


def build_rows_grid(
    *,
    zoom: int,
    lang: str,
    step_m: float,
    keywords: list[str],
) -> list[list[str]]:
    lat_min = float(NOBROKER_BBOX["lat_min"])
    lat_max = float(NOBROKER_BBOX["lat_max"])
    lng_min = float(NOBROKER_BBOX["lng_min"])
    lng_max = float(NOBROKER_BBOX["lng_max"])
    mid_lat = (lat_min + lat_max) / 2.0
    dlat = step_m / 111_320.0
    dlng = step_m / (111_320.0 * math.cos(math.radians(mid_lat)))

    rows: list[list[str]] = []
    lat = lat_min
    while lat <= lat_max + 1e-9:
        lng = lng_min
        while lng <= lng_max + 1e-9:
            for kw in keywords:
                rows.append([kw, f"{lat:.6f}", f"{lng:.6f}", str(zoom), lang])
            lng += dlng
        lat += dlat
    return rows


def _thin_sector_keys(coverage_path: Path) -> set[str]:
    data = json.loads(coverage_path.read_text(encoding="utf-8"))
    thin = data.get("thin_sectors") or []
    return {str(s).strip().lower() for s in thin}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-viewport gmaps query CSV")
    sub = ap.add_subparsers(dest="mode", required=True)

    p_geo = sub.add_parser("geocode", help="Geocode each TARGET_SECTORS label via Nominatim")
    p_geo.add_argument("--out", type=Path, default=ROOT / "gmaps-output" / "queries.csv")
    p_geo.add_argument("--zoom", type=int, default=15)
    p_geo.add_argument("--lang", default="en")
    p_geo.add_argument("--user-agent", default=USER_AGENT_DEFAULT)
    p_geo.add_argument("--sleep", type=float, default=1.1, help="Delay between Nominatim calls (ToS)")
    p_geo.add_argument(
        "--only-thin",
        type=Path,
        metavar="COVERAGE_JSON",
        help="Only sectors listed in thin_sectors from gosom_gmaps.coverage.json",
    )

    p_gr = sub.add_parser("grid", help="Grid over NOBROKER_BBOX from config (no geocoder)")
    p_gr.add_argument("--out", type=Path, default=ROOT / "gmaps-output" / "queries.csv")
    p_gr.add_argument("--zoom", type=int, default=16)
    p_gr.add_argument("--lang", default="en")
    p_gr.add_argument("--step-m", type=float, default=600.0, help="Tile step in metres")
    p_gr.add_argument(
        "--keywords",
        default="PG,paying guest,girls PG,boys PG,hostel",
        help="Comma-separated search strings (no location; grid supplies viewport)",
    )

    args = ap.parse_args()
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    only: set[str] | None = None
    if args.mode == "geocode" and getattr(args, "only_thin", None):
        only = _thin_sector_keys(args.only_thin)
        if not only:
            print("thin_sectors empty — nothing to do", file=sys.stderr)
            return 1

    if args.mode == "geocode":
        cache = load_centroid_cache(CENTROIDS_CACHE)
        rows = build_rows_geocode(
            zoom=args.zoom,
            lang=args.lang,
            user_agent=args.user_agent,
            sleep_s=args.sleep,
            only_sectors=only,
            cache=cache,
        )
    else:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        rows = build_rows_grid(
            zoom=args.zoom,
            lang=args.lang,
            step_m=args.step_m,
            keywords=kws,
        )

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query", "lat", "lng", "zoom", "lang"])
        w.writerows(rows)

    print(f"wrote {len(rows)} data rows → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
