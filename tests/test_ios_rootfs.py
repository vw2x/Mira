"""Exercise production rootfs installation with real macOS Foundation file operations."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "requires macOS Foundation")
class IOSRootfsTest(unittest.TestCase):
    def test_installation_preservation_and_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "rootfs-test"
            subprocess.run(
                [
                    "env", "-u", "LIBRARY_PATH", "-u", "SDKROOT",
                    "xcrun", "--sdk", "macosx", "clang", "-fobjc-arc",
                    "-Wall", "-Wextra", "-Werror", "-framework", "Foundation",
                    "-I", str(ROOT / "native/src/shell"),
                    str(ROOT / "native/src/shell/ish_rootfs.m"),
                    str(ROOT / "tests/native/ish_rootfs_test.m"),
                    "-o", str(binary),
                ],
                check=True, capture_output=True, text=True,
            )
            result = subprocess.run(
                [str(binary), str(Path(temporary) / "fixtures")],
                check=True, capture_output=True, text=True, timeout=20,
            )
            self.assertIn("PASS: 10 rootfs preservation and installation cases", result.stdout)


if __name__ == "__main__":
    unittest.main()
