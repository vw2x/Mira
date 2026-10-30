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
