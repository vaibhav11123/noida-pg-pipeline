"""
phase1_extract/google_places.py
================================
Pull PG / hostel / coliving POIs from Google Places.

Primary: **Places API (New)** — `places:searchText` (needs "Places API (New)" enabled).
Fallback: **Legacy** Text Search + Place Details when New returns 403/5xx or
`GOOGLE_PLACES_USE_LEGACY_FALLBACK` is True and New fails (needs "Places API" legacy).

Outputs:  data/raw/google_places.json
"""

import json
import time
import logging
from pathlib import Path

import httpx

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    GOOGLE_LEGACY_DETAIL_CAP,
    GOOGLE_PLACES_USE_LEGACY_FALLBACK,
    GOOGLE_PLACES_API_KEY,
    GOOGLE_SEARCH_QUERIES,
    REQUEST_DELAY_SECONDS,
    RAW_DATA_DIR,
)

# ── Places API (New) ────────────────────────────────────────────────────────
PLACES_TEXTSEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.location",
        "places.types",
        "places.businessStatus",
        "places.regularOpeningHours",
        "places.primaryType",
        "places.shortFormattedAddress",
    ]
)

LOCATION_BIAS = {
    "rectangle": {
        "low": {"latitude": 28.595, "longitude": 77.355},
        "high": {"latitude": 28.645, "longitude": 77.400},
    }
}

# ── Legacy Places API ─────────────────────────────────────────────────────────
LEGACY_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
LEGACY_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
LEGACY_DETAIL_FIELDS = "place_id,name,formatted_address,formatted_phone_number,international_phone_number,geometry,rating,user_ratings_total,business_status,website"

# ── logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [PLACES] %(message)s", force=True)
log = logging.getLogger(__name__)


def _place_record_new(place: dict) -> dict:
    pid = place.get("id", "")
    nat = place.get("nationalPhoneNumber", "") or ""
    intl = place.get("internationalPhoneNumber", "") or ""
    return {
        "source": "google_places",
        "place_id": pid,
        "property_id": pid,
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "phone_raw": nat or intl,
        "phone_intl": intl,
        "website": place.get("websiteUri", ""),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "lat": place.get("location", {}).get("latitude"),
        "lng": place.get("location", {}).get("longitude"),
        "types": place.get("types", []),
        "business_status": place.get("businessStatus", ""),
    }


def fetch_query_new(client: httpx.Client, query: str) -> tuple[list[dict], bool]:
    """
    Run Places API (New) text search with pagination.
    Returns (flattened records, ok_without_error).
    """
    results: list[dict] = []
    page_token = None
    page_num = 0
    had_http_error = False

    while True:
        page_num += 1
        payload: dict = {
            "textQuery": query,
            "locationBias": LOCATION_BIAS,
            "languageCode": "en",
            "maxResultCount": 20,
        }
        if page_token:
            payload["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        }

        try:
            resp = client.post(PLACES_TEXTSEARCH_URL, json=payload, headers=headers, timeout=20)
            if resp.status_code >= 400:
                log.error(
                    "HTTP %s for query '%s': %s",
                    resp.status_code,
                    query,
                    resp.text[:400],
                )
                had_http_error = True
                break
            data = resp.json()
        except Exception as exc:
            log.error("Request failed for query '%s': %s", query, exc)
            had_http_error = True
            break

        places = data.get("places", [])
        log.info("  (New API) query='%s' page=%d → %d results", query, page_num, len(places))
        for place in places:
            results.append(_place_record_new(place))

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(max(REQUEST_DELAY_SECONDS, 2.0))

    return results, not had_http_error and bool(results)


def _legacy_details(client: httpx.Client, place_id: str) -> dict:
    params = {
        "place_id": place_id,
        "fields": LEGACY_DETAIL_FIELDS,
        "key": GOOGLE_PLACES_API_KEY,
    }
    resp = client.get(LEGACY_DETAILS_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result") or {}


def fetch_query_legacy(client: httpx.Client, query: str, detail_budget: list[int]) -> list[dict]:
    """Legacy Text Search + capped Place Details (phones)."""
    out: list[dict] = []
    page_token = None
    page_num = 0

    while True:
        page_num += 1
        params = {
            "query": query,
            "key": GOOGLE_PLACES_API_KEY,
            "region": "in",
        }
        if page_token:
            params["pagetoken"] = page_token

        resp = client.get(LEGACY_TEXTSEARCH_URL, params=params, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        status = body.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            log.error("Legacy Text Search status=%s msg=%s", status, body.get("error_message", ""))
            break

        batch = body.get("results", [])
        log.info("  (Legacy) query='%s' page=%d → %d results", query, page_num, len(batch))

        for r in batch:
            pid = r.get("place_id", "")
            rec = {
                "source": "google_places",
                "place_id": pid,
                "property_id": pid,
                "name": r.get("name", ""),
                "address": r.get("formatted_address", ""),
                "phone_raw": "",
                "phone_intl": "",
                "website": "",
                "rating": r.get("rating"),
                "review_count": r.get("user_ratings_total"),
                "lat": (r.get("geometry") or {}).get("location", {}).get("lat"),
                "lng": (r.get("geometry") or {}).get("location", {}).get("lng"),
                "types": r.get("types", []),
                "business_status": r.get("business_status", ""),
            }
            if pid and detail_budget[0] < GOOGLE_LEGACY_DETAIL_CAP:
                try:
                    det = _legacy_details(client, pid)
                    detail_budget[0] += 1
                    rec["phone_raw"] = det.get("formatted_phone_number", "") or ""
                    intl = det.get("international_phone_number", "")
                    if intl:
                        rec["phone_intl"] = intl
                    rec["website"] = det.get("website", "") or ""
                    if det.get("rating") is not None:
                        rec["rating"] = det.get("rating")
                    if det.get("user_ratings_total") is not None:
                        rec["review_count"] = det.get("user_ratings_total")
                    time.sleep(0.12)
                except Exception as exc:
                    log.debug("Place details failed for %s: %s", pid, exc)
            elif pid:
                log.debug("Legacy detail cap reached — skipping phone for %s", pid)
            out.append(rec)

        page_token = body.get("next_page_token")
        if not page_token:
            break
        time.sleep(2.0)

    return out


def run() -> list[dict]:
    if not GOOGLE_PLACES_API_KEY or GOOGLE_PLACES_API_KEY == "YOUR_GOOGLE_PLACES_API_KEY_HERE":
        log.error("Google Places API key not set. Set GOOGLE_PLACES_API_KEY in `.env` (see SETUP.md → Step 1).")
        return []

    Path(RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)

    seen_ids: set[str] = set()
    all_places: list[dict] = []
    use_legacy_only = False
    detail_budget = [0]

    with httpx.Client() as client:
        for query in GOOGLE_SEARCH_QUERIES:
            log.info("Querying: %s", query)
            batch: list[dict] = []

            if not use_legacy_only:
                new_batch, ok = fetch_query_new(client, query)
                if ok:
                    batch = new_batch
                elif GOOGLE_PLACES_USE_LEGACY_FALLBACK:
                    log.warning("New Places API yielded no data — trying legacy Text Search …")
                    try:
                        batch = fetch_query_legacy(client, query, detail_budget)
                        if batch:
                            use_legacy_only = True
                    except Exception as exc:
                        log.error("Legacy fallback failed: %s", exc)
                else:
                    batch = new_batch
            else:
                try:
                    batch = fetch_query_legacy(client, query, detail_budget)
                except Exception as exc:
                    log.error("Legacy search failed for '%s': %s", query, exc)

            for rec in batch:
                pid = rec.get("place_id") or rec.get("property_id") or ""
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_places.append(rec)

            time.sleep(REQUEST_DELAY_SECONDS)

    out_path = Path(RAW_DATA_DIR) / "google_places.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_places, f, ensure_ascii=False, indent=2)

    log.info("✓ Saved %d unique places → %s", len(all_places), out_path)
    return all_places


if __name__ == "__main__":
    run()
