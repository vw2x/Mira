#!/usr/bin/env python3
import contextlib
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Iterable


def resolve_termux_repos() -> tuple[str, ...]:
    repos_csv = os.environ.get("MIRA_TERMUX_REPOS")
    if repos_csv:
        repos = tuple(repo.strip().rstrip("/") for repo in repos_csv.split(",") if repo.strip())
        if repos:
            return repos

    explicit = os.environ.get("MIRA_TERMUX_REPO")
    if explicit:
        return (explicit.rstrip("/"),)

    return (
        "https://packages.termux.dev/apt/termux-main",
        "https://grimler.se/termux-packages-24",
        "https://packages-cf.termux.dev/apt/termux-main",
    )


TERMUX_REPOS = resolve_termux_repos()
PRIMARY_TERMUX_REPO = TERMUX_REPOS[0]
TARGET_ABI = "arm64-v8a"
TERMUX_PREFIX_PATH = "/data/data/com.termux/files/usr"
MIRA_PREFIX_PATH = "/data/data/com.vwww.mira/files/usr"
FRIDA_VERSION = os.environ.get("MIRA_FRIDA_VERSION", "16.0.7")
ANDROID_API = os.environ.get("MIRA_ANDROID_API", "23")
ROOT_PACKAGES = ("python", "python-pip")
PURE_PYTHON_SPECS = (
    "colorama<1.0.0,>=0.2.7",
    "prompt-toolkit<4.0.0,>=2.0.0",
    "pygments<3.0.0,>=2.0.2",
    "wcwidth",
)
EXPECTED_IMPORTS = ("frida_tools", "colorama", "prompt_toolkit", "pygments", "wcwidth", "pip")

SCRIPT_PATH = Path(__file__).resolve()
ROOT_DIR = SCRIPT_PATH.parents[2]
FRIDA_TOOLS_DIR = ROOT_DIR / "third_party" / "frida-tools"
DEFAULT_OUT_ROOT = ROOT_DIR / "android" / "app" / "build" / "generated" / "mira-toolbox-assets"
BUILD_ROOT = ROOT_DIR / "build" / "termux-prefix"
DEB_CACHE_DIR = BUILD_ROOT / "debs"
WHEEL_CACHE_DIR = BUILD_ROOT / "wheels"
SDIST_CACHE_DIR = BUILD_ROOT / "sdists"
INDEX_CACHE_PATH = BUILD_ROOT / "Packages.aarch64"
ANDROID_SDK_ROOT = Path(os.environ.get("ANDROID_SDK_ROOT", str(Path.home() / "Library" / "Android" / "sdk"))).expanduser()
ANDROID_NDK_ROOT = Path(
    os.environ.get("ANDROID_NDK_ROOT", str(ANDROID_SDK_ROOT / "ndk" / os.environ.get("MIRA_NDK_VERSION", "29.0.14206865")))
).expanduser()
FRIDA_DEVKIT_DIR = ROOT_DIR / "build" / "frida" / "devkit" / FRIDA_VERSION / "android-arm64"
FRIDA_DEVKIT_TAR = FRIDA_DEVKIT_DIR / f"frida-core-devkit-{FRIDA_VERSION}-android-arm64.tar.xz"
FRIDA_DEVKIT_URL = (
    f"https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/frida-core-devkit-{FRIDA_VERSION}-android-arm64.tar.xz"
)
FRIDA_DEVKIT_SHA256 = {
    "16.0.7": "a77fe0caabeadaa3ee053b1b24a91968b9a3854fba3b9e39bb3886911a18207b",
    "16.7.19": "219250c50c3894386793945bfc46a26d85d2c2b38454db682dadba2a0b3288b1",
}
FRIDA_SDIST_SHA256 = {
    "16.0.7": "8e6e285fd9846aa045d6827b90b5ad2fe2e42117c4bb0e453bd825da0ad825a1",
    "16.7.19": "3f266fd01b61f4c0c7079435f8e44d0789abca36c326b97925df98d61eb3dc9c",
}


def verify_sha256(path: Path, expected: str, label: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{label} SHA-256 不匹配: expected={expected}, actual={actual}, path={path}")


def resolve_toolchain_dir() -> Path:
    explicit = os.environ.get("MIRA_ANDROID_NDK_TOOLCHAIN_DIR")
    if explicit:
        return Path(explicit).expanduser()

    prebuilt_root = ANDROID_NDK_ROOT / "toolchains" / "llvm" / "prebuilt"
    candidates: list[Path] = []

    platform = sys.platform
    machine = os.uname().machine if hasattr(os, "uname") else ""
    if platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            candidates.extend(
                [
                    prebuilt_root / "darwin-arm64" / "bin",
                    prebuilt_root / "darwin-x86_64" / "bin",
                ]
            )
        else:
            candidates.append(prebuilt_root / "darwin-x86_64" / "bin")
    elif platform.startswith("linux"):
        candidates.append(prebuilt_root / "linux-x86_64" / "bin")
    else:
        candidates.append(prebuilt_root / "windows-x86_64" / "bin")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    for candidate in sorted(prebuilt_root.glob("*/bin")):
        if candidate.is_dir():
            return candidate

    raise FileNotFoundError(f"缺少 Android NDK toolchain 目录: {prebuilt_root}")


TOOLCHAIN_DIR = resolve_toolchain_dir()


@dataclass(frozen=True)
class PackageInfo:
    name: str
    version: str
    filename: str
    depends: tuple[tuple[str, ...], ...]

    @property
    def url(self) -> str:
        return f"{PRIMARY_TERMUX_REPO}/{self.filename}"


def log(message: str) -> None:
    print(f"[mira-termux] {message}", flush=True)


def run(cmd: list[str], **kwargs) -> None:
    log("run: " + " ".join(shlex.quote(part) for part in cmd))
    subprocess.run(cmd, check=True, **kwargs)


def fetch(url: str | Iterable[str], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MIRA_FORCE_FETCH") != "1" and destination.is_file() and destination.stat().st_size > 0:
        log(f"reuse cached file: {destination}")
        return
    urls = (url,) if isinstance(url, str) else tuple(url)
    last_error: subprocess.CalledProcessError | None = None
    for candidate in urls:
        log(f"fetch candidate: {candidate}")
        try:
            run(
                [
                    "curl",
                    "-L",
                    "--fail",
                    "--retry",
                    "5",
                    "--retry-all-errors",
                    "--retry-delay",
                    "2",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    "180",
                    "--continue-at",
                    "-",
                    "-o",
                    str(destination),
                    candidate,
                ]
            )
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if destination.exists():
                destination.unlink()
            log(f"fetch failed: {candidate} (exit={exc.returncode})")
    if last_error is not None:
        raise last_error
    raise RuntimeError("no candidate urls provided")


def read_package_index() -> str:
    fetch(
        tuple(f"{repo}/dists/stable/main/binary-aarch64/Packages" for repo in TERMUX_REPOS),
        INDEX_CACHE_PATH,
    )
    return INDEX_CACHE_PATH.read_text(encoding="utf-8")


def parse_package_index(raw_text: str) -> dict[str, PackageInfo]:
    packages: dict[str, PackageInfo] = {}
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        current_key: str | None = None
        for line in block.splitlines():
            if line.startswith(" "):
                if current_key is None:
                    raise ValueError(f"无效的 Packages 字段续行: {line!r}")
                fields[current_key] += "\n" + line[1:]
                continue
            key, value = line.split(":", 1)
            current_key = key
            fields[key] = value.lstrip()
        name = fields.get("Package")
        filename = fields.get("Filename")
        version = fields.get("Version")
        if not name or not filename or not version:
            continue
        packages[name] = PackageInfo(
            name=name,
            version=version,
            filename=filename,
            depends=parse_depends(fields.get("Depends", "")),
        )
    return packages


def parse_depends(raw_depends: str) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    for group in raw_depends.split(","):
        group = group.strip()
        if not group:
            continue
        alternatives: list[str] = []
        for choice in group.split("|"):
            choice = re.sub(r"\s*\(.*?\)", "", choice).strip()
            if not choice:
                continue
            choice = choice.split(":", 1)[0].strip()
            if choice:
                alternatives.append(choice)
        if alternatives:
            groups.append(tuple(alternatives))
    return tuple(groups)


def resolve_packages(index: dict[str, PackageInfo], root_packages: Iterable[str]) -> list[PackageInfo]:
    ordered: list[PackageInfo] = []
    visiting: set[str] = set()
    resolved: set[str] = set()

    def visit(package_name: str) -> None:
        if package_name in resolved:
            return
        if package_name in visiting:
            raise RuntimeError(f"发现循环依赖: {package_name}")
        info = index.get(package_name)
        if info is None:
            raise KeyError(f"Packages 索引中缺少依赖: {package_name}")
        visiting.add(package_name)
        for group in info.depends:
            selected = next((candidate for candidate in group if candidate in index), None)
            if selected is None:
                raise KeyError(f"无法解析 {package_name} 的依赖候选: {' | '.join(group)}")
            visit(selected)
        visiting.remove(package_name)
        resolved.add(package_name)
        ordered.append(info)

    for package_name in root_packages:
        visit(package_name)
    return ordered


def deb_member_bytes(deb_path: Path, prefix: str) -> tuple[str, bytes]:
    with deb_path.open("rb") as handle:
        if handle.read(8) != b"!<arch>\n":
            raise ValueError(f"不是合法的 deb/ar 文件: {deb_path}")
        while True:
            header = handle.read(60)
            if not header:
                break
            if len(header) != 60:
                raise ValueError(f"deb 成员头损坏: {deb_path}")
            name = header[:16].decode("utf-8", errors="replace").strip()
            name = name.rstrip("/")
            size = int(header[48:58].decode("ascii").strip())
            payload = handle.read(size)
            if size % 2 == 1:
                handle.read(1)
            if name.startswith(prefix):
                return name, payload
    raise KeyError(f"{deb_path} 中缺少 {prefix} 成员")


def extract_deb(deb_path: Path, destination: Path) -> None:
    member_name, payload = deb_member_bytes(deb_path, "data.tar.")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="mira-termux-data-", suffix="-" + member_name, delete=False) as handle:
        handle.write(payload)
        archive_path = Path(handle.name)
    try:
        try:
            run(["tar", "--no-same-owner", "--no-same-permissions", "-xf", str(archive_path), "-C", str(destination)])
        except subprocess.CalledProcessError:
            with tarfile.open(archive_path, mode="r:*") as archive:
                safe_extract_tar(archive, destination)
    finally:
        archive_path.unlink(missing_ok=True)


def find_raw_prefix(raw_root: Path) -> Path:
    candidates = (
        raw_root / "data" / "data" / "com.termux" / "files" / "usr",
        raw_root / "." / "data" / "data" / "com.termux" / "files" / "usr",
        raw_root / "usr",
        raw_root / "." / "usr",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for candidate in raw_root.rglob("usr"):
        if candidate.is_dir() and (candidate / "bin").exists():
            return candidate
    raise FileNotFoundError(f"未找到 Termux prefix 根目录: {raw_root}")


def reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_dereferenced_tree(source_root: Path, destination_root: Path) -> None:
    seen_real_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, followlinks=True):
        dir_path = Path(dirpath)
        real_dir = os.path.realpath(dir_path)
        if real_dir in seen_real_dirs:
            dirnames[:] = []
            continue
        seen_real_dirs.add(real_dir)

        relative_dir = dir_path.relative_to(source_root)
        target_dir = destination_root / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        for name in filenames:
            source_file = dir_path / name
            target_file = target_dir / name
            if source_file.is_symlink():
                resolved = Path(os.path.realpath(source_file))
                if not resolved.exists():
                    log(f"skip broken symlink: {source_file} -> {source_file.readlink()}")
                    continue
                shutil.copy2(resolved, target_file, follow_symlinks=True)
                source_mode = resolved.stat().st_mode
            else:
                shutil.copy2(source_file, target_file, follow_symlinks=True)
                source_mode = source_file.stat().st_mode
            os.chmod(target_file, stat.S_IMODE(source_mode))


def detect_python_version(prefix_root: Path) -> str:
    candidates = sorted(
        path.name
        for path in (prefix_root / "lib").glob("python*")
        if path.is_dir() and re.fullmatch(r"python\d+\.\d+", path.name)
    )
    if not candidates:
        raise FileNotFoundError(f"在 {prefix_root / 'lib'} 下未找到 pythonX.Y 目录")
    return candidates[-1].removeprefix("python")


def site_packages_dir(prefix_root: Path, python_version: str) -> Path:
    return prefix_root / "lib" / f"python{python_version}" / "site-packages"


def validate_pure_python_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            if any(part == ".env" or (part.startswith(".env.") and part != ".env.example")
                   for part in Path(name.replace("\\", "/")).parts):
                raise ValueError(f"wheel 含禁止读取的环境配置条目: {path.name}")
        metadata_paths = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_paths) != 1:
            raise ValueError(f"wheel 缺少唯一 WHEEL 元数据: {path.name}")
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
    tags = metadata.get_all("Tag", [])
    if (not path.name.endswith("-none-any.whl")
            or metadata.get("Root-Is-Purelib", "").lower() != "true"
            or not tags
            or any(len(tag.split("-")) != 3 or tag.split("-")[1:] != ["none", "any"] for tag in tags)):
        raise ValueError(f"Android runtime 只接受纯 Python wheel: {path.name}")


def download_wheels(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    # An empty destination describes this resolution, regardless of old cache hits.
    with tempfile.TemporaryDirectory(prefix=".resolve-", dir=destination) as temporary:
        staging = Path(temporary)
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--no-deps",
                "--platform", "any",
                "--abi", "none",
                "--dest",
                str(staging),
                *PURE_PYTHON_SPECS,
            ]
        )
        wheels = sorted(staging.glob("*.whl"))
        expected = {re.split(r"[<>=!~]", spec, maxsplit=1)[0].replace("_", "-") for spec in PURE_PYTHON_SPECS}
        selected = [path.name.split("-", 1)[0].replace("_", "-") for path in wheels]
        if len(selected) != len(expected) or set(selected) != expected:
            raise RuntimeError(f"下载的 Python wheel 集合不完整: expected={sorted(expected)}, actual={selected}")
        for path in wheels:
            validate_pure_python_wheel(path)
        result = []
        for path in wheels:
            cached = destination / path.name
            path.replace(cached)
            result.append(cached.resolve())
        return result


def download_source_distribution(destination: Path, package_spec: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in destination.glob("*")}
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-binary=:all:",
            "--no-deps",
            "--dest",
            str(destination),
            package_spec,
        ]
    )
    after = sorted({path.resolve() for path in destination.glob("*")} - before)
    if not after:
        if "==" in package_spec:
            package, version = package_spec.split("==", 1)
            normalized = package.replace("-", "_")
            after = sorted(path.resolve() for path in destination.glob(f"{normalized}-{version}.*"))
        else:
            prefix = package_spec.replace("-", "_")
            after = sorted(path.resolve() for path in destination.glob(f"{prefix}-*"))
    if not after:
        raise FileNotFoundError(f"未下载到源码包: {package_spec}")
    return after[-1]


def safe_extract_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path) -> None:
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / member.name).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"tar 成员越界: {member.name}")
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return
    if member.issym():
        link_target = (target.parent / member.linkname).resolve()
        if link_target != root and root not in link_target.parents:
            raise RuntimeError(f"tar 符号链接越界: {member.name} -> {member.linkname}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        target.symlink_to(member.linkname)
        return
    if member.islnk():
        link_target = (root / member.linkname).resolve()
        if link_target != root and root not in link_target.parents:
            raise RuntimeError(f"tar 硬链接越界: {member.name} -> {member.linkname}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        target.hardlink_to(link_target)
        return
    if not member.isfile():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        return
    with source, target.open("wb") as output:
        shutil.copyfileobj(source, output)
    target.chmod(member.mode & 0o777)

def safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        safe_extract_tar_member(archive, member, destination)


def extract_tarball(archive_path: Path, destination: Path) -> Path:
    reset_directory(destination)
    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(archive, destination)
    directories = [path for path in destination.iterdir() if path.is_dir()]
    if len(directories) == 1:
        return directories[0]
    return destination


def install_wheel(wheel_path: Path, site_packages: Path) -> None:
    validate_pure_python_wheel(wheel_path)
    with zipfile.ZipFile(wheel_path) as archive:
        for entry in archive.infolist():
            name = entry.filename
            if name.endswith("/"):
                continue
            parts = Path(name).parts
            if not parts:
                continue
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    continue
                relative_target = Path(*parts[2:])
            else:
                relative_target = Path(*parts)
            target_path = site_packages / relative_target
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)


def install_pure_python_dependencies(site_packages: Path) -> list[str]:
    wheel_paths = download_wheels(WHEEL_CACHE_DIR)
    installed: list[str] = []
    for wheel_path in wheel_paths:
        install_wheel(wheel_path, site_packages)
        installed.append(wheel_path.name)
    return installed


def copytree_replace(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def install_vendored_frida_tools(site_packages: Path) -> None:
    if not FRIDA_TOOLS_DIR.is_dir():
        raise FileNotFoundError(f"缺少 vendored frida-tools: {FRIDA_TOOLS_DIR}")
    copytree_replace(FRIDA_TOOLS_DIR / "frida_tools", site_packages / "frida_tools")
    copytree_replace(FRIDA_TOOLS_DIR / "frida_tools.egg-info", site_packages / "frida_tools.egg-info")


def ensure_frida_devkit() -> Path:
    expected = FRIDA_DEVKIT_SHA256.get(FRIDA_VERSION)
    if expected is None:
        raise RuntimeError(f"Frida {FRIDA_VERSION} 没有固定 devkit SHA-256")
    FRIDA_DEVKIT_DIR.mkdir(parents=True, exist_ok=True)
    if not (FRIDA_DEVKIT_DIR / "frida-core.h").exists() or not (FRIDA_DEVKIT_DIR / "libfrida-core.a").exists():
        fetch(FRIDA_DEVKIT_URL, FRIDA_DEVKIT_TAR)
        verify_sha256(FRIDA_DEVKIT_TAR, expected, "Frida devkit")
        run(["tar", "-xJf", str(FRIDA_DEVKIT_TAR), "-C", str(FRIDA_DEVKIT_DIR)])
    elif FRIDA_DEVKIT_TAR.exists():
        verify_sha256(FRIDA_DEVKIT_TAR, expected, "Frida devkit cache")
    return FRIDA_DEVKIT_DIR


def build_frida_extension(prefix_root: Path, python_version: str, source_root: Path, output_path: Path) -> None:
    devkit_dir = ensure_frida_devkit()
    build_root = BUILD_ROOT / "frida-python" / TARGET_ABI
    build_root.mkdir(parents=True, exist_ok=True)
    version_script = build_root / "_frida.version"
    version_script.write_text(
        "\n".join(
            [
                "{",
                "  global:",
                "    PyInit__frida;",
                "",
                "  local:",
                "    *;",
                "};",
                "",
            ]
        ),
        encoding="utf-8",
    )
    clang = TOOLCHAIN_DIR / f"aarch64-linux-android{ANDROID_API}-clang"
    strip = TOOLCHAIN_DIR / "llvm-strip"
    if not clang.exists():
        raise FileNotFoundError(f"缺少 Android clang: {clang}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    extension_source = source_root / "src" / "_frida.c"
    if not extension_source.exists():
        extension_source = source_root / "frida" / "_frida" / "extension.c"
    if not extension_source.exists():
        raise FileNotFoundError(f"缺少 Frida Python 扩展源码: {source_root}")
    run(
        [
            str(clang),
            "-shared",
            "-fPIC",
            "-DANDROID",
            "-ffunction-sections",
            "-fdata-sections",
            f"-I{devkit_dir}",
            f"-I{prefix_root / 'include' / f'python{python_version}'}",
            str(extension_source),
            "-o",
            str(output_path),
            f"-L{devkit_dir}",
            "-lfrida-core",
            f"-L{prefix_root / 'lib'}",
            f"-lpython{python_version}",
            "-llog",
            "-ldl",
            "-lm",
            "-pthread",
            "-Wl,--gc-sections",
            f"-Wl,--version-script,{version_script}",
        ]
    )
    if strip.exists():
        run([str(strip), str(output_path)])


def install_frida_python_binding(prefix_root: Path, python_version: str, site_packages: Path) -> str:
    sdist_path = download_source_distribution(SDIST_CACHE_DIR, f"frida=={FRIDA_VERSION}")
    expected = FRIDA_SDIST_SHA256.get(FRIDA_VERSION)
    if expected is None:
        raise RuntimeError(f"Frida {FRIDA_VERSION} 没有固定 sdist SHA-256")
    verify_sha256(sdist_path, expected, "Frida Python sdist")
    source_root = extract_tarball(sdist_path, BUILD_ROOT / "frida-python-src")
    copytree_replace(source_root / "frida", site_packages / "frida")
    legacy_stubs = source_root / "_frida"
    if legacy_stubs.is_dir():
        copytree_replace(legacy_stubs, site_packages / "_frida")
        extension_path = site_packages / "_frida.abi3.so"
    else:
        # Frida >= 16.5 imports the extension relatively from the package.
        extension_path = site_packages / "frida" / "_frida.abi3.so"
    build_frida_extension(prefix_root, python_version, source_root, extension_path)
    if not legacy_stubs.is_dir():
        (site_packages / "_frida.py").write_text(
            '"""Compatibility import for the bundled frida-tools CLI."""\n'
            "import sys\n"
            "from frida import _frida\n"
            "sys.modules[__name__] = _frida\n",
            encoding="utf-8",
        )
    return sdist_path.name


def write_text_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def python_launcher_script(binary_name: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/system/bin/sh
        SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
        PREFIX_DIR="$(dirname -- "$SELF_DIR")"
        export LD_LIBRARY_PATH="$PREFIX_DIR/lib${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
        exec /system/bin/linker64 "$SELF_DIR/{binary_name}" "$@"
        """
    )


def ensure_python_entrypoints(prefix_root: Path, python_version: str) -> None:
    bin_dir = prefix_root / "bin"
    versioned_python = bin_dir / f"python{python_version}"
    if not versioned_python.exists():
        raise FileNotFoundError(f"缺少 Python 可执行文件: {versioned_python}")

    runtime_binary = bin_dir / f"python{python_version}.bin"
    if not runtime_binary.exists():
        versioned_python.replace(runtime_binary)
    runtime_binary.chmod(0o755)

    launcher = python_launcher_script(runtime_binary.name)
    for name in (f"python{python_version}", "python3", "python"):
        target = bin_dir / name
        if target.exists():
            target.unlink()
        write_text_executable(target, launcher)

    pip_wrapper = textwrap.dedent(
        """\
        #!/system/bin/sh
        SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
        PYTHON_BIN="$SELF_DIR/python3"
        exec "$PYTHON_BIN" -m pip "$@"
        """
    )
    for name in ("pip", "pip3", f"pip{python_version}"):
        write_text_executable(bin_dir / name, pip_wrapper)


def ensure_frida_entrypoint(prefix_root: Path) -> None:
    bin_dir = prefix_root / "bin"
    frida_wrapper = textwrap.dedent(
        """\
        #!/system/bin/sh
        SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
        PYTHON_BIN="$SELF_DIR/python3"
        exec "$PYTHON_BIN" -c 'import frida, runpy, sys; sys.modules["_frida"] = frida._frida; runpy.run_module("frida_tools.repl", run_name="__main__", alter_sys=True)' "$@"
        """
    )
    write_text_executable(bin_dir / "frida-official", frida_wrapper)


def write_source_manifest(
    asset_root: Path,
    packages: list[PackageInfo],
    wheels: list[str],
    python_version: str,
    frida_sdist_name: str,
) -> None:
    lines = [
        f"termux-repos: {', '.join(TERMUX_REPOS)}",
        f"target-abi: {TARGET_ABI}",
        f"python-version: {python_version}",
        f"frida-version: {FRIDA_VERSION}",
        f"relocated-prefix: {TERMUX_PREFIX_PATH} -> {MIRA_PREFIX_PATH}",
        "packages:",
    ]
    for package in packages:
        lines.append(f"  - {package.name} {package.version} {package.filename}")
    lines.append("python-wheels:")
    for wheel in wheels:
        lines.append(f"  - {wheel}")
    lines.append("vendored-frida-tools: third_party/frida-tools")
    lines.append(f"frida-sdist: {frida_sdist_name}")
    lines.append(f"frida-devkit: {FRIDA_DEVKIT_URL}")
    (asset_root / "SOURCE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_layout(prefix_root: Path, python_version: str) -> None:
    site_packages = site_packages_dir(prefix_root, python_version)
    required_paths = (
        prefix_root / "bin" / "python3",
        prefix_root / "bin" / "pip",
        prefix_root / "bin" / "frida-official",
        site_packages / "frida" / "__init__.py",
        site_packages / "frida_tools" / "__init__.py",
        site_packages / "pip" / "__init__.py",
    )
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(f"构建结果缺少必需文件: {path}")
    extension_candidates = (site_packages / "_frida.abi3.so", site_packages / "frida" / "_frida.abi3.so")
    if not any(path.is_file() and path.stat().st_size > 0 for path in extension_candidates):
        raise FileNotFoundError(f"构建结果缺少非空 Frida 扩展: {extension_candidates}")
    if (site_packages / "frida" / "_frida.abi3.so").is_file():
        alias = site_packages / "_frida.py"
        if not alias.is_file() or alias.stat().st_size == 0:
            raise FileNotFoundError(f"构建结果缺少 Frida CLI 兼容模块: {alias}")

    import_program = textwrap.dedent(f"""\
        import importlib
        from pathlib import Path
        import sys
        target = Path({str(site_packages.resolve())!r})
        sys.path.insert(0, str(target))
        for name in {EXPECTED_IMPORTS!r}:
            module = importlib.import_module(name)
            origin = getattr(module, "__file__", None)
            if origin is None or target not in Path(origin).resolve().parents:
                raise ImportError(f"{{name}} resolved outside packaged runtime: {{origin}}")
        """)
    run([sys.executable, "-I", "-S", "-B", "-c", import_program])


def relocate_text_prefixes(prefix_root: Path) -> int:
    old_bytes = TERMUX_PREFIX_PATH.encode("utf-8")
    replaced = 0

    for path in prefix_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if old_bytes not in content or b"\x00" in content:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        updated = text.replace(TERMUX_PREFIX_PATH, MIRA_PREFIX_PATH)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8")
        replaced += 1

    return replaced


def build(out_root: Path) -> Path:
    asset_root = out_root / "bootstrap" / "prefix" / TARGET_ABI
    work_root = BUILD_ROOT / TARGET_ABI
    raw_root = work_root / "raw"

    log(f"output root: {asset_root}")
    package_index = parse_package_index(read_package_index())
    packages = resolve_packages(package_index, ROOT_PACKAGES)
    log("resolved packages: " + ", ".join(package.name for package in packages))

    reset_directory(raw_root)
    reset_directory(asset_root)

    for package in packages:
        deb_name = Path(package.filename).name
        deb_path = DEB_CACHE_DIR / deb_name
        fetch(tuple(f"{repo}/{package.filename}" for repo in TERMUX_REPOS), deb_path)
        extract_deb(deb_path, raw_root)

    prefix_source = find_raw_prefix(raw_root)
    copy_dereferenced_tree(prefix_source, asset_root)

    python_version = detect_python_version(asset_root)
    site_packages = site_packages_dir(asset_root, python_version)
    site_packages.mkdir(parents=True, exist_ok=True)

    wheels = install_pure_python_dependencies(site_packages)
    install_vendored_frida_tools(site_packages)
    frida_sdist_name = install_frida_python_binding(asset_root, python_version, site_packages)
    ensure_python_entrypoints(asset_root, python_version)
    ensure_frida_entrypoint(asset_root)
    agent_source = ROOT_DIR / "mira" / "mcp" / "frida_agent.py"
    agent_target = asset_root / "bin" / "mira-frida-agent.py"
    shutil.copy2(agent_source, agent_target)
    agent_target.chmod(agent_target.stat().st_mode | stat.S_IXUSR)
    relocated_files = relocate_text_prefixes(asset_root)
    log(f"relocated text files: {relocated_files}")
    write_source_manifest(asset_root, packages, wheels, python_version, frida_sdist_name)
    validate_layout(asset_root, python_version)

    return asset_root


def main(argv: list[str]) -> int:
    out_root = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_OUT_ROOT.resolve()
    built_path = build(out_root)
    log(f"done: {built_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
