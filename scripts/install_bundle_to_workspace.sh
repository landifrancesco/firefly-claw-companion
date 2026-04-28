#!/usr/bin/env bash
set -euo pipefail

umask 027

SOURCE_ROOT="${1:-/opt/firefly-openclaw/workspace}"
TARGET_ROOT="${2:-${OPENCLAW_WORKSPACE:-$HOME/workspace}}"

mkdir -p "${TARGET_ROOT}"

if [[ ! -d "${SOURCE_ROOT}" ]]; then
  echo "Bundle source does not exist: ${SOURCE_ROOT}" >&2
  exit 1
fi

find "${SOURCE_ROOT}" -type d -print | while IFS= read -r dir; do
  relative="${dir#${SOURCE_ROOT}}"
  mkdir -p "${TARGET_ROOT}${relative}"
done

find "${SOURCE_ROOT}" -type f -print | while IFS= read -r file; do
  relative="${file#${SOURCE_ROOT}/}"
  destination="${TARGET_ROOT}/${relative}"
  if [[ ! -e "${destination}" ]]; then
    install -m 0640 "${file}" "${destination}"
  fi
done

find "${TARGET_ROOT}/tools" -maxdepth 1 -type f -print 2>/dev/null | while IFS= read -r tool_file; do
  chmod 0750 "${tool_file}"
done

if [[ -d "${TARGET_ROOT}/logs" ]]; then
  chmod 0750 "${TARGET_ROOT}/logs"
fi
