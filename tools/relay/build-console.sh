#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONSOLE_DIR="${MIRA_CONSOLE_DIR:-${ROOT_DIR}/apps/console}"
NPM_BIN="${MIRA_NPM_BIN:-npm}"
PACKAGE_FILE="${CONSOLE_DIR}/package.json"
LOCK_FILE="${CONSOLE_DIR}/package-lock.json"
STAMP_FILE="${CONSOLE_DIR}/node_modules/.mira-console-dependencies.sha256"

if [[ ! -f "${PACKAGE_FILE}" || ! -f "${LOCK_FILE}" ]]; then
  echo "Console package files are incomplete: ${PACKAGE_FILE}, ${LOCK_FILE}" >&2
  exit 1
fi

dependency_digest() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${PACKAGE_FILE}" "${LOCK_FILE}" | awk '{print $1}' | shasum -a 256 | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${PACKAGE_FILE}" "${LOCK_FILE}" | awk '{print $1}' | sha256sum | awk '{print $1}'
  else
    echo "Neither shasum nor sha256sum is available." >&2
    return 1
  fi
}

expected_digest="$(dependency_digest)"
installed_digest=""
if [[ -f "${STAMP_FILE}" ]]; then
  installed_digest="$(tr -d '\r\n' < "${STAMP_FILE}")"
fi

if [[ ! -d "${CONSOLE_DIR}/node_modules" || "${installed_digest}" != "${expected_digest}" ]]; then
  echo "Installing Mira console dependencies for package files ${expected_digest} ..."
  "${NPM_BIN}" --prefix "${CONSOLE_DIR}" ci
  mkdir -p "${CONSOLE_DIR}/node_modules"
  stamp_temp="${STAMP_FILE}.tmp.$$"
  trap 'rm -f "${stamp_temp}"' EXIT
  printf '%s\n' "${expected_digest}" > "${stamp_temp}"
  mv "${stamp_temp}" "${STAMP_FILE}"
  trap - EXIT
fi

"${NPM_BIN}" --prefix "${CONSOLE_DIR}" run build
