# Setup Guide — Noida PG Operator Pipeline

## Quick start

```bash
cd pg_pipeline
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# → Edit `.env` with your keys (see steps below)
python main.py --skip-portals   # fastest run: Places + JustDial + NoBroker only
```

Secrets (`GOOGLE_PLACES_API_KEY`, `EXA_API_KEY`, cookies, etc.) live in a repo-root **`.env`** file (see `.env.example`). Non-secret geography, delays, and feature flags stay in **`config.py`**.

---

## Step 1 — Google Places API Key (REQUIRED — free tier sufficient)

This is your highest-yield data source. Get it in ~5 minutes:

1. Go to https://console.cloud.google.com/
2. Create a new project (or select an existing one)
3. Go to **APIs & Services → Library**
4. Search for **"Places API (New)"** → Enable it
5. Go to **APIs & Services → Credentials → Create Credentials → API Key**
6. Copy the key
7. (Optional but recommended) Restrict the key to **Places API (New)** only
8. Add to your repo-root `.env` file:
   ```bash
   GOOGLE_PLACES_API_KEY=AIza...
   ```

**Cost**: The Places API has a free monthly credit of $200 (~40,000 text search calls).
Your pipeline makes ~14–30 queries total — well within free tier.

---

## Step 2 — Numverify API Key (optional — for HLR phone validation)

1. Go to https://numverify.com
2. Sign up for a free account (100 lookups/month free)
3. Copy your API key from the dashboard
4. Add to `.env`:
   ```bash
   NUMVERIFY_API_KEY=your_key_here
   ```

**Alternative**: Use Twilio Lookup instead — set `USE_TWILIO_LOOKUP = True`
and fill in your Twilio SID + Auth Token.

---

## Step 3 — NoBroker Session Cookies (optional — improves NoBroker yield)

Without cookies the NoBroker scraper will hit auth walls quickly.
Export your session cookies after logging in manually:

1. Open Chrome / Firefox
2. Go to https://www.nobroker.in and **log in** with your account
3. Open DevTools → **Network** tab
4. Navigate to any PG search page on NoBroker
5. Click any XHR request in the Network tab
6. Copy the **Cookie** header value from the Request Headers
7. Add to `.env` (single line; quote if needed in your shell, not required in `.env`):
   ```bash
   NOBROKER_COOKIES_RAW=nb_session=xxx; auth_token=yyy; ...
   ```

---

## Step 4 — WhatsApp Business Cloud API (optional — for WA presence checks)

1. Create a Meta Business account at https://business.facebook.com
2. Add a WhatsApp Business number (can use a secondary SIM)
3. Go to https://developers.facebook.com → Create App → Business
4. Add **WhatsApp** product to your app
5. From the WhatsApp > Getting Started page, copy:
   - **Temporary access token** (or generate permanent token)
   - **Phone number ID**
6. Add to `.env`:
   ```bash
   WHATSAPP_API_TOKEN=EAAxxxx...
   WHATSAPP_PHONE_ID=123456789
   ```

---

## Step 5 — Run the pipeline

```bash
# Full pipeline (all phases, ~90–120 min)
python main.py

# Fast run — skip Playwright scrapers (~30–45 min)
python main.py --skip-portals

# Single phase
python main.py --phase 1    # extraction only
python main.py --phase 2    # aggregation only
python main.py --phase 3    # validation only
python main.py --phase 4    # export only

# With Truecaller enrichment (if you have a token)
python main.py --truecaller YOUR_TC_TOKEN

# Attempt NoBroker phone resolution (burns quota — use sparingly)
python main.py --resolve-phones
```

---

## Output Files

| File | Description |
|------|-------------|
| `data/raw/google_places.json` | Raw Google Places results |
| `data/raw/justdial.json` | Raw JustDial listings |
| `data/raw/nobroker.json` | Raw NoBroker listings |
| `data/raw/99acres.json` | Raw 99acres listings |
| `data/raw/magicbricks.json` | Raw MagicBricks listings |
| `data/raw/housing.json` | Raw Housing.com listings |
| `data/merged/merged_contacts.csv` | Deduplicated, sector-tagged dataset |
| `data/merged/validated_contacts.csv` | Phone-verified, broker-filtered |
| `data/merged/rejected_contacts.csv` | Rejected (invalid/broker/dead) |
| `data/final_pg_contacts.csv` | **Master deliverable** — ranked, scored |
| `data/whatsapp_outreach_batch.csv` | Ready-to-send WA messages |

---

## Expected Yields

| Stage | Expected Count |
|-------|---------------|
| Raw extraction (all sources) | 400–700 listings |
| After deduplication | 250–450 unique entries |
| After phone validation | 150–300 reachable numbers |
| After broker filtering | 100–220 owner contacts |
| WhatsApp-verified | 80–180 WA-ready contacts |
| Expected replies (10–25%) | 10–45 engaged leads |

---

## Proxy Setup (recommended for portal scrapers)

If you have residential proxy credentials:

Edit `config.py`: set `USE_PROXIES = True` and add URLs to `PROXY_LIST` (do not commit real credentials in a public repo; use a private branch or local-only edits).

Recommended providers for Indian IP pools:
- Bright Data (Luminati) — best Indian ISP coverage
- Oxylabs — good Jio/Airtel pools
- IPRoyal — budget option

---

## Troubleshooting

**"Google Places API key not set"**
→ Set `GOOGLE_PLACES_API_KEY` in `.env` (Step 1 above)

**"Playwright not installed"**
→ Run: `pip install playwright && playwright install chromium`

**NoBroker returns empty results**
→ Export fresh session cookies (Step 3). Cookies expire every ~24 hours.

**JustDial phone numbers showing "????"**
→ JustDial rotated their CSS glyph map. The scraper rebuilds it per session —
  if you still see this, open an issue. The glyph-map builder may need updating.

**Rate limit errors**
→ Increase `REQUEST_DELAY_SECONDS` in `config.py` (default: 2.5s)
→ Enable proxy rotation: `USE_PROXIES = True` in `config.py`
