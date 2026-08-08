"""Tests for `maf_sandbox.testing`.

A fake nobody outside its own package tests is a fake that drifts silently — this pins the
public testing surface's own contract, separately from the router and bicep-kind suites that
consume it.
"""

from __future__ import annotations

import asyncio
import shlex

import pytest

from maf_sandbox import (
    Egress,
    ExecResult,
    Isolation,
    SandboxBackend,
    SandboxKey,
    SandboxSpec,
    WorkspaceContext,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandbox, InProcessSandboxBackend

_KEY = SandboxKey(scope="scope-a", thread_id="thread-1", agent_dir="devops-engineer")
_SPEC = SandboxSpec(kind="test")


class TestInProcessSandboxExec:
    def test_write_file_records_content_by_path(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.write_file("/work/a.txt", "hello"))
        assert sandbox.files == {"/work/a.txt": "hello"}

    def test_a_string_command_is_recorded_as_given(self):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.exec("echo hi", working_directory="/work", timeout=5))
        assert sandbox.commands == [("echo hi", "/work", 5)]

    def test_default_stdout_is_empty_when_not_configured(self):
        sandbox = InProcessSandbox()
        result = asyncio.run(sandbox.exec("anything", working_directory="/work", timeout=5))
        assert result.stdout == ""

    def test_default_stdout_is_the_callers_choice(self):
        """The bicep kind's tests want an empty SARIF document here — this proves it is
        configurable rather than baked into the generic fake."""
        sandbox = InProcessSandbox(default_stdout="EMPTY-SARIF")
        result = asyncio.run(sandbox.exec("bicep build x", working_directory="/work", timeout=5))
        assert result.stdout == "EMPTY-SARIF"

    def test_the_first_matching_marker_scripts_the_output(self):
        sandbox = InProcessSandbox(outputs={"lint": "LINT-OUT", "build": "BUILD-OUT"})
        result = asyncio.run(
            sandbox.exec("bicep lint main.bicep", working_directory="/work", timeout=5)
        )
        assert result.stdout == "LINT-OUT"

    def test_raises_propagates_instead_of_returning_a_result(self):
        sandbox = InProcessSandbox(raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(sandbox.exec("x", working_directory="/work", timeout=5))

    def test_exec_returns_an_exec_result(self):
        sandbox = InProcessSandbox()
        result = asyncio.run(sandbox.exec("x", working_directory="/work", timeout=5))
        assert isinstance(result, ExecResult)


class TestInProcessSandboxArgvHardening:
    """A sequence must be quoted before recording — and the quoting must be reversible.

    ``shlex.split`` of the recorded command must recover exactly the original argv: that is
    the property that proves an element containing a shell metacharacter cannot be
    re-interpreted as a second token or a second command once it reaches a real shell.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["echo", "a b"],
            ["echo", "a; rm -rf /"],
            ["echo", "$(id)"],
            ["echo", "`id`"],
            ["echo", "it's mine"],
            ["echo", 'say "hi"'],
            ["bicep", "build", "/acas/work/r1/main.bicep", "--diagnostics-format", "sarif"],
        ],
    )
    def test_a_sequence_round_trips_through_shlex_split(self, argv):
        sandbox = InProcessSandbox()
        asyncio.run(sandbox.exec(argv, working_directory="/work", timeout=5))
        (recorded,) = [command for command, _, _ in sandbox.commands]
        assert recorded == shlex.join(argv)
        assert shlex.split(recorded) == argv

    def test_marker_matching_still_works_against_a_joined_sequence(self):
        """A marker written against a string command must still match the joined form."""
        sandbox = InProcessSandbox(outputs={"bicep lint": "LINT-OUT"})
        result = asyncio.run(
            sandbox.exec(["bicep", "lint", "main.bicep"], working_directory="/work", timeout=5)
        )
        assert result.stdout == "LINT-OUT"


class TestInProcessSandboxBackend:
    def test_satisfies_the_backend_protocol(self):
        assert isinstance(InProcessSandboxBackend(), SandboxBackend)

    def test_defaults_to_in_process_name_and_process_isolation(self):
        backend = InProcessSandboxBackend()
        assert backend.name == "in-process"
        assert backend.isolation == Isolation.PROCESS

    def test_name_and_isolation_are_configurable(self):
        backend = InProcessSandboxBackend(name="fake", isolation=Isolation.VM)
        assert backend.name == "fake"
        assert backend.isolation == Isolation.VM

    def test_egress_defaults_to_allowlist_so_a_workload_attaches(self):
        """Not `CLOSED`, or every consumer's offline test becomes a test of the refusal."""
        assert InProcessSandboxBackend().egress == Egress.ALLOWLIST

    def test_egress_is_configurable(self):
        assert InProcessSandboxBackend(egress=Egress.UNRESTRICTED).egress == Egress.UNRESTRICTED

    def test_acquire_records_the_key_and_spec_and_returns_the_sandbox(self):
        sandbox = InProcessSandbox()
        backend = InProcessSandboxBackend(sandbox)
        result = asyncio.run(backend.acquire(_KEY, _SPEC))
        assert result is sandbox
        assert backend.keys == [_KEY]
        assert backend.specs == [_SPEC]

    def test_a_default_sandbox_is_created_when_none_is_given(self):
        backend = InProcessSandboxBackend()
        assert isinstance(backend.sandbox, InProcessSandbox)

    def test_acquire_error_is_raised_and_nothing_is_recorded(self):
        backend = InProcessSandboxBackend(acquire_error=RuntimeError("unavailable"))
        with pytest.raises(RuntimeError, match="unavailable"):
            asyncio.run(backend.acquire(_KEY, _SPEC))
        assert backend.keys == []
        assert backend.specs == []

    def test_dispose_records_the_key(self):
        backend = InProcessSandboxBackend()
        asyncio.run(backend.dispose(_KEY))
        assert backend.disposed == [_KEY]

    def test_dispose_scope_records_and_returns_the_purge_count(self):
        backend = InProcessSandboxBackend()
        result = asyncio.run(backend.dispose_scope("scope-a", "thread-1"))
        assert result == 1
        assert backend.purged == [("scope-a", "thread-1")]

    def test_purge_count_is_settable(self):
        backend = InProcessSandboxBackend()
        backend.purge_count = 3
        assert asyncio.run(backend.dispose_scope("scope-a", "thread-1")) == 3


class TestInMemoryStore:
    def test_read_returns_the_content(self):
        store = InMemoryStore({"main.bicep": "param x string"})
        assert asyncio.run(store.read("main.bicep")) == "param x string"

    def test_read_returns_none_for_a_missing_file_rather_than_raising(self):
        """Mirrors `AgentFileStore.read`'s real contract — a miss is data, not an exception."""
        store = InMemoryStore({})
        assert asyncio.run(store.read("missing.bicep")) is None

    def test_list_returns_every_name(self):
        store = InMemoryStore({"a.bicep": "x", "b.bicep": "y"})
        assert sorted(asyncio.run(store.list())) == ["a.bicep", "b.bicep"]

    def test_construction_copies_the_callers_dict(self):
        source = {"a.bicep": "x"}
        store = InMemoryStore(source)
        store.files["b.bicep"] = "y"
        assert "b.bicep" not in source

    def test_the_unbound_list_method_matches_workspace_context_list_files(self):
        """`list_files=InMemoryStore.list` must work directly, with no wrapper."""
        store = InMemoryStore({"a.bicep": "x", "b.bicep": "y"})
        context = WorkspaceContext(
            current_scope=lambda: "scope-a",
            current_thread_id=lambda: "thread-1",
            list_files=InMemoryStore.list,
        )
        assert sorted(asyncio.run(context.list_files(store))) == ["a.bicep", "b.bicep"]
