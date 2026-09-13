"""Deterministic admission, retirement and cleanup checks at the worker boundary."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    Egress,
    EgressRule,
    IdentityScope,
    Isolation,
    IsolationScope,
    ListedFile,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxQueuedTimeout,
    SandboxRouter,
    SandboxSpec,
    Selection,
)
from maf_sandbox.conformance import (
    ConformanceSubject,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_reclaim_conformance,
)

from maf_sandbox_hyperlight import (
    RUNTIME_INSTRUCTIONS,
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    _backend,
)
from maf_sandbox_hyperlight._wire import HyperlightWorkerError

KEY = SandboxKey("tenant", "conversation", "agent")
SPEC = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))


class FakeWorker:
    def __init__(self, config: HyperlightSandboxConfig) -> None:
        self.alive = True
        self.calls: list[dict[str, object]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.closing = threading.Event()
        self.finish_close = threading.Event()
        self.block: str | None = None
        self.block_close = False
        self.close_failures = 0
        self.reply: dict[str, object] | None = None

    def request(self, message: dict[str, object], *, deadline: float) -> dict[str, object]:
        self.calls.append(message)
        if message["op"] == self.block:
            self.started.set()
            assert self.release.wait(5), "test did not release the worker"
        if not self.alive:
            raise HyperlightWorkerError("worker terminated")
        if self.reply is not None:
            return self.reply
        if message["op"] == "run":
            return {"stdout": message["code"], "stderr": "", "exit_code": 0}
        return {"ok": True}

    def close(self) -> None:
        self.closing.set()
        if self.block_close:
            assert self.finish_close.wait(5), "test did not finish cleanup"
        if self.close_failures:
            self.close_failures -= 1
            raise HyperlightWorkerError("termination failed")
        self.alive = False
        self.release.set()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_backend, "Worker", FakeWorker)
    monkeypatch.setattr(_backend, "check_host", lambda: None)
    backend = HyperlightSandboxBackend()
    yield backend
    asyncio.run(backend.aclose())
    assert not backend._sandboxes


async def acquire(backend: HyperlightSandboxBackend):
    return cast("_backend._HyperlightSandbox", await backend.acquire(KEY, SPEC))


async def signalled(event: threading.Event) -> None:
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def test_offload_forwards_base_exceptions_instead_of_stranding_the_waiter():
    class NativePanic(BaseException):
        pass

    failure = NativePanic("native failure")

    def fail():
        raise failure

    async def check():
        with pytest.raises(NativePanic) as raised:
            await asyncio.wait_for(_backend._offload(fail), timeout=1)
        assert raised.value is failure

    asyncio.run(check())


def test_declarations_and_worker_free_construction(monkeypatch: pytest.MonkeyPatch):
    def unexpected(*args: object):
        raise AssertionError("construction must start no process")

    monkeypatch.setattr(_backend, "Worker", unexpected)
    backend = HyperlightSandboxBackend()
    assert backend.isolation is Isolation.MICROVM
    assert backend.declarations.capabilities == {Capability.RUN_CODE, Capability.SNAPSHOT}
    assert backend.declarations.egress_modes == {Egress.CLOSED, Egress.ALLOWLIST}
    assert backend.declarations.os_families == frozenset()


@pytest.mark.parametrize(
    "capability",
    [
        capability
        for capability in Capability
        if capability not in {Capability.RUN_CODE, Capability.SNAPSHOT, Capability.RECLAIM}
    ],
)
def test_unsupported_capabilities_refuse_before_worker(backend, capability):
    spec = (
        replace(
            SPEC,
            requires=frozenset({capability}),
            max_identity_scope=IdentityScope.PER_SANDBOX,
            max_identity_retention_seconds=60,
        )
        if capability is Capability.ATTACHED_IDENTITY
        else replace(SPEC, requires=frozenset({capability}))
    )
    with pytest.raises(SandboxCapabilityNotSupported):
        asyncio.run(backend.acquire(KEY, spec))
    assert not backend._sandboxes


@pytest.mark.parametrize(
    "spec",
    [
        replace(SPEC, image="image"),
        replace(SPEC, image_id="immutable"),
        replace(SPEC, work_dir="/work"),
        replace(SPEC, isolation_scope=IsolationScope.CALL),
        replace(SPEC, egress=Egress.UNRESTRICTED),
        replace(SPEC, egress=Egress.ALLOWLIST, egress_allow=("*.example.com",)),
        replace(
            SPEC,
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("example.com", methods=("GET",)),),
        ),
    ],
)
def test_unsupported_specs_refuse_before_worker(backend, spec):
    with pytest.raises((ValueError, SandboxCapabilityNotSupported)):
        asyncio.run(backend.acquire(KEY, spec))
    assert not backend._sandboxes


def test_exact_hosts_translate_both_schemes_and_policy_cannot_change(backend):
    async def check():
        spec = replace(
            SPEC, egress=Egress.ALLOWLIST, egress_allow=("EXAMPLE.com", EgressRule("example.com"))
        )
        sandbox = cast("_backend._HyperlightSandbox", await backend.acquire(KEY, spec))
        worker = cast("FakeWorker", sandbox.worker)
        assert worker.calls[0]["targets"] == ("http://example.com/", "https://example.com/")
        assert await backend.acquire(KEY, spec) is sandbox
        with pytest.raises(ValueError, match="changing its execution policy"):
            await backend.acquire(KEY, SPEC)
        with pytest.raises(ValueError, match="changing its execution policy"):
            await backend.acquire(KEY, replace(spec, execution_contract="different"))

    asyncio.run(check())


def test_backend_objects_and_event_loops_share_the_full_key(backend):
    first = asyncio.run(acquire(backend))
    second_backend = HyperlightSandboxBackend()
    assert asyncio.run(second_backend.acquire(KEY, SPEC)) is first
    keys = [
        replace(KEY, scope="other"),
        replace(KEY, thread_id="other"),
        replace(KEY, agent_id="other"),
        replace(KEY, call_id="other"),
    ]
    siblings = [asyncio.run(backend.acquire(key, SPEC)) for key in keys]
    assert len({sandbox.instance_id for sandbox in [first, *siblings]}) == 5
    asyncio.run(second_backend.aclose())
    assert first.alive
    assert asyncio.run(second_backend.dispose(KEY)) is None
    assert not first.alive
    assert all(cast("_backend._HyperlightSandbox", sandbox).alive for sandbox in siblings)


def test_reset_rotates_identity_and_stale_disposal_preserves_replacement(backend):
    sandbox = asyncio.run(acquire(backend))
    old = sandbox.instance_id
    asyncio.run(sandbox.reset(timeout=2))
    assert sandbox.instance_id != old
    assert asyncio.run(backend.dispose(KEY, kind=SPEC.kind, instance_id=old)) is None
    assert sandbox.alive
    assert asyncio.run(sandbox.run_code("still alive", timeout=2)).stdout == "still alive"


def test_admission_serializes_concurrent_event_loops(backend):
    sandbox = asyncio.run(acquire(backend))
    worker = cast("FakeWorker", sandbox.worker)
    worker.block = "run"
    results: list[str] = []

    def run() -> None:
        results.append(asyncio.run(sandbox.run_code("first", timeout=3)).stdout)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert worker.started.wait(2)
        with pytest.raises(SandboxQueuedTimeout):
            asyncio.run(sandbox.run_code("second", timeout=0.03))
        assert sandbox.alive
    finally:
        worker.release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert results == ["first"]


def test_queued_timeout_and_cancellation_leave_running_guest_intact(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block = "run"
        first = asyncio.create_task(sandbox.run_code("first", timeout=3))
        await signalled(worker.started)
        with pytest.raises(SandboxQueuedTimeout):
            await sandbox.run_code("must not run", timeout=0.03)
        with pytest.raises(SandboxQueuedTimeout):
            await sandbox.reset(timeout=0.03)
        queued = asyncio.create_task(sandbox.run_code("cancel queued", timeout=3))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert sandbox.alive
        worker.release.set()
        assert (await first).stdout == "first"
        assert [call["op"] for call in worker.calls] == ["init", "run"]

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["run", "reset"])
def test_started_deadline_terminates_worker_and_reacquire_is_fresh(backend, operation):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block = operation
        with pytest.raises(TimeoutError) as caught:
            if operation == "run":
                await sandbox.run_code("hang", timeout=0.04)
            else:
                await sandbox.reset(timeout=0.04)
        assert not isinstance(caught.value, SandboxQueuedTimeout)
        assert not worker.alive
        with pytest.raises(HyperlightWorkerError, match="retired"):
            await sandbox.run_code("cannot run", timeout=1)
        replacement = await acquire(backend)
        assert replacement.instance_id != sandbox.instance_id
        assert replacement.alive

    asyncio.run(check())


def test_repeated_cancellation_waits_for_cleanup(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block = "run"
        worker.block_close = True
        running = asyncio.create_task(sandbox.run_code("hang", timeout=3))
        await signalled(worker.started)
        running.cancel()
        await signalled(worker.closing)
        running.cancel()
        await asyncio.sleep(0.01)
        assert not running.done()
        worker.finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not worker.alive

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["dispose", "dispose_scope", "aclose"])
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_disposal_preserves_interruption_after_active_cleanup(
    backend, operation, interruption, cleanup_fails
):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block_close = True
        worker.close_failures = int(cleanup_fails)
        sibling = await backend.acquire(KEY, replace(SPEC, kind="other"))
        deadline = asyncio.timeout(None)

        async def dispose():
            async with deadline:
                if operation == "dispose":
                    return await backend.dispose(KEY)
                if operation == "dispose_scope":
                    return await backend.dispose_scope(KEY.scope, KEY.thread_id)
                return await backend.aclose()

        pending = asyncio.create_task(dispose())
        try:
            await signalled(worker.closing)
            if interruption == "cancel":
                pending.cancel("first cancellation")
                await asyncio.sleep(0)
                pending.cancel("repeated cancellation")
            else:
                deadline.reschedule(asyncio.get_running_loop().time())
            async with asyncio.timeout(3):
                while not pending.cancelling():
                    await asyncio.sleep(0)
            assert not pending.done()
            worker.finish_close.set()
            expected = asyncio.CancelledError if interruption == "cancel" else TimeoutError
            with pytest.raises(expected):
                await pending
            assert worker.alive is cleanup_fails
            assert sibling.alive
            assert (KEY, SPEC.kind) in backend._sandboxes
        finally:
            worker.finish_close.set()
            await asyncio.gather(pending, return_exceptions=True)
            await backend.aclose()

    asyncio.run(check())


def test_router_disposal_deadline_reports_failure_after_cleanup(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block_close = True
        router = SandboxRouter([backend])
        pending = asyncio.create_task(router.dispose_kind(KEY, SPEC.kind, timeout=1))
        try:
            await signalled(worker.closing)
            async with asyncio.timeout(3):
                while not pending.cancelling():
                    await asyncio.sleep(0.001)
            assert not pending.done()
            worker.finish_close.set()
            assert await pending is False
            assert not worker.alive
            assert await router.dispose_kind(KEY, SPEC.kind, timeout=1)
        finally:
            worker.finish_close.set()
            await asyncio.gather(pending, return_exceptions=True)
            await backend.aclose()

    asyncio.run(check())


def test_disposal_interrupts_an_active_program(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.block = "run"
        running = asyncio.create_task(sandbox.run_code("hang", timeout=3))
        await signalled(worker.started)
        assert await backend.dispose(KEY) is None
        with pytest.raises(HyperlightWorkerError):
            await running
        assert not worker.alive

    asyncio.run(check())


def test_failed_termination_can_be_retried_from_another_event_loop(backend):
    sandbox = asyncio.run(acquire(backend))
    worker = cast("FakeWorker", sandbox.worker)
    worker.block = "run"
    worker.close_failures = 1
    with pytest.raises(HyperlightWorkerError, match="termination failed"):
        asyncio.run(sandbox.run_code("hang", timeout=0.04))
    assert worker.alive and not sandbox.alive
    replacement = asyncio.run(acquire(backend))
    assert not worker.alive and replacement.alive
    assert replacement.instance_id != sandbox.instance_id


def test_failed_disposal_retains_target_for_retry_and_scope_purge_preserves_siblings(backend):
    async def check():
        target = await acquire(backend)
        worker = cast("FakeWorker", target.worker)
        worker.close_failures = 1
        sibling = cast(
            "_backend._HyperlightSandbox", await backend.acquire(replace(KEY, scope="other"), SPEC)
        )
        purge = await backend.dispose_scope(KEY.scope, KEY.thread_id)
        assert purge.disposed == 0 and purge.undisposed is not None
        assert (KEY, SPEC.kind) in backend._sandboxes
        assert worker.alive and not target.alive
        purge = await backend.dispose_scope(KEY.scope, KEY.thread_id)
        assert purge.disposed == 1 and purge.undisposed is None
        assert sibling.alive

    asyncio.run(check())


@pytest.mark.parametrize(
    "reply",
    [
        {"stdout": 3},
        {"stdout": "", "stderr": "", "exit_code": True},
        {"stdout": "native stdout", "stderr": "native panic details", "exit_code": -1},
    ],
)
def test_bad_native_results_retire_the_worker(backend, reply):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.reply = reply
        with pytest.raises(HyperlightWorkerError) as raised:
            await sandbox.run_code("malformed", timeout=1)
        if reply.get("exit_code") == -1:
            assert str(raised.value) == "native execution failed"
        assert not worker.alive
        assert not sandbox.alive
        replacement = await acquire(backend)
        assert replacement.alive and replacement.instance_id != sandbox.instance_id

    asyncio.run(check())


@pytest.mark.parametrize("selection", list(Selection))
def test_native_panics_are_sanitized_for_codeact(backend, monkeypatch, selection):
    from maf_sandbox_codeact import CodeactRuntime, make_codeact_tools

    request = FakeWorker.request
    workers: list[FakeWorker] = []

    def panic(
        worker: FakeWorker, message: dict[str, object], *, deadline: float
    ) -> dict[str, object]:
        if message["op"] == "run":
            workers.append(worker)
            return {"stdout": "native stdout", "stderr": "native panic details", "exit_code": -1}
        return request(worker, message, deadline=deadline)

    monkeypatch.setattr(FakeWorker, "request", panic)
    router = SandboxRouter([backend], selection=selection, min_cleanup=Cleanup.RESET)

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
    result = asyncio.run(function(code="print('hello')"))
    assert result == "Error: could not run the program in the sandbox"
    assert workers and all(not worker.alive for worker in workers)


def test_guest_errors_preserve_output_and_worker_reuse(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        worker.reply = {"stdout": "guest stdout", "stderr": "guest error", "exit_code": 1}
        result = await sandbox.run_code("raise ValueError('guest error')", timeout=1)
        assert (result.stdout, result.stderr, result.exit_code) == (
            "guest stdout",
            "guest error",
            1,
        )
        assert worker.alive and sandbox.alive
        assert await acquire(backend) is sandbox

    asyncio.run(check())


def test_source_and_timeout_validation_do_not_enter_worker(backend):
    async def check():
        sandbox = await acquire(backend)
        worker = cast("FakeWorker", sandbox.worker)
        for timeout in (0, -1, float("nan"), float("inf"), True, "1"):
            with pytest.raises(ValueError):
                await sandbox.run_code("source", timeout=cast("float", timeout))
        with pytest.raises(ValueError, match="max_code_bytes"):
            await sandbox.run_code("é" * backend.config.max_code_bytes, timeout=1)
        with pytest.raises(TypeError):
            await sandbox.run_code(cast("str", 1), timeout=1)
        assert worker.calls == [
            {"op": "init", "targets": (), "output_limit": backend.config.max_output_bytes}
        ]

    asyncio.run(check())


def test_withheld_surfaces_answer_shared_conformance(backend):
    subject = cast(
        "ConformanceSubject", SimpleNamespace(capabilities=backend.declarations.capabilities)
    )

    async def check():
        with pytest.raises(ValueError, match="FILES_IN"):
            await assert_files_in_conformance(subject)
        with pytest.raises(ValueError, match="EXEC"):
            await assert_exec_conformance(subject)
        with pytest.raises(ValueError, match="FILES_DELETE"):
            await assert_files_delete_conformance(subject)
        with pytest.raises(ValueError, match="RECLAIM"):
            await assert_reclaim_conformance(subject)

    asyncio.run(check())


@pytest.mark.parametrize(
    "name",
    [
        "startup_timeout",
        "cleanup_timeout",
        "max_code_bytes",
        "max_output_bytes",
        "max_worker_memory_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, "5", float("inf"), float("nan")])
def test_config_refuses_invalid_budgets(name, value):
    with pytest.raises(ValueError):
        HyperlightSandboxConfig(**{name: value})
