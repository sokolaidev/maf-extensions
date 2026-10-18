"""MAF adoption preserves call authority and exactly-once committed guidance."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from agent_framework import Content, FunctionInvocationContext, FunctionTool, security
from agent_framework.security import (
    ConfidentialityLabel,
    ContentLabel,
    IntegrityLabel,
    LabelTrackingFunctionMiddleware,
)

from maf_sandbox import CallerContext, Isolation, SandboxRouter, SandboxSpec
from maf_sandbox.maf import (
    SandboxResult,
    argument_provenance_middleware,
    positions_holding_hidden_content,
    sandbox_label_tracking_middleware,
    sandboxed_tool,
)
from maf_sandbox.testing import InMemoryStore, InProcessSandboxBackend


def attach(body, *, guidance=("Fixed guidance.",), contract=False):
    return sandboxed_tool(
        lambda session: body,
        router=SandboxRouter([InProcessSandboxBackend()], min_isolation=Isolation.NONE),
        context=CallerContext(
            current_scope=lambda: "scope",
            current_thread_id=lambda: "thread",
            list_files=InMemoryStore.list,
        ),
        agent_id="agent",
        spec=SandboxSpec(kind="probe"),
        name="probe",
        source_integrity="untrusted",
        standing_guidance=guidance,
        result_contract=contract,
    )[0]


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("outer", [False, True])
def test_real_rewrites_are_positional_and_call_scoped(sync, outer, monkeypatch):
    from maf_sandbox import maf

    observed = []
    accessor = security.rewritten_arguments

    def public(context):
        result = accessor(context)
        observed.append(result)
        return result

    monkeypatch.setattr(security, "rewritten_arguments", public)
    monkeypatch.setattr(maf, "_reachable_middleware", lambda: None)

    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        ref = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        finished = asyncio.Event()
        children = []
        seen = []

        async def late():
            await finished.wait()
            return positions_holding_hidden_content(
                ["hidden", "hidden"], argument="files", candidates=frozenset()
            )

        def read(files):
            return positions_holding_hidden_content(files, argument="files")

        async def body(files: list[str]):
            seen.append(await asyncio.to_thread(read, files) if sync else read(files))
            children.append(asyncio.create_task(late()))
            return "ok"

        tool = FunctionTool(name="probe", func=body)
        context = FunctionInvocationContext(
            function=tool, arguments={"files": ["hidden", f"[{ref}]"]}
        )

        async def invoke():
            context.result = await context.function.invoke(arguments=context.arguments)

        async def inner():
            await (tracker if outer else observer).process(context, invoke)

        await (observer if outer else tracker).process(context, inner)
        finished.set()
        assert seen == [frozenset({1})]
        assert observed and observed[-1]["files"] == {1}
        assert await asyncio.gather(*children) == [frozenset()]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "positions,values,expected",
    [
        ({1}, ["a", "b"], {1}),
        ({-1}, ["a"], {0}),
        ({-1}, ["a", "b"], {0, 1}),
        ({7}, ["a", "b"], {0, 1}),
        ({True}, ["a", "b"], {0, 1}),
    ],
)
def test_public_map_translates_and_validates_positions(monkeypatch, positions, values, expected):
    called = []

    def accessor(context):
        called.append(context)
        return {"files": positions}

    monkeypatch.setattr(security, "rewritten_arguments", accessor, raising=False)
    context = SimpleNamespace(arguments={"files": values}, metadata={})

    async def body():
        assert positions_holding_hidden_content(values, argument="files") == frozenset(expected)

    asyncio.run(argument_provenance_middleware().process(context, body))
    assert called == [context]


def test_empty_public_map_cannot_hide_missing_tracking(monkeypatch):
    from maf_sandbox import maf

    monkeypatch.setattr(security, "rewritten_arguments", lambda context: {}, raising=False)
    monkeypatch.setattr(maf, "_reachable_middleware", object)
    context = SimpleNamespace(arguments={"files": ["a", "b"]}, metadata={})

    async def body():
        assert positions_holding_hidden_content(["a", "b"], argument="files") == {0, 1}

    asyncio.run(argument_provenance_middleware().process(context, body))


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("contract", [False, True])
@pytest.mark.parametrize("tracking", [False, True])
def test_fixed_guidance_exactly_once_with_and_without_native_tracking(sync, contract, tracking):
    def result():
        return (
            SandboxResult(completed=True, output=("diagnostic",))
            if contract
            else [
                Content.from_text("diagnostic"),
                Content.from_text("Fixed guidance."),
            ]
        )

    def body():
        return result()

    async def async_body():
        return result()

    tool = attach(body if sync else async_body, contract=contract)
    tracker = sandbox_label_tracking_middleware()
    native = "standing_guidance" in inspect.signature(tracker._label_result).parameters
    assert native, "The admitted MAF minor must exercise native standing guidance"
    # A later declaration edit cannot replace the attachment commitment.
    if native and tracking:
        tool.additional_properties["standing_guidance"] = ["Changed later."]

    async def scenario():
        context = FunctionInvocationContext(function=tool, arguments={})

        async def invoke():
            context.result = await context.function.invoke(arguments=context.arguments)

        if tracking:
            await tracker.process(context, invoke)
        else:
            await invoke()
        assert context.function is tool
        texts = [item.text for item in context.result]
        assert texts.count("Fixed guidance.") == 1
        assert "Changed later." not in texts
        if tracking:
            assert (
                context.result[-1].additional_properties["security_label"]["integrity"] == "trusted"
            )

    asyncio.run(scenario())


def test_native_tracker_restores_function_after_cancellation():
    async def body():
        raise asyncio.CancelledError

    tool = attach(body)
    context = FunctionInvocationContext(function=tool, arguments={})

    async def invoke():
        context.result = await context.function.invoke(arguments=context.arguments)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sandbox_label_tracking_middleware().process(context, invoke))
    assert context.function is tool


def test_fixed_guidance_preserves_confidentiality_and_principals():
    async def body():
        return [Content.from_text("diagnostic"), Content.from_text("Fixed guidance.")]

    tool = attach(body)
    principals = [{"tenant_id": "tenant-a", "user_id": "user-a"}]
    tool.additional_properties.update(
        {
            "confidentiality": "user_identity",
            "agent_framework.security.principals": principals,
        }
    )
    tracker = sandbox_label_tracking_middleware()

    async def scenario():
        context = FunctionInvocationContext(function=tool, arguments={})

        async def invoke():
            context.result = await context.function.invoke(arguments=context.arguments)

        await tracker.process(context, invoke)
        guidance = context.result[-1]
        assert guidance.text == "Fixed guidance."
        label = guidance.additional_properties["security_label"]
        assert label["confidentiality"] == "user_identity"
        assert label["metadata"]["agent_framework.security.principals"] == principals

    asyncio.run(scenario())


def test_concurrent_native_calls_do_not_change_attached_tool():
    async def body(value: str):
        await asyncio.sleep(0)
        return [Content.from_text(value), Content.from_text("Fixed guidance.")]

    tool = attach(body)
    tracker = sandbox_label_tracking_middleware()
    properties = dict(tool.additional_properties)

    async def one(value):
        context = FunctionInvocationContext(function=tool, arguments={"value": value})

        async def invoke():
            context.result = await context.function.invoke(arguments=context.arguments)

        await tracker.process(context, invoke)
        assert context.function is tool
        assert sum(item.text == "Fixed guidance." for item in context.result) == 1

    async def scenario():
        await asyncio.gather(one("a"), one("b"))

    asyncio.run(scenario())
    assert tool.additional_properties == properties


def test_call_routes_keep_local_rendering_and_reject_extra_native_guidance():
    async def body():
        return SandboxResult(completed=True)

    tool = attach(body, guidance=("Artifacts: {call_id}",), contract=True)
    tracker = sandbox_label_tracking_middleware()
    if "standing_guidance" in inspect.signature(tracker._label_result).parameters:
        tool.additional_properties["standing_guidance"] = ["extra"]

    async def scenario():
        context = FunctionInvocationContext(function=tool, arguments={})

        async def invoke():
            context.result = await context.function.invoke(arguments=context.arguments)

        await tracker.process(context, invoke)
        texts = [item.text for item in context.result]
        assert len([text for text in texts if text and text.startswith("Artifacts: ")]) == 1
        assert "extra" not in texts
        assert not any(text and "{call_id}" in text for text in texts)

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["reverse", "filter", "preserve"])
def test_validated_arguments_do_not_reuse_stale_positions(mutation):
    from typing import Annotated

    from pydantic import AfterValidator

    def transform(values):
        if mutation == "reverse":
            return list(reversed(values))
        if mutation == "filter":
            return [value for value in values if value != "remove"]
        return values

    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        ref = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        seen = []

        async def body(files: Annotated[list[str], AfterValidator(transform)]):
            seen.append((files, positions_holding_hidden_content(files, argument="files")))
            return "ok"

        tool = FunctionTool(name="validated", func=body)
        context = FunctionInvocationContext(
            function=tool, arguments={"files": [f"[{ref}]", "remove", "literal"]}
        )

        async def invoke():
            context.result = await tool.invoke(arguments=context.arguments)

        async def inner():
            await observer.process(context, invoke)

        await tracker.process(context, inner)
        values, positions = seen[0]
        assert values.index("hidden") in positions
        if callable(getattr(security, "rewritten_arguments", None)) and mutation != "preserve":
            assert positions == frozenset(range(len(values)))

    asyncio.run(scenario())


def test_concurrent_rewrites_keep_separate_call_contexts():
    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        refs = [
            tracker.get_variable_store().store(
                value, ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
            )
            for value in ("first", "second")
        ]

        answers = {}

        async def body(files: list[str]):
            await asyncio.sleep(0)
            answers[tuple(files)] = positions_holding_hidden_content(files, argument="files")
            return "ok"

        tool = FunctionTool(name="concurrent", func=body)

        async def one(files):
            context = FunctionInvocationContext(function=tool, arguments={"files": files})
            seen = []

            async def invoke():
                seen.append(await tool.invoke(arguments=context.arguments))

            async def inner():
                await observer.process(context, invoke)

            await tracker.process(context, inner)
            return seen[0]

        await asyncio.gather(one([f"[{refs[0]}]", "literal"]), one(["literal", f"[{refs[1]}]"]))
        assert answers == {
            ("first", "literal"): frozenset({0}),
            ("literal", "second"): frozenset({1}),
        }

    asyncio.run(scenario())


def test_schema_alias_cannot_strand_hidden_provenance_under_the_old_name():
    from pydantic import BaseModel, Field

    class Arguments(BaseModel):
        files: list[str] = Field(alias="fileNames")

    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        ref = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        seen = []

        async def body(files: list[str]):
            seen.append(positions_holding_hidden_content(files, argument="files"))
            return "ok"

        tool = FunctionTool(name="aliased", func=body, input_model=Arguments)
        context = FunctionInvocationContext(
            function=tool, arguments={"fileNames": [f"[{ref}]", "literal"]}
        )

        async def invoke():
            context.arguments = Arguments.model_validate(context.arguments).model_dump()
            context.result = await body(**context.arguments)

        async def inner():
            await observer.process(context, invoke)

        await tracker.process(context, inner)
        assert seen and 0 in seen[0]

    asyncio.run(scenario())


def test_guided_call_preserves_policy_approval_request_and_resumes():
    calls = []

    async def body():
        calls.append("executed")
        return [Content.from_text("diagnostic"), Content.from_text("Fixed guidance.")]

    tool = attach(body)
    tracker = sandbox_label_tracking_middleware()
    request = Content(type="function_approval_request")

    async def scenario():
        context = FunctionInvocationContext(function=tool, arguments={})

        async def suspend():
            context.result = request

        await tracker.process(context, suspend)
        assert context.result is request
        assert context.function is tool
        assert calls == []

        async def resume():
            context.result = await context.function.invoke(arguments=context.arguments)

        await tracker.process(context, resume)
        assert calls == ["executed"]
        assert context.function is tool
        assert sum(item.text == "Fixed guidance." for item in context.result) == 1

    asyncio.run(scenario())


def test_tracker_forwards_positional_and_keyword_constructor_options():
    tracker = sandbox_label_tracking_middleware(
        IntegrityLabel.TRUSTED,
        ConfidentialityLabel.PUBLIC,
        False,
        IntegrityLabel.UNTRUSTED,
        session_state_key="custom_security_state",
    )

    async def body():
        return "visible result"

    tool = FunctionTool(name="plain", func=body)

    async def scenario():
        context = FunctionInvocationContext(function=tool, arguments={})

        async def invoke():
            context.result = await tool.invoke(arguments=context.arguments)

        await tracker.process(context, invoke)
        assert context.result[0].text == "visible result"
        assert context.result[0].additional_properties["security_label"]["integrity"] == "trusted"

    asyncio.run(scenario())


@pytest.mark.parametrize("rewrites,expected", [({"files": {1}}, {1}), ({"other": {0}}, set())])
def test_adapter_does_not_read_private_metadata(monkeypatch, rewrites, expected):
    class Context:
        arguments = {"files": ["literal", "hidden"]}

        @property
        def metadata(self):
            raise AssertionError("Only the framework accessor may inspect its metadata")

    monkeypatch.setattr(security, "rewritten_arguments", lambda context: rewrites)

    async def body():
        assert positions_holding_hidden_content(["literal", "hidden"], argument="files") == expected

    asyncio.run(argument_provenance_middleware().process(Context(), body))


@pytest.mark.parametrize("result", [None, [], {"files": [0]}, {"files": {-2}}])
def test_invalid_public_provenance_withholds_all_values(monkeypatch, result):
    monkeypatch.setattr(security, "rewritten_arguments", lambda context: result)
    context = SimpleNamespace(arguments={"files": ["a", "b"]})

    async def body():
        assert positions_holding_hidden_content(["a", "b"], argument="files") == {0, 1}

    asyncio.run(argument_provenance_middleware().process(context, body))


@pytest.mark.parametrize("completion", ["return", "error", "cancel"])
def test_completed_call_cannot_supply_provenance_to_inherited_children(completion):
    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        ref = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        release = asyncio.Event()
        children = []

        async def child():
            await release.wait()
            return positions_holding_hidden_content(["literal", "hidden"], argument="files")

        async def body(files: list[str]):
            assert positions_holding_hidden_content(files, argument="files") == {1}
            children.append(asyncio.create_task(child()))
            if completion == "error":
                raise RuntimeError("tool failed")
            if completion == "cancel":
                raise asyncio.CancelledError
            return "ok"

        tool = FunctionTool(name="lifetime", func=body)
        context = FunctionInvocationContext(
            function=tool, arguments={"files": ["literal", f"[{ref}]"]}
        )

        async def invoke():
            await tool.invoke(arguments=context.arguments)

        async def inner():
            await observer.process(context, invoke)

        try:
            await tracker.process(context, inner)
        except (RuntimeError, asyncio.CancelledError):
            assert completion != "return"
        finally:
            release.set()
        assert children
        assert await asyncio.gather(*children) == [frozenset({0, 1})]
        assert positions_holding_hidden_content(["literal", "hidden"], argument="files") == set()

    asyncio.run(scenario())


def test_nested_calls_restore_the_outer_public_map():
    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        ref = tracker.get_variable_store().store(
            "hidden", ContentLabel(integrity=IntegrityLabel.UNTRUSTED)
        )
        seen = []

        async def body(files: list[str]):
            seen.append(positions_holding_hidden_content(files, argument="files"))
            if files[0] == "hidden":
                await run(["literal", f"[{ref}]"])
                seen.append(positions_holding_hidden_content(files, argument="files"))
            return "ok"

        tool = FunctionTool(name="nested", func=body)

        async def run(files):
            context = FunctionInvocationContext(function=tool, arguments={"files": files})

            async def invoke():
                await tool.invoke(arguments=context.arguments)

            async def inner():
                await observer.process(context, invoke)

            await tracker.process(context, inner)

        await run([f"[{ref}]", "literal"])
        assert seen == [{0}, {1}, {0}]

    asyncio.run(scenario())


@pytest.mark.parametrize("in_kwargs", [False, True])
@pytest.mark.parametrize("producer", ["ordinary_source", "quarantined_llm"])
def test_public_provenance_covers_payload_expansion_without_store_scans(
    in_kwargs, producer, monkeypatch
):
    async def scenario():
        tracker = LabelTrackingFunctionMiddleware()
        observer = argument_provenance_middleware()
        store = tracker.get_variable_store()
        payload = '{"response": "hidden"}'
        source = FunctionTool(name=producer, func=lambda: payload)
        source_context = FunctionInvocationContext(function=source, arguments={})

        async def produce():
            source_context.result = [Content.from_text(payload)]

        await tracker.process(source_context, produce)
        (ref,) = store.list_variables()
        seen = []

        def no_store_access():
            raise AssertionError("Refusal provenance must not inspect stored payloads")

        async def body(files: list[str]):
            with monkeypatch.context() as patch:
                patch.setattr(tracker, "get_variable_store", no_store_access)
                seen.append(positions_holding_hidden_content(files, argument="files"))
                assert positions_holding_hidden_content(files) == {0, 1}
            return "ok"

        tool = FunctionTool(name="payload", func=body)
        values = {"files": ["literal", f"prefix-[{ref}].txt"]}
        context = FunctionInvocationContext(
            function=tool,
            arguments={} if in_kwargs else values,
            kwargs=values if in_kwargs else {},
        )

        async def invoke():
            await tool.invoke(arguments=context.arguments or None, **context.kwargs)

        async def inner():
            await observer.process(context, invoke)

        await tracker.process(context, inner)
        assert seen == [{1}]

    asyncio.run(scenario())
