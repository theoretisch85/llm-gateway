#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

MI50_SSH_HOST="${MI50_SSH_HOST:-}"
MI50_SSH_USER="${MI50_SSH_USER:-}"
MI50_SSH_PORT="${MI50_SSH_PORT:-22}"
MI50_RESTART_COMMAND="${MI50_RESTART_COMMAND:-sudo systemctl restart llama.cpp}"
MI50_STATUS_COMMAND="${MI50_STATUS_COMMAND:-sudo systemctl status llama.cpp --no-pager}"

if [[ -z "$MI50_SSH_HOST" || -z "$MI50_SSH_USER" ]]; then
  echo "Fehler: MI50_SSH_HOST und MI50_SSH_USER muessen in .env oder als Umgebungsvariablen gesetzt sein."
  exit 1
fi

echo "Verbinde zu ${MI50_SSH_USER}@${MI50_SSH_HOST}:${MI50_SSH_PORT}"
ssh -p "$MI50_SSH_PORT" "${MI50_SSH_USER}@${MI50_SSH_HOST}" "$MI50_RESTART_COMMAND && $MI50_STATUS_COMMAND"
