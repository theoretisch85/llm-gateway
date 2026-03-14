#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
TOKEN="${API_BEARER_TOKEN:-}"
ATTEMPTS="${READY_ATTEMPTS:-30}"
SLEEP_SECONDS="${READY_SLEEP_SECONDS:-2}"

if [[ -z "$TOKEN" ]]; then
  echo "Fehler: API_BEARER_TOKEN ist nicht gesetzt."
  exit 1
fi

for _ in $(seq 1 "$ATTEMPTS"); do
  if curl -fsS "$BASE_URL/internal/health" \
    -H "Authorization: Bearer $TOKEN" \
    >/dev/null; then
    exit 0
  fi
  sleep "$SLEEP_SECONDS"
done

echo "Readiness-Check gegen $BASE_URL/internal/health ist fehlgeschlagen."
exit 1
