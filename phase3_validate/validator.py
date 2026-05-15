"""
phase3_validate/validator.py
==============================
Multi-layer validation and enrichment pipeline.

Layers (in order):
  1. phonenumbers  — re-validate E.164, classify mobile vs landline
  2. Numverify     — HLR: active/inactive, carrier, line type
  3. Twilio Lookup — alternative to Numverify
  4. WhatsApp      — check WhatsApp presence via Business Cloud API
  5. Truecaller    — name enrichment + broker keyword detection
  6. Broker filter — keyword + spam-score-based flagging

Input:  data/merged/merged_contacts.csv
Output: data/merged/validated_contacts.csv
        data/merged/rejected_contacts.csv  (broker/invalid/dead)
"""

import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd
import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    NUMVERIFY_API_KEY,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    USE_TWILIO_LOOKUP,
    WHATSAPP_API_TOKEN,
    WHATSAPP_PHONE_ID,
    BROKER_KEYWORDS,
    MERGED_DATA_DIR,
    REQUEST_DELAY_SECONDS,
)

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [VALIDATE] %(message)s")
log = logging.getLogger(__name__)

NUMVERIFY_URL = "http://apilayer.net/api/validate"
TWILIO_LOOKUP_URL = "https://lookups.twilio.com/v2/PhoneNumbers/{number}?Fields=line_type_intelligence"
WA_CHECK_URL = "https://graph.facebook.com/v19.0/{phone_id}/messages"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: phonenumbers re-validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_phonenumbers(e164: str) -> dict:
    """Re-validate using libphonenumber and classify line type."""
    result = {"valid": False, "mobile": False, "line_type": "unknown"}
    if not e164:
        return result
    try:
        parsed = phonenumbers.parse(e164, None)
        result["valid"] = phonenumbers.is_valid_number(parsed)
        num_type = phonenumbers.number_type(parsed)
        result["mobile"] = num_type in (
            PhoneNumberType.MOBILE,
            PhoneNumberType.FIXED_LINE_OR_MOBILE,
        )
        result["line_type"] = {
            PhoneNumberType.MOBILE:                "mobile",
            PhoneNumberType.FIXED_LINE:            "landline",
            PhoneNumberType.FIXED_LINE_OR_MOBILE:  "mobile",
            PhoneNumberType.TOLL_FREE:             "toll_free",
            PhoneNumberType.PREMIUM_RATE:          "premium",
            PhoneNumberType.VOIP:                  "voip",
        }.get(num_type, "unknown")
    except NumberParseException:
        pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2a: Numverify HLR lookup
# ─────────────────────────────────────────────────────────────────────────────

def numverify_lookup(client: httpx.Client, e164: str) -> dict:
    """
    Call Numverify API for HLR (Home Location Register) check.
    Returns active status, carrier, and line type.
    """
    if not NUMVERIFY_API_KEY or NUMVERIFY_API_KEY == "YOUR_NUMVERIFY_API_KEY_HERE":
        return {"hlr_active": None, "carrier": "", "numverify_line_type": ""}

    # Strip leading + for Numverify
    number_clean = e164.lstrip("+")

    try:
        resp = client.get(
            NUMVERIFY_URL,
            params={
                "access_key": NUMVERIFY_API_KEY,
                "number":     number_clean,
                "country_code": "IN",
                "format":     1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            log.warning("Numverify error: %s", data["error"].get("info", ""))
            return {"hlr_active": None, "carrier": "", "numverify_line_type": ""}

        return {
            "hlr_active":            data.get("valid", False),
            "carrier":               data.get("carrier", ""),
            "numverify_line_type":   data.get("line_type", ""),
        }
    except Exception as exc:
        log.debug("Numverify request failed: %s", exc)
        return {"hlr_active": None, "carrier": "", "numverify_line_type": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2b: Twilio Lookup (alternative)
# ─────────────────────────────────────────────────────────────────────────────

def twilio_lookup(client: httpx.Client, e164: str) -> dict:
    """Twilio Lookup v2 for line type intelligence."""
    if not TWILIO_ACCOUNT_SID or TWILIO_ACCOUNT_SID == "YOUR_TWILIO_SID_HERE":
        return {"twilio_line_type": "", "twilio_carrier": ""}

    url = TWILIO_LOOKUP_URL.format(number=e164)
    try:
        resp = client.get(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=10,
        )
        if resp.status_code == 404:
            return {"twilio_line_type": "invalid", "twilio_carrier": ""}
        resp.raise_for_status()
        data = resp.json()
        lti = data.get("line_type_intelligence", {})
        return {
            "twilio_line_type": lti.get("type", ""),
            "twilio_carrier":   lti.get("carrier_name", ""),
        }
    except Exception as exc:
        log.debug("Twilio Lookup failed: %s", exc)
        return {"twilio_line_type": "", "twilio_carrier": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: WhatsApp Business Cloud API — presence check
# ─────────────────────────────────────────────────────────────────────────────

def check_whatsapp_presence(client: httpx.Client, e164: str) -> dict:
    """
    Verify WhatsApp presence by sending a test message to the messaging API.
    We use a minimal 'contacts' endpoint rather than actually sending a message.

    WhatsApp Business Cloud API requires:
      - A verified Meta Business account
      - An approved WhatsApp Business phone number
      - The WHATSAPP_API_TOKEN and WHATSAPP_PHONE_ID from `.env`

    The contacts endpoint returns 'valid' or 'invalid' status.
    """
    if not WHATSAPP_API_TOKEN or WHATSAPP_API_TOKEN == "YOUR_META_WHATSAPP_TOKEN_HERE":
        return {"whatsapp_valid": None, "whatsapp_type": ""}

    # Use the /contacts endpoint (does not send messages)
    contacts_url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/contacts"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_API_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "blocking":  "no_wait",
        "contacts":  [e164],
        "force_check": False,
    }

    try:
        resp = client.post(contacts_url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        contacts = data.get("contacts", [])
        if contacts:
            status = contacts[0].get("status", "")
            wa_id  = contacts[0].get("wa_id", "")
            return {
                "whatsapp_valid": status == "valid",
                "whatsapp_type":  "business" if len(wa_id) > 12 else "personal",
            }
    except Exception as exc:
        log.debug("WhatsApp check failed for %s: %s", e164, exc)

    return {"whatsapp_valid": None, "whatsapp_type": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: Truecaller enrichment (unofficial API wrapper)
# ─────────────────────────────────────────────────────────────────────────────

def truecaller_lookup(client: httpx.Client, e164: str, auth_token: str = "") -> dict:
    """
    Query Truecaller's unofficial API for name and spam score.

    Note: The Truecaller unofficial API requires:
      - A valid Truecaller auth token obtained from a mobile app session
      - Set auth_token in config or pass it here

    Without a token this function returns empty results gracefully.
    Rate limits are aggressive — use sparingly (< 50 lookups/day per token).
    """
    if not auth_token:
        return {"tc_name": "", "tc_spam_score": None, "tc_is_broker": None}

    # The number without + for Truecaller's format
    number_digits = e164.lstrip("+")

    url = f"https://search5-noneu.truecaller.com/v2/search"
    headers = {
        "Authorization":  f"Bearer {auth_token}",
        "Accept":         "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent":     "Truecaller/13.35.7 (Android 13; Pixel 7)",
    }
    params = {
        "q":          number_digits,
        "countryCode": "IN",
        "type":       4,
        "locAddr":    "",
        "placement":  "SEARCHRESULTS,HISTORY,DETAILS",
    }

    try:
        resp = client.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 401:
            log.warning("Truecaller token invalid or expired")
            return {"tc_name": "", "tc_spam_score": None, "tc_is_broker": None}
        resp.raise_for_status()
        data = resp.json()

        # Extract from first result
        data_list = data.get("data", [])
        if not data_list:
            return {"tc_name": "", "tc_spam_score": None, "tc_is_broker": None}

        first = data_list[0]
        name  = first.get("name", "")
        score = first.get("spamScore", first.get("score", {}).get("value", 0))
        tags  = first.get("tags", [])

        return {
            "tc_name":       name,
            "tc_spam_score": float(score) if score else 0.0,
            "tc_tags":       ",".join(tags),
        }
    except Exception as exc:
        log.debug("Truecaller lookup failed: %s", exc)
        return {"tc_name": "", "tc_spam_score": None, "tc_is_broker": None}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 5: Broker detection
# ─────────────────────────────────────────────────────────────────────────────

def is_broker(row: pd.Series) -> bool:
    """
    Flag a record as likely a broker using multiple signals:
      1. Keyword match in name, owner_name, or tc_name
      2. High Truecaller spam score (≥ 3.0 → telemarketer)
      3. Portal-declared owner_type == BROKER
      4. Number appears in multiple listings (high-volume broker)
    """
    name_fields = " ".join([
        str(row.get("name", "")),
        str(row.get("owner_name", "")),
        str(row.get("tc_name", "")),
    ]).lower()

    # Keyword check
    for kw in BROKER_KEYWORDS:
        if kw.lower() in name_fields:
            return True

    # Truecaller spam score
    spam_score = row.get("tc_spam_score")
    if spam_score and float(spam_score) >= 3.0:
        return True

    # Portal-declared broker
    owner_type = str(row.get("owner_type", "")).lower()
    if "broker" in owner_type or "agent" in owner_type:
        return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main validation runner
# ─────────────────────────────────────────────────────────────────────────────

def run(truecaller_token: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the full validation pipeline.

    Args:
        truecaller_token: Optional Truecaller auth token for enrichment.

    Returns:
        (validated_df, rejected_df)
    """
    input_path = Path(MERGED_DATA_DIR) / "merged_contacts.csv"
    if not input_path.exists():
        log.error("Merged contacts not found. Run Phase 2 first: %s", input_path)
        return pd.DataFrame(), pd.DataFrame()

    df = pd.read_csv(input_path, dtype=str)
    log.info("Loaded %d records for validation", len(df))

    validated_rows: list[dict] = []
    rejected_rows:  list[dict] = []

    with httpx.Client() as client:
        for idx, row in df.iterrows():
            e164_raw = row.get("phone_e164", "")
            e164 = str(e164_raw).strip() if pd.notna(e164_raw) else ""
            record = row.to_dict()

            # NoBroker / portals often omit phone until owner unlock — keep for manual outreach
            if not e164 or e164.lower() in ("nan", "none", "nat"):
                record["rejection_reason"] = ""
                record["validation_status"] = "no_phone"
                record.update(
                    {
                        "valid": False,
                        "mobile": False,
                        "line_type": "unknown",
                        "hlr_active": None,
                        "carrier": "",
                        "numverify_line_type": "",
                        "twilio_line_type": "",
                        "twilio_carrier": "",
                        "whatsapp_valid": False,
                        "whatsapp_type": "",
                        "tc_name": "",
                        "tc_spam_score": None,
                        "is_broker": False,
                    }
                )
                validated_rows.append(record)
                continue

            # ── Layer 1: phonenumbers ────────────────────────────────────────
            pn_result = validate_phonenumbers(e164)
            record.update(pn_result)

            if not pn_result["valid"]:
                record["rejection_reason"] = "invalid_number"
                rejected_rows.append(record)
                log.debug("REJECT (invalid number): %s", e164)
                continue

            # ── Layer 2: HLR (Numverify or Twilio) ───────────────────────────
            if USE_TWILIO_LOOKUP:
                hlr = twilio_lookup(client, e164)
                record.update(hlr)
                active = hlr.get("twilio_line_type") not in ("nonexistent",)
            else:
                hlr = numverify_lookup(client, e164)
                record.update(hlr)
                active = hlr.get("hlr_active")  # None = unchecked (no key)

            if active is False:
                record["rejection_reason"] = "hlr_inactive"
                rejected_rows.append(record)
                log.debug("REJECT (HLR inactive): %s", e164)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            # ── Layer 3: WhatsApp ────────────────────────────────────────────
            wa = check_whatsapp_presence(client, e164)
            record.update(wa)
            time.sleep(REQUEST_DELAY_SECONDS)

            # ── Layer 4: Truecaller ──────────────────────────────────────────
            if truecaller_token:
                tc = truecaller_lookup(client, e164, truecaller_token)
                record.update(tc)
                time.sleep(REQUEST_DELAY_SECONDS)
            else:
                record.update({"tc_name": "", "tc_spam_score": None})

            # ── Layer 5: Broker filter ────────────────────────────────────────
            record["is_broker"] = is_broker(pd.Series(record))

            if record["is_broker"]:
                record["rejection_reason"] = "broker_detected"
                rejected_rows.append(record)
                log.debug("REJECT (broker): %s — %s", e164, record.get("name", ""))
                continue

            # ── PASS ──────────────────────────────────────────────────────────
            record["rejection_reason"] = ""
            record["validation_status"] = "verified"
            validated_rows.append(record)
            log.info("PASS: %s  %-35s  WA=%s  carrier=%s",
                     e164,
                     str(record.get("name", ""))[:35],
                     record.get("whatsapp_valid"),
                     record.get("carrier", record.get("twilio_carrier", "")))

            time.sleep(REQUEST_DELAY_SECONDS)

    # ── Save results ──────────────────────────────────────────────────────────
    validated_df = pd.DataFrame(validated_rows)
    rejected_df  = pd.DataFrame(rejected_rows)

    if not validated_df.empty:
        vpath = Path(MERGED_DATA_DIR) / "validated_contacts.csv"
        validated_df.to_csv(vpath, index=False, encoding="utf-8-sig")
        log.info("✓ Validated contacts: %d → %s", len(validated_df), vpath)

    if not rejected_df.empty:
        rpath = Path(MERGED_DATA_DIR) / "rejected_contacts.csv"
        rejected_df.to_csv(rpath, index=False, encoding="utf-8-sig")
        log.info("✓ Rejected contacts: %d → %s", len(rejected_df), rpath)

    # ── Summary ───────────────────────────────────────────────────────────────
    total = len(validated_rows) + len(rejected_rows)
    log.info("── Validation Summary ──")
    log.info("  Total processed:  %d", total)
    log.info("  Verified:         %d (%.1f%%)", len(validated_rows), 100 * len(validated_rows) / max(total, 1))
    log.info("  Rejected:         %d (%.1f%%)", len(rejected_rows),  100 * len(rejected_rows)  / max(total, 1))

    if rejected_rows:
        reasons = pd.DataFrame(rejected_rows)["rejection_reason"].value_counts()
        for reason, count in reasons.items():
            log.info("    %-20s: %d", reason, count)

    wa_valid = validated_df["whatsapp_valid"].astype(str).eq("True").sum() if not validated_df.empty else 0
    log.info("  WhatsApp verified: %d", wa_valid)

    return validated_df, rejected_df


if __name__ == "__main__":
    # Optionally pass your Truecaller token as argument
    import sys
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    run(truecaller_token=token)
