# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "maf-sandbox==0.48.0",
#     "maf-sandbox-docker==0.27.0",
#     "agent-framework-core==1.20.0",
# ]
# ///
"""Qualify a signed Graphviz release through the published SDK and optional hardened CLI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
import time
import zlib
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path
from typing import Any
from uuid import uuid4

from diagram_kind import diagram_sandbox_spec, make_diagram_tools
from maf_sandbox import (
    Isolation,
    Sandbox,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    make_file_system_sink,
)
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "ghcr.io/sokolaidev/maf-extensions"


def now() -> str:
    """Timestamp qualification evidence in UTC."""
    return datetime.now(UTC).isoformat()


def verify_release(policy: Path, evidence: Path) -> dict[str, Any]:
    """Delegate identity verification and separate monitoring status to the consumer verifier."""
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_container_release.py"),
            "--policy",
            str(policy),
            "--evidence",
            str(evidence),
            "--bundles",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    result = json.loads(completed.stdout)
    require(result.get("releaseIdentityVerified") is True, "Release identity was not verified")
    return result


def require(condition: bool, message: str) -> None:
    """Keep qualification gates active even when Python assertions are disabled."""
    if not condition:
        raise ValueError(message)


def installed_packages() -> dict[str, str]:
    """Record the resolved environment, refusing editable or direct-source SDK installs."""
    installed = {}
    for package in distributions():
        name = package.metadata["Name"]
        if name.lower().replace("_", "-") in {"maf-sandbox", "maf-sandbox-docker"}:
            require(package.read_text("direct_url.json") is None, "Use published SDK wheels")
        installed[name] = package.version
    return dict(sorted(installed.items()))


def png_details(data: bytes) -> dict[str, Any]:
    """Validate bounded, noninterlaced 8-bit RGB/RGBA scanlines for the fixed render probe."""
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), "Output is not PNG")
    offset, chunks, dimensions = 8, [], None
    compressed = bytearray()
    stride = expected_size = 0
    while offset < len(data):
        require(offset + 12 <= len(data), "Truncated PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + size
        require(end <= len(data), "Truncated PNG payload")
        kind = data[offset + 4 : offset + 8]
        require(bool(chunks) or kind == b"IHDR", "PNG must start with IHDR")
        require(
            kind[0] & 32 != 0 or kind in {b"IHDR", b"IDAT", b"IEND"},
            "Unsupported critical PNG chunk",
        )
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        require(zlib.crc32(kind + payload) == crc, "PNG checksum mismatch")
        if kind == b"IHDR":
            require(not chunks and size == 13, "Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            dimensions = (width, height)
            require(all(dimensions), "Empty PNG dimensions")
            require(
                depth == 8 and color in {2, 6} and compression == filtering == interlace == 0,
                "Probe requires noninterlaced 8-bit RGB or RGBA PNG",
            )
            stride = 1 + width * (3 if color == 2 else 4)
            expected_size = stride * height
            # The fixed three-node graph cannot reasonably need more than this output budget.
            require(expected_size <= 16 * 1024 * 1024, "PNG raster exceeds probe budget")
        if kind == b"IDAT":
            require(b"IDAT" not in chunks or chunks[-1] == b"IDAT", "Nonconsecutive PNG data")
            compressed.extend(payload)
        chunks.append(kind)
        offset = end
        if kind == b"IEND":
            require(size == 0 and end == len(data), "Invalid PNG end")
            break
    require(dimensions is not None and chunks[-1:] == [b"IEND"], "Incomplete PNG")
    require(bool(compressed), "PNG contains no pixels")
    inflater = zlib.decompressobj()
    pixels = inflater.decompress(bytes(compressed), expected_size + 1)
    require(
        len(pixels) == expected_size
        and inflater.eof
        and not inflater.unused_data
        and not inflater.unconsumed_tail,
        "PNG raster does not match IHDR",
    )
    require(
        all(pixels[start] <= 4 for start in range(0, len(pixels), stride)),
        "Invalid PNG scanline filter",
    )
    if dimensions is None:
        raise ValueError("Missing PNG dimensions")
    return {
        "width": dimensions[0],
        "height": dimensions[1],
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class Engine:
    """Read the same selected Docker context throughout one qualification."""

    def __init__(self) -> None:
        self.env = dict(os.environ)
        self.context = self.command("context", "show").strip()
        self.env["DOCKER_CONTEXT"] = self.context

    def command(self, *args: str, timeout: int = 60) -> str:
        """Run one bounded Docker command and refuse an unreadable engine."""
        return subprocess.run(
            ["docker", *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        ).stdout

    def owned(self, scope: str) -> list[str]:
        """List running and stopped containers under this unique qualification scope."""
        return self.command(
            "ps", "-aq", "--no-trunc", "--filter", f"label=maf-sandbox.scope={scope}"
        ).split()

    def inspect(self, scope: str, image: str, local_id: str) -> dict[str, Any]:
        """Observe the selected image and network on the actual SDK container."""
        ids = self.owned(scope)
        require(len(ids) == 1, "Expected exactly one owned SDK container")
        value = json.loads(self.command("inspect", ids[0]))[0]
        require(value["State"]["Running"] is True, "SDK container is not running")
        require(
            value["Config"]["Image"] == image and value["Image"] == local_id,
            "SDK container image differs from the verified selection",
        )
        host = value["HostConfig"]
        require(host["NetworkMode"] == "none", "SDK container has network access")
        require(
            set(value["NetworkSettings"]["Networks"]) <= {"none"},
            "SDK container has an additional network",
        )
        return {
            "containerId": ids[0],
            "imageReference": image,
            "engineImageId": local_id,
            "networkMode": host["NetworkMode"],
            "user": value["Config"]["User"],
            "readOnlyRoot": host["ReadonlyRootfs"],
            "capDrop": host["CapDrop"],
        }


def check_cli_container(
    value: dict[str, Any], image: str, local_id: str, user: str, source: Path, target: Path
) -> dict[str, Any]:
    """Require the consumer guide's controls on the container before starting its renderer."""
    host, config = value["HostConfig"], value["Config"]
    require(config["Image"] == image and value["Image"] == local_id, "CLI image changed")
    require(config["User"] == user, "CLI user differs from the non-root host user")
    require(host["NetworkMode"] == "none", "CLI networking is not closed")
    require(host["ReadonlyRootfs"] is True, "CLI root filesystem is writable")
    require(set(host["CapDrop"]) == {"ALL"}, "CLI capabilities are not all dropped")
    require(
        "no-new-privileges" in host["SecurityOpt"]
        or "no-new-privileges:true" in host["SecurityOpt"],
        "CLI privilege escalation is not disabled",
    )
    require(host["PidsLimit"] == 256, "CLI process limit differs")
    require(host["Memory"] == 1024**3, "CLI memory limit differs")
    require(host["NanoCpus"] == 2_000_000_000, "CLI CPU limit differs")
    require(host["AutoRemove"] is True, "CLI automatic removal is disabled")
    require(host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=64m"}, "CLI scratch mount differs")
    require(config["Entrypoint"] == ["dot"], "CLI renderer entrypoint differs")
    require(
        config["Cmd"] == ["-Tpng", "/input/diagram.dot", "-o", "/output/diagram.png"],
        "CLI renderer arguments differ",
    )
    require("XDG_CACHE_HOME=/tmp/cache" in config["Env"], "CLI cache is outside scratch")
    mounts = {item["Destination"]: item for item in value["Mounts"]}
    require(len(mounts) == len(value["Mounts"]), "CLI mount destinations are duplicated")
    require(set(mounts) <= {"/input", "/output", "/tmp"}, "CLI has an unexpected mount")
    for destination, path, writable in (("/input", source, False), ("/output", target, True)):
        item = mounts.get(destination, {})
        require(
            item.get("Type") == "bind"
            and item.get("Source") == str(path)
            and item.get("RW") is writable,
            f"CLI {destination} mount differs",
        )
    if "/tmp" in mounts:
        require(mounts["/tmp"]["Type"] == "tmpfs", "CLI scratch is not tmpfs")
    return {"user": user, "hostConfig": host, "mounts": value["Mounts"]}


def exercise_cli(expected: dict[str, Any], output: Path, result: dict[str, Any]) -> None:
    """Qualify the hardened CLI render with bounded execution and independently checked cleanup."""
    report: dict[str, Any] = {"passed": False, "timeoutSeconds": 30}
    result["hardenedCli"] = report
    require(sys.platform == "linux", "Hardened CLI qualification requires a Linux host")
    uid, gid = getattr(os, "getuid")(), getattr(os, "getgid")()
    require(uid > 0, "Hardened CLI qualification requires a non-root host user")
    user = f"{uid}:{gid}"
    engine = Engine()
    image = f"{PREFIX}/graphviz@{expected['registryDigest']}"
    engine.command("pull", "--platform", "linux/amd64", image, timeout=600)
    selected = json.loads(engine.command("image", "inspect", image))[0]
    require(image in selected["RepoDigests"], "CLI pull lacks the selected digest")
    require(
        selected["Id"] in {expected["imageId"], expected["registryDigest"]},
        "CLI pull has an unexpected identity",
    )
    source, target = (output / "cli-input").resolve(), (output / "cli-output").resolve()
    source.mkdir()
    target.mkdir()
    (source / "diagram.dot").write_text(
        "digraph { consumer -> renderer -> png }\n", encoding="utf-8"
    )
    scope = "graphviz-cli-" + uuid4().hex
    report.update(imageReference=image, engineImageId=selected["Id"], scope=scope)
    try:
        # Split run into create/start so short-lived dot can be inspected before execution.
        container = engine.command(
            "create",
            "--rm",
            "--platform",
            "linux/amd64",
            "--label",
            f"maf-sandbox.scope={scope}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            user,
            "--env",
            "XDG_CACHE_HOME=/tmp/cache",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,src={source},dst=/input,readonly",
            "--mount",
            f"type=bind,src={target},dst=/output",
            "--entrypoint",
            "dot",
            image,
            "-Tpng",
            "/input/diagram.dot",
            "-o",
            "/output/diagram.png",
        ).strip()
        require(engine.owned(scope) == [container], "CLI ownership is ambiguous")
        report["containerId"] = container
        inspected = json.loads(engine.command("inspect", container))
        require(len(inspected) == 1, "CLI inspection is ambiguous")
        report["controls"] = check_cli_container(
            inspected[0], image, selected["Id"], user, source, target
        )
        started = time.monotonic()
        try:
            engine.command("start", "--attach", container, timeout=30)
        finally:
            report["seconds"] = time.monotonic() - started
        remaining = engine.owned(scope)
        report["containersAfterRender"] = remaining
        require(not remaining, "CLI render left a container before fallback cleanup")
        png = target / "diagram.png"
        require(
            png.is_file() and not png.is_symlink() and png.stat().st_size <= 16 * 1024 * 1024,
            "CLI PNG is absent, linked or oversized",
        )
        report["png"] = png_details(png.read_bytes())
    finally:
        remaining = engine.owned(scope)
        report["fallbackRemoved"] = list(remaining)
        for container in remaining:
            engine.command("rm", "--force", container)
        remaining = engine.owned(scope)
        report["containersRemaining"] = remaining
        require(not remaining, "CLI cleanup left owned containers")
    report["passed"] = True


class ObservedRouter(SandboxRouter):
    """Exercise the real router while retaining independent engine observations."""

    def __init__(
        self, backend: DockerSandboxBackend, engine: Engine, scope: str, image: str, local_id: str
    ) -> None:
        super().__init__([backend], min_isolation=Isolation.CONTAINER)
        self.engine, self.qualification_scope, self.image, self.local_id = (
            engine,
            scope,
            image,
            local_id,
        )
        self.observations: list[dict[str, Any]] = []

    async def acquire(self, key: SandboxKey, spec: SandboxSpec, **kwargs: Any) -> Sandbox:
        """Inspect every successful acquisition before letting the caller execute code."""
        sandbox = await super().acquire(key, spec, **kwargs)
        self.observations.append(
            await asyncio.to_thread(
                self.engine.inspect, self.qualification_scope, self.image, self.local_id
            )
        )
        return sandbox


async def exercise(expected: dict[str, Any], output: Path, result: dict[str, Any]) -> None:
    """Drive the sample tool and a deterministic SDK timeout against the selected digest."""
    engine = Engine()
    server = json.loads(engine.command("version", "--format", "{{json .Server}}"))
    require(server["Os"] == "linux" and server["Arch"] == "amd64", "Requires Linux/amd64 Docker")
    result["docker"] = {key: server[key] for key in ("Version", "Os", "Arch")}
    image = f"{PREFIX}/graphviz@{expected['registryDigest']}"
    engine.command("pull", "--platform", "linux/amd64", image, timeout=600)
    selected = json.loads(engine.command("image", "inspect", image))[0]
    require(image in selected["RepoDigests"], "Pulled image lacks the selected repository digest")
    require(
        selected["Id"] in {expected["imageId"], expected["registryDigest"]},
        "Unexpected Docker image identity",
    )
    scope = "graphviz-qualification-" + uuid4().hex
    thread = "sdk"
    # The backend snapshots its environment at construction, before any asynchronous work.
    previous = os.environ.get("DOCKER_CONTEXT")
    os.environ["DOCKER_CONTEXT"] = engine.context
    try:
        backend = DockerSandboxBackend(DockerSandboxConfig())
    finally:
        if previous is None:
            os.environ.pop("DOCKER_CONTEXT", None)
        else:
            os.environ["DOCKER_CONTEXT"] = previous
    router = ObservedRouter(backend, engine, scope, image, selected["Id"])
    result["containers"] = router.observations
    result["checks"] = checks = {}
    try:
        for name, dot in (
            ("render", "digraph { ingest -> transform -> load }"),
            ("invalidDot", "digraph { -> }"),
        ):
            directory = output / name
            tools = make_diagram_tools(
                router,
                "diagram-designer",
                make_caller_context(list_no_files, lambda: scope, lambda: thread),
                make_file_system_sink(
                    directory,
                    existing="replace",
                    display=lambda _artifact, _path: "artifact:diagram.png",
                ),
                image=image,
            )
            tool = tools[0]
            function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
            before = len(router.observations)
            response = await function(dot=dot)
            require(len(router.observations) == before + 1, "Sample did not acquire one sandbox")
            remaining = await asyncio.to_thread(engine.owned, scope)
            require(not remaining, f"SDK left containers after {name}")
            files = list(directory.rglob("*.png")) if directory.exists() else []
            if name == "render":
                require(
                    response == "artifact:diagram.png" and len(files) == 1,
                    "Sample did not deliver its output reference and PNG",
                )
                checks[name] = {
                    "passed": True,
                    "png": png_details(files[0].read_bytes()),
                    "containersRemaining": remaining,
                }
            else:
                require(
                    isinstance(response, str)
                    and response.startswith("dot could not render the diagram (exit 1):")
                    and not files,
                    "Invalid DOT did not fail without delivering a PNG",
                )
                checks[name] = {"passed": True, "containersRemaining": remaining}
        sandbox = await router.acquire(
            SandboxKey(scope, thread, "timeout-probe"), diagram_sandbox_spec(image)
        )
        probe = await sandbox.exec(
            [
                "sh",
                "-ec",
                (
                    "command -v sleep; for path in /sys/class/net/*; do "
                    '[ -d "$path" ] || continue; flags=$(cat "$path/flags"); '
                    'printf "interface:%s:%s\n" "${path##*/}" "$flags"; done'
                ),
            ],
            working_directory="/tmp",
            timeout=10,
        )
        require(probe.exit_code == 0 and "/sleep" in probe.stdout, "Timeout probe unavailable")
        interfaces = {
            line.split(":")[1]: int(line.split(":")[2], 16)
            for line in probe.stdout.splitlines()
            if line.startswith("interface:")
        }
        active = sorted(name for name, flags in interfaces.items() if flags & 1)
        require(active == ["lo"], "Guest has an active external network interface")
        checks["network"] = {
            "passed": True,
            "interfaceFlags": interfaces,
            "activeInterfaces": active,
        }
        started = time.monotonic()
        timed_out = False
        try:
            await sandbox.exec(["sleep", "30"], working_directory="/tmp", timeout=1)
        except TimeoutError:
            timed_out = True
        elapsed = time.monotonic() - started
        remaining = await asyncio.to_thread(engine.owned, scope)
        require(
            timed_out and elapsed < 20 and not remaining,
            "SDK timeout did not promptly remove its container",
        )
        checks["timeout"] = {
            "passed": True,
            "seconds": elapsed,
            "limitSeconds": 1,
            "command": ["sleep", "30"],
            "containersRemaining": remaining,
        }
    finally:
        await router.dispose_scope(scope, thread)
        remaining = await asyncio.to_thread(engine.owned, scope)
        result["cleanup"] = {"containersRemaining": remaining}
        require(not remaining, "Qualification cleanup left owned containers")


def main() -> None:
    """Verify release identity and retain SDK and optional CLI qualification evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hardened-cli", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "graphviz-sdk-qualification",
        "passed": False,
        "startedAt": now(),
        "modelDriven": False,
        "python": platform.python_version(),
        "hostOs": platform.system(),
    }
    try:
        expected = json.loads(args.policy.read_text(encoding="utf-8"))
        require(expected.get("profile") == "graphviz", "Requires a Graphviz release policy")
        result["policy"] = expected
        result["packages"] = installed_packages()
        result["sourceCommit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        result["sourceDirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True
            )
        )
        result["sourceFiles"] = {
            str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in (Path(__file__), Path(__file__).with_name("diagram_kind.py"))
        }
        result["verification"] = verify_release(args.policy, args.evidence)
        asyncio.run(exercise(expected, args.output, result))
        if args.hardened_cli:
            exercise_cli(expected, args.output, result)
        result["passed"] = True
    except Exception as error:
        result["failureType"] = type(error).__name__
        if isinstance(error, ValueError):
            result["failure"] = str(error)
        raise
    finally:
        result["finishedAt"] = now()
        (args.output / "qualification.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
