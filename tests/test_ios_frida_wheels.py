"""Check complete, platform-independent iOS Python dependencies without building iSH."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ios_frida_runtime", ROOT / "tools/ios/prepare-ish-frida-runtime.py")
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)


class FridaWheelTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.available = self.root / "available"
        self.available.mkdir()

    def wheel(self, name, version="1.0", pure="true", tag="py3-none-any"):
        path = self.available / f"{name}-{version}-{tag}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}/__init__.py", f"VERSION = {version!r}\n")
            archive.writestr(f"{name}-{version}.dist-info/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: {pure}\nTag: {tag}\n")
            archive.writestr(f"{name}-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        return path

    def pip_result(self, wheels):
        def download(command):
            self.assertEqual(command[command.index("--platform") + 1], "any")
            self.assertEqual(command[command.index("--abi") + 1], "none")
            destination = Path(command[command.index("--dest") + 1])
            self.assertEqual(list(destination.iterdir()), [])
            for wheel in wheels:
                shutil.copy2(wheel, destination / wheel.name)
        return download

    def test_partial_cache_installs_all_selected_wheels_and_no_obsolete_versions(self):
        wheels = [self.wheel(name) for name in runtime.PURE_PYTHON_MODULES]
        for wheel in wheels[:-1]:
            shutil.copy2(wheel, self.cache / wheel.name)
        obsolete = self.wheel("colorama", version="0.1")
        shutil.copy2(obsolete, self.cache / obsolete.name)
        installed = self.root / "site-packages"
        with patch.object(runtime, "run", side_effect=self.pip_result(wheels)):
            names = runtime.install_pure_python_dependencies(installed, self.cache)
        self.assertEqual(names, sorted(wheel.name for wheel in wheels))
        for module in runtime.PURE_PYTHON_MODULES:
            self.assertTrue((installed / module / "__init__.py").is_file())
        self.assertFalse((installed / "colorama-0.1.dist-info").exists())
        self.assertEqual((installed / "colorama/__init__.py").read_text(), "VERSION = '1.0'\n")

    def test_all_cached_wheels_remain_the_complete_resolution(self):
        wheels = [self.wheel(name) for name in runtime.PURE_PYTHON_MODULES]
        for wheel in wheels:
            shutil.copy2(wheel, self.cache / wheel.name)
        with patch.object(runtime, "run", side_effect=self.pip_result(wheels)):
            selected = runtime.download_wheels(self.cache, runtime.PURE_PYTHON_SPECS)
        self.assertEqual(selected, sorted((self.cache / wheel.name).resolve() for wheel in wheels))

    @unittest.skipUnless(importlib.util.find_spec("pip"), "pip is needed for the offline resolver check")
    def test_real_pip_resolves_complete_wheel_set_offline(self):
        wheels = [self.wheel("colorama"), self.wheel("wcwidth")]
        shutil.copy2(wheels[0], self.cache / wheels[0].name)
        with patch.object(runtime, "run", side_effect=lambda command: subprocess.run(command, check=True, capture_output=True, text=True)):
            selected = runtime.download_wheels(
                self.cache, ("colorama==1.0", "wcwidth==1.0"),
                ["--isolated", "--no-index", "--no-cache-dir", "--find-links", str(self.available)],
            )
        self.assertEqual(selected, sorted((self.cache / wheel.name).resolve() for wheel in wheels))

    def test_native_or_non_pure_wheels_are_rejected_before_caching(self):
        for pure, tag in (("false", "py3-none-any"), ("true", "cp310-cp310-macosx_11_0_arm64")):
            with self.subTest(pure=pure, tag=tag):
                wheel = self.wheel("native", pure=pure, tag=tag)
                with patch.object(runtime, "run", side_effect=self.pip_result([wheel])):
                    with self.assertRaisesRegex(ValueError, "仅允许纯 Python any wheel"):
                        runtime.download_wheels(self.cache, ("native",))
                self.assertFalse((self.cache / wheel.name).exists())

    def test_missing_wheel_metadata_is_rejected(self):
        wheel = self.available / "broken-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("broken/__init__.py", "")
        with self.assertRaisesRegex(ValueError, "wheel 元数据不完整"):
            runtime.validate_pure_python_wheel(wheel)

    def test_protected_entry_names_are_rejected_before_metadata_or_payload_reads(self):
        for name in (".env", "package/.env.production", "package/.env.local/data", "package\\.env.test"):
            with self.subTest(name=name):
                archive = MagicMock()
                archive.infolist.return_value = [
                    zipfile.ZipInfo("package/__init__.py"),
                    zipfile.ZipInfo("package-1.0.dist-info/WHEEL"),
                    zipfile.ZipInfo(name),
                ]
                with patch.object(runtime.zipfile, "ZipFile") as opening:
                    opening.return_value.__enter__.return_value = archive
                    with self.assertRaisesRegex(ValueError, "受保护的环境文件路径"):
                        runtime.validate_pure_python_wheel(self.root / "fixture.whl")
                    with self.assertRaisesRegex(ValueError, "受保护的环境文件路径"):
                        runtime.install_wheel(self.root / "fixture.whl", self.root / "installed")
                archive.read.assert_not_called()
                archive.open.assert_not_called()
                self.assertFalse((self.root / "installed").exists())

    def test_exact_env_example_is_allowed(self):
        wheel = self.wheel("example")
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("example/.env.example", "EXAMPLE=value\n")
        runtime.validate_pure_python_wheel(wheel)
        runtime.install_wheel(wheel, self.root / "installed")
        self.assertTrue((self.root / "installed/example/.env.example").is_file())

    def test_empty_resolution_does_not_reuse_obsolete_cache(self):
        wheel = self.wheel("obsolete")
        shutil.copy2(wheel, self.cache / wheel.name)
        with patch.object(runtime, "run", side_effect=self.pip_result([])):
            with self.assertRaisesRegex(FileNotFoundError, "未下载到纯 Python wheel"):
                runtime.download_wheels(self.cache, ("required",))


class FridaLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "rootfs"
        self.site = runtime.site_packages_dir(self.root)
        for relative in (
            "usr/bin/python3", "usr/bin/frida", "usr/bin/frida-setup",
            "lib/ld-musl-i386.so.1", "lib/libc.musl-x86.so.1",
            "usr/include/python3.12/Python.h", f"opt/mira/frida-state/{runtime.RUNTIME_VERSION}",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        (self.root / "usr/lib/python3.12").mkdir(parents=True)
        for name in (*runtime.PURE_PYTHON_MODULES, "frida", "frida_tools"):
            (self.site / name).mkdir(parents=True)
            (self.site / name / "__init__.py").write_text("")
        (self.site / "_frida.abi3.so").write_bytes(b"foreign ELF fixture; must not be imported on the host")

    def validate(self):
        with patch.object(runtime, "run", side_effect=lambda command: subprocess.run(command, check=True, capture_output=True, text=True)):
            runtime.validate_layout(self.root)

    def test_complete_target_passes_without_loading_foreign_extension(self):
        self.validate()
        self.assertEqual(list(self.site.glob("*/__pycache__")), [])

    def test_host_pythonpath_cannot_fill_a_missing_target_dependency(self):
        host = Path(self.temporary.name) / "host"
        host.mkdir()
        shutil.move(str(self.site / "wcwidth"), host / "wcwidth")
        with patch.dict(os.environ, {"PYTHONPATH": str(host)}):
            ordinary = subprocess.run([sys.executable, "-S", "-c", "import wcwidth; print(wcwidth.__file__)"], check=True, capture_output=True, text=True)
            self.assertIn(str(host), ordinary.stdout)
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                self.validate()
        self.assertIn("No module named 'wcwidth'", failure.exception.stderr)

    def test_dependency_symlink_outside_target_is_rejected(self):
        host = Path(self.temporary.name) / "host"
        host.mkdir()
        shutil.move(str(self.site / "wcwidth"), host / "wcwidth")
        (self.site / "wcwidth").symlink_to(host / "wcwidth", target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.validate()
        self.assertIn("was imported outside the target", failure.exception.stderr)


if __name__ == "__main__":
    unittest.main()
