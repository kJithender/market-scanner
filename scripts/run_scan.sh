#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${MARKET_SCANNER_VENV:-$PROJECT_DIR/.venv}"
OUTPUT_DIR="${MARKET_SCANNER_OUTPUT_DIR:-$PROJECT_DIR/AllScreenersResults}"

cd "$PROJECT_DIR"
mkdir -p "$OUTPUT_DIR"

if [[ ! -x "$VENV_DIR/bin/market-scanner" ]]; then
  echo "market-scanner is not installed in $VENV_DIR" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

exec "$VENV_DIR/bin/market-scanner" scan \
  --provider "${MARKET_SCANNER_PROVIDER:-alpaca}" \
  --output-dir "$OUTPUT_DIR"
