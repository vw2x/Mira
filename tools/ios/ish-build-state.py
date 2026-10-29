#!/usr/bin/env python3
"""Track iSH library inputs separately from the bundled Linux filesystem."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


SCHEMA = 1
LIBRARIES = ("libish.a", "libish_emu.a", "libfakefs.a")
DEFAULT_ROOTFS_URL = "https://github.com/ish-app/roots/releases/download/g00712ff0a54b2839c5aa1a8ed758003ca65357dc/appstore-apk.tar.gz"
# The archive already used by Mira, frozen independently of future iSH releases.
DEFAULT_ROOTFS_SHA256 = "776b16416894e5a8bec220b8d4726c46bb993289e8e48fdac54a519396e40a93"


def sensitive_path(path: Path) -> bool:
    return any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in path.parts)


def file_sha256(path: Path) -> str:
    if sensitive_path(path) or sensitive_path(path.resolve()) or path.is_symlink():
        raise ValueError(f"Refusing to hash sensitive or symlinked input: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path) -> dict:
    if sensitive_path(path) or sensitive_path(path.resolve()) or path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def source_identity(directory: Path) -> dict:
    """Hash tracked build sources, including local patches, without opening env files."""
    revision = command("git", "-C", str(directory), "rev-parse", "HEAD")
    names = subprocess.check_output(["git", "-C", str(directory), "ls-files", "-z"]).split(b"\0")
    files = {}
    for name in names:
        if not name:
            continue
        relative = Path(os.fsdecode(name))
        path = directory / relative
        if sensitive_path(relative) or sensitive_path(path.resolve()):
            continue
        if path.is_symlink():
            files[str(relative)] = {"symlink": os.readlink(path)}
        elif path.is_file():
            files[str(relative)] = {"sha256": file_sha256(path), "executable": bool(path.stat().st_mode & 0o111)}
        elif not path.exists():
            files[str(relative)] = {"missing": True}
        # Gitlinks are fingerprinted separately below, not traversed here.
    return {"revision": revision, "tracked_sources_sha256": digest_json(files)}


def tool_identity(name: str, *version_args: str) -> dict:
    executable = shutil.which(name)
    if not executable:
        return {"available": False}
    return {"path": executable, "version": command(executable, *version_args)}


def library_inputs(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    ish = root / "third_party/ish"
    sdk_path = command("xcrun", "--sdk", args.sdk, "--show-sdk-path")
    scripts = ("build-ish-libs.sh", "ish-build-state.py", "prepare-ish-source.py", "patches/ish-mira.patch")
    flags = {name: os.environ.get(name, "") for name in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "CC", "CXX", "IPHONEOS_DEPLOYMENT_TARGET", "MACOSX_DEPLOYMENT_TARGET")}
    flags["LDFLAGS"] = f"-L{sdk_path}/usr/lib {args.extra_ldflags}"
    return {
        "schema": SCHEMA,
        "ish": source_identity(ish),
        "submodules": {name: source_identity(ish / name) for name in ("deps/libarchive", "deps/libapps")},
        "scripts": {name: file_sha256(root / "tools/ios" / name) for name in scripts},
        "sdk": {"name": args.sdk, "path": sdk_path, "version": command("xcrun", "--sdk", args.sdk, "--show-sdk-version"), "build": command("xcrun", "--sdk", args.sdk, "--show-sdk-build-version")},
        "xcode": command("xcodebuild", "-version"),
        "tools": {"meson": tool_identity("meson", "--version"), "ninja": tool_identity("ninja", "--version"), "clang": tool_identity("/opt/homebrew/opt/llvm/bin/clang", "--version"), "ld.lld": tool_identity("ld.lld", "--version")},
        "build": {"directory": str(args.build_dir.resolve()), "configuration": args.configuration, "archs": args.archs.split(), "targets": ["libish", "libish_emu", "libfakefs"], "only_active_arch": True, "compiler_flags_sha256": digest_json(flags)},
    }


def product_directory(args: argparse.Namespace) -> Path:
    return args.build_dir / f"{args.configuration}-{args.sdk}" / "meson"


def library_products(args: argparse.Namespace) -> dict:
    products = {}
    for name in LIBRARIES:
        path = product_directory(args) / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or empty iSH archive: {path}")
        products[name] = {"size": path.stat().st_size, "sha256": file_sha256(path)}
    return products


def libraries(args: argparse.Namespace) -> int:
    state_path = args.build_dir / "mira-ish-libraries.json"
    pending_path = args.build_dir / "mira-ish-libraries.pending.json"
    inputs = library_inputs(args)
    fingerprint = digest_json(inputs)
    if args.action == "check":
        state = read_json(state_path)
        if state.get("schema") != SCHEMA or state.get("fingerprint") != fingerprint:
            print("iSH library cache is stale: build inputs changed or manifest is missing.")
            return 1
        if state.get("products") != library_products(args):
            print("iSH library cache is stale: archive contents changed.")
            return 1
        print("iSH library cache matches source, toolchain, and build parameters.")
        return 0
    if args.action == "begin":
        # Invalidate before xcodebuild: interrupted/failed builds must never reuse a
        # manifest for archives they may have partially overwritten.
        state_path.unlink(missing_ok=True)
        write_json(pending_path, {"inputs": inputs, "fingerprint": fingerprint})
        return 0
    pending = read_json(pending_path)
    if pending.get("fingerprint") != fingerprint:
        raise ValueError("iSH inputs changed during the build; no reusable manifest was written.")
    products = library_products(args)
    write_json(state_path, {"schema": SCHEMA, "inputs": inputs, "fingerprint": fingerprint, "products": products})
    pending_path.unlink(missing_ok=True)
    return 0


def archive_state(args: argparse.Namespace) -> int:
    path = args.archive
    if sensitive_path(path) or sensitive_path(path.resolve()) or path.is_symlink():
        raise ValueError("Refusing to inspect a sensitive or symlinked rootfs archive.")
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty rootfs archive: {path}")
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if sensitive_path(Path(member.name)) or ((member.issym() or member.islnk()) and sensitive_path(Path(member.linkname))):
                raise ValueError("Rootfs archive contains a protected environment file; extraction refused.")
    actual = file_sha256(path)
    manifest_path = path.with_name(path.name + ".source.json")
    previous = read_json(manifest_path)
    if args.url == DEFAULT_ROOTFS_URL:
        if actual != DEFAULT_ROOTFS_SHA256:
            raise ValueError("Rootfs archive differs from Mira's pinned baseline; cache was not replaced.")
        verification = "mira-pinned-baseline-sha256"
    elif args.action == "record":
        verification = "downloaded-sha256"
    elif previous.get("url") == args.url and previous.get("sha256") == actual:
        return 0
    else:
        raise ValueError("Custom rootfs cache has no matching URL/checksum provenance; use a separate empty cache directory.")
    write_json(manifest_path, {"schema": SCHEMA, "url": args.url, "sha256": actual, "verification": verification})
    return 0


def rootfs_state(args: argparse.Namespace) -> int:
    output = args.output
    stamp = output / ".mira-ish-rootfs.stamp"
    meta = output / "meta.db"
    if not (output / "data").is_dir() or not meta.is_file() or meta.stat().st_size == 0 or not stamp.is_file():
        return 1
    path = output / ".mira-ish-rootfs.provenance.json"
    previous = read_json(path)
    expected = {"url": args.url, "runtime_stamp": args.runtime_stamp}
    observed = {"stamp_sha256": file_sha256(stamp), "metadata_sha256": file_sha256(meta)}
    if args.runtime_stamp not in stamp.read_text().splitlines():
        return 1
    if args.action == "reuse" and previous:
        return 0 if previous.get("schema") == SCHEMA and previous.get("baseline") == expected and previous.get("artifact") == observed else 1
    if args.action == "reuse":
        # A complete pre-existing filesystem is valuable: do not regenerate its
        # Python/Alpine packages just because the emulator source was upgraded.
        if args.url != DEFAULT_ROOTFS_URL:
            return 1
        provenance = {"creation": "adopted-legacy", "producer_ish_revision": None, "note": "Existing v13 filesystem retained; original archive and build inputs are not reconstructable from its stamp."}
    else:
        if not args.staging_root or not args.archive:
            raise ValueError("Recording a new rootfs requires its staging directory and archive.")
        source = args.staging_root / "opt/mira/frida-python/SOURCE.txt"
        provenance = {
            "creation": "built", "producer_ish_revision": command("git", "-C", str(args.root / "third_party/ish"), "rev-parse", "HEAD"),
            "archive_sha256": file_sha256(args.archive), "runtime_source_sha256": file_sha256(source),
            "scripts": {name: file_sha256(args.root / "tools/ios" / name) for name in ("prepare-ish-rootfs.sh", "prepare-ish-frida-runtime.py")},
            "skip_compileall": args.skip_compileall,
        }
    write_json(path, {"schema": SCHEMA, "baseline": expected, "artifact": observed, **provenance})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="kind", required=True)
    library = commands.add_parser("libraries")
    library.add_argument("action", choices=("check", "begin", "finish"))
    library.add_argument("--root", type=Path, required=True)
    library.add_argument("--build-dir", type=Path, required=True)
    library.add_argument("--sdk", required=True)
    library.add_argument("--configuration", required=True)
    library.add_argument("--archs", required=True)
    library.add_argument("--extra-ldflags", default="")
    archive = commands.add_parser("archive")
    archive.add_argument("action", choices=("verify", "record"))
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--url", required=True)
    rootfs = commands.add_parser("rootfs")
    rootfs.add_argument("action", choices=("reuse", "record"))
    rootfs.add_argument("--root", type=Path, required=True)
    rootfs.add_argument("--output", type=Path, required=True)
    rootfs.add_argument("--url", required=True)
    rootfs.add_argument("--runtime-stamp", required=True)
    rootfs.add_argument("--archive", type=Path)
    rootfs.add_argument("--staging-root", type=Path)
    rootfs.add_argument("--skip-compileall", action="store_true")
    commands.add_parser("rootfs-url")
    safe_tree = commands.add_parser("tree-safe")
    safe_tree.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.kind == "libraries":
            return libraries(args)
        if args.kind == "archive":
            return archive_state(args)
        if args.kind == "rootfs":
            return rootfs_state(args)
        if args.kind == "tree-safe":
            for directory, names, files in os.walk(args.directory, followlinks=False):
                for name in names + files:
                    path = Path(directory) / name
                    if sensitive_path(path.relative_to(args.directory)) or sensitive_path(path.resolve()):
                        raise ValueError("Rootfs contains a protected environment path; archive creation refused.")
            return 0
        print(DEFAULT_ROOTFS_URL)
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"iSH build state: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
