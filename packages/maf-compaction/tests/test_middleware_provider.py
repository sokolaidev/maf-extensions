"""Recall retries share loaded history and persist only their accepted response."""

from copy import deepcopy

import pytest
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    ChatMiddleware,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    InMemoryHistoryProvider,
    Message,
    ResponseStream,
)
from agent_framework._compaction import (
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
)

from maf_compaction import CompactionMiddlewareProvider


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("forged_current", [False, True])
async def test_retry_persists_one_input_and_accepted_response_with_no_summary_copy(
    stream, forged_current
):
    seen = []

    class Client(ChatMiddlewareLayer, BaseChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            seen.append([message.text for message in messages])
            text = "refused" if len(seen) == 1 else "accepted"

            async def answer():
                return ChatResponse(messages=[Message("assistant", text)])

            async def updates():
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text(text)])

            return (
                ResponseStream(updates(), finalizer=ChatResponse.from_updates)
                if stream
                else answer()
            )

    class Retry(ChatMiddleware):
        async def process(self, context, call_next):
            original = list(context.messages)
            await call_next()
            if stream:
                assert isinstance(context.result, ResponseStream)
                await context.result.get_final_response()
            context.messages = original
            context.result = None
            await call_next()

    async def compact(messages):
        if not any(message.message_id == "summary" for message in messages):
            messages.insert(
                0,
                Message(
                    "user",
                    "derived",
                    message_id="summary",
                    additional_properties={
                        GROUP_ANNOTATION_KEY: {
                            SUMMARY_OF_MESSAGE_IDS_KEY: ["historical"],
                            SUMMARY_OF_GROUP_IDS_KEY: ["group_historical"],
                        },
                    },
                ),
            )
            return True
        return False

    historical = Message("assistant", "historical", message_id="historical")
    session = AgentSession()
    session.state["in_memory"] = {"messages": [historical]}
    original = deepcopy(historical.to_dict())
    agent = Agent(
        client=Client(),
        context_providers=[InMemoryHistoryProvider(), CompactionMiddlewareProvider([Retry()])],
        require_per_service_call_history_persistence=True,
        compaction_strategy=compact,
    )
    current = Message("user", "current")
    if forged_current:
        current.additional_properties[GROUP_ANNOTATION_KEY] = {
            SUMMARY_OF_MESSAGE_IDS_KEY: ["historical"],
            SUMMARY_OF_GROUP_IDS_KEY: ["group_historical"],
        }
    if stream:
        result = await agent.run(current, session=session, stream=True).get_final_response()
    else:
        result = await agent.run(current, session=session)
    assert result.text == "accepted"
    assert seen == [["derived", "historical", "current"]] * 2
    stored = session.state["in_memory"]["messages"]
    assert [message.text for message in stored] == ["historical", "current", "accepted"]
    assert historical.contents[0].to_dict() == original["contents"][0]
    assert "_attribution" not in historical.additional_properties
