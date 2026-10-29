"""Apply the shipped iSH patch in isolated Git repositories, never in the submodule."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "tools/ios/patches/ish-mira.patch"
FILES = ("kernel/mmap.c", "app/xcode-meson.sh", "app/xcode-ninja.sh", "fs/tty.c")
spec = importlib.util.spec_from_file_location("prepare_ish_source", ROOT / "tools/ios/prepare-ish-source.py")
prepare_source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_source)


def git(directory, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(directory), "-c", "core.hooksPath=/dev/null", *arguments],
        stderr=subprocess.PIPE,
    )


class IOSISHSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Read only the four named source blobs from the reviewed upstream commit.
        cls.upstream = {
            name: git(ROOT / "third_party/ish", "show", f"{prepare_source.ISH_REVISION}:{name}")
            for name in FILES
        }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        git(self.source, "init", "--quiet", "--template=")
        for name, content in self.upstream.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / "notes.txt").write_text("original notes\n")
        git(self.source, "add", *FILES, "notes.txt")
        git(
            self.source, "-c", "user.name=Mira Test", "-c",
            "user.email=mira-test@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "--quiet", "-m", "fixture",
        )
        self.revision = git(self.source, "rev-parse", "HEAD").decode().strip()

    def prepare(self, revision=None):
        prepare_source.prepare(self.source, PATCH, self.revision if revision is None else revision)

    def snapshot(self):
        return (
            {name: (self.source / name).read_bytes() for name in (*FILES, "notes.txt")},
            git(self.source, "diff", "--binary"),
            git(self.source, "diff", "--cached", "--binary"),
            git(self.source, "status", "--porcelain"),
        )

    def test_shipped_patch_applies_completely_and_repeating_is_a_noop(self):
        self.prepare()
        changed = set(git(self.source, "diff", "--name-only").decode().splitlines())
        self.assertEqual(changed, set(FILES))
        git(self.source, "apply", "--reverse", "--check", str(PATCH))
        self.assertEqual(git(self.source, "diff", "--cached"), b"")
        applied = self.snapshot()
        self.prepare()
        self.assertEqual(self.snapshot(), applied)

    def test_revision_mismatch_refuses_before_modifying_any_files(self):
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "no reviewed Mira patch"):
            self.prepare(revision="0" * 40)
        self.assertEqual(self.snapshot(), before)
        # The revision guard also runs when the working tree is already patched.
        self.prepare()
        applied = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "no reviewed Mira patch"):
            self.prepare(revision="0" * 40)
        self.assertEqual(self.snapshot(), applied)

    def test_conflict_preserves_local_edits_and_other_patch_targets(self):
        path = self.source / "kernel/mmap.c"
        original = path.read_bytes()
        self.assertIn(b"    page_t page;\n", original)
        path.write_bytes(original.replace(b"    page_t page;\n", b"    page_t local_page;\n", 1))
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "conflicts with local changes"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        for name in FILES[1:]:
            self.assertEqual((self.source / name).read_bytes(), self.upstream[name])

    def test_unrelated_working_index_and_untracked_changes_survive(self):
        local_suffix = b"\n/* Local research note outside the compatibility patch. */\n"
        mmap = self.source / "kernel/mmap.c"
        mmap.write_bytes(mmap.read_bytes() + local_suffix)
        (self.source / "notes.txt").write_text("user notes staged before preparation\n")
        git(self.source, "add", "notes.txt")
        scratch = self.source / "scratch.txt"
        scratch.write_bytes(b"untracked user work\n")
        index_before = git(self.source, "diff", "--cached", "--binary")
        self.prepare()
        self.assertTrue(mmap.read_bytes().endswith(local_suffix))
        self.assertEqual((self.source / "notes.txt").read_text(), "user notes staged before preparation\n")
        self.assertEqual(scratch.read_bytes(), b"untracked user work\n")
        self.assertEqual(git(self.source, "diff", "--cached", "--binary"), index_before)
        applied = self.snapshot()
        self.prepare()
        self.assertEqual(self.snapshot(), applied)
        self.assertEqual(scratch.read_bytes(), b"untracked user work\n")

    def test_partial_prior_patch_refuses_without_applying_remaining_hunks(self):
        git(self.source, "apply", "--include=kernel/mmap.c", str(PATCH))
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "conflicts with local changes"):
            self.prepare()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
