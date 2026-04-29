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

if [[ "${MODE}" == "--runtime" ]]; then
  port="${PICOCLAW_PORT:-18790}"
  curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null \
    || curl -fsS "http://127.0.0.1:${port}/health" >/dev/null \
    || curl -fsS "http://127.0.0.1:${port}/" >/dev/null \
    || fail "PicoClaw gateway is unreachable on 127.0.0.1:${port}"
  if ! "${PYTHON_BIN}" -m firefly_companion.cli health >/dev/null 2>&1; then
    echo "verify_setup: warning: Firefly bridge health command failed" >&2
  fi
else
  [[ -f "${ROOT_DIR}/Dockerfile" ]] || fail "Dockerfile missing"
  [[ -f "${ROOT_DIR}/docker-compose.yml" ]] || fail "docker-compose.yml missing"
  [[ -f "${ROOT_DIR}/config.example.json" ]] || fail "config.example.json missing"
  [[ -f "${ROOT_DIR}/scripts/setup_wizard.py" ]] || fail "scripts/setup_wizard.py missing"
  [[ -f "${ROOT_DIR}/workspace/tools/firefly-bridge" ]] || fail "workspace/tools/firefly-bridge missing"
  [[ -f "${ROOT_DIR}/workspace/config/mappings.yml.example" ]] || fail "workspace/config/mappings.yml.example missing"
  [[ -f "${ROOT_DIR}/workspace/config/policy.yml" ]] || fail "workspace/config/policy.yml missing"

  "${PYTHON_BIN}" -m compileall "${ROOT_DIR}/src" "${ROOT_DIR}/scripts" >/dev/null
fi

echo "verify_setup: ok" >&2
