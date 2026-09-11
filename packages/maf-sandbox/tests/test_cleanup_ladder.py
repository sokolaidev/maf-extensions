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


def _stored(path):
    return f"/maf-sandbox/work/{path}"


def _tool(router, spec, use, **kw):
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
        **kw,
    )[0]
    return getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool


def test_default_disposes_even_a_confined_kind_after_every_call():
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)
    seen = []

    async def use(sandbox, guest_path, target):
        seen.append(sandbox)

    async def scenario():
        tool = _tool(router, _SPEC, use)
        await tool(target="first")
        await tool(target="second")
        assert seen[0] is not seen[1]
        assert backend.disposed == [_KEY, _KEY]

    asyncio.run(scenario())


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
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=Cleanup.RECLAIM)
    sibling_spec = dataclasses.replace(_SPEC, kind="sibling")
    served = []

    async def use(sandbox, guest_path, target):
        served.append((sandbox, len(sandbox.resets)))
        assert sandbox.contents[_stored(f"{guest_path}/payload")] == b"data"
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
            assert _stored(f"{guest_path}/payload") not in sandbox.contents
            assert _stored(guest_path) not in sandbox.directories
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
def test_concurrent_bodies_drain_before_cleanup_and_block_a_third(rung, expire, monkeypatch):
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=Cleanup.RECLAIM)
    spec = dataclasses.replace(_SPEC, min_cleanup=rung)
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    release_first = asyncio.Event()
    served, guest_paths = {}, {}

    async def use(sandbox, guest_path, target):
        served[target], guest_paths[target] = sandbox, guest_path
        if target == "first":
            first_entered.set()
            await release_first.wait()
            assert sandbox.contents[_stored(f"{guest_path}/payload")] == b"first"
        else:
            assert sandbox.contents[_stored(f"{guest_paths['first']}/payload")] == b"first"
            second_entered.set()

    async def scenario():
        run = _tool(router, spec, use)
        first = asyncio.create_task(run(target="first"))
        await first_entered.wait()
        resets = len(backend.sandbox.resets)
        third = None
        second = asyncio.create_task(run(target="second"))
        await second_entered.wait()
        assert served["first"] is served["second"]
        if rung is Cleanup.RECLAIM:
            assert await second == guest_paths["second"]
            assert _stored(f"{guest_paths['second']}/payload") not in served["first"].contents
        else:
            assert router._slots._slots[(_KEY, spec.kind)].state == "draining"
            assert not second.done()
            assert len(backend.sandbox.resets) == resets
            assert not backend.disposed
            third = asyncio.create_task(
                router.enter_call(
                    _KEY,
                    spec,
                    owner="third",
                    timeout=0.01 if expire else 2,
                )
            )
            if expire:
                with pytest.raises(TimeoutError):
                    await third
            else:
                await asyncio.sleep(0)
                assert not third.done()
        release_first.set()
        assert await first == guest_paths["first"]
        assert await second == guest_paths["second"]
        if rung is not Cleanup.RECLAIM and not expire:
            assert third is not None
            await third
            await router.release_call(_KEY, spec.kind, owner="third")
        assert len(backend.sandbox.resets) == resets + (rung is Cleanup.RESET)
        assert len(backend.disposed) == (rung is Cleanup.DISPOSE)
        assert not router._slots._slots

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
        min_cleanup=Cleanup.RECLAIM,
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
            assert backend.sandbox.contents[_stored(f"{guest_path}/payload")] == b"data"
            if policy is FailedReclaimPolicy.KEEP:
                assert await router.acquire(_KEY, spec) is backend.sandbox
            else:
                for kind in (spec.kind, "sibling"):
                    with pytest.raises(SandboxUnclean):
                        await router.acquire(_KEY, dataclasses.replace(spec, kind=kind))

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


@pytest.mark.parametrize("policy", list(FailedReclaimPolicy))
@pytest.mark.parametrize("delete_fails", [False, True])
def test_failed_reclaim_waits_for_running_sibling_and_reports_after_cleanup(policy, delete_fails):
    class _RefusesOne(InProcessSandbox):
        refused_path = None

        async def reclaim(self, directory, *, working_directory, timeout):
            if directory == self.refused_path:
                raise PermissionError("call directory remains")
            await super().reclaim(directory, working_directory=working_directory, timeout=timeout)

    heard = []
    backend = InProcessSandboxBackend(
        _RefusesOne(), sandbox_per_key=True, declarations=_DECLARATIONS
    )

    async def report(failure):
        heard.append(failure)
        if policy is FailedReclaimPolicy.DISPOSE:
            assert len(backend.disposed) == 1

    router = SandboxRouter(
        [backend],
        min_cleanup=Cleanup.RECLAIM,
        min_isolation=Isolation.NONE,
        reclaim=ReclaimConfig(failed_reclaim_policy=policy, on_failure=report),
    )
    entered, leaving, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def use(sandbox, guest_path, target):
        if target == "first":
            entered.set()
            await release.wait()
            assert sandbox.contents[_stored(f"{guest_path}/payload")] == b"first"
        else:
            sandbox.refused_path = guest_path
            if delete_fails:
                backend.dispose_failure = DisposalFailure("refused", "delete refused")
            leaving.set()

    async def scenario():
        run = _tool(router, _SPEC, use)
        first = asyncio.create_task(run(target="first"))
        await entered.wait()
        second = asyncio.create_task(run(target="second"))
        await leaving.wait()
        assert not backend.disposed
        if policy is FailedReclaimPolicy.DISPOSE:
            assert not heard and not second.done()
            with pytest.raises(TimeoutError):
                await router.enter_call(_KEY, _SPEC, owner="third", timeout=0.01)
        else:
            await second
            assert heard[0].disposal == "kept"
        release.set()
        await asyncio.gather(first, second)
        assert len(heard) == 1
        expected = (
            "kept"
            if policy is FailedReclaimPolicy.KEEP
            else "failed"
            if delete_fails
            else "disposed"
        )
        assert heard[0].disposal == expected
        assert bool(router._pending_for(_KEY)) == (
            delete_fails and policy is FailedReclaimPolicy.DISPOSE
        )
        assert not router._slots._slots

    asyncio.run(asyncio.wait_for(scenario(), 3))


_EXCLUSIVE = dataclasses.replace(_SPEC, exclusive_admission=True)


@pytest.mark.parametrize("rung", [Cleanup.RECLAIM, Cleanup.DISPOSE])
def test_an_exclusive_kind_runs_one_call_at_a_time(rung):
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=rung)
    first_entered, release_first = asyncio.Event(), asyncio.Event()
    served, paths, entered = {}, {}, []

    async def use(sandbox, guest_path, target):
        entered.append(target)
        served[target], paths[target] = sandbox, guest_path
        if target == "first":
            first_entered.set()
            await release_first.wait()
        else:
            # The call ahead was cleaned before this one was admitted, whichever rung ran.
            assert _stored(f"{paths['first']}/payload") not in sandbox.contents

    async def scenario():
        run = _tool(router, _EXCLUSIVE, use)
        first = asyncio.create_task(run(target="first"))
        await first_entered.wait()
        second = asyncio.create_task(run(target="second"))
        await asyncio.sleep(0.05)
        assert entered == ["first"]
        assert not second.done()
        release_first.set()
        assert await first == paths["first"]
        assert await second == paths["second"]
        assert entered == ["first", "second"]
        if rung is Cleanup.RECLAIM:
            assert served["first"] is served["second"]
            assert not backend.disposed
        else:
            assert served["first"] is not served["second"]
            assert backend.disposed == [_KEY, _KEY]
        assert not router._slots._slots

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_the_router_holds_an_exclusive_spec_that_way_without_being_told():
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    async def scenario():
        await router.enter_call(_KEY, _EXCLUSIVE, owner="first")
        with pytest.raises(TimeoutError):
            await router.enter_call(_KEY, _EXCLUSIVE, owner="second", timeout=0.01)
        await router.release_call(_KEY, _EXCLUSIVE.kind, owner="first")
        await router.enter_call(_KEY, _EXCLUSIVE, owner="second", timeout=1)
        await router.release_call(_KEY, _EXCLUSIVE.kind, owner="second")
        assert not router._slots._slots

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


@pytest.mark.parametrize("stated", [None, 7.5])
def test_a_call_waits_its_stated_bound_plus_two_cleanup_bounds_per_call_ahead(stated):
    seen = []

    class _Recording(SandboxRouter):
        async def enter_call(
            self, key, spec, *, owner, timeout=QUEUED_CALL_TIMEOUT, exclusive=False
        ):
            seen.append(timeout)
            return await super().enter_call(
                key, spec, owner=owner, timeout=timeout, exclusive=exclusive
            )

    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = _Recording([backend], min_isolation=Isolation.NONE, reclaim=ReclaimConfig(timeout=2.5))

    async def use(sandbox, guest_path, target):
        pass

    async def scenario():
        await _tool(router, _SPEC, use, admission_timeout=stated)(target="x")

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert seen == [(QUEUED_CALL_TIMEOUT if stated is None else stated) + 2 * 2.5]


@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_an_admission_bound_must_be_finite_and_positive(bad):
    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE)

    async def use(sandbox, guest_path, target):
        pass

    with pytest.raises(ValueError, match="admission_timeout must be a finite positive"):
        _tool(router, _SPEC, use, admission_timeout=bad)


@pytest.mark.parametrize("stated", [None, 7.5])
def test_the_wait_adds_the_tools_own_cleanup_bound_rather_than_the_routers(stated):
    """Both cleanup stages use the tool's effective bound."""
    seen = []

    class _Recording(SandboxRouter):
        async def enter_call(
            self, key, spec, *, owner, timeout=QUEUED_CALL_TIMEOUT, exclusive=False
        ):
            seen.append(timeout)
            return await super().enter_call(
                key, spec, owner=owner, timeout=timeout, exclusive=exclusive
            )

    backend = InProcessSandboxBackend(sandbox_per_key=True, declarations=_DECLARATIONS)
    router = _Recording([backend], min_isolation=Isolation.NONE, reclaim=ReclaimConfig(timeout=2.5))

    async def use(sandbox, guest_path, target):
        pass

    async def scenario():
        run = _tool(router, _SPEC, use, admission_timeout=stated, reclaim_timeout=90.0)
        await run(target="x")

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert seen == [(QUEUED_CALL_TIMEOUT if stated is None else stated) + 2 * 90.0]


@pytest.mark.parametrize("rung", [Cleanup.RECLAIM, Cleanup.RESET])
@pytest.mark.parametrize("failure", ["raised", "timeout"])
def test_admission_waits_for_failed_cleanup_then_disposal(rung, failure):
    entered, release = asyncio.Event(), asyncio.Event()
    stages, paths = [], {}
    bound = 0.4

    class _SlowCleanup(InProcessSandbox):
        fail_cleanup = False

        async def fail(self, stage, timeout):
            stages.append(stage)
            assert timeout == bound
            if failure == "timeout":
                async with asyncio.timeout(timeout):
                    await asyncio.Event().wait()
            await asyncio.sleep(0.3)
            raise RuntimeError("cleanup refused")

        async def reclaim(self, directory, *, working_directory, timeout):
            if self.fail_cleanup:
                await self.fail("reclaim", timeout)
            await super().reclaim(directory, working_directory=working_directory, timeout=timeout)

        async def reset(self, *, timeout):
            if self.fail_cleanup:
                await self.fail("reset", timeout)
            await super().reset(timeout=timeout)

    class _SlowDisposal(InProcessSandboxBackend):
        async def dispose(self, key, *, kind=None, instance_id=None):
            stages.append("dispose")
            await asyncio.sleep(0.3)
            return await super().dispose(key, kind=kind, instance_id=instance_id)

    backend = _SlowDisposal(_SlowCleanup(), sandbox_per_key=True, declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=rung)

    async def use(sandbox, guest_path, target):
        paths[target] = guest_path
        if target == "first":
            sandbox.fail_cleanup = True
            entered.set()
            await release.wait()
        else:
            assert stages == [rung.value, "dispose"]
            assert _stored(f"{paths['first']}/payload") not in sandbox.contents

    async def scenario():
        run = _tool(router, _EXCLUSIVE, use, admission_timeout=0.05, reclaim_timeout=bound)
        first = asyncio.create_task(run(target="first"))
        await entered.wait()
        second = asyncio.create_task(run(target="second"))
        while not router._slots._slots[(_KEY, _EXCLUSIVE.kind)].waiters:
            await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert "second" in paths, results
        assert results == [paths["first"], paths["second"]]
        assert backend.disposed == [_KEY]
        assert not router._slots._slots

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_a_waiter_is_renewed_as_each_held_instance_is_reclaimed():
    """A RECLAIM call removes every instance it holds before releasing the entry, each under its
    own bound. Charging a waiter for all of them refuses it while the predecessor is still
    inside every bound it was given."""
    bound, stage, held = 0.3, 0.25, 4

    class _SlowReclaim(InProcessSandbox):
        async def reclaim(self, directory, *, working_directory, timeout):
            await asyncio.sleep(stage)
            await super().reclaim(directory, working_directory=working_directory, timeout=timeout)

    sandboxes = [_SlowReclaim() for _ in range(held)]

    class _OneInstancePerAcquire(InProcessSandboxBackend):
        async def acquire(self, key, spec):
            self.keys.append(key)
            self.specs.append(spec)
            serving = sandboxes[min(len(self.keys) - 1, held - 1)]
            await serving.prepare_work_dir(spec)
            return serving

    backend = _OneInstancePerAcquire(sandboxes[0], declarations=_DECLARATIONS)
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, min_cleanup=Cleanup.RECLAIM)
    for sandbox in sandboxes:
        router._remember_instance(_KEY, _EXCLUSIVE.kind, backend, sandbox)
    reclaimed = []

    def build(session):
        async def run(target: str) -> str:
            key = session.key()
            assert not isinstance(key, str)
            session.guest_call_path()
            for _ in range(held):
                answer = await session.acquire(key)
                if isinstance(answer, str):
                    return answer
            reclaimed.append(target)
            return target

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
        spec=_EXCLUSIVE,
        name="run",
        logger=logging.getLogger(__name__),
        admission_timeout=0.05,
        reclaim_timeout=bound,
    )[0]
    fn = getattr(tool, "func", None) or getattr(tool, "__wrapped__", None) or tool

    async def scenario():
        first = asyncio.create_task(fn(target="first"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(fn(target="second"))
        return await asyncio.gather(first, second)

    answers = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert not [one for one in answers if "another call is using the sandbox" in one]
    assert reclaimed == ["first", "second"]
