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
ENTRYPOINT_BUILD_MARKER="config-scrub-v10"

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


def find_forbidden(value, path="$"):
    matches = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}"
            if key in forbidden:
                matches.append(current)
            matches.extend(find_forbidden(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(find_forbidden(item, f"{path}[{index}]"))
    return matches


config_paths = [
    Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / "config.json",
    Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / "config.json",
]

for p in config_paths:
    if not p.exists():
        continue
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Error reading PicoClaw config {p}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"Invalid PicoClaw config (expected JSON object): {p}", file=sys.stderr)
        sys.exit(1)
    cleaned = scrub(data)
    remaining = find_forbidden(cleaned)
    if remaining:
        print(f"Refusing to continue: {p} still has forbidden keys after scrub: {remaining}", file=sys.stderr)
        sys.exit(1)
    p.write_text(json.dumps(cleaned, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    p.chmod(0o600)
    print(f"Scrubbed config {p} (phase={phase})")

if yaml:
    security_paths = [
        Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / ".security.yml",
        Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / ".security.yml",
    ]
    for p in security_paths:
        if not p.exists():
            continue
        try:
            raw_sec = p.read_text(encoding="utf-8")
            data = yaml.safe_load(raw_sec)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Error reading security file {p}: {exc}", file=sys.stderr)
            sys.exit(1)
        except yaml.YAMLError as exc:
            print(f"Invalid YAML security file {p}: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print(f"Invalid security file (expected YAML mapping): {p}", file=sys.stderr)
            sys.exit(1)
        cleaned = scrub(data)
        remaining = find_forbidden(cleaned)
        if remaining:
            print(f"Refusing to continue: {p} still has forbidden keys after scrub: {remaining}", file=sys.stderr)
            sys.exit(1)
        p.write_text(yaml.safe_dump(cleaned, sort_keys=False), encoding="utf-8")
        p.chmod(0o600)
        print(f"Scrubbed security {p} (phase={phase})")
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

if [[ -z "${FIREFLY_TOKEN}" ]]; then
  echo "Error: FIREFLY_ACCESS_TOKEN is required via env, *_FILE, Docker secret, or /run/host-secrets/firefly_access_token.txt." >&2
  exit 12
fi

export PICOCLAW_HOME PICOCLAW_CONFIG_DIR PICOCLAW_WORKSPACE RUNTIME_PORT RUNTIME_HOST
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

required_model_secret=""
required_model_secret_name=""
case "${PICOCLAW_DEFAULT_MODEL_NAME}" in
  gemini|google)
    required_model_secret="${GOOGLE_TOKEN}"
    required_model_secret_name="GOOGLE_API_KEY"
    ;;
  groq)
    required_model_secret="${GROQ_TOKEN}"
    required_model_secret_name="GROQ_API_KEY"
    ;;
  openai)
    required_model_secret="${OPENAI_TOKEN}"
    required_model_secret_name="OPENAI_API_KEY"
    ;;
  anthropic)
    required_model_secret="${ANTHROPIC_TOKEN}"
    required_model_secret_name="ANTHROPIC_API_KEY"
    ;;
  openrouter)
    required_model_secret="${OPENROUTER_TOKEN}"
    required_model_secret_name="OPENROUTER_API_KEY"
    ;;
  codex)
    required_model_secret_name=""
    ;;
  *)
    echo "Error: unsupported PICOCLAW_DEFAULT_MODEL_NAME=${PICOCLAW_DEFAULT_MODEL_NAME}." >&2
    echo "Use one of: gemini, groq, openai, anthropic, openrouter, codex." >&2
    exit 16
    ;;
esac

if [[ -n "${required_model_secret_name}" && -z "${required_model_secret}" ]]; then
  echo "Error: PICOCLAW_DEFAULT_MODEL_NAME=${PICOCLAW_DEFAULT_MODEL_NAME} requires ${required_model_secret_name}, but it is empty." >&2
  echo "Set ${required_model_secret_name} in .env or secrets/, or choose a provider whose key is configured." >&2
  echo "For your current .env, either add GOOGLE_API_KEY for Gemini or set PICOCLAW_DEFAULT_MODEL_NAME=groq and PICOCLAW_DEFAULT_MODEL=groq/openai/gpt-oss-20b." >&2
  exit 16
fi

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

unset_picoclaw_flat_secret_env

export PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"
export PICOCLAW_RENDER_OPENAI_API_KEY="${OPENAI_TOKEN}"
export PICOCLAW_RENDER_ANTHROPIC_API_KEY="${ANTHROPIC_TOKEN}"
export PICOCLAW_RENDER_OPENROUTER_API_KEY="${OPENROUTER_TOKEN}"
export PICOCLAW_RENDER_GROQ_API_KEY="${GROQ_TOKEN}"
export PICOCLAW_RENDER_GOOGLE_API_KEY="${GOOGLE_TOKEN}"
export PICOCLAW_RENDER_PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"

# Generate dynamic config files (aligned with setup_wizard.py and historical 6a85fa0 tree)
python3 <<'PY'
from __future__ import annotations

import json
import os

import yaml
from pathlib import Path

config_dir = Path(os.environ["PICOCLAW_CONFIG_DIR"])
workspace = Path(os.environ["PICOCLAW_WORKSPACE"])
security_dir = config_dir / ".security"
security_dir.mkdir(parents=True, exist_ok=True)

telegram_owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
raw_tg_chan = os.getenv("PICOCLAW_TELEGRAM_CHANNEL_ENABLED", "false").strip().lower()
telegram_channel_enabled = raw_tg_chan in {"1", "true", "yes", "on"}
model_name = os.getenv("PICOCLAW_DEFAULT_MODEL_NAME", "gemini")
model_string = os.getenv("PICOCLAW_DEFAULT_MODEL", "gemini/gemini-2.5-flash")

model_config = {
    "model_name": model_name,
    "model": model_string,
}
if model_name == "codex":
    model_config["auth_method"] = "oauth"

config = {
    "version": 2,
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
            "enabled": telegram_channel_enabled,
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

# Model keys for .security.yml use PICOCLAW_RENDER_* (flat env cleared before PicoClaw exec)
model_key_env = f"PICOCLAW_RENDER_{model_name.upper()}_API_KEY"
if model_name in {"gemini", "google"}:
    model_key_env = "PICOCLAW_RENDER_GOOGLE_API_KEY"

model_key = os.getenv(model_key_env, "")
security = {
    "channels": {"telegram": {"token": os.getenv("PICOCLAW_RENDER_TELEGRAM_BOT_TOKEN", "")}},
    "model_list": {
        model_name: {"api_keys": [model_key]} if model_key else {}
    }
}
if model_key:
    security["model_list"][f"{model_name}:0"] = {"api_keys": [model_key]}

security_yml = config_dir / ".security.yml"
security_yml.write_text(yaml.safe_dump(security, sort_keys=False), encoding="utf-8")
security_yml.chmod(0o600)

token_path = security_dir / "firefly_access_token"
token_path.write_text(os.getenv("FIREFLY_ACCESS_TOKEN", "") + "\n", encoding="utf-8")
token_path.chmod(0o600)
PY

# PicoClaw reads $PICOCLAW_HOME/config.json and migrates in place — keep both paths in sync each boot.
cp "${PICOCLAW_CONFIG_DIR}/config.json" "${PICOCLAW_HOME}/config.json"
cp "${PICOCLAW_CONFIG_DIR}/.security.yml" "${PICOCLAW_HOME}/.security.yml"
chmod 0600 "${PICOCLAW_HOME}/config.json" "${PICOCLAW_HOME}/.security.yml"
scrub_picoclaw_resources "before-startup"

# Strip legacy flat secret keys only at JSON root (1c4a252 companion path; newer PicoClaw rejects these).
python3 <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

forbidden = {
    "telegram_bot_token",
    "openai_api_key",
    "anthropic_api_key",
    "openrouter_api_key",
    "groq_api_key",
    "google_api_key",
    "pdfapihub_api_key",
}

paths = [
    Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / "config.json",
    Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / "config.json",
]

for path in paths:
    if not path.exists():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        continue
    if not isinstance(payload, dict):
        continue
    changed = False
    for key in forbidden:
        if key in payload:
            payload.pop(key, None)
            changed = True
    if changed:
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        path.chmod(0o600)
PY

if [[ "${VERIFY_ON_BOOT}" == "true" ]]; then
  if ! python3 -m firefly_companion.cli health >/dev/null 2>&1; then
    echo "Warning: Firefly bridge health check failed during bootstrap." >&2
  fi
fi

if [[ "$#" -eq 0 ]]; then set -- picoclaw gateway; fi

if [[ "$1" == "picoclaw" && "${2:-}" == "gateway" ]]; then
  # Model keys for Python bots
  export TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN}"
  export PDFAPIHUB_API_KEY="${PDFAPIHUB_TOKEN}"
  export OPENAI_API_KEY="${OPENAI_TOKEN}"
  export ANTHROPIC_API_KEY="${ANTHROPIC_TOKEN}"
  export OPENROUTER_API_KEY="${OPENROUTER_TOKEN}"
  export GROQ_API_KEY="${GROQ_TOKEN}"
  export GOOGLE_API_KEY="${GOOGLE_TOKEN}"

  python3 <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

forbidden = {
    "telegram_bot_token",
    "openai_api_key",
    "anthropic_api_key",
    "openrouter_api_key",
    "groq_api_key",
    "google_api_key",
    "pdfapihub_api_key",
}

paths = [
    Path(os.getenv("PICOCLAW_CONFIG_DIR", "/home/picoclaw/.picoclaw")) / "config.json",
    Path(os.getenv("PICOCLAW_HOME", "/home/picoclaw")) / "config.json",
]

for path in paths:
    if not path.exists():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        continue
    if not isinstance(payload, dict):
        continue
    for key in forbidden:
        payload.pop(key, None)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
PY

  python3 /opt/firefly-picoclaw/bin/token_expiry_reminder.py &
  if [[ "${TELEGRAM_ENABLED:-true}" == "true" ]]; then
    python3 /opt/firefly-picoclaw/bin/telegram_firefly_bot.py &
  fi
fi

if [[ -f "${PICOCLAW_CONFIG_DIR}/config.json" ]]; then
  cp "${PICOCLAW_CONFIG_DIR}/config.json" "${PICOCLAW_HOME}/config.json"
  chmod 0600 "${PICOCLAW_HOME}/config.json"
fi

scrub_picoclaw_resources "before-exec"

if [[ "$1" == "picoclaw" ]]; then
  unset_picoclaw_flat_secret_env
  unset_picoclaw_render_secret_env
fi

exec "$@"
