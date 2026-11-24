"""Installed 1.26 contracts used by tool results, sessions and benchmark providers."""

import errno
import inspect
import json
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from agent_framework import (
    AgentResponse,
    AgentSession,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FileSessionStore,
    FunctionTool,
    Message,
    ResponseStream,
)
from maf_cachebench import serialize_message
from pydantic import BaseModel, ConfigDict, Field


@pytest.mark.anyio
async def test_json_mode_tool_result_survives_benchmark_history():
    class Result(BaseModel):
        timestamp: datetime
        identifier: UUID

    async def result():
        return Result(timestamp=datetime(2026, 1, 1, tzinfo=UTC), identifier=UUID(int=1))

    contents = await FunctionTool(name="result", func=result).invoke(arguments={})
    assert json.loads(contents[0].text) == {
        "timestamp": "2026-01-01T00:00:00Z",
        "identifier": "00000000-0000-0000-0000-000000000001",
    }
    message = Message("tool", [Content.from_function_result(call_id="result", result=contents)])
    restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
    assert serialize_message(restored) == serialize_message(message)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("by_name", [False, True])
def test_structured_summary_accepts_schema_alias_without_changing_name_policy(stream, by_name):
    class Summary(BaseModel):
        model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
        text: str = Field(alias="summary")

    # A model must enable at least one input spelling.
    Summary.model_config = ConfigDict(validate_by_alias=not by_name, validate_by_name=by_name)
    Summary.model_rebuild(force=True)

    def response(payload):
        if stream:
            return ChatResponse.from_updates(
                [ChatResponseUpdate(role="assistant", contents=[Content.from_text(payload)])],
                output_format_type=Summary,
            )
        return ChatResponse(messages=[Message("assistant", payload)], response_format=Summary)

    assert response('{"summary":"retained"}').value.text == "retained"
    if by_name:
        assert response('{"text":"retained"}').value.text == "retained"
    else:
        with pytest.raises(ValueError):
            _ = response('{"text":"retained"}').value


@pytest.mark.anyio
@pytest.mark.parametrize("serialization_format", ["json", "msgpack"])
@pytest.mark.parametrize("failures", [1, 3])
async def test_atomic_session_replacement_preserves_last_durable_snapshot(
    tmp_path, monkeypatch, serialization_format, failures
):
    import agent_framework._sessions as sessions

    store = FileSessionStore(tmp_path, serialization_format=serialization_format)
    session = AgentSession(session_id="adoption")
    session.state["revision"] = 1
    await store.set(session.session_id, session)
    original_replace = os.replace
    attempts = []

    def replace(source, target):
        attempts.append((source, target))
        if len(attempts) <= failures:
            raise PermissionError("snapshot is held open")
        original_replace(source, target)

    monkeypatch.setattr(sessions, "_RETRY_REPLACE_ON_THIS_PLATFORM", True)
    monkeypatch.setattr(os, "replace", replace)
    session.state["revision"] = 2
    if failures == 3:
        with pytest.raises(PermissionError, match="held open"):
            await store.set(session.session_id, session)
    else:
        await store.set(session.session_id, session)
    restored = await store.get(session.session_id)
    assert restored.state["revision"] == (1 if failures == 3 else 2)
    assert len(attempts) == (3 if failures == 3 else 2)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, FileExistsError])
@pytest.mark.parametrize("parent_exists", [False, True])
def test_windows_path_classification_preserves_permission_and_collision_errors(
    tmp_path, monkeypatch, error, parent_exists
):
    from agent_framework._harness import _file_access

    monkeypatch.setattr(_file_access, "_WINDOWS_PATH_LIMITS_APPLY", True)
    parent = tmp_path if parent_exists else tmp_path / "missing"
    path = parent / ("x" * 260)
    failure = error("operation failed")
    if parent_exists and error is FileNotFoundError:
        with pytest.raises(OSError) as caught:
            _file_access._raise_if_path_too_long(failure, path, limit=260)
        assert caught.value.errno == errno.ENAMETOOLONG
        assert caught.value.__cause__ is failure
    else:
        _file_access._raise_if_path_too_long(failure, path, limit=260)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["resources", "scripts"])
async def test_harness_skills_reject_ambiguous_case_names(tmp_path, kind):
    from agent_framework import FileSkill, FileSkillScript, InlineSkillResource, SkillFrontmatter

    def entry(name):
        if kind == "resources":
            return InlineSkillResource(name=name, content="contract")
        return FileSkillScript(name=name, full_path=str(tmp_path / name))

    options = dict(
        frontmatter=SkillFrontmatter(name="adoption", description="Adoption contracts"),
        content="Use the named resource.",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="differ only by case"):
        FileSkill(**options, **{kind: [entry("Contract"), entry("contract")]})
    resource = entry("Contract")
    skill = FileSkill(**options, **{kind: [resource]})
    lookup = skill.get_resource if kind == "resources" else skill.get_script
    assert await lookup("CONTRACT") is resource


@pytest.mark.anyio
async def test_evaluation_rejects_misaligned_responses_before_scoring():
    from agent_framework import evaluate_agent

    evaluator = AsyncMock()
    with pytest.raises(ValueError, match="queries.*responses"):
        await evaluate_agent(
            queries=["one", "two"],
            responses=[AgentResponse(messages=[Message("assistant", "one")])],
            evaluators=evaluator,
        )
    evaluator.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_instrumented_client_keeps_effective_model_override(monkeypatch, stream):
    from agent_framework import observability
    from maf_cachebench._providers import ConfiguredClient

    seen = []
    attributes = []

    class Client(observability.ChatTelemetryLayer, BaseChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            seen.append((options["model"], kwargs["model"]))

            async def response():
                return ChatResponse(messages=[Message("assistant", "done")])

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("done")])

            return (
                ResponseStream(updates(), finalizer=ChatResponse.from_updates)
                if stream
                else response()
            )

    original = observability._get_span_attributes

    def capture(**kwargs):
        attributes.append(kwargs["model"])
        return original(**kwargs)

    monkeypatch.setattr(
        observability,
        "OBSERVABILITY_SETTINGS",
        observability.ObservabilitySettings(enable_instrumentation=True),
    )
    monkeypatch.setattr(observability, "_get_span_attributes", capture)
    client = ConfiguredClient(Client(), {"model": "default"})
    response = client.get_response(
        [Message("user", "probe")],
        options={"model": "option"},
        client_kwargs={"model": "transport"},
        stream=stream,
    )
    if stream:
        await response.get_final_response()
    else:
        await response
    assert seen == [("option", "transport")]
    assert attributes == ["transport"]


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [{"passed": "false"}, {"score": "unknown"}])
async def test_evaluator_refuses_invalid_verdict_instead_of_coercing_success(payload):
    from agent_framework import EvalItem, evaluator

    @evaluator
    def judge(response):
        return payload

    with pytest.raises(TypeError, match="returned dict"):
        result = judge(EvalItem([Message("user", "probe"), Message("assistant", "answer")]))
        assert inspect.isawaitable(result)
        await result


@pytest.mark.anyio
@pytest.mark.parametrize("endpoint", ["https://api.mistral.ai", "https://mistral.example.test"])
async def test_injected_mistral_endpoint_and_transport_stay_owned_by_caller(endpoint):
    import httpx
    from agent_framework_mistral import MistralChatClient
    from mistralai.client import Mistral

    targets = []

    def respond(request):
        targets.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "probe",
                "object": "chat.completion",
                "created": 1,
                "model": "probe",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        sdk = Mistral(api_key="test", server_url=endpoint, async_client=transport)
        client = MistralChatClient(model="probe", client=sdk, server_url="https://ignored.test")
        assert client.service_url() == endpoint
        assert (await client.get_response("probe")).text == "done"
        await client.close()
        assert not transport.is_closed
        assert (await client.get_response("still owned")).text == "done"
        assert targets == [f"{endpoint}/v1/chat/completions"] * 2
