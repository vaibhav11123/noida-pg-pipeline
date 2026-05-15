# Exa extractor — canonical workflow and deep reference

This document is the **canonical** operational spec for `phase1_extract/exa_extractor.py`: what the pipeline *is*, how each mode works, the **blessed** commands and artifact names, and when to change scope. For CLI flags, run `python phase1_extract/exa_extractor.py --help`.

---

## 0. What “canonical” means here

| Term | Meaning |
|------|--------|
| **Canonical mode order** | `similar` → `agent` → `search`. Same order as `--mode all` inside `run()`. |
| **Canonical geography** | Start `--tier noida`; widen to `delhi` → `ncr` → `india` only when the product ask expands. |
| **Canonical production artifacts** | `data/raw/exa_similar.json`, `exa_agent.json`, `exa_search.json` (+ matching `.csv`). These are what `phase2_aggregate/aggregator.py` loads by stem name. |
| **Canonical entrypoint** | One process: `python phase1_extract/exa_extractor.py …` from **repo root** (`pg_pipeline/`), so `config` / `RAW_DATA_DIR` resolve. |

Anything else (smoke files, partial runs) is **valid but non-canonical** for merge unless you change aggregator config.

---

## 1. Deep dive — architecture and data flow

### 1.1 Position in the wider pipeline

```text
Google Maps / gosom  →  gmaps_operator_seeds.json (optional but ideal)
                              │
                              ▼
                    ┌─────────────────────┐
                    │  exa_extractor.py  │
                    │  Exa API (cloud)    │
                    └─────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
  exa_similar.*        exa_agent.*         exa_search.*
          │                   │                   │
          └───────────────────┴───────────────────┘
                              │
                              ▼
                    phase2_aggregate (JSON by fixed names)
```

**Inputs you control locally**

- `.env` / environment: `EXA_API_KEY`. `config.py`: `RAW_DATA_DIR`, `REQUEST_DELAY_SECONDS`.
- `data/raw/gmaps_operator_seeds.json`: list of `{…, "url": …}`. Filtered to drop junk domains (Wix, Facebook, etc.). If missing, **fallback seeds** in code are used (smaller, static list).

**Outputs (canonical names)**

| File | Mode | Downstream key in aggregator |
|------|------|------------------------------|
| `exa_similar.json` / `.csv` | `similar` | `exa_similar` |
| `exa_agent.json` / `.csv` | `agent` | `exa_agent` |
| `exa_search.json` / `.csv` | `search` | `exa_search` |

Each run **overwrites** the JSON+CSV for the modes it executes (non-smoke). There is no append-by-default.

### 1.2 Mode 1 — `similar` (findSimilar)

**API:** `exa_client.find_similar_and_contents(seed_url, num_results=…, exclude_source_domain=True, …)`.

**Intent:** Treat real operator homepages as **seeds**; Exa returns URLs *structurally similar* to those sites — often other PG operators, directory company pages, etc.

**Local logic (after Exa returns):**

- Dedupe by URL.
- `is_valid()` drops portals / wrong categories by URL and title regex.
- Require at least one **phone or email** in text (regex extraction); else row discarded.
- `operator_scale()` classifies copy as confirmed vs possible scale.
- **Ignores `--tier`** for API parameters; `geo_tier` in rows is effectively Noida-anchored from product design.

**Cost / time:** One API call per seed × `REQUEST_DELAY_SECONDS` between seeds. With dozens of seeds, this is usually the **longest wall-clock** part of `--mode all`, though cheaper per call than agent.

### 1.3 Mode 2 — `agent` (Exa Agent beta)

**API:** `exa_client.beta.agent.runs.create` + `poll_until_finished`, with a fixed `output_schema` (operators array with phones, emails, capacity, etc.).

**Intent:** For each **(sector batch × tier)** in a capped plan (`_build_agent_batch_plan`), run a multi-hop research task; Exa returns **structured JSON**, not raw HTML to regex.

**`--tier`:** Selects which batch lists run (`SECTOR_BATCHES` for noida, `DELHI_BATCHES`, etc.). **`--agent-batches`** caps total `(batch, tier)` pairs **in plan order** (Noida batches first, then Delhi, … as included in `tier_list`).

**Cost / time:** Highest cost and **minutes per batch** (polling). Scale `--agent-batches` deliberately.

### 1.4 Mode 3 — `search` (keyword + optional `category=company`)

**API:** `exa_client.search_and_contents(query, type="auto", include_domains=OPERATOR_DOMAINS, …)`.

**Intent:** Fixed **query list per tier** (`SEARCH_QUERIES`): some queries use `category="company"` for directory-style quality on IndiaMART/Sulekha-style intent.

**Important API constraint (implemented in code):** Exa’s **company** category does **not** allow `startPublishedDate`. Non-company queries still send `start_published_date` for recency. Mixing them caused historical 400s until that split was enforced.

**Local logic:** Same style as similar — dedupe, `is_valid`, phone/email required, `operator_scale`.

### 1.5 Orchestrator `run()` and `--mode all`

When `all` is in the mode set, execution order is **always**: similar → agent → search. Each block calls `save()` immediately after that mode returns, then the next mode runs. Final log line:

```text
=== DONE: <total> total leads | <confirmed> confirmed | <possible> possible ===
```

**`--smoke`:** Caps volume and writes `exa_*_smoke.*` so canonical merge files stay untouched for experiments.

---

## 2. Canonical commands (copy-paste)

Assume `cd` to **repository root** (`pg_pipeline/`).

| Goal | Canonical command |
|------|-------------------|
| **Full Exa Noida (all three modes, production files)** | `python3 phase1_extract/exa_extractor.py --mode all --tier noida` |
| **Full geographic cone in one shot** | `python3 phase1_extract/exa_extractor.py --mode all --tier all` |
| **Default script (similar only)** | `python3 phase1_extract/exa_extractor.py` |
| **Repair only search** (similar+agent already good) | `python3 phase1_extract/exa_extractor.py --mode search --tier noida` |
| **Pre-flight (unit tests, no network)** | `python3 -m unittest discover -s tests -p 'test_*.py' -q` |
| **Pre-flight (live API, minimal)** | `python3 phase1_extract/exa_extractor.py --smoke --mode similar` |

---

## 3. Default order and when to switch modes

| Step | Mode | Role |
|------|------|------|
| 1 | `similar` | **Always first in production.** Seeds from `gmaps_operator_seeds.json` or fallback. Cheapest relevance per rupee. |
| 2 | `agent` | **Second.** Structured deep leads; expensive. |
| 3 | `search` | **Third.** Directory / keyword sweep after 1+2. |

**If you only run one mode for a minimal pull, canonical choice is `similar`**, not `search` alone.

### When to switch

- **Stay on similar** until smoke or a short run proves keys + network + row counts look sane.
- **Add agent** when you need structured fields and accept time/cost.
- **Add search** after agent for your tier is done (or capped), or when you specifically need query-list coverage.

---

## 4. Geography (`--tier`)

Cone order (narrow → wide): **noida → delhi → ncr → india**. **`--tier all`** expands to that full list in one run.

**Note:** `similar` does not use tier for Exa parameters; tiers drive **agent** and **search** batch/query plans.

---

## 5. `--agent-batches`

| Intent | Value |
|--------|--------|
| First agent run / IT belt focus | `4` (default) |
| Full Noida batches | `10` |
| Noida + Delhi | `14` |
| Full NCR depth in plan | `18` |

Raise the cap only after a smaller run succeeds — avoids wasting agent budget on misconfiguration.

---

## 6. Smoke vs production

| Situation | Pattern |
|-----------|---------|
| Cheap connectivity check | `--smoke --mode similar` |
| All modes, minimal API | `--smoke --mode all --tier noida` |
| Merge-ready outputs | **No** `--smoke` → `exa_similar.*`, `exa_agent.*`, `exa_search.*` |

Do not point `phase2_aggregate` at `*_smoke` files unless you intentionally change the aggregator map.

---

## 7. Re-run vs skip (canonical decisions)

| Event | Action |
|-------|--------|
| Seeds file updated | Re-run **`similar`** |
| Want more sectors without redoing similar | Re-run **`agent`** with higher `--agent-batches` |
| Search failed or code fix for search | Re-run **`search`** only for that tier |
| Want entirely new snapshot of everything | `--mode all` for the relevant `--tier` |

---

## 8. Troubleshooting (deep but practical)

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `exa_search.*` nearly empty; log showed 400 + company + `startPublishedDate` | Exa constraint on company index | Fixed in extractor: re-run **`--mode search`** on current code |
| Log said “Credits exhausted” on 400 with UUID in body | Old substring heuristic on `402` inside requestId | Fixed: re-run search; real 402 still stops the loop |
| `Install exa-py` | Missing dependency | `pip install -r requirements.txt` |
| `Set EXA_API_KEY` | Placeholder key | Set `EXA_API_KEY` in `.env` |
| Every `Agent run failed` with **500** + `path":"/runs"` | Exa Agent API outage or overload (their side) | Wait and re-run **`--mode agent`** (or `--mode all`); extractor now **retries** each batch on 5xx/429 with backoff |
| `exa_agent.*` empty but similar/search OK | All agent batches failed (e.g. sustained 500) | Same as above; check [status.exa.ai](https://status.exa.ai) or Exa support if it persists |

---

## 9. One-line cheatsheet

```text
Canonical:  smoke (optional) → unittest (optional) → --mode all --tier noida
Repair:     --mode search --tier noida  (if only search failed)
Widen:      same command with --tier delhi | ncr | india | all
Done:       log line "=== DONE:" and non-empty JSON for modes you care about
```

---

## 10. Automated checks (no Exa traffic)

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Covers CLI parsing, tier resolution, credit-error heuristics, and `run()` guard rails; does **not** replace a `--smoke` live call.
