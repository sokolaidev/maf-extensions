"""Tests for the anchored compaction strategy.

The behaviour that matters is not "does it shrink the prompt" -- every strategy does that.
It is whether the decisions it makes on turn N survive unchanged into turn N+1, because
that is the only thing separating it from the strategies already measured costing more than
not compacting at all.
"""

from __future__ import annotations

import pytest
from agent_framework import CharacterEstimatorTokenizer, Message
from agent_framework._compaction import (
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    project_included_messages,
)

from maf_compaction._anchored import (
    DEFAULT_KEEP_TOKENS,
    DEFAULT_MIN_GAIN_FRACTION,
    REMOVAL_MARKER,
    AnchoredCompactionStrategy,
    MinimumGainAnchoredCompactionStrategy,
)
from maf_compaction._preserve import set_preserved

pytestmark = pytest.mark.anyio

TOKENIZER = CharacterEstimatorTokenizer()


def _tool_turn(index: int, payload_chars: int = 8_000) -> list[Message]:
    """Return one tool-call group plus the assistant reply that follows it."""
    call_id = f"call_{index}"
    return [
        Message(
            role="assistant",
            contents=[
                {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"}
            ],
            message_id=f"a_call_{index}",
        ),
        Message(
            role="tool",
            contents=[
                {
                    "type": "function_result",
                    "call_id": call_id,
                    "result": f"R{index} " + "x" * payload_chars,
                }
            ],
            message_id=f"t_res_{index}",
        ),
        Message(role="assistant", contents=[f"I looked up {index}."], message_id=f"a_txt_{index}"),
    ]


def _conversation(
    tool_turns: int, payload_chars: int = 8_000, *, oversized: dict[int, int] | None = None
) -> list[Message]:
    """Return a conversation with a stable head and ``tool_turns`` tool groups.

    Keyword Args:
        oversized: Payload sizes in characters for individual tool turns, overriding
            ``payload_chars``. Lets a test put the one result a budget bites on where it wants
            it, rather than making every result the same size and every edit start at the
            front of the band.
    """
    messages = [
        Message(role="system", contents=["You are an assistant."], message_id="sys"),
        Message(role="user", contents=["Requirement: region is EU-WEST-1."], message_id="u0"),
        Message(role="assistant", contents=["Understood."], message_id="a0"),
    ]
    for index in range(tool_turns):
        messages.append(
            Message(role="user", contents=[f"Look up {index}."], message_id=f"u_{index}")
        )
        messages.extend(_tool_turn(index, (oversized or {}).get(index, payload_chars)))
    return messages


def _text_of(message: Message) -> str:
    """Return a message's payload as the model would see it, tool results included."""
    parts: list[str] = []
    for content in message.contents:
        result = getattr(content, "result", None)
        text = getattr(content, "text", None)
        parts.append(
            str(result) if result is not None else (text if text is not None else str(content))
        )
    return "".join(parts)


def _rendered(messages: list[Message]) -> str:
    """Return what the model would actually receive, as one string."""
    return "\n".join(f"{m.role}:{_text_of(m)}" for m in project_included_messages(messages))


def _fingerprint(messages: list[Message]) -> list[tuple[str, str | None, str, str]]:
    """Return everything a compaction pass could have touched, message by message.

    Rendering is not enough to prove a pass did nothing. Exclusion is recorded in
    ``additional_properties`` and so are the cached token counts, and a strategy that flagged
    a message and then failed to unflag it would render identically while having changed what
    the next pass sees.
    """
    return [(m.role, m.message_id, _text_of(m), repr(m.additional_properties)) for m in messages]


def _annotated(tool_turns: int, payload_chars: int = 8_000) -> list[Message]:
    """Return a conversation already carrying the annotations a compaction pass writes first.

    Written so that a test about mutation does not read the bookkeeping every strategy does on
    entry as though it were the strategy acting.
    """
    messages = _conversation(tool_turns, payload_chars)
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER)
    return messages


async def test_head_and_tail_anchors_are_never_touched() -> None:
    """The requirement stated at the start must survive, however long the conversation runs."""
    strategy = AnchoredCompactionStrategy(max_input_tokens=1_000, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)

    await strategy(messages)
    rendered = _rendered(messages)

    assert "EU-WEST-1" in rendered
    # The most recent tool result is working context and is kept verbatim.
    assert "R7 " + "x" * 100 in rendered


async def test_the_ceiling_is_met() -> None:
    """A token-aware strategy that leaves the prompt over its ceiling has failed at its job."""
    strategy = AnchoredCompactionStrategy(max_input_tokens=4_000, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=10)

    await strategy(messages)

    kept = project_included_messages(messages)
    total = sum(TOKENIZER.count_tokens("".join(str(c) for c in m.contents)) for m in kept)
    assert total <= 4_000


async def test_decisions_are_frozen_as_the_conversation_grows() -> None:
    """A group compacted at turn N must look identical at turn N+1.

    This is the whole design. Prompt caching is strict-prefix, so a strategy that re-decides
    the fate of an old group -- as any "compact to 50% when over 80%" rule does -- rewrites
    the start of the prompt and re-bills everything after it. Here the prefix that both turns
    share must come out byte-identical.

    Run at a ceiling the strategy is actually configured for, and with results large enough to
    be cut well clear of ``DEFAULT_KEEP_TOKENS``: if every per-result budget clamped to that
    floor, both conversations would trim to the same length whatever rule produced it, and the
    test would pass against retention that moves with the band's width.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=117_952, tokenizer=TOKENIZER)

    earlier = _conversation(tool_turns=8, payload_chars=40_000)
    await strategy(earlier)
    earlier_rendered = _rendered(earlier)

    # The same conversation two tool turns later, compacted from scratch as the before-phase
    # always does: exclusion flags do not survive into storage.
    later = _conversation(tool_turns=10, payload_chars=40_000)
    await strategy(later)

    assert REMOVAL_MARKER in earlier_rendered, (
        "the fixture has to give the collapse something to freeze"
    )
    # Shortening alone has to reach the ceiling here, or the comparison below would be pinning
    # the shed step's holes rather than the retention rule.
    assert "[compacted: an earlier tool call and its result]" not in earlier_rendered

    # Tool turns 0-6 are in the middle band of both conversations: the band only ever grows
    # from the tail end, so a group that has entered it never leaves. Each must render
    # identically. Turn 7 is the tail's at eight turns and the band's at ten, so it is not a
    # group whose fate was already settled.
    earlier_by_id = {m.message_id: _text_of(m) for m in project_included_messages(earlier)}
    later_by_id = {m.message_id: _text_of(m) for m in project_included_messages(later)}
    for index in range(7):
        for message_id in (f"a_call_{index}", f"t_res_{index}", f"a_txt_{index}"):
            kept_earlier, kept_later = earlier_by_id.get(message_id), later_by_id.get(message_id)
            assert kept_earlier is not None and kept_later is not None, message_id
            assert len(kept_earlier) == len(kept_later), (
                f"{message_id} kept {len(kept_earlier):,} characters at eight tool turns and "
                f"{len(kept_later):,} at ten: retention moved with the band's width"
            )
            assert kept_earlier == kept_later, message_id

    # And the surviving slices are two orders of magnitude clear of the floor, so no part of
    # the agreement above comes from both sides clamping to the same constant.
    assert TOKENIZER.count_tokens(later_by_id["t_res_6"]) > 10 * DEFAULT_KEEP_TOKENS


async def test_running_twice_changes_nothing_further() -> None:
    """Idempotence. A second pass that shortens an already-shortened result would compound."""
    strategy = AnchoredCompactionStrategy(max_input_tokens=3_000, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)

    await strategy(messages)
    once = _rendered(messages)
    changed_again = await strategy(messages)

    assert _rendered(messages) == once
    assert changed_again is False


async def test_shortening_alone_is_preferred_to_removing_anything() -> None:
    """When trimming results is enough, nothing is dropped and the structure stays intact.

    This is the cheap case and it should be the common one: the model still sees that every
    call happened and roughly what each returned, and no message is missing.

    The ceiling has to be roomy for that to be reachable. Retaining a share of the ceiling per
    band *position* rather than splitting one share across the band's current width keeps more
    in total -- ``band_share`` times the harmonic number of the band's tool groups rather than
    ``band_share`` -- so the window in which trimming alone suffices starts higher than it did.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=10_000, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)

    await strategy(messages)
    rendered = _rendered(messages)

    assert REMOVAL_MARKER in rendered
    assert "[compacted: an earlier tool call and its result]" not in rendered
    assert "[compacted: an earlier assistant reply]" not in rendered
    # Every reply is still there, so nothing the model said about a result was lost.
    assert all(f"I looked up {index}." in rendered for index in range(8))


async def test_assistant_narration_is_the_last_thing_dropped() -> None:
    """Under a ceiling too tight for trimming, tool groups go before any prose does."""
    strategy = AnchoredCompactionStrategy(max_input_tokens=120, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)

    await strategy(messages)
    rendered = _rendered(messages)

    assert "[compacted: an earlier tool call and its result]" in rendered


async def test_collapse_assistant_text_can_be_forbidden() -> None:
    """The last-resort step must be switchable, so its cost can be measured separately."""
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=500, tokenizer=TOKENIZER, collapse_assistant_text=False
    )
    messages = _conversation(tool_turns=8)

    await strategy(messages)

    assert "[compacted: an earlier assistant reply]" not in _rendered(messages)


async def test_short_conversations_are_left_alone() -> None:
    """With nothing between the anchors there is nothing to compact, and no cache to spend."""
    strategy = AnchoredCompactionStrategy(max_input_tokens=10, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=1)
    before = _rendered(messages)

    assert await strategy(messages) is False
    assert _rendered(messages) == before


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_input_tokens": 0}, "max_input_tokens"),
        ({"max_input_tokens": 100, "keep_head_groups": -1}, "keep_head_groups"),
        ({"max_input_tokens": 100, "keep_tokens": -1}, "keep_tokens"),
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, int], match: str) -> None:
    """A silently accepted bad bound would produce a plausible-looking wrong measurement."""
    with pytest.raises(ValueError, match=match):
        AnchoredCompactionStrategy(tokenizer=TOKENIZER, **kwargs)


async def test_the_ceiling_is_best_effort_when_the_anchors_alone_exceed_it() -> None:
    """The anchors are inviolable, so an impossible ceiling is missed rather than obeyed.

    One tool result in the tail is larger than the whole ceiling here. No strategy can split
    a single result, so the honest behaviour is to shed everything it is allowed to shed and
    stop -- not to start eating the working set or the requirements to chase a number it
    cannot reach.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=120, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)

    await strategy(messages)
    rendered = _rendered(messages)

    assert included_token_count(messages) > 120
    # What it was allowed to shed, it shed.
    assert "[compacted: an earlier tool call and its result]" in rendered
    assert "[compacted: an earlier assistant reply]" in rendered
    # What it was not allowed to touch is untouched.
    assert "EU-WEST-1" in rendered


async def test_retention_scales_with_the_ceiling() -> None:
    """A fixed retention becomes a rounding error as tool results grow.

    Measured: a 600-character retention is 1.9% of an 8,000-token result and 0.6% of a
    25,200-token one. The strategy scored 32 of 53 planted facts at the first size and 11 at
    the second, and 11 was exactly the five non-tool facts plus the single code that fell
    inside each surviving head fragment. The band budget now scales with the window instead.
    """
    small = AnchoredCompactionStrategy(max_input_tokens=57_952, tokenizer=TOKENIZER)
    large = AnchoredCompactionStrategy(max_input_tokens=269_952, tokenizer=TOKENIZER)

    assert large._keep_tokens_for(5) > 4 * small._keep_tokens_for(5)
    # And an explicit value still wins, so a caller can pin it for a comparison.
    pinned = AnchoredCompactionStrategy(
        max_input_tokens=269_952, tokenizer=TOKENIZER, keep_tokens=600
    )
    assert pinned._keep_tokens_for(5) == 600


async def test_a_wider_band_share_keeps_more_of_each_result() -> None:
    """The knob has to move the trade-off, or it is decoration."""
    narrow = AnchoredCompactionStrategy(
        max_input_tokens=269_952, tokenizer=TOKENIZER, band_share=0.05
    )
    wide = AnchoredCompactionStrategy(max_input_tokens=269_952, tokenizer=TOKENIZER, band_share=0.5)

    assert wide._keep_tokens_for(5) > narrow._keep_tokens_for(5)


async def test_retention_is_fixed_by_position_not_by_the_bands_width() -> None:
    """A result's allowance has to be a number its own place in the band settles, once.

    The rule this replaced divided one band-wide share by the tool groups in the band *at that
    moment*, and :meth:`_shorten` refuses to re-trim a result already carrying the marker. A
    result therefore froze at whatever share was in force on the turn the trim happened to
    fire: at this ceiling, 29,488 tokens if the trim caught it alone in the band and 4,914 if
    it caught it sixth -- six times the retention for nothing but arrival order, and a decision
    that moves with the conversation's current size, which this module's first design
    constraint forbids outright.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=117_952, tokenizer=TOKENIZER)

    assert strategy._keep_tokens_for(0) == 29_488
    assert strategy._keep_tokens_for(5) == 4_914
    # And never below the floor, however deep into the band the result sits.
    assert strategy._keep_tokens_for(10_000) == DEFAULT_KEEP_TOKENS


def test_band_share_is_validated() -> None:
    """A share outside (0, 1] would silently produce a nonsensical budget."""
    with pytest.raises(ValueError, match="band_share"):
        AnchoredCompactionStrategy(max_input_tokens=1_000, tokenizer=TOKENIZER, band_share=0.0)
    with pytest.raises(ValueError, match="band_share"):
        AnchoredCompactionStrategy(max_input_tokens=1_000, tokenizer=TOKENIZER, band_share=1.5)


# region minimum gain


async def test_a_collapse_below_the_floor_leaves_the_conversation_untouched() -> None:
    """An edit worth less than the cache it breaks is refused.

    Without the floor, a 60,000-token window at 0.86 fill holds a lower prompt-cache hit rate
    than no compaction at all, 89-93% against 91-95%. The geometry is reproduced here -- a band budget nearly as large as the result it is trimming
    -- and the only correct action is none at all. Not a smaller edit: none, because the cache
    is spent on editing at a position rather than on how much was edited there.
    """
    strategy = MinimumGainAnchoredCompactionStrategy(max_input_tokens=50_000, tokenizer=TOKENIZER)
    messages = _annotated(tool_turns=8)
    before = _fingerprint(messages)

    assert await strategy(messages) is False
    assert _fingerprint(messages) == before
    assert strategy.declined_collapses == 1


async def test_the_floor_is_measured_against_the_tokens_the_edit_re_bills() -> None:
    """The base is the suffix behind the edit, not the prompt, and the two are not the same.

    A strict-prefix cache is spent on everything from the earliest rewrite to the end; what
    sits in front of it stays cached and is never paid for again. That suffix is the ``B`` of
    ``R > B(p - c) / (p + T·c)``. Charging a collapse for the whole prompt instead overstates
    its cost by ``prompt / B`` -- barely visible for a collapse that begins at the head of the
    band, and fatal for the case the strategy meets most often, one group that has just aged
    out of the tail, where the edit is near the end and ``B`` is a fraction of the prompt.

    Here the only result the band's budget bites on is the last one in the band. The saving is
    over half of what the edit re-bills and comfortably repays it, and under a quarter of the
    prompt, so the prompt-based test would have refused it.
    """
    strategy = MinimumGainAnchoredCompactionStrategy(max_input_tokens=100_000, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8, payload_chars=14_400, oversized={6: 48_000})
    annotate_message_groups(messages)
    annotate_token_counts(messages, tokenizer=TOKENIZER)

    plan = strategy._plan_shortenings(
        messages, strategy._middle_band(messages, group_messages(messages))
    )
    saved = sum(item.saved_tokens for item in plan)
    behind = included_token_count(messages[min(item.message_index for item in plan) :])

    assert len(plan) == 1, "the fixture has to put one edit near the end of the band"
    assert saved < included_token_count(messages) * DEFAULT_MIN_GAIN_FRACTION
    assert saved > behind * DEFAULT_MIN_GAIN_FRACTION

    assert await strategy(messages) is True
    assert strategy.declined_collapses == 0


async def test_above_the_floor_it_is_the_anchored_strategy() -> None:
    """The floor is the only difference between the two rows, so above it there is none.

    That is what makes the pair a measurement: any gap between the rows in a run has to come
    from the collapses this one declined, and not from a second change riding along with it.
    """
    floored = MinimumGainAnchoredCompactionStrategy(max_input_tokens=20_000, tokenizer=TOKENIZER)
    plain = AnchoredCompactionStrategy(max_input_tokens=20_000, tokenizer=TOKENIZER)
    with_floor, without = _conversation(tool_turns=8), _conversation(tool_turns=8)

    assert await floored(with_floor) is True
    assert await plain(without) is True

    assert _rendered(with_floor) == _rendered(without)
    assert floored.declined_collapses == 0
    assert REMOVAL_MARKER in _rendered(with_floor)


async def test_the_projection_is_what_the_collapse_actually_removes() -> None:
    """The floor is only as good as the number it is compared against.

    A projection computed one way and a collapse performed another would drift apart silently,
    and the row would then be measuring neither. They share ``_plan_shortenings`` for exactly
    that reason. The residual is the JSON envelope each message is counted inside, which the
    rewrite does not change: about a token per shortened result.
    """
    strategy = MinimumGainAnchoredCompactionStrategy(max_input_tokens=20_000, tokenizer=TOKENIZER)
    messages = _annotated(tool_turns=8)

    band = strategy._middle_band(messages, group_messages(messages))
    plan = strategy._plan_shortenings(messages, band)
    projected = sum(item.saved_tokens for item in plan)
    before = included_token_count(messages)

    assert plan, "the fixture has to give the collapse something to do"
    assert await strategy(messages) is True

    assert projected == pytest.approx(before - included_token_count(messages), rel=0.01)


async def test_an_overflow_is_never_declined() -> None:
    """A prompt that will not fit is not a cost question, and treating it as one disqualifies.

    Below the ceiling a collapse is judged on whether its saving repays the cache it spends.
    At or above it the same collapse is the cheapest way left to make the conversation
    admissible, and refusing it would only hand the work to the shed step, which removes whole
    groups instead of trimming them. A pinned ``keep_tokens`` puts the projected gain far
    under the floor here, so the floor is what would otherwise have refused.
    """
    floored = MinimumGainAnchoredCompactionStrategy(
        max_input_tokens=12_000, tokenizer=TOKENIZER, keep_tokens=1_900
    )
    plain = AnchoredCompactionStrategy(
        max_input_tokens=12_000, tokenizer=TOKENIZER, keep_tokens=1_900
    )
    with_floor, without = _conversation(tool_turns=8), _conversation(tool_turns=8)

    await floored(with_floor)
    await plain(without)

    assert floored.declined_collapses == 0
    # Shortening alone could not reach the ceiling, so groups had to go, and they did.
    assert "[compacted: an earlier tool call and its result]" in _rendered(with_floor)
    assert included_token_count(with_floor) <= 12_000
    assert _rendered(with_floor) == _rendered(without)


async def test_a_zero_floor_is_the_unfloored_strategy() -> None:
    """The knob has to reach both ends, or the pair cannot be run as a single-variable test."""
    strategy = MinimumGainAnchoredCompactionStrategy(
        max_input_tokens=50_000, tokenizer=TOKENIZER, min_gain_fraction=0.0
    )
    plain = AnchoredCompactionStrategy(max_input_tokens=50_000, tokenizer=TOKENIZER)
    with_floor, without = _conversation(tool_turns=8), _conversation(tool_turns=8)

    assert await strategy(with_floor) is True
    assert await plain(without) is True

    assert _rendered(with_floor) == _rendered(without)
    assert strategy.declined_collapses == 0


async def test_every_declined_pass_is_counted() -> None:
    """One refusal per pass, so a row that declined all run long says how many times.

    A single flag would not distinguish a strategy that declined once early from one that
    declined on every turn, and those are different findings about the floor. One instance
    against three loads of the history, because that is how the framework runs it: compaction
    is re-applied to a freshly loaded conversation on every turn, and exclusion flags do not
    survive into storage.
    """
    strategy = MinimumGainAnchoredCompactionStrategy(max_input_tokens=50_000, tokenizer=TOKENIZER)

    for _ in range(3):
        assert await strategy(_conversation(tool_turns=8)) is False

    assert strategy.declined_collapses == 3


def test_the_default_floor_is_the_break_even_it_claims_to_be() -> None:
    """The default is derived, and this is the derivation.

    ``T * R * c > (B - R) * p - B * c`` rearranges to ``R > B * (p - c) / (p + T * c)``, whose
    right-hand side is a share of the tokens behind the edit and of nothing else. At the
    measured prices -- 0.66 and 0.07 per million -- with twenty turns left that share is 0.286,
    and the constant is it rounded up to the next percent, so the floor is never below the
    break-even it comes from.

    The constant it replaces, 0.23, was this same share multiplied by the ratio of tokens-
    behind to prompt at the single cell it was fitted to -- about 40,000 against 52,322 -- and
    then compared against the prompt. Both roads land near 0.22 at that geometry and nowhere
    else, which is why the base and not the constant was the defect.

    Pinned as a test because the number is the whole argument for the row existing: if the
    arithmetic is revised and the constant does not follow it, the strategy declines edits it
    should make or makes edits it should decline, and either way the run measures nothing.
    """
    price_input, price_cached = 0.66, 0.07
    turns = 20

    break_even = (price_input - price_cached) / (price_input + turns * price_cached)

    assert break_even == pytest.approx(0.2864, abs=0.0001)
    assert break_even <= DEFAULT_MIN_GAIN_FRACTION < break_even + 0.01
    assert break_even * (40_000 / 52_322) == pytest.approx(0.219, abs=0.001)


@pytest.mark.parametrize("fraction", [-0.1, 1.0, 1.5])
def test_an_unusable_floor_is_rejected(fraction: float) -> None:
    """A floor of a whole prompt can never be met, so the strategy would silently never act."""
    with pytest.raises(ValueError, match="min_gain_fraction"):
        MinimumGainAnchoredCompactionStrategy(
            max_input_tokens=1_000, tokenizer=TOKENIZER, min_gain_fraction=fraction
        )


def test_no_band_share_makes_a_small_payload_worth_trimming() -> None:
    """Tuning the dial cannot rescue a payload the break-even is larger than.

    The default leaves 3,500-token results untouched at a 117,952-token ceiling, because the
    oldest banded result may keep 29,488. That looks like a mis-configuration, and the flag now
    exists to test it -- but the arithmetic says it is the regime, not the setting: the whole
    tool payload is 21,000 tokens in a 103,200-token prompt, and the edit re-bills roughly
    30,000. Even shedding 94% of every result falls short.
    """
    ceiling, result, groups, prompt = 117_952, 3_500, 6, 103_200
    break_even = 0.29 * prompt * 0.84  # the suffix behind an edit early in the band

    for share in (0.25, 0.10, 0.05, 0.03, 0.01):
        keeps = [min(int(ceiling * share / (position + 1)), result) for position in range(groups)]
        removed = sum(result - keep for keep in keeps)
        assert removed < break_even, (
            f"band_share {share} removed {removed:,}, which would clear {break_even:,.0f} -- "
            "if this ever fails the regime claim needs re-deriving, not the test relaxing"
        )


# region preservation


def _preserve(messages: list[Message], *message_ids: str) -> None:
    """Mark the named messages as protected from removal."""
    by_id = {message.message_id: message for message in messages}
    for message_id in message_ids:
        set_preserved(by_id[message_id], preserved=True, reason="test")


async def test_shortening_in_place_skips_a_preserved_result() -> None:
    """Removal path one: the trim that rewrites a tool result where it stands.

    The record written by the record-then-drop strategy is a tool result like any other, so
    this trim reaches it unless it honours the flag, and shortening loses values without
    removing a message. ``keep_tokens`` is pinned and the ceiling chosen so that shortening
    alone reaches it, which isolates this path from the shed steps below.
    """
    strategy = AnchoredCompactionStrategy(
        max_input_tokens=8_000, tokenizer=TOKENIZER, keep_tokens=200
    )
    messages = _conversation(tool_turns=8)
    _preserve(messages, "t_res_3")

    await strategy(messages)
    rendered = _rendered(messages)

    assert REMOVAL_MARKER in rendered, "the fixture has to give the collapse something to trim"
    assert "[compacted: an earlier tool call and its result]" not in rendered, (
        "no shedding, so this is the trim path"
    )
    assert "R3 " + "x" * 8_000 in rendered, "the preserved result is intact to its last character"
    assert "R2 " + "x" * 8_000 not in rendered, (
        "and its unprotected neighbour was trimmed, so the trim did run"
    )


async def test_shedding_tool_groups_skips_a_preserved_group() -> None:
    """Removal path two: the last-resort step that excludes a whole tool group.

    One preserved member protects the pair, because a tool call sent without its result is a
    malformed conversation on most providers -- there is no half-shed to fall back on. The
    ceiling here is far below what the anchors alone need, so the shed step runs as hard as it
    is ever going to.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=120, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)
    _preserve(messages, "a_call_3", "t_res_3")

    await strategy(messages)
    rendered = _rendered(messages)

    assert "[compacted: an earlier tool call and its result]" in rendered, (
        "everything else was shed"
    )
    assert "R3 " + "x" * 8_000 in rendered, "the preserved group survived the shed whole"
    assert "R2 " not in rendered, "and its unprotected neighbour did not"


async def test_shedding_assistant_narration_skips_a_preserved_reply() -> None:
    """Removal path three: the step that drops assistant prose once tool shedding is not enough.

    Narration is often where a tool's values ended up after the model restated them, so it is
    exactly the kind of message another strategy may have to declare irreplaceable. Tested
    separately from the tool path because it is a second call into the same method with a
    different group kind, and a guard added to one call site and not the other would pass every
    test above.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=120, tokenizer=TOKENIZER)
    messages = _conversation(tool_turns=8)
    _preserve(messages, "a_txt_3")

    await strategy(messages)
    rendered = _rendered(messages)

    assert "[compacted: an earlier assistant reply]" in rendered, "the narration shed ran"
    assert "I looked up 3." in rendered, "and stepped over the preserved reply"
    assert "I looked up 2." not in rendered, "which it would not have done for an ordinary one"


async def test_a_band_of_nothing_but_preserved_messages_stops_over_the_ceiling() -> None:
    """Protecting a message must be able to fail loudly, and must never become a loop.

    A preserved message is not excluded: it is still sent and still counted, so a conversation
    can sit over its ceiling with every remaining candidate protected. The two wrong answers
    are spinning -- re-examining a band nothing may be taken from -- and quietly reporting
    success while handing the provider a prompt it will reject. The right one is to change
    nothing and leave the overflow where the caller can see it, which is what the anchors
    bigger than the ceiling already do.
    """
    strategy = AnchoredCompactionStrategy(max_input_tokens=120, tokenizer=TOKENIZER)
    messages = _annotated(tool_turns=8)
    for group in strategy._middle_band(messages, group_messages(messages)):
        for message in messages[group["start_index"] : group["end_index"] + 1]:
            set_preserved(message, preserved=True, reason="test")
    before = _fingerprint(messages)

    assert await strategy(messages) is False
    assert _fingerprint(messages) == before, "a pass that may remove nothing must remove nothing"
    assert included_token_count(messages) > 120, (
        "and must say so by leaving the prompt over the ceiling"
    )
