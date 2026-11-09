"""Compact tool results, then user turns, then run the last-resort chain.

The record phase runs first. The user phase reads the remaining prompt at the
record trigger unless overridden, and waits for a pending record for at most
two model responses. The caller supplies its summary mode and wires the recall
tool and middleware; ``repeat_records`` requests repeated recording.

If the prompt still exceeds the budget, the chain rewrites model-made records
losslessly as written records, merges the oldest records in bounded batches
while they exceed ``record_budget_share``, merges records, folds user
summaries, tries harder record rewrites, then runs the anchored fallback with
unrecorded tool groups held. It aims to remove ``chain_gain_fraction`` of the
tokens behind its earliest edit, accepting only non-empty, smaller replacements.
If still over budget, the overflow policy sheds unrecorded groups or raises locally.
Without a record, the overflow policy also owns shedding; tool results are not shortened
as they leave the tail.

Each instance belongs to one conversation. It keeps record-wait state,
accepted record replacements, shed-message ids, refusals and counters; the
user half remembers summaries and folds. Kept decisions replay on loaded
copies and stored history before new decisions, regardless of that list's
size. Unchanged refused requests are skipped. The user half's summarizer
writes the chain's records as preserved assistant messages, never tool calls.

See ``docs/compaction/strategies.md`` for defaults, replay and design rationale.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from agent_framework import Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    annotate_message_groups,
    included_token_count,
)

from ._anchored import (
    DEFAULT_MIN_GAIN_FRACTION,
    EXCLUDE_REASON,
    _is_marker,  # pyright: ignore[reportPrivateUsage]
)
from ._media import annotate_token_counts
from ._preserve import PRESERVE_REASON_KEY, is_preserved, set_preserved
from ._request import RequestOverhead
from ._shed import durable_message_id, unique_shed_groups
from ._summary_input import bounded_prefix, fits_summary_input
from ._toolsummary import (
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    _distinctive_tokens,  # pyright: ignore[reportPrivateUsage]
    _is_written_record,  # pyright: ignore[reportPrivateUsage]
    _newest_record_identity,  # pyright: ignore[reportPrivateUsage]
    active_record_groups,
    build_record_message,
    consolidatable_record_groups,
    find_record_index,
    record_anchor_position,
    record_bodies,
    reshape_payloads,
    reshapeable_record_groups,
)
from ._toolsummary import _tokens as _record_tokens  # pyright: ignore[reportPrivateUsage]
from ._usersummary import SUMMARY_MODE_RECOMPACT, UserTurnAnchoredSummarizationCompactionStrategy

if TYPE_CHECKING:
    from agent_framework import TokenizerProtocol

__all__ = [
    "CompactionBudgetExceeded",
    "DEFAULT_CHAIN_GAIN_FRACTION",
    "DEFAULT_HARDER_ATTEMPTS",
    "DEFAULT_RECORD_BUDGET_SHARE",
    "DEFAULT_RECORD_MERGE_PROMPT",
    "ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy",
    "harder_record_prompt",
]

logger = logging.getLogger(__name__)


class CompactionBudgetExceeded(ValueError):
    """The composed strategy could not fit the prompt without removing protected material."""

    def __init__(self, tokens: int, budget: int) -> None:
        self.tokens = tokens
        self.budget = budget
        super().__init__(
            f"Compacted prompt has {tokens} tokens, exceeding max_input_tokens={budget}."
        )


@contextmanager
def _release_record_holds(messages: list[Message]) -> Generator[None]:
    """Temporarily release only the record phase's unrecorded-result holds."""
    held = [
        message
        for message in messages
        if is_preserved(message)
        and message.additional_properties.get(PRESERVE_REASON_KEY)
        in {PRESERVE_REASON_UNCOVERED, PRESERVE_REASON_UNRECORDED}
    ]
    for message in held:
        set_preserved(message, preserved=False)
    try:
        yield
    finally:
        for message in held:
            set_preserved(message, preserved=True)


#: One of the two phases, which is what walking :attr:`.strategies` hands back.
_Phase = (
    ToolResultAnchoredSummarizationCompactionStrategy
    | UserTurnAnchoredSummarizationCompactionStrategy
)

#: What one attempt to replace the records ended as.
_Consolidation = Literal["accepted", "rejected", "failed"]

#: Rewrites of the record the chain may try on one pass, each asking for more compression.
#:
#: **Two.** Each attempt costs a summarizer call and, if kept, rewrites a preserved message the
#: cached prefix runs through, so the number is a bound on spend and on breaks rather than a
#: patience setting. One is the rewrite that pays most: the first request is the one that finds
#: the wording a record can lose. The second is there because the first may be kept and still not
#: be enough, and the second asks for markedly less. A third would ask a record of values to fall
#: to about a fifth of its length while keeping every value verbatim, which is below the floor the
#: values themselves set on a record written to carry them; beyond that point attempts are
#: rejected as no smaller, or kept by dropping what the instruction says to keep, and neither is
#: worth a call. Zero switches the step off. The bound is per pass: a prompt that stays over the
#: budget meets the chain again on the next one, where an attempt already refused on the same
#: record is skipped rather than asked again -- see ``_rewrite_records``.
DEFAULT_HARDER_ATTEMPTS: Final[int] = 2

#: How much shorter each successive rewrite is asked to be, as a share of the record's length.
#:
#: Attempt ``k`` asks for about ``0.6 ** k`` of it: 60%, then 36%. A share rather than a token
#: count, because the summarizer can judge a proportion of the text in front of it and cannot
#: count tokens; and stated as an aim, because a record that meets it by dropping values is the
#: failure the instruction beside it is there to forbid.
_HARDER_RATIO: Final[float] = 0.6

#: Share of the tokens behind the chain's earliest edit that a firing of the chain must remove.
#:
#: **The chain's hysteresis, and derived from the break-even every other threshold here comes
#: from.** The chain starts only when the prompt is over the input budget. Stopping as soon as
#: the prompt fits leaves it just under the budget, where the next turn's growth puts it back
#: over and the chain fires again. Every firing edits early in the prompt -- a record sits where
#: the earliest tool results were, a user summary straight after the first user turn, narration
#: in the oldest part of the band -- so every firing re-bills nearly the whole prompt, and a chain
#: without hysteresis fires on nearly every call: measured, a 38% cache hit rate against the
#: uncompacted control's 98%.
#:
#: A firing is an edit, and :data:`~._anchored.DEFAULT_MIN_GAIN_FRACTION` already prices one:
#: an edit that re-bills the ``B`` tokens behind it repays itself over ``T`` further calls only
#: when it removes ``R > B * (p - c) / (p + T * c)``, where ``p`` and ``c`` are the uncached
#: and cached prices -- its docstring carries the derivation. The chain has no choice about
#: *whether* to edit, since the prompt is over the budget, but it does choose *how much* to
#: remove once it has, and the re-bill of ``B`` is paid either way. So a firing is made to
#: pass the same test: it goes on, past the budget, until it has removed at least this share
#: of the tokens behind the earliest edit it made -- ``B`` measured on the pass, from where the
#: first changed message sat in the prompt the chain started from. A firing that stopped at the
#: budget removed the few hundred tokens of one turn's growth for a re-bill of the whole prompt,
#: which is the loss that inequality exists to refuse, and bought another such firing on the
#: next call. One removing ``R`` leaves room for about ``R`` tokens of growth before the next.
#:
#: **The same number as the anchored floor, on purpose**: the same prices and the same twenty
#: remaining calls give the same share, and a second constant would be a second place for them to
#: drift apart. At a tenth of the uncached rate for a cached token, the formula reads 0.30 at twenty
#: calls -- the default's rounding -- 0.45 at ten and 0.18 at forty.
#:
#: **A target, not a promise.** The steps are what they are: a record merge runs once per set
#: of records, a user fold once, the harder rewrites a bounded number of times, and the
#: fallback sheds narration and nothing else. When they cannot reach the target the chain stops
#: where they left the prompt, exactly as it stops over the budget when they cannot reach that;
#: ``CHAINSHORT`` counts those firings and ``CHAINTARGET`` the ones that reached it. Zero is the
#: old behaviour: the target is the budget.
DEFAULT_CHAIN_GAIN_FRACTION: Final[float] = DEFAULT_MIN_GAIN_FRACTION

#: Rounds the chain's fallback may run on one pass, when its own shedding moves the target.
#:
#: The target is a share of the tokens behind the earliest edit, and the fallback may make an
#: edit earlier than any before it, so the target it was handed can move below where it
#: stopped. It is handed the new one and runs again, which it may do this many times in all.
#: Bounded so a termination mistake cannot loop inside a chat client; in practice the second
#: round is the last, because the fallback sheds oldest first and its first shed is its
#: earliest.
_MAX_FALLBACK_ROUNDS: Final[int] = 3

#: Share of the sending budget the active records may hold once the chain has started.
#:
#: **Nothing else bounds their total.** Every record is preserved for the run and
#: ``record_target_tokens`` sizes one record, not how many there are, so on a long tool-heavy run
#: the records alone can outgrow the budget; the overflow shed may take only unrecorded history,
#: and every pass then ends over the budget. Past this share the chain merges the oldest records
#: in batches; see ``_merge_batches``. **Half**, leaving the rest for what no merge can shrink:
#: the head and tail, user turns, and the tool work the next record will cover. A judgment rather
#: than a measurement. ``None`` switches the bound off, and with it the batch limit on record
#: requests.
DEFAULT_RECORD_BUDGET_SHARE: Final[float] = 0.5

#: Model responses the user half waits through for a record that is due, before acting anyway.
#:
#: **Two, and it is the re-force layer's number for the re-force layer's reason**, counted in a
#: different unit. The wait begins on the pass that finds the prompt over the line with a record
#: due. The recall middleware decides on the exit of that pass's call and pins the call after
#: it, so the first response the wait sees is the deciding call's own, and it cannot be the
#: record. The second is the pinned call's. If the model wrote the record, the follow-up call's
#: pass finds it and ends the wait as a completed cycle before the count is read. If the model
#: did anything else -- ignored the pin, was cut off, had the option refused -- the count
#: reaches two on the next pass, the record was not written, and the user half acts. Waiting
#: longer would keep the prompt over the line on the evidence of an ask that has already failed;
#: waiting less would give up on a pass that could not have seen the record. See
#: ``_toolsummary._REFORCE_ARRIVAL_PASSES``, whose argument this is.
#:
#: **Responses rather than passes, because the live path runs two passes per call.** The
#: strategy runs over the copies sent on a call and again over the store after the turn, so a
#: bound of two *passes* would be spent on the crossing turn's own two lists and expire on the
#: pinned call's pass, before the model had written anything. A response is counted off the
#: conversation itself -- an assistant message, see :func:`_responses` -- so the store pass and
#: the next call's copy pass read the same number for the same point in the conversation, and
#: one crossing cannot be counted twice.
_RECORD_WAIT_RESPONSES: Final[int] = 2

#: What the summarizer is asked when the chain merges the active records into one.
#:
#: Generic on purpose, in the sense the acceptance rule is: nothing in it names any workload's
#: codes or scopes. Values are to be copied verbatim because a record is exactly the place where a
#: value that cannot be reconstructed survives; wording is to be cut because that is what a merge
#: can remove without losing anything a later question could need.
DEFAULT_RECORD_MERGE_PROMPT: Final[str] = (
    "The numbered texts below are compaction records: each is an account of tool results an "
    "agent received earlier in a conversation, written so those results could be removed. "
    "Write one record that replaces all of them. Copy every identifier, code, number, date, "
    "name and other exact value verbatim, from every record; do not paraphrase, round, "
    "abbreviate or drop any of them. Remove what the records repeat and cut wording, not "
    "values. Reply with the record text only."
)


def harder_record_prompt(attempt: int) -> str:
    """Return what the summarizer is asked on the ``attempt``-th rewrite of the record.

    Each attempt asks for a smaller share of the record's length -- see :data:`_HARDER_RATIO` --
    and keeps the same generic rule about what may not be cut. Several records are handed over
    together when an earlier merge was refused, and the instruction covers that case rather than
    leaving a rewrite of several to be read as a rewrite of the first.

    Args:
        attempt: One for the first rewrite on a pass, two for the second, and so on.

    Returns:
        The system prompt.
    """
    percent = round(100 * _HARDER_RATIO**attempt)
    return (
        "Rewrite the compaction record below so that it is at most about "
        f"{percent}% of its current length. Keep every identifier, code, number, date, name and "
        "other exact value verbatim: do not paraphrase, round, abbreviate or drop any of them. "
        "Cut wording instead -- repetition, explanation, connecting prose and formatting. If "
        "several numbered records are given, write one record that carries the values of all of "
        "them. Reply with the record text only."
    )


def _responses(messages: list[Message]) -> int:
    """Count the model's responses in the conversation: the clock the user half's wait runs on.

    Excluded messages count too, because the clock measures how far the conversation has gone
    and not what is sent. A record the model writes is an assistant message, but the pass that
    sees it ends the wait as an arrival before the count is read.

    **A compaction's own insertions are not responses, and are subtracted.** The anchored
    fallback's notes and the records the chain writes are assistant messages the model never
    sent. The chain keeps its decisions on every list
    (:meth:`ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy._keep_decisions`), so a
    merged record or a shed note is put back on a pass under the budget, which a held pass is,
    and counted as a response it would end a wait a response early.
    """
    return sum(
        1
        for message in messages
        if message.role == "assistant"
        and not _is_marker(message)
        and not _is_written_record(message)
    )


def _transcript(
    messages: list[Message], groups: list[dict[str, Any]], *, bodies: list[str] | None = None
) -> str:
    """Return the numbered record bodies a merge or rewrite of ``groups`` hands the summarizer.

    Also the key a refusal is remembered by. It is the records' content and nothing else -- no
    position, no object, no pass -- so the copies sent on a call and the store after it, which
    hold the same records at different positions in different objects, give the same key, as
    they must for a kept answer to be put back at all.
    """
    return _numbered_records(record_bodies(messages, groups) if bodies is None else bodies)


def _numbered_records(bodies: list[str]) -> str:
    return "\n".join(f"{number}. {body}" for number, body in enumerate(bodies, start=1))


def _payload_digest(payload: str) -> str:
    """Key a reshape by its record's payload without storing the payload in session state."""
    return hashlib.sha256(payload.encode()).hexdigest()


def _is_excluded(message: Message) -> bool:
    """Return whether ``message`` carries the exclusion flag."""
    return bool(message.additional_properties.get(EXCLUDED_KEY, False))


@dataclass(frozen=True, slots=True)
class _ChainStart:
    """The prompt the chain started from on one pass, which its target is measured against.

    Held by identity and exclusion flag rather than copied, because what the target needs from
    it is *where* the chain first changed the prompt, and every step changes it in one of two
    ways a walk from the front can see: it excludes a message that was included, or it inserts
    one where another stood. ``behind`` is read in the prompt as it was, because the re-bill an
    edit costs is of the tokens that stood behind it when it was made.
    """

    messages: tuple[Message, ...]
    excluded: tuple[bool, ...]
    #: Included tokens at each position and every one after it, with a zero at the end.
    behind: tuple[int, ...]

    @classmethod
    def take(cls, messages: list[Message]) -> _ChainStart:
        """Read ``messages``, already token-annotated, as the chain finds them."""
        behind = [0] * (len(messages) + 1)
        for index in range(len(messages) - 1, -1, -1):
            behind[index] = behind[index + 1] + included_token_count([messages[index]])
        return cls(
            tuple(messages), tuple(_is_excluded(message) for message in messages), tuple(behind)
        )

    @property
    def tokens(self) -> int:
        """The prompt's included tokens when the chain started."""
        return self.behind[0]

    def tokens_behind_first_edit(self, messages: list[Message]) -> int | None:
        """Return the tokens that stood behind the earliest change to ``messages``, or None.

        None when nothing has changed: the lists agree message for message and flag for flag.
        """
        for index, message in enumerate(messages):
            if index >= len(self.messages):
                return 0
            if message is not self.messages[index] or _is_excluded(message) != self.excluded[index]:
                return self.behind[index]
        return None if len(messages) == len(self.messages) else self.behind[len(messages)]


@dataclass(frozen=True, slots=True)
class ChainDecisions:
    """The composed chain's carried decisions, with the record half's, as one restorable value.

    The user half's wait is clocked on the conversation but kept on the instance: when it
    started, which record it is keyed to, the response it is quiet through, and the record it
    declined to wait behind. Re-entering a conversation from a snapshot without putting these
    back lets the wait expire on the first re-entry and act on every one after, which is a
    different prompt from the first re-entry's. The chain's other decisions -- kept merges and
    rewrites, shed ids, refusals -- are keyed by the records' content, so every re-entry from
    one snapshot replays them alike, and they are not part of this value.
    """

    wait_since: int | None
    anchor: str
    quiet_through: int
    declined_behind: str | None
    records: RecordDecisions

    def to_dict(self) -> dict[str, Any]:
        """Return the wait and record decisions as independent JSON-safe values."""
        return {
            "record_identity_version": 2,
            "wait_since": self.wait_since,
            "anchor": self.anchor,
            "quiet_through": self.quiet_through,
            "declined_behind": self.declined_behind,
            "records": self.records.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChainDecisions:
        """Restore decisions serialized by :meth:`to_dict`."""
        identity_version = data.get("record_identity_version", 1)
        if identity_version not in (1, 2):
            raise ValueError("Unsupported compaction record identity version.")
        if identity_version == 1 and any(
            (data[key] or "").startswith("call:") for key in ("anchor", "declined_behind")
        ):
            raise ValueError(
                "Cannot restore compaction state with legacy recall identities; "
                "resume with the original reader or explicitly rebuild from retained history."
            )
        return cls(
            data["wait_since"],
            data["anchor"],
            data["quiet_through"],
            data["declined_behind"],
            RecordDecisions.from_dict(data["records"]),
        )


class ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
    """Run the record strategy, then the user-turn strategy, then a last-resort chain.

    Keyword Args:
        tokenizer: Token counter, and it must be the one both phases were given. Every size this
            class reads -- the entry reading, the post-record reading the user half is judged
            against, and the live readings the chain runs on -- is taken with it.
        tool_results: The record-then-drop strategy, configured as its own row configures it.
            Its recall middleware is wired by the caller and is not optional: without it the
            model is never pinned to the recall tool, no record is written, and this phase can
            only ever wait and then fall back. :func:`~._nested.find_nested_strategy` is how
            the wiring discovers this object inside this one, and it reads
            :attr:`repeat_records` off this one to switch the middleware's repeats on.
        user_turns: The user-band summarising strategy, summarizer client included. Its client
            also writes the chain's merged and rewritten records; see
            ``docs/compaction/strategies.md`` for the rationale. In boundary or fold mode it
            must remember two requests: a pass can ask for a band and then the chain's fold,
            and the store pass replays both.
        user_trigger_fraction: Fraction of the shared ceiling the user half is judged at *inside
            this composition*. None, the default, means ``tool_results.trigger_fraction``: one
            line for both halves, moving with whatever the record row's trigger was swept to.
            The object handed in is not reconfigured either way.
        harder_attempts: Rewrites of the record the chain may try on one pass once merging has
            not brought the prompt under the budget. See :data:`DEFAULT_HARDER_ATTEMPTS`; zero
            switches the step off.
        merge_prompt: What the summarizer is asked when the records are merged. See
            :data:`DEFAULT_RECORD_MERGE_PROMPT`.
        overflow_policy: ``"shed"`` (default) releases the record phase's holds as a last
            resort and removes whole unrecorded groups, logging their message IDs at WARNING.
            ``"raise"`` retains those groups. Both raise :class:`CompactionBudgetExceeded`
            if the prompt still exceeds the budget; anchors, records and external holds
            are never released. The budget measures messages with the supplied tokenizer;
            :meth:`compact_before` also reserves the call's instructions and tool schemas.
        estimate_margin: Fraction of the sending budget left unused for tokenizer estimate
            error, in ``[0.0, 1.0)``. Direct history-storage passes do not apply this margin.
        chain_gain_fraction: Share of the tokens behind the chain's earliest edit on a pass that
            the chain, once started, goes on removing until it has removed -- past the budget
            if need be. Overflow shedding reserves the same share behind its own earliest
            edit below the budget, capped at that share of the budget, when groups permit. See
            :data:`DEFAULT_CHAIN_GAIN_FRACTION`; zero stops both at the budget.
        record_budget_share: Share of the sending budget, in ``(0.0, 1.0]``, the active records
            may hold once the chain has started; past it the oldest records are merged in
            batches no larger than the summarizer's output cap where ``summary_options`` gives
            one, and every record merge or rewrite is held to that cap. See
            :data:`DEFAULT_RECORD_BUDGET_SHARE`. ``None`` keeps record requests bounded by
            ``max_summary_input_tokens`` alone.

    Raises:
        ValueError: If the two phases measure against different ceilings or with different
            tokenizer objects, if an explicit
            ``user_trigger_fraction`` is outside ``(0.0, 1.0]``, or if ``harder_attempts`` is
            negative, or if ``chain_gain_fraction`` is outside ``[0.0, 1.0)``, or
            ``record_budget_share`` outside ``(0.0, 1.0]``, or if a user half
            outside the recompacting mode remembers fewer than two requests. Each phase's
            trigger is a fraction of its own ``max_input_tokens``, and one shared line is a line
            only while the two fractions are fractions of one number;
            the chain's budget is that number too. A fraction of zero would fire the user half on
            an empty conversation and one above one could never fire it, which are the bounds
            :class:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy` sets on its
            own trigger for the same two reasons.
    """

    def __init__(
        self,
        *,
        tokenizer: TokenizerProtocol,
        tool_results: ToolResultAnchoredSummarizationCompactionStrategy,
        user_turns: UserTurnAnchoredSummarizationCompactionStrategy,
        user_trigger_fraction: float | None = None,
        harder_attempts: int = DEFAULT_HARDER_ATTEMPTS,
        merge_prompt: str | None = None,
        chain_gain_fraction: float = DEFAULT_CHAIN_GAIN_FRACTION,
        overflow_policy: Literal["shed", "raise"] = "shed",
        estimate_margin: float = 0.0,
        record_budget_share: float | None = DEFAULT_RECORD_BUDGET_SHARE,
    ) -> None:
        """Validate the pair and store it."""
        if tool_results.max_input_tokens != user_turns.max_input_tokens:
            raise ValueError(
                "tool_results and user_turns must share one max_input_tokens: "
                f"{tool_results.max_input_tokens} and {user_turns.max_input_tokens} are two "
                "ceilings, "
                "and the trigger fractions are then not comparable."
            )
        if tool_results.tokenizer is not tokenizer or user_turns.tokenizer is not tokenizer:
            raise ValueError(
                "tool_results, user_turns and the composition must share one tokenizer: the "
                "phases' cached counts drive each other's thresholds, so two counters disagree "
                "about one ceiling."
            )
        if user_trigger_fraction is not None and not 0.0 < user_trigger_fraction <= 1.0:
            raise ValueError("user_trigger_fraction must be in (0.0, 1.0].")
        if harder_attempts < 0:
            raise ValueError("harder_attempts must be >= 0.")
        if user_turns.summary_mode != SUMMARY_MODE_RECOMPACT and user_turns.remembered_requests < 2:
            raise ValueError(
                f"user_turns in the {user_turns.summary_mode!r} mode must have "
                "remembered_requests >= 2: one pass can ask for a band and a fold, and the store "
                "pass must replay both rather than summarise again."
            )
        # One is refused as well as anything above it: a firing asked to remove everything behind
        # its earliest edit would be asked to empty the prompt from there on.
        if not 0.0 <= chain_gain_fraction < 1.0:
            raise ValueError("chain_gain_fraction must be in [0.0, 1.0).")
        if overflow_policy not in {"shed", "raise"}:
            raise ValueError("overflow_policy must be 'shed' or 'raise'.")
        if isinstance(estimate_margin, bool) or not 0.0 <= estimate_margin < 1.0:
            raise ValueError("estimate_margin must be in [0.0, 1.0).")
        if record_budget_share is not None and (
            isinstance(record_budget_share, bool) or not 0.0 < record_budget_share <= 1.0
        ):
            raise ValueError("record_budget_share must be in (0.0, 1.0] or None.")
        self.record_budget_share = record_budget_share
        self.estimate_margin = estimate_margin
        self._request_overhead = RequestOverhead(tokenizer)
        self._request_budget: ContextVar[int | None] = ContextVar("request_budget", default=None)
        self.overflow_policy = overflow_policy
        self.tokenizer = tokenizer
        self.tool_results = tool_results
        self.user_turns = user_turns
        self.max_input_tokens = tool_results.max_input_tokens
        #: The line the user half is judged at in this composition, defaulted to the record
        #: half's so that the two cannot drift apart under a sweep of the record row's trigger.
        self.user_trigger_fraction = (
            tool_results.trigger_fraction
            if user_trigger_fraction is None
            else user_trigger_fraction
        )
        self.harder_attempts = harder_attempts
        self.merge_prompt = merge_prompt or DEFAULT_RECORD_MERGE_PROMPT
        self.chain_gain_fraction = chain_gain_fraction
        self._removed_by_record = 0
        self._records_reshaped = 0
        self._records_merged = 0
        self._record_merges_rejected = 0
        self._user_summaries_merged = 0
        self._user_merges_rejected = 0
        self._record_rewrites = 0
        self._record_rewrites_rejected = 0
        self._record_consolidations_lossy = 0
        self._record_merges_skipped = 0
        self._record_rewrites_skipped = 0
        self._record_summary_failures = 0
        self._last_resort_fallbacks = 0
        self._chain_targets_reached = 0
        self._chain_targets_missed = 0
        self._decisions_kept = 0
        # The user half's wait for the record half; see ``_holds_for_record``. All of it is read
        # off the conversation or kept here, never in message annotations, because the copies'
        # annotations do not reach the store the next pass runs over.
        self._user_passes_waited = 0
        # The response count the current wait began at, or None when none is running.
        self._wait_since: int | None = None
        # The newest record's identity as the last pass saw it -- see ``_newest_record_identity``;
        # a change is a record arriving.
        self._anchor = ""
        # No new wait may begin at or before this response count: set when a record arrives.
        self._quiet_through = -1
        # The anchor a wait ran out behind, or None. No new wait begins behind it.
        self._declined_behind: str | None = None
        # What the chain decided, kept for the whole run and put back on every list it applies
        # to, whatever that list's size: the records a merge or rewrite replaced, by their
        # transcript and source count, with their replacement text; and the ids of the messages
        # its fallback shed. See ``_keep_decisions``. The user half keeps its fold decisions.
        self._kept_records: dict[tuple[str, int | None], str] = {}
        # Digests of the record bodies the chain reshaped, which every list reshapes alike.
        self._reshaped: set[str] = set()
        self._shed_ids: set[str] = set()
        self._shed_groups: set[str] = set()
        # Shrink acceptance depends on the source count as well as the request's transcript.
        self._refused_merges: set[tuple[str, int]] = set()
        self._refused_rewrites: dict[tuple[str, int], int] = {}

    @property
    def strategies(self) -> tuple[_Phase, ...]:
        """The phases, in the order :meth:`__call__` runs them.

        One source of truth for the order, and the attribute name is
        :class:`~agent_framework._compaction.TokenBudgetComposedStrategy`'s so that anything
        walking a composition to find a part -- :func:`~._nested.find_nested_strategy`, which
        the middleware wiring relies on -- reads one name rather than a name per composing class.
        """
        return (self.tool_results, self.user_turns)

    @property
    def repeat_records(self) -> bool:
        """Whether the recall middleware should ask again for new tool work. Always True here.

        The first part of this composition's design: every new batch of tool results is
        recorded, and the covered results dropped, rather than one record being written and the
        rest of the run's tool work left to the fallback. The setting belongs to the middleware,
        which the caller builds; this is how the composition says it wants it, and the code
        wiring the middleware turns it on for any strategy reporting True here. The standalone
        record strategy reports nothing, so its default is untouched.
        """
        return True

    @property
    def records_found(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.records_found`."""
        return self.tool_results.records_found

    @property
    def records_in_conversation(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.records_in_conversation`."""
        return self.tool_results.records_in_conversation

    @property
    def fallbacks_used(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.fallbacks_used`."""
        return self.tool_results.fallbacks_used

    @property
    def fallbacks_after_record(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.fallbacks_after_record`."""
        return self.tool_results.fallbacks_after_record

    @property
    def fallbacks_held_after_record(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.fallbacks_held_after_record`."""
        return self.tool_results.fallbacks_held_after_record

    @property
    def records_without_values(self) -> int:
        """Observed records quoting no distinctive value from eligible targets."""
        return self.tool_results.records_without_values

    @property
    def groups_held_unreachable_coverage(self) -> int:
        """Groups whose estimated value coverage exceeds the record target on the last pass."""
        return self.tool_results.groups_held_unreachable_coverage

    @property
    def groups_kept_uncovered(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.groups_kept_uncovered`."""
        return self.tool_results.groups_kept_uncovered

    @property
    def groups_preserved_uncovered(self) -> int:
        """:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.groups_preserved_uncovered`."""
        return self.tool_results.groups_preserved_uncovered

    @property
    def user_compactions(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_compactions`."""
        return self.user_turns.user_compactions

    @property
    def user_messages_replaced(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_messages_replaced`."""
        return self.user_turns.user_messages_replaced

    @property
    def user_summaries_in_conversation(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_summaries_in_conversation`."""
        return self.user_turns.user_summaries_in_conversation

    @property
    def user_summary_tokens(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_summary_tokens`."""
        return self.user_turns.user_summary_tokens

    @property
    def user_folds(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_folds`.

        A user half in the boundary mode never folds on its own, so there every fold counted
        here is one the chain asked for -- :attr:`user_summaries_merged` -- or a replay of one
        on the other list. In the fold mode it also counts the user half's own folds.
        """
        return self.user_turns.user_folds

    @property
    def user_summaries_replayed(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_summaries_replayed`."""
        return self.user_turns.user_summaries_replayed

    @property
    def user_summary_skips(self) -> int:
        """User-summary or fold requests skipped during the user half's cooldown."""
        return self.user_turns.user_summary_skips

    @property
    def user_summary_failures(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_summary_failures`."""
        return self.user_turns.user_summary_failures

    @property
    def user_passes_below_trigger(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_passes_below_trigger`.

        On this row it includes every pass where the record half alone brought the prompt under
        the shared line, which is the user half staying idle by design rather than starved; see
        the module docstring.
        """
        return self.user_turns.user_passes_below_trigger

    @property
    def user_passes_declined(self) -> int:
        """:attr:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.user_passes_declined`."""
        return self.user_turns.user_passes_declined

    def decision_state(self) -> ChainDecisions:
        """Return the chain's carried decisions and the record half's, independent of both."""
        return ChainDecisions(
            wait_since=self._wait_since,
            anchor=self._anchor,
            quiet_through=self._quiet_through,
            declined_behind=self._declined_behind,
            records=self.tool_results.decision_state(),
        )

    def restore_decisions(self, decisions: ChainDecisions) -> None:
        """Put back decisions taken with :meth:`decision_state`, chain and record half alike."""
        self._wait_since = decisions.wait_since
        self._anchor = decisions.anchor
        self._quiet_through = decisions.quiet_through
        self._declined_behind = decisions.declined_behind
        self.tool_results.restore_decisions(decisions.records)

    def session_state(self) -> dict[str, Any]:
        """Return JSON-safe behavioral state for session persistence."""
        return {
            "decisions": self.decision_state().to_dict(),
            "user_turns": self.user_turns.session_state(),
            "kept_records": [
                {"transcript": transcript, "source_count": count, "text": text}
                for (transcript, count), text in self._kept_records.items()
            ],
            "reshaped_records": sorted(self._reshaped),
            "shed_ids": sorted(self._shed_ids),
            "shed_groups": sorted(self._shed_groups),
            "refused_merges": [
                {"transcript": transcript, "source_count": count}
                for transcript, count in sorted(self._refused_merges)
            ],
            "refused_rewrites": [
                {"transcript": transcript, "source_count": count, "attempt": attempt}
                for (transcript, count), attempt in self._refused_rewrites.items()
            ],
        }

    def restore_session_state(self, data: dict[str, Any]) -> None:
        """Replace behavioral state with a snapshot from :meth:`session_state`."""
        self.restore_decisions(ChainDecisions.from_dict(data["decisions"]))
        self.user_turns.restore_session_state(data["user_turns"])
        kept_records = data["kept_records"]
        if isinstance(kept_records, dict):
            counts = data.get("kept_record_counts", {})
            self._kept_records = {
                (transcript, counts.get(transcript)): text
                for transcript, text in cast(dict[str, str], kept_records).items()
            }
        else:
            self._kept_records = {
                (entry["transcript"], entry["source_count"]): entry["text"]
                for entry in kept_records
            }
        self._reshaped = set(data.get("reshaped_records", []))
        self._shed_ids = {value for value in data["shed_ids"] if durable_message_id(value)}
        self._shed_groups = set(data.get("shed_groups", []))
        # A legacy refusal without a count cannot safely suppress another request.
        merges: list[dict[str, Any] | str] = data["refused_merges"]
        self._refused_merges = {
            (entry["transcript"], entry["source_count"])
            for entry in merges
            if not isinstance(entry, str)
        }
        rewrites: dict[str, int] | list[dict[str, Any]] = data["refused_rewrites"]
        self._refused_rewrites = (
            {}
            if isinstance(rewrites, dict)
            else {
                (entry["transcript"], entry["source_count"]): entry["attempt"] for entry in rewrites
            }
        )

    @property
    def user_passes_waited(self) -> int:
        """Passes where the user half was over the line and held back for a record that was due.

        This row's own count, not the user half's: on these passes the user half was not asked
        at all, so none of its counters moved. Counted per pass, as ``USERUNDER`` is, so one
        wait on the live path usually reads three or four -- the copy and store passes of the
        crossing turn and the pinned call's pass -- and a wait that ended in a record reads
        beside a ``RECORDS`` that grew, where one that ran out reads beside the ``USERCOMPACT``
        that followed it. ``USERWAIT`` in the flags. See :meth:`_holds_for_record`.
        """
        return self._user_passes_waited

    @property
    def tokens_removed_by_record_phase(self) -> int:
        """Tokens the record phase has removed from the prompt over this run.

        A running total of per-pass drops in the included token count, taken straight after the
        record phase and before the user phase or the chain, so it is the record phase's own work:
        the fallback runs at the end of the chain and is not in it. Read beside
        :attr:`user_messages_replaced` it says how the two halves divided the work. Floored at zero
        per pass, because removal markers can leave a pass larger than it started.

        Not a flag: it is a token count rather than an event count, and it moves with the
        workload rather than with the strategy.
        """
        return self._removed_by_record

    @property
    def records_reshaped(self) -> int:
        """Model-made records the chain rewrote as written records, paying their values once.

        The chain's first step, lossless and with no summarizer call: see :meth:`_reshape_records`.
        Counted when decided, not when put back on another list.
        """
        return self._records_reshaped

    @property
    def records_merged(self) -> int:
        """Record merges the chain kept in step a: one a pass, or one a batch with a record budget.

        Each is a summarizer call and a rewrite of the prompt from the oldest record it replaced,
        and a floor under the prompt lowered from several records to one. ``RECMERGE`` in the
        flags.
        """
        return self._records_merged

    @property
    def record_merges_rejected(self) -> int:
        """Merges of the records that came back no smaller, or lossy, and were discarded.

        Step a refused on the generic rule: the old records stayed and the chain moved on. Counted
        once per refusal: the same records met again, on the other list or on a later pass, are
        skipped and counted under :attr:`record_merges_skipped`. ``RECMERGEREJ``.
        """
        return self._record_merges_rejected

    @property
    def user_summaries_merged(self) -> int:
        """Passes where the chain folded the standing user summaries into one, and kept it.

        Step b. Also counted by the user half as a fold, which is what the machinery is.
        ``USERMERGE`` in the flags.
        """
        return self._user_summaries_merged

    @property
    def user_merges_rejected(self) -> int:
        """Folds the chain asked for that came back no smaller, discarded. ``USERMERGEREJ``."""
        return self._user_merges_rejected

    @property
    def record_rewrites(self) -> int:
        """Harder rewrites of the record the chain tried, kept or not. ``RECHARDER`` in the flags.

        Step c, counted per attempt, so a row reading ``RECHARDER:2`` on a pass-bounded
        ``harder_attempts`` of two used every attempt it had. A summarizer that did not answer
        still used an attempt.
        """
        return self._record_rewrites

    @property
    def record_consolidations_lossy(self) -> int:
        """Of the refused merges and rewrites, those that were smaller but dropped values.

        Counted inside :attr:`record_merges_rejected` and :attr:`record_rewrites_rejected`
        rather than beside them: a lossy answer is refused exactly like one that is no smaller.
        See :meth:`_keeps_values`. ``RECLOSSY``.
        """
        return self._record_consolidations_lossy

    @property
    def record_rewrites_rejected(self) -> int:
        """Of :attr:`record_rewrites`, those refused as no smaller or lossy. ``RECHARDERREJ``.

        Counted once per refusal, as :attr:`record_merges_rejected` is: the attempt met again on
        the same record is skipped, under :attr:`record_rewrites_skipped`.
        """
        return self._record_rewrites_rejected

    @property
    def record_merges_skipped(self) -> int:
        """Merges of the records not asked for, because a merge of the same records was refused.

        Step a skipped for :meth:`_merge_records`'s reason, per pass. On the live path the store
        pass skips what the copies pass refused, so :attr:`record_merges_rejected` counts each
        refusal once. ``RECMERGESKIP`` in the flags.
        """
        return self._record_merges_skipped

    @property
    def record_rewrites_skipped(self) -> int:
        """Harder rewrite attempts not made, because the same record was already refused at them.

        Step c, per attempt, as :attr:`record_rewrites` is: an attempt skipped here is neither
        tried nor refused, and costs nothing. See :meth:`_rewrite_records` for which attempts a
        refusal forecloses. The store pass skips what its copies pass refused rather than
        replaying it, so a refusal is counted once under :attr:`record_rewrites` and
        :attr:`record_rewrites_rejected`. ``RECHARDERSKIP`` in the flags.
        """
        return self._record_rewrites_skipped

    @property
    def record_summary_failures(self) -> int:
        """Record merges and rewrites where the summarizer raised or answered with nothing.

        The conversation is left as it was, exactly as on the user half's failures, and the chain
        moves on. ``RECSUMMFAIL`` in the flags; not a seed-record column, because the run-level
        summarizer failure count already carries the calls.
        """
        return self._record_summary_failures

    @property
    def last_resort_fallbacks(self) -> int:
        """Passes on which the chain reached step d and ran the record half's fallback.

        Counts the fallback being *run*, where ``RECFALLBACK`` counts it changing something and
        ``RECHELD`` counts tool groups being held from it: non-zero says everything above it in
        the chain was tried and was not enough. Beside ``DQ`` it is step e, the intended loud
        failure. ``LASTFALLBACK`` in the flags.
        """
        return self._last_resort_fallbacks

    @property
    def chain_targets_reached(self) -> int:
        """Passes on which the chain started and brought the prompt down to its target.

        The target is the budget less :attr:`chain_gain_fraction` of the tokens behind the
        chain's earliest edit on that pass -- see :data:`DEFAULT_CHAIN_GAIN_FRACTION` -- or the
        budget itself at a fraction of zero. ``CHAINTARGET`` in the flags. Counted per pass, so
        one firing on the live path can read twice: once on a call's copies and once on the
        store.
        """
        return self._chain_targets_reached

    @property
    def chain_targets_missed(self) -> int:
        """Passes on which the chain started and every step it had left the prompt above its target.

        Under the budget or over it: a firing that fitted but fell short of the target counts
        here, and so does one that ended over the budget, which the row also reads as ``DQ`` if a
        model call went out that way. ``CHAINSHORT`` in the flags.
        """
        return self._chain_targets_missed

    @property
    def chain_decisions_kept(self) -> int:
        """Passes on which a decision the chain made earlier was put back on the list in hand.

        A merged or rewritten record, a user fold, or narration the chain's fallback shed, made
        on one of the live path's two lists and missing from the other -- see
        :meth:`_keep_decisions`. Nothing is asked or decided on these passes, so a high count is
        not spend: it is how often the two lists would otherwise have disagreed, and the model
        been sent back what it had already been sent without. ``CHAINKEPT`` in the flags.
        """
        return self._decisions_kept

    @property
    def _budget(self) -> int:
        budget = self._request_budget.get()
        return self.max_input_tokens if budget is None else budget

    def sending_budget(self, options: Mapping[str, Any]) -> int:
        """Return the message ceiling after reserving this call's overhead and headroom."""
        available = int(self.max_input_tokens * (1.0 - self.estimate_margin))
        overhead = self._request_overhead.count(options)
        if overhead > available:
            raise CompactionBudgetExceeded(overhead, available)
        return available - overhead

    async def compact_before(self, messages: list[Message], *, options: Mapping[str, Any]) -> bool:
        """Compact outgoing messages; direct calls retain the history-storage budget."""
        budget = self.sending_budget(options)
        token = self._request_budget.set(budget)
        try:
            return await self(messages)
        finally:
            self._request_budget.reset(token)

    async def __call__(self, messages: list[Message]) -> bool:
        """Run the record phase, then the user phase on what it left, then the chain if still over.

        The record phase is judged against the size the pass began with, at the record half's
        trigger. The user phase is judged against the size the record phase left, at the shared
        line, so it acts only when tool compaction was not enough -- and not at all while a record
        is due and still has time to arrive, because the record half compacts in two steps and a
        pass that only asked for one has not yet shown what tool compaction can do: see
        :meth:`_holds_for_record`. What either does once it has decided to act -- the band's
        share of the prompt, and so on -- is read off the conversation as it now stands. The
        chain then puts back what it decided on earlier passes, starts if the prompt is still
        over the input budget, and reads the live size against its target before each step.

        Args:
            messages: The conversation, mutated in place.

        Returns:
            True if anything changed the outgoing messages. A successful return fits the budget;
            an irreducible overflow raises CompactionBudgetExceeded.
        """
        if not messages:
            return False
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        replayed_shed = self._keep_shedding(messages)
        # Before the record phase counts records, so a stand-in and the pair it replaced are
        # never both counted.
        replayed = self._reshape_records(messages, replay=True) or replayed_shed
        decisions_kept = self._decisions_kept
        shed_sources = (
            [
                (key, members)
                for key, members in unique_shed_groups(messages)
                if any(not durable_message_id(message.message_id) for message in members)
                and not any(_is_excluded(message) for message in members)
            ]
            if any(not durable_message_id(message.message_id) for message in messages)
            else []
        )
        entry_tokens = included_token_count(messages)

        changed = await self.tool_results.compact_against(
            messages,
            prompt_tokens=entry_tokens,
            trigger_tokens=int(self._budget * self.tool_results.trigger_fraction),
            fallback_after_record=False,
            fallback_without_record=False,
            ceiling=self._request_budget.get(),
        )
        if changed:
            # Record compaction can rewrite results whose token counts are already cached.
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer, force_retokenize=True)
            self._removed_by_record += max(entry_tokens - included_token_count(messages), 0)

        replay = self.user_turns.replay_summaries(messages)
        prompt_tokens = included_token_count(messages)
        compacted = False
        if (not replay.changed or replay.new_turns) and not self._holds_for_record(
            messages, entry_tokens=entry_tokens, prompt_tokens=prompt_tokens
        ):
            compacted = await self.user_turns.compact_against(
                messages,
                prompt_tokens=prompt_tokens,
                trigger_tokens=int(self._budget * self.user_trigger_fraction),
            )
        chained = await self._last_resort(messages, replayed_user_fold=replay.chain_fold)
        if replayed and self._decisions_kept == decisions_kept:
            self._decisions_kept += 1
        try:
            fitted = self._enforce_budget(messages)
        finally:
            self._shed_groups.update(
                key
                for key, members in shed_sources
                if all(
                    _is_excluded(message)
                    and message.additional_properties.get(EXCLUDE_REASON_KEY) == EXCLUDE_REASON
                    for message in members
                )
            )
        return replayed or replay.changed or changed or compacted or chained or fitted

    def _enforce_budget(self, messages: list[Message]) -> bool:
        """Shed unrecorded groups as a last resort, or refuse an oversized prompt."""
        changed = False
        if self._over(messages) and self.overflow_policy == "shed":
            included = [message for message in messages if not _is_excluded(message)]
            start = _ChainStart.take(messages)
            ceiling = self._budget
            with _release_record_holds(messages):
                for _ in range(_MAX_FALLBACK_ROUNDS):
                    shed = self.tool_results.fallback.shed_to(messages, ceiling=ceiling)
                    changed = shed or changed
                    behind = start.tokens_behind_first_edit(messages)
                    reserve = ceil(self.chain_gain_fraction * min(behind or 0, self._budget))
                    target = self._budget - reserve
                    if not shed or target >= ceiling:
                        break
                    ceiling = target
            lost = [message for message in included if _is_excluded(message)]
            self._shed_ids.update(
                message.message_id
                for message in lost
                if message.message_id and durable_message_id(message.message_id)
            )
            if lost:
                logger.warning(
                    "Compaction shed unrecorded history to fit max_input_tokens=%s; "
                    "lost message IDs: %s",
                    self._budget,
                    [message.message_id for message in lost],
                )
        if self._over(messages):
            raise CompactionBudgetExceeded(included_token_count(messages), self._budget)
        return changed

    def _holds_for_record(
        self, messages: list[Message], *, entry_tokens: int, prompt_tokens: int
    ) -> bool:
        """Return whether the user half must stay idle on this pass because a record is on its way.

        **Why there is a wait at all.** The record half compacts in two steps and the user half
        in one. On the pass where the prompt first crosses the line, the record half can only
        ask: its middleware pins a later call, the model writes the record there, and only the
        pass after that drops what the record covers. The user half, judged on that first pass,
        sees a prompt the record half has not yet touched, acts at once, and -- when summarising
        the user turns alone gets back under the line -- takes the prompt below the trigger the
        middleware reads, so the record is never asked for and the cache-breaking half does all
        the work.

        **The rule.** The user half holds on a pass where the prompt, as the record phase left
        it, is over the shared line and the wired recall middleware can request a record or
        already has one outstanding --
        :meth:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.record_pending`,
        which shares the middleware's size, repeat and group-bound decision without consuming
        a re-force ask. It stops holding when either

        - the record arrives -- the newest record changes, which on the pass that sees it means
          ``_drop_before`` has just applied it -- and the user half is then judged, on that pass,
          against the prompt the record left; or
        - :data:`_RECORD_WAIT_RESPONSES` responses pass without one, and the user half is judged
          as though there had been no wait. No new wait then begins behind the same newest
          record: the model has had its chance at it, and a model that never records must not
          leave the user half idling one bound in every three responses.

        A group bound or an outstanding request can justify waiting below the recall size
        trigger. A wait is one unbroken run of held passes: any pass that does not hold ends it.

        **Two more guards, each for a way the wait could do harm.** None at or past the record
        half's give-up line, read on the pass-entry size the record half itself judged: that line is
        how long this row may wait for a record at all, and past it the record half has stopped
        waiting and shed tool results, so the user half must not wait longer than it does. It
        also keeps every held pass under the input budget, so the chain never runs on one. And
        none for one response after a record arrives: the live path runs this over a call's
        copies and then over the store, and a user half that acted on the copies must act on the
        store too -- where it replays the same summary -- or the store keeps the band the model
        was already sent without, and the next call is sent it again.

        **State that survives the two lists.** Everything is read off the conversation -- the
        response count, the newest record's identity (:func:`_newest_record_identity`) -- or kept
        on this object, and nothing in a
        message annotation, because the copies' annotations never reach the store. A
        conversation that has gone backwards past the wait's start, which is what restoring a
        snapshot for a probe looks like, drops the wait rather than reading a negative count.

        Args:
            messages: The conversation, grouped and token-annotated.

        Keyword Args:
            entry_tokens: The prompt as the pass began, which the record half judged.
            prompt_tokens: The prompt as the record phase left it, which the user half is judged on.

        Returns:
            True to keep the user half idle on this pass.
        """
        clock = _responses(messages)
        anchor = _newest_record_identity(messages)
        if anchor != self._anchor:
            previous = record_anchor_position(messages, self._anchor)
            newest = record_anchor_position(messages, anchor)
            if anchor and (
                not self._anchor
                or (previous is not None and newest is not None and newest > previous)
            ):
                self._anchor = anchor
                self._quiet_through = clock + 1
            else:
                # Losing an anchor or exposing an older one does not complete a pending recall.
                anchor = self._anchor
        if self._wait_since is not None and clock < self._wait_since:
            self._wait_since = None
        holding = (
            prompt_tokens > int(self._budget * self.user_trigger_fraction)
            and entry_tokens < int(self._budget * self.tool_results.fallback_fraction)
            and clock > self._quiet_through
            and anchor != self._declined_behind
            and self.tool_results.record_pending(messages, max_input_tokens=self._budget)
        )
        if not holding:
            self._wait_since = None
            return False
        if self._wait_since is None:
            self._wait_since = clock
        if clock - self._wait_since >= _RECORD_WAIT_RESPONSES:
            self._wait_since = None
            self._declined_behind = anchor
            return False
        self._user_passes_waited += 1
        return True

    async def _last_resort(
        self, messages: list[Message], *, replayed_user_fold: bool = False
    ) -> bool:
        """Put back the chain's earlier decisions, then run its steps if the prompt is over.

        The chain starts only on a prompt over the input budget. Once started, each step runs
        only while the prompt is still over the chain's target -- :meth:`_short_of_target` --
        which is below the budget by :data:`DEFAULT_CHAIN_GAIN_FRACTION`'s argument. A pass that
        started is counted as reaching the target or missing it.

        **Records first.** Model-made records are reshaped into written ones, which pays their
        values once at no summarizer cost -- :meth:`_reshape_records`, run whenever the chain
        does. The merge then runs while the prompt is short of its target and, with
        :attr:`record_budget_share` set, also while the records exceed their share, so they
        leave room for the turns after this firing -- :meth:`_merge_records`.

        **Which steps reach the target.** Each is as bounded as it was: the merge runs once per
        set of records or batch, the fold once, the harder rewrites up to :attr:`harder_attempts`
        times (the second is asked while the prompt is short of the target, not only while it is
        over the budget), and the fallback sheds narration, and only narration, down to the
        target it is handed -- see :meth:`_fall_back`. Measured offline with a fuzzed model, the
        fallback does about two thirds of the removal, the record merge and the user fold about a
        third between them, and the rewrites almost nothing, a record dense with codes having
        little else to lose.

        Args:
            messages: The conversation, mutated in place.

        Returns:
            True if anything changed the outgoing messages.
        """
        changed = self._keep_decisions(messages, replayed_user_fold=replayed_user_fold)
        if not self._over(messages):
            return changed
        start = _ChainStart.take(messages)
        changed = self._reshape_records(messages) or changed
        changed = await self._merge_records(messages, start) or changed
        if self._short_of_target(messages, start):
            changed = await self._merge_user_summaries(messages) or changed
        if self._short_of_target(messages, start):
            changed = await self._rewrite_records(messages, start) or changed
        if self._short_of_target(messages, start) and find_record_index(messages) is not None:
            # With no record, budget enforcement owns unrecorded shedding and its headroom.
            self._last_resort_fallbacks += 1
            changed = await self._fall_back(messages, start) or changed
        if self._short_of_target(messages, start):
            self._chain_targets_missed += 1
        else:
            self._chain_targets_reached += 1
        return changed

    def _keep_decisions(self, messages: list[Message], *, replayed_user_fold: bool = False) -> bool:
        """Put back on ``messages`` every decision the chain made on an earlier pass.

        **Why.** The framework runs this strategy over the copies a model call sends and then
        over the stored history, and the chain runs only on a list over the budget -- which the
        two need not agree on. The record phase on the next list can drop a tool group the
        copies still had to carry, and take the prompt under the budget without the chain.
        Without this, a record rewrite kept on a call's copies never reaches the store, the next
        call is sent the old record -- an early edit undone -- and when the prompt next goes
        over, the chain asks the summarizer the identical request again. Narration the fallback
        shed and a user fold go the same way, silently, since neither asks anything a repeat
        would show.

        **What.** Whatever the chain changes on one list is put on every list that holds what it
        changed, whatever that list's size, before the chain decides anything new:

        - a reshape, by a digest of the record's payload, put back by :meth:`__call__` before the
          record phase so no record is counted in both forms;
        - a record merge or rewrite, by the transcript of the records it replaced -- the one
          thing both lists present identically -- replacing any run of the records that
          matches one, as often as one does, since a merge followed by a rewrite is two;
        - a user fold, through
          :meth:`~._usersummary.UserTurnAnchoredSummarizationCompactionStrategy.refold`;
        - narration the chain's fallback shed, by message id, through
          :meth:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.shed_again_after_record`.

        Nothing is asked and nothing is judged: each was judged when it was made, on the same
        content. A record whose content changed has a new transcript and is not touched, and a
        group that gained a message is not shed. The halves' own decisions are not here: they
        run on every pass and re-derive them.

        Args:
            messages: The conversation, mutated in place.

        Returns:
            True if anything was put back.
        """
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        changed = self._keep_records(messages) or replayed_user_fold
        if changed:
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer)
        changed = self.user_turns.refold(messages) or changed
        changed = self._keep_shedding(messages) or changed
        if changed:
            self._decisions_kept += 1
        return changed

    def _keep_shedding(self, messages: list[Message]) -> bool:
        """Replay prior removals before their contents can trigger new compaction work."""
        message_ids = set(self._shed_ids)
        if self._shed_groups:
            for key, members in unique_shed_groups(messages):
                if key in self._shed_groups:
                    message_ids.update(
                        message.message_id for message in members if message.message_id
                    )
        with _release_record_holds(
            [message for message in messages if message.message_id in message_ids]
        ):
            return self.tool_results.shed_again_after_record(messages, message_ids)

    def _keep_records(self, messages: list[Message]) -> bool:
        """Replace every run of records the chain has already replaced, as it did.

        The earliest run first, and at one start the longest. A batch merge need not start at the
        oldest record, so any start is tried; an entry from a legacy snapshot, which has no
        source count, only at the oldest, as it was taken.
        """
        changed = False
        while self._kept_records:
            groups = consolidatable_record_groups(messages)
            source_counts = {count for _, count in self._kept_records}
            counted = sorted((count for count in source_counts if count is not None), reverse=True)
            # Legacy snapshots need a search until their source counts have been learned.
            legacy = range(len(groups), 0, -1) if None in source_counts else counted
            bodies = record_bodies(messages, groups)
            transcripts = [transcript for transcript, _ in self._kept_records]
            found = None
            for start in range(len(groups)):
                # Every kept transcript opens with its first record, which skips most starts.
                head = _numbered_records(bodies[start : start + 1])
                if not any(transcript.startswith(head) for transcript in transcripts):
                    continue
                for count in legacy if start == 0 else counted:
                    if not 1 <= count <= len(groups) - start:
                        continue
                    run = groups[start : start + count]
                    transcript = _transcript(messages, run, bodies=bodies[start : start + count])
                    key = (transcript, count)
                    text = self._kept_records.get(key)
                    if text is None and start == 0:
                        text = self._kept_records.pop((transcript, None), None)
                        if text is not None:
                            self._kept_records[key] = text
                    if text is not None:
                        found = (run, text)
                        break
                if found is not None:
                    break
            if found is None:
                break
            self.tool_results.consolidate_records(messages, *found)
            changed = True
        return changed

    def _over(self, messages: list[Message]) -> bool:
        """Return whether the prompt, as it now stands, is over the input budget."""
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        return included_token_count(messages) > self._budget

    def _target(self, messages: list[Message], start: _ChainStart) -> int:
        """Return the included tokens the chain is working down to on this pass.

        The budget until the chain has edited anything -- until then the prompt is over the
        budget and every step runs whatever the target is -- and after that the lower of the
        budget and the chain's starting size less :attr:`chain_gain_fraction` of the tokens
        behind its earliest edit so far. Re-read before every step, because a later step may
        edit earlier than any before it and so raise what the firing has to remove.
        """
        behind = start.tokens_behind_first_edit(messages)
        if behind is None or not self.chain_gain_fraction:
            return self._budget
        return min(self._budget, start.tokens - ceil(self.chain_gain_fraction * behind))

    def _short_of_target(self, messages: list[Message], start: _ChainStart) -> bool:
        """Return whether the prompt, as it now stands, is over the chain's target."""
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        return included_token_count(messages) > self._target(messages, start)

    async def _fall_back(self, messages: list[Message], start: _ChainStart) -> bool:
        """Step d: the record half's fallback, shedding down to the target, and remembered.

        Handed the target as its ceiling. Its own first shed may be the chain's earliest edit,
        which moves the target lower; it is then run again against the new one, up to
        :data:`_MAX_FALLBACK_ROUNDS` times in all, and only while each round both shed something
        and moved the target. What it shed is remembered by message id for
        :meth:`_keep_decisions`.
        """
        changed = False
        for _ in range(_MAX_FALLBACK_ROUNDS):
            ceiling = self._target(messages, start)
            included = [message for message in messages if not _is_excluded(message)]
            shed = await self.tool_results.fall_back_after_record(messages, ceiling=ceiling)
            self._shed_ids.update(
                message.message_id
                for message in included
                if message.message_id
                and durable_message_id(message.message_id)
                and _is_excluded(message)
                and message.additional_properties.get(EXCLUDE_REASON_KEY) == EXCLUDE_REASON
            )
            changed = shed or changed
            if not shed or self._target(messages, start) >= ceiling:
                break
        return changed

    def _reshape_records(self, messages: list[Message], *, replay: bool = False) -> bool:
        """Rewrite each model-made record as a written record holding the same payload, losslessly.

        A record the model made is paid twice, once in the recall call's arguments and again in
        its result; the written form (:func:`~._toolsummary.build_record_message`) carries it
        once, and is the form a merge would write anyway. No summarizer is asked and no value
        can be lost: the payload after the record's framing moves byte for byte, whitespace
        included (:func:`~._toolsummary.reshapeable_record_groups`). A record whose
        span holds anything else -- a reasoning item the framework grouped with the call, an
        extra output under its call id -- or whose result this model call carried in is left
        exactly as it is, as a merge leaves it.

        **Only when the chain fires, not whenever the written form is cheaper.** A reshape
        rewrites the prompt from the record it replaces. Inside a firing it is one more edit the
        firing's target is measured from, so the firing goes on until it has removed its share of
        what that edit re-bills -- the rule every other step answers to -- and the merge it
        precedes edits at the same record. Reshaping each record as soon as it could be would
        break the cache once per record, on prompts that fit. The decision is kept by a digest of
        the payload and, with ``replay``, put back on every list that holds the record, whatever
        its size, so the copies a call sends and the store agree.

        Args:
            messages: The conversation, mutated in place.

        Keyword Args:
            replay: Reshape only records an earlier pass reshaped.

        Returns:
            True if any record was reshaped.
        """
        groups = reshapeable_record_groups(messages)
        digests = [_payload_digest(payload) for payload in reshape_payloads(messages, groups)]
        chosen = [
            (group, digest)
            for group, digest in zip(groups, digests, strict=True)
            if not replay or digest in self._reshaped
        ]
        if not chosen:
            return False
        self.tool_results.reshape_records(messages, [group for group, _ in chosen])
        if not replay:
            self._records_reshaped += len(chosen)
            self._reshaped.update(digest for _, digest in chosen)
        return True

    async def _merge_batches(
        self, messages: list[Message], start: _ChainStart | None, share: float
    ) -> bool:
        """Merge the oldest records in batches while they exceed ``share`` or the prompt its target.

        Batches are taken oldest first and do not overlap, each the longest run of whole records
        whose request fits :meth:`_record_request_limit`, so a batch the summarizer can answer
        whole replaces a few records rather than all of them. A record too large to share a
        batch is passed over, and so is a batch already refused on the same records, which
        stays as it is until it changes; a refusal is remembered, logged at WARNING with counts
        and never values while the records exceed their share, and the sweep goes on to the next
        batch. A summarizer that fails ends the sweep, since the next batch would be asked of it
        too, and a record a batch writes is not merged again on the same pass.

        Returns:
            True if any batch was merged.
        """
        bound = int(self._budget * share)
        handled: set[int] = set()
        changed = False

        def last(group: dict[str, Any]) -> int:
            return id(messages[int(group["end_index"])])

        while self._record_tokens(messages) > bound or (
            start is not None and self._short_of_target(messages, start)
        ):
            groups = [
                group
                for group in consolidatable_record_groups(messages)
                if last(group) not in handled
            ]
            if len(groups) < 2:
                break
            batch = self._bounded_records(messages, self.merge_prompt, groups=groups, min_items=2)
            if len(batch) < 2:
                handled.add(last(groups[0]))
                continue
            handled.update(last(group) for group in batch)
            transcript = _transcript(messages, batch)
            key = (transcript, len(batch))
            if key in self._refused_merges:
                self._record_merges_skipped += 1
                continue
            standing = {id(message) for message in messages}
            lossy = self._record_consolidations_lossy
            outcome = await self._consolidate(
                messages, batch, prompt=self.merge_prompt, transcript=transcript
            )
            if outcome == "failed":
                break
            if outcome == "accepted":
                self._records_merged += 1
                handled.update(id(message) for message in messages if id(message) not in standing)
                changed = True
                continue
            self._record_merges_rejected += 1
            self._refused_merges.add(key)
            held = self._record_tokens(messages)
            if held > bound:
                logger.warning(
                    "Compaction kept a batch of %d records unmerged: the merge was refused as "
                    "%s; records hold %d tokens against a share of %d.",
                    len(batch),
                    "dropping values"
                    if self._record_consolidations_lossy > lossy
                    else "no smaller",
                    held,
                    bound,
                )
        return changed

    def _record_tokens(self, messages: list[Message]) -> int:
        """Return the included tokens of every record still being sent."""
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        return included_token_count(
            [
                message
                for group in active_record_groups(messages)
                for message in messages[group["start_index"] : group["end_index"] + 1]
            ]
        )

    def _record_request_limit(self) -> int:
        """Return the tokens one record merge or rewrite request may hold.

        With :attr:`record_budget_share` set, the summarizer's output cap, or
        ``max_summary_input_tokens`` where that is lower. **One cap, not a multiple of it**: the
        merge prompt asks for every value copied and only wording cut, so its longest acceptable
        answer is about the batch itself, and a request within the cap has an answer the cap
        cannot cut. At twice the cap an answer must halve its batch to fit, which a batch dense
        with values cannot; it is cut, refused as lossy and remembered -- the doomed request this
        bound exists to avoid, only smaller. The cap is ``max_tokens`` in the user half's
        ``summary_options``, which every record request is sent with. Without one, or once those
        options are retired, no cap is known and ``max_summary_input_tokens`` alone bounds the
        request: an assumed cap would be a guess, and one below twice a record's size would stop
        records being merged at all.
        """
        limit = self.user_turns.max_summary_input_tokens
        if self.record_budget_share is None:
            return limit
        options = (
            {}
            if self.user_turns._summary_options_disabled  # pyright: ignore[reportPrivateUsage]
            else self.user_turns.summary_options
        )
        cap = options.get("max_tokens")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            return limit
        return min(limit, cap)

    async def _merge_records(
        self, messages: list[Message], start: _ChainStart | None = None
    ) -> bool:
        """Step a: merge the oldest records that fit, when at least two fit.

        Only records :func:`~._toolsummary.consolidatable_record_groups` offers: a record whose
        result this model call carried in waits for the next pass, and is not counted towards the
        two.

        **Not asked again on records it was refused on.** A refused merge leaves the records as
        they were, and if step c is refused too they are still as they were when the prompt next
        goes over the budget -- possibly passes later, long after the request was made. The same
        records give the same transcript and source count, and a merge refused on that pair is
        skipped. Changing either makes the merge eligible again.

        **In batches, with a record budget.** With :attr:`record_budget_share` set, the records
        are merged in bounded batches -- :meth:`_merge_batches` -- while the prompt is short of
        its target or the records exceed their share, rather than in one request of every record
        that fits ``max_summary_input_tokens``, which a summarizer whose answer is capped below
        that cannot answer without dropping values. Without it, one such merge while the prompt is
        short of its target. Without ``start`` there is no target, only the share.
        """
        if self.record_budget_share is not None:
            return await self._merge_batches(messages, start, self.record_budget_share)
        if start is not None and not self._short_of_target(messages, start):
            return False
        groups = self._bounded_records(messages, self.merge_prompt, min_items=2)
        if len(groups) < 2:
            return False
        transcript = _transcript(messages, groups)
        key = (transcript, len(groups))
        if key in self._refused_merges:
            self._record_merges_skipped += 1
            return False
        outcome = await self._consolidate(
            messages, groups, prompt=self.merge_prompt, transcript=transcript
        )
        if outcome == "accepted":
            self._records_merged += 1
            return True
        if outcome == "rejected":
            self._record_merges_rejected += 1
            self._refused_merges.add(key)
        return False

    async def _merge_user_summaries(self, messages: list[Message]) -> bool:
        """Step b: fold the oldest user summaries that fit, when at least two fit."""
        outcome = await self.user_turns.fold_if_smaller(messages)
        if outcome == "folded":
            self._user_summaries_merged += 1
            return True
        if outcome == "rejected":
            self._user_merges_rejected += 1
        return False

    async def _rewrite_records(self, messages: list[Message], start: _ChainStart) -> bool:
        """Step c: rewrite the record harder, up to :attr:`harder_attempts` times on this pass.

        The oldest whole records that fit the request budget are handed over. With several,
        a rewrite is a harder merge, and the prompt says so. Each attempt is judged against the
        records as they stand when it is made, so a kept attempt raises the bar for the next.
        A record whose result this model call carried in is not handed over, as step a does
        not merge it.

        **A refusal forecloses its own attempt and every milder one, on that record, for the
        run.** Remembered by transcript and source count as the harshest attempt refused
        on it; an attempt at or below that is skipped rather than asked, and the loop goes on to
        the next attempt, and the chain to step d, exactly as after a refusal. The two directions
        are decided apart:

        - *Milder refused, harsher still open.* The pass that refuses attempt one goes straight on
          to attempt two, which is the escalation this step exists for, so a record is never
          written off on its milder refusal alone. It leaves a pass with one refused and two
          unanswered only when the summarizer failed on two, and a failure is not remembered: two
          is asked on the next pass, one is skipped.
        - *Harsher refused, milder foreclosed.* A refusal means the answer was not smaller at all:
          told to keep every value and cut the rest to about a third, the summarizer cut nothing.
          Asking it to cut less is weaker pressure towards the same outcome. The case is common --
          attempt one kept, attempt two refused on the kept record -- and without this every such
          record would buy one more refusal on the next pass. A rewrite refused as lossy
          (:meth:`_keeps_values`) is foreclosed the same way: the milder attempt was asked first on
          this pass, so the record it would revisit is one the chain already had its chance at,
          and the record simply stands at its size until it changes.

        A record refused at attempt ``harder_attempts`` is therefore incompressible until it
        changes; a merge folding new material in, or a kept rewrite, gives it a new transcript and
        every attempt back.
        """
        changed = False
        for attempt in range(1, self.harder_attempts + 1):
            if attempt > 1 and not self._short_of_target(messages, start):
                break
            prompt = harder_record_prompt(attempt)
            groups = self._bounded_records(messages, prompt)
            if not groups:
                continue
            transcript = _transcript(messages, groups)
            key = (transcript, len(groups))
            if self._refused_rewrites.get(key, 0) >= attempt:
                self._record_rewrites_skipped += 1
                continue
            self._record_rewrites += 1
            outcome = await self._consolidate(
                messages, groups, prompt=prompt, transcript=transcript
            )
            if outcome == "accepted":
                changed = True
            elif outcome == "rejected":
                self._record_rewrites_rejected += 1
                self._refused_rewrites[key] = max(self._refused_rewrites.get(key, 0), attempt)
        return changed

    def _bounded_records(
        self,
        messages: list[Message],
        prompt: str,
        *,
        min_items: int = 1,
        groups: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if groups is None:
            groups = consolidatable_record_groups(messages)
        bodies = record_bodies(messages, groups)
        return bounded_prefix(
            groups,
            render=lambda prefix: _transcript(messages, prefix, bodies=bodies[: len(prefix)]),
            prompt=prompt,
            tokenizer=self.tokenizer,
            limit=self._record_request_limit(),
            min_items=min_items,
        )

    async def _consolidate(
        self, messages: list[Message], groups: list[dict[str, Any]], *, prompt: str, transcript: str
    ) -> _Consolidation:
        """Ask for one record in place of ``groups``, and put it there if it is smaller and whole.

        A replacement must be non-empty -- a
        summarizer that answers with nothing is a failure -- fewer tokens than the records it
        replaces, and it must keep their values (:meth:`_keeps_values`). Both sizes are measured in
        the form the replacement is inserted in: the
        candidate as built, and each replaced record rebuilt from its body the same way, rather
        than the replaced messages themselves. A record the model made carries its text twice,
        once in the call's arguments, and a candidate measured against that would come out at
        about half the size with its text unchanged -- see
        :func:`~._toolsummary.build_record_message`.

        Args:
            messages: The conversation, mutated in place when the replacement is kept.
            groups: The active records, oldest first.

        Keyword Args:
            prompt: What the summarizer is asked.
            transcript: ``groups`` as :func:`_transcript` numbers them.

        Returns:
            Whether the replacement was kept, refused as no smaller or as lossy, or never arrived.
        """
        text = await self._ask(prompt=prompt, transcript=transcript)
        if text is None:
            return "failed"
        bodies = record_bodies(messages, groups)
        candidate = [build_record_message(text)]
        replaced = [build_record_message(body) for body in bodies]
        annotate_token_counts(candidate, tokenizer=self.tokenizer, force_retokenize=True)
        annotate_token_counts(replaced, tokenizer=self.tokenizer, force_retokenize=True)
        if included_token_count(candidate) >= included_token_count(replaced):
            return "rejected"
        if not self._keeps_values(bodies, text):
            self._record_consolidations_lossy += 1
            return "rejected"
        self.tool_results.consolidate_records(messages, groups, text)
        self._kept_records[(transcript, len(groups))] = text
        # The new record may read exactly as one an earlier pass went on to replace -- a merge of
        # new material can come back as the record an earlier rewrite started from -- and that
        # replacement is already decided, so it is taken here rather than asked for again.
        self._keep_records(messages)
        return "accepted"

    def _keeps_values(self, bodies: list[str], text: str) -> bool:
        """Whether ``text`` quotes ``coverage_share`` of the distinctive values in ``bodies``.

        The rule a record meets before it may stand in for a tool group (see
        :meth:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy._is_covered`),
        applied to a record that replaces records. Records holding no distinctive value pass.
        """
        values: set[str] = set()
        for body in bodies:
            values |= _distinctive_tokens(body)
        if not values:
            return True
        quoted = set(_record_tokens(text, fold_case=False))
        return len(values & quoted) >= ceil(len(values) * self.tool_results.coverage_share)

    async def _ask(self, *, prompt: str, transcript: str) -> str | None:
        """Return the summarizer's answer to one merge or rewrite request.

        Asked every time it is reached, because nothing reaches it twice for the same records: a
        kept answer is put back by :meth:`_keep_decisions` before the chain starts, so the second
        list the live path runs on holds the text the first was given -- the model is sent one
        merged record, the store holds the same one, and :func:`_newest_record_identity` reads
        the same name off both -- and a request refused as no smaller is skipped by its caller
        (see :meth:`_rewrite_records`). A failure is not remembered, for the reason the user half
        gives: the next view should ask again.

        Keyword Args:
            prompt: The system prompt.
            transcript: The numbered records.

        Returns:
            The text, or None when the summarizer raised or said nothing.
        """
        if not fits_summary_input(
            prompt,
            transcript,
            tokenizer=self.tokenizer,
            limit=self.user_turns.max_summary_input_tokens,
        ):
            logger.warning("Skipping record consolidation: summary input exceeds its token limit.")
            self._record_summary_failures += 1
            return None
        try:
            return await self.user_turns._generate(transcript, prompt=prompt)  # pyright: ignore[reportPrivateUsage]
        except Exception as error:
            # Broad on purpose, as the user half's is: this runs inside the chat client's own
            # call path, so anything not caught here fails the user's turn.
            logger.warning("Skipping record consolidation: summary generation failed (%s).", error)
            self._record_summary_failures += 1
            return None
