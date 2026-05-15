# =============================================================================
# config.py — Central configuration for Noida PG Operator Pipeline
# =============================================================================
# Secrets are read from the environment (and optional repo-root `.env`).
# Copy `.env.example` → `.env` and fill values, or export variables in your shell.
# See SETUP.md for step-by-step instructions on obtaining each key.
# =============================================================================

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip()


# ---------------------------------------------------------------------------
# 1. GOOGLE PLACES API (New, v1)
#    https://developers.google.com/maps/documentation/places/web-service/get-started
# ---------------------------------------------------------------------------
GOOGLE_PLACES_API_KEY = _env("GOOGLE_PLACES_API_KEY")

# If True, on HTTP 403 / errors from Places API (New), fall back to legacy
# Text Search + Place Details (enable "Places API" + billing in GCP).
GOOGLE_PLACES_USE_LEGACY_FALLBACK = True

# Max Place Details calls when using legacy fallback (each Text Search hit can
# return ~20 rows — uncapped details will exhaust quota quickly).
GOOGLE_LEGACY_DETAIL_CAP = 80

# ---------------------------------------------------------------------------
# 1b. GOSOM / GOOGLE MAPS SCRAPER (Docker) — optional high-coverage Maps leads
#     https://github.com/gosom/google-maps-scraper
#
#     Production flow (approval-gated; does not start scrape by default):
#       ./scripts/run_gmaps_scraper.sh preflight
#       GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh queries
#       # optional: geocode sectors → queries.csv → one Docker per centroid (max viewport match):
#       python3 scripts/build_query_grid.py geocode
#       python3 scripts/gmaps_centroid_batches.py materialize
#       GMAPS_SCRAPE_APPROVED=yes python3 scripts/gmaps_centroid_batches.py run
#       # optional hybrid mop, then merge:
#       GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh grid-mop
#       python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results.csv gmaps-output/gmaps_grid_mop.csv
#
#     Env: GMAPS_SCRAPE_APPROVED, GMAPS_GRID_BBOX, GMAPS_GRID_CELL_KM, GOSOM_DOCKER_IMAGE
#     Reliability (see scripts/run_gmaps_scraper.sh): GMAPS_GOSOM_C, GMAPS_USE_GEO / GMAPS_GEO,
#     GOSOM_DOCKER_PLATFORM, GOSOM_DISABLE_TELEMETRY; optional GMAPS_EXIT_ON_INACTIVITY (usually omit;
#     scrapemate can exit ~60s early if set before first activity). Optional --fast-mode on queries.
# ---------------------------------------------------------------------------
GOSOM_GMAPS_ENABLED = True
# Path to CSV produced by Docker (-results flag), relative to repo root or absolute
GOSOM_GMAPS_CSV = "gmaps-output/gmaps_results.csv"

# ---------------------------------------------------------------------------
# 2. TARGET GEOGRAPHY
#    All residential Noida sectors (1–168, excluding non-residential/non-existent).
#    Industrial-only: 1–11, 80–83. Non-existent: 13, 103, 109, 111, 114.
#    Green/institutional-only: 21A, 33A, 38, 38A, 79, 95, 101, 104, 150–153, 167.
#    We include all sectors where PGs are realistically found.
# ---------------------------------------------------------------------------
TARGET_SECTORS = [
    # Inner Noida — high density, established PG supply
    "Sector 1", "Sector 2", "Sector 3", "Sector 4", "Sector 5", "Sector 6",
    "Sector 7", "Sector 8", "Sector 9", "Sector 10", "Sector 12", "Sector 14",
    "Sector 15", "Sector 15A", "Sector 16", "Sector 17", "Sector 18", "Sector 19",
    "Sector 20", "Sector 21", "Sector 22", "Sector 23", "Sector 24", "Sector 25",
    "Sector 26", "Sector 27", "Sector 28", "Sector 29", "Sector 30", "Sector 31",
    "Sector 32", "Sector 33", "Sector 34", "Sector 35", "Sector 36", "Sector 37",
    "Sector 39", "Sector 40", "Sector 41",
    # Mid Noida — IT/corporate belt
    "Sector 44", "Sector 45", "Sector 46", "Sector 47", "Sector 48", "Sector 49",
    "Sector 50", "Sector 51", "Sector 52", "Sector 53", "Sector 54", "Sector 55",
    "Sector 56", "Sector 57", "Sector 58", "Sector 59", "Sector 60", "Sector 61",
    "Sector 62", "Sector 63", "Sector 63A", "Sector 64", "Sector 65", "Sector 66",
    "Sector 67", "Sector 68", "Sector 69", "Sector 70", "Sector 71", "Sector 72",
    "Sector 73", "Sector 74", "Sector 75", "Sector 76", "Sector 77", "Sector 78",
    # South Noida — expressway belt, growing PG supply
    "Sector 100", "Sector 107", "Sector 108", "Sector 110",
    # Sector 105 = Greater Noida geography — excluded from this list
    "Sector 113", "Sector 115", "Sector 116", "Sector 117", "Sector 118",
    "Sector 119", "Sector 120", "Sector 121", "Sector 122", "Sector 123",
    "Sector 125", "Sector 126", "Sector 127", "Sector 128", "Sector 129",
    "Sector 130", "Sector 131", "Sector 132", "Sector 133", "Sector 134",
    "Sector 135", "Sector 136", "Sector 137", "Sector 138", "Sector 140",
    "Sector 142", "Sector 143", "Sector 143B", "Sector 144", "Sector 145",
    "Sector 146", "Sector 147", "Sector 148", "Sector 149",
]
TARGET_CITY    = "Noida"
TARGET_STATE   = "Uttar Pradesh"
TARGET_COUNTRY = "India"

# ── Google Places queries — all residential sectors ───────────────────────────
# Grouped in batches of ~20 sectors per query term to stay under API limits.
# Each query returns up to 20 results (Places API New) paginated × 3 pages = 60 max.
_ALL_SECTORS = [
    1,2,3,4,5,6,7,8,9,10,12,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,
    30,31,32,33,34,35,36,37,39,40,41,44,45,46,47,48,49,50,51,52,53,54,55,56,
    57,58,59,60,61,62,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,
    100,107,108,110,113,115,116,117,118,119,120,121,122,123,125,126,127,
    128,129,130,131,132,133,134,135,136,137,138,140,142,143,144,145,146,147,148,149,
]

GOOGLE_SEARCH_QUERIES = []
for _s in _ALL_SECTORS:
    GOOGLE_SEARCH_QUERIES.append(f"PG in Sector {_s} Noida")
    GOOGLE_SEARCH_QUERIES.append(f"paying guest Sector {_s} Noida")

# Extra intent variants for high-density sectors
for _s in [15, 16, 18, 22, 34, 41, 50, 51, 52, 57, 58, 62, 63, 66, 70, 73, 74, 75,
           100, 119, 120, 121, 122, 125, 128, 131, 134, 137, 142, 144]:
    GOOGLE_SEARCH_QUERIES.append(f"boys PG Sector {_s} Noida")
    GOOGLE_SEARCH_QUERIES.append(f"girls PG Sector {_s} Noida")
    GOOGLE_SEARCH_QUERIES.append(f"hostel Sector {_s} Noida")

# Remove duplicates while preserving order
_seen = set()
GOOGLE_SEARCH_QUERIES = [q for q in GOOGLE_SEARCH_QUERIES
                          if not (q in _seen or _seen.add(q))]

# ---------------------------------------------------------------------------
# 3. NOBROKER — optional standalone extractor (not run by main.py)
#    To backfill:  python -m phase1_extract.nobroker_api
#    Export from browser DevTools after manual login at nobroker.in
# ---------------------------------------------------------------------------
NOBROKER_COOKIES_RAW = _env("NOBROKER_COOKIES_RAW")

# NoBroker search bounding box — full Noida (inner + expressway belt)
NOBROKER_BBOX = {
    "lat_min": 28.495,   # south tip (Sector 150 area)
    "lat_max": 28.660,   # north tip (Sector 1 / DND flyover area)
    "lng_min": 77.310,   # west (Sector 1–12 belt)
    "lng_max": 77.430,   # east (Sector 62–63 / expressway belt)
}

# NoBroker v3 filter: shared rooms / PG-style
NOBROKER_SHARED_ACCOMMODATION = True

# Explicit map pins covering all major Noida PG clusters.
# NoBroker API accepts up to ~20 pins per request; we cover 5 geographic zones.
NOBROKER_SECTOR_PINS = [
    # Zone A — Inner / North Noida (Sectors 1–41)
    {"lat": 28.638, "lon": 77.315, "placeName": "Sector 1, Noida",  "showMap": False},
    {"lat": 28.630, "lon": 77.325, "placeName": "Sector 4, Noida",  "showMap": False},
    {"lat": 28.625, "lon": 77.345, "placeName": "Sector 12, Noida", "showMap": False},
    {"lat": 28.618, "lon": 77.355, "placeName": "Sector 15, Noida", "showMap": False},
    {"lat": 28.615, "lon": 77.360, "placeName": "Sector 18, Noida", "showMap": False},
    {"lat": 28.612, "lon": 77.370, "placeName": "Sector 22, Noida", "showMap": False},
    {"lat": 28.607, "lon": 77.375, "placeName": "Sector 27, Noida", "showMap": False},
    {"lat": 28.600, "lon": 77.380, "placeName": "Sector 34, Noida", "showMap": False},
    {"lat": 28.596, "lon": 77.385, "placeName": "Sector 39, Noida", "showMap": False},
    # Zone B — Mid Noida IT Belt (Sectors 44–78)
    {"lat": 28.592, "lon": 77.355, "placeName": "Sector 50, Noida", "showMap": False},
    {"lat": 28.588, "lon": 77.360, "placeName": "Sector 52, Noida", "showMap": False},
    {"lat": 28.584, "lon": 77.365, "placeName": "Sector 55, Noida", "showMap": False},
    {"lat": 28.632, "lon": 77.390, "placeName": "Sector 57, Noida", "showMap": False},
    {"lat": 28.630, "lon": 77.383, "placeName": "Sector 58, Noida", "showMap": False},
    {"lat": 28.622, "lon": 77.372, "placeName": "Sector 62, Noida", "showMap": False},
    {"lat": 28.614, "lon": 77.363, "placeName": "Sector 63, Noida", "showMap": False},
    {"lat": 28.608, "lon": 77.395, "placeName": "Sector 66, Noida", "showMap": False},
    {"lat": 28.602, "lon": 77.400, "placeName": "Sector 70, Noida", "showMap": False},
    {"lat": 28.596, "lon": 77.405, "placeName": "Sector 75, Noida", "showMap": False},
    # Zone C — South Noida / Expressway (Sectors 100–149)
    {"lat": 28.566, "lon": 77.365, "placeName": "Sector 100, Noida", "showMap": False},
    {"lat": 28.551, "lon": 77.355, "placeName": "Sector 110, Noida", "showMap": False},
    {"lat": 28.535, "lon": 77.345, "placeName": "Sector 119, Noida", "showMap": False},
    {"lat": 28.525, "lon": 77.338, "placeName": "Sector 122, Noida", "showMap": False},
    {"lat": 28.514, "lon": 77.330, "placeName": "Sector 128, Noida", "showMap": False},
    {"lat": 28.503, "lon": 77.322, "placeName": "Sector 134, Noida", "showMap": False},
    {"lat": 28.492, "lon": 77.315, "placeName": "Sector 143, Noida", "showMap": False},
    {"lat": 28.482, "lon": 77.308, "placeName": "Sector 149, Noida", "showMap": False},
]

# ---------------------------------------------------------------------------
# 4. PROXY CONFIGURATION (optional but strongly recommended)
#    Use rotating Indian residential proxies (Jio/Airtel/Vodafone IP pools)
#    Format: "http://user:pass@host:port"
# ---------------------------------------------------------------------------
USE_PROXIES = False   # Set True when you have proxies configured
PROXY_LIST  = [
    # "http://user:pass@proxy1:port",
    # "http://user:pass@proxy2:port",
]

# ---------------------------------------------------------------------------
# 5. EXA AI — Neural web search + Websets lead enrichment
#    https://dashboard.exa.ai/api-keys
#    You have $100 credit. Full pipeline costs ~$0.15 (Search) or ~$10 (Websets).
# ---------------------------------------------------------------------------
EXA_API_KEY = _env("EXA_API_KEY")

# Set True to also run Exa Websets (enriched structured leads, costs ~$5–10)
# Set False to run Search-only mode (costs ~$0.15, very fast)
EXA_USE_WEBSETS = False

# ---------------------------------------------------------------------------
# 6. NUMVERIFY — Telecom / HLR validation
#    https://numverify.com  (free tier: 100 req/month)
# ---------------------------------------------------------------------------
NUMVERIFY_API_KEY = _env("NUMVERIFY_API_KEY")

# ---------------------------------------------------------------------------
# 6. TWILIO LOOKUP (optional alternative to Numverify)
#    https://www.twilio.com/docs/lookup
# ---------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
USE_TWILIO_LOOKUP  = False   # Set True to use Twilio instead of Numverify

# ---------------------------------------------------------------------------
# 7. WHATSAPP BUSINESS CLOUD API — presence verification
#    https://developers.facebook.com/docs/whatsapp/cloud-api
# ---------------------------------------------------------------------------
WHATSAPP_API_TOKEN = _env("WHATSAPP_API_TOKEN")
WHATSAPP_PHONE_ID = _env("WHATSAPP_PHONE_ID")

# ---------------------------------------------------------------------------
# 8. OUTPUT PATHS
# ---------------------------------------------------------------------------
RAW_DATA_DIR    = "data/raw"
MERGED_DATA_DIR = "data/merged"
OUTPUT_CSV      = "data/final_pg_contacts.csv"
LOG_FILE        = "pipeline.log"

# ---------------------------------------------------------------------------
# 9. SCRAPER BEHAVIOUR
# ---------------------------------------------------------------------------
REQUEST_DELAY_SECONDS  = 2.5   # polite delay between requests
PROXY_ROTATE_EVERY     = 7     # rotate proxy every N requests
MAX_RETRIES            = 3
PLAYWRIGHT_HEADLESS    = True  # set False to watch browser during debug

# JustDial mobile listings are JS-rendered — set True after `playwright install chromium`
JUSTDIAL_USE_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# 10. BROKER KEYWORD FILTER
#     Numbers whose Truecaller name contains any of these strings are flagged
# ---------------------------------------------------------------------------
BROKER_KEYWORDS = [
    "associate", "properties", "realtor", "real estate",
    "broker", "realty", "estates", "consultancy", "homes",
    "infra", "infratech", "builders", "developers", "agency",
    "proptech", "housing solutions",
]
