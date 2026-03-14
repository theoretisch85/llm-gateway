#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
OUTPUT_FILE="$ROOT_DIR/deploy/continue.config.local.yaml"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Fehler: $ENV_FILE wurde nicht gefunden."
  exit 1
fi

TOKEN="$(sed -n 's/^API_BEARER_TOKEN=//p' "$ENV_FILE")"

if [[ -z "$TOKEN" ]]; then
  echo "Fehler: API_BEARER_TOKEN ist in $ENV_FILE nicht gesetzt."
  exit 1
fi

cat > "$OUTPUT_FILE" <<EOF
name: llm-gateway
version: 0.0.1
schema: v1

models:
  - name: qwen2.5-coder-local
    provider: openai
    model: qwen2.5-coder
    apiBase: http://127.0.0.1:8000/v1
    apiKey: $TOKEN
EOF

chmod 600 "$OUTPUT_FILE"

echo "Continue-Konfiguration geschrieben:"
echo "$OUTPUT_FILE"
