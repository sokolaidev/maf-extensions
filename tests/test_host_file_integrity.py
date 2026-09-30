"""Host file evidence reaches shipped tool results and the framework's write policy."""

import asyncio
import json
from dataclasses import replace

import pytest
from agent_framework import FunctionInvocationContext, tool
from agent_framework.security import (
    LabelTrackingFunctionMiddleware,
    PolicyEnforcementFunctionMiddleware,
)
from maf_sandbox import (
    CallerContext,
    Egress,
    FileStoreProvenance,
    Isolation,
    IsolationScope,
    ListedFile,
    OsFamily,
    SandboxRouter,
    SourceIntegrity,
)
from maf_sandbox.maf import (
    COMPLETED_TEXT,
    DERIVED_INTEGRITY_PROPERTY,
    file_store_provenance_middleware,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_terraform import make_terraform_tools

_REPORT = "host-vouched diagnostic text"


def _terraform_report():
    phase = {"exit_code": 0, "stdout": "", "stderr": ""}
    validation = {
        "format_version": "1.0",
        "valid": False,
        "error_count": 1,
        "warning_count": 0,
        "diagnostics": [{"severity": "error", "summary": _REPORT, "detail": _REPORT}],
    }
    return json.dumps(
        {
            "protocol": 1,
            "engine": "terraform",
            "version": "1.16.2",
            "error": None,
            "phases": {
                "init": phase,
                "validate": {**phase, "exit_code": 1, "stdout": json.dumps(validation)},
                "fmt": phase,
            },
        }
    )


def _bicep_report():
    return json.dumps(
        {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "bicep"}},
                    "results": [
                        {
                            "ruleId": "BCP033",
                            "level": "error",
                            "message": {"text": _REPORT},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "main.bicep"},
                                        "region": {"startLine": 1, "startColumn": 1},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def _attach(kind, levels, *, provenance=None, mutate_during_read=False, network=False):
    extension = {"bicep": "bicep", "terraform": "tf", "codeact": "txt"}[kind]
    names = [f"main.{extension}", f"second.{extension}"][: len(levels)]

    class Store(InMemoryStore):
        async def read(self, name):
            content = await super().read(name)
            if mutate_during_read:
                assert provenance is not None
                provenance.record(name)
            return content

    store = Store(dict.fromkeys(names, _REPORT))

    async def listing(_store):
        return [ListedFile(name, level) for name, level in zip(names, levels, strict=True)]

    context = CallerContext(
        current_scope=lambda: "scope",
        current_thread_id=lambda: "thread",
        list_files=listing,
    )
    stdout = {"bicep": _bicep_report(), "terraform": _terraform_report(), "codeact": _REPORT}[kind]
    backend = InProcessSandboxBackend(
        InProcessSandbox(default_stdout=stdout),
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    arguments: dict[str, list[str] | str] = {"files": names}
    if kind == "terraform":
        attached = make_terraform_tools(
            router, store, "agent", context, file_store_provenance=provenance
        )[0]
    elif kind == "bicep":
        attached = make_bicep_tools(
            router, store, "agent", context, egress=Egress.ALLOWLIST if network else Egress.CLOSED
        )[0]
    else:
        attached = make_codeact_tools(
            router,
            "agent",
            context,
            file_store=store,
            egress_allow=("example.com",) if network else (),
        )[0]
        arguments["code"] = "print('report')"
    return attached, arguments


@pytest.mark.parametrize("kind", ["bicep", "terraform", "codeact"])
@pytest.mark.parametrize(
    "levels,expected",
    [
        ([SourceIntegrity.TRUSTED], "trusted"),
        ([SourceIntegrity.TRUSTED, SourceIntegrity.TRUSTED], "trusted"),
        ([SourceIntegrity.UNTRUSTED], "untrusted"),
        ([None], "untrusted"),
        ([SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED], "untrusted"),
        ([SourceIntegrity.TRUSTED, None], "untrusted"),
    ],
)
def test_shipped_reports_use_the_weakest_host_file_label(kind, levels, expected):
    attached, arguments = _attach(kind, levels)
    attached.additional_properties["confidentiality"] = "private"
    items = asyncio.run(attached.invoke(arguments=arguments))
    assert items[0].text == COMPLETED_TEXT
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"] == {
        "integrity": expected,
        "confidentiality": "private",
    }
    if kind != "codeact":
        assert items[-1].additional_properties["security_label"] == {
            "integrity": "trusted",
            "confidentiality": "public",
        }
    assert attached.additional_properties[DERIVED_INTEGRITY_PROPERTY] == "untrusted"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("trusted", "trusted"),
        ("untrusted", "untrusted"),
        ("unknown", "untrusted"),
        ("changed", "untrusted"),
    ],
)
def test_terraform_report_uses_stable_provenance(state, expected):
    record = FileStoreProvenance(floor=None if state == "unknown" else SourceIntegrity.TRUSTED)
    file_store_provenance_middleware(record)
    if state == "untrusted":
        record.record("main.tf")
    attached, arguments = _attach(
        "terraform",
        [SourceIntegrity.TRUSTED],
        provenance=record,
        mutate_during_read=state == "changed",
    )
    items = asyncio.run(attached.invoke(arguments=arguments))
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"]["integrity"] == expected


@pytest.mark.parametrize("kind", ["bicep", "terraform", "codeact"])
def test_trusted_report_remains_visible_and_allows_a_following_write(kind):
    async def exercise():
        attached, arguments = _attach(kind, [SourceIntegrity.TRUSTED])
        tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
        policy = PolicyEnforcementFunctionMiddleware()
        written = []

        @tool(additional_properties={"source_integrity": "trusted"})
        async def write_file(text: str) -> str:
            written.append(text)
            return "written"

        async def call(function, arguments):
            context = FunctionInvocationContext(function=function, arguments=arguments)

            async def body():
                context.result = await function.invoke(arguments=arguments)

            async def enforce():
                await policy.process(context, body)

            await tracker.process(context, enforce)
            return context.result

        items = await call(attached, arguments)
        report = next(item for item in items if _REPORT in (item.text or ""))
        assert not report.additional_properties.get("_variable_reference")
        assert tracker.get_context_label().integrity.value == "trusted"
        await call(write_file, {"text": "fixed content"})
        assert written == ["fixed content"]

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["bicep", "codeact"])
def test_trusted_files_do_not_clear_a_kinds_network_source(kind):
    attached, arguments = _attach(kind, [SourceIntegrity.TRUSTED], network=True)
    items = asyncio.run(attached.invoke(arguments=arguments))
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"]["integrity"] == "untrusted"
