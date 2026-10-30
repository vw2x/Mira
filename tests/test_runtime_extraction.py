"""Run the actual Java runtime installation and extraction code on the host JVM."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeExtractionTest(unittest.TestCase):
    def test_failure_recovery_and_verified_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            classes = Path(temporary) / "classes"
            classes.mkdir()
            subprocess.run([
                "javac", "--release", "8", "-d", str(classes),
                str(ROOT / "android/app/src/main/java/com/vwww/mira/runtime/VerifiedExtraction.java"),
                str(ROOT / "tests/java/com/vwww/mira/runtime/VerifiedExtractionTest.java"),
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([
                "java", "-cp", str(classes), "com.vwww.mira.runtime.VerifiedExtractionTest",
                str(Path(temporary) / "runtime"),
            ], check=True, capture_output=True, text=True)
            self.assertIn("PASS:", result.stdout)

    def test_install_state_failure_and_recovery(self):
        # Only Android environment services are stubbed; installation code is real.
        wrapper = (ROOT / "tools/android/build-frida-16.7-compat-apk.sh").read_text()
        match = re.search(r"MIRA_BOOTSTRAP_STATE_VERSION=(\d+)", wrapper)
        self.assertIsNotNone(match)
        state_version = int(match.group(1))
        self.assertGreaterEqual(state_version, 19)
        gradle = (ROOT / "android/app/build.gradle").read_text()
        ordinary_match = re.search(r"miraBootstrapStateVersion.*getOrElse\((\d+)\)", gradle)
        self.assertIsNotNone(ordinary_match)
        self.assertGreaterEqual(int(ordinary_match.group(1)), 10)
        stubs = {
            "android/content/Context.java": "package android.content; import java.io.File; import android.content.res.AssetManager; public abstract class Context { public abstract Context getApplicationContext(); public abstract AssetManager getAssets(); public abstract File getFilesDir(); public abstract File getCacheDir(); public abstract String getPackageCodePath(); }",
            "android/content/res/AssetManager.java": "package android.content.res; public class AssetManager { public String[] list(String path) { return new String[] {\"bin\"}; } }",
            "android/os/Build.java": "package android.os; public class Build { public static String[] SUPPORTED_ABIS = {\"arm64-v8a\"}; }",
            "android/os/SystemClock.java": "package android.os; public class SystemClock { public static long elapsedRealtime() { return 0; } }",
            "android/util/Log.java": "package android.util; public class Log { public static int i(String t, String m) { return 0; } public static int w(String t, String m, Throwable e) { return 0; } }",
            "com/vwww/mira/BuildConfig.java": f"package com.vwww.mira; public final class BuildConfig {{ public static final int MIRA_BOOTSTRAP_STATE_VERSION = {state_version}; }}",
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sources = []
            for name, content in stubs.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                sources.append(str(path))
            classes = directory / "classes"
            classes.mkdir()
            subprocess.run(["javac", "--release", "8", "-d", str(classes), *sources,
                str(ROOT / "android/app/src/main/java/com/vwww/mira/runtime/RuntimeInstaller.java"),
                str(ROOT / "android/app/src/main/java/com/vwww/mira/runtime/RuntimeScripts.java"),
                str(ROOT / "android/app/src/main/java/com/vwww/mira/runtime/VerifiedExtraction.java"),
                str(ROOT / "tests/java/com/vwww/mira/runtime/RuntimeInstallTest.java")],
                check=True, capture_output=True, text=True)
            result = subprocess.run(["java", "-cp", str(classes),
                "com.vwww.mira.runtime.RuntimeInstallTest", str(directory / "runtime")],
                check=True, capture_output=True, text=True)
            self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
