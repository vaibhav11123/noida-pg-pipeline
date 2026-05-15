"""
phase2_aggregate/aggregator.py
=================================
Merge all raw JSON outputs from Phase 1 into a single deduplicated DataFrame.

Pipeline steps:
  1. Load all raw JSON files from data/raw/
  2. Normalize phone numbers to E.164 (Indian numbers)
  3. Deduplicate by:
       a. E.164 phone number (primary key)
       b. Google place_id
       c. Rounded coordinates (within ~50m)
       d. Normalized address string
  4. Tag each record with its detected sector (57/58/62/63)
  5. Score listing freshness based on posted_on date
  6. Output merged CSV to data/merged/merged_contacts.csv

Dependencies: pandas, phonenumbers
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import phonenumbers
from phonenumbers import NumberParseException

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import RAW_DATA_DIR, MERGED_DATA_DIR

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [AGGREGATE] %(message)s")
log = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────
INDIA_COUNTRY_CODE = "IN"

# All residential Noida sectors — detect from address/name text.
# Pattern handles: "Sector 62", "Sec 62", "Sec-62", "sector62", plain " 62 " near Noida.
_ALL_NOIDA_SECTORS = [
    "1","2","3","4","5","6","7","8","9","10","12","14","15","15A","16","17",
    "18","19","20","21","22","23","24","25","26","27","28","29","30","31","32",
    "33","34","35","36","37","39","40","41","44","45","46","47","48","49","50",
    "51","52","53","54","55","56","57","58","59","60","61","62","63","63A","64",
    "65","66","67","68","69","70","71","72","73","74","75","76","77","78",
    "100","107","108","110","113","115","116","117","118","119","120",
    "121","122","123","125","126","127","128","129","130","131","132","133",
    "134","135","136","137","138","140","142","143","143B","144","145","146",
    "147","148","149",
]

def _sector_pattern(s: str) -> re.Pattern:
    """Build a regex for a sector label, e.g. '63A' matches 'Sector 63A' and 'Sector 63 A'."""
    # Insert optional whitespace before any trailing letter suffix (63A → 63\s*A)
    escaped = re.sub(r"([A-Za-z]+)$", lambda m: r"\s*" + re.escape(m.group(1)), re.escape(s))
    return re.compile(r"\bsec(?:tor)?[\s\-]*" + escaped + r"\b", re.I)

# Sort longest first so "63A" / "143B" are checked before "63" / "143"
SECTOR_PATTERNS = {
    s: _sector_pattern(s)
    for s in sorted(_ALL_NOIDA_SECTORS, key=len, reverse=True)
}

# Files to load (source → filename mapping)
RAW_FILES = {
    # Exa — operator-targeted extraction (highest quality, no single-room noise)
    "exa_agent":      "exa_agent.json",     # Exa Agent beta — deep research, structured
    "exa_similar":    "exa_similar.json",   # findSimilar seeded from gmaps operator sites
    "exa_search":     "exa_search.json",    # keyword search on business directories
    "exa_websets":    "exa_websets.json",   # Websets enriched (optional)
    # Maps
    "gosom_gmaps":    "gosom_gmaps.json",   # gosom Google Maps scraper
    "google_places":  "google_places.json", # Google Places API
    # Portals
    "nobroker":       "nobroker.json",
    "justdial":       "justdial.json",
    "99acres":        "99acres.json",
    "magicbricks":    "magicbricks.json",
    "housing":        "housing.json",
}


# ── phone normalization ───────────────────────────────────────────────────────

def normalize_phone(raw: str) -> Optional[str]:
    """
    Normalize a raw phone string to E.164 format for India.
    Returns None if the number is invalid or not a mobile number.
    """
    if not raw:
        return None

    # Strip everything that isn't a digit or leading +
    cleaned = re.sub(r"[^\d+]", "", str(raw))
    if not cleaned:
        return None

    # Handle common Indian formatting quirks
    # Remove leading zeros that aren't part of country code
    if cleaned.startswith("0") and not cleaned.startswith("00"):
        cleaned = cleaned[1:]

    # Add country code if missing (10-digit Indian mobile)
    if re.match(r"^[6-9]\d{9}$", cleaned):
        cleaned = "+91" + cleaned
    elif re.match(r"^91[6-9]\d{9}$", cleaned):
        cleaned = "+" + cleaned

    try:
        parsed = phonenumbers.parse(cleaned, INDIA_COUNTRY_CODE)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException:
        pass

    return None


# ── sector detection ──────────────────────────────────────────────────────────

def detect_sector(address: str, name: str = "") -> str:
    """Detect Noida sector from address or name fields."""
    text = f"{address} {name}".lower()
    for sector, pattern in SECTOR_PATTERNS.items():
        if pattern.search(text):
            return sector
    return "unknown"


# ── freshness scoring ─────────────────────────────────────────────────────────

def freshness_score(posted_on: str) -> int:
    """
    Score listing freshness 1–5 based on age.
      5 = < 7 days
      4 = 7–30 days
      3 = 30–90 days
      2 = 90–180 days
      1 = > 180 days or unknown
    """
    if posted_on is None or posted_on == "":
        return 1

    if isinstance(posted_on, (int, float)):
        try:
            ts = float(posted_on)
            if ts > 1e12:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            now_ts = datetime.now(timezone.utc)
            age_days = (now_ts - dt).days
            if age_days < 7:
                return 5
            if age_days < 30:
                return 4
            if age_days < 90:
                return 3
            if age_days < 180:
                return 2
            return 1
        except (ValueError, OSError):
            return 1

    posted_on = str(posted_on)

    now = datetime.now(timezone.utc)

    # Try multiple date formats
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%d %b %Y"):
        try:
            dt = datetime.strptime(posted_on[:len(fmt) + 2], fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now - dt).days
            if age_days < 7:   return 5
            if age_days < 30:  return 4
            if age_days < 90:  return 3
            if age_days < 180: return 2
            return 1
        except ValueError:
            continue

    return 1


# ── coordinate rounding (for dedup) ──────────────────────────────────────────

def round_coord(val, precision: int = 3) -> Optional[float]:
    """Round a lat/lng to ~50m precision for approximate dedup."""
    try:
        return round(float(val), precision)
    except (TypeError, ValueError):
        return None


# ── data loader ───────────────────────────────────────────────────────────────

def load_raw_files() -> pd.DataFrame:
    """Load all raw JSON files into a single DataFrame."""
    raw_dir = Path(RAW_DATA_DIR)
    frames: list[pd.DataFrame] = []

    for source, filename in RAW_FILES.items():
        fpath = raw_dir / filename
        if not fpath.exists():
            log.warning("Raw file not found (skipping): %s", fpath)
            continue

        with open(fpath, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as exc:
                log.error("JSON parse error in %s: %s", fpath, exc)
                continue

        if not data:
            log.info("Empty file: %s", fpath)
            continue

        df = pd.DataFrame(data)
        # Ensure the source column is present
        if "source" not in df.columns:
            df["source"] = source

        frames.append(df)
        log.info("Loaded %d rows from %s", len(df), filename)

    if not frames:
        log.error("No raw data files found. Run Phase 1 first.")
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


# ── column standardization ────────────────────────────────────────────────────

REQUIRED_COLUMNS = [
    "source", "property_id", "name", "address", "phone_raw",
    "lat", "lng", "owner_name", "owner_type", "posted_on", "listing_url",
    "rent", "rating",
]

def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all required columns exist (fill missing with empty string)."""
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    # Google Places uses `place_id`; dedupe prefers `property_id`
    if "place_id" in df.columns:
        pid = df["property_id"].astype(str).str.strip()
        empty_pid = df["property_id"].isna() | (pid == "") | (pid == "nan")
        df.loc[empty_pid, "property_id"] = df.loc[empty_pid, "place_id"].astype(str)
    return df[REQUIRED_COLUMNS + [c for c in df.columns if c not in REQUIRED_COLUMNS]]


# ── main aggregation pipeline ─────────────────────────────────────────────────

def run() -> pd.DataFrame:
    """Full aggregation and deduplication pipeline."""
    Path(MERGED_DATA_DIR).mkdir(parents=True, exist_ok=True)

    # 1. Load
    df = load_raw_files()
    if df.empty:
        return df

    log.info("Total raw rows loaded: %d", len(df))

    # 2. Standardize columns
    df = standardize_columns(df)

    # 3. Normalize phones
    log.info("Normalizing phone numbers …")
    df["phone_e164"] = df["phone_raw"].apply(normalize_phone)

    # 4. Detect sector
    log.info("Detecting sectors …")
    df["sector"] = df.apply(
        lambda r: detect_sector(str(r.get("address", "")), str(r.get("name", ""))),
        axis=1,
    )

    # 5. Coordinate rounding
    df["lat_r"] = df["lat"].apply(round_coord)
    df["lng_r"] = df["lng"].apply(round_coord)

    # 6. Freshness score
    log.info("Scoring freshness …")
    df["freshness_score"] = df["posted_on"].apply(freshness_score)

    # 7. Deduplication — multi-key priority cascade
    log.info("Deduplicating …")
    initial_count = len(df)

    # 7a. By E.164 phone (strongest signal — keep most recent / highest quality source)
    source_priority = {
        # Exa Agent: multi-hop deep research per sector, structured JSON — best signal
        "exa_agent":    8,
        # Exa Websets: async enrichment, verified operator pages
        "exa_websets":  7,
        # findSimilar: seeded from real gmaps operator websites — highly targeted
        "exa_similar":  6,
        # Google Maps (gosom): ground-truth geographic, real businesses
        "gosom_gmaps":  6, "google_places": 6,
        # Keyword search on business directories
        "exa_search":   5,
        # JustDial: direct business listings
        "justdial":     4,
        # NoBroker: mix of operators and rooms, lower signal
        "nobroker":     3,
        # Listing portals: mostly individual rooms
        "99acres": 2, "magicbricks": 2, "housing": 1,
    }
    df["source_score"] = df["source"].map(source_priority).fillna(1)

    # Sort: phones with values first, highest source score, newest first
    df = df.sort_values(
        ["phone_e164", "source_score", "freshness_score"],
        ascending=[True, False, False],
        na_position="last",
    )

    # 7a. By E.164 phone (NaN phones are NOT collapsed — pandas treats NaN as equal)
    has_phone = df["phone_e164"].notna()
    df_phone = df[has_phone].drop_duplicates(subset=["phone_e164"], keep="first")
    df_no_phone = df[~has_phone]
    df_deduped = pd.concat([df_phone, df_no_phone], ignore_index=True)

    # 7a-ii. By property_id / place_id (Maps, portals)
    pid = df_deduped["property_id"].astype(str).str.strip()
    has_pid = pid.ne("") & pid.ne("nan")
    df_with_id = df_deduped[has_pid].drop_duplicates(subset=["property_id"], keep="first")
    df_no_id = df_deduped[~has_pid]
    df_deduped = pd.concat([df_with_id, df_no_id], ignore_index=True)

    # 7b. Coordinate-based dedup only for rows with no phone AND no property_id
    no_phone = df_deduped["phone_e164"].isna()
    pid2 = df_deduped["property_id"].astype(str).str.strip()
    no_pid = pid2.eq("") | pid2.eq("nan")
    has_coords = df_deduped["lat_r"].notna() & df_deduped["lng_r"].notna()
    to_coord_dedup = df_deduped[no_phone & no_pid & has_coords]
    keep_others = df_deduped[~(no_phone & no_pid & has_coords)]
    to_coord_dedup = to_coord_dedup.drop_duplicates(subset=["lat_r", "lng_r"], keep="first")
    df_deduped = pd.concat([keep_others, to_coord_dedup], ignore_index=True)

    log.info("Deduplication: %d → %d rows (removed %d duplicates)",
             initial_count, len(df_deduped), initial_count - len(df_deduped))

    # 8. Final column selection and ordering
    output_cols = [
        "name", "sector", "address", "phone_raw", "phone_e164",
        "owner_name", "owner_type", "source", "listing_url",
        "rent", "rating", "freshness_score", "posted_on",
        "lat", "lng", "property_id", "website",
    ]
    # Only include columns that exist
    output_cols = [c for c in output_cols if c in df_deduped.columns]
    df_out = df_deduped[output_cols].copy()

    # 9. Save
    out_path = Path(MERGED_DATA_DIR) / "merged_contacts.csv"
    df_out.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("✓ Merged dataset saved → %s (%d rows)", out_path, len(df_out))

    # Summary stats
    log.info("── Sector breakdown ──")
    for sector, count in df_out["sector"].value_counts().items():
        log.info("  Sector %s: %d listings", sector, count)

    log.info("── Source breakdown ──")
    for source, count in df_out["source"].value_counts().items():
        log.info("  %s: %d listings", source, count)

    phones_found = df_out["phone_e164"].notna().sum()
    log.info("── Phone numbers found: %d / %d (%.1f%%)",
             phones_found, len(df_out), 100 * phones_found / max(len(df_out), 1))

    return df_out


if __name__ == "__main__":
    run()
