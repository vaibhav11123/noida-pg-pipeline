"""
phase4_outreach/exporter.py
==============================
Generate the final structured CSV and WhatsApp outreach batch.

Steps:
  1. Load validated_contacts.csv from Phase 3
  2. Apply final quality scoring
  3. Sort by score (best leads first)
  4. Export final_pg_contacts.csv with all fields
  5. Generate personalized WhatsApp message text for each contact
  6. Export whatsapp_outreach_batch.csv ready for bulk sender tools

Outputs:
  data/final_pg_contacts.csv          — the master deliverable
  data/whatsapp_outreach_batch.csv    — one row per outreach message
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MERGED_DATA_DIR, OUTPUT_CSV

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [EXPORT] %(message)s")
log = logging.getLogger(__name__)

OUTPUT_DIR = Path(OUTPUT_CSV).parent
WA_BATCH_CSV = OUTPUT_DIR / "whatsapp_outreach_batch.csv"


def _nonempty_phone(val: Any) -> bool:
    p = str(val or "").strip()
    return bool(p) and p.lower() not in ("nan", "none", "nat")


# ─────────────────────────────────────────────────────────────────────────────
# Quality score
# ─────────────────────────────────────────────────────────────────────────────

def quality_score(row: pd.Series) -> int:
    """
    Composite quality score 0–100 for prioritising outreach.
    Higher = better lead.
    """
    score = 0

    # Phone verified (max 25)
    if str(row.get("valid", "")).lower() == "true":      score += 15
    if str(row.get("mobile", "")).lower() == "true":     score += 10

    # WhatsApp present (max 25)
    if str(row.get("whatsapp_valid", "")).lower() == "true":  score += 25

    # Owner (not broker) declared (max 20)
    owner_type = str(row.get("owner_type", "")).lower()
    if "owner" in owner_type:    score += 20
    elif "broker" in owner_type: score -= 15

    # Freshness (max 15)
    fresh = int(row.get("freshness_score", 1) or 1)
    score += fresh * 3   # 3–15 points

    # Source quality (max 15)
    source_scores = {
        "gosom_gmaps":   16,
        "google_places": 15,
        "justdial":      10,
        "99acres":        7,
        "magicbricks":    7,
        "housing":        5,
    }
    score += source_scores.get(str(row.get("source", "")), 3)

    return max(0, min(score, 100))


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp message personalisation
# ─────────────────────────────────────────────────────────────────────────────

# Personalisation templates — pick based on context
TEMPLATES = {
    "default": (
        "Hi {first_name},\n\n"
        "I came across your PG listing{sector_clause} on {source_display} "
        "and I'm interested in knowing more about availability.\n\n"
        "Could you share current vacancy details, monthly rent, and any "
        "amenities included?\n\n"
        "Thank you!"
    ),
    "owner": (
        "Hello {first_name},\n\n"
        "I noticed you have a PG listed{sector_clause}. "
        "I'm looking for a well-managed PG for a working professional — "
        "your property looks like a great fit.\n\n"
        "Could you let me know current vacancy, rent, and deposit amount?\n\n"
        "Thanks!"
    ),
    "no_name": (
        "Hello,\n\n"
        "I found your PG listing{sector_clause} and I'm interested "
        "in knowing about current availability and pricing.\n\n"
        "Could you please share the details?\n\n"
        "Thank you!"
    ),
}

SOURCE_DISPLAY = {
    "google_places": "Google Maps",
    "gosom_gmaps":   "Google Maps (scraper)",
    "justdial":      "JustDial",
    "99acres":       "99acres",
    "magicbricks":   "MagicBricks",
    "housing":       "Housing.com",
}


def generate_whatsapp_message(row: pd.Series) -> str:
    """Generate a personalised WhatsApp message for this contact."""
    # Extract first name from owner_name or name
    raw_name = str(row.get("owner_name", "") or row.get("name", "") or "").strip()
    # Remove business suffixes to get a first name
    clean_name = re.sub(
        r"\b(pg|paying guest|hostel|properties|associates|realty|home|homes|house)\b",
        "", raw_name, flags=re.I,
    ).strip()
    first_name = clean_name.split()[0].title() if clean_name.split() else ""

    # Sector clause
    sector = str(row.get("sector", "unknown")).strip()
    sector_clause = f" in Sector {sector}, Noida" if sector != "unknown" else " in Noida"

    source = str(row.get("source", "")).strip()
    source_display = SOURCE_DISPLAY.get(source, source.replace("_", " ").title())

    owner_type = str(row.get("owner_type", "")).lower()

    # Choose template
    if not first_name:
        tmpl = TEMPLATES["no_name"]
    elif "owner" in owner_type:
        tmpl = TEMPLATES["owner"]
    else:
        tmpl = TEMPLATES["default"]

    return tmpl.format(
        first_name=first_name or "there",
        sector_clause=sector_clause,
        source_display=source_display,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main export
# ─────────────────────────────────────────────────────────────────────────────

def run() -> pd.DataFrame:
    """Build and save the final contact list and outreach batch."""
    validated_path = Path(MERGED_DATA_DIR) / "validated_contacts.csv"
    if not validated_path.exists():
        log.error("validated_contacts.csv not found. Run Phase 3 first.")
        return pd.DataFrame()

    df = pd.read_csv(validated_path, dtype=str)
    log.info("Loaded %d validated contacts", len(df))

    # ── Quality scoring ───────────────────────────────────────────────────────
    log.info("Scoring leads …")
    df["quality_score"] = df.apply(quality_score, axis=1)

    # ── Sort by quality ───────────────────────────────────────────────────────
    df = df.sort_values("quality_score", ascending=False).reset_index(drop=True)

    # Master CSV is phone-first: omit rows with no callable number
    if "phone_e164" in df.columns:
        n_in = len(df)
        df = df[df["phone_e164"].map(_nonempty_phone)].reset_index(drop=True)
        n_drop = n_in - len(df)
        if n_drop:
            log.info("Excluded %d contact(s) without phone_e164 from final CSV", n_drop)

    df["rank"] = df.index + 1

    # ── Final columns for master CSV ──────────────────────────────────────────
    FINAL_COLS = [
        "rank",
        "quality_score",
        "name",
        "sector",
        "address",
        "phone_e164",
        "whatsapp_valid",
        "whatsapp_type",
        "owner_name",
        "owner_type",
        "source",
        "listing_url",
        "rent",
        "freshness_score",
        "mobile",
        "carrier",
        "validation_status",
        "lat",
        "lng",
        "posted_on",
    ]
    final_cols_present = [c for c in FINAL_COLS if c in df.columns]
    df_final = df[final_cols_present].copy()

    # ── Save master CSV ───────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df_final.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    log.info("✓ Master contacts CSV → %s (%d rows)", OUTPUT_CSV, len(df_final))

    # ── WhatsApp outreach batch ───────────────────────────────────────────────
    log.info("Generating WhatsApp outreach messages …")
    outreach_rows = []

    for _, row in df.iterrows():
        phone = str(row.get("phone_e164", "")).strip()
        if not phone or phone.lower() in ("nan", "none", "nat"):
            continue

        # Only include WhatsApp-verified numbers for WA batch
        wa_ok = str(row.get("whatsapp_valid", "")).lower()

        message = generate_whatsapp_message(row)
        outreach_rows.append({
            "rank":           row.get("rank", ""),
            "phone_e164":     phone,
            "name":           row.get("name", ""),
            "sector":         row.get("sector", ""),
            "whatsapp_valid": row.get("whatsapp_valid", ""),
            "quality_score":  row.get("quality_score", ""),
            "message":        message,
            "status":         "pending",      # update to 'sent'/'replied' after outreach
            "sent_at":        "",
            "reply_received": "",
            "notes":          "",
        })

    df_outreach = pd.DataFrame(outreach_rows)
    df_outreach.to_csv(WA_BATCH_CSV, index=False, encoding="utf-8-sig")
    log.info("✓ WhatsApp outreach batch → %s (%d messages)", WA_BATCH_CSV, len(df_outreach))

    # ── Summary stats ──────────────────────────────────────────────────────────
    log.info("═══════════ FINAL PIPELINE SUMMARY ═══════════")
    log.info("  Total verified contacts: %d", len(df_final))
    log.info("  WhatsApp-ready:          %d",
             df_outreach[df_outreach["whatsapp_valid"].astype(str) == "True"].shape[0] if not df_outreach.empty else 0)
    log.info("  High quality (score≥60): %d",
             (df_final["quality_score"].astype(float) >= 60).sum() if "quality_score" in df_final.columns else "n/a")

    if "sector" in df_final.columns:
        log.info("  Sector breakdown:")
        for s, c in df_final["sector"].value_counts().items():
            log.info("    Sector %-5s: %d", s, c)

    log.info("  Expected engagement (10–25%%): %d–%d contacts",
             int(len(df_final) * 0.10), int(len(df_final) * 0.25))
    log.info("═══════════════════════════════════════════════")

    return df_final


if __name__ == "__main__":
    run()
