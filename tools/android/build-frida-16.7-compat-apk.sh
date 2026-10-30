#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${MIRA_FRIDA_COMPAT_OUTPUT_DIR:-${ROOT_DIR}/build/frida-compat-apks}"
OUTPUT_APK="${MIRA_FRIDA_COMPAT_OUTPUT_APK:-${OUTPUT_DIR}/mira-app-android14-15-frida-16.7.19-debug.apk}"

MIRA_FRIDA_COMPAT_OUTPUT_DIR="${OUTPUT_DIR}" \
MIRA_FRIDA_COMPAT_OUTPUT_APK="${OUTPUT_APK}" \
MIRA_FRIDA_VERSION=16.7.19 \
MIRA_BOOTSTRAP_STATE_VERSION=19 \
  "${ROOT_DIR}/tools/android/build-frida-versioned-compat-apk.sh"
