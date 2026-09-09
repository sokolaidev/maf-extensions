"""Per-call result labels weaken declarations using only the files the call actually read."""

import asyncio
from typing import Any

import pytest
from agent_framework import Content, FunctionInvocationContext
from agent_framework.security import LabelTrackingFunctionMiddleware

from maf_sandbox import (
    CallerContext,
    Isolation,
    ListedFile,
    SandboxRouter,
    SandboxSpec,
    SourceChannel,
    SourceIntegrity,
)
from maf_sandbox.maf import SandboxToolSession, sandboxed_tool
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend

_GUIDANCE = "A hidden result is not a successful check."
_SPEC = SandboxSpec(kind="probe", work_dir="/work")


def _attach(build, *, source="trusted", guidance=()):
    return sandboxed_tool(
        build,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        agent_dir="agent",
        spec=_SPEC,
        name="probe",
        source_integrity=source,
        nothing_survives_from=(SourceChannel.FILE_STORE,) if source == "trusted" else (),
        standing_guidance=guidance,
    )[0]


def _reading(levels, *, answer="answer", source="trusted", guidance=(), store=None):
    store = store if store is not None else InMemoryStore({"a": "content"})

    def build(session: SandboxToolSession):
        async def probe() -> Any:
            """Read the named files and return the report."""
            for level in levels:
                await session.read_file(store, ListedFile("a", level))
            return answer

        return probe

    return _attach(build, source=source, guidance=guidance)


@pytest.mark.parametrize("source", ["trusted", "untrusted"])
@pytest.mark.parametrize(
    ("levels", "weak"),
    [
        ([], False),
        ([SourceIntegrity.TRUSTED], False),
        ([SourceIntegrity.UNTRUSTED], True),
        ([None], True),
        ([SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED], True),
        ([SourceIntegrity.UNTRUSTED, SourceIntegrity.TRUSTED], True),
        ([None, SourceIntegrity.TRUSTED], True),
    ],
)
@pytest.mark.parametrize("split", [False, True])
def test_every_result_path_is_stamped_and_a_trusted_read_never_promotes(
    source, levels, weak, split
):
    answer = [Content.from_text("answer"), Content.from_text(_GUIDANCE)] if split else "answer"
    tool = _reading(levels, answer=answer, source=source, guidance=(_GUIDANCE,) if split else ())
    tool.additional_properties["confidentiality"] = "private"
    result = asyncio.run(tool.invoke(arguments={}))
    assert result[0].additional_properties["security_label"] == {
        "integrity": "untrusted" if weak else source,
        "confidentiality": "private",
    }
    if split:
        assert result[1].additional_properties["security_label"] == {
            "integrity": "trusted",
            "confidentiality": "public",
        }
    assert tool.additional_properties["source_integrity"] == source


@pytest.mark.parametrize("confidentiality", [None, "", "unknown"])
def test_without_a_valid_host_confidentiality_the_result_keeps_its_fallback(confidentiality):
    tool = _reading([None])
    if confidentiality is not None:
        tool.additional_properties["confidentiality"] = confidentiality
    assert asyncio.run(tool.invoke(arguments={}))[0].additional_properties == {}


def test_without_an_integrity_declaration_the_wrapper_cannot_override_the_input_join():
    tool = _reading([SourceIntegrity.TRUSTED], source=None)
    tool.additional_properties["confidentiality"] = "private"
    assert asyncio.run(tool.invoke(arguments={}))[0].additional_properties == {}


@pytest.mark.parametrize("confidentiality", ["public", "private", "user_identity"])
def test_the_host_can_replace_the_declarations_and_its_classification_is_copied(confidentiality):
    tool = _reading([None])
    tool.additional_properties = {
        **tool.additional_properties,
        "confidentiality": confidentiality,
    }
    result = asyncio.run(tool.invoke(arguments={}))
    assert result[0].additional_properties["security_label"]["confidentiality"] == confidentiality


def test_absent_and_refused_reads_do_not_weaken_a_call_that_received_nothing():
    class RefusingStore(InMemoryStore):
        async def read(self, path: str) -> str | None:
            raise OSError("unavailable")

    for store in (InMemoryStore({}), RefusingStore({})):
        tool = _reading([None], store=store, answer="Error: the file is unavailable")
        tool.additional_properties["confidentiality"] = "private"
        result = asyncio.run(tool.invoke(arguments={}))
        assert result[0].additional_properties["security_label"]["integrity"] == "trusted"


def test_a_refusal_after_reading_weak_content_is_demoted_too():
    tool = _reading([None], answer="Error: validation failed")
    tool.additional_properties["confidentiality"] = "private"
    result = asyncio.run(tool.invoke(arguments={}))
    assert result[0].additional_properties["security_label"]["integrity"] == "untrusted"


def test_the_real_middleware_hides_demoted_content_and_preserves_confidentiality():
    tool = _reading(
        [None],
        answer=[Content.from_text("derived"), Content.from_text(_GUIDANCE)],
        guidance=(_GUIDANCE,),
    )
    tool.additional_properties["confidentiality"] = "private"
    middleware = LabelTrackingFunctionMiddleware()
    context = FunctionInvocationContext(function=tool, arguments={})

    async def call_next():
        context.result = await tool.invoke(arguments={})

    asyncio.run(middleware.process(context, call_next))
    assert context.result[0].additional_properties.get("_variable_reference")
    assert context.result[1].text == _GUIDANCE
    assert str(context.metadata["result_label"].integrity) == "untrusted"
    assert str(context.metadata["result_label"].confidentiality) == "private"
    assert str(middleware.get_context_label().confidentiality) == "private"


def test_concurrent_calls_and_reused_content_do_not_share_their_labels():
    answer = [Content.from_text("shared result"), Content.from_text(_GUIDANCE)]

    async def run():
        both_read = asyncio.Barrier(2)
        store = InMemoryStore({"a": "content"})

        def build(session: SandboxToolSession):
            async def probe(trusted: bool) -> list[Content]:
                """Read a file and return the report."""
                level = SourceIntegrity.TRUSTED if trusted else None
                await session.read_file(store, ListedFile("a", level))
                await both_read.wait()
                return answer

            return probe

        tool = _attach(build, guidance=(_GUIDANCE,))
        tool.additional_properties["confidentiality"] = "private"
        trusted, unknown = await asyncio.gather(
            tool.invoke(arguments={"trusted": True}),
            tool.invoke(arguments={"trusted": False}),
        )
        return trusted, unknown, tool

    trusted, unknown, tool = asyncio.run(run())
    assert trusted[0].additional_properties["security_label"]["integrity"] == "trusted"
    assert unknown[0].additional_properties["security_label"]["integrity"] == "untrusted"
    assert all(item.additional_properties == {} for item in answer)
    assert tool.additional_properties["source_integrity"] == "trusted"


def test_synchronous_bodies_use_the_same_labelling_wrapper():
    def build(session: SandboxToolSession):
        def probe() -> str:
            """Return a report without acquiring a sandbox."""
            asyncio.run(session.read_file(InMemoryStore({"a": "content"}), ListedFile("a")))
            return "answer"

        return probe

    tool = _attach(build)
    tool.additional_properties["confidentiality"] = "private"
    result = asyncio.run(tool.invoke(arguments={}))
    assert result[0].additional_properties["security_label"] == {
        "integrity": "untrusted",
        "confidentiality": "private",
    }
