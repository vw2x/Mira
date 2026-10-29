"""Keep nested Frida build dependencies at their parent repository's gitlinks."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/ios/build-frida-musl-devkit.sh"


class FridaSourcePinsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mira-frida-source-pins-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = dict(os.environ)
        for name in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.environment.pop(name, None)
        self.environment.update(GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.remote_gvdb = self.root / "gvdb origin"
        self.init(self.remote_gvdb)
        self.commit_file(self.remote_gvdb, "meson.build", "project('gvdb', 'c', meson_version: '>=0.54.0')\n")
        self.commit_file(self.remote_gvdb, "notes.txt", "unchanged in both revisions\n")
        self.expected = self.head(self.remote_gvdb)
        self.commit_file(self.remote_gvdb, "meson.build", "project('gvdb', 'c', meson_version: '>=1.1')\n")
        self.newer = self.head(self.remote_gvdb)

        glib_origin = self.root / "glib origin"
        self.init(glib_origin)
        self.git(glib_origin, "submodule", "add", "--", str(self.remote_gvdb), "subprojects/gvdb")
        self.git(glib_origin / "subprojects/gvdb", "checkout", "--detach", self.expected)
        self.git(glib_origin, "add", "--", ".gitmodules", "subprojects/gvdb")
        self.git(glib_origin, "commit", "-qm", "Pin compatible gvdb")
        self.glib_pin = self.head(glib_origin)
        self.git(glib_origin / "subprojects/gvdb", "checkout", "--detach", self.newer)
        self.git(glib_origin, "add", "--", "subprojects/gvdb")
        self.git(glib_origin, "commit", "-qm", "Advance gvdb on current branch")

        self.source = self.root / "frida source"
        self.init(self.source)
        self.commit_file(self.source, "Makefile.sdk.mk", "deps/.zlib-stamp deps/.libffi-stamp deps/.glib-stamp:\n\t@touch $@\n")
        self.git(self.source, "tag", "16.0.7")
        (self.source / "deps").mkdir()
        self.glib = self.source / "deps/glib"
        # Reproduce Frida's recipe: recursive clone at current HEAD, then parent-only checkout.
        self.git(self.source, "clone", "--quiet", "--recurse-submodules", str(glib_origin), str(self.glib))
        self.git(self.glib, "checkout", "--quiet", "--detach", self.glib_pin)
        self.gvdb = self.glib / "subprojects/gvdb"
        self.assertEqual(self.head(self.gvdb), self.newer)
        self.environment.update(MIRA_IOS_FRIDA_SOURCE_DIR=str(self.source),
            MIRA_IOS_FRIDA_BUILD_ROOT=str(self.root / "build"),
            MIRA_IOS_FRIDA_OUTPUT_DIR=str(self.root / "output"), FRIDA_VERSION="16.0.7")

    def git(self, directory, *arguments):
        return subprocess.run(["git", "-c", "user.name=Mira Test", "-c", "user.email=mira-test@example.invalid",
            "-C", str(directory), *arguments], env=self.environment, check=True, capture_output=True, text=True).stdout.strip()

    def init(self, directory):
        directory.mkdir(parents=True)
        self.git(directory, "init", "-q", "--initial-branch=main")

    def commit_file(self, directory, filename, content):
        (directory / filename).write_text(content)
        self.git(directory, "add", "--", filename)
        self.git(directory, "commit", "-qm", "Fixture update")

    def head(self, directory):
        return self.git(directory, "rev-parse", "HEAD")

    def prepare(self, check=True):
        # Only unrelated source rewriting is stubbed; preparation and Git operations are real.
        program = 'source "$1"\napply_source_patches() { printf "APPLY_PIN="; git -C "$SOURCE_DIR/deps/glib/subprojects/gvdb" rev-parse HEAD; }\nprepare_source_tree\n'
        return subprocess.run(["bash", "-c", program, "test", str(SCRIPT)], cwd=self.root,
            env=self.environment, check=check, capture_output=True, text=True)

    def test_recursive_clone_drift_is_repaired_before_source_patching(self):
        result = self.prepare()
        self.assertEqual(self.head(self.gvdb), self.expected)
        self.assertIn("APPLY_PIN=" + self.expected, result.stdout)
        self.assertIn(">=0.54.0", (self.gvdb / "meson.build").read_text())

    def test_existing_dependency_stamps_do_not_skip_gitlink_verification(self):
        self.prepare()
        self.assertTrue((self.source / "deps/.glib-stamp").is_file())
        self.git(self.gvdb, "checkout", "--detach", self.newer)
        self.prepare()
        self.assertEqual(self.head(self.gvdb), self.expected)
        self.prepare()
        self.assertEqual(self.git(self.glib, "status", "--porcelain", "--untracked-files=no"), "")

    def test_uninitialized_submodule_is_installed_at_recorded_gitlink(self):
        self.git(self.glib, "submodule", "deinit", "--force", "--", "subprojects/gvdb")
        self.prepare()
        self.assertEqual(self.head(self.gvdb), self.expected)

    def test_local_submodule_edit_is_preserved_and_stops_preparation(self):
        modified = "local change that must not be overwritten\n"
        (self.gvdb / "meson.build").write_text(modified)
        result = self.prepare(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.gvdb / "meson.build").read_text(), modified)
        self.assertNotIn("APPLY_PIN=", result.stdout)

    def test_nonconflicting_submodule_edit_is_preserved_when_revision_changes(self):
        modified = "local notes to preserve\n"
        (self.gvdb / "notes.txt").write_text(modified)
        self.prepare()
        self.assertEqual(self.head(self.gvdb), self.expected)
        self.assertEqual((self.gvdb / "notes.txt").read_text(), modified)


if __name__ == "__main__":
    unittest.main()
