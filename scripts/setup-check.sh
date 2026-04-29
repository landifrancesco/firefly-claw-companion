#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
SECRETS_DIR="${PROJECT_ROOT}/secrets"

echo "Checking setup status..."
echo ""

missing=0

check_file() {
  local path="$1"
  local name="$2"
  if [[ -f "${path}" && -s "${path}" ]]; then
    echo "ok: ${name}"
  else
    echo "missing: ${name}"
    missing=$((missing + 1))
  fi
}

check_file "${SECRETS_DIR}/firefly_access_token.txt" "Firefly access token"
check_file "${SECRETS_DIR}/telegram_bot_token.txt" "Telegram bot token"
check_file "${PROJECT_ROOT}/.env" ".env configuration file"
check_file "${PROJECT_ROOT}/config.example.json" "PicoClaw config example"

echo ""
if [[ ${missing} -eq 0 ]]; then
  echo "Setup is complete. Ready to run:"
  echo "  docker compose up -d --build"
else
  echo "Setup incomplete. Run this first:"
  echo "  docker compose run --rm setup"
fi
