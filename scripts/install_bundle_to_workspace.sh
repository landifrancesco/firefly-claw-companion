#!/usr/bin/env bash
set -euo pipefail

umask 027

SOURCE_ROOT="${1:-/opt/firefly-picoclaw/workspace}"
TARGET_ROOT="${2:-${PICOCLAW_WORKSPACE:-$HOME/.picoclaw/workspace}}"

mkdir -p "${TARGET_ROOT}/config" "${TARGET_ROOT}/tools" "${TARGET_ROOT}/i18n" "${TARGET_ROOT}/logs"

if [[ ! -d "${SOURCE_ROOT}" ]]; then
  echo "Bundle source does not exist: ${SOURCE_ROOT}" >&2
  exit 1
fi

copy_if_absent() {
  local source="$1"
  local destination="$2"
  if [[ -f "${source}" && ! -e "${destination}" ]]; then
    install -m 0640 "${source}" "${destination}"
  fi
}

copy_if_absent "${SOURCE_ROOT}/config/mappings.yml.example" "${TARGET_ROOT}/config/mappings.yml"
copy_if_absent "${SOURCE_ROOT}/config/policy.yml.example" "${TARGET_ROOT}/config/policy.yml"
copy_if_absent "${SOURCE_ROOT}/config/policy.yml.example" "${TARGET_ROOT}/config/policy.yml.example"
copy_if_absent "${SOURCE_ROOT}/i18n/telegram_bot.en.json" "${TARGET_ROOT}/i18n/telegram_bot.en.json"
copy_if_absent "${SOURCE_ROOT}/i18n/telegram_bot.it.json" "${TARGET_ROOT}/i18n/telegram_bot.it.json"

if [[ -f "${SOURCE_ROOT}/tools/firefly-bridge" ]]; then
  install -m 0750 "${SOURCE_ROOT}/tools/firefly-bridge" "${TARGET_ROOT}/tools/firefly-bridge"
fi

chmod 0750 "${TARGET_ROOT}/logs"
