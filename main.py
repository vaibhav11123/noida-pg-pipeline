"""
main.py — Noida PG Operator Lead-Generation Pipeline
======================================================
Master orchestrator. Runs all 4 phases in sequence.

Usage:
    python main.py                          # full pipeline
    python main.py --phase 1               # extraction only
    python main.py --phase 2               # aggregation only
    python main.py --phase 3               # validation only
    python main.py --phase 4               # export only
    python main.py --skip-portals          # skip Playwright scrapers (faster)
    python main.py --skip-gosom           # skip gosom Maps CSV ingest
    python main.py --gosom-only           # only ingest gosom CSV → gosom_gmaps.json (skip other Phase 1 sources)
    python main.py --truecaller TOKEN      # pass Truecaller auth token

Before running:
    1. Copy `.env.example` to `.env` and add API keys / cookies (see SETUP.md)
    2. pip install -r requirements.txt
    3. playwright install chromium

Expected runtime (full pipeline):
    ~90–120 minutes depending on proxy speed and API quotas.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAIN] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║   Noida PG Operator Intelligence Pipeline — 2025/2026           ║
║   Target: Sectors 57 · 58 · 62 · 63                            ║
║   Channels: Google Maps (gosom) · JustDial · 99acres            ║
║             MagicBricks · Housing.com                           ║
╚══════════════════════════════════════════════════════════════════╝
"""


def run_phase1(skip_portals: bool = False, skip_gosom: bool = False, gosom_only: bool = False):
    log.info("━━━ PHASE 1: EXTRACTION ━━━")

    if gosom_only:
        log.info("Gosom-only mode — skipping Exa, JustDial, Google Places, portal scrapers")
        if skip_gosom:
            log.warning("Both --gosom-only and --skip-gosom set — nothing to run in Phase 1")
        else:
            try:
                from config import GOSOM_GMAPS_ENABLED
                if GOSOM_GMAPS_ENABLED:
                    log.info("[1/1] Gosom Google Maps scraper CSV ingest …")
                    from phase1_extract.gosom_gmaps import run as gosom_run
                    gosom_run()
                else:
                    log.info("  GOSOM_GMAPS_ENABLED=False — skipping")
            except Exception as exc:
                log.warning("Gosom ingest skipped: %s", exc)
        log.info("✓ Phase 1 complete")
        return

    # Exa AI — neural search across all platforms at once (fastest, best bang-per-dollar)
    log.info("[1/5] Exa AI neural search …")
    try:
        from config import EXA_API_KEY, EXA_USE_WEBSETS
        if EXA_API_KEY and EXA_API_KEY != "YOUR_EXA_API_KEY_HERE":
            from phase1_extract.exa_extractor import run as exa_run
            exa_run(use_websets=EXA_USE_WEBSETS)
        else:
            log.info("  Exa API key not set — skipping. Add EXA_API_KEY to `.env`")
    except Exception as exc:
        log.warning("Exa extractor skipped: %s", exc)

    # JustDial → Google Places (Places last: its logging.basicConfig should win)
    log.info("[2/5] JustDial mobile scraper …")
    from phase1_extract.justdial_scraper import run as jd_run
    jd_run()

    log.info("[3/5] Google Places API …")
    from phase1_extract.google_places import run as gp_run
    gp_run()

    if not skip_gosom:
        log.info("[4/5] Gosom Google Maps scraper CSV ingest …")
        try:
            from config import GOSOM_GMAPS_ENABLED
            if GOSOM_GMAPS_ENABLED:
                from phase1_extract.gosom_gmaps import run as gosom_run
                gosom_run()
            else:
                log.info("  GOSOM_GMAPS_ENABLED=False — skipping")
        except Exception as exc:
            log.warning("Gosom ingest skipped: %s", exc)
    else:
        log.info("[4/5] Skipping Gosom ingest (--skip-gosom)")

    # Playwright portal scrapers (optional — slower, higher bot risk)
    if not skip_portals:
        log.info("[5/5] Portal Playwright scrapers (99acres / MagicBricks / Housing) …")
        from phase1_extract.portal_playwright import run as pw_run
        pw_run()
    else:
        log.info("[5/5] Skipping Playwright portal scrapers (--skip-portals flag)")

    log.info("✓ Phase 1 complete")


def run_phase2():
    log.info("━━━ PHASE 2: AGGREGATION & DEDUPLICATION ━━━")
    from phase2_aggregate.aggregator import run as agg_run
    df = agg_run()
    log.info("✓ Phase 2 complete — %d unique records", len(df))
    return df


def run_phase3(truecaller_token: str = ""):
    log.info("━━━ PHASE 3: VALIDATION ━━━")
    from phase3_validate.validator import run as val_run
    validated, rejected = val_run(truecaller_token=truecaller_token)
    log.info("✓ Phase 3 complete — %d verified, %d rejected",
             len(validated), len(rejected))
    return validated, rejected


def run_phase4():
    log.info("━━━ PHASE 4: EXPORT & OUTREACH ━━━")
    from phase4_outreach.exporter import run as exp_run
    df = exp_run()
    log.info("✓ Phase 4 complete — %d final contacts", len(df))
    return df


def main():
    print(BANNER)

    parser = argparse.ArgumentParser(description="Noida PG Operator Pipeline")
    parser.add_argument("--phase",          type=int, default=0,
                        help="Run a single phase (1–4). Default: all phases.")
    parser.add_argument("--skip-portals",   action="store_true",
                        help="Skip Playwright scrapers (faster, no 99acres/MB/Housing)")
    parser.add_argument("--skip-gosom",     action="store_true",
                        help="Skip gosom/google-maps-scraper CSV ingest (gosom_gmaps.json)")
    parser.add_argument("--gosom-only",     action="store_true",
                        help="Phase 1: only gosom CSV ingest (skip Exa, JustDial, Places, portals)")
    parser.add_argument("--truecaller",     type=str, default="",
                        help="Truecaller auth token for name enrichment")
    args = parser.parse_args()

    start = time.time()

    if args.phase == 1:
        run_phase1(args.skip_portals, args.skip_gosom, args.gosom_only)
    elif args.phase == 2:
        run_phase2()
    elif args.phase == 3:
        run_phase3(args.truecaller)
    elif args.phase == 4:
        run_phase4()
    else:
        # Full pipeline
        run_phase1(args.skip_portals, args.skip_gosom, args.gosom_only)
        run_phase2()
        run_phase3(args.truecaller)
        run_phase4()

    elapsed = time.time() - start
    log.info("Pipeline finished in %.1f minutes", elapsed / 60)
    log.info("Final contacts → data/final_pg_contacts.csv")
    log.info("WhatsApp batch → data/whatsapp_outreach_batch.csv")


if __name__ == "__main__":
    main()
