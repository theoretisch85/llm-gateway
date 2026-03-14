#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"

if [[ ! -x "$ROOT_DIR/.venv/bin/uvicorn" ]]; then
  echo "Fehler: .venv fehlt oder uvicorn ist nicht installiert."
  echo "Bitte zuerst ausfuehren:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  exit 1
fi

exec "$ROOT_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000 --reload
