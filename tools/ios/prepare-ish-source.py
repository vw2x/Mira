#!/usr/bin/env python3
"""Apply Mira's reviewed compatibility patch to its pinned iSH checkout."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
ISH_REVISION = "de5ab2c4b1039551203c1106e75f715cd917544b"


def prepare(source: Path, patch: Path, revision: str = ISH_REVISION) -> None:
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != revision:
        raise RuntimeError(f"iSH revision {head} has no reviewed Mira patch; expected {revision}")
    command = ["git", "-C", str(source), "apply"]
    already_applied = subprocess.run(
        [*command, "--reverse", "--check", str(patch)], capture_output=True, text=True
    )
    if already_applied.returncode == 0:
        return
    check = subprocess.run(
        [*command, "--check", str(patch)], capture_output=True, text=True
    )
    if check.returncode:
        raise RuntimeError("iSH compatibility patch conflicts with local changes; nothing was changed.\n" + check.stderr.strip())
    subprocess.run([*command, str(patch)], check=True)


def main() -> int:
    try:
        prepare(ROOT / "third_party/ish", ROOT / "tools/ios/patches/ish-mira.patch")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"prepare-ish-source: {error}", file=sys.stderr)
        return 1
    print(f"iSH {ISH_REVISION[:12]} with Mira compatibility patch is ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
