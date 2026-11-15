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
from typing import Any, Final

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
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
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
from maf_compaction._carryover import CarriedView, Handed
from maf_compaction._request import RECALL_PROMPT_KEY

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


def _own(message: Message) -> dict[str, Any]:
    """Return what a message holds for itself: a loaded copy shares only its group annotation."""
    properties = message.additional_properties
    return copy.deepcopy(
        {key: properties[key] for key in properties if key != GROUP_ANNOTATION_KEY}
    )


def _half_excluded(messages: list[Message]) -> list[str]:
    """Return the calls whose call and result do not share one exclusion."""
    calls: dict[str, bool] = {}
    results: dict[str, bool] = {}
    for message in messages:
        excluded = bool(message.additional_properties.get(EXCLUDED_KEY))
        for content in message.contents:
            if content.type == "function_call":
                calls[str(content.call_id)] = excluded
            elif content.type == "function_result":
                results[str(content.call_id)] = excluded
    return [call for call in calls.keys() & results.keys() if calls[call] != results[call]]


@pytest.mark.parametrize(("shape", "share"), _SHAPES)
async def test_a_resumed_pass_writes_on_no_stored_message(
    shape: tuple[int, int, int], share: float
) -> None:
    """A flag reaches the store on a message the call carried in, and never on half a pair."""
    strategy = _strategy(_Summarizer(), budget=_budget(_transcript(*shape), share), policy="shed")
    host = _Rebuilding(_transcript(*shape))

    for call in range(1, _CALLS + 1):
        stored = [(message, _own(message)) for message in host.store]
        await _compact(strategy, host.hand())
        host.answered(call)

        changed = [message.message_id for message, own in stored if _own(message) != own]
        assert not changed, f"call {call} changed stored messages {changed}"
        marked = [m.message_id for m in host.store if _ATTRIBUTION in m.additional_properties]
        assert not marked, f"call {call} left the framework's mark on stored {marked}"
        assert not _half_excluded(host.store), f"call {call} stored half a pair excluded"


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


@pytest.mark.parametrize("linked", [True, False])
def test_a_kept_message_gives_way_only_to_one_carrying_its_links(linked: bool) -> None:
    """A link a pass wrote on its own copy is lost if the handed message takes its place."""
    stored = Message("user", ["an earlier turn"], message_id="turn")
    handed = Handed.take([stored])
    kept = copy.copy(stored)
    kept.additional_properties = {
        GROUP_ANNOTATION_KEY: {SUMMARIZED_BY_SUMMARY_ID_KEY: "user_summary_0"}
    }
    view = CarriedView(handed, [kept])
    loaded = copy.copy(stored)
    loaded.additional_properties = {
        GROUP_ANNOTATION_KEY: {SUMMARIZED_BY_SUMMARY_ID_KEY: "user_summary_0"} if linked else {}
    }
    messages = [loaded]

    resumed = view.resume(messages, Handed.take(messages))

    assert resumed is not None and resumed.restored and not resumed.changed
    assert messages[0] is (loaded if linked else kept)


def test_a_flag_written_out_as_false_is_no_change() -> None:
    """A pass writes an included message's flag out, and the store may hold it without one."""
    stored = Message("user", ["a turn"], message_id="turn")
    kept = copy.copy(stored)
    kept.additional_properties = {EXCLUDED_KEY: False}
    view = CarriedView(Handed.take([stored]), [kept])
    loaded = copy.copy(stored)
    loaded.additional_properties = {}
    messages = [loaded]

    resumed = view.resume(messages, Handed.take(messages))

    assert resumed is not None and resumed.restored and not resumed.changed
    assert messages[0] is loaded


def test_a_kept_message_keeps_standing_for_one_handed_under_new_lists() -> None:
    """A host that rebuilds a message on every load still has its dropped count honoured."""
    stored = Message("tool", ["a long result"], message_id="result")
    kept = copy.copy(stored)
    kept.additional_properties = {EXCLUDED_KEY: True}
    view = CarriedView(Handed.take([stored]), [kept])
    for _ in (1, 2):
        _counted(kept, 40)
        loaded = Message("tool", ["a long result"], message_id="result")
        messages = [loaded]
        current = Handed.take(messages)

        assert view.resume(messages, current) is not None

        assert messages == [kept]
        assert GROUP_TOKEN_COUNT_KEY not in kept.additional_properties[GROUP_ANNOTATION_KEY]
        view = CarriedView(current, messages)


def _rewrite(contents: list[Content], edit: str) -> None:
    if edit == "an-item-replaced":
        contents[0] = Content.from_text("an edited turn")
    elif edit == "an-item-added":
        contents.append(Content.from_text("and more"))
    elif edit == "an-item-removed":
        contents.clear()
    else:
        contents[0].text = "an edited turn"


@pytest.mark.parametrize("edit", ["an-item-replaced", "an-item-added", "an-item-removed"])
def test_a_stored_list_edited_in_place_gets_a_full_pass(edit: str) -> None:
    """The list is the same object, so only the items it held when handed show the edit."""
    stored = Message("user", ["an earlier turn"], message_id="turn")
    kept = copy.copy(stored)
    kept.additional_properties = {EXCLUDED_KEY: True}
    view = CarriedView(Handed.take([stored]), [kept])
    _rewrite(stored.contents, edit)
    loaded = copy.copy(stored)
    loaded.additional_properties = {}
    messages = [loaded]

    assert view.resume(messages, Handed.take(messages)) is None

    assert messages == [loaded]


@pytest.mark.parametrize("edit", ["an-item-replaced", "an-item-rewritten"])
def test_a_message_reloaded_under_a_new_list_and_then_edited_gets_a_full_pass(edit: str) -> None:
    """A kept copy still holding the first list must not stand for the edited message."""
    stored = Message("user", ["an earlier turn"], message_id="turn")
    kept = copy.copy(stored)
    kept.additional_properties = {
        GROUP_ANNOTATION_KEY: {SUMMARIZED_BY_SUMMARY_ID_KEY: "user_summary_0"}
    }
    view = CarriedView(Handed.take([stored]), [kept])
    reloaded = Message("user", ["an earlier turn"], message_id="turn")
    messages = [reloaded]
    current = Handed.take(messages)
    assert view.resume(messages, current) is not None
    assert messages == [kept]
    view = CarriedView(current, messages)
    _rewrite(reloaded.contents, edit)
    edited = copy.copy(reloaded)
    edited.additional_properties = {}
    messages = [edited]

    assert view.resume(messages, Handed.take(messages)) is None

    assert messages == [edited] and edited.text == "an edited turn"


def test_an_item_rewritten_in_place_is_sent_as_rewritten() -> None:
    """Nothing tells a rewritten content object from itself; the kept copy shares it."""
    stored = Message("user", ["an earlier turn"], message_id="turn")
    kept = copy.copy(stored)
    kept.additional_properties = {EXCLUDED_KEY: True}
    view = CarriedView(Handed.take([stored]), [kept])
    _rewrite(stored.contents, "an-item-rewritten")
    loaded = copy.copy(stored)
    loaded.additional_properties = {}
    messages = [loaded]

    assert view.resume(messages, Handed.take(messages)) is not None

    assert messages == [kept] and kept.text == "an edited turn"


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


def _depart(host: _Rebuilding, departure: str) -> None:
    if departure == "dropped":
        del host.store[30]
    elif departure == "truncated":
        del host.store[-2:]
        host.carried_in = [Message("user", ["Start again from there."], message_id="again")]
    elif departure == "cut-short":
        del host.store[-2:]
        host.carried_in = []
    elif departure == "reordered":
        host.store[1], host.store[-2] = host.store[-2], host.store[1]
    elif departure == "another-conversation":
        host.store = copy.deepcopy(host.store)
        other = next(m for m in host.store if m.message_id == "question3")
        other.contents = [Content.from_text("A question nobody asked here.")]
    else:
        held = next(m for m in host.store if m.message_id == "question3")
        held.additional_properties[EXCLUDED_KEY] = True
        held.additional_properties[EXCLUDE_REASON_KEY] = _HOST_REASON


@pytest.mark.parametrize(
    "departure",
    ["dropped", "truncated", "cut-short", "reordered", "another-conversation", "host-hold"],
)
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
    _depart(host, departure)
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
        _depart(rebuilt, departure)
        assert sent == await _compact(control, rebuilt.hand())
    if departure == "host-hold":
        standing = next(m for m in handed if m.message_id == "question3")
        assert standing.additional_properties[EXCLUDE_REASON_KEY] == _HOST_REASON


class _Reloading(_Rebuilding):
    """A host whose loads rebuild stored messages, under new lists or as new objects."""

    def __init__(self, transcript: list[Message], rebuilt: str) -> None:
        super().__init__(transcript)
        self.rebuilt = rebuilt

    def hand(self) -> list[Message]:
        handed = super().hand()
        for message in handed[: len(self.store)]:
            if self.rebuilt == "same-items":
                message.contents = list(message.contents)
            else:
                message.contents = copy.deepcopy(message.contents)
        return handed


@pytest.mark.parametrize("rebuilt", ["same-items", "equal-items"])
@pytest.mark.parametrize(("shape", "share"), _SHAPES[:2])
async def test_a_stored_message_handed_under_new_contents_still_resumes(
    monkeypatch: pytest.MonkeyPatch, shape: tuple[int, int, int], share: float, rebuilt: str
) -> None:
    """The framework rebuilds a message that mixes a resolved approval with other content.

    It does so on every load, under a new list of the same items; a host that deserializes its
    store hands equal items instead.
    """
    budget = _budget(_transcript(*shape), share)
    kept = await _run(
        _Keeping(_transcript(*shape)), _strategy(_Summarizer(), budget=budget, policy="shed")
    )
    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    calls: Counter[str] = Counter()
    _counting(monkeypatch, _Composed, "_record_replay_view", calls)

    sent = await _run(_Reloading(_transcript(*shape), rebuilt), strategy)

    assert sent == kept
    assert strategy.calls_resumed == _CALLS - 1
    assert not calls


async def test_the_call_after_a_recall_prompt_resumes() -> None:
    """The prompt a forced record call ends with is handed once, so it is no stored message."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.6), policy="shed")
    host = _Rebuilding(transcript)
    for call in (1, 2):
        await _compact(strategy, host.hand())
        host.answered(call)
    handed = host.hand()
    prompt = Message("user", ["Write the record."], additional_properties={RECALL_PROMPT_KEY: True})
    handed.append(prompt)
    await _compact(strategy, handed)
    assert handed[-1] is prompt
    host.answered(3)
    before = strategy.calls_resumed
    handed = host.hand()

    await _compact(strategy, handed)

    assert strategy.calls_resumed == before + 1
    assert prompt not in handed


async def test_a_list_compacted_twice_still_resumes_on_the_next_call() -> None:
    """Handed back as the pass left it, a list holds records no rebuilt list will."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.6), policy="shed")
    host = _Rebuilding(transcript)
    for call in (1, 2):
        await _compact(strategy, host.hand())
        host.answered(call)
    handed = host.hand()
    first = await _compact(strategy, handed)
    assert await _compact(strategy, handed) == first
    host.answered(3)
    before = strategy.calls_resumed

    await _compact(strategy, host.hand())

    assert strategy.calls_resumed == before + 1


async def test_a_message_handed_again_as_new_input_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host may hand one object as a later input too, and it is a new message all the same."""
    transcript = _transcript(14, 10, 6)
    budget = _budget(transcript, 0.6)

    async def second_call(strategy: _Composed, messages: list[Message]) -> list[Any]:
        host = _Rebuilding(messages)
        await _compact(strategy, host.hand())
        again = messages[-1]
        host.store = [*host.store, *host.carried_in, Message("assistant", ["Here it is."])]
        host.carried_in = [again]
        return await _compact(strategy, host.hand())

    strategy = _strategy(_Summarizer(), budget=budget, policy="shed")
    sent = await second_call(strategy, transcript)

    assert sent[-1][0] == "user"
    assert strategy.calls_resumed == 1
    _without_carrying(monkeypatch)
    control = _strategy(_Summarizer(), budget=budget, policy="shed")
    assert sent == await second_call(control, _transcript(14, 10, 6))


async def test_ids_stay_unique_and_each_group_keeps_one_note() -> None:
    """A positional id handed for this call cannot take one the kept list already uses."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    host = _Rebuilding(transcript)
    most = 0

    for call in range(1, _CALLS + 1):
        handed = host.hand()
        await _compact(strategy, handed)
        host.answered(call)

        ids = [message.message_id for message in handed]
        assert all(ids) and len(ids) == len(set(ids)), f"call {call} repeated an id"
        noted = Counter(
            group
            for message in handed
            if _is_marker(message)
            for group in message.additional_properties[GROUP_ANNOTATION_KEY][
                SUMMARY_OF_GROUP_IDS_KEY
            ]
        )
        most = max(most, len(noted))
        assert not [group for group, count in noted.items() if count > 1], f"call {call}"
    assert most, "the shape sheds, so there were notes to count"
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


async def test_behind_a_chat_middleware_one_object_listed_twice_is_sent_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool loop's list may hold one message object as a stored and as a new message."""
    budget = _budget(_transcript(14, 10, 6), 0.6)

    async def second_call(strategy: _Composed) -> list[Any]:
        loop = _transcript(14, 10, 6)
        client = _Layered()

        async def before(messages: list[Message]) -> bool:
            return await strategy.compact_before(messages, options={})

        for _ in (1, 2):
            await client.get_response(
                loop, compaction_strategy=before, client_kwargs={"middleware": [_NoOp()]}
            )
            loop.extend([Message("assistant", ["Here it is."]), loop[-1]])
        return client.sent[-1]

    sent = await second_call(_strategy(_Summarizer(), budget=budget, policy="shed"))

    assert sent[-1][0] == "user"
    _without_carrying(monkeypatch)
    assert sent == await second_call(_strategy(_Summarizer(), budget=budget, policy="shed"))


_HARNESS_CALLS = 7


@tool(name="read", approval_mode="never_require")
def _read() -> str:
    return "result " + "y" * 400


class _Scripted(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Answers with a tool call on every roundtrip but the last."""

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
        if stream:

            async def updates() -> Any:
                yield ChatResponseUpdate(role="assistant", contents=contents)

            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[Message("assistant", contents)])

        return respond()


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


async def _harness_run(
    monkeypatch: pytest.MonkeyPatch, *, stream: bool
) -> tuple[list[int], list[int], _Composed]:
    """Run one harness turn; return what each call was sent, rebuilt, and the strategy."""
    transcript = _transcript(14, 10, 6)
    built: list[_Composed] = []
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
    session = agent.create_session()
    if stream:
        async for _ in agent.run(transcript, session=session, stream=True):
            pass
    else:
        await agent.run(transcript, session=session)
    assert len(built) == 1
    return client.sent, rebuilt, built[0]


@pytest.mark.parametrize("stream", [True, False])
async def test_a_harness_run_resumes_on_every_later_call(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """Per-service-call persistence, as the framework's harness agent runs it."""
    with monkeypatch.context() as patch:
        sent, rebuilt, strategy = await _harness_run(patch, stream=stream)
    with monkeypatch.context() as patch:
        _without_carrying(patch)
        control_sent, control_rebuilt, control = await _harness_run(patch, stream=stream)

    assert len(sent) == _HARNESS_CALLS
    assert sent == control_sent
    assert rebuilt == [0] * _HARNESS_CALLS
    assert strategy.calls_resumed == _HARNESS_CALLS - 1
    assert all(control_rebuilt[1:])
    assert control.calls_resumed == 0


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


_SAME_DECISIONS: Final = ("the-same", "an-equal-copy")


@pytest.mark.parametrize(
    "restored",
    [*_SAME_DECISIONS, "from-elsewhere", "into-the-same-dictionary", "into-its-nested-state"],
)
async def test_decisions_restored_from_elsewhere_drop_the_list(restored: str) -> None:
    """The kept list belongs to the decisions that made it, wherever a host puts others."""
    registry = _registry(_budget(_transcript(14, 10, 6), 0.33), [])
    session = AgentSession()
    strategy = await _pass(registry, session)
    assert strategy.carries_view
    key = registry.state_key
    if restored == "an-equal-copy":
        session.state[key] = copy.deepcopy(session.state[key])
    elif restored not in _SAME_DECISIONS:
        other = AgentSession()
        with registry.activate(other) as stack:
            assert isinstance(stack.strategy, _Composed)
            await _compact(stack.strategy, _transcript(8, 4, 20))
        elsewhere = copy.deepcopy(other.state[key])
        held = session.state[key]
        assert held != elsewhere
        if restored == "from-elsewhere":
            session.state[key] = elsewhere
        elif restored == "into-the-same-dictionary":
            held.clear()
            held.update(elsewhere)
        else:
            nested = [name for name, value in held.items() if isinstance(value, dict)]
            assert nested
            for name in nested:
                held[name].clear()
                held[name].update(elsewhere[name])
            held.update({name: elsewhere[name] for name in elsewhere if name not in nested})
            assert session.state[key] is held and held == elsewhere

    with registry.activate(session) as stack:
        assert stack.strategy is strategy
        assert strategy.carries_view is (restored in _SAME_DECISIONS)


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


@pytest.mark.parametrize("through", ["session-state", "decisions"])
@pytest.mark.parametrize("state", ["an-earlier-one", "the-one-it-holds"])
async def test_a_direct_restore_keeps_the_list_only_for_the_state_it_holds(
    through: str, state: str
) -> None:
    """The strategy's own restore methods are public, so a host can roll it back without a registry."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    earlier = copy.deepcopy(strategy.session_state()), strategy.decision_state()
    await _compact(strategy, _Rebuilding(transcript).hand())
    held = copy.deepcopy(strategy.session_state()), strategy.decision_state()
    assert strategy.carries_view and held[0] != earlier[0] and held[1] != earlier[1]
    snapshot, decisions = earlier if state == "an-earlier-one" else held

    if through == "session-state":
        strategy.restore_session_state(snapshot)
    else:
        strategy.restore_decisions(decisions)

    assert strategy.carries_view is (state == "the-one-it-holds")


async def test_a_retry_from_an_earlier_snapshot_does_not_start_from_the_failed_attempt() -> None:
    """Rolled back on the same instance, a retry over the same rebuilt history is a pass of its own."""
    transcript = _transcript(14, 10, 6)
    strategy = _strategy(_Summarizer(), budget=_budget(transcript, 0.33), policy="shed")
    earlier = copy.deepcopy(strategy.session_state())
    first = await _compact(strategy, _Rebuilding(copy.deepcopy(transcript)).hand())
    assert strategy.carries_view

    strategy.restore_session_state(earlier)
    retry = await _compact(strategy, _Rebuilding(copy.deepcopy(transcript)).hand())

    assert strategy.calls_resumed == 0
    assert retry == first


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
