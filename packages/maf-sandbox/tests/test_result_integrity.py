"""Per-call result labels use the host evidence for files the call actually read."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from agent_framework import Content, FunctionInvocationContext
from agent_framework.security import LabelTrackingFunctionMiddleware

from maf_sandbox import (
    CallerContext,
    Capability,
    Egress,
    HostToolAggregate,
    Isolation,
    ListedFile,
    SandboxLimits,
    SandboxRouter,
    SandboxSpec,
    SourceChannel,
    SourceIntegrity,
    TransferLimits,
)
from maf_sandbox.maf import (
    DERIVED_INTEGRITY_PROPERTY,
    SandboxResult,
    SandboxToolSession,
    sandboxed_tool,
)
from maf_sandbox.testing import FAKE_BACKEND_DECLARATIONS, InMemoryStore, InProcessSandboxBackend

_GUIDANCE = "A hidden result is not a successful check."
_SPEC = SandboxSpec(kind="probe", work_dir="/work")


def _attach(build, *, source="trusted", guidance=(), spec=_SPEC, contract=False, **kwargs):
    return sandboxed_tool(
        build,
        router=SandboxRouter(
            [
                InProcessSandboxBackend(
                    declarations=replace(
                        FAKE_BACKEND_DECLARATIONS,
                        capabilities=FAKE_BACKEND_DECLARATIONS.capabilities
                        | {Capability.HOST_TOOLS},
                        egress_modes=frozenset(Egress),
                        limits=SandboxLimits(
                            files_in=TransferLimits(64 * 1024 * 1024, 256 * 1024 * 1024, 1024),
                            files_out=TransferLimits(64 * 1024 * 1024, 256 * 1024 * 1024, 1024),
                        ),
                    )
                )
            ],
            min_isolation=Isolation.NONE,
        ),
        context=CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        agent_id="agent",
        spec=spec,
        result_contract=contract,
        name="probe",
        source_integrity=source,
        nothing_survives_from=(SourceChannel.FILE_STORE,) if source == "trusted" else (),
        standing_guidance=guidance,
        **kwargs,
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
def test_every_result_path_uses_the_host_fold_or_the_no_read_default(source, levels, weak, split):
    answer = [Content.from_text("answer"), Content.from_text(_GUIDANCE)] if split else "answer"
    tool = _reading(levels, answer=answer, source=source, guidance=(_GUIDANCE,) if split else ())
    tool.additional_properties["confidentiality"] = "private"
    result = asyncio.run(tool.invoke(arguments={}))
    assert result[0].additional_properties["security_label"] == {
        "integrity": ("untrusted" if weak else "trusted") if levels else source,
        "confidentiality": "private",
    }
    if split:
        assert result[1].additional_properties["security_label"] == {
            "integrity": "trusted",
            "confidentiality": "public",
        }
    if split:
        # Guidance to keep visible is what raises the declaration: the tool tells the framework
        # the labels are the wrapper's to write, and keeps its own claim on a key of its own.
        assert tool.additional_properties["source_integrity"] == "trusted"
        assert tool.additional_properties[DERIVED_INTEGRITY_PROPERTY] == source
    else:
        assert tool.additional_properties["source_integrity"] == source
        assert DERIVED_INTEGRITY_PROPERTY not in tool.additional_properties


@pytest.mark.parametrize("confidentiality", [None, "", "unknown"])
def test_without_a_valid_host_confidentiality_the_result_keeps_its_fallback(confidentiality):
    tool = _reading([None])
    if confidentiality is not None:
        tool.additional_properties["confidentiality"] = confidentiality
    assert asyncio.run(tool.invoke(arguments={}))[0].additional_properties == {}


@pytest.mark.parametrize("confidentiality", [None, "", "unknown"])
def test_a_raised_tool_floors_an_unreadable_confidentiality_rather_than_dropping_the_label(
    confidentiality,
):
    """The fallback a dropped label falls to is the raised declaration, so the label is written
    either way. The framework keeps the stricter of the item's classification and the
    invocation's, so flooring costs the item nothing."""
    tool = _reading(
        [None],
        answer=[Content.from_text("derived"), Content.from_text(_GUIDANCE)],
        guidance=(_GUIDANCE,),
    )
    if confidentiality is not None:
        tool.additional_properties["confidentiality"] = confidentiality
    assert asyncio.run(tool.invoke(arguments={}))[0].additional_properties["security_label"] == {
        "integrity": "untrusted",
        "confidentiality": "public",
    }


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


@pytest.mark.parametrize("source", ["trusted", "untrusted"])
def test_absent_and_refused_reads_keep_the_declaration(source):
    class RefusingStore(InMemoryStore):
        async def read(self, path: str) -> str | None:
            raise OSError("unavailable")

    for store in (InMemoryStore({}), RefusingStore({})):
        tool = _reading([None], source=source, store=store, answer="Error: the file is unavailable")
        tool.additional_properties["confidentiality"] = "private"
        result = asyncio.run(tool.invoke(arguments={}))
        assert result[0].additional_properties["security_label"]["integrity"] == source


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

        tool = _attach(build, source="untrusted", guidance=(_GUIDANCE,))
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


class TestTheFrameworkContractARaisedToolRestsOn:
    """Three framework behaviours, driven against a bare tool so a change in them fails here.

    Raising a committing tool's declaration to ``trusted`` puts the weight on per-item labels:
    what keeps a sandbox's own output out of the conversation is the wrapper's ``untrusted``
    stamp rather than the tool's declaration. A framework that stopped honouring that stamp
    would hand every derived item the raised declaration, visible and trusted — so these are
    asserted at the boundary rather than trusted to stay true.
    """

    def _processed(self, items, *, declarations):
        from agent_framework import FunctionInvocationContext, FunctionTool

        async def body() -> list[Content]:
            return items

        tool = FunctionTool(name="probe", func=body, additional_properties=declarations)
        middleware = LabelTrackingFunctionMiddleware()
        context = FunctionInvocationContext(function=tool, arguments={})

        async def call_next() -> None:
            context.result = await tool.invoke(arguments={})

        asyncio.run(middleware.process(context, call_next))
        return context, middleware

    @staticmethod
    def _labelled(text, **label):
        return Content.from_text(text, additional_properties={"security_label": label})

    def test_an_items_untrusted_label_restricts_a_trusted_declaration(self):
        """The one that must not change: this is what hides a sandbox's output."""
        context, _ = self._processed(
            [self._labelled("derived", integrity="untrusted", confidentiality="public")],
            declarations={"source_integrity": "trusted"},
        )
        assert context.result[0].additional_properties.get("_variable_reference")
        assert str(context.metadata["result_label"].integrity) == "untrusted"

    def test_an_unlabelled_item_takes_the_declaration_whole(self):
        """Why every item of a raised tool is labelled: silence here reads as trusted."""
        context, _ = self._processed(
            [Content.from_text("derived")], declarations={"source_integrity": "trusted"}
        )
        assert not context.result[0].additional_properties.get("_variable_reference")
        assert str(context.metadata["result_label"].integrity) == "trusted"

    def test_an_items_public_floor_does_not_declassify_the_call(self):
        """Why an unreadable host classification floors at ``public`` instead of dropping the
        label: confidentiality combines, so the floor cannot loosen what the call carries."""
        context, _ = self._processed(
            [self._labelled("derived", integrity="untrusted", confidentiality="public")],
            declarations={"source_integrity": "trusted", "confidentiality": "private"},
        )
        assert str(context.metadata["result_label"].confidentiality) == "private"


def _spec_with_other_source(channel):
    if channel == "closed":
        return _SPEC
    if channel == "allowlist":
        return replace(_SPEC, egress=Egress.ALLOWLIST, egress_allow=("example.com",))
    if channel == "unrestricted":
        return replace(_SPEC, egress=Egress.UNRESTRICTED)
    levels = {
        "host-trusted": SourceIntegrity.TRUSTED,
        "host-untrusted": SourceIntegrity.UNTRUSTED,
        "host-pure": None,
    }
    surface = (
        None
        if channel == "host-missing"
        else HostToolAggregate(
            result_integrity=levels[channel],
            outbound_caps=frozenset(),
            identities=frozenset(),
            requires_approval=False,
            has_undeclared=False,
            response_limits=TransferLimits(1024, 1024, 1),
            max_host_tool_calls_per_run=1,
        )
    )
    return replace(_SPEC, requires=_SPEC.requires | {Capability.HOST_TOOLS}, host_tools=surface)


@pytest.mark.parametrize(
    "channel,expected",
    [
        ("closed", "trusted"),
        ("allowlist", "untrusted"),
        ("unrestricted", "untrusted"),
        ("host-missing", "untrusted"),
        ("host-untrusted", "untrusted"),
        ("host-trusted", "trusted"),
        ("host-pure", "trusted"),
    ],
)
@pytest.mark.parametrize("form", ["string", "items", "guidance", "contract"])
@pytest.mark.parametrize("synchronous", [False, True])
def test_trusted_files_cannot_promote_other_untrusted_sources(channel, expected, form, synchronous):
    store = InMemoryStore({"a": "trusted file"})
    answer = {
        "string": "external report",
        "items": [Content.from_text("external report")],
        "guidance": [Content.from_text("external report"), Content.from_text(_GUIDANCE)],
        "contract": SandboxResult(completed=True, output=("external report",)),
    }[form]

    def build(session):
        async def read():
            await session.read_file(store, ListedFile("a", SourceIntegrity.TRUSTED))
            return answer

        def read_synchronously():
            return asyncio.run(read())

        return read_synchronously if synchronous else read

    tool = _attach(
        build,
        source="untrusted",
        spec=_spec_with_other_source(channel),
        guidance=(_GUIDANCE,) if form == "guidance" else (),
        contract=form == "contract",
    )
    tool.additional_properties["confidentiality"] = "private"
    items = asyncio.run(tool.invoke(arguments={}))
    report = next(item for item in items if item.text == "external report")
    assert report.additional_properties["security_label"] == {
        "integrity": expected,
        "confidentiality": "private",
    }


@pytest.mark.parametrize(
    "egress_integrity", [None, SourceIntegrity.UNTRUSTED, SourceIntegrity.TRUSTED]
)
@pytest.mark.parametrize(
    "levels",
    [[], [SourceIntegrity.TRUSTED], [SourceIntegrity.TRUSTED, SourceIntegrity.UNTRUSTED], [None]],
)
@pytest.mark.parametrize("contract", [False, True])
def test_egress_trust_clears_only_the_network_source(egress_integrity, levels, contract):
    store = InMemoryStore({"a": "content"})

    def build(session):
        async def read():
            for level in levels:
                await session.read_file(store, ListedFile("a", level))
            return SandboxResult(completed=True, output=("report",)) if contract else "report"

        return read

    tool = _attach(
        build,
        source="untrusted",
        spec=_spec_with_other_source("allowlist"),
        contract=contract,
        egress_integrity=egress_integrity,
        outbound_max_confidentiality="private",
    )
    tool.additional_properties["confidentiality"] = "private"
    items = asyncio.run(tool.invoke(arguments={}))
    report = next(item for item in items if item.text == "report")
    expected = (
        "trusted"
        if egress_integrity is SourceIntegrity.TRUSTED and levels == [SourceIntegrity.TRUSTED]
        else "untrusted"
    )
    assert report.additional_properties["security_label"] == {
        "integrity": expected,
        "confidentiality": "private",
    }
    assert tool.additional_properties["max_allowed_confidentiality"] == "private"
    claim = DERIVED_INTEGRITY_PROPERTY if contract else "source_integrity"
    assert tool.additional_properties[claim] == "untrusted"


@pytest.mark.parametrize("channel", ["closed", "unrestricted"])
def test_trusted_egress_requires_an_allowlist_at_attach(channel):
    with pytest.raises(ValueError, match="egress_integrity=TRUSTED requires Egress.ALLOWLIST"):
        _attach(
            lambda _: pytest.fail("body must not be built"),
            source="untrusted",
            spec=_spec_with_other_source(channel),
            egress_integrity=SourceIntegrity.TRUSTED,
        )


def test_an_unknown_egress_integrity_is_refused_at_attach():
    with pytest.raises(ValueError, match="SourceIntegrity"):
        _attach(lambda _: None, source="untrusted", egress_integrity="unknown")


@pytest.mark.parametrize("mapping", [False, True])
def test_trusting_egress_does_not_license_a_blanket_trusted_output(mapping):
    with pytest.raises(ValueError, match="network"):
        _attach(
            lambda _: pytest.fail("body must not be built"),
            spec=_spec_with_other_source("allowlist"),
            egress_integrity=SourceIntegrity.TRUSTED,
            declarations={"source_integrity": "trusted"} if mapping else None,
        )


def test_unconfigured_hosts_still_attach_nothing():
    for router in (None, SandboxRouter([])):
        assert (
            sandboxed_tool(
                lambda _: pytest.fail("body must not be built"),
                router=router,
                context=CallerContext(
                    current_scope=lambda: "scope",
                    current_thread_id=lambda: "thread",
                    list_files=InMemoryStore.list,
                ),
                spec=_SPEC,
                name="probe",
                egress_integrity=SourceIntegrity.TRUSTED,
            )
            == []
        )


@pytest.mark.parametrize(
    "channel,expected",
    [("host-trusted", "trusted"), ("host-untrusted", "untrusted"), ("host-missing", "untrusted")],
)
def test_trusting_egress_does_not_clear_host_tool_sources(channel, expected):
    spec = replace(
        _spec_with_other_source(channel), egress=Egress.ALLOWLIST, egress_allow=("example.com",)
    )

    def build(session):
        async def read():
            await session.read_file(
                InMemoryStore({"a": "content"}), ListedFile("a", SourceIntegrity.TRUSTED)
            )
            return "report"

        return read

    tool = _attach(build, source="untrusted", spec=spec, egress_integrity=SourceIntegrity.TRUSTED)
    tool.additional_properties["confidentiality"] = "public"
    items = asyncio.run(tool.invoke(arguments={}))
    assert items[0].additional_properties["security_label"]["integrity"] == expected


@pytest.mark.parametrize(
    "channel,expected",
    [
        ("closed", "trusted"),
        ("allowlist", "untrusted"),
        ("unrestricted", "untrusted"),
        ("host-missing", "untrusted"),
        ("host-untrusted", "untrusted"),
        ("host-trusted", "trusted"),
        ("host-pure", "trusted"),
    ],
)
@pytest.mark.parametrize("form", ["string", "items", "guidance", "contract"])
@pytest.mark.parametrize("synchronous", [False, True])
def test_no_reads_use_trusted_call_evidence_only_when_other_sources_allow_it(
    channel,
    expected,
    form,
    synchronous,
):
    answer = {
        "string": "report",
        "items": [Content.from_text("report")],
        "guidance": [Content.from_text("report"), Content.from_text(_GUIDANCE)],
        "contract": SandboxResult(completed=True, output=("report",)),
    }[form]

    def build(session):
        async def asynchronous(value: str):
            return answer

        def synchronous_body(value: str):
            return answer

        return synchronous_body if synchronous else asynchronous

    tool = _attach(
        build,
        source="untrusted",
        spec=_spec_with_other_source(channel),
        guidance=(_GUIDANCE,) if form == "guidance" else (),
        contract=form == "contract",
    )
    tool.additional_properties["confidentiality"] = "private"
    assert list(tool.parameters()["properties"]) == ["value"]
    tracker = LabelTrackingFunctionMiddleware(auto_hide_untrusted=False)
    context = FunctionInvocationContext(function=tool, arguments={"value": "argument"})

    async def call_next():
        context.result = await tool.invoke(context=context)
        report = next(item for item in context.result if item.text == "report")
        assert report.additional_properties["security_label"] == {
            "integrity": expected,
            "confidentiality": "private",
        }

    asyncio.run(tracker.process(context, call_next))
    if form in {"guidance", "contract"}:
        assert context.metadata["result_label"].integrity.value == expected
    else:
        # The framework's unraised source declaration still restricts the result.
        assert context.metadata["result_label"].integrity.value == "untrusted"


@pytest.mark.parametrize("evidence", [None, "malformed", "untrusted", "trusted"])
@pytest.mark.parametrize(
    "levels", [[], [None], [SourceIntegrity.UNTRUSTED], [SourceIntegrity.TRUSTED]]
)
def test_call_evidence_is_used_only_without_accepted_reads(evidence, levels):
    from agent_framework.security import ContentLabel, IntegrityLabel

    tool = _reading(
        levels,
        source="untrusted",
        guidance=(_GUIDANCE,),
        answer=[Content.from_text("report"), Content.from_text(_GUIDANCE)],
    )
    context = FunctionInvocationContext(function=tool, arguments={})
    if evidence in {"trusted", "untrusted"}:
        context.metadata["effective_invocation_label"] = ContentLabel(
            integrity=IntegrityLabel(evidence)
        )
    elif evidence is not None:
        context.metadata["effective_invocation_label"] = evidence
    answer = asyncio.run(tool.invoke(context=context))
    assert answer[0].additional_properties["security_label"]["integrity"] == (
        "trusted"
        if levels == [SourceIntegrity.TRUSTED] or (not levels and evidence == "trusted")
        else "untrusted"
    )


def test_existing_context_parameter_is_forwarded_and_snapshot_precedes_body():
    from agent_framework.security import ContentLabel, IntegrityLabel

    def build(session):
        async def probe(ctx: FunctionInvocationContext, value: str):
            ctx.metadata["effective_invocation_label"] = ContentLabel(
                integrity=IntegrityLabel.TRUSTED
            )
            return SandboxResult(completed=True, output=(value,))

        return probe

    tool = _attach(build, source="untrusted", contract=True)
    assert list(tool.parameters()["properties"]) == ["value"]
    context = FunctionInvocationContext(function=tool, arguments={"value": "report"})
    context.metadata["effective_invocation_label"] = ContentLabel(
        integrity=IntegrityLabel.UNTRUSTED
    )
    answer = asyncio.run(tool.invoke(context=context))
    assert answer[-1].additional_properties["security_label"]["integrity"] == "untrusted"


@pytest.mark.parametrize("existing_context", [False, True])
def test_context_injection_preserves_keyword_arguments_and_optional_context(existing_context):
    def build(session):
        async def optional(value: str, ctx: FunctionInvocationContext | None = None):
            assert ctx is not None
            return SandboxResult(completed=True, output=(value,))

        async def colliding(maf_call_context: str, **kwargs):
            assert not kwargs
            return SandboxResult(completed=True, output=(maf_call_context,))

        return optional if existing_context else colliding

    tool = _attach(build, source="untrusted", contract=True)
    parameter = "value" if existing_context else "maf_call_context"
    assert list(tool.parameters()["properties"]) == [parameter]
    result = asyncio.run(tool.invoke(arguments={parameter: "report"}))
    assert result[-1].text == "report"
    assert result[-1].additional_properties["security_label"]["integrity"] == "untrusted"


def test_concurrent_calls_keep_their_own_argument_evidence():
    from agent_framework.security import ContentLabel, IntegrityLabel

    async def run():
        both_started = asyncio.Barrier(2)

        def build(session):
            async def probe(value: str):
                await both_started.wait()
                return SandboxResult(completed=True, output=(value,))

            return probe

        tool = _attach(build, source="untrusted", contract=True)
        tracker = LabelTrackingFunctionMiddleware()
        reference = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )

        async def call(value):
            context = FunctionInvocationContext(function=tool, arguments={"value": value})

            async def call_next():
                context.result = await tool.invoke(context=context)

            await tracker.process(context, call_next)
            return context.metadata["result_label"].integrity.value

        return await asyncio.gather(call("own argument"), call(f"[{reference}]"))

    assert asyncio.run(run()) == ["trusted", "untrusted"]


@pytest.mark.parametrize("annotation", ["optional_module.Result", "("])
@pytest.mark.parametrize("synchronous", [False, True])
def test_unresolvable_return_annotations_keep_framework_attachment_fallback(
    annotation, synchronous
):
    from types import SimpleNamespace

    from agent_framework import FunctionTool

    namespace = {"optional_module": SimpleNamespace()}
    prefix = "" if synchronous else "async "
    exec(f"{prefix}def probe(value: str):\n    return value", namespace)
    body = namespace["probe"]
    body.__annotations__["return"] = annotation
    bare = FunctionTool(name="probe", func=body)
    wrapped = _attach(lambda session: body, source="untrusted")
    wrapped.additional_properties["confidentiality"] = "private"
    assert wrapped.parameters() == bare.parameters()
    result = asyncio.run(wrapped.invoke(arguments={"value": "report"}))
    assert result[0].text == "report"
    assert result[0].additional_properties["security_label"]["integrity"] == "untrusted"
