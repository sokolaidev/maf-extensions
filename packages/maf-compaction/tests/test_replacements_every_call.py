"""User summaries, folds and removal notes reach every later model call of the run.

An agent's tool loop hands each model call the same message objects for the whole run, whether
the host passed the transcript in as the run's input or a history provider loaded it once. A flag
set on one of them outlives the call that set it, while a message the call inserted lives in that
call's list until the framework copies it back into the run's transcript by its summary links.
A refused pinned call is retried from the list it started with, a per-call history store keeps
the run's input messages without what was inserted beside them, and the framework's tool loop
releases a superseded summary while it keeps the one that superseded it. These tests read what
the client is sent on each call, and what the store keeps.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import pytest
from agent_framework import (
    Agent,
    AgentSession,
    BaseChatClient,
    CharacterEstimatorTokenizer,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    InMemoryHistoryProvider,
    Message,
    ResponseStream,
    create_harness_agent,
    tool,
)
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    annotate_message_groups,
    annotate_token_counts,
)

from maf_compaction import (
    CompactionStack,
    RecallGate,
    SessionCompactionProvider,
    SessionCompactionRegistry,
    SessionToolResultRecallMiddleware,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    make_session_recall_tool,
)
from maf_compaction._anchored import _ANSWERED_NOTE, _NOTES  # pyright: ignore[reportPrivateUsage]
from maf_compaction._toolsummary import RECORD_MARKER
from maf_compaction._usersummary import (
    SUMMARY_MODE_BOUNDARY,
    SUMMARY_MODE_RECOMPACT,
    USER_SUMMARY_MARKER,
)

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()

_CODE = re.compile(r"CODE-\d{4}")

#: What the summarizer answers, for user turns and user summaries alike.
_USER_ACCOUNT: Final[str] = "The user asked for a short list of things."

#: Lookups the model makes before it answers, each one a further model call of the run.
_LOOKUPS: Final[int] = 3

#: The size of a lookup's result, small enough that the lookups leave the summaries be.
_LOOKUP_CHARS: Final[int] = 40

#: The size of a lookup's result where a scenario pins a call that compacts again.
_RETRY_LOOKUP_CHARS: Final[int] = 200

#: Model calls after the one that first sends a replacement that must send it too.
_LATER_CALLS: Final[int] = 2

#: What every note the anchored fallback leaves in place of a shed group opens with.
_NOTE: Final[str] = os.path.commonprefix([*_NOTES.values(), _ANSWERED_NOTE])

#: How the earlier conversation reaches the run: as the run's input, or loaded once for it.
Shape = Literal["carried", "loaded"]
_SHAPES: Final[tuple[Shape, ...]] = ("carried", "loaded")


def _summary(serial: int) -> Message:
    """Return a user summary as the boundary mode leaves one standing, links included."""
    return UserTurnAnchoredSummarizationCompactionStrategy._summary_message(  # pyright: ignore[reportPrivateUsage]
        [Message("user", ["An earlier turn."], message_id=f"source_{serial}")],
        [{"group_id": f"source_group_{serial}"}],
        f"Summary {serial}: " + "the user asked for things. " * 30,
        summary_id=f"user_summary_{serial}",
    )


def _summaries() -> list[Message]:
    """Return two standing summaries, which a fold collapses."""
    return [
        Message("user", ["Turn 0: the task."], message_id="u0"),
        Message("assistant", ["Reply 0."], message_id="a0"),
        _summary(0),
        Message("assistant", ["Reply 1."], message_id="a1"),
        _summary(1),
        Message("assistant", ["Reply 2."], message_id="a2"),
        Message("user", ["Turn 3: go on."], message_id="u3"),
    ]


def _turns(*, after_summary: bool = False) -> list[Message]:
    """Return user turns to summarize, behind a standing summary a recompaction rereads."""
    conversation = [
        Message("user", ["Turn 0: the task."], message_id="u0"),
        Message("assistant", ["Reply 0."], message_id="a0"),
    ]
    if after_summary:
        conversation.append(_summary(0))
    for index in range(1, 7):
        conversation += [
            Message("assistant", [f"Reply {index}."], message_id=f"a{index}"),
            Message("user", [f"Turn {index}: " + "t" * 600], message_id=f"u{index}"),
        ]
    conversation += [
        Message("assistant", ["Reply 7."], message_id="a7"),
        Message("user", ["Turn 7: go on."], message_id="u7"),
    ]
    return conversation


def _lookups_done(count: int, chars: int) -> list[Message]:
    """Return a conversation whose tool groups no record covers, which the overflow sheds."""
    conversation = [Message("user", ["Look everything up. " + "u" * 200], message_id="task")]
    for index in range(count):
        conversation += [
            Message(
                "assistant",
                [
                    Content.from_function_call(
                        call_id=f"old{index}", name="lookup", arguments={"index": 100 + index}
                    )
                ],
                message_id=f"old{index}_call",
            ),
            Message(
                "tool",
                [
                    Content.from_function_result(
                        call_id=f"old{index}", result=f"CODE-{100 + index:04d} " + "z" * chars
                    )
                ],
                message_id=f"old{index}_res",
            ),
        ]
    conversation.append(Message("user", ["Now compare them."], message_id="last"))
    return conversation


class _Summarizer:
    """Answer every request with one short line, counting the requests."""

    def __init__(self) -> None:
        self.requests = 0

    async def get_response(self, messages: list[Message], **kwargs: Any) -> ChatResponse:
        self.requests += 1
        return ChatResponse(messages=[Message("assistant", [_USER_ACCOUNT])])


@dataclass(frozen=True)
class _Stack:
    """What a scenario configures on the composed stack."""

    budget: int
    summary_mode: str = SUMMARY_MODE_BOUNDARY


def _registry(config: _Stack, summarizer: _Summarizer) -> SessionCompactionRegistry:
    def build() -> CompactionStack:
        records = ToolResultAnchoredSummarizationCompactionStrategy(
            max_input_tokens=config.budget, tokenizer=TOKENIZER, keep_head_groups=1
        )
        users = UserTurnAnchoredSummarizationCompactionStrategy(
            max_input_tokens=config.budget,
            tokenizer=TOKENIZER,
            client=summarizer,
            max_summary_input_tokens=100_000,
            summary_mode=config.summary_mode,
            remembered_requests=2,
        )
        strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=TOKENIZER, tool_results=records, user_turns=users, harder_attempts=0
        )
        gate = RecallGate()
        middleware = ToolResultRecallMiddleware(
            max_input_tokens=config.budget,
            tokenizer=TOKENIZER,
            arm=gate.arm,
            disarm=gate.disarm,
            reforce=records.take_reforce,
            repeat_records=True,
            min_groups_before_repeat=2,
        )
        return CompactionStack(strategy, gate, middleware)

    return SessionCompactionRegistry(build)


def _lookup_tool(chars: int) -> Any:
    @tool
    def lookup(index: int) -> str:
        """Look one thing up."""
        return f"CODE-{index:04d} " + "y" * chars

    return lookup


class _Model(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Make ``lookups`` lookups, record them when pinned or refuse the pin, then answer.

    Keeps what each call was sent, the role and a rendering of every message, whether it was
    pinned to the recall tool, and how many summarizer requests had been made by then.
    """

    def __init__(self, lookups: int, summarizer: _Summarizer, *, refuse_pins: bool = False) -> None:
        super().__init__()
        self.lookups = lookups
        self.summarizer = summarizer
        self.refuse_pins = refuse_pins
        self.looked = 0
        self.sent: list[list[tuple[str, str]]] = []
        self.pinned: list[bool] = []
        self.asked: list[int] = []

    def _inner_get_response(
        self, *, messages: list[Message], stream: bool, options: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.sent.append([(str(message.role), _render(message)) for message in messages])
        call = len(self.sent)
        choice = options.get("tool_choice")
        required = choice.get("required_function_name") if isinstance(choice, dict) else None
        self.pinned.append(bool(required))
        self.asked.append(self.summarizer.requests)
        if required and self.refuse_pins:
            contents = [
                Content.from_text(
                    "No.", additional_properties={"core": {"model_output_kind": "refusal"}}
                )
            ]
        elif required:
            seen = sorted(set(_CODE.findall(" ".join(text for _, text in self.sent[-1]))))
            contents = [
                Content.from_function_call(
                    call_id=f"recall{call}",
                    name=required,
                    arguments={"values": "lookups returned " + " ".join(seen)},
                )
            ]
        elif self.looked < self.lookups:
            self.looked += 1
            contents = [
                Content.from_function_call(
                    call_id=f"look{call}", name="lookup", arguments={"index": self.looked}
                )
            ]
        else:
            contents = [Content.from_text("answer")]
        response = ChatResponse(messages=Message("assistant", contents, message_id=f"out{call}"))
        if stream:

            async def updates() -> AsyncIterator[ChatResponseUpdate]:
                await asyncio.sleep(0)
                yield ChatResponseUpdate(
                    role="assistant", contents=contents, message_id=f"out{call}"
                )

            return ResponseStream(updates(), finalizer=lambda _: response)

        async def answer() -> ChatResponse:
            await asyncio.sleep(0)
            return response

        return answer()


def _render(message: Message) -> str:
    parts: list[str] = []
    for content in message.contents:
        if content.type == "function_call":
            parts.append(f"call:{content.name}:{content.call_id}")
        elif content.type == "function_result":
            parts.append(f"result:{content.call_id}:{content.result}")
        elif content.type == "text":
            parts.append(content.text or "")
    return "\n".join(parts)


class _HostHistory(InMemoryHistoryProvider):
    """A host that rebuilds the conversation itself and passes it in as the run's input."""

    async def before_run(self, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class _Run:
    """What each model call of a run was sent, which were pinned, and summarizer requests.

    ``requests`` counts the requests made by the run's last model call, so the store's own pass
    after the turn, over a list that need not end where the run's input did, is not in it.
    """

    sent: list[list[tuple[str, str]]]
    pinned: list[bool]
    requests: int


async def _run(
    shape: Shape,
    conversation: list[Message],
    config: _Stack,
    *,
    lookup_chars: int = _LOOKUP_CHARS,
    refuse_pins: bool = False,
) -> _Run:
    """Run one turn over ``conversation``."""
    summarizer = _Summarizer()
    registry = _registry(config, summarizer)
    model = _Model(_LOOKUPS, summarizer, refuse_pins=refuse_pins)
    history = _HostHistory() if shape == "carried" else InMemoryHistoryProvider()
    agent = Agent(
        client=model,
        name="replacements",
        context_providers=[
            history,
            SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False),
        ],
        middleware=[SessionToolResultRecallMiddleware(registry, compact_before=True)],
        tools=[make_session_recall_tool(registry), _lookup_tool(lookup_chars)],
    )
    session = AgentSession()
    if shape == "carried":
        run_input = conversation
    else:
        session.state[history.source_id] = {"messages": conversation[:-1]}
        run_input = conversation[-1:]
    await agent.run(run_input, session=session)
    return _Run(model.sent, model.pinned, model.asked[-1])


def _stand_ins(sent: list[tuple[str, str]]) -> list[str]:
    """Return the written records, user summaries and removal notes a call was sent, in order."""
    return [
        text
        for role, text in sent
        if (role == "assistant" and text.startswith((RECORD_MARKER, _NOTE)))
        or (role == "user" and text.startswith(USER_SUMMARY_MARKER))
    ]


def _is_superseded(text: str) -> bool:
    return text.startswith(USER_SUMMARY_MARKER) and "Summary " in text


def _orphaned(messages: list[Message]) -> list[str]:
    """Return the user turns excluded for a summary ``messages`` does not hold."""
    held = {message.message_id for message in messages}
    return [
        str(message.message_id)
        for message in messages
        if message.role == "user"
        and message.additional_properties.get(EXCLUDED_KEY)
        and message.additional_properties[GROUP_ANNOTATION_KEY].get(SUMMARIZED_BY_SUMMARY_ID_KEY)
        not in held
    ]


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize(
    ("conversation", "config"),
    [
        pytest.param(_summaries, _Stack(budget=1000), id="fold"),
        pytest.param(
            lambda: _turns(after_summary=True),
            _Stack(budget=1200, summary_mode=SUMMARY_MODE_RECOMPACT),
            id="recompact",
        ),
    ],
)
async def test_a_summary_superseding_summaries_reaches_every_later_call(
    conversation: Callable[[], list[Message]], config: _Stack, shape: Shape
) -> None:
    run = await _run(shape, conversation(), config)

    first = next(i for i, sent in enumerate(run.sent) if _USER_ACCOUNT in "".join(_stand_ins(sent)))
    assert len(run.sent) - first > _LATER_CALLS
    expected = _stand_ins(run.sent[first])
    assert len(expected) == 1
    for number, sent in enumerate(run.sent[first:], start=first + 1):
        texts = [text for _, text in sent]
        assert _stand_ins(sent) == expected, f"call {number} lost or changed the summary"
        assert not [text for text in texts if _is_superseded(text)], (
            f"call {number} sent a superseded summary beside the one superseding it"
        )
        assert not [text for text, count in Counter(texts).items() if count > 1 and text]
    assert run.requests == 1, "the summaries were summarized again"


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize(
    ("conversation", "made"),
    [
        pytest.param(
            _summaries,
            lambda stand_ins: len(stand_ins) == 1 and _USER_ACCOUNT in stand_ins[0],
            id="fold",
        ),
        pytest.param(
            lambda: _lookups_done(4, 300),
            lambda stand_ins: bool(stand_ins) and all(s.startswith(_NOTE) for s in stand_ins),
            id="note",
        ),
    ],
)
async def test_a_replacement_made_on_a_refused_pinned_call_reaches_its_retry(
    conversation: Callable[[], list[Message]], made: Callable[[list[str]], bool], shape: Shape
) -> None:
    run = await _run(
        shape,
        conversation(),
        _Stack(budget=1200),
        lookup_chars=_RETRY_LOOKUP_CHARS,
        refuse_pins=True,
    )

    pinned = run.pinned.index(True)
    assert made(_stand_ins(run.sent[pinned])), "the pinned call made no replacement"
    assert _stand_ins(run.sent[pinned]) != _stand_ins(run.sent[pinned - 1])
    assert not run.pinned[pinned + 1], "the refused call was not retried"
    assert run.sent[pinned + 1] == run.sent[pinned], "the retry sent other messages"


def _user_half(
    summarizer: _Summarizer, mode: str = SUMMARY_MODE_BOUNDARY
) -> UserTurnAnchoredSummarizationCompactionStrategy:
    return UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=1_000,
        tokenizer=TOKENIZER,
        client=summarizer,
        max_summary_input_tokens=100_000,
        summary_mode=mode,
        remembered_requests=2,
    )


@pytest.mark.parametrize("change", ["removed", "edited"])
@pytest.mark.parametrize("mode", [SUMMARY_MODE_BOUNDARY, SUMMARY_MODE_RECOMPACT])
async def test_a_held_summary_excludes_again_what_it_links_when_another_source_changed(
    mode: str, change: str
) -> None:
    summarizer = _Summarizer()
    strategy = _user_half(summarizer, mode)
    if mode == SUMMARY_MODE_RECOMPACT:
        messages = _turns(after_summary=True)
        assert await strategy(messages)
    else:
        messages = _summaries()
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=TOKENIZER)
        assert await strategy.fold_if_smaller(messages) == "folded"
    released = [m for m in messages if _is_superseded(m.text or "")][-1]
    index, other = next(
        (index, m)
        for index, m in enumerate(messages)
        if m.additional_properties.get(EXCLUDED_KEY) and m is not released
    )
    if change == "removed":
        del messages[index]
    else:
        messages[index] = Message(
            other.role,
            [f"{other.text} Edited."],
            message_id=other.message_id,
            additional_properties=dict(other.additional_properties),
        )
    # What the framework's tool loop does to a superseded summary while it keeps the new one.
    released.additional_properties[GROUP_ANNOTATION_KEY].pop(SUMMARIZED_BY_SUMMARY_ID_KEY)
    released.additional_properties.pop(EXCLUDE_REASON_KEY)
    released.additional_properties[EXCLUDED_KEY] = False
    annotate_message_groups(messages)

    strategy.replay_summaries(messages)

    assert released.additional_properties[EXCLUDED_KEY], "sent beside the summary superseding it"
    assert summarizer.requests == 1


@pytest.mark.parametrize("lost", ["source", "state"])
async def test_turns_excluded_for_a_summary_that_cannot_be_put_back_are_compacted_afresh(
    lost: str,
) -> None:
    summarizer = _Summarizer()
    compaction = _user_half(summarizer)
    carried = _turns()
    assert await compaction(list(carried))
    if lost == "source":
        carried.remove(next(m for m in carried if m.additional_properties.get(EXCLUDED_KEY)))
    else:
        compaction = _user_half(summarizer)

    retry = list(carried)
    assert await compaction(retry)

    assert not _orphaned(retry)
    assert summarizer.requests == 2, "the released turns were not summarized afresh"


async def test_a_harness_store_keeps_each_summary_the_resent_turns_are_excluded_for() -> None:
    summarizer = _Summarizer()
    registry = _registry(_Stack(budget=6_000), summarizer)
    provider = SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False)
    history = _HostHistory()
    agent = create_harness_agent(
        _Model(0, summarizer),
        name="replacements",
        history_provider=history,
        disable_compaction=True,
        disable_file_memory=True,
        disable_web_search=True,
        disable_tool_auto_approval=True,
        context_providers=[provider],
        middleware=[SessionToolResultRecallMiddleware(registry, compact_before=True)],
        tools=[make_session_recall_tool(registry)],
    )
    # Where the harness puts its own compaction provider: straight after its history provider.
    agent.context_providers.remove(provider)
    agent.context_providers.insert(1, provider)
    session = AgentSession()
    turns = [Message("user", [f"Turn {i}: " + "x" * 4_000], message_id=f"u{i}") for i in range(6)]

    await agent.run(turns, session=session)

    stored = session.state[history.source_id]["messages"]
    assert [m for m in stored if m.additional_properties.get(EXCLUDED_KEY)]
    assert not _orphaned(stored)
    assert summarizer.requests == 1
