"""Measure the shipped host-tools transport against real sbx lifecycle boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from maf_sandbox import (
    Capability,
    Egress,
    HostToolRegistry,
    HostToolRun,
    Identity,
    SandboxKey,
    SandboxSpec,
    SourceIntegrity,
    guest_run_layout,
    host_tool_calls_over_exec,
    host_tool_shim,
    sandbox_tool,
)
from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig

OUTPUT = Path(os.environ.get("SBX_MEASUREMENT_OUTPUT", "sbx-host-tools.jsonl"))
WRITE_LOCK = threading.Lock()
START = time.monotonic()


def record(event: str, **data: Any) -> None:
    """Append one flushed evidence record from either observer thread."""
    line = json.dumps({"event": event, "seconds": round(time.monotonic() - START, 3), **data})
    with WRITE_LOCK:
        with OUTPUT.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)


def observe(name: str, stop: threading.Event, label: str) -> None:
    """Observe state without starting a guest exec or relying on the asyncio loop."""
    while not stop.wait(5):
        try:
            result = subprocess.run(
                ["sbx", "ls", "--json"], capture_output=True, text=True, timeout=10, check=True
            )
            rows = json.loads(result.stdout).get("sandboxes", [])
            row = next((row for row in rows if row.get("name") == name), {})
            record(
                "state",
                case=label,
                state={k: v for k, v in row.items() if k in {"status", "state", "running"}},
                fields=sorted(row),
            )
        except Exception as error:
            record("state_error", case=label, error=str(error))


async def measure(mode: str, image: str | None, keepalive: bool = False) -> None:
    """Run one case in a fresh sandbox and retain its observed outcome."""
    label = f"{image or 'default'}:{mode}:keepalive={keepalive}"
    key = SandboxKey(scope="measure-1614", thread_id=uuid4().hex, agent_id="probe")
    backend = SbxSandboxBackend(
        SbxSandboxConfig(
            workspace_root=Path(".sbx-measure-workspaces"),
            name_prefix="m1614",
            cpus=2,
            memory="1g",
            create_timeout_seconds=120,
        )
    )
    assert Capability.HOST_TOOLS not in backend.declarations.capabilities
    layout = guest_run_layout("/maf-sandbox/work/probe")
    spec = SandboxSpec(kind="measurement", image=image, egress=Egress.CLOSED)
    stop = threading.Event()
    monitor: threading.Thread | None = None
    held: asyncio.Task[Any] | None = None
    effects: list[str] = []
    record("begin", case=label)
    try:
        sandbox = await backend.acquire(key, spec)
        check = await sandbox.exec(
            ["python3", "--version"], working_directory=spec.work_dir or ".", timeout=10
        )
        record("runtime", case=label, exit_code=check.exit_code, stdout=check.stdout)
        if check.exit_code:
            raise RuntimeError("template has no usable Python runtime")
        registry = HostToolRegistry()

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        async def async_tool() -> str:
            record("tool_started", case=label)
            if mode == "slow_async":
                await asyncio.sleep(45)
            effects.append("effect")
            record("tool_effect", case=label, count=len(effects))
            return "answer"

        @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
        def sync_tool() -> str:
            record("tool_started", case=label)
            time.sleep(45)
            effects.append("effect")
            record("tool_effect", case=label, count=len(effects))
            return "answer"

        registry.register(sync_tool if mode == "slow_sync" else async_tool, name="probe")
        delay = 45 if mode == "idle" else 120 if mode in {"timeout", "cancel"} else 0
        program = (
            "import os, time\nfrom pathlib import Path\nimport maf_host_tools\n"
            "Path('pid').write_text(str(os.getpid()))\n"
            "Path('identity').write_text(Path('/proc/self/stat').read_text())\n"
            "print('started', flush=True)\n"
            f"time.sleep({delay})\n"
            "answer = maf_host_tools.call('probe')\n"
            "Path('answer').write_text(answer)\n"
            "print('received:' + answer, flush=True)\n"
        )
        await sandbox.write_file(layout.program, program, working_directory=".")
        await sandbox.write_file(
            layout.shim, host_tool_shim(call_timeout=90), working_directory="."
        )
        if keepalive:
            held = asyncio.create_task(
                sandbox.exec(
                    ["sh", "-c", f"echo ready > {layout.directory}/held; sleep 100"],
                    working_directory=".",
                    timeout=105,
                )
            )
            for _ in range(100):
                if await sandbox.stat_file(f"{layout.directory}/held", working_directory="."):
                    break
                if held.done():
                    raise RuntimeError(f"keepalive ended early: {await held}")
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("keepalive failed to start")
            record("keepalive_ready", case=label)
        monitor = threading.Thread(target=observe, args=(sandbox.name, stop, label), daemon=True)
        monitor.start()
        began = time.monotonic()
        transport = asyncio.create_task(
            host_tool_calls_over_exec(
                sandbox,
                HostToolRun(registry, key=key),
                layout,
                timeout=6 if mode == "timeout" else 70,
                interpreter="python3",
            )
        )
        if mode == "cancel":
            await asyncio.sleep(6)
            transport.cancel()
        try:
            result = await transport
            record(
                "result",
                case=label,
                elapsed=round(time.monotonic() - began, 3),
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                effects=len(effects),
            )
        except (Exception, asyncio.CancelledError) as error:
            record(
                "transport_error",
                case=label,
                elapsed=round(time.monotonic() - began, 3),
                error_type=type(error).__name__,
                detail=str(error),
                effects=len(effects),
                signal=getattr(error, "signal", None),
                reach=getattr(error, "reach", None),
            )
        stop.set()
        await asyncio.to_thread(monitor.join, 12)
        if held is not None:
            record("keepalive_after_transport", case=label, still_running=not held.done())
            held.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await held
            held = None
        answer = await sandbox.stat_file(f"{layout.work}/answer", working_directory=".")
        record("answer_file", case=label, exists=answer is not None)
        pid_entry = await sandbox.stat_file(f"{layout.work}/pid", working_directory=".")
        if pid_entry is not None:
            identity = await sandbox.read_file(
                f"{layout.work}/identity", working_directory=".", max_bytes=4096
            )
            record("process_before", case=label, stat=identity.decode())
            pid = (
                await sandbox.read_file(f"{layout.work}/pid", working_directory=".", max_bytes=32)
            ).decode()
            if not pid.isdigit():
                raise ValueError("invalid measured PID")
            probe = await sandbox.exec(
                [
                    "sh",
                    "-c",
                    f"if [ -r /proc/{pid}/stat ]; then cat /proc/{pid}/stat; else echo absent; fi",
                ],
                working_directory=".",
                timeout=10,
            )
            record("process_after", case=label, exit_code=probe.exit_code, stdout=probe.stdout)
        transport_entry = await sandbox.stat_file(layout.calls, working_directory=".")
        record(
            "transport_files_after", case=label, calls_directory_exists=transport_entry is not None
        )
    except Exception as error:
        record(
            "setup_or_cleanup_error", case=label, error_type=type(error).__name__, detail=str(error)
        )
        raise
    finally:
        stop.set()
        if held is not None:
            held.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await held
        if monitor is not None:
            await asyncio.to_thread(monitor.join, 12)
        failure = await backend.dispose(key)
        record("disposed", case=label, failure=None if failure is None else str(failure))
        if failure is not None:
            raise RuntimeError("measurement sandbox disposal failed")


async def main() -> None:
    """Measure controls and keepalive experiments without declaring capabilities."""
    if OUTPUT.exists():
        raise FileExistsError("use a fresh output file for each measurement")
    version = subprocess.run(
        ["sbx", "version"], capture_output=True, text=True, timeout=10, check=True
    )
    record(
        "environment",
        sbx=version.stdout.strip(),
        platform=platform.platform(),
        revision=os.environ.get("GITHUB_SHA"),
    )
    cases = [
        ("short", None, False),
        ("short", "maf-sbx-e2e-agent:local", False),
        ("short", "maf-sbx-e2e-root:local", False),
        ("idle", "maf-sbx-e2e-agent:local", False),
        ("slow_async", "maf-sbx-e2e-agent:local", False),
        ("slow_sync", "maf-sbx-e2e-agent:local", False),
        ("idle", "maf-sbx-e2e-agent:local", True),
        ("slow_async", "maf-sbx-e2e-agent:local", True),
        ("slow_sync", "maf-sbx-e2e-agent:local", True),
        ("timeout", "maf-sbx-e2e-agent:local", True),
        ("cancel", "maf-sbx-e2e-agent:local", True),
    ]
    for mode, image, keepalive in cases:
        await measure(mode, image, keepalive)
    record("complete", cases=len(cases), capability_declared=False)


if __name__ == "__main__":
    asyncio.run(main())
