"""Acquire the pinned artifacts and freeze an external qualification environment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence import INTEGRITY, LIMITS, PROFILE, WHEEL, digest, file_hash, read, require, write

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/experimental/openclaw_bicep_e2e"
FIXTURES = REPO / "tests/fixtures"
WHEEL_URL = "https://files.pythonhosted.org/packages/ad/f5/f754bb2813520234f6fbcab3de05e0456534fa898e443589956b083b7aea/maf_sandbox_mcp-0.1.0-py3-none-any.whl"
NPM_URL = "https://registry.npmjs.org/openclaw/-/openclaw-2026.9.7.tgz"


def environment(root: Path) -> dict[str, str]:
    """Isolate host configuration while retaining explicit Docker connectivity."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
        }
    }
    env.update(
        HOME=str(root),
        USERPROFILE=str(root),
        APPDATA=str(root / "appdata"),
        LOCALAPPDATA=str(root / "local"),
        TEMP=str(root / "temp"),
        TMP=str(root / "temp"),
        TMPDIR=str(root / "temp"),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        UV_NO_CONFIG="1",
        NODE_DISABLE_COMPILE_CACHE="1",
    )
    return env


def command(args: list[str], root: Path, timeout: int = 1200) -> str:
    """Run a setup or observer command with a finite deadline and private diagnostics."""
    resolved = shutil.which(args[0]) or args[0]
    with (root / "setup.log").open("ab") as log:
        completed = subprocess.run(
            [resolved, *args[1:]],
            cwd=root,
            env=environment(root),
            stdout=subprocess.PIPE,
            stderr=log,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    require((root / "setup.log").stat().st_size <= LIMITS["log"], "setup_log_limit")
    require(completed.returncode == 0, "setup_command_failed")
    require(len(completed.stdout) <= LIMITS["events"], "setup_output_limit")
    return completed.stdout.decode("utf-8").strip()


def download(url: str, target: Path, limit: int) -> None:
    """Acquire an artifact with a fixed byte limit."""
    with urllib.request.urlopen(url, timeout=60) as response, target.open("xb") as stream:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            require(size <= limit, "download_limit")
            stream.write(chunk)


def _without_extended_prefix(path: Path) -> Path:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def npm_tree(archive: Path, package: Path, *, completed: bool = True) -> dict[str, Any]:
    """Verify archive bytes and the pinned installer's single lifecycle-marker removal."""
    with archive.open("rb") as stream:
        require(
            "sha512-" + base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
            == INTEGRITY,
            "npm_integrity",
        )
    package_root = _without_extended_prefix(package.resolve())
    entries = []
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            require(
                path.parts[:1] == ("package",)
                and ".." not in path.parts
                and "\\" not in member.name,
                "archive_path",
            )
            if member.isdir():
                continue
            require(member.isfile(), "archive_entry")
            name = PurePosixPath(*path.parts[1:]).as_posix()
            require(name not in seen, "duplicate_archive_path")
            seen.add(name)
            total += member.size
            require(len(seen) <= 20000 and total <= 400000000, "archive_limit")
            stream = source.extractfile(member)
            require(stream is not None, "archive_read")
            assert stream is not None
            with stream:
                hashed = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    hashed.update(block)
            entries.append((name, hashed.hexdigest()))
    require(".openclaw-lifecycle-pending" in seen, "missing_lifecycle_marker")

    def verify(entry: tuple[str, str]) -> bytes:
        name, expected = entry
        target = package / name
        resolved = target.resolve()
        require(
            _without_extended_prefix(resolved).is_relative_to(package_root), "installed_file_escape"
        )
        if name == ".openclaw-lifecycle-pending" and completed:
            require(
                expected == hashlib.sha256(b"pending\n").hexdigest()
                and not target.exists()
                and not target.is_symlink(),
                "lifecycle_not_completed",
            )
        else:
            require(file_hash(resolved) == expected, "installed_npm_bytes")
        return f"{name}\0{expected}\n".encode()

    tree = hashlib.sha256()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for record in pool.map(verify, entries):
            tree.update(record)
    return {
        "files": len(seen) - int(completed),
        "archive_files": len(seen),
        "bytes": total - (8 if completed else 0),
        "tree_sha256": tree.hexdigest(),
        "lifecycle_marker": "removed_by_pinned_postinstall" if completed else "pending",
    }


def runner_hash() -> str:
    """Bind all adapters, assertions and process supervision helpers."""
    paths = [
        *Path(__file__).parent.glob("*.py"),
        *FIXTURES.glob("openclaw*.py"),
        *SAMPLE.glob("*.py"),
        REPO / "tests/test_mcp_workload_service.py",
    ]
    return digest({p.relative_to(REPO).as_posix(): file_hash(p) for p in sorted(paths)})


def runner_revision() -> str:
    """Require committed qualification sources before binding a candidate."""
    paths = [
        "scripts/openclaw_conformance",
        "tests/fixtures",
        "tests/test_mcp_workload_service.py",
        "samples/experimental/openclaw_bicep_e2e",
    ]
    changed = subprocess.check_output(
        ["git", "-C", str(REPO), "status", "--porcelain", "--", *paths], timeout=30
    )
    require(not changed.strip(), "uncommitted_runner")
    return subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], encoding="utf-8", timeout=30
    ).strip()


def python_path(root: Path) -> Path:
    """Locate the prepared environment interpreter."""
    return root / ("worker/Scripts/python.exe" if os.name == "nt" else "worker/bin/python")


def host(root: Path) -> dict[str, Any]:
    """Record platform and engine identity without infrastructure identifiers."""
    info = json.loads(command(["docker", "info", "--format", "{{json .}}"], root, 30))
    return {
        "platform": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "architecture": platform.machine(),
        "node": command(["node", "--version"], root, 30),
        "docker": info["ServerVersion"],
        "kernel": info["KernelVersion"],
        "engine_os": info["OSType"],
        "engine_arch": info["Architecture"],
    }


def prepare(root: Path, image: str) -> dict[str, Any]:
    """Resolve a fresh index-only environment and freeze its exact manifest."""
    require(
        not root.exists() and not root.resolve().is_relative_to(REPO),
        "fresh_external_environment_required",
    )
    require(platform.system() == "Windows", "unqualified_platform")
    root.mkdir(parents=True, mode=0o700)
    (root / "temp").mkdir()
    host_info = host(root)
    image_info = json.loads(command(["docker", "image", "inspect", image], root, 30))[0]
    require(image_info["Os"] == "linux" and image_info["Architecture"] == "amd64", "image_platform")
    wheel = root / "maf_sandbox_mcp-0.1.0-py3-none-any.whl"
    download(WHEEL_URL, wheel, 1048576)
    require(file_hash(wheel) == WHEEL, "wheel_integrity")
    (root / "requirements.in").write_text(
        "maf-sandbox-mcp==0.1.0\npytest==9.0.3\n", encoding="utf-8"
    )
    command(["uv", "venv", "--python", "3.13", str(root / "worker")], root)
    python = str(python_path(root))
    command(
        [
            "uv",
            "pip",
            "compile",
            "--python",
            python,
            "--default-index",
            "https://pypi.org/simple",
            "--generate-hashes",
            "--no-emit-index-url",
            "--output-file",
            "requirements.lock",
            "requirements.in",
        ],
        root,
    )
    command(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            python,
            "--default-index",
            "https://pypi.org/simple",
            "--require-hashes",
            "requirements.lock",
        ],
        root,
    )
    manifest = json.loads(
        command(
            [
                python,
                "-I",
                "-B",
                "-c",
                "import importlib.metadata as m,json,platform; print(json.dumps({'python':platform.python_version(),'versions':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}}))",
            ],
            root,
        )
    )
    host_info["python"] = manifest["python"]
    # npm's lock records the resolved transitive package integrities; scripts stay disabled.
    npm = "npm.cmd" if os.name == "nt" else "npm"
    command(
        [
            npm,
            "install",
            "--prefix",
            str(root / "openclaw"),
            "--registry",
            "https://registry.npmjs.org",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "openclaw@2026.9.7",
        ],
        root,
    )
    claw = root / "openclaw/node_modules/openclaw"
    archive = root / "openclaw-2026.9.7.tgz"
    download(NPM_URL, archive, 200000000)
    before = npm_tree(archive, claw, completed=False)
    lock = read(root / "openclaw/package-lock.json", LIMITS["events"])
    require(
        lock["packages"]["node_modules/openclaw"]["integrity"] == INTEGRITY, "npm_lock_identity"
    )
    command(["node", str(claw / "scripts/postinstall-bundled-plugins.mjs")], root)
    tree = npm_tree(archive, claw)
    require(tree["tree_sha256"] == before["tree_sha256"], "postinstall_changed_artifact")
    shutil.copyfile(SAMPLE / "bicepconfig.json", root / "bicepconfig.json")
    probe = "import hashlib,json,pathlib,maf_sandbox_mcp; p=pathlib.Path(maf_sandbox_mcp.__file__).parent; print(json.dumps({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in p.glob('*.py')}))"
    modules = json.loads(command([python, "-I", "-B", "-c", probe], root))
    from .assets import prepare as prepare_assets

    runtime_assets = prepare_assets(root, manifest["versions"])
    value = {
        "runtime_assets": runtime_assets,
        "schema": 1,
        "profile": PROFILE,
        "openclaw": "2026.9.7",
        "npm_integrity": INTEGRITY,
        "wheel_sha256": WHEEL,
        "versions": manifest["versions"],
        "python_lock_sha256": file_hash(root / "requirements.lock"),
        "npm_lock_sha256": file_hash(root / "openclaw/package-lock.json"),
        "npm_tree": tree,
        "worker_modules": modules,
        "image": image_info["Id"],
        "config_sha256": file_hash(root / "bicepconfig.json"),
        "host": host_info,
        "runner_sha256": runner_hash(),
        "runner_commit": runner_revision(),
    }
    write(root / "candidate.json", value)
    return value
