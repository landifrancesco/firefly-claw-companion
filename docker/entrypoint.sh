#!/usr/bin/env bash
set -euo pipefail

umask 027

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/openclaw}"
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-${OPENCLAW_HOME}/.openclaw}"
OPENCLAW_WORKSPACE="${OPENCLAW_WORKSPACE:-/home/openclaw/workspace}"
BUNDLE_ROOT="${BUNDLE_ROOT:-/opt/firefly-openclaw/workspace}"
CONFIG_BUNDLE="${CONFIG_BUNDLE:-/opt/firefly-openclaw/config/openclaw.base.json5}"
BIN_ROOT="${BIN_ROOT:-/opt/firefly-openclaw/bin}"
RUNTIME_PORT="${OPENCLAW_PORT:-18789}"
RUNTIME_BIND="${OPENCLAW_BIND:-loopback}"
RUNTIME_MODEL="${OPENCLAW_DEFAULT_MODEL:-openai/gpt-5.4-mini}"
VERIFY_ON_BOOT="${FIREFLY_RUNTIME_VERIFY_ON_BOOT:-true}"
STARTUP_NOTICE_COOLDOWN_SECONDS="${STARTUP_NOTICE_COOLDOWN_SECONDS:-86400}"
SEND_UNHEALTHY_STARTUP_MESSAGE="${SEND_UNHEALTHY_STARTUP_MESSAGE:-false}"
FIREFLY_BOOTSTRAP_WARNING=""

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

send_telegram_message() {
  local message="$1"
  local delivery_target="${TELEGRAM_TARGET_ID:-${TELEGRAM_OWNER_ID:-}}"
  if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${delivery_target}" ]]; then
    return 0
  fi

  curl -fsS -X POST \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${delivery_target}" \
    --data-urlencode "text=${message}" \
    >/dev/null || true
}

should_send_startup_notice() {
  local status="$1"
  local state_file="${OPENCLAW_CONFIG_DIR}/startup-notice.state"
  local now
  now="$(date +%s)"

  if [[ "${status}" == "unhealthy" ]]; then
    local unhealthy_enabled
    unhealthy_enabled="$(printf '%s' "${SEND_UNHEALTHY_STARTUP_MESSAGE}" | tr '[:upper:]' '[:lower:]')"
    if [[ "${unhealthy_enabled}" != "1" && "${unhealthy_enabled}" != "true" && "${unhealthy_enabled}" != "yes" && "${unhealthy_enabled}" != "on" ]]; then
      return 1
    fi
  fi

  if [[ -f "${state_file}" ]]; then
    local last_status=""
    local last_ts="0"
    IFS=' ' read -r last_status last_ts < "${state_file}" || true
    if [[ "${last_status}" == "${status}" ]] && [[ $((now - last_ts)) -lt ${STARTUP_NOTICE_COOLDOWN_SECONDS} ]]; then
      return 1
    fi
  fi

  printf '%s %s\n' "${status}" "${now}" > "${state_file}"
  chmod 600 "${state_file}" || true
  return 0
}

health_headers=()
if [[ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  health_headers=(-H "Authorization: Bearer ${OPENCLAW_GATEWAY_TOKEN}")
fi

fail_with_telegram() {
  local code="$1"
  local message="$2"
  send_telegram_message "firefly-openclaw-companion startup failed: ${message}"
  echo "${message}" >&2
  exit "${code}"
}

ensure_dir() {
  local path="$1"
  local mode="$2"
  mkdir -p "${path}"
  chmod "${mode}" "${path}"
}

write_if_absent() {
  local target="$1"
  local source="$2"
  if [[ ! -e "${target}" ]]; then
    cp "${source}" "${target}"
  fi
}

ensure_dir "${OPENCLAW_HOME}" 700
ensure_dir "${OPENCLAW_CONFIG_DIR}" 700
ensure_dir "${OPENCLAW_WORKSPACE}" 700
ensure_dir "${OPENCLAW_WORKSPACE}/config" 750
ensure_dir "${OPENCLAW_WORKSPACE}/logs" 750
ensure_dir "${OPENCLAW_WORKSPACE}/skills" 750
ensure_dir "${OPENCLAW_WORKSPACE}/tools" 750

"${BIN_ROOT}/install_bundle_to_workspace.sh" "${BUNDLE_ROOT}" "${OPENCLAW_WORKSPACE}"

write_if_absent "${OPENCLAW_CONFIG_DIR}/openclaw.base.json5" "${CONFIG_BUNDLE}"

# Migrate older persisted base configs that hardcoded the legacy OpenAI model.
# The runtime config now owns model selection so provider choice from setup is
# not shadowed by a stale volume copy.
if [[ -f "${OPENCLAW_CONFIG_DIR}/openclaw.base.json5" ]] && grep -q 'primary: "openai/gpt-5.4-mini"' "${OPENCLAW_CONFIG_DIR}/openclaw.base.json5"; then
  python3 - "${OPENCLAW_CONFIG_DIR}/openclaw.base.json5" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = re.sub(
    r'\n\s*model:\s*\{\s*\n\s*primary:\s*"openai/gpt-5\.4-mini",\s*\n\s*\},',
    "",
    text,
    count=1,
)
path.write_text(text, encoding="utf-8")
PY
  chmod 600 "${OPENCLAW_CONFIG_DIR}/openclaw.base.json5"
fi

sync_agent_state() {
  python3 - "${OPENCLAW_CONFIG_DIR}" "${RUNTIME_MODEL}" <<'PY'
from __future__ import annotations

from pathlib import Path
import shutil
import sys

config_dir = Path(sys.argv[1])
runtime_model = sys.argv[2]
legacy_model = "openai/gpt-5.4-mini"
agent_dir = config_dir / "agents" / "main" / "agent"

if agent_dir.exists():
    for path in agent_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {"", ".json", ".json5", ".yaml", ".yml", ".txt", ".md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if legacy_model in text and runtime_model != legacy_model:
            path.write_text(text.replace(legacy_model, runtime_model), encoding="utf-8")

root_auth_candidates = sorted(
    p for p in config_dir.rglob("auth-profiles.json")
    if agent_dir not in p.parents
)
agent_auth = agent_dir / "auth-profiles.json"
if root_auth_candidates and runtime_model.startswith("openai-codex/"):
    source = root_auth_candidates[0]
    copy_required = not agent_auth.exists()
    if not copy_required:
        try:
            copy_required = "openai-codex" not in agent_auth.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            copy_required = True
    if copy_required:
        agent_auth.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, agent_auth)
PY
}

GATEWAY_TOKEN="$(read_secret OPENCLAW_GATEWAY_TOKEN /run/secrets/openclaw_gateway_token || true)"
FIREFLY_TOKEN="$(read_secret FIREFLY_ACCESS_TOKEN /run/secrets/firefly_access_token || true)"
if [[ -z "${GATEWAY_TOKEN}" ]]; then
  GATEWAY_TOKEN="$(read_secret OPENCLAW_GATEWAY_TOKEN /run/host-secrets/openclaw_gateway_token.txt || true)"
fi
if [[ -z "${FIREFLY_TOKEN}" ]]; then
  FIREFLY_TOKEN="$(read_secret FIREFLY_ACCESS_TOKEN /run/host-secrets/firefly_access_token.txt || true)"
fi
TELEGRAM_TOKEN="$(read_secret TELEGRAM_BOT_TOKEN /run/host-secrets/telegram_bot_token.txt || true)"

if [[ -z "${GATEWAY_TOKEN}" ]]; then
  echo "OPENCLAW_GATEWAY_TOKEN is required via env or Docker secret." >&2
  exit 11
fi

if [[ -z "${FIREFLY_TOKEN}" ]]; then
  fail_with_telegram 12 "FIREFLY_ACCESS_TOKEN is required via env or Docker secret."
fi

export OPENCLAW_GATEWAY_TOKEN="${GATEWAY_TOKEN}"
export FIREFLY_ACCESS_TOKEN="${FIREFLY_TOKEN}"
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
export FIREFLY_MAPPINGS_PATH="${FIREFLY_MAPPINGS_PATH:-${OPENCLAW_WORKSPACE}/config/mappings.yml}"
export FIREFLY_POLICY_PATH="${FIREFLY_POLICY_PATH:-${OPENCLAW_WORKSPACE}/config/policy.yml}"
export TELEGRAM_ENABLED="${TELEGRAM_ENABLED:-true}"
export TELEGRAM_OWNER_ID="${TELEGRAM_OWNER_ID:-${TELEGRAM_TARGET_ID:-}}"
export TELEGRAM_TARGET_ID="${TELEGRAM_TARGET_ID:-${TELEGRAM_OWNER_ID:-}}"
export TELEGRAM_DM_POLICY="${TELEGRAM_DM_POLICY:-allowlist}"
export FIREFLY_CHAT_LANGUAGE="${FIREFLY_CHAT_LANGUAGE:-auto}"
export OPENCLAW_BIND="${RUNTIME_BIND}"
export OPENCLAW_TELEGRAM_CHANNEL_ENABLED="${OPENCLAW_TELEGRAM_CHANNEL_ENABLED:-false}"

localized_text() {
  local key="$1"
  if [[ "${FIREFLY_CHAT_LANGUAGE}" == "it" ]]; then
    case "${key}" in
      startup_ok)
        printf '%s' "Firefly bridge sano. Bot pronto. /help mostra cosa puoi chiedermi."
        ;;
      startup_warn)
        printf '%s' "Firefly bridge avviato con avviso. /help mostra cosa puoi chiedermi."
        ;;
      gateway_unhealthy)
        printf '%s' "Sono partito, ma il gateway OpenClaw non è ancora sano su localhost:${RUNTIME_PORT}. Controlla i log di docker compose."
        ;;
    esac
    return 0
  fi

  case "${key}" in
    startup_ok)
      printf '%s' "Firefly bridge is healthy. Bot ready. /help shows what you can ask."
      ;;
    startup_warn)
      printf '%s' "Firefly bridge started with a warning. /help shows what you can ask."
      ;;
    gateway_unhealthy)
      printf '%s' "I started, but the OpenClaw gateway is still not healthy on localhost:${RUNTIME_PORT}. Check docker compose logs."
      ;;
  esac
}

TELEGRAM_CONFIG_BLOCK=""
if [[ "${TELEGRAM_ENABLED}" == "true" ]]; then
  if [[ -z "${TELEGRAM_TOKEN}" ]]; then
    echo "TELEGRAM_ENABLED=true but no Telegram bot token was provided." >&2
    exit 14
  fi
  if [[ "${TELEGRAM_DM_POLICY}" == "allowlist" || "${TELEGRAM_DM_POLICY}" == "open" ]]; then
    if [[ -z "${TELEGRAM_OWNER_ID}" ]]; then
      fail_with_telegram 15 "TELEGRAM_OWNER_ID is required when Telegram is enabled with dmPolicy=${TELEGRAM_DM_POLICY}."
    fi
  fi
  TELEGRAM_ALLOW_FROM_VALUE="${TELEGRAM_OWNER_ID}"
  if [[ "${TELEGRAM_ALLOW_FROM_VALUE}" =~ ^-?[0-9]+$ ]]; then
    TELEGRAM_ALLOW_FROM_VALUE=${TELEGRAM_ALLOW_FROM_VALUE}
  else
    TELEGRAM_ALLOW_FROM_VALUE="\"${TELEGRAM_ALLOW_FROM_VALUE}\""
  fi
  TELEGRAM_DEFAULT_TO_VALUE="\"${TELEGRAM_TARGET_ID}\""
  if [[ "${OPENCLAW_TELEGRAM_CHANNEL_ENABLED}" == "true" ]]; then
    TELEGRAM_CONFIG_BLOCK="$(cat <<EOF
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_TOKEN}",
      dmPolicy: "${TELEGRAM_DM_POLICY}",
      allowFrom: [${TELEGRAM_ALLOW_FROM_VALUE}],
      defaultTo: ${TELEGRAM_DEFAULT_TO_VALUE},
      groupPolicy: "disabled",
    },
  },
EOF
)"
  fi
fi

PLUGIN_CONFIG_BLOCK="$(cat <<EOF
  plugins: {
    entries: {
      bonjour: {
        enabled: false,
      },
    },
  },
EOF
)"

cat > "${OPENCLAW_CONFIG_DIR}/openclaw.runtime.json5" <<EOF
{
  gateway: {
    mode: "local",
    bind: "${RUNTIME_BIND}",
    port: ${RUNTIME_PORT},
    http: {
      endpoints: {
        responses: {
          enabled: true,
        },
      },
    },
    controlUi: {
      enabled: false,
    },
    auth: {
      mode: "token",
      token: "${GATEWAY_TOKEN}",
      allowTailscale: false,
    },
  },
  agents: {
    defaults: {
      workspace: "${OPENCLAW_WORKSPACE}",
      model: {
        primary: "${RUNTIME_MODEL}",
      },
    },
  },
${TELEGRAM_CONFIG_BLOCK}
${PLUGIN_CONFIG_BLOCK}
}
EOF
chmod 600 "${OPENCLAW_CONFIG_DIR}/openclaw.runtime.json5"

sync_agent_state

cat > "${OPENCLAW_CONFIG_DIR}/openclaw.json" <<EOF
{
  gateway: {
    mode: "local",
    bind: "${RUNTIME_BIND}",
    port: ${RUNTIME_PORT},
    http: {
      endpoints: {
        responses: {
          enabled: true,
        },
      },
    },
    controlUi: {
      enabled: false,
    },
    auth: {
      mode: "token",
      token: "${GATEWAY_TOKEN}",
      allowTailscale: false,
    },
  },
  tools: {
    deny: ["browser", "canvas", "nodes", "cron"],
    allow: ["gateway"],
  },
  browser: {
    enabled: false,
  },
  agents: {
    defaults: {
      workspace: "${OPENCLAW_WORKSPACE}",
      model: {
        primary: "${RUNTIME_MODEL}",
      },
    },
  },
${TELEGRAM_CONFIG_BLOCK}
${PLUGIN_CONFIG_BLOCK}
  skills: {
    load: {
      watch: false,
    },
  },
  logging: {
    level: "info",
    consoleLevel: "info",
    consoleStyle: "compact",
    redactSensitive: "tools",
    file: "${OPENCLAW_WORKSPACE}/logs/openclaw.log",
  },
}
EOF
chmod 600 "${OPENCLAW_CONFIG_DIR}/openclaw.json"

cat > "${OPENCLAW_CONFIG_DIR}/firefly-bridge.env" <<EOF
FIREFLY_BASE_URL=${FIREFLY_BASE_URL}
FIREFLY_API_BASE_PATH=${FIREFLY_API_BASE_PATH}
FIREFLY_TIMEOUT_SECONDS=${FIREFLY_TIMEOUT_SECONDS}
FIREFLY_REQUEST_RETRIES=${FIREFLY_REQUEST_RETRIES}
FIREFLY_RETRY_BACKOFF_SECONDS=${FIREFLY_RETRY_BACKOFF_SECONDS}
FIREFLY_VERIFY_TLS=${FIREFLY_VERIFY_TLS}
FIREFLY_FORCE_CONNECTION_CLOSE=${FIREFLY_FORCE_CONNECTION_CLOSE}
FIREFLY_DEFAULT_DRY_RUN=${FIREFLY_DEFAULT_DRY_RUN}
FIREFLY_HIGH_VALUE_THRESHOLD=${FIREFLY_HIGH_VALUE_THRESHOLD}
FIREFLY_DEDUPE_WINDOW_DAYS=${FIREFLY_DEDUPE_WINDOW_DAYS}
FIREFLY_ALLOW_DELETE=${FIREFLY_ALLOW_DELETE}
FIREFLY_ACCESS_TOKEN_EXPIRES_ON=${FIREFLY_ACCESS_TOKEN_EXPIRES_ON}
FIREFLY_TOKEN_REMINDER_DAYS=${FIREFLY_TOKEN_REMINDER_DAYS}
FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS=${FIREFLY_TOKEN_REMINDER_CHECK_INTERVAL_SECONDS}
FIREFLY_MAPPINGS_PATH=${FIREFLY_MAPPINGS_PATH}
FIREFLY_POLICY_PATH=${FIREFLY_POLICY_PATH}
TELEGRAM_OWNER_ID=${TELEGRAM_OWNER_ID}
TELEGRAM_TARGET_ID=${TELEGRAM_TARGET_ID}
FIREFLY_CHAT_LANGUAGE=${FIREFLY_CHAT_LANGUAGE}
FIREFLY_ACCESS_TOKEN=${FIREFLY_TOKEN}
EOF
chmod 600 "${OPENCLAW_CONFIG_DIR}/firefly-bridge.env"

if [[ "${VERIFY_ON_BOOT}" == "true" ]]; then
  firefly_health_output="$(python3 -m firefly_companion.cli health 2>&1 || true)"
  if [[ "${firefly_health_output}" != *'"status": "ok"'* ]]; then
    FIREFLY_BOOTSTRAP_WARNING="Firefly bridge health check failed during bootstrap. ${firefly_health_output}"
    echo "${FIREFLY_BOOTSTRAP_WARNING}" >&2
  fi
fi

if [[ "$#" -eq 0 ]]; then
  set -- openclaw gateway
fi

if [[ "$1" == "openclaw" && "${2:-}" == "gateway" ]]; then
  python3 /opt/firefly-openclaw/bin/token_expiry_reminder.py &
  reminder_pid=$!
  FIREFLY_TELEGRAM_BOT_TOKEN="${TELEGRAM_TOKEN:-}" \
  FIREFLY_TELEGRAM_OWNER_ID="${TELEGRAM_OWNER_ID:-}" \
  python3 /opt/firefly-openclaw/bin/telegram_firefly_bot.py &
  telegram_bot_pid=$!
  if [[ "${OPENCLAW_TELEGRAM_CHANNEL_ENABLED}" != "true" ]]; then
    unset TELEGRAM_BOT_TOKEN
  fi
  "$@" &
  gateway_pid=$!

  health_url="http://127.0.0.1:${RUNTIME_PORT}/health"
  gateway_ready=false
  for _ in $(seq 1 45); do
    if curl -fsS "${health_headers[@]}" "${health_url}" >/dev/null 2>&1; then
      gateway_ready=true
      break
    fi
    if ! kill -0 "${gateway_pid}" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  if [[ "${gateway_ready}" == "true" ]]; then
    startup_message="$(localized_text startup_warn)"
    if [[ -n "${FIREFLY_BOOTSTRAP_WARNING}" ]]; then
      startup_message="${startup_message} Warning: ${FIREFLY_BOOTSTRAP_WARNING}"
    else
      startup_message="$(localized_text startup_ok)"
    fi
    if should_send_startup_notice "ok"; then
      send_telegram_message "${startup_message}"
    fi
  else
    if should_send_startup_notice "unhealthy"; then
      send_telegram_message "$(localized_text gateway_unhealthy)"
    fi
  fi

  wait "${gateway_pid}"
  kill "${telegram_bot_pid}" >/dev/null 2>&1 || true
  kill "${reminder_pid}" >/dev/null 2>&1 || true
else
  exec "$@"
fi
