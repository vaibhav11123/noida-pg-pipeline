"""
phase1_extract/portal_playwright.py
======================================
Playwright-based scraper for 99acres, MagicBricks, and Housing.com.

These portals:
  - Render via React/Angular SPAs (JavaScript hydration)
  - Embed listing data inside window.__INITIAL_STATE__ or inline JSON blobs
  - Gate phone numbers behind OTP/login walls (we harvest listing metadata only)
  - Expose listing IDs and owner names without authentication

Strategy:
  1. Navigate to PG search results pages.
  2. Wait for hydration to complete.
  3. Extract window.__INITIAL_STATE__ (or equivalent) via page.evaluate().
  4. Fall back to DOM parsing for any remaining surface data.
  5. Paginate through result pages.

Outputs: data/raw/99acres.json, data/raw/magicbricks.json, data/raw/housing.json
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import RAW_DATA_DIR, PLAYWRIGHT_HEADLESS, REQUEST_DELAY_SECONDS

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [PLAYWRIGHT] %(message)s")
log = logging.getLogger(__name__)

# ── search URLs ───────────────────────────────────────────────────────────────
PORTAL_CONFIGS = {
    "99acres": {
        "pages": [
            "https://www.99acres.com/pg-in-sector-57-noida-ffid",
            "https://www.99acres.com/pg-in-sector-58-noida-ffid",
            "https://www.99acres.com/pg-in-sector-62-noida-ffid",
            "https://www.99acres.com/pg-in-sector-63-noida-ffid",
        ],
        "state_key":    "__INITIAL_STATE__",
        "listing_path": ["srp", "data", "propertyList"],
        "output_file":  "99acres.json",
    },
    "magicbricks": {
        "pages": [
            "https://www.magicbricks.com/property-for-rent/pg-in-sector-57-noida",
            "https://www.magicbricks.com/property-for-rent/pg-in-sector-58-noida",
            "https://www.magicbricks.com/property-for-rent/pg-in-sector-62-noida",
            "https://www.magicbricks.com/property-for-rent/pg-in-sector-63-noida",
        ],
        "state_key":    "__NEXT_DATA__",
        "listing_path": ["props", "pageProps", "propertyList", "properties"],
        "output_file":  "magicbricks.json",
    },
    "housing": {
        "pages": [
            "https://housing.com/rent/paying-guest-noida-sector-57",
            "https://housing.com/rent/paying-guest-noida-sector-58",
            "https://housing.com/rent/paying-guest-noida-sector-62",
            "https://housing.com/rent/paying-guest-noida-sector-63",
        ],
        "state_key":    "__NEXT_DATA__",
        "listing_path": ["props", "pageProps", "searchResults", "results"],
        "output_file":  "housing.json",
    },
}

# ── helpers ───────────────────────────────────────────────────────────────────

def safe_get(d: dict | list, *keys):
    """Safely traverse nested dicts/lists."""
    cur = d
    for k in keys:
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def extract_listings_from_state(state: dict, listing_path: list[str]) -> list[dict]:
    """Walk listing_path inside parsed state JSON."""
    return safe_get(state, *listing_path) or []


def normalize_portal_listing(raw: dict, source: str) -> dict:
    """Flatten a raw portal listing object to our standard schema."""
    # Different portals use different field names — we try all known variants
    def pick(*keys):
        for k in keys:
            v = raw.get(k)
            if v:
                return str(v)
        return ""

    return {
        "source":      source,
        "property_id": pick("propId", "id", "listingId", "propertyId"),
        "name":        pick("propName", "name", "title", "propertyName", "societyName"),
        "address":     pick("fullAddress", "address", "localityName", "locality"),
        "city":        pick("city", "cityName"),
        "sector":      pick("sector", "localityId", "area"),
        "lat":         str(raw.get("latitude", raw.get("lat", ""))),
        "lng":         str(raw.get("longitude", raw.get("lng", raw.get("lon", "")))),
        "rent":        pick("rent", "price", "expectedRent", "monthlyRent"),
        "owner_name":  pick("ownerName", "contactName", "postedBy"),
        "owner_type":  pick("listedBy", "propertyFor", "ownerType"),
        "phone_raw":   "",   # gated — will remain empty for gated portals
        "posted_on":   pick("postedOn", "updatedOn", "modifiedDate"),
        "listing_url": pick("listingUrl", "url", "propertyUrl"),
    }


# ── Playwright extraction ─────────────────────────────────────────────────────

async def scrape_portal(portal_name: str, config: dict) -> list[dict]:
    """Scrape a single portal using Playwright."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return []

    all_listings: list[dict] = []
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        # Mask webdriver flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        for url in config["pages"]:
            log.info("[%s] Navigating to %s", portal_name, url)
            try:
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
            except PWTimeout:
                log.warning("[%s] Page load timed out: %s", portal_name, url)
                continue
            except Exception as exc:
                log.warning("[%s] Navigation error: %s", portal_name, exc)
                continue

            # ── Strategy 1: extract hydration state from window object ────────
            state_key = config["state_key"]
            raw_state = None
            try:
                raw_state = await page.evaluate(f"() => window.{state_key}")
            except Exception:
                pass

            if not raw_state:
                # Try __NEXT_DATA__ embedded in <script> tag (Next.js)
                try:
                    script_content = await page.inner_text(f"#__NEXT_DATA__")
                    raw_state = json.loads(script_content)
                except Exception:
                    pass

            if raw_state:
                raw_items = extract_listings_from_state(raw_state, config["listing_path"])
                log.info("[%s]   State extraction → %d raw items", portal_name, len(raw_items or []))
                for item in (raw_items or []):
                    listing = normalize_portal_listing(item, portal_name)
                    pid     = listing["property_id"]
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        all_listings.append(listing)

            # ── Strategy 2: DOM scraping fallback ─────────────────────────────
            else:
                log.info("[%s]   Falling back to DOM scraping …", portal_name)
                # Scroll to trigger lazy loading
                for _ in range(5):
                    await page.keyboard.press("End")
                    await asyncio.sleep(0.8)

                cards = await page.query_selector_all(
                    "[class*='card'], [class*='listing'], [class*='property-item'], [class*='result']"
                )
                log.info("[%s]   Found %d card elements", portal_name, len(cards))

                for card in cards:
                    try:
                        name_el  = await card.query_selector("[class*='title'], [class*='name'], h2, h3")
                        addr_el  = await card.query_selector("[class*='address'], [class*='locality']")
                        price_el = await card.query_selector("[class*='price'], [class*='rent']")

                        name  = await name_el.inner_text()  if name_el  else ""
                        addr  = await addr_el.inner_text()  if addr_el  else ""
                        price = await price_el.inner_text() if price_el else ""

                        if name.strip():
                            all_listings.append({
                                "source":      portal_name,
                                "property_id": "",
                                "name":        name.strip(),
                                "address":     addr.strip(),
                                "rent":        price.strip(),
                                "phone_raw":   "",
                                "lat": "", "lng": "", "city": "Noida",
                                "owner_name": "", "owner_type": "",
                                "posted_on": "", "listing_url": url,
                                "sector": "", "city": "Noida",
                            })
                    except Exception:
                        continue

            await asyncio.sleep(REQUEST_DELAY_SECONDS)

        await browser.close()

    return all_listings


async def run_all() -> dict[str, list[dict]]:
    """Run all portal scrapers sequentially (parallel risks IP ban)."""
    Path(RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {}

    for portal_name, config in PORTAL_CONFIGS.items():
        log.info("=== Scraping portal: %s ===", portal_name)
        listings = await scrape_portal(portal_name, config)
        results[portal_name] = listings

        out_path = Path(RAW_DATA_DIR) / config["output_file"]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(listings, f, ensure_ascii=False, indent=2)
        log.info("✓ [%s] Saved %d listings → %s", portal_name, len(listings), out_path)

        # Pause between portals to avoid simultaneous fingerprinting
        await asyncio.sleep(REQUEST_DELAY_SECONDS * 3)

    return results


def run() -> dict[str, list[dict]]:
    return asyncio.run(run_all())


if __name__ == "__main__":
    run()
