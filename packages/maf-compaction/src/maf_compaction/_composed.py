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

import asyncio
import hashlib
import json
import logging
import threading
from bisect import bisect_left
from collections.abc import AsyncGenerator, Callable, Collection, Generator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from copy import copy, deepcopy
from dataclasses import asdict, dataclass
from functools import partial
from math import ceil, isfinite
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from weakref import ReferenceType, ref

from agent_framework import Message
from agent_framework._compaction import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    annotate_message_groups,
    included_token_count,
    set_excluded,
)

from ._anchored import (
    DEFAULT_MIN_GAIN_FRACTION,
    EXCLUDE_REASON,
    AnchoredCompactionStrategy,
    _is_marker,  # pyright: ignore[reportPrivateUsage]
)
from ._artifacts import exclude_compaction_echoes
from ._background_records import BackgroundRecords, counted_history, has_records
from ._carryover import CarriedView, Handed
from ._media import annotate_token_counts, structural_counter
from ._merge_coverage import LossyMerge, MergeCoverage
from ._offload import off_loop, steps_locked
from ._preserve import (
    PRESERVE_REASON_KEY,
    STANDS_IN_KEY,
    is_preserved,
    pending_result_messages,
    set_preserved,
)
from ._record_diagnostics import (
    RecordMergePhase,
    RecordReduction,
    record_merge_identity,
    record_merge_observer,
    tracked_merge_values,
)
from ._record_replay import (
    LINEAGE_KEY,
    MessageKeyCache,
    RecordEdit,
    RecordReplayIndex,
    ReplayUndo,
    ReplayView,
    keeps_state_in_dict,
    replacement_identity,
    replay_match,
    source_key,
    source_lineage,
    source_members,
)
from ._record_roles import RecordRoleJudge, RecordRolePass, RecordRoleReport
from ._request import RequestOverhead
from ._saved_view import resume_saved_view, save_view
from ._shed import durable_message_id, positional_shed_sources, unique_shed_groups
from ._summary_input import (
    async_bounded_prefix_extent,
    bounded_prefix,
    fits_summary_input,
    summary_input_tokens,
    versioned_prompt,
)
from ._summary_limiter import SummaryRateLimited
from ._tool_rounds import SettledTools
from ._toolsummary import (
    CONSOLIDATE_EXCLUDE_REASON,
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    RECALL_TOOL_NAME,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    _call_identity,  # pyright: ignore[reportPrivateUsage]
    _consolidatable_record_groups,  # pyright: ignore[reportPrivateUsage]
    _distinctive_tokens,  # pyright: ignore[reportPrivateUsage]
    _is_written_record,  # pyright: ignore[reportPrivateUsage]
    _last_reasoning_index,  # pyright: ignore[reportPrivateUsage]
    _newest_record_identity,  # pyright: ignore[reportPrivateUsage]
    _reasoning_prefix_yields_to_budget,  # pyright: ignore[reportPrivateUsage]
    _record_has_external_hold,  # pyright: ignore[reportPrivateUsage]
    active_record_groups,
    build_record_message,
    consolidatable_record_groups,
    covered_tools,
    find_record_index,
    record_anchor_position,
    record_bodies,
    reshape_payloads,
    reshape_replacements,
    reshapeable_record_groups,
    settled_recall_counts,
)
from ._toolsummary import _tokens as _record_tokens  # pyright: ignore[reportPrivateUsage]
from ._usersummary import (
    SUMMARY_MODE_RECOMPACT,
    SummaryWorkStopped,
    UserTurnAnchoredSummarizationCompactionStrategy,
    summary_request_admission,
)
from ._usersummary import (
    _linked_ids as _summary_source_ids,  # pyright: ignore[reportPrivateUsage]
)
from ._usersummary import (
    _release_sources as _release_user_sources,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from agent_framework import ChatResponse, TokenizerProtocol

__all__ = [
    "CompactionBudgetExceeded",
    "CompactionPendingResultBudgetExceeded",
    "DEFAULT_CHAIN_GAIN_FRACTION",
    "DEFAULT_CHAIN_SECONDS",
    "DEFAULT_HARDER_ATTEMPTS",
    "DEFAULT_RECORD_BUDGET_SHARE",
    "DEFAULT_RECORD_MERGE_PROMPT",
    "DEFAULT_REWRITE_GAIN_FRACTION",
    "RECORD_ANSWER_BUDGET_SHARE",
    "ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy",
    "harder_record_prompt",
    "record_answer_budget",
]

logger = logging.getLogger(__name__)

type _PassResult = tuple[bool, list[tuple[str, list[Message]]], int]


def _first_batch_with_sources(
    messages: list[Message], groups: list[dict[str, Any]], count: int, sources: str | None
) -> list[dict[str, Any]] | None:
    """The first ``count`` consecutive groups whose source key is ``sources``, off the loop."""
    for index in range(len(groups) - count + 1):
        batch = groups[index : index + count]
        if source_key(messages, batch) == sources:
            return batch
    return None


def _background_batch_with_sources(
    messages: list[Message], count: int, sources: str
) -> list[dict[str, Any]] | None:
    records = BackgroundRecords.capture(messages)
    groups = consolidatable_record_groups(records.messages)
    batch = _first_batch_with_sources(records.messages, groups, count, sources)
    if batch is None:
        return None
    return [records.originals[id(records.messages[int(group["end_index"])])] for group in batch]


def _expected_record_groups(
    messages: list[Message], expected: Collection[int]
) -> list[dict[str, Any]]:
    """The consolidatable record groups whose last message is one of ``expected`` (by id)."""
    return [
        group
        for group in consolidatable_record_groups(messages)
        if id(messages[int(group["end_index"])]) in expected
    ]


def _worker_copy[T](part: T, *keep: str) -> T:
    """Own mutable containers, carrying only the state used by record search and requests."""
    owned = copy(part)
    for name, value in vars(part).items():
        if isinstance(value, (dict, list, set)):
            container = cast("dict[Any, Any] | list[Any] | set[Any]", value)
            setattr(owned, name, copy(container) if name in keep else type(container)())
    return owned


class _BatchProbesSpent(Exception):
    """A record-merge sweep has rendered :data:`_MAX_BATCH_PROBES` batch prefixes."""


class _ChainTimeSpent(SummaryWorkStopped):
    """The current pass has spent its summarizer deadline."""


class _RunFinished(_ChainTimeSpent):
    """A retained worker may finish admitted requests only."""


class _BackgroundRequests:
    """Arm each outstanding request's timeout when its run finishes."""

    def __init__(self, timeout_seconds: float | None) -> None:
        self.closed = False
        self._seconds = timeout_seconds if timeout_seconds is not None else 120.0
        self._deadlines: dict[asyncio.Timeout, float] = {}

    def finish_run(self) -> None:
        if not self.closed:
            self.closed = True
            for timeout, deadline in self._deadlines.items():
                timeout.reschedule(deadline)

    @asynccontextmanager
    async def request(self) -> AsyncGenerator[None]:
        if self.closed:
            raise _RunFinished
        timeout = asyncio.timeout(None)
        self._deadlines[timeout] = asyncio.get_running_loop().time() + self._seconds
        try:
            async with timeout:
                yield
        except TimeoutError:
            if not timeout.expired():
                raise
        finally:
            del self._deadlines[timeout]
        if timeout.expired():
            raise _RunFinished


@dataclass(frozen=True)
class _RecordAnswer:
    """Record text and the provider's output-limit verdict."""

    text: str
    incomplete: bool = False


@dataclass
class _PreparedMerge:
    sources: str
    count: int
    transcript: str
    answer: _RecordAnswer
    prompt: str


class _RecordMergeWork:
    """Prepare isolated record answers; only a foreground pass may accept them."""

    def __init__(self) -> None:
        # Stream result and cleanup hooks can run in different readers' tasks.
        self._run_context = copy_context()
        self.strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy | None = None
        self.expected_state: dict[str, Any] | None = None
        self.defer_merges = False
        self._history: list[Message] | None = None
        self._budget = 0
        self._generation = 0
        self._ready: list[_PreparedMerge] = []
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy | None = None
        # Held by each of the worker's steps on its thread (_offload.steps_locked).
        self._worker_steps = threading.Lock()

    def _in_run_context[**P, R](
        self, callback: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Isolate each hook's bindings from other callbacks and the worker task."""
        return self._run_context.copy().run(callback, *args, **kwargs)

    def bind(
        self,
        strategy: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
        state: dict[str, Any] | None,
    ) -> None:
        if self.strategy is not strategy or (
            self.expected_state is not None and state != self.expected_state
        ):
            self.cancel()
            if self.strategy is not None:
                self.strategy._background = None  # pyright: ignore[reportPrivateUsage]
        self.strategy = strategy
        strategy._background = self  # pyright: ignore[reportPrivateUsage]

    def remember(self, messages: list[Message], budget: int) -> None:
        self._history = messages
        self._budget = budget

    def cancel(self) -> None:
        if self.strategy is not None and (self._task is not None or self._ready):
            pending = int(self._task is not None and not self._task.done())
            self._in_run_context(
                self.strategy._merge_event,  # pyright: ignore[reportPrivateUsage]
                "worker_closed",
                phase="background",
                pending=pending,
                discarded=len(self._ready),
            )
            if pending or self._ready:
                # Each is a batch the summarizer will be asked again; without an observer this
                # line is the only trace of it.
                self._in_run_context(
                    logger.info,
                    "Background record merge closed with work unapplied: discarded=%d "
                    "pending=%d; those batches are asked again later.",
                    len(self._ready),
                    pending,
                )
        self._generation += 1
        for task in self._tasks:
            task.cancel()
        self._task = None
        self._worker = None
        self._history = None
        self._ready.clear()
        self.defer_merges = False

    async def close(self) -> None:
        self.cancel()
        if self.strategy is not None:
            self.strategy._background = None  # pyright: ignore[reportPrivateUsage]
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def retain(self) -> bool:
        """Detach this run's answers and close admission for its worker."""
        self._history = None
        if self.strategy is None or (self._task is None and not self._ready):
            return False
        self.strategy._background = None  # pyright: ignore[reportPrivateUsage]
        self.defer_merges = self._task is not None and not self._task.done()
        if self._worker and (requests := self._worker._background_requests):  # pyright: ignore[reportPrivateUsage]
            requests.finish_run()
        return True

    async def start(self, response: list[Message]) -> None:
        if not any(c.type == "function_call" for m in response for c in m.contents):
            # The end-of-run pass can still consume a ready answer.
            self._history = None
            if self._task is None or self._task.done():
                self.defer_merges = False
            return
        if self._history is None or self.strategy is None:
            # No worker can start, so the foreground keeps its merges.
            return
        self.defer_merges = True
        if self._task is not None:
            self._history = None
            return
        generation = self._generation
        history, self._history = self._history, None
        strategy = self.strategy
        budget = self._budget

        def snapshot() -> BackgroundRecords:
            if not has_records(history) and not has_records(response):
                return BackgroundRecords([], {}, 0, {})
            messages = counted_history(history, response, strategy.tokenizer)
            with _yielding_when(
                over_budget=included_token_count(messages) > budget, policy=strategy.overflow_policy
            ):
                return BackgroundRecords.capture(messages)

        try:
            records = await off_loop(self._in_run_context, snapshot)
        except Exception as error:
            self.defer_merges = False
            self._in_run_context(strategy._merge_event, "worker_failed", phase="background")  # pyright: ignore[reportPrivateUsage]
            self._in_run_context(
                logger.warning, "Background record snapshot failed (%s).", type(error).__name__
            )
            return
        if generation != self._generation or self._task is not None:
            return
        prompt = self.strategy.merge_prompt
        self._in_run_context(strategy._merge_event, "worker_started", phase="background")  # pyright: ignore[reportPrivateUsage]

        def ready(sources: str, count: int, transcript: str, answer: _RecordAnswer) -> None:
            if generation == self._generation:
                self._ready.append(_PreparedMerge(sources, count, transcript, answer, prompt))
                if record_merge_observer.get() is not None:
                    worker._merge_event(  # pyright: ignore[reportPrivateUsage]
                        "answer_prepared", records=count, batch=_digest(f"{sources}:{answer.text}")
                    )

        worker = self.strategy._record_worker(  # pyright: ignore[reportPrivateUsage]
            budget,
            partial(self._in_run_context, ready),
            lambda: generation == self._generation,
        )
        worker._background_records = records  # pyright: ignore[reportPrivateUsage]
        self._worker = worker
        self._worker_steps = threading.Lock()
        self._task = asyncio.create_task(
            self._run_worker(worker, records.messages, self._worker_steps),
            context=self._run_context.copy(),
        )
        self._tasks.add(self._task)
        self._task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run_worker(
        worker: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
        messages: list[Message],
        steps: threading.Lock,
    ) -> None:
        try:
            with steps_locked(steps):
                await worker._prepare_record_merges(messages)  # pyright: ignore[reportPrivateUsage]
        finally:
            worker._background_records = None  # pyright: ignore[reportPrivateUsage]
            worker._record_answer_ready = None  # pyright: ignore[reportPrivateUsage]
            worker._record_work_current = None  # pyright: ignore[reportPrivateUsage]

    async def apply_ready(self, messages: list[Message]) -> bool:
        strategy = self.strategy
        if strategy is None:
            return False
        prepared, self._ready = self._ready, []
        changed = await strategy._apply_record_answers(  # pyright: ignore[reportPrivateUsage]
            messages, prepared
        )
        # A worker step still writing what the search found is not read half way: the next
        # pass reads it instead. A finished worker holds no step, so its search is always read.
        if self._worker is not None and self._worker_steps.acquire(blocking=False):
            try:
                strategy._remember_record_search(self._worker)  # pyright: ignore[reportPrivateUsage]
            finally:
                self._worker_steps.release()
        if self._task is not None and self._task.done():
            worker = self._worker
            self._worker = None
            self._task = None
            if worker is not None:
                if worker._background_requests and worker._background_requests.closed:  # pyright: ignore[reportPrivateUsage]
                    # A late answer may have arrived while the first answers were being applied.
                    tail, self._ready = self._ready, []
                    changed = await strategy._apply_record_answers(messages, tail) or changed  # pyright: ignore[reportPrivateUsage]
                    self.defer_merges = False
                changed = (
                    await strategy._apply_completed_merge_sweep(  # pyright: ignore[reportPrivateUsage]
                        messages, worker
                    )
                    or changed
                )
        return changed


class CompactionBudgetExceeded(ValueError):
    """The composed strategy could not fit the prompt without removing protected material."""

    def __init__(self, tokens: int, budget: int) -> None:
        self.tokens = tokens
        self.budget = budget
        super().__init__(
            f"Compacted prompt has {tokens} tokens, exceeding max_input_tokens={budget}."
        )


class CompactionPendingResultBudgetExceeded(CompactionBudgetExceeded):
    """Budget refusal with unstored results for calls already in history.

    ``pending_results`` retains the input message objects. The host must persist those results
    or roll back their stored calls before retrying the conversation.
    """

    def __init__(self, tokens: int, budget: int, pending_results: tuple[Message, ...]) -> None:
        self.pending_results = pending_results
        super().__init__(tokens, budget)


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
_Consolidation = Literal[
    "accepted", "rejected", "lossy", "incomplete", "failed", "skipped", "rate_limited"
]
_MergeStop = Literal["failed", "rate_limited"]

#: Shared time allowance for the chain's merges, user fold and record rewrites.
DEFAULT_CHAIN_SECONDS: Final[float] = 30.0

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

#: Share of the records a harder rewrite replaces that it must remove to be kept.
#:
#: **A tenth, a quarter of what the mildest attempt asks for.** A kept rewrite is a new record
#: with every attempt open again, so on a prompt that stays over the budget an answer that
#: trims a hundredth buys ``harder_attempts`` more requests on the next pass, and on the one
#: after. When each kept rewrite removes a tenth, the first attempt's aim is met in five
#: requests; when each removes a hundredth, it takes fifty. An answer under the floor is refused
#: like one that is no smaller, and the record stands until it changes. Zero keeps any smaller
#: answer.
DEFAULT_REWRITE_GAIN_FRACTION: Final[float] = 0.1

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

#: Batch prefixes one record-merge sweep may render and count while looking for batches.
#:
#: Finding a batch of ``k`` records takes about ``2 * log2(k)`` of them, so this is some sixteen
#: batches of eight, more than a sweep that stops at the record share needs. What it bounds is
#: a search over longer runs from a start whose pair does not fit, which runs only with
#: ``run_past_limit_factor`` above one or ``None``: token counts need not grow with text, so
#: there one failed prefix proves nothing. A sweep that spends them stops, and the next one
#: resumes each start where this one left it, so it goes on from where the last left off. A
#: record that does not fit alone is passed over without a search and costs none of them.
_MAX_BATCH_PROBES: Final[int] = 128

#: How far past the request cap a run may count and still be searched over longer runs.
#:
#: **One: a run over the cap ends the search from its start**, which goes on to the runs starting
#: at the next record. Appending a record adds its text, so with an ordinary tokenizer no longer
#: run from the same start fits once one does not, and searching on would cost one probe per
#: remaining record: a front of records too large to merge in pairs then spent every sweep before
#: the search reached the records behind it.
DEFAULT_RUN_PAST_LIMIT_FACTOR: Final[float] = 1.0

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


def _rewrite_percent(attempt: int) -> int:
    return round(100 * _HARDER_RATIO**attempt)


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
    percent = _rewrite_percent(attempt)
    return (
        "Rewrite the compaction record below so that it is at most about "
        f"{percent}% of its current length. Keep every identifier, code, number, date, name and "
        "other exact value verbatim: do not paraphrase, round, abbreviate or drop any of them. "
        "Cut wording instead -- repetition, explanation, connecting prose and formatting. If "
        "several numbered records are given, write one record that carries the values of all of "
        "them. Reply with the record text only."
    )


#: Share of the output cap a record answer is asked to stay within. Reasoning spends part of the
#: same allowance on providers that count it as output, and a model's sense of its own length is
#: approximate.
RECORD_ANSWER_BUDGET_SHARE: Final[float] = 0.8
#: Words per token for stating the budget in words too: a model judges words better than tokens,
#: and record text, dense with identifiers and numbers, runs below the prose rate of 0.75.
_RECORD_WORDS_PER_TOKEN: Final[float] = 0.6

#: Why a merge batch is not asked when its required values cannot fit the answer budget.
_SKIP_NO_ANSWER_ROOM: Final[str] = "required_values_over_budget"
#: A batch's distinct values, the values its merge must keep, their tokens and the answer budget.
_AnswerOverflow = tuple[int, int, int, int]


def _record_answer_tokens(max_tokens: int) -> int:
    return int(max_tokens * RECORD_ANSWER_BUDGET_SHARE)


def record_answer_budget(max_tokens: int) -> str:
    """Return the sentence that tells the summarizer how long its record may be.

    ``max_tokens`` is a hard stop the provider applies and never shows the model, so without
    this a model writes the record its prompt asks for and is cut off; a cut-off record is
    refused, since it would lose values. Told the budget, the model can fit the record to it,
    and keeping most values is better than keeping none: an answer that keeps the coverage
    share is accepted, and one that does not is split like a cut-off one.

    Args:
        max_tokens: The output cap the request is sent with.

    Returns:
        A sentence to append to a merge or rewrite prompt.
    """
    budget = _record_answer_tokens(max_tokens)
    return (
        f" Your reply is cut off after {max_tokens:,} tokens, and a reply that is cut off is "
        f"discarded whole. Keep the record within about {budget:,} tokens, roughly "
        f"{int(budget * _RECORD_WORDS_PER_TOKEN):,} words; if every value cannot fit, keep as "
        "many as fit and cut everything else first, rather than run past it."
    )


def _meets_gain_floor(saved: int, replaced: int, fraction: float) -> bool:
    """Return whether ``saved`` of ``replaced`` tokens is at least ``fraction`` of them.

    Compared as a ratio. The product ``fraction * replaced`` can round up past a saving that
    meets the floor exactly, as ``0.14 * 100`` does, and would refuse it.
    """
    return saved / replaced >= fraction


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


def _same_tool_segment_ends(tools: list[frozenset[str]]) -> list[int]:
    """Return, per record, where the run a merge batch from it may take ends.

    Two or more adjacent records covering the same tools form a run of their own; the records
    between such runs form one together. A batch never crosses from one run into another.
    """
    ends = [0] * len(tools)
    start = 0
    while start < len(tools):
        end = start + 1
        while end < len(tools) and tools[end] == tools[start]:
            end += 1
        if end - start < 2:
            end = start + 1
            while end < len(tools) and not (end + 1 < len(tools) and tools[end] == tools[end + 1]):
                end += 1
        for index in range(start, end):
            ends[index] = end
        start = end
    return ends


def _numbered_records(bodies: list[str]) -> str:
    return "\n".join(f"{number}. {body}" for number, body in enumerate(bodies, start=1))


def _record_values(bodies: list[str]) -> set[str]:
    return {value for body in bodies for value in _distinctive_tokens(body)}


def _has_prefix(ordered: list[str], prefix: str) -> bool:
    """Return whether any of the sorted ``ordered`` starts with ``prefix``.

    The strings that do sort together, from the first at or after ``prefix``, so that one is
    the only one to read.
    """
    index = bisect_left(ordered, prefix)
    return index < len(ordered) and ordered[index].startswith(prefix)


def _digest(text: str) -> str:
    """Key a decision by a record's text without storing the text in session state."""
    return hashlib.sha256(text.encode()).hexdigest()


#: A Mersenne prime and a fixed base for the polynomial hash a batch sweep combines record
#: digests with: shared prefix sums give the digest of any run of records in constant time, so
#: a start checks what it searched against the records standing without hashing them again.
_RUN_MODULUS: Final[int] = (1 << 521) - 1
_RUN_BASE: Final[int] = int(hashlib.sha256(b"maf-compaction record runs").hexdigest(), 16)


def _is_excluded(message: Message) -> bool:
    """Return whether ``message`` carries the exclusion flag."""
    return bool(message.additional_properties.get(EXCLUDED_KEY, False))


def _yielding_when(*, over_budget: bool, policy: str) -> AbstractContextManager[None]:
    """The reasoning-prefix guard yields only over budget under the ``"shed"`` policy.

    That shed edits the prefix anyway. Under ``"raise"`` the host keeps its local refusal.
    """
    return (
        _reasoning_prefix_yields_to_budget() if over_budget and policy == "shed" else nullcontext()
    )


@dataclass(frozen=True, slots=True)
class _Searched:
    """How far a batch search from one start record got, and what it found there."""

    #: Records from the start the search's answer was decided on.
    covered: int
    #: The digest of those records' run, so the answer holds only while they stand unchanged.
    records: str
    #: The batch found, in records, or 0 for no run within ``covered`` that fits.
    batch: int = 0
    #: Whether where the records ended decided the answer too, so a record appended reopens it.
    ended: bool = False
    #: The ``run_past_limit_factor`` the run of ``covered`` records counted past, so no longer
    #: run from this start is searched while they stand and the factor is no larger; None if not.
    past_limit: float | None = None


@dataclass(frozen=True, slots=True)
class _PendingMerge:
    """A smaller batch still awaiting its result after an interrupted split."""

    count: int
    records: str
    depth: int
    prompt: str
    limit: int


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
        rewrite_gain_fraction: Share of the records a harder rewrite replaces that it must
            remove to be kept, in ``[0.0, 1.0)``. See :data:`DEFAULT_REWRITE_GAIN_FRACTION`;
            zero keeps any smaller answer.
        merge_prompt: What the summarizer is asked when the records are merged. Defaults to
            :data:`DEFAULT_RECORD_MERGE_PROMPT` with the version captured by ``user_turns``.
        rewrite_prompt: A pure function of the one-based attempt, returning a nonempty system
            prompt with progressively stronger shortening requests. Defaults to
            :func:`harder_record_prompt` with the version captured by ``user_turns``.
            Custom prompts own their data/instruction boundary.
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
        chain_seconds: Positive finite seconds shared by the last-resort chain's merge,
            fold and rewrite requests, including retries. Defaults to 30; ``None`` disables
            the admission deadline. In-flight requests may finish after it. Earlier user-turn
            summaries and local fallback work are outside it.
        record_merge_split_depth: Maximum recursive halvings of a lossy or truncated merge
            batch, or of one not asked because its required values cannot fit the answer
            budget, default two. Zero disables smaller retries. Every child holds at least
            two records and shares the chain deadline.
        run_past_limit_factor: How many times the request cap a run of records may count and
            still be searched over longer runs from the same start. The default, one, ends the
            search from a start at its first run over the cap and goes on to the next record.
            ``None`` searches every longer run, for a tokenizer whose count can fall as records
            are appended.
        merge_same_tool_first: Merge adjacent records that cover the same tools apart from
            their neighbours, so two or three records of one tool's results merge into one
            before records of different tools are merged together. Default on.
        record_request_input_tokens: The tokens one record merge or rewrite request may hold,
            in place of ``summary_options["max_tokens"]``, which otherwise bounds both the
            answer and the request. Set it for a summarizer whose merges compress, for example
            twice the output cap. Never more than ``max_summary_input_tokens``.
        record_merge_max_records: Most records one merge or rewrite request may hold, at least two.
            ``None``, the default, bounds a batch by its request tokens alone. A batch of many
            small records fits the request limit yet holds more values than one answer keeps,
            so it is refused and split; the cap asks for the batches the summarizer can keep.
            Pending splits larger than the cap are dropped and searched again.
        merge_coverage_share: Starting value coverage for merges and rewrites, defaulting to
            ``tool_results.coverage_share`` without changing tool-group grounding.
        merge_coverage_floor: Lowest merge coverage, defaulting to the starting bar (no
            relaxation). Smaller splits are tried before relaxing under record-share pressure.
        merge_coverage_step: Coverage decrement after an exhausted sweep, default 0.05.
        state_record_budget: Tell the summarizer the output cap a merge or rewrite request is
            sent with, by appending :func:`record_answer_budget` to its prompt. Default on: the
            cap is a hard stop the model never sees, so without it a record longer than the cap
            is cut off and refused. Requests sent without a cap are unchanged, a retry that
            drops the options included, and so is one the sentence would take over
            ``max_summary_input_tokens``.
        merge_answer_cache_bytes: Maximum serialized UTF-8 bytes of refused merge answers,
            default 1 MiB. Oldest answers are evicted first; zero disables retention.

    Raises:
        ValueError: If the two phases measure against different ceilings or with different
            tokenizer objects, if an explicit
            ``user_trigger_fraction`` is outside ``(0.0, 1.0]``, or if ``harder_attempts`` is
            negative, or if ``chain_gain_fraction`` or ``rewrite_gain_fraction`` is outside
            ``[0.0, 1.0)``, or
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
        rewrite_prompt: Callable[[int], str] | None = None,
        chain_gain_fraction: float = DEFAULT_CHAIN_GAIN_FRACTION,
        overflow_policy: Literal["shed", "raise"] = "shed",
        estimate_margin: float = 0.0,
        record_budget_share: float | None = DEFAULT_RECORD_BUDGET_SHARE,
        chain_seconds: float | None = DEFAULT_CHAIN_SECONDS,
        record_merge_split_depth: int = 2,
        run_past_limit_factor: float | None = DEFAULT_RUN_PAST_LIMIT_FACTOR,
        record_request_input_tokens: int | None = None,
        record_merge_max_records: int | None = None,
        merge_same_tool_first: bool = True,
        rewrite_gain_fraction: float = DEFAULT_REWRITE_GAIN_FRACTION,
        merge_coverage_share: float | None = None,
        merge_coverage_floor: float | None = None,
        merge_coverage_step: float = 0.05,
        merge_answer_cache_bytes: int = 1_048_576,
        state_record_budget: bool = True,
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
        if isinstance(rewrite_gain_fraction, bool) or not 0.0 <= rewrite_gain_fraction < 1.0:
            raise ValueError("rewrite_gain_fraction must be in [0.0, 1.0).")
        if overflow_policy not in {"shed", "raise"}:
            raise ValueError("overflow_policy must be 'shed' or 'raise'.")
        if isinstance(estimate_margin, bool) or not 0.0 <= estimate_margin < 1.0:
            raise ValueError("estimate_margin must be in [0.0, 1.0).")
        if record_budget_share is not None and (
            isinstance(record_budget_share, bool) or not 0.0 < record_budget_share <= 1.0
        ):
            raise ValueError("record_budget_share must be in (0.0, 1.0] or None.")
        self.record_budget_share = record_budget_share
        if chain_seconds is not None and (
            isinstance(chain_seconds, bool) or not isfinite(chain_seconds) or chain_seconds <= 0
        ):
            raise ValueError("chain_seconds must be positive and finite or None.")
        if type(record_merge_split_depth) is not int or record_merge_split_depth < 0:
            raise ValueError("record_merge_split_depth must be a non-negative integer.")
        if run_past_limit_factor is not None and (
            isinstance(run_past_limit_factor, bool)
            or not isfinite(run_past_limit_factor)
            or run_past_limit_factor < 1
        ):
            raise ValueError("run_past_limit_factor must be finite and at least 1, or None.")
        if record_request_input_tokens is not None and (
            type(record_request_input_tokens) is not int or record_request_input_tokens <= 0
        ):
            raise ValueError("record_request_input_tokens must be a positive integer or None.")
        if record_merge_max_records is not None and (
            type(record_merge_max_records) is not int or record_merge_max_records < 2
        ):
            raise ValueError("record_merge_max_records must be an integer of at least 2, or None.")
        self.record_merge_max_records = record_merge_max_records
        self.chain_seconds = chain_seconds
        self.record_merge_split_depth = record_merge_split_depth
        self.run_past_limit_factor = run_past_limit_factor
        self.record_request_input_tokens = record_request_input_tokens
        self.merge_same_tool_first = merge_same_tool_first
        self.state_record_budget = state_record_budget
        self._chain_deadline: ContextVar[float | None] = ContextVar("chain_deadline", default=None)
        self._role_measurement: ContextVar[ReferenceType[RecordRolePass] | None] = ContextVar(
            "record_role_measurement", default=None
        )
        self._chain_timeouts = 0
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
        self.rewrite_gain_fraction = rewrite_gain_fraction
        prompt_version = user_turns.prompt_version
        self.merge_prompt = merge_prompt or versioned_prompt(
            DEFAULT_RECORD_MERGE_PROMPT, prompt_version
        )
        self.rewrite_prompt: Callable[[int], str] = (
            (lambda attempt: versioned_prompt(harder_record_prompt(attempt), prompt_version))
            if rewrite_prompt is None
            else rewrite_prompt
        )
        self.chain_gain_fraction = chain_gain_fraction
        self._inherit_merge_coverage = merge_coverage_share is None
        self._inherit_merge_floor = merge_coverage_floor is None
        coverage = (
            tool_results.coverage_share if merge_coverage_share is None else merge_coverage_share
        )
        self._merge_coverage = MergeCoverage(
            coverage,
            coverage if merge_coverage_floor is None else merge_coverage_floor,
            merge_coverage_step,
            merge_answer_cache_bytes,
        )
        self._merge_coverage.bind(_digest(self.merge_prompt))
        self._record_consolidations_by_coverage: dict[float, int] = {}
        self._record_values_omitted = 0
        self._merge_sweep_complete = False
        self._completed_merge_sweep: str | None = None
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
        # Content caches can be reused only where current reasoning guards permit edits.
        self._reshaped: set[str] = set()
        # Only source-bound decisions may replay across retained reasoning.
        self._record_order: list[RecordEdit] = []
        self._shed_ids: set[str] = set()
        self._shed_groups: set[str] = set()
        # Refusals apply to the same sources and prompt; accepted edits retain their history.
        self._refused_merges: set[tuple[str, int, str]] = set()
        self._split_merges: set[tuple[str, int, str]] = set()
        # Per start record (body digest, cap): how far the batch search from it got, so a later
        # sweep resumes it, or takes the batch it found, only while those records stand.
        self._searched: dict[tuple[str, int], _Searched] = {}
        self._search_prompt: str | None = None
        self._record_input_fits: dict[tuple[str, str, int], bool] = {}
        self._batch_resume: tuple[str, int, int, str, int] | None = None
        self._pending_merges: list[_PendingMerge] = []
        self._refused_rewrites: dict[tuple[str, int, str], int] = {}
        self._background_records: BackgroundRecords | None = None
        self._background: _RecordMergeWork | None = None
        self._record_answer_ready: Callable[[str, int, str, _RecordAnswer], None] | None = None
        self._record_work_current: Callable[[], bool] | None = None
        self._background_requests: _BackgroundRequests | None = None
        # The list the last per-call pass left, for the next call to start from; see
        # ``_carryover``. Process-local, and never part of ``session_state``.
        self._carried: CarriedView | None = None
        self._saved_view: dict[str, Any] | None = None
        self._calls_resumed = 0
        # Every phase of the pass in hand has run, and only the budget verdict is left.
        self._settled = False

    def _record_worker(
        self,
        budget: int,
        ready: Callable[[str, int, str, _RecordAnswer], None],
        current: Callable[[], bool],
    ) -> ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy:
        worker = _worker_copy(
            self,
            "_refused_merges",
            "_split_merges",
            "_searched",
            "_record_input_fits",
            "_pending_merges",
        )
        # Every container it can write is its own, so its steps and the foreground's run on
        # worker threads side by side (_offload).
        worker.tool_results = _worker_copy(self.tool_results)
        worker.tool_results.fallback = _worker_copy(self.tool_results.fallback)
        worker.user_turns = _worker_copy(self.user_turns)
        worker.user_turns.summary_options = deepcopy(self.user_turns.summary_options)
        worker._background = None
        worker._merge_coverage = copy(self._merge_coverage)
        worker._merge_coverage.answers = []
        worker._carried = None
        worker._saved_view = None
        worker._request_budget = ContextVar("background_record_budget", default=budget)
        worker._chain_deadline = ContextVar("background_record_deadline", default=None)
        worker._record_answer_ready = ready
        worker._record_work_current = current
        worker._background_requests = _BackgroundRequests(self.user_turns.summary_timeout_seconds)
        return worker

    def _merge_event(
        self,
        event: str,
        *,
        phase: RecordMergePhase | None = None,
        details: Mapping[str, int | str] | None = None,
        **counts: int | str,
    ) -> None:
        observer = record_merge_observer.get()
        if observer is not None:
            try:
                observer(
                    phase
                    or ("background" if self._record_answer_ready is not None else "foreground"),
                    event,
                    counts if details is None else {**counts, **details},
                )
            except Exception as error:
                logger.warning("Record merge observer failed (%s).", type(error).__name__)

    async def _prepare_record_merges(self, messages: list[Message]) -> None:
        self._completed_merge_sweep = None
        deadline = None if self.chain_seconds is None else monotonic() + self.chain_seconds
        token = self._chain_deadline.set(deadline)
        try:
            await off_loop(self._annotate_messages, messages)
            start = _ChainStart.take(messages) if await off_loop(self._over, messages) else None
            if start is None and self.record_budget_share is None:
                self._merge_event("no_pressure")
                return
            with _yielding_when(over_budget=start is not None, policy=self.overflow_policy):
                await self._merge_records(messages, start)
                if self._merge_coverage.adaptive and self._merge_sweep_complete:
                    self._completed_merge_sweep = self._merge_sweep_key(messages)
        except _RunFinished:
            # Closing admission is expected. Prepared answers still belong to the foreground.
            return
        except _ChainTimeSpent:
            # Ready answers remain available to the next foreground pass.
            self._merge_event("deadline")
            logger.info(
                "Background record merge stopped at its %.3f-second deadline; ready answers "
                "stay for the next pass and the search resumes on the next tool round.",
                self.chain_seconds,
            )
        except Exception as error:
            self._merge_event("worker_failed")
            logger.warning("Background record merge stopped (%s).", type(error).__name__)
        finally:
            self._chain_deadline.reset(token)

    def _discard_ready_answer(self, item: _PreparedMerge, reason: str) -> None:
        """Drop a ready background answer the pass cannot apply, and say so in the log.

        The batch is asked again on a later sweep, so a host without an observer needs the line
        to see that a summarizer answer was paid for and not kept.
        """
        self._merge_event("answer_discarded", phase="apply", reason=reason)
        logger.info(
            "Compaction discarded a ready background merge of %d records: %s.", item.count, reason
        )

    async def _apply_record_answers(
        self, messages: list[Message], prepared: list[_PreparedMerge]
    ) -> bool:
        changed = False
        for item in prepared:
            if item.prompt != self.merge_prompt:
                self._discard_ready_answer(item, "prompt_changed")
                continue
            batch = await off_loop(
                _background_batch_with_sources, messages, item.count, item.sources
            )
            if batch is None:
                self._discard_ready_answer(item, "sources_unavailable")
                continue
            outcome = await self._consolidate(
                messages,
                batch,
                prompt=self.merge_prompt,
                transcript=item.transcript,
                answer=item.answer,
                remember_lossy=True,
                merge_phase="apply",
                prepared_sources=item.sources,
            )
            if outcome == "accepted":
                self._records_merged += 1
                changed = True
            elif outcome in {"rejected", "lossy", "incomplete"}:
                key = item.transcript, item.count, _digest(item.prompt)
                self._record_merges_rejected += 1
                self._refused_merges.add(key)
                if outcome in {"lossy", "incomplete"}:
                    self._split_merges.add(key)
        return changed

    def _remember_record_search(
        self, worker: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
    ) -> None:
        if worker.merge_prompt != self.merge_prompt:
            return
        if worker.user_turns._summary_options_disabled:  # pyright: ignore[reportPrivateUsage]
            self.user_turns._summary_options_disabled = True  # pyright: ignore[reportPrivateUsage]
        if self._search_prompt != worker._search_prompt:
            self._searched.clear()
        self._search_prompt = worker._search_prompt
        self._searched.update(worker._searched)
        self._record_input_fits.update(worker._record_input_fits)
        self._batch_resume = worker._batch_resume
        self._pending_merges = list(worker._pending_merges)

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
        """Put back decisions taken with :meth:`decision_state`, chain and record half alike.

        Decisions other than the ones in force let go of the list the last per-call pass left.
        """
        if self._carried is not None and decisions != self.decision_state():
            self._carried = None
        self._put_decisions(decisions)

    def _put_decisions(self, decisions: ChainDecisions) -> None:
        self._wait_since = decisions.wait_since
        self._anchor = decisions.anchor
        self._quiet_through = decisions.quiet_through
        self._declined_behind = decisions.declined_behind
        self.tool_results.restore_decisions(decisions.records)

    def session_state(self) -> dict[str, Any]:
        """Return JSON-safe behavioral state for session persistence."""
        self._sync_merge_coverage()
        return {
            "decisions": self.decision_state().to_dict(),
            "user_turns": self.user_turns.session_state(),
            "merge_coverage": self._merge_coverage.snapshot(),
            "kept_records": [
                {"transcript": transcript, "source_count": count, "text": text}
                for (transcript, count), text in self._kept_records.items()
            ],
            "reshaped_records": sorted(self._reshaped),
            "record_order": [asdict(edit) for edit in self._record_order],
            "searched_records": [
                [
                    start,
                    limit,
                    found.covered,
                    found.records,
                    found.batch,
                    found.ended,
                    found.past_limit,
                ]
                for (start, limit), found in sorted(self._searched.items())
            ],
            "search_prompt": self._search_prompt,
            "record_input_fits": [
                [prompt, record, limit, fits]
                for (prompt, record, limit), fits in sorted(self._record_input_fits.items())
            ],
            "batch_resume": list(self._batch_resume) if self._batch_resume is not None else None,
            "pending_merges": [
                [pending.count, pending.records, pending.depth, pending.prompt, pending.limit]
                for pending in self._pending_merges
            ],
            "shed_ids": sorted(self._shed_ids),
            "shed_groups": sorted(self._shed_groups),
            "refused_merges": [
                {"transcript": transcript, "source_count": count, "prompt": prompt}
                for transcript, count, prompt in sorted(self._refused_merges)
            ],
            "split_merges": [
                {"transcript": transcript, "source_count": count, "prompt": prompt}
                for transcript, count, prompt in sorted(self._split_merges)
            ],
            "refused_rewrites": [
                {
                    "transcript": transcript,
                    "source_count": count,
                    "prompt": prompt,
                    "attempt": attempt,
                }
                for (transcript, count, prompt), attempt in self._refused_rewrites.items()
            ],
        }

    def restore_session_state(self, data: dict[str, Any]) -> None:
        """Replace behavioral state with a snapshot from :meth:`session_state`.

        A snapshot other than the one the strategy would write now lets go of the list the last
        per-call pass left: that list belongs to the decisions that made it.
        """
        if self._carried is not None and data != self.session_state():
            self._carried = None
        self._put_decisions(ChainDecisions.from_dict(data["decisions"]))
        self.user_turns.restore_session_state(data["user_turns"])
        self._sync_merge_coverage()
        self._merge_coverage.restore(data.get("merge_coverage", {}))
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
        self._record_order = [RecordEdit(**edit) for edit in data.get("record_order", [])]
        # Earlier snapshots of this schema kept only searches that found nothing.
        searched = data.get("searched_records", data.get("unpaired_records", []))
        self._searched = {}
        for entry in searched:
            if len(entry) not in (4, 6, 7):
                continue
            fields = list(entry[2:])
            if len(fields) == 5 and not (
                fields[4] is None
                or (isinstance(fields[4], (int, float)) and not isinstance(fields[4], bool))
            ):
                # A stop saved without the factor that made it cannot say when it still holds.
                fields[4] = None
            self._searched[(entry[0], entry[1])] = _Searched(*fields)
        self._search_prompt = data.get("search_prompt")
        self._record_input_fits = {
            (entry[0], entry[1], entry[2]): entry[3] for entry in data.get("record_input_fits", [])
        }
        resume = data.get("batch_resume")
        self._batch_resume = cast(tuple[str, int, int, str, int], tuple(resume)) if resume else None
        self._pending_merges = [_PendingMerge(*entry) for entry in data.get("pending_merges", [])]
        self._shed_ids = {value for value in data["shed_ids"] if durable_message_id(value)}
        self._shed_groups = set(data.get("shed_groups", []))
        # A refusal without its source count and prompt cannot suppress another request.
        merges: list[dict[str, Any] | str] = data["refused_merges"]
        self._refused_merges = {
            (entry["transcript"], entry["source_count"], entry["prompt"])
            for entry in merges
            if not isinstance(entry, str) and "source_count" in entry and "prompt" in entry
        }
        self._split_merges = (
            {
                (entry["transcript"], entry["source_count"], entry["prompt"])
                for entry in data.get("split_merges", [])
                if "source_count" in entry and "prompt" in entry
            }
            & self._refused_merges
            if "split_merges" in data
            else set(self._refused_merges)
        )
        rewrites: dict[str, int] | list[dict[str, Any]] = data["refused_rewrites"]
        self._refused_rewrites = (
            {}
            if isinstance(rewrites, dict)
            else {
                (entry["transcript"], entry["source_count"], entry["prompt"]): entry["attempt"]
                for entry in rewrites
                if "source_count" in entry and "prompt" in entry
            }
        )

    @property
    def merge_coverage_share(self) -> float:
        """Starting coverage for record merges and rewrites."""
        self._sync_merge_coverage()
        return self._merge_coverage.start

    @property
    def merge_coverage_floor(self) -> float:
        """Lowest coverage allowed under record-share pressure."""
        self._sync_merge_coverage()
        return self._merge_coverage.floor

    @property
    def merge_coverage_step(self) -> float:
        """Size of one coverage decrement."""
        return self._merge_coverage.step

    @property
    def merge_answer_cache_bytes(self) -> int:
        """Maximum serialized UTF-8 bytes retained for refused merge answers."""
        return self._merge_coverage.cache_bytes

    @property
    def current_merge_coverage_share(self) -> float:
        """Current acceptance bar, persisted with the session."""
        self._sync_merge_coverage()
        return self._merge_coverage.current

    @property
    def record_consolidations_by_coverage(self) -> dict[float, int]:
        """Newly accepted merges and rewrites at each bar on this instance, excluding replay."""
        return dict(self._record_consolidations_by_coverage)

    @property
    def record_values_omitted(self) -> int:
        """Sum of distinctive source values omitted by newly accepted merges and rewrites."""
        return self._record_values_omitted

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
        """Of :attr:`record_rewrites`, those refused as no smaller, as saving too little, or lossy.

        ``RECHARDERREJ`` in the flags.

        Counted once per refusal, as :attr:`record_merges_rejected` is: the attempt met again on
        the same record is skipped, under :attr:`record_rewrites_skipped`.
        """
        return self._record_rewrites_rejected

    @property
    def record_merges_skipped(self) -> int:
        """Merges not asked for after a refusal or for lack of answer room for required values.

        Step a skipped for :meth:`_merge_records`'s reason, per pass, or because the shortest
        text holding the values a batch must keep exceeds the answer budget of its capped
        request; see :meth:`_merge_batch`. On the live path the store pass skips what the copies
        pass refused, so :attr:`record_merges_rejected` counts each refusal once.
        ``RECMERGESKIP`` in the flags.
        """
        return self._record_merges_skipped

    @property
    def record_rewrites_skipped(self) -> int:
        """Harder rewrite attempts skipped after a refusal or for lack of room for required values.

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
    def chain_timeouts(self) -> int:
        """Passes whose last-resort summarizer work exhausted its shared deadline."""
        return self._chain_timeouts

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
    def calls_resumed(self) -> int:
        """Per-call passes that started from the list the previous call's pass left.

        A host that hands every model call a rebuilt list -- copies of its stored messages, or
        the tool loop's list without what a pass inserted -- would otherwise have every
        decision put back on every call; see :mod:`._carryover`. A pass that resumes finds its
        records, notes and exclusions standing, so it does not add to
        :attr:`chain_decisions_kept`.
        """
        return self._calls_resumed

    @property
    def carries_view(self) -> bool:
        """Whether the list the last per-call pass left is being kept for the next call."""
        return self._carried is not None

    def forget_carried_view(self) -> None:
        """Release the list kept for the next call; the next pass puts its decisions back.

        The strategy's own restore methods do this for a state other than the one it holds. A
        host that restores one half on its own, through ``tool_results`` or ``user_turns``,
        calls this itself.
        """
        self._carried = None
        self._saved_view = None

    def restore_saved_view(self, saved: dict[str, Any]) -> None:
        """Offer a layout from trusted session storage for the next pass to match to history."""
        self._carried = None
        self._saved_view = saved

    def take_saved_view(self) -> dict[str, Any] | None:
        """Release the kept list and return its portable layout when it can be represented."""
        carried, self._carried = self._carried, None
        self._saved_view = None
        if carried is None:
            return None
        decisions = self.session_state()
        if (
            carried.decisions_before_storage is not None
            and carried.decisions_before_storage != decisions
        ):
            return None
        return save_view(carried, decisions)

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
        """Compact outgoing messages, optionally starting a turn from a previously saved view."""
        saved_view, self._saved_view = self._saved_view, None
        try:
            budget = self.sending_budget(options)
        except CompactionBudgetExceeded as exc:
            if pending := pending_result_messages(messages):
                raise CompactionPendingResultBudgetExceeded(
                    exc.tokens, exc.budget, pending
                ) from exc
            raise
        handed = await off_loop(Handed.take, messages)
        carried, self._carried = self._carried, None
        resumed = await off_loop(carried.resume, messages, handed) if carried is not None else None
        if carried is None and saved_view is not None:
            resumed = await off_loop(
                resume_saved_view, saved_view, self.session_state(), messages, handed
            )
        if resumed is not None and resumed.restored:
            self._calls_resumed += 1
        # Handed back as the last pass left it, the list is no account of the stored messages.
        continuing = carried if resumed is not None and not resumed.restored else None
        self._settled = False
        token = self._request_budget.set(budget)
        steady = carried.settled_tools if resumed is not None and carried is not None else None
        try:
            if (
                messages
                and resumed is not None
                and type(self) is ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy
                and type(self.tool_results) is ToolResultAnchoredSummarizationCompactionStrategy
                and type(self.user_turns) is UserTurnAnchoredSummarizationCompactionStrategy
                and type(self.tool_results.fallback) is AnchoredCompactionStrategy
                and self.tool_results.rewrite_result_text is None
                and self.tool_results.tokenizer is self.tokenizer
                and structural_counter(self.tokenizer) is None
            ):
                changed, steady = await self._continued(messages, steady)
            else:
                changed = await self(messages)
                steady = None
        except CompactionBudgetExceeded:
            # The closing budget check refuses a list every phase has already worked on.
            if self._settled:
                self._carried = await off_loop(CarriedView, handed, messages, continuing=continuing)
            raise
        finally:
            self._request_budget.reset(token)
        if changed or resumed is not None:
            self._carried = await off_loop(CarriedView, handed, messages, continuing=continuing)
            if len(self._carried.view) == len(messages):
                self._carried.settled_tools = steady
        return changed or (resumed is not None and resumed.changed)

    def _round_policy(self) -> tuple[Any, ...]:
        tool = self.tool_results
        user = self.user_turns
        # The journal and shed sets only grow between restores. Restoring different state
        # releases the entire carried view.
        return (
            len(self._record_order),
            self._kept_records.copy(),
            len(self._reshaped),
            len(self._shed_ids),
            len(self._shed_groups),
            tool.decision_state(),
            user.session_state(),
            self.tokenizer,
            tool.coverage_share,
            tool.keep_head_groups,
            tool.record_target_tokens,
            user.keep_head_user_turns,
            user.keep_tail_user_turns,
            user.summary_mode,
        )

    async def _continued(
        self, messages: list[Message], steady: SettledTools | None
    ) -> tuple[bool, SettledTools | None]:
        """Reuse a settled prefix only across complete, independent tool exchanges."""
        tokens = None
        if steady is not None:
            tokens = await off_loop(
                steady.measure,
                messages,
                self._round_policy(),
                tokenizer=self.tokenizer,
                budget=self._budget,
                trigger=int(self._budget * self.tool_results.trigger_fraction),
                shed_ids=self._shed_ids,
                shed_groups=self._shed_groups,
            )
        if tokens is None:
            steady = None
            await off_loop(self._annotate_messages, messages)
        changed, sources, entry_tokens = await self._compact_annotated(
            messages, steady=steady, tokens=tokens or 0
        )
        if changed:
            return True, None
        if steady is None:
            return False, await off_loop(self._settled_tools, messages, sources, entry_tokens)
        steady.extend(messages, entry_tokens, self._round_policy())
        return False, steady

    def _settled_tools(
        self, messages: list[Message], sources: list[tuple[str, list[Message]]], tokens: int
    ) -> SettledTools | None:
        if included_token_count(messages) != tokens:
            return None
        return SettledTools.capture(
            messages,
            tokens=tokens,
            policy=self._round_policy(),
            responses=_responses(messages),
            triggered=tokens > int(self._budget * self.tool_results.trigger_fraction),
            head=self.tool_results.keep_head_groups,
            shed_sources=sources,
        )

    async def measure_record_roles(
        self,
        messages: list[Message],
        *,
        task: str,
        judge: RecordRoleJudge,
        task_role: str | None = None,
        max_requests: int = 1,
        batch_size: int = 16,
        max_text_bytes: int = 32_768,
        options: Mapping[str, Any] | None = None,
    ) -> RecordRoleReport:
        """Run one real compaction pass, then measure its record decisions in bounded batches.

        Supply the task and optional field label from the host. Passing sending options runs
        the normal before-send path. Judgments are observational and never persisted.
        """
        measurement = RecordRolePass(task, task_role, max_requests, batch_size, max_text_bytes)
        # Tasks started during compaction may outlive the measured pass.
        token = self._role_measurement.set(ref(measurement))
        try:
            changed = (
                await self(messages)
                if options is None
                else await self.compact_before(messages, options=options)
            )
        finally:
            self._role_measurement.reset(token)
        return await measurement.finish(changed, judge)

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
        if self._carried is not None:
            self._carried.settled_tools = None
            # A separate stored-history pass can advance the decisions after the last model call.
            if self._carried.decisions_before_storage is None:
                self._carried.decisions_before_storage = self.session_state()
        if not messages:
            self._merge_coverage.current = self._merge_coverage.start
            self._merge_coverage.answers = []
            return False
        await off_loop(self._annotate_messages, messages)
        changed, _, _ = await self._compact_annotated(messages)
        return changed

    async def _compact_annotated(
        self, messages: list[Message], *, steady: SettledTools | None = None, tokens: int = 0
    ) -> _PassResult:
        echoes = steady is None and await off_loop(exclude_compaction_echoes, messages)
        refusals = await off_loop(self.user_turns.invalid_replay_summaries, messages)
        with self.user_turns.replay_scope(messages, refusals=frozenset(refusals)):
            changed, sources, entry_tokens = await self._compact(
                messages, steady=steady, tokens=tokens
            )
        return changed or echoes, sources, entry_tokens

    async def _compact(
        self,
        messages: list[Message],
        *,
        steady: SettledTools | None = None,
        tokens: int = 0,
    ) -> _PassResult:
        """Apply all phases within the caller's replay-refusal scope."""
        decisions_kept = self._decisions_kept
        if steady is None:
            replayed, shed_sources, entry_tokens = await off_loop(self._enter_pass, messages)
            changed = await self.tool_results.compact_against(
                messages,
                prompt_tokens=entry_tokens,
                trigger_tokens=int(self._budget * self.tool_results.trigger_fraction),
                fallback_after_record=False,
                fallback_without_record=False,
                ceiling=self._request_budget.get(),
            )
        else:
            replayed, entry_tokens = False, tokens
            shed_sources = await off_loop(steady.shed_sources, messages)
            with settled_recall_counts(messages, steady, entry_tokens):
                await off_loop(
                    self.tool_results._resume_record_phase,  # pyright: ignore[reportPrivateUsage]
                    messages,
                    steady=steady,
                    budget=self._budget,
                )
            changed = False
        replay, prompt_tokens, holding = await off_loop(
            self._after_record_phase,
            messages,
            changed=changed,
            entry_tokens=entry_tokens,
            steady=steady,
        )
        compacted = False
        if (not replay.changed or replay.new_turns) and not holding:
            compacted = await self.user_turns.compact_against(
                messages,
                prompt_tokens=prompt_tokens,
                trigger_tokens=int(self._budget * self.user_trigger_fraction),
            )
        known_fit = steady is not None and not (replay.changed or compacted)
        chained = await self._last_resort(
            messages, replayed_user_fold=replay.chain_fold, known_fit=known_fit
        )
        if replayed and self._decisions_kept == decisions_kept:
            self._decisions_kept += 1
        self._settled = True
        try:
            fitted = (
                False
                if known_fit and not chained
                else await off_loop(self._enforce_budget, messages)
            )
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
            # In the finally: a pass still over budget raises here, and that list is the one the
            # next request carries, so the worker must start from it all the same.
            if self._background is not None:
                self._background.remember(messages, self._budget)
        return (
            replayed or replay.changed or changed or compacted or chained or fitted,
            shed_sources,
            entry_tokens,
        )

    def _enter_pass(self, messages: list[Message]) -> _PassResult:
        """Put back earlier passes' decisions and measure the entry, off the loop.

        Returns whether anything was put back, the shed groups this pass may lose, and the
        prompt's size as the pass began.
        """
        replayed = (
            self._replay_record_edits(messages) if _last_reasoning_index(messages) >= 0 else False
        )
        replayed = self._keep_shedding(messages) or replayed
        # Before the record phase counts records, so a stand-in and the pair it replaced are
        # never both counted.
        replayed = self._reshape_records(messages, replay=True) or replayed
        return replayed, positional_shed_sources(messages), included_token_count(messages)

    def _after_record_phase(
        self,
        messages: list[Message],
        *,
        changed: bool,
        entry_tokens: int,
        steady: SettledTools | None = None,
    ) -> tuple[Any, int, bool]:
        """Re-measure what the record phase left and decide whether the user half holds."""
        if changed:
            # Record compaction can rewrite results whose token counts are already cached.
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer, force_retokenize=True)
            self._removed_by_record += max(entry_tokens - included_token_count(messages), 0)
        replay = self.user_turns.replay_summaries(messages)
        if steady is not None and not replay.changed:
            with settled_recall_counts(messages, steady, entry_tokens):
                holding = self._holds_for_record(
                    messages,
                    entry_tokens=entry_tokens,
                    prompt_tokens=entry_tokens,
                    clock=steady.responses + steady.added_rounds(messages),
                )
            return replay, entry_tokens, holding
        prompt_tokens = included_token_count(messages)
        holding = (not replay.changed or replay.new_turns) and self._holds_for_record(
            messages, entry_tokens=entry_tokens, prompt_tokens=prompt_tokens
        )
        return replay, prompt_tokens, holding

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
            if pending := pending_result_messages(messages):
                raise CompactionPendingResultBudgetExceeded(
                    included_token_count(messages), self._budget, pending
                )
            raise CompactionBudgetExceeded(included_token_count(messages), self._budget)
        return changed

    def _holds_for_record(
        self,
        messages: list[Message],
        *,
        entry_tokens: int,
        prompt_tokens: int,
        clock: int | None = None,
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
        if clock is None:
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
        self, messages: list[Message], *, replayed_user_fold: bool = False, known_fit: bool = False
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
        changed = over = False
        if not known_fit:
            changed = await off_loop(
                self._keep_decisions, messages, replayed_user_fold=replayed_user_fold
            )
            over = await off_loop(self._over, messages)
        with _yielding_when(over_budget=over, policy=self.overflow_policy):
            await off_loop(self._refresh_merge_coverage, messages)
            if self._background is not None:
                changed = await self._background.apply_ready(messages) or changed
        if (known_fit and not changed) or not await off_loop(self._over, messages):
            self._merge_event("under_budget")
            return changed
        start = _ChainStart.take(messages)
        self._merge_event("chain_started", prompt_tokens=start.tokens, budget=self._budget)
        # A run's later calls carry its loop's reasoning after every record; under "shed" the
        # shed below edits that prefix anyway, so the record steps are not barred from it here.
        with _yielding_when(over_budget=True, policy=self.overflow_policy):
            changed = await off_loop(self._reshape_records, messages) or changed
            kept = len(self._kept_records), self._user_summaries_merged
            deadline = None if self.chain_seconds is None else monotonic() + self.chain_seconds
            token = self._chain_deadline.set(deadline)
            try:
                if self._background is None or not self._background.defer_merges:
                    changed = await self._merge_records(messages, start) or changed
                else:
                    self._merge_event("foreground_deferred")
                self._check_chain_deadline()
                if self._short_of_target(messages, start):
                    changed = await self._merge_user_summaries(messages) or changed
                self._check_chain_deadline()
                if self._short_of_target(messages, start):
                    changed = await self._rewrite_records(messages, start) or changed
            except _ChainTimeSpent:
                self._merge_event("deadline")
                self._chain_timeouts += 1
                changed = kept != (len(self._kept_records), self._user_summaries_merged) or changed
                logger.warning(
                    "Compaction stopped starting last-resort summary work after %.3f seconds; "
                    "completed reductions are retained and remaining batches stay eligible.",
                    self.chain_seconds,
                )
            finally:
                self._chain_deadline.reset(token)
                await off_loop(self._refresh_merge_coverage, messages)
        if self._short_of_target(messages, start) and find_record_index(messages) is not None:
            # With no record, budget enforcement owns unrecorded shedding and its headroom.
            self._last_resort_fallbacks += 1
            if record_merge_observer.get() is not None:
                self._merge_event("fallback", target=self._target(messages, start))
            changed = await self._fall_back(messages, start) or changed
        if self._short_of_target(messages, start):
            self._chain_targets_missed += 1
        else:
            self._chain_targets_reached += 1
        return changed

    def _check_chain_deadline(self) -> None:
        if self._record_work_current is not None and not self._record_work_current():
            raise asyncio.CancelledError
        if self._background_requests is not None and self._background_requests.closed:
            raise _RunFinished
        deadline = self._chain_deadline.get()
        if deadline is not None and monotonic() >= deadline:
            raise _ChainTimeSpent

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

        - a reshape, by its complete accepted batch of identified sources, put back by
          :meth:`__call__` before the record phase so no record is counted in both forms;
        - a record merge or rewrite, by the transcript of the records it replaced -- the one
          thing both lists present identically -- replacing any run of the records that
          matches one, as often as one does, since a merge followed by a rewrite is two.
          Bypassing a retained-reasoning guard additionally requires exact source identities
          and the accepted source window, including written-record lineage. Record edits
          replay in acceptance order with their prerequisite user edits and shedding;
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
        changed = self._replay_record_edits(messages) or replayed_user_fold
        changed = self._keep_shedding(messages) or changed
        changed = self._keep_records(messages) or changed
        if changed:
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer)
        changed = self.user_turns.refold(messages) or changed
        if changed:
            self._decisions_kept += 1
        return changed

    def _keep_shedding(
        self,
        messages: list[Message],
        *,
        edit: RecordEdit | None = None,
        group_lookup: dict[str, set[str]] | None = None,
    ) -> bool:
        """Replay prior removals before their contents can trigger new compaction work."""
        message_ids = set(self._shed_ids if edit is None else edit.shed_ids)
        group_keys = self._shed_groups if edit is None else set(edit.shed_groups)
        if group_keys:
            if group_lookup is None:
                group_lookup = ReplayView(messages).shed_lookup()
            for key in group_keys:
                message_ids.update(group_lookup.get(key, set()))
        with _release_record_holds(
            [message for message in messages if message.message_id in message_ids]
        ):
            return self.tool_results.shed_again_after_record(messages, message_ids)

    def _record_edit(
        self,
        messages: list[Message],
        groups: list[dict[str, Any]],
        text: str | None,
        *,
        keys: MessageKeyCache | None = None,
    ) -> RecordEdit | None:
        """Capture the accepted window's replay prerequisites without copying their contents.

        ``keys`` shares fingerprints between the edits one :meth:`_keep_records` call captures.
        """
        sources = source_key(messages, groups, keys=keys)
        if sources is None:
            return None
        excluded = {message.message_id for message in messages if _is_excluded(message)}
        shed_groups = (
            [
                key
                for key, members in unique_shed_groups(
                    messages, keys=None if keys is None else keys.shed_groups
                )
                if key in self._shed_groups and all(_is_excluded(message) for message in members)
            ]
            if self._shed_groups
            else []
        )
        return RecordEdit(
            sources,
            len(groups),
            text,
            self.user_turns.replay_summary_ids(messages),
            sorted(self._shed_ids & excluded),
            shed_groups,
            source_members(messages, groups, keys=keys),
        )

    def _replay_record_edits(self, messages: list[Message], *, reshape_only: bool = False) -> bool:
        """Restore each accepted record window before applying edits that followed it.

        A history that holds package-written records can refuse a chain of staged edits part
        of the way through, and must then be left as it was handed. The traversal that finds
        that out is the one that commits: it runs on the history itself, and a traversal that
        refuses, changes nothing or raises is taken back from what
        :class:`~._record_replay.ReplayUndo` and :meth:`_replay_state` kept. It used to run
        twice, once on copies to learn the answer and again on the history, which doubled
        the cost of putting every edit back on a history handed without them.
        """
        key_cache = MessageKeyCache()
        if not self._record_order or not any(replacement_identity(message) for message in messages):
            return bool(
                self._replay_record_candidates(
                    messages, reshape_only=reshape_only, key_cache=key_cache
                )
            )
        undo = ReplayUndo.take(messages)
        state = self._replay_state()
        if undo is None or state is None:
            return self._replay_record_edits_previewed(messages, reshape_only, key_cache)
        try:
            committed = self._replay_record_candidates(
                messages, reshape_only=reshape_only, required_outputs=set(), key_cache=key_cache
            )
        except BaseException:
            undo.restore()
            state()
            raise
        if not committed:
            undo.restore()
            state()
        return bool(committed)

    def _replay_record_edits_previewed(
        self, messages: list[Message], reshape_only: bool, key_cache: MessageKeyCache
    ) -> bool:
        """Replay on copies first, for a history or a strategy that cannot be taken back."""
        preview = copy(self)
        preview.user_turns = copy(self.user_turns)
        preview.tool_results = copy(self.tool_results)
        preview.tool_results.restore_decisions(self.tool_results.decision_state())
        # The preview's copies share their contents with the history, and so its fingerprints.
        candidate = ReplayView(messages).fork().messages
        if not preview._replay_record_candidates(
            candidate, reshape_only=reshape_only, required_outputs=set(), key_cache=key_cache
        ):
            return False
        return bool(
            self._replay_record_candidates(messages, reshape_only=reshape_only, key_cache=key_cache)
        )

    def _replay_state(self) -> Callable[[], None] | None:
        """Keep what a replay changes on this strategy, and return how to put it back.

        What a copy of the strategy and of each half would have had of its own: every
        attribute as it is bound, and the record half's decisions by value, since a replayed
        merge moves an outstanding ask's record count in place. None for a strategy or a half
        whose state is not all in its ``__dict__``.
        """
        halves = (self, self.user_turns, self.tool_results)
        if not all(keeps_state_in_dict(type(half)) for half in halves):
            return None
        kept = [(half, dict(vars(half))) for half in halves]
        decisions = self.tool_results.decision_state()

        def restore() -> None:
            for half, attributes in kept:
                current = vars(half)
                current.clear()
                current.update(attributes)
            if self.tool_results.decision_state() != decisions:
                self.tool_results.restore_decisions(decisions)

        return restore

    def _replay_record_candidates(
        self,
        messages: list[Message],
        *,
        reshape_only: bool,
        required_outputs: set[tuple[str, str]] | None = None,
        key_cache: MessageKeyCache | None = None,
    ) -> bool | None:
        changed = False
        restored_view = False
        base = ReplayView(messages, key_cache=MessageKeyCache() if key_cache is None else key_cache)
        base.invalid_summary_ids = self.user_turns.invalid_replay_summaries(
            messages, cache=base.summary_cache
        )
        candidates: dict[
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[int, ...]], ReplayView
        ] = {}
        restored_summaries: set[tuple[str, ...]] = set()
        for edit_index, edit in enumerate(self._record_order):
            if reshape_only and edit.text is not None:
                continue
            summaries = tuple(edit.summaries)
            if summaries not in restored_summaries:
                restored = self.user_turns.replay_summaries(
                    messages,
                    summary_ids=edit.summaries,
                    cache=base.summary_cache,
                    invalid_summary_ids=base.invalid_summary_ids,
                ).changed
                if restored:
                    base.invalidate()
                    candidates.clear()
                    restored_summaries.clear()
                    changed = True
                restored_summaries.add(summaries)
            record_indices = self._held_record_replacements(base, edit_index)
            if record_indices is None:
                continue
            # Answers what _match_record_edit's staged-replacement refusal would, without the view.
            if (
                record_indices
                and edit.members
                and not self._available_record_members(base).intersection(edit.members)
            ):
                continue
            prerequisites = (
                tuple(edit.summaries),
                tuple(edit.shed_ids),
                tuple(edit.shed_groups),
                tuple(sorted(record_indices)),
            )
            view = candidates.get(prerequisites)
            if view is None:
                view = self._record_replay_view(base, edit, record_indices)
                candidates[prerequisites] = view
            if self._keep_shedding(
                view.messages,
                edit=edit,
                group_lookup=view.shed_lookup() if edit.shed_groups else None,
            ):
                view.invalidate()
                if view is base:
                    candidates.clear()
                    restored_summaries.clear()
                    changed = True
            matched = self._match_record_edit(view, edit)
            if matched is None:
                continue
            view, groups = matched
            plans = self._record_replacement_plans(view, edit, groups)
            if not self._record_replacement_positions(view, plans):
                if required_outputs is not None:
                    return None
                continue
            if required_outputs is not None:
                outputs = {
                    identity
                    for _, replacement in plans
                    if (identity := replacement_identity(replacement)) is not None
                }
                affected = set(outputs)
                fingerprints = {
                    ReplayView([replacement]).message_key(0) for _, replacement in plans
                }
                # Unrelated held outputs can outlive their pruned source windows.
                for later in self._record_order[edit_index + 1 :]:
                    if (
                        later.text is None
                        or not later.sources.startswith("v2:")
                        or (later.members and not fingerprints.intersection(later.members))
                    ):
                        continue
                    replacement = build_record_message(later.text)
                    identity = (LINEAGE_KEY, later.sources.split(":")[1])
                    replacement.additional_properties[LINEAGE_KEY] = identity[1]
                    affected.add(identity)
                    fingerprints.add(ReplayView([replacement]).message_key(0))
                required_outputs.update(
                    identity
                    for message, _ in view.deferred
                    if id(message) in view.record_replacements
                    and (identity := replacement_identity(message)) is not None
                    and identity in affected
                )
                required_outputs.difference_update(outputs)
            if edit.text is None:
                self.tool_results.reshape_records(view.messages, groups, replay=True)
            else:
                self.tool_results.consolidate_records(
                    view.messages, groups, edit.text, replay=True, keys=view.key_cache
                )
            view.invalidate()
            if view is not base:
                if view.summary_preview is not None:
                    view.summary_preview.replay_summaries(
                        view.messages,
                        restore_held_sources=True,
                        invalid_summary_ids=view.invalid_summary_ids,
                        cache=view.summary_cache,
                        skip_summary_ids={
                            message.message_id
                            for message, _ in view.deferred
                            if message.message_id is not None
                        },
                    )
                view.restore_later()
                if view.summary_preview is not None:
                    view.summary_preview.replay_summaries(
                        view.messages,
                        restore_held_sources=True,
                        invalid_summary_ids=view.invalid_summary_ids,
                        cache=view.summary_cache,
                    )
                messages[:] = view.messages
                if view.summary_preview is not None:
                    self.user_turns.accept_replay_preview(messages, view.summary_preview)
                restored_view = True
            base.invalidate()
            candidates.clear()
            restored_summaries.clear()
            changed = True
        if restored_view:
            self.user_turns.replay_summaries(messages, invalid_summary_ids=base.invalid_summary_ids)
            # Still inside the replay call, so its group fingerprints serve this lookup too.
            self._keep_shedding(
                messages,
                group_lookup=ReplayView(messages, key_cache=base.key_cache).shed_lookup()
                if self._shed_groups
                else None,
            )
        return None if required_outputs else changed

    @staticmethod
    def _owned_record_exclusions(messages: list[Message]) -> set[int]:
        return {
            index
            for index, message in enumerate(messages)
            if _is_excluded(message)
            and message.additional_properties.get(EXCLUDE_REASON_KEY) == CONSOLIDATE_EXCLUDE_REASON
            and not _record_has_external_hold(message)
        }

    def _match_record_edit(
        self, view: ReplayView, edit: RecordEdit
    ) -> tuple[ReplayView, list[dict[str, Any]]] | None:
        groups_for = (
            reshapeable_record_groups if edit.text is None else consolidatable_record_groups
        )
        matched = replay_match(
            view.messages,
            groups_for(view.messages, replay=True),
            {(edit.sources, edit.count)},
            message_keys=view.message_keys,
            key_cache=view.key_cache,
        )
        if matched is not None:
            return view, matched[0]
        owned = self._owned_record_exclusions(view.messages)
        if not owned or not edit.members:
            return None
        members = set(edit.members)
        # The member skip in _replay_record_candidates is sound only while this refusal holds.
        if view.record_replacements and not any(
            not _is_excluded(message) and view.message_key(index) in members
            for index, message in enumerate(view.messages)
        ):
            return None
        candidate = view.fork()
        released = False
        for index in owned:
            message = candidate.messages[index]
            set_excluded(message, excluded=False)
            candidate.message_keys.pop(index, None)
            if candidate.message_key(index) in members:
                released = True
            else:
                set_excluded(message, excluded=True)
                candidate.message_keys.pop(index, None)
        if not released:
            return None
        annotate_message_groups(candidate.messages)
        matched = replay_match(
            candidate.messages,
            groups_for(candidate.messages, replay=True),
            {(edit.sources, edit.count)},
            message_keys=candidate.message_keys,
            key_cache=candidate.key_cache,
        )
        return None if matched is None else (candidate, matched[0])

    @staticmethod
    def _available_record_members(base: ReplayView) -> set[str]:
        """Bound members a candidate can include before partial record-exclusion recovery.

        Candidate staging can release shed and user-summary exclusions, but never record
        exclusions. Newly inserted user summaries and shed notes cannot be record members.
        """
        if base.available_members is None:
            base.available_members = set()
            for index, message in enumerate(base.messages):
                if not _is_excluded(message):
                    base.available_members.add(base.message_key(index))
                elif message.additional_properties.get(EXCLUDE_REASON_KEY) != (
                    CONSOLIDATE_EXCLUDE_REASON
                ):
                    candidate = copy(message)
                    candidate.additional_properties = dict(message.additional_properties)
                    set_excluded(candidate, excluded=False)
                    base.available_members.add(
                        ReplayView([candidate], key_cache=base.key_cache).message_key(0)
                    )
        return base.available_members

    def _index_record_replacements(self, base: ReplayView) -> RecordReplayIndex:
        messages = base.messages
        held = [
            (index, message)
            for index, message in enumerate(messages)
            if replacement_identity(message) is not None
        ]
        if not held:
            return RecordReplayIndex(held, set(), {}, {}, -1)
        known = {
            (LINEAGE_KEY, edit.sources.split(":")[1])
            for edit in self._record_order
            if edit.text is not None and edit.sources.startswith("v2:")
        } | {
            (STANDS_IN_KEY, _call_identity(message, position))
            for message in messages
            for position, content in enumerate(message.contents)
            if content.type == "function_call" and content.name == RECALL_TOOL_NAME
        }
        merges: dict[tuple[str, str], tuple[int, Message]] = {}
        last_reshape = max(
            (index for index, edit in enumerate(self._record_order) if edit.text is None),
            default=-1,
        )
        for index, edit in enumerate(self._record_order):
            if edit.text is not None and edit.sources.startswith("v2:"):
                replacement = build_record_message(edit.text)
                replacement.additional_properties[LINEAGE_KEY] = edit.sources.split(":")[1]
                identity = replacement_identity(replacement)
                assert identity is not None
                merges[identity] = (index, replacement)
        return RecordReplayIndex(held, known, None, merges, last_reshape)

    def _index_reshape_replacements(self, base: ReplayView) -> dict[tuple[str, str], Message]:
        """Build the written record each model-made record of ``base`` reshapes into."""
        source_view = base
        owned = self._owned_record_exclusions(base.messages)
        if owned:
            source_view = base.fork()
            for index in owned:
                set_excluded(source_view.messages[index], excluded=False)
            annotate_message_groups(source_view.messages)
        reshapes: dict[tuple[str, str], Message] = {}
        for _, replacement in reshape_replacements(
            source_view.messages, reshapeable_record_groups(source_view.messages, replay=True)
        ):
            identity = replacement_identity(replacement)
            if identity is not None:
                reshapes[identity] = replacement
        return reshapes

    def _held_record_replacements(self, base: ReplayView, edit_index: int) -> set[int] | None:
        if base.record_index is None:
            base.record_index = self._index_record_replacements(base)
        indexed = base.record_index
        if not indexed.held:
            return set()
        expected: dict[tuple[str, str], Message] = {}
        if edit_index <= indexed.last_reshape:
            if indexed.reshapes is None:
                indexed.reshapes = self._index_reshape_replacements(base)
            expected.update(indexed.reshapes)
        expected.update(
            (identity, replacement)
            for identity, (index, replacement) in indexed.merges.items()
            if index >= edit_index
        )
        expected_texts = {message.text for message in expected.values()}
        indices: set[int] = set()
        seen: set[tuple[str, str]] = set()
        for index, message in indexed.held:
            identity = replacement_identity(message)
            replacement = expected.get(identity) if identity is not None else None
            if replacement is None:
                if identity not in indexed.known and message.text in expected_texts:
                    return None
                continue
            if (
                identity in seen
                or not _is_written_record(message)
                or _record_has_external_hold(message)
                or not base.matches_replacement(index, replacement)
                or (
                    _is_excluded(message)
                    and message.additional_properties.get(EXCLUDE_REASON_KEY)
                    != CONSOLIDATE_EXCLUDE_REASON
                )
            ):
                return None
            assert identity is not None
            seen.add(identity)
            indices.add(index)
        return indices

    @staticmethod
    def _record_replacement_plans(
        view: ReplayView, edit: RecordEdit, groups: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], Message]]:
        if edit.text is None:
            return reshape_replacements(view.messages, groups)
        replacement = build_record_message(edit.text)
        replacement.additional_properties[LINEAGE_KEY] = edit.sources.split(":")[1]
        return [(groups[-1], replacement)]

    @staticmethod
    def _record_replacement_positions(
        view: ReplayView, plans: list[tuple[dict[str, Any], Message]]
    ) -> bool:
        if not view.record_replacements:
            return True
        following = {id(message): after for message, after in view.deferred}
        for group, replacement in plans:
            for message, after in view.deferred:
                if id(message) not in view.record_replacements or (
                    replacement_identity(message) != replacement_identity(replacement)
                ):
                    continue
                while after is not None and id(after) in following:
                    after = following[id(after)]
                end = int(group["end_index"]) + 1
                while end < len(view.messages) and (
                    view.messages[end].message_id in view.restored_summary_ids
                    and (after is None or view.messages[end].message_id != after.message_id)
                ):
                    end += 1
                next_message = view.messages[end] if end < len(view.messages) else None
                if after is not next_message and (
                    after is None
                    or next_message is None
                    or not after.message_id
                    or after.message_id != next_message.message_id
                ):
                    return False
        return True

    def _record_replay_view(
        self, base: ReplayView, edit: RecordEdit, record_indices: set[int]
    ) -> ReplayView:
        """Undo later owned exclusions on copies; the source certificate still authorizes edits."""
        user_exclusions = self.user_turns.replay_source_exclusions(base.messages, edit.summaries)
        later_summaries = set(self.user_turns.replay_summary_ids(base.messages)) - set(
            edit.summaries
        )
        later_shed = self._shed_ids - set(edit.shed_ids)
        if self._shed_groups:
            lookup = base.shed_lookup()
            for key in self._shed_groups - set(edit.shed_groups):
                later_shed.update(lookup.get(key, set()))
        shed_indices = [
            index
            for index, message in enumerate(base.messages)
            if message.message_id in later_shed
            and _is_excluded(message)
            and message.additional_properties.get(EXCLUDE_REASON_KEY) == EXCLUDE_REASON
        ]
        later_indices = {
            index
            for index, message in enumerate(base.messages)
            if message.message_id in later_summaries
            or (
                _is_marker(message)
                and bool(sources := _summary_source_ids(message))
                and set(sources) <= later_shed
            )
        }
        prerequisite_sources = {
            source
            for message in base.messages
            if message.message_id in edit.summaries
            for source in _summary_source_ids(message)
        }
        missing_exclusions = any(
            message.message_id in prerequisite_sources and not _is_excluded(message)
            for message in base.messages
        )
        missing_summaries = set(edit.summaries) - {message.message_id for message in base.messages}
        if (
            not user_exclusions
            and not shed_indices
            and not later_indices
            and not missing_exclusions
            and not missing_summaries
            and not record_indices
        ):
            return base
        invalid_summaries = (
            self.user_turns.invalid_replay_summaries(base.messages, cache=base.summary_cache)
            | base.invalid_summary_ids
        )
        messages = [copy(message) for message in base.messages]
        original_indices = {id(message): index for index, message in enumerate(messages)}
        for message in messages:
            message.additional_properties = dict(message.additional_properties)
            annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
            if isinstance(annotation, dict):
                message.additional_properties[GROUP_ANNOTATION_KEY] = dict(
                    cast(dict[str, Any], annotation)
                )
        originals = [(message, dict(message.additional_properties)) for message in messages]
        _release_user_sources(messages, user_exclusions)
        for index in shed_indices:
            messages[index].additional_properties.pop(EXCLUDE_REASON_KEY, None)
            set_excluded(messages[index], excluded=False)
        exclusions = [
            (message, properties)
            for message, properties in originals
            if properties.get(EXCLUDED_KEY) and not _is_excluded(message)
        ]
        staged_indices = later_indices | record_indices
        staged_record_ids = {id(messages[index]) for index in record_indices}
        deferred = [
            (messages[index], messages[index + 1] if index + 1 < len(messages) else None)
            for index in sorted(staged_indices)
        ]
        messages[:] = [
            message for index, message in enumerate(messages) if index not in staged_indices
        ]
        if staged_indices:
            annotate_message_groups(messages)
        preview = copy(self.user_turns)
        held_summary_ids = {message.message_id for message in messages}
        summary_cache = base.summary_cache.fork()
        restored = preview.replay_summaries(
            messages,
            summary_ids=edit.summaries,
            restore_held_sources=True,
            invalid_summary_ids=invalid_summaries,
            cache=summary_cache,
        ).changed
        return ReplayView(
            messages,
            summary_cache=summary_cache,
            message_keys={
                index: base.message_key(original_indices[id(message)])
                for index, message in enumerate(messages)
                if id(message) in original_indices
                and _is_excluded(message)
                == _is_excluded(base.messages[original_indices[id(message)]])
            },
            shed_groups=base.shed_groups if not staged_indices and not restored else None,
            exclusions=exclusions,
            deferred=deferred,
            summary_preview=preview,
            invalid_summary_ids=invalid_summaries,
            record_replacements=staged_record_ids,
            restored_summary_ids={
                message.message_id
                for message in messages
                if message.message_id is not None and message.message_id not in held_summary_ids
            },
            key_cache=base.key_cache,
        )

    def _keep_records(self, messages: list[Message]) -> bool:
        """Replace every run of records the chain has already replaced, as it did.

        The earliest run first, and at one start the longest. A batch merge need not start at the
        oldest record, so any start is tried; an entry from a legacy snapshot, which has no
        source count, only at the oldest, as it was taken.

        By transcript only. Replaying accepted windows exactly is the entry points' work:
        :meth:`_keep_decisions` does it before calling this, and one replay is a fixpoint, so
        another here would walk every accepted edit to change nothing. After a merge the chain
        has just accepted, a kept replacement of the new record is found here by its transcript,
        under the reasoning-prefix guard the merge itself was accepted under.
        """
        changed = False
        # A history handed without its merges takes every one of them here when the exact
        # replay matches none. Nothing between two of them rewrites a message in place, so
        # the fingerprints, group keys and token counts of one are the next one's too.
        keys = MessageKeyCache()
        while self._kept_records:
            groups = consolidatable_record_groups(messages)
            source_counts = {count for _, count in self._kept_records}
            counted = sorted((count for count in source_counts if count is not None), reverse=True)
            # Legacy snapshots need a search until their source counts have been learned.
            legacy = range(len(groups), 0, -1) if None in source_counts else counted
            bodies = record_bodies(messages, groups)
            transcripts = sorted(transcript for transcript, _ in self._kept_records)
            found = None
            for start in range(len(groups)):
                # Every kept transcript opens with its first record, which skips most starts.
                head = _numbered_records(bodies[start : start + 1])
                if not _has_prefix(transcripts, head):
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
            edit = self._record_edit(messages, found[0], found[1], keys=keys)
            self.tool_results.consolidate_records(messages, *found, keys=keys)
            if edit is not None and edit not in self._record_order:
                self._record_order.append(edit)
            changed = True
        return changed

    def _annotate_messages(self, messages: list[Message]) -> None:
        if self._background_records is not None and messages is self._background_records.messages:
            self._background_records.annotate(self.tokenizer)
        else:
            annotate_message_groups(messages)
            annotate_token_counts(messages, tokenizer=self.tokenizer)

    def _over(self, messages: list[Message]) -> bool:
        """Return whether the prompt, as it now stands, is over the input budget."""
        self._annotate_messages(messages)
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
        self._annotate_messages(messages)
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
        span holds unrelated content or active-turn reasoning, whose result carries metadata
        or non-text items, or whose result this model call carried in is left as it is.
        Reasoning attached only to completed-turn records leaves with their whole groups in one
        batch, so later retained reasoning never sees its prefix edited. Merges spare the same
        protected spans.

        **Only when the chain fires, not whenever the written form is cheaper.** A reshape
        rewrites the prompt from the record it replaces. Inside a firing it is one more edit the
        firing's target is measured from, so the firing goes on until it has removed its share of
        what that edit re-bills -- the rule every other step answers to -- and the merge it
        precedes edits at the same record. Reshaping each record as soon as it could be would
        break the cache once per record, on prompts that fit. Replay across retained reasoning
        requires the complete accepted batch and its exact identified source window. Legacy
        payload-only decisions remain subject to the current reasoning guards.

        Args:
            messages: The conversation, mutated in place.

        Keyword Args:
            replay: Reshape only records an earlier pass reshaped.

        Returns:
            True if any record was reshaped.
        """
        changed = self._replay_record_edits(messages, reshape_only=True) if replay else False
        groups = reshapeable_record_groups(messages)
        digests = [_digest(payload) for payload in reshape_payloads(messages, groups)]
        chosen = [
            (group, digest)
            for group, digest in zip(groups, digests, strict=True)
            if not replay or digest in self._reshaped
        ]
        if not chosen:
            return changed
        selected = [group for group, _ in chosen]
        edit = self._record_edit(messages, selected, None)
        try:
            self.tool_results.reshape_records(messages, selected)
        except ValueError:
            if not replay:
                raise
            return changed
        if edit is not None and edit not in self._record_order:
            self._record_order.append(edit)
        if not replay:
            self._records_reshaped += len(chosen)
            self._reshaped.update(digest for _, digest in chosen)
        return True

    async def _merge_batches(
        self, messages: list[Message], start: _ChainStart | None, share: float | None
    ) -> bool:
        """Merge the oldest records in batches while they exceed ``share`` or the prompt its target.

        Batches are taken oldest first and do not overlap, each the longest run of whole records
        whose request fits :meth:`_record_request_limit`, so a batch the summarizer can answer
        whole replaces a few records rather than all of them. The history is read once, and
        again only after a merge changes it; passing a record over moves on to the next.
        An interrupted batch expansion retains its fitting pair for the next sweep.
        An interrupted sweep resumes its position while the record layout, prompt and cap stand.
        A changed merge prompt invalidates cached batch searches too.
        Pending smaller batches resume before a new sweep and retain their split depth.
        Without a share, pending work resumes before the single input-bounded merge.

        Single-record fit results persist while their record, prompt and cap stand unchanged.
        A record whose own request exceeds the cap is passed over without a search from it. One
        whose two-record request does not fit is searched over longer runs, since token counts
        need not grow with text, until a run counts past ``run_past_limit_factor`` times the
        cap. What a search from a record found -- its batch, or how far it
        got with nothing fitting -- is remembered with a digest of the records that decided it,
        and a later sweep takes that batch, or resumes the search past those records, while
        they stand unchanged: an appended record is tried without searching again what was
        searched, and a changed one, or one appended where the end of the records decided the
        answer, has the search start afresh. The search spends at most
        :data:`_MAX_BATCH_PROBES` prefixes a sweep and a remembered batch costs none, so a run of
        refused batches longer than one sweep can search does not hold every later sweep short
        of the batches after it. A batch already refused on the same records is passed over
        too, and stays as it is until it changes; a refusal is remembered, logged at WARNING with
        counts and never values while the records exceed their share, and the sweep goes on to
        the next batch. A summarizer that fails ends the sweep, since the next batch would be
        asked of it too, and a record a batch writes is not merged again on the same pass.

        Returns:
            True if any batch was merged.
        """
        self._merge_sweep_complete = False
        bound = None if share is None else int(self._budget * share)
        limit = self._record_request_limit()
        prompt_digest = _digest(self.merge_prompt)
        if self._search_prompt != prompt_digest:
            self._searched.clear()
            self._search_prompt = prompt_digest
        handled: set[int] = set()
        # Exclusion can replace message instances; keep cache identities alive for this sweep.
        seen_messages = {id(message): message for message in messages}
        bodies: dict[int, str] = {}
        digests: dict[int, str] = {}
        runs: list[int] = [0]
        powers: list[int] = [1]
        # Per record, where the run a batch from it may take ends (``merge_same_tool_first``).
        ends: list[int] = []
        probes = 0
        changed = False

        def last(group: dict[str, Any]) -> int:
            return id(messages[int(group["end_index"])])

        def render(prefix: list[dict[str, Any]]) -> str:
            nonlocal probes
            self._check_chain_deadline()
            probes += 1
            if probes > _MAX_BATCH_PROBES:
                raise _BatchProbesSpent
            return _numbered_records([bodies[last(group)] for group in prefix])

        def survey() -> tuple[list[dict[str, Any]], int, bool]:
            exclusions: dict[str, int] | None = (
                {} if record_merge_observer.get() is not None else None
            )
            groups, _, active = _consolidatable_record_groups(messages, exclusions=exclusions)
            if exclusions is not None and self._background_records is not None:
                exclusions.update(self._background_records.exclusions)
            missing = [group for group in groups if last(group) not in bodies]
            if missing:
                for group, body in zip(missing, record_bodies(messages, missing), strict=True):
                    bodies[last(group)] = body
                    digests[last(group)] = _digest(body)
            runs[:] = [0]
            for group in groups:
                value = int(digests[last(group)], 16)
                runs.append((runs[-1] * _RUN_BASE + value) % _RUN_MODULUS)
            while len(powers) <= len(groups):
                powers.append(powers[-1] * _RUN_BASE % _RUN_MODULUS)
            ends[:] = (
                _same_tool_segment_ends(covered_tools(messages, groups, active))
                if self.merge_same_tool_first
                else [len(groups)] * len(groups)
            )
            inputs = {(prompt_digest, digests[last(group)], limit) for group in groups}
            self._record_input_fits = {
                key: fits for key, fits in self._record_input_fits.items() if key in inputs
            }
            held = self._record_tokens(messages)
            over = (
                (bound is not None and held > bound)
                or (start is not None and self._short_of_target(messages, start))
                or (bound is None and start is None)
            )
            if exclusions is not None:
                self._merge_event(
                    "survey",
                    details=exclusions,
                    prompt_tokens=included_token_count(messages),
                    budget=self._budget,
                    target=self._target(messages, start) if start is not None else "none",
                    record_tokens=held,
                    record_limit=bound if bound is not None else "none",
                    request_limit=limit,
                    records=len(groups) + sum(exclusions.values()),
                    eligible=len(groups),
                    pressure=int(over),
                )
            return groups, held, over

        def size(run: list[dict[str, Any]]) -> int:
            return summary_input_tokens(self.merge_prompt, render(run), tokenizer=self.tokenizer)

        factor = self.run_past_limit_factor

        def past_limit(tokens: int) -> bool:
            return factor is not None and tokens > limit * factor

        def chain(first: int, count: int) -> str:
            run = (runs[first + count] - runs[first] * powers[count]) % _RUN_MODULUS
            return format(run, "x")

        def remember(
            first: int,
            covered: int,
            batch: int = 0,
            *,
            ended: bool = False,
            past_limit: float | None = None,
        ) -> None:
            key = (digests[last(groups[first])], limit)
            self._searched[key] = _Searched(
                covered, chain(first, covered), batch, ended, past_limit
            )

        async def search(first: int) -> list[dict[str, Any]]:
            """Return the batch from ``first``, resuming a search of the same records."""
            remaining = len(groups) - first
            checked = 1
            known = self._searched.get((digests[last(groups[first])], limit))
            if (
                known is not None
                and known.covered <= remaining
                and (not known.ended or known.covered == remaining)
                and chain(first, known.covered) == known.records
            ):
                if known.batch:
                    return groups[first : first + known.batch]
                if (
                    known.past_limit is not None
                    and factor is not None
                    and factor <= known.past_limit
                ):
                    return []
                checked = known.covered
            if checked < 2:
                pair = await off_loop(size, groups[first : first + 2])
                if pair <= limit:
                    remember(first, 2, 2)
                    batch, extent = await async_bounded_prefix_extent(
                        groups[first:],
                        render=render,
                        prompt=self.merge_prompt,
                        tokenizer=self.tokenizer,
                        limit=limit,
                        min_items=2,
                        cooperative=self._record_answer_ready is not None,
                    )
                    if extent is None:
                        remember(first, remaining, len(batch), ended=True)
                    else:
                        remember(first, extent, len(batch))
                    return batch
                checked = 2
                if past_limit(pair):
                    remember(first, checked, past_limit=factor)
                    return []
            # Token counts need not grow with text, so a longer run may fit where two do not.
            for count in range(checked + 1, remaining + 1):
                if self._record_answer_ready is not None:
                    await asyncio.sleep(0)
                try:
                    tokens = await off_loop(size, groups[first : first + count])
                except (_BatchProbesSpent, _ChainTimeSpent):
                    remember(first, checked)
                    raise
                if tokens <= limit:
                    remember(first, count, count)
                    return groups[first : first + count]
                checked = count
                if past_limit(tokens):
                    remember(first, checked, past_limit=factor)
                    return []
            remember(first, checked)
            return []

        async def capped(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """``batch`` cut to ``record_merge_max_records``; empty if the cut does not fit."""
            cap = self.record_merge_max_records
            if cap is None or len(batch) <= cap:
                return batch
            batch = batch[:cap]
            # A shorter run need not fit where a longer one did.
            return batch if await off_loop(size, batch) <= limit else []

        def pending_batch(
            selected: set[int],
        ) -> tuple[_PendingMerge, list[dict[str, Any]]] | None:
            matches: list[tuple[int, _PendingMerge]] = []
            cap = self.record_merge_max_records
            for pending in self._pending_merges:
                if (
                    pending.prompt != prompt_digest
                    or pending.limit != limit
                    or pending.depth > self.record_merge_split_depth
                    or (cap is not None and pending.count > cap)
                ):
                    continue
                for first in range(len(groups) - pending.count + 1):
                    if chain(first, pending.count) == pending.records:
                        matches.append((first, pending))
                        break
            self._pending_merges = [pending for _, pending in matches]
            matches = [
                (first, pending)
                for first, pending in matches
                if not any(
                    last(group) in selected for group in groups[first : first + pending.count]
                )
            ]
            if not matches:
                return None
            first, pending = min(matches, key=lambda match: (match[0], -match[1].depth))
            return pending, groups[first : first + pending.count]

        groups, _, over = await off_loop(survey)
        resumed = False
        while over and (selected := await off_loop(pending_batch, set())) is not None:
            if self._record_answer_ready is not None:
                await asyncio.sleep(0)
            self._check_chain_deadline()
            pending, batch = selected
            transcript = await off_loop(_numbered_records, [bodies[last(group)] for group in batch])
            if not await off_loop(
                fits_summary_input,
                self.merge_prompt,
                transcript,
                tokenizer=self.tokenizer,
                limit=limit,
            ):
                self._pending_merges.remove(pending)
                continue
            resumed = True
            pending_wave = [pending]
            batches = [(batch, transcript, pending.depth)]
            selected_ids = {last(group) for group in batch}
            while len(batches) < self._summary_parallelism():
                next_pending = await off_loop(pending_batch, selected_ids)
                if next_pending is None:
                    break
                item, child = next_pending
                selected_ids.update(last(group) for group in child)
                body = await off_loop(_numbered_records, [bodies[last(group)] for group in child])
                if not await off_loop(
                    fits_summary_input,
                    self.merge_prompt,
                    body,
                    tokenizer=self.tokenizer,
                    limit=limit,
                ):
                    self._pending_merges.remove(item)
                    continue
                pending_wave.append(item)
                batches.append((child, body, item.depth))
            if len(batches) == 1:
                merged, failed = await self._merge_batch(
                    messages, batch, transcript=transcript, depth=pending.depth
                )
            else:
                merged, failed = await self._merge_parallel_batches(messages, batches)
            changed = merged or changed
            if failed:
                return changed
            for item in pending_wave:
                if item in self._pending_merges:
                    self._pending_merges.remove(item)
            if merged:
                groups, _, over = await off_loop(survey)
        if resumed:
            self._batch_resume = None
            return changed
        if share is None:
            if not over:
                return False
            groups, _ = await async_bounded_prefix_extent(
                groups[: self.record_merge_max_records],
                render=lambda prefix: _numbered_records([bodies[last(group)] for group in prefix]),
                prompt=self.merge_prompt,
                tokenizer=self.tokenizer,
                limit=limit,
                min_items=2,
                cooperative=self._record_answer_ready is not None,
            )
            if len(groups) < 2:
                return False
            changed, _ = await self._merge_batch(
                messages, groups, transcript=await off_loop(_transcript, messages, groups)
            )
            return changed
        layout = prompt_digest, limit, len(groups), chain(0, len(groups))
        cursor = 0
        if self._batch_resume is not None and self._batch_resume[:4] == layout:
            cursor = self._batch_resume[4]
        self._batch_resume = None
        batches: list[tuple[list[dict[str, Any]], str, int]] = []
        # The first batch the same-tool runs cut below two records, for a sweep that merges none.
        across: list[dict[str, Any]] | None = None
        exhausted = False
        interrupted = False
        while over:
            if self._record_answer_ready is not None:
                await asyncio.sleep(0)
            while cursor < len(groups) and last(groups[cursor]) in handled:
                cursor += 1
            if len(groups) - cursor < 2:
                self._batch_resume = None
                exhausted = True
            if not exhausted:
                if not batches:
                    self._batch_resume = (*layout, cursor)
                self._check_chain_deadline()
                oldest = last(groups[cursor])
                key = prompt_digest, digests[oldest], limit
                if key not in self._record_input_fits:
                    self._record_input_fits[key] = await off_loop(
                        lambda: fits_summary_input(
                            self.merge_prompt,
                            _numbered_records([bodies[oldest]]),
                            tokenizer=self.tokenizer,
                            limit=limit,
                        )
                    )
                if not self._record_input_fits[key]:
                    self._merge_event("single_over_limit")
                    handled.add(oldest)
                    continue
                try:
                    batch = await capped(await search(cursor))
                    run = ends[cursor] - cursor
                    if len(batch) > run:
                        found = batch
                        batch = batch[:run] if run >= 2 else []
                        # A shorter run need not fit where a longer one did.
                        if batch and await off_loop(size, batch) > limit:
                            batch = []
                        if not batch and across is None:
                            across = found
                except _BatchProbesSpent:
                    interrupted = True
                    self._merge_event("search_limit", remaining=len(groups) - cursor)
                    logger.info(
                        "Compaction stopped looking for record batches after %d prefixes; "
                        "%d records were left unsearched.",
                        _MAX_BATCH_PROBES,
                        len(groups) - cursor,
                    )
                    exhausted = True
                else:
                    if len(batch) < 2:
                        self._merge_event("no_fitting_batch")
                        handled.add(oldest)
                        continue
                    handled.update(last(group) for group in batch)
                    transcript = await off_loop(
                        _numbered_records, [bodies[last(group)] for group in batch]
                    )
                    batches.append((batch, transcript, 0))
                    if len(batches) < self._summary_parallelism():
                        continue
            if not batches and across is not None and not changed:
                # Same-tool runs are a preference: when no batch within them merges, the first
                # batch across them the search found is taken rather than none.
                transcript = await off_loop(
                    _numbered_records, [bodies[last(group)] for group in across]
                )
                batches.append((across, transcript, 0))
                across = None
            if not batches:
                break
            seen_messages.update((id(message), message) for message in messages)
            if len(batches) == 1:
                batch, transcript, _ = batches[0]
                merged, failed = await self._merge_batch(messages, batch, transcript=transcript)
            else:
                merged, failed = await self._merge_parallel_batches(messages, batches)
            batches.clear()
            if merged:
                fresh = {
                    id(message): message for message in messages if id(message) not in seen_messages
                }
                handled.update(fresh)
                seen_messages.update(fresh)
                changed = True
                across = None
                groups, _, over = await off_loop(survey)
                layout = prompt_digest, limit, len(groups), chain(0, len(groups))
                self._batch_resume = None
                cursor = 0
            if failed:
                # A rate-limited or unstarted batch must be considered on the next sweep.
                self._batch_resume = None
                interrupted = True
                break
            if exhausted:
                break
        # Kept only while its start record stands, so it stays one entry a record.
        starts = {(digests[last(group)], limit) for group in groups}
        self._searched = {key: found for key, found in self._searched.items() if key in starts}
        self._merge_sweep_complete = exhausted and not interrupted and not changed
        return changed

    def _summary_parallelism(self) -> int:
        limiter = self.user_turns.summary_limiter
        if limiter is None:
            return 1
        return limiter.configure_request(
            self.user_turns.max_summary_input_tokens,
            {} if self.user_turns._summary_options_disabled else self.user_turns.summary_options,  # pyright: ignore[reportPrivateUsage]
        )

    async def _merge_parallel_batches(
        self,
        messages: list[Message],
        batches: list[tuple[list[dict[str, Any]], str, int]],
    ) -> tuple[bool, _MergeStop | None]:
        """Ask independently, then validate sources and apply answers in selection order."""
        prompt = self.merge_prompt
        sources = [source_key(messages, groups) for groups, _, _ in batches]
        if any(source is None for source in sources):
            # Object identities reattach sequential batches without authorizing persisted replay.
            identities = [
                {id(messages[int(group["end_index"])]) for group in groups}
                for groups, _, _ in batches
            ]
            changed = False
            for (_, body, depth), expected in zip(batches, identities, strict=True):
                if self.merge_prompt != prompt:
                    break
                groups = await off_loop(_expected_record_groups, messages, expected)
                if (
                    len(groups) != len(expected)
                    or await off_loop(_transcript, messages, groups) != body
                ):
                    continue
                merged, failed = await self._merge_batch(
                    messages, groups, transcript=body, depth=depth
                )
                changed = merged or changed
                if failed:
                    return changed, failed
            return changed, None
        # A batch not asked here reaches ``_merge_batch`` without an answer, to be split there.
        overflows = [
            await off_loop(self._merge_answer_overflow, body, len(groups))
            for groups, body, _ in batches
        ]
        tasks = [
            asyncio.create_task(self._ask(prompt=prompt, transcript=body))
            if (body, len(groups), _digest(prompt)) not in self._refused_merges and overflow is None
            else None
            for (groups, body, _), overflow in zip(batches, overflows, strict=True)
        ]
        active = [task for task in tasks if task is not None]
        changed = False
        stopped: _ChainTimeSpent | None = None
        rate_limited = False
        try:
            await asyncio.gather(*active, return_exceptions=True)
            for (original, body, depth), expected, task, overflow in zip(
                batches, sources, tasks, overflows, strict=True
            ):
                answer = None
                if task is not None:
                    try:
                        answer = task.result()
                    except SummaryRateLimited:
                        rate_limited = True
                        continue
                    except _ChainTimeSpent as error:
                        stopped = error
                        continue
                    if answer is None:
                        if self._background_requests and self._background_requests.closed:
                            stopped = _RunFinished()
                            continue
                        return changed, "failed"
                if expected is None or self.merge_prompt != prompt:
                    continue
                standing = await off_loop(consolidatable_record_groups, messages)
                groups = await off_loop(
                    _first_batch_with_sources, messages, standing, len(original), expected
                )
                if groups is None:
                    continue
                try:
                    merged, failed = await self._merge_batch(
                        messages,
                        groups,
                        transcript=body,
                        depth=depth,
                        answer=answer,
                        room_checked=True,
                        overflow=overflow,
                    )
                except _ChainTimeSpent as error:
                    stopped = error
                    continue
                changed = merged or changed
                if failed == "failed":
                    return changed, failed
                rate_limited = rate_limited or failed == "rate_limited"
            if stopped is not None:
                raise stopped
            return changed, "rate_limited" if rate_limited else None
        finally:
            for task in active:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    async def _merge_batch(
        self,
        messages: list[Message],
        groups: list[dict[str, Any]],
        *,
        transcript: str,
        depth: int = 0,
        answer: _RecordAnswer | None = None,
        room_checked: bool = False,
        overflow: _AnswerOverflow | None = None,
    ) -> tuple[bool, _MergeStop | None]:
        """Merge a batch or its smaller halves; return progress and any reason to stop asking.

        A batch whose required values cannot fit the answer budget is not asked, and is split
        as a truncated answer would have it split. ``room_checked`` says the caller made that
        check, with ``overflow`` its result. Unfinished smaller batches are retained
        independently of their parent.
        """
        if answer is None:
            self._check_chain_deadline()
        self._merge_event("batch_selected", records=len(groups), depth=depth)
        if self._record_answer_ready is not None and source_key(messages, groups) is None:
            # Background answers need durable sources to be applied by the foreground.
            self._merge_event("batch_skipped", reason="sources_not_durable")
            return False, None
        key = transcript, len(groups), _digest(self.merge_prompt)
        checked, overflow = overflow, None
        if key in self._refused_merges:
            self._merge_event("batch_skipped", reason="previous_refusal")
            self._record_merges_skipped += 1
        elif answer is None and (
            overflow := checked
            if room_checked
            else await off_loop(self._merge_answer_overflow, transcript, len(groups))
        ):
            self._merge_event("batch_skipped", reason=_SKIP_NO_ANSWER_ROOM)
            self._record_merges_skipped += 1
            logger.info(
                "Compaction did not ask to merge a batch of %d records: its required values "
                "cannot fit the answer budget; values_total=%d required=%d required_tokens=%d "
                "answer_tokens=%d.",
                len(groups),
                *overflow,
            )
        else:
            outcome = await self._consolidate(
                messages,
                groups,
                prompt=self.merge_prompt,
                transcript=transcript,
                answer=answer,
                remember_lossy=True,
            )
            if outcome == "failed" or outcome == "rate_limited":
                return False, outcome
            if outcome == "skipped":
                return False, None
            if outcome == "accepted":
                self._records_merged += 1
                return True, None
            self._record_merges_rejected += 1
            self._refused_merges.add(key)
            if outcome in {"lossy", "incomplete"}:
                self._split_merges.add(key)
        if (
            (overflow is None and key not in self._split_merges)
            or depth >= self.record_merge_split_depth
            or len(groups) < 3
        ):
            return False, None
        midpoint = len(groups) // 2
        changed = False
        children = groups[:midpoint], groups[midpoint:]
        identities = [
            {id(messages[int(group["end_index"])]) for group in child} for child in children
        ]
        queued: list[_PendingMerge] = []
        for child in children:
            records = 0
            for body in await off_loop(record_bodies, messages, child):
                records = (records * _RUN_BASE + int(_digest(body), 16)) % _RUN_MODULUS
            pending = _PendingMerge(
                len(child),
                format(records, "x"),
                depth + 1,
                _digest(self.merge_prompt),
                self._record_request_limit(),
            )
            queued.append(pending)
            if len(child) >= 2 and pending not in self._pending_merges:
                self._pending_merges.append(pending)
        if self._summary_parallelism() > 1:

            def fitting() -> list[tuple[list[dict[str, Any]], str, int]]:
                return [
                    (child, _transcript(messages, child), depth + 1)
                    for child in children
                    if len(child) >= 2
                    and fits_summary_input(
                        self.merge_prompt,
                        _transcript(messages, child),
                        tokenizer=self.tokenizer,
                        limit=self._record_request_limit(),
                    )
                ]

            batches = await off_loop(fitting)
            changed, failed = await self._merge_parallel_batches(messages, batches)
            if not failed:
                for pending in queued:
                    if pending in self._pending_merges:
                        self._pending_merges.remove(pending)
            return changed, failed
        for child, expected, pending in zip(children, identities, queued, strict=True):
            self._check_chain_deadline()
            if len(child) < 2:
                continue
            if changed:
                child = await off_loop(_expected_record_groups, messages, expected)
                if len(child) != len(expected):
                    if pending in self._pending_merges:
                        self._pending_merges.remove(pending)
                    continue
            child_transcript = await off_loop(_transcript, messages, child)
            if not await off_loop(
                fits_summary_input,
                self.merge_prompt,
                child_transcript,
                tokenizer=self.tokenizer,
                limit=self._record_request_limit(),
            ):
                if pending in self._pending_merges:
                    self._pending_merges.remove(pending)
                continue
            merged, failed = await self._merge_batch(
                messages, child, transcript=child_transcript, depth=depth + 1
            )
            changed = merged or changed
            if failed:
                return changed, failed
            if pending in self._pending_merges:
                self._pending_merges.remove(pending)
        return changed, None

    def _record_tokens(self, messages: list[Message]) -> int:
        """Return the included tokens of every record still being sent."""
        self._annotate_messages(messages)
        held = self._background_records.held_tokens if self._background_records is not None else 0
        return held + included_token_count(
            [
                message
                for group in active_record_groups(messages)
                for message in messages[group["start_index"] : group["end_index"] + 1]
            ]
        )

    def _record_request_limit(self) -> int:
        """Return the tokens one record merge or rewrite request may hold.

        ``record_request_input_tokens`` when set, never more than ``max_summary_input_tokens``.
        Otherwise, with a record share, the lesser of ``max_summary_input_tokens`` and an active
        ``summary_options.max_tokens`` cap. Reasoning also consumes the output allowance,
        so this limits batch size without promising an untruncated answer.
        """
        limit = self.user_turns.max_summary_input_tokens
        if self.record_request_input_tokens is not None:
            return min(limit, self.record_request_input_tokens)
        if self.record_budget_share is None:
            return limit
        cap = self._record_output_cap()
        return limit if cap is None else min(limit, cap)

    def _budgeted_prompt(self, prompt: str, request_tokens: int) -> str | None:
        """``prompt`` with :func:`record_answer_budget` for the request's cap, or None for none.

        None when the budget is switched off, the request has no cap, or the sentence would push
        the request over ``max_summary_input_tokens``: the plain request, ``request_tokens``, was
        checked against that ceiling, and it then goes out without the sentence rather than not
        at all. A request counts the sum of its messages, so only the two prompts are measured
        here, not the transcript again. Kept apart from the batch search, which sizes and
        remembers batches by the plain prompt.
        """
        cap = self._record_output_cap()
        if not self.state_record_budget or cap is None:
            return None
        budgeted = prompt + record_answer_budget(cap)
        added = summary_input_tokens(budgeted, "", tokenizer=self.tokenizer) - summary_input_tokens(
            prompt, "", tokenizer=self.tokenizer
        )
        fits = request_tokens + added <= self.user_turns.max_summary_input_tokens
        return budgeted if fits else None

    def _record_output_cap(self) -> int | None:
        """The ``max_tokens`` a summarizer request is sent with now, or None when it has none.

        None too once the options are retired after a failure, since the request then goes out
        without them.
        """
        options = (
            {}
            if self.user_turns._summary_options_disabled  # pyright: ignore[reportPrivateUsage]
            else self.user_turns.summary_options
        )
        cap = options.get("max_tokens")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            return None
        return cap

    def _merge_answer_overflow(self, transcript: str, records: int) -> _AnswerOverflow | None:
        """Return values, required values, their tokens and the answer budget, if over it.

        The required values are the shortest ones, written once each with a space between, and
        that text is counted whole, as an answer carries them: a value counted alone can cost
        more than it does among others. Read from the batch's own transcript, whose numbering
        is left out, so the check reads no history. None for a request without an output cap.
        """
        cap = self._record_output_cap()
        if cap is None:
            return None
        self._sync_merge_coverage()
        numbering = {str(number) for number in range(1, records + 1)}
        values = _distinctive_tokens(transcript) - numbering
        required = self._merge_coverage.required(len(values))
        shortest = " ".join(sorted(values, key=len)[:required])
        tokens = self.tokenizer.count_tokens(shortest) if shortest else 0
        budget = _record_answer_tokens(cap)
        return (len(values), required, tokens, budget) if tokens > budget else None

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
        await off_loop(self._refresh_merge_coverage, messages)
        changed = await self._merge_batches(messages, start, self.record_budget_share)
        await off_loop(self._refresh_merge_coverage, messages)
        # Worker histories cannot decide whether the foreground still needs a lower bar.
        if (
            not self._merge_coverage.adaptive
            or self._record_answer_ready is not None
            or not self._merge_sweep_complete
        ):
            return changed
        return await self._relax_merge_coverage(messages) or changed

    def _merge_sweep_key(self, messages: list[Message]) -> str | None:
        groups = consolidatable_record_groups(messages)
        if not groups:
            return None
        sources = source_key(messages, groups)
        if sources is None:
            return None
        self._sync_merge_coverage()
        coverage = self._merge_coverage
        return _digest(
            json.dumps(
                [
                    sources,
                    coverage.prompt,
                    coverage.start,
                    coverage.floor,
                    coverage.step,
                    coverage.current,
                    self.record_merge_split_depth,
                    self._record_request_limit(),
                ]
            )
        )

    async def _apply_completed_merge_sweep(
        self,
        messages: list[Message],
        worker: ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ) -> bool:
        if worker._completed_merge_sweep is None or worker._completed_merge_sweep != await off_loop(
            lambda: self._merge_sweep_key(BackgroundRecords.capture(messages).messages)
        ):
            return False
        return await self._relax_merge_coverage(messages)

    async def _relax_merge_coverage(self, messages: list[Message]) -> bool:
        if not self._merge_coverage.adaptive:
            return False
        if await self._apply_lossy_merges(messages):
            await off_loop(self._refresh_merge_coverage, messages)
            return True
        changed = False
        while await off_loop(self._records_over_share, messages) and self._merge_coverage.adaptive:
            self._check_chain_deadline()
            if not self._merge_coverage.lower_to_next_answer():
                break
            if await self._apply_lossy_merges(messages):
                changed = True
                break
        await off_loop(self._refresh_merge_coverage, messages)
        return changed

    def _records_over_share(self, messages: list[Message]) -> bool:
        return self.record_budget_share is not None and self._record_tokens(messages) > int(
            self._budget * self.record_budget_share
        )

    def _lossy_merge_matches(
        self, messages: list[Message]
    ) -> list[tuple[list[dict[str, Any]], LossyMerge]]:
        groups = consolidatable_record_groups(messages)
        wanted = {(answer.count, answer.sources) for answer in self._merge_coverage.answers}
        lineages: dict[int, set[str]] = {}
        for count, sources in wanted:
            version, _, payload = sources.partition(":")
            lineage, separator, _ = payload.partition(":")
            if version == "v2" and separator:
                lineages.setdefault(count, set()).add(lineage)
        windows: dict[tuple[int, str], list[dict[str, Any]] | None] = {}
        keys = MessageKeyCache()
        for count, wanted_lineages in lineages.items():
            for first in range(len(groups) - count + 1):
                batch = groups[first : first + count]
                sources = source_key(messages, batch, keys=keys, wanted_lineages=wanted_lineages)
                if sources is not None and (count, sources) in wanted:
                    key = count, sources
                    windows[key] = None if key in windows else batch
        matches: list[tuple[list[dict[str, Any]], LossyMerge]] = []
        for answer in self._merge_coverage.answers:
            batch = windows.get((answer.count, answer.sources))
            if batch is not None:
                matches.append((batch, answer))
        self._merge_coverage.answers = [answer for _, answer in matches]
        return sorted(matches, key=lambda item: (item[0][0]["start_index"], -item[1].count))

    def _sync_merge_coverage(self) -> None:
        coverage = self._merge_coverage
        start = self.tool_results.coverage_share if self._inherit_merge_coverage else coverage.start
        floor = start if self._inherit_merge_floor else coverage.floor
        if (start, floor) != (coverage.start, coverage.floor):
            self._merge_coverage = MergeCoverage(start, floor, coverage.step, coverage.cache_bytes)
        self._merge_coverage.bind(_digest(self.merge_prompt))

    def _refresh_merge_coverage(self, messages: list[Message]) -> None:
        self._sync_merge_coverage()
        if not self._merge_coverage.adaptive:
            return
        if not self._records_over_share(messages):
            self._merge_coverage.current = self._merge_coverage.start
        if self._merge_coverage.answers:
            self._lossy_merge_matches(messages)

    async def _apply_lossy_merges(self, messages: list[Message]) -> bool:
        changed = False
        while await off_loop(self._records_over_share, messages):
            self._check_chain_deadline()
            matches = await off_loop(self._lossy_merge_matches, messages)
            candidate = next(
                (
                    (groups, answer)
                    for groups, answer in matches
                    if answer.kept >= self._merge_coverage.required(answer.total)
                ),
                None,
            )
            if candidate is None:
                break
            groups, answer = candidate
            self._merge_coverage.answers.remove(answer)
            outcome = await self._consolidate(
                messages,
                groups,
                prompt=self.merge_prompt,
                transcript=await off_loop(_transcript, messages, groups),
                answer=_RecordAnswer(answer.text),
                remember_lossy=True,
            )
            # Scores from a snapshot are hints; consolidation rechecks every acceptance guard.
            if outcome == "accepted":
                self._records_merged += 1
                changed = True
            elif answer in self._merge_coverage.answers:
                self._merge_coverage.answers.remove(answer)
        return changed

    async def _merge_user_summaries(self, messages: list[Message]) -> bool:
        """Step b: fold the oldest user summaries that fit, when at least two fit."""
        with summary_request_admission(self._check_chain_deadline):
            outcome = await self.user_turns.fold_if_smaller(messages)
        if outcome == "folded":
            self._user_summaries_merged += 1
            return True
        if outcome == "rejected":
            self._user_merges_rejected += 1
        return False

    async def _rewrite_records(self, messages: list[Message], start: _ChainStart) -> bool:
        """Step c: rewrite the record harder, up to :attr:`harder_attempts` times on this pass.

        The oldest whole records within both request limits are handed over. With several,
        a rewrite is a harder merge, and the prompt says so. Each attempt is judged against the
        records as they stand when it is made, so a kept attempt raises the bar for the next.
        A record whose result this model call carried in is not handed over, as step a does
        not merge it.

        **A refusal forecloses its own attempt and every milder one, on that record, for the
        run under the same prompts.** Remembered by transcript, source count and the complete
        rewrite prompt sequence as the harshest attempt refused
        on it; an attempt at or below that is skipped rather than asked, and the loop goes on to
        the next attempt, and the chain to step d, exactly as after a refusal. The two directions
        are decided apart:

        - *Milder refused, harsher still open.* The pass that refuses attempt one goes straight on
          to attempt two, which is the escalation this step exists for, so a record is never
          written off on its milder refusal alone. It leaves a pass with one refused and two
          unanswered only when the summarizer failed on two, and a failure is not remembered: two
          is asked on the next pass, one is skipped.
        - *Harsher refused, milder foreclosed.* A refusal means the answer was not smaller, or
          smaller by less than :attr:`rewrite_gain_fraction` of what it replaced: told to keep
          every value and cut the rest to about a third, the summarizer cut next to nothing.
          Asking it to cut less is weaker pressure towards the same outcome. The case is common --
          attempt one kept, attempt two refused on the kept record -- and without this every such
          record would buy one more refusal on the next pass. A rewrite refused as lossy
          (:meth:`_keeps_values`) is foreclosed the same way: the milder attempt was asked first on
          this pass, so the record it would revisit is one the chain already had its chance at,
          and the record simply stands at its size until it changes.

        A record refused at attempt ``harder_attempts`` is therefore incompressible until it
        or the prompt sequence changes; a merge folding new material in, or a kept rewrite,
        gives it a new transcript and every attempt back.
        """
        changed = False
        prompts = [self.rewrite_prompt(attempt) for attempt in range(1, self.harder_attempts + 1)]
        if any(
            not isinstance(prompt, str) or not prompt.strip()  # pyright: ignore[reportUnnecessaryIsInstance]
            for prompt in prompts
        ):
            raise ValueError("rewrite_prompt must return a nonempty string for each attempt.")
        policy = _digest(json.dumps(prompts))
        for attempt, prompt in enumerate(prompts, 1):
            self._check_chain_deadline()
            if attempt > 1 and not self._short_of_target(messages, start):
                break
            groups, bodies = await off_loop(self._bounded_records, messages, prompt)
            if not groups:
                continue
            transcript = await off_loop(_numbered_records, bodies)
            key = (transcript, len(groups), policy)
            if self._refused_rewrites.get(key, 0) >= attempt or not await off_loop(
                self._rewrite_target_fits, bodies, attempt, prompt
            ):
                self._record_rewrites_skipped += 1
                continue
            self._record_rewrites += 1
            outcome = await self._consolidate(
                messages,
                groups,
                prompt=prompt,
                transcript=transcript,
                min_gain_fraction=self.rewrite_gain_fraction,
                operation="rewrite",
            )
            if outcome == "accepted":
                changed = True
            elif outcome in {"rejected", "lossy", "incomplete"}:
                self._record_rewrites_rejected += 1
                self._refused_rewrites[key] = max(self._refused_rewrites.get(key, 0), attempt)
            elif outcome == "rate_limited":
                break
        return changed

    def _bounded_records(
        self, messages: list[Message], prompt: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        groups = consolidatable_record_groups(messages)[: self.record_merge_max_records]
        bodies = record_bodies(messages, groups)
        batch = bounded_prefix(
            groups,
            render=lambda prefix: _numbered_records(bodies[: len(prefix)]),
            prompt=prompt,
            tokenizer=self.tokenizer,
            limit=self._record_request_limit(),
        )
        return batch, bodies[: len(batch)]

    def _rewrite_target_fits(self, bodies: list[str], attempt: int, prompt: str) -> bool:
        """A built-in multi-record target must fit the shortest required distinct values."""
        if len(bodies) == 1 or prompt != versioned_prompt(
            harder_record_prompt(attempt), self.user_turns.prompt_version
        ):
            return True
        self._sync_merge_coverage()
        values = _record_values(bodies)
        required = self._merge_coverage.required(len(values))
        lengths = sorted(map(len, values))
        minimum = sum(lengths[:required]) + max(0, required - 1)
        return 100 * minimum <= _rewrite_percent(attempt) * sum(map(len, bodies))

    async def _consolidate(
        self,
        messages: list[Message],
        groups: list[dict[str, Any]],
        *,
        prompt: str,
        transcript: str,
        answer: _RecordAnswer | None = None,
        min_gain_fraction: float = 0.0,
        remember_lossy: bool = False,
        merge_phase: RecordMergePhase | None = None,
        prepared_sources: str | None = None,
        operation: RecordReduction = "merge",
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
            min_gain_fraction: Share of the replaced records' tokens the replacement must
                remove; an answer that keeps the values and removes less is refused though
                it is smaller. One that drops values is refused as lossy first, and counted.
            remember_lossy: Retain an eligible merge answer for adaptive acceptance.

        Returns:
            Whether it was kept, refused, unavailable, or its sources became ineligible.
        """
        sources = [
            message
            for group in groups
            for message in messages[group["start_index"] : group["end_index"] + 1]
        ]
        merge = operation == "merge" and len(groups) >= 2
        if any(_record_has_external_hold(message) for message in sources):
            if merge:
                self._merge_event("answer_discarded", phase=merge_phase, reason="external_hold")
            return "skipped"
        self._sync_merge_coverage()
        cache_sources = (
            source_key(messages, groups)
            if self._merge_coverage.adaptive and remember_lossy
            else None
        )
        if answer is None:
            try:
                answer = await self._ask(prompt=prompt, transcript=transcript, operation=operation)
            except SummaryRateLimited:
                return "rate_limited"
        if answer is None:
            return "failed"
        if self._record_answer_ready is not None:
            prepared_sources = source_key(messages, groups)
            if prepared_sources is not None:
                self._record_answer_ready(prepared_sources, len(groups), transcript, answer)
        if any(_record_has_external_hold(message) for message in sources):
            if merge:
                self._merge_event("answer_discarded", phase=merge_phase, reason="external_hold")
            return "skipped"
        text = answer.text
        bodies, total, kept, required, replaced_tokens, saved = await off_loop(
            self._assess_answer, messages, groups, text
        )
        outcome: _Consolidation = "accepted"
        reason = ""
        if answer.incomplete:
            outcome, reason = "incomplete", "output limit"
        elif saved <= 0:
            outcome, reason = "rejected", "no smaller"
        elif kept < required:
            self._record_consolidations_lossy += 1
            outcome, reason = "lossy", "dropping values"
        elif not _meets_gain_floor(saved, replaced_tokens, min_gain_fraction):
            outcome, reason = "rejected", "saving too little"
        if self._record_answer_ready is None and (active := self._role_measurement.get()):
            if (measurement := active()) is not None:
                await off_loop(measurement.observe, operation, outcome, bodies, text)
        if outcome != "accepted":
            if (
                outcome == "lossy"
                and len(groups) >= 2
                and cache_sources is not None
                and cache_sources == source_key(messages, groups)
                and _meets_gain_floor(saved, replaced_tokens, min_gain_fraction)
            ):
                self._merge_coverage.remember(
                    LossyMerge(cache_sources, len(groups), text, total, kept)
                )
            if merge:
                self._merge_event("answer_rejected", phase=merge_phase, reason=reason)
            logger.warning(
                "Compaction kept a batch of %d records unmerged: the %s was refused as %s; "
                "values_total=%d kept=%d missing=%d required=%d.",
                len(groups),
                operation,
                reason,
                total,
                kept,
                total - kept,
                required,
            )
            return outcome
        bar = self.current_merge_coverage_share
        self._record_consolidations_by_coverage[bar] = (
            self._record_consolidations_by_coverage.get(bar, 0) + 1
        )
        self._record_values_omitted += total - kept
        if kept < total:
            logger.warning(
                "Compaction accepted a batch of %d records at coverage=%g; "
                "values_total=%d kept=%d missing=%d required=%d.",
                len(groups),
                bar,
                total,
                kept,
                total - kept,
                required,
            )
        edit = await off_loop(self._accept_record, messages, groups, text, transcript)
        if merge and record_merge_observer.get() is not None:
            tracked = set(tracked_merge_values.get())
            kept_tracked = 0
            if tracked:
                source_values = {
                    value for body in bodies for value in _record_tokens(body, fold_case=False)
                }
                tracked.intersection_update(source_values)
                kept_tracked = len(tracked.intersection(_record_tokens(text, fold_case=False)))
            if prepared_sources is None and edit is not None:
                prepared_sources = edit.sources
            replacement = messages[int(groups[-1]["end_index"]) + 1]
            self._merge_event(
                "answer_accepted",
                phase=merge_phase,
                records=len(groups),
                replacement=record_merge_identity(replacement) or "",
                batch=_digest(f"{prepared_sources}:{text}") if prepared_sources else "",
                values=total,
                kept_values=kept,
                tracked_values=len(tracked),
                kept_tracked_values=kept_tracked,
                saved_tokens=saved,
            )
        await off_loop(self._settle_accepted_record, messages)
        return "accepted"

    def _assess_answer(
        self, messages: list[Message], groups: list[dict[str, Any]], text: str
    ) -> tuple[list[str], int, int, int, int, int]:
        """Measure a record answer against the records it would replace, off the loop.

        Returns the source bodies, the values they hold, kept and required, the tokens the
        sources count, and how many the answer saves.
        """
        bodies = record_bodies(messages, groups)
        total, kept, required = self._value_coverage(bodies, text)
        candidate = [build_record_message(text)]
        replaced = [build_record_message(body) for body in bodies]
        annotate_token_counts(candidate, tokenizer=self.tokenizer, force_retokenize=True)
        annotate_token_counts(replaced, tokenizer=self.tokenizer, force_retokenize=True)
        replaced_tokens = included_token_count(replaced)
        return (
            bodies,
            total,
            kept,
            required,
            replaced_tokens,
            replaced_tokens - included_token_count(candidate),
        )

    def _accept_record(
        self, messages: list[Message], groups: Any, text: str, transcript: str
    ) -> Any:
        """Put an accepted record in place of the records it merges, off the loop.

        Returns the edit recorded for replay, or None when there is none.
        """
        if self._background_records is not None:
            # Only the foreground journals an edit against the authoritative history.
            replacement = build_record_message(text)
            replacement.additional_properties[LINEAGE_KEY] = source_lineage(messages, groups)
            for group in groups:
                set_excluded(messages[int(group["end_index"])], excluded=True)
            messages.insert(int(groups[-1]["end_index"]) + 1, replacement)
            self._annotate_messages(messages)
            return None
        edit = self._record_edit(messages, groups, text)
        self.tool_results.consolidate_records(messages, groups, text)
        self._kept_records[(transcript, len(groups))] = text
        if edit is not None and edit not in self._record_order:
            self._record_order.append(edit)
        return edit

    def _settle_accepted_record(self, messages: list[Message]) -> None:
        """Take decided replacements of the new record and refresh coverage, off the loop."""
        # The new record may read exactly as one an earlier pass went on to replace -- a merge of
        # new material can come back as the record an earlier rewrite started from -- and that
        # replacement is already decided, so it is taken here rather than asked for again.
        if self._background_records is None:
            self._keep_records(messages)
        self._refresh_merge_coverage(messages)

    def _keeps_values(self, bodies: list[str], text: str) -> bool:
        """Whether ``text`` meets the current merge coverage for distinctive source values.

        The rule a record meets before it may stand in for a tool group (see
        :meth:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy._is_covered`),
        applied to a record that replaces records. Records holding no distinctive value pass.
        """
        _, kept, required = self._value_coverage(bodies, text)
        return kept >= required

    def _value_coverage(self, bodies: list[str], text: str) -> tuple[int, int, int]:
        """Return distinct source values, matched values and the required match count."""
        self._sync_merge_coverage()
        values = _record_values(bodies)
        quoted = set(_record_tokens(text, fold_case=False))
        return (
            len(values),
            len(values & quoted),
            self._merge_coverage.required(len(values)),
        )

    async def _ask(
        self, *, prompt: str, transcript: str, operation: RecordReduction = "merge"
    ) -> _RecordAnswer | None:
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
        request_tokens = await off_loop(
            summary_input_tokens, prompt, transcript, tokenizer=self.tokenizer
        )
        if request_tokens > self.user_turns.max_summary_input_tokens:
            logger.warning("Skipping record consolidation: summary input exceeds its token limit.")
            self._record_summary_failures += 1
            return None
        self._check_chain_deadline()
        started = monotonic()
        incomplete = False
        merge = operation == "merge"
        if merge:
            self._merge_event("request_prepared")

        def observe(response: ChatResponse) -> None:
            nonlocal incomplete
            finish = (
                str(response.finish_reason) if response.finish_reason is not None else "unknown"
            )
            incomplete = finish == "length"
            if finish not in {"stop", "length", "content_filter", "tool_calls"}:
                finish = "unknown"
            usage = response.usage_details or {}
            if merge:
                self._merge_event("response_received", finish=finish)
            logger.info(
                "Compaction record %s response: seconds=%.3f finish_reason=%s "
                "input_tokens=%s output_tokens=%s reasoning_tokens=%s.",
                operation,
                monotonic() - started,
                finish,
                usage.get("input_token_count"),
                usage.get("output_token_count"),
                usage.get("reasoning_output_token_count"),
            )

        capped_prompt = await off_loop(self._budgeted_prompt, prompt, request_tokens)
        try:
            requests = self._background_requests
            async with requests.request() if requests is not None else nullcontext():
                text = await self.user_turns._generate(  # pyright: ignore[reportPrivateUsage]
                    transcript,
                    prompt=prompt,
                    observe=observe,
                    allow_empty_on_length=True,
                    before_request=self._check_chain_deadline,
                    capped_prompt=capped_prompt,
                )
            return _RecordAnswer(text, incomplete)
        except asyncio.CancelledError:
            if merge:
                self._merge_event("request_cancelled")
            raise
        except (_ChainTimeSpent, SummaryRateLimited):
            raise
        except Exception as error:
            if merge:
                self._merge_event("request_failed")
            # Broad on purpose, as the user half's is: this runs inside the chat client's own
            # call path, so anything not caught here fails the user's turn.
            logger.warning(
                "Skipping record consolidation: summary generation failed (%s).",
                type(error).__name__,
            )
            self._record_summary_failures += 1
            return None
