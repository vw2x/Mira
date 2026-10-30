"""Mira 统一构建入口."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

ROOT_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT_DIR / "dist"
PYTHON_DIST_DIR = DIST_DIR / "python"
ANDROID_DIST_DIR = DIST_DIR / "android"
IOS_DIST_DIR = DIST_DIR / "ios"
DEFAULT_ANDROID_APK = ROOT_DIR / "android/app/build/outputs/apk/debug/mira-app-debug.apk"
DEFAULT_IOS_PROJECT = ROOT_DIR / "ios/Mira/Mira.xcodeproj"
DEFAULT_IOS_SCHEME = "Mira"
DEFAULT_IOS_DERIVED_DATA = ROOT_DIR / "build/ios-mira-device-native-relay-derived"
DEFAULT_IOS_APP = DEFAULT_IOS_DERIVED_DATA / "Build/Products/Debug-iphoneos/Mira.app"


class ReleaseError(Exception):
    """统一构建错误."""


@dataclass
class ArtifactRecord:
    target: str
    path: str
    kind: str


@dataclass
class BuildResult:
    target: str
    status: str
    artifacts: list[ArtifactRecord]
    details: dict[str, Any]


@dataclass
class VersionInfo:
    pyproject: str
    package: str
    android_version: str
    android_build: int
    ios_version: str
    ios_build: int
    expected: str | None


def run_command(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, dry_run: bool = False) -> None:
    rendered = " ".join(shell_quote(part) for part in command)
    print(f"[mira-build] {rendered}")
    if dry_run:
        return
    subprocess.run(command, cwd=cwd or ROOT_DIR, env=env, check=True)


def shell_quote(value: str) -> str:
    if not value or any(char.isspace() or char in '"\'"\'`$' for char in value):
        return subprocess.list2cmdline([value])
    return value


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_pyproject_version() -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', read_text(ROOT_DIR / "pyproject.toml"))
    if not match:
        raise ReleaseError("未在 pyproject.toml 中找到 project.version")
    return match.group(1)


def read_package_version() -> str:
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', read_text(ROOT_DIR / "mira/__init__.py"))
    if not match:
        raise ReleaseError("未在 mira/__init__.py 中找到 __version__")
    return match.group(1)


def read_android_version() -> tuple[str, int]:
    source = read_text(ROOT_DIR / "android/app/build.gradle")
    version_match = re.search(r'(?m)^\s*versionName\s+"([^"]+)"\s*$', source)
    build_match = re.search(r"(?m)^\s*versionCode\s+(\d+)\s*$", source)
    if not version_match or not build_match:
        raise ReleaseError("未在 android/app/build.gradle 中找到 versionName/versionCode")
    return version_match.group(1), int(build_match.group(1))


def read_ios_version() -> tuple[str, int]:
    source = read_text(ROOT_DIR / "ios/Mira/Mira.xcodeproj/project.pbxproj")
    versions = set(re.findall(r"(?m)^\s*MARKETING_VERSION\s*=\s*([^;]+);\s*$", source))
    builds = set(re.findall(r"(?m)^\s*CURRENT_PROJECT_VERSION\s*=\s*(\d+);\s*$", source))
    if len(versions) != 1 or len(builds) != 1:
        raise ReleaseError(
            "iOS Debug/Release 版本源不一致或缺失: "
            f"MARKETING_VERSION={sorted(versions)}, "
            f"CURRENT_PROJECT_VERSION={sorted(builds)}"
        )
    return versions.pop().strip(), int(builds.pop())


def release_build_number(version: str) -> int | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return int("".join(match.groups()))


def normalize_release_version(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith("refs/tags/"):
        normalized = normalized[len("refs/tags/") :]
    if normalized.startswith("v"):
        normalized = normalized[1:]
    return normalized


def resolve_expected_release_version(args: argparse.Namespace) -> str | None:
    if args.release_tag:
        return normalize_release_version(args.release_tag)
    if os.environ.get("MIRA_RELEASE_TAG"):
        return normalize_release_version(os.environ["MIRA_RELEASE_TAG"])
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and os.environ.get("GITHUB_REF_NAME"):
        return normalize_release_version(os.environ["GITHUB_REF_NAME"])
    if os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
        return normalize_release_version(os.environ["GITHUB_REF"])
    return None


def collect_version_info(expected_version: str | None = None) -> VersionInfo:
    android_version, android_build = read_android_version()
    ios_version, ios_build = read_ios_version()
    return VersionInfo(
        pyproject=read_pyproject_version(),
        package=read_package_version(),
        android_version=android_version,
        android_build=android_build,
        ios_version=ios_version,
        ios_build=ios_build,
        expected=expected_version,
    )


def validate_release_versions(expected_version: str | None = None) -> VersionInfo:
    info = collect_version_info(expected_version)
    versions = {
        "pyproject.toml": info.pyproject,
        "mira/__init__.py": info.package,
        "android/app/build.gradle": info.android_version,
        "ios/Mira/Mira.xcodeproj/project.pbxproj": info.ios_version,
    }
    if len(set(versions.values())) != 1:
        raise ReleaseError(
            "版本源不一致: " + ", ".join(f"{path}={version}" for path, version in versions.items())
        )
    if info.android_build != info.ios_build:
        raise ReleaseError(
            "移动端构建号不一致: "
            f"Android versionCode={info.android_build}, "
            f"iOS CURRENT_PROJECT_VERSION={info.ios_build}"
        )
    expected_build = release_build_number(info.pyproject)
    if expected_build is not None and info.android_build != expected_build:
        raise ReleaseError(
            "移动端构建号与发布版本不匹配: "
            f"version={info.pyproject}, expectedBuild={expected_build}, "
            f"actualBuild={info.android_build}"
        )
    if info.expected and info.pyproject != info.expected:
        raise ReleaseError(
            "发布版本不匹配: "
            f"tag={info.expected}, "
            f"pyproject.toml={info.pyproject}, "
            f"mira/__init__.py={info.package}, "
            f"Android={info.android_version}, "
            f"iOS={info.ios_version}"
        )
    return info


def validate_python_artifacts(version_info: VersionInfo, artifact_names: list[str]) -> None:
    expected_version = version_info.pyproject
    expected_wheel = f"mira-{expected_version}-py3-none-any.whl"
    expected_sdist = f"mira-{expected_version}.tar.gz"
    names = set(artifact_names)
    missing: list[str] = []
    if expected_wheel not in names:
        missing.append(expected_wheel)
    if expected_sdist not in names:
        missing.append(expected_sdist)
    if missing:
        raise ReleaseError(
            "Python 构建产物版本不匹配: "
            f"缺少 {', '.join(missing)}, 实际产物 {', '.join(sorted(names)) or '<empty>'}"
        )


def write_manifest(results: list[BuildResult]) -> Path:
    ensure_directory(DIST_DIR)
    manifest_path = DIST_DIR / "release-manifest.json"
    payload = {
        "projectRoot": str(ROOT_DIR),
        "results": [asdict(result) for result in results],
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest_path


def build_python(args: argparse.Namespace) -> BuildResult:
    version_info = validate_release_versions(resolve_expected_release_version(args))
    ensure_directory(PYTHON_DIST_DIR)
    artifacts: list[ArtifactRecord] = []
    if args.dry_run:
        command = [
            args.python,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            "<tempdir>",
            "--no-isolation",
        ]
        run_command(command, cwd=ROOT_DIR, dry_run=True)
        return BuildResult(
            target="python",
            status="dry-run",
            artifacts=[],
            details={
                "distDir": str(PYTHON_DIST_DIR),
                "packageVersion": version_info.pyproject,
                "releaseTagVersion": version_info.expected,
            },
        )

    with tempfile.TemporaryDirectory(prefix="mira-python-dist-") as temp_dir:
        temp_dist_dir = Path(temp_dir)
        command = [
            args.python,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            str(temp_dist_dir),
            "--no-isolation",
        ]
        run_command(command, cwd=ROOT_DIR, dry_run=False)
        for path in sorted(temp_dist_dir.iterdir()):
            destination = PYTHON_DIST_DIR / path.name
            shutil.copy2(path, destination)
            artifacts.append(
                ArtifactRecord(
                    target="python",
                    path=str(destination),
                    kind="wheel" if destination.suffix == ".whl" else "sdist",
                )
            )
    if not artifacts and not args.dry_run:
        raise ReleaseError(f"Python 包产物缺失: {PYTHON_DIST_DIR}")
    validate_python_artifacts(version_info, [Path(artifact.path).name for artifact in artifacts])
    return BuildResult(
        target="python",
        status="built",
        artifacts=artifacts,
        details={
            "distDir": str(PYTHON_DIST_DIR),
            "packageVersion": version_info.pyproject,
            "releaseTagVersion": version_info.expected,
        },
    )


def build_android(args: argparse.Namespace) -> BuildResult:
    ensure_directory(ANDROID_DIST_DIR)
    command = [str(ROOT_DIR / "gradlew"), args.android_gradle_task]
    run_command(command, cwd=ROOT_DIR, dry_run=args.dry_run)

    destination = ANDROID_DIST_DIR / args.android_output_name
    artifacts: list[ArtifactRecord] = []
    if not args.dry_run:
        if not DEFAULT_ANDROID_APK.is_file():
            raise ReleaseError(f"Android APK 缺失: {DEFAULT_ANDROID_APK}")
        shutil.copy2(DEFAULT_ANDROID_APK, destination)
        artifacts.append(ArtifactRecord(target="android", path=str(destination), kind="apk"))
    return BuildResult(
        target="android",
        status="dry-run" if args.dry_run else "built",
        artifacts=artifacts,
        details={
            "gradleTask": args.android_gradle_task,
            "sourceApk": str(DEFAULT_ANDROID_APK),
            "distDir": str(ANDROID_DIST_DIR),
        },
    )


def detect_ios_device_id(explicit_device_id: str | None) -> str | None:
    if explicit_device_id:
        return explicit_device_id

    completed = subprocess.run(
        ["xcrun", "xctrace", "list", "devices"],
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None

    in_devices_section = False
    pattern = re.compile(r"^(?P<name>.+) \((?P<version>[^()]+)\) \((?P<identifier>[^()]+)\)$")
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if line == "== Devices ==":
            in_devices_section = True
            continue
        if line.startswith("== ") and line.endswith(" =="):
            in_devices_section = False
        if not in_devices_section or not line:
            continue
        match = pattern.match(line)
        if not match:
            continue
        return match.group("identifier")
    return None


def make_zip_from_directory(source_dir: Path, output_path: Path, root_name: str) -> None:
    if output_path.exists():
        output_path.unlink()
    with tempfile.TemporaryDirectory(prefix="mira-zip-") as temp_dir:
        staging_root = Path(temp_dir) / root_name
        shutil.copytree(source_dir, staging_root)
        subprocess.run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(staging_root),
                str(output_path),
            ],
            check=True,
            cwd=temp_dir,
        )


def validate_archive_root(archive_path: Path, expected_root: str) -> None:
    prefix = expected_root.rstrip("/") + "/"
    try:
        with ZipFile(archive_path) as archive:
            names = [
                entry.filename
                for entry in archive.infolist()
                if not entry.filename.startswith("__MACOSX/") and entry.filename != "__MACOSX/"
            ]
    except BadZipFile as exc:
        raise ReleaseError(f"归档格式无效: {archive_path}") from exc
    if not names or any(name != prefix and not name.startswith(prefix) for name in names):
        raise ReleaseError(f"归档根目录无效: {archive_path}, 预期 {prefix}")


def package_ios_outputs(app_path: Path) -> list[ArtifactRecord]:
    ensure_directory(IOS_DIST_DIR)
    app_zip = IOS_DIST_DIR / f"{app_path.name}.zip"
    make_zip_from_directory(app_path, app_zip, app_path.name)
    validate_archive_root(app_zip, app_path.name)

    ipa_path = IOS_DIST_DIR / "Mira-unsigned.ipa"
    payload_dir = Path(tempfile.mkdtemp(prefix="mira-payload-"))
    try:
        payload_root = payload_dir / "Payload"
        payload_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(app_path, payload_root / app_path.name)
        if ipa_path.exists():
            ipa_path.unlink()
        subprocess.run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(payload_root),
                str(ipa_path),
            ],
            check=True,
            cwd=payload_dir,
        )
    finally:
        shutil.rmtree(payload_dir, ignore_errors=True)
    validate_archive_root(ipa_path, "Payload")

    return [
        ArtifactRecord(target="ios", path=str(app_zip), kind="app-zip"),
        ArtifactRecord(target="ios", path=str(ipa_path), kind="unsigned-ipa"),
    ]


def build_ios(args: argparse.Namespace) -> BuildResult:
    ensure_directory(IOS_DIST_DIR)
    requested_mode = args.ios_destination_mode
    detected_device_id = detect_ios_device_id(args.ios_device_id)

    if requested_mode == "device":
        destination_mode = "device"
        device_id = detected_device_id
    elif requested_mode == "generic":
        destination_mode = "generic"
        device_id = None
    else:
        if detected_device_id:
            destination_mode = "device"
            device_id = detected_device_id
        else:
            destination_mode = "generic"
            device_id = None

    if args.dry_run and destination_mode == "device" and not device_id:
        device_id = args.ios_device_id or "<device-udid>"

    if destination_mode == "device" and not device_id:
        raise ReleaseError("未检测到可用 iOS 真机. 请通过 --ios-device-id 或 MIRA_IOS_DEVICE_ID 指定 UDID, 或改用 --ios-destination-mode generic.")

    app_path = Path(args.ios_app_path)
    rootfs_output = app_path / "MiraISHRoot.fakefs"

    env = os.environ.copy()
    env.pop("LIBRARY_PATH", None)
    env.pop("SDKROOT", None)
    env.setdefault("MIRA_ISH_ROOTFS_OUTPUT", str(rootfs_output))
    command = [
        "xcodebuild",
        "-project",
        str(DEFAULT_IOS_PROJECT),
        "-scheme",
        args.ios_scheme,
        "-configuration",
        "Debug",
        "-sdk",
        "iphoneos",
        "-derivedDataPath",
        str(args.ios_derived_data),
        "ENABLE_DEBUG_DYLIB=NO",
        "ENABLE_PREVIEWS=NO",
    ]

    if destination_mode == "device":
        command.extend(
            [
                "-destination",
                f"id={device_id}",
                "-allowProvisioningUpdates",
                "-allowProvisioningDeviceRegistration",
                "build",
            ]
        )
    else:
        command.extend(
            [
                "-destination",
                "generic/platform=iOS",
                "CODE_SIGNING_ALLOWED=NO",
                "CODE_SIGNING_REQUIRED=NO",
                "CODE_SIGN_IDENTITY=",
                "build",
            ]
        )

    run_command(command, cwd=ROOT_DIR, env=env, dry_run=args.dry_run)

    artifacts: list[ArtifactRecord] = []
    if not args.dry_run:
        if not app_path.is_dir():
            raise ReleaseError(f"iOS .app 产物缺失: {app_path}")
        artifacts = package_ios_outputs(app_path)
    return BuildResult(
        target="ios",
        status="dry-run" if args.dry_run else "built",
        artifacts=artifacts,
        details={
            "destinationMode": destination_mode,
            "deviceId": device_id,
            "derivedDataPath": str(args.ios_derived_data),
            "appPath": str(app_path),
            "rootfsOutput": str(rootfs_output),
            "distDir": str(IOS_DIST_DIR),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mira 统一构建入口")
    parser.add_argument("--target", dest="targets", action="append", choices=["python", "android", "ios", "all"], help="要构建的目标, 可重复传入")
    parser.add_argument("--python", default=os.environ.get("PYTHON_BIN", sys.executable or "python3"), help="Python 可执行文件")
    parser.add_argument("--release-tag", default=os.environ.get("MIRA_RELEASE_TAG"), help="要校验的发布 tag 或版本号, 例如 v1.1.2")
    parser.add_argument("--validate-only", action="store_true", help="仅校验版本一致性, 不执行构建")
    parser.add_argument("--android-gradle-task", default=os.environ.get("MIRA_ANDROID_GRADLE_TASK", ":mira-app:assembleDebug"), help="Android Gradle 任务")
    parser.add_argument("--android-output-name", default=os.environ.get("MIRA_ANDROID_DIST_NAME", "mira-app-debug.apk"), help="dist/android 下的 APK 文件名")
    parser.add_argument("--ios-device-id", default=os.environ.get("MIRA_IOS_DEVICE_ID"), help="iOS 真机 UDID")
    parser.add_argument("--ios-destination-mode", choices=["auto", "device", "generic"], default=os.environ.get("MIRA_IOS_DESTINATION_MODE", "auto"), help="iOS 构建目标模式. auto 优先真机, 无真机时退回 generic iphoneos")
    parser.add_argument("--ios-scheme", default=os.environ.get("MIRA_IOS_SCHEME", DEFAULT_IOS_SCHEME), help="iOS scheme")
    parser.add_argument("--ios-derived-data", type=Path, default=Path(os.environ.get("MIRA_IOS_DEVICE_DERIVED_DATA", str(DEFAULT_IOS_DERIVED_DATA))), help="iOS DerivedData 目录")
    parser.add_argument("--ios-app-path", type=Path, default=Path(os.environ.get("MIRA_IOS_DEVICE_APP_PATH", str(DEFAULT_IOS_APP))), help="iOS device .app 路径")
    parser.add_argument("--dry-run", action="store_true", help="仅打印命令, 不实际执行")
    return parser


def normalize_targets(raw_targets: list[str] | None) -> list[str]:
    if not raw_targets:
        return ["python", "android", "ios"]
    resolved: list[str] = []
    for item in raw_targets:
        if item == "all":
            for target in ["python", "android", "ios"]:
                if target not in resolved:
                    resolved.append(target)
            continue
        if item not in resolved:
            resolved.append(item)
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    expected_release_version = resolve_expected_release_version(args)

    if args.validate_only:
        try:
            info = validate_release_versions(expected_release_version)
        except ReleaseError as exc:
            sys.stderr.write(f"mira-build: {exc}\n")
            return 1
        print(
            "[mira-build] 版本校验通过: "
            f"pyproject.toml={info.pyproject}, "
            f"mira/__init__.py={info.package}, "
            f"Android={info.android_version} ({info.android_build}), "
            f"iOS={info.ios_version} ({info.ios_build}), "
            f"releaseTag={info.expected or '<none>'}"
        )
        return 0

    targets = normalize_targets(args.targets)

    builders = {
        "python": build_python,
        "android": build_android,
        "ios": build_ios,
    }

    results: list[BuildResult] = []
    try:
        for target in targets:
            result = builders[target](args)
            results.append(result)
        manifest_path = write_manifest(results)
    except (ReleaseError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"mira-build: {exc}\n")
        if results:
            manifest_path = write_manifest(results)
            sys.stderr.write(f"mira-build: 已写入部分构建清单 {manifest_path}\n")
        return 1

    print(f"[mira-build] 清单: {manifest_path}")
    for result in results:
        print(f"[mira-build] {result.target}: {result.status}")
        for artifact in result.artifacts:
            print(f"  - {artifact.kind}: {artifact.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
