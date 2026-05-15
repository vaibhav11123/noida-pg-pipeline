# PG Operator Intelligence Pipeline

> **Not a scraper. A pipeline.**  
> Multi-source extraction → normalization → qualification → ranked artifacts.  
> 900+ PG operator leads aggregated across India's NCR corridor.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-22%20passed-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What This Is

A production-grade, local-first Python ETL pipeline that discovers, qualifies, and ranks Paying Guest (PG) operators across India — built for the Noida corporate corridor (Sectors 57–66, proximate to Airtel, EXL Software, and Uflex Industries).

The system aggregates operator leads from four independent extraction channels, normalizes and deduplicates across sources, scores operators by geographic proximity and business quality signals, and produces ranked artifacts for structured outreach.

**This is not a demo project.** Every design decision reflects a real commercial constraint.

---

## Elevator Pitch

Architected a production-style, multi-phase Python pipeline combining neural web search (Exa), geospatial Maps scraping (Gosom / Docker), Places + portal extraction (Playwright / httpx), and automated lead qualification (phone normalization, broker signal filtering, optional HLR checks) into ranked CSV outputs for targeted outreach campaigns.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    pg-operator-pipeline                           │
│                                                                    │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────┐          │
│  │ phase1_     │   │ phase2_      │   │ phase3_      │          │
│  │ extract/    │──▶│ aggregate/   │──▶│ validate/    │──▶ ...   │
│  │             │   │              │   │              │          │
│  │ • exa_      │   │ • dedup by   │   │ • E.164      │          │
│  │   extractor │   │   phone      │   │   checks     │          │
│  │ • gosom_    │   │ • normalize  │   │ • broker     │          │
│  │   gmaps     │   │ • sector tag │   │   filter     │          │
│  │ • portal_   │   │ • merge      │   │ • HLR enrich │          │
│  │   playwright│   │              │   │              │          │
│  └─────────────┘   └──────────────┘   └──────────────┘          │
│                                                │                  │
│                            ┌───────────────────▼──────────────┐  │
│                            │        phase4_outreach/           │  │
│                            │  • ranked CSV artifacts           │  │
│                            │  • tiered contact scoring         │  │
│                            └──────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

| Phase | Package | Responsibility |
|---|---|---|
| 1 — Extract | `phase1_extract/` | Exa (search / websets / similar / agent), Google Places, JustDial / NoBroker, Gosom CSV → JSON ingest, Playwright portal scrapers |
| 2 — Aggregate | `phase2_aggregate/` | Multi-source merge, deduplication, normalization, sector tagging |
| 3 — Validate | `phase3_validate/` | E.164 checks, Numverify / HLR enrichment, broker keyword filtering, WhatsApp presence (optional) |
| 4 — Outreach | `phase4_outreach/` | Final ranked deliverables and tiered contact CSVs |

**Orchestration:** `main.py` runs all four phases with CLI flags (`--phase`, `--skip-portals`, `--skip-gosom`, `--gosom-only`). Batch CLI workflow — not a hosted service.

**Secrets:** API keys and cookies load from `.env` (see `.env.example`). Geography, delays, and feature flags live in `config.py` (tracked); `config.py` reads secrets via `_env("…")` at import time — never commit real values.

---

## Methodology

### Extraction Design Philosophy

No single source covers the full PG operator market. Google Maps has breadth and structured metadata but misses operators without a listing. Exa captures operator-direct websites and portal pages Maps doesn't index. Portal crawlers reach operators who list exclusively on JustDial or NoBroker. Running all sources in parallel and merging on phone fingerprint maximises recall without sacrificing precision.

---

### Source 1 — Exa Neural Search (`phase1_extract/exa_extractor.py`)

#### Why Neural Search

Exa performs semantic similarity retrieval rather than keyword matching. PG operator pages use inconsistent terminology: "paying guest", "co-living", "hostel", "boys/girls rooms", "serviced rooms". A keyword search for "PG operator Noida Sector 58" misses operators whose pages say "Furnished rooms for working professionals near EXL Software". Neural search captures all of these.

#### Three Extraction Modes

**Mode 1 — `similar` (`findSimilar`)**
Seeds the search with URLs of known high-quality operators (`standardstays.in`, `flycolive.com`, etc.; see `FALLBACK_SEEDS` in code, plus optional `data/raw/gmaps_operator_seeds.json`). Exa finds semantically similar pages — discovering long-tail operators with non-standard domain names that query-based search misses. Highest-signal mode.

- Seeds from JSON or **~30** curated fallback URLs × configurable results per seed  
- Output: `data/raw/exa_similar.json` + `.csv`

**Mode 2 — `agent` (structured entity extraction)**
Structured extraction against Noida sector pages. `--agent-batches` controls depth:

| `--agent-batches` | Coverage |
|---|---|
| 4 (default) | Noida core |
| 10 | Full Noida |
| 14 | Noida + Delhi |
| 18 | Full NCR |

**Mode 3 — `search` (directory sweep)**
Domain-scoped queries against portal domains (JustDial, 99acres, MagicBricks, NoBroker, Sulekha, Housing.com). Broadest pass — catches operators who list exclusively on portals.

#### Geographic Tier Hierarchy

```
noida → delhi → ncr → india
```

Start with `--tier noida`. Widen only when Noida coverage is satisfactory. `--tier all` runs the full geo cone (see runbook for timing / cost).

#### Mode Sequencing

Canonical order: `similar` → `agent` → `search` (same as `--mode all`).

- Stay on `similar` until smoke run validates connectivity  
- Add `agent` when structured fields and budget allow  
- Re-run `search` only when queries change — no need to rerun similar/agent  

Mode independence matters: if only `exa_search.csv` is stale, re-run only Mode 3. Rerunning the full pipeline to fix one broken output wastes API credits.

#### Contact Extraction Logic

For each returned URL, full page text is fetched and parsed:

- Indian mobile regex: `[6-9]\d{9}` (10-digit, 6–9 prefix enforcement)
- Email: RFC-5322-compatible pattern
- Portal inbox blocklist: `feedback@99acres.com`, `hello@nobroker.in`, `rusers@justdial.com` — generic platform addresses that naive regex misclassifies as operator contacts

**Emission rule:** A row is emitted only if it contains at least one valid Indian mobile OR one non-portal email. URL-only rows are discarded.

**Multi-phone handling:** If a page yields N phone numbers, N rows are emitted with the first usable email copied to each. Phase 2 dedup handles cross-page duplicates.

#### Smoke Mode (`--smoke`)

Validates SDK, API key, and network before any production run:

```bash
python3 phase1_extract/exa_extractor.py --smoke --mode similar
python3 phase1_extract/exa_extractor.py --smoke --mode all --tier noida
```

Smoke writes to `data/raw/exa_*_smoke.*` — separate from production outputs the merge step expects.

#### Failure Handling

Four distinct failure modes encountered during development:

| Failure | Root Cause | Fix |
|---|---|---|
| `Invalid option: use_autoprompt` | exa-py breaking change — flag removed from SDK | Removed deprecated argument |
| HTTP 402 / `NO_MORE_CREDITS` | Credits exhausted mid-run | Early-exit on first 402; stops remaining queries immediately |
| `category=company` + `start_published_date` conflict | Exa API constraint: company category rejects date filter | `start_published_date` only sent for non-company queries |
| False 402 from requestId substring | `"402" in str(exc)` matched requestId `...d2fc4402c...` — legitimate 400 misclassified as billing error | Credit detection now uses `response.status_code == 402`, not substring match |

The requestId false positive is worth noting: a 400 (invalid request body) was being treated as a 402 (credits exhausted) because Exa's error response JSON contained a requestId with "402" as a substring. The fix is a precision change — `status_code == 402` vs `"402" in str(exc)` — but the impact was the search loop aborting silently on every company-category query.

#### Dual-Key Architecture

| Consumer | Key location | Purpose |
|---|---|---|
| Pipeline (`exa_extractor.py` → `config`) | **`EXA_API_KEY` in `.env`**, read via `config._env()` | Production extraction |
| Cursor IDE agent | `~/.cursor/mcp.json` → `x-api-key` | IDE searches without hitting free MCP rate limit |

These do not share config. Filling `.env` does not configure Cursor MCP.

---

### Source 2 — Google Maps Grid Scraper (`scripts/run_gmaps_scraper.sh`)

Powered by [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) — Go/Playwright headless scraper, Docker-packaged. Wrapper + preflight: `scripts/run_gmaps_scraper.sh`, `scripts/gmaps_preflight.sh`.

#### Why Grid-Based

Standard Maps search caps at ~20 results per query. For a 10km² target area, single-point search misses most operators. Grid mode tiles the bounding box and fires one search per cell:

```bash
docker run gosom/google-maps-scraper \
  -input /queries.txt \
  -results /out/results.csv \
  -grid-bbox "28.585,77.340,28.615,77.390" \
  -grid-cell 0.5 \
  -zoom 16 \
  -depth 3 \
  -c 8 \
  -exit-on-inactivity 3m
```

0.5km cell resolution captures village-road PGs that don't surface in broad searches. The `-c 8` concurrency flag controls parallelism — tune down if Maps rate-limits.

**Query design:** Multiple operator-intent terms surface different subsets:

```
PG in Sector 57 Noida
PG near EXL Noida
paying guest Noida Expressway
hostel Sector 58 Noida
PG for working professionals Sector 62
```

Ingest of Gosom CSV → pipeline JSON: `phase1_extract/gosom_gmaps.py`.

---

### Source 3 — Portal Crawlers (`phase1_extract/`)

**JustDial:** `justdial_scraper.py` — category pages and listing flows (Playwright optional for glyph-heavy paths).

**NoBroker:** `nobroker_api.py` — session cookies from `.env`; httpx for API-style flows.

**Other portals:** `portal_playwright.py` — Playwright for JS-heavy listing sites (99acres, MagicBricks, Housing, etc.).

**Rate limiting:** Default `REQUEST_DELAY_SECONDS` in `config.py` (e.g. 2.5s). Proxy rotation optional via `PROXY_LIST`.

---

### Synthetic smoke fixtures

`data/raw/exa_similar_smoke.csv` and `gmaps-output/docker_smoke.csv` are **fully synthetic** (example.com URLs, placeholder phones) and tracked for regression / layout checks — not live scrape dumps.

---

## Phase 2: Aggregate & Normalize

`phase2_aggregate/aggregator.py` combines source outputs into `data/merged/merged_contacts.csv`.

#### E.164 Phone Normalization

The pipeline uses **`phonenumbers`** and pandas-aware cleaning in code paths (see aggregator / validator). Conceptually:

```python
def normalize_phone(raw):
    if pd.isna(raw):
        return ''
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) >= 10:
        return '+91' + digits[-10:]
    return ''
```

Handles: raw 10-digit strings, space/dash-separated formats, already-prefixed +91 (no double-prefixing), float NaN from pandas CSV read.

#### Deduplication

Primary key: `phone_clean` (E.164 normalized). Fallback: `(name, address)` tuple. Source provenance preserved for downstream scoring.

Cross-source enrichment: where the same operator appears in GMaps (rating/review count) and Exa (listing URL/email), merges combine fields rather than dropping either row arbitrarily.

---

## Phase 3: Validate & Score

`phase3_validate/validator.py` applies a tiered scoring model (example framing from a representative run — **your counts will vary**):

| Tier | Criteria | Count (example) |
|---|---|---|
| **1** | Sector 57/58/59/62/63 + rating ≥ 4.0 + phone | 19 |
| **2** | Sector 57–66 + phone (any rating) | 9 |
| **3** | Any sector + rating ≥ 4.5 + website + phone | 49 |
| **4** | Phone only | 237 |
| **Excluded** | Rating < 3.0 | — |

**Operator scale signals:**

- Review count ≥ 30: likely multi-property operator  
- Custom domain website: semi-organized business  
- Business name pattern: registered entity  
- Exa similarity score > 0.86: semantically close to known large operators  

---

## Test Suite

**22 tests** in `tests/test_exa_extractor_smoke.py` — stdlib **`unittest`** (run via **`pytest`** or **`unittest discover`**).

Coverage highlights: CLI / tier resolution, smoke flag, numeric flags, **402 vs substring false positive** (`test_uuid_with_402_substring_not_credit_error`), API key guards, SDK-missing guard, help exit code.

The `_parse_args(argv=None)` pattern enables in-process testing without subprocess overhead or live Exa calls.

```bash
python3 -m pytest tests/ -q
# or
python3 -m unittest discover -s tests -p 'test_*.py' -q
```

---

## Repository Layout

```
noida-pg-pipeline/
├── main.py                          # Phase orchestration + CLI
├── config.py                        # Geography & toggles; reads secrets from .env
├── .env.example                     # Variable names only (copy → .env)
├── requirements.txt
├── LICENSE
├── SETUP.md
├── README.md
├── gmaps-queries.txt
│
├── phase1_extract/
│   ├── exa_extractor.py
│   ├── portal_playwright.py
│   ├── justdial_scraper.py
│   ├── nobroker_api.py
│   ├── google_places.py
│   ├── gosom_gmaps.py
│   └── EXA_EXTRACTOR_RUNBOOK.md
│
├── phase2_aggregate/
│   └── aggregator.py
│
├── phase3_validate/
│   └── validator.py
│
├── phase4_outreach/
│   └── exporter.py
│
├── scripts/
│   ├── run_gmaps_scraper.sh
│   ├── gmaps_preflight.sh
│   ├── build_query_grid.py
│   ├── gmaps_centroid_batches.py
│   └── gmaps_dry_run_estimate.py
│
├── tests/
│   └── test_exa_extractor_smoke.py
│
└── data/
    └── raw/
        └── exa_similar_smoke.csv    # Synthetic fixture — tracked
```

---

## Quick Start

```bash
git clone https://github.com/vaibhav11123/noida-pg-pipeline
cd noida-pg-pipeline
pip install -r requirements.txt
playwright install chromium
cp .env.example .env       # add keys — see SETUP.md
```

```bash
# Validate before spending API budget
python3 -m pytest tests/ -q
python3 phase1_extract/exa_extractor.py --smoke --mode similar

# Full Noida extract
python3 phase1_extract/exa_extractor.py --mode all --tier noida

# Expand geography
python3 phase1_extract/exa_extractor.py --mode all --tier delhi
python3 phase1_extract/exa_extractor.py --mode all --tier ncr

# GMaps grid scrape (Docker required — see scripts/run_gmaps_scraper.sh)
./scripts/run_gmaps_scraper.sh preflight

# Skip Playwright portal scrapers (fast path)
python main.py --skip-portals

# Full pipeline
python main.py
```

**Monitor a long run:**

```bash
python3 phase1_extract/exa_extractor.py --mode all --tier all 2>&1 | tee data/raw/exa_run.log
tail -f data/raw/exa_run.log
grep "=== DONE:" data/raw/exa_run.log
```

Full setup and API key instructions: [SETUP.md](SETUP.md)  
Exa mode sequencing rules: [phase1_extract/EXA_EXTRACTOR_RUNBOOK.md](phase1_extract/EXA_EXTRACTOR_RUNBOOK.md)

---

## Results

*Representative merge snapshot — your run will differ.*

| Source | Raw Rows | After Dedup | With Phone | With Email |
|---|---|---|---|---|
| Gosom GMaps | 930 | 314 | 314 | 0 |
| Exa Similar | 170 | 47 | 35 | 18 |
| Exa Search | 53 | 38 | 22 | 31 |
| **Total** | **1,153** | **399** | **371** | **49** |

---

## Interview Sound Bites

**On the Exa failure modes:**  
"The Exa path had to handle three distinct failure classes: rate pressure on free-tier MCP, SDK drift when `use_autoprompt` was removed from exa-py without a major version bump, and 402 credit exhaustion mid-run. Each needs a different recovery path — generic retry loops don't work here."

**On the requestId false positive:**  
"A legitimate 400 (invalid request body — `category=company` combined with `startPublishedDate`) was being misclassified as a 402 (credits exhausted) because `'402' in str(exc)` matched the Exa requestId `d2fc4402c...`. The fix is one-character precise: `response.status_code == 402`."

**On portal inbox filtering:**  
"Portal crawls surface `feedback@99acres.com` or `hello@nobroker.in` if you only regex for `something@something`. The fix is an explicit blocklist of known platform inbox addresses — not a heuristic, a closed list."

**On phone normalization:**  
"The same operator appears as `9810XXXXXX`, `+91 9810 XX XXXX`, and `091-9810-XXXXXX` across sources. Without E.164 normalization before dedup, you inflate your lead count with duplicates. `phonenumbers` is the spine; everything else is edge-case glue."

---

## Stack

```
Python 3.11+ · exa-py · Playwright · Gosom (Docker) · pandas ·
phonenumbers · httpx · BeautifulSoup4 · lxml · tenacity ·
tqdm · argparse · Geospatial grid scraping · Multi-source ETL
```

---

## Design Decisions

**Why local-first?** One-time acquisition pipeline for a specific asset. Hosting adds overhead with no benefit. Runs once per outreach cycle, produces artifacts, done.

**Why four extraction sources?** Each has a different coverage profile. No single source covers the full market.

**Why E.164 normalization before dedup?** Same operator appears in 3–4 formats across sources. Without normalization, dedup misses these and inflates lead count.

**Why emit one row per phone number?** A single operator page often has 2–3 numbers. Collapsing loses contacts. Phase 2 dedup handles cross-page duplicates.

**Why the portal inbox blocklist?** Naive regex extracts `feedback@99acres.com` from every 99acres page — appearing as a contact for hundreds of different operators. Explicit blocklist, not a heuristic.

**Why `_parse_args(argv=None)`?** Decouples CLI parsing from `sys.argv` for in-process testing. Many flag-combination tests, sub-second, no API calls.

---

## Security & Privacy

- Never commit `.env` — use `.env.example` as template only  
- Rotate any key that has ever lived in a chat log, ticket, or public branch  
- `data/raw/exa_similar_smoke.csv` and `gmaps-output/docker_smoke.csv` are **fully synthetic** (example.com URLs, placeholder phones) — safe for public repo  
- All production CSVs / JSON under `data/` (except the smoke fixture) are **gitignored** by default  

---

## License

MIT — see [LICENSE](LICENSE).

---

## Author

**Vaibhav Singh**  
B.Tech Production & Industrial Engineering, IIT Delhi (2027)  
[linkedin.com/in/vaibhav-singh-vs23](https://linkedin.com/in/vaibhav-singh-vs23)  
Claude Campus Ambassador, Anthropic | ML Engineer, Ministry of Education India

> *Built as infrastructure for a real engineering problem. The pipeline ran. The data was clean. The operator was found.*
