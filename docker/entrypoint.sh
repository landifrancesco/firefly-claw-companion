#!/usr/bin/env bash
set -euo pipefail

umask 027

PICOCLAW_HOME="${PICOCLAW_HOME:-/home/picoclaw}"
PICOCLAW_CONFIG_DIR="${PICOCLAW_CONFIG_DIR:-${PICOCLAW_HOME}/.picoclaw}"
PICOCLAW_WORKSPACE="${PICOCLAW_WORKSPACE:-${PICOCLAW_CONFIG_DIR}/workspace}"
BUNDLE_ROOT="${BUNDLE_ROOT:-/opt/firefly-picoclaw/workspace}"
BIN_ROOT="${BIN_ROOT:-/opt/firefly-picoclaw/bin}"
RUNTIME_PORT="${PICOCLAW_PORT:-18790}"
RUNTIME_HOST="${PICOCLAW_GATEWAY_HOST:-127.0.0.1}"
VERIFY_ON_BOOT="${FIREFLY_RUNTIME_VERIFY_ON_BOOT:-true}"
ENTRYPOINT_BUILD_MARKER="config-scrub-v5"

echo "firefly-picoclaw entrypoint ${ENTRYPOINT_BUILD_MARKER}" >&2

read_secret() {
  local variable_name="$1"
  local fallback_file="${2:-}"
  local file_variable="${variable_name}_FILE"
  local direct_value="${!variable_name:-}"
  local file_value="${!file_variable:-}"
  local candidate_file=""

  if [[ -n "${direct_value}" ]]; then
    printf '%s' "${direct_value}"
    return 0
  fi

  if [[ -n "${file_value}" ]]; then
    candidate_file="${file_value}"
  elif [[ -n "${fallback_file}" ]]; then
    candidate_file="${fallback_file}"
  fi

  if [[ -n "${candidate_file}" && -f "${candidate_file}" ]]; then
    tr -d '\r' < "${candidate_file}" | sed -e 's/[[:space:]]*$//'
    return 0
  fi

  return 1
}

ensure_dir() {
  local path="$1"
  local mode="$2"
  mkdir -p "${path}"
  chmod "${mode}" "${path}"
}

secret_from_standard_locations() {
  local name="$1"
  local secret_name="$2"
  read_secret "${name}" "/run/secrets/${secret_name}" \
    || read_secret "${name}" "/run/host-secrets/${secret_name}.txt" \
    || true
}

archive_legacy_openclaw_state() {
  local legacy_dir="${PICOCLAW_HOME}/.openclaw"
  if [[ -d "${legacy_dir}" ]]; then
    local archive_dir="${PICOCLAW_HOME}/.openclaw.legacy.$(date -u +%Y%m%dT%H%M%SZ)"
    mv "${legacy_dir}" "${archive_dir}"
    echo "Archived incompatible OpenClaw state at ${archive_dir}; PicoClaw uses ${PICOCLAW_CONFIG_DIR}." >&2
  fi
}

scrub_picoclaw_resources() {
  local phase="${1:-manual}"
  python3 - "${phase}" <<'PY'
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

phase = sys.argv[1]
forbidden = {
    "telegram_bot_token",
    "openai_api_key",
    "anthropic_api_key",
    "openrouter_api_key",
    "groq_api_key",
    "google_api_key",
    "pdfapihub_api_key",
}

def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in forbidden}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value

config_paths = [
    Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / "config.json",
    Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / "config.json",
]

for p in config_paths:
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cleaned = scrub(data)
                p.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
                p.chmod(0o600)
                print(f"Scrubbed config {p} (phase={phase})")
        except Exception as e:
            print(f"Error scrubbing {p}: {e}", file=sys.stderr)

if yaml:
    security_paths = [
        Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / ".security.yml",
        Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / ".security.yml",
    ]
    for p in security_paths:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cleaned = scrub(data)
                    p.write_text(yaml.safe_dump(cleaned, sort_keys=False), encoding="utf-8")
                    p.chmod(0o600)
                    print(f"Scrubbed security {p} (phase={phase})")
            except Exception as e:
                print(f"Error scrubbing {p}: {e}", file=sys.stderr)
PY
}

unset_picoclaw_flat_secret_env() {
  unset TELEGRAM_BOT_TOKEN TELEGRAM_BOT_TOKEN_FILE
  unset OPENAI_API_KEY OPENAI_API_KEY_FILE
  unset ANTHROPIC_API_KEY ANTHROPIC_API_KEY_FILE
  unset OPENROUTER_API_KEY OPENROUTER_API_KEY_FILE
  unset GROQ_API_KEY GROQ_API_KEY_FILE
  unset GOOGLE_API_KEY GOOGLE_API_KEY_FILE
  unset PDFAPIHUB_API_KEY PDFAPIHUB_API_KEY_FILE
}

unset_picoclaw_render_secret_env() {
  unset PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN
  unset PICOCLAW_RENDER_OPENAI_API_KEY
  unset PICOCLAW_RENDER_ANTHROPIC_API_KEY
  unset PICOCLAW_RENDER_OPENROUTER_API_KEY
  unset PICOCLAW_RENDER_GROQ_API_KEY
  unset PICOCLAW_RENDER_GOOGLE_API_KEY
  unset PICOCLAW_RENDER_PDFAPIHUB_API_KEY
}

ensure_dir "${PICOCLAW_HOME}" 700
ensure_dir "${PICOCLAW_CONFIG_DIR}" 700
ensure_dir "${PICOCLAW_WORKSPACE}" 700
ensure_dir "${PICOCLAW_WORKSPACE}/config" 750
ensure_dir "${PICOCLAW_CONFIG_DIR}/logs" 750
ensure_dir "${PICOCLAW_CONFIG_DIR}/.security" 700

archive_legacy_openclaw_state
"${BIN_ROOT}/install_bundle_to_workspace.sh" "${BUNDLE_ROOT}" "${PICOCLAW_WORKSPACE}"

OPENAI_TOKEN="$(secret_from_standard_locations OPENAI_API_KEY openai_api_key)"
ANTHROPIC_TOKEN="$(secret_from_standard_locations ANTHROPIC_API_KEY anthropic_api_key)"
OPENROUTER_TOKEN="$(secret_from_standard_locations OPENROUTER_API_KEY openrouter_api_key)"
GROQ_TOKEN="$(secret_from_standard_locations GROQ_API_KEY groq_api_key)"
GOOGLE_TOKEN="$(secret_from_standard_locations GOOGLE_API_KEY google_api_key)"
TELEGRAM_TOKEN="$(secret_from_standard_locations TELEGRAM_BOT_TOKEN telegram_bot_token)"
FIREFLY_TOKEN="$(secret_from_standard_locations FIREFLY_ACCESS_TOKEN firefly_access_token)"
PDFAPIHUB_TOKEN="$(secret_from_standard_locations PDFAPIHUB_API_KEY pdfapihub_api_key)"

unset_picoclaw_flat_secret_env

if [[ -z "${FIREFLY_TOKEN}" ]]; then
  echo "Error: FIREFLY_ACCESS_TOKEN is required." >&2
  exit 12
fi

export PICOCLAW_HOME PICOCLAW_CONFIG_DIR PICOCLAW_WORKSPACE RUNTIME_PORT RUNTIME_HOST
export FIREFLY_ACCESS_TOKEN="${FIREFLY_TOKEN}"
export FIREFLY_ACCESS_TOKEN_FILE="${PICOCLAW_CONFIG_DIR}/.security/firefly_access_token"
export PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"
export PICOCLAW_RENDER_OPENAI_API_KEY="${OPENAI_TOKEN}"
export PICOCLAW_RENDER_ANTHROPIC_API_KEY="${ANTHROPIC_TOKEN}"
export PICOCLAW_RENDER_OPENROUTER_API_KEY="${OPENROUTER_TOKEN}"
export PICOCLAW_RENDER_GROQ_API_KEY="${GROQ_TOKEN}"
export PICOCLAW_RENDER_GOOGLE_API_KEY="${GOOGLE_TOKEN}"
export PICOCLAW_RENDER_PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"

# Generate dynamic config files
python3 <<'PY'
import json, os, yaml
from pathlib import Path
config_dir = Path(os.environ["PICOCLAW_CONFIG_DIR"])
workspace = Path(os.environ["PICOCLAW_WORKSPACE"])
security_dir = config_dir / ".security"
security_dir.mkdir(parents=True, exist_ok=True)

telegram_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
model_name = os.getenv("PICOCLAW_DEFAULT_MODEL_NAME", "gemini")
model_string = os.getenv("PICOCLAW_DEFAULT_MODEL", "gemini/gemini-2.5-flash")

config = {
    "version": 2,
    "agents": {"defaults": {"workspace": str(workspace), "model_name": model_name}},
    "model_list": [{"model_name": model_name, "model": model_string}],
    "channels": {"telegram": {"enabled": True, "allow_from": [telegram_owner] if telegram_owner else []}},
    "tools": {
        "spawn": {"enabled": True}, "subagent": {"enabled": True}, "message": {"enabled": True},
        "list_dir": {"enabled": True}, "read_file": {"enabled": True}, "write_file": {"enabled": True},
        "edit_file": {"enabled": True},
        "mcp": {
            "enabled": True,
            "servers": {
                "firefly-bridge": {
                    "enabled": True,
                    "command": "/opt/firefly-picoclaw/bin/firefly-bridge",
                    "env": {k: os.getenv(k, "") for k in os.environ if k.startswith("FIREFLY_")}
                }
            }
        }
    },
    "gateway": {"host": os.getenv("RUNTIME_HOST", "127.0.0.1"), "port": int(os.getenv("RUNTIME_PORT", "18790"))}
}
(config_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")

security = {
    "channels": {"telegram": {"token": os.getenv("PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN", "")}},
    "model_list": {model_name: {"api_keys": [os.getenv(f"PICOCLAW_RENDER_{model_name.upper()}_API_KEY", "")]}}
}
(config_dir / ".security.yml").write_text(yaml.safe_dump(security))
(security_dir / "firefly_access_token").write_text(os.getenv("FIREFLY_ACCESS_TOKEN", "") + "\n")
PY

cp "${PICOCLAW_CONFIG_DIR}/config.json" "${PICOCLAW_HOME}/config.json"
cp "${PICOCLAW_CONFIG_DIR}/.security.yml" "${PICOCLAW_HOME}/.security.yml"
scrub_picoclaw_resources "before-startup"

if [[ "${VERIFY_ON_BOOT}" == "true" ]]; then
  python3 -m firefly_companion.cli health >/dev/null 2>&1 || echo "Warning: Health check failed" >&2
fi

if [[ "$#" -eq 0 ]]; then set -- picoclaw gateway; fi

if [[ "$1" == "picoclaw" && "${2:-}" == "gateway" ]]; then
  TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}" python3 /opt/firefly-picoclaw/bin/token_expiry_reminder.py &
  if [[ "${TELEGRAM_ENABLED:-true}" == "true" ]]; then
    (
      export TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"
      export PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"
      export GOOGLE_API_KEY="${GOOGLE_TOKEN}"
      python3 /opt/firefly-picoclaw/bin/telegram_firefly_bot.py
    ) &
  fi
fi

scrub_picoclaw_resources "before-exec"

if [[ "$1" == "picoclaw" ]]; then
  unset_picoclaw_flat_secret_env
  unset_picoclaw_render_secret_env
fi

exec "$@"
