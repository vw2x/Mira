#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${MIRA_RELAY_PORT:-8765}"
HOST="${MIRA_RELAY_HOST:-0.0.0.0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONSOLE_DIR="${ROOT_DIR}/apps/console"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'MSG'
Usage: ./mira-local-web

Starts a Mira relay server for local LAN testing only. No cpolar or public
tunnel is started.

Environment variables:
  MIRA_RELAY_PORT   Relay port, default 8765
  MIRA_RELAY_HOST   Relay bind host, default 0.0.0.0
  MIRA_LAN_RELAY_URL
                    Android relay URL to advertise. Auto-detected by default.
  MIRA_LOCAL_BROWSER_URL
                    Browser URL printed for the computer, default http://localhost:<port>
  MIRA_LAN_BROWSER_URL
                    Browser URL printed for other LAN computers, default MIRA_LAN_RELAY_URL
  MIRA_SKIP_CONSOLE_BUILD
                    Set to 1 to skip building apps/console
  PYTHON_BIN        Python command, default python3
MSG
  exit 0
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  exit 1
fi

port_listener_pids() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | sort -u
}

ensure_port_available() {
  local listener_pids=()
  local pid

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof not found. Skipping port owner check for ${PORT}." >&2
    return 0
  fi

  while IFS= read -r pid; do
    [[ -n "${pid}" ]] && listener_pids+=("${pid}")
  done < <(port_listener_pids)

  if [[ "${#listener_pids[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "Port ${PORT} is already in use by:" >&2
  printf '  %-8s %-24s %s\n' "PID" "Process name" "Command" >&2
  for pid in "${listener_pids[@]}"; do
    local process_name process_command
    process_name="$(ps -p "${pid}" -o comm= 2>/dev/null | xargs basename 2>/dev/null || true)"
    process_command="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    printf '  %-8s %-24s %s\n' "${pid}" "${process_name:-unknown}" "${process_command:-unknown}" >&2
  done
  echo "Stop that process or choose another port with MIRA_RELAY_PORT." >&2
  exit 1
}

lan_relay_url() {
  if [[ -n "${MIRA_LAN_RELAY_URL:-}" ]]; then
    echo "${MIRA_LAN_RELAY_URL%/}"
    return 0
  fi

  "${PYTHON_BIN}" - "${PORT}" <<'PY'
import socket
import sys

port = sys.argv[1]
preferred = []
for iface in ("en0", "en1", "en2", "en3", "en4", "en5", "en6"):
    try:
        import subprocess
        ip = subprocess.check_output(["ipconfig", "getifaddr", iface], text=True, stderr=subprocess.DEVNULL).strip()
        if ip and not ip.startswith(("127.", "169.254.", "198.18.")):
            preferred.append(ip)
    except Exception:
        pass
if preferred:
    print(f"http://{preferred[0]}:{port}")
    raise SystemExit(0)

candidates = []
try:
    infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    for info in infos:
        ip = info[4][0]
        if ip not in candidates and not ip.startswith(("127.", "169.254.", "198.18.")):
            candidates.append(ip)
except Exception:
    pass
if candidates:
    print(f"http://{candidates[0]}:{port}")
    raise SystemExit(0)

raise SystemExit(1)
PY
}

cd "${ROOT_DIR}"
ensure_port_available

if [[ "${MIRA_SKIP_CONSOLE_BUILD:-0}" != "1" && -f "${CONSOLE_DIR}/package.json" ]]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Building Mira console ..."
    bash "${ROOT_DIR}/tools/relay/build-console.sh"
  elif [[ ! -f "${CONSOLE_DIR}/out/index.html" ]]; then
    echo "npm not found and apps/console/out is missing. Build the console first." >&2
    exit 1
  else
    echo "npm not found. Using existing apps/console/out." >&2
  fi
fi

LAN_RELAY_URL="$(lan_relay_url || true)"
if [[ -z "${LAN_RELAY_URL}" ]]; then
  echo "Unable to auto-detect a LAN IP. Set MIRA_LAN_RELAY_URL=http://<computer-ip>:${PORT}." >&2
  exit 1
fi

LOCAL_BROWSER_URL="${MIRA_LOCAL_BROWSER_URL:-http://localhost:${PORT}}"
LAN_BROWSER_URL="${MIRA_LAN_BROWSER_URL:-${LAN_RELAY_URL}}"

cat <<MSG

Mira local relay is starting.
Local Browser URL: ${LOCAL_BROWSER_URL}
LAN Browser URL: ${LAN_BROWSER_URL}
Android Relay URL: ${LAN_RELAY_URL}
Screen info API: ${LAN_BROWSER_URL%/}/api/screen/info

Open Local Browser URL on this computer for WebCodecs secure-context playback.
Open LAN Browser URL from other computers on the same Wi-Fi to access devices and screen metadata.
Paste Android Relay URL into the Mira Android app on the same Wi-Fi.
Press Ctrl-C to stop the local relay.

MSG

exec "${PYTHON_BIN}" -m mira.relay.server \
  --host "${HOST}" \
  --port "${PORT}" \
  --advertise-url "${LAN_RELAY_URL}"
