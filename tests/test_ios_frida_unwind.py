"""Exercise Frida's pinned x86 resume function without executing sigreturn."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/ios/build-frida-musl-devkit.sh"

# Verbatim x86_local_resume from Frida libunwind
# ccd3a38597a8397a3382e4e58fdabb26a6f0be13, src/x86/Gos-linux.c.
# Kept here so clean checkouts need neither downloaded dependencies nor network.
UPSTREAM_RESUME = r'''HIDDEN int
x86_local_resume (unw_addr_space_t as, unw_cursor_t *cursor, void *arg)
{
  struct cursor *c = (struct cursor *) cursor;
  ucontext_t *uc = c->uc;

  /* Ensure c->pi is up-to-date.  On x86, it's relatively common to be
     missing DWARF unwind info.  We don't want to fail in that case,
     because the frame-chain still would let us do a backtrace at
     least.  */
  dwarf_make_proc_info (&c->dwarf);

  if (unlikely (c->sigcontext_format != X86_SCF_NONE))
    {
      struct sigcontext *sc = (struct sigcontext *) c->sigcontext_addr;

      Debug (8, "resuming at ip=%x via sigreturn(%p)\n", c->dwarf.ip, sc);
#if !defined(__ANDROID__)
      x86_sigreturn (sc);
#endif
    }
  else
    {
      Debug (8, "resuming at ip=%x via setcontext()\n", c->dwarf.ip);
#if !defined(__ANDROID__)
      setcontext (uc);
#endif
    }
  return -UNW_EINVAL;
}
'''

# The patch validates the callee's real signature and cursor interpretation.
SIGRETURN_PREFIX = '''HIDDEN void
x86_sigreturn (unw_cursor_t *cursor)
{
  struct cursor *c = (struct cursor *) cursor;
  struct sigcontext *sc = (struct sigcontext *) c->sigcontext_addr;
'''
UPSTREAM_SOURCE = UPSTREAM_RESUME + '\n' + SIGRETURN_PREFIX + '}\n'

HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#define HIDDEN
#define unlikely(value) (value)
#define Debug(...) ((void) 0)
#define X86_SCF_NONE 0
#define UNW_EINVAL 22
typedef void *unw_addr_space_t;
typedef struct { unsigned marker; } ucontext_t;
struct sigcontext { unsigned marker; };
struct dwarf_cursor { unsigned ip; };
struct cursor {
  struct dwarf_cursor dwarf;
  ucontext_t *uc;
  int sigcontext_format;
  uintptr_t sigcontext_addr;
};
typedef struct cursor unw_cursor_t;
static struct cursor *expected;
static unsigned prepared, signals, contexts, wrong_pointer;
static void dwarf_make_proc_info(struct dwarf_cursor *dwarf) {
  prepared += dwarf == &expected->dwarf;
}
/* Accept either pointer type to observe the original bug without executing it. */
static void x86_sigreturn(void *argument) {
  ++signals;
  if (argument != expected) { ++wrong_pointer; return; }
  struct cursor *cursor = argument;
  if (cursor->uc->marker != 42 ||
      ((struct sigcontext *) cursor->sigcontext_addr)->marker != 77)
    ++wrong_pointer;
}
static void setcontext(ucontext_t *context) {
  ++contexts;
  if (context != expected->uc) ++wrong_pointer;
}
@RESUME@
int main(void) {
  ucontext_t context = {42};
  struct sigcontext frame = {77};
  struct cursor cursor = {{123}, &context, 1, (uintptr_t) &frame};
  expected = &cursor;
  int result = x86_local_resume(NULL, &cursor, NULL);
  if (result != -UNW_EINVAL || prepared != 1 || signals != 1 ||
      contexts != 0 || wrong_pointer != 0) {
    puts("FAIL: signal resume did not receive the complete cursor");
    return 1;
  }
  cursor.sigcontext_format = X86_SCF_NONE;
  result = x86_local_resume(NULL, &cursor, NULL);
  if (result != -UNW_EINVAL || prepared != 2 || signals != 1 ||
      contexts != 1 || wrong_pointer != 0) return 2;
  puts("PASS: signal cursor and ordinary ucontext paths");
  return 0;
}
'''


class FridaUnwindTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mira-frida-unwind-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "frida source"
        self.path = self.source / "deps/libunwind/src/x86/Gos-linux.c"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(UPSTREAM_SOURCE)
        self.environment = dict(os.environ)
        for name in ("BASH_ENV", "ENV"):
            self.environment.pop(name, None)
        self.environment["MIRA_IOS_FRIDA_SOURCE_DIR"] = str(self.source)

    def patch(self, check=True):
        return subprocess.run(["bash", "-c", 'source "$1"\npatch_libunwind_x86_sigreturn', "test", str(SCRIPT)], cwd=self.root,
            env=self.environment, capture_output=True, text=True, check=check)

    def run_resume(self, source):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("A host C compiler is required for the resume behavior test")
        # Extract the production function after applying the real source patch.
        start = source.index("HIDDEN int\nx86_local_resume (")
        end = source.index("\nHIDDEN void", start)
        program = self.root / "resume.c"
        executable = self.root / "resume"
        program.write_text(HARNESS.replace("@RESUME@", source[start:end]))
        subprocess.run([compiler, "-std=c99", str(program), "-o", str(executable)],
            check=True, capture_output=True, text=True, timeout=30)
        return subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)

    def test_signal_resume_receives_complete_cursor_and_normal_resume_is_preserved(self):
        old = self.run_resume(self.path.read_text())
        self.assertEqual(old.returncode, 1)
        self.assertIn("did not receive the complete cursor", old.stdout)
        self.patch()
        fixed = self.run_resume(self.path.read_text())
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn("signal cursor and ordinary ucontext paths", fixed.stdout)

    def test_patch_is_idempotent_and_changes_only_the_argument(self):
        self.patch()
        expected = UPSTREAM_SOURCE.replace("x86_sigreturn (sc);", "x86_sigreturn (cursor);")
        self.assertEqual(self.path.read_text(), expected)
        timestamp = self.path.stat().st_mtime_ns
        self.patch()
        self.assertEqual(self.path.read_text(), expected)
        self.assertEqual(self.path.stat().st_mtime_ns, timestamp)

    def test_unexpected_source_is_rejected_without_overwrite(self):
        for contents in (
            UPSTREAM_SOURCE.replace("x86_sigreturn (sc);", "x86_sigreturn (other);"),
            UPSTREAM_SOURCE.replace("x86_sigreturn (unw_cursor_t *cursor)", "x86_sigreturn (void *cursor)"),
            UPSTREAM_SOURCE + "      x86_sigreturn (sc);\n",
        ):
            with self.subTest(contents=contents[-45:]):
                self.path.write_text(contents)
                result = self.patch(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unexpected libunwind x86 sigreturn", result.stderr)
                self.assertEqual(self.path.read_text(), contents)


if __name__ == "__main__":
    unittest.main()
