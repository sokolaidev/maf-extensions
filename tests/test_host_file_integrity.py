"""Host file evidence reaches shipped tool results and the framework's write policy."""

import asyncio
import json
from dataclasses import replace

import pytest
from agent_framework import FunctionInvocationContext, tool
from agent_framework.security import (
    ContentLabel,
    IntegrityLabel,
    LabelTrackingFunctionMiddleware,
    PolicyEnforcementFunctionMiddleware,
)
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    CallerContext,
    Capability,
    Egress,
    ExecResult,
    FileStoreProvenance,
    Isolation,
    IsolationScope,
    ListedFile,
    OsFamily,
    SandboxRouter,
    SourceIntegrity,
    make_file_system_sink,
)
from maf_sandbox.maf import (
    COMPLETED_TEXT,
    DERIVED_INTEGRITY_PROPERTY,
    NOT_COMPLETED_TEXT,
    argument_provenance_middleware,
    file_store_provenance_middleware,
)
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InMemoryStore,
    InProcessSandbox,
    InProcessSandboxBackend,
)
from maf_sandbox_antivirus import make_antivirus_tools
from maf_sandbox_bicep import make_bicep_tools
from maf_sandbox_codeact import make_codeact_tools
from maf_sandbox_document import make_document_tools
from maf_sandbox_drawio import DrawioExport, make_drawio_export_tools, make_drawio_tools
from maf_sandbox_shell import ShellFlavor, make_shell_tools
from maf_sandbox_shell._report import EMPTY_INVENTORY_SHA256, IDENTITY
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


def _attach(
    kind,
    levels,
    *,
    provenance=None,
    mutation=None,
    network=False,
    egress_integrity=None,
    argument_integrity=None,
):
    extension = {"bicep": "bicep", "terraform": "tf", "codeact": "txt"}[kind]
    names = [f"main.{extension}", f"second.{extension}"][: len(levels)]

    class Store(InMemoryStore):
        async def read(self, name):
            content = await super().read(name)
            if mutation in {"during-read", "changed-back"}:
                assert provenance is not None
                provenance.record(name)
                if mutation == "changed-back":
                    provenance.forget(name)
            return content

    store = Store(dict.fromkeys(names, _REPORT))

    async def listing(_store):
        entries = [ListedFile(name, level) for name, level in zip(names, levels, strict=True)]
        if mutation == "after-listing":
            assert provenance is not None
            provenance.record(names[0])
        return entries

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
            router,
            store,
            "agent",
            context,
            file_store_provenance=provenance,
            argument_integrity=argument_integrity,
        )[0]
    elif kind == "bicep":
        attached = make_bicep_tools(
            router,
            store,
            "agent",
            context,
            egress=Egress.ALLOWLIST if network else Egress.CLOSED,
            file_store_provenance=provenance,
            egress_integrity=egress_integrity,
            argument_integrity=argument_integrity,
        )[0]
    else:
        attached = make_codeact_tools(
            router,
            "agent",
            context,
            file_store=store,
            file_store_provenance=provenance,
            egress_allow=("example.com",) if network else (),
            argument_integrity=argument_integrity,
        )[0]
        arguments["code"] = "print('report')"
    return attached, arguments


async def _tracked_call(function, arguments, tracker, observer=None):
    context = FunctionInvocationContext(function=function, arguments=arguments)

    async def invoke():
        context.result = await function.invoke(arguments=context.arguments, context=context)

    async def tracked():
        await tracker.process(context, invoke)

    if observer == "outer":
        await argument_provenance_middleware().process(context, tracked)
    elif observer == "inner":

        async def observed():
            await argument_provenance_middleware().process(context, invoke)

        await tracker.process(context, observed)
    else:
        await tracked()
    return context.result


async def _taint(tracker):
    @tool(additional_properties={"source_integrity": "untrusted"})
    async def source() -> str:
        return "External content."

    await _tracked_call(source, {}, tracker)
    assert tracker.get_context_label().integrity is IntegrityLabel.UNTRUSTED


@pytest.mark.parametrize("kind", ["bicep", "terraform", "codeact"])
@pytest.mark.parametrize("observer", [None, "outer", "inner"])
@pytest.mark.parametrize("tainted", [False, True])
@pytest.mark.parametrize("file_level", [SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED, None])
@pytest.mark.parametrize("override", [None, SourceIntegrity.TRUSTED])
def test_quarantine_off_returns_diagnostics_with_names_and_files_folded(
    kind, observer, tainted, file_level, override
):
    async def scenario():
        attached, arguments = _attach(kind, [file_level], argument_integrity=override)
        tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
        if tainted:
            await _taint(tracker)
        items = await _tracked_call(attached, arguments, tracker, observer)
        reports = [item for item in items if _REPORT in (item.text or "")]
        assert reports
        expected = (
            "trusted"
            if file_level is SourceIntegrity.TRUSTED and (not tainted or override is not None)
            else "untrusted"
        )
        assert all(
            item.additional_properties["security_label"]["integrity"] == expected
            for item in reports
        )
        assert not any("argument names are hidden" in (item.text or "") for item in items)
        assert items[0].text == COMPLETED_TEXT
        assert items[0].additional_properties["security_label"]["integrity"] == "trusted"
        if expected == "untrusted":
            assert tracker.get_context_label().integrity is IntegrityLabel.UNTRUSTED

    asyncio.run(scenario())


@pytest.mark.parametrize("quarantine", [False, True])
@pytest.mark.parametrize("observer", [None, "outer", "inner"])
@pytest.mark.parametrize("override", [None, SourceIntegrity.TRUSTED])
def test_manual_hidden_terraform_names_remain_withheld(quarantine, observer, override):
    async def scenario():
        attached, arguments = _attach(
            "terraform", [SourceIntegrity.TRUSTED], argument_integrity=override
        )
        tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=quarantine)
        reference = tracker.get_variable_store().store(
            "main.tf", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        arguments["files"] = [f"[{reference}]"]
        items = await _tracked_call(attached, arguments, tracker, observer)
        assert not any(_REPORT in (item.text or "") for item in items)
        assert not any("main.tf" in (item.text or "") for item in items)
        assert items[0].text == COMPLETED_TEXT

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [None, SourceIntegrity.TRUSTED])
def test_bicep_trusted_refusals_only_echo_trusted_argument_names(override):
    async def scenario():
        attached, _ = _attach("bicep", [SourceIntegrity.TRUSTED], argument_integrity=override)
        tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
        await _taint(tracker)
        items = await _tracked_call(attached, {"files": ["untrusted-name.txt"]}, tracker)
        trusted = "\n".join(
            item.text or ""
            for item in items
            if item.additional_properties.get("security_label", {}).get("integrity") == "trusted"
        )
        assert ("untrusted-name.txt" in trusted) is (override is not None)
        assert "only accepts" in trusted

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["shell", "document", "drawio", "drawio-create", "terraform-format"]
)
@pytest.mark.parametrize("tainted", [False, True])
@pytest.mark.parametrize("level", [SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED])
@pytest.mark.parametrize("override", [None, SourceIntegrity.TRUSTED])
def test_other_kind_reports_fold_argument_integrity(kind, tainted, level, override, tmp_path):
    name = {
        "shell": "main.sh",
        "document": "input.pdf",
        "drawio": "input.drawio",
        "drawio-create": "unused",
        "terraform-format": "main.tf",
    }[kind]
    phase = {"exit_code": 0, "stdout": "", "stderr": ""}
    shell_report = {
        "protocol": 1,
        "identity": IDENTITY,
        "flavor": "bash",
        "error": None,
        "dependency_inventory_sha256": EMPTY_INVENTORY_SHA256,
        "files": [
            {
                "path": name,
                "parsed": True,
                "policy_conflict": False,
                "syntax": phase,
                "diagnostics": [
                    {
                        "file": name,
                        "line": 1,
                        "column": 1,
                        "rule": "SC2086",
                        "severity": "info",
                        "message": _REPORT,
                    }
                ],
                "suppressed_diagnostics": [],
                "suppressions": [],
            }
        ],
    }
    format_report = {
        "protocol": 1,
        "engine": "terraform",
        "version": "1.16.6",
        "error": None,
        "mode": "format",
        "phases": {"fmt": phase},
        "formatted_files": {name: _REPORT},
    }

    class Guest(InProcessSandbox):
        async def exec(self, *args, **kwargs):
            if kind in {"shell", "terraform-format"}:
                return ExecResult(
                    stdout=json.dumps(shell_report if kind == "shell" else format_report)
                )
            return ExecResult(exit_code=1, stderr=_REPORT)

    class Store:
        async def read(self, path):
            return b"document" if kind == "document" else "original text"

    async def listing(store):
        return [ListedFile(name, level)]

    backend = InProcessSandboxBackend(
        Guest(),
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = CallerContext(lambda: "scope", lambda: "thread", listing)
    sink = make_file_system_sink(tmp_path)
    store = Store()
    if kind == "shell":
        attached = make_shell_tools(
            router, store, "agent", context, flavor=ShellFlavor.BASH, argument_integrity=override
        )[0]
    elif kind == "terraform-format":
        attached = make_terraform_tools(
            router, store, "agent", context, formatting=True, argument_integrity=override
        )[1]
    elif kind == "document":
        attached = make_document_tools(
            router, store, "agent", context, sink, argument_integrity=override
        )[0]
    elif kind == "drawio":
        attached = make_drawio_export_tools(
            router,
            "agent",
            context,
            sink,
            store,
            export=DrawioExport(),
            argument_integrity=override,
        )[0]
    else:
        attached = make_drawio_tools(router, "agent", context, sink, argument_integrity=override)[0]
    arguments = (
        {"files": [name]}
        if kind in {"shell", "terraform-format"}
        else {"xml": "<mxfile/>"}
        if kind == "drawio-create"
        else {"file": name}
    )

    async def scenario():
        tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
        if tainted:
            await _taint(tracker)
        items = await _tracked_call(attached, arguments, tracker, "inner")
        report = next(item for item in items if _REPORT in (item.text or ""))
        expected = (
            "trusted"
            if (not tainted or override is not None)
            and (level is SourceIntegrity.TRUSTED or kind == "drawio-create")
            else "untrusted"
        )
        assert report.additional_properties["security_label"]["integrity"] == expected
        assert items[0].additional_properties["security_label"]["integrity"] == "trusted"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "levels,expected",
    [
        ([SourceIntegrity.TRUSTED], "trusted"),
        ([SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED], "untrusted"),
        ([SourceIntegrity.TRUSTED, None], "untrusted"),
    ],
)
def test_bicep_reports_accept_host_trusted_registry_sources(levels, expected):
    attached, arguments = _attach(
        "bicep", levels, network=True, egress_integrity=SourceIntegrity.TRUSTED
    )
    attached.additional_properties["confidentiality"] = "private"
    items = asyncio.run(attached.invoke(arguments=arguments))
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"] == {
        "integrity": expected,
        "confidentiality": "private",
    }
    assert attached.additional_properties[DERIVED_INTEGRITY_PROPERTY] == "untrusted"


def test_bicep_refuses_trusted_egress_in_closed_mode():
    with pytest.raises(ValueError, match="egress_integrity=TRUSTED"):
        _attach("bicep", [SourceIntegrity.TRUSTED], egress_integrity=SourceIntegrity.TRUSTED)


@pytest.mark.parametrize("mutation", [None, "after-listing", "during-read", "changed-back"])
def test_bicep_trusted_egress_keeps_read_time_provenance_checks(mutation):
    record = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
    file_store_provenance_middleware(record)
    attached, arguments = _attach(
        "bicep",
        [SourceIntegrity.TRUSTED],
        network=True,
        egress_integrity=SourceIntegrity.TRUSTED,
        provenance=record,
        mutation=mutation,
    )
    items = asyncio.run(attached.invoke(arguments=arguments))
    if mutation is not None:
        _assert_torn_refusal(items)
        return
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"]["integrity"] == "trusted"


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
        ("after-listing", "untrusted"),
        ("during-read", "untrusted"),
        ("changed-back", "untrusted"),
    ],
)
@pytest.mark.parametrize("kind", ["bicep", "terraform", "codeact"])
def test_shipped_report_uses_stable_provenance(kind, state, expected):
    record = FileStoreProvenance(floor=None if state == "unknown" else SourceIntegrity.TRUSTED)
    file_store_provenance_middleware(record)
    if state == "untrusted":
        extension = {"bicep": "bicep", "terraform": "tf", "codeact": "txt"}[kind]
        record.record(f"main.{extension}")
    attached, arguments = _attach(
        kind,
        [SourceIntegrity.TRUSTED],
        provenance=record,
        mutation=state,
    )
    items = asyncio.run(attached.invoke(arguments=arguments))
    if state in {"after-listing", "during-read", "changed-back"}:
        _assert_torn_refusal(items)
        return
    report = next(item for item in items if _REPORT in (item.text or ""))
    assert report.additional_properties["security_label"]["integrity"] == expected


@pytest.mark.parametrize("kind", ["bicep", "terraform", "codeact"])
@pytest.mark.parametrize("mutation", [None, "after-listing", "during-read", "changed-back"])
def test_trusted_report_or_torn_refusal_allows_a_following_write(kind, mutation):
    async def exercise():
        record = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
        file_store_provenance_middleware(record)
        attached, arguments = _attach(
            kind, [SourceIntegrity.TRUSTED], provenance=record, mutation=mutation
        )
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
        if mutation is not None:
            _assert_torn_refusal(items)
        else:
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


def _assert_torn_refusal(items):
    assert items[0].text == NOT_COMPLETED_TEXT
    assert any("changed while this call" in (item.text or "") for item in items)
    assert not any(_REPORT in (item.text or "") for item in items)
    assert all(
        item.additional_properties.get("security_label", {}).get("integrity", "trusted")
        == "trusted"
        for item in items
    )


@pytest.mark.parametrize("kind", ["shell", "document", "drawio", "antivirus"])
@pytest.mark.parametrize("when", ["listing", "read"])
def test_other_file_reading_kinds_stop_before_execution(kind, when, tmp_path):
    record = FileStoreProvenance(floor=SourceIntegrity.TRUSTED)
    file_store_provenance_middleware(record)
    name = {
        "shell": "main.sh",
        "document": "input.pdf",
        "drawio": "input.drawio",
        "antivirus": "input.zip",
    }[kind]

    class Store:
        async def read(self, path):
            if when == "read":
                record.record(path)
            return b"private bytes" if kind in {"document", "antivirus"} else "private text"

    async def listing(_store):
        if when == "listing":
            record.record(name)
        return [ListedFile(name, SourceIntegrity.TRUSTED)]

    class Guest(InProcessSandbox):
        async def exec(self, *args, **kwargs):
            pytest.fail("A changed-input call must not execute a guest program")

    backend = InProcessSandboxBackend(
        Guest(),
        isolation=Isolation.CONTAINER,
        declarations=replace(
            FAKE_BACKEND_DECLARATIONS,
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
            os_families=frozenset({OsFamily.POSIX}),
            isolation_scopes=frozenset(IsolationScope),
        ),
    )
    router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
    context = CallerContext(lambda: "scope", lambda: "thread", listing)
    store = Store()
    sink = make_file_system_sink(tmp_path)
    if kind == "shell":
        attached = make_shell_tools(
            router, store, "agent", context, flavor=ShellFlavor.BASH, file_store_provenance=record
        )[0]
    elif kind == "document":
        attached = make_document_tools(
            router, store, "agent", context, sink, file_store_provenance=record
        )[0]
    elif kind == "drawio":
        attached = make_drawio_export_tools(
            router,
            "agent",
            context,
            sink,
            store,
            export=DrawioExport(),
            file_store_provenance=record,
        )[0]
    else:
        attached = make_antivirus_tools(
            router, store, "agent", context, file_store_provenance=record
        )[0]
    arguments = {"files": [name]} if kind == "shell" else {"file": name}
    items = asyncio.run(attached.invoke(arguments=arguments))
    _assert_torn_refusal(items)
    assert "private bytes" not in str(items) and "private text" not in str(items)
