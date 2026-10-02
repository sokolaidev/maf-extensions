"""Channel selection and publication must preserve the live run's authority."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace

import pytest

from maf_sandbox import (
    Capability,
    Cleanup,
    ExecProgramChannel,
    HostToolCalled,
    HostToolRegistry,
    HostToolRun,
    Isolation,
    ProgramRequirements,
    SandboxBackendUnavailable,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
    Selection,
    TransferLimits,
    sandbox_tool,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessProgramChannel,
    InProcessSandboxBackend,
)

_KEY = SandboxKey("scope", "thread", "agent")
_SPEC = SandboxSpec(kind="program", program=ProgramRequirements(), requires=frozenset())


def _backend(name: str, *, mode="exec", error=None):
    return InProcessSandboxBackend(
        name=name,
        acquire_error=error,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            program_channels=(InProcessProgramChannel(name=name, mode=mode),),
        ),
    )


def _router(*backends, **kwargs):
    return SandboxRouter(
        list(backends), min_isolation=Isolation.NONE, selection=Selection.PER_SPEC, **kwargs
    )


def test_default_exec_first_and_explicit_runtime_preference():
    runtime, execution = _backend("runtime", mode="runtime"), _backend("exec")
    for preference, expected in [(("exec", "runtime"), execution), (("runtime", "exec"), runtime)]:
        router = _router(runtime, execution, program_channel_preference=preference)
        result = asyncio.run(router.acquire(_KEY, _SPEC))
        assert result is expected.sandbox


def test_availability_fallback_is_pinned_until_disposal():
    first = _backend("first", error=SandboxBackendUnavailable("offline"))
    second = _backend("second")
    router = _router(first, second)

    async def exercise():
        assert await router.acquire(_KEY, _SPEC) is second.sandbox
        first.acquire_error = None
        assert await router.acquire(_KEY, _SPEC) is second.sandbox
        await router.dispose(_KEY)
        assert await router.acquire(_KEY, _SPEC) is first.sandbox

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure", [ValueError("configuration"), TimeoutError("uncertain"), RuntimeError("policy")]
)
def test_unclassified_failure_never_falls_back(failure):
    first, second = _backend("first", error=failure), _backend("second")
    with pytest.raises(type(failure)):
        asyncio.run(_router(first, second).acquire(_KEY, _SPEC))
    assert second.keys == []


def test_a_pinned_sandbox_never_falls_back_after_an_availability_failure():
    first, second = _backend("first"), _backend("second")
    router = _router(first, second)
    asyncio.run(router.acquire(_KEY, _SPEC))
    first.acquire_error = SandboxBackendUnavailable("lost")
    with pytest.raises(SandboxBackendUnavailable):
        asyncio.run(router.acquire(_KEY, _SPEC))
    assert second.keys == []


def test_fallback_transfers_backend_admission_and_cleanup():
    events = []

    class Owned(InProcessSandboxBackend):
        @asynccontextmanager
        async def call_admission(self, key, spec, *, owner, timeout):
            events.append((self.name, "enter"))
            try:
                yield nullcontext()
            finally:
                events.append((self.name, "exit"))

    first = Owned(name="first", acquire_error=SandboxBackendUnavailable("offline"))
    second = Owned(name="second")
    router = _router(first, second)

    async def exercise():
        admission = await router.enter_call(_KEY, _SPEC, owner="call")
        sandbox = await router.acquire(_KEY, _SPEC, _admission=admission)
        assert admission.backend is second
        assert admission.channel is second.declarations.program_channels[0]
        assert admission.rung is Cleanup.DISPOSE
        assert events == [("first", "enter"), ("first", "exit"), ("second", "enter")]
        await router.finish_call(_KEY, _SPEC, admission=admission, sandbox=sandbox, owner="call")
        assert events[-1] == ("second", "exit")
        assert second.disposed
        assert not first.disposed

    asyncio.run(exercise())


def test_host_tools_without_an_explicit_channel_are_refused():
    backend = InProcessSandboxBackend(
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            program_channels=(),
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.HOST_TOOLS},
        )
    )
    with pytest.raises(SandboxCapabilityNotSupported):
        _router(backend).ensure_can_serve(_SPEC)


def test_profile_failure_retires_the_instance_without_fallback():
    first = _backend("first")
    first._declarations = replace(first.declarations, program_channels=(ExecProgramChannel(),))
    second = _backend("second")
    with pytest.raises(ValueError, match="profile|satisfy"):
        asyncio.run(_router(first, second).acquire(_KEY, _SPEC))
    assert first.disposed
    assert second.keys == []


class _Observer(SandboxObserver):
    def __init__(self):
        self.events: list[HostToolCalled] = []

    def host_tool_called(self, event):
        self.events.append(event)


def _run(*, limit=2):
    observer = _Observer()
    effects = []

    @sandbox_tool(source=None, sink=None, identity=None)
    def value():
        effects.append("executed")
        return "value"

    registry = HostToolRegistry(observer=observer, response_limits=TransferLimits(100, 100, limit))
    registry.register(value)
    return HostToolRun(registry), observer, effects


async def _accept(result):
    pass


def test_publication_failure_records_execution_without_claiming_delivery():
    run, observer, effects = _run()

    async def failed(result):
        raise OSError("write failed")

    with pytest.raises(OSError):
        asyncio.run(run.call("value", publish=failed))
    (event,) = observer.events
    assert event.host_started and event.host_completed
    assert event.outcome == "delivery_uncertain"
    assert event.response_bytes == 0
    assert run._delivered == run._delivered_bytes == 0
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(run.call("value", publish=_accept))
    assert effects == ["executed"]


def test_pending_publication_reserves_budget_without_marking_delivery():
    run, observer, effects = _run(limit=1)

    async def exercise():
        prepared, release = asyncio.Event(), asyncio.Event()

        async def publish(result):
            prepared.set()
            await release.wait()

        first = asyncio.create_task(run.call("value", publish=publish, framing_bytes=3))
        await prepared.wait()
        assert run._delivered == run._delivered_bytes == 0
        assert observer.events == []
        assert not (await run.call("value", publish=_accept)).ok
        assert effects == ["executed"]
        release.set()
        assert (await first).ok
        assert observer.events[-1].response_bytes == len('"value"') + 3
        assert observer.events[-1].outcome == "delivered"

    asyncio.run(exercise())


def test_cancellation_during_publication_closes_the_run():
    run, observer, effects = _run()

    async def exercise():
        ready = asyncio.Event()

        async def publish(result):
            ready.set()
            await asyncio.Event().wait()

        pending = asyncio.create_task(run.call("value", publish=publish))
        await ready.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(RuntimeError, match="closed"):
            await run.call("value", publish=_accept)

    asyncio.run(exercise())
    assert effects == ["executed"]
    assert observer.events[-1].outcome == "delivery_uncertain"


def test_close_during_identity_mint_prevents_host_execution():
    async def exercise():
        ready, release = asyncio.Event(), asyncio.Event()
        effects = []
        from maf_sandbox import Identity

        async def mint(run_id):
            ready.set()
            await release.wait()
            return "identity"

        @sandbox_tool(source=None, sink=None, identity=Identity.USER)
        def value(*, user_identity):
            effects.append(user_identity)
            return 1

        registry = HostToolRegistry(
            mint_user_identity=mint, allowed_identities=frozenset({Identity.USER})
        )
        registry.register(value)
        run = HostToolRun(registry)
        pending = asyncio.create_task(run.call("value", publish=_accept))
        await ready.wait()
        run.close()
        release.set()
        with pytest.raises(RuntimeError, match="closed"):
            await pending
        assert effects == []
        assert run._minted_user_identity is None

    asyncio.run(exercise())


@pytest.mark.parametrize("stubborn", [False, True])
def test_callback_timeout_recovers_only_after_the_host_task_stops(stubborn):
    import time

    from maf_sandbox._host_tools import BoundedHostToolPolicy
    from maf_sandbox._reclaim import close_unclean_notes, open_unclean_notes
    from maf_sandbox.testing import InProcessSandbox

    async def exercise():
        release, stopped = asyncio.Event(), asyncio.Event()

        @sandbox_tool(source=None, sink=None, identity=None)
        async def wait():
            try:
                await release.wait()
            except asyncio.CancelledError:
                if stubborn:
                    await release.wait()
                raise
            finally:
                stopped.set()

        registry = HostToolRegistry()
        registry.register(wait)
        run = HostToolRun(registry)
        policy = BoundedHostToolPolicy(
            run, InProcessSandbox(), deadline=time.monotonic() + 10, timeout=0.01
        )
        answers = []

        async def publish(result):
            answers.append(result)

        notes, token = open_unclean_notes()
        try:
            if stubborn:
                with pytest.raises(TimeoutError, match="cleanup budget"):
                    await policy.call("wait", publish=publish)
                assert notes and not stopped.is_set()
                assert answers == []
                with pytest.raises(RuntimeError, match="closed"):
                    await run.call("wait", publish=publish)
            else:
                answer = await policy.call("wait", publish=publish)
                assert stopped.is_set() and answer.refusal and "timed out" in answer.refusal
                assert answers == [answer] and notes == []
                answer = await policy.call("missing", publish=publish)
                assert answer.refusal and "not a registered" in answer.refusal
        finally:
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            close_unclean_notes(token)

    asyncio.run(exercise())


def test_program_validation_failure_revokes_the_supplied_policy():
    from maf_sandbox.testing import InProcessSandbox

    run, _, _ = _run()

    async def exercise():
        with pytest.raises(ValueError, match="byte limit"):
            await ExecProgramChannel().run(
                InProcessSandbox(),
                "too long",
                requirements=ProgramRequirements(max_program_bytes=1),
                guest_call_path="run",
                timeout=5,
                policy=run,
            )
        with pytest.raises(RuntimeError, match="closed"):
            await run.call("value", publish=_accept)

    asyncio.run(exercise())


class _NativeChannel(InProcessProgramChannel):
    def required_capabilities(self, spec):
        return frozenset({Capability.RUN_CODE})

    def transfer_limits(self, spec):
        from maf_sandbox import SandboxLimits

        return SandboxLimits(files_in=TransferLimits(0, 0, 0), files_out=TransferLimits(0, 0, 0))


def test_native_host_tools_do_not_require_file_or_exec_capabilities():
    from maf_sandbox import SandboxLimits

    registry = HostToolRegistry(response_limits=TransferLimits(1024, 4096, 2))
    registry.register(sandbox_tool(source=None, sink=None, identity=None)(lambda: 1), name="echo")
    channel = _NativeChannel(mode="runtime")
    backend = InProcessSandboxBackend(
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            program_channels=(channel,),
            capabilities=frozenset({Capability.RUN_CODE, Capability.HOST_TOOLS}),
            limits=SandboxLimits(
                files_in=TransferLimits(0, 0, 0), files_out=TransferLimits(0, 0, 0)
            ),
        )
    )
    spec = replace(
        _SPEC,
        requires=frozenset({Capability.HOST_TOOLS}),
        host_tools=registry.aggregate(),
        work_dir=None,
    )
    assert asyncio.run(_router(backend).acquire(_KEY, spec)) is backend.sandbox
    assert backend.specs[-1].requires == frozenset({Capability.HOST_TOOLS, Capability.RUN_CODE})


def test_an_incompatible_exec_budget_can_select_another_channel_on_the_same_backend():
    from maf_sandbox import SandboxLimits

    native = _NativeChannel(name="native", mode="runtime")
    backend = InProcessSandboxBackend(
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            program_channels=(InProcessProgramChannel(), native),
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.RUN_CODE},
            limits=SandboxLimits(
                files_in=TransferLimits(0, 0, 0), files_out=TransferLimits(0, 0, 0)
            ),
        )
    )
    router = _router(backend)

    async def exercise():
        admission = await router.enter_call(_KEY, _SPEC, owner="call")
        assert admission.channel is native
        await router.release_call(_KEY, _SPEC.kind, owner="call")

    asyncio.run(exercise())


def test_a_pinned_channel_cannot_switch_to_fit_a_later_program():
    from maf_sandbox import SandboxLimits, SandboxTransferLimitsNotPermitted

    backend = InProcessSandboxBackend(
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            program_channels=(InProcessProgramChannel(), _NativeChannel(mode="runtime")),
            capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.RUN_CODE},
            limits=SandboxLimits(
                files_in=TransferLimits(100, 100, 1), files_out=TransferLimits(0, 0, 0)
            ),
        )
    )
    router = _router(backend)
    small = replace(_SPEC, program=ProgramRequirements(max_program_bytes=100))
    larger = replace(_SPEC, program=ProgramRequirements(max_program_bytes=101))

    async def exercise():
        await router.acquire(_KEY, small)
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            await router.acquire(_KEY, larger)
        with pytest.raises(SandboxTransferLimitsNotPermitted):
            await router.enter_call(_KEY, larger, owner="later")
        await router.dispose(_KEY)
        admission = await router.enter_call(_KEY, larger, owner="fresh")
        assert admission.channel.mode == "runtime"
        await router.release_call(_KEY, larger.kind, owner="fresh")

    asyncio.run(exercise())


def test_disposal_during_profile_verification_cannot_leave_a_pin():
    from maf_sandbox import SandboxUnclean

    async def exercise():
        ready, release = asyncio.Event(), asyncio.Event()

        class HeldProfile(InProcessProgramChannel):
            async def prepare(self, sandbox, requirements):
                ready.set()
                await release.wait()

        first, second = _backend("first"), _backend("second")
        first._declarations = replace(first.declarations, program_channels=(HeldProfile(),))
        router = _router(first, second)
        pending = asyncio.create_task(router.acquire(_KEY, _SPEC))
        await ready.wait()
        await router.dispose(_KEY)
        release.set()
        with pytest.raises(SandboxUnclean):
            await pending
        first.acquire_error = SandboxBackendUnavailable("offline")
        assert await router.acquire(_KEY, _SPEC) is second.sandbox

    asyncio.run(exercise())


def test_acquisition_observation_names_the_channel_that_actually_served():
    from maf_sandbox import EffectiveState

    events = []

    class Observer(SandboxObserver):
        def sandbox_acquired(self, event):
            events.append(event)

    first = _backend("first", error=SandboxBackendUnavailable("offline"))
    second = _backend("second", mode="runtime")
    asyncio.run(_router(first, second, observer=Observer()).acquire(_KEY, _SPEC))
    assert events[-1].program_channel == "second"
    state = EffectiveState.of(events[-1])
    assert state.program_channel == "second"
    assert state.execution_profile == "python-portable-v1"
