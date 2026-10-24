"""Public-tool boundary, provenance-safe summaries and strict analyzer-report regressions."""

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from maf_sandbox import (
    CallerContext,
    Cleanup,
    Egress,
    Isolation,
    IsolationScope,
    OsFamily,
    SandboxRouter,
)
from maf_sandbox.maf import COMPLETED_TEXT, NOT_COMPLETED_TEXT
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

import maf_sandbox_shell._tool as workload
from maf_sandbox_shell import (
    ShellDialect,
    ShellFlavor,
    ShellPolicy,
    ShellSeverity,
    make_shell_tools,
    shell_sandbox_spec,
)
from maf_sandbox_shell._paths import dialect_for
from maf_sandbox_shell._report import EMPTY_INVENTORY_SHA256, IDENTITY, report_outcome


def envelope(flavor="bash", path="main.sh", diagnostics=None):
    return {
        "protocol": 1,
        "identity": IDENTITY,
        "flavor": flavor,
        "error": None,
        "dependency_inventory_sha256": EMPTY_INVENTORY_SHA256,
        "files": [
            {
                "path": path,
                "parsed": True,
                "policy_conflict": False,
                "syntax": {"exit_code": 0, "stdout": "", "stderr": ""},
                "diagnostics": diagnostics or [],
                "suppressed_diagnostics": [],
                "suppressions": [],
            }
        ],
    }


def diagnostic(rule="SC2086", severity="info", path="main.sh"):
    return {
        "file": path,
        "line": 3,
        "column": 6,
        "rule": rule,
        "severity": severity,
        "message": "AUTHOR-CONTROLLED",
    }


def outcome(data, *, policy=ShellPolicy(), hidden=False):
    return report_outcome(
        json.dumps(data).encode(),
        ShellFlavor(data["flavor"]),
        [item["path"] for item in data["files"]],
        policy,
        hidden=hidden,
    )


class RecordingSandbox(InProcessSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.uploads = []

    async def write_file(self, path, content, *, working_directory):
        self.uploads.append((path, content))
        await super().write_file(path, content, working_directory=working_directory)


def attach(data=None, flavor=ShellFlavor.BASH, report=None, sandbox=None, **kwargs):
    path = "main.sh" if flavor == ShellFlavor.BASH else "main.ps1"
    sandbox = sandbox or RecordingSandbox(
        default_stdout=json.dumps(report or envelope(flavor, path))
    )
    backend = InProcessSandboxBackend(
        sandbox,
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    store = InMemoryStore(data or {path: "echo hello"})
    context = CallerContext(
        current_scope=lambda: "test",
        current_thread_id=lambda: "thread",
        list_files=InMemoryStore.list,
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    tools = make_shell_tools(router, store, "agent", context, flavor=flavor, **kwargs)
    return tools[0], backend, store


def verdict(result):
    return next(
        (
            item.additional_properties["sandbox_verdict"]
            for item in result
            if item.additional_properties and "sandbox_verdict" in item.additional_properties
        ),
        None,
    )


@pytest.mark.parametrize("flavor", list(ShellFlavor))
def test_host_selects_flavor_and_source_bundle_is_the_only_upload(flavor):
    path = "-main.sh" if flavor == ShellFlavor.BASH else "-main.ps1"
    sandbox = RecordingSandbox(default_stdout=json.dumps(envelope(flavor, path)))
    tool, backend, store = attach({path: "echo hello"}, flavor, sandbox=sandbox)
    result = asyncio.run(tool.func(files=[path]))
    assert tool.name == f"{flavor}_validate"
    assert str(result[0].text) == COMPLETED_TEXT
    assert len(backend.disposed) == 1
    assert len(sandbox.uploads) == 1 and sandbox.uploads[0][0] == "request.json"
    assert json.loads(sandbox.uploads[0][1])["files"][0]["path"] == path
    command = sandbox.commands[0][0]
    assert "/opt/maf-shell/runner.py" in command and path not in command
    assert store.files == {path: "echo hello"}
    spec = shell_sandbox_spec(flavor=flavor)
    assert spec.kind == flavor and spec.egress == Egress.CLOSED
    assert spec.isolation_scope == IsolationScope.CALL and spec.min_cleanup == Cleanup.DISPOSE
    assert len(spec.requires) == 2


@pytest.mark.parametrize(
    "path",
    [
        "../bad.sh",
        "/bad.sh",
        "sub/.shellcheckrc",
        "sub/PSScriptAnalyzerSettings.psd1",
        "x.py",
        "x;id.sh",
        "x\\y.sh",
    ],
)
def test_unsupported_manifest_paths_never_acquire(path):
    tool, backend, _ = attach({path: "echo hi"})
    result = asyncio.run(tool.func(files=[path]))
    assert str(result[0].text) == NOT_COMPLETED_TEXT
    assert not backend.disposed


@pytest.mark.parametrize("files", [[], ["main.sh", "./main.sh"], ["absent.sh"]])
def test_empty_duplicate_and_absent_files_are_refused(files):
    tool, backend, _ = attach()
    result = asyncio.run(tool.func(files=files))
    assert str(result[0].text) == NOT_COMPLETED_TEXT
    assert not backend.disposed


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        ("x.sh", "echo hi", "bash"),
        ("x.dash", "echo hi", "dash"),
        ("x.sh", "#!/bin/sh\n", "sh"),
        ("x", "#!/usr/bin/env bash\n", "bash"),
    ],
)
def test_fixed_grammar_selection(path, text, expected):
    assert dialect_for(path, text, ShellDialect.BASH) == expected


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("x", "echo hi"),
        ("x.bash", "#!/bin/dash"),
        ("x.sh", "#!/usr/bin/env bash -c"),
        ("x.sh", "#!/bin/zsh"),
    ],
)
def test_unsupported_or_conflicting_shebangs_are_refused(path, text):
    with pytest.raises(ValueError):
        dialect_for(path, text, ShellDialect.BASH)


@pytest.mark.parametrize("severity", list(ShellSeverity))
def test_default_threshold_includes_all_unsuppressed_findings(severity):
    result = outcome(envelope(diagnostics=[diagnostic(severity=severity)]))
    assert result.completed and result.verdict == "findings"
    assert "AUTHOR-CONTROLLED" not in "\n".join(result.summary)
    assert "AUTHOR-CONTROLLED" in result.detail


def test_host_threshold_and_exclusions_do_not_remove_audit_details():
    data = envelope(diagnostics=[diagnostic()])
    for policy in [
        ShellPolicy(minimum_severity=ShellSeverity.WARNING),
        ShellPolicy(excluded_rules=("SC2086",)),
    ]:
        result = outcome(data, policy=policy)
        assert result.verdict == "clean" and "SC2086" in result.detail


def test_every_suppression_is_reported_even_when_no_findings_were_suppressed():
    data = envelope()
    data["files"][0]["suppressions"] = [
        {
            "line": 1,
            "column": 14,
            "rules": [{"first": 9999, "end_exclusive": 10000}],
            "scope": None,
        },
    ]
    result = outcome(data)
    assert result.verdict == "clean"
    assert any("suppressions are present" in item for item in result.summary)
    detail = json.loads(result.detail)["files"][0]
    assert detail["suppression_declarations"] == 1 and detail["suppressed_findings"] == 0


@pytest.mark.parametrize("field", ["parsed", "policy_conflict", "syntax"])
def test_failed_phase_cannot_return_a_verdict(field):
    data = envelope()
    data["files"][0][field] = {
        "parsed": False,
        "policy_conflict": True,
        "syntax": {"exit_code": 2, "stdout": "", "stderr": "syntax"},
    }[field]
    result = outcome(data)
    assert not result.completed and result.verdict is None


def test_hidden_names_withhold_diagnostic_and_suppression_text():
    data = envelope(path="private.sh", diagnostics=[diagnostic(path="private.sh")])
    result = outcome(data, hidden=True)
    assert not result.detail and "private.sh" not in str(result.summary)
    assert any("files[0]" in item for item in result.summary)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(protocol=True),
        lambda data: data.update(identity={"launcher": "forged"}),
        lambda data: data.update(dependency_inventory_sha256="f" * 64),
        lambda data: data["files"][0].pop("suppressions"),
        lambda data: data["files"][0].update(diagnostics=[diagnostic(severity="invented")]),
        lambda data: data["files"][0].update(diagnostics=[diagnostic(path="outside.sh")]),
        lambda data: data["files"][0].update(parsed=1),
    ],
)
def test_malformed_envelopes_are_refused(mutation):
    data = copy.deepcopy(envelope())
    mutation(data)
    with pytest.raises(ValueError):
        outcome(data)


def test_duplicate_json_keys_are_refused():
    raw = json.dumps(envelope()).replace('"protocol": 1', '"protocol": 1, "protocol": 1')
    with pytest.raises(ValueError, match="duplicate"):
        report_outcome(raw.encode(), ShellFlavor.BASH, ["main.sh"], ShellPolicy())


def test_cancellation_drains_execution_before_disposal():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class WaitingSandbox(RecordingSandbox):
            async def exec(self, command, *, working_directory, timeout):
                entered.set()
                await release.wait()
                return await super().exec(
                    command, working_directory=working_directory, timeout=timeout
                )

        sandbox = WaitingSandbox(default_stdout=json.dumps(envelope()))
        tool, backend, _ = attach(sandbox=sandbox)
        task = asyncio.create_task(tool.func(files=["main.sh"]))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not backend.disposed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(backend.disposed) == 1

    asyncio.run(scenario())


def test_hidden_argument_tool_keeps_fixed_summaries(monkeypatch):
    tool, _, _ = attach(report=envelope(diagnostics=[diagnostic()]))
    monkeypatch.setattr(
        workload, "positions_holding_hidden_content", lambda *args, **kwargs: frozenset({0})
    )
    result = asyncio.run(tool.func(files=["main.sh"]))
    rendered = "\n".join(str(item.text) for item in result)
    assert "AUTHOR-CONTROLLED" not in rendered
    assert "files[0]" in rendered


@pytest.mark.parametrize("flavor", ["sh", "powershell", "BASH"])
def test_flavor_aliases_are_refused(flavor):
    with pytest.raises(ValueError):
        attach(flavor=flavor)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 601])
def test_unbounded_deadlines_are_refused(timeout):
    with pytest.raises(ValueError):
        attach(exec_timeout_seconds=timeout)
