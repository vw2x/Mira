#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ISH_DIR="$ROOT_DIR/third_party/ish"
CACHE_DIR="${MIRA_ISH_ROOTFS_CACHE_DIR:-$ROOT_DIR/build/ios-ish-rootfs}"
HOST_TOOLS_DIR="${MIRA_ISH_HOST_TOOLS_DIR:-$ROOT_DIR/build/ios-ish-host-tools}"
ROOTFS_NAME="MiraISHRoot.fakefs"
FRIDA_RUNTIME_HELPER="$ROOT_DIR/tools/ios/prepare-ish-frida-runtime.py"
STATE_HELPER="$ROOT_DIR/tools/ios/ish-build-state.py"
EXPECTED_FRIDA_RUNTIME_STAMP="frida_runtime=official-frida-tools-12.1.0-frida-16.0.7-v13"
ISH_HOST="$HOST_TOOLS_DIR/ish"

if [[ -n "${MIRA_ISH_ROOTFS_OUTPUT:-}" ]]; then
    OUTPUT_DIR="$MIRA_ISH_ROOTFS_OUTPUT"
elif [[ -n "${TARGET_BUILD_DIR:-}" && -n "${UNLOCALIZED_RESOURCES_FOLDER_PATH:-}" ]]; then
    OUTPUT_DIR="$TARGET_BUILD_DIR/$UNLOCALIZED_RESOURCES_FOLDER_PATH/$ROOTFS_NAME"
else
    OUTPUT_DIR="$ROOT_DIR/ios/Mira/Mira/Resources/$ROOTFS_NAME"
fi

ROOTFS_URL="${MIRA_ISH_ROOTFS_URL:-}"
if [[ -z "$ROOTFS_URL" ]]; then
    # Updating the emulator must not silently update Alpine/Python userspace.
    ROOTFS_URL="$(python3 "$STATE_HELPER" rootfs-url)"
fi
if [[ "$ROOTFS_URL" != http://* && "$ROOTFS_URL" != https://* ]]; then
    ROOTFS_URL="https://$ROOTFS_URL"
fi

ARCHIVE="$CACHE_DIR/appstore-apk.tar.gz"
FAKEFSIFY="$HOST_TOOLS_DIR/tools/fakefsify"
STAMP="$OUTPUT_DIR/.mira-ish-rootfs.stamp"

cleanup_path() {
    local path="$1"
    local attempt
    for attempt in 1 2 3 4 5; do
        rm -rf "$path" 2>/dev/null || true
        if [[ ! -e "$path" ]]; then
            return 0
        fi
        sleep 1
    done
    rm -rf "$path"
}

ensure_host_tools_builddir() {
    python3 "$ROOT_DIR/tools/ios/prepare-ish-source.py"
    if [[ ! -f "$HOST_TOOLS_DIR/build.ninja" ]]; then
        rm -rf "$HOST_TOOLS_DIR"
        meson setup "$HOST_TOOLS_DIR" "$ISH_DIR" --buildtype=release -Dlog_handler=dprintf -Dkernel=ish
    fi
}

rootfs_state_args=(
    --root "$ROOT_DIR" --output "$OUTPUT_DIR" --url "$ROOTFS_URL"
    --runtime-stamp "$EXPECTED_FRIDA_RUNTIME_STAMP"
)
if python3 "$STATE_HELPER" rootfs reuse "${rootfs_state_args[@]}"; then
    echo "iSH rootfs already prepared: $OUTPUT_DIR"
    exit 0
fi

mkdir -p "$CACHE_DIR" "$(dirname "$OUTPUT_DIR")"

if [[ ! -s "$ARCHIVE" ]]; then
    echo "Downloading iSH rootfs: $ROOTFS_URL"
    curl -fL --retry 3 --connect-timeout 20 -o "$ARCHIVE.tmp" "$ROOTFS_URL"
    python3 "$STATE_HELPER" archive record --archive "$ARCHIVE.tmp" --url "$ROOTFS_URL"
    mv "$ARCHIVE.tmp" "$ARCHIVE"
    mv "$ARCHIVE.tmp.source.json" "$ARCHIVE.source.json"
fi
python3 "$STATE_HELPER" archive verify --archive "$ARCHIVE" --url "$ROOTFS_URL"

if ! command -v meson >/dev/null 2>&1; then
    echo "meson is required to build iSH rootfs tools." >&2
    exit 1
fi
if ! command -v ninja >/dev/null 2>&1; then
    echo "ninja is required to build iSH rootfs tools." >&2
    exit 1
fi
if ! command -v pkg-config >/dev/null 2>&1 && [[ ! -d /opt/homebrew/opt/libarchive && ! -d /usr/local/opt/libarchive ]]; then
    echo "pkg-config or Homebrew libarchive is required to build iSH fakefsify." >&2
    exit 1
fi
ensure_host_tools_builddir
# Existing executables may have been built from a different iSH revision.
ninja -C "$HOST_TOOLS_DIR" tools/fakefsify ish

TMP_DIR="$CACHE_DIR/$ROOTFS_NAME.tmp"
STAGING_ROOT="$CACHE_DIR/$ROOTFS_NAME.rootfs"
PATCHED_ARCHIVE="$CACHE_DIR/$ROOTFS_NAME.patched.tar.gz"
STAGING_STAMP="$CACHE_DIR/$ROOTFS_NAME.stamp"
cleanup_path "$TMP_DIR"
cleanup_path "$STAGING_ROOT"
mkdir -p "$STAGING_ROOT"
tar -xzf "$ARCHIVE" -C "$STAGING_ROOT"
python3 "$FRIDA_RUNTIME_HELPER" --rootfs "$STAGING_ROOT" --cache-dir "$CACHE_DIR/frida-runtime" --stamp-path "$STAGING_STAMP"
python3 "$STATE_HELPER" tree-safe "$STAGING_ROOT"
tar -C "$STAGING_ROOT" -czf "$PATCHED_ARCHIVE" .
"$FAKEFSIFY" "$PATCHED_ARCHIVE" "$TMP_DIR"
if [[ "${MIRA_ISH_SKIP_COMPILEALL:-0}" != "1" ]]; then
    "$ISH_HOST" -f "$TMP_DIR" /bin/sh -lc 'export PYTHONPATH=/opt/mira/frida-python/site-packages; /usr/bin/python3 -c "import compileall, sys; compileall.compile_dir(f\"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}\", quiet=1); compileall.compile_dir(\"/opt/mira/frida-python/site-packages\", quiet=1)"'
fi
rm -rf "$OUTPUT_DIR"
ditto "$TMP_DIR" "$OUTPUT_DIR"
cleanup_path "$TMP_DIR"
cp "$ISH_DIR/LICENSE.md" "$OUTPUT_DIR/iSH-LICENSE.md"
cp "$ISH_DIR/LICENSE.IOS" "$OUTPUT_DIR/iSH-LICENSE.IOS"
cp "$STAGING_STAMP" "$STAMP"

if [[ ! -f "$STAMP" ]]; then
    echo "prepare-ish-frida-runtime.py did not write stamp: $STAMP" >&2
    exit 1
fi

if [[ "${MIRA_ISH_SKIP_COMPILEALL:-0}" == "1" ]]; then
    rootfs_state_args+=(--skip-compileall)
fi
python3 "$STATE_HELPER" rootfs record "${rootfs_state_args[@]}" \
    --archive "$ARCHIVE" --staging-root "$STAGING_ROOT"
cleanup_path "$STAGING_ROOT"

echo "Prepared iSH rootfs at $OUTPUT_DIR"
