import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from mira import release
from mira.release import ReleaseError, validate_archive_root


def write_archive(path: Path, names: list[str]) -> None:
    with ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"test")


def test_validate_archive_root_accepts_expected_tree(tmp_path: Path) -> None:
    archive_path = tmp_path / "Mira-unsigned.ipa"
    write_archive(archive_path, ["Payload/Mira.app/Info.plist", "Payload/Mira.app/Mira"])

    validate_archive_root(archive_path, "Payload")


def test_validate_archive_root_rejects_flat_ipa(tmp_path: Path) -> None:
    archive_path = tmp_path / "Mira-unsigned.ipa"
    write_archive(archive_path, ["Mira.app/Info.plist", "Mira.app/Mira"])

    with pytest.raises(ReleaseError, match="预期 Payload/"):
        validate_archive_root(archive_path, "Payload")


def write_version_sources(
    root: Path,
    *,
    python_version: str = "1.2.3",
    android_version: str = "1.2.3",
    android_build: int = 123,
    ios_version: str = "1.2.3",
    ios_build: int = 123,
) -> None:
    (root / "mira").mkdir()
    (root / "android/app").mkdir(parents=True)
    (root / "ios/Mira/Mira.xcodeproj").mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'version = "{python_version}"\n')
    (root / "mira/__init__.py").write_text(f'__version__ = "{python_version}"\n')
    (root / "android/app/build.gradle").write_text(
        f'        versionCode {android_build}\n        versionName "{android_version}"\n'
    )
    (root / "ios/Mira/Mira.xcodeproj/project.pbxproj").write_text(
        f"MARKETING_VERSION = {ios_version};\nCURRENT_PROJECT_VERSION = {ios_build};\n"
    )


def test_validate_release_versions_checks_all_platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_version_sources(tmp_path)
    monkeypatch.setattr(release, "ROOT_DIR", tmp_path)

    info = release.validate_release_versions("1.2.3")

    assert info.android_version == info.ios_version == "1.2.3"
    assert info.android_build == info.ios_build == 123


def test_validate_release_versions_rejects_stale_mobile_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_version_sources(tmp_path, android_version="1.2.2")
    monkeypatch.setattr(release, "ROOT_DIR", tmp_path)

    with pytest.raises(ReleaseError, match="版本源不一致"):
        release.validate_release_versions("1.2.3")


def test_validate_release_versions_rejects_stale_mobile_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_version_sources(tmp_path, android_build=122, ios_build=122)
    monkeypatch.setattr(release, "ROOT_DIR", tmp_path)

    with pytest.raises(ReleaseError, match="移动端构建号与发布版本不匹配"):
        release.validate_release_versions("1.2.3")


def write_fake_mach_o(path: Path, magic: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"fake-mach-o")


def test_sensitive_environment_entries_are_excluded_from_ios_staging() -> None:
    ignored = release.sensitive_environment_entries_to_ignore(
        "unused",
        ["Info.plist", ".env", ".env.local", ".env.example"],
    )

    assert ignored == [".env", ".env.local"]


def test_has_code_signature_accepts_only_the_known_unsigned_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsigned_result = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stderr=b"code object is not signed at all\n",
    )
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs: unsigned_result)

    assert not release.has_code_signature(tmp_path / "unsigned-binary")

    inspection_error = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stderr=b"unexpected codesign inspection error\n",
    )
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs: inspection_error)

    with pytest.raises(ReleaseError, match="无法判断 iOS 代码签名状态"):
        release.has_code_signature(tmp_path / "uninspectable-binary")


def test_prepare_unsigned_ios_app_strips_signed_mach_o_without_mutating_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_app = tmp_path / "source/Mira.app"
    destination_app = tmp_path / "staging/Mira.app"
    write_fake_mach_o(source_app / "Mira", b"\xcf\xfa\xed\xfe")
    write_fake_mach_o(source_app / "Frameworks/libdynamic.dylib", b"\xca\xfe\xba\xbe")
    write_fake_mach_o(source_app / "Frameworks/unsigned.dylib", b"\xfe\xed\xfa\xcf")
    (source_app / "Assets.car").write_bytes(b"not-mach-o")
    (source_app / "_CodeSignature").mkdir()
    (source_app / "_CodeSignature/CodeResources").write_bytes(b"signature")
    (source_app / "Frameworks/Nested.framework/_CodeSignature").mkdir(parents=True)
    (source_app / "Frameworks/Nested.framework/_CodeSignature/CodeResources").write_bytes(b"signature")
    (source_app / "embedded.mobileprovision").write_bytes(b"profile")

    signed = {"Mira", "Frameworks/libdynamic.dylib"}
    inspected: list[str] = []
    removed: list[str] = []

    def fake_has_code_signature(path: Path) -> bool:
        relative = path.relative_to(destination_app).as_posix()
        inspected.append(relative)
        return relative in signed

    def fake_remove_code_signature(path: Path) -> None:
        relative = path.relative_to(destination_app).as_posix()
        removed.append(relative)
        signed.remove(relative)

    monkeypatch.setattr(release, "has_code_signature", fake_has_code_signature)
    monkeypatch.setattr(release, "remove_code_signature", fake_remove_code_signature)

    stripped = release.prepare_unsigned_ios_app(source_app, destination_app)

    assert {path.relative_to(destination_app).as_posix() for path in stripped} == {
        "Mira",
        "Frameworks/libdynamic.dylib",
    }
    assert set(removed) == {"Mira", "Frameworks/libdynamic.dylib"}
    assert set(inspected) == {"Mira", "Frameworks/libdynamic.dylib", "Frameworks/unsigned.dylib"}
    assert (source_app / "_CodeSignature/CodeResources").is_file()
    assert (source_app / "embedded.mobileprovision").is_file()
    assert not (destination_app / "_CodeSignature").exists()
    assert not (destination_app / "Frameworks/Nested.framework/_CodeSignature").exists()
    assert not (destination_app / "embedded.mobileprovision").exists()
    assert (destination_app / "Assets.car").read_bytes() == b"not-mach-o"


def test_strip_ios_code_signatures_rejects_signature_that_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_path = tmp_path / "Mira.app"
    write_fake_mach_o(app_path / "Mira", b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(release, "has_code_signature", lambda _path: True)
    monkeypatch.setattr(release, "remove_code_signature", lambda _path: None)

    with pytest.raises(ReleaseError, match="代码签名移除失败"):
        release.strip_ios_code_signatures(app_path)


def test_package_ios_outputs_archives_the_same_sanitized_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_app = tmp_path / "source/Mira.app"
    source_app.mkdir(parents=True)
    (source_app / "Mira").write_bytes(b"signed")
    dist_dir = tmp_path / "dist/ios"
    archived_contents: dict[str, bytes] = {}

    def fake_prepare_unsigned_ios_app(source: Path, destination: Path) -> list[Path]:
        shutil.copytree(source, destination)
        executable = destination / "Mira"
        executable.write_bytes(b"unsigned")
        return [executable]

    def fake_make_zip_from_directory(source: Path, output: Path, root_name: str) -> None:
        archived_contents["app-zip"] = (source / "Mira").read_bytes()
        write_archive(output, [f"{root_name}/Info.plist"])

    def fake_make_ipa_from_app(source: Path, output: Path) -> None:
        archived_contents["ipa"] = (source / "Mira").read_bytes()
        write_archive(output, ["Payload/Mira.app/Info.plist"])

    monkeypatch.setattr(release, "IOS_DIST_DIR", dist_dir)
    monkeypatch.setattr(release, "prepare_unsigned_ios_app", fake_prepare_unsigned_ios_app)
    monkeypatch.setattr(release, "make_zip_from_directory", fake_make_zip_from_directory)
    monkeypatch.setattr(release, "make_ipa_from_app", fake_make_ipa_from_app)

    artifacts = release.package_ios_outputs(source_app)

    assert (source_app / "Mira").read_bytes() == b"signed"
    assert archived_contents == {"app-zip": b"unsigned", "ipa": b"unsigned"}
    assert [Path(artifact.path).name for artifact in artifacts] == [
        "Mira.app.zip",
        "Mira-unsigned.ipa",
    ]


def test_package_ios_outputs_keeps_previous_pair_when_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_app = tmp_path / "source/Mira.app"
    source_app.mkdir(parents=True)
    dist_dir = tmp_path / "dist/ios"
    dist_dir.mkdir(parents=True)
    app_zip = dist_dir / "Mira.app.zip"
    ipa_path = dist_dir / "Mira-unsigned.ipa"
    app_zip.write_bytes(b"previous-app-zip")
    ipa_path.write_bytes(b"previous-ipa")

    def fake_prepare_unsigned_ios_app(source: Path, destination: Path) -> list[Path]:
        shutil.copytree(source, destination)
        return []

    def fake_make_zip_from_directory(source: Path, output: Path, root_name: str) -> None:
        write_archive(output, [f"{root_name}/Info.plist"])

    def fake_make_ipa_from_app(_source: Path, _output: Path) -> None:
        raise ReleaseError("IPA staging failed")

    monkeypatch.setattr(release, "IOS_DIST_DIR", dist_dir)
    monkeypatch.setattr(release, "prepare_unsigned_ios_app", fake_prepare_unsigned_ios_app)
    monkeypatch.setattr(release, "make_zip_from_directory", fake_make_zip_from_directory)
    monkeypatch.setattr(release, "make_ipa_from_app", fake_make_ipa_from_app)

    with pytest.raises(ReleaseError, match="IPA staging failed"):
        release.package_ios_outputs(source_app)

    assert app_zip.read_bytes() == b"previous-app-zip"
    assert ipa_path.read_bytes() == b"previous-ipa"
