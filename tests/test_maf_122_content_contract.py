"""MAF 1.22 content must retain suite semantics through streaming and provider replay."""

import json

import pytest
from agent_framework import (
    CharacterEstimatorTokenizer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
)
from agent_framework._compaction import _serialize_content
from agent_framework_mistral import MistralChatClient
from agent_framework_ollama import OllamaChatClient
from agent_framework_openai import OpenAIChatClient, OpenAIChatCompletionClient
from maf_cachebench import serialize_message
from maf_compaction import AnchoredCompactionStrategy


def test_streamed_snapshots_finalize_before_cache_identity_and_persistence():
    updates = [
        ChatResponseUpdate(
            role="assistant",
            message_id="m1",
            contents=[Content.from_text("abc", merge_mode="append")],
        ),
        ChatResponseUpdate(
            role="assistant",
            message_id="m1",
            contents=[Content.from_text("abc", merge_mode="append")],
        ),
        ChatResponseUpdate(
            role="assistant",
            message_id="m1",
            contents=[Content.from_text("abcabc!", merge_mode="replace")],
        ),
        ChatResponseUpdate(
            role="assistant",
            message_id="m2",
            contents=[Content.from_text("next", merge_mode="append")],
        ),
    ]
    response = ChatResponse.from_updates(updates)
    assert [m.text for m in response.messages] == ["abcabc!", "next"]
    assert [m.message_id for m in response.messages] == ["m1", "m2"]
    for message in response.messages:
        assert all(c.merge_mode is None for c in message.contents)
        restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
        assert serialize_message(message) == serialize_message(restored)
        assert "merge_mode" not in serialize_message(restored)


def test_namespaced_replay_metadata_keeps_provider_identity_separate_from_occurrence():
    call = Content.from_function_call(
        call_id="service-call",
        id="occurrence",
        name="lookup",
        arguments="{}",
        additional_properties={"openai": {"item_id": "fc_provider"}},
    )
    result = Content.from_function_result(call_id="service-call", id="occurrence", result="answer")
    history = [Message("assistant", [call]), Message("tool", [result])]
    restored = [Message.from_dict(json.loads(json.dumps(m.to_dict()))) for m in history]
    client = OpenAIChatClient(model="test", api_key="unused")
    wire = client._prepare_messages_for_openai(restored, request_uses_service_side_storage=False)
    function = next(item for item in wire if item.get("type") == "function_call")
    assert function["id"] == "fc_provider"
    assert function["call_id"] == "service-call"
    assert restored[0].contents[0].id == restored[1].contents[0].id == "occurrence"
    assert all(c.raw_representation is None for m in restored for c in m.contents)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["responses", "chat", "mistral", "ollama"])
async def test_compacted_occurrence_reaches_provider_after_reload(provider):
    result = Content.from_function_result(
        call_id="service-call",
        id="occurrence",
        result="head " + "payload " * 2000 + " tail",
        additional_properties={
            "openai": {"item_id": "fc-provider"},
            "application": {"retain": True},
        },
    )
    messages = [
        Message(
            "assistant",
            [
                Content.from_function_call(
                    call_id="service-call", id="occurrence", name="lookup", arguments="{}"
                )
            ],
        ),
        Message("tool", [result]),
    ]
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=1000,
        tokenizer=CharacterEstimatorTokenizer(),
        keep_head_groups=0,
        keep_tail_groups=0,
        keep_tokens=100,
    )
    assert await strategy(messages)
    restored = [Message.from_dict(json.loads(json.dumps(m.to_dict()))) for m in messages]
    output = restored[1].contents[0]
    assert output.id == "occurrence"
    assert output.additional_properties == result.additional_properties
    assert output.items is not None
    texts = []
    for item in output.items:
        assert isinstance(item.text, str)
        texts.append(item.text)
    assert output.result == "\n".join(texts)
    if provider in {"responses", "chat"}:
        client_type = OpenAIChatClient if provider == "responses" else OpenAIChatCompletionClient
        wire = client_type(model="test", api_key="unused")._prepare_messages_for_openai(restored)
        key = "output" if provider == "responses" else "content"
        assert wire[-1][key] == output.result
    elif provider == "mistral":
        wire = MistralChatClient(model="test", api_key="unused")._prepare_mistral_messages(restored)
        assert wire[-1]["content"] == output.result
    else:
        wire = OllamaChatClient(model="test")._prepare_messages_for_ollama(restored)
        assert wire[-1].content == output.result
        assert wire[-1].tool_name == "lookup"


def test_nested_opaque_reasoning_is_not_counted_but_remains_replayable():
    content = Content.from_text_reasoning(
        text="visible reasoning",
        additional_properties={
            "foundry": {
                "reasoning_item": {
                    "encrypted_content": "opaque" * 1000,
                    "summary": "visible summary",
                }
            }
        },
    )
    estimated = json.dumps(_serialize_content(content))
    assert "opaque" not in estimated
    assert "visible reasoning" in estimated and "visible summary" in estimated
    restored = Content.from_dict(json.loads(json.dumps(content.to_dict())))
    assert (
        restored.additional_properties["foundry"]["reasoning_item"]["encrypted_content"]
        == "opaque" * 1000
    )


def test_old_history_without_hosted_fields_remains_readable():
    restored = Message.from_dict(
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "call_id": "legacy",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
        }
    )
    assert restored.contents[0].hosted is False
    assert restored.contents[0].id is None
    assert restored.to_dict()["contents"][0]["hosted"] is False
