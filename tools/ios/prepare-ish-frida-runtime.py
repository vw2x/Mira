#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from email.parser import Parser
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

FRIDA_VERSION = "16.0.7"
FRIDA_TOOLS_VERSION = "12.1.0"
RUNTIME_VERSION = "official-frida-tools-12.1.0-frida-16.0.7-v13"
ALPINE_VERSION = "v3.19"
ALPINE_ARCH = "x86"
ALPINE_REPOS = (
    f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_VERSION}/main/{ALPINE_ARCH}",
    f"https://dl-cdn.alpinelinux.org/alpine/{ALPINE_VERSION}/community/{ALPINE_ARCH}",
)
ROOT_PACKAGES = (
    "python3",
    "python3-dev",
    "musl-dev",
    "linux-headers",
)
PURE_PYTHON_SPECS = (
    "colorama<1.0.0,>=0.2.7",
    "prompt-toolkit<4.0.0,>=2.0.0",
    "pygments<3.0.0,>=2.0.2",
    "wcwidth",
)
PURE_PYTHON_MODULES = ("colorama", "prompt_toolkit", "pygments", "wcwidth")

SCRIPT_PATH = Path(__file__).resolve()
ROOT_DIR = SCRIPT_PATH.parents[2]
FRIDA_TOOLS_DIR = ROOT_DIR / "third_party" / "frida-tools"
FRIDA_SDIST_CANDIDATES = (
    ROOT_DIR / "build" / "termux-prefix" / "sdists" / f"frida-{FRIDA_VERSION}.tar.gz",
)
FRIDA_CC_CANDIDATES = (
    ROOT_DIR / "build" / f"frida-src-{FRIDA_VERSION}" / "build" / "musl-wrap" / "i686-linux-musl-gcc",
)
FRIDA_STRIP_CANDIDATES = (
    ROOT_DIR / "build" / f"frida-src-{FRIDA_VERSION}" / "build" / "toolchain-macos-arm64" / "bin" / "llvm-strip",
    ROOT_DIR / "build" / f"frida-src-{FRIDA_VERSION}" / "build" / "musl-wrap" / "i686-linux-musl-strip",
)
FRIDA_SDK_LIB_CANDIDATES = (
    ROOT_DIR / "build" / f"frida-src-{FRIDA_VERSION}" / "build" / "sdk-linux-x86" / "lib",
)
FRIDA_MUSL_DEVKIT_ENV = "MIRA_IOS_FRIDA_DEVKIT_DIR"
FRIDA_CC_ENV = "MIRA_IOS_FRIDA_CC"
FRIDA_STRIP_ENV = "MIRA_IOS_FRIDA_STRIP"


@dataclass(frozen=True)
class AlpinePackage:
    name: str
    version: str
    repo_url: str
    depends: tuple[str, ...]
    provides: frozenset[str]

    @property
    def filename(self) -> str:
        return f"{self.name}-{self.version}.apk"

    @property
    def url(self) -> str:
        return f"{self.repo_url}/{self.filename}"


def log(message: str) -> None:
    print(f"[mira-ios-frida] {message}", flush=True)


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0:
        return
    tmp = target.with_suffix(target.suffix + ".tmp")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as sink:
        shutil.copyfileobj(response, sink)
    tmp.replace(target)


def run(cmd: list[str]) -> None:
    log("run: " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def read_apk_index(repo_url: str, cache_dir: Path) -> str:
    archive_path = cache_dir / f"{repo_url.rstrip('/').split('/')[-2]}-{repo_url.rstrip('/').split('/')[-1]}-APKINDEX.tar.gz"
    download(f"{repo_url}/APKINDEX.tar.gz", archive_path)
    with tarfile.open(archive_path, "r:gz") as archive:
        return archive.extractfile("APKINDEX").read().decode("utf-8")


def normalize_dep(token: str) -> str:
    token = token.strip()
    if not token or token.startswith("!"):
        return ""
    token = re.split(r"(?<!:)[<>=~]", token, maxsplit=1)[0]
    return token.strip()


def parse_depends(raw_depends: str) -> tuple[str, ...]:
    tokens = []
    for raw in raw_depends.split():
        normalized = normalize_dep(raw)
        if normalized:
            tokens.append(normalized)
    return tuple(tokens)


def parse_provides(raw_provides: str) -> frozenset[str]:
    provided: set[str] = set()
    for raw in raw_provides.split():
        name = raw.split("=", 1)[0].strip()
        if name:
            provided.add(name)
    return frozenset(provided)


def parse_package_index(raw_text: str, repo_url: str) -> dict[str, AlpinePackage]:
    packages: dict[str, AlpinePackage] = {}
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.strip()
        name = fields.get("P")
        version = fields.get("V")
        arch = fields.get("A")
        if not name or not version or not arch or arch != ALPINE_ARCH:
            continue
        packages[name] = AlpinePackage(
            name=name,
            version=version,
            repo_url=repo_url,
            depends=parse_depends(fields.get("D", "")),
            provides=parse_provides(fields.get("p", "")),
        )
    return packages


def build_provider_index(packages: dict[str, AlpinePackage]) -> dict[str, list[str]]:
    providers: dict[str, list[str]] = {}
    for package in packages.values():
        for capability in {package.name, *package.provides}:
            providers.setdefault(capability, []).append(package.name)
    return providers


def choose_provider(capability: str, packages: dict[str, AlpinePackage], providers: dict[str, list[str]]) -> str | None:
    if capability in packages:
        return capability
    candidates = providers.get(capability, [])
    if not candidates:
        return None
    return sorted(candidates)[0]


def resolve_packages(packages: dict[str, AlpinePackage], root_packages: tuple[str, ...]) -> list[AlpinePackage]:
    providers = build_provider_index(packages)
    ordered: list[AlpinePackage] = []
    resolved: set[str] = set()
    visiting: set[str] = set()

    def visit(capability: str, from_package: str | None = None) -> None:
        provider = choose_provider(capability, packages, providers)
        if provider is None:
            if capability.startswith(("so:", "cmd:", "pc:")):
                log(f"skip unresolved capability provided by base rootfs: {capability}")
                return
            raise KeyError(f"Packages 索引中缺少依赖: {capability}")
        if from_package is not None and provider == from_package:
            return
        if provider in resolved:
            return
        if provider in visiting:
            raise RuntimeError(f"发现循环依赖: {provider}")
        visiting.add(provider)
        package = packages[provider]
        for dependency in package.depends:
            visit(dependency, from_package=provider)
        visiting.remove(provider)
        resolved.add(provider)
        ordered.append(package)

    for package_name in root_packages:
        visit(package_name)
    return ordered


def safe_extract_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, destination: Path) -> None:
    # Validate archive paths lexically so a symlink loop inside the archive
    # cannot make Path.resolve() fail before the member is extracted.
    root = destination.absolute()
    root.mkdir(parents=True, exist_ok=True)
    target = Path(os.path.normpath(root / member.name))
    if target != root and root not in target.parents:
        raise RuntimeError(f"tar 成员越界: {member.name}")
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return
    if member.issym():
        link_target = Path(os.path.normpath(target.parent / member.linkname))
        if link_target != root and root not in link_target.parents:
            raise RuntimeError(f"tar 符号链接越界: {member.name} -> {member.linkname}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        target.symlink_to(member.linkname)
        return
    if member.islnk():
        link_target = Path(os.path.normpath(root / member.linkname))
        if link_target != root and root not in link_target.parents:
            raise RuntimeError(f"tar 硬链接越界: {member.name} -> {member.linkname}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        os.link(link_target, target)
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

def extract_apk(apk_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(apk_path, "r:gz") as archive:
        for member in archive.getmembers():
            if Path(member.name).name.startswith("."):
                continue
            safe_extract_tar_member(archive, member, destination)


def install_apk_packages(data_root: Path, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    merged_packages: dict[str, AlpinePackage] = {}
    for repo_url in ALPINE_REPOS:
        merged_packages.update(parse_package_index(read_apk_index(repo_url, cache_dir), repo_url))
    packages = resolve_packages(merged_packages, ROOT_PACKAGES)
    for package in packages:
        apk_path = cache_dir / package.filename
        download(package.url, apk_path)
        extract_apk(apk_path, data_root)
    return detect_python_version(data_root)


def detect_python_version(data_root: Path) -> str:
    candidates = sorted(
        path.name
        for path in (data_root / "usr" / "lib").glob("python*")
        if path.is_dir() and re.fullmatch(r"python\d+\.\d+", path.name)
    )
    if not candidates:
        raise FileNotFoundError(f"在 {data_root / 'usr' / 'lib'} 下未找到 pythonX.Y 目录")
    return candidates[-1].removeprefix("python")


def site_packages_dir(data_root: Path) -> Path:
    return data_root / "opt" / "mira" / "frida-python" / "site-packages"


def download_wheels(destination: Path, specs: tuple[str, ...], extra_args: list[str] | None = None) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-deps",
        "--platform", "any",
        "--abi", "none",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(specs)
    # An empty destination contains exactly this resolution, including pip cache hits.
    with tempfile.TemporaryDirectory(prefix="wheel-download-", dir=destination) as staging:
        run([*cmd, "--dest", staging])
        wheels = sorted(Path(staging).glob("*.whl"))
        if not wheels:
            raise FileNotFoundError(f"未下载到纯 Python wheel: {destination}")
        for wheel in wheels:
            validate_pure_python_wheel(wheel)
        selected = []
        for wheel in wheels:
            target = destination / wheel.name
            shutil.copy2(wheel, target)
            selected.append(target.resolve())
        return selected


def checked_wheel_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    for entry in entries:
        for part in entry.filename.replace("\\", "/").split("/"):
            if part != ".env.example" and (part == ".env" or part.startswith(".env.")):
                raise ValueError(f"wheel 包含受保护的环境文件路径: {entry.filename}")
    return entries


def validate_pure_python_wheel(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_paths = [entry.filename for entry in checked_wheel_entries(archive) if entry.filename.endswith(".dist-info/WHEEL")]
        if len(metadata_paths) != 1:
            raise ValueError(f"wheel 元数据不完整: {wheel_path.name}")
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
    tags = metadata.get_all("Tag", [])
    if metadata.get("Root-Is-Purelib", "").lower() != "true" or not tags or any(
        len(tag.split("-")) != 3 or tag.split("-")[-2:] != ["none", "any"] for tag in tags
    ):
        raise ValueError(f"仅允许纯 Python any wheel: {wheel_path.name}")


def install_wheel(wheel_path: Path, site_packages: Path) -> None:
    with zipfile.ZipFile(wheel_path) as archive:
        for entry in checked_wheel_entries(archive):
            if entry.filename.endswith("/"):
                continue
            parts = Path(entry.filename).parts
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


def install_pure_python_dependencies(site_packages: Path, wheel_cache_dir: Path) -> list[str]:
    wheel_paths = download_wheels(wheel_cache_dir, PURE_PYTHON_SPECS)
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


def download_source_distribution(destination: Path, spec: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in destination.glob("*")}
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-binary=:all:",
        "--no-deps",
        "--dest",
        str(destination),
        spec,
    ]
    run(cmd)
    after = sorted({path.resolve() for path in destination.glob("*")} - before)
    if not after:
        after = sorted(path.resolve() for path in destination.glob("*"))
    tarballs = [path for path in after if path.suffixes[-2:] == [".tar", ".gz"]]
    if not tarballs:
        raise FileNotFoundError(f"未下载到 frida sdist: {destination}")
    return tarballs[0]


def safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        safe_extract_tar_member(archive, member, destination)


def extract_tarball(archive_path: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        safe_extract_tar(archive, destination)
    children = [path for path in destination.iterdir() if path.is_dir()]
    if len(children) == 1:
        return children[0]
    raise RuntimeError(f"无法识别 tarball 根目录: {archive_path}")


def ensure_frida_sdist(cache_dir: Path) -> Path:
    for candidate in FRIDA_SDIST_CANDIDATES:
        if candidate.is_file():
            target = cache_dir / candidate.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
            return target
    return download_source_distribution(cache_dir, f"frida=={FRIDA_VERSION}")


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def resolve_host_tool(env_name: str, fallback_names: tuple[str, ...]) -> list[str]:
    override = os.environ.get(env_name, "").strip()
    if override:
        return override.split()
    local_candidates: tuple[Path, ...] = ()
    if env_name == FRIDA_CC_ENV:
        local_candidates = FRIDA_CC_CANDIDATES
    elif env_name == FRIDA_STRIP_ENV:
        local_candidates = FRIDA_STRIP_CANDIDATES
    for candidate in local_candidates:
        if candidate.is_file():
            return [str(candidate)]
    for name in fallback_names:
        path = shutil.which(name)
        if path:
            return [path]
    raise FileNotFoundError(f"缺少可用工具: {env_name} 或 {', '.join(fallback_names)}")


def ensure_frida_musl_devkit(cache_dir: Path) -> Path:
    override = os.environ.get(FRIDA_MUSL_DEVKIT_ENV, "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser().resolve())
    candidates.extend(
        (
            cache_dir / "frida-core-devkit-musl-x86",
            ROOT_DIR / "build" / "frida" / "devkit" / FRIDA_VERSION / "linux-x86-musl",
        )
    )
    for candidate in candidates:
        if (candidate / "frida-core.h").is_file() and (candidate / "libfrida-core.a").is_file():
            return candidate
    raise FileNotFoundError(
        "缺少 musl 原生 frida-core devkit. 需要先准备 frida-core.h 和 libfrida-core.a, "
        f"并通过 {FRIDA_MUSL_DEVKIT_ENV} 指向该目录"
    )


def ensure_frida_sdk_libdir() -> Path:
    for candidate in FRIDA_SDK_LIB_CANDIDATES:
        if (candidate / "libucontext.so").is_file() or (candidate / "libucontext.a").is_file():
            return candidate
    raise FileNotFoundError("缺少 linux-x86 musl SDK lib 目录, 无法链接 libucontext")


def build_frida_extension(data_root: Path, python_version: str, source_root: Path, output_path: Path, cache_dir: Path) -> tuple[str, str]:
    devkit_dir = ensure_frida_musl_devkit(cache_dir)
    sdk_lib_dir = ensure_frida_sdk_libdir()
    cc = resolve_host_tool(FRIDA_CC_ENV, ("i686-linux-musl-gcc",))
    strip = None
    try:
        strip = resolve_host_tool(FRIDA_STRIP_ENV, ("i686-linux-musl-strip",))
    except FileNotFoundError:
        strip = None

    build_root = cache_dir / "frida-extension-build"
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
    compat_source = build_root / "frida_linux_musl_compat.c"
    compat_source.write_text(
        "\n".join(
            [
                "#define _GNU_SOURCE",
                "#include <errno.h>",
                "#include <linux/stat.h>",
                "#include <sys/syscall.h>",
                "#include <unistd.h>",
                "",
                "int statx(int dirfd, const char *pathname, int flags, unsigned int mask, struct statx *statbuf) {",
                "#ifdef SYS_statx",
                "    return syscall(SYS_statx, dirfd, pathname, flags, mask, statbuf);",
                "#else",
                "    errno = ENOSYS;",
                "    return -1;",
                "#endif",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    python_lib_name = f"python{python_version}"
    compile_cmd = [
        *cc,
        "-shared",
        "-fPIC",
        "-ffunction-sections",
        "-fdata-sections",
        f"-I{devkit_dir}",
        f"-I{data_root / 'usr' / 'include'}",
        f"-I{data_root / 'usr' / 'include' / f'python{python_version}'}",
        str(source_root / "src" / "_frida.c"),
        str(compat_source),
        "-o",
        str(output_path),
        f"-L{devkit_dir}",
        "-lfrida-core",
        f"-L{sdk_lib_dir}",
        f"-L{data_root / 'usr' / 'lib'}",
        f"-l{python_lib_name}",
        "-ldl",
        "-lm",
        "-pthread",
        "-lresolv",
        "-lucontext",
        "-Wl,--gc-sections",
        f"-Wl,--version-script,{version_script}",
        "-Wl,-rpath,/usr/lib",
    ]
    run(compile_cmd)
    if strip is not None:
        run([*strip, str(output_path)])
    return (source_root.name, str(devkit_dir))


def install_frida_python_binding(data_root: Path, python_version: str, site_packages: Path, cache_dir: Path) -> tuple[str, str]:
    sdist_path = ensure_frida_sdist(cache_dir)
    source_root = extract_tarball(sdist_path, cache_dir / "frida-python-src")
    copytree_replace(source_root / "frida", site_packages / "frida")
    copytree_replace(source_root / "_frida", site_packages / "_frida")
    _, devkit_dir = build_frida_extension(data_root, python_version, source_root, site_packages / "_frida.abi3.so", cache_dir)
    return (sdist_path.name, devkit_dir)


def write_runtime_scripts(data_root: Path) -> None:
    state_dir = data_root / "opt" / "mira" / "frida-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / RUNTIME_VERSION).write_text("ready\n", encoding="utf-8")

    frida_setup = f"""#!/bin/sh
set -eu

STAMP="/opt/mira/frida-state/{RUNTIME_VERSION}"

if [ ! -f "$STAMP" ]; then
    echo "frida-setup: bundled runtime is incomplete" >&2
    exit 1
fi

exit 0
"""

    frida_wrapper = """#!/bin/sh
set -eu

/usr/bin/frida-setup

export PYTHONUNBUFFERED=1
export PYTHONPATH="/opt/mira/frida-python/site-packages${PYTHONPATH:+:$PYTHONPATH}"

for mira_arg in "$@"; do
    case "$mira_arg" in
        --version)
            exec /usr/bin/python3 -c "import _frida; print(_frida.__version__)"
            ;;
    esac
done

if [ "${1:-}" = "--status" ]; then
    shift
    exec /usr/bin/python3 -c "import frida, json; dev=frida.get_device_manager().add_remote_device('127.0.0.1:27042'); ps=dev.enumerate_processes(); first=ps[0] if ps else None; print(json.dumps({'frida': frida.__version__, 'connected': True, 'processCount': len(ps), 'pid': getattr(first, 'pid', None), 'target': getattr(first, 'name', None)}, separators=(',', ':')))"
fi

mira_needs_default_target=1
for mira_arg in "$@"; do
    case "$mira_arg" in
        -h|--help|--version|-D|--device|-U|--usb|-R|--remote|-H|--host|-f|--file|-F|--attach-frontmost|-n|--attach-name|-N|--attach-identifier|-p|--attach-pid|-W|--await)
            mira_needs_default_target=0
            ;;
    esac
done

if [ "$mira_needs_default_target" = "1" ]; then
    exec /usr/bin/python3 -m frida_tools.repl -H 127.0.0.1 -n Gadget "$@"
fi

exec /usr/bin/python3 -m frida_tools.repl "$@"
"""

    write_executable(data_root / "usr" / "bin" / "frida-setup", frida_setup)
    write_executable(data_root / "usr" / "bin" / "frida", frida_wrapper)


def write_source_manifest(
    data_root: Path,
    python_version: str,
    pure_wheels: list[str],
    frida_sdist_name: str,
    frida_devkit_dir: str,
) -> None:
    manifest_path = data_root / "opt" / "mira" / "frida-python" / "SOURCE.txt"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"alpine-version: {ALPINE_VERSION}",
        f"alpine-arch: {ALPINE_ARCH}",
        f"python-version: {python_version}",
        f"frida-version: {FRIDA_VERSION}",
        f"frida-tools-version: {FRIDA_TOOLS_VERSION}",
        f"frida-runtime: {RUNTIME_VERSION}",
        f"frida-sdist: {frida_sdist_name}",
        f"frida-devkit: {frida_devkit_dir}",
        "pure-python-wheels:",
    ]
    lines.extend(f"  - {wheel}" for wheel in pure_wheels)
    lines.append(f"vendored-frida-tools: {FRIDA_TOOLS_DIR}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_layout(data_root: Path) -> None:
    required_paths = (
        data_root / "usr" / "bin" / "python3",
        data_root / "usr" / "bin" / "frida",
        data_root / "usr" / "bin" / "frida-setup",
        data_root / "opt" / "mira" / "frida-python" / "site-packages" / "frida" / "__init__.py",
        data_root / "opt" / "mira" / "frida-python" / "site-packages" / "frida_tools" / "__init__.py",
        data_root / "opt" / "mira" / "frida-python" / "site-packages" / "_frida.abi3.so",
        data_root / "opt" / "mira" / "frida-state" / RUNTIME_VERSION,
        data_root / "lib" / "ld-musl-i386.so.1",
        data_root / "lib" / "libc.musl-x86.so.1",
        data_root / "usr" / "include" / f"python{detect_python_version(data_root)}" / "Python.h",
    )
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(f"构建结果缺少必需文件: {path}")
    # Ignore host site-packages and PYTHONPATH so they cannot fill target omissions.
    validation_script = "\n".join([
        "import importlib, sys",
        "from pathlib import Path",
        "target = Path(sys.argv[1]).resolve()",
        "sys.path.insert(0, str(target))",
        "for name in sys.argv[2:]:",
        "    module = importlib.import_module(name)",
        "    if target not in Path(module.__file__).resolve().parents:",
        "        raise ImportError(f'{name} was imported outside the target: {module.__file__}')",
    ]) + "\n"
    run([
        sys.executable, "-I", "-S", "-B", "-c",
        validation_script,
        str(site_packages_dir(data_root)), *PURE_PYTHON_MODULES,
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rootfs", required=True, help="Path to MiraISHRoot.fakefs directory")
    parser.add_argument("--cache-dir", required=True, help="Cache directory for downloaded artifacts")
    parser.add_argument("--stamp-path", help="Optional explicit path for the rootfs stamp file")
    args = parser.parse_args()

    rootfs = Path(args.rootfs)
    data_root = rootfs / "data" if (rootfs / "data").is_dir() else rootfs
    if not data_root.is_dir():
        raise SystemExit(f"rootfs directory not found: {data_root}")
    stamp_path = Path(args.stamp_path) if args.stamp_path else rootfs / ".mira-ish-rootfs.stamp"

    cache_dir = Path(args.cache_dir)
    python_version = install_apk_packages(data_root, cache_dir / "apk")
    site_packages = site_packages_dir(data_root)
    site_packages.mkdir(parents=True, exist_ok=True)
    pure_wheels = install_pure_python_dependencies(site_packages, cache_dir / "wheels")
    install_vendored_frida_tools(site_packages)
    frida_sdist_name, frida_devkit_dir = install_frida_python_binding(data_root, python_version, site_packages, cache_dir / "frida-python")
    write_runtime_scripts(data_root)
    write_source_manifest(data_root, python_version, pure_wheels, frida_sdist_name, frida_devkit_dir)
    validate_layout(data_root)

    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(
        "\n".join(
            [
                "rootfs=appstore-apk",
                f"frida_runtime={RUNTIME_VERSION}",
                f"frida_version={FRIDA_VERSION}",
                f"frida_tools_version={FRIDA_TOOLS_VERSION}",
                f"python_version={python_version}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
