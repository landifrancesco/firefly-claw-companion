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

if [[ -z "${FIREFLY_TOKEN}" ]]; then
  echo "Error: FIREFLY_ACCESS_TOKEN is required via env, *_FILE, Docker secret, or /run/host-secrets/firefly_access_token.txt." >&2
  exit 12
fi

export PICOCLAW_CONFIG_DIR PICOCLAW_WORKSPACE RUNTIME_PORT RUNTIME_HOST
export PICOCLAW_DEFAULT_MODEL_NAME="${PICOCLAW_DEFAULT_MODEL_NAME:-gemini}"
export PICOCLAW_DEFAULT_MODEL="${PICOCLAW_DEFAULT_MODEL:-gemini/gemini-2.5-flash}"
export PICOCLAW_LOG_LEVEL="${PICOCLAW_LOG_LEVEL:-info}"
export FIREFLY_BASE_URL="${FIREFLY_BASE_URL:-http://firefly:8080}"
export FIREFLY_API_BASE_PATH="${FIREFLY_API_BASE_PATH:-/api/v1}"
export FIREFLY_TIMEOUT_SECONDS="${FIREFLY_TIMEOUT_SECONDS:-15}"
export FIREFLY_REQUEST_RETRIES="${FIREFLY_REQUEST_RETRIES:-2}"
export FIREFLY_RETRY_BACKOFF_SECONDS="${FIREFLY_RETRY_BACKOFF_SECONDS:-0.5}"
export FIREFLY_VERIFY_TLS="${FIREFLY_VERIFY_TLS:-true}"
export FIREFLY_FORCE_CONNECTION_CLOSE="${FIREFLY_FORCE_CONNECTION_CLOSE:-true}"
export FIREFLY_DEFAULT_DRY_RUN="${FIREFLY_DEFAULT_DRY_RUN:-true}"
export FIREFLY_HIGH_VALUE_THRESHOLD="${FIREFLY_HIGH_VALUE_THRESHOLD:-250.00}"
export FIREFLY_DEDUPE_WINDOW_DAYS="${FIREFLY_DEDUPE_WINDOW_DAYS:-7}"
export FIREFLY_ALLOW_DELETE="${FIREFLY_ALLOW_DELETE:-false}"
export FIREFLY_ACCESS_TOKEN_EXPIRES_ON="${FIREFLY_ACCESS_TOKEN_EXPIRES_ON:-}"
export FIREFLY_TOKEN_REMINDER_DAYS="${FIREFLY_TOKEN_REMINDER_DAYS:-60,30,14,7,3,1}"
export FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS="${FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS:-21600}"
export FIREFLY_MAPPINGS_PATH="${FIREFLY_MAPPINGS_PATH:-${PICOCLAW_WORKSPACE}/config/mappings.yml}"
export FIREFLY_POLICY_PATH="${FIREFLY_POLICY_PATH:-${PICOCLAW_WORKSPACE}/config/policy.yml}"
export TELEGRAM_ENABLED="${TELEGRAM_ENABLED:-true}"
export PICOCLAW_TELEGRAM_CHANNEL_ENABLED="${PICOCLAW_TELEGRAM_CHANNEL_ENABLED:-false}"
export TELEGRAM_OWNER_ID="${TELEGRAM_OWNER_ID:-${TELEGRAM_TARGET_ID:-}}"
export TELEGRAM_TARGET_ID="${TELEGRAM_TARGET_ID:-${TELEGRAM_OWNER_ID:-}}"
export FIREFLY_CHAT_LANGUAGE="${FIREFLY_CHAT_LANGUAGE:-auto}"
export FIREFLY_ACCESS_TOKEN="${FIREFLY_TOKEN}"
export FIREFLY_ACCESS_TOKEN_FILE="${PICOCLAW_CONFIG_DIR}/.security/firefly_access_token"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"
export OPENAI_API_KEY="${OPENAI_TOKEN}"
export ANTHROPIC_API_KEY="${ANTHROPIC_TOKEN}"
export OPENROUTER_API_KEY="${OPENROUTER_TOKEN}"
export GROQ_API_KEY="${GROQ_TOKEN}"
export GOOGLE_API_KEY="${GOOGLE_TOKEN}"
export PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"

if [[ "${TELEGRAM_ENABLED}" == "true" && -z "${TELEGRAM_TOKEN}" ]]; then
  echo "Error: TELEGRAM_ENABLED=true but no Telegram bot token was provided." >&2
  exit 14
fi

if [[ "${TELEGRAM_ENABLED}" == "true" && -z "${TELEGRAM_OWNER_ID}" ]]; then
  echo "Error: TELEGRAM_OWNER_ID is required when Telegram is enabled." >&2
  exit 15
fi

if [[ ! -f "${PICOCLAW_CONFIG_DIR}/config.json" ]]; then
  picoclaw onboard >/dev/null 2>&1 || true
fi

python3 <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

config_dir = Path(os.environ["PICOCLAW_CONFIG_DIR"])
workspace = Path(os.environ["PICOCLAW_WORKSPACE"])
security_dir = config_dir / ".security"
security_dir.mkdir(parents=True, exist_ok=True)

telegram_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
telegram_enabled = os.getenv("PICOCLAW_TELEGRAM_CHANNEL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

model_name = os.getenv("PICOCLAW_DEFAULT_MODEL_NAME", "gemini")
model_string = os.getenv("PICOCLAW_DEFAULT_MODEL", "gemini/gemini-2.5-flash")

# Determine auth method and API key reference based on provider.
auth_method = "api_key"
api_key_ref = None

if model_name == "codex":
    auth_method = "oauth"
elif model_name == "groq":
    auth_method = "api_key"
    api_key_ref = "ref:groq_api_key"
elif model_name == "openai":
    auth_method = "api_key"
    api_key_ref = "ref:openai_api_key"
elif model_name == "anthropic":
    auth_method = "api_key"
    api_key_ref = "ref:anthropic_api_key"
elif model_name == "openrouter":
    auth_method = "api_key"
    api_key_ref = "ref:openrouter_api_key"
elif model_name in {"google", "gemini"}:
    auth_method = "api_key"
    api_key_ref = "ref:google_api_key"

model_config = {
    "model_name": model_name,
    "model": model_string,
    "auth_method": auth_method,
}
if api_key_ref:
    model_config["api_key"] = api_key_ref

config = {
    "agents": {
        "defaults": {
            "workspace": str(workspace),
            "restrict_to_workspace": True,
            "model_name": model_name,
            "max_tokens": 8192,
            "context_window": 131072,
            "temperature": 0.7,
            "max_tool_iterations": 20,
        }
    },
    "model_list": [model_config],
    "channels": {
        "telegram": {
            "enabled": telegram_enabled,
            "token": "ref:telegram_bot_token",
            "allow_from": [telegram_owner] if telegram_owner else [],
            "use_markdown_v2": False,
            "streaming": {"enabled": True},
        }
    },
    "tools": {
        "exec": {"enabled": False},
        "cron": {"enabled": False},
        "web": {"enabled": False},
        "i2c": {"enabled": False},
        "serial": {"enabled": False},
        "send_tts": {"enabled": False},
        "skills": {"enabled": False},
        "find_skills": {"enabled": False},
        "install_skill": {"enabled": False},
        "spawn": {"enabled": True},
        "subagent": {"enabled": True},
        "message": {"enabled": True},
        "list_dir": {"enabled": True},
        "read_file": {"enabled": True, "mode": "bytes"},
        "write_file": {"enabled": True},
        "edit_file": {"enabled": True},
        "append_file": {"enabled": True},
        "web_fetch": {"enabled": False},
        "media_cleanup": {"enabled": True, "max_age_minutes": 30, "interval_minutes": 5},
        "mcp": {
            "enabled": True,
            "servers": {
                "firefly-bridge": {
                    "enabled": True,
                    "command": "/opt/firefly-picoclaw/bin/firefly-bridge",
                    "env": {
                        "FIREFLY_BASE_URL": os.getenv("FIREFLY_BASE_URL", ""),
                        "FIREFLY_API_BASE_PATH": os.getenv("FIREFLY_API_BASE_PATH", ""),
                        "FIREFLY_TIMEOUT_SECONDS": os.getenv("FIREFLY_TIMEOUT_SECONDS", ""),
                        "FIREFLY_REQUEST_RETRIES": os.getenv("FIREFLY_REQUEST_RETRIES", ""),
                        "FIREFLY_RETRY_BACKOFF_SECONDS": os.getenv("FIREFLY_RETRY_BACKOFF_SECONDS", ""),
                        "FIREFLY_VERIFY_TLS": os.getenv("FIREFLY_VERIFY_TLS", ""),
                        "FIREFLY_FORCE_CONNECTION_CLOSE": os.getenv("FIREFLY_FORCE_CONNECTION_CLOSE", ""),
                        "FIREFLY_DEFAULT_DRY_RUN": os.getenv("FIREFLY_DEFAULT_DRY_RUN", ""),
                        "FIREFLY_HIGH_VALUE_THRESHOLD": os.getenv("FIREFLY_HIGH_VALUE_THRESHOLD", ""),
                        "FIREFLY_DEDUPE_WINDOW_DAYS": os.getenv("FIREFLY_DEDUPE_WINDOW_DAYS", ""),
                        "FIREFLY_ALLOW_DELETE": os.getenv("FIREFLY_ALLOW_DELETE", ""),
                        "FIREFLY_MAPPINGS_PATH": os.getenv("FIREFLY_MAPPINGS_PATH", ""),
                        "FIREFLY_POLICY_PATH": os.getenv("FIREFLY_POLICY_PATH", ""),
                        "FIREFLY_ACCESS_TOKEN_FILE": os.getenv("FIREFLY_ACCESS_TOKEN_FILE", ""),
                    },
                }
            },
        },
    },
    "hooks": {"enabled": True},
    "heartbeat": {"enabled": True, "interval": 30},
    "gateway": {
        "host": os.getenv("RUNTIME_HOST", "127.0.0.1"),
        "port": int(os.getenv("RUNTIME_PORT", "18790")),
        "log_level": os.getenv("PICOCLAW_LOG_LEVEL", "info"),
    },
}

config_path = config_dir / "config.json"
config_path.write_text(json.dumps(config, indent=2, sort_keys=False) + "\n", encoding="utf-8")
config_path.chmod(0o600)

security = {
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "openrouter_api_key": os.getenv("OPENROUTER_API_KEY", ""),
    "groq_api_key": os.getenv("GROQ_API_KEY", ""),
    "google_api_key": os.getenv("GOOGLE_API_KEY", ""),
    "pdfapihub_api_key": os.getenv("PDFAPIHUB_API_KEY", ""),
}
(config_dir / ".security.yml").write_text(yaml.safe_dump(security, sort_keys=False), encoding="utf-8")
(config_dir / ".security.yml").chmod(0o600)

token_path = security_dir / "firefly_access_token"
token_path.write_text(os.getenv("FIREFLY_ACCESS_TOKEN", "") + "\n", encoding="utf-8")
token_path.chmod(0o600)
PY

# PicoClaw 0.2.x reads config from $HOME/config.json and $HOME/.security.yml,
# then migrates them in place to its current schema. Seed those root paths from
# the mounted .picoclaw files on every boot so env changes are applied.
cp "${PICOCLAW_CONFIG_DIR}/config.json" "${PICOCLAW_HOME}/config.json"
cp "${PICOCLAW_CONFIG_DIR}/.security.yml" "${PICOCLAW_HOME}/.security.yml"
chmod 0600 "${PICOCLAW_HOME}/config.json" "${PICOCLAW_HOME}/.security.yml"

# Force schema migration before gateway startup, then replace migrated secret
# placeholders with actual values. PicoClaw v3 stores secrets in a nested shape
# matching config.json, not in the flat legacy file generated above.
picoclaw status >/dev/null 2>&1 || true

python3 <<'PY'
from __future__ import annotations

import os
from pathlib import Path

import yaml

home = Path(os.environ["PICOCLAW_HOME"])
security_path = home / ".security.yml"
security = yaml.safe_load(security_path.read_text(encoding="utf-8")) if security_path.exists() else {}
if not isinstance(security, dict):
    security = {}

channel_list = security.setdefault("channel_list", {})
telegram = channel_list.setdefault("telegram", {})
telegram_settings = telegram.setdefault("settings", {})
telegram_settings["token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")

model_name = os.getenv("PICOCLAW_DEFAULT_MODEL_NAME", "gemini")
model_secrets = {
    "groq": os.getenv("GROQ_API_KEY", ""),
    "openai": os.getenv("OPENAI_API_KEY", ""),
    "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
    "openrouter": os.getenv("OPENROUTER_API_KEY", ""),
    "google": os.getenv("GOOGLE_API_KEY", ""),
    "gemini": os.getenv("GOOGLE_API_KEY", ""),
}
if model_name in model_secrets:
    model_list = security.setdefault("model_list", {})
    model_list[f"{model_name}:0"] = {"api_keys": [model_secrets[model_name]]}

security_path.write_text(yaml.safe_dump(security, sort_keys=False), encoding="utf-8")
security_path.chmod(0o600)
PY

if [[ "${VERIFY_ON_BOOT}" == "true" ]]; then
  if ! python3 -m firefly_companion.cli health >/dev/null 2>&1; then
    echo "Warning: Firefly bridge health check failed during bootstrap." >&2
  fi
fi

if [[ "$#" -eq 0 ]]; then
  set -- picoclaw gateway
fi

if [[ "$1" == "picoclaw" && "${2:-}" == "gateway" ]]; then
  python3 /opt/firefly-picoclaw/bin/token_expiry_reminder.py &
  if [[ "${TELEGRAM_ENABLED}" == "true" ]]; then
    python3 /opt/firefly-picoclaw/bin/telegram_firefly_bot.py &
  fi
fi

exec "$@"
