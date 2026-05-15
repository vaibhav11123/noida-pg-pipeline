# PG Operator Intelligence Pipeline

**Not a scraper. A pipeline.** Extraction → normalization → qualification → outreach, with failure handling at every layer.

> **4-phase Python ETL** — Exa neural search + Google Maps grid scraping + portal crawl → dedup → geo-tiered qualification → outreach artifacts. Built to extract and score **900+** PG operator contacts across India’s NCR corridor (Noida-centric, expandable).

---

## Elevator pitch

Architected a production-style, **multi-phase Python pipeline** that combines **neural web search (Exa)**, **geospatial Maps scraping (Gosom / Docker + CSV ingest)**, **Places + portal extraction (Playwright / httpx)**, and **automated lead qualification** (phones, broker signals, optional WhatsApp / HLR checks) into ranked CSV outputs for calling and WhatsApp campaigns.

---

## Architecture

| Phase | Package | Responsibility |
|-------|-----------|----------------|
| **1 — Extract** | `phase1_extract/` | Exa (Search / Websets / similar / agent modes), Google Places, JustDial / NoBroker paths, Gosom CSV → JSON ingest, Playwright portal scrapers |
| **2 — Aggregate** | `phase2_aggregate/` | Multi-source merge, deduplication, normalization, sector tagging |
| **3 — Validate** | `phase3_validate/` | E.164 checks, Numverify / Twilio HLR-style enrichment, WhatsApp presence (optional), Truecaller hook, broker keyword filtering |
| **4 — Outreach** | `phase4_outreach/` | Final ranked deliverables and outreach-oriented CSVs |

**Orchestration:** `main.py` runs all four phases with CLI flags (`--phase`, `--skip-portals`, `--skip-gosom`, `--gosom-only`, etc.). This is a **batch CLI workflow**, not a hosted service.

**Secrets:** API keys and cookies load from a repo-root **`.env`** (see `.env.example`). Geography, delays, and feature flags live in **`config.py`**.

---

## What makes it “production-grade” (resume bullets)

- **Modular 4-phase layout** with explicit handoffs (`data/raw` → `data/merged` → final CSVs), structured logging, and Docker-gated Gosom workflows (`scripts/run_gmaps_scraper.sh`, approval env vars) so scrapes are reproducible, not accidental.
- **Exa integration** (`exa_extractor.py`) across a **geo tier cone** (Noida → Delhi → NCR → India) with parameterized modes (similar / search / agent / websets), domain hygiene, autoprompt deprecation awareness, and guard rails for quota / **402**-style exhaustion — preferring operators with **verified phone** or **non-portal** email where the pipeline enforces it.
- **Multi-source dedup & normalization** merging Gosom grid output, Exa artifacts, Places JSON, and portal crawls — **E.164** normalization (`phonenumbers`), **+91 / 10-digit** hygiene, portal **inbox blocklists** (e.g. generic listing-site addresses so `feedback@…` / `hello@…` do not pollute operator contact fields), and cross-source dedup keyed on phone fingerprints.
- **Geospatial grid scraping** over configurable **bounding boxes** (Noida sectors encoded in `config.py` + grid helpers in `scripts/`) using Gosom’s **`-grid-bbox`** path, tunable cell size, concurrency via **`-c`**, producing structured CSV with **lat/lng**, ratings, review counts, and **website** fields for downstream scoring.
- **Tiered qualification** combining phonenumbers validation, optional carrier / line-type APIs, WhatsApp Business checks, broker keyword lists, and source / rating heuristics — emitting **validated** vs **rejected** splits plus **final ranked** call sheets and WhatsApp-oriented exports.

---

## Interview sound bites

> The Exa path had to tolerate **three different failure classes**: rate pressure on constrained tiers, **SDK / API drift** (e.g. `use_autoprompt` deprecation in **exa-py**), and **402 / credit exhaustion** mid-run — each needs a different backoff or exit story than a generic retry loop.

> Portal crawls will happily surface **`feedback@99acres.com`** or **`hello@nobroker.in`** if you only regex for “something@something”; the fix is an **explicit blocklist** of listing-site inboxes plus stricter “operator-looking” contact rules.

> Phone normalization has to accept **10-digit** Indian mobiles, **spaced / dashed** variants, and numbers that already carry **`+91`** without double-prefixing — **libphonenumber** (`phonenumbers`) is the spine; everything else is edge-case glue.

---

## Stack

```
Python 3.11+ · exa-py · Playwright · Gosom (Docker) · pandas · phonenumbers ·
httpx · BeautifulSoup4 · lxml · tqdm · tenacity · CLI (argparse) ·
Geospatial grid scraping · Multi-source ETL · .env secrets hygiene
```

---

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env — at minimum GOOGLE_PLACES_API_KEY (see SETUP.md)
python main.py --skip-portals   # fast path: skips Playwright portal scrapers
```

- **Full setup & API keys:** [SETUP.md](SETUP.md)  
- **Exa runbook (modes, tiers, ops):** [phase1_extract/EXA_EXTRACTOR_RUNBOOK.md](phase1_extract/EXA_EXTRACTOR_RUNBOOK.md)

## Tests

```bash
python3 -m pytest tests/ -q
```

## Repository layout

| Path | Role |
|------|------|
| `main.py` | Phase orchestration |
| `phase1_extract/` | Extraction & ingest |
| `phase2_aggregate/` | Merge / dedup |
| `phase3_validate/` | Validation & enrichment |
| `phase4_outreach/` | Export artifacts |
| `scripts/` | GMaps grid, Docker helpers, preflight |
| `data/` | Raw + merged outputs (mostly **gitignored**; smoke fixtures tracked) |

## Security & privacy

- **Never commit `.env`.** Use `.env.example` as a template only.
- **Rotate keys** if they ever lived in chat logs, tickets, or a public branch.
- Tracked **`data/raw/exa_similar_smoke.csv`** and **`gmaps-output/docker_smoke.csv`** are **fully synthetic** (example.com URLs, placeholder phones) so the repository is safe to keep **public** without exposing real operators or reviewers.

## License

[MIT License](LICENSE).
