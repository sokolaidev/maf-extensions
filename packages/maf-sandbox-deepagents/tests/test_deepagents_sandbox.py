"""The adapter against the in-process fake: what Deep Agents is handed, and what the router keeps.

No container and no model. The fake declares `FILES_OUT` here because the adapter requires it,
and the router runs at `Isolation.NONE` because the fake declares no more — both are the test
wiring, not a posture a host would choose.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import threading
import time

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol, execute_accepts_timeout
from maf_sandbox import (
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    IsolationScope,
    NoSandboxBackend,
    SandboxBackendNotPermitted,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InProcessSandbox, InProcessSandboxBackend

from maf_sandbox_deepagents import (
    DEEPAGENTS_KIND,
    DEFAULT_EXEC_TIMEOUT_SECONDS,
    REQUIRED_CAPABILITIES,
    SANDBOX_UNAVAILABLE,
    MafSandbox,
    deepagents_spec,
)
from maf_sandbox_deepagents._sandbox import _response

KEY = SandboxKey(scope="tenant-a", thread_id="thread-1", agent_dir="coder")
WORK = "/maf-sandbox/work"

#: The fake plus the pull surface the adapter needs for `download_files`.
DECLARATIONS = dataclasses.replace(
    FAKE_BACKEND_DECLARATIONS,
    capabilities=FAKE_BACKEND_DECLARATIONS.capabilities | {Capability.FILES_OUT},
)


def _backend(sandbox: InProcessSandbox | None = None, **kwargs) -> InProcessSandboxBackend:
    return InProcessSandboxBackend(sandbox, declarations=DECLARATIONS, **kwargs)


def _router(backend: InProcessSandboxBackend) -> SandboxRouter:
    return SandboxRouter([backend], min_isolation=Isolation.NONE)


def _adapter(
    sandbox: InProcessSandbox | None = None, **kwargs
) -> tuple[MafSandbox, InProcessSandboxBackend]:
    backend = _backend(sandbox, **kwargs)
    return MafSandbox(_router(backend), KEY, deepagents_spec("img:1")), backend


class TestTheSpec:
    def test_requires_what_deep_agents_needs(self):
        spec = deepagents_spec("img:1")
        assert spec.kind == DEEPAGENTS_KIND
        assert spec.requires == REQUIRED_CAPABILITIES
        assert Capability.FILES_OUT in spec.requires
        # Left unset, as the shipped kinds leave it: the plain Docker constructor declares no
        # guest family, and the tools' needs (`sh`, `python3`) are the image's, not the shape's.
        assert spec.requires_os_family is None

    def test_egress_is_closed_unless_hosts_are_named(self):
        assert deepagents_spec("img:1").egress is Egress.CLOSED
        allowed = deepagents_spec("img:1", egress_allow=("pypi.org",))
        assert allowed.egress is Egress.ALLOWLIST
        assert allowed.egress_allow == ("pypi.org",)

    def test_the_work_dir_default_is_the_protocol_s(self):
        assert deepagents_spec("img:1").work_dir == SandboxSpec(kind="x").work_dir
        assert deepagents_spec("img:1", work_dir="/w").work_dir == "/w"


class TestConstruction:
    def test_is_a_deep_agents_sandbox_with_a_per_command_timeout(self):
        adapter, _ = _adapter()
        assert isinstance(adapter, SandboxBackendProtocol)
        assert execute_accepts_timeout(MafSandbox)

    def test_refuses_a_spec_missing_a_required_capability(self):
        spec = dataclasses.replace(deepagents_spec("img:1"), requires=frozenset({Capability.EXEC}))
        with pytest.raises(ValueError, match="files_in"):
            MafSandbox(_router(_backend()), KEY, spec)

    def test_refuses_a_per_call_scope(self):
        spec = dataclasses.replace(deepagents_spec("img:1"), isolation_scope=IsolationScope.CALL)
        with pytest.raises(ValueError, match="whole conversation"):
            MafSandbox(_router(_backend()), KEY, spec)

    def test_refuses_an_open_egress_however_the_spec_was_built(self):
        spec = dataclasses.replace(deepagents_spec("img:1"), egress=Egress.UNRESTRICTED)
        with pytest.raises(ValueError, match="egress"):
            MafSandbox(_router(_backend()), KEY, spec)

    def test_refuses_a_key_naming_a_call(self):
        with pytest.raises(ValueError, match="call_id"):
            MafSandbox(
                _router(_backend()),
                dataclasses.replace(KEY, call_id="c1"),
                deepagents_spec("img:1"),
            )

    def test_refuses_a_backend_without_the_pull_surface(self):
        """The router's capability match, asked at construction rather than at the first command."""
        backend = InProcessSandboxBackend(declarations=FAKE_BACKEND_DECLARATIONS)
        with pytest.raises(SandboxCapabilityNotSupported):
            MafSandbox(_router(backend), KEY, deepagents_spec("img:1"))

    @pytest.mark.parametrize("budget", [0, -1, 1.5, True])
    def test_refuses_an_output_budget_that_bounds_nothing(self, budget: object):
        with pytest.raises(ValueError, match="max_output_bytes"):
            MafSandbox(
                _router(_backend()),
                KEY,
                deepagents_spec("img:1"),
                max_output_bytes=budget,  # pyright: ignore[reportArgumentType]
            )

    def test_refuses_a_spec_raising_the_floor_above_the_backend(self):
        with pytest.raises(SandboxBackendNotPermitted):
            MafSandbox(
                _router(_backend()), KEY, deepagents_spec("img:1", min_isolation=Isolation.MICROVM)
            )

    @pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
    def test_refuses_a_timeout_that_bounds_nothing(self, seconds: float):
        with pytest.raises(ValueError, match="exec_timeout_seconds"):
            MafSandbox(
                _router(_backend()), KEY, deepagents_spec("img:1"), exec_timeout_seconds=seconds
            )

    def test_refuses_a_router_with_no_backend(self):
        with pytest.raises(NoSandboxBackend):
            MafSandbox(
                SandboxRouter([], min_isolation=Isolation.NONE), KEY, deepagents_spec("img:1")
            )

    def test_refuses_a_base_the_backend_would_allocate(self):
        with pytest.raises(ValueError, match="work_dir"):
            MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", work_dir=None))

    @pytest.mark.parametrize("work_dir", ["relative/base", "", "/with\0nul"])
    def test_refuses_a_base_the_backends_would(self, work_dir: str):
        """The backends' rule for a named base, applied at construction: a host error is
        reported where the host is, not as a sandbox unavailable on the first command."""
        with pytest.raises(ValueError, match="work_dir"):
            MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", work_dir=work_dir))


class TestTheId:
    def test_is_opaque(self):
        first, _ = _adapter()
        assert first.id.startswith("maf-sandbox-")
        for part in (KEY.scope, KEY.thread_id, KEY.agent_dir):
            assert part not in first.id

    def test_names_the_sandbox_the_router_reaches(self):
        """Two adapters over one key, kind and backend reach one sandbox, and say so."""
        first, _ = _adapter()
        second, _ = _adapter()
        assert first.id == second.id

    def test_differs_by_conversation_kind_and_backend(self):
        base, _ = _adapter()
        other_thread = MafSandbox(
            _router(_backend()),
            dataclasses.replace(KEY, thread_id="thread-2"),
            deepagents_spec("img:1"),
        )
        other_kind = MafSandbox(_router(_backend()), KEY, deepagents_spec("img:1", kind="shell"))
        other_backend = MafSandbox(_router(_backend(name="second")), KEY, deepagents_spec("img:1"))
        other_egress = MafSandbox(
            _router(_backend()), KEY, deepagents_spec("img:1", egress_allow=("pypi.org",))
        )
        assert (
            len({base.id, other_thread.id, other_kind.id, other_backend.id, other_egress.id}) == 5
        )

    def test_the_encoding_keeps_field_boundaries(self):
        """A scope ending where a thread begins must not collide with the split moved."""
        shifted = SandboxKey(scope="tenant-", thread_id="athread-1", agent_dir="coder")
        base, _ = _adapter()
        other = MafSandbox(_router(_backend()), shifted, deepagents_spec("img:1"))
        assert base.id != other.id


class TestExecute:
    def test_runs_the_command_in_the_storage_base_under_the_default_timeout(self):
        """The base is addressed as `"."`; the backend resolves it to the spec's `work_dir`."""
        fake = InProcessSandbox(outputs={"echo": "hello\n"})
        adapter, _ = _adapter(fake)

        response = asyncio.run(adapter.aexecute("echo hello"))

        assert response.output == "hello\n"
        assert response.exit_code == 0
        assert response.truncated is False
        ((command, directory, bound),) = fake.commands
        assert (command, directory) == ("echo hello", WORK)
        assert DEFAULT_EXEC_TIMEOUT_SECONDS - 0.5 < bound <= DEFAULT_EXEC_TIMEOUT_SECONDS
        assert adapter.spec.work_dir == WORK

    def test_a_per_command_timeout_bounds_the_command_less_what_the_acquire_spent(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        asyncio.run(adapter.aexecute("true", timeout=7))
        assert 6.5 < fake.commands[0][2] <= 7.0

    def test_an_acquire_that_outlives_the_budget_is_cut_off(self, monkeypatch: pytest.MonkeyPatch):
        """The deadline bounds the acquire too: a queued or slow create cannot exceed it."""
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        adapter = MafSandbox(adapter.router, KEY, adapter.spec, exec_timeout_seconds=0.05)
        acquire = adapter.router.acquire

        async def slow_acquire(*args, **kwargs):
            await asyncio.sleep(5)
            return await acquire(*args, **kwargs)

        monkeypatch.setattr(adapter.router, "acquire", slow_acquire)

        started = time.monotonic()
        response = asyncio.run(adapter.aexecute("true"))

        assert time.monotonic() - started < 1
        assert response.exit_code is None
        assert "0.05 seconds" in response.output
        assert fake.commands == []

    def test_output_past_the_budget_is_dropped_whole_and_said_so(self):
        fake = InProcessSandbox(outputs={"seq": "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"})
        adapter, backend = _adapter(fake)
        adapter = MafSandbox(adapter.router, KEY, adapter.spec, max_output_bytes=8)

        # Warm first: the router's adoption of an unfamiliar instance disposes once on its own.
        asyncio.run(adapter.aexecute("true"))
        before = len(backend.disposed)

        async def scenario():
            response = await adapter.aexecute("seq 10")
            await adapter.aclose()  # joins the delete the overflow started
            return response

        response = asyncio.run(scenario())

        assert response.truncated is True
        assert response.exit_code is None
        assert "8 bytes" in response.output
        assert "1" not in response.output.replace("8 bytes", "")
        # Nothing says the program stopped when the host stopped reading, so the sandbox goes;
        # `aclose` then names the same instance again, a no-op under the protocol.
        assert backend.disposed[before:] == [KEY, KEY]

    def test_the_rendered_stream_is_held_to_the_budget(self):
        """The stderr prefixes grow the stream, so short lines under the raw budget could step
        over it once rendered; the rendered stream is what the model reads."""

        class Chatty(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                return ExecResult(stdout="", stderr="x\n" * 4)

        adapter, backend = _adapter(Chatty())
        adapter = MafSandbox(adapter.router, KEY, adapter.spec, max_output_bytes=16)
        asyncio.run(adapter.aexecute("true"))  # warm, and adopted
        before = len(backend.disposed)

        response = asyncio.run(adapter.aexecute("chatter"))

        assert response.truncated is True
        assert response.exit_code is None
        assert "16 bytes" in response.output
        # The command ended and its output was read whole: nothing is unknown, nothing goes.
        assert backend.disposed[before:] == []

    def test_a_sandbox_that_cannot_bound_output_runs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ):
        class Unbounded(InProcessSandbox):
            exec_bounded = None  # type: ignore[assignment]  # opts out of `BoundedExec`

        fake = Unbounded()
        adapter, _ = _adapter(fake)
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))

        assert response.exit_code is None
        assert "did not run" in response.output
        assert fake.commands == []
        assert "exec_bounded" in caplog.text

    def test_an_empty_command_is_an_error_result_not_a_raise(self):
        adapter, _ = _adapter()
        response = asyncio.run(adapter.aexecute(""))
        assert response.exit_code == 1
        assert "non-empty" in response.output

    @pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
    def test_a_timeout_that_bounds_nothing_raises(self, seconds: float):
        adapter, _ = _adapter()
        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(adapter.aexecute("true", timeout=seconds))  # pyright: ignore[reportArgumentType]

    def test_the_sandbox_is_reused_warm_under_this_key_and_kind(self):
        """The router cleans an instance it has never seen before the first command, which is
        one extra create and dispose on a fresh conversation; every command after that reuses."""
        adapter, backend = _adapter(InProcessSandbox())
        asyncio.run(adapter.aexecute("one"))
        adopted = len(backend.keys)
        asyncio.run(adapter.aexecute("two"))
        assert len(backend.keys) == adopted + 1
        assert set(backend.keys) == {KEY}
        assert {spec.kind for spec in backend.specs} == {DEEPAGENTS_KIND}

    def test_an_unavailable_sandbox_is_a_fixed_sentence_with_the_detail_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        detail = "subscription 0000-1111 refused the create"
        adapter, _ = _adapter(acquire_error=RuntimeError(detail))
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))
        assert response.output == SANDBOX_UNAVAILABLE
        assert response.exit_code is None
        assert "subscription" not in response.output
        assert detail in caplog.text

    def test_a_failure_to_run_is_a_fixed_sentence_with_the_detail_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        detail = "docker exec: subscription 0000-1111 refused"
        adapter, _ = _adapter(InProcessSandbox(raises=RuntimeError(detail)))
        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            response = asyncio.run(adapter.aexecute("true"))
        assert response.exit_code is None
        assert "subscription" not in response.output
        assert response.output != SANDBOX_UNAVAILABLE
        assert "result" in response.output
        assert detail in caplog.text

    def test_a_failure_to_run_condemns_the_sandbox_too(self):
        """The result did not come back, so the command's end is as unknown as after a timeout."""
        adapter, backend = _adapter(InProcessSandbox(raises=RuntimeError("transport gone")))

        async def scenario():
            await adapter.aexecute("true")
            return await adapter.aclose()

        assert asyncio.run(scenario()) is True
        # Adoption, the queued delete, and `aclose` naming the instance again (a no-op).
        assert backend.disposed == [KEY, KEY, KEY]
        assert backend.disposed_instances[1] == backend.disposed_instances[2]

    def test_the_acquire_is_handed_the_call_s_admission(self, monkeypatch: pytest.MonkeyPatch):
        """The admission retains the backend that admitted the call; the acquire must use it,
        as the framework's own glue does, or a re-routed acquire could serve from another."""
        adapter, backend = _adapter(InProcessSandbox())
        acquire = adapter.router.acquire
        admitted_backends: list[object] = []

        async def spying_acquire(*args, **kwargs):
            admission = kwargs.get("_admission")
            admitted_backends.append(None if admission is None else admission.backend)
            return await acquire(*args, **kwargs)

        monkeypatch.setattr(adapter.router, "acquire", spying_acquire)
        asyncio.run(adapter.aexecute("true"))

        assert admitted_backends == [backend]

    def test_a_cancelled_acquire_releases_its_admission(self, monkeypatch: pytest.MonkeyPatch):
        """An admission that outlived its call would block an exclusive close for good."""
        adapter, _ = _adapter(InProcessSandbox())
        acquire = adapter.router.acquire

        async def hanging_acquire(*args, **kwargs):
            await asyncio.sleep(10)
            return await acquire(*args, **kwargs)

        async def scenario():
            monkeypatch.setattr(adapter.router, "acquire", hanging_acquire)
            call = asyncio.create_task(adapter.aexecute("true", timeout=60))
            await asyncio.sleep(0.05)
            call.cancel()
            await asyncio.wait({call})
            assert call.cancelled()
            monkeypatch.setattr(adapter.router, "acquire", acquire)
            return await asyncio.wait_for(adapter.aclose(), 2)

        assert asyncio.run(scenario()) is True

    def test_a_queued_delete_that_failed_is_retried_by_close(self):
        fake = InProcessSandbox(raises=TimeoutError())
        adapter, backend = _adapter(fake)

        async def scenario():
            await adapter.aexecute("true")  # warm, and adopted
            backend.dispose_failure = DisposalFailure("timeout", "still there")
            await adapter.aexecute("sleep 999", timeout=3)  # the queued delete fails
            failed = await adapter.aclose()  # retries, and fails the same way
            backend.dispose_failure = None
            return failed, await adapter.aclose()

        failed, retried = asyncio.run(scenario())
        assert failed is False
        assert retried is True
        assert backend.disposed_instances[-1] == fake.instance_id

    def test_a_timeout_disposes_the_sandbox(self):
        """A timeout says the wait ended, not that the program did; the next command starts cold."""
        adapter, backend = _adapter(InProcessSandbox(raises=TimeoutError()))

        # Two loops on purpose: the delete outlives the loop the timed-out call ran on, and
        # `aclose` on another loop joins it.
        asyncio.run(adapter.aexecute("sleep 999", timeout=3))
        closed = asyncio.run(adapter.aclose())

        assert closed is True
        # The router's adoption of an unfamiliar instance disposes once; the queued delete,
        # once more; `aclose` names the same instance again, a no-op under the protocol that
        # the fake records, so a delete that failed would be retried there.
        assert backend.disposed == [KEY, KEY, KEY]
        assert backend.disposed_instances[1] == backend.disposed_instances[2]
        assert backend.disposed_kinds[-1] == DEEPAGENTS_KIND

    def test_a_parallel_healthy_call_finishes_before_the_delete_a_timeout_started(self):
        """The router asks callers to keep active calls off a sandbox being deleted, and Deep
        Agents may run tool calls in parallel."""
        disposed_when_healthy_done: list[int] = []

        class Mixed(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                self.commands.append((str(command), working_directory, timeout))
                if "slow" in str(command):
                    await asyncio.sleep(0.05)
                    raise TimeoutError()
                await asyncio.sleep(0.3)
                disposed_when_healthy_done.append(len(backend.disposed))
                return ExecResult(stdout="ok")

        adapter, backend = _adapter(Mixed())

        async def scenario():
            slow, healthy = await asyncio.gather(
                adapter.aexecute("slow", timeout=5), adapter.aexecute("healthy", timeout=5)
            )
            await adapter.aclose()  # joins the delete the timeout started
            return slow, healthy

        slow, healthy = asyncio.run(scenario())

        assert slow.exit_code is None
        assert healthy.output == "ok"
        # Only the adoption's own delete had happened when the healthy call finished; the
        # queued delete landed after it.
        assert disposed_when_healthy_done == [1]
        assert len(backend.disposed) >= 2
        assert backend.disposed[-1] == KEY

    def test_a_sibling_adapters_call_finishes_before_the_delete_this_ones_timeout_started(self):
        """Two adapters over one router, key and kind share one instance; the lifecycle that
        the delete waits on is the router's, so the sibling's call is counted too."""
        disposed_when_healthy_done: list[int] = []

        class Mixed(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                self.commands.append((str(command), working_directory, timeout))
                if "slow" in str(command):
                    await asyncio.sleep(0.05)
                    raise TimeoutError()
                await asyncio.sleep(0.3)
                disposed_when_healthy_done.append(len(backend.disposed))
                return ExecResult(stdout="ok")

        first, backend = _adapter(Mixed())
        second = MafSandbox(first.router, KEY, first.spec)

        async def scenario():
            slow, healthy = await asyncio.gather(
                second.aexecute("slow", timeout=5), first.aexecute("healthy", timeout=5)
            )
            closed = await first.aclose()
            return slow, healthy, closed

        slow, healthy, closed = asyncio.run(scenario())

        assert slow.exit_code is None
        assert healthy.output == "ok"
        assert closed is True
        # Only the adoption's own delete had happened when the healthy call finished; the
        # queued delete landed after it.
        assert disposed_when_healthy_done == [1]
        assert len(backend.disposed) >= 2
        assert backend.disposed[-1] == KEY

    def test_a_call_still_acquiring_is_counted_before_the_delete_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Admission comes before the acquire, so a slow acquire is a call in flight too."""
        disposed_when_healthy_done: list[int] = []

        class Mixed(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                self.commands.append((str(command), working_directory, timeout))
                if "slow" in str(command):
                    await asyncio.sleep(0.05)
                    raise TimeoutError()
                disposed_when_healthy_done.append(len(backend.disposed))
                return ExecResult(stdout="ok")

        adapter, backend = _adapter(Mixed())
        acquire = adapter.router.acquire
        delayed: list[bool] = []

        async def slow_first_acquire(*args, **kwargs):
            if not delayed:
                delayed.append(True)
                await asyncio.sleep(0.3)
            return await acquire(*args, **kwargs)

        async def scenario():
            monkeypatch.setattr(adapter.router, "acquire", slow_first_acquire)
            healthy_call = asyncio.create_task(adapter.aexecute("healthy", timeout=5))
            await asyncio.sleep(0.05)  # the healthy call is admitted and still acquiring
            slow = await adapter.aexecute("slow", timeout=5)
            healthy = await healthy_call
            closed = await adapter.aclose()
            return slow, healthy, closed

        slow, healthy, closed = asyncio.run(scenario())

        assert slow.exit_code is None
        assert healthy.output == "ok"
        assert closed is True
        assert disposed_when_healthy_done == [1]
        assert len(backend.disposed) >= 2
        assert backend.disposed[-1] == KEY

    def test_a_cancelled_command_disposes_the_sandbox_and_stays_cancelled(self):
        class Hanging(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                await asyncio.sleep(10)
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        adapter, backend = _adapter(Hanging())

        async def scenario():
            call = asyncio.create_task(adapter.aexecute("sleep 10", timeout=60))
            await asyncio.sleep(0.05)
            call.cancel()
            await asyncio.wait({call})
            assert call.cancelled()  # the cancellation reached the caller unchanged
            return await adapter.aclose()  # joins the delete the cancellation started

        assert asyncio.run(scenario()) is True
        # Adoption, the queued delete, and `aclose` naming the instance again (a no-op).
        assert backend.disposed == [KEY, KEY, KEY]
        assert backend.disposed_instances[1] == backend.disposed_instances[2]

    def test_the_delete_after_a_timeout_runs_past_the_answer_and_the_next_call_waits_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        disposed_when_run: list[int] = []

        class OnceSlow(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                if "sleep" in str(command):
                    self.commands.append((str(command), working_directory, timeout))
                    raise TimeoutError()
                disposed_when_run.append(len(backend.disposed))
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        adapter, backend = _adapter(OnceSlow())
        dispose = backend.dispose

        async def slow_dispose(*args, **kwargs):
            await asyncio.sleep(0.3)
            return await dispose(*args, **kwargs)

        async def scenario():
            await adapter.aexecute("true")  # warm: the adoption's own delete happens here
            disposed_when_run.clear()
            monkeypatch.setattr(backend, "dispose", slow_dispose)
            started = time.monotonic()
            timed_out = await adapter.aexecute("sleep 999", timeout=5)
            answered_after = time.monotonic() - started
            in_flight = len(backend.disposed)
            again = await adapter.aexecute("true")
            return timed_out, answered_after, in_flight, again

        timed_out, answered_after, in_flight, again = asyncio.run(scenario())

        assert timed_out.exit_code is None
        assert answered_after < 0.2  # the 0.3-second delete did not extend the answer
        assert in_flight == 1  # the adoption's own; the timeout's delete was still running
        assert again.exit_code == 0
        # By the time the next command ran, the delete had landed (and nothing ran after it).
        assert len(backend.disposed) >= 2
        assert disposed_when_run == [len(backend.disposed)]

    def test_a_timeout_is_reported_as_one_and_claims_no_stop(self):
        adapter, _ = _adapter(InProcessSandbox(raises=TimeoutError()))
        response = asyncio.run(adapter.aexecute("sleep 999", timeout=3))
        assert response.exit_code is None
        assert "3 seconds" in response.output
        assert "stopped" not in response.output


class TestTheCombinedStream:
    def test_stderr_lines_are_labelled(self):
        response = _response(ExecResult(stdout="out\n", stderr="warn 1\nwarn 2\n", exit_code=2))
        assert response.output == "out\n\n[stderr] warn 1\n[stderr] warn 2"
        assert response.exit_code == 2

    def test_a_producer_s_note_is_labelled_as_the_host_s(self):
        response = _response(
            ExecResult(stdout="", stderr="output dropped", producer_owns_stderr=True)
        )
        assert response.output == "[note] output dropped"

    def test_nothing_is_said_so(self):
        assert _response(ExecResult(stdout="")).output == "<no output>"

    def test_the_prefixes_count_against_the_budget(self):
        within = _response(ExecResult(stdout="", stderr="a\nb\n"), max_output_bytes=24)
        over = _response(ExecResult(stdout="", stderr="a\nb\n"), max_output_bytes=16)
        assert within.output == "[stderr] a\n[stderr] b"
        assert over.truncated is True
        assert over.exit_code is None


class TestFilesIn:
    def test_uploads_land_under_the_work_dir(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.aupload_files([("main.bicep", b"param x string"), ("sub/two.txt", b"2")])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("main.bicep", None),
            ("sub/two.txt", None),
        ]
        assert fake.contents[f"{WORK}/main.bicep"] == b"param x string"
        assert fake.contents[f"{WORK}/sub/two.txt"] == b"2"

    def test_paths_are_guest_paths_and_the_base_is_the_file_planes_reach(self):
        """Deep Agents' file tools spell paths absolutely; under the base the file plane serves
        them, and a relative path that climbs out of it is the plane's own refusal."""
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.aupload_files(
                [
                    (f"{WORK}/notes/todo.txt", b"1"),
                    ("../etc/passwd", b"x"),
                    ("/notes/todo.txt", b"2"),
                ]
            )
        )

        assert [(r.path, r.error) for r in responses] == [
            (f"{WORK}/notes/todo.txt", None),
            ("../etc/passwd", "invalid_path"),
            ("/notes/todo.txt", None),
        ]
        # Under the base: the plane. Outside it: the shell, never the plane.
        assert sorted(fake.contents) == [f"{WORK}/notes/todo.txt"]
        commands = [command for command, _, _ in fake.commands]
        assert commands[0].startswith("mkdir -p /notes && if [ -d /notes/todo.txt ]")
        assert "base64 -d >> /notes/todo.txt." in commands[1]
        assert commands[2].startswith("mv -f /notes/todo.txt.") and commands[2].endswith(
            ".part /notes/todo.txt"
        )
        (read,) = asyncio.run(adapter.adownload_files([f"{WORK}/notes/todo.txt"]))
        assert read.content == b"1"

    def test_the_planes_permission_refusal_keeps_its_code(self):
        class Unsearchable(InProcessSandbox):
            async def write_file(self, path, content, *, working_directory):
                raise PermissionError("an ancestor is not searchable")

        adapter, _ = _adapter(Unsearchable())
        (response,) = asyncio.run(adapter.aupload_files([("locked/f.txt", b"1")]))
        assert response.error == "permission_denied"

    def test_a_plane_write_that_did_not_finish_condemns_and_fails_the_batch(self):
        """The plane does not promise a whole file or none, so a write that failed midway may
        have left part of it; the batch ends as it would on the shell road."""

        class Broken(InProcessSandbox):
            async def write_file(self, path, content, *, working_directory):
                raise RuntimeError("tar stream reset")

        adapter, backend = _adapter(Broken())

        async def scenario():
            responses = await adapter.aupload_files([("a.txt", b"1"), ("b.txt", b"2")])
            await adapter.aclose()
            return responses

        first, second = asyncio.run(scenario())
        assert first.error is not None and "did not land" in first.error
        assert second.error == first.error
        assert backend.disposed[-1] == KEY

    def test_a_base_spelled_with_dots_still_bounds_the_file_plane(self):
        """A spec may write the base as `/a/../b`; the backends resolve it, and so must the
        road choice, or a file under it would take the shell for nothing."""
        fake = InProcessSandbox()
        adapter = MafSandbox(
            _router(_backend(fake)), KEY, deepagents_spec("img:1", work_dir="/maf-sandbox/../b")
        )

        (response,) = asyncio.run(adapter.aupload_files([("/b/f.txt", b"1")]))

        assert response.error is None
        assert fake.commands == []  # the file plane, not the shell
        assert any(path.endswith("b/f.txt") for path in fake.contents)

    def test_a_shell_upload_carries_the_bytes_in_chunks_the_shell_can_take(self):
        """Deep Agents' large-edit temporaries land under `/tmp`, outside the base, whole."""
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        content = bytes(range(256)) * 400  # 100 KiB: three chunks
        adapter = MafSandbox(
            adapter.router,
            KEY,
            dataclasses.replace(
                adapter.spec,
                files_in=TransferLimits(
                    max_bytes_per_file=200_000, max_total_bytes=200_000, max_files=8
                ),
            ),
        )

        (response,) = asyncio.run(adapter.aupload_files([("/tmp/.deepagents_edit_x_old", content)]))

        assert response.error is None
        commands = [command for command, _, _ in fake.commands]
        staged = commands[0].removeprefix(
            "mkdir -p /tmp && if [ -d /tmp/.deepagents_edit_x_old ]; then "
            "echo 'Is a directory' >&2; exit 1; fi && : > "
        )
        assert staged.startswith("/tmp/.deepagents_edit_x_old.") and staged.endswith(".part")
        chunks = [c.removeprefix("printf %s ").split(" | ")[0] for c in commands[1:-1]]
        assert len(chunks) == 3
        assert all(len(chunk) <= 65536 for chunk in chunks)
        assert all(c.endswith(f"base64 -d >> {staged}") for c in commands[1:-1])
        assert base64.b64decode("".join(chunks)) == content
        assert commands[-1] == f"mv -f {staged} /tmp/.deepagents_edit_x_old"
        # A second write over the same path stages beside it under its own name, so two
        # writers admitted together each land a whole file and the last one stands.
        asyncio.run(adapter.aupload_files([("/tmp/.deepagents_edit_x_old", b"again")]))
        again = [command for command, _, _ in fake.commands][len(commands) :]
        assert again[0] != commands[0]
        assert again[-1].endswith(" /tmp/.deepagents_edit_x_old") and again[-1] != commands[-1]

    def test_what_the_shell_refuses_comes_back_by_code(self):
        class Refusing(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                await super().exec(command, working_directory=working_directory, timeout=timeout)
                return ExecResult(
                    stdout="", stderr="sh: can't create /etc/x: Permission denied", exit_code=1
                )

        adapter, _ = _adapter(Refusing())
        (response,) = asyncio.run(adapter.aupload_files([("/etc/x", b"1")]))
        assert response.error == "permission_denied"

    def test_a_shell_upload_that_raises_fails_the_batch_with_the_detail_logged(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A failure that kept the result from coming back is an unknown end, as a timeout is."""
        detail = "docker exec: subscription 0000-1111 refused"
        adapter, backend = _adapter(InProcessSandbox(raises=RuntimeError(detail)))

        async def scenario():
            responses = await adapter.aupload_files([("/tmp/x", b"1")])
            await adapter.aclose()
            return responses

        with caplog.at_level(logging.ERROR, logger="maf_sandbox_deepagents"):
            (response,) = asyncio.run(scenario())
        assert response.error is not None and "did not land" in response.error
        assert "subscription" not in response.error
        assert detail in caplog.text
        assert backend.disposed[-1] == KEY

    def test_a_shell_upload_that_times_out_disposes_and_fails_the_batch(self):
        adapter, backend = _adapter(InProcessSandbox(raises=TimeoutError()))

        async def scenario():
            responses = await adapter.aupload_files([("kept.txt", b"1"), ("/tmp/x", b"2")])
            await adapter.aclose()  # joins the delete the timeout started
            return responses

        responses = asyncio.run(scenario())
        assert [r.error is not None and "did not land" in r.error for r in responses] == [
            True,
            True,
        ]
        assert backend.disposed[-1] == KEY

    def test_a_shell_upload_whose_command_floods_output_disposes_and_fails_the_batch(self):
        """Past the 4 KiB budget the write may still be running, as after a timeout."""
        adapter, backend = _adapter(InProcessSandbox(outputs={"base64 -d": "y" * 5000}))

        async def scenario():
            responses = await adapter.aupload_files([("/tmp/x", b"1")])
            await adapter.aclose()
            return responses

        (response,) = asyncio.run(scenario())
        assert response.error is not None and "did not land" in response.error
        assert backend.disposed[-1] == KEY

    def test_the_shell_road_needs_a_backend_that_bounds_output(self):
        class Unbounded(InProcessSandbox):
            exec_bounded = None  # type: ignore[assignment]  # opts out of `BoundedExec`

        adapter, _ = _adapter(Unbounded())
        (response,) = asyncio.run(adapter.aupload_files([("/tmp/x", b"1")]))
        assert response.error is not None and "outside the storage base" in response.error

    def test_a_batch_over_max_files_is_refused_whole_before_anything_crosses(self):
        fake = InProcessSandbox()
        spec = deepagents_spec(
            "img:1", files_in=TransferLimits(max_bytes_per_file=8, max_total_bytes=8, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.aupload_files([("a", b"1"), ("b", b"2")]))

        assert [r.error for r in responses] == [
            "the batch has more files than files_in.max_files allows"
        ] * 2
        assert fake.contents == {}

    def test_each_file_is_held_to_the_per_file_and_total_caps(self):
        fake = InProcessSandbox()
        spec = deepagents_spec(
            "img:1", files_in=TransferLimits(max_bytes_per_file=4, max_total_bytes=6, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(
            adapter.aupload_files([("big", b"12345"), ("a", b"1234"), ("b", b"123"), ("c", b"12")])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("big", "the file is larger than files_in.max_bytes_per_file"),
            ("a", None),
            ("b", "the batch would exceed files_in.max_total_bytes"),
            ("c", None),
        ]
        assert sorted(fake.contents) == [f"{WORK}/a", f"{WORK}/c"]

    def test_an_unavailable_sandbox_fails_every_file_without_raising(self):
        adapter, _ = _adapter(acquire_error=RuntimeError("down"))
        responses = asyncio.run(adapter.aupload_files([("a", b""), ("b", b"")]))
        assert [r.error for r in responses] == ["upload failed; see the host log"] * 2


class TestFilesOut:
    def test_a_round_trip_comes_back_byte_identical(self):
        fake = InProcessSandbox()
        adapter, _ = _adapter(fake)
        payload = bytes(range(256))
        asyncio.run(adapter.aupload_files([("blob.bin", payload)]))

        (response,) = asyncio.run(adapter.adownload_files(["blob.bin"]))

        assert response.error is None
        assert response.content == payload

    def test_each_refusal_has_its_code(self):
        fake = InProcessSandbox(
            seed_files={
                f"{WORK}/dir": EntryKind.DIRECTORY,
                f"{WORK}/link": EntryKind.SYMLINK,
                f"{WORK}/there.txt": "content",
            }
        )
        adapter, _ = _adapter(fake)

        responses = asyncio.run(
            adapter.adownload_files(["missing.txt", "dir", "link", "../outside", "there.txt"])
        )

        assert [(r.path, r.error) for r in responses] == [
            ("missing.txt", "file_not_found"),
            ("dir", "is_directory"),
            ("link", "invalid_path"),
            ("../outside", "invalid_path"),
            ("there.txt", None),
        ]
        assert responses[-1].content == b"content"

    def test_a_download_outside_the_base_goes_through_the_shell(self):
        """Deep Agents reads its offloaded history back from `/conversation_history`."""
        encoded = base64.b64encode(b"# history\n").decode()
        fake = InProcessSandbox(
            outputs={"wc -c": "10\n", "base64 <": encoded[:8] + "\n" + encoded[8:]}
        )
        adapter, _ = _adapter(fake)

        (response,) = asyncio.run(adapter.adownload_files(["/conversation_history/s.md"]))

        assert response.content == b"# history\n"
        probe, read = (command for command, _, _ in fake.commands)
        assert probe.startswith("if [ ! -e /conversation_history/s.md ]")
        assert read == "base64 < /conversation_history/s.md"

    @pytest.mark.parametrize(
        ("answer", "error"),
        [
            ("missing", "file_not_found"),
            ("sh: 1: cannot open /tmp/x: No such file or directory\nmissing", "file_not_found"),
            ("sh: can't open '/tmp/x': Permission denied\nmissing", "permission_denied"),
            ("sh: can't open '/tmp/x': Not a directory\nmissing", "invalid_path"),
            ("directory", "is_directory"),
            ("other", "invalid_path"),
            ("unreadable", "permission_denied"),
        ],
    )
    def test_the_shell_probe_answers_by_code(self, answer: str, error: str):
        """`test -e` is false behind an unsearchable ancestor as for an absent file; the
        open's own words, in the shell's phrasing (busybox, dash), tell the two apart."""
        adapter, _ = _adapter(InProcessSandbox(outputs={"wc -c": f"{answer}\n"}))
        (response,) = asyncio.run(adapter.adownload_files(["/tmp/x"]))
        assert response.error == error

    def test_a_shell_read_over_the_cap_is_refused_before_and_after_the_read(self):
        fake = InProcessSandbox(
            outputs={"wc -c": "3\n", "base64 <": base64.b64encode(b"grown!").decode()}
        )
        adapter, _ = _adapter(fake)
        adapter = MafSandbox(
            adapter.router,
            KEY,
            dataclasses.replace(
                adapter.spec,
                files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=8, max_files=8),
            ),
        )

        (grown,) = asyncio.run(adapter.adownload_files(["/tmp/grew"]))
        assert grown.error is not None and "max_bytes_per_file" in grown.error

        big = InProcessSandbox(outputs={"wc -c": "5\n"})
        adapter, _ = _adapter(big)
        adapter = MafSandbox(
            adapter.router,
            KEY,
            dataclasses.replace(
                adapter.spec,
                files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=8, max_files=8),
            ),
        )
        (refused,) = asyncio.run(adapter.adownload_files(["/tmp/big"]))
        assert refused.error is not None and "max_bytes_per_file" in refused.error
        assert len(big.commands) == 1  # refused on the probe, never read

    def test_a_shell_download_that_times_out_disposes_and_ends_the_batch(self):
        fake = InProcessSandbox(raises=TimeoutError(), seed_files={f"{WORK}/a.txt": "1"})
        adapter, backend = _adapter(fake)

        async def scenario():
            responses = await adapter.adownload_files([f"{WORK}/a.txt", "/tmp/x", "/tmp/y"])
            await adapter.aclose()  # joins the delete the timeout started
            return responses

        first, second, third = asyncio.run(scenario())
        assert first.content == b"1"
        assert second.error is not None and "was not read" in second.error
        assert third.error == second.error
        assert len(fake.commands) == 1  # the probe that timed out; nothing after it ran
        assert backend.disposed[-1] == KEY

    def test_a_shell_read_that_overflows_is_over_the_cap_and_ends_the_batch(self):
        """A file that outgrew its cap mid-read may leave the read running: over the cap for
        it, and the rest of the batch is not attempted."""
        fake = InProcessSandbox(outputs={"wc -c": "3\n", "base64 <": "x" * 5000})
        adapter, backend = _adapter(fake)
        adapter = MafSandbox(
            adapter.router,
            KEY,
            dataclasses.replace(
                adapter.spec,
                files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=8, max_files=8),
            ),
        )

        async def scenario():
            responses = await adapter.adownload_files(["/tmp/grew", "/tmp/next"])
            await adapter.aclose()
            return responses

        grew, rest = asyncio.run(scenario())
        assert grew.error is not None and "max_bytes_per_file" in grew.error
        assert rest.error is not None and "was not read" in rest.error
        assert backend.disposed[-1] == KEY

    def test_an_unreadable_file_is_permission_denied_on_stat_and_on_read(self):
        class LockedStat(InProcessSandbox):
            async def stat_file(self, path, *, working_directory):
                raise PermissionError("no search permission")

        class LockedRead(InProcessSandbox):
            async def read_file(self, path, *, working_directory, max_bytes):
                raise PermissionError("no read permission")

        adapter, _ = _adapter(LockedStat())
        (on_stat,) = asyncio.run(adapter.adownload_files(["f.txt"]))
        adapter, _ = _adapter(LockedRead(seed_files={f"{WORK}/f.txt": "1"}))
        (on_read,) = asyncio.run(adapter.adownload_files(["f.txt"]))
        assert on_stat.error == "permission_denied"
        assert on_read.error == "permission_denied"

    def test_a_parent_that_is_a_file_is_invalid_path_on_stat(self):
        """The plane's own refusal, the same code the upload road gives it."""

        class FileParent(InProcessSandbox):
            async def stat_file(self, path, *, working_directory):
                raise NotADirectoryError("'file' is not a directory")

        adapter, _ = _adapter(FileParent())
        (response,) = asyncio.run(adapter.adownload_files(["file/child.txt"]))
        assert response.error == "invalid_path"

    def test_a_read_that_comes_back_over_the_cap_is_refused_after_the_fact(self):
        """The protocol has the caller re-count: a backend that buffers first can only refuse late."""

        class Oversized(InProcessSandbox):
            async def read_file(self, path, *, working_directory, max_bytes):
                return b"x" * (max_bytes + 1)

        fake = Oversized(seed_files={f"{WORK}/grew.bin": "1"})
        adapter, _ = _adapter(fake)
        adapter = MafSandbox(
            adapter.router,
            KEY,
            dataclasses.replace(
                adapter.spec,
                files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=4, max_files=8),
            ),
        )

        (response,) = asyncio.run(adapter.adownload_files(["grew.bin"]))

        assert response.content is None
        assert response.error is not None and "max_bytes_per_file" in response.error

    def test_a_file_over_the_cap_is_refused_rather_than_truncated(self):
        fake = InProcessSandbox(seed_files={f"{WORK}/big.bin": "0123456789"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=4, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        (response,) = asyncio.run(adapter.adownload_files(["big.bin"]))

        assert response.content is None
        assert response.error == "the file is larger than files_out.max_bytes_per_file"

    def test_a_read_that_times_out_is_a_failure_not_a_bad_path(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        fake = InProcessSandbox(seed_files={f"{WORK}/slow.txt": "content"})
        adapter, _ = _adapter(fake)

        async def read_file(*args, **kwargs):
            raise TimeoutError("the read did not finish")

        monkeypatch.setattr(fake, "read_file", read_file)
        with caplog.at_level(logging.WARNING, logger="maf_sandbox_deepagents"):
            (response,) = asyncio.run(adapter.adownload_files(["slow.txt"]))

        assert response.error == "download failed; see the host log"
        assert "timed out" in caplog.text

    def test_a_timed_out_plane_read_fails_that_file_alone(self, monkeypatch: pytest.MonkeyPatch):
        """A read changes nothing in the sandbox: the rest of the batch is read, and it stays."""
        fake = InProcessSandbox(seed_files={f"{WORK}/slow.txt": "slow", f"{WORK}/fine.txt": "fine"})
        adapter, backend = _adapter(fake)
        asyncio.run(adapter.aexecute("true"))
        before = len(backend.disposed)
        real_read = fake.read_file

        async def read_file(path, *args, **kwargs):
            if path.endswith("slow.txt"):
                raise TimeoutError("the read did not finish")
            return await real_read(path, *args, **kwargs)

        monkeypatch.setattr(fake, "read_file", read_file)
        slow, fine = asyncio.run(adapter.adownload_files(["slow.txt", "fine.txt"]))
        again = asyncio.run(adapter.aexecute("cat fine.txt"))

        assert slow.error == "download failed; see the host log"
        assert fine.content == b"fine"
        assert again.exit_code == 0
        assert backend.disposed[before:] == []

    def test_a_cap_the_read_reports_is_named_by_the_cap_handed_down(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A file that grew after the stat is judged by the ceiling the read was given."""
        fake = InProcessSandbox(seed_files={f"{WORK}/grew.txt": "123"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=64, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        async def read_file(*args, **kwargs):
            raise SandboxTransferCapExceeded("grew past the cap")

        monkeypatch.setattr(fake, "read_file", read_file)
        (response,) = asyncio.run(adapter.adownload_files(["grew.txt"]))

        assert response.error == "the file is larger than files_out.max_bytes_per_file"

    def test_a_batch_over_max_files_is_refused_whole(self):
        fake = InProcessSandbox(seed_files={f"{WORK}/a": "1", f"{WORK}/b": "2"})
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=8, max_total_bytes=8, max_files=1)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.adownload_files(["a", "b"]))

        assert [r.error for r in responses] == [
            "the batch has more files than files_out.max_files allows"
        ] * 2

    def test_the_total_cap_bounds_the_batch_and_a_refused_file_spends_nothing(self):
        fake = InProcessSandbox(
            seed_files={f"{WORK}/a": "1234", f"{WORK}/b": "123", f"{WORK}/c": "12"}
        )
        spec = deepagents_spec(
            "img:1", files_out=TransferLimits(max_bytes_per_file=4, max_total_bytes=6, max_files=8)
        )
        adapter = MafSandbox(_router(_backend(fake)), KEY, spec)

        responses = asyncio.run(adapter.adownload_files(["a", "b", "c"]))

        assert [(r.path, r.error, r.content) for r in responses] == [
            ("a", None, b"1234"),
            ("b", "the batch would exceed files_out.max_total_bytes", None),
            ("c", None, b"12"),
        ]


class TestTheSynchronousSurface:
    def test_works_with_no_loop_running(self):
        fake = InProcessSandbox(outputs={"echo": "hi"})
        adapter, _ = _adapter(fake)
        assert adapter.execute("echo hi").output == "hi"
        assert adapter.upload_files([("f", b"1")])[0].error is None
        assert adapter.download_files(["f"])[0].content == b"1"

    def test_works_from_inside_a_running_loop(self):
        """A sync tool called on the loop's own thread must not trip `asyncio.run`'s nesting refusal."""
        fake = InProcessSandbox(outputs={"echo": "hi"})
        adapter, _ = _adapter(fake)

        async def scenario() -> str:
            return adapter.execute("echo hi").output

        assert asyncio.run(scenario()) == "hi"

    def test_an_exception_crosses_back_to_the_caller(self):
        adapter, _ = _adapter()

        async def scenario() -> None:
            adapter.execute("true", timeout=0)

        with pytest.raises(ValueError, match="timeout"):
            asyncio.run(scenario())

    def test_one_loop_serves_every_sync_call_of_every_adapter(self):
        """A backend may cache a client per loop and never evict one, so the sync surface runs
        one loop for the process, not one per adapter or per call."""
        # The loop objects themselves, held so a collected loop's address cannot be reused.
        loops: list[asyncio.AbstractEventLoop] = []

        class Recording(InProcessSandbox):
            async def exec(self, command, *, working_directory, timeout):
                loops.append(asyncio.get_running_loop())
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        first, _ = _adapter(Recording())
        second, _ = _adapter(Recording())
        first.execute("true")
        first.upload_files([("f", b"1")])
        second.execute("true")
        closed = first.close()
        assert closed is True
        second.execute("true")

        assert len({id(loop) for loop in loops}) == 1
        assert sum(t.name == "maf-sandbox-deepagents" for t in threading.enumerate()) == 1
        second.close()


class TestClose:
    def test_disposes_this_kind_for_this_conversation(self):
        fake = InProcessSandbox()
        adapter, backend = _adapter(fake)
        asyncio.run(adapter.aexecute("true"))

        before = len(backend.disposed)

        closed = asyncio.run(adapter.aclose())
        assert closed is True

        assert backend.disposed[before:] == [KEY]
        assert backend.disposed_kinds[before:] == [DEEPAGENTS_KIND]
        # The one instance this adapter acquired, never a sweep of the key.
        assert backend.disposed_instances[before:] == [fake.instance_id]

    def test_a_failed_disposal_keeps_the_instance_for_a_retry(self):
        fake = InProcessSandbox()
        adapter, backend = _adapter(fake)
        asyncio.run(adapter.aexecute("true"))
        # Set after the warm-up, or the router's adoption dispose would fail the acquire itself.
        backend.dispose_failure = DisposalFailure("timeout", "still there")
        attempts = len(backend.disposed)

        assert asyncio.run(adapter.aclose()) is False
        backend.dispose_failure = None
        retried = asyncio.run(adapter.aclose())

        assert retried is True
        assert backend.disposed_instances[attempts:] == [fake.instance_id, fake.instance_id]
        assert asyncio.run(adapter.aclose()) is True
        assert len(backend.disposed) == attempts + 2

    def test_a_close_before_any_acquire_deletes_nothing(self):
        adapter, backend = _adapter(InProcessSandbox())
        closed = asyncio.run(adapter.aclose())
        assert closed is True
        assert backend.disposed == []

    def test_the_synchronous_close_is_the_same_call(self):
        adapter, backend = _adapter(InProcessSandbox())
        adapter.execute("true")
        before = len(backend.disposed)
        closed = adapter.close()
        assert closed is True
        assert backend.disposed[before:] == [KEY]

    def test_the_next_command_after_a_close_starts_a_fresh_sandbox(self):
        fake = InProcessSandbox()
        adapter, backend = _adapter(fake, sandbox_per_key=True)
        asyncio.run(adapter.aupload_files([("keep.txt", b"1")]))
        asyncio.run(adapter.aclose())
        before = len(backend.keys)

        (response,) = asyncio.run(adapter.adownload_files(["keep.txt"]))

        assert response.error == "file_not_found"
        assert len(backend.keys) > before
