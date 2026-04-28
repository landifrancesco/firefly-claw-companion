#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_TESSERACT="${INSTALL_TESSERACT:-1}"

sudo_cmd=()
if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  sudo_cmd=(sudo)
fi

install_tesseract_if_missing() {
  if [[ "${INSTALL_TESSERACT}" == "0" ]] || command -v tesseract >/dev/null 2>&1; then
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    "${sudo_cmd[@]}" apt-get update
    "${sudo_cmd[@]}" apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-ita
    return 0
  fi

  if command -v apk >/dev/null 2>&1; then
    "${sudo_cmd[@]}" apk add --no-cache tesseract-ocr tesseract-ocr-data-eng tesseract-ocr-data-ita \
      || "${sudo_cmd[@]}" apk add --no-cache tesseract-ocr
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    "${sudo_cmd[@]}" dnf install -y tesseract tesseract-langpack-eng tesseract-langpack-ita
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    "${sudo_cmd[@]}" yum install -y tesseract tesseract-langpack-eng tesseract-langpack-ita
    return 0
  fi

  if command -v pacman >/dev/null 2>&1; then
    "${sudo_cmd[@]}" pacman -Sy --noconfirm tesseract tesseract-data-eng tesseract-data-ita
    return 0
  fi

  if command -v brew >/dev/null 2>&1; then
    brew install tesseract
    return 0
  fi

  echo "Could not install tesseract automatically on this platform. Install it manually or set INSTALL_TESSERACT=0 to skip." >&2
  return 1
}

install_tesseract_if_missing

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"

echo "Virtual environment ready at ${VENV_DIR}" >&2
