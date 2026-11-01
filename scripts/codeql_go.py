"""Build the repository's Go sources in their production module context for CodeQL."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTEXTS = (
    Path("packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy"),
    Path("packages/maf-sandbox-wslc/src/maf_sandbox_wslc/_proxy"),
)
STANDALONE = (
    Path("scripts/proxy_dependency_imports.go"),
    Path("tests/fixtures/openclaw_docker_authz/main.go"),
)


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run a checked build command and return its output."""
    try:
        return subprocess.run(
            args, cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        print(error.stdout, flush=True)
        raise


def build_identity(context: Path) -> tuple[str, str]:
    """Use the same upstream commit and Go version as the image build."""
    text = (context / "Dockerfile").read_text(encoding="utf-8")
    revision = re.search(r'\$\(git rev-parse HEAD\)"\s*=\s*([0-9a-f]{40})', text)
    version = re.search(r"^FROM golang:(\d+\.\d+\.\d+)-alpine@sha256:", text, re.M)
    if revision is None or version is None:
        raise ValueError(f"Missing proxy build identity: {context}")
    if "https://github.com/paradigmxyz/iron-proxy.git" not in text:
        raise ValueError(f"Unexpected proxy upstream: {context}")
    return revision[1], version[1]


def expected_sources(root: Path) -> list[Path]:
    """Reject tracked Go files without an explicit extraction target."""
    sources = list(STANDALONE)
    sources.append(STANDALONE[1].with_name("main_test.go"))
    for context in CONTEXTS:
        sources.extend(context / name for name in ("credential_gate.go", "credential_gate_test.go"))
        sources.append(context / "dependencies/imports.go")
    tracked = {Path(name) for name in run("git", "ls-files", "--", "*.go", cwd=root).splitlines()}
    if tracked != set(sources):
        raise ValueError(f"Go source inventory changed: {sorted(tracked ^ set(sources))}")
    return sources


def build(root: Path, output: Path) -> None:
    """Reconstruct both proxy modules in a fresh directory and compile owned sources."""
    sources = expected_sources(root)
    output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, GOTOOLCHAIN="local", GOFLAGS="-mod=readonly", GOWORK="off")
    version = run("go", "version", cwd=root).split()[2].removeprefix("go")
    mappings: list[str] = []
    builds: list[Path] = []
    for context in CONTEXTS:
        source = root / context
        revision, required_version = build_identity(source)
        if version != required_version:
            raise ValueError(f"Expected Go {required_version}, got {version}")
        destination = output / context.parts[1]
        run("git", "init", str(destination), cwd=root)
        run(
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/paradigmxyz/iron-proxy.git",
            cwd=destination,
        )
        run("git", "fetch", "--depth=1", "origin", revision, cwd=destination)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=destination)
        if run("git", "rev-parse", "HEAD", cwd=destination) != revision:
            raise ValueError("Proxy upstream revision mismatch")
        run("git", "apply", "--check", str(source / "iron.patch"), cwd=destination)
        run("git", "apply", str(source / "iron.patch"), cwd=destination)
        for name in ("go.mod", "go.sum"):
            shutil.copyfile(source / "dependencies" / name, destination / name)
        for name in ("credential_gate.go", "credential_gate_test.go"):
            shutil.copyfile(source / name, destination / "internal/proxy" / name)
            mappings.extend(
                (f"#{source.as_posix()}", f"{destination.as_posix()}/internal/proxy//{name}")
            )
        builds.append(destination)
    transformer = output / "path-transformer.txt"
    transformer.write_text("\n".join(mappings) + "\n", encoding="utf-8")
    environment["CODEQL_PATH_TRANSFORMER"] = str(transformer)
    for destination in builds:
        run("go", "mod", "download", cwd=destination, env=environment)
        run("go", "mod", "verify", cwd=destination, env=environment)
        run(
            "go",
            "test",
            "-count=1",
            "-run",
            "^TestMAFCredential",
            "./internal/proxy",
            cwd=destination,
            env=environment,
        )
        run(
            "go",
            "build",
            "-a",
            "-o",
            str(destination / "iron-proxy"),
            "./cmd/iron-proxy",
            cwd=destination,
            env=dict(environment, CGO_ENABLED="0"),
        )
    for number, source in enumerate(STANDALONE):
        run(
            "go",
            "build",
            "-a",
            "-o",
            str(output / f"standalone-{number}"),
            str(root / source),
            cwd=root,
            env=environment,
        )
    for context in CONTEXTS:
        run("go", "build", "-a", ".", cwd=root / context / "dependencies", env=environment)
    fixture = root / STANDALONE[1]
    run(
        "go",
        "test",
        "-count=1",
        str(fixture),
        str(fixture.with_name("main_test.go")),
        cwd=root,
        env=environment,
    )
    (output / "sources.json").write_text(
        json.dumps([p.as_posix() for p in sources], indent=2) + "\n", encoding="utf-8"
    )


def check_coverage(root: Path, archive: Path) -> None:
    """Require the database to contain each owned source at its original path and bytes."""
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        for source in expected_sources(root):
            suffix = (root / source).as_posix().lstrip("/")
            matches = [name for name in names if name == suffix or name.endswith("/" + suffix)]
            if len(matches) != 1 or bundle.read(matches[0]) != (root / source).read_bytes():
                raise ValueError(f"CodeQL did not extract the original source: {source}")
            print(f"Extracted: {source.as_posix()}")


def main() -> None:
    """Read the build version, compile Go sources, or check database coverage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("version", "build", "coverage"))
    parser.add_argument("--output", type=Path, default=ROOT / ".codeql-build")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "version":
        versions = {build_identity(ROOT / context)[1] for context in CONTEXTS}
        if len(versions) != 1:
            raise ValueError("Proxy Go versions differ")
        print(versions.pop())
    elif args.command == "build":
        build(ROOT, args.output.resolve())
    else:
        if args.archive is None:
            parser.error("coverage requires --archive")
        check_coverage(ROOT, args.archive)


if __name__ == "__main__":
    main()
