"""The cleanup ladder separates tool calls and removes their guest state."""

import asyncio
import dataclasses
import logging

import pytest

from maf_sandbox import (
    CallerContext,
    Capability,
    Cleanup,
    DisposalFailure,
    FailedReclaimPolicy,
    Isolation,
    IsolationScope,
    ReclaimConfig,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxUnclean,
)
from maf_sandbox._cleanup import QUEUED_CALL_TIMEOUT
from maf_sandbox.maf import sandboxed_tool
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

_KEY = SandboxKey(scope="scope", thread_id="thread", agent_dir="agent")
_SPEC = SandboxSpec(kind="test", confined_to_guest_call_path=True)
_DECLARATIONS = dataclasses.replace(
    FAKE_BACKEND_DECLARATIONS,
    capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.SNAPSHOT},
    isolation_scopes=frozenset(IsolationScope),
)


def _tool(router, spec, use):
    def build(session):
        async def run(target: str) -> str:
            key = session.key()
            assert not isinstance(key, str)
            sandbox = await session.acquire(key)
            if isinstance(sandbox, str):
                return sandbox
            guest_path = session.guest_call_path()
            await sandbox.write_file("payload", target, working_directory=guest_path)
            await use(sandbox, guest_path, target)
            return guest_path

        return run

    tool = sandboxed_tool(
        build,
        router=router,
        context=CallerContext(
            current_scope=lambda: _KEY.scope,
            current_thread_id=lambda: _KEY.thread_id,
            list_files=InMemoryStore.list,
        ),
        agent_dir=_KEY.agent_dir,
        spec=spec,
        name="run",
        logger=logging.getLogger(__name__),
    )[0]
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


@pytest.mark.parametrize(
    "spec,rung",
    [
        (_SPEC, Cleanup.RECLAIM),
        (dataclasses.replace(_SPEC, min_cleanup=Cleanup.RESET), Cleanup.RESET),
        (dataclasses.replace(_SPEC, min_cleanup=Cleanup.DISPOSE), Cleanup.DISPOSE),
        (dataclasses.replace(_SPEC, isolation_scope=IsolationScope.CALL), Cleanup.DISPOSE),
    ],
)
def test_rungs_remove_call_state_and_preserve_a_warm_sibling(spec, rung):
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    sibling_spec = dataclasses.replace(_SPEC, kind="sibling")
    served = []

    async def use(sandbox, guest_path, target):
        served.append((sandbox, len(sandbox.resets)))
        assert sandbox.contents[f"{guest_path}/payload"] == b"data"
        if rung is Cleanup.RESET:
            sandbox.contents["/outside-call"] = b"residue"
            sandbox.running.add("background-program")

    async def scenario():
        sibling = await router.acquire(_KEY, sibling_spec)
        assert isinstance(sibling, InProcessSandbox)
        await sibling.write_file("warm", "keep", working_directory=sibling_spec.work_dir)
        guest_path = await _tool(router, spec, use)(target="data")
        sandbox, resets = served[0]
        assert len(sandbox.reclaims) == (rung is Cleanup.RECLAIM)
        assert len(sandbox.resets) == resets + (rung is Cleanup.RESET)
        if rung is Cleanup.DISPOSE:
            if spec.isolation_scope is IsolationScope.CALL:
                assert backend.disposed_kinds == [None]
                assert backend.disposed[0] != _KEY
            else:
                assert backend.disposed_kinds == [spec.kind]
                assert backend.disposed == [_KEY]
            assert len(backend.sandboxes) == 1
        else:
            assert f"{guest_path}/payload" not in sandbox.contents
            assert guest_path not in sandbox.directories
            assert not backend.disposed
            assert await router.acquire(_KEY, spec) is sandbox
        if rung is Cleanup.RESET:
            assert "/outside-call" not in sandbox.contents
            assert not sandbox.running
        assert await router.acquire(_KEY, sibling_spec) is sibling
        assert sibling.contents[f"{sibling_spec.work_dir}/warm"] == b"keep"

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_an_unclaimed_spec_disposes_by_default_and_next_call_gets_a_fresh_sandbox():
    backend = InProcessSandboxBackend(sandbox_per_key=True)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    spec = SandboxSpec(kind="test")
    served = []

    async def use(sandbox, guest_path, target):
        assert list(sandbox.contents.values()) == [target.encode()]
        served.append(sandbox)

    async def scenario():
        run = _tool(router, spec, use)
        await run(target="first")
        assert not backend.sandboxes
        await run(target="second")
        assert not backend.sandboxes
        assert served[0] is not served[1]
        assert backend.disposed_kinds == [spec.kind] * 4

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


@pytest.mark.parametrize("rung", list(Cleanup))
@pytest.mark.parametrize("expire", [False, True])
def test_two_tool_calls_share_or_wait_for_cleanup(rung, expire, monkeypatch):
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    spec = dataclasses.replace(_SPEC, min_cleanup=rung)
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    release_first, queued = asyncio.Event(), asyncio.Event()
    served, guest_paths, bounds = {}, {}, []
    take = router._slots.take

    async def observe_admission(key, kind, *, owner, exclusive, timeout):
        if first_entered.is_set():
            bounds.append(timeout)
            queued.set()
        await take(key, kind, owner=owner, exclusive=exclusive, timeout=0.05 if expire else 2)

    monkeypatch.setattr(router._slots, "take", observe_admission)

    async def use(sandbox, guest_path, target):
        served[target], guest_paths[target] = sandbox, guest_path
        if target == "first":
            first_entered.set()
            await release_first.wait()
            assert sandbox.contents[f"{guest_path}/payload"] == b"first"
        else:
            if rung is not Cleanup.RECLAIM:
                assert f"{guest_paths['first']}/payload" not in sandbox.contents
            second_entered.set()

    async def scenario():
        run = _tool(router, spec, use)
        first = asyncio.create_task(run(target="first"))
        await first_entered.wait()
        resets = list(backend.sandbox.resets)
        second = asyncio.create_task(run(target="second"))
        try:
            await queued.wait()
            assert QUEUED_CALL_TIMEOUT - 1 < bounds[0] <= QUEUED_CALL_TIMEOUT
            if rung is Cleanup.RECLAIM:
                await second_entered.wait()
                assert await second == guest_paths["second"]
                assert not first.done()
                assert served["first"] is served["second"]
                assert f"{guest_paths['second']}/payload" not in served["first"].contents
            elif expire:
                assert "another call is using" in await second
                assert not second_entered.is_set()
                assert len(backend.keys) == 1
                assert not backend.disposed and backend.sandbox.resets == resets
            else:
                assert not second_entered.is_set()
                assert not second.done()
                assert len(backend.keys) == 1
        finally:
            release_first.set()
            assert await first == guest_paths["first"]
        if not expire or rung is Cleanup.RECLAIM:
            assert await second == guest_paths["second"]
            assert second_entered.is_set()
            assert (served["first"] is served["second"]) is (rung is not Cleanup.DISPOSE)
        else:
            assert await run(target="second") == guest_paths["second"]
            assert second_entered.is_set()
        assert guest_paths["first"] != guest_paths["second"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


@pytest.mark.parametrize("reset_fails", [False, True])
@pytest.mark.parametrize("disposal", ["success", "reported", "raised"])
@pytest.mark.parametrize("policy", list(FailedReclaimPolicy))
def test_failed_reset_escalates_and_failed_disposal_obeys_host_policy(
    reset_fails, disposal, policy
):
    class _ResetFails(InProcessSandbox):
        fail_reset = False

        async def reset(self, *, timeout):
            if not self.fail_reset:
                await super().reset(timeout=timeout)
                return
            self.resets.append(0)
            raise RuntimeError("reset refused")

    backend = InProcessSandboxBackend(
        _ResetFails(), sandbox_per_key=True, declarations=_DECLARATIONS
    )
    router = SandboxRouter(
        [backend],
        min_isolation=Isolation.NONE,
        reclaim=ReclaimConfig(failed_reclaim_policy=policy),
    )
    spec = dataclasses.replace(_SPEC, min_cleanup=Cleanup.RESET if reset_fails else Cleanup.DISPOSE)
    resets_at_use = []

    async def use(sandbox, guest_path, target):
        resets_at_use.append(len(sandbox.resets))
        sandbox.fail_reset = True
        if disposal == "reported":
            backend.dispose_failure = DisposalFailure("refused", "delete refused")
        elif disposal == "raised":
            backend.dispose_error = RuntimeError("delete refused")

    async def scenario():
        guest_path = await _tool(router, spec, use)(target="data")
        assert len(backend.sandbox.resets) == resets_at_use[0] + reset_fails
        assert backend.disposed_kinds == [spec.kind]
        if disposal == "success":
            assert not backend.sandboxes
            assert await router.acquire(_KEY, spec) is not backend.sandbox
        else:
            assert backend.sandbox.contents[f"{guest_path}/payload"] == b"data"
            if policy is FailedReclaimPolicy.KEEP:
                assert await router.acquire(_KEY, spec) is backend.sandbox
            else:
                for kind in (spec.kind, "sibling"):
                    with pytest.raises(SandboxUnclean):
                        await router.acquire(_KEY, dataclasses.replace(spec, kind=kind))

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
