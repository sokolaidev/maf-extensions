"""Build reviewed provider packages; --update-lock prepares hashes for human review."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import terraform_dependencies as prep  # noqa: E402
from scripts.terraform_manifest import render  # noqa: E402


def digest(data: bytes) -> str:
    """Hash exact build or package bytes."""
    return hashlib.sha256(data).hexdigest()


def package(files: dict[str, bytes]) -> bytes:
    """Normalize ZIP metadata so an approved package hash is reproducible."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (
                0o100755 << 16 if name.startswith("terraform-provider-") else 0o100644 << 16
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)
    return buffer.getvalue()


def unpack(data: bytes, target: Path) -> None:
    """Extract bounded source files; the build resolves reviewed modules instead of vendor."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        prefix = archive.infolist()[0].filename.split("/")[0] + "/"
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            if not entry.filename.startswith(prefix):
                raise ValueError("Source archive has multiple roots")
            if entry.orig_filename != entry.filename:
                raise ValueError("Source archive path escapes its root")
            name = entry.filename.removeprefix(prefix)
            if (
                name.startswith("/")
                or "\\" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
            ):
                raise ValueError("Source archive path escapes its root")
            if name.startswith(("vendor/", ".git/")):
                continue
            if entry.external_attr >> 16 & 0o170000 == 0o120000:
                if "testdata" in name.split("/"):
                    continue
                raise ValueError("Source archive contains a symlink outside test fixtures")
            total += entry.file_size
            if total > 1024 * 1024 * 1024:
                raise ValueError("Source archive exceeds expanded limit")
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(entry))


def build(provider: dict[str, Any], work: Path) -> tuple[bytes, dict[str, str]]:
    """Verify source and security overrides and build a self-identifying provider package."""
    plan = provider["source_build"]
    if subprocess.check_output(["go", "env", "GOVERSION"], text=True).strip() != plan["go_version"]:
        raise ValueError("Provider build toolchain mismatch")
    data = prep.fetch(
        plan["artifact"],
        time.monotonic() + 600,
        max_bytes=256 * 1024 * 1024,
        archive_limit=256 * 1024 * 1024,
    )
    unpack(data, work)
    env = {
        **os.environ,
        "CGO_ENABLED": "0",
        "GOOS": "linux",
        "GOARCH": "amd64",
        "GOAMD64": "v1",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOFLAGS": "-mod=mod",
        "GOMAXPROCS": "2",
    }

    def run(arguments: list[str]) -> None:
        subprocess.run(["go", *arguments], cwd=work, env=env, check=True)

    if plan["modules"]:
        run(["get", *(name + "@" + version for name, version in sorted(plan["modules"].items()))])
    run(["mod", "verify"])
    for name, version in plan["modules"].items():
        actual = json.loads(
            subprocess.check_output(
                ["go", "list", "-m", "-json", name], cwd=work, env=env, text=True
            )
        )
        if actual.get("Version") != version or "Replace" in actual:
            raise ValueError("Provider security dependency mismatch: " + name)
    source = provider["source"]
    kind = source.split("/")[-1]
    version = provider["version"]
    binary = "terraform-provider-" + kind + "_v" + version
    flags = (
        "-s -w -X main.version="
        + version
        + " -X main.Version="
        + version
        + " -X main.commit="
        + plan["revision"]
    )
    if kind in {"azuread", "azurerm"}:
        flags += (
            " -X github.com/hashicorp/terraform-provider-"
            + kind
            + "/version.ProviderVersion="
            + version
        )
    run(
        [
            "build",
            "-p=2",
            "-trimpath",
            "-buildvcs=false",
            "-ldflags=" + flags,
            "-o",
            str(work / binary),
            ".",
        ]
    )
    files = {name: (work / name).read_bytes() for name in ("go.mod", "go.sum", binary)}
    licenses = [
        path
        for path in work.iterdir()
        if path.is_file() and path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING"))
    ]
    if not licenses:
        raise ValueError("Provider source has no license notice")
    files.update({path.name: path.read_bytes() for path in licenses})
    module_files = {name: digest(files[name]) for name in ("go.mod", "go.sum")}
    record = {
        "builder": "SOKOLAI",
        "source": source,
        "version": version,
        "upstream_artifact": provider["artifact"],
        "inputs": {
            key: value for key, value in plan.items() if key not in {"sha256", "module_files"}
        },
        "files": {name: digest(content) for name, content in files.items()},
    }
    files["build.json"] = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    return package(files), module_files


def main() -> None:
    """Build reviewed hashes, or explicitly write a replacement manifest for review."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--update-lock", type=Path)
    args = parser.parse_args()
    if args.update_lock is not None and args.update_lock.resolve() == args.manifest.resolve():
        raise ValueError("Write proposed locks to a separate manifest for review")
    manifest = prep.load_manifest(args.manifest.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    for provider in manifest["providers"]:
        if "source_build" not in provider:
            continue
        print("Building", provider["source"], provider["version"], flush=True)
        with tempfile.TemporaryDirectory(prefix="provider-build-") as folder:
            data, module_files = build(provider, Path(folder))
        plan = provider["source_build"]
        if args.update_lock is None and (
            digest(data) != plan["sha256"] or module_files != plan["module_files"]
        ):
            raise ValueError("Provider build differs from reviewed package or module hashes")
        plan["sha256"], plan["module_files"] = digest(data), module_files
        target = (
            args.output / provider["source"] / provider["version"] / (provider["platform"] + ".zip")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if args.update_lock is not None:
            args.update_lock.with_suffix(".partial.json").write_text(
                render(manifest) + "\n", encoding="utf-8"
            )
        print("Built", provider["source"], provider["version"], digest(data), flush=True)
    if args.update_lock is not None:
        args.update_lock.write_text(render(manifest) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
