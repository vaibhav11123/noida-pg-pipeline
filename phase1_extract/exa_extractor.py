"""
phase1_extract/exa_extractor.py
================================
Exa AI deep-dive extractor targeting BUILDING-SCALE PG operators and owners.

WHO WE WANT
-----------
  • PG operators running 20+ bed hostels who might want to expand to a new building
  • Building/property owners who lease their entire building for PG/hostel use
  • Managed coliving companies (Zolo, Stanza, etc.) looking for new properties
  • Real estate investors with large buildings open to PG lease arrangements

WHO WE DO NOT WANT
------------------
  • Anyone renting or looking for a single room
  • Brokers listing individual units
  • Generic listing portal pages (NoBroker, 99acres, MagicBricks index pages)

GEO FUNNEL (conical expansion)
-------------------------------
  CONE TIP   → Noida   : Local operators already running PGs in Noida
  CONE MID   → Delhi   : Delhi operators who may want Noida expansion
  CONE WIDE  → NCR     : Gurugram/Faridabad/Ghaziabad/Greater Noida operators
  CONE BASE  → India   : Pan-India managed PG companies seeking new cities

MODES
-----
  Mode 1 — findSimilar  : Seed with 84 real operator websites extracted from
                          Google Maps. Exa finds hundreds of structurally similar
                          operator pages. Cheapest, fastest, highest relevance.
                          Run this first, always.

  Mode 2 — Agent        : Exa Agent (beta) does multi-hop research per sector
                          batch. Extracts {name, phone, email, address, capacity}
                          as structured JSON. $0.07/phone, $0.02/email.
                          Run sector-by-sector, Sectors 57/58/62/63 first.

  Mode 3 — Search       : Operator-intent keyword queries with category="company"
                          on business directories. Covers IndiaMART, Sulekha, JD.
                          Good as a sweep after Modes 1+2.

  Canonical workflow, architecture, merge contract, troubleshooting:
  → phase1_extract/EXA_EXTRACTOR_RUNBOOK.md

CLI
---
  # Start here — uses YOUR gmaps operator websites as seeds:
  python phase1_extract/exa_extractor.py --mode similar

  # Deep structured extraction per sector (best quality, costs more):
  python phase1_extract/exa_extractor.py --mode agent --agent-batches 4

  # Keyword sweep across business directories:
  python phase1_extract/exa_extractor.py --mode search --tier noida

  # All modes, full Noida cone:
  python phase1_extract/exa_extractor.py --mode all --tier noida

  # Full conical run (Noida → Delhi → NCR → India):
  python phase1_extract/exa_extractor.py --mode all --tier all

  # Cheap live sanity check (real API; caps volume) before a full run:
  python phase1_extract/exa_extractor.py --smoke --mode similar
  # Writes data/raw/exa_similar_smoke.json + .csv (leaves full exa_similar.* intact)
  python phase1_extract/exa_extractor.py --smoke --mode all --tier noida

Outputs (all in data/raw/):
  exa_similar.json / .csv   (Mode 1)
  exa_agent.json   / .csv   (Mode 2)
  exa_search.json  / .csv   (Mode 3)
  With --smoke: exa_*_smoke.json / .csv (same columns; safe for merge pipeline)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

# Ensure project root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import RAW_DATA_DIR, REQUEST_DELAY_SECONDS  # noqa: E402

try:
    from exa_py import Exa  # pyright: ignore[reportMissingImports] — optional; see requirements.txt (exa-py)
    EXA_SDK_AVAILABLE = True
except ImportError:
    Exa = None  # type: ignore[assignment]
    EXA_SDK_AVAILABLE = False

try:
    from config import EXA_API_KEY  # noqa: E402
except ImportError:
    EXA_API_KEY = "YOUR_EXA_API_KEY_HERE"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [EXA] %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SEED URLS — real PG operator websites extracted from Google Maps scrape
# These are the backbone of Mode 1 (findSimilar).
# Loaded from gmaps_operator_seeds.json if available; fallback list below.
# ─────────────────────────────────────────────────────────────────────────────

FALLBACK_SEEDS: tuple[str, ...] = (
    "https://www.standardstays.in",
    "https://neemhomes.in",
    "https://phsgirlspgnoida.com",
    "https://slateliving.in",
    "https://happy-living.in",
    "https://primestayz.com",
    "https://pgwalaah.com",
    "https://www.shftin.in",
    "https://addoluxuria.com",
    "https://admirehomes.in",
    "https://www.mothertouchhomes.com",
    "https://soholiv.com",
    "https://bljhomes.in",
    "https://www.aggarwalpgnoida.com",
    "https://anorehomes.com",
    "https://paramparastay.in",
    "https://otostays.com",
    "https://guestlostays.com",
    "https://www.vizima.in",
    "https://www.stayinclass.in",
    "https://amyhousepg.com",
    "https://www.viratmansion.com",
    "https://www.homesteadpg.in",
    "https://flycolive.com",
    "https://comfortstaypg.in",
    "https://madhavpg.in",
    "https://prestigeparadise.homes",
    "https://aurumliving.net",
    "https://shrikrishnaboyspg.co.in",
)

_JUNK_SEED_DOMAINS = re.compile(
    r"(wix\.|canva\.|facebook\.|wa\.me|weebly\.|wordpress\.|grexa\.|netlify\.|"
    r"framer\.|blogspot\.|great-site\.net)",
    re.I,
)


def load_seeds() -> list[str]:
    """Load operator seed URLs from gmaps extraction, fall back to hardcoded list."""
    seeds_path = Path(RAW_DATA_DIR) / "gmaps_operator_seeds.json"
    if seeds_path.exists():
        try:
            seeds = json.loads(seeds_path.read_text())
            urls = [s["url"] for s in seeds if s.get("url")]
            urls = [u for u in urls if not _JUNK_SEED_DOMAINS.search(u)]
            log.info("Loaded %d operator seeds from gmaps_operator_seeds.json", len(urls))
            return urls
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Could not load seeds file: %s — using fallback", exc)
    log.info("Using fallback seed list (%d URLs)", len(FALLBACK_SEEDS))
    return list(FALLBACK_SEEDS)


# ─────────────────────────────────────────────────────────────────────────────
# GEO CONE — conical expansion from Noida outward
# ─────────────────────────────────────────────────────────────────────────────

GEO_CONE: tuple[str, ...] = ("noida", "delhi", "ncr", "india")


def resolve_tiers(tiers: Sequence[str] | None) -> list[str]:
    """Normalize and validate the requested geo tiers."""
    if not tiers:
        return ["noida"]
    flat = [t.strip().lower() for t in tiers]
    if "all" in flat:
        return list(GEO_CONE)
    valid = [t for t in flat if t in GEO_CONE]
    return valid or ["noida"]


# ─────────────────────────────────────────────────────────────────────────────
# QUALITY FILTERS
# ─────────────────────────────────────────────────────────────────────────────

# Portal index pages — listing portals, not operators
_PORTAL_URL = re.compile(
    r"(nobroker\.in/pg-in-|nobroker\.in/male-roommates|nobroker\.in/female-roommates|"
    r"99acres\.com/pg-in-|magicbricks\.com/pg-|housing\.com/paying-guests/|"
    r"squareyards\.com/rent/pg-|commonfloor\.com|makaan\.com)",
    re.I,
)

# Titles that signal we're on a listing index or wrong category
_REJECT_TITLE = re.compile(
    r"\b(school|college|university|coaching|institute|"
    r"hospital|clinic|pharmacy|medical|"
    r"restaurant|hotel\s+booking|catering|"
    r"software|hardware|it\s+solution|technology company|"
    r"factory|warehouse|industrial|manufacturing|"
    r"news|article|blog post|press release|"
    r"for sale|buy flat|buy apartment|plot for sale)\b",
    re.I,
)

# Must have PG/hostel context
_PG_CONTEXT = re.compile(
    r"\b(paying guest|pg|hostel|coliving|co-living|accommodation|"
    r"dormitory|beds?|rooms? for rent|working professional|student stay|"
    r"boys.?pg|girls.?pg|mens.?pg|ladies.?pg|mixed.?pg)\b",
    re.I,
)

# Operator scale signals — confirms building-level operation
_SCALE_SIGNALS = re.compile(
    r"\b("
    r"\d{2,3}\s*beds?|\d{2,3}\s*rooms?|"
    r"capacity|total beds|multiple floors?|entire building|whole building|"
    r"managed property|property manager|hostel management|"
    r"franchise|partner property|lease agreement|"
    r"GSTIN|GST\s*no|registered business|Pvt\.?\s*Ltd|LLP|"
    r"expansion|new property|new location|looking for building|"
    r"50\+?\s*beds?|100\+?\s*beds?|200\+?\s*beds?"
    r")\b",
    re.I,
)

# Emails that are clearly artifacts
_JUNK_EMAIL = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js)$", re.I)
_SYSTEM_EMAILS: frozenset[str] = frozenset({
    "noreply@nobroker.in", "hello@nobroker.in", "assist@nobroker.in",
    "feedback@99acres.com", "noreply@99acres.com",
    "feedback@magicbricks.com", "crm@magicbricks.com",
    "support@housing.com", "editor@housing.com",
    "enquire@2x.png",
})


def is_valid(title: str, url: str, text: str) -> bool:
    """Reject portal pages, off-topic titles; require PG context in title+text."""
    if _PORTAL_URL.search(url):
        return False
    if title and _REJECT_TITLE.search(title):
        return False
    combined = f"{title} {text[:2000]}"
    return bool(_PG_CONTEXT.search(combined))


def operator_scale(text: str) -> str:
    """Heuristic: 'confirmed' if scale signals appear; otherwise 'possible'."""
    return "confirmed" if _SCALE_SIGNALS.search(text[:4000]) else "possible"


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s\-]?)?[6-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(
    r"\b[A-Z0-9][A-Z0-9._%+\-]*@[A-Z0-9][A-Z0-9.\-]*\.[A-Z]{2,63}\b", re.I,
)
_SKIP_LOCALS: frozenset[str] = frozenset({
    "noreply", "no-reply", "donotreply", "postmaster", "mailer-daemon",
})
_ADDR_RE = re.compile(
    r"sector\s*\d+|\bnoida\b|gurugram|gurgaon|faridabad|ghaziabad|"
    r"greater\s*noida|new\s*delhi|\bdelhi\b|rohini|dwarka|ncr",
    re.I,
)


def phones(text: str) -> list[str]:
    """Extract unique 10-digit Indian mobile numbers, stripping +91/0 prefixes."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", match)
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if digits.startswith("0") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "6789" and digits not in seen:
            seen.add(digits)
            out.append(digits)
    return out


def emails(text: str) -> list[str]:
    """Extract unique, non-system, non-artifact email addresses."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _EMAIL_RE.findall(text or ""):
        email = raw.lower()
        if _JUNK_EMAIL.search(email) or email in _SYSTEM_EMAILS:
            continue
        local = email.split("@", 1)[0]
        if local in _SKIP_LOCALS or local.startswith("noreply"):
            continue
        if email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def address(text: str) -> str:
    """Return the first text line that looks like an NCR-area address."""
    for line in (text or "").splitlines():
        if _ADDR_RE.search(line):
            return line.strip()[:200]
    return ""


def hl_text(result: Any) -> str:
    """Combine page text + highlights from a search result object."""
    page = getattr(result, "text", "") or ""
    highlights = getattr(result, "highlights", []) or []
    if highlights and hasattr(highlights[0], "text"):
        hl_str = " ".join(h.text for h in highlights)
    else:
        hl_str = " ".join(str(h) for h in highlights)
    return f"{page} {hl_str}"


def _http_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status from SDK / httpx exceptions (no substring heuristics)."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        rcode = getattr(resp, "status_code", None)
        if isinstance(rcode, int):
            return rcode
    return None


def _is_credit_error(exc: Exception) -> bool:
    """Detect Exa 'out of credits' errors so we can stop early.

    Avoid matching ``'402'`` as a substring of UUIDs in error JSON (false positives).
    """
    err = str(exc).lower()
    if "no_more_credits" in err:
        return True
    if _http_status_code(exc) == 402:
        return True
    if "status code 402" in err or " 402 " in err:
        return True
    if "insufficient" in err and "credit" in err:
        return True
    if "out of credit" in err or "no credit" in err:
        return True
    return False


def _is_transient_agent_error(exc: BaseException) -> bool:
    """HTTP conditions where retrying the same agent batch may succeed."""
    code = _http_status_code(exc)
    if code is not None and code in (408, 429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return (
        "unable to handle your request" in msg
        or "internal server error" in msg
        or "bad gateway" in msg
        or "gateway timeout" in msg
        or "service unavailable" in msg
    )


def save(results: list[dict], stem: str, columns: list[str]) -> None:
    """Persist results to data/raw/{stem}.json and {stem}.csv."""
    raw = Path(RAW_DATA_DIR)
    raw.mkdir(parents=True, exist_ok=True)
    json_path = raw / f"{stem}.json"
    csv_path = raw / f"{stem}.csv"

    with json_path.open("w", encoding="utf-8") as fp:
        json.dump(results, fp, ensure_ascii=False, indent=2)

    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow(
                {c: "" if row.get(c) is None else str(row[c]) for c in columns}
            )
    log.info("✓ %s.json + .csv  (%d rows)", stem, len(results))


# ─────────────────────────────────────────────────────────────────────────────
# MODE 1 — findSimilar
# Seed with real operator websites from gmaps. Exa finds structurally similar
# pages — other PG operator sites, business directory listings, company pages.
# Cheapest and most targeted mode. Run this first.
# ─────────────────────────────────────────────────────────────────────────────

SIMILAR_COLS = [
    "source", "geo_tier", "operator_scale",
    "name", "address", "phone_raw", "email_raw",
    "listing_url", "seed_url", "exa_score",
]


def run_similar(
    exa_client: Any,
    tiers: list[str],
    results_per_seed: int = 20,
    max_seeds: int | None = None,
) -> list[dict]:
    """
    For each seed URL (real PG operator website from gmaps), ask Exa to find
    similar pages. Returns operator contacts found on similar pages.

    Each seed fans out to ~results_per_seed similar pages. With 60 seeds,
    this can discover 1000+ operator pages at very low cost.
    """
    del tiers  # geo cone not used by findSimilar; seeds are already Noida-anchored
    seeds = load_seeds()
    if max_seeds is not None:
        seeds = seeds[: max(0, max_seeds)]
    all_results: list[dict] = []
    seen_urls: set[str] = set()

    log.info(
        "findSimilar: %d seeds × %d results/seed = up to %d pages",
        len(seeds), results_per_seed, len(seeds) * results_per_seed,
    )

    for seed_url in seeds:
        log.info("  Seed: %s", seed_url)
        try:
            resp = exa_client.find_similar_and_contents(
                seed_url,
                num_results=results_per_seed,
                exclude_source_domain=True,
                start_published_date="2022-01-01",
                text={"max_characters": 3000},
                highlights={"num_sentences": 3, "highlights_per_url": 2},
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises a wide variety
            log.warning("  findSimilar failed for %s: %s", seed_url, exc)
            if _is_credit_error(exc):
                log.error("Exa credits exhausted — stopping")
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        added = 0
        for result in resp.results:
            url = result.url or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            full = hl_text(result)
            title = result.title or ""
            if not is_valid(title, url, full):
                continue

            phs = phones(full)
            ems = emails(full)
            if not phs and not ems:
                continue

            scale = operator_scale(full)
            addr = address(full)
            score = getattr(result, "score", None)

            for phone in phs or [""]:
                all_results.append({
                    "source":         "exa_similar",
                    "geo_tier":       "noida",
                    "operator_scale": scale,
                    "name":           title,
                    "address":        addr,
                    "phone_raw":      phone,
                    "email_raw":      ems[0] if ems else "",
                    "listing_url":    url,
                    "seed_url":       seed_url,
                    "exa_score":      score,
                })
                added += 1

        log.info("    → %d pages scanned | %d leads added", len(resp.results), added)
        time.sleep(REQUEST_DELAY_SECONDS)

    confirmed = sum(1 for r in all_results if r["operator_scale"] == "confirmed")
    log.info("findSimilar done: %d leads (%d confirmed operators)", len(all_results), confirmed)
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# MODE 2 — Exa Agent (beta)
# Deep multi-hop research per sector batch. Structured JSON output.
# Exa Agent searches, visits pages, cross-references, and extracts
# {name, phone, email, address, capacity} directly — no regex needed.
# Cost: ACUs + $0.07/phone + $0.02/email. Takes 2–5 min per batch.
# ─────────────────────────────────────────────────────────────────────────────

# Sector batches ordered by PG density (highest first)
SECTOR_BATCHES: list[dict[str, str]] = [
    # Tier 1 — IT belt, maximum PG density
    {"label": "it_belt_core",     "sectors": "57, 58, 62, 63, 63A, 66"},
    {"label": "it_belt_extended", "sectors": "44, 45, 46, 47, 48, 49, 50, 51, 52, 53"},
    {"label": "it_belt_south",    "sectors": "54, 55, 56, 59, 60, 61, 64, 65, 67, 68, 69, 70"},
    {"label": "it_belt_west",     "sectors": "71, 72, 73, 74, 75, 76, 77, 78"},
    # Tier 2 — Inner Noida
    {"label": "inner_east",       "sectors": "15, 15A, 16, 17, 18, 19, 20, 21, 22, 23"},
    {"label": "inner_north",      "sectors": "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14"},
    {"label": "inner_south",      "sectors": "24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 39, 40, 41"},
    # Tier 3 — South Noida expressway
    {"label": "south_near",       "sectors": "100, 107, 108, 110, 113, 115, 116, 117, 118, 119, 120"},
    {"label": "south_mid",        "sectors": "121, 122, 123, 125, 126, 127, 128, 129, 130, 131, 132"},
    {"label": "south_far",        "sectors": "133, 134, 135, 136, 137, 138, 140, 142, 143, 143B, 144, 145, 146, 147, 148, 149"},
]

DELHI_BATCHES: list[dict[str, str]] = [
    {"label": "delhi_north",   "sectors": "Rohini, Pitampura, Shalimar Bagh, Ashok Vihar"},
    {"label": "delhi_south",   "sectors": "South Delhi, Saket, Malviya Nagar, Lajpat Nagar"},
    {"label": "delhi_central", "sectors": "Karol Bagh, Patel Nagar, Rajouri Garden"},
    {"label": "delhi_east",    "sectors": "Laxmi Nagar, Preet Vihar, Mayur Vihar, Patparganj"},
]

NCR_BATCHES: list[dict[str, str]] = [
    {"label": "gurugram",      "sectors": "Gurugram (DLF, Cyber City, Sohna Road, Golf Course Road)"},
    {"label": "faridabad",     "sectors": "Faridabad (Sector 14, 15, 16, NIT, Old Faridabad)"},
    {"label": "ghaziabad",     "sectors": "Ghaziabad (Indirapuram, Vaishali, Kaushambi, Raj Nagar)"},
    {"label": "greater_noida", "sectors": "Greater Noida (Knowledge Park, Gamma, Alpha, Beta, Omicron)"},
]

INDIA_BATCHES: list[dict[str, str]] = [
    {"label": "india_managed_pg", "sectors": "pan-India managed PG / coliving companies"},
]

TIER_BATCHES: dict[str, list[dict[str, str]]] = {
    "noida": SECTOR_BATCHES,
    "delhi": DELHI_BATCHES,
    "ncr":   NCR_BATCHES,
    "india": INDIA_BATCHES,
}

# outputSchema — Exa Agent fills this from the pages it reads
AGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["operators"],
    "properties": {
        "operators": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["business_name"],
                "properties": {
                    "business_name":    {"type": "string"},
                    "owner_or_manager": {"type": "string"},
                    "phone":            {"type": "string", "format": "phone"},
                    "alternate_phone":  {"type": "string", "format": "phone"},
                    "email":            {"type": "string", "format": "email"},
                    "address":          {"type": "string"},
                    "sector":           {"type": "string"},
                    "bed_capacity":     {"type": "string"},
                    "property_type": {
                        "type": "string",
                        "description": "boys / girls / co-ed / mixed",
                    },
                    "source_url":       {"type": "string", "format": "uri"},
                    "open_to_lease_new_building": {"type": "boolean"},
                },
            },
        },
    },
}

AGENT_COLS = [
    "source", "geo_tier", "operator_scale",
    "name", "owner_name", "address", "phone_raw", "alternate_phone",
    "email_raw", "capacity", "property_type", "open_to_lease",
    "listing_url", "agent_run_id", "zone",
]

EXA_AGENT_BETA = "agent-2026-05-07"

# Retries when Exa Agent /runs returns 5xx or rate limits (transient upstream).
AGENT_BATCH_MAX_RETRIES = 4
AGENT_RETRY_BASE_SECONDS = 15.0


def _agent_query(batch: dict, geo: str) -> str:
    """Compose a natural-language research brief for the Exa Agent."""
    sectors = batch["sectors"]
    if geo == "noida":
        return (
            f"Find PG hostel OPERATORS and BUILDING OWNERS in Noida Sectors {sectors}, "
            f"Uttar Pradesh, India. "
            f"I need people who: (1) run PG hostels with 20 or more beds / multiple rooms — "
            f"they already operate a hostel business and may want to expand to a new building; "
            f"(2) own a large independent house or building and lease it entirely for PG / hostel use. "
            f"Search on: IndiaMART, Sulekha, JustDial business category pages, "
            f"local PG operator websites, Google Maps business listings. "
            f"EXCLUDE: anyone renting a single room, NoBroker/99acres listings, "
            f"individual room-seekers, brokers advertising one room. "
            f"For each operator: extract business name, owner/manager name, "
            f"mobile phone, email, full address with sector, bed/room capacity, "
            f"and the source URL."
        )
    if geo == "delhi":
        return (
            f"Find PG hostel operators and property owners in Delhi — specifically in "
            f"{sectors} — who run large PG hostels (20+ beds) or own buildings "
            f"leased for hostel use. I am looking for Delhi-based operators who might "
            f"want to expand to a new building in Noida or who already have properties "
            f"in both Delhi and Noida. Search IndiaMART, Sulekha, JustDial, and "
            f"local operator websites. For each: name, phone, email, address, capacity."
        )
    if geo == "ncr":
        return (
            f"Find large-scale PG hostel operators and managed coliving companies in "
            f"{sectors}, NCR, India. I want companies or individuals running 20+ bed "
            f"hostels who might be interested in leasing a new building in Noida. "
            f"Also include property owners who have leased entire buildings for PG use "
            f"in these areas. Source: IndiaMART, Sulekha, JustDial, company websites. "
            f"For each: name, phone, email, address, capacity, source URL."
        )
    # india
    return (
        "Find pan-India managed PG and coliving companies that operate in multiple "
        "cities and are actively expanding. I want their business development or "
        "property acquisition contact — the person who evaluates new buildings for "
        "lease. Companies like Stanza Living, Zolo, OYO Life, Nestaway, Cofynd, "
        "Settl, Housr, and similar. Also find large independent PG operators with "
        "10+ properties across India. For each company: name, BD contact name, "
        "phone, email, expansion cities, property acquisition criteria, website."
    )


def _build_agent_batch_plan(
    tiers: Iterable[str], max_batches: int,
) -> list[tuple[dict, str]]:
    """Flatten requested tiers into a (batch, geo) plan capped at max_batches."""
    plan: list[tuple[dict, str]] = []
    for tier in tiers:
        for batch in TIER_BATCHES.get(tier, []):
            plan.append((batch, tier))
    return plan[:max_batches]


def _first_phone(value: str) -> str:
    found = phones(value)
    return found[0] if found else (value or "")


def _first_email(value: str) -> str:
    found = emails(value)
    return found[0] if found else (value or "")


def run_agent(
    exa_client: Any,
    tiers: list[str],
    max_batches: int = 4,
) -> list[dict]:
    """
    Exa Agent deep research. One run per sector batch.
    Returns structured operator data directly from Exa — no regex extraction.
    """
    all_results: list[dict] = []
    batch_list = _build_agent_batch_plan(tiers, max_batches)

    log.info("Agent mode: %d batches across tiers %s", len(batch_list), tiers)

    sdk_missing = False
    for batch, geo in batch_list:
        if sdk_missing:
            break
        query = _agent_query(batch, geo)
        label = batch["label"]
        log.info("[agent/%s/%s] %s", geo, label, query[:100])

        run_obj: Any = None
        for attempt in range(AGENT_BATCH_MAX_RETRIES):
            try:
                run_obj = exa_client.beta.agent.runs.create(
                    betas=[EXA_AGENT_BETA],
                    query=query,
                    effort="auto",
                    output_schema=AGENT_SCHEMA,
                )
                log.info("  Run %s created | status=%s", run_obj.id, run_obj.status)
                run_obj = exa_client.beta.agent.runs.poll_until_finished(
                    run_obj.id,
                    betas=[EXA_AGENT_BETA],
                    poll_interval=5000,
                )
                log.info("  Run %s finished | status=%s", run_obj.id, run_obj.status)
                break
            except AttributeError:
                log.error("Exa Agent beta not available — upgrade: pip install --upgrade exa-py")
                sdk_missing = True
                break
            except Exception as exc:  # noqa: BLE001
                if _is_credit_error(exc):
                    log.error("Exa credits exhausted [%s/%s]: %s", geo, label, exc)
                    return all_results
                if _is_transient_agent_error(exc) and attempt + 1 < AGENT_BATCH_MAX_RETRIES:
                    wait = AGENT_RETRY_BASE_SECONDS * (2**attempt)
                    log.warning(
                        "  Agent transient error [%s/%s] (attempt %d/%d): %s — retry in %.0fs",
                        geo, label, attempt + 1, AGENT_BATCH_MAX_RETRIES, exc, wait,
                    )
                    time.sleep(wait)
                    run_obj = None
                    continue
                log.error("Agent run failed [%s/%s]: %s", geo, label, exc)
                time.sleep(REQUEST_DELAY_SECONDS * 3)
                run_obj = None
                break

        if sdk_missing:
            break
        if run_obj is None:
            continue

        output = getattr(run_obj, "output", None)
        if not output:
            log.warning("  No output from agent run %s", run_obj.id)
            continue

        structured = getattr(output, "structured", None) or {}
        ops = structured.get("operators", []) if isinstance(structured, dict) else []
        if not ops:
            text_preview = str(getattr(output, "text", ""))[:200]
            log.warning("  0 operators returned. text preview: %s", text_preview)
            continue

        log.info("  %d operators extracted", len(ops))

        for op in ops:
            if not isinstance(op, dict):
                continue

            phone_clean = _first_phone(op.get("phone", "") or "")
            alt_clean = _first_phone(op.get("alternate_phone", "") or "")
            email_clean = _first_email(op.get("email", "") or "")
            if not phone_clean and not email_clean:
                continue

            sector = op.get("sector", "") or ""
            addr = op.get("address", "") or (f"Noida Sector {sector}" if sector else "")
            capacity = op.get("bed_capacity", "") or ""
            src_url = op.get("source_url", "") or ""
            open_to = op.get("open_to_lease_new_building")

            scale = operator_scale(f"{capacity} {addr} {src_url}")

            all_results.append({
                "source":         "exa_agent",
                "geo_tier":       geo,
                "operator_scale": scale,
                "name":           op.get("business_name", "") or "",
                "owner_name":     op.get("owner_or_manager", "") or "",
                "address":        addr,
                "phone_raw":      phone_clean,
                "alternate_phone": alt_clean,
                "email_raw":      email_clean,
                "capacity":       capacity,
                "property_type":  op.get("property_type", "") or "",
                "open_to_lease":  "" if open_to is None else str(open_to),
                "listing_url":    src_url,
                "agent_run_id":   run_obj.id,
                "zone":           label,
            })

        time.sleep(REQUEST_DELAY_SECONDS * 3)

    confirmed = sum(1 for r in all_results if r["operator_scale"] == "confirmed")
    log.info("Agent done: %d leads (%d confirmed)", len(all_results), confirmed)
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# MODE 3 — Keyword Search (operator-intent, conical geo)
# Operator-intent queries on business directories.
# category="company" on IndiaMART/Sulekha queries for better page quality.
# ─────────────────────────────────────────────────────────────────────────────

# Domains: business directories with real operator listings — not listing portals
OPERATOR_DOMAINS: list[str] = [
    "indiamart.com", "sulekha.com", "justdial.com",
    "tradeindia.com", "exportersindia.com",
    "indianyellowpages.com", "grihumindia.com",
    "pgwalaah.com", "stanzaliving.com", "zolostays.com",
    "settl.in", "cofynd.com", "colive.com", "housr.in",
]

# Queries per tier — each tagged (query, use_company_category)
SEARCH_QUERIES: dict[str, list[tuple[str, bool]]] = {
    "noida": [
        # Business directory queries — category="company" improves results
        ("PG hostel accommodation operator Noida IndiaMART business owner phone",          True),
        ("paying guest accommodation service provider Noida Sulekha business contact",     True),
        ("PG hostel business operator Noida Sector 57 62 JustDial owner phone number",     True),
        ("managed PG hostel Noida multiple rooms business listing operator contact",       True),
        ("boys girls hostel Noida 50 100 beds operator business IndiaMART contact",        True),
        # Building owner / lease intent — neural (no category)
        ("building owner Noida lease entire property for PG hostel operator contact",      False),
        ("independent house Noida whole building paying guest accommodation lease owner",  False),
        ("property owner Noida large house rent for hostel PG company contact number",    False),
        ("bungalow Noida lease for PG coliving hostel owner contact phone",                False),
        # Scale signals
        ("PG hostel operator Noida 50 100 beds capacity contact owner phone number",      False),
        ("hostel management company Noida multiple properties operator contact",           False),
        ("PG franchise Noida building operator coliving business partner contact",         False),
        # Managed operators
        ("Stanza Living Noida property acquisition business development contact",          True),
        ("Zolo stays Noida new building lease partner contact",                            True),
        ("managed coliving operator Noida new property expansion contact",                 True),
        # Expressway belt
        ("PG hostel operator Noida expressway Sector 100 120 128 134 142 owner contact", False),
        ("paying guest accommodation operator Noida Sector 119 120 121 122 125 contact", False),
    ],
    "delhi": [
        ("PG hostel operator Delhi IndiaMART business owner phone contact",                True),
        ("managed PG hostel Delhi North South East operator 50 beds contact",              True),
        ("building owner Delhi rent entire building for PG hostel company contact",        False),
        ("paying guest accommodation business Delhi Rohini Dwarka operator contact",       True),
        ("PG franchise Delhi operator coliving expansion Noida contact",                   False),
        ("large hostel Delhi 50 100 beds business operator owner phone",                   False),
        ("property owner Delhi want to lease entire building PG hostel contact",           False),
    ],
    "ncr": [
        ("PG hostel operator Gurugram Gurgaon IndiaMART business owner contact",           True),
        ("managed PG Gurgaon coliving operator expansion Noida contact",                   True),
        ("PG hostel operator Faridabad Ghaziabad business owner phone",                    True),
        ("PG hostel operator Greater Noida Knowledge Park operator contact",               False),
        ("building owner Gurugram Ghaziabad lease building for PG hostel company",         False),
        ("large hostel Gurugram 50 100 beds operator business contact",                    False),
    ],
    "india": [
        ("managed coliving company India B2B property acquisition new city expansion contact", True),
        ("hostel franchise India operator looking for new buildings lease contact",            False),
        ("PG hostel operator India 100 200 beds multiple cities expansion contact",            False),
        ("student accommodation company India new property partnership BD contact",            True),
        ("coliving company India franchise partner building owner expansion",                  True),
    ],
}

SEARCH_COLS = [
    "source", "geo_tier", "operator_scale",
    "name", "address", "phone_raw", "email_raw",
    "listing_url", "posted_on", "exa_score", "domain",
]


def _domain_of(url: str) -> str:
    """Extract the host portion of a URL (best-effort, no urllib needed)."""
    parts = url.split("/", 3)
    return parts[2] if len(parts) >= 3 else ""


def run_search(
    exa_client: Any,
    tiers: list[str],
    max_queries: int | None = None,
) -> list[dict]:
    """
    Operator-intent keyword search across business directories.
    Uses category="company" on IndiaMART/Sulekha/JD queries.
    """
    all_results: list[dict] = []
    seen_urls: set[str] = set()

    query_plan: list[tuple[str, str, bool]] = [
        (query, tier, use_cat)
        for tier in tiers
        for query, use_cat in SEARCH_QUERIES.get(tier, [])
    ]
    if max_queries is not None:
        query_plan = query_plan[: max(0, max_queries)]

    log.info("Search mode: %d queries across tiers %s", len(query_plan), tiers)

    for query, geo, use_cat in query_plan:
        log.info("[search/%s]%s %s", geo, " [company]" if use_cat else "", query[:80])
        # company category uses a dedicated index — Exa rejects startPublishedDate with it.
        kwargs: dict[str, Any] = {
            "type": "auto",
            "num_results": 10,
            "include_domains": OPERATOR_DOMAINS,
            "text": {"max_characters": 3000},
            "highlights": {"num_sentences": 3, "highlights_per_url": 2},
        }
        if use_cat:
            kwargs["category"] = "company"
        else:
            kwargs["start_published_date"] = "2022-01-01"

        try:
            resp = exa_client.search_and_contents(query, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Search failed: %s", exc)
            if _is_credit_error(exc):
                log.error("Credits exhausted")
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        added = 0
        for result in resp.results:
            url = result.url or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            full = hl_text(result)
            title = result.title or ""
            if not is_valid(title, url, full):
                continue

            phs = phones(full)
            ems = emails(full)
            if not phs and not ems:
                continue

            scale = operator_scale(full)
            addr = address(full)
            posted = str(getattr(result, "published_date", "") or "")
            score = getattr(result, "score", None)

            for phone in phs or [""]:
                all_results.append({
                    "source":         "exa_search",
                    "geo_tier":       geo,
                    "operator_scale": scale,
                    "name":           title,
                    "address":        addr,
                    "phone_raw":      phone,
                    "email_raw":      ems[0] if ems else "",
                    "listing_url":    url,
                    "posted_on":      posted,
                    "exa_score":      score,
                    "domain":         _domain_of(url),
                })
                added += 1

        log.info("  → %d hits | %d leads", len(resp.results), added)
        time.sleep(REQUEST_DELAY_SECONDS)

    confirmed = sum(1 for r in all_results if r["operator_scale"] == "confirmed")
    log.info("Search done: %d leads (%d confirmed)", len(all_results), confirmed)
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

VALID_MODES: frozenset[str] = frozenset({"similar", "agent", "search", "all"})


def run(
    modes: list[str],
    tiers: Sequence[str] | None = None,
    agent_max_batches: int = 4,
    similar_per_seed: int = 20,
    smoke: bool = False,
) -> list[dict]:
    """Top-level orchestrator. Returns the combined list of leads from all modes.

    When ``smoke`` is True, caps Exa usage: one seed and up to three similar URLs
    per seed, one agent batch, and one search query — for a cheap end-to-end check.
    """
    if EXA_API_KEY in ("YOUR_EXA_API_KEY_HERE", ""):
        log.error("Set EXA_API_KEY in `.env` — https://dashboard.exa.ai/api-keys")
        return []
    if not EXA_SDK_AVAILABLE:
        log.error("Install exa-py: pip install exa-py")
        return []

    mode_set = {m.lower() for m in modes}
    unknown = mode_set - VALID_MODES
    if unknown:
        log.warning("Ignoring unknown modes: %s", ", ".join(sorted(unknown)))
        mode_set -= unknown
    if not mode_set:
        log.error("No valid modes selected. Choose from: %s", sorted(VALID_MODES))
        return []

    tier_list = resolve_tiers(tiers)
    log.info("=== Exa PG Operator Deep-Dive ===")
    log.info("Modes: %s | Geo cone: %s", sorted(mode_set), tier_list)
    if smoke:
        log.info("SMOKE TEST: minimal API volume (caps on similar / agent / search)")

    similar_n = min(similar_per_seed, 3) if smoke else similar_per_seed
    similar_max_seeds = 1 if smoke else None
    agent_batches_eff = 1 if smoke else agent_max_batches
    search_max_q = 1 if smoke else None
    out_suffix = "_smoke" if smoke else ""

    Path(RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)
    exa_client = Exa(EXA_API_KEY)  # type: ignore[misc]
    all_results: list[dict] = []
    run_all = "all" in mode_set

    if run_all or "similar" in mode_set:
        log.info("── Mode 1: findSimilar (gmaps operator seeds) ──")
        results = run_similar(
            exa_client,
            tier_list,
            results_per_seed=similar_n,
            max_seeds=similar_max_seeds,
        )
        all_results.extend(results)
        save(results, f"exa_similar{out_suffix}", SIMILAR_COLS)

    if run_all or "agent" in mode_set:
        log.info("── Mode 2: Exa Agent (deep structured extraction) ──")
        results = run_agent(exa_client, tier_list, max_batches=agent_batches_eff)
        all_results.extend(results)
        save(results, f"exa_agent{out_suffix}", AGENT_COLS)

    if run_all or "search" in mode_set:
        log.info("── Mode 3: Keyword search (operator-intent, business dirs) ──")
        results = run_search(exa_client, tier_list, max_queries=search_max_q)
        all_results.extend(results)
        save(results, f"exa_search{out_suffix}", SEARCH_COLS)

    confirmed = sum(1 for r in all_results if r.get("operator_scale") == "confirmed")
    possible = sum(1 for r in all_results if r.get("operator_scale") == "possible")
    log.info(
        "=== DONE: %d total leads | %d confirmed | %d possible ===",
        len(all_results), confirmed, possible,
    )
    return all_results


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description="Exa deep-dive: PG building operators and owners only",
    )
    parser.add_argument(
        "--mode", action="append", dest="modes", default=[],
        choices=sorted(VALID_MODES),
        help="Modes to run (repeatable): similar / agent / search / all",
    )
    parser.add_argument(
        "--tier", action="append", dest="tiers",
        choices=[*GEO_CONE, "all"],
        help="Geo cone tier(s) (repeatable): noida / delhi / ncr / india / all",
    )
    parser.add_argument(
        "--agent-batches", type=int, default=4,
        help=(
            "Max sector batches for agent mode (default 4 = Sectors 57/58/62/63 + IT belt). "
            "Set 10 for full Noida. Set 14 for Noida+Delhi. Set 18 for full NCR."
        ),
    )
    parser.add_argument(
        "--similar-per-seed", type=int, default=20,
        help="Results per seed URL in findSimilar mode (default 20)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Minimal live run: 1 seed × ≤3 similar URLs, 1 agent batch, 1 search query. "
            "Writes data/raw/exa_*_smoke.json (does not overwrite full exa_similar.csv). "
            "Uses real Exa API; use before a full extract."
        ),
    )
    return parser.parse_args(None if argv is None else list(argv))


if __name__ == "__main__":
    args = _parse_args()
    selected_modes = args.modes or ["similar"]   # default: findSimilar only
    run(
        modes=selected_modes,
        tiers=args.tiers,
        agent_max_batches=args.agent_batches,
        similar_per_seed=args.similar_per_seed,
        smoke=args.smoke,
    )