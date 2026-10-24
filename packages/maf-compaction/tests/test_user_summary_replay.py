"""Accepted user summaries survive hosts that resend original history."""

import json
from copy import deepcopy

import pytest
from agent_framework import CharacterEstimatorTokenizer, ChatResponse, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
    set_excluded,
)

from maf_compaction import (
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    UserTurnAnchoredSummarizationCompactionStrategy,
)
from maf_compaction._preserve import set_preserved
from maf_compaction._summary_input import summary_request

pytestmark = pytest.mark.anyio
TOKENIZER = CharacterEstimatorTokenizer()


class Client:
    def __init__(self):
        self.requests = []

    async def get_response(self, messages, **kwargs):
        self.requests.append(messages)
        return ChatResponse(messages=Message("assistant", ["Earlier facts."]))


def strategy(client, mode="boundary", **kwargs):
    return UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=3_000,
        tokenizer=TOKENIZER,
        client=client,
        max_summary_input_tokens=20_000,
        summary_mode=mode,
        remembered_requests=2,
        **kwargs,
    )


def history():
    return [Message("user", [f"Turn {i}: " + "x" * 4_000], message_id=f"u{i}") for i in range(6)]


def visible(messages):
    return [(message.message_id, message.text) for message in project_included_messages(messages)]


@pytest.mark.parametrize("mode", ["boundary", "fold", "recompact"])
@pytest.mark.parametrize("composed", [False, True])
async def test_original_history_with_new_tail_replays_after_json_restore(mode, composed):
    client = Client()
    first = strategy(client, mode)
    original = history()
    compacted = deepcopy(original)
    assert await first(compacted)
    state = json.loads(json.dumps(first.session_state()))
    state["remembered"] = []
    restored = strategy(client, mode)
    restored.restore_session_state(state)
    run = restored
    if composed:
        run = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
            tokenizer=TOKENIZER,
            tool_results=ToolResultAnchoredSummarizationCompactionStrategy(
                max_input_tokens=3_000,
                tokenizer=TOKENIZER,
            ),
            user_turns=restored,
            user_trigger_fraction=0.8,
        )
    newer = Message("user", ["Thanks."], message_id="new")
    resent = original + [newer]
    assert await run(resent)
    assert len(client.requests) == 1
    assert restored.user_summaries_replayed == 1
    assert visible(resent) == visible(compacted) + [(newer.message_id, newer.text)]


@pytest.mark.parametrize(
    "change", ["edit", "remove", "reorder", "insert", "preserve", "exclude", "id"]
)
async def test_changed_source_window_is_not_replayed(change):
    client = Client()
    subject = strategy(client)
    original = history()
    assert await subject(deepcopy(original))
    if change == "edit":
        original[2].contents = Message("user", ["Edited requirement."]).contents
    elif change == "remove":
        del original[2]
    elif change == "reorder":
        original[2], original[3] = original[3], original[2]
    elif change == "insert":
        original.insert(2, Message("user", ["New requirement."], message_id="inserted"))
    elif change == "preserve":
        set_preserved(original[2], preserved=True, reason="host")
    elif change == "exclude":
        set_excluded(original[2], excluded=True, reason="host")
    else:
        original[2].message_id = "different-source"
    subject.max_input_tokens = 100_000
    before = visible(original)
    assert not await subject(original)
    assert visible(original) == before
    assert subject.user_summaries_replayed == 0
    assert len(client.requests) == 1


async def test_replay_below_trigger_during_cooldown_is_idempotent():
    client = Client()
    subject = strategy(client, max_consecutive_summary_failures=1, summary_backoff_seconds=60)
    original = history()
    assert await subject(deepcopy(original))
    subject.max_input_tokens = 100_000
    subject._summary_failed()
    state = subject.session_state()
    assert await subject(original)
    assert subject.session_state()["summary_backoff_until"] == state["summary_backoff_until"]
    assert not await subject(original)
    assert not await subject(project_included_messages(original))
    assert len(client.requests) == 1
    assert subject.user_summaries_replayed == 1


@pytest.mark.parametrize("mode", ["boundary", "fold", "recompact"])
async def test_repeated_growth_summarizes_only_new_sources_and_replays_all_edits(mode):
    client = Client()
    subject = strategy(client, mode)
    original = history()
    for round_number in range(3):
        current = deepcopy(original)
        assert await subject(current)
        assert len(client.requests) == round_number + 1
        if round_number:
            assert "Turn 1:" not in client.requests[-1][-1].text
        state = json.loads(json.dumps(subject.session_state()))
        subject = strategy(client, mode)
        subject.restore_session_state(state)
        resent = deepcopy(original)
        assert await subject(resent)
        assert visible(resent) == visible(current)
        assert len(client.requests) == round_number + 1
        original.extend(
            Message(
                "user",
                [f"Growth {round_number}/{i}: " + "g" * 4_000],
                message_id=f"g{round_number}-{i}",
            )
            for i in range(3)
        )


async def test_accepted_fold_replays_after_its_original_bands():
    client = Client()
    subject = strategy(client, "fold")
    original = history()
    compacted = deepcopy(original)
    assert await subject(compacted)
    growth = [Message("user", ["g" * 4_000], message_id=f"g{i}") for i in range(3)]
    original.extend(deepcopy(growth))
    compacted.extend(growth)
    assert await subject(compacted)
    assert await subject.fold_if_smaller(compacted) == "folded"
    asked = len(client.requests)
    state = json.loads(json.dumps(subject.session_state()))
    restored = strategy(client, "fold")
    restored.restore_session_state(state)
    assert await restored(original)
    assert visible(original) == visible(compacted)
    assert len(client.requests) == asked
    assert restored.user_summaries_replayed == 3
    assert not restored.refold(original)


async def test_legacy_snapshot_without_replacements_restores():
    client = Client()
    subject = strategy(client)
    original = history()
    assert await subject(deepcopy(original))
    state = subject.session_state()
    del state["replacements"]
    restored = strategy(client)
    restored.restore_session_state(state)
    assert await restored(original)
    assert len(client.requests) == 1


async def test_short_final_chunk_is_kept_and_merged_without_failure():
    class ChunkClient(Client):
        async def get_response(self, messages, **kwargs):
            self.requests.append(messages)
            text = messages[-1].text
            answer = "A verbose answer that cannot shrink OK." if text == "3. OK" else "Facts."
            return ChatResponse(messages=Message("assistant", [answer]))

    client = ChunkClient()
    largest_chunk = summary_request("Summarize.", "2. " + "y" * 255)
    annotate_token_counts(largest_chunk, tokenizer=TOKENIZER)
    subject = UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=200,
        trigger_fraction=0.01,
        tokenizer=TOKENIZER,
        client=client,
        prompt="Summarize.",
        fold_prompt="Merge.",
        summary_input_policy="chunk",
        max_summary_input_tokens=included_token_count(largest_chunk),
    )
    messages = [
        Message("user", [text], message_id=str(index))
        for index, text in enumerate(["head", "x" * 255, "y" * 255, "OK", "tail"])
    ]
    assert await subject(messages)
    assert any(request[-1].text == "3. OK" for request in client.requests)
    assert "OK" in client.requests[-1][-1].text
    assert subject.user_summary_failures == 0
    assert subject.user_compactions == 1
    assert any(message.additional_properties.get(EXCLUDED_KEY) for message in messages)
