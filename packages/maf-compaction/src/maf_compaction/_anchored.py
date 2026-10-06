"""A compaction strategy that keeps the cached prefix stable.

Every compaction strategy the framework ships measures costlier than not compacting at all,
and the measurements say why in enough detail to design against them. Three constraints fall
out:

**1. The decision must not depend on the current size.** Prompt caching is strict-prefix: a
mutation at position K re-bills everything after it. A rule like "when over 80% of the
budget, compact down to 50%" re-decides the whole history every time it trips, so the same
old group is rewritten differently at turn 12 and turn 15 and the cache is lost from the
head each time. Measured: ``truncation`` and ``context_window`` hold 60-83% hit rates where
the uncompacted control holds 93%. A rule that depends only on a group's *position* produces
byte-identical output for the same prefix on every later turn, so the prefix stays cached.

**2. Mutations must march forward, never backward.** ``SlidingWindowStrategy`` drops the
oldest group each turn, which changes the *start* of the prompt every time; it measured a
1-9% hit rate, the worst of anything tested. Collapsing from the front once and leaving it
collapsed means each turn only invalidates the small suffix that newly aged out.

**3. What gets shed matters more than how much.** ``truncation`` left 29 of 53 planted facts
in the prompt and the model used none of them, because the codes survived while the turns
saying which deployment each belonged to were deleted. Facts near the start of a conversation
-- requirements, corrections, the actual task -- are cheap to keep and expensive to lose.

The strategy that follows keeps a fixed head and a fixed tail verbatim and collapses the band
between them by a position-only rule, shedding in a defensible order: tool results first,
then the tool call requests that produced them, and assistant narration only as a last
resort. It never touches user turns.

**What it does not do is invent information.** Any strategy that reduces size destroys what
it removes. This one does not preserve every fact and does not claim to; it preserves the
head, guarantees a ceiling that token-blind strategies cannot, and pays as little cache as
the mechanism allows.

**One class of message is off limits, and it is not one this module can recognise on its own.**
A message carrying :data:`~._preserve.PRESERVED_KEY` is a message some other strategy has
already made the sole surviving copy of something it deleted -- the record written by
``_toolsummary`` is the case this was built for, and shortening it there discarded thirty-two
identifiers while the run reported nothing. Every path here that removes content therefore
consults :func:`~._preserve.is_preserved` first: shortening in place, shedding whole tool
groups, and shedding assistant narration. A preserved message is not excluded, so it still
counts against the ceiling in full; it simply may not be made smaller. When the only thing
left to remove is preserved, this strategy stops over its ceiling rather than looping, on the
same reasoning as an anchor larger than the ceiling: an honest overflow the caller can see
beats a silent loss it cannot.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from agent_framework import Content, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    set_excluded,
)

from ._preserve import any_preserved, is_preserved, removable_whole

if TYPE_CHECKING:
    from agent_framework import TokenizerProtocol

__all__ = [
    "DEFAULT_KEEP_TOKENS",
    "DEFAULT_MIN_GAIN_FRACTION",
    "MARKER_ID_PREFIX",
    "REMOVAL_MARKER",
    "AnchoredCompactionStrategy",
    "MinimumGainAnchoredCompactionStrategy",
]

#: Reason recorded on every message this strategy excludes, so a caller inspecting the
#: history can tell our removals apart from the framework's.
EXCLUDE_REASON: Final[str] = "anchored_compaction"

#: Tokens of a collapsed tool result that survive, taken from the head and the tail in equal
#: measure. Head-only retention is what the framework does; keeping both ends is a better
#: general policy for logs and documents, where the conclusion is as often at the bottom as
#: the top.
#:
#: It is *not* enough to preserve values scattered through the middle, and arithmetic says so
#: rather than taste: n values spread evenly through a result sit 1/n apart, so a head slice
#: of f/2 captures the second one only when f exceeds 2/n. With 8 values that means retaining
#: over 25% of the result to keep more than one of them. Head-and-tail retention cannot
#: preserve uniformly distributed information at any budget worth calling compaction.
DEFAULT_KEEP_TOKENS: Final[int] = 150

#: Fraction of the ceiling the oldest tool result in the collapsed middle band may occupy.
#: The ``n``-th result in the band gets an ``n``-th of it; see :meth:`_keep_tokens_for` for
#: why the divisor is the result's own position rather than the band's width.
#:
#: A budget fixed in tokens cannot work: measured at a 60,000-token window a 600-character
#: retention is 0.9% of the result, and at 272,000 it is 0.3%. The strategy scored 32 of 53
#: facts in the first case and 11 in the second -- 11 being exactly the five non-tool facts
#: plus the one code per result that fell inside the surviving head fragment.
DEFAULT_BAND_SHARE: Final[float] = 0.25

#: Refinement passes when converting a token budget into a character offset. The first estimate
#: uses the text's own measured ratio, which is close when density is even and can be far under
#: when the slice is sparser than the whole, so passes correct in both directions. Bounded
#: because the tokenizer is called on every candidate slice.
_FIT_PASSES: Final[int] = 4

#: Marks where a tool result was cut. It serves two purposes: a model shown a truncated
#: document with no sign of truncation answers as though it had seen all of it, and the
#: marker is how a later pass recognises its own earlier work and leaves it alone.
REMOVAL_MARKER: Final[str] = "... removed by compaction"

#: The marker exactly as :meth:`AnchoredCompactionStrategy._shorten` writes it, so a result that
#: merely quotes :data:`REMOVAL_MARKER` is not mistaken for one already shortened.
_SHORTENED = re.compile(rf"\n\[{re.escape(REMOVAL_MARKER)}: [\d,]+ characters\]\n")

#: Prefix of the ``message_id`` given to every note this strategy leaves behind.
MARKER_ID_PREFIX: Final[str] = "anchored_"

#: The two group kinds the shed step removes, and the note each leaves in its place.
_TOOL_CALL: Final[str] = "tool_call"
_ASSISTANT_TEXT: Final[str] = "assistant_text"
_NOTES: Final[dict[str, str]] = {
    _TOOL_CALL: "[compacted: an earlier tool call and its result]",
    _ASSISTANT_TEXT: "[compacted: an earlier assistant reply]",
}

#: Share of the tokens *behind* a collapse that the collapse must remove before it is worth
#: making.
#:
#: Derived rather than chosen. Prompt caching is strict-prefix, so an edit at position K makes
#: the provider re-read everything behind K once at the uncached price; the edit then saves
#: the tokens it removed on every turn that follows, at the cached price. Write ``R`` for the
#: tokens removed, ``B`` for the included tokens sitting behind the edit, ``T`` for the turns
#: still to come, and ``p`` and ``c`` for the uncached and cached prices. On the turn after
#: the edit the compacted arm pays ``(B - R) * p`` where the uncompacted arm pays ``B * c``,
#: and on each of the ``T`` turns after that it pays ``R * c`` less. So the edit repays itself
#: when ``T * R * c > (B - R) * p - B * c``, which rearranges to::
#:
#:     R > B * (p - c) / (p + T * c)
#:
#: The first term on the right of that inequality is ``(B - R) * p - B * c`` and not
#: ``(B - R) * (p - c)``: the tokens removed are not re-sent, so they are not re-read at the
#: cached price either, and writing it the other way drops an ``R * c`` and overstates the
#: floor by about 3%.
#:
#: The right-hand side is a share of ``B`` and of nothing else. At the measured prices --
#: 0.66 and 0.07 per million -- with twenty turns left it is 0.286, and this default is that
#: rounded up to the next percent so the floor is never *below* the break-even it comes from.
#:
#: The previous default, 0.23, was the same figure scaled by the ``B``-to-prompt ratio of the
#: single cell it was fitted to -- ``B`` about 40,000 tokens behind the edit against a
#: 52,322-token snapshot, so about 0.76 -- and then compared against the whole prompt. The two
#: forms agree at that one geometry and differ everywhere else by a factor of ``prompt / B``.
#: A whole-band collapse starting near the head of the band has a large ``B`` and is barely
#: affected; the incremental collapse of one group that has just aged out of the tail sits
#: near the end of the prompt, where ``B`` can be a fifth of it, and the prompt-based form
#: then asks that collapse for five times its own base. That is the shape the floor met most
#: often -- tens of refusals a conversation -- and it could not pass.
#:
#: ``T`` is the term nobody knows at decision time, and it divides: ten remaining turns need
#: 43% of ``B`` and forty need 17%. This default is the twenty-turn figure, so a caller who
#: expects shorter conversations should raise it rather than trust it.
DEFAULT_MIN_GAIN_FRACTION: Final[float] = 0.29


def _is_marker(message: Message) -> bool:
    """Return whether ``message`` is a note this strategy left in place of a dropped group."""
    return bool(message.message_id and message.message_id.startswith(MARKER_ID_PREFIX))


@dataclass(frozen=True, slots=True)
class _Shortening:
    """One tool result a collapse would rewrite, and what rewriting it would save.

    ``saved_tokens`` is measured on the result text rather than on the serialized message, so
    it omits the few tokens of JSON envelope that the rewrite does not change. The two agree
    to within a token per result, and the text is what the reduction is actually made of.

    ``message_index`` is where the rewrite lands, which a subclass pricing the collapse needs:
    a strict-prefix cache is spent on everything from the earliest rewrite to the end of the
    prompt, so that suffix and not the prompt is what a saving has to be weighed against.
    """

    content: Content
    text: str
    saved_tokens: int
    message_index: int


class AnchoredCompactionStrategy:
    """Collapse the middle of a conversation by a rule that never revisits its own decisions.

    Args:
        max_input_tokens: Ceiling the included prompt must stay under. This is the model's
            real input limit, not its advertised context window: on GPT-5-class deployments
            those differ by 128,000 tokens and configuring the larger one puts every
            threshold above what the service will accept.
        tokenizer: Token counter, shared with whatever measures the result.

    Keyword Args:
        keep_head_groups: Message groups at the start that are never touched. These carry the
            task and its requirements, which every deleting strategy measured so far throws
            away first and which are the cheapest facts in the conversation to keep.
        keep_tail_groups: Recent groups kept verbatim. The working set: too small and the
            model loses the thread of what it is doing, too large and each new turn shifts a
            large block and re-bills it.
        keep_tokens: Tokens of a collapsed tool result to retain, split between its head and
            its tail, the same for every result. ``None`` derives it from ``band_share`` and
            the result's position, which is what makes the retention scale with the window
            instead of shrinking to nothing as results grow. Counted with the tokenizer rather
            than converted from characters: a fixed characters-per-token guess was wrong by a
            factor of two on this workload, which both wasted budget and made the reported
            retention wrong.
        band_share: Fraction of ``max_input_tokens`` the band's oldest tool result may occupy,
            the ``n``-th getting an ``n``-th of it. Raising it keeps more of each result and
            saves less. See :meth:`_keep_tokens_for` for why the divisor is the result's own
            position and not the band's width.
        collapse_assistant_text: Allow assistant narration in the middle band to be dropped
            when tool shedding is not enough. Last resort, because narration is often where
            a tool's values ended up after the model restated them.
    """

    def __init__(
        self,
        *,
        max_input_tokens: int,
        tokenizer: TokenizerProtocol,
        keep_head_groups: int = 3,
        keep_tail_groups: int = 4,
        keep_tokens: int | None = None,
        band_share: float = DEFAULT_BAND_SHARE,
        collapse_assistant_text: bool = True,
    ) -> None:
        """Validate and store the configuration.

        Raises:
            ValueError: If any bound is negative or the ceiling is not positive.
        """
        if max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive.")
        if keep_head_groups < 0 or keep_tail_groups < 0:
            raise ValueError("keep_head_groups and keep_tail_groups must be >= 0.")
        if keep_tokens is not None and keep_tokens < 0:
            raise ValueError("keep_tokens must be >= 0.")
        if not 0.0 < band_share <= 1.0:
            raise ValueError("band_share must be in (0.0, 1.0].")
        self.max_input_tokens = max_input_tokens
        self.tokenizer = tokenizer
        self.keep_head_groups = keep_head_groups
        self.keep_tail_groups = keep_tail_groups
        self.keep_tokens = keep_tokens
        self.band_share = band_share
        self.collapse_assistant_text = collapse_assistant_text

    async def __call__(self, messages: list[Message]) -> bool:
        """Compact in place down to :attr:`max_input_tokens`, and report whether anything changed.

        :meth:`compact_to` at this strategy's own ceiling, which is all a standalone row ever
        asks for.

        Returns:
            True if any message was excluded or replaced. False does not imply the prompt now
            fits -- check ``included_token_count`` for that.
        """
        return await self.compact_to(messages, ceiling=self.max_input_tokens)

    async def compact_to(self, messages: list[Message], *, ceiling: int) -> bool:
        """Compact in place, shedding until the prompt is at or under ``ceiling``.

        The composed row's seam: its last-resort chain hands this a ceiling below the input
        budget when it compacts to a target rather than to the budget -- see
        ``compaction/_composed``. The shortening step does not read the ceiling at all, since it
        is decided by position alone; only how far the shedding goes does.

        The shed loop below terminates on "nothing moved" rather than on "the ceiling is met",
        and that is what makes preservation safe. A preserved group is skipped but still
        counted, so a conversation can be over the ceiling with every remaining candidate
        protected; the pass then sheds nothing, breaks, and returns having left the prompt too
        large. That is deliberate and is the same behaviour as an anchor bigger than the
        ceiling: the caller sees a prompt it must deal with, instead of a strategy quietly
        eating the one message another strategy has made irreplaceable, or spinning against a
        band it is no longer allowed to touch.

        Args:
            messages: The conversation, mutated in place.

        Keyword Args:
            ceiling: Included tokens the shedding stops at.

        Returns:
            True if any message was excluded or replaced. False does not imply the prompt now
            fits -- check ``included_token_count`` for that.
        """
        if not messages:
            return False
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)

        groups = group_messages(messages)
        band = self._middle_band(messages, groups)
        if not band:
            return False

        # Shortening results first is what keeps the conversation legible: the model still
        # sees that each call happened and roughly what it returned. Only when that is not
        # enough does anything get removed outright.
        changed = self._collapse_tool_results(messages, band, ceiling=ceiling)
        if changed:
            # Token counts are cached per message inside the group annotations, so shortening
            # a result in place leaves the cached number describing text that no longer
            # exists. Without this the ceiling check below reads the original sizes and sheds
            # groups that were already small enough.
            annotate_token_counts(messages, tokenizer=self.tokenizer, force_retokenize=True)
        # Shedding is repeated because each pass adds notes of its own, which can leave the
        # prompt over the ceiling it just tried to meet. It ends: a pass that sheds excludes at
        # least one more group, excluded groups and notes are never shed, and the loop stops at
        # the first pass that sheds nothing.
        while True:
            if included_token_count(messages) <= ceiling:
                break
            shed = self._shed(messages, _TOOL_CALL, ceiling=ceiling)
            if self.collapse_assistant_text and included_token_count(messages) > ceiling:
                shed = self._shed(messages, _ASSISTANT_TEXT, ceiling=ceiling) or shed
            changed = shed or changed
            if not shed:
                break
        return changed

    def _middle_band(
        self, messages: list[Message], groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the groups between the two anchors.

        The band is decided from group *positions* alone. Nothing about the current token
        count enters, which is what makes a group's fate identical on every later turn and
        so keeps the prefix byte-identical for the cache.

        Returns:
            The middle groups, oldest first. Empty when the conversation is still short
            enough that the anchors cover all of it.
        """
        # Notes left by an earlier pass are skipped before the anchors are counted. Each one
        # stands in for a removed group, so counting them would shift the head and tail by one
        # per note and hand a different band back on every pass -- which would make the
        # strategy's own output change what it does next, the exact instability it exists to
        # remove.
        real = [group for group in groups if not self._is_marker_group(messages, group)]
        tail_start = len(real) - self.keep_tail_groups
        if tail_start <= self.keep_head_groups:
            return []
        return real[self.keep_head_groups : tail_start]

    @staticmethod
    def _is_marker_group(messages: list[Message], group: dict[str, Any]) -> bool:
        """Return whether every message in ``group`` is a note this strategy inserted."""
        members = messages[group["start_index"] : group["end_index"] + 1]
        return bool(members) and all(_is_marker(message) for message in members)

    def _collapse_tool_results(
        self, messages: list[Message], band: list[dict[str, Any]], *, ceiling: int
    ) -> bool:
        """Shrink every tool result in the band, in place, keeping both of its ends.

        Rewrites content rather than excluding the message, so the tool-call structure stays
        intact: the model still sees that a call happened and what it returned a little of,
        which reads far better than a hole in place of a result.

        Keyword Args:
            ceiling: The ceiling this pass compacts to. Unread here, since shortening is decided
                by position alone; a subclass that prices a collapse reads it.

        Returns:
            True if any result was shortened.
        """
        return self._apply_shortenings(self._plan_shortenings(messages, band))

    def _plan_shortenings(
        self, messages: list[Message], band: list[dict[str, Any]]
    ) -> list[_Shortening]:
        """Return the rewrites a collapse would make, without making any of them.

        Split out from applying them so a subclass can price a collapse before it happens.
        Nothing here writes: ``_shorten`` is a pure function of the text and the budget, so
        the plan *is* what the collapse does and the two cannot drift apart. A dry run that
        instead mutated and rolled back would have to unwind ``additional_properties``
        exactly, and one flag missed there is a wrong measurement that looks like a right one.

        Args:
            messages: The message list, read but not modified.
            band: The middle groups, as returned by :meth:`_middle_band`.

        Returns:
            One entry per tool result whose text would change, in band order.
        """
        plan: list[_Shortening] = []
        position = 0
        for group in band:
            if group.get("kind") != "tool_call":
                continue
            budget = self._keep_tokens_for(position)
            # Counted over every tool group in the band, whether or not it still has a result
            # to shorten, so that a group already shed or already trimmed still occupies its
            # place and the groups behind it keep the position -- and the budget -- they had.
            position += 1
            for index in range(group["start_index"], group["end_index"] + 1):
                message = messages[index]
                if message.additional_properties.get(EXCLUDED_KEY, False):
                    continue
                # Removal path one of three. A preserved result is the only surviving copy of
                # material another strategy has already deleted, so the tokens this rewrite
                # would save are not a saving at all: they are the deletion's receipt. Skipped
                # *after* the position counter above has advanced, so the groups behind it keep
                # the budget they had and the retention rule stays a function of position only.
                if is_preserved(message):
                    continue
                for content in message.contents:
                    if content.type != "function_result":
                        continue
                    text = (
                        content.result if isinstance(content.result, str) else str(content.result)
                    )
                    shortened = self._shorten(text, budget)
                    if shortened == text:
                        continue
                    saved = self.tokenizer.count_tokens(text) - self.tokenizer.count_tokens(
                        shortened
                    )
                    plan.append(
                        _Shortening(
                            content=content, text=shortened, saved_tokens=saved, message_index=index
                        )
                    )
        return plan

    @staticmethod
    def _apply_shortenings(plan: list[_Shortening]) -> bool:
        """Write a plan out, in place.

        Args:
            plan: What :meth:`_plan_shortenings` returned.

        Returns:
            True if the plan held anything at all.
        """
        for item in plan:
            item.content.result = item.text
        return bool(plan)

    def _keep_tokens_for(self, band_position: int) -> int:
        """Return how many tokens one collapsed tool result may keep.

        An explicit ``keep_tokens`` is honoured verbatim. Otherwise the band's oldest tool
        result may keep ``band_share`` of the ceiling and the ``n``-th may keep an ``n``-th of
        that, so the retention grows with the window rather than becoming a rounding error
        against it, and each result's share is a number its own position fixes for good.

        The share must not depend on the band's *current* width. A result is trimmed once --
        :meth:`_shorten` recognises its own marker and leaves a trimmed result alone -- so a
        width-based share would freeze at whatever width was in force on the turn the trim
        fired, giving retention by arrival order. It would also break this module's first
        design constraint: a decision that moves with the band's width depends on the
        conversation's current size.

        The price is that the band is no longer bounded by ``band_share`` alone. It is now
        bounded by ``band_share`` times the ceiling times the harmonic number of its tool
        groups -- 2.4x at six groups, 3.6x at twenty -- with each further group contributing
        less than the one before. That is unavoidable rather than chosen: a budget that never
        trims what the width-based rule left alone must be at least
        ``band_share * ceiling / (position + 1)`` for every position, because that is the
        widest a width-based rule is at that position, and those terms sum without limit. So
        the alternatives are a bounded band that starts editing where this strategy is
        correctly idle, or an unbounded one that does not, and the second is
        worth more: an edit that saves less than the cache it invalidates is a loss, whereas
        the residual here lands on the shed step that already exists for it.

        Rejected on that reasoning: a geometric split summing to ``band_share`` exactly, which
        bounds the band but hands every group after the first less than it gets here, so the
        strategy would begin paying for edits at windows where everything already fits; and a
        flat per-result budget, which does the same at one end, goes inert at the other, and
        throws away the retention's scaling with the window, which measured 32 planted facts
        of 53 recalled against 11 without it.

        Args:
            band_position: This result's place among the band's tool groups, counted from the
                head, the oldest being zero.

        Returns:
            A token budget for that result, never below a floor that still carries a
            recognisable fragment.
        """
        if self.keep_tokens is not None:
            return self.keep_tokens
        share = self.max_input_tokens * self.band_share / (band_position + 1)
        return max(int(share), DEFAULT_KEEP_TOKENS)

    def _shorten(self, text: str, budget: int) -> str:
        """Return ``text`` reduced to about ``budget`` tokens, taken from both ends.

        Args:
            text: The tool result to shorten.
            budget: Tokens the result may keep in total, split between its two ends.

        Returns:
            The text unchanged when it already fits, otherwise its head and tail joined by a
            marker that says how much was removed. The marker matters: a model shown a
            truncated document with no sign of truncation will answer as though it saw all
            of it.
        """
        # The marker check is what makes this idempotent. The replacement carries the marker's
        # own tokens on top of the budget, so a second pass would shorten it again and a third
        # again -- each one a fresh mutation at the same position, which is precisely the cache
        # behaviour this strategy exists to avoid.
        if _SHORTENED.search(text) or self.tokenizer.count_tokens(text) <= budget:
            return text
        head_budget = budget // 2
        head_chars = self._fit(text, head_budget, from_end=False)
        tail_chars = self._fit(text, budget - head_budget, from_end=True)
        head, tail = text[:head_chars], text[len(text) - tail_chars :]
        removed = len(text) - head_chars - tail_chars
        return f"{head}\n[{REMOVAL_MARKER}: {removed:,} characters]\n{tail}"

    def _fit(self, text: str, tokens: int, *, from_end: bool) -> int:
        """Return how many characters from one end of ``text`` are worth about ``tokens``.

        The tokenizer counts but cannot slice, so the offset has to be measured. Starting from
        the text's own characters-per-token ratio lands within a few percent immediately. A
        fixed ratio does not: using 4 where the real value was 7.9 made the strategy keep half
        the budget it was entitled to, and made every retention figure reported from it wrong
        by the same factor.

        Args:
            text: The text to measure into.
            tokens: Target token count for the slice.

        Keyword Args:
            from_end: Measure a suffix rather than a prefix.

        Returns:
            The largest character count tried whose slice is at or under ``tokens``.
        """
        total = max(self.tokenizer.count_tokens(text), 1)
        chars = min(int(len(text) * tokens / total), len(text))
        best = 0
        for _ in range(_FIT_PASSES):
            if chars <= 0:
                break
            piece = text[-chars:] if from_end else text[:chars]
            counted = self.tokenizer.count_tokens(piece)
            if counted <= tokens:
                best = max(best, chars)
                if counted == tokens or chars == len(text):
                    break
            following = min(int(chars * tokens / max(counted, 1)), len(text))
            if following == chars:
                break
            chars = following
        return best

    def _shed(self, messages: list[Message], kind: str, *, ceiling: int) -> bool:
        """Exclude whole groups of one kind from the band, oldest first, until the ceiling is met.

        Exclusion and insertion are separated on purpose. Excluding leaves every index in the
        band valid, so the selection loop can re-measure after each step; the notes are then
        inserted in reverse index order, where each insertion cannot disturb the position of
        one still to come. Doing both in one forward pass silently shifts every later group by
        the number of notes already inserted.

        Args:
            messages: The message list, mutated in place.
            kind: Group kind to shed, one of ``"tool_call"`` or ``"assistant_text"``.

        Keyword Args:
            ceiling: Included tokens the shedding stops at.

        Returns:
            True if any group was dropped.
        """
        spans = group_messages(messages)
        band = self._middle_band(messages, spans)
        dropped: list[dict[str, Any]] = []
        for group in band:
            if group.get("kind") != kind:
                continue
            if included_token_count(messages) <= ceiling:
                break
            members = messages[group["start_index"] : group["end_index"] + 1]
            if all(message.additional_properties.get(EXCLUDED_KEY, False) for message in members):
                continue
            # A note left by an earlier phase is an assistant message, so the assistant-text
            # phase would otherwise shed the very markers the tool phase just inserted --
            # leaving a silent hole instead of a stated one, and undoing the only signal the
            # model has that something was removed.
            if all(_is_marker(message) for message in members):
                continue
            # Removal paths two and three: this method is called once for tool groups and once
            # for assistant narration, and both arrive here. One preserved member protects the
            # whole group, because dropping a tool call while keeping its result -- or the
            # reverse -- is a malformed conversation on most providers, so there is no partial
            # shed available. A group skipped here still counts toward the ceiling, which is
            # why the caller's loop can end over budget; see :meth:`compact_to`.
            if any_preserved(members):
                continue
            # The same rule from the other side: a group whose call and result would not leave
            # together is kept whole, as a preserved one is. See
            # :func:`~._preserve.removable_whole`.
            if not removable_whole(messages, spans, group):
                continue
            for message in members:
                set_excluded(message, excluded=True, reason=EXCLUDE_REASON)
            dropped.append(group)

        for group in reversed(dropped):
            self._insert_note(messages, group, _NOTES[kind])
        if dropped:
            # Each note is itself a message with a cost. Counting it only on the next call
            # would let this one stop just above its ceiling and look as though it had met
            # it -- and on the following turn the strategy would shed one more group,
            # changing a decision it had already made.
            annotate_token_counts(messages, tokenizer=self.tokenizer)
        return bool(dropped)

    def shed_again(self, messages: list[Message], message_ids: Collection[str]) -> bool:
        """Shed again every group an earlier pass shed, found by its messages' ids.

        The composed row's seam for keeping a decision this strategy made on one list: the live
        path compacts a model call's copies and then the stored history, and a group shed on the
        copies is sent without it while the store still holds it -- so the next call would be
        sent it again. This takes the same groups off whatever list it is given, whatever its
        size, with the note :meth:`_shed` leaves, so every list that carries them agrees with
        the prompt the model was already sent.

        Read off the whole conversation rather than the band: the band is counted in positions,
        and two lists holding different tails can place the same group differently. The rules
        :meth:`_shed` keeps are kept here too -- a preserved group, a group whose call and
        result would not leave together, and a note are never shed -- and a group is shed only
        when every one of its messages carries an id in ``message_ids``, so a group that has
        gained a message since is left alone.

        Args:
            messages: The conversation, mutated in place.
            message_ids: The ids of the messages shed before.

        Returns:
            True if any group was shed.
        """
        if not message_ids:
            return False
        annotate_message_groups(messages)
        spans = group_messages(messages)
        dropped: list[dict[str, Any]] = []
        for group in spans:
            kind = group.get("kind")
            if kind not in _NOTES:
                continue
            members = messages[group["start_index"] : group["end_index"] + 1]
            if not all(message.message_id in message_ids for message in members):
                continue
            if all(message.additional_properties.get(EXCLUDED_KEY, False) for message in members):
                continue
            if any_preserved(members) or not removable_whole(messages, spans, group):
                continue
            for message in members:
                set_excluded(message, excluded=True, reason=EXCLUDE_REASON)
            dropped.append(group)
        for group in reversed(dropped):
            self._insert_note(messages, group, _NOTES[str(group["kind"])])
        if dropped:
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer)
        return bool(dropped)

    def _insert_note(self, messages: list[Message], group: dict[str, Any], note: str) -> None:
        """Leave one deterministic marker in place of a dropped group.

        The ``message_id`` is derived from the group, not from a counter or a timestamp, so
        the replacement is byte-identical on every later turn. A marker that varied would be
        a cache mutation in its own right, which is the failure this whole strategy is built
        to avoid. The framework's own summary insertion uses the same convention.
        """
        marker_id = f"{MARKER_ID_PREFIX}{group['group_id']}"
        if any(message.message_id == marker_id for message in messages):
            return
        members = messages[group["start_index"] : group["end_index"] + 1]
        messages.insert(
            group["start_index"],
            Message(
                role="assistant",
                contents=[note],
                message_id=marker_id,
                additional_properties={
                    GROUP_ANNOTATION_KEY: {
                        SUMMARY_OF_MESSAGE_IDS_KEY: [m.message_id for m in members if m.message_id],
                        SUMMARY_OF_GROUP_IDS_KEY: [group["group_id"]],
                    }
                },
            ),
        )


class MinimumGainAnchoredCompactionStrategy(AnchoredCompactionStrategy):
    """The anchored strategy, refusing any collapse too small to repay the cache it spends.

    Anchored compaction is cheap per edit but not free. An edit at position K makes the
    provider re-read everything behind K once at the uncached price, so a collapse that removes
    less than the break-even in :data:`DEFAULT_MIN_GAIN_FRACTION` is a loss however sensible
    the removal looks. At a 60,000-token window with 0.86 fill and 3,500-token tool results the
    unfloored row held a lower prompt-cache hit rate than the uncompacted control on every one
    of five seeds -- 89-93% against 91-95% -- which is that re-read arriving on the bill.
    Nothing was wrong with *what* it shortened. The edits were simply too small to be worth
    making, and the strategy had no way to notice that because it never asked.

    This one asks. Before any result is rewritten it prices the whole collapse against the
    break-even in :data:`DEFAULT_MIN_GAIN_FRACTION`, and when the projected reduction falls
    under that floor it leaves the conversation exactly as it found it and counts the refusal.
    Everything else -- the anchors, the position-only band, the shed order, the markers -- is
    inherited unchanged, so a run of this row beside ``anchored`` measures the floor and
    nothing else.

    **The projection is dry, not undone.** It is :meth:`_plan_shortenings`, the same call the
    collapse itself makes, so the number the floor is compared against is the reduction the
    collapse would produce rather than an estimate of it. Compaction records its decisions by
    mutating ``additional_properties`` in place; a projection that mutated and rolled back
    would have to unwind every one of those flags, and a single missed flag is a silent wrong
    measurement rather than a failure.

    **The floor does not apply when the prompt will not fit.** Over the ceiling, shortening is
    not an optimisation whose saving has to beat a cache cost -- it is what keeps the
    conversation admissible at all, and declining it would only push the work onto the shed
    step, which drops whole groups instead of trimming them. So the floor governs the case
    the measurement was about, a prompt that already fits and is being tidied, and the
    last-resort shedding behind it is untouched.

    Keyword Args:
        min_gain_fraction: Share of the tokens behind the collapse -- the included prompt from
            its earliest rewrite to the end -- that it must be projected to remove before it
            is allowed to happen. Zero disables the floor,
            which makes this row identical to ``anchored``. See
            :data:`DEFAULT_MIN_GAIN_FRACTION` for where the default comes from and for the
            one term in it -- the turns remaining -- that no strategy can know.

    See :class:`AnchoredCompactionStrategy` for every other parameter.
    """

    def __init__(
        self,
        *,
        max_input_tokens: int,
        tokenizer: TokenizerProtocol,
        keep_head_groups: int = 3,
        keep_tail_groups: int = 4,
        keep_tokens: int | None = None,
        band_share: float = DEFAULT_BAND_SHARE,
        collapse_assistant_text: bool = True,
        min_gain_fraction: float = DEFAULT_MIN_GAIN_FRACTION,
    ) -> None:
        """Validate and store the configuration.

        Raises:
            ValueError: If ``min_gain_fraction`` is negative or at least 1.0 -- a floor of one
                whole prompt can never be met, so the strategy would silently never act -- or
                if any bound the anchored strategy validates is out of range.
        """
        super().__init__(
            max_input_tokens=max_input_tokens,
            tokenizer=tokenizer,
            keep_head_groups=keep_head_groups,
            keep_tail_groups=keep_tail_groups,
            keep_tokens=keep_tokens,
            band_share=band_share,
            collapse_assistant_text=collapse_assistant_text,
        )
        if not 0.0 <= min_gain_fraction < 1.0:
            raise ValueError("min_gain_fraction must be in [0.0, 1.0).")
        self.min_gain_fraction = min_gain_fraction
        self._declined = 0

    @property
    def declined_collapses(self) -> int:
        """Passes that had a collapse available and refused it as too small to pay for itself.

        Reported because the two outcomes this strategy can produce are otherwise
        indistinguishable: a run with nothing to compact and a run that decided compacting was
        not worth it both report no reduction, and they are opposite findings. A non-zero count
        says the floor is what is being measured; a zero count beside no removal says the
        conversation never gave it anything to remove.
        """
        return self._declined

    def _collapse_tool_results(
        self, messages: list[Message], band: list[dict[str, Any]], *, ceiling: int
    ) -> bool:
        """Collapse the band's tool results, unless doing so would not pay for itself.

        The saving is weighed against the tokens the edit puts back on the meter, which is the
        included prompt from the earliest rewrite to the end -- the ``B`` of the break-even in
        :data:`DEFAULT_MIN_GAIN_FRACTION` -- and not the whole prompt. Everything in front of
        the earliest rewrite stays cached and is not paid for again, so charging the collapse
        for it overstates its cost by a factor of ``prompt / B``: harmless for a collapse that
        starts at the head of the band, and enough to refuse every incremental collapse of a
        group that has just aged out of the tail, where the edit is near the end and ``B`` is
        a fraction of the prompt.

        Relies on the token annotations :meth:`AnchoredCompactionStrategy.compact_to` refreshes
        before it calls this, which is the only caller.

        Args:
            messages: The message list, mutated in place only if the collapse goes ahead.
            band: The middle groups, as returned by :meth:`_middle_band`.

        Keyword Args:
            ceiling: The ceiling this pass compacts to, which may be below
                :attr:`max_input_tokens` when a composition compacts to a target.

        Returns:
            True if any result was shortened.
        """
        plan = self._plan_shortenings(messages, band)
        if not plan:
            return False
        # Over the ceiling the collapse is not being judged on its saving: it is the cheapest
        # way left to make the conversation fit, and refusing it here would hand the work to
        # the shed step, which removes whole groups rather than trimming them.
        if included_token_count(messages) > ceiling:
            return self._apply_shortenings(plan)
        behind = included_token_count(messages[min(item.message_index for item in plan) :])
        if sum(item.saved_tokens for item in plan) < int(behind * self.min_gain_fraction):
            self._declined += 1
            return False
        return self._apply_shortenings(plan)
