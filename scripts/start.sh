#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -f .env ]]; then
  echo "Missing $ROOT/.env. Run scripts/register_gateway.sh first." >&2
  exit 1
fi
PYTHON_BIN="${JARVIS_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3.11 >/dev/null 2>&1; then PYTHON_BIN="$(command -v python3.11)"; else PYTHON_BIN="$(command -v python3)"; fi
fi
TARGET_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ -x .venv/bin/python ]]; then
  VENV_VERSION="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "$VENV_VERSION" != "$TARGET_VERSION" ]]; then rm -rf .venv; fi
fi
if [[ ! -x .venv/bin/python ]]; then "$PYTHON_BIN" -m venv .venv; fi
STAMP=.venv/.requirements.sha256
HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
if [[ ! -f "$STAMP" || "$(cat "$STAMP")" != "$HASH" ]]; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  printf '%s' "$HASH" > "$STAMP"
fi
set -a; source .env; set +a
exec .venv/bin/uvicorn server:app --host "${JARVIS_HOST:-127.0.0.1}" --port "${JARVIS_PORT:-8787}"
