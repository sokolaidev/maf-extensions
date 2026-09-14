"""Real pipes and worker processes exercise bounded I/O without requiring a hypervisor."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_hyperlight import (
    HyperlightOutputLimitExceeded,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
    _backend,
    _process,
    _windows,
)
from maf_sandbox_hyperlight._wire import decode, encode

KEY = SandboxKey("process-test", "conversation", "agent")
SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


def test_native_base_exception_is_reported_before_worker_exit():
    script = """import sys
from types import SimpleNamespace
from maf_sandbox_hyperlight import _worker
if sys.platform == 'linux':
    from maf_sandbox_hyperlight import _linux
    _linux.check_kvm = lambda: None
class NativePanic(BaseException):
    pass
def fail(**kwargs):
    raise NativePanic('native failure')
_worker.version = lambda package: '0.7.0'
_worker.ctypes.WinDLL = lambda name: None
_worker.importlib.import_module = lambda name: SimpleNamespace(Sandbox=fail)
_worker.main()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-u", "-c", script],
        input=encode({"op": "init", "targets": [], "output_limit": 1024})
        + encode({"op": "run", "code": "pass"}),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert decode(result.stdout) == {"error": "native", "detail": "native failure"}


class NoJob(_windows.Job):
    def __init__(self, memory_limit: int) -> None:
        pass

    def assign(self, pid: int) -> None:
        pass

    def ready(self, *, deadline: float) -> None:
        pass

    def close(self, *, deadline: float | None = None) -> None:
        pass


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_worker_environment_uses_platform_name_semantics(
    system: str, monkeypatch: pytest.MonkeyPatch
):
    source = {
        "PATH": "runtime-path",
        "HOME": "runtime-home",
        "XDG_CACHE_HOME": "runtime-cache",
        "TMPDIR": "runtime-temp",
        "LANG": "runtime-language",
        "LC_ALL": "runtime-locale",
        "home": "application-secret",
        "path": "application-secret",
        "lang": "application-secret",
        "Home": "application-secret",
        "xdg_cache_home": "application-secret",
        "tmpdir": "application-secret",
        "lc_all": "application-secret",
        "SystemRoot": "windows-root",
        "LocalAppData": "windows-cache",
        "Temp": "windows-temp",
        "APPLICATION_TOKEN": "application-secret",
        "HYPERLIGHT_MAX_SURROGATES": "20",
    }
    expected_names = (
        {"PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "LANG", "LC_ALL"}
        if system == "linux"
        else {"PATH", "path", "SystemRoot", "LocalAppData", "Temp"}
    )
    environments = []

    def intercept(*args, env, **kwargs):
        environments.append(env)
        raise OSError("intercepted worker spawn")

    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(os, "environ", source)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: ".")
    monkeypatch.setattr(_process, "create_job", lambda config: NoJob(0))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", intercept)
    with pytest.raises(OSError, match="intercepted worker spawn"):
        _process.Worker(HyperlightSandboxConfig())
    assert environments == [
        {**{name: source[name] for name in expected_names}, "HYPERLIGHT_MAX_SURROGATES": "0"}
    ]


@pytest.fixture
def process_backend(monkeypatch: pytest.MonkeyPatch):
    if sys.platform != "win32":
        monkeypatch.setattr(
            _process, "create_job", lambda config: NoJob(config.max_worker_memory_bytes)
        )
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    mode = ["normal"]
    processes: list[subprocess.Popen[bytes]] = []
    original = _process.Worker.__init__

    def record(self: _process.Worker, config: HyperlightSandboxConfig) -> None:
        original(self, config)
        processes.append(self.process)

    monkeypatch.setattr(_process.Worker, "__init__", record)
    monkeypatch.setattr(
        _process.Worker,
        "command",
        staticmethod(
            lambda: [
                sys.executable,
                "-I",
                "-u",
                str(Path(__file__).with_name("worker_fixture.py")),
                mode[0],
            ]
        ),
    )
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(startup_timeout=1, cleanup_timeout=2, max_output_bytes=1024)
    )
    yield backend, mode, processes
    asyncio.run(backend.aclose())
    assert all(process.poll() is not None for process in processes)
    assert not backend._sandboxes


def test_stderr_is_drained_but_only_bounded_diagnostics_are_retained(process_backend):
    backend, _, _ = process_backend

    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await backend.acquire(KEY, SPEC))
        assert (await sandbox.run_code("diagnostics", timeout=3)).stdout == "diagnostics"
        assert len(sandbox.worker._stderr) == 64 * 1024
        assert (await sandbox.run_code("next", timeout=1)).stdout == "next"

    asyncio.run(check())


@pytest.mark.parametrize(
    "code, exception",
    [
        ("hang", TimeoutError),
        ("oversize", HyperlightWorkerError),
        ("die", HyperlightWorkerError),
        ("limits", HyperlightOutputLimitExceeded),
    ],
)
def test_failure_reaps_process_and_closes_pipes(process_backend, code, exception):
    backend, _, processes = process_backend

    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await backend.acquire(KEY, SPEC))
        started = time.monotonic()
        with pytest.raises(exception):
            await sandbox.run_code(code, timeout=0.1)
        assert time.monotonic() - started < 3
        assert processes[0].poll() is not None
        assert all(
            stream is not None and stream.closed
            for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr)
        )
        assert not sandbox.worker._drainer.is_alive()
        replacement = await backend.acquire(KEY, SPEC)
        assert replacement.instance_id != sandbox.instance_id
        assert (await replacement.run_code("recovered", timeout=1)).stdout == "recovered"

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["hang_init", "fail_init"])
def test_startup_failure_is_bounded_and_leaves_no_registry_entry(process_backend, mode):
    backend, selected, processes = process_backend
    selected[0] = mode
    started = time.monotonic()
    with pytest.raises((TimeoutError, HyperlightWorkerError)):
        asyncio.run(backend.acquire(KEY, SPEC))
    assert time.monotonic() - started < 4
    assert not backend._sandboxes
    assert processes[0].poll() is not None


def test_cancellation_kills_a_real_blocked_reader(process_backend):
    backend, _, processes = process_backend

    async def check():
        sandbox = await backend.acquire(KEY, SPEC)
        running = asyncio.create_task(sandbox.run_code("hang", timeout=5))
        await asyncio.sleep(0.05)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert processes[0].poll() is not None

    asyncio.run(check())


def test_blocked_stdin_is_interrupted_by_the_same_deadline(process_backend):
    backend, selected, processes = process_backend
    selected[0] = "stop_reading"

    async def check():
        sandbox = await backend.acquire(KEY, SPEC)
        with pytest.raises(TimeoutError):
            await sandbox.run_code("x" * backend.config.max_code_bytes, timeout=0.1)
        assert processes[0].poll() is not None

    asyncio.run(check())


def test_job_assignment_failure_reaps_process_and_closes_pipes(
    process_backend, monkeypatch: pytest.MonkeyPatch
):
    backend, _, _ = process_backend
    spawned: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = cast("subprocess.Popen[bytes]", original(*args, **kwargs))
        spawned.append(process)
        return process

    class RefusedJob(NoJob):
        def assign(self, pid: int) -> None:
            raise OSError("job assignment refused")

    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.setattr(
        _process, "create_job", lambda config: RefusedJob(config.max_worker_memory_bytes)
    )
    with pytest.raises(OSError, match="job assignment"):
        asyncio.run(backend.acquire(KEY, SPEC))
    assert not backend._sandboxes
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert all(
        stream is not None and stream.closed
        for stream in (spawned[0].stdin, spawned[0].stdout, spawned[0].stderr)
    )
