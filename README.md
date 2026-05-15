# Noida PG operator pipeline

Local Python pipeline to discover and validate paying-guest (PG) operator leads in Noida: multi-source extraction, merge/dedupe, phone and enrichment checks, then CSV exports for outreach.

This is a **CLI / batch workflow**, not a hosted service. Geography, toggles, and non-secret settings live in `config.py`. **API keys and cookies** are loaded from environment variables (recommended: a repo-root `.env` file — see `.env.example`).

## Prerequisites

- Python 3.11+ (recommended)
- Optional: Docker for the [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) ingest path

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env — at minimum set GOOGLE_PLACES_API_KEY (see SETUP.md)
python main.py --skip-portals
```

## Layout

| Path | Role |
|------|------|
| `main.py` | Orchestrates phases 1–4 |
| `phase1_extract/` | Places, portals, Exa, Gosom CSV ingest |
| `phase2_aggregate/` | Merge and normalize |
| `phase3_validate/` | Phone / WA / broker filters |
| `phase4_outreach/` | Final CSV exports |
| `scripts/` | GMaps grid, Docker helper scripts |
| `data/` | Raw JSON/CSV, merged outputs (generated locally) |

Full setup, API quotas, and troubleshooting: **[SETUP.md](SETUP.md)**. Exa-specific operations: **[phase1_extract/EXA_EXTRACTOR_RUNBOOK.md](phase1_extract/EXA_EXTRACTOR_RUNBOOK.md)**.

## Tests

```bash
pytest tests/
```

## Security

- Never commit `.env` or real keys. Keys that were previously in `config.py` should be **rotated** in the provider dashboards if this tree was ever shared or pushed to a public remote.
- Generated contact data under `data/` and large scrape outputs are **gitignored** by default; keep clones private if you remove those rules.

## License

No license file is included yet; add one before publishing the repository.
