"""Exercise dependency packaging without a network, NDK build, or Android device."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import unittest
import unittest.mock as mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/termux/build-bootstrap-prefix.py"
VERSIONS = {"colorama": "0.4.6", "prompt_toolkit": "3.0.53", "pygments": "2.21.0", "wcwidth": "0.9.4"}


def wheel(directory, package, version, *, tag="py3-none-any", pure=True):
    path = directory / f"{package}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", f"VERSION = {version!r}\n")
        archive.writestr(f"{package}-{version}.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {str(pure).lower()}\nTag: {tag}\n")
        archive.writestr(f"{package}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n")
    return path


class TermuxBootstrapTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mira-termux-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        spec = importlib.util.spec_from_file_location("mira_termux_bootstrap_test", SCRIPT)
        self.builder = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.builder
        with mock.patch.dict(os.environ, MIRA_ANDROID_NDK_TOOLCHAIN_DIR=str(self.root / "ndk")):
            spec.loader.exec_module(self.builder)
        self.cache = self.root / "wheels"
        self.cache.mkdir()
        self.builder.WHEEL_CACHE_DIR = self.cache
        self.site = self.root / "site-packages"
        self.site.mkdir()

    def fake_download(self, command):
        destination = Path(command[command.index("--dest") + 1])
        for package, version in VERSIONS.items():
            if not (destination / f"{package}-{version}-py3-none-any.whl").exists():
                wheel(destination, package, version)

    def test_mixed_cached_and_new_wheels_install_the_complete_requested_set(self):
        for package in ("colorama", "prompt_toolkit", "pygments"):
            wheel(self.cache, package, VERSIONS[package])
        with mock.patch.object(self.builder, "run", side_effect=self.fake_download):
            installed = self.builder.install_pure_python_dependencies(self.site)
        self.assertEqual(set(installed), {f"{name}-{version}-py3-none-any.whl" for name, version in VERSIONS.items()})
        for package, version in VERSIONS.items():
            self.assertEqual((self.site / package / "__init__.py").read_text(), f"VERSION = {version!r}\n")

    def test_warm_cache_does_not_install_unselected_historical_versions(self):
        for package, version in VERSIONS.items():
            wheel(self.cache, package, version)
        stale = wheel(self.cache, "wcwidth", "0.8.3")
        with mock.patch.object(self.builder, "run", side_effect=self.fake_download):
            installed = self.builder.install_pure_python_dependencies(self.site)
        self.assertNotIn(stale.name, installed)
        self.assertFalse((self.site / "wcwidth-0.8.3.dist-info").exists())
        self.assertTrue(stale.is_file(), "Historical cache files need not be deleted")

    def test_downloader_requests_portable_wheels(self):
        def select_for_target(command):
            portable = "--platform" in command and command[command.index("--platform") + 1] == "any"
            portable = portable and "--abi" in command and command[command.index("--abi") + 1] == "none"
            destination = Path(command[command.index("--dest") + 1])
            for package, version in VERSIONS.items():
                wheel(destination, package, version, tag="py3-none-any" if portable else "cp310-abi3-macosx_11_0_arm64", pure=portable)
        with mock.patch.object(self.builder, "run", side_effect=select_for_target):
            installed = self.builder.install_pure_python_dependencies(self.site)
        self.assertTrue(all(name.endswith("-none-any.whl") for name in installed))

    @unittest.skipUnless(importlib.util.find_spec("pip"), "pip is needed for the offline resolver check")
    def test_real_pip_offline_resolution_ignores_old_cache_and_host_wheels(self):
        available = self.root / "available wheels"
        available.mkdir()
        for package, version in VERSIONS.items():
            wheel(available, package, version)
        wheel(available, "wcwidth", "99.0", tag="cp310-abi3-macosx_11_0_arm64", pure=False)
        wheel(self.cache, "colorama", VERSIONS["colorama"])
        wheel(self.cache, "wcwidth", "0.8.3")
        def offline_download(command):
            subprocess.run([*command, "--isolated", "--no-index", "--no-cache-dir", "--find-links", str(available)],
                check=True, capture_output=True, text=True)
        with mock.patch.object(self.builder, "run", side_effect=offline_download):
            installed = self.builder.install_pure_python_dependencies(self.site)
        self.assertEqual(set(installed), {f"{name}-{version}-py3-none-any.whl" for name, version in VERSIONS.items()})
        self.assertFalse((self.site / "wcwidth-0.8.3.dist-info").exists())

    def test_nonportable_or_nonpure_downloads_fail_before_installing(self):
        for tag, pure in (("cp310-abi3-macosx_11_0_arm64", False), ("py3-none-any", False), ("py3-none-linux_aarch64", True)):
            with self.subTest(tag=tag, pure=pure):
                def invalid_download(command):
                    destination = Path(command[command.index("--dest") + 1])
                    for package, version in VERSIONS.items():
                        wheel(destination, package, version,
                            tag=tag if package == "wcwidth" else "py3-none-any",
                            pure=pure if package == "wcwidth" else True)
                with mock.patch.object(self.builder, "run", side_effect=invalid_download):
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.builder.install_pure_python_dependencies(self.site)
                self.assertEqual(list(self.site.iterdir()), [])

    def test_environment_entries_are_rejected_before_reading_any_archive_content(self):
        for entry in (".env", "package/.env.production", "package\\.env.local"):
            with self.subTest(entry=entry):
                path = wheel(self.cache, "colorama", VERSIONS["colorama"])
                with zipfile.ZipFile(path, "a") as archive:
                    archive.writestr(entry, b"synthetic test fixture")
                with mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("Archive content must not be read")):
                    with self.assertRaisesRegex(ValueError, "环境配置"):
                        self.builder.install_wheel(path, self.site)
                self.assertEqual(list(self.site.iterdir()), [])

    def test_environment_example_entry_is_allowed(self):
        path = wheel(self.cache, "colorama", VERSIONS["colorama"])
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("colorama/.env.example", b"EXAMPLE=placeholder\n")
        self.builder.install_wheel(path, self.site)
        self.assertTrue((self.site / "colorama/__init__.py").is_file())

    def test_failed_download_cannot_install_stale_cache(self):
        wheel(self.cache, "wcwidth", "0.8.3")
        with mock.patch.object(self.builder, "run", side_effect=subprocess.CalledProcessError(1, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.builder.install_pure_python_dependencies(self.site)
        self.assertEqual(list(self.site.iterdir()), [])

    def runtime_fixture(self):
        prefix = self.root / "prefix with spaces"
        site = self.builder.site_packages_dir(prefix, "3.13")
        for filename in ("bin/python3", "bin/pip", "bin/frida-official", "lib/python3.13/site-packages/frida/__init__.py", "lib/python3.13/site-packages/_frida.abi3.so"):
            path = prefix / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        for package in self.builder.EXPECTED_IMPORTS:
            path = site / package / "__init__.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PACKAGED = True\n")
        return prefix, site

    def test_complete_packaged_modules_pass_isolated_imports(self):
        prefix, site = self.runtime_fixture()
        self.validate(prefix)
        self.assertEqual(list(site.glob("*/__pycache__")), [])

    def validate(self, prefix):
        with mock.patch.object(self.builder, "run", side_effect=lambda command: subprocess.run(
                command, check=True, capture_output=True, text=True)):
            self.builder.validate_layout(prefix, "3.13")

    def test_missing_dependency_cannot_be_supplied_by_host_pythonpath(self):
        prefix, site = self.runtime_fixture()
        (site / "colorama/__init__.py").unlink()
        host = self.root / "host-site"
        (host / "colorama").mkdir(parents=True)
        marker = self.root / "host-imported"
        (host / "colorama/__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with mock.patch.dict(os.environ, PYTHONPATH=str(host)):
            with self.assertRaises((FileNotFoundError, subprocess.CalledProcessError)):
                self.validate(prefix)
        self.assertFalse(marker.exists())

    def test_packaged_module_cannot_import_a_host_only_transitive_dependency(self):
        prefix, site = self.runtime_fixture()
        (site / "colorama/__init__.py").write_text("import mira_host_only\n")
        host = self.root / "host-site"
        host.mkdir()
        (host / "mira_host_only.py").write_text("HOST_ONLY = True\n")
        with mock.patch.dict(os.environ, PYTHONPATH=str(host)):
            with self.assertRaises(subprocess.CalledProcessError):
                self.validate(prefix)

    def test_dependency_origin_must_stay_inside_packaged_runtime(self):
        prefix, site = self.runtime_fixture()
        outside = self.root / "outside-colorama"
        (site / "colorama").rename(outside)
        (site / "colorama").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.validate(prefix)
        self.assertIn("outside packaged runtime", failure.exception.stderr)

    def install_binding_fixture(self, legacy=False):
        source = self.root / "frida-source"
        (source / "frida").mkdir(parents=True)
        if legacy:
            (source / "_frida").mkdir()
            (source / "_frida/__init__.py").write_text("__version__ = '16.0.7'\n_PRIVATE = object()\n")
            (source / "frida/__init__.py").write_text("import _frida\n")
        else:
            (source / "frida/_frida.py").write_text("__version__ = '16.7.19'\n_PRIVATE = object()\n")
            (source / "frida/__init__.py").write_text("from . import _frida\n")
        archive = self.root / "frida-fixture.tar.gz"
        with mock.patch.object(self.builder, "download_source_distribution", return_value=archive), \
                mock.patch.object(self.builder, "verify_sha256"), \
                mock.patch.object(self.builder, "extract_tarball", return_value=source), \
                mock.patch.object(self.builder, "build_frida_extension") as compile_extension:
            self.builder.install_frida_python_binding(self.root, "3.13", self.site)
        return compile_extension.call_args.args[-1]

    def test_nested_binding_exposes_identical_legacy_module_alias(self):
        output = self.install_binding_fixture()
        self.assertEqual(output, self.site / "frida/_frida.abi3.so")
        program = f"import sys; sys.path.insert(0, {str(self.site)!r}); import _frida, frida; assert _frida is frida._frida; assert _frida._PRIVATE is frida._frida._PRIVATE; print(_frida.__version__)"
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", program], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "16.7.19")

    def test_legacy_binding_keeps_original_top_level_extension_layout(self):
        output = self.install_binding_fixture(legacy=True)
        self.assertEqual(output, self.site / "_frida.abi3.so")
        self.assertFalse((self.site / "_frida.py").exists())

    def test_cli_entrypoint_overrides_stale_top_level_module_and_preserves_arguments(self):
        self.install_binding_fixture()
        (self.site / "_frida.py").write_text("raise AssertionError('stale top-level module must not load')\n")
        (self.site / "frida_tools").mkdir()
        (self.site / "frida_tools/__init__.py").write_text("")
        (self.site / "frida_tools/repl.py").write_text("import json, sys, _frida, frida\nassert _frida is frida._frida\nprint(json.dumps(sys.argv[1:]))\n")
        prefix = self.root / "prefix with spaces"
        (prefix / "bin").mkdir(parents=True)
        python = prefix / "bin/python3"
        python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -S -B \"$@\"\n")
        python.chmod(0o755)
        self.builder.ensure_frida_entrypoint(prefix)
        arguments = ["--version", "argument with spaces", "$(literal)"]
        with mock.patch.dict(os.environ, PYTHONPATH=str(self.site)):
            result = subprocess.run(["sh", str(prefix / "bin/frida-official"), *arguments], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_nested_runtime_without_legacy_alias_fails_layout_validation(self):
        prefix, site = self.runtime_fixture()
        (site / "_frida.abi3.so").rename(site / "frida/_frida.abi3.so")
        with self.assertRaises(FileNotFoundError):
            self.validate(prefix)


if __name__ == "__main__":
    unittest.main()
