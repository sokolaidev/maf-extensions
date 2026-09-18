"""Exercises sample 19's executor offline, on the fake backend the core ships.

The sample is not a uv workspace member and not in any ``testpaths``: this is the suite that
drives the executor the way AutoGen's tool does, against `maf_sandbox.testing`'s in-process
fake, and holds the block the sample prints to the shared checker. The fake records rather than
runs, so nothing here reaches Docker; the model clients are constructed and never called.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from maf_sandbox import Capability, Isolation, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox.testing import InProcessSandbox, InProcessSandboxBackend

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE = _ROOT / "samples" / "19_autogen_docker_codeact"


def _load_sample() -> ModuleType:
    """`agent.py` loaded by path, the way `test_sample_modules_import.py` loads a sample.

    Nineteen sample directories hold a module named `agent`, so a plain import would resolve to
    whichever one the type checker saw first; loading by path under this suite's own name keeps
    that ambiguity out. Evicting the cache afterwards keeps a later suite's
    `from _scaffold import …` from answering with this sample's copy.
    """
    name = "sample_19_agent"
    sys.path.insert(0, str(_SAMPLE))
    try:
        spec = importlib.util.spec_from_file_location(name, _SAMPLE / "agent.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_SAMPLE))
        sys.modules.pop(name, None)
        sys.modules.pop("_scaffold", None)


sample_19 = _load_sample()


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load(
    "check_live_codeact_sample_for_19", _ROOT / "scripts" / "check_live_codeact_sample.py"
)
scaffold = _load("_scaffold_for_19", _SAMPLE / "_scaffold.py")


def _spec() -> SandboxSpec:
    return SandboxSpec(
        kind=sample_19.KIND,
        image=sample_19.CODEACT_IMAGE,
        requires=frozenset({Capability.EXEC}),
    )


def _router(sandbox: InProcessSandbox) -> tuple[SandboxRouter, InProcessSandboxBackend]:
    backend = InProcessSandboxBackend(sandbox, isolation=Isolation.NONE)
    return SandboxRouter([backend], min_isolation=Isolation.NONE), backend


def _run(sandbox: InProcessSandbox, code: str, language: str = "python"):
    """One `execute_code_blocks` call over a router serving ``sandbox``, as the tool makes it."""

    async def body():
        router, backend = _router(sandbox)
        try:
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
            return await executor.execute_code_blocks(
                [CodeBlock(code=code, language=language)], CancellationToken()
            ), backend
        finally:
            await router.dispose_scope(_key().scope, _key().thread_id)

    return asyncio.run(body())


def _key() -> SandboxKey:
    return SandboxKey(scope="test-scope", thread_id="test-thread", agent_id="data_analyst")


class TestTheExecutionRoad:
    def test_a_python_block_runs_as_argv_through_exec_bounded(self):
        """``python3 -c <code>``, no shell, at the spec's working directory — the base as `.`."""
        sandbox = InProcessSandbox(outputs={"print": "354224848179261915075"})
        result, backend = _run(sandbox, "print(354224848179261915075)")
        assert result.exit_code == 0
        assert result.output == "stdout:\n354224848179261915075"
        joined, working_directory, _ = sandbox.commands[-1]
        assert joined.startswith("python3 -c ")
        assert working_directory == "/maf-sandbox/work"
        assert backend.keys, "the executor never reached the router"

    def test_a_non_python_block_is_refused_before_any_acquire(self):
        sandbox = InProcessSandbox()
        result, backend = _run(sandbox, "console.log('hi')", language="javascript")
        assert result.exit_code == 1
        assert result.output.startswith("Error:")
        assert backend.keys == [], "a refused block must not pay for a sandbox"

    def test_a_mixed_list_runs_the_python_blocks_before_the_unsupported_one_stops_it(self):
        """Both reference executors run the preceding blocks and stop at the unsupported one."""

        class Counting(InProcessSandbox):
            ran = 0

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import ExecResult

                Counting.ran += 1
                return ExecResult(stdout="done", exit_code=0)

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Counting()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                return (
                    await executor.execute_code_blocks(
                        [
                            CodeBlock(code="print('first')", language="python"),
                            CodeBlock(code="console.log('no')", language="javascript"),
                        ],
                        CancellationToken(),
                    ),
                    sandbox,
                    backend,
                )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, sandbox, backend = asyncio.run(body())
        assert result.exit_code == 1
        assert "stdout:\ndone" in result.output and "Error: only Python" in result.output
        assert Counting.ran == 1
        assert backend.keys, "the runnable block paid for its sandbox"

    def test_a_list_that_begins_with_an_unsupported_language_never_acquires(self):
        """The sandbox is acquired only when a runnable block is reached."""

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            router, backend = _router(InProcessSandbox())
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                return (
                    await executor.execute_code_blocks(
                        [
                            CodeBlock(code="console.log('no')", language="javascript"),
                            CodeBlock(code="print('first')", language="python"),
                        ],
                        CancellationToken(),
                    ),
                    backend,
                )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, backend = asyncio.run(body())
        assert result.exit_code == 1
        assert result.output.startswith("Error: only Python")
        assert backend.keys == [], "a list that never reaches a runnable block pays for nothing"

    def test_the_guests_own_exit_code_answers_for_the_result(self):
        """AutoGen reads `success` off `CodeResult.exit_code`, so the guest's exit stands."""

        class Failing(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import ExecResult

                return ExecResult(stderr="boom", exit_code=3)

        result, _ = _run(Failing(), "print('boom')")
        assert result.exit_code == 3
        assert "stderr:\nboom" in result.output and "exit code: 3" in result.output

    def test_a_list_stops_at_the_first_failing_block(self):
        """Both of AutoGen's reference executors break at the first nonzero exit."""

        class Failing(InProcessSandbox):
            ran = 0

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import ExecResult

                Failing.ran += 1
                return ExecResult(stderr="boom", exit_code=3)

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Failing()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                return (
                    await executor.execute_code_blocks(
                        [
                            CodeBlock(code="print('boom')", language="python"),
                            CodeBlock(code="print('second')", language="python"),
                        ],
                        CancellationToken(),
                    ),
                    sandbox,
                )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, _ = asyncio.run(body())
        # The failing block's exit stands, and the block after it never ran: one execution
        # call, not two.
        assert result.exit_code == 3
        assert Failing.ran == 1

    def test_a_sandbox_without_exec_bounded_is_refused(self):
        """The contract's optional surface, refused the way `maf-sandbox-deepagents` refuses it.

        The fake hides the method rather than raising from it: a runtime-checkable protocol
        reads presence, and a property that raises still reads as present.
        """

        class Unbounded(InProcessSandbox):
            # Hidden, not raised from: a runtime-checkable protocol reads presence, and a
            # property that raises still reads as present.
            exec_bounded = None  # pyright: ignore[reportAssignmentType] - the surface this fake omits

        result, _ = _run(Unbounded(), "print('hi')")
        assert result.exit_code == 1
        assert "exec_bounded" in result.output

    def test_an_output_overflow_is_rendered_as_an_error(self):
        """The budget is the host's live cap; an overrunning program is refused, not truncated."""

        async def body():
            from unittest.mock import patch

            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = InProcessSandbox(default_stdout="x" * 4096)
            router, _ = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                with patch.object(sample_19, "MAX_OUTPUT_BYTES", 8):
                    return await executor.execute_code_blocks(
                        [CodeBlock(code="print('x')", language="python")], CancellationToken()
                    )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result = asyncio.run(body())
        assert result.exit_code == 1
        assert "byte budget" in result.output

    def test_an_output_overflow_disposes_the_instance_before_returning(self):
        """Nothing about an overflow establishes the guest stopped, so the sandbox is not reused.

        The overflow raises past the backend's own timeout handler without any removal act.
        Reusing warm here would hand the next tool call a sandbox a runaway program is still
        writing into.
        """

        class Overflowing(InProcessSandbox):
            """An overflow raise, without the fake's already-resident result."""

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import SandboxExecOutputLimitExceeded

                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Overflowing()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                # Warm acquire first: its adoption is the one disposal that must not count,
                # and the executor's own acquire then reuses rather than adopting again.
                await router.acquire(_key(), _spec())
                before = len(backend.disposed)
                result = await executor.execute_code_blocks(
                    [CodeBlock(code="print('x')", language="python")], CancellationToken()
                )
                return result, len(backend.disposed) - before
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, condemned = asyncio.run(body())
        # The disposal is the act that makes the next acquire a fresh create rather than a warm
        # reuse. It happens mid-call, before the error is returned. The count is read as a
        # window after a warm acquire, because the cold acquire's own adoption also disposes
        # (the router's unfamiliar-instance path) — the window over a reused sandbox is the
        # executor's condemnation alone, and the scope purge records in `purged`, never here.
        assert result.exit_code == 1
        assert "byte budget" in result.output
        assert condemned == 1, (
            f"the executor's condemnation should be exactly one disposal in the armed window, "
            f"not {condemned} — fewer means the error was returned without the instance being "
            f"condemned, more means the count is reading something else"
        )

    def test_a_timeout_condemns_the_instance_before_returning(self):
        """A timeout does not establish the guest stopped either — the sandbox is not reused.

        The docker backend runs a best-effort ``rm -f`` on its own timeout and suppresses every
        failure from it, so the backend's removal is not a guarantee the executor can lean on;
        the timed-out process's container must not be reacquirable warm.
        """

        class Hanging(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                raise TimeoutError("the execution did not finish in time")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Hanging()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                # Warm acquire first: its adoption is the one disposal that must not count,
                # and the executor's own acquire then reuses rather than adopting again.
                await router.acquire(_key(), _spec())
                before = len(backend.disposed)
                result = await executor.execute_code_blocks(
                    [CodeBlock(code="print('never')", language="python")], CancellationToken()
                )
                return result, len(backend.disposed) - before
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, condemned = asyncio.run(body())
        assert result.exit_code == 1
        assert "timed out" in result.output
        assert condemned == 1, (
            f"the timeout's condemnation should be exactly one disposal in the armed window, "
            f"not {condemned} — fewer means the error was returned without the instance being "
            f"condemned, more means the count is reading something else"
        )

    def test_a_timeout_with_a_failed_delete_refuses_the_next_acquire(self):
        """A timed-out container the backend could not remove leaves the key refused.

        Docker's ``rm -f`` failure is suppressed inside the backend, so the executor cannot
        learn the delete failed from the raise — the unclean mark is what refuses the key, and
        the refusal retires when a later disposal lands.
        """

        class Hanging(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                raise TimeoutError("the execution did not finish in time")

        class HoldingOnFailure(InProcessSandboxBackend):
            """Holds the sandbox mapped across a failed delete, as a real backend would."""

            fail = False

            async def dispose(
                self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
            ):
                self.disposed.append(key)
                self.disposed_kinds.append(kind)
                self.disposed_instances.append(instance_id)
                if self.fail:
                    return "the engine would not remove it"
                for held in [entry for entry in list(self.sandboxes) if entry[0] == key]:
                    del self.sandboxes[held]
                return None

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock
            from maf_sandbox import SandboxUnclean

            sandbox = Hanging()
            backend = HoldingOnFailure(sandbox, isolation=Isolation.NONE)
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                # Warm acquire first with deletes landing — its adoption must succeed — then
                # arm the failure, so the failing delete is the timeout's condemnation rather
                # than a cold acquire's adoption.
                await router.acquire(_key(), _spec())
                backend.fail = True
                result = await executor.execute_code_blocks(
                    [CodeBlock(code="print('never')", language="python")], CancellationToken()
                )
                with pytest.raises(SandboxUnclean):
                    await router.acquire(_key(), _spec())
                return result
            finally:
                backend.fail = False
                await router.dispose_scope(_key().scope, _key().thread_id)

        result = asyncio.run(body())
        assert result.exit_code == 1
        assert "timed out" in result.output

    def test_an_unexpected_execution_error_condemns_and_reads_as_a_refusal(self):
        """Any failure the result did not come back from condemns the sandbox — not just the
        two the handler names. The model still reads an ``Error:`` string, not an exception.
        """

        class Breaking(InProcessSandbox):
            """A failure that is neither a timeout nor an overflow."""

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                raise RuntimeError("the transport fell over mid-execution")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Breaking()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await router.acquire(_key(), _spec())
                before = len(backend.disposed)
                result = await executor.execute_code_blocks(
                    [CodeBlock(code="print('lost')", language="python")], CancellationToken()
                )
                return result, len(backend.disposed) - before
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, condemned = asyncio.run(body())
        # The unknown-state container is condemned before anything reuses it, and the tool's
        # answer is the Error string a model can act on — not a RuntimeError out of the tool.
        assert result.exit_code == 1
        assert result.output.startswith("Error:")
        assert condemned == 1, (
            f"the unexpected failure should condemn exactly once in the armed window, "
            f"not {condemned} — zero means the sandbox stayed reacquirable in unknown state"
        )

    def test_a_cancelled_execution_condemns_and_propagates(self):
        """A cancelled tool call is a lost run: the sandbox is condemned and the cancellation
        propagates out of the executor.
        """

        class Hanging(InProcessSandbox):
            """A guest that never answers, the way a cancelled run finds one."""

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                await asyncio.sleep(3600)
                raise AssertionError("the cancelled wait never returns here")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Hanging()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await router.acquire(_key(), _spec())
                before = len(backend.disposed)
                token = CancellationToken()
                run = asyncio.ensure_future(
                    executor.execute_code_blocks(
                        [CodeBlock(code="print('never')", language="python")], token
                    )
                )
                # Let the executor reach its await before cancelling, so the cancellation
                # lands on the linked future rather than on the not-yet-started call.
                await asyncio.sleep(0.05)
                token.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run
                return len(backend.disposed) - before
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        condemned = asyncio.run(body())
        assert condemned == 1, (
            f"the cancellation should condemn exactly once in the armed window, "
            f"not {condemned} — zero means the cancelled run left the sandbox reacquirable"
        )

    def test_a_cancelled_execution_with_a_failed_delete_refuses_the_next_acquire(self):
        """The condemnation on cancellation is the unclean path, like every other lost run."""

        class Hanging(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                await asyncio.sleep(3600)
                raise AssertionError("the cancelled wait never returns here")

        class HoldingOnFailure(InProcessSandboxBackend):
            fail = False

            async def dispose(
                self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
            ):
                self.disposed.append(key)
                self.disposed_kinds.append(kind)
                self.disposed_instances.append(instance_id)
                if self.fail:
                    return "the engine would not remove it"
                for held in [entry for entry in list(self.sandboxes) if entry[0] == key]:
                    del self.sandboxes[held]
                return None

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock
            from maf_sandbox import SandboxUnclean

            sandbox = Hanging()
            backend = HoldingOnFailure(sandbox, isolation=Isolation.NONE)
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await router.acquire(_key(), _spec())
                backend.fail = True
                token = CancellationToken()
                run = asyncio.ensure_future(
                    executor.execute_code_blocks(
                        [CodeBlock(code="print('never')", language="python")], token
                    )
                )
                await asyncio.sleep(0.05)
                token.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run
                with pytest.raises(SandboxUnclean):
                    await router.acquire(_key(), _spec())
            finally:
                backend.fail = False
                await router.dispose_scope(_key().scope, _key().thread_id)

        asyncio.run(body())

    def test_an_overflow_failure_attempts_the_disposal_and_still_answers(self):
        """The unclean disposal is attempted on a lost run even when the delete itself raises,
        and the model still reads its `Error:` string rather than the raise.

        `_dispose_each` catches the raise, records it, and the error wins — the one failure
        shape the suite's other overflow tests do not carry.
        """

        class Overflowing(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import SandboxExecOutputLimitExceeded

                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")

        class DisposingLoudly(InProcessSandboxBackend):
            """Raises from ``dispose`` once armed — a delete that breaks, not one that reports."""

            fail = False

            async def dispose(
                self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
            ):
                self.disposed.append(key)
                self.disposed_kinds.append(kind)
                self.disposed_instances.append(instance_id)
                if self.fail:
                    raise RuntimeError("the engine fell over mid-delete")
                for held in [entry for entry in list(self.sandboxes) if entry[0] == key]:
                    del self.sandboxes[held]
                return None

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Overflowing()
            backend = DisposingLoudly(sandbox, isolation=Isolation.NONE)
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                # Warm first: the executor's own acquire reuses, and the failing delete is the
                # unclean condemnation rather than a cold acquire's adoption.
                await router.acquire(_key(), _spec())
                before = len(backend.disposed)
                backend.fail = True
                result = await executor.execute_code_blocks(
                    [CodeBlock(code="print('x')", language="python")], CancellationToken()
                )
                return result, len(backend.disposed) - before
            finally:
                backend.fail = False
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, condemned = asyncio.run(body())
        # Measured over the armed window: `disposed` also counts the warm acquire's adoption,
        # and the scope purge records in `purged`, so only the delta after arming is the
        # executor's condemnation.
        assert result.exit_code == 1
        assert "byte budget" in result.output
        assert condemned >= 1, (
            "the unclean disposal never ran — the error was returned without the instance "
            "having been condemned"
        )

    def test_a_failed_overflow_disposal_refuses_the_next_acquire(self):
        """The unclean path: a delete that did not land leaves the key refused, not reacquirable.

        `dispose` is best-effort by the router's own contract — a failure reaches the caller as
        a return value, not a refusal — so a container the delete failed on stays mapped and the
        next tool call would reacquire it, runaway process and all. `dispose_unclean` marks the
        key, and the refusal retires when a later disposal lands. The backend here holds the
        sandbox mapped across a failed delete, the way `DockerSandboxBackend` holds a container
        `rm -f` could not remove, and fails every delete while the executor's call is in flight.
        """

        class Overflowing(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import SandboxExecOutputLimitExceeded

                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")

        class FailingDuringCall(InProcessSandboxBackend):
            """Fails deletes only while the executor's overflow call is in flight."""

            fail = False

            async def dispose(
                self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
            ):
                self.disposed.append(key)
                self.disposed_kinds.append(kind)
                self.disposed_instances.append(instance_id)
                if self.fail:
                    return "the engine would not remove it"
                for held in [entry for entry in list(self.sandboxes) if entry[0] == key]:
                    del self.sandboxes[held]
                return None

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock
            from maf_sandbox import SandboxUnclean

            sandbox = Overflowing()
            backend = FailingDuringCall(sandbox, isolation=Isolation.NONE)
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                # Warm the sandbox first, so the executor's condemnation is the failing delete
                # and not a cold acquire's adoption.
                await router.acquire(_key(), _spec())
                backend.fail = True
                first = await executor.execute_code_blocks(
                    [CodeBlock(code="print('x')", language="python")], CancellationToken()
                )
                with pytest.raises(SandboxUnclean):
                    await router.acquire(_key(), _spec())
                return first
            finally:
                backend.fail = False
                await router.dispose_scope(_key().scope, _key().thread_id)

        first = asyncio.run(body())
        assert first.exit_code == 1
        assert "byte budget" in first.output

    def test_a_landed_overflow_disposal_does_not_stay_refused(self):
        """When the delete lands, the refusal retires with it: the next acquire is a fresh create."""

        class Overflowing(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import SandboxExecOutputLimitExceeded

                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            # Per-key mode: the fake's default hands back the same object on every acquire,
            # so a fresh filesystem would be asserted against a fake that had not given one.
            sandbox = Overflowing()
            backend = InProcessSandboxBackend(
                sandbox, isolation=Isolation.NONE, sandbox_per_key=True
            )
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await executor.execute_code_blocks(
                    [CodeBlock(code="print('x')", language="python")], CancellationToken()
                )
                reacquired = await router.acquire(_key(), _spec())
                purge = await router.dispose_scope(_key().scope, _key().thread_id)
                return reacquired, sandbox, backend, purge
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        reacquired, sandbox, backend, purge = asyncio.run(body())
        # The refusal was written when the unclean disposal queued, and retired when the same
        # call's delete landed inside it — so the key is servable again, on a fresh filesystem.
        # Two disposals, recorded and reported: the condemnation, and the scope purge sweeping
        # the fresh create. The fresh-create path itself records nothing here — per-key mode
        # handed a genuinely new instance, where the shared fake's default would have handed
        # back the same object and had the router retire it a second time.
        assert reacquired is not sandbox, (
            "the reacquire came back with the same sandbox the overflow ran in — no fresh "
            "filesystem, so the condemnation did not separate the turns"
        )
        assert backend.disposed.count(_key()) == 1
        assert purge.disposed == 1

    @pytest.mark.parametrize("method", ["stop", "restart"])
    def test_a_lifecycle_release_condemns_the_acquired_sandbox(self, method: str):
        """`stop` and `restart` release what the executor acquired, per the contract.

        A no-op ``stop`` would leave the container's filesystem and any running program alive
        past a ``with`` block that promised cleanup; ``restart`` is called when the agent is
        reset, and reset means what one turn left behind is not what the next turn finds.
        """

        class Holder(InProcessSandbox):
            """Records that a program ran and stays running after its call returned."""

            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import ExecResult

                self.running.add("leftover-python")
                return ExecResult(stdout="ran")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = Holder()
            router, backend = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await executor.execute_code_blocks(
                    [CodeBlock(code="print('left behind')", language="python")],
                    CancellationToken(),
                )
                before = len(backend.disposed)
                await getattr(executor, method)()
                return len(backend.disposed) - before
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        condemned = asyncio.run(body())
        assert condemned >= 1, f"executor.{method}() released nothing"

    def test_a_lifecycle_release_leaves_a_sibling_kind_on_the_key_alone(self):
        """`stop` releases what this executor acquired — not every kind sharing the key."""

        async def body():
            sibling_spec = SandboxSpec(
                kind="sibling-kind", image="sibling-image", requires=frozenset({Capability.EXEC})
            )
            backend = InProcessSandboxBackend(
                InProcessSandbox(), isolation=Isolation.NONE, sandbox_per_key=True
            )
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await router.acquire(_key(), _spec())
                await router.acquire(_key(), sibling_spec)
                await executor.stop()
                return set(backend.sandboxes)
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        held = asyncio.run(body())
        assert held == {(_key(), "sibling-kind")}, (
            f"stop() swept a sibling workload's sandbox off the shared key: {held}"
        )

    @pytest.mark.parametrize("method", ["stop", "restart"])
    def test_a_failed_lifecycle_release_refuses_the_next_acquire(
        self, method: str, monkeypatch: pytest.MonkeyPatch
    ):
        """A delete that does not land closes the key — the lifecycle path is not looser."""

        class Holder(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import ExecResult

                return ExecResult(stdout="ran")

        class HoldingOnFailure(InProcessSandboxBackend):
            async def dispose(
                self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
            ):
                self.disposed.append(key)
                self.disposed_kinds.append(kind)
                self.disposed_instances.append(instance_id)
                return "the engine would not remove it"

        async def body():
            from maf_sandbox import SandboxUnclean

            sandbox = Holder()
            backend = HoldingOnFailure(sandbox, isolation=Isolation.NONE)
            router = SandboxRouter([backend], min_isolation=Isolation.NONE)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                await getattr(executor, method)()
                with pytest.raises(SandboxUnclean):
                    await router.acquire(_key(), _spec())
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        asyncio.run(body())


class TestTheBlockTheSamplePrints:
    """The evidence block, rendered as the sample renders it, read by the shared checker."""

    _ANSWER = "354224848179261915075"

    def test_a_healthy_run_passes_the_checker(self):
        reply = f"I ran it and it printed {self._ANSWER}."
        block = scaffold.evidence(
            sample_19.EXECUTOR_TOOL_HEADING,
            [f"stdout:\n{self._ANSWER}"],
            "programs whose output came back from the sandbox",
        )
        output = f"{reply}\n\n{block}\n\n{scaffold.MEASURED}Disposed 1 sandbox(es)."
        assert check.assess(output) == []

    def test_the_heading_lives_in_the_sample_as_one_literal(self):
        source = (_SAMPLE / "agent.py").read_text(encoding="utf-8")
        assert f'"{sample_19.EXECUTOR_TOOL_HEADING}"' in source


class TestTheModelWiring:
    def test_the_local_road_constructs_without_a_key(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        model, credential = sample_19.build_model()
        assert credential is None
        assert type(model).__name__ == "OpenAIChatCompletionClient"
        # The flag keeps `AssistantAgent`'s concurrent tool calls off one unadmitted key; the
        # wiring tests type-check only, so dropping the flag would leave this suite green.
        assert model._create_args["parallel_tool_calls"] is False  # pyright: ignore[reportPrivateUsage]

    def test_the_azure_road_constructs_with_a_token_provider(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.example.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_CHAT_MODEL", "gpt-5.4")
        model, credential = sample_19.build_model()
        assert credential is not None
        assert type(model).__name__ == "AzureOpenAIChatCompletionClient"
        assert model._create_args["parallel_tool_calls"] is False  # pyright: ignore[reportPrivateUsage]
        asyncio.run(credential.close())

    def test_an_endpoint_without_a_deployment_is_reported_not_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.example.openai.azure.com")
        monkeypatch.delenv("AZURE_OPENAI_CHAT_MODEL", raising=False)
        assert sample_19.build_model() is None
        assert "AZURE_OPENAI_CHAT_MODEL" in capsys.readouterr().err


class TestTheResultReading:
    """`executor_results` reads AutoGen's events, the scaffold's `tool_results` reads MAF's."""

    def _result(self, name: str):
        from autogen_agentchat.messages import ToolCallExecutionEvent
        from autogen_core.models import FunctionExecutionResult

        run = ToolCallExecutionEvent(
            source="data_analyst",
            content=[
                FunctionExecutionResult(
                    content="stdout:\n354224848179261915075",
                    name=name,
                    call_id="call-1",
                    is_error=False,
                )
            ],
        )
        return sample_19.TaskResult(
            messages=[
                sample_19.TextMessage(source="user", content="compute"),
                run,
                sample_19.TextMessage(
                    source="data_analyst", content="It printed 354224848179261915075."
                ),
            ],
            stop_reason=None,
        )

    def test_results_of_this_tool_are_read_and_others_are_not(self):
        assert sample_19.executor_results(self._result("CodeExecutor")) == [
            "stdout:\n354224848179261915075"
        ]
        assert sample_19.executor_results(self._result("other_tool")) == []

    def test_the_final_reply_is_the_assistants_last_word(self):
        assert sample_19.final_reply(self._result("CodeExecutor")) == (
            "It printed 354224848179261915075."
        )
