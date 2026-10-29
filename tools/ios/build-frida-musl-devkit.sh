#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FRIDA_VERSION="${FRIDA_VERSION:-16.0.7}"
LIBUCONTEXT_VERSION="${MIRA_IOS_LIBUCONTEXT_VERSION:-1.5}"
BUILD_ROOT="${MIRA_IOS_FRIDA_BUILD_ROOT:-$ROOT_DIR/build/ios-frida-musl}"
SOURCE_DIR="${MIRA_IOS_FRIDA_SOURCE_DIR:-$ROOT_DIR/build/frida-src-$FRIDA_VERSION}"
OUTPUT_DIR="${MIRA_IOS_FRIDA_OUTPUT_DIR:-$ROOT_DIR/build/frida/devkit/$FRIDA_VERSION/linux-x86-musl}"
JOBS="${MIRA_IOS_FRIDA_JOBS:-2}"
HOST_MACHINE=""
FRIDA_BUILD_CONNECTIVITY="${MIRA_IOS_FRIDA_CONNECTIVITY:-disabled}"
FRIDA_BUILD_V8="${MIRA_IOS_FRIDA_V8:-disabled}"

log() {
  printf '[mira-ios-frida] %s\n' "$*"
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

patch_libunwind_x86_sigreturn() {
  MIRA_FRIDA_SOURCE_DIR="$SOURCE_DIR" python3 - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["MIRA_FRIDA_SOURCE_DIR"]) / "deps/libunwind/src/x86/Gos-linux.c"
text = path.read_text()
before = "      x86_sigreturn (sc);\n"
after = "      x86_sigreturn (cursor);\n"
caller = "x86_local_resume (unw_addr_space_t as, unw_cursor_t *cursor, void *arg)\n"
callee = """x86_sigreturn (unw_cursor_t *cursor)
{
  struct cursor *c = (struct cursor *) cursor;
  struct sigcontext *sc = (struct sigcontext *) c->sigcontext_addr;
"""
if text.count(caller) != 1 or text.count(callee) != 1 or text.count(before) + text.count(after) != 1:
    raise SystemExit(f"unexpected libunwind x86 sigreturn source: {path}")
# The callee reads both c->uc and c->sigcontext_addr from the complete cursor.
if before in text:
    path.write_text(text.replace(before, after, 1))
PY
}

apply_source_patches() {
  patch_libunwind_x86_sigreturn
  MIRA_FRIDA_SOURCE_DIR="$SOURCE_DIR" python3 - <<'PY'
from pathlib import Path
import os

root = Path(os.environ["MIRA_FRIDA_SOURCE_DIR"])
zutil = root / "deps" / "zlib" / "zutil.h"
text = zutil.read_text()
before = "#if defined(MACOS) || defined(TARGET_OS_MAC)\n"
after = "#if defined(MACOS) || (defined(TARGET_OS_MAC) && !defined(__APPLE__))\n"
if after not in text and before not in text:
    raise SystemExit(f"unexpected zutil.h contents: {zutil}")
if after not in text:
    zutil.write_text(text.replace(before, after, 1))

sysv = root / "deps" / "libffi" / "src" / "aarch64" / "sysv.S"
sysv_text = sysv.read_text()
sysv_before = "\tcfi_def_cfa(x1, 0);\n"
sysv_after = "\tcfi_def_cfa(x1, 0)\n"
if sysv_before in sysv_text:
    sysv_text = sysv_text.replace(sysv_before, sysv_after, 1)
sysv_before = "\tcfi_def_cfa(x1, 40);\n"
sysv_after = "\tcfi_def_cfa(x1, 40)\n"
if sysv_before in sysv_text:
    sysv_text = sysv_text.replace(sysv_before, sysv_after, 1)
sysv.write_text(sysv_text)

cfi = root / "deps" / "libffi" / "include" / "ffi_cfi.h"
cfi_text = cfi.read_text()
cfi_before = "#ifdef HAVE_AS_CFI_PSEUDO_OP\n"
cfi_after = "#if defined(HAVE_AS_CFI_PSEUDO_OP) && !(defined(__APPLE__) && defined(__aarch64__))\n"
if cfi_after not in cfi_text:
    if cfi_before not in cfi_text:
        raise SystemExit(f"unexpected ffi_cfi.h contents: {cfi}")
    cfi.write_text(cfi_text.replace(cfi_before, cfi_after, 1))

hidden_test = root / "deps" / "libffi" / "meson-scripts" / "test-cc-supports-hidden-visibility.py"
hidden_text = hidden_test.read_text()
hidden_before = "args += ['-Werror', '-S', infile, '-o', outfile]\n"
hidden_after = "args += ['-Werror', '-Wno-unused-command-line-argument', '-S', infile, '-o', outfile]\n"
if hidden_after not in hidden_text:
    if hidden_before not in hidden_text:
        raise SystemExit(f"unexpected hidden visibility test contents: {hidden_test}")
    hidden_test.write_text(hidden_text.replace(hidden_before, hidden_after, 1))

sdk_make = root / "Makefile.sdk.mk"
sdk_text = sdk_make.read_text()
sdk_before = """packages = \\\n\tzlib \\\n\txz \\\n\tbrotli \\\n\tminizip \\\n\tsqlite \\\n\tlibffi \\\n\tpcre2 \\\n\tglib \\\n\tglib-networking \\\n\tlibnice \\\n\tusrsctp \\\n\tlibgee \\\n\tjson-glib \\\n\tlibxml2 \\\n\tlibsoup \\\n\tcapstone \\\n\tquickjs \\\n\t$(NULL)\n"""
sdk_after = """packages = \\\n\tzlib \\\n\txz \\\n\tbrotli \\\n\tminizip \\\n\tsqlite \\\n\tlibffi \\\n\tpcre2 \\\n\tglib \\\n\tlibgee \\\n\tjson-glib \\\n\tlibxml2 \\\n\tlibsoup \\\n\tcapstone \\\n\tquickjs \\\n\t$(NULL)\n\nifneq ($(FRIDA_CONNECTIVITY), disabled)\npackages += \\\n\tglib-networking \\\n\tlibnice \\\n\tusrsctp \\\n\t$(NULL)\nendif\n"""
if sdk_after not in sdk_text:
    if sdk_before not in sdk_text:
        raise SystemExit(f"unexpected Makefile.sdk.mk package block: {sdk_make}")
    sdk_make.write_text(sdk_text.replace(sdk_before, sdk_after, 1))

glib_meson = root / "deps" / "glib" / "meson.build"
glib_meson_text = glib_meson.read_text()
glib_statx_before = """if host_system != 'android' and cc.compiles(statx_code, name : 'statx() test')\n  glib_conf.set('HAVE_STATX', 1)\nendif\n"""
glib_statx_after = """have_glibc_statx = cc.has_header_symbol('features.h', '__GLIBC__', required : false)\nif host_system != 'android' and have_glibc_statx and cc.compiles(statx_code, name : 'statx() test')\n  glib_conf.set('HAVE_STATX', 1)\nendif\n"""
if glib_statx_after not in glib_meson_text:
    if glib_statx_before not in glib_meson_text:
        raise SystemExit(f"unexpected glib statx block: {glib_meson}")
    glib_meson.write_text(glib_meson_text.replace(glib_statx_before, glib_statx_after, 1))

frida_mk = root / "releng" / "frida.mk"
frida_mk_text = frida_mk.read_text()
frida_mk_before = """frida_gum_flags := \\\n\t--default-library static \\\n\t$(FRIDA_FLAGS_COMMON) \\\n\t-Djailbreak=$(FRIDA_JAILBREAK) \\\n\t-Dgumpp=enabled \\\n\t-Dgumjs=enabled \\\n\t-Dv8=$(FRIDA_V8) \\\n\t-Ddatabase=$(FRIDA_DATABASE) \\\n\t-Dfrida_objc_bridge=$(FRIDA_OBJC_BRIDGE) \\\n\t-Dfrida_swift_bridge=$(FRIDA_SWIFT_BRIDGE) \\\n\t-Dfrida_java_bridge=$(FRIDA_JAVA_BRIDGE) \\\n\t-Dtests=enabled \\\n\t$(NULL)\n"""
frida_mk_after = """FRIDA_GUMPP ?= disabled\nFRIDA_GUM_TESTS ?= disabled\n\nfrida_gum_flags := \\\n\t--default-library static \\\n\t$(FRIDA_FLAGS_COMMON) \\\n\t-Djailbreak=$(FRIDA_JAILBREAK) \\\n\t-Dgumpp=$(FRIDA_GUMPP) \\\n\t-Dgumjs=enabled \\\n\t-Dv8=$(FRIDA_V8) \\\n\t-Ddatabase=$(FRIDA_DATABASE) \\\n\t-Dfrida_objc_bridge=$(FRIDA_OBJC_BRIDGE) \\\n\t-Dfrida_swift_bridge=$(FRIDA_SWIFT_BRIDGE) \\\n\t-Dfrida_java_bridge=$(FRIDA_JAVA_BRIDGE) \\\n\t-Dtests=$(FRIDA_GUM_TESTS) \\\n\t$(NULL)\n"""
if frida_mk_after not in frida_mk_text:
    if frida_mk_before not in frida_mk_text:
        raise SystemExit(f"unexpected releng/frida.mk gum flags block: {frida_mk}")
    frida_mk.write_text(frida_mk_text.replace(frida_mk_before, frida_mk_after, 1))

gum_meson = root / "frida-gum" / "meson.build"
gum_meson_text = gum_meson.read_text()
glibc_before = """if cc.compiles(glibc_src, name: 'compiling for glibc')\n  cdata.set('HAVE_GLIBC', 1)\nendif\n"""
glibc_after = """have_glibc = cc.compiles(glibc_src, name: 'compiling for glibc')\nif have_glibc\n  cdata.set('HAVE_GLIBC', 1)\nendif\n"""
if glibc_after not in gum_meson_text:
    if glibc_before not in gum_meson_text:
        raise SystemExit(f"unexpected frida-gum glibc block: {gum_meson}")
    gum_meson_text = gum_meson_text.replace(glibc_before, glibc_after, 1)

libucontext_anchor = """if host_os == 'android'\n  extra_libs_private += ['-llog']\nendif\n\nif host_os_family in ['linux', 'freebsd', 'qnx']\n"""
libucontext_block = """if host_os == 'android'\n  extra_libs_private += ['-llog']\nendif\n\nlibucontext_dep = disabler()\nif host_os == 'linux' and host_arch == 'x86' and not have_glibc\n  libucontext_dep = dependency('libucontext', required: false)\n  if libucontext_dep.found()\n    extra_deps += [libucontext_dep]\n    extra_requires_private += ['libucontext']\n  endif\nendif\n\nif host_os_family in ['linux', 'freebsd', 'qnx']\n"""
if libucontext_block not in gum_meson_text:
    if libucontext_anchor not in gum_meson_text:
        raise SystemExit(f"unexpected frida-gum libucontext anchor: {gum_meson}")
    gum_meson_text = gum_meson_text.replace(libucontext_anchor, libucontext_block, 1)

gum_meson.write_text(gum_meson_text)

fork_monitor = root / "frida-core" / "lib" / "payload" / "fork-monitor.vala"
fork_text = fork_monitor.read_text()
fork_helper = """\n\t\t[CCode (cname = \"gum_module_find_export_by_name\")]\n\t\tprivate static extern Gum.Address resolve_libc_symbol_address (string? module_name, string symbol_name);\n"""
if 'private static extern Gum.Address resolve_libc_symbol_address' not in fork_text:
    fork_helper_anchor = """\t\tprivate static void * vfork_impl;\n"""
    if fork_helper_anchor not in fork_text:
        raise SystemExit(f"unexpected fork-monitor helper anchor: {fork_monitor}")
    fork_text = fork_text.replace(fork_helper_anchor, fork_helper_anchor + fork_helper, 1)
fork_replacements = [
    ('fork_impl = Gum.Module.find_export_by_name (libc, "fork");',
     'fork_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "fork");'),
    ('vfork_impl = Gum.Module.find_export_by_name (libc, "vfork");',
     'vfork_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "vfork");'),
]
for before, after in fork_replacements:
    if after not in fork_text:
        if before not in fork_text:
            raise SystemExit(f"unexpected fork-monitor symbol line: {fork_monitor}")
        fork_text = fork_text.replace(before, after, 1)
fork_monitor.write_text(fork_text)

spawn_monitor = root / "frida-core" / "lib" / "payload" / "spawn-monitor.vala"
spawn_text = spawn_monitor.read_text()
spawn_helper = """#endif\n\n\t\t[CCode (cname = \"gum_module_find_export_by_name\")]\n\t\tprivate static extern Gum.Address resolve_libc_symbol_address (string? module_name, string symbol_name);\n\n\t\tpublic SpawnMonitor (SpawnHandler handler, MainContext main_context) {"""
spawn_helper_anchor = """#endif\n\n\t\tpublic SpawnMonitor (SpawnHandler handler, MainContext main_context) {"""
if 'private static extern Gum.Address resolve_libc_symbol_address' not in spawn_text:
    if spawn_helper_anchor not in spawn_text:
        raise SystemExit(f"unexpected spawn-monitor helper anchor: {spawn_monitor}")
    spawn_text = spawn_text.replace(spawn_helper_anchor, spawn_helper, 1)
spawn_before = 'interceptor.attach (Gum.Module.find_export_by_name (libc, "execve"), this);'
spawn_after = 'interceptor.attach ((void *) (uintptr) resolve_libc_symbol_address (libc, "execve"), this);'
if spawn_after not in spawn_text:
    if spawn_before not in spawn_text:
        raise SystemExit(f"unexpected spawn-monitor attach block: {spawn_monitor}")
    spawn_text = spawn_text.replace(spawn_before, spawn_after, 1)
spawn_monitor.write_text(spawn_text)

exit_monitor = root / "frida-core" / "lib" / "payload" / "exit-monitor.vala"
exit_text = exit_monitor.read_text()
exit_helper = """\n\t\t[CCode (cname = \"gum_module_find_export_by_name\")]\n\t\tprivate static extern Gum.Address resolve_libc_symbol_address (string? module_name, string symbol_name);\n"""
if 'private static extern Gum.Address resolve_libc_symbol_address' not in exit_text:
    exit_helper_anchor = """\t\tprivate MainLoop loop;\n"""
    if exit_helper_anchor not in exit_text:
        raise SystemExit(f"unexpected exit-monitor helper anchor: {exit_monitor}")
    exit_text = exit_text.replace(exit_helper_anchor, exit_helper_anchor + exit_helper, 1)
exit_before = 'interceptor.attach (Gum.Module.find_export_by_name (libc, symbol), listener);'
exit_after = 'interceptor.attach ((void *) (uintptr) resolve_libc_symbol_address (libc, symbol), listener);'
if exit_after not in exit_text:
    if exit_before not in exit_text:
        raise SystemExit(f"unexpected exit-monitor attach block: {exit_monitor}")
    exit_text = exit_text.replace(exit_before, exit_after, 1)
exit_monitor.write_text(exit_text)

cloak = root / "frida-core" / "lib" / "payload" / "cloak.vala"
cloak_text = cloak.read_text()
cloak_helper = """\n\t\t[CCode (cname = \"gum_module_find_export_by_name\")]\n\t\tprivate static extern Gum.Address resolve_libc_symbol_address (string? module_name, string symbol_name);\n"""
if 'private static extern Gum.Address resolve_libc_symbol_address' not in cloak_text:
    cloak_helper_anchor = """\t\tpublic weak DirListFilter filter {\n\t\t\tget;\n\t\t\tconstruct;\n\t\t}\n"""
    if cloak_helper_anchor not in cloak_text:
        raise SystemExit(f"unexpected cloak helper anchor: {cloak}")
    cloak_text = cloak_text.replace(cloak_helper_anchor, cloak_helper_anchor + cloak_helper, 1)
cloak_replacements = [
    ('interceptor.attach (Gum.Module.find_export_by_name (libc, "opendir"), open_listener);',
     'interceptor.attach ((void *) (uintptr) resolve_libc_symbol_address (libc, "opendir"), open_listener);'),
    ('interceptor.attach (Gum.Module.find_export_by_name (libc, "closedir"), close_listener);',
     'interceptor.attach ((void *) (uintptr) resolve_libc_symbol_address (libc, "closedir"), close_listener);'),
    ('var readdir_impl = Gum.Module.find_export_by_name (libc, "readdir");',
     'var readdir_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "readdir");'),
    ('var readdir64_impl = Gum.Module.find_export_by_name (libc, "readdir64");',
     'var readdir64_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "readdir64");'),
    ('var readdir_r_impl = Gum.Module.find_export_by_name (libc, "readdir_r");',
     'var readdir_r_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "readdir_r");'),
    ('var readdir64_r_impl = Gum.Module.find_export_by_name (libc, "readdir64_r");',
     'var readdir64_r_impl = (void *) (uintptr) resolve_libc_symbol_address (libc, "readdir64_r");'),
]
for before, after in cloak_replacements:
    if after not in cloak_text:
        if before not in cloak_text:
            raise SystemExit(f"unexpected cloak symbol block: {cloak}")
        cloak_text = cloak_text.replace(before, after, 1)
cloak.write_text(cloak_text)

fd_guard = root / "frida-core" / "lib" / "payload" / "fd-guard.vala"
fd_guard_text = fd_guard.read_text()
fd_guard_helper = """\n\t\t[CCode (cname = \"gum_module_find_export_by_name\")]\n\t\tprivate static extern Gum.Address resolve_libc_symbol_address (string? module_name, string symbol_name);\n"""
if 'private static extern Gum.Address resolve_libc_symbol_address' not in fd_guard_text:
    fd_guard_helper_anchor = """#else\n\tpublic class FileDescriptorGuard : Object {\n\t\tpublic Gum.MemoryRange agent_range {\n\t\t\tget;\n\t\t\tconstruct;\n\t\t}\n"""
    if fd_guard_helper_anchor not in fd_guard_text:
        raise SystemExit(f"unexpected fd-guard helper anchor: {fd_guard}")
    fd_guard_text = fd_guard_text.replace(fd_guard_helper_anchor, fd_guard_helper_anchor + fd_guard_helper, 1)
fd_guard_before = 'var close = Gum.Module.find_export_by_name (Gum.Process.query_libc_name (), "close");'
fd_guard_after = 'var close = (void *) (uintptr) resolve_libc_symbol_address (Gum.Process.query_libc_name (), "close");'
if fd_guard_after not in fd_guard_text:
    if fd_guard_before not in fd_guard_text:
        raise SystemExit(f"unexpected fd-guard close block: {fd_guard}")
    fd_guard_text = fd_guard_text.replace(fd_guard_before, fd_guard_after, 1)
fd_guard.write_text(fd_guard_text)

linux_helper = root / "frida-core" / "src" / "linux" / "frida-helper-backend.vala"
linux_helper_text = linux_helper.read_text()
linux_helper_replacements = [
    ('private Gee.HashMap<uint, uint> watch_sources = new Gee.HashMap<uint, uint> ();', 'private Gee.HashMap<uint, void *> watch_sources = new Gee.HashMap<uint, void *> ();'),
    ('private Gee.HashMap<uint, uint> inject_expiry_by_id = new Gee.HashMap<uint, uint> ();', 'private Gee.HashMap<uint, void *> inject_expiry_by_id = new Gee.HashMap<uint, void *> ();'),
    ('watch_sources[pid] = ChildWatch.add ((Pid) pid, on_child_dead);', 'watch_sources[pid] = (void *) (uintptr) ChildWatch.add ((Pid) pid, on_child_dead);'),
    ("""uint watch_id;
			if (watch_sources.unset (pid, out watch_id))
				Source.remove (watch_id);""", """void * watch_id;
			if (watch_sources.unset (pid, out watch_id))
				Source.remove ((uint) (uintptr) watch_id);"""),
    ("""uint previous_timer;
			if (inject_expiry_by_id.unset (id, out previous_timer))
				Source.remove (previous_timer);""", """void * previous_timer;
			if (inject_expiry_by_id.unset (id, out previous_timer))
				Source.remove ((uint) (uintptr) previous_timer);"""),
    ('inject_expiry_by_id[id] = Timeout.add_seconds (20, () => {', 'inject_expiry_by_id[id] = (void *) (uintptr) Timeout.add_seconds (20, () => {'),
    ("""uint timer;
			var found = inject_expiry_by_id.unset (id, out timer);
			assert (found);

			Source.remove (timer);""", """void * timer;
			var found = inject_expiry_by_id.unset (id, out timer);
			assert (found);

			Source.remove ((uint) (uintptr) timer);"""),
]
for before, after in linux_helper_replacements:
    if after not in linux_helper_text:
        if before not in linux_helper_text:
            raise SystemExit(f"unexpected linux helper block while patching {linux_helper}: {before[:80]}")
        linux_helper_text = linux_helper_text.replace(before, after, 1)
linux_helper.write_text(linux_helper_text)

portal_client = root / "frida-core" / "lib" / "payload" / "portal-client.vala"
portal_text = portal_client.read_text()
portal_before = """AgentSessionProvider provider = this;
			registrations.add (connection.register_object (ObjectPath.AGENT_SESSION_PROVIDER, provider));"""
portal_after = """AgentSessionProvider provider = this;
			uint registration_id = connection.register_object (ObjectPath.AGENT_SESSION_PROVIDER, provider);
			registrations.add (registration_id);"""
if portal_after not in portal_text:
    if portal_before not in portal_text:
        raise SystemExit(f"unexpected portal-client registration block: {portal_client}")
    portal_client.write_text(portal_text.replace(portal_before, portal_after, 1))

agent_meson = root / "frida-core" / "lib" / "agent" / "meson.build"
agent_meson_text = agent_meson.read_text()
agent_meson_before = """if host_os_family == 'darwin'
  extra_link_args += ['-Wl,-exported_symbol,_frida_agent_main']
elif host_os_family != 'windows'
  extra_link_args += ['-Wl,--version-script,' + join_paths(meson.current_source_dir(), 'frida-agent.version')]
endif
"""
agent_meson_after = """if host_os_family == 'darwin'
  extra_link_args += ['-Wl,-exported_symbol,_frida_agent_main']
elif host_os_family != 'windows'
  agent_version_script = 'frida-agent.version'
  if host_os_family == 'linux' and host_arch == 'x86' and not cdata.has('HAVE_ANDROID')
    agent_version_script = 'frida-agent-no-jni.version'
  endif
  extra_link_args += ['-Wl,--version-script,' + join_paths(meson.current_source_dir(), agent_version_script)]
endif
"""
if agent_meson_after not in agent_meson_text:
    if agent_meson_before not in agent_meson_text:
        raise SystemExit(f"unexpected agent meson version-script block: {agent_meson}")
    agent_meson.write_text(agent_meson_text.replace(agent_meson_before, agent_meson_after, 1))

agent_no_jni = root / "frida-core" / "lib" / "agent" / "frida-agent-no-jni.version"
if not agent_no_jni.exists():
    agent_no_jni.write_text("{\n  global:\n    frida_agent_main;\n\n  local:\n    *;\n};\n")
PY
}

sync_glib_gvdb_revision() {
  local glib_dir="$SOURCE_DIR/deps/glib" expected actual
  expected="$(git -C "$glib_dir" rev-parse HEAD:subprojects/gvdb)"
  # Frida clones recursively before checking out GLib's pin, leaving gvdb at a newer revision.
  git -C "$glib_dir" submodule update --init --checkout --recursive -- subprojects/gvdb
  actual="$(git -C "$glib_dir/subprojects/gvdb" rev-parse HEAD)"
  if [[ "$actual" != "$expected" ]]; then
    echo "GLib gvdb revision mismatch: expected=$expected actual=$actual" >&2
    return 1
  fi
  log "GLib gvdb revision: $actual"
}

prepare_source_tree() {
  mkdir -p "$BUILD_ROOT" "$(dirname "$OUTPUT_DIR")"

  if [[ ! -d "$SOURCE_DIR/.git" ]]; then
    git clone --branch "$FRIDA_VERSION" https://github.com/frida/frida.git "$SOURCE_DIR"
    (
      cd "$SOURCE_DIR"
      git submodule update --init --depth 1 \
        releng/meson \
        frida-core \
        frida-gum \
        frida-python \
        frida-tools
    )
  else
    log "reuse existing source tree: $SOURCE_DIR"
    git -C "$SOURCE_DIR" checkout --force "$FRIDA_VERSION"
  fi

  (
    cd "$SOURCE_DIR"
    FRIDA_HOST=linux-x86 make -f Makefile.sdk.mk deps/.zlib-stamp deps/.libffi-stamp deps/.glib-stamp deps/.libunwind-stamp
  )

  sync_glib_gvdb_revision
  apply_source_patches
}

prepare_native_env() {
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64)
      HOST_MACHINE="macos-arm64"
      ;;
    Darwin:x86_64)
      HOST_MACHINE="macos-x86_64"
      ;;
    Linux:x86_64)
      HOST_MACHINE="linux-x86_64"
      ;;
    Linux:i?86)
      HOST_MACHINE="linux-x86"
      ;;
    *)
      echo "Unsupported build host: $(uname -s):$(uname -m)" >&2
      exit 1
      ;;
  esac

  if [[ ! -f "$SOURCE_DIR/build/frida-$HOST_MACHINE.txt" ]]; then
    log "prepare native env for $HOST_MACHINE"
    (
      cd "$SOURCE_DIR"
      FRIDA_HOST="$HOST_MACHINE" FRIDA_CROSS=no ./releng/setup-env.sh
    )
  fi
}

patch_native_env_files() {
  if [[ "$HOST_MACHINE" != macos-* ]]; then
    return
  fi

  local machine_file
  machine_file="$SOURCE_DIR/build/fs-$HOST_MACHINE.txt"
  if [[ ! -f "$machine_file" ]]; then
    machine_file="$SOURCE_DIR/build/frida-$HOST_MACHINE.txt"
  fi

  MIRA_FRIDA_MACHINE_FILE="$machine_file" python3 - <<'PY'
from pathlib import Path
import os
import re

path = Path(os.environ["MIRA_FRIDA_MACHINE_FILE"])
text = path.read_text()
flags = [
    "'-Wno-error=incompatible-function-pointer-types'",
    "'-Wno-int-conversion'",
]
pattern = re.compile(r"^c_like_flags = \[(.*)\]$", re.MULTILINE)
match = pattern.search(text)
if match is None:
    raise SystemExit(f"missing c_like_flags in {path}")
existing = match.group(1).strip()
if all(flag in existing for flag in flags):
    raise SystemExit(0)
prefix = ", ".join(flags)
replacement = "c_like_flags = [" + prefix
if existing:
    replacement += ", " + existing
replacement += "]"
path.write_text(pattern.sub(replacement, text, count=1))
PY
}

patch_cross_machine_file() {
  local machine_file="$1"
  if [[ ! -f "$machine_file" ]]; then
    return
  fi

  MIRA_FRIDA_MACHINE_FILE="$machine_file" python3 - <<'PY'
from pathlib import Path
import os
import re

path = Path(os.environ["MIRA_FRIDA_MACHINE_FILE"])
text = path.read_text()
flags = [
    "'-Wno-error=incompatible-function-pointer-types'",
    "'-Wno-int-conversion'",
]
pattern = re.compile(r"^c_like_flags = \[(.*)\]$", re.MULTILINE)
match = pattern.search(text)
if match is None:
    raise SystemExit(f"missing c_like_flags in {path}")
existing = match.group(1)
if all(flag in existing for flag in flags):
    raise SystemExit(0)
prefix = ", ".join(flags)
replacement = "c_like_flags = [" + prefix
if existing.strip():
    replacement += ", " + existing.strip()
replacement += "]"
path.write_text(pattern.sub(replacement, text, count=1))
PY
}

resolve_macos_tool() {
  local name="$1" formula="$2" configured="${3:-}"
  local candidate="" prefix=""
  if [[ -n "$configured" ]]; then
    candidate="$(command -v -- "$configured" 2>/dev/null || true)"
    if [[ ! -f "$candidate" || ! -x "$candidate" ]]; then
      echo "Configured tool for $name is not executable." >&2
      return 1
    fi
  else
    if command -v brew >/dev/null 2>&1; then
      prefix="$(brew --prefix "$formula" 2>/dev/null || true)"
      if [[ -n "$prefix" && -f "$prefix/bin/$name" && -x "$prefix/bin/$name" ]]; then
        candidate="$prefix/bin/$name"
      fi
    fi
    if [[ -z "$candidate" ]]; then
      candidate="$(command -v -- "$name" 2>/dev/null || true)"
    fi
    if [[ ! -f "$candidate" || ! -x "$candidate" ]]; then
      if [[ "$formula" == llvm || "$formula" == lld ]] && command -v brew >/dev/null 2>&1; then
        prefix="$(brew --prefix "$formula@21" 2>/dev/null || true)"
        if [[ -n "$prefix" && -f "$prefix/bin/$name" && -x "$prefix/bin/$name" ]]; then
          candidate="$prefix/bin/$name"
        fi
      fi
    fi
    if [[ ! -f "$candidate" || ! -x "$candidate" ]]; then
      echo "Unable to find executable $name. Install Homebrew $formula or configure the tool explicitly." >&2
      return 1
    fi
  fi
  printf '%s/%s\n' "$(cd "$(dirname "$candidate")" && pwd)" "$(basename "$candidate")"
}

prepare_macos_musl_wrapper() {
  require_tool python3

  local zig_bin lld_bin wrapdir name wrapper_name
  zig_bin="$(resolve_macos_tool zig zig)"
  lld_bin="$(resolve_macos_tool ld.lld lld "${LD:-}")"
  AR="$(resolve_macos_tool llvm-ar llvm "${AR:-}")"
  NM="$(resolve_macos_tool llvm-nm llvm "${NM:-}")"
  RANLIB="$(resolve_macos_tool llvm-ranlib llvm "${RANLIB:-}")"
  STRIP="$(resolve_macos_tool llvm-strip llvm "${STRIP:-}")"
  READELF="$(resolve_macos_tool llvm-readelf llvm "${READELF:-}")"
  OBJCOPY="$(resolve_macos_tool llvm-objcopy llvm "${OBJCOPY:-}")"
  OBJDUMP="$(resolve_macos_tool llvm-objdump llvm "${OBJDUMP:-}")"
  wrapdir="$SOURCE_DIR/build/musl-wrap"
  mkdir -p "$wrapdir"

  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'zig_bin=%q\nlld_bin=%q\n' "$zig_bin" "$lld_bin"
    cat <<'EOF'
for arg in "$@"; do
  if [[ "$arg" == "--version" ]]; then
    exec "$zig_bin" cc -target x86-linux-musl --version
  fi
  if [[ "$arg" == "-Wl,--version" ]]; then
    exec "$lld_bin" --version
  fi
done
use_target="x86-linux-musl"
seen_E=0
seen_dM=0
for arg in "$@"; do
  [[ "$arg" == "-E" ]] && seen_E=1
  [[ "$arg" == "-dM" ]] && seen_dM=1
done
if [[ $seen_E -eq 1 && $seen_dM -eq 1 ]]; then
  use_target="i386-linux-musl"
fi
args=()
for arg in "$@"; do
  case "$arg" in
    -m32|-march=*)
      ;;
    -c|-S)
      # Meson's libffi map target adds -c to -E; Zig otherwise assembles the map.
      if [[ $seen_E -eq 0 ]]; then args+=("$arg"); fi
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done
args+=("-Wno-error=incompatible-function-pointer-types" "-Wno-int-conversion")
exec "$zig_bin" cc -target "$use_target" "${args[@]}"
EOF
  } > "$wrapdir/i686-linux-musl-gcc"

  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'zig_bin=%q\nlld_bin=%q\n' "$zig_bin" "$lld_bin"
    cat <<'EOF'
for arg in "$@"; do
  if [[ "$arg" == "--version" ]]; then
    exec "$zig_bin" c++ -target x86-linux-musl --version
  fi
  if [[ "$arg" == "-Wl,--version" ]]; then
    exec "$lld_bin" --version
  fi
done
use_target="x86-linux-musl"
seen_E=0
seen_dM=0
for arg in "$@"; do
  [[ "$arg" == "-E" ]] && seen_E=1
  [[ "$arg" == "-dM" ]] && seen_dM=1
done
if [[ $seen_E -eq 1 && $seen_dM -eq 1 ]]; then
  use_target="i386-linux-musl"
fi
args=()
for arg in "$@"; do
  case "$arg" in
    -m32|-march=*)
      ;;
    -c|-S)
      # Meson's libffi map target adds -c to -E; Zig otherwise assembles the map.
      if [[ $seen_E -eq 0 ]]; then args+=("$arg"); fi
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done
args+=("-Wno-error=incompatible-function-pointer-types" "-Wno-int-conversion")
exec "$zig_bin" c++ -target "$use_target" "${args[@]}"
EOF
  } > "$wrapdir/i686-linux-musl-g++"

  chmod +x "$wrapdir/i686-linux-musl-gcc" "$wrapdir/i686-linux-musl-g++"

  # Frida 16.0.7's setup-env.sh evals these variables as shell command arrays.
  # Pass fixed command names; consume selected paths only in quoted wrappers.
  LD="$lld_bin"
  for name in AR LD NM RANLIB STRIP READELF OBJCOPY OBJDUMP; do
    wrapper_name="mira-musl-$name"
    {
      printf '#!/usr/bin/env bash\nset -euo pipefail\n'
      printf 'exec %q "$@"\n' "${!name}"
    } > "$wrapdir/$wrapper_name"
    chmod +x "$wrapdir/$wrapper_name"
    printf -v "$name" '%s' "$wrapper_name"
  done

  export PATH="$wrapdir:$PATH"
  export FRIDA_LIBC=musl
  export CC=i686-linux-musl-gcc
  export CXX=i686-linux-musl-g++
  export AR LD NM RANLIB STRIP READELF OBJCOPY OBJDUMP
}

prepare_linux_cross_env() {
  log "prepare linux-x86 musl cross env"
  (
    cd "$SOURCE_DIR"
    rm -rf build/sdk-linux-x86
    rm -f build/frida-env-linux-x86.rc build/frida-linux-x86.txt
    FRIDA_HOST=linux-x86 FRIDA_CROSS=yes FRIDA_CONNECTIVITY="$FRIDA_BUILD_CONNECTIVITY" FRIDA_V8="$FRIDA_BUILD_V8" ./releng/setup-env.sh
  )
  patch_cross_machine_file "$SOURCE_DIR/build/fs-linux-x86.txt"
  patch_cross_machine_file "$SOURCE_DIR/build/frida-linux-x86.txt"
}

build_linux_sdk() {
  log "build linux-x86 musl sdk"
  patch_cross_machine_file "$SOURCE_DIR/build/fs-linux-x86.txt"
  (
    cd "$SOURCE_DIR"
    rm -rf build/fs-linux-x86 build/fs-tmp-linux-x86 build/sdk-linux-x86 build/sdk-linux-x86.tar.bz2
    python3 releng/generate-version-header.py build/frida-version.h
    FRIDA_HOST=linux-x86 FRIDA_CONNECTIVITY="$FRIDA_BUILD_CONNECTIVITY" FRIDA_V8="$FRIDA_BUILD_V8" make -f Makefile.sdk.mk all -j"$JOBS"
  )
}

build_libucontext_for_sdk() {
  local libucontext_src sdk_prefix
  libucontext_src="$BUILD_ROOT/libucontext-src"
  sdk_prefix="$SOURCE_DIR/build/sdk-linux-x86"

  log "build libucontext for linux-x86 musl sdk"
  if [[ ! -d "$libucontext_src/.git" ]]; then
    git clone --depth 1 --branch "libucontext-$LIBUCONTEXT_VERSION" https://github.com/kaniini/libucontext.git "$libucontext_src"
  else
    if ! git -C "$libucontext_src" rev-parse -q --verify "refs/tags/libucontext-$LIBUCONTEXT_VERSION" >/dev/null; then
      git -C "$libucontext_src" fetch --tags origin
    fi
    git -C "$libucontext_src" checkout --force "libucontext-$LIBUCONTEXT_VERSION"
    git -C "$libucontext_src" clean -fdx
  fi

  MIRA_IOS_LIBUCONTEXT_DIR="$libucontext_src" python3 - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["MIRA_IOS_LIBUCONTEXT_DIR"]) / "Makefile"
text = path.read_text()

header = "LIBUCONTEXT_HOST_OS ?= $(shell uname)\n"
if not text.startswith(header):
    text = header + text

text = text.replace("ifneq ($(shell uname),Darwin)", "ifneq ($(LIBUCONTEXT_HOST_OS),Darwin)")
text = text.replace("ifeq ($(shell uname),Darwin)", "ifeq ($(LIBUCONTEXT_HOST_OS),Darwin)")

path.write_text(text)
PY

  (
    cd "$libucontext_src"
    make clean >/dev/null 2>&1 || true
    make \
      ARCH=x86 \
      LIBUCONTEXT_HOST_OS=Linux \
      CC="$CC" \
      AR="$AR" \
      RANLIB="$RANLIB" \
      prefix="$sdk_prefix" \
      libdir="$sdk_prefix/lib" \
      shared_libdir="$sdk_prefix/lib" \
      static_libdir="$sdk_prefix/lib" \
      includedir="$sdk_prefix/include" \
      pkgconfigdir="$sdk_prefix/lib/pkgconfig" \
      BUILD_POSIX=no \
      EXPORT_UNPREFIXED=yes \
      all
    mkdir -p \
      "$sdk_prefix/lib" \
      "$sdk_prefix/lib/pkgconfig" \
      "$sdk_prefix/include/libucontext"
    cp -f libucontext.so "$sdk_prefix/lib/libucontext.so.1"
    ln -sf libucontext.so.1 "$sdk_prefix/lib/libucontext.so"
    cp -f libucontext.a "$sdk_prefix/lib/libucontext.a"
    cp -f libucontext.pc "$sdk_prefix/lib/pkgconfig/libucontext.pc"
    cp -f include/libucontext/libucontext.h "$sdk_prefix/include/libucontext/libucontext.h"
    cp -f arch/common/include/libucontext/bits.h "$sdk_prefix/include/libucontext/bits.h"
  )
}

build_linux_core() {
  log "build linux-x86 musl core"
  MIRA_FRIDA_SOURCE_DIR="$SOURCE_DIR" python3 - <<'PY'
from pathlib import Path
import shutil
import os
root = Path(os.environ["MIRA_FRIDA_SOURCE_DIR"])
for relative in (
    "build/tmp-linux-x86/frida-gum",
    "build/tmp-linux-x86/frida-core",
    "build/frida-linux-x86",
):
    shutil.rmtree(root / relative, ignore_errors=True)
PY
  (
    cd "$SOURCE_DIR"
    make git-submodule-stamps
    python3 releng/generate-version-header.py build/frida-version.h
    FRIDA_CONNECTIVITY="$FRIDA_BUILD_CONNECTIVITY" \
    FRIDA_V8="$FRIDA_BUILD_V8" \
    FRIDA_GUMPP=disabled \
    FRIDA_GUM_TESTS=disabled \
    make -f Makefile.linux.mk core-linux-x86 -j"$JOBS"
  )
}

generate_devkit() {
  log "generate linux-x86-musl devkit"
  mkdir -p "$OUTPUT_DIR"
  (
    cd "$SOURCE_DIR"
    python3 releng/devkit.py frida-core linux-x86 "$OUTPUT_DIR"
  )
}

main() {
  require_tool git
  require_tool make
  require_tool python3

  prepare_source_tree
  prepare_native_env
  patch_native_env_files
  prepare_macos_musl_wrapper
  prepare_linux_cross_env
  build_linux_sdk
  prepare_linux_cross_env
  build_libucontext_for_sdk
  build_linux_core
  generate_devkit

  log "generated musl-native Frida devkit at: $OUTPUT_DIR"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
