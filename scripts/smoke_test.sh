#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
TOKEN="${API_BEARER_TOKEN:-dev-local-token}"
MODEL_NAME="${PUBLIC_MODEL_NAME:-devstral-q3}"

echo "[1/5] health"
curl -fsS "$BASE_URL/health"
echo
echo

echo "[2/5] internal health"
curl -fsS "$BASE_URL/internal/health" \
  -H "Authorization: Bearer $TOKEN"
echo
echo

echo "[3/5] models"
curl -fsS "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $TOKEN"
echo
echo

echo "[4/5] chat completion"
curl -fsS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"$MODEL_NAME"'",
    "messages": [
      {"role": "user", "content": "Reply with exactly the word OK."}
    ],
    "stream": false,
    "max_tokens": 16
  }'
echo
echo

echo "[5/5] metrics"
curl -fsS "$BASE_URL/internal/metrics" \
  -H "Authorization: Bearer $TOKEN"
echo
