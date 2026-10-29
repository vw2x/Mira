"""Exercise iSH cache invalidation and rootfs retention without invoking Xcode."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ish_build_state", ROOT / "tools/ios/ish-build-state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
RUNTIME = "frida_runtime=official-frida-tools-12.1.0-frida-16.0.7-v13"


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], stderr=subprocess.PIPE)


class LibraryCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        ish = self.root / "third_party/ish"
        for path in (ish, ish / "deps/libarchive", ish / "deps/libapps"):
            path.mkdir(parents=True, exist_ok=True)
            git(path, "init", "-q")
            (path / "source.c").write_text("original source")
            git(path, "add", "source.c")
            git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        scripts = self.root / "tools/ios"
        (scripts / "patches").mkdir(parents=True)
        for name in ("ish-build-state.py", "build-ish-libs.sh", "prepare-ish-source.py", "patches/ish-mira.patch"):
            (scripts / name).write_text("fixture " + name)
        self.args = argparse.Namespace(root=self.root, build_dir=self.root / "output", sdk="iphoneos", configuration="Debug", archs="arm64", extra_ldflags="", action="check")
        self.manifest = self.args.build_dir / "mira-ish-libraries.json"
        products = state.product_directory(self.args)
        products.mkdir(parents=True)
        for name in state.LIBRARIES:
            (products / name).write_bytes(b"nonempty archive " + name.encode())
        real_command = state.command
        self.xcode_version = "Xcode fixture"

        def command(*args):
            if args[0] == "xcrun":
                return "sdk-fixture/" + args[2] + "/" + args[3]
            if args[0] == "xcodebuild":
                return self.xcode_version
            return real_command(*args)

        self.command_patch = patch.object(state, "command", side_effect=command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.tool_patch = patch.object(state, "tool_identity", return_value={"version": "fixture"})
        self.tool_patch.start()
        self.addCleanup(self.tool_patch.stop)

    def operation(self, action):
        self.args.action = action
        return state.libraries(self.args)

    def complete_build(self):
        self.assertEqual(self.operation("begin"), 0)
        self.assertEqual(self.operation("finish"), 0)
        self.assertEqual(self.operation("check"), 0)

    def test_old_archives_without_manifest_are_stale(self):
        self.assertEqual(self.operation("check"), 1)
        self.complete_build()

    def test_sdk_architecture_configuration_and_flags_invalidate(self):
        self.complete_build()
        for field, value in (("sdk", "iphonesimulator"), ("archs", "x86_64"), ("configuration", "Release"), ("extra_ldflags", "-lcustom")):
            with self.subTest(field=field):
                previous = getattr(self.args, field)
                setattr(self.args, field, value)
                self.assertEqual(self.operation("check"), 1)
                setattr(self.args, field, previous)
        self.xcode_version = "New Xcode"
        self.assertEqual(self.operation("check"), 1)

    def test_source_dependency_and_patch_edits_invalidate(self):
        self.complete_build()
        for name in ("third_party/ish/source.c", "third_party/ish/deps/libarchive/source.c", "third_party/ish/deps/libapps/source.c", "tools/ios/patches/ish-mira.patch"):
            with self.subTest(path=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b" modified")
                self.assertEqual(self.operation("check"), 1)
                path.write_bytes(original)
        self.assertEqual(self.operation("check"), 0)

    def test_interrupted_rebuild_cannot_reuse_previous_manifest(self):
        self.complete_build()
        self.operation("begin")
        self.assertFalse(self.manifest.exists())
        (state.product_directory(self.args) / "libish.a").write_bytes(b"partial archive")
        self.assertEqual(self.operation("check"), 1)

    def test_changed_inputs_and_empty_products_refuse_completion(self):
        self.operation("begin")
        self.xcode_version = "changed while building"
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.operation("finish")
        self.assertFalse(self.manifest.exists())
        self.operation("begin")
        (state.product_directory(self.args) / "libish.a").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.operation("finish")
        self.assertFalse(self.manifest.exists())

    def test_modified_archive_is_not_reusable(self):
        self.complete_build()
        (state.product_directory(self.args) / "libish.a").write_bytes(b"different nonempty archive")
        self.assertEqual(self.operation("check"), 1)

    def test_protected_names_and_symlink_targets_are_never_hashed(self):
        directory = self.root / "third_party/ish"
        before = state.source_identity(directory)
        (directory / "alias").symlink_to(".env")
        tracked_names = git(directory, "ls-files", "-z") + b".env\0.env.local\0alias\0"
        real_output = subprocess.check_output

        def listed_output(arguments, **kwargs):
            if arguments[-2:] == ["ls-files", "-z"]:
                return tracked_names
            return real_output(arguments, **kwargs)

        real_hash = state.file_sha256

        def guarded_hash(path):
            self.assertNotIn(path.name, (".env", ".env.local", "alias"))
            return real_hash(path)

        with patch.object(state, "file_sha256", side_effect=guarded_hash), \
                patch.object(state.subprocess, "check_output", side_effect=listed_output):
            self.assertEqual(state.source_identity(directory), before)


class RootfsProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "MiraISHRoot.fakefs"
        (self.output / "data").mkdir(parents=True)
        (self.output / "data/1").write_bytes(b"retained user-space")
        (self.output / "meta.db").write_bytes(b"filesystem metadata")
        (self.output / ".mira-ish-rootfs.stamp").write_text("rootfs=appstore-apk\n" + RUNTIME + "\npython_version=3.11\n")
        self.args = argparse.Namespace(action="reuse", root=self.directory, output=self.output, url=state.DEFAULT_ROOTFS_URL, runtime_stamp=RUNTIME)

    def test_legacy_rootfs_is_preserved_without_any_source_or_download(self):
        before = (self.output / "data/1").read_bytes()
        with patch.object(state, "command", side_effect=AssertionError("Must not inspect/build iSH to reuse userspace")):
            self.assertEqual(state.rootfs_state(self.args), 0)
            self.args.root = self.directory / "a-different-ish-checkout"
            self.assertEqual(state.rootfs_state(self.args), 0)
        manifest = json.loads((self.output / ".mira-ish-rootfs.provenance.json").read_text())
        self.assertEqual(manifest["creation"], "adopted-legacy")
        self.assertIsNone(manifest["producer_ish_revision"])
        self.assertEqual((self.output / "data/1").read_bytes(), before)

    def test_changed_runtime_source_or_artifact_is_not_silently_adopted(self):
        self.assertEqual(state.rootfs_state(self.args), 0)
        self.args.runtime_stamp = "frida_runtime=different"
        self.assertEqual(state.rootfs_state(self.args), 1)
        self.args.runtime_stamp = RUNTIME
        self.args.url = "https://example.invalid/different.tar.gz"
        self.assertEqual(state.rootfs_state(self.args), 1)
        self.args.url = state.DEFAULT_ROOTFS_URL
        (self.output / "meta.db").write_bytes(b"changed metadata")
        self.assertEqual(state.rootfs_state(self.args), 1)

    def test_real_prepare_script_reuses_prebuilt_without_build_tools(self):
        environment = dict(os.environ, MIRA_ISH_ROOTFS_OUTPUT=str(self.output), MIRA_ISH_ROOTFS_URL=state.DEFAULT_ROOTFS_URL)
        result = subprocess.run(["bash", str(ROOT / "tools/ios/prepare-ish-rootfs.sh")], env=environment, capture_output=True, text=True, check=True)
        self.assertIn("already prepared", result.stdout)
        self.assertNotIn("Downloading", result.stdout)
        self.assertEqual((self.output / "data/1").read_bytes(), b"retained user-space")

    def make_archive(self, name="etc/version"):
        archive_path = self.directory / "rootfs.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            payload = b"fixture"
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        return archive_path

    def test_archive_pin_and_custom_url_provenance(self):
        archive = self.make_archive()
        args = argparse.Namespace(action="verify", archive=archive, url=state.DEFAULT_ROOTFS_URL)
        with patch.object(state, "DEFAULT_ROOTFS_SHA256", state.file_sha256(archive)):
            self.assertEqual(state.archive_state(args), 0)
        with self.assertRaisesRegex(ValueError, "pinned baseline"):
            state.archive_state(args)
        args.url = "https://example.invalid/custom.tar.gz"
        with self.assertRaisesRegex(ValueError, "provenance"):
            state.archive_state(args)
        args.action = "record"
        self.assertEqual(state.archive_state(args), 0)
        args.action = "verify"
        self.assertEqual(state.archive_state(args), 0)
        args.url = "https://example.invalid/other.tar.gz"
        with self.assertRaisesRegex(ValueError, "provenance"):
            state.archive_state(args)

    def test_protected_archive_is_rejected_before_hashing_or_extraction(self):
        archive = self.make_archive("etc/.env.local")
        args = argparse.Namespace(action="record", archive=archive, url=state.DEFAULT_ROOTFS_URL)
        with patch.object(state, "file_sha256", side_effect=AssertionError("Must inspect names before hashing")):
            with self.assertRaisesRegex(ValueError, "protected environment"):
                state.archive_state(args)

    def test_symlink_archive_is_rejected_before_tarfile_opens_it(self):
        archive = self.make_archive()
        alias = self.directory / "alias.tar.gz"
        alias.symlink_to(archive.name)
        args = argparse.Namespace(action="record", archive=alias, url=state.DEFAULT_ROOTFS_URL)
        with patch.object(state.tarfile, "open", side_effect=AssertionError("Must reject symlinks before opening")):
            with self.assertRaisesRegex(ValueError, "symlinked rootfs archive"):
                state.archive_state(args)

    def test_protected_hardlink_target_is_rejected_before_hashing(self):
        archive = self.directory / "hardlink.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            member = tarfile.TarInfo("alias")
            member.type = tarfile.LNKTYPE
            member.linkname = "etc/.env.production"
            handle.addfile(member)
        args = argparse.Namespace(action="record", archive=archive, url=state.DEFAULT_ROOTFS_URL)
        with patch.object(state, "file_sha256", side_effect=AssertionError("Must inspect link targets before hashing")):
            with self.assertRaisesRegex(ValueError, "protected environment"):
                state.archive_state(args)


if __name__ == "__main__":
    unittest.main()
