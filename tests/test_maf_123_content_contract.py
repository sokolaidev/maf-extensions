"""MAF 1.23 nested content and stream closure preserve suite persistence semantics."""

import json

import pytest
from agent_framework import ChatResponseUpdate, Content, Message, ResponseStream
from maf_cachebench import serialize_message


@pytest.mark.parametrize("nested_field", ["outputs", "output"])
def test_nested_content_reload_preserves_cache_identity_and_typed_outputs(nested_field):
    nested = Content.from_text("retained result")
    content = (
        Content("image_generation_tool_result", outputs=nested)
        if nested_field == "outputs"
        else Content(
            "mcp_server_tool_result", output=[nested, {"type": "text", "application": True}]
        )
    )
    message = Message("assistant", [content])
    restored = Message.from_dict(json.loads(json.dumps(message.to_dict())))
    value = getattr(restored.contents[0], nested_field)
    if nested_field == "output":
        assert value[1] == {"type": "text", "application": True}
        value = value[0]
    assert isinstance(value, Content) and value.text == "retained result"
    assert restored.to_dict() == message.to_dict()
    assert serialize_message(restored) == serialize_message(message)


@pytest.mark.anyio
async def test_abandoned_nested_stream_closes_source_and_exposes_failure_to_persistence_hooks():
    closed = []
    observed = []

    async def updates():
        try:
            yield ChatResponseUpdate(contents=[Content.from_text("partial")])
            yield ChatResponseUpdate(contents=[Content.from_text("unread")])
        finally:
            closed.append(True)

    inner = ResponseStream(updates(), finalizer=lambda updates: tuple(updates))
    inner.with_cleanup_hook(lambda: observed.append(inner._stream_error))
    outer = ResponseStream(inner, finalizer=lambda updates: tuple(updates))
    await anext(outer.__aiter__())
    failure = RuntimeError("host rejected the response")
    await outer.close(failure)
    await outer.close()
    assert closed == [True]
    assert observed == [failure]
