"""
phase1_extract/nobroker_api.py
================================
NoBroker shared-accommodation / PG-style listings via the public **v3** filter
API (GET). The old POST `/api/v5/property/search/pg` path returns 404 and is
no longer valid.

Docs / behaviour inferred from live traffic:
  GET https://www.nobroker.in/api/v3/multi/property/RENT/filter
      ?city=noida&searchParam=<base64(json_array)>&sharedAccomodation=1&pageNo=…

`searchParam` is Base64(JSON.stringify([{lat, lon, placeName, showMap}, …])).

Outputs: data/raw/nobroker.json
"""

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    NOBROKER_COOKIES_RAW,
    NOBROKER_BBOX,
    NOBROKER_SECTOR_PINS,
    NOBROKER_SHARED_ACCOMMODATION,
    REQUEST_DELAY_SECONDS,
    RAW_DATA_DIR,
    USE_PROXIES,
    PROXY_LIST,
    TARGET_CITY,
)

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [NOBROKER] %(message)s", force=True)
log = logging.getLogger(__name__)

# ── API (current as of 2025) ─────────────────────────────────────────────────
NB_FILTER_URL = "https://www.nobroker.in/api/v3/multi/property/RENT/filter"
NB_OWNER_TEL_URL = "https://www.nobroker.in/api/v3/user/owner/phone/{property_id}"

HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://www.nobroker.in",
    "Referer": "https://www.nobroker.in/property/rent/noida",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def _bbox_center_pins() -> list[dict]:
    """Four approximate pins inside NOBROKER_BBOX — one per target quadrant."""
    lat_mid = (NOBROKER_BBOX["lat_min"] + NOBROKER_BBOX["lat_max"]) / 2
    lng_mid = (NOBROKER_BBOX["lng_min"] + NOBROKER_BBOX["lng_max"]) / 2
    d_lat = (NOBROKER_BBOX["lat_max"] - NOBROKER_BBOX["lat_min"]) / 4
    d_lng = (NOBROKER_BBOX["lng_max"] - NOBROKER_BBOX["lng_min"]) / 4
    city_label = (TARGET_CITY or "Noida").strip()
    return [
        {"lat": lat_mid + d_lat, "lon": lng_mid - d_lng, "placeName": f"North-West, {city_label}", "showMap": False},
        {"lat": lat_mid + d_lat, "lon": lng_mid + d_lng, "placeName": f"North-East, {city_label}", "showMap": False},
        {"lat": lat_mid - d_lat, "lon": lng_mid - d_lng, "placeName": f"South-West, {city_label}", "showMap": False},
        {"lat": lat_mid - d_lat, "lon": lng_mid + d_lng, "placeName": f"South-East, {city_label}", "showMap": False},
    ]


def build_search_param_b64() -> str:
    pins = NOBROKER_SECTOR_PINS if NOBROKER_SECTOR_PINS else _bbox_center_pins()
    return base64.b64encode(json.dumps(pins, separators=(",", ":")).encode("utf-8")).decode("utf-8")


def _ms_to_iso(ms: object) -> str:
    try:
        ts = int(ms) / 1000.0  # type: ignore[arg-type]
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def parse_listing(raw: dict) -> dict:
    """Flatten a NoBroker v3 filter listing."""
    loc = raw.get("location") or {}
    lat = raw.get("latitude")
    lng = raw.get("longitude")
    if lat is None:
        lat = loc.get("lat")
    if lng is None:
        lng = loc.get("lng")

    detail = raw.get("detailUrl") or ""
    if detail.startswith("/"):
        listing_url = f"https://www.nobroker.in{detail}"
    elif detail.startswith("http"):
        listing_url = detail
    else:
        listing_url = f"https://www.nobroker.in/property/{raw.get('id', '')}"

    rent_val = raw.get("rent")
    rent_display = raw.get("formattedPrice") or (str(rent_val) if rent_val is not None else "")

    return {
        "source": "nobroker",
        "property_id": str(raw.get("id", "")),
        "name": raw.get("propertyTitle") or raw.get("title") or raw.get("propertyTitleTruncated", ""),
        "address": raw.get("address") or raw.get("completeStreetName", ""),
        "locality": raw.get("locality") or raw.get("nbLocality", ""),
        "city": raw.get("city", ""),
        "lat": lat,
        "lng": lng,
        "phone_raw": raw.get("phone") or raw.get("mobile") or (raw.get("owner") or {}).get("phone", ""),
        "owner_name": (raw.get("ownerName") or (raw.get("owner") or {}).get("name", "")).strip(),
        "owner_type": raw.get("listedBy") or raw.get("ownerType") or "SHARED",
        "rent_min": rent_val,
        "rent_max": rent_val,
        "rent": rent_display,
        "rating": raw.get("score"),
        "deposit": raw.get("deposit"),
        "furnishing": raw.get("furnishing", ""),
        "occupancy": raw.get("tenantTypeDesc") or raw.get("accomodationTypeDesc", ""),
        "posted_on": raw.get("postedOn") or raw.get("lastUpdateDate") or _ms_to_iso(raw.get("creationDate", "")),
        "listing_url": listing_url,
    }


def resolve_owner_phone(
    client: httpx.Client,
    property_id: str,
    cookies: dict[str, str],
    max_attempts: int = 1,
) -> str:
    url = NB_OWNER_TEL_URL.format(property_id=property_id)
    for attempt in range(max_attempts):
        try:
            resp = client.get(url, headers=HEADERS_BASE, cookies=cookies, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                phone = (
                    data.get("phone")
                    or data.get("mobile")
                    or data.get("data", {}).get("phone", "")
                )
                return str(phone) if phone else ""
            if resp.status_code == 429:
                log.warning("Rate-limited on phone resolution — skipping remaining")
                return ""
            if resp.status_code in (401, 403):
                log.warning("Auth error on phone resolution — check session cookies")
                return ""
        except Exception as exc:
            log.debug("Phone resolution attempt %d failed: %s", attempt + 1, exc)
        time.sleep(REQUEST_DELAY_SECONDS)
    return ""


def run(resolve_phones: bool = False) -> list[dict]:
    if not NOBROKER_COOKIES_RAW or NOBROKER_COOKIES_RAW == "YOUR_NOBROKER_COOKIE_STRING_HERE":
        log.warning(
            "NoBroker cookies not set — public filter API still works for listing "
            "metadata. Owner phone resolution will fail without cookies."
        )
        cookies: dict[str, str] = {}
    else:
        cookies = parse_cookie_string(NOBROKER_COOKIES_RAW)

    Path(RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)

    proxies = None
    if USE_PROXIES and PROXY_LIST:
        import random

        proxy_url = random.choice(PROXY_LIST)
        proxies = {"http://": proxy_url, "https://": proxy_url}

    search_param = build_search_param_b64()
    city_slug = (TARGET_CITY or "Noida").lower().replace(" ", "-")

    all_listings: list[dict] = []
    seen_ids: set[str] = set()
    page = 1

    with httpx.Client(proxies=proxies, follow_redirects=True) as client:
        while True:
            log.info("Fetching NoBroker filter page %d …", page)
            params = {
                "pageNo": str(page),
                "searchParam": search_param,
                "sharedAccomodation": "1" if NOBROKER_SHARED_ACCOMMODATION else "0",
                "orderBy": "nbRank,desc",
                "radius": "5",
                "traffic": "true",
                "travelTime": "30",
                "propertyType": "rent",
                "rent": "2000,200000",
                "city": city_slug,
            }

            try:
                resp = client.get(
                    NB_FILTER_URL,
                    params=params,
                    headers=HEADERS_BASE,
                    cookies=cookies,
                    timeout=25,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                log.error("HTTP %s — %s", exc.response.status_code, exc.response.text[:300])
                break
            except Exception as exc:
                log.error("Request failed: %s", exc)
                break

            raw_items = data.get("data") or []

            if not raw_items:
                log.info("No items on page %d — done", page)
                break

            log.info("  Page %d → %d items (%s)", page, len(raw_items), data.get("message", "")[:60])

            for item in raw_items:
                pid = str(item.get("id", ""))
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                listing = parse_listing(item)

                if resolve_phones and not listing["phone_raw"]:
                    listing["phone_raw"] = resolve_owner_phone(client, pid, cookies)
                    time.sleep(REQUEST_DELAY_SECONDS * 2)

                all_listings.append(listing)

            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    out_path = Path(RAW_DATA_DIR) / "nobroker.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_listings, f, ensure_ascii=False, indent=2)

    log.info("✓ Saved %d NoBroker listings → %s", len(all_listings), out_path)
    return all_listings


if __name__ == "__main__":
    run(resolve_phones=False)
