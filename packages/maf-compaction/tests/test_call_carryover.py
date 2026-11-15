"""A per-call pass starts from the list the previous call's pass left.

A host that compacts before every model call rarely hands the strategy the list it compacted
last time: the framework's chat-middleware layer compacts a copy of the tool loop's list, and
per-service-call history persistence hands fresh copies of the stored messages. These tests
drive both, and a host that keeps its list, through the same calls. They read what each call
sends, how much of a pass is spent putting decisions back, and what the store is left holding.
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

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
    GROUP_TOKEN_COUNT_KEY,
    apply_compaction,
    included_token_count,
    project_included_messages,
)
from test_pass_cost import _counting
from test_record_budget import (
    TOKENIZER,
    _history,
    _sent,
    _size,
    _strategy,
    _Summarizer,
    _tool_group,
)

from maf_compaction import (
    DEFAULT_MAX_CARRIED_VIEWS,
    CompactionBudgetExceeded,
    CompactionStack,
    RecallGate,
    SessionCompactionProvider,
    SessionCompactionRegistry,
    SessionToolResultRecallMiddleware,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    make_session_record_merge_middleware,
)
from maf_compaction._anchored import _is_marker
from maf_compaction._carryover import CarriedView, Handed, _host_state

pytestmark = pytest.mark.anyio

_Composed = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
_ATTRIBUTION = "_attribution"
_HOST_REASON = "host"

#: Records, shed narration and plain tool groups in the transcript, and the budget's share of it.
_SHAPES = [
    pytest.param((14, 10, 6), 0.33, id="tight"),
    pytest.param((14, 10, 6), 0.6, id="loose"),
    pytest.param((30, 20, 12), 0.25, id="long-tight"),
    pytest.param((8, 4, 20), 0.5, id="mostly-tools"),
    pytest.param((20, 0, 0), 0.4, id="records-only"),
]
_CALLS = 8


def _transcript(records: int, narration: int, groups: int) -> list[Message]:
    messages = _history(records)
    for index in range(narration):
        messages[2:2] = [
            Message("user", [f"Question {index}."], message_id=f"question{index}"),
            Message("assistant", [f"Narration {index}. " * 20], message_id=f"narration{index}"),
        ]
    for index in range(groups):
        messages[-1:-1] = _tool_group(100 + index)
    return messages


def _budget(transcript: list[Message], share: float) -> int:
    return int(_size(copy.deepcopy(transcript)) * share)


def _answer(call: int) -> tuple[Message, Message]:
    """Return a model's tool call and its result, neither with an id of its own."""
    return (
        Message(
            "assistant",
            [{"type": "function_call", "call_id": f"run{call}", "name": "read", "arguments": "{}"}],
        ),
        Message(
            "tool",
            [
                {
                    "type": "function_result",
                    "call_id": f"run{call}",
                    "result": f"result {call} " + "y" * (300 + 40 * call),
                }
            ],
        ),
    )


class _Rebuilding:
    """A host on per-service-call history persistence.

    Each call is handed copies of the stored messages, made as ``SessionContext.extend_messages``
    makes them, and the messages the call carries in as they are. Those are stored once the
    call returns, with whatever the pass left on them.
    """

    def __init__(self, transcript: list[Message]) -> None:
        self.store: list[Message] = []
        self.carried_in = list(transcript)

    def hand(self) -> list[Message]:
        copies: list[Message] = []
        for message in self.store:
            clone = copy.copy(message)
            clone.additional_properties = dict(message.additional_properties)
            clone.additional_properties[_ATTRIBUTION] = {"source_id": "in_memory"}
            copies.append(clone)
        return [*copies, *self.carried_in]

    def answered(self, call: int) -> None:
        response, result = _answer(call)
        self.store = [*self.store, *self.carried_in, response]
        self.carried_in = [result]


class _Keeping:
    """A host that hands every call the list the last pass left, with what was said since."""

    def __init__(self, transcript: list[Message]) -> None:
        self.messages = list(transcript)

    def hand(self) -> list[Message]:
        return self.messages

    def answered(self, call: int) -> None:
        self.messages.extend(_answer(call))


async def _compact(strategy: _Composed, messages: list[Message]) -> list[Any]:
    """Compact as the framework does before a model call, and return what the call sends."""

    async def before(messages: list[Message]) -> bool:
        try:
            return await strategy.compact_before(messages, options={})
        except CompactionBudgetExceeded:
            return True

    await apply_compaction(messages, strategy=before)
    return _sent(messages)


def _without_carrying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CarriedView, "resume", lambda self, messages, current: None)


async def _run(
    host: _Rebuilding | _Keeping, strategy: _Composed, calls: int = _CALLS
) -> list[list[Any]]:
    sent: list[list[Any]] = []
    for call in range(1, calls + 1):
        sent.append(await _compact(strategy, host.hand()))
        host.answered(call)
    return sent


@pytest.mark.parametrize(("shape", "share"), _SHAPES)
async def test_a_rebuilt_list_sends_what_a_kept_list_sends(
    monkeypatch: pytest.MonkeyPatch, shape: tuple[int, int, int], share: float
) -> None:
    """Rebuilding the list every call changes nothing a call sends, and puts nothing back."""
    budget = _budget(_transcript(*shape), share)
    kept = await _run(
        _Keeping(_transcript(*shape)), _strategy(_Summarizer(), budget=budget, policy="shed")
    )
    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    host = _Rebuilding(_transcript(*shape))
    calls: Counter[str] = Counter()
    for name in ("_record_replay_view", "_match_record_edit"):
        _counting(monkeypatch, _Composed, name, calls)

    for call in range(1, _CALLS + 1):
        calls.clear()
        sent = await _compact(strategy, host.hand())
        host.answered(call)

        assert sent == kept[call - 1], f"call {call} sent something else than a kept list"
        if call > 1:
            assert not calls, f"call {call} rebuilt a record's window"
    assert strategy.calls_resumed == _CALLS - 1


@pytest.mark.parametrize(("shape", "share"), _SHAPES)
async def test_a_resumed_pass_leaves_the_store_as_a_rebuilt_list_would(
    shape: tuple[int, int, int], share: float
) -> None:
    """A flag reaches the store only on a message the call carried in, as without resuming."""
    strategy = _strategy(_Summarizer(), budget=_budget(_transcript(*shape), share), policy="shed")
    host = _Rebuilding(_transcript(*shape))

    for call in range(1, _CALLS + 1):
        stored = [(message, _host_state(message)) for message in host.store]
        await _compact(strategy, host.hand())
        host.answered(call)

        changed = [message.message_id for message, state in stored if _host_state(message) != state]
        assert not changed, f"call {call} changed stored messages {changed}"


@pytest.mark.parametrize(("shape", "share"), _SHAPES)
async def test_a_resumed_list_carries_the_marks_this_call_was_handed(
    shape: tuple[int, int, int], share: float
) -> None:
    """The framework's mark says where a flag lasts, so a kept message takes this call's."""
    strategy = _strategy(_Summarizer(), budget=_budget(_transcript(*shape), share), policy="shed")
    host = _Rebuilding(_transcript(*shape))

    for call in range(1, _CALLS + 1):
        stored = {id(message.contents) for message in host.store}
        handed = host.hand()
        await _compact(strategy, handed)
        host.answered(call)

        wrong = [
            message.message_id
            for message in handed
            if (_ATTRIBUTION in message.additional_properties) != (id(message.contents) in stored)
        ]
        assert not wrong, f"call {call} left another call's mark on {wrong}"


def _counted(message: Message, count: int | None) -> Message:
    annotation = message.additional_properties.setdefault(GROUP_ANNOTATION_KEY, {})
    if count is None:
        annotation.pop(GROUP_TOKEN_COUNT_KEY, None)
    else:
        annotation[GROUP_TOKEN_COUNT_KEY] = count
    return message


@pytest.mark.parametrize("dropped", [True, False])
def test_a_kept_message_is_counted_again_only_when_the_host_dropped_its_count(
    dropped: bool,
) -> None:
    """A message the pass changed stays in the list, with the count taken before a rewrite."""
    stored = _counted(Message("tool", ["a long result"], message_id="result"), 40)
    handed = Handed.take([stored])
    kept = copy.copy(stored)
    kept.additional_properties = {
        EXCLUDED_KEY: True,
        GROUP_ANNOTATION_KEY: {GROUP_TOKEN_COUNT_KEY: 40},
    }
    view = CarriedView(handed, [kept])
    loaded = copy.copy(stored)
    loaded.additional_properties = {GROUP_ANNOTATION_KEY: {}}
    _counted(loaded, None if dropped else 40)
    messages = [loaded]

    resumed = view.resume(messages, Handed.take(messages))

    assert resumed is not None and resumed.restored
    assert messages == [kept]
    count = kept.additional_properties[GROUP_ANNOTATION_KEY].get(GROUP_TOKEN_COUNT_KEY)
    assert count == (None if dropped else 40)


async def test_a_lost_list_costs_one_pass_and_no_pair() -> None:
    """Without the kept list a pass puts its decisions back, and no call is sent a half pair."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    host = _Rebuilding(transcript)
    resumed: list[bool] = []

    for call in range(1, _CALLS + 1):
        if call == 5:
            strategy.forget_carried_view()
        before = strategy.calls_resumed
        handed = host.hand()
        await _compact(strategy, handed)
        host.answered(call)

        resumed.append(strategy.calls_resumed > before)
        sent = project_included_messages(handed)
        called = {c.call_id for m in sent for c in m.contents if c.type == "function_call"}
        answered = {c.call_id for m in sent for c in m.contents if c.type == "function_result"}
        assert called == answered, f"call {call} sent a call or a result alone"
    assert resumed == [False, True, True, True, False, True, True, True]


@pytest.mark.parametrize("departure", ["dropped", "reordered", "another-conversation", "host-hold"])
async def test_a_list_that_does_not_continue_the_last_one_is_compacted_afresh(
    monkeypatch: pytest.MonkeyPatch, departure: str
) -> None:
    """Anything but the same stored messages, in order and unchanged, gets today's pass."""
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)
    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    host = _Rebuilding(transcript)
    for call in (1, 2):
        await _compact(strategy, host.hand())
        host.answered(call)
    assert strategy.calls_resumed == 1
    held = None
    if departure == "dropped":
        del host.store[30]
    elif departure == "reordered":
        host.store[1], host.store[-2] = host.store[-2], host.store[1]
    elif departure == "another-conversation":
        host.store = copy.deepcopy(host.store)
    else:
        held = next(m for m in host.store if m.message_id == "question3")
        held.additional_properties[EXCLUDED_KEY] = True
        held.additional_properties[EXCLUDE_REASON_KEY] = _HOST_REASON
    handed = host.hand()

    sent = await _compact(strategy, handed)

    assert strategy.calls_resumed == 1
    with monkeypatch.context() as patch:
        _without_carrying(patch)
        control = _strategy(_Summarizer(), budget=budget, policy="shed")
        rebuilt = _Rebuilding(_transcript(14, 10, 6))
        for call in (1, 2):
            await _compact(control, rebuilt.hand())
            rebuilt.answered(call)
        if departure == "dropped":
            del rebuilt.store[30]
        elif departure == "reordered":
            rebuilt.store[1], rebuilt.store[-2] = rebuilt.store[-2], rebuilt.store[1]
        elif departure == "host-hold":
            other = next(m for m in rebuilt.store if m.message_id == "question3")
            other.additional_properties[EXCLUDED_KEY] = True
            other.additional_properties[EXCLUDE_REASON_KEY] = _HOST_REASON
        assert sent == await _compact(control, rebuilt.hand())
    if held is not None:
        standing = next(m for m in handed if m.message_id == "question3")
        assert standing.additional_properties[EXCLUDE_REASON_KEY] == _HOST_REASON


async def test_ids_stay_unique_and_each_group_keeps_one_note() -> None:
    """A positional id handed for this call cannot take one the kept list already uses."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    host = _Rebuilding(transcript)

    for call in range(1, _CALLS + 1):
        handed = host.hand()
        await _compact(strategy, handed)
        host.answered(call)

        ids = [message.message_id for message in handed]
        assert all(ids) and len(ids) == len(set(ids)), f"call {call} repeated an id"
        notes = Counter(m.message_id for m in handed if _is_marker(m))
        assert not [note for note, count in notes.items() if count > 1]
        texts = Counter(m.text for m in project_included_messages(handed) if _is_marker(m))
        assert len(notes) >= len(texts)
    assert strategy.calls_resumed == _CALLS - 1


async def test_a_count_the_host_dropped_is_taken_again() -> None:
    """A host that rewrites a result and drops its count is counted at the new size."""
    transcript = _transcript(8, 0, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.9), policy="shed")
    host = _Rebuilding(transcript)
    for call in (1, 2):
        await _compact(strategy, host.hand())
        host.answered(call)
    result = next(m for m in host.store if m.message_id == "r103")
    result.contents[0].result = "code=CODE-0103"
    handed = host.hand()
    for message in handed:
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
        if isinstance(annotation, dict):
            annotation.pop(GROUP_TOKEN_COUNT_KEY, None)

    await _compact(strategy, handed)

    assert strategy.calls_resumed == 2
    counted = included_token_count(handed)
    assert counted == _size(handed)


async def test_a_refused_budget_still_carries_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closing budget check refuses a list every phase has worked on, and it is kept."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=120, policy="shed")
    host = _Rebuilding(transcript)
    calls: Counter[str] = Counter()
    _counting(monkeypatch, _Composed, "_record_replay_view", calls)

    for call in (1, 2, 3):
        calls.clear()
        with pytest.raises(CompactionBudgetExceeded):
            await strategy.compact_before(host.hand(), options={})
        host.answered(call)
        if call > 1:
            assert not calls

    assert strategy.calls_resumed == 2


@pytest.mark.parametrize("error", [RuntimeError("down"), CompactionBudgetExceeded(2, 1)])
async def test_a_pass_that_fails_carries_nothing(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Only the closing budget check refuses a finished list; a phase that raises has not."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    host = _Rebuilding(transcript)
    await _compact(strategy, host.hand())
    host.answered(1)
    assert strategy.carries_view

    async def fail(*args: Any, **kwargs: Any) -> bool:
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(_Composed, "_last_resort", fail)
        with pytest.raises(type(error)):
            await strategy.compact_before(host.hand(), options={})

    assert not strategy.carries_view
    before = strategy.calls_resumed
    await _compact(strategy, host.hand())
    assert strategy.calls_resumed == before


async def test_below_every_trigger_nothing_is_kept_or_reported() -> None:
    transcript = _transcript(2, 1, 1)
    strategy = _strategy(_Summarizer(), budget=10 * _size(copy.deepcopy(transcript)), policy="shed")
    strategy.tool_results.trigger_fraction = 0.9
    strategy.user_trigger_fraction = 0.9
    host = _Rebuilding(transcript)

    for call in (1, 2, 3):
        handed = host.hand()
        assert not await strategy.compact_before(handed, options={})
        assert not strategy.carries_view
        host.answered(call)
    assert strategy.calls_resumed == 0


async def test_a_resumed_list_is_reported_as_changed() -> None:
    """The framework annotates and logs only what a strategy says it changed."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.6), policy="shed")
    host = _Rebuilding(transcript)
    assert await strategy.compact_before(host.hand(), options={})
    host.answered(1)
    handed = host.hand()
    included = len(project_included_messages(handed))

    assert await strategy.compact_before(handed, options={})

    assert strategy.calls_resumed == 1
    assert len(project_included_messages(handed)) < included


@pytest.mark.parametrize(("shape", "share"), _SHAPES[:2])
async def test_a_host_that_keeps_its_list_has_nothing_restored(
    shape: tuple[int, int, int], share: float
) -> None:
    strategy = _strategy(_Summarizer(), budget=_budget(_transcript(*shape), share), policy="shed")

    await _run(_Keeping(_transcript(*shape)), strategy)

    assert strategy.calls_resumed == 0


class _NoOp(ChatMiddleware):
    async def process(self, context: Any, call_next: Any) -> None:
        await call_next()


class _Layered(ChatMiddlewareLayer, BaseChatClient):
    """A client with chat middleware, which compacts a copy of the list it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[list[Any]] = []

    def _inner_get_response(
        self, *, messages: Any, stream: bool, options: Any, **kwargs: Any
    ) -> Any:
        self.sent.append(
            [(str(m.role), [content.to_dict() for content in m.contents]) for m in messages]
        )

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[Message("assistant", ["Done."])])

        return respond()


@pytest.mark.parametrize(("shape", "share"), _SHAPES)
async def test_behind_a_chat_middleware_later_calls_put_nothing_back(
    monkeypatch: pytest.MonkeyPatch, shape: tuple[int, int, int], share: float
) -> None:
    """The tool loop's list never holds a record, and every call is still sent them."""
    budget = _budget(_transcript(*shape), share)
    kept = await _run(
        _Keeping(_transcript(*shape)), _strategy(_Summarizer(), budget=budget, policy="shed")
    )
    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    loop = _transcript(*shape)
    client = _Layered()
    calls: Counter[str] = Counter()
    for name in ("_record_replay_view", "_match_record_edit"):
        _counting(monkeypatch, _Composed, name, calls)

    async def before(messages: list[Message]) -> bool:
        try:
            return await strategy.compact_before(messages, options={})
        except CompactionBudgetExceeded:
            return True

    for call in range(1, _CALLS + 1):
        calls.clear()
        await client.get_response(
            loop, compaction_strategy=before, client_kwargs={"middleware": [_NoOp()]}
        )
        loop.extend(_answer(call))

        assert client.sent[-1] == kept[call - 1], f"call {call} sent something else"
        if call > 1:
            assert not calls, f"call {call} rebuilt a record's window"
    assert strategy.calls_resumed == _CALLS - 1


_HARNESS_CALLS = 7


@tool(name="read", approval_mode="never_require")
def _read() -> str:
    return "result " + "y" * 400


class _Scripted(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Streams a tool call on every roundtrip but the last."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[int] = []

    def _inner_get_response(
        self, *, messages: Any, stream: bool, options: Any, **kwargs: Any
    ) -> Any:
        self.sent.append(len(messages))
        call = len(self.sent)
        contents = (
            [Content.from_function_call(call_id=f"run{call}", name="read", arguments={})]
            if call < _HARNESS_CALLS
            else [Content.from_text("Done.")]
        )

        async def updates() -> Any:
            yield ChatResponseUpdate(role="assistant", contents=contents)

        return ResponseStream(updates(), finalizer=ChatResponse.from_updates)


def _registry(budget: int, built: list[_Composed], **options: Any) -> SessionCompactionRegistry:
    def build() -> CompactionStack:
        strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
        built.append(strategy)
        gate = RecallGate()
        recall = ToolResultRecallMiddleware(
            max_input_tokens=budget, tokenizer=TOKENIZER, arm=gate.arm, disarm=gate.disarm
        )
        return CompactionStack(strategy, gate, recall)

    return SessionCompactionRegistry(build, **options)


@pytest.mark.parametrize("carried", [False, True])
async def test_a_harness_run_resumes_on_every_later_call(
    monkeypatch: pytest.MonkeyPatch, carried: bool
) -> None:
    """Per-service-call persistence, streaming: the run the deployed hosts make."""
    transcript = _transcript(14, 10, 6)
    built: list[_Composed] = []
    if not carried:
        _without_carrying(monkeypatch)
    rebuilt: list[int] = []
    calls: Counter[str] = Counter()
    for name in ("_record_replay_view", "_match_record_edit"):
        _counting(monkeypatch, _Composed, name, calls)
    compact_before = _Composed.compact_before

    async def counted(self: _Composed, messages: list[Message], *, options: Any) -> bool:
        calls.clear()
        try:
            return await compact_before(self, messages, options=options)
        finally:
            rebuilt.append(sum(calls.values()))

    monkeypatch.setattr(_Composed, "compact_before", counted)
    client = _Scripted()
    agent = create_harness_agent(
        client=client,
        name="carryover",
        tools=[_read],
        disable_compaction=True,
        disable_web_search=True,
        disable_file_memory=True,
        disable_todo=True,
        disable_mode=True,
        middleware=[
            SessionToolResultRecallMiddleware(
                _registry(_budget(transcript, 0.6), built), compact_before=True
            )
        ],
    )

    async for _ in agent.run(transcript, session=agent.create_session(), stream=True):
        pass

    assert client.sent == [45, 47, 49, 46, 48, 50, 52]
    assert len(built) == 1
    if carried:
        assert rebuilt == [0] * _HARNESS_CALLS
        assert built[0].calls_resumed == _HARNESS_CALLS - 1
    else:
        assert all(rebuilt[1:])
        assert built[0].calls_resumed == 0


async def _pass(registry: SessionCompactionRegistry, session: AgentSession) -> _Composed:
    with registry.activate(session) as stack:
        strategy = stack.strategy
        assert isinstance(strategy, _Composed)
        await _compact(strategy, _transcript(14, 10, 6))
    return strategy


async def test_only_the_sessions_used_last_keep_a_list() -> None:
    budget = _budget(_transcript(14, 10, 6), 0.6)
    registry = _registry(budget, [], max_carried_views=2)
    sessions = [AgentSession() for _ in range(4)]

    strategies = [await _pass(registry, session) for session in sessions]

    assert [strategy.carries_view for strategy in strategies] == [False, False, True, True]
    await _pass(registry, sessions[0])
    assert [strategy.carries_view for strategy in strategies] == [True, False, False, True]
    assert SessionCompactionRegistry(lambda: None).max_carried_views == DEFAULT_MAX_CARRIED_VIEWS  # type: ignore[arg-type, return-value]


async def test_a_registry_can_keep_no_list() -> None:
    registry = _registry(_budget(_transcript(14, 10, 6), 0.6), [], max_carried_views=0)

    strategy = await _pass(registry, AgentSession())

    assert not strategy.carries_view
    with pytest.raises(ValueError, match="max_carried_views"):
        _registry(1, [], max_carried_views=-1)


@pytest.mark.parametrize("ending", [None, "stored-history-phase", "record-merge-middleware"])
async def test_the_list_is_released_when_the_turn_ends(ending: str | None) -> None:
    """Whatever the package runs at the end of a turn lets the list go: no call follows."""
    transcript = _transcript(14, 10, 6)
    built: list[_Composed] = []
    registry = _registry(_budget(transcript, 0.6), built)
    providers: list[Any] = [InMemoryHistoryProvider()]
    middleware: list[Any] = [SessionToolResultRecallMiddleware(registry, compact_before=True)]
    if ending == "stored-history-phase":
        providers.append(SessionCompactionProvider(registry, tokenizer=TOKENIZER, before=False))
    elif ending == "record-merge-middleware":
        middleware.insert(0, make_session_record_merge_middleware(registry))
    agent = Agent(
        client=_Layered(),
        name="carryover",
        context_providers=providers,
        middleware=middleware,
    )

    await agent.run(transcript, session=AgentSession())

    assert len(built) == 1
    assert built[0].carries_view is (ending is None)


async def test_the_kept_list_is_no_part_of_a_session_snapshot() -> None:
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.33)
    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    host = _Rebuilding(transcript)
    await _compact(strategy, host.hand())
    host.answered(1)
    assert strategy.carries_view
    state = copy.deepcopy(strategy.session_state())
    worker = strategy._record_worker(budget, lambda *args: None, lambda: True)
    assert not worker.carries_view

    strategy.forget_carried_view()

    assert strategy.session_state() == state
    restored = _strategy(_Summarizer(), budget=budget, policy="shed")
    restored.restore_session_state(state)
    assert not restored.carries_view
