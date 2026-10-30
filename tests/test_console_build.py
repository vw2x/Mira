import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_console_dependencies_follow_package_lock(tmp_path: Path) -> None:
    console = tmp_path / "console"
    console.mkdir()
    package_file = console / "package.json"
    package_file.write_text('{"scripts": {"build": "next build"}}\n')
    lock_file = console / "package-lock.json"
    lock_file.write_text('{"lockfileVersion": 3}\n')
    log_file = tmp_path / "npm.log"
    fake_npm = tmp_path / "npm"
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >> \"$MIRA_NPM_LOG\"\n"
        "if [[ \"${*: -1}\" == ci ]]; then mkdir -p \"$MIRA_CONSOLE_DIR/node_modules\"; fi\n"
    )
    fake_npm.chmod(0o755)
    env = dict(
        os.environ,
        MIRA_CONSOLE_DIR=str(console),
        MIRA_NPM_BIN=str(fake_npm),
        MIRA_NPM_LOG=str(log_file),
    )

    helper = ROOT / "tools/relay/build-console.sh"
    subprocess.run(["bash", str(helper)], env=env, check=True)
    subprocess.run(["bash", str(helper)], env=env, check=True)
    lock_file.write_text('{"lockfileVersion": 3, "changed": true}\n')
    subprocess.run(["bash", str(helper)], env=env, check=True)

    prefix = f"--prefix {console}"
    assert log_file.read_text().splitlines() == [
        f"{prefix} ci",
        f"{prefix} run build",
        f"{prefix} run build",
        f"{prefix} ci",
        f"{prefix} run build",
    ]
    source_hashes = "".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "\n"
        for path in (package_file, lock_file)
    ).encode()
    expected = hashlib.sha256(source_hashes).hexdigest()
    assert (console / "node_modules/.mira-console-dependencies.sha256").read_text().strip() == expected


def test_failed_install_keeps_stale_stamp_and_skips_build(tmp_path: Path) -> None:
    console = tmp_path / "console"
    (console / "node_modules").mkdir(parents=True)
    (console / "package.json").write_text('{"name": "console"}\n')
    (console / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    stamp = console / "node_modules/.mira-console-dependencies.sha256"
    stamp.write_text("stale\n")
    log_file = tmp_path / "npm.log"
    fake_npm = tmp_path / "npm"
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$MIRA_NPM_LOG\"\n"
        "[[ \"${*: -1}\" != ci ]]\n"
    )
    fake_npm.chmod(0o755)
    env = dict(
        os.environ,
        MIRA_CONSOLE_DIR=str(console),
        MIRA_NPM_BIN=str(fake_npm),
        MIRA_NPM_LOG=str(log_file),
    )

    result = subprocess.run(["bash", str(ROOT / "tools/relay/build-console.sh")], env=env)

    assert result.returncode != 0
    assert stamp.read_text() == "stale\n"
    assert log_file.read_text().splitlines() == [f"--prefix {console} ci"]


def test_relay_launchers_share_console_build_helper() -> None:
    expected = 'bash "${ROOT_DIR}/tools/relay/build-console.sh"'
    for relative in ("tools/relay/start-local-relay.sh", "tools/relay/start-public-relay.sh"):
        assert expected in (ROOT / relative).read_text()
