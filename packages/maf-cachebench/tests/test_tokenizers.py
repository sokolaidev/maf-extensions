"""The reasoning stamp counter.

The framework's ``_serialize_message`` excludes an opaque reasoning payload itself, so a
message carrying one counts as its visible text and nothing else. What the wrapper adds is
the stamp: the reasoning token count a run records on the content carrying the payload is
counted in the payload's place and the stamp itself is not. Every expected value is pinned to
``_serialize_message`` of a twin message, counted by the unwrapped tokenizer, so the tests
hold the wrapper to the framework's own serialization rather than to "smaller" or "larger".
"""

from __future__ import annotations

import base64
import contextlib
import random
from typing import Any

import pytest
from agent_framework import CharacterEstimatorTokenizer, Content, Message, included_token_count
from agent_framework._compaction import _serialize_message, annotate_token_counts

from maf_cachebench._tokenizers import (
    REASONING_TOKENS_KEY,
    TOKENIZER_NAMES,
    ReasoningStampTokenizer,
    TiktokenTokenizer,
    build_tokenizer,
    stamp_reasoning_tokens,
)

ESTIMATOR = CharacterEstimatorTokenizer()


def _blob(size: int = 1200, *, seed: int = 1) -> str:
    """Return base64 of ``size`` random bytes, the shape of an encrypted reasoning payload."""
    rng = random.Random(seed)
    return base64.b64encode(rng.randbytes(size)).decode()


def _reasoning_message(
    *blobs: str | None, stamps: dict[int, Any] | None = None, replay: dict[int, str] | None = None
) -> Message:
    """Return an assistant message with one reasoning content per entry, then visible text.

    A ``None`` entry is a reasoning content without a payload; ``stamps`` maps a content index
    to a value for :data:`REASONING_TOKENS_KEY`; ``replay`` maps one to the encrypted payload
    of a replayed reasoning item kept whole under ``additional_properties``, the way the
    Foundry client keeps it.
    """
    contents = [
        Content.from_text_reasoning(id=f"rs_{index}", text="", protected_data=blob)
        for index, blob in enumerate(blobs)
    ]
    for index, value in (stamps or {}).items():
        contents[index].additional_properties[REASONING_TOKENS_KEY] = value
    for index, blob in (replay or {}).items():
        contents[index].additional_properties["__foundry_reasoning_replay_item__"] = {
            "type": "reasoning",
            "id": f"rs_{index}",
            "response_id": "resp_1",
            "summary": [{"type": "summary_text", "text": "Weighed the two options."}],
            "content": [],
            "encrypted_content": blob,
        }
    # Non-ASCII on purpose: a re-serialization that escaped it would count the escapes.
    contents.append(
        Content.from_text(text="Réponse: the pipeline keeps the streaming group — 日本語 too.")
    )
    return Message("assistant", contents, message_id="m-1")


class _SpyTokenizer:
    """Estimator that records the strings it was asked to count."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def count_tokens(self, text: str) -> int:
        self.seen.append(text)
        return ESTIMATOR.count_tokens(text)


def _bases() -> list[Any]:
    """Return the counters to check, tiktoken only where it is installed; the estimator always runs."""
    bases: list[Any] = [CharacterEstimatorTokenizer()]
    with contextlib.suppress(RuntimeError):
        bases.append(TiktokenTokenizer())
    return bases


@pytest.mark.parametrize("base", _bases(), ids=lambda base: type(base).__name__)
def test_a_payload_counts_at_the_visible_text_size_not_the_payloads(
    base: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The framework leaves the opaque payload out of what it hands the tokenizer; the wrapper adds nothing back."""
    blob = _blob()
    text = _serialize_message(_reasoning_message(blob, _blob(seed=2)))
    without_blobs = _serialize_message(_reasoning_message(None, None))
    assert blob not in text
    # Not parsed: without a stamp there is nothing in the string for the wrapper to do.
    monkeypatch.setattr(
        "maf_cachebench._tokenizers.json.loads",
        lambda _: pytest.fail("parsed a message with no stamp"),
    )

    counted = ReasoningStampTokenizer(base).count_tokens(text)

    assert counted == base.count_tokens(without_blobs)
    # Both payloads went, not only the first: one payload alone costs more than the whole message now.
    assert counted < base.count_tokens(blob)


def test_counting_leaves_the_message_and_what_is_sent_untouched() -> None:
    blob = _blob()
    message = _reasoning_message(blob)
    before = _serialize_message(message)
    wrapped = ReasoningStampTokenizer(ESTIMATOR)

    annotate_token_counts([message], tokenizer=wrapped)

    assert message.contents[0].protected_data == blob
    assert _serialize_message(message) == before
    # The framework read back the corrected count, not the payload-inclusive one.
    assert included_token_count([message]) == ESTIMATOR.count_tokens(
        _serialize_message(_reasoning_message(None))
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('"reasoning_output_token_count" is a phrase here, not a field', id="prose"),
        pytest.param('["reasoning_output_token_count", 1]', id="json-list"),
        pytest.param('{"reasoning_output_token_count": 300}', id="json-without-contents"),
        pytest.param(
            '{"contents": {"reasoning_output_token_count": 300}}', id="contents-not-a-list"
        ),
        pytest.param(
            '{"contents": [{"arguments": {"reasoning_output_token_count": 300}, "type": "function_call"}], '
            '"role": "assistant"}',
            id="nested-in-arguments",
        ),
    ],
)
def test_strings_that_are_not_a_message_with_a_stamp_count_unchanged(text: str) -> None:
    spy = _SpyTokenizer()

    assert ReasoningStampTokenizer(spy).count_tokens(text) == ESTIMATOR.count_tokens(text)
    assert spy.seen == [text]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            Message(
                "user", [Content.from_text(text="Keep the RQ-AAA requirement.")], message_id="u"
            ),
            id="user",
        ),
        pytest.param(
            Message(
                "tool",
                [Content.from_function_result(call_id="c1", result="rows: 1, 2, 3")],
                message_id="t",
            ),
            id="tool-result",
        ),
        pytest.param(
            Message(
                "assistant",
                [Content.from_text(text='It said "reasoning_output_token_count" in the doc.')],
                message_id="a",
            ),
            id="assistant-quoting-the-field-name",
        ),
        pytest.param(_reasoning_message(None), id="reasoning-without-payload"),
        pytest.param(_reasoning_message(_blob()), id="payload-without-stamp"),
    ],
)
def test_a_message_without_a_stamp_counts_exactly_as_before(
    message: Message, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _serialize_message(message)
    spy = _SpyTokenizer()
    monkeypatch.setattr(
        "maf_cachebench._tokenizers.json.loads",
        lambda _: pytest.fail("parsed a message with no stamp"),
    )

    assert ReasoningStampTokenizer(spy).count_tokens(text) == ESTIMATOR.count_tokens(text)
    # The very same string object reached the wrapped tokenizer: no re-serialization happened.
    assert spy.seen[0] is text


def test_the_framework_charges_nothing_for_the_payload_and_the_wrapper_charges_the_stamp() -> None:
    """An opaque payload costs zero under the framework's own count; only a stamp changes that.

    Base64 tokenizes at about 0.68 o200k tokens per character, three to four times what the
    provider bills for the reasoning it encrypts, which is what counting the payload as text
    would charge. The framework excludes it, and the stamp is what repays the provider's
    actual bill in its place.
    """
    pytest.importorskip("tiktoken", reason="tiktoken not installed")
    raw = TiktokenTokenizer()
    wrapped = ReasoningStampTokenizer(raw)
    blob = _blob()
    with_blob = _serialize_message(_reasoning_message(blob))
    without_blob = _serialize_message(_reasoning_message(None))
    stamped = _serialize_message(_reasoning_message(blob, stamps={0: 300}))

    assert raw.count_tokens(with_blob) == raw.count_tokens(without_blob)
    assert wrapped.count_tokens(with_blob) == raw.count_tokens(without_blob)
    assert wrapped.count_tokens(stamped) == raw.count_tokens(without_blob) + 300
    assert raw.count_tokens(blob) > 0.6 * len(blob)


def test_a_stamped_reasoning_count_is_counted_in_the_payloads_place() -> None:
    wrapped = ReasoningStampTokenizer(ESTIMATOR)
    plain = ESTIMATOR.count_tokens(_serialize_message(_reasoning_message(None, None)))

    stamped = _serialize_message(_reasoning_message(_blob(), _blob(seed=2), stamps={0: 300}))
    assert wrapped.count_tokens(stamped) == plain + 300

    # Stamps on several payloads add up; the stamps themselves are never counted.
    split = _serialize_message(_reasoning_message(_blob(), _blob(seed=2), stamps={0: 200, 1: 100}))
    assert wrapped.count_tokens(split) == plain + 300

    # A stamp that is not a positive integer is dropped, not counted.
    for value in (True, -5, "300", 2.5):
        odd = _serialize_message(_reasoning_message(_blob(), _blob(seed=2), stamps={0: value}))
        assert wrapped.count_tokens(odd) == plain, value


def test_stamp_reasoning_tokens_marks_the_first_payload_only() -> None:
    message = _reasoning_message(None, _blob(), _blob(seed=2))
    other = Message("assistant", [Content.from_text(text="no reasoning here")], message_id="o")

    assert stamp_reasoning_tokens([other, message], 312) is True
    assert REASONING_TOKENS_KEY not in message.contents[0].additional_properties
    assert message.contents[1].additional_properties[REASONING_TOKENS_KEY] == 312
    assert REASONING_TOKENS_KEY not in message.contents[2].additional_properties
    assert REASONING_TOKENS_KEY not in other.contents[0].additional_properties
    # The payload is still there to be replayed; only the count moved.
    assert message.contents[1].protected_data == _blob()

    assert stamp_reasoning_tokens([other], 312) is False
    assert stamp_reasoning_tokens([message], 0) is False


def test_build_tokenizer_wraps_every_name_and_keeps_the_names() -> None:
    assert TOKENIZER_NAMES == ("estimator", "tiktoken")
    for name in TOKENIZER_NAMES:
        if name == "tiktoken":
            pytest.importorskip("tiktoken", reason="tiktoken not installed")
        tokenizer = build_tokenizer(name)
        assert isinstance(tokenizer, ReasoningStampTokenizer)
        assert isinstance(
            tokenizer.base,
            CharacterEstimatorTokenizer if name == "estimator" else TiktokenTokenizer,
        )
    with pytest.raises(KeyError):
        build_tokenizer("bpe")


@pytest.mark.parametrize("base", _bases(), ids=lambda base: type(base).__name__)
def test_a_replayed_items_nested_payload_is_not_counted_but_its_summary_is(base: Any) -> None:
    """The Foundry client keeps the whole replayed reasoning item under additional_properties.

    The framework strips ``encrypted_content`` at the top level of those properties and nothing
    below, so the payload inside the kept item is counted as prompt text: 1,128 tokens for a
    reply billed at about 110. The wrapper drops the member at any depth and leaves the item's
    clear-text summary, which the provider does send, counted as the framework counts it.
    """
    blob = _blob()
    with_replay = _serialize_message(_reasoning_message(blob, replay={0: blob}))
    assert blob in with_replay, "the framework counts the nested payload; the premise of this test"
    twin = _reasoning_message(blob, replay={0: blob})
    del twin.contents[0].additional_properties["__foundry_reasoning_replay_item__"][
        "encrypted_content"
    ]
    expected = base.count_tokens(_serialize_message(twin))

    counted = ReasoningStampTokenizer(base).count_tokens(with_replay)

    assert counted == expected
    assert counted < base.count_tokens(blob)
    assert "Weighed the two options." in _serialize_message(twin)


def test_a_stamp_and_a_nested_payload_settle_together() -> None:
    blob = _blob()
    message = _reasoning_message(blob, stamps={0: 300}, replay={0: blob})
    twin = _reasoning_message(blob, replay={0: blob})
    del twin.contents[0].additional_properties["__foundry_reasoning_replay_item__"][
        "encrypted_content"
    ]

    assert ReasoningStampTokenizer(ESTIMATOR).count_tokens(_serialize_message(message)) == (
        ESTIMATOR.count_tokens(_serialize_message(twin)) + 300
    )


@pytest.mark.parametrize("reasoning", [False, True])
def test_encrypted_application_metadata_remains_counted(reasoning: bool) -> None:
    content = (
        Content.from_text_reasoning(text="thinking")
        if reasoning
        else Content.from_text(text="tool data")
    )
    content.additional_properties["application"] = {"encrypted_content": _blob()}
    message = Message("assistant", [content])
    text = _serialize_message(message)
    assert ReasoningStampTokenizer(ESTIMATOR).count_tokens(text) == ESTIMATOR.count_tokens(text)


def test_foundry_shaped_metadata_on_nonreasoning_content_remains_counted() -> None:
    content = Content.from_text(text="tool data")
    content.additional_properties["__foundry_reasoning_replay_item__"] = {
        "type": "reasoning",
        "encrypted_content": _blob(),
    }
    text = _serialize_message(Message("assistant", [content]))
    assert ReasoningStampTokenizer(ESTIMATOR).count_tokens(text) == ESTIMATOR.count_tokens(text)
