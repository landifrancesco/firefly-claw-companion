#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-static}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"

fail() {
  echo "$1" >&2
  exit 1
}

health_headers=()
if [[ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  health_headers=(-H "Authorization: Bearer ${OPENCLAW_GATEWAY_TOKEN}")
fi

if [[ "${MODE}" == "--runtime" ]]; then
  curl -fsS "${health_headers[@]}" "http://127.0.0.1:${OPENCLAW_PORT:-18789}/health" >/dev/null || fail "OpenClaw gateway health endpoint unreachable"
  if ! "${PYTHON_BIN}" -m firefly_companion.cli health >/dev/null 2>&1; then
    echo "verify_setup: warning: Firefly bridge health command failed" >&2
  fi
else
  [[ -f "${ROOT_DIR}/Dockerfile" ]] || fail "Dockerfile missing"
  [[ -f "${ROOT_DIR}/docker-compose.yml" ]] || fail "docker-compose.yml missing"
  [[ -f "${ROOT_DIR}/scripts/setup_wizard.py" ]] || fail "scripts/setup_wizard.py missing"
  [[ -f "${ROOT_DIR}/workspace/skills/firefly_companion/SKILL.md" ]] || fail "Local companion skill missing"
  [[ -f "${ROOT_DIR}/workspace/skills/upstream_firefly_iii/SKILL.md" ]] || fail "Vendored upstream skill missing"
  [[ -f "${ROOT_DIR}/workspace/config/mappings.yml" ]] || fail "workspace/config/mappings.yml missing"
  [[ -f "${ROOT_DIR}/workspace/config/policy.yml" ]] || fail "workspace/config/policy.yml missing"

  "${PYTHON_BIN}" -m compileall "${ROOT_DIR}/src" "${ROOT_DIR}/scripts" >/dev/null
fi

echo "verify_setup: ok" >&2
