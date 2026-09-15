"""The architecture repair sample's real converter, evidence and cleanup boundaries."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_framework import AgentSession, FileAccessProvider, InMemoryAgentFileStore, SessionContext
from maf_sandbox import (
    DEFAULT_CAPABILITIES,
    Capability,
    Egress,
    ExecResult,
    Isolation,
    OsFamily,
    SandboxRouter,
)
from maf_sandbox.maf import list_no_files, make_caller_context
from maf_sandbox.testing import (
    FAKE_BACKEND_DECLARATIONS,
    InProcessSandbox,
    InProcessSandboxBackend,
)
from maf_sandbox_drawio import make_drawio_tools

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE = _ROOT / "samples" / "18_acas_drawio_repair"


@pytest.fixture
def sample(monkeypatch):
    scaffold_spec = importlib.util.spec_from_file_location("_scaffold", _SAMPLE / "_scaffold.py")
    assert scaffold_spec and scaffold_spec.loader
    scaffold = importlib.util.module_from_spec(scaffold_spec)
    scaffold_spec.loader.exec_module(scaffold)
    monkeypatch.setitem(sys.modules, "_scaffold", scaffold)
    spec = importlib.util.spec_from_file_location("acas_drawio_sample", _SAMPLE / "agent.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def xml(sample):
    document = ET.Element("mxGraphModel")
    root = ET.SubElement(document, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    for index, (identifier, label) in enumerate(sample.VERTICES.items()):
        cell = ET.SubElement(
            root, "mxCell", {"id": identifier, "value": label, "vertex": "1", "parent": "1"}
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {"as": "geometry", "x": str(index * 200), "y": "0", "width": "160", "height": "80"},
        )
    for identifier, (source, target) in sample.EDGES.items():
        cell = ET.SubElement(
            root,
            "mxCell",
            {"id": identifier, "edge": "1", "parent": "1", "source": source, "target": target},
        )
        ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})
    return ET.tostring(document, encoding="unicode")


class Converter(InProcessSandbox):
    async def exec(self, command, *, working_directory, timeout):
        await super().exec(command, working_directory=working_directory, timeout=timeout)
        directory = self._working_directory(working_directory)
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("renderer.py", "input.xml"):
                Path(temporary, name).write_bytes(self.contents[f"{directory}/{name}"])
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, *command[1:]],
                cwd=temporary,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
            output = Path(temporary, "diagram.drawio")
            if output.exists():
                self.contents[f"{directory}/diagram.drawio"] = output.read_bytes()
        return ExecResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)


async def exercise(sample, xml, repairs, *, read_ok=True, store=None):
    store = store if store is not None else InMemoryAgentFileStore()
    await store.write("unrelated.txt", "keep")
    storage = sample.StoredDiagrams(store)
    backend = InProcessSandboxBackend(
        Converter(),
        declarations=dataclasses.replace(
            FAKE_BACKEND_DECLARATIONS,
            os_families=frozenset({OsFamily.POSIX}),
            capabilities=DEFAULT_CAPABILITIES | {Capability.FILES_OUT},
        ),
    )

    class Timings(sample.CallTimings):
        def tool_call_ended(self, event):
            assert backend.disposed_instances[-1] is not None
            assert not backend.purged
            super().tool_call_ended(event)

    timings = Timings()
    router = SandboxRouter([backend], min_isolation=Isolation.NONE, observer=timings)
    ask = AsyncMock(side_effect=[xml, *repairs])
    seen = []

    async def read_back(path, expected):
        seen.append(path)
        assert await store.read(path) == expected
        provider = FileAccessProvider(
            store, disable_write_tools=True, disable_readonly_tool_approval=True
        )
        context = SessionContext(input_messages=[])
        await provider.before_run(agent=None, session=AgentSession(), context=context, state={})
        names = {tool.name for tool in context.tools}
        assert "file_access_write" not in names and "file_access_delete" not in names
        [read] = [tool for tool in context.tools if tool.name == "file_access_read"]
        result = sample.result_text(await read.invoke(arguments={"file_name": path}))
        return read_ok and result == expected

    try:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(sample.purge_scope, router, "test")
            cleanup.push_async_callback(storage.cleanup)
            [tool] = make_drawio_tools(
                router,
                "test",
                make_caller_context(list_no_files, lambda: "samples", lambda: "test"),
                storage.sink,
            )

            async def validate(source):
                return await sample.validate_diagram(tool, source, timings, storage)

            await sample.repair_diagram(
                ask, validate, read_back, storage, (_SAMPLE / "architecture.md").read_text("utf-8")
            )
    finally:
        assert await store.read("unrelated.txt") == "keep"
        assert all([not await store.file_exists(path) for path in storage.attempted])
        assert backend.disposed
    return ask, storage, seen


def test_only_the_selected_edge_target_is_changed(sample, xml):
    original = ET.fromstring(xml)
    broken = ET.fromstring(sample.inject_error(xml))
    differences = [
        (a.get("id"), key, a.get(key), b.get(key))
        for a, b in zip(original.iter(), broken.iter(), strict=True)
        for key in a.attrib.keys() | b.attrib.keys()
        if a.get(key) != b.get(key)
    ]
    assert differences == [("api_to_database", "target", "database", "missing_database")]


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("link", "https://example.invalid/diagram"),
        ("style", "shape=image;image=https://example.invalid/image.png;"),
        ("style", "fontFamily=https://example.invalid/font;"),
        ("onclick", "alert(1)"),
        ("value", "<img src='https://example.invalid/image.png'>"),
    ],
)
def test_external_resources_are_refused_before_storage(sample, xml, attribute, value):
    document = ET.fromstring(xml)
    document.find(".//mxCell[@id='web_to_api']").set(attribute, value)
    unsafe = ET.tostring(document, encoding="unicode")
    with pytest.raises(ValueError, match="self-contained"):
        sample.architecture(unsafe)

    async def check():
        store = InMemoryAgentFileStore()
        storage = sample.StoredDiagrams(store)
        artifact = sample.Artifact(
            name="diagram.drawio",
            content=unsafe.encode(),
            media_type="application/xml",
            call_id="test",
            kind="drawio",
        )
        with pytest.raises(ValueError, match="self-contained"):
            await storage.sink.deliver(artifact)
        assert not storage.attempted and not storage.delivered
        assert not await store.file_exists("test/diagram.drawio")

    asyncio.run(check())


def test_real_converter_rejects_then_saves_model_repair_and_cleans_up(sample, xml, capsys):
    repaired = xml.replace('value="Web client"', 'value="Web client" style="rounded=1;"')
    ask, storage, reads = asyncio.run(exercise(sample, xml, [repaired]))
    assert ask.await_count == 2
    prompt = ask.await_args_list[1].args[0]
    assert "must reference a vertex" in prompt and "missing_database" in prompt
    assert "# Order service architecture" in prompt
    assert len(storage.delivered) == 1 and reads == [storage.delivered[0][0].handle]
    document = ET.fromstring(storage.delivered[0][1].content)
    assert document.find(".//mxCell[@id='web']").get("style") == "rounded=1;"
    output = capsys.readouterr().out
    assert '"stage": "saved_and_read"' in output
    records = [
        json.loads(line.strip().removeprefix("[measured] "))
        for line in output.splitlines()
        if line.strip().startswith("[measured] {")
    ]
    calls = [record for record in records if record["stage"] == "tool_call_ended"]
    assert len(calls) == 2
    assert len({call["call"] for call in calls}) == 2
    assert all(call["tool"] == "create_drawio" and call["seconds"] > 0 for call in calls)
    assert all(call["failure"] is None and call["unclean"] == 0 for call in calls)
    assert calls[0]["call"] not in reads[0]
    assert calls[1]["call"] in reads[0]


def test_malformed_model_repair_returns_converter_diagnostic_before_retry(sample, xml, capsys):
    ask, _, _ = asyncio.run(exercise(sample, xml, ["<mxGraphModel>", xml]))
    assert ask.await_count == 3
    assert "Invalid XML" in ask.await_args_list[2].args[0]
    output = capsys.readouterr().out
    evidence_spec = importlib.util.spec_from_file_location(
        "drawio_evidence", _ROOT / "scripts/check_live_drawio_sample.py"
    )
    assert evidence_spec and evidence_spec.loader
    evidence = importlib.util.module_from_spec(evidence_spec)
    evidence_spec.loader.exec_module(evidence)
    configuration = '  [measured] {"stage":"configuration","backend":"acas","guest_egress":"closed","allowed_hosts":[]}\n'
    assert evidence.assess(configuration + output + '  [measured] {"stage":"complete"}\n') == []
    validations = [record for record in evidence.records(output) if record["stage"] == "validation"]
    assert ask.await_args_list[2].args[0].endswith(validations[1]["diagnostic"])


def test_model_repairs_a_refused_external_resource_without_storing_it(sample, xml):
    unsafe = xml.replace('id="web"', 'id="web" link="https://example.invalid/image"')
    ask, storage, _ = asyncio.run(exercise(sample, xml, [unsafe, xml]))
    assert ask.await_count == 3
    assert "delivery of diagram.drawio failed" in ask.await_args_list[2].args[0]
    assert len(storage.attempted) == len(storage.delivered) == 1


def test_model_repair_exhaustion_fails_without_delivery(sample, xml):
    with pytest.raises(RuntimeError, match="within 3 attempts"):
        asyncio.run(exercise(sample, xml, [sample.inject_error(xml)] * 3))


def test_file_access_read_is_required_and_artifact_is_removed_on_failure(sample, xml):
    with pytest.raises(RuntimeError, match="file_access_read"):
        asyncio.run(exercise(sample, xml, [xml], read_ok=False))


def test_empty_but_valid_diagram_cannot_count_as_a_repair(sample, xml):
    empty = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>'
    ask, storage, _ = asyncio.run(exercise(sample, xml, [empty, xml]))
    assert ask.await_count == 3
    assert "delivery of diagram.drawio failed" in ask.await_args_list[2].args[0]
    assert len(storage.attempted) == len(storage.delivered) == 1


@pytest.mark.parametrize("defect", ["empty", "label", "connection"])
def test_wrong_architecture_never_reaches_storage(sample, xml, defect):
    document = ET.fromstring(xml)
    if defect == "empty":
        document.find("root").clear()
    elif defect == "label":
        document.find(".//mxCell[@id='web']").set("value", "Wrong client")
    else:
        document.find(".//mxCell[@id='web_to_api']").set("target", "database")

    async def check():
        store = InMemoryAgentFileStore()
        storage = sample.StoredDiagrams(store)
        artifact = sample.Artifact(
            name="diagram.drawio",
            content=ET.tostring(document),
            media_type="application/xml",
            call_id="test",
            kind="drawio",
        )
        with pytest.raises(ValueError):
            await storage.sink.deliver(artifact)
        assert not storage.attempted and not storage.delivered
        assert not await store.file_exists("test/diagram.drawio")

    asyncio.run(check())


@pytest.mark.parametrize("error", [RuntimeError("model unavailable"), asyncio.CancelledError()])
def test_model_failure_or_cancellation_still_purges(sample, xml, error):
    with pytest.raises(type(error)):
        asyncio.run(exercise(sample, xml, [error]))


def test_partially_written_artifact_is_removed(sample, xml):
    class PartialStore(InMemoryAgentFileStore):
        async def write(self, path, content, *, overwrite=True):
            await super().write(path, content, overwrite=overwrite)
            if path.endswith("diagram.drawio"):
                raise OSError("partial write")

    with pytest.raises(RuntimeError, match="failed conversion attempted"):
        asyncio.run(exercise(sample, xml, [xml], store=PartialStore()))


def test_cleanup_checks_absence_and_attempts_remaining_deletions(sample):
    class RefusingStore(InMemoryAgentFileStore):
        async def delete(self, path):
            return False if path == "a/diagram.drawio" else await super().delete(path)

    async def check():
        store = RefusingStore()
        storage = sample.StoredDiagrams(store)
        storage.attempted.update({"a/diagram.drawio", "b/diagram.drawio"})
        for path in storage.attempted:
            await store.write(path, "xml")
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            await storage.cleanup()
        assert await store.file_exists("a/diagram.drawio")
        assert not await store.file_exists("b/diagram.drawio")

    asyncio.run(check())


@pytest.mark.parametrize("collision_during_write", [False, True])
def test_existing_artifact_is_never_owned_or_deleted(sample, xml, collision_during_write):
    class OccupiedStore(InMemoryAgentFileStore):
        async def file_exists(self, path):
            if collision_during_write:
                return False
            return await super().file_exists(path)

    async def check():
        store = OccupiedStore()
        path = "call/diagram.drawio"
        await store.write(path, "someone else's artifact")
        storage = sample.StoredDiagrams(store)
        artifact = sample.Artifact(
            name="diagram.drawio",
            content=xml.encode(),
            media_type="application/xml",
            call_id="call",
            kind="drawio",
        )
        with pytest.raises((FileExistsError, sample.SandboxLandingExists)):
            await storage.sink.deliver(artifact)
        assert not storage.attempted
        await storage.cleanup()
        assert await store.read(path) == "someone else's artifact"

    asyncio.run(check())


@pytest.mark.parametrize(
    "path,result,expected",
    [
        ("call/diagram.drawio", "<xml/>", True),
        ("wrong/diagram.drawio", "<xml/>", False),
        ("call/diagram.drawio", "claimed success", False),
    ],
)
def test_readback_matches_call_arguments_and_content(sample, path, result, expected):
    reply = SimpleNamespace(
        messages=[
            SimpleNamespace(
                contents=[
                    SimpleNamespace(
                        type="function_call",
                        name="file_access_read",
                        call_id="read",
                        arguments=json.dumps({"file_name": path}),
                    ),
                    SimpleNamespace(type="function_result", call_id="read", result=result),
                ]
            )
        ]
    )
    assert sample.read_was_verified(reply, "call/diagram.drawio", "<xml/>") is expected


def test_acas_attachment_and_service_policy_have_no_allowed_egress(sample):
    env = {key: "test" for key in sample.SANDBOX_VARS}
    env["ACAS_SANDBOX_ENDPOINT"] = "https://management.example.invalid"

    async def check():
        backend = sample.build_backend(env)
        try:
            router = SandboxRouter([backend])
            storage = sample.StoredDiagrams(InMemoryAgentFileStore())
            assert make_drawio_tools(
                router,
                "test",
                make_caller_context(list_no_files, lambda: "s", lambda: "t"),
                storage.sink,
                image="drawio:revision",
            )
            spec = sample.drawio_sandbox_spec("drawio:revision")
            assert spec.egress == Egress.CLOSED and spec.egress_allow == ()
            policy = backend._egress_policy(spec)
            assert policy.default_action == "Deny" and policy.host_rules == []
        finally:
            await backend.aclose()

    asyncio.run(check())


def test_missing_configuration_creates_no_backend(sample, monkeypatch):
    for key in (*sample.SANDBOX_VARS, *sample.MODEL_VARS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sample, "build_backend", lambda _: pytest.fail("backend created"))
    assert asyncio.run(sample.run()) == 2


def test_sample_scaffold_is_the_canonical_copy():
    assert (_SAMPLE / "_scaffold.py").read_bytes() == (
        _ROOT / "samples/01_acas_bicep/_scaffold.py"
    ).read_bytes()


@pytest.mark.parametrize("failure", [None, RuntimeError("model failed"), asyncio.CancelledError()])
def test_run_unwinds_storage_backend_and_credentials(sample, monkeypatch, failure):
    for key in (*sample.SANDBOX_VARS, *sample.MODEL_VARS):
        monkeypatch.setenv(key, "test")
    monkeypatch.setenv("GITHUB_RUN_ID", "9" * 20)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "10")
    thread_id = sample.conversation_id("sample-18")
    monkeypatch.setattr(sample, "THREAD_ID", thread_id)
    tool_threads = []
    store = InMemoryAgentFileStore()
    backend = SimpleNamespace(aclose=AsyncMock())
    router = SimpleNamespace(
        dispose_scope=AsyncMock(return_value=SimpleNamespace(disposed=0, undisposed=None))
    )
    credential = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock())

    class Credential:
        async def __aenter__(self):
            return await credential.__aenter__()

        async def __aexit__(self, *args):
            await credential.__aexit__(*args)

    def make_agent(**kwargs):
        [provider] = kwargs["context_providers"]
        assert provider.store is store and provider.disable_write_tools
        return SimpleNamespace(create_session=object)

    async def flow(ask, validate, read_back, storage, markdown):
        storage.attempted.add("this-call/diagram.drawio")
        await store.write("this-call/diagram.drawio", "test")
        if failure is not None:
            raise failure

    monkeypatch.setattr(sample, "build_backend", lambda _: backend)

    def make_router(backends, *, observer):
        assert backends == [backend] and isinstance(observer, sample.CallTimings)
        return router

    def make_tools(router, agent_id, context, sink, **kwargs):
        tool_threads.append(context.current_thread_id())
        return [object()]

    monkeypatch.setattr(sample, "SandboxRouter", make_router)
    monkeypatch.setattr(sample, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(sample, "InMemoryAgentFileStore", lambda: store)
    monkeypatch.setattr(sample, "make_drawio_tools", make_tools)
    monkeypatch.setattr(sample, "OpenAIChatClient", lambda **kwargs: object())
    monkeypatch.setattr(sample, "Agent", make_agent)
    monkeypatch.setattr(sample, "repair_diagram", flow)

    async def check():
        await store.write("unrelated.txt", "keep")
        if failure is None:
            assert await sample.run() == 0
        else:
            with pytest.raises(type(failure)):
                await sample.run()
        assert await store.read("this-call/diagram.drawio") is None
        assert await store.read("unrelated.txt") == "keep"

    asyncio.run(check())
    backend.aclose.assert_awaited_once()
    assert all(len(value) <= 63 for value in tool_threads)
    assert tool_threads == [thread_id]
    router.dispose_scope.assert_awaited_once_with(sample.SCOPE, thread_id)
    credential.__aexit__.assert_awaited_once()
