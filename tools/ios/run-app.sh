#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_PATH="${ROOT_DIR}/ios/Mira/Mira.xcodeproj"
SCHEME="${MIRA_IOS_SCHEME:-Mira}"
DEVICE_NAME="${MIRA_IOS_DEVICE:-iPhone 17 Pro}"
TARGET="${MIRA_IOS_TARGET:-auto}"
BUNDLE_ID="${MIRA_IOS_BUNDLE_ID:-com.vwww.mira.ios}"
AUTO_LAUNCH_DEVICE="${MIRA_IOS_AUTO_LAUNCH_DEVICE:-1}"
AUTO_OPEN_RELAY_CONSOLE="${MIRA_IOS_AUTO_OPEN_RELAY_CONSOLE:-1}"
AUTO_START_LOCAL_RELAY="${MIRA_IOS_AUTO_START_LOCAL_RELAY:-1}"
AUTO_BUILD_ISH_LIBS="${MIRA_IOS_AUTO_BUILD_ISH_LIBS:-1}"
AUTO_CONNECT_RELAY_URL="${MIRA_IOS_RELAY_URL:-}"
HOST_RELAY_PORT="${MIRA_IOS_HOST_RELAY_PORT:-8765}"
RELAY_LOG_PATH="${MIRA_IOS_RELAY_LOG_PATH:-${ROOT_DIR}/build/mira-local-relay.log}"
OPEN_URL="${MIRA_IOS_OPEN_URL:-http://127.0.0.1:${HOST_RELAY_PORT}}"
VERIFY_RELAY_REGISTRATION="${MIRA_IOS_VERIFY_RELAY_REGISTRATION:-1}"
ALLOW_UNINSTALL="${MIRA_IOS_ALLOW_UNINSTALL:-0}"
SIM_DERIVED_DATA="${MIRA_IOS_SIM_DERIVED_DATA:-${ROOT_DIR}/build/ios/DerivedData}"
SIM_APP_PATH="${SIM_DERIVED_DATA}/Build/Products/Debug-iphonesimulator/Mira.app"
DEVICE_DERIVED_DATA="${MIRA_IOS_DEVICE_DERIVED_DATA:-${ROOT_DIR}/build/ios-mira-device-native-relay-derived}"
DEVICE_APP_PATH="${DEVICE_DERIVED_DATA}/Build/Products/Debug-iphoneos/Mira.app"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'MSG'
Usage: ./mira-ios [--device|--simulator]

Builds the Mira iOS app, installs it, and restarts it.

Default target is auto:
  1. use a connected physical iOS device when ios-deploy can see one
  2. otherwise fall back to the booted iOS Simulator

Environment variables:
  MIRA_IOS_TARGET       auto, device, or simulator. Default auto
  MIRA_IOS_DEVICE_ID    physical device UDID. If empty, first USB device from ios-deploy is used
  MIRA_IOS_DEPLOY       ios-deploy executable path. Default local build/ios-tools, then PATH
  MIRA_IOS_AUTO_LAUNCH_DEVICE  1 to auto launch after physical-device install. Default 1
  MIRA_IOS_AUTO_OPEN_RELAY_CONSOLE  1 to open Relay console on computer. Default 1
  MIRA_IOS_AUTO_START_LOCAL_RELAY   1 to auto-start ./mira-local-web when localhost relay is missing. Default 1
  MIRA_IOS_AUTO_BUILD_ISH_LIBS      1 to rebuild missing or stale iSH static libraries before Xcode build. Default 1
  MIRA_IOS_RELAY_URL    Relay URL to inject at launch time for automated connect. Default http://<host-lan-ip>:8765 on physical devices, http://127.0.0.1:8765 on Simulator
  MIRA_IOS_HOST_RELAY_PORT Relay port for local relay and browser. Default 8765
  MIRA_IOS_RELAY_LOG_PATH Log file for auto-started local relay
  MIRA_IOS_OPEN_URL     Browser URL to open on this computer. Default http://127.0.0.1:<relay-port>
  MIRA_IOS_VERIFY_RELAY_REGISTRATION  1 to check whether the iOS app registered with Relay after launch. Default 1
  MIRA_IOS_ALLOW_UNINSTALL  1 to allow uninstall/reinstall fallback on physical devices. Default 0 to preserve developer trust
  MIRA_IOS_DEVICE       Simulator device name. Default iPhone 17 Pro
  MIRA_IOS_SCHEME       Xcode scheme. Default Mira
  MIRA_IOS_BUNDLE_ID    Bundle identifier. Default com.vwww.mira.ios
MSG
  exit 0
fi

case "${1:-}" in
  --device)
    TARGET="device"
    shift
    ;;
  --simulator)
    TARGET="simulator"
    shift
    ;;
esac

if [[ $# -gt 0 ]]; then
  echo "Unknown argument: $1" >&2
  echo "Run ./mira-ios --help for usage." >&2
  exit 1
fi

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "xcodebuild not found. Install Xcode and run xcode-select first." >&2
  exit 1
fi
if ! command -v xcrun >/dev/null 2>&1; then
  echo "xcrun not found. Install Xcode and run xcode-select first." >&2
  exit 1
fi
if [[ ! -d "${PROJECT_PATH}" ]]; then
  echo "Missing iOS project: ${PROJECT_PATH}" >&2
  exit 1
fi

ish_product_dir() {
  local sdk_name="$1"
  local build_dir="$2"
  printf '%s/%s-%s/meson\n' "${build_dir}" "${MIRA_ISH_CONFIGURATION:-Debug-ApplePleaseFixFB19282108}" "${sdk_name}"
}

ensure_ish_static_libraries() {
  local sdk_name="$1"
  local build_dir="$2"
  local archs="$3"
  local product_dir

  product_dir="$(ish_product_dir "${sdk_name}" "${build_dir}")"
  if MIRA_ISH_SDK="${sdk_name}" \
      MIRA_ISH_BUILD_DIR="${build_dir}" \
      MIRA_ISH_ARCHS="${archs}" \
      "${ROOT_DIR}/tools/ios/build-ish-libs.sh" --check; then
    return 0
  fi

  if [[ "${AUTO_BUILD_ISH_LIBS}" != "1" ]]; then
    cat >&2 <<MSG
Missing or stale iSH static libraries in:
  ${product_dir}
Run:
  MIRA_ISH_SDK=${sdk_name} MIRA_ISH_BUILD_DIR=${build_dir} MIRA_ISH_ARCHS=${archs} ./tools/ios/build-ish-libs.sh
Or set MIRA_IOS_AUTO_BUILD_ISH_LIBS=1.
MSG
    exit 1
  fi

  echo "Missing or stale iSH static libraries for ${sdk_name}. Building them first ..."
  MIRA_ISH_SDK="${sdk_name}" \
    MIRA_ISH_BUILD_DIR="${build_dir}" \
    MIRA_ISH_ARCHS="${archs}" \
    "${ROOT_DIR}/tools/ios/build-ish-libs.sh"
}

open_url() {
  local url="$1"
  if [[ -z "${url}" ]]; then
    return 0
  fi
  open "${url}" >/dev/null 2>&1 || true
}

detect_host_lan_ip() {
  local iface=""
  local ip=""

  if command -v route >/dev/null 2>&1; then
    iface="$(route get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
  fi

  if [[ -n "${iface}" ]] && command -v ipconfig >/dev/null 2>&1; then
    ip="$(ipconfig getifaddr "${iface}" 2>/dev/null || true)"
    if [[ -n "${ip}" ]]; then
      printf '%s\n' "${ip}"
      return 0
    fi
  fi

  python3 - <<'PY'
import ipaddress
import subprocess

ips = []
try:
    data = subprocess.run(["ifconfig"], capture_output=True, text=True, check=False).stdout
    for line in data.splitlines():
        line = line.strip()
        if not line.startswith("inet "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[1]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version == 4 and not addr.is_loopback and not addr.is_link_local:
            ips.append(ip)
except Exception:
    pass

private = []
for ip in ips:
    try:
        if ipaddress.ip_address(ip).is_private:
            private.append(ip)
    except ValueError:
        pass

if private:
    print(private[0])
elif ips:
    print(ips[0])
PY
}

is_local_relay_ready() {
  python3 - "$1" <<'PY'
import sys
import urllib.request

port = sys.argv[1]
for host in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
    try:
        with urllib.request.urlopen(host + "/api/devices", timeout=1.5) as r:
            if r.status == 200:
                sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
}

is_ios_device_registered_in_relay() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
import urllib.request

port = sys.argv[1]
bundle_id = sys.argv[2]

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/devices", timeout=2.0) as response:
        payload = json.load(response)
except Exception:
    sys.exit(1)

for device in payload.get("devices", []):
    if device.get("platform") != "ios":
        continue
    if bundle_id and device.get("packageName") not in ("", None, bundle_id):
        continue
    screen = device.get("screen") or {}
    if device.get("state") in ("active", "idle") or screen.get("live") is True:
        sys.exit(0)

sys.exit(1)
PY
}

verify_ios_relay_registration() {
  local bundle_id="$1"
  local i

  if [[ "${VERIFY_RELAY_REGISTRATION}" != "1" ]]; then
    return 0
  fi

  echo "Waiting for Mira iOS to register with Relay ..."
  for i in {1..10}; do
    if is_ios_device_registered_in_relay "${HOST_RELAY_PORT}" "${bundle_id}"; then
      echo "Mira iOS registered with Relay."
      return 0
    fi
    sleep 1
  done

  cat >&2 <<MSG
Mira iOS did not register with Relay yet.
If this is the first launch on this iPhone, approve Local Network access:
  Settings -> Privacy & Security -> Local Network -> Mira
Then rerun ./mira-ios --device, or tap Connect Relay in the app.
MSG
}

start_local_relay_if_needed() {
  if is_local_relay_ready "${HOST_RELAY_PORT}"; then
    return 0
  fi

  if [[ "${AUTO_START_LOCAL_RELAY}" != "1" ]]; then
    echo "Relay is not running on 127.0.0.1:${HOST_RELAY_PORT}. Start ./mira-local-web first or enable MIRA_IOS_AUTO_START_LOCAL_RELAY=1." >&2
    exit 1
  fi

  echo "Starting local relay via ./mira-local-web ..."
  mkdir -p "$(dirname "${RELAY_LOG_PATH}")"
  (
    cd "${ROOT_DIR}"
    nohup env MIRA_RELAY_PORT="${HOST_RELAY_PORT}" ./mira-local-web >"${RELAY_LOG_PATH}" 2>&1 &
  )

  local i
  for i in {1..60}; do
    if is_local_relay_ready "${HOST_RELAY_PORT}"; then
      echo "Local relay is ready on http://127.0.0.1:${HOST_RELAY_PORT}"
      return 0
    fi
    sleep 1
  done

  echo "Local relay did not become ready. Check ${RELAY_LOG_PATH}" >&2
  exit 1
}

if [[ -z "${AUTO_CONNECT_RELAY_URL}" ]]; then
  HOST_LAN_IP="$(detect_host_lan_ip)"
  if [[ -z "${HOST_LAN_IP}" ]]; then
    echo "Unable to detect host LAN IP. Set MIRA_IOS_RELAY_URL manually." >&2
    exit 1
  fi
  AUTO_CONNECT_RELAY_URL="http://${HOST_LAN_IP}:${HOST_RELAY_PORT}"
fi

start_local_relay_if_needed

ensure_ios_deploy() {
  if [[ -n "${MIRA_IOS_DEPLOY:-}" ]]; then
    if [[ -x "${MIRA_IOS_DEPLOY}" ]]; then
      printf '%s\n' "${MIRA_IOS_DEPLOY}"
      return
    fi
    echo "MIRA_IOS_DEPLOY is not executable: ${MIRA_IOS_DEPLOY}" >&2
    exit 1
  fi

  local local_ios_deploy="${ROOT_DIR}/build/ios-tools/node_modules/.bin/ios-deploy"
  if [[ -x "${local_ios_deploy}" ]]; then
    printf '%s\n' "${local_ios_deploy}"
    return
  fi

  if command -v ios-deploy >/dev/null 2>&1; then
    command -v ios-deploy
    return
  fi

  if ! command -v npm >/dev/null 2>&1; then
    echo "ios-deploy not found. Install it or set MIRA_IOS_DEPLOY=/path/to/ios-deploy." >&2
    exit 1
  fi

  echo "Installing local ios-deploy ..." >&2
  mkdir -p "${ROOT_DIR}/build/ios-tools"
  if [[ ! -f "${ROOT_DIR}/build/ios-tools/package.json" ]]; then
    cat >"${ROOT_DIR}/build/ios-tools/package.json" <<'JSON'
{
  "dependencies": {
    "ios-deploy": "^1.12.2"
  }
}
JSON
  fi
  npm --prefix "${ROOT_DIR}/build/ios-tools" install >&2

  if [[ ! -x "${local_ios_deploy}" ]]; then
    echo "ios-deploy install did not produce ${local_ios_deploy}" >&2
    exit 1
  fi
  printf '%s\n' "${local_ios_deploy}"
}

detect_ios_device_id() {
  local ios_deploy="$1"
  if [[ -n "${MIRA_IOS_DEVICE_ID:-}" ]]; then
    printf '%s\n' "${MIRA_IOS_DEVICE_ID}"
    return
  fi

  "${ios_deploy}" --detect --faster-path-search 2>/dev/null | awk '
    /through USB/ && !usb { usb=$3 }
    /Found/ && !any { any=$3 }
    END {
      if (usb) print usb
      else if (any) print any
    }
  '
}

launch_device_app_with_devicectl() {
  local device_id="$1"
  local bundle_id="$2"
  local output
  local status

  if ! command -v xcrun >/dev/null 2>&1; then
    return 127
  fi

  set +e
  output="$(xcrun devicectl device process launch \
    --device "${device_id}" \
    --terminate-existing \
    --activate \
    "${bundle_id}" 2>&1)"
  status=$?
  set -e

  printf '%s\n' "${output}"
  return "${status}"
}

install_device_app() {
  local ios_deploy="$1"
  local device_id="$2"
  local app_path="$3"
  local bundle_id="$4"
  local output
  local status

  if has_idb; then
    if install_device_app_with_idb "${device_id}" "${app_path}"; then
      return 0
    fi
    echo "idb install failed, falling back to ios-deploy ..." >&2
  fi

  set +e
  output="$("${ios_deploy}" \
    --id "${device_id}" \
    --bundle "${app_path}" \
    --faster-path-search 2>&1)"
  status=$?
  set -e

  printf '%s\n' "${output}"
  if [[ ${status} -eq 0 ]]; then
    return 0
  fi

  if [[ "${output}" == *"0xe8000067"* || "${output}" == *"internal API error"* ]]; then
    if [[ "${ALLOW_UNINSTALL}" != "1" ]]; then
      cat >&2 <<MSG
Install hit internal API error, but uninstall fallback is disabled.
Keeping the existing app installed preserves the trusted developer state on physical iOS devices.
Use a plain overlay install after resolving the install issue, or set MIRA_IOS_ALLOW_UNINSTALL=1 only when you accept losing trust state and app container data.
MSG
      return "${status}"
    fi

    echo "Install hit internal API error, retrying after uninstall because MIRA_IOS_ALLOW_UNINSTALL=1 ..." >&2
    "${ios_deploy}" \
      --id "${device_id}" \
      --bundle_id "${bundle_id}" \
      --uninstall_only \
      --faster-path-search >/dev/null 2>&1 || true

    "${ios_deploy}" \
      --id "${device_id}" \
      --bundle "${app_path}" \
      --uninstall \
      --faster-path-search
    return 0
  fi

  return "${status}"
}

ensure_idb() {
  local user_base
  if command -v idb >/dev/null 2>&1; then
    command -v idb
    return
  fi

  user_base="$(python3 -m site --user-base 2>/dev/null || true)"
  if [[ -n "${user_base}" && -x "${user_base}/bin/idb" ]]; then
    printf '%s\n' "${user_base}/bin/idb"
    return
  fi

  echo "idb not found. Install fb-idb client and idb-companion first." >&2
  echo "Docs: https://fbidb.io/docs/installation/" >&2
  exit 1
}

has_idb() {
  command -v idb >/dev/null 2>&1 || [[ -x "$(python3 -m site --user-base 2>/dev/null || true)/bin/idb" ]]
}

connect_idb_target() {
  local idb_bin="$1"
  local device_id="$2"
  "${idb_bin}" connect "${device_id}" >/dev/null
}

install_device_app_with_idb() {
  local device_id="$1"
  local app_path="$2"
  local idb_bin

  idb_bin="$(ensure_idb)"
  connect_idb_target "${idb_bin}" "${device_id}"
  "${idb_bin}" install --udid "${device_id}" "${app_path}"
}

launch_device_app_with_idb() {
  local device_id="$1"
  local bundle_id="$2"
  local relay_url="$3"
  local idb_bin

  idb_bin="$(ensure_idb)"
  connect_idb_target "${idb_bin}" "${device_id}"
  "${idb_bin}" terminate --udid "${device_id}" "${bundle_id}" >/dev/null 2>&1 || true
  if [[ -n "${relay_url}" ]]; then
    IDB_MIRA_RELAY_URL="${relay_url}" IDB_MIRA_AUTO_CONNECT=1 "${idb_bin}" launch --udid "${device_id}" "${bundle_id}"
  else
    "${idb_bin}" launch --udid "${device_id}" "${bundle_id}"
  fi
}

launch_device_app() {
  local ios_deploy="$1"
  local device_id="$2"
  local app_path="$3"
  local bundle_id="$4"
  local relay_url="$5"

  if has_idb; then
    launch_device_app_with_idb "${device_id}" "${bundle_id}" "${relay_url}"
    return 0
  fi

  local output
  local status

  if output="$(launch_device_app_with_devicectl "${device_id}" "${bundle_id}")"; then
    printf '%s\n' "${output}"
    return 0
  fi
  status=$?
  printf '%s\n' "${output}"
  echo "devicectl launch failed, falling back to ios-deploy ..." >&2

  set +e
  output="$("${ios_deploy}" \
    --id "${device_id}" \
    --bundle "${app_path}" \
    --noinstall \
    --justlaunch \
    --noninteractive \
    --faster-path-search 2>&1)"
  status=$?
  set -e

  printf '%s\n' "${output}"
  if [[ "${output}" == *"error: Cannot launch"* || "${output}" == *"invalid code signature"* ]]; then
    return 1
  fi
  if [[ ${status} -ne 0 && "${output}" != *"success"* ]]; then
    return "${status}"
  fi
}

run_device() {
  local ios_deploy="$1"
  local device_id="$2"

  if [[ -z "${device_id}" ]]; then
    echo "No physical iOS device found. Connect a device or run ./mira-ios --simulator." >&2
    exit 1
  fi

  echo "Building Mira for physical iOS device: ${device_id}"
  ensure_ish_static_libraries "iphoneos" "${ROOT_DIR}/build/ios-ish-device-libs" "arm64"
  env -u SDKROOT -u LIBRARY_PATH xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${SCHEME}" \
    -configuration Debug \
    -sdk iphoneos \
    -destination "id=${device_id}" \
    -derivedDataPath "${DEVICE_DERIVED_DATA}" \
    -allowProvisioningUpdates \
    -allowProvisioningDeviceRegistration \
    ENABLE_DEBUG_DYLIB=NO \
    ENABLE_PREVIEWS=NO \
    build

  if [[ ! -d "${DEVICE_APP_PATH}" ]]; then
    echo "Built app not found: ${DEVICE_APP_PATH}" >&2
    exit 1
  fi

  echo "Stopping existing Mira app if it is running ..."
  "${ios_deploy}" \
    --id "${device_id}" \
    --bundle_id "${BUNDLE_ID}" \
    --kill \
    --faster-path-search >/dev/null 2>&1 || true

  echo "Installing Mira app with ios-deploy using overlay install to preserve trusted developer state ..."
  install_device_app "${ios_deploy}" "${device_id}" "${DEVICE_APP_PATH}" "${BUNDLE_ID}"

  if [[ "${AUTO_LAUNCH_DEVICE}" == "1" ]]; then
    echo "Restarting Mira app ..."
    launch_device_app "${ios_deploy}" "${device_id}" "${DEVICE_APP_PATH}" "${BUNDLE_ID}" "${AUTO_CONNECT_RELAY_URL}"
    verify_ios_relay_registration "${BUNDLE_ID}"
    launch_note="Auto launch attempted."
  else
    launch_note="Auto launch skipped on physical device by default. Open Mira manually, or set MIRA_IOS_AUTO_LAUNCH_DEVICE=1. If idb is installed, MIRA_IOS_RELAY_URL can be injected for auto connect."
  fi

  cat <<MSG

Mira iOS app installed on device.
Project: ${PROJECT_PATH}
Device ID: ${device_id}
Bundle: ${BUNDLE_ID}
App: ${DEVICE_APP_PATH}
Note: ${launch_note}

MSG

  if [[ "${AUTO_OPEN_RELAY_CONSOLE}" == "1" ]]; then
    open_url "${OPEN_URL}"
  fi
}

run_simulator() {
  local booted_device
  local sim_relay_url
  booted_device="$(xcrun simctl list devices booted | awk -F '[()]' '/Booted/ {print $2; exit}')"
  if [[ -z "${booted_device}" ]]; then
    echo "Booting simulator: ${DEVICE_NAME}"
    xcrun simctl boot "${DEVICE_NAME}"
  fi
  open -a Simulator

  ensure_ish_static_libraries "iphonesimulator" "${ROOT_DIR}/build/ios-ish-libs" "arm64"
  MIRA_ISH_SKIP_COMPILEALL="${MIRA_ISH_SKIP_COMPILEALL:-1}" \
  env -u SDKROOT -u LIBRARY_PATH xcodebuild \
    -project "${PROJECT_PATH}" \
    -scheme "${SCHEME}" \
    -configuration Debug \
    -destination "platform=iOS Simulator,name=${DEVICE_NAME}" \
    -derivedDataPath "${SIM_DERIVED_DATA}" \
    CODE_SIGNING_ALLOWED=NO \
    ENABLE_DEBUG_DYLIB=NO \
    ENABLE_PREVIEWS=NO \
    build

  if [[ ! -d "${SIM_APP_PATH}" ]]; then
    echo "Built app not found: ${SIM_APP_PATH}" >&2
    exit 1
  fi

  sim_relay_url="${AUTO_CONNECT_RELAY_URL}"
  if [[ -z "${MIRA_IOS_RELAY_URL:-}" ]]; then
    sim_relay_url="http://127.0.0.1:${HOST_RELAY_PORT}"
  fi

  xcrun simctl terminate booted "${BUNDLE_ID}" >/dev/null 2>&1 || true
  xcrun simctl install booted "${SIM_APP_PATH}"
  SIMCTL_CHILD_MIRA_RELAY_URL="${sim_relay_url}" \
  SIMCTL_CHILD_MIRA_AUTO_CONNECT=1 \
  xcrun simctl launch \
    --terminate-running-process \
    booted \
    "${BUNDLE_ID}"
  verify_ios_relay_registration "${BUNDLE_ID}"

  cat <<MSG

Mira iOS app installed and restarted on simulator.
Project: ${PROJECT_PATH}
Device: ${DEVICE_NAME}
Bundle: ${BUNDLE_ID}
App: ${SIM_APP_PATH}
Relay: ${sim_relay_url}

MSG

  if [[ "${AUTO_OPEN_RELAY_CONSOLE}" == "1" ]]; then
    open_url "${OPEN_URL}"
  fi
}

case "${TARGET}" in
  auto)
    IOS_DEPLOY="$(ensure_ios_deploy)"
    DEVICE_ID="$(detect_ios_device_id "${IOS_DEPLOY}")"
    if [[ -n "${DEVICE_ID}" ]]; then
      run_device "${IOS_DEPLOY}" "${DEVICE_ID}"
    else
      run_simulator
    fi
    ;;
  device)
    IOS_DEPLOY="$(ensure_ios_deploy)"
    DEVICE_ID="$(detect_ios_device_id "${IOS_DEPLOY}")"
    run_device "${IOS_DEPLOY}" "${DEVICE_ID}"
    ;;
  simulator|sim)
    run_simulator
    ;;
  *)
    echo "Invalid MIRA_IOS_TARGET: ${TARGET}. Use auto, device, or simulator." >&2
    exit 1
    ;;
esac
