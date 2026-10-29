"""Run the generated musl compiler wrappers against discovered executable tools."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/ios/build-frida-musl-devkit.sh"
LLVM_TOOLS = ("llvm-ar", "llvm-nm", "llvm-ranlib", "llvm-strip", "llvm-readelf", "llvm-objcopy", "llvm-objdump")
TOOL_VARIABLES = ("AR", "LD", "NM", "RANLIB", "STRIP", "READELF", "OBJCOPY", "OBJDUMP")

# Verbatim reader from Frida 16.0.7 releng/setup-env.sh. Keep this legacy
# consumer independent of the wrappers, without requiring a downloaded build tree.
FRIDA_TOOLCHAIN_READER = r"""
read_toolchain_variable ()
{
  local result_var_name=$1
  local env_var_name=$2
  local fallback_value=$3

  if [ $host_machine == $build_machine ] && [ "$FRIDA_CROSS" == yes ]; then
    local contextual_env_var_name=${env_var_name}_FOR_BUILD
  else
    local contextual_env_var_name=${env_var_name}
  fi

  eval "$result_var_name=(${!contextual_env_var_name:-$fallback_value})"
}
"""


class FridaMuslToolchainTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.prefix = self.root / "tool chain $(touch UNEXPECTED_EXECUTION)"
        self.source = self.root / "frida source"
        self.prefixes = {name: self.prefix / name for name in ("llvm", "lld", "zig")}
        for prefix in self.prefixes.values():
            (prefix / "bin").mkdir(parents=True)
        for name in LLVM_TOOLS:
            self.fake_tool(self.prefixes["llvm"] / "bin" / name)
        self.fake_tool(self.prefixes["lld"] / "bin/ld.lld")
        self.fake_tool(self.prefixes["zig"] / "bin/zig")
        self.executable(self.bin / "brew", "import sys\nprefixes = " + repr({key: str(value) for key, value in self.prefixes.items()}) + "\nif len(sys.argv) != 3 or sys.argv[1] != '--prefix' or sys.argv[2] not in prefixes:\n    raise SystemExit(1)\nprint(prefixes[sys.argv[2]])\n")
        self.environment = dict(os.environ)
        for name in (*TOOL_VARIABLES, "ZIG", "BASH_ENV", "ENV"):
            self.environment.pop(name, None)
        self.environment.update(PATH=str(self.bin) + os.pathsep + os.defpath, MIRA_IOS_FRIDA_SOURCE_DIR=str(self.source), FRIDA_VERSION="16.0.7")

    def executable(self, path, body):
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def fake_tool(self, path):
        self.executable(path, "import json, sys\nprint(json.dumps({'tool': " + repr(path.name) + ", 'args': sys.argv[1:]}))\n")

    def prepare(self, extra="", check=True):
        return subprocess.run(["bash", "-c", 'source "$1"\nprepare_macos_musl_wrapper\n' + extra, "test", str(SCRIPT)], cwd=self.root, env=self.environment, capture_output=True, text=True, check=check)

    def wrapper(self, compiler, *args):
        path = self.source / "build/musl-wrap" / compiler
        result = subprocess.run([str(path), *args], cwd=self.root, env=self.environment, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def prepare_real_zig(self):
        zig = shutil.which("zig")
        if zig is None:
            self.skipTest("Zig is required for the real compiler smoke test")
        selected = self.prefixes["zig"] / "bin/zig"
        selected.unlink()
        selected.symlink_to(Path(zig).resolve())
        self.environment["ZIG_GLOBAL_CACHE_DIR"] = str(self.root / "zig-cache")
        self.prepare()

    def run_real_compiler(self, compiler, *args):
        result = subprocess.run([str(self.source / "build/musl-wrap" / compiler), *map(str, args)], cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unversioned_homebrew_tools_and_wrapper_version_probes(self):
        self.prepare()
        for compiler, language in (("i686-linux-musl-gcc", "cc"), ("i686-linux-musl-g++", "c++")):
            with self.subTest(compiler=compiler):
                self.assertEqual(self.wrapper(compiler, "-Wl,--version"), {"tool": "ld.lld", "args": ["--version"]})
                self.assertEqual(self.wrapper(compiler, "--version"), {"tool": "zig", "args": [language, "-target", "x86-linux-musl", "--version"]})
        self.assertFalse((self.root / "UNEXPECTED_EXECUTION").exists())

    def test_compiler_target_and_existing_argument_handling_are_preserved(self):
        self.prepare()
        for compiler, language in (("i686-linux-musl-gcc", "cc"), ("i686-linux-musl-g++", "c++")):
            with self.subTest(compiler=compiler):
                normal = self.wrapper(compiler, "-m32", "-march=pentium4", "hello source.c", "-o", "output.o")
                self.assertEqual(normal["args"], [language, "-target", "x86-linux-musl", "hello source.c", "-o", "output.o", "-Wno-error=incompatible-function-pointer-types", "-Wno-int-conversion"])
                macros = self.wrapper(compiler, "-E", "-dM", "input.h")
                self.assertEqual(macros["args"][:3], [language, "-target", "i386-linux-musl"])

    def test_preprocessing_takes_precedence_over_compile_and_assembly_modes(self):
        self.prepare()
        for compiler in ("i686-linux-musl-gcc", "i686-linux-musl-g++"):
            for modes in (("-E", "-c"), ("-c", "-E"), ("-E", "-S"), ("-S", "-E", "-c")):
                with self.subTest(compiler=compiler, modes=modes):
                    args = self.wrapper(compiler, *modes, "-P", "-x", "assembler-with-cpp", "libffi.map.in", "-o", "libffi.map")["args"]
                    self.assertIn("-E", args)
                    self.assertNotIn("-c", args)
                    self.assertNotIn("-S", args)
                    self.assertEqual(args[4:-2], ["-P", "-x", "assembler-with-cpp", "libffi.map.in", "-o", "libffi.map"])
            for mode in ("-c", "-S"):
                with self.subTest(compiler=compiler, mode=mode):
                    args = self.wrapper(compiler, mode, "input.c", "-o", "output")["args"]
                    self.assertEqual(args[3:-2], [mode, "input.c", "-o", "output"])

    def test_real_zig_preprocesses_libffi_version_script(self):
        self.prepare_real_zig()
        source = self.root / "libffi.map.in"
        source.write_text("#define ABI LIBFFI_BASE_8.0\nABI {\n  global:\n    ffi_call;\n  local:\n    *;\n};\n")
        for compiler in ("i686-linux-musl-gcc", "i686-linux-musl-g++"):
            for mode in ("-c", "-S"):
                with self.subTest(compiler=compiler, mode=mode):
                    output = self.root / (compiler + mode + ".map")
                    dependencies = output.with_suffix(".d")
                    self.run_real_compiler(compiler, "-m32", "-march=pentium4", "-E", "-P", "-x", "assembler-with-cpp", "-DGENERATE_LIBFFI_MAP", "-MD", "-MQ", output, "-MF", dependencies, "-o", output, mode, source)
                    self.assertEqual(" ".join(output.read_text().split()), "LIBFFI_BASE_8.0 { global: ffi_call; local: *; };")
                    self.assertIn(source.name, dependencies.read_text())

    def test_real_zig_still_compiles_objects_and_emits_assembly(self):
        self.prepare_real_zig()
        for compiler, extension in (("i686-linux-musl-gcc", "c"), ("i686-linux-musl-g++", "cpp")):
            source = self.root / ("answer." + extension)
            source.write_text("int answer(void) { return 42; }\n")
            with self.subTest(compiler=compiler):
                obj = source.with_name(source.name + ".o")
                self.run_real_compiler(compiler, "-c", source, "-o", obj)
                self.assertEqual(obj.read_bytes()[:5], b"\x7fELF\x01")
                assembly = source.with_name(source.name + ".s")
                self.run_real_compiler(compiler, "-S", source, "-o", assembly)
                self.assertIn("answer", assembly.read_text())

    def test_every_exported_llvm_tool_is_executable(self):
        result = self.prepare('for name in AR LD NM RANLIB STRIP READELF OBJCOPY OBJDUMP; do "${!name}" --version; done')
        actual = [json.loads(line)["tool"] for line in result.stdout.splitlines()]
        self.assertEqual(actual, ["llvm-ar", "ld.lld", "llvm-nm", "llvm-ranlib", "llvm-strip", "llvm-readelf", "llvm-objcopy", "llvm-objdump"])

    def test_explicit_linker_wins_and_invalid_override_fails_early(self):
        linker = self.root / "custom ld.lld"
        self.fake_tool(linker)
        self.environment["LD"] = str(linker)
        self.prepare()
        self.assertEqual(self.wrapper("i686-linux-musl-gcc", "-Wl,--version")["tool"], "custom ld.lld")
        self.environment["LD"] = str(self.root / "missing")
        result = self.prepare(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Configured tool for ld.lld is not executable", result.stderr)

    def test_explicit_zig_override_wins_and_preserves_literal_path(self):
        zig = self.root / "pinned zig $(touch ZIG_OVERRIDE_EXECUTION)"
        self.fake_tool(zig)
        self.environment["ZIG"] = str(zig)
        self.prepare()
        for compiler, language in (("i686-linux-musl-gcc", "cc"), ("i686-linux-musl-g++", "c++")):
            with self.subTest(compiler=compiler):
                version = self.wrapper(compiler, "--version")
                self.assertEqual(version, {"tool": zig.name, "args": [language, "-target", "x86-linux-musl", "--version"]})
                compilation = self.wrapper(compiler, "-c", "input.c", "-o", "output.o")
                self.assertEqual(compilation["tool"], zig.name)
                self.assertEqual(compilation["args"][3:7], ["-c", "input.c", "-o", "output.o"])
        self.assertFalse((self.root / "ZIG_OVERRIDE_EXECUTION").exists())

    def test_invalid_zig_override_fails_before_generating_wrappers(self):
        self.environment["ZIG"] = str(self.root / "missing zig")
        result = self.prepare(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Configured tool for zig is not executable", result.stderr)
        self.assertFalse((self.source / "build/musl-wrap").exists())

    def test_upstream_cross_environment_consumes_selected_tools_without_evaluation(self):
        linker = self.prefix / "explicit linker $(touch OVERRIDE_EXECUTION)"
        self.fake_tool(linker)
        self.environment["LD"] = str(linker)
        releng = self.source / "releng"
        releng.mkdir(parents=True)
        setup = releng / "setup-env.sh"
        setup.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + FRIDA_TOOLCHAIN_READER + r"""
host_machine=linux-x86
build_machine=macos-arm64
for name in CC CXX AR LD NM RANLIB STRIP READELF OBJCOPY OBJDUMP; do
  read_toolchain_variable selected "$name" unused
  if [[ "$name" == CC || "$name" == CXX ]]; then
    # The production Linux branch evaluates compiler commands a second time.
    eval selected=($selected)
  fi
  "${selected[@]}" --version
done
""")
        setup.chmod(0o755)
        result = self.prepare("prepare_linux_cross_env")
        actual = [json.loads(line)["tool"] for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(actual, ["zig", "zig", "llvm-ar", linker.name, "llvm-nm", "llvm-ranlib", "llvm-strip", "llvm-readelf", "llvm-objcopy", "llvm-objdump"])
        for directory in (self.root, self.source):
            self.assertFalse((directory / "UNEXPECTED_EXECUTION").exists())
            self.assertFalse((directory / "OVERRIDE_EXECUTION").exists())

    def test_missing_homebrew_tool_falls_back_to_path(self):
        (self.prefixes["lld"] / "bin/ld.lld").unlink()
        self.fake_tool(self.bin / "ld.lld")
        self.prepare()
        self.assertEqual(self.wrapper("i686-linux-musl-gcc", "-Wl,--version")["tool"], "ld.lld")

    def test_missing_linker_stops_before_generating_wrappers(self):
        (self.prefixes["lld"] / "bin/ld.lld").unlink()
        result = self.prepare(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to find executable ld.lld", result.stderr)
        self.assertFalse((self.source / "build/musl-wrap").exists())

    def test_known_versioned_homebrew_installations_remain_supported(self):
        self.executable(self.bin / "brew", "import sys\nprefixes = " + repr({key if key == "zig" else key + "@21": str(value) for key, value in self.prefixes.items()}) + "\nif len(sys.argv) != 3 or sys.argv[1] != '--prefix' or sys.argv[2] not in prefixes:\n    raise SystemExit(1)\nprint(prefixes[sys.argv[2]])\n")
        result = self.prepare('for name in AR LD NM RANLIB STRIP READELF OBJCOPY OBJDUMP; do "${!name}" --version; done')
        self.assertEqual(len(result.stdout.splitlines()), len(TOOL_VARIABLES))
        self.assertEqual(self.wrapper("i686-linux-musl-gcc", "-Wl,--version"), {"tool": "ld.lld", "args": ["--version"]})


if __name__ == "__main__":
    unittest.main()
