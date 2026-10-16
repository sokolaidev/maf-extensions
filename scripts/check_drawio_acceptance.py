"""Qualify Draw.io fonts, active-render cancellation and recovery after host process death."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import check_drawio_exports as exports
from drawio_qualification import owned, require
from maf_sandbox import (
    DisposalFailure,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    ScopePurge,
    ToolCallEnded,
    make_file_system_sink,
)
from maf_sandbox.maf import COMPLETED_TEXT, list_no_files, make_caller_context
from maf_sandbox_drawio import DrawioExport, make_drawio_tools

FONTS = ("DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono")
STOP_RENDERER = """
import json, os, signal, time
from pathlib import Path
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\\0')
            if not args or args[0] != b'/opt/drawio/drawio':
                continue
            if any(arg.startswith(b'--type=') for arg in args):
                continue
            pid = int(entry.name)
            os.kill(pid, signal.SIGSTOP)
            print(json.dumps({'renderer_pid': pid, 'stopped': True}), flush=True)
            raise SystemExit(0)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    time.sleep(0.02)
raise SystemExit('No active native renderer observed')
"""
EGRESS_PROBE = """
import http.client, json
connection = http.client.HTTPSConnection('example.com', timeout=15)
connection.request('GET', '/')
response = connection.getresponse()
print(json.dumps({'status': response.status, 'denial': response.getheader('x-deny-reason')}))
connection.close()
"""


class ObservedSandbox:
    """Signal native execution after the router finishes preparing guest inputs."""

    def __init__(self, sandbox: Sandbox, running: asyncio.Queue[Sandbox]) -> None:
        self.sandbox = sandbox
        self.running = running

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sandbox, name)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        if not isinstance(command, str) and list(command[:3]) == ["python3", "-I", "renderer.py"]:
            self.running.put_nowait(self.sandbox)
        return await self.sandbox.exec(
            command, working_directory=working_directory, timeout=timeout
        )


class ObservedBackend:
    """Preserve provider behavior while observing each acquired qualification sandbox."""

    def __init__(self, backend: SandboxBackend, provider: str) -> None:
        self.backend = backend
        self.provider = provider
        self.acquired: asyncio.Queue[Sandbox] = asyncio.Queue()
        self.running: asyncio.Queue[Sandbox] = asyncio.Queue()
        self.observations: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    @property
    def name(self) -> str:
        return self.backend.name

    @property
    def isolation(self) -> Isolation:
        return self.backend.isolation

    async def dispose(
        self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
    ) -> DisposalFailure | None:
        return await self.backend.dispose(key, kind=kind, instance_id=instance_id)

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        return await self.backend.dispose_scope(scope, thread_id)

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        sandbox = await self.backend.acquire(key, spec)
        try:
            if self.provider == "acas":
                probe = await sandbox.exec(
                    ["python3", "-c", EGRESS_PROBE], working_directory="/tmp", timeout=20
                )
                require(probe.exit_code == 0, "ACAS egress probe failed")
                evidence = json.loads(probe.stdout)
                require(
                    evidence["status"] == 403 and bool(evidence["denial"]),
                    "ACAS did not return an explicit policy denial",
                )
                evidence = {"closedEgress": "service-policy-denial"}
            else:
                from drawio_export_qualification import observation

                if spec.image is None:
                    raise ValueError("Docker qualification needs an immutable image")
                evidence = await asyncio.to_thread(observation, key.scope, spec.image)
            self.observations.append(evidence)
            self.acquired.put_nowait(sandbox)
            return cast(Sandbox, ObservedSandbox(sandbox, self.running))
        except BaseException:
            await self.backend.dispose(key, kind=spec.kind, instance_id=sandbox.instance_id)
            raise


async def backend_for(provider: str) -> SandboxBackend:
    """Use host-selected configuration without provisioning or importing cloud images."""
    if provider == "docker":
        from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

        return await DockerSandboxBackend.create(
            DockerSandboxConfig(
                memory="1g",
                cpus=2,
                pids_limit=256,
                cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"),
            )
        )
    from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

    return AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=os.environ["ACAS_SANDBOX_ENDPOINT"],
            subscription_id=os.environ["ACAS_SANDBOX_SUBSCRIPTION_ID"],
            resource_group=os.environ["ACAS_SANDBOX_RESOURCE_GROUP"],
            sandbox_group=os.environ["ACAS_SANDBOX_GROUP"],
            registry=os.environ.get("ACAS_SANDBOX_REGISTRY", ""),
        )
    )


async def close_backend(backend: SandboxBackend) -> None:
    """Close hosted credential clients on the event loop that opened them."""
    close = getattr(backend, "aclose", None)
    if close is not None:
        await close()


def font_diagram(family: str) -> str:
    """Exercise regular, bold, italic and bold-italic native labels in both pages."""
    document = ET.fromstring(exports.diagram())
    for cell in document.findall(".//mxCell[@vertex='1']"):
        cell.set("style", cell.get("style", "") + f"fontFamily={family};")
    for root in document.findall("./diagram/mxGraphModel/root"):
        for style in range(4):
            cell = ET.SubElement(
                root,
                "mxCell",
                {
                    "id": f"font-{style}",
                    "parent": "1",
                    "vertex": "1",
                    "value": f"Résumé Ω Ж {family} {style}",
                    "style": f"fontFamily={family};fontStyle={style};",
                },
            )
            ET.SubElement(
                cell,
                "mxGeometry",
                {
                    "as": "geometry",
                    "x": "0",
                    "y": str(260 + style * 50),
                    "width": "400",
                    "height": "40",
                },
            )
    return ET.tostring(document, encoding="unicode")


async def exercise(
    provider: str, image: str, output: Path, scope: str, *, worker: bool = False
) -> dict[str, Any]:
    """Require observed native execution before cancellation or owner-death injection."""
    backend = await backend_for(provider)
    observed = ObservedBackend(backend, provider)
    calls: list[ToolCallEnded] = []

    class Observer(SandboxObserver):
        def tool_call_ended(self, event: ToolCallEnded) -> None:
            calls.append(event)

    router = SandboxRouter([observed], min_isolation=backend.isolation, observer=Observer())
    context = make_caller_context(list_no_files, lambda: scope, lambda: "acceptance")
    report: dict[str, Any] = {}
    task: asyncio.Task[Any] | None = None
    try:
        if not worker:
            for family in FONTS:
                destination = output / family.replace(" ", "-")
                [tool] = make_drawio_tools(
                    router,
                    "fonts",
                    context,
                    make_file_system_sink(destination),
                    image=image,
                    export=DrawioExport(formats=("png", "jpg", "svg")),
                    exec_timeout_seconds=120,
                )
                answer = [item.text for item in await tool.func(xml=font_diagram(family))]
                require(answer[:2] == [COMPLETED_TEXT, "Result: created"], str(answer))
                report[family] = exports.verify_output(destination)
                for page in (1, 2):
                    svg = (destination / f"diagram-{page}.svg").read_text("utf-8")
                    require(
                        all(f"{family} {style}" in svg for style in range(4)),
                        "SVG lost a font-variant label",
                    )
                    require("Ж" in svg, "SVG lost Cyrillic text")
        while not observed.running.empty():
            observed.running.get_nowait()
        destination = output / "interrupted"
        [tool] = make_drawio_tools(
            router,
            "interrupted",
            context,
            make_file_system_sink(destination),
            image=image,
            export=DrawioExport(formats=("png", "jpg", "svg")),
            exec_timeout_seconds=120,
        )
        task = asyncio.create_task(tool.func(xml=exports.diagram()))
        sandbox = await asyncio.wait_for(observed.running.get(), 180)
        probe = await sandbox.exec(
            ["python3", "-c", STOP_RENDERER], working_directory="/tmp", timeout=50
        )
        require(probe.exit_code == 0, "Could not interrupt an active renderer: " + probe.stderr)
        report["interrupted"] = json.loads(probe.stdout)
        require(not task.done(), "Export finished before fault injection")
        if worker:
            output.mkdir(parents=True, exist_ok=True)
            ready = output / "ready.json"
            temporary = ready.with_suffix(".tmp")
            temporary.write_text(json.dumps(report["interrupted"]), encoding="utf-8")
            temporary.replace(ready)
            await task
            raise RuntimeError("Owner-death worker was not terminated")
        task.cancel()
        try:
            await asyncio.wait_for(task, 90)
        except asyncio.CancelledError:
            pass
        else:
            raise RuntimeError("Export swallowed cancellation")
        require(not destination.exists(), "Interrupted export delivered artifacts")
        require(len(calls) == len(FONTS) + 1, "Missing completed-call evidence")
        require(all(event.unclean == 0 for event in calls[:-1]), "Font export cleanup failed")
        require(calls[-1].failure == "CancelledError", "Missing cancellation evidence")
        purge = await backend.dispose_scope(scope, "acceptance")
        require(not purge.undisposed, "Cancellation recovery failed")
        if provider == "docker":
            require(not await asyncio.to_thread(owned, scope), "Cancellation left a container")
        repeated = await backend.dispose_scope(scope, "acceptance")
        require(
            not repeated.undisposed and repeated.disposed == 0, "Cancellation recovery incomplete"
        )
        report["cancellation"] = {
            "failure": calls[-1].failure,
            "uncleanAtCallEnd": calls[-1].unclean,
            "recoveredSandboxes": purge.disposed,
            "repeatedPurgeEmpty": True,
            "artifactsDelivered": False,
        }
        report["observations"] = observed.observations
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        purge = await backend.dispose_scope(scope, "acceptance")
        try:
            require(not purge.undisposed, "Acceptance cleanup incomplete")
        finally:
            await close_backend(backend)
    return report


async def owner_death(provider: str, image: str, output: Path) -> dict[str, Any]:
    """Kill a separate host process, then recover only its scope using a fresh backend."""
    scope = "drawio-death-" + uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    backend = await backend_for(provider)
    process = None
    try:
        with (output / "worker.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--backend",
                provider,
                "--image",
                image,
                "--output",
                str(output),
                "--worker-scope",
                scope,
                stdout=log,
                stderr=log,
            )
            async with asyncio.timeout(240):
                while not (output / "ready.json").exists():
                    require(process.returncode is None, "Owner-death worker failed; see worker.log")
                    await asyncio.sleep(0.1)
            observed = json.loads((output / "ready.json").read_text("utf-8"))
            require(observed["stopped"] is True, "Worker did not observe a native renderer")
            process.kill()
            await process.wait()
        purge = await backend.dispose_scope(scope, "acceptance")
        require(purge.disposed == 1 and not purge.undisposed, "Owner-death recovery failed")
        if provider == "docker":
            require(not await asyncio.to_thread(owned, scope), "Owner-death container remains")
        repeated = await backend.dispose_scope(scope, "acceptance")
        require(repeated.disposed == 0 and not repeated.undisposed, "Recovery is not complete")
        require(not (output / "interrupted").exists(), "Killed owner delivered artifacts")
        return {
            "activeRenderer": observed,
            "recoveredSandboxes": purge.disposed,
            "automaticCleanup": False,
            "repeatedPurgeEmpty": True,
        }
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        purge = await backend.dispose_scope(scope, "acceptance")
        try:
            require(not purge.undisposed, "Owner-death fallback cleanup failed")
        finally:
            await close_backend(backend)


async def main_async(args: argparse.Namespace) -> None:
    """Keep reports local and omit cloud resource identifiers."""
    if args.worker_scope:
        await exercise(args.backend, args.image, args.output, args.worker_scope, worker=True)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    backend = await backend_for(args.backend)
    try:
        observed = ObservedBackend(backend, args.backend)
        baseline = await exports.check(args.image, args.output / "exports", observed, emit=False)
    finally:
        await close_backend(backend)
    report = {
        "backend": args.backend,
        "image": args.image if args.backend == "docker" else "host-selected imported image",
        "packages": {
            name: version(name)
            for name in (
                "maf-sandbox",
                "maf-sandbox-drawio",
                f"maf-sandbox-{args.backend}",
            )
        },
        "exports": baseline,
        "exportObservations": observed.observations,
    }
    report["acceptance"] = await exercise(
        args.backend, args.image, args.output / "fonts", "drawio-acceptance-" + uuid4().hex
    )
    report["ownerDeath"] = await owner_death(args.backend, args.image, args.output / "owner-death")
    report["artifacts"] = {
        path.relative_to(args.output).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(args.output.rglob("*"))
        if path.is_file() and path.suffix in {".drawio", ".png", ".jpg", ".svg"}
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> None:
    """Read the prepared runtime and optional host configuration file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("docker", "acas"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--worker-scope", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    if not __debug__:
        raise RuntimeError("Export qualification requires Python assertions")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
