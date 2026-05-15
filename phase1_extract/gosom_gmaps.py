"""
phase1_extract/gosom_gmaps.py
===============================
Ingest CSV output from **gosom/google-maps-scraper** (Docker or binary) into
`data/raw/gosom_gmaps.json` so Phase 2 merges it with other sources.

Upstream: https://github.com/gosom/google-maps-scraper

Typical workflow:
  1. Run Docker via `scripts/run_gmaps_scraper.sh` (preflight → approved queries / grid-mop).
  2. Point `GOSOM_GMAPS_CSV` in config.py at the produced CSV (or default
     `gmaps-output/gmaps_results.csv`), or pass multiple CSV paths on the CLI.
  3. Run `python main.py --phase 1` — this module runs after Google Places API.
  4. After ingest, see `data/raw/gosom_gmaps.coverage.json` for per-sector counts / thin_sectors.

CSV columns (official names): title, phone, address, complete_address, website,
latitude, longitude, review_count, review_rating, link, emails, place_id, cid, …
We match headers case-insensitively and tolerate minor renames. Address: prefer plain
`address` over `complete_address` when the latter is empty structured JSON (common in
gosom exports). Output uses a real Google `place_id` only in `place_id`; `cid` is
separate; `property_id` is the dedupe key (place_id, else cid, else stable hash from
link or title|address).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
import phonenumbers

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import GOSOM_GMAPS_CSV, RAW_DATA_DIR

log = logging.getLogger(__name__)


def _configure_cli_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [GOSOM] %(message)s")


def _stable_id(s: str) -> str:
    """Deterministic internal id (replaces builtin hash(), which is salted per process)."""
    return "gmaps:" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _norm_cols(df: pd.DataFrame) -> dict[str, str]:
    """Map lowercased stripped header → original column name."""
    return {str(c).strip().lower(): c for c in df.columns}


def _lower_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case headers and coalesce duplicate names (e.g. after concat of Title vs title)."""
    df = df.copy()
    low = [str(c).strip().lower() for c in df.columns]
    df.columns = low
    if not any(low.count(n) > 1 for n in set(low)):
        return df

    def _first_nonempty(row: pd.Series) -> str:
        for v in row:
            if v is None:
                continue
            s = str(v).strip()
            if s and s.lower() not in ("nan", "none"):
                return s
        return ""

    names_in_order: list[str] = []
    blocks: list[pd.Series] = []
    seen: set[str] = set()
    for name in low:
        if name in seen:
            continue
        seen.add(name)
        idxs = [i for i, n in enumerate(low) if n == name]
        sub = df.iloc[:, idxs]
        if sub.shape[1] == 1:
            blocks.append(sub.iloc[:, 0])
        else:
            blocks.append(sub.apply(_first_nonempty, axis=1))
        names_in_order.append(name)
    return pd.concat(blocks, axis=1, keys=names_in_order)


def _read_gosom_csv(csv_path: Path) -> pd.DataFrame:
    """Read gosom CSV; skip malformed lines (long JSON/review blobs sometimes break row boundaries)."""
    return pd.read_csv(
        csv_path,
        dtype=str,
        keep_default_na=False,
        on_bad_lines="skip",
    )


def _pick(row: dict[str, Any], aliases: tuple[str, ...], cols: dict[str, str]) -> str:
    for a in aliases:
        key = cols.get(a.lower())
        if key is None:
            continue
        v = row.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("nan", "none"):
            return s
    return ""


def _normalize_address_candidate(raw: str) -> str:
    """
    gosom CSV often has a readable line in `address` and a structured `complete_address`
    cell that is JSON — sometimes all-empty keys. Prefer human text; flatten real JSON.
    """
    t = (raw or "").strip()
    if not t or t.lower() in ("nan", "none"):
        return ""
    if not t.startswith("{"):
        return t
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return t
    if not isinstance(obj, dict):
        return t
    parts = [str(v).strip() for v in obj.values() if str(v).strip()]
    return ", ".join(parts)


def _pick_best_address(row: dict[str, Any], cols: dict[str, str]) -> str:
    """Prefer plain `address` over `complete_address` when the latter is empty JSON."""
    for alias in ("address", "formatted_address", "full_address", "complete_address"):
        raw = _pick(row, (alias,), cols)
        norm = _normalize_address_candidate(raw)
        if norm:
            return norm
    return ""


def _to_float(val: Any) -> Any:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_emails_raw(raw: str) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[,;|\s]+", str(raw).strip())
    return [p.strip() for p in parts if p.strip() and "@" in p]


def _digits_phone_e164(s: str, default_cc: str = "91") -> str:
    digits = re.sub(r"\D", "", s or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = default_cc + digits
    if digits.startswith("0") and len(digits) == 11:
        digits = default_cc + digits[1:]
    return "+" + digits


def _phone_intl_from_raw(phone_raw: str, default_region: str = "IN") -> str:
    s = (phone_raw or "").strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    try:
        p = phonenumbers.parse(s, default_region)
        if phonenumbers.is_valid_number(p):
            return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return _digits_phone_e164(s)


def _sane_coords(lat: Any, lng: Any) -> tuple[Any, Any]:
    if lat is None or lng is None:
        return None, None
    try:
        la = float(lat)
        lo = float(lng)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None, None
    if abs(la) < 0.001 and abs(lo) < 0.001:
        return None, None
    # Drop obvious non-India coordinates (gosom occasionally emits garbage / 0 island).
    if la < 6.0 or la > 37.5 or lo < 68.0 or lo > 98.5:
        return None, None
    return la, lo


def _dedupe_key(rec: dict[str, Any]) -> str:
    pid = rec.get("property_id") or ""
    ph = ((rec.get("phone_intl") or "").strip() or (rec.get("phone_raw") or "").strip())
    return pid + "|" + ph + "|" + (rec.get("name") or "")


def _coverage_report(rows: list[dict[str, Any]], out_path: Path) -> None:
    sectors: Counter[str] = Counter()
    no_phone = 0
    no_coords = 0
    for r in rows:
        addr = (r.get("address") or "").lower()
        m = re.search(r"sector\s+(\d+[a-z]?)", addr)
        if m:
            sectors[m.group(1)] += 1
        if not (r.get("phone_raw") or "").strip():
            no_phone += 1
        if r.get("lat") is None or r.get("lng") is None:
            no_coords += 1

    thin = sorted(s for s, n in sectors.items() if n < 3)
    report = {
        "total_rows": len(rows),
        "rows_without_phone": no_phone,
        "rows_without_coords": no_coords,
        "by_sector": dict(sectors.most_common()),
        "thin_sectors": thin,
    }
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "Coverage: %d rows across %d sector tags; thin: %s",
        report["total_rows"],
        len(sectors),
        thin[:20] if len(thin) > 20 else thin,
    )


def _col(cols: dict[str, str], *names: str) -> str | None:
    for n in names:
        orig = cols.get(n.lower())
        if orig is not None:
            return orig
    return None


def csv_row_to_record(row: dict[str, Any], cols: dict[str, str]) -> dict[str, Any]:
    """One gosom CSV row → pipeline row (aligned with `google_places` extractor)."""
    title = _pick(row, ("title", "name", "business_name"), cols)
    phone = _pick(row, ("phone", "phone_number", "telephone"), cols)
    addr = _pick_best_address(row, cols)
    website = _pick(row, ("website", "web_site"), cols)
    lat_k = _col(cols, "latitude", "lat")
    lng_k = _col(cols, "longitude", "lng", "lon")
    lat, lng = _sane_coords(
        _to_float(row.get(lat_k)) if lat_k else None,
        _to_float(row.get(lng_k)) if lng_k else None,
    )

    place_id = _pick(row, ("place_id", "google_place_id"), cols)
    cid = _pick(row, ("cid",), cols)
    link = _pick(row, ("link", "maps_url"), cols)

    pid = place_id or cid or ""
    if not pid and link:
        pid = _stable_id(link)
    if not pid:
        fallback = f"{title}|{addr}".strip("|")
        if fallback:
            pid = _stable_id(fallback)

    rr_k = _col(cols, "review_rating", "rating")
    rating: Any = None
    if rr_k:
        rating = _to_float(row.get(rr_k))

    rc_k = _col(cols, "review_count", "reviews")
    rc: Any = None
    if rc_k and row.get(rc_k) not in (None, "", "nan"):
        try:
            rc = int(float(str(row.get(rc_k)).replace(",", "")))
        except (TypeError, ValueError):
            rc = None

    emails_raw = _pick(row, ("emails", "email"), cols)
    emails_list = _parse_emails_raw(emails_raw)
    phone_intl = _phone_intl_from_raw(phone)

    return {
        "source": "gosom_gmaps",
        "place_id": place_id,
        "cid": cid,
        "property_id": pid,
        "name": title,
        "address": addr,
        "phone_raw": phone,
        "phone_intl": phone_intl,
        "website": website,
        "email": emails_list[0] if emails_list else "",
        "emails_all": emails_list,
        "listing_url": link,
        "rating": rating,
        "review_count": rc,
        "lat": lat,
        "lng": lng,
        "types": [],
        "business_status": _pick(row, ("status", "business_status"), cols),
    }


def ingest_csv(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.is_file():
        log.warning("Gosom CSV not found — %s (run scripts/run_gmaps_scraper.sh first)", csv_path)
        return []

    df = _lower_headers(_read_gosom_csv(csv_path))
    if df.empty:
        log.warning("Gosom CSV is empty: %s", csv_path)
        return []

    cols = _norm_cols(df)
    if not any(k in cols for k in ("title", "name")):
        log.error(
            "CSV missing expected columns (need at least title/name). Found: %s",
            list(df.columns),
        )
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, ser in df.iterrows():
        row = ser.to_dict()
        rec = csv_row_to_record(row, cols)
        key = _dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)

    log.info("Ingested %d unique rows from %s", len(out), csv_path)
    return out


def ingest_csv_files(paths: list[Path]) -> list[dict[str, Any]]:
    """Merge multiple gosom CSVs (concat rows), then dedupe like ingest_csv."""
    dfs: list[pd.DataFrame] = []
    for csv_path in paths:
        if not csv_path.is_file():
            log.warning("Skip missing CSV: %s", csv_path)
            continue
        dfs.append(_lower_headers(_read_gosom_csv(csv_path)))
    if not dfs:
        log.warning("No readable CSV files among: %s", paths)
        return []
    df = pd.concat(dfs, ignore_index=True, sort=False)
    if df.empty:
        log.warning("Merged CSV is empty")
        return []

    cols = _norm_cols(df)
    if not any(k in cols for k in ("title", "name")):
        log.error(
            "CSV missing expected columns (need at least title/name). Found: %s",
            list(df.columns),
        )
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, ser in df.iterrows():
        row = ser.to_dict()
        rec = csv_row_to_record(row, cols)
        key = _dedupe_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)

    log.info("Ingested %d unique rows from %d CSV file(s)", len(out), len(dfs))
    return out


def run(*csv_paths: str | Path | None) -> list[dict[str, Any]]:
    """
    Read one or more gosom CSVs → write `data/raw/gosom_gmaps.json`.

    Args:
        csv_paths: Paths relative to repo root or absolute. If empty, uses
            config `GOSOM_GMAPS_CSV`. Multiple paths are merged and deduped.
    """
    root = Path(__file__).resolve().parent.parent
    (root / RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)

    resolved: list[Path] = []
    for p in csv_paths:
        if p is None:
            continue
        raw = Path(p)
        if not raw.is_absolute():
            raw = (root / raw).resolve()
        resolved.append(raw)

    if not resolved:
        raw = Path(GOSOM_GMAPS_CSV)
        if not raw.is_absolute():
            raw = (root / raw).resolve()
        resolved = [raw]

    any_input = any(p.is_file() for p in resolved)
    if len(resolved) > 1:
        rows = ingest_csv_files(resolved)
    else:
        rows = ingest_csv(resolved[0])

    out_path = root / RAW_DATA_DIR / "gosom_gmaps.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        if not any_input:
            log.info("Gosom CSV absent — not writing %s (keeps any previous scrape)", out_path.name)
            return []
        if out_path.exists():
            log.warning(
                "Ingest produced 0 rows — leaving existing %s unchanged",
                out_path.name,
            )
            return []

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    cov_path = out_path.with_name("gosom_gmaps.coverage.json")
    _coverage_report(rows, cov_path)
    log.info("✓ Saved %d gosom Maps leads → %s", len(rows), out_path)
    return rows


if __name__ == "__main__":
    import argparse

    _configure_cli_logging()

    p = argparse.ArgumentParser(
        description="Ingest gosom/google-maps-scraper CSV (one file or merge several)",
    )
    p.add_argument(
        "csv",
        nargs="*",
        help="Path(s) to results CSV (omit = use config GOSOM_GMAPS_CSV)",
    )
    args = p.parse_args()
    paths = tuple(args.csv) if args.csv else ()
    run(*paths)
