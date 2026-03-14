#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Fehler: $ENV_FILE wurde nicht gefunden."
  exit 1
fi

NEW_TOKEN="$("$ROOT_DIR/scripts/generate_token.sh")"
TMP_FILE="$(mktemp)"

awk -v new_token="$NEW_TOKEN" '
  BEGIN { updated = 0 }
  /^API_BEARER_TOKEN=/ {
    print "API_BEARER_TOKEN=" new_token
    updated = 1
    next
  }
  { print }
  END {
    if (updated == 0) {
      print "API_BEARER_TOKEN=" new_token
    }
  }
' "$ENV_FILE" > "$TMP_FILE"

mv "$TMP_FILE" "$ENV_FILE"
chmod 640 "$ENV_FILE"
chown llmgateway:llmgateway "$ENV_FILE" 2>/dev/null || true

if systemctl is-active --quiet llm-gateway; then
  systemctl restart llm-gateway
fi

echo "API_BEARER_TOKEN wurde rotiert."
echo "Neuen Token lokal verwenden:"
echo "$NEW_TOKEN"
