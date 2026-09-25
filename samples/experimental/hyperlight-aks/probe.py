"""A scoped application exercises the real adapter under the pod's PID 1 supervisor."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    Egress,
    SandboxQueuedTimeout,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    Selection,
)
from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools
from maf_sandbox_hyperlight import (
    RUNTIME_INSTRUCTIONS,
    HyperlightPodConfig,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
)


def report(event: str, **values: object) -> None:
    """Emit host-established evidence separately from guest output."""
    print(json.dumps({"event": event, **values}), flush=True)


def resident_bytes(pid: int) -> int:
    """Read process RSS separately from aggregate cgroup accounting."""
    fields = dict(
        line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
    )
    return int(fields["VmRSS"].split()[0]) * 1024


async def main(mode: str) -> None:
    """The host chooses a failure mode; guest source never controls the pod binding."""
    binding = HyperlightPodConfig.from_environment()
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            pod=binding,
            max_worker_memory_bytes=None,
            file_outputs=mode == "files",
            max_output_bytes=1024 if mode == "output-limit" else 1024**2,
        )
    )
    key = binding.key
    if mode.startswith("codeact"):
        await codeact(backend, binding, mode)
        await backend.aclose()
        report("complete", mode=mode)
        return
    if mode == "files":
        await files(backend, binding)
        await backend.aclose()
        report("complete", mode=mode)
        return
    if mode == "allowlist":
        await network(backend, binding)
        await backend.aclose()
        report("complete", mode=mode)
        return
    spec = SandboxSpec(kind=binding.kind, work_dir=None, requires=frozenset({Capability.RUN_CODE}))
    started = time.monotonic()
    sandbox = await backend.acquire(key, spec)
    first = time.monotonic()
    answer = await sandbox.run_code("print(6 * 7)", timeout=5)
    assert answer.stdout.strip() == "42" and answer.exit_code == 0
    report(
        "initialized", acquire_seconds=first - started, first_call_seconds=time.monotonic() - first
    )
    if mode == "timeout":
        await sandbox.run_code("while True: pass", timeout=1)
        raise AssertionError("infinite guest survived its deadline")
    if mode == "output-limit":
        await sandbox.run_code("print('x' * 2048)", timeout=5)
        raise AssertionError("native output exceeded its budget")
    if mode in {"worker-death", "native-hang"}:
        worker = getattr(sandbox, "worker").process
        os.kill(worker.pid, getattr(signal, "SIGKILL" if mode == "worker-death" else "SIGSTOP"))
        await sandbox.run_code("print('unreachable')", timeout=1)
        raise AssertionError("native failure returned normally")
    if mode == "cancel":
        task = asyncio.create_task(sandbox.run_code("while True: pass", timeout=30))
        await asyncio.sleep(0.5)
        task.cancel()
        await task
        raise AssertionError("cancelled guest returned normally")
    if mode == "owner-death":
        os.kill(os.getpid(), getattr(signal, "SIGKILL"))
    if mode == "oom":
        retained = []
        while True:
            retained.append(bytearray(64 * 1024**2))
    if mode == "hold":
        await sandbox.run_code("while True: pass", timeout=120)
        raise AssertionError("held guest returned normally")
    assert mode == "positive"
    async with backend.call_admission(key, spec, owner="first", timeout=5):
        try:
            async with backend.call_admission(key, spec, owner="queued", timeout=0.02):
                raise AssertionError("overlapping call admitted")
        except SandboxQueuedTimeout:
            pass
    try:
        await sandbox.run_code("#" * (1024**2 + 1), timeout=5)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized source accepted")
    denied = await sandbox.run_code("http_get('http://127.0.0.1/denied')", timeout=5)
    assert denied.exit_code != 0
    previous_id = sandbox.instance_id
    await sandbox.run_code("private_value = 987", timeout=5)
    assert "987" in (await sandbox.run_code("print(private_value)", timeout=5)).stdout
    reset_started = time.monotonic()
    await sandbox.reset(timeout=5)
    reset_seconds = time.monotonic() - reset_started
    assert sandbox.instance_id != previous_id
    assert (
        "False" in (await sandbox.run_code("print('private_value' in globals())", timeout=5)).stdout
    )
    assert await backend.dispose(key, kind=binding.kind, instance_id=previous_id) is None
    assert "alive" in (await sandbox.run_code("print('alive')", timeout=5)).stdout
    result = await sandbox.run_code("raise ValueError('ordinary')", timeout=5)
    assert result.exit_code != 0
    assert "reused" in (await sandbox.run_code("print('reused')", timeout=5)).stdout
    for wrong in (
        replace(key, scope="other"),
        replace(key, thread_id="other"),
        replace(key, agent_id="other"),
    ):
        try:
            await backend.acquire(wrong, spec)
        except HyperlightWorkerError:
            pass
        else:
            raise AssertionError("cross-scope acquire succeeded")
        assert await backend.dispose(wrong) is not None
    policy = HyperlightSandboxBackend(
        HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None, max_output_bytes=100)
    )
    try:
        await policy.acquire(key, spec)
    except HyperlightWorkerError:
        pass
    else:
        raise AssertionError("pod accepted a different execution policy")
    report(
        "positive",
        identity_checks=5,
        reset=True,
        ordinary_exception_reuse=True,
        memory_current=int(Path("/sys/fs/cgroup/memory.current").read_text()),
        memory_peak=int(Path("/sys/fs/cgroup/memory.peak").read_text()),
        reset_seconds=reset_seconds,
        worker_rss_bytes=resident_bytes(getattr(sandbox, "worker").process.pid),
        application_rss_bytes=resident_bytes(os.getpid()),
        queued_expiry_preserves_worker=True,
        closed_network=True,
    )
    assert await backend.dispose(key, kind=binding.kind, instance_id=sandbox.instance_id) is None
    replacement = await backend.acquire(key, spec)
    assert replacement.instance_id != sandbox.instance_id
    assert (
        "False"
        in (await replacement.run_code("print('private_value' in globals())", timeout=5)).stdout
    )
    await backend.aclose()
    report("complete", fresh_worker_after_disposal=True, total_seconds=time.monotonic() - started)


async def files(backend: HyperlightSandboxBackend, binding: HyperlightPodConfig) -> None:
    """Verify bounded binary delivery and reset using the public file capability contract."""
    spec = SandboxSpec(
        kind=binding.kind,
        work_dir="/output",
        requires=frozenset({Capability.RUN_CODE, Capability.FILES_OUT, Capability.FILES_LIST}),
    )
    async with backend.call_admission(binding.key, spec, owner="files", timeout=30):
        sandbox = await backend.acquire(binding.key, spec)
        result = await sandbox.run_code(
            "with open('/output/result.bin', 'wb') as f:\n    f.write(bytes(range(256)))", timeout=5
        )
        assert result.exit_code == 0, result.stderr
        assert len(await sandbox.list_dir(".", working_directory=".")) == 1
        try:
            await sandbox.read_file("result.bin", working_directory=".", max_bytes=255)
        except SandboxTransferCapExceeded:
            pass
        else:
            raise AssertionError("file transfer exceeded its bound")
        assert await sandbox.read_file("result.bin", working_directory=".", max_bytes=256) == bytes(
            range(256)
        )
        await sandbox.reset(timeout=5)
        assert await sandbox.list_dir(".", working_directory=".") == ()
        assert await backend.dispose(binding.key) is None
    report("files", binary_delivery=True, transfer_limit=True, reset_cleanup=True)


async def network(backend: HyperlightSandboxBackend, binding: HyperlightPodConfig) -> None:
    """Use a host-owned loopback server so the probe needs no external HTTP service."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"hyperlight-network-proof")

    with HTTPServer(("127.0.0.1", 80), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            spec = SandboxSpec(
                kind=binding.kind,
                work_dir=None,
                requires=frozenset({Capability.RUN_CODE}),
                egress=Egress.ALLOWLIST,
                egress_allow=("localhost",),
            )
            sandbox = await backend.acquire(binding.key, spec)
            allowed = await sandbox.run_code(
                "print(http_get('http://localhost/allowed')['body'])", timeout=5
            )
            assert allowed.exit_code == 0 and "hyperlight-network-proof" in allowed.stdout
            denied = await sandbox.run_code("http_get('http://127.0.0.1/denied')", timeout=5)
            assert denied.exit_code != 0 and hits == ["/allowed"]
            await sandbox.reset(timeout=5)
            restored = await sandbox.run_code(
                "print(http_get('http://localhost/restored')['status'])", timeout=5
            )
            assert restored.stdout.strip() == "200" and hits == ["/allowed", "/restored"]
            report("allowlist", exact_host=True, reset_preserves_policy=True)
        finally:
            server.shutdown()
            thread.join(timeout=2)


async def codeact(
    backend: HyperlightSandboxBackend, binding: HyperlightPodConfig, mode: str
) -> None:
    """Exercise the real tool contract with reset between calls."""
    key = binding.key
    if mode.startswith("codeact"):
        selection = Selection.FIXED if mode == "codeact-fixed" else Selection.PER_SPEC
        router = SandboxRouter([backend], selection=selection, min_cleanup=Cleanup.RESET)

        async def no_files(store: object):
            return []

        context = CallerContext(
            current_scope=lambda: key.scope,
            current_thread_id=lambda: key.thread_id,
            list_files=no_files,
        )
        tool = make_codeact_tools(
            router, key.agent_id, context, runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS)
        )[0]
        function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool
        result = await function(code="transient = 7; print('codeact-ok')")
        text = (
            str(result) if isinstance(result, str) else "\n".join(str(item.text) for item in result)
        )
        assert "codeact-ok" in text, text
        result = await function(code="print('transient' in globals())")
        text = (
            str(result) if isinstance(result, str) else "\n".join(str(item.text) for item in result)
        )
        assert "False" in text, text
        report("codeact", selection=str(selection), reset_between_calls=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "positive"))
