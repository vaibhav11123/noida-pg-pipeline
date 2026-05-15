#!/usr/bin/env bash
# Thin wrapper — full checks live in run_gmaps_scraper.sh preflight
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/scripts/run_gmaps_scraper.sh" preflight "$@"
