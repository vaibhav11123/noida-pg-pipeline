#!/usr/bin/env bash
# =============================================================================
# gosom/google-maps-scraper — production-style wrapper
# =============================================================================
# No scrape runs unless you explicitly approve (see below).
#
# Commands:
#   ./scripts/run_gmaps_scraper.sh preflight
#       Docker/disk/query counts, optional backups — safe to run anytime.
#
#   GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh queries [--no-email] [--fast-mode]
#       Full gmaps-queries.txt (after stripping “#” comment lines) → gmaps-output/gmaps_results.csv
#       Tip: under Docker Desktop on Apple Silicon (linux/amd64 image), full Maps often hits Playwright’s
#       30s navigation timeout without --fast-mode; use --fast-mode for reliable runs (≤~21 results/query).
#
#   GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh queries-smoke [--no-email] [--fast-mode]
#       gmaps-output/smoke_queries.txt (3 jobs) → gmaps-output/gmaps_results_smoke.csv
#
#   GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh grid-mop [--no-email]
#       Hybrid mop: tiny broad query file + bbox grid (large cells) → gmaps-output/gmaps_grid_mop.csv
#
#   GMAPS_SCRAPE_APPROVED=yes ./scripts/run_gmaps_scraper.sh grid-legacy [--no-email]
#       Tight bbox (Sector 62 cluster) → gmaps-output/gmaps_grid.csv
#       Uses gmaps-output/grid_legacy_queries.txt (one line — never the full 251-line file on grid).
#
# Approval (any one):
#   export GMAPS_SCRAPE_APPROVED=yes
#   export GMAPS_SCRAPE_APPROVED=1
#   or pass  --approve  on the command line (same effect).
#
# After hybrid, merge into JSON:
#   python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results.csv gmaps-output/gmaps_grid_mop.csv
#
# Env overrides:
#   GOSOM_DOCKER_IMAGE          (default gosom/google-maps-scraper; pin e.g. :v1.12.1)
#   GOSOM_DOCKER_PLATFORM       e.g. linux/amd64 — passed as docker --platform (see Docker Desktop / Apple Silicon)
#   GMAPS_EXIT_ON_INACTIVITY    optional duration (e.g. 20m). Default: unset = flag omitted.
#       scrapemate treats lastActivityAt==0 as infinitely stale; a positive value often exits
#       on the first ~60s stats tick before any job completes — do not rely on this until upstream fixes.
#   GMAPS_GOSOM_C               concurrency override (default 2 on macOS, 4 elsewhere)
#   GMAPS_USE_GEO               yes|no — pass -geo/-zoom/-radius for query-list modes (default no).
#       **Deep sector queries:** keep no so each "PG in Sector … Noida" URL is not anchored to one
#       centroid (see scripts/build_query_grid.py + gmaps_centroid_batches.py for per-centroid runs).
#       yes + fixed GMAPS_GEO biases every job to the same viewport (OK for tiny smoke tests only).
#   GMAPS_GEO GMAPS_ZOOM GMAPS_RADIUS  Noida bias (radius in meters per gosom -radius)
#   GMAPS_DEPTH                 scroll depth for normal query mode (default 10)
#   GMAPS_FAST_DEPTH GMAPS_FAST_RADIUS  used with --fast-mode (defaults 1 / 8000)
#   GOSOM_DISABLE_TELEMETRY     yes|no — default yes → docker -e DISABLE_TELEMETRY=1
#   GMAPS_GRID_BBOX             grid-mop bbox minLat,minLon,maxLat,maxLon
#   GMAPS_GRID_CELL_KM          grid-mop cell size km (default 1.5)
#
# macOS Docker tips (upstream): https://github.com/gosom/google-maps-scraper/blob/main/MacOS%20instructions.md
# =============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE="${GOSOM_DOCKER_IMAGE:-gosom/google-maps-scraper}"
QUERIES_MAIN="$ROOT/gmaps-queries.txt"
QUERIES_MOP="$ROOT/gmaps-output/grid_mop_queries.txt"
QUERIES_LEGACY_GRID="$ROOT/gmaps-output/grid_legacy_queries.txt"
QUERIES_SMOKE="$ROOT/gmaps-output/smoke_queries.txt"
# gosom treats every non-empty line as a Maps query (no “# comment” support).
QUERIES_SCRUBBED_MAIN="$ROOT/gmaps-output/_queries_for_docker.txt"
QUERIES_SCRUBBED_MOP="$ROOT/gmaps-output/_grid_mop_for_docker.txt"
QUERIES_SCRUBBED_LEGACY="$ROOT/gmaps-output/_grid_legacy_for_docker.txt"
QUERIES_SCRUBBED_SMOKE="$ROOT/gmaps-output/_smoke_for_docker.txt"
GRID_BBOX="${GMAPS_GRID_BBOX:-28.482,77.300,28.660,77.430}"
GRID_CELL="${GMAPS_GRID_CELL_KM:-1.5}"

MODE="preflight"
NO_EMAIL=0
FAST_MODE=0
APPROVED="${GMAPS_SCRAPE_APPROVED:-}"
EMAIL_ARGS=()

for arg in "$@"; do
  case "$arg" in
    preflight|queries|queries-smoke|grid-mop|grid-legacy) MODE="$arg" ;;
    --no-email) NO_EMAIL=1 ;;
    --fast-mode) FAST_MODE=1 ;;
    --approve) APPROVED="yes" ;;
    -h|--help)
      sed -n '1,52p' "$0" | tail -n +2
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg (use --help)" >&2
      exit 2
      ;;
  esac
done

if [[ "$FAST_MODE" -eq 1 ]] && [[ "$MODE" == "grid-mop" || "$MODE" == "grid-legacy" ]]; then
  echo "[FAIL] --fast-mode cannot be used with grid-mop or grid-legacy (gosom: -fast-mode with -grid-bbox is unsupported)." >&2
  exit 2
fi

# ── gosom / Docker tuning (README: -geo, -c, -fast-mode; -exit-on-inactivity optional — see header) ──
EXIT_INACT="${GMAPS_EXIT_ON_INACTIVITY:-}"
GMAPS_GEO="${GMAPS_GEO:-28.535,77.391}"
GMAPS_ZOOM="${GMAPS_ZOOM:-15}"
GMAPS_RADIUS="${GMAPS_RADIUS:-12000}"
GMAPS_FAST_RADIUS="${GMAPS_FAST_RADIUS:-8000}"
GMAPS_USE_GEO="${GMAPS_USE_GEO:-no}"
GMAPS_DEPTH="${GMAPS_DEPTH:-10}"
GMAPS_FAST_DEPTH="${GMAPS_FAST_DEPTH:-1}"
if [[ -n "${GMAPS_GOSOM_C:-}" ]]; then
  GOSOM_CONCURRENCY="$GMAPS_GOSOM_C"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  GOSOM_CONCURRENCY=2
else
  GOSOM_CONCURRENCY=4
fi
DOCKER_PLATFORM_ARGS=()
if [[ -n "${GOSOM_DOCKER_PLATFORM:-}" ]]; then
  DOCKER_PLATFORM_ARGS=(--platform "$GOSOM_DOCKER_PLATFORM")
fi
TELEMETRY_ENV=()
if [[ "${GOSOM_DISABLE_TELEMETRY:-yes}" =~ ^(yes|1|true)$ ]]; then
  TELEMETRY_ENV=(-e DISABLE_TELEMETRY=1)
fi

mkdir -p gmaps-output/gmaps-backups

# Strip Markdown/doc comments (# …) and blank lines — gosom would otherwise search them on Maps.
_scrub_queries_to() {
  local src="$1"
  local dst="$2"
  if [[ ! -f "$src" ]]; then
    echo "[FAIL] missing query file: $src" >&2
    exit 1
  fi
  grep -Ev '^[[:space:]]*(#|$)' "$src" > "$dst"
  local c
  c="$(wc -l < "$dst" | tr -d '[:space:]')"
  if [[ -z "$c" || "$c" -eq 0 ]]; then
    echo "[FAIL] After stripping # comments, no queries left in $src" >&2
    exit 1
  fi
  echo "[info] scrubbed queries: $c line(s) from $(basename "$src") → $(basename "$dst")"
}

# gosom flags for query-list modes only (not grid — README: -fast-mode cannot combine with -grid-bbox).
_gosom_query_flags() {
  GOSOM_QUERY_FLAGS=()
  if [[ "$FAST_MODE" -eq 1 ]]; then
    GOSOM_QUERY_FLAGS+=(-fast-mode -depth "$GMAPS_FAST_DEPTH" -geo "$GMAPS_GEO" -zoom "$GMAPS_ZOOM" -radius "$GMAPS_FAST_RADIUS")
  else
    GOSOM_QUERY_FLAGS+=(-depth "$GMAPS_DEPTH")
    if [[ "${GMAPS_USE_GEO}" =~ ^(yes|1|true)$ ]]; then
      GOSOM_QUERY_FLAGS+=(-geo "$GMAPS_GEO" -zoom "$GMAPS_ZOOM" -radius "$GMAPS_RADIUS")
    fi
  fi
  GOSOM_QUERY_FLAGS+=(-c "$GOSOM_CONCURRENCY" -lang en)
  if [[ -n "$EXIT_INACT" ]]; then
    GOSOM_QUERY_FLAGS+=(-exit-on-inactivity "$EXIT_INACT")
  fi
}

# Grid docker runs use the same optional flag; keep as an array (avoids ${var:+...} word-splitting quirks).
_gosom_grid_inact_flags() {
  GOSOM_GRID_INACT=()
  if [[ -n "$EXIT_INACT" ]]; then
    GOSOM_GRID_INACT=(-exit-on-inactivity "$EXIT_INACT")
  fi
}

backup_tag() {
  tag="$(date -u +%Y%m%dT%H%M%SZ)"
  local d="$ROOT/gmaps-output/gmaps-backups/$tag"
  mkdir -p "$d"
  for f in \
    "$ROOT/gmaps-output/gmaps_results.csv" \
    "$ROOT/gmaps-output/gmaps_results_smoke.csv" \
    "$ROOT/gmaps-output/gmaps_grid_mop.csv" \
    "$ROOT/gmaps-output/gmaps_grid.csv" \
    "$ROOT/data/raw/gosom_gmaps.json"
  do
    if [[ -f "$f" ]]; then
      cp -p "$f" "$d/" || true
    fi
  done
  echo "$d"
}

cmd_preflight() {
  echo "━━━ GMAPS PREFLIGHT (no network scrape) ━━━"
  echo "Repo: $ROOT"
  if ! command -v docker >/dev/null 2>&1; then
    echo "[WARN] docker not in PATH — skipping daemon check (install Docker for real scrapes)"
  elif ! docker info >/dev/null 2>&1; then
    echo "[WARN] docker daemon not reachable — start Docker Desktop when you scrape"
  else
    echo "[OK] docker CLI and daemon"
  fi

  if [[ -f "$QUERIES_MAIN" ]]; then
    n="$(grep -Ev '^[[:space:]]*(#|$)' "$QUERIES_MAIN" | wc -l | tr -d '[:space:]')"
    echo "[OK] $QUERIES_MAIN — $n active query lines"
  else
    echo "[FAIL] missing $QUERIES_MAIN"
    return 1
  fi

  if [[ -f "$QUERIES_MOP" ]]; then
    m="$(grep -Ev '^[[:space:]]*(#|$)' "$QUERIES_MOP" | wc -l | tr -d '[:space:]')"
    echo "[OK] $QUERIES_MOP — $m active lines (grid-mop)"
  else
    echo "[WARN] missing $QUERIES_MOP (grid-mop will fail until created)"
  fi

  if [[ -f "$QUERIES_LEGACY_GRID" ]]; then
    lg="$(grep -Ev '^[[:space:]]*(#|$)' "$QUERIES_LEGACY_GRID" | wc -l | tr -d '[:space:]')"
    echo "[OK] $QUERIES_LEGACY_GRID — $lg line(s) (grid-legacy; keep tiny)"
  else
    echo "[WARN] missing $QUERIES_LEGACY_GRID"
  fi

  echo ""
  echo "── gosom defaults (override with env; see script header) ──"
  if [[ -n "$EXIT_INACT" ]]; then
    echo "  exit-on-inactivity=$EXIT_INACT  concurrency=$GOSOM_CONCURRENCY  fast-mode=$FAST_MODE"
  else
    echo "  exit-on-inactivity=(omitted)  concurrency=$GOSOM_CONCURRENCY  fast-mode=$FAST_MODE"
  fi
  echo "  geo-bias=$GMAPS_USE_GEO (GMAPS_GEO=$GMAPS_GEO zoom=$GMAPS_ZOOM radius_m=$GMAPS_RADIUS)"
  if [[ -n "${GOSOM_DOCKER_PLATFORM:-}" ]]; then
    echo "  docker --platform=$GOSOM_DOCKER_PLATFORM"
  fi
  echo "  image=$IMAGE"

  echo ""
  echo "── Disk (gmaps-output, data/raw) ──"
  df -h "$ROOT/gmaps-output" "$ROOT/data/raw" 2>/dev/null || df -h .

  echo ""
  echo "── Dry-run job estimate (Python) ──"
  if [[ -f "$ROOT/scripts/gmaps_dry_run_estimate.py" ]]; then
    python3 "$ROOT/scripts/gmaps_dry_run_estimate.py" || true
  fi

  echo ""
  ans="${GMAPS_PREFLIGHT_BACKUP:-}"
  if [[ -z "$ans" && -t 0 ]]; then
    read -r -p "Create timestamped backup of existing CSV/JSON now? [y/N] " ans || true
  elif [[ -z "$ans" ]]; then
    echo "(non-interactive: no backup; set GMAPS_PREFLIGHT_BACKUP=yes|no to skip this prompt)"
  fi
  if [[ "${ans:-}" =~ ^[yY]$ || "${GMAPS_PREFLIGHT_BACKUP:-}" == "yes" ]]; then
    d="$(backup_tag)"
    echo "[OK] Backup dir: $d"
  elif [[ "${GMAPS_PREFLIGHT_BACKUP:-}" == "no" ]]; then
    echo "(GMAPS_PREFLIGHT_BACKUP=no — skipped backup)"
  else
    echo "(skipped backup — set GMAPS_PREFLIGHT_BACKUP=yes before scrape, or answer y next time)"
  fi

  echo ""
  echo "━━━ Next step (requires your approval) ━━━"
  echo "Do NOT run scrape until you intentionally start it:"
  echo "  export GMAPS_SCRAPE_APPROVED=yes"
  echo "  ./scripts/run_gmaps_scraper.sh queries-smoke --approve --no-email   # quick 3-query test"
  echo "  ./scripts/run_gmaps_scraper.sh queries --approve   # or rely on env only"
  echo "  # optional: per-sector centroids (queries.csv → one Docker per viewport):"
  echo "  python3 scripts/build_query_grid.py geocode && python3 scripts/gmaps_centroid_batches.py materialize"
  echo "  GMAPS_SCRAPE_APPROVED=yes python3 scripts/gmaps_centroid_batches.py run"
  echo "  # optional second pass:"
  echo "  ./scripts/run_gmaps_scraper.sh grid-mop"
  echo "  python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results.csv gmaps-output/gmaps_grid_mop.csv"
}

require_approve() {
  if [[ "$APPROVED" != "yes" && "$APPROVED" != "1" && "$APPROVED" != "true" ]]; then
    echo "" >&2
    echo "Refusing to start Docker scrape (production gate)." >&2
    echo "Set:  export GMAPS_SCRAPE_APPROVED=yes   OR add  --approve  to this command." >&2
    echo "Run ./scripts/run_gmaps_scraper.sh preflight first if you have not." >&2
    exit 3
  fi
}

# Docker: all -v flags first, then IMAGE, then gosom CLI flags.
_email_args() {
  EMAIL_ARGS=()
  if [[ "$NO_EMAIL" -eq 0 ]]; then
    EMAIL_ARGS=(-email)
  else
    echo "[info] --no-email: skipping website email crawl (faster, fewer requests)"
    EMAIL_ARGS=()
  fi
}

run_queries() {
  require_approve
  echo "━━━ QUERY-LIST SCRAPE → gmaps-output/gmaps_results.csv ━━━"
  d="$(backup_tag)"
  echo "[info] Pre-scrape backup → $d"
  _email_args
  _scrub_queries_to "$QUERIES_MAIN" "$QUERIES_SCRUBBED_MAIN"
  _gosom_query_flags
  echo "[info] gosom: image=$IMAGE fast_mode=$FAST_MODE exit_inact=${EXIT_INACT:-omitted} c=$GOSOM_CONCURRENCY"
  docker run --rm \
    "${DOCKER_PLATFORM_ARGS[@]+"${DOCKER_PLATFORM_ARGS[@]}"}" \
    "${TELEMETRY_ENV[@]+"${TELEMETRY_ENV[@]}"}" \
    -v gmaps-playwright-cache:/opt \
    -v "$QUERIES_SCRUBBED_MAIN:/queries.txt:ro" \
    -v "$ROOT/gmaps-output:/out" \
    "$IMAGE" \
    -input /queries.txt \
    -results /out/gmaps_results.csv \
    "${GOSOM_QUERY_FLAGS[@]}" \
    "${EMAIL_ARGS[@]+"${EMAIL_ARGS[@]}"}"
  echo "Done. Ingest: python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results.csv"
}

run_queries_smoke() {
  require_approve
  if [[ ! -f "$QUERIES_SMOKE" ]]; then
    echo "Missing $QUERIES_SMOKE" >&2
    exit 1
  fi
  echo "━━━ QUERY-LIST SMOKE (3 jobs) → gmaps-output/gmaps_results_smoke.csv ━━━"
  d="$(backup_tag)"
  echo "[info] Pre-scrape backup → $d"
  _email_args
  _scrub_queries_to "$QUERIES_SMOKE" "$QUERIES_SCRUBBED_SMOKE"
  _gosom_query_flags
  echo "[info] gosom: image=$IMAGE fast_mode=$FAST_MODE exit_inact=${EXIT_INACT:-omitted} c=$GOSOM_CONCURRENCY"
  docker run --rm \
    "${DOCKER_PLATFORM_ARGS[@]+"${DOCKER_PLATFORM_ARGS[@]}"}" \
    "${TELEMETRY_ENV[@]+"${TELEMETRY_ENV[@]}"}" \
    -v gmaps-playwright-cache:/opt \
    -v "$QUERIES_SCRUBBED_SMOKE:/queries.txt:ro" \
    -v "$ROOT/gmaps-output:/out" \
    "$IMAGE" \
    -input /queries.txt \
    -results /out/gmaps_results_smoke.csv \
    "${GOSOM_QUERY_FLAGS[@]}" \
    "${EMAIL_ARGS[@]+"${EMAIL_ARGS[@]}"}"
  echo "Done. Ingest test: python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results_smoke.csv"
}

run_grid_mop() {
  require_approve
  if [[ ! -f "$QUERIES_MOP" ]]; then
    echo "Missing $QUERIES_MOP" >&2
    exit 1
  fi
  echo "━━━ GRID MOP → gmaps-output/gmaps_grid_mop.csv ━━━"
  echo "bbox=$GRID_BBOX  cell=${GRID_CELL}km  queries=$QUERIES_MOP"
  d="$(backup_tag)"
  echo "[info] Pre-scrape backup → $d"
  _email_args
  _scrub_queries_to "$QUERIES_MOP" "$QUERIES_SCRUBBED_MOP"
  echo "[info] gosom grid: exit_inact=${EXIT_INACT:-omitted} c=$GOSOM_CONCURRENCY"
  _gosom_grid_inact_flags
  docker run --rm \
    "${DOCKER_PLATFORM_ARGS[@]+"${DOCKER_PLATFORM_ARGS[@]}"}" \
    "${TELEMETRY_ENV[@]+"${TELEMETRY_ENV[@]}"}" \
    -v gmaps-playwright-cache:/opt \
    -v "$QUERIES_SCRUBBED_MOP:/queries.txt:ro" \
    -v "$ROOT/gmaps-output:/out" \
    "$IMAGE" \
    -input /queries.txt \
    -results /out/gmaps_grid_mop.csv \
    -grid-bbox "$GRID_BBOX" \
    -grid-cell "$GRID_CELL" \
    -zoom 16 \
    -depth 8 \
    -c "$GOSOM_CONCURRENCY" \
    -lang en \
    "${GOSOM_GRID_INACT[@]+"${GOSOM_GRID_INACT[@]}"}" \
    "${EMAIL_ARGS[@]+"${EMAIL_ARGS[@]}"}"
  echo "Done. Merge ingest example:"
  echo "  python3 phase1_extract/gosom_gmaps.py gmaps-output/gmaps_results.csv gmaps-output/gmaps_grid_mop.csv"
}

run_grid_legacy() {
  require_approve
  if [[ ! -f "$QUERIES_LEGACY_GRID" ]]; then
    echo "Missing $QUERIES_LEGACY_GRID" >&2
    exit 1
  fi
  echo "━━━ GRID LEGACY (small bbox, single-line queries) → gmaps-output/gmaps_grid.csv ━━━"
  d="$(backup_tag)"
  echo "[info] Pre-scrape backup → $d"
  _email_args
  _scrub_queries_to "$QUERIES_LEGACY_GRID" "$QUERIES_SCRUBBED_LEGACY"
  echo "[info] gosom grid: exit_inact=${EXIT_INACT:-omitted} c=$GOSOM_CONCURRENCY"
  _gosom_grid_inact_flags
  docker run --rm \
    "${DOCKER_PLATFORM_ARGS[@]+"${DOCKER_PLATFORM_ARGS[@]}"}" \
    "${TELEMETRY_ENV[@]+"${TELEMETRY_ENV[@]}"}" \
    -v gmaps-playwright-cache:/opt \
    -v "$QUERIES_SCRUBBED_LEGACY:/queries.txt:ro" \
    -v "$ROOT/gmaps-output:/out" \
    "$IMAGE" \
    -input /queries.txt \
    -results /out/gmaps_grid.csv \
    -grid-bbox "28.595,77.355,28.645,77.400" \
    -grid-cell 0.5 \
    -zoom 16 \
    -depth 5 \
    -c "$GOSOM_CONCURRENCY" \
    -lang en \
    "${GOSOM_GRID_INACT[@]+"${GOSOM_GRID_INACT[@]}"}" \
    "${EMAIL_ARGS[@]+"${EMAIL_ARGS[@]}"}"
  printf '%s\n' 'Done. Uses gmaps-output/grid_legacy_queries.txt (single line). Avoid full gmaps-queries.txt on grid.'
}

case "$MODE" in
  preflight) cmd_preflight ;;
  queries) run_queries ;;
  queries-smoke) run_queries_smoke ;;
  grid-mop) run_grid_mop ;;
  grid-legacy) run_grid_legacy ;;
  *) echo "Internal error: mode=$MODE" >&2; exit 2 ;;
esac
