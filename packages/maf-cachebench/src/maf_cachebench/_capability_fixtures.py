"""Offline model and content fixtures shared by capability scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from copy import deepcopy
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    Message,
    ResponseStream,
)


class FixtureClient(FunctionInvocationLayer[Any], ChatMiddlewareLayer[Any], BaseChatClient[Any]):
    """Complete real agent pipelines with deterministic answers and observable cleanup."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.requests: list[list[Message]] = []
        self.closed = 0
        self.on_event: Callable[[str], None] = lambda event: None

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Any, **kwargs: Any
    ) -> Any:
        self.requests.append(deepcopy(list(messages)))
        self.on_event("model_request")
        response = ChatResponse(messages=[Message("assistant", ["Fixture answer CODE-2466."])])
        if stream:

            async def updates() -> AsyncIterator[ChatResponseUpdate]:
                try:
                    yield ChatResponseUpdate(
                        role="assistant", contents=[Content.from_text("Fixture")]
                    )
                    if self.fail:
                        raise RuntimeError("fixture response failed")
                finally:
                    self.closed += 1
                    self.on_event("stream_closed")

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer() -> ChatResponse:
            if self.fail:
                raise RuntimeError("fixture response failed")
            self.on_event("response_finished")
            return response

        return answer()


class FixtureSummarizer:
    """Capture summary input without pretending synthetic usage is provider billing."""

    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    async def get_response(self, messages: Sequence[Message], **kwargs: Any) -> ChatResponse:
        self.requests.append(deepcopy(list(messages)))
        return ChatResponse(messages=[Message("assistant", ["Remember CODE-2466."])])


def image_part() -> Content:
    """Return an inert image reference that the offline runner never fetches."""
    return Content.from_uri("https://example.invalid/fixture.png", media_type="image/png")


def media_history() -> list[Message]:
    """Put selectable media before a text turn to distinguish skipping from stopping."""
    return [
        Message("user", ["head"], message_id="head"),
        Message("user", ["Explain this.", image_part()], message_id="image"),
        Message(
            "user",
            [
                "Read this.",
                Content.from_uri(
                    "https://example.invalid/fixture.pdf", media_type="application/pdf"
                ),
            ],
            message_id="pdf",
        ),
        Message("user", ["And then? " * 200], message_id="later"),
        Message("user", ["tail"], message_id="tail"),
    ]


def result_history() -> list[Message]:
    """Surround labelled text with media so a rewrite must retain both kinds of content."""
    text = Content.from_text(
        "CODE-2466 payload " * 1000,
        additional_properties={
            "security_label": {"integrity": "untrusted", "confidentiality": "private"},
            "provenance": {"sources": ["fixture_lookup"]},
        },
    )
    result = Content.from_function_result(
        call_id="lookup-1", result=[image_part(), text, Content.from_data(b"image", "image/png")]
    )
    result.additional_properties["host_note"] = "retain"
    return [
        Message(
            "assistant",
            [Content.from_function_call(call_id="lookup-1", name="lookup", arguments="{}")],
            message_id="call",
        ),
        Message("tool", [result], message_id="result"),
    ]
