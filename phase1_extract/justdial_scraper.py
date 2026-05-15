"""
phase1_extract/justdial_scraper.py
====================================
JustDial **Paying Guest** listings (mobile) for Noida target sectors.

Notes:
  * The old `nct-10001455` URLs map to a hotel gallery shell — not PG results.
    PG vertical uses `nct-10110563` (Paying Guest Accommodations).
  * Listing cards are **JS-rendered**; plain httpx often sees an empty shell.
    Set `JUSTDIAL_USE_PLAYWRIGHT = True` in config.py after
    `playwright install chromium` to populate the DOM before parsing.

Outputs: data/raw/justdial.json
"""

import json
import logging
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    JUSTDIAL_USE_PLAYWRIGHT,
    PLAYWRIGHT_HEADLESS,
    PROXY_LIST,
    RAW_DATA_DIR,
    REQUEST_DELAY_SECONDS,
    USE_PROXIES,
    TARGET_CITY,
)

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [JUSTDIAL] %(message)s", force=True)
log = logging.getLogger(__name__)

# Mobile PG category (verified path — do not use hotel nct-10001455 for PG)
JD_NCAT = "10110563"
_CITY_SLUG = (TARGET_CITY or "Noida").replace(" ", "-")

JD_SEARCH_URL = (
    f"https://m.justdial.com/{_CITY_SLUG}/{{slug}}/nct-{JD_NCAT}"
    "?city={city}&source=2&searchcity={city}"
)

# Slugs are SEO paths under the PG vertical
JD_SLUGS = [
    "Paying-Guest-Accommodations-in-Sector-57",
    "Paying-Guest-Accommodations-in-Sector-58",
    "Paying-Guest-Accommodations-in-Sector-62",
    "Paying-Guest-Accommodations-in-Sector-63",
    "Hostels-in-Sector-62-Noida",
    "Hostels-in-Sector-57-Noida",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://m.justdial.com/",
    "DNT": "1",
}


# ── CSS font-obfuscation decoder ─────────────────────────────────────────────

def build_glyph_map(css_text: str) -> dict[str, str]:
    glyph_map: dict[str, str] = {}
    content_pattern = re.compile(
        r"\.(icon-[a-z]+)[^{]*\{[^}]*content\s*:\s*[\"']\\([0-9a-fA-F]{4})[\"']",
        re.IGNORECASE,
    )
    class_to_hex: dict[str, str] = {}
    for m in content_pattern.finditer(css_text):
        class_name, hex_code = m.group(1), m.group(2).lower()
        class_to_hex[class_name] = hex_code

    digit_map_pattern = re.compile(r"\"([0-9a-fA-F]{4})\"\s*:\s*\"(\d)\"")
    hex_to_digit: dict[str, str] = {}
    for m in digit_map_pattern.finditer(css_text):
        hex_to_digit[m.group(1).lower()] = m.group(2)

    for cls, hex_code in class_to_hex.items():
        if hex_code in hex_to_digit:
            glyph_map[cls] = hex_to_digit[hex_code]

    if not hex_to_digit and class_to_hex:
        sorted_classes = sorted(class_to_hex.items(), key=lambda x: x[1])
        for idx, (cls, _) in enumerate(sorted_classes):
            glyph_map[cls] = str(idx % 10)

    return glyph_map


def decode_phone(phone_element: Tag, glyph_map: dict[str, str]) -> str:
    digits = []
    for span in phone_element.find_all("span"):
        for cls in span.get("class", []):
            if cls.startswith("icon-"):
                digits.append(glyph_map.get(cls, "?"))
    return "".join(digits)


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _extract_card(card: Tag, glyph_map: dict[str, str]) -> dict | None:
    """Parse one listing card (multiple layouts)."""
    name = ""
    for sel in [".jcn a", ".lcname", "h2 a", "h3 a", ".jdtitle a", "a.alst_name", ".store-name"]:
        hit = card.select_one(sel)
        if hit:
            name = _text(hit)
            break
    if not name:
        name = _text(card.select_one("h2, h3, .jdtitle"))

    address = ""
    for sel in [".cont_fluid", ".address", ".mreinf", ".alst_address", "p.address-info", ".mlist-address"]:
        hit = card.select_one(sel)
        if hit:
            address = _text(hit)
            if len(address) > 5:
                break

    phone = ""
    phone_el = card.select_one(".mobilesv, .contact-info .phone, [class*='mobileno'], .phn, span.mobsv")
    if phone_el:
        phone = decode_phone(phone_el, glyph_map)
        if "?" in phone or len(phone) < 7:
            plain = re.sub(r"\D", "", phone_el.get_text())
            if plain:
                phone = plain

    rating = _text(card.select_one(".star_rating, .jd-rating, .rstrt, .rtng"))

    if not name or len(name) < 2:
        return None

    return {
        "source": "justdial",
        "name": name,
        "address": address,
        "phone_raw": phone,
        "rating": rating,
    }


def parse_page(html: str) -> tuple[list[dict], dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    css_text = ""
    for style_tag in soup.find_all("style"):
        css_text += style_tag.get_text()
    glyph_map = build_glyph_map(css_text)

    listings: list[dict] = []
    seen: set[str] = set()

    card_selectors = [
        "li.cntanr",
        "li.cntanr.mt",
        "ul.rslt_lstng > li",
        "li[class*='lstpge']",
        "li.resultbox",
        "div.resultbox",
        "div[class*='store-details']",
    ]
    cards: list[Tag] = []
    for sel in card_selectors:
        found = soup.select(sel)
        if len(found) >= 2:
            cards = found
            log.debug("Using card selector %s (%d nodes)", sel, len(found))
            break

    for card in cards:
        row = _extract_card(card, glyph_map)
        if not row:
            continue
        key = (row["name"], row.get("phone_raw", ""))
        if key in seen:
            continue
        seen.add(key)
        listings.append(row)

    return listings, glyph_map


def fetch_html_playwright(url: str) -> str | None:
    if not JUSTDIAL_USE_PLAYWRIGHT:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=PLAYWRIGHT_HEADLESS)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_selector(
                    "li.cntanr, ul.rslt_lstng li, .mobilesv",
                    timeout=18_000,
                )
            except Exception:
                log.debug("wait_for_selector timed out — continuing with partial DOM")
            page.wait_for_timeout(1500)
            html = page.content()
            ctx.close()
            browser.close()
        return html
    except Exception as exc:
        log.warning("Playwright fetch failed (%s). Install browsers: playwright install chromium", exc)
        return None


def fetch_all_pages(client: httpx.Client, base_url: str, max_pages: int = 5) -> list[dict]:
    all_listings: list[dict] = []

    for page_num in range(1, max_pages + 1):
        url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
        log.info("  Fetching page %d: %s", page_num, url)

        try:
            resp = client.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("HTTP %s on page %d — stopping pagination", exc.response.status_code, page_num)
            break
        except Exception as exc:
            log.warning("Request error on page %d: %s", page_num, exc)
            break

        listings, _ = parse_page(resp.text)
        if not listings and JUSTDIAL_USE_PLAYWRIGHT:
            log.info("  No static HTML listings — trying Playwright …")
            html = fetch_html_playwright(url)
            if html:
                listings, _ = parse_page(html)

        if not listings:
            log.info("  No listings on page %d — end of results", page_num)
            break

        log.info("  Page %d → %d listings", page_num, len(listings))
        all_listings.extend(listings)
        time.sleep(REQUEST_DELAY_SECONDS)

    return all_listings


def run() -> list[dict]:
    Path(RAW_DATA_DIR).mkdir(parents=True, exist_ok=True)

    proxies = None
    if USE_PROXIES and PROXY_LIST:
        import random

        proxy_url = random.choice(PROXY_LIST)
        proxies = {"http://": proxy_url, "https://": proxy_url}
        log.info("Using proxy: %s", proxy_url)

    city = TARGET_CITY or "Noida"
    all_results: list[dict] = []
    seen_phones: set[str] = set()

    with httpx.Client(proxies=proxies, follow_redirects=True) as client:
        for slug in JD_SLUGS:
            url = JD_SEARCH_URL.format(slug=slug, city=city)
            log.info("Query slug: %s", slug)
            listings = fetch_all_pages(client, url)

            for item in listings:
                phone = re.sub(r"\D", "", item.get("phone_raw", ""))
                if phone and phone in seen_phones:
                    continue
                if phone:
                    seen_phones.add(phone)
                all_results.append(item)

            time.sleep(REQUEST_DELAY_SECONDS * 2)

    out_path = Path(RAW_DATA_DIR) / "justdial.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    log.info("✓ Saved %d JustDial listings → %s", len(all_results), out_path)
    if not all_results and not JUSTDIAL_USE_PLAYWRIGHT:
        log.warning(
            "JustDial returned 0 rows. Mobile listings are JS-rendered — set "
            "JUSTDIAL_USE_PLAYWRIGHT = True in config.py and run "
            "`playwright install chromium`."
        )
    return all_results


if __name__ == "__main__":
    run()
