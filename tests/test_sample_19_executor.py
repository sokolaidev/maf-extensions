"""Exercises sample 19's executor offline, on the fake backend the core ships.

The sample is not a uv workspace member and not in any ``testpaths``. ``test_sample_modules_import.py``
imports it, along with every other sample, which proves only that its module level runs; this
suite drives the executor the way AutoGen's tool does, against `maf_sandbox.testing`'s in-process
fake, and holds the block the sample prints to the shared checker:

* a Python block runs as ``python3 -c <code>`` through ``exec_bounded`` — argv, no shell;
* a block in any other language is refused before a sandbox is acquired;
* a sandbox without ``exec_bounded`` is refused, as `maf-sandbox-deepagents` refuses one;
* the guest's own nonzero exit code answers for the result, so AutoGen's `success` is honest;
* the evidence block the sample prints is the checker's shape, under its own heading.

The fake records rather than runs, so nothing here reaches Docker. The model clients are
constructed, which proves the wiring and nothing more; no model is called. Async tests follow the
repo convention: a synchronous ``def test_*`` that drives one ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

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

        A timeout removes the container inside the backend; an overflow raises past it without
        any such act. Reusing warm here would hand the next tool call a sandbox a runaway
        program is still writing into.
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
                return (
                    await executor.execute_code_blocks(
                        [CodeBlock(code="print('x')", language="python")], CancellationToken()
                    ),
                    backend,
                )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result, backend = asyncio.run(body())
        # The disposal is the act that makes the next acquire a fresh create rather than a warm
        # reuse. It happens mid-call, before the error is returned — so the count here is what
        # the executor's own dispose added, above whatever the scope purge reached. Counting
        # rather than truthing: a fake that reports a disposal for every outcome would make an
        # `is not None` pass while proving nothing.
        assert result.exit_code == 1
        assert "byte budget" in result.output
        assert len(backend.disposed) == 2, (
            "one from the executor's condemnation, one from the scope purge — fewer means the "
            "executor returned its error without disposing the instance it just overflowed"
        )

    def test_an_overflow_failure_lands_the_disposal_before_the_error(self):
        """A disposal that fails is still best-effort: the error is returned, the key stays refused."""

        class OverflowingThenDead(InProcessSandbox):
            async def exec_bounded(self, command, *, working_directory, timeout, max_output_bytes):
                from maf_sandbox import SandboxExecOutputLimitExceeded

                raise SandboxExecOutputLimitExceeded("execution output exceeded its byte budget")

        async def body():
            from autogen_core import CancellationToken
            from autogen_core.code_executor import CodeBlock

            sandbox = OverflowingThenDead()
            router, _ = _router(sandbox)
            try:
                executor = sample_19.SandboxCodeExecutor(router, _key(), _spec())
                return await executor.execute_code_blocks(
                    [CodeBlock(code="print('x')", language="python")], CancellationToken()
                )
            finally:
                await router.dispose_scope(_key().scope, _key().thread_id)

        result = asyncio.run(body())
        assert result.exit_code == 1
        assert "byte budget" in result.output


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
    def test_the_local_road_constructs_without_a_key(self):
        import os

        saved = {name: os.environ.pop(name, None) for name in ("AZURE_OPENAI_ENDPOINT",)}
        try:
            model, credential = sample_19.build_model()
            assert credential is None
            assert type(model).__name__ == "OpenAIChatCompletionClient"
        finally:
            os.environ.update({name: value for name, value in saved.items() if value is not None})

    def test_the_azure_road_constructs_with_a_token_provider(self):
        import os

        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://fake.example.openai.azure.com"
        os.environ["AZURE_OPENAI_CHAT_MODEL"] = "gpt-5.4"
        try:
            model, credential = sample_19.build_model()
            assert credential is not None
            assert type(model).__name__ == "AzureOpenAIChatCompletionClient"
            asyncio.run(credential.close())
        finally:
            os.environ.pop("AZURE_OPENAI_ENDPOINT")
            os.environ.pop("AZURE_OPENAI_CHAT_MODEL")

    def test_an_endpoint_without_a_deployment_is_reported_not_run(self, capsys):
        import os

        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://fake.example.openai.azure.com"
        os.environ.pop("AZURE_OPENAI_CHAT_MODEL", None)
        try:
            assert sample_19.build_model() is None
            assert "AZURE_OPENAI_CHAT_MODEL" in capsys.readouterr().err
        finally:
            os.environ.pop("AZURE_OPENAI_ENDPOINT")


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
