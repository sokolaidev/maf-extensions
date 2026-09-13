"""Opt-in WHP/KVM checks against the pinned guest: set MAF_HYPERLIGHT_LIVE=1."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    _backend,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_HYPERLIGHT_LIVE") != "1" or sys.platform not in {"win32", "linux"},
    reason="requires opt-in and Windows WHP or WSL2 KVM",
)
KEY = SandboxKey("hyperlight-live", "runtime", "agent")
SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


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
        assert "42" in first
        identity = next(iter(live_backend._sandboxes.values())).instance_id
        second = await function(code="print('answer' in globals())")
        assert "False" in second
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
