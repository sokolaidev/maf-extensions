"""Bind installed dependency and native assets to locked archives and a complete tree."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence import LIMITS, digest, encode, file_hash, read, require


def inventory(root: Path) -> dict[str, str]:
    """Include every runtime file, including launchers, metadata and bytecode."""
    files: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for directory in ("worker/Lib/site-packages", "worker/Scripts", "openclaw/node_modules"):
            base = root / directory
            require(base.is_dir(), "missing_runtime_tree")
            resolved_base = base.resolve()

            def inspect(path: Path) -> tuple[str, str] | None:
                require(
                    not path.is_symlink() and path.resolve().is_relative_to(resolved_base),
                    "runtime_link",
                )
                if path.is_file():
                    return path.relative_to(root).as_posix(), file_hash(path)
                return None

            # Bound queued work while overlapping Windows file-open latency.
            for batch in batched(base.rglob("*"), 256):
                for entry in pool.map(inspect, batch):
                    if entry is not None:
                        files[entry[0]] = entry[1]
                        require(len(files) <= 150000, "runtime_inventory_limit")
    return files


def verify(root: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Refuse changed, missing or added files before and after execution."""
    record = root / "runtime-assets.json"
    require(file_hash(record) == expected["manifest_sha256"], "runtime_manifest_changed")
    saved = read(record, LIMITS["events"])
    require(inventory(root) == saved["files"], "runtime_assets_changed")
    for name, hashed in saved["archives"].items():
        require(file_hash(root / "archives" / name) == hashed, "runtime_archive_changed")
    require(expected == _summary(saved, file_hash(record)), "runtime_summary_changed")
    return expected


def _summary(saved: dict[str, Any], hashed: str) -> dict[str, Any]:
    return {
        "manifest_sha256": hashed,
        "tree_sha256": digest(saved["files"]),
        "files": len(saved["files"]),
        "archives": len(saved["archives"]),
        "archive_verified_files": saved["archive_verified_files"],
        "generated_files": saved["generated_files"],
    }


def _path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    require(
        not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name,
        "archive_path",
    )
    return path


def _file(target: Path, data: bytes, covered: set[str], root: Path) -> None:
    require(target.resolve().is_relative_to(root.resolve()), "archive_target_escape")
    require(
        target.is_file() and file_hash(target) == hashlib.sha256(data).hexdigest(),
        "locked_archive_bytes",
    )
    covered.add(target.relative_to(root).as_posix())


def wheel(archive: Path, site: Path, root: Path) -> set[str]:
    """Verify wheel payloads, including native libraries, against installed bytes."""
    covered: set[str] = set()
    with zipfile.ZipFile(archive) as source:
        require(sum(i.file_size for i in source.infolist()) <= 500000000, "wheel_size")
        for member in source.infolist():
            path = _path(member.filename)
            if member.is_dir() or member.filename.endswith(".dist-info/RECORD"):
                continue
            destination = site
            if path.parts[0].endswith(".data"):
                require(
                    path.parts[1] in ("purelib", "platlib", "scripts"),
                    "unsupported_wheel_relocation",
                )
                if path.parts[1] == "scripts":
                    destination = root / "worker/Scripts"
                path = PurePosixPath(*path.parts[2:])
            _file(destination / str(path), source.read(member), covered, root)
    return covered


def npm(archive: Path, package: Path, root: Path) -> set[str]:
    """Verify every tarball file, including bundled dependency and native assets."""
    covered: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        metadata = [
            m
            for m in source.getmembers()
            if len(PurePosixPath(m.name).parts) == 2
            and PurePosixPath(m.name).name == "package.json"
        ]
        bins: set[str] = set()
        if metadata:
            require(len(metadata) == 1 and metadata[0].size <= LIMITS["summary"], "npm_metadata")
            stream = source.extractfile(metadata[0])
            assert stream is not None
            with stream:
                declared = json.load(stream).get("bin", {})
            bins = {
                str(_path(p))
                for p in ([declared] if isinstance(declared, str) else declared.values())
            }
        for member in source:
            path = _path(member.name)
            require(len(path.parts) > 0, "archive_path")
            if member.isdir():
                continue
            require(member.isfile(), "archive_entry")
            total += member.size
            require(total <= 1000000000, "npm_archive_size")
            stream = source.extractfile(member)
            require(stream is not None, "archive_read")
            assert stream is not None
            with stream:
                data = stream.read()
            relative = PurePosixPath(*path.parts[1:])
            if package.name == "openclaw" and str(relative) == ".openclaw-lifecycle-pending":
                require(
                    data == b"pending\n" and not (package / str(relative)).exists(),
                    "lifecycle_marker",
                )
                continue
            if str(relative) in bins and data.startswith(b"#!"):
                first, separator, rest = data.partition(b"\n")
                if first.endswith(b"\r"):
                    data = first[:-1] + separator + rest
            _file(package / str(relative), data, covered, root)
    return covered


def prepare(root: Path, versions: dict[str, str]) -> dict[str, Any]:
    """Verify locked payloads before freezing generated installer files alongside them."""
    from .install import download

    archives = root / "archives"
    archives.mkdir(exist_ok=True)
    site = root / "worker/Lib/site-packages"
    allowed = set(
        re.findall(r"--hash=sha256:([0-9a-f]{64})", (root / "requirements.lock").read_text())
    )
    jobs: list[tuple[str, str, str, Path, str]] = []
    for name, version in versions.items():
        dist = list(site.glob(name.replace("-", "_") + "-*.dist-info"))
        require(len(dist) == 1, "wheel_metadata")
        tags = set(re.findall(r"^Tag: (.+)$", (dist[0] / "WHEEL").read_text(), re.M))
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{version}/json", timeout=60
        ) as response:
            metadata = json.load(response)
        matches = [
            v
            for v in metadata["urls"]
            if v["filename"].endswith(".whl")
            and v["digests"]["sha256"] in allowed
            and any(v["filename"].endswith(tag + ".whl") for tag in tags)
        ]
        require(len(matches) == 1, "locked_wheel_selection")
        selected = matches[0]
        jobs.append(
            (
                selected["url"],
                "sha256-" + selected["digests"]["sha256"],
                selected["filename"],
                site,
                "wheel",
            )
        )
    lock = read(root / "openclaw/package-lock.json", LIMITS["events"])
    bundled: list[Path] = []
    for name, entry in lock["packages"].items():
        if not name:
            continue
        package = root / "openclaw" / str(_path(name))
        if not package.exists():
            require(entry.get("optional") is True, "missing_npm_package")
            continue
        if entry.get("inBundle"):
            bundled.append(package)
            continue
        require(
            entry.get("resolved", "").startswith("https://registry.npmjs.org/")
            and entry.get("integrity", "").startswith(("sha512-", "sha1-")),
            "npm_lock_artifact",
        )
        jobs.append((entry["resolved"], entry["integrity"], digest(name) + ".tgz", package, "npm"))

    def acquire(job: tuple[str, str, str, Path, str]) -> tuple[str, str, set[str]]:
        url, integrity, name, target, kind = job
        archive = archives / name
        if not archive.exists():
            download(url, archive, 500000000)
        algorithm, expected = integrity.split("-", 1)
        with archive.open("rb") as stream:
            hashed = hashlib.file_digest(stream, algorithm)
        observed = (
            hashed.hexdigest() if kind == "wheel" else base64.b64encode(hashed.digest()).decode()
        )
        require(observed == expected, "locked_archive_integrity")
        covered = wheel(archive, target, root) if kind == "wheel" else npm(archive, target, root)
        return name, file_hash(archive), covered

    covered: set[str] = set()
    saved_archives = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, hashed, files in pool.map(acquire, jobs):
            saved_archives[name] = hashed
            covered.update(files)
    files = inventory(root)
    for package in bundled:
        prefix = package.relative_to(root).as_posix() + "/"
        require(any(name.startswith(prefix) for name in covered), "unverified_bundled_package")
    generated = sorted(set(files) - covered)
    # Only known installer outputs may sit outside the locked archive payloads.
    for name in generated:
        parts = PurePosixPath(name).parts
        allowed_generated = (
            name.startswith("worker/Scripts/")
            or ".bin" in parts
            or name == "openclaw/node_modules/.package-lock.json"
            or name
            in (
                "worker/Lib/site-packages/_virtualenv.py",
                "worker/Lib/site-packages/_virtualenv.pth",
            )
            or (
                any(p.endswith(".dist-info") for p in parts)
                and parts[-1] in ("RECORD", "INSTALLER", "REQUESTED", "uv_cache.json")
            )
        )
        require(allowed_generated, "unexpected_installed_file:" + name)
    saved = {
        "files": files,
        "archives": saved_archives,
        "archive_verified_files": len(covered),
        "generated_files": generated,
    }
    data = encode(saved)
    require(len(data) <= LIMITS["events"], "runtime_manifest_limit")
    path = root / "runtime-assets.json"
    path.write_bytes(data)
    return _summary(saved, file_hash(path))
