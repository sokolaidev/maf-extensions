"""Opt-in WHP/KVM checks against the pinned guest: set MAF_HYPERLIGHT_LIVE=1."""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

import pytest
from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    Egress,
    ListedFile,
    SandboxKey,
    SandboxQueuedTimeout,
    SandboxRouter,
    SandboxSpec,
    Selection,
)
from maf_sandbox.conformance import assert_instance_disposal_conformance

from maf_sandbox_hyperlight import (
    RUNTIME_INSTRUCTIONS,
    HyperlightOutputLimitExceeded,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
    _backend,
    _linux,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1" or sys.platform not in {"win32", "linux"},
    reason="requires opt-in and Windows WHP or Linux KVM",
)
KEY = SandboxKey("hyperlight-live", "runtime", "agent")
SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


def _said(answer) -> str:
    """One call's text, whichever parts the result contract rendered it into."""
    if isinstance(answer, str):
        return answer
    return chr(10).join(str(item.text) for item in answer)


@pytest.fixture
def live_backend():
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(linux_cgroup_root=os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT"))
    )
    yield backend
    asyncio.run(backend.aclose())
    assert not backend._sandboxes


def test_real_python_results_errors_persistence_and_reset(live_backend):
    async def check():
        sandbox = await live_backend.acquire(KEY, SPEC)
        result = await sandbox.run_code(
            "import sys\nanswer = 42\nprint(answer)\nprint('diagnostic', file=sys.stderr)",
            timeout=5,
        )
        assert result.stdout == "42\n" and result.stderr == "diagnostic\n" and result.exit_code == 0
        failure = await sandbox.run_code("raise ValueError('guest failure')", timeout=5)
        assert failure.exit_code == 1 and "guest failure" in failure.stderr
        assert (await sandbox.run_code("print(answer)", timeout=5)).stdout == "42\n"
        await sandbox.run_code("import builtins\nbuiltins.saved = 99", timeout=5)
        previous = sandbox.instance_id
        await sandbox.reset(timeout=5)
        assert sandbox.instance_id != previous
        result = await sandbox.run_code(
            "import builtins\nprint('answer' in globals(), hasattr(builtins, 'saved'))", timeout=5
        )
        assert result.stdout == "False False\n"
        assert await live_backend.dispose(KEY, instance_id=previous) is None
        assert (await sandbox.run_code("print('replacement')", timeout=5)).stdout == "replacement\n"

    asyncio.run(check())


def test_no_environment_or_filesystem_channel(live_backend, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAF_HYPERLIGHT_SECRET", "must-never-reach-the-guest")

    async def check():
        sandbox = await live_backend.acquire(KEY, SPEC)
        result = await sandbox.run_code(
            "import os\nprint(os.environ.get('MAF_HYPERLIGHT_SECRET', 'absent'))\nfor path in ('/input/secret', '/output/created', 'created'):\n    try:\n        open(path, 'w').write('data')\n        print('unexpected write')\n    except OSError:\n        print('blocked')",
            timeout=5,
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout == "absent\nblocked\nblocked\nblocked\n"

    asyncio.run(check())


def test_real_flat_outputs_are_binary_and_reset_before_reuse():
    from maf_sandbox import SandboxTransferCapExceeded

    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            file_outputs=True, linux_cgroup_root=os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT")
        )
    )
    spec = replace(SPEC, work_dir="/output", requires=SPEC.requires | {Capability.FILES_OUT})

    async def check():
        try:
            async with backend.call_admission(KEY, spec, owner="files", timeout=30):
                sandbox = await backend.acquire(KEY, spec)
                result = await sandbox.run_code(
                    "import os\nfor operation in (lambda: os.symlink('/output/result.bin', '/output/link'), lambda: os.mkdir('/output/nested')):\n    try:\n        operation()\n    except (OSError, AttributeError):\n        pass\n    else:\n        raise RuntimeError('unexpected guest link/directory creation')\nwith open('/output/result.bin', 'wb') as f:\n    f.write(bytes(range(256)))",
                    timeout=5,
                )
                assert result.exit_code == 0, result.stderr
                assert (
                    await sandbox.stat_file("result.bin", working_directory=".")
                ).size_bytes == 256
                with pytest.raises(SandboxTransferCapExceeded):
                    await sandbox.read_file("result.bin", working_directory=".", max_bytes=255)
                assert await sandbox.read_file(
                    "result.bin", working_directory=".", max_bytes=256
                ) == bytes(range(256))
                await sandbox.reset(timeout=5)
                assert await sandbox.stat_file("result.bin", working_directory=".") is None
                result = await sandbox.run_code("print('next')", timeout=5)
                assert result.stdout == "next\n"
        finally:
            await backend.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("cancel", [False, True])
def test_infinite_guest_program_is_reaped_and_queue_does_not_enter(live_backend, cancel):
    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await live_backend.acquire(KEY, SPEC))
        process = sandbox.worker.process
        started = time.monotonic()
        running = asyncio.create_task(
            sandbox.run_code("while True:\n    pass", timeout=5 if cancel else 0.3)
        )
        await asyncio.sleep(0.05)
        with pytest.raises(SandboxQueuedTimeout):
            await sandbox.run_code("print('must not run')", timeout=0.04)
        if cancel:
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await running
        assert time.monotonic() - started < 4
        assert process.poll() is not None and not sandbox.worker._drainer.is_alive()
        fresh = await live_backend.acquire(KEY, SPEC)
        assert fresh.instance_id != sandbox.instance_id
        assert (await fresh.run_code("print(6 * 7)", timeout=5)).stdout == "42\n"

    asyncio.run(check())


def test_native_output_limit_retires_worker(live_backend):
    live_backend.config = replace(live_backend.config, max_output_bytes=1024)

    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await live_backend.acquire(KEY, SPEC))
        with pytest.raises(HyperlightOutputLimitExceeded):
            await sandbox.run_code("print('é' * 1024)", timeout=5)
        assert sandbox.worker.process.poll() is not None

    asyncio.run(check())


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:
        self.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hyperlight-network-proof")

    def do_POST(self) -> None:
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_closed_and_exact_host_allowlist_reach_only_the_named_host(live_backend):
    Handler.hits = []
    with HTTPServer(("127.0.0.1", 80), Handler) as server:
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()

        async def check():
            closed = await live_backend.acquire(KEY, SPEC)
            denied = await closed.run_code("http_get('http://127.0.0.1/probe')", timeout=5)
            assert denied.exit_code != 0 and not Handler.hits
            assert await live_backend.dispose(KEY) is None
            spec = replace(SPEC, egress=Egress.ALLOWLIST, egress_allow=("127.0.0.1",))
            allowed = await live_backend.acquire(KEY, spec)
            result = await allowed.run_code(
                "print(http_get('http://127.0.0.1/allowed')['body'])\nprint(http_post('http://127.0.0.1/posted', body='hello')['status'])",
                timeout=5,
            )
            assert result.exit_code == 0, result.stderr
            assert "hyperlight-network-proof" in result.stdout and "200" in result.stdout
            assert Handler.hits == ["/allowed", "/posted"]
            denied = await allowed.run_code("http_get('http://localhost/off-list')", timeout=5)
            assert denied.exit_code != 0
            assert Handler.hits == ["/allowed", "/posted"]
            await allowed.reset(timeout=5)
            assert (
                await allowed.run_code(
                    "print(http_get('http://127.0.0.1/after-reset')['status'])", timeout=5
                )
            ).stdout == "200\n"

        try:
            asyncio.run(check())
        finally:
            server.shutdown()
            serving.join(timeout=3)


def test_instance_disposal_against_real_worker_liveness(live_backend):
    async def check():
        target = cast("_backend._HyperlightSandbox", await live_backend.acquire(KEY, SPEC))
        sibling = cast(
            "_backend._HyperlightSandbox",
            await live_backend.acquire(KEY, replace(SPEC, kind="sibling")),
        )
        processes = {sandbox.instance_id: sandbox.worker.process for sandbox in (target, sibling)}

        async def exists(identity: str) -> bool:
            return processes[identity].poll() is None

        await assert_instance_disposal_conformance(
            live_backend, KEY, SPEC.kind, target.instance_id, [sibling.instance_id], exists
        )
        assert (await sibling.run_code("print('survived')", timeout=5)).stdout == "survived\n"
        purge = await live_backend.dispose_scope(KEY.scope, KEY.thread_id)
        assert purge.disposed == 1 and purge.undisposed is None
        assert sibling.worker.process.poll() is not None

    asyncio.run(check())


@pytest.mark.parametrize("selection", list(Selection))
def test_codeact_runtime_uses_real_guest_and_resets_between_calls(live_backend, selection):
    from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools

    router = SandboxRouter([live_backend], selection=selection, min_cleanup=Cleanup.RESET)

    async def no_files(store: object) -> list[ListedFile]:
        return []

    context = CallerContext(
        current_scope=lambda: KEY.scope,
        current_thread_id=lambda: KEY.thread_id,
        list_files=no_files,
    )
    tool = make_codeact_tools(
        router, KEY.agent_id, context, runtime=CodeactRuntime(RUNTIME_INSTRUCTIONS)
    )[0]
    function = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    async def check():
        first = await function(
            code="import json, math\nanswer = math.factorial(3) * 7\nprint(json.dumps({'answer': answer}))"
        )
        assert "42" in _said(first)
        identity = next(iter(live_backend._sandboxes.values())).instance_id
        second = await function(code="print('answer' in globals())")
        assert "False" in _said(second)
        assert next(iter(live_backend._sandboxes.values())).instance_id != identity
        assert len(live_backend._sandboxes) == 1

    asyncio.run(check())


def test_a_second_process_cannot_serve_or_report_a_successful_scope_purge(live_backend):
    asyncio.run(live_backend.acquire(KEY, SPEC))
    script = """import asyncio
from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightWorkerError
async def check():
    backend = HyperlightSandboxBackend()
    try:
        await backend.acquire(SandboxKey('s', 't', 'a'), SandboxSpec(kind='python', work_dir=None, requires=frozenset({Capability.RUN_CODE})))
    except HyperlightWorkerError:
        pass
    else:
        raise AssertionError('second owner was admitted')
    assert (await backend.dispose_scope('hyperlight-live', 'runtime')).undisposed is not None
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup containment")
KILL = signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL
# The endless guest program is what puts this worker inside a native call, where closing
# its input cannot end it. Any failure before that propagates instead of being reported ready.
OWNER = """import asyncio, sys
from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig, _linux
_linux._LOCK_PATH = sys.argv[1]

async def main():
    backend = HyperlightSandboxBackend(HyperlightSandboxConfig(linux_cgroup_root=sys.argv[2]))
    spec = SandboxSpec(kind='python', work_dir=None, requires=frozenset({Capability.RUN_CODE}))
    sandbox = await backend.acquire(SandboxKey('hyperlight-live', 'owner', 'agent'), spec)
    print((await sandbox.run_code("print('live')", timeout=30)).stdout.strip(), flush=True)
    await sandbox.run_code("while True: pass", timeout=3600)

asyncio.run(main())
"""
CLAIM = "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH = sys.argv[1]; _linux.claim_host()"


def cgroup_root() -> Path:
    return Path(os.environ.get("MAF_HYPERLIGHT_CGROUP_ROOT") or _linux.DEFAULT_CGROUP_ROOT)


def worker_groups() -> set[str]:
    return {entry.name for entry in cgroup_root().iterdir() if entry.name.startswith("worker-")}


def group_kills() -> int:
    """Count kernel group-OOM kills below the delegated root; a removed group keeps its count."""
    rows = (cgroup_root() / "memory.events").read_text().splitlines()
    return int(dict(row.split() for row in rows)["oom_group_kill"])


def native_worker_pid(sandbox: _backend._HyperlightSandbox) -> int:
    if sys.platform != "linux":
        return sandbox.worker.process.pid
    # The Linux process is the supervisor; only its cgroup names the contained worker.
    (group,) = worker_groups()
    return int((cgroup_root() / group / "cgroup.procs").read_text().split()[0])


def settles(condition: Callable[[], bool], *, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


def guest_cpu(pid: int) -> float:
    """Seconds of CPU the worker has spent. Only a guest program inside its native call spends any."""
    if sys.platform != "linux":
        pytest.skip("Linux /proc accounting")
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def claimed(lock: str) -> bool:
    command = [sys.executable, "-I", "-c", CLAIM, lock]
    return subprocess.run(command, capture_output=True, timeout=30).returncode == 0


def exited(fd: int) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux pidfd API")
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(10_000), "a micro-VM worker survived its owner"


def answer(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None and process.stderr is not None
    ready, _, _ = select.select([process.stdout], [], [], 60)
    assert ready, "the owner did not answer"
    if not (value := process.stdout.readline()):
        raise AssertionError(process.stderr.read().decode(errors="replace"))
    return value.strip()


@LINUX_ONLY
def test_kernel_memory_ceiling_kills_the_real_guest_and_removes_its_cgroup():
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            linux_cgroup_root=str(cgroup_root()), max_worker_memory_bytes=128 * 1024**2
        )
    )
    kills = group_kills()
    with pytest.raises(HyperlightWorkerError, match="worker communication failed"):
        asyncio.run(backend.acquire(SandboxKey("hyperlight-live", "memory", "agent"), SPEC))
    assert group_kills() == kills + 1
    assert not worker_groups()


def test_abrupt_worker_death_retires_the_sandbox_and_a_fresh_acquire_replaces_it(live_backend):
    async def check():
        sandbox = cast("_backend._HyperlightSandbox", await live_backend.acquire(KEY, SPEC))
        assert (await sandbox.run_code("print(6 * 7)", timeout=5)).stdout == "42\n"
        os.kill(native_worker_pid(sandbox), KILL)
        # Termination is asynchronous, and a worker alive a moment longer still answers.
        sandbox.worker.process.wait(timeout=10)
        with pytest.raises(HyperlightWorkerError):
            await sandbox.run_code("print('must not run')", timeout=5)
        if sys.platform == "linux":
            assert not worker_groups()
        fresh = await live_backend.acquire(KEY, SPEC)
        assert fresh.instance_id != sandbox.instance_id
        assert (await fresh.run_code("print(6 * 7)", timeout=5)).stdout == "42\n"

    asyncio.run(check())


@LINUX_ONLY
def test_abrupt_owner_death_leaves_no_micro_vm_worker_and_releases_ownership(tmp_path: Path):
    if sys.platform != "linux":
        pytest.skip("Linux pidfd API")
    lock = str(tmp_path / "owner.lock")
    existing = worker_groups()
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", OWNER, lock, str(cgroup_root())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    handle = -1
    try:
        assert answer(owner) == b"live"
        (group,) = worker_groups() - existing
        (worker,) = (
            int(pid) for pid in (cgroup_root() / group / "cgroup.procs").read_text().split()
        )
        handle = os.pidfd_open(worker)
        idle = guest_cpu(worker)
        assert settles(lambda: guest_cpu(worker) > idle + 0.1), "the guest never began executing"
        owner.kill()
        owner.wait(timeout=10)
        exited(handle)
        assert settles(lambda: not (cgroup_root() / group).exists())
        assert settles(lambda: claimed(lock))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)
        if handle >= 0:
            os.close(handle)
