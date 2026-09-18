"""Boundary and protocol regressions using the core's in-process test backend."""

import asyncio
import json
from dataclasses import replace

import pytest
from maf_sandbox import (
    CallerContext,
    Cleanup,
    Egress,
    ExecResult,
    Isolation,
    IsolationScope,
    ListedFile,
    OsFamily,
    SandboxRouter,
    SourceIntegrity,
)
from maf_sandbox.maf import DERIVED_INTEGRITY_PROPERTY
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)

import maf_sandbox_terraform._tool as workload
from maf_sandbox_terraform import make_terraform_tools, terraform_sandbox_spec
from maf_sandbox_terraform._paths import resolve_manifest
from maf_sandbox_terraform._report import render_report


def envelope(engine="terraform", *, valid=True, fmt=0):
    diagnostics = [] if valid else [{"severity": "error", "summary": "bad", "detail": "detail"}]
    validation = {
        "format_version": "1.0",
        "valid": valid,
        "error_count": int(not valid),
        "warning_count": 0,
        "diagnostics": diagnostics,
    }
    phase = {"exit_code": 0, "stdout": "", "stderr": ""}
    return {
        "protocol": 1,
        "engine": engine,
        "version": "1.16.2",
        "error": None,
        "phases": {
            "init": phase.copy(),
            "validate": {**phase, "exit_code": int(not valid), "stdout": json.dumps(validation)},
            "fmt": {**phase, "exit_code": fmt},
        },
    }


class RecordingSandbox(InProcessSandbox):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.uploads = []

    async def write_file(self, path, content, *, working_directory):
        self.uploads.append((path, content, working_directory))
        await super().write_file(path, content, working_directory=working_directory)


def attach(data=None, *, sandbox=None, engine="terraform", **kwargs):
    sandbox = sandbox or RecordingSandbox(default_stdout=json.dumps(envelope(engine)))
    backend = InProcessSandboxBackend(
        sandbox,
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset({IsolationScope.CALL}),
        ),
    )
    store = InMemoryStore(data or {"main.tf": 'output "hello" { value = "world" }'})
    context = CallerContext(
        current_scope=lambda: "test",
        current_thread_id=lambda: "thread",
        list_files=InMemoryStore.list,
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    tool = make_terraform_tools(router, store, "agent", context, engine=engine, **kwargs)[0]
    return tool, backend, store


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_engine_contract_and_call_disposal(engine):
    spec = terraform_sandbox_spec(engine=engine)
    assert spec.kind == engine and spec.egress is Egress.CLOSED
    assert spec.min_isolation is Isolation.CONTAINER
    assert spec.requires_os_family is OsFamily.POSIX
    assert spec.isolation_scope is IsolationScope.CALL and spec.min_cleanup is Cleanup.DISPOSE
    assert spec.work_dir is None and not spec.confined_to_guest_call_path
    tool, backend, _ = attach(engine=engine)
    assert tool.name == f"{engine}_validate"

    async def scenario():
        for _ in range(2):
            result = await tool.func(files=["main.tf"])
            assert "validation PASS" in result[0].text
            assert result[1].text == workload.STANDING_GUIDANCE
        assert len(backend.disposed) == 2
        assert backend.keys[0].call_id != backend.keys[1].call_id

    asyncio.run(scenario())


@pytest.mark.parametrize("engine", ["tofu", "Terraform", "", None])
def test_engine_aliases_refused(engine):
    with pytest.raises(ValueError, match="engine"):
        terraform_sandbox_spec(engine=engine)


@pytest.mark.parametrize("timeout", [0, -1, 601, float("nan"), float("inf"), True])
def test_timeout_validation(timeout):
    with pytest.raises(ValueError, match="exec_timeout_seconds"):
        attach(exec_timeout_seconds=timeout)


@pytest.mark.parametrize(
    "files,root,listing",
    [
        (["../main.tf"], ".", ["../main.tf"]),
        (["main.tf", "./main.tf"], ".", ["main.tf"]),
        (["main.tf"], ".", ["main.tf", "./main.tf"]),
        (["main.tf"], ".", ["main.tf", "variables.tf"]),
        (["main.tf"], "absent", ["main.tf"]),
        (["readme.txt"], ".", ["readme.txt"]),
        (["main.tofu"], ".", ["main.tofu"]),
        ([".main.tf"], ".", [".main.tf"]),
        (["main.tf", ".terraform/evil.tf"], ".", ["main.tf", ".terraform/evil.tf"]),
        (["main.tf", "terraform.tfstate.backup"], ".", ["main.tf", "terraform.tfstate.backup"]),
        (["main.tf", "x.tfvars.json"], ".", ["main.tf", "x.tfvars.json"]),
        (["main.tf", "credentials.tfrc.json"], ".", ["main.tf", "credentials.tfrc.json"]),
        (["missing.tf"], ".", ["main.tf"]),
    ],
)
def test_partial_or_reserved_manifests_never_run(files, root, listing):
    with pytest.raises(ValueError):
        resolve_manifest(files, root, [ListedFile(name=x) for x in listing], "terraform")


def test_nested_modules_json_and_ancillary_assets_preserve_layout():
    data = {"root/main.tf.json": "{}", "modules/child/main.tf": "", "modules/child/data.txt": "x"}
    tool, backend, store = attach(data)
    result = asyncio.run(tool.func(files=list(data), root_module="./root"))
    assert "validation PASS" in result[0].text
    assert isinstance(backend.sandbox, RecordingSandbox)
    assert [x[0] for x in backend.sandbox.uploads] == ["project/" + x for x in data]
    assert store.files == data


@pytest.mark.parametrize("failure", ["missing", "read", "write", "oversize"])
def test_transfer_failures_prevent_exec(failure, monkeypatch):
    tool, backend, store = attach({"main.tf": "", "other.tf": ""})
    if failure == "write":

        async def fail(*args, **kwargs):
            raise OSError("private-detail")

        monkeypatch.setattr(backend.sandbox, "write_file", fail)
    else:

        async def read(name):
            if name == "main.tf":
                return ""
            if failure == "read":
                raise OSError("private-detail")
            return None if failure == "missing" else "x" * (8 * 1024 * 1024 + 1)

        monkeypatch.setattr(store, "read", read)
    result = asyncio.run(tool.func(files=["main.tf", "other.tf"]))
    assert "PASS" not in result[0].text and "private-detail" not in result[0].text
    assert not backend.sandbox.commands
    if failure == "write":
        assert len(backend.disposed) == 1
    else:
        assert not backend.keys


@pytest.mark.parametrize("valid,fmt", [(True, 0), (True, 3), (False, 0), (False, 3)])
def test_validity_and_formatting_are_independent(valid, fmt):
    report = render_report(json.dumps(envelope(valid=valid, fmt=fmt)).encode(), "terraform")
    assert ("validation PASS" in report) is valid
    assert ("formatting PASS" in report) is (fmt == 0)


@pytest.mark.parametrize("engine", ["terraform", "opentofu"])
def test_a_formatting_verdict_says_the_tool_neither_rewrites_nor_returns_files(engine):
    data = {"main.tf": 'output "hello" {\nvalue = "world"\n}\n'}
    sandbox = RecordingSandbox(default_stdout=json.dumps(envelope(engine, fmt=3)))
    tool, _, store = attach(data, sandbox=sandbox, engine=engine)
    result = asyncio.run(tool.func(files=["main.tf"]))
    assert "formatting CHANGES REQUIRED" in result[0].text
    description = " ".join(tool.description.split())
    for told in (description, result[1].text):
        assert "does not rewrite" in told and "formatted text" in told, told
    assert "fix formatting by editing the files" in result[1].text
    assert store.files == data


@pytest.mark.parametrize(
    "mutation",
    [
        "engine",
        "protocol",
        "phases",
        "valid",
        "error_count",
        "diagnostics",
        "exit_code",
        "format_version",
    ],
)
def test_corrupt_reports_fail_closed(mutation):
    data = envelope()
    if mutation in ("engine", "protocol", "phases"):
        data[mutation] = "wrong"
    elif mutation == "exit_code":
        data["phases"]["validate"][mutation] = 1
    else:
        value = json.loads(data["phases"]["validate"]["stdout"])
        value[mutation] = {
            "valid": 1,
            "error_count": 2,
            "diagnostics": [{}],
            "format_version": "2.0",
        }[mutation]
        data["phases"]["validate"]["stdout"] = json.dumps(value)
    with pytest.raises(ValueError):
        render_report(json.dumps(data).encode(), "terraform")


@pytest.mark.parametrize(
    "raw",
    [b"", b"{}", b"{", b'{"protocol":1,"protocol":1}', b"x" * (1024 * 1024 + 1)],
    ids=["empty", "object", "truncated", "duplicate", "oversized"],
)
def test_unparseable_or_oversized_reports(raw):
    with pytest.raises(ValueError):
        render_report(raw, "terraform")


def test_init_failure_is_incomplete_and_hides_all_guest_names():
    data = envelope()
    data["phases"] = {"init": {"exit_code": 1, "stdout": "hidden.tf", "stderr": "secret"}}
    report = render_report(json.dumps(data).encode(), "terraform", hidden=True)
    assert "INCOMPLETE" in report and "hidden" not in report and "secret" not in report


def test_hidden_argument_suppresses_diagnostic_text(monkeypatch):
    data = envelope(valid=False)
    data["phases"]["fmt"]["stdout"] = "hidden.tf"
    sandbox = RecordingSandbox(default_stdout=json.dumps(data))
    tool, _, _ = attach({"hidden.tf": ""}, sandbox=sandbox)
    monkeypatch.setattr(
        workload, "positions_holding_hidden_content", lambda *a, **k: frozenset({0})
    )
    result = asyncio.run(tool.func(files=["hidden.tf"]))
    assert "validation FAIL" in result[0].text
    assert "hidden.tf" not in result[0].text and '"summary"' not in result[0].text


def test_cancellation_waits_for_exec_then_disposes():
    async def scenario():
        started, finished = asyncio.Event(), asyncio.Event()

        class Delayed(RecordingSandbox):
            async def exec(self, command, *, working_directory, timeout):
                started.set()
                await finished.wait()
                return ExecResult(stdout=json.dumps(envelope()))

        tool, backend, _ = attach(sandbox=Delayed())
        pending = asyncio.create_task(tool.func(files=["main.tf"]))
        await started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not backend.disposed
        finished.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(backend.disposed) == 1

    asyncio.run(scenario())


def test_manifest_keeps_the_original_provenance_entry():
    listed = ListedFile("./main.tf", integrity=SourceIntegrity.UNTRUSTED)
    _root, selected = resolve_manifest(["main.tf"], ".", [listed], "terraform")
    assert selected[0][1] is listed


def test_result_integrity_does_not_promote_compiler_output():
    """The standing sentence is the one trusted item; the engine's own text says otherwise for
    itself, so neither depends on which tier the framework would have answered from."""
    tool, _, _ = attach()
    assert tool.additional_properties == {
        "source_integrity": "trusted",
        DERIVED_INTEGRITY_PROPERTY: "untrusted",
        "sandbox_isolation_scope": "call",
    }
    result = asyncio.run(tool.func(files=["main.tf"]))
    assert result[0].additional_properties["security_label"] == {
        "integrity": "untrusted",
        "confidentiality": "public",
    }
    assert result[1].additional_properties["security_label"] == {
        "integrity": "trusted",
        "confidentiality": "public",
    }


@pytest.mark.parametrize("count", [0, 65])
def test_file_count_bound_precedes_store_reads(count):
    tool, backend, _ = attach()
    result = asyncio.run(tool.func(files=[f"{i}.tf" for i in range(count)]))
    assert "file-count" in result[0].text
    assert not backend.keys


def test_combined_input_bytes_are_bounded():
    data = {f"{i}.tf": "x" * (8 * 1024 * 1024) for i in range(5)}
    tool, backend, _ = attach(data)
    result = asyncio.run(tool.func(files=list(data)))
    assert "transfer byte limits" in result[0].text
    assert not backend.keys


def test_non_utf8_text_is_refused_before_acquire():
    tool, backend, _ = attach({"main.tf": "\ud800"})
    result = asyncio.run(tool.func(files=["main.tf"]))
    assert "INCOMPLETE" in result[0].text
    assert not backend.keys


@pytest.mark.parametrize(
    "case", ["nonfinite", "stderr", "truncated", "failed-init-with-validation"]
)
def test_incomplete_guest_reports_never_pass(case):
    data = envelope()
    if case == "stderr":
        data["phases"]["validate"]["stderr"] = "unexpected error"
    elif case == "truncated":
        data["phases"]["validate"]["stdout"] = '{"valid":true'
    elif case == "failed-init-with-validation":
        data["phases"]["init"]["exit_code"] = 1
    else:
        data["unused"] = float("nan")
    tool, backend, _ = attach(sandbox=RecordingSandbox(default_stdout=json.dumps(data)))
    result = asyncio.run(tool.func(files=["main.tf"]))
    assert "INCOMPLETE" in result[0].text and "PASS" not in result[0].text
    assert len(backend.disposed) == 1


class TestWhatAFidesHostSeesOfASplitResult:
    """Driven against the real middleware, because the value of the split is entirely its."""

    def _processed(self, tool, files):
        from agent_framework import FunctionInvocationContext
        from agent_framework.security import LabelTrackingFunctionMiddleware

        middleware = LabelTrackingFunctionMiddleware()
        arguments = {"files": files}
        context = FunctionInvocationContext(function=tool, arguments=arguments)

        async def call_next() -> None:
            context.result = await tool.invoke(arguments=arguments)

        asyncio.run(middleware.process(context, call_next))
        seen = [
            "hidden" if (item.additional_properties or {}).get("_variable_reference") else item.text
            for item in context.result
        ]
        return seen, context.metadata["result_label"], middleware.get_context_label()

    def _tool_answering_one_string(self, text):
        """What this kind would be without the split: the same claim over a single string."""
        from agent_framework import tool as as_tool

        async def terraform_validate(files: list[str]) -> str:
            return text

        return as_tool(
            name="terraform_validate",
            additional_properties={"source_integrity": "untrusted"},
        )(terraform_validate)

    def test_the_sentence_stays_readable_while_the_report_is_hidden(self):
        tool, _, _ = attach()

        seen, _, _ = self._processed(tool, ["main.tf"])

        assert seen == ["hidden", workload.STANDING_GUIDANCE]

    def test_one_string_would_have_hidden_the_sentence_with_it(self):
        """The counterfactual: the same host, the same claim, one item."""
        seen, _, _ = self._processed(self._tool_answering_one_string("PASS"), ["main.tf"])

        assert seen == ["hidden"]

    def test_the_conversation_stays_trusted(self):
        """Only visible items taint, and the visible one is a constant this package ships."""
        tool, _, _ = attach()

        _, result, conversation = self._processed(tool, ["main.tf"])

        assert str(result.integrity) == "untrusted"
        assert str(conversation.integrity) == "trusted"
