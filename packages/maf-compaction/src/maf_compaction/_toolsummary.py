"""Have the agent record the facts, then drop everything behind the record.

Every truncating strategy measured here fails the same way, and the failure is arithmetic
rather than tuning. Head-and-tail retention keeps the first and last f/2 of a result, so ``n``
values spread evenly through it -- sitting 1/n apart -- survive only when f exceeds 2/n. With
eight values per result that needs more than 25% of it retained, which is not compaction.
Measured: at 30% retention 3 of 8 codes survived, at 0.9% just 1.

Truncation preserves *positions*. What is needed is something that preserves *information*,
after which the bulk it came from is genuinely redundant and can be dropped outright rather
than sampled.

**Four parts, and all four are the design rather than harness around it.** The strategy
cannot work without a tool for the model to call (:func:`make_recall_tool`) or without
something keeping that tool inert when nobody asked for a record (:class:`RecallGate`), so
both live here beside the middleware that arms them.

**Two phases, split across a middleware and this strategy.**

1. :class:`ToolResultRecallMiddleware` forces ``tool_choice`` to the recall tool on the call
   after the one where it saw the conversation grow past the trigger -- and again later, once
   the agent has done tool work no existing record accounts for. It sends no message at all:
   the tool's own description already says what to pass, and the schema travels on every
   request anyway. Nothing is added to the prompt, and nothing extra reaches the caller's
   stored history. :meth:`ToolResultRecallMiddleware._record_due` is where "no existing record
   accounts for it" is defined, and it is defined nowhere else.
2. The model makes the call, the agent executes it, and the result is persisted through the
   ordinary path. On a later pass this strategy finds that real tool result in the loaded
   history and drops the tool groups in front of it whose contents the record demonstrably
   carries -- see the coverage note below, and ``_drop_before`` for how that is decided.

Sending no message matters for more than tokens. A message appended here carries no history
provider's source tag, so the per-service-call persistence would treat it as new input and
store it -- and an instruction of ours would show up in the conversation the application
replays to its user. Forcing the option leaves no such trace. The tool call and its result do
appear, which is correct: they are a real record of what the agent did.

**Why not synthesise the tool call directly.** A ``call_id`` invented by the client is only
safe when the client owns the conversation. Responses-API routes with ``store=True`` track
tool calls server-side, so a fabricated call is unknown to the service or mismatched against
it, and Gemini's thought signatures behave similarly. Letting the provider issue the call
avoids the problem entirely, at the price of one extra agent turn. The argument reaches every
record, not only the first: a merged record written as a synthesised recall call is refused by
Foundry with ``400 invalid_payload`` on the next request. A record this package writes is an
ordinary message; see :func:`build_record_message`.

**Why a tool result rather than an assistant message.** A tool result is data. Assistant prose
is the first thing a size-pressed strategy sheds -- this package's own anchored strategy sheds
it as a last resort -- so a record written as narration would be eligible for exactly the step
that destroys it. The one record written as an assistant message -- the one the composed row
writes in place of several, which cannot be a tool call for the reason above -- is preserved
from the moment it is inserted, so that step never reaches it; :func:`build_record_message`
says why that is enough.

**Why forcing beats asking.** Asking requires the model to choose, which leaves ``tool_choice``
unpinned -- and unpinned, an uncompacted conversation's cost varies by a factor of two between
identical runs while the strategy gathers fewer facts. Forcing the call keeps every other turn
pinned, so a comparison stays measurable, and makes phase 1 deterministic rather than a
compliance rate to be estimated.

**How the record is bounded, and why it takes two numbers rather than one.** The instructions
ask for everything, so the record wants to grow. A ``max_tokens`` cap does not answer that: a
model does not plan to fit a cap, it writes until it is cut, and on a *tool call* the cut
lands inside the arguments JSON, so a cap set where the record should end produces no record
instead of a shorter one. The two bounds therefore do different jobs.
:data:`DEFAULT_RECORD_TARGET_TOKENS` is stated in the tool's own description, which is the
only channel that reaches the model before it writes, since the middleware sends no message.
:data:`DEFAULT_RECORD_MAX_TOKENS` is set on the forced call alone and is roughly twice the
target, so it bounds the bill without ever being the thing that stops the writing. When it is
the thing that stops it, ``ToolResultRecallMiddleware.records_truncated`` says so.

**What the record covers is checked, not assumed.** Dropping every tool group in front of the record
on the assumption that the record replaced them is model-dependent: one model writes records naming
every tool group, another writes one covering two of six, and the other four would go with nothing
preserving them and nothing reporting it. Raising the cap, raising the stated target and rewriting
the prompt each measure as no change, so the strategy drops only the groups the record demonstrably
carries, and ``groups_kept_uncovered`` counts the rest. That turns the failure direction around: a
partial record now costs tokens it should not have cost, instead of losing facts nobody can trace.
``ToolResultRecallMiddleware``'s ``max_groups_before_record`` is the other half, bounding how much
any one record is asked to cover so partial coverage stops being the normal case.

**Several records, and nothing merges them.** Once the size trigger may fire more than once, a
long conversation accumulates records, and every one of them is preserved: unshrinkable,
undroppable, and counted against the ceiling in full. That is a floor under the prompt that
grows a record at a time, and
:attr:`ToolResultAnchoredSummarizationCompactionStrategy.records_in_conversation` reports it,
because a row whose compaction has stopped paying for a good reason and one whose unshrinkable
part has quietly grown are otherwise the same row. This strategy still never consolidates them:
an older record is the sole account of the groups behind *it*, so a merge rewrites the evidence
rather than the bulk.

**The composition consolidates records as a last resort; this strategy never does.**
:class:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` merges the
active records into one when the prompt is still over the ceiling after both of its halves have
run, and rewrites the record shorter if that is not enough -- on the stated ground that the
alternative at that point is the fallback or an overflow, both worse than a rewrite that is at
least smaller. What this module supplies for that is the record's shape and nothing else:
:func:`active_record_groups` says which records still stand,
:meth:`ToolResultAnchoredSummarizationCompactionStrategy.consolidate_records` swaps them for one,
:func:`build_record_message` is the form the replacement takes -- an assistant message carrying
the marker, never a tool call -- and every reader of records recognises that form beside the
recall tool's result; an excluded record is not a record to anything that reads records -- see
:func:`find_record_index` and :func:`record_text`. The standalone strategy calls none of it.

**Coverage is measured in values, not in tool names, because models do not write tool names.**
The first version of the check asked whether the record contained the group's function name,
on the reading that :data:`RECALL_VALUES_DESCRIPTION` asks for the results "grouped by the
tool that produced it". Models do not comply with that clause the way the check assumed.
Luna's record says *"extra0 deployment lookup returned codes: ..."* and never writes
``lookup_extra0`` anywhere; gpt-5.4-mini, whose records carry every value from every group,
scored ``UNCOVERED:4`` on the same rule, and its compaction fell from a 20% reduction to 5-6%
for no benefit whatsoever. A check that penalises the model that complied is not a check. The
rule is now the first thing that description actually asks for -- "Quote verbatim any value
that cannot be reconstructed or guessed" -- so a group is covered when the record quotes
enough of the distinctive values its results contain. :data:`DEFAULT_COVERAGE_SHARE` is how
much of them, and :func:`_distinctive_tokens` states the rule that finds them and what it
cannot see.

**The record is protected from the fallback, and had to be.** When a record does not free
enough, whatever remains goes to ``fallback``, which defaults to
:class:`~._anchored.AnchoredCompactionStrategy` -- and that strategy shortens and sheds tool
results, of which the record is one. Nothing in it recognised a record, so the record was
trimmed like any other bulk: a live seed's record of four lookups reached the answering prompt
carrying two, 16,617 tokens gone with only three messages removed. Every deletion phase 2
performs is licensed by the record, so trimming the record afterwards destroys the sole
surviving copy of what was already deleted. Both halves therefore agree through
:mod:`._preserve`: this strategy marks every record it observes, and the anchored strategy
skips preserved messages in each of its three removal paths.

**What the record failed to cover is held out of the fallback's reach too, and asked for
again.** The coverage check keeps a group the record does not carry, and for a while that was
the whole of it: the group stayed in the prompt as an ordinary tool group, and the fallback
that runs when the prompt is still over the ceiling could then shorten or shed it like any
other. That is a gap in the headline claim -- this row is supposed never to lose a fact -- and
it is reachable whenever the fallback legitimately runs behind a partial record, which is not a
one-model concern: gpt-5.4-mini's ``RECFALLBACK`` records sit at 0.89 to 0.955 of the billed
ceiling and are genuine firings. Two layers now stand between an uncovered group and the
fallback, in this order. First the
strategy asks the middleware for another record while the group is still whole, and holds the
group out of the fallback's reach until that record has had its chance --
:meth:`ToolResultAnchoredSummarizationCompactionStrategy.take_reforce` is the channel, and the
bound on asking again is stated on
:meth:`ToolResultAnchoredSummarizationCompactionStrategy._reforce_or_settle`.
Second, once asking has stopped working, the group is preserved for good under
:data:`PRESERVE_REASON_UNCOVERED`, which the fallback honours exactly as it honours the record.
A preserved group still counts against the ceiling, so the accepted consequence is a prompt
that cannot be brought under it and a row that reads ``DQ``: loud, and preferable to the quiet
loss it replaces. ``REFORCED`` and ``PRESERVED`` in the flags say which layer acted.

**The two layers are not enough on their own, because they only see what is in front of the
record.** Uncovered groups in front of the record are preserved and survive, which keeps the
prompt near the ceiling, so the fallback fires pass after pass and shortens the one tool group
after the record inside its band until its values are gone -- no record covers a group after
the newest one, and nothing else protects it. The conversation finishes under the limit and
facts short. So the fallback that runs behind a record runs with every tool group no record
covers held under :data:`PRESERVE_REASON_UNRECORDED`, wherever it sits, and may remove only
what is not a tool group. That frees enough or it does not; the conversation keeps every fact
or overflows loudly, and a quiet loss is not one of the outcomes. ``RECHELD`` says the rule was
in force. The give-up fallback taken when no record ever arrives is left as it is: that path
measures the fallback strategy rather than this one, and ``FALLBACK`` says so.
"""

from __future__ import annotations

import string
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, replace
from math import ceil
from typing import TYPE_CHECKING, Any, Final

from agent_framework import ChatContext, ChatMiddleware, ChatResponse, Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    set_excluded,
)

from ._anchored import AnchoredCompactionStrategy
from ._preserve import (
    PRESERVE_REASON_KEY,
    any_preserved,
    is_preserved,
    removable_whole,
    set_preserved,
)

if TYPE_CHECKING:
    from agent_framework import CompactionStrategy, TokenizerProtocol

__all__ = [
    "CONSOLIDATE_EXCLUDE_REASON",
    "DEFAULT_COVERAGE_SHARE",
    "DEFAULT_FALLBACK_FRACTION",
    "DEFAULT_RECORD_MAX_TOKENS",
    "DEFAULT_RECORD_TARGET_TOKENS",
    "DEFAULT_TRIGGER_FRACTION",
    "PRESERVE_REASON_UNCOVERED",
    "PRESERVE_REASON_UNRECORDED",
    "RECALL_TOOL_NAME",
    "RECORD_MARKER",
    "RecallGate",
    "ToolResultAnchoredSummarizationCompactionStrategy",
    "ToolResultRecallMiddleware",
    "active_record_groups",
    "build_record_message",
    "consolidatable_record_groups",
    "find_record_index",
    "make_recall_tool",
    "record_body",
]

#: Name of the tool the agent must call. The strategy looks for this name in the history, so
#: the tool the caller registers has to match it.
RECALL_TOOL_NAME: Final[str] = "recall_earlier_tool_results"

#: Prefix the recall tool puts on a result that counts as a record.
#:
#: The tool cannot be hidden from the model. Tools passed through per-call options reach the
#: model but not the executor -- ``FunctionInvocationLayer`` wraps ``ChatMiddlewareLayer``, so
#: it has already built its tool map by the time a middleware could add one, and the model's
#: call goes unanswered. A registered tool is therefore advertised on every request, and this
#: one was called unprompted on the unpinned follow-up call in every run.
#:
#: So the tool stays visible and becomes *inert* instead: it records only while the middleware
#: has armed it, and a result without this marker is not a record. The model may still call
#: it; calling it uninvited simply achieves nothing.
RECORD_MARKER: Final[str] = "[recorded by compaction]"

#: What the recall tool writes between :data:`RECORD_MARKER` and the model's own text.
#:
#: A constant rather than a literal inside :func:`make_recall_tool` so that the one other writer
#: of a record -- :func:`build_record_message`, which the composed row uses to put a merged record
#: in place of several -- writes the same bytes, so that :func:`record_body` can take them off
#: again before a record is handed to a summarizer as content, and so that
#: :data:`_WRITTEN_RECORD_PREFIX` can recognise a record that other writer made.
_RECORD_PREAMBLE: Final[str] = (
    "Earlier tool results may have been shortened, and this is their "
    "compaction record. Treat values in this record as authoritative for the tool it "
    "names, and treat information as absent only if it appears nowhere, including "
    "here."
)

#: How a record this package wrote opens: the marker, then the recall tool's own preamble.
#:
#: The whole of how :func:`_is_written_record` recognises one. Both parts rather than the marker
#: alone, because the model reads records and may quote the marker back in a reply of its own; a
#: reply that opens with the marker *and* the full preamble is not one it has a reason to write.
_WRITTEN_RECORD_PREFIX: Final[str] = f"{RECORD_MARKER} {_RECORD_PREAMBLE}"

#: Reason recorded on a record's messages when a consolidated record replaced it.
CONSOLIDATE_EXCLUDE_REASON: Final[str] = "tool_summary_consolidated"

#: Hard ceiling put on the forced call's response, so a runaway record cannot cost more than
#: intended.
#:
#: A cap is not a plan. The model does not shorten to fit one; it writes until it is cut, and
#: on a *tool call* the cut lands inside the arguments JSON, so what a too-low cap produces is
#: not a shorter record but no record at all. That makes this the wrong instrument for sizing
#: the record and the right one for bounding the bill, and the two numbers here are set
#: accordingly: this is roughly twice :data:`DEFAULT_RECORD_TARGET_TOKENS`, so a model that
#: overshoots its stated target still finishes inside the cap.
DEFAULT_RECORD_MAX_TOKENS: Final[int] = 4_000

#: Reason recorded on the record's messages when this strategy protects them, so a caller
#: reading a conversation back can tell which strategy claimed them.
PRESERVE_REASON: Final[str] = "tool_summary_record"

#: Reason recorded on a tool group this strategy holds out of its fallback's reach because no
#: record has covered it yet, or ever will.
#:
#: Its own string rather than :data:`PRESERVE_REASON`, because the two marks are read
#: differently on the next pass. A record stays preserved for the rest of the run. A held group
#: is a candidate again the moment a later record quotes its values, and is released and dropped
#: then -- which is the whole point of asking for that record. ``_drop_before`` tells the two
#: apart by this string, and skips a group only when something *else* has claimed it.
PRESERVE_REASON_UNCOVERED: Final[str] = "tool_summary_uncovered"

#: Reason recorded on a tool group no record covers, held out of the fallback's reach because
#: the fallback is about to run behind a record.
#:
#: The case :data:`PRESERVE_REASON_UNCOVERED` cannot reach: a group *after* the newest record,
#: which no record was asked to cover, or one in front of it that the coverage check never
#: weighed, such as the head. Shortening such a group destroys its facts exactly as
#: shortening an uncovered one does, and once the fallback is running behind a record there is
#: no case where that is the right trade -- the row either fits on what else may go, or
#: overflows and reads ``DQ``. Its own string so a conversation read back tells this rule from
#: the coverage check's hold, and so ``fallbacks_held_after_record`` counts what it says.
#: Released the way that hold is: ``_drop_before`` treats both as this strategy's own, so a
#: later record that quotes the group licenses its deletion.
PRESERVE_REASON_UNRECORDED: Final[str] = "tool_summary_unrecorded"

#: The preservation reasons this strategy puts on ordinary tool groups, and so may lift again.
#: Every other reason -- the record's own mark, or one another strategy left -- is not this
#: strategy's to decide about. See :func:`_claimed_elsewhere`.
_OWN_HOLDS: Final[frozenset[str]] = frozenset(
    {PRESERVE_REASON_UNCOVERED, PRESERVE_REASON_UNRECORDED}
)

#: Compaction passes a re-forced record is given to reach the history before the ask is judged
#: to have failed.
#:
#: Two, and the number is the pipeline's shape rather than a patience setting. The ask is made
#: on a pass; the middleware takes it on the exit of that pass's call and pins the call after
#: it; that pinned call runs its own compaction pass before the model writes anything, which is
#: the first pass after the ask and cannot see a record. The model's tool call is executed and
#: the follow-up call carries its result, so the follow-up's pass -- the second -- is the first
#: that can see the record, and a record not there by then was not written: the model ignored
#: the pin, the provider cut the call, or the option was refused. Waiting longer would hold the
#: groups out of the fallback's reach on the evidence of an ask that has already failed; waiting
#: less would judge the ask on a pass that could not have seen its answer. A re-sent call adds a
#: pass and can settle a group one call early, which errs towards preserving and costs nothing
#: a later record cannot undo: a settled group that a record then quotes is released and
#: dropped like any other.
_REFORCE_ARRIVAL_PASSES: Final[int] = 2

#: Share of a group's distinctive values the record must quote before the group may be dropped.
#:
#: The number has to sit between two failures. At 1.0 the check is as brittle as the tool-name
#: rule it replaces: one value the model rendered differently -- a number regrouped, a
#: timestamp normalised, an identifier wrapped in quotes the strip below does not remove --
#: keeps a whole group whose content is demonstrably present, and the measured cost of that
#: mistake was compaction falling from 20% to 5-6% on a model whose records were complete. Far
#: below 0.5 the check stops being one: a record that quoted two values from a group of eight
#: would license deleting the other six, which is the silent loss the whole coverage check
#: exists to prevent.
#:
#: 0.8 is the loosest setting that still refuses a record which dropped a quarter of a group,
#: and at the eight values per result these runs were measured on it tolerates exactly one
#: value in eight being unrecognisable. It is a threshold rather than a derivation, and it is a
#: constructor keyword because the right value depends on how many values a workload's results
#: carry: at two values per group the share can only be 0, 0.5 or 1, so a workload like that
#: should set it deliberately rather than inherit this.
DEFAULT_COVERAGE_SHARE: Final[float] = 0.8

#: Shortest token :func:`_distinctive_tokens` will treat as a value worth quoting.
#:
#: Three characters and under is where ordinary prose with a digit in it lives -- "3rd", "v2",
#: "10%", "1)" -- and none of that is a value a later question could depend on. It is also
#: where collisions live: a record about anything at all is likely to contain "42" or "v3"
#: somewhere, and a token that short would be matched by a mention that has nothing to do with
#: the group it came from.
_MIN_DISTINCTIVE_LENGTH: Final[int] = 4

#: Characters that end a token, over and above whitespace.
#:
#: These are the characters that *separate* values in the shapes a tool result arrives in --
#: CSV rows, JSON objects, semicolon-delimited pairs, bracketed and quoted forms -- so a value
#: sitting next to one of them has to come out as a token in its own right.
#:
#: What is deliberately absent is the punctuation that lives *inside* values, because splitting
#: on it would shred the very things :data:`RECALL_VALUES_DESCRIPTION` calls unreconstructable:
#: ``-`` and ``_`` in identifiers, ``.`` in versions and hostnames, ``/`` and ``\`` in paths and
#: URLs, and ``:`` in clock times and timestamps. ``=`` is absent for a different reason and is
#: handled separately in :func:`_tokens`.
_SEPARATORS: Final[str] = ",;|\"'`()[]{}<>"

#: :data:`_SEPARATORS` as a translation table, built once rather than per call.
_SEPARATOR_TABLE: Final[dict[int, str]] = str.maketrans(dict.fromkeys(_SEPARATORS, " "))

#: Record length stated in the tool's own description, which is the only channel that makes
#: the model aim for a size.
#:
#: The middleware deliberately sends no message -- an appended instruction would be persisted
#: into the caller's own conversation -- so the description and the ``values`` parameter are
#: the entire prompt, and a target has to be baked into them at construction.
DEFAULT_RECORD_TARGET_TOKENS: Final[int] = 2_000

#: Fraction of the ceiling at which a record is first asked for.
#:
#: One constant read by both halves rather than a default written twice, because the middleware
#: asks and the strategy waits: a caller who moved one without the other would either have the
#: strategy dropping groups before anything had been recorded, or have the middleware recording
#: what nothing was yet willing to drop.
#:
#: **0.6, and it was briefly 0.8.** Every measured run of this strategy used 0.6. 0.8 was
#: reasoned to and never run, and the reasoning does not survive the project's own data.
#:
#: The argument for moving it was that 0.6 of the input budget is 58% of a 60,000-token window,
#: so a record is forced part-way through a conversation that might have ended without ever
#: needing compaction. That is arithmetic about when the trigger fires, not evidence that firing
#: there hurt anything: no run has reported a cost for it, and no run has used the alternative.
#:
#: What *is* measured runs the other way. **The record degrades with the bulk it is asked to
#: read.** At 8,000-token tool results the record carried 53 of 53 facts; at 16,000 it carried
#: 46; at 25,200 it carried 18. The same shape appears in context: at a 120,000-token window the
#: record frays somewhere around 96,000 tokens of accumulated conversation. A trigger at 0.8 of
#: that window asks for the record at 94,400 tokens -- at the fraying point, with everything
#: gathered so far to summarise -- where 0.6 asks at 70,800, comfortably inside where records
#: were complete. A later trigger is a bigger ask and a worse record, and a worse record is the
#: failure this whole strategy exists to avoid.
#:
#: The break-even argument points the same way once it is read correctly. An edit repays itself
#: over the turns that *follow* it, so it wants as many of them as possible -- and firing later
#: leaves fewer of them, not more. Waiting does not make the compaction cheaper; it makes the
#: record worse and gives it less time to pay for itself.
DEFAULT_TRIGGER_FRACTION: Final[float] = 0.6

#: Fraction of the ceiling at which the strategy stops waiting for a record and compacts
#: without one.
#:
#: **0.9, and it was briefly 0.95.** It moves with the trigger, and back with it. A record
#: arrives one call late by construction: the middleware can only read the history on the way
#: *out* of a call and can only pin the *next* one, so the conversation grows by a whole turn
#: between the ask and the answer -- see :meth:`ToolResultRecallMiddleware.process`. The gap
#: between the two lines has to be wide enough for that turn to land in, and at a 0.6 trigger
#: it is three tenths of the ceiling, which is several turns rather than one.
#:
#: 0.95 was set to widen that gap under a 0.8 trigger, against a scenario -- the strategy
#: compacting without a record while the record is still in flight -- that has never been
#: observed: no measured run carries a ``FALLBACK`` flag at all.
#:
#: It cannot simply be raised to 1.0. Past this line the fallback still has to bring the
#: conversation under the ceiling, and a fallback given no headroom has nothing to work in.
DEFAULT_FALLBACK_FRACTION: Final[float] = 0.9

#: What the recall tool is for, as the model reads it.
#:
#: Deliberately not "identifiers and values": that phrasing was fitted to one benchmark's hex
#: codes and would drop prose, findings and conclusions from any real tool output, which is
#: most of what a real tool returns.
RECALL_DESCRIPTION: Final[str] = (
    "Record what must survive from earlier tool results, so it remains available after those "
    "results are removed from the conversation to save space."
)

#: What to put in the ``values`` argument, as the model reads it.
#:
#: The four instructions after the opening sentence partition the content, so that nothing in
#: a tool result falls outside all of them: values that cannot be reconstructed are quoted,
#: findings and conclusions are kept as stated, a summary the tool already wrote is carried
#: over rather than rewritten, and whatever remains is summarised. Removing one of the four
#: opens a gap that the model is then free to drop silently, which is the failure this whole
#: strategy exists to avoid.
RECALL_VALUES_DESCRIPTION: Final[str] = (
    "Everything from earlier tool results, grouped by the tool that produced it, so that "
    "nothing is lost without being noticed. Quote verbatim any value that cannot be "
    "reconstructed or guessed: identifiers, codes, names, numbers, paths, URLs, versions, "
    "states, timestamps. Keep findings and conclusions as they were stated, shortening only "
    "those long enough to need it. Carry over any summary a tool already produced as it "
    "stands, rather than rewriting it. Summarise the remaining content briefly, so its "
    "substance is still represented. Record only what the results actually contained, and "
    "where you must choose, keep exactness over brevity."
)


def find_record_index(messages: Sequence[Message]) -> int | None:
    """Return the index of the newest recall tool result, if one exists.

    Shared by the strategy and the middleware so the two halves cannot disagree about whether
    a record exists -- otherwise one would force a call that was already made, or drop results
    a record never covered.

    The call and its result are matched by ``call_id`` rather than by adjacency, because a
    provider is free to order or batch them differently. A result whose call is absent does not
    count: that is the shape a client-synthesised pair produces, and exactly what breaks on
    routes that track tool calls server-side.

    **Two forms are records.** The recall tool's result, as above, and a record this package
    wrote in place of several (:func:`build_record_message`), which is a message of its own with
    no call behind it and is recognised by how it opens -- see :func:`_is_written_record`. The
    index is then that message's.

    **An excluded record is not a record.** Nothing excluded one until the composed row began
    consolidating them -- see
    :meth:`ToolResultAnchoredSummarizationCompactionStrategy.consolidate_records` -- and the
    records it replaces stay in the stored conversation as excluded messages. Counting them
    would make "the newest record" depend on where the replacement was inserted rather than on
    what is being sent; on every conversation the standalone row produces, where no record is
    ever excluded, the answer is what it was.

    Args:
        messages: The conversation to search.

    Returns:
        The index of the result message, or None when no complete record exists.
    """
    recall_ids = {
        content.call_id
        for message in messages
        for content in message.contents
        if content.type == "function_call" and content.name == RECALL_TOOL_NAME and content.call_id
    }
    newest: int | None = None
    for index, message in enumerate(messages):
        if message.additional_properties.get(EXCLUDED_KEY, False):
            continue
        if _is_written_record(message):
            newest = index
            continue
        if not recall_ids:
            continue
        for content in message.contents:
            if content.type != "function_result" or content.call_id not in recall_ids:
                continue
            result = content.result if isinstance(content.result, str) else str(content.result)
            # The marker is what separates a record from a call the model made on its own
            # initiative. Without it an uninvited call would look like a record and the
            # strategy would drop results nothing had preserved.
            if RECORD_MARKER in result:
                newest = index
    return newest


def _is_written_record(message: Message) -> bool:
    """Return whether ``message`` is a record this package wrote, rather than one the model made.

    The form :func:`build_record_message` produces: an assistant message holding no function call,
    whose text opens with :data:`_WRITTEN_RECORD_PREFIX`. Whether it is excluded is the caller's
    question, as it is for a recall tool result. The role is tested first because it is free and
    rules out every user turn and tool result before any text is joined.

    Args:
        message: The message to inspect.

    Returns:
        True when the message is a written record.
    """
    return (
        message.role == "assistant"
        and not any(content.type == "function_call" for content in message.contents)
        and (message.text or "").startswith(_WRITTEN_RECORD_PREFIX)
    )


def record_text(message: Message) -> str:
    """Return the record text a message carries: a recall result, or a record this package wrote.

    A written record (:func:`_is_written_record`) is its text, whole. Otherwise only results
    bearing :data:`RECORD_MARKER` are read. A provider may batch several tool
    results into one message, and text from an unrelated result sitting beside the record would
    then count towards coverage without anyone having written it as a record -- which is
    precisely the mistake the coverage check exists to stop.

    An excluded message carries no record, for the reason :func:`find_record_index` gives: a
    record a consolidated one has replaced is not in the prompt, so it may neither license a
    deletion in ``_drop_before`` nor be counted and re-preserved by :func:`_preserve_records`.

    Args:
        message: The message at the anchor index.

    Returns:
        The record, or an empty string when the message carries none.
    """
    if message.additional_properties.get(EXCLUDED_KEY, False):
        return ""
    if _is_written_record(message):
        return message.text
    parts: list[str] = []
    for content in message.contents:
        if content.type != "function_result":
            continue
        result = content.result if isinstance(content.result, str) else str(content.result)
        if RECORD_MARKER in result:
            parts.append(result)
    return "\n".join(parts)


def _called_function_names(messages: Sequence[Message], group: dict[str, Any]) -> set[str]:
    """Return the distinct function names called inside one group's span.

    Args:
        messages: The conversation the span indexes into.
        group: One span from :func:`group_messages`.

    Returns:
        The names, empty when the span holds only results whose declaration sits elsewhere.
    """
    return {
        content.name
        for message in messages[group["start_index"] : group["end_index"] + 1]
        for content in message.contents
        if content.type == "function_call" and content.name
    }


def _group_result_text(messages: Sequence[Message], group: dict[str, Any]) -> str:
    """Return everything the tools in one group returned, concatenated.

    Only ``function_result`` contents are read. The call's arguments are excluded on purpose:
    the model wrote those, so they are reconstructable from the conversation and quoting them
    back proves nothing about whether the *result* survived.

    Args:
        messages: The conversation the span indexes into.
        group: One span from :func:`group_messages`.

    Returns:
        The results' text, empty when the span returned nothing.
    """
    parts: list[str] = []
    for message in messages[group["start_index"] : group["end_index"] + 1]:
        for content in message.contents:
            if content.type == "function_result":
                parts.append(
                    content.result if isinstance(content.result, str) else str(content.result)
                )
    return "\n".join(parts)


def _tokens(text: str) -> list[str]:
    """Split ``text`` into the words a record and a tool result can be compared through.

    **The rule.** Break on whitespace and on :data:`_SEPARATORS`; strip punctuation from both
    ends of each piece; keep only what follows the last remaining ``=``; lowercase it. Order is
    preserved and duplicates are kept, because the tool-name test counts mentions rather than
    merely looking for them.

    **Why ``=`` is not simply another separator.** ``key=value`` is the shape the values in a tool
    result actually arrive in, and the two halves are not equal. The key is the schema -- the tool
    wrote it, it repeats on every row, and it is reconstructable from the conversation -- while the
    value is the datum. Splitting symmetrically would put every key in the set a record has to quote
    from, so a record quoting every value and none of the labels would score half; keeping the
    compound is worse still: ``code_1=TL-BA44A9`` matches only a record that copied the tool's own
    label format. Reading the value side alone leaves one token per value, which is the thing the
    record is asked for. Punctuation is stripped before the split rather than after it, so base64
    padding is gone by the time the last ``=`` is looked for and such a value keeps its whole self;
    a value with an ``=`` genuinely inside it, such as a query string, is read from after that one
    and loses the part in front.

    **Shapes this handles**, all of which yield the bare value as a token of its own:
    ``key=value``, ``key: value``, ``key = value``, CSV and semicolon-delimited rows, JSON
    objects and arrays with quoted keys or values, bracketed and parenthesised lists, and any
    of those quoted.

    **Shapes this does not handle.**

    - *Unquoted, unspaced ``key:value``.* A colon is not a separator, because clock times and
      timestamps are built out of colons and splitting on them would destroy exactly the values
      the record is asked to quote. ``{"id":"AB-1"}`` is fine -- the quotes separate it -- and
      so is ``id: AB-1``; bare ``id:AB-1`` keeps the compound.
    - *Values containing a separator.* A quoted string with a comma inside it comes out as two
      tokens. The record is read with this same function, so a record quoting it verbatim comes
      out as the same two tokens and still matches; what is lost is the one-token form.
    - *Windows paths and escapes.* The backslash is not a separator, so a path like
      ``C:/logs/app2.log`` written with backslashes stays whole, and a JSON string with escaped
      quotes keeps its escapes.

    Args:
        text: The text to read.

    Returns:
        The tokens, lowercased, in order, duplicates included.
    """
    found: list[str] = []
    for piece in text.translate(_SEPARATOR_TABLE).split():
        token = piece.strip(string.punctuation)
        _, equals, value = token.rpartition("=")
        if equals:
            token = value
        if token:
            found.append(token.lower())
    return found


def _distinctive_tokens(text: str) -> set[str]:
    """Return the tokens in ``text`` that look like values nothing could reconstruct.

    **The rule.** Tokenise with :func:`_tokens`, then keep what is at least
    :data:`_MIN_DISTINCTIVE_LENGTH` characters long and contains at least one digit.

    It is deliberately generic and deliberately crude. The temptation is to match this
    benchmark's hex identifiers, and a rule fitted to those would be worthless on the next
    workload -- the same mistake the tool's own description already had to be rewritten out of
    (see :data:`RECALL_VALUES_DESCRIPTION`). A digit is the one signal shared by nearly
    everything :data:`RECALL_VALUES_DESCRIPTION` lists as unreconstructable: identifiers,
    codes, numbers, versions, timestamps, and most paths and URLs that matter.

    **What it cannot see, stated rather than discovered later.**

    - *Alphabetic values.* A name, a status word, a region, a UUID that happens to have no
      digits: none of these are found, so a group whose results hold only those yields nothing
      and falls through to the tool-name test instead. That is the whole reason the fallback in
      :meth:`ToolResultAnchoredSummarizationCompactionStrategy._drop_before` exists.
    - *Ordinary numbers.* Line numbers, counts, prices and dates are collected as though they
      were identifiers. That over-collects, which makes coverage harder to claim and keeps more
      -- the direction this package errs in everywhere.
    - *Its own leftovers.* A result already shortened by the anchored fallback carries that
      strategy's marker, whose character count is a digit-bearing token no record will ever
      quote. It costs the group one token's worth of coverage, in the keeping direction again.
    - *Whatever :func:`_tokens` cannot separate*, which that function lists.

    Args:
        text: Tool result text to read.

    Returns:
        The distinctive tokens, lowercased, without duplicates.
    """
    return {
        token
        for token in _tokens(text)
        if len(token) >= _MIN_DISTINCTIVE_LENGTH and any(character.isdigit() for character in token)
    }


def _is_recall_group(messages: Sequence[Message], group: dict[str, Any]) -> bool:
    """Return whether a group is itself a record rather than ordinary tool work.

    Shared by the strategy and the middleware for the same reason :func:`find_record_index` is:
    one half must not count a record as work still to be covered while the other treats it as
    the coverage. A record this package wrote is a record group too, though it holds no call: it
    is an assistant message, so the framework groups it as narration, and without this it would
    be neither a record to :func:`active_record_groups` nor anything the readers of tool groups
    skip by name.

    Args:
        messages: The conversation the span indexes into.
        group: One span from :func:`group_messages`.

    Returns:
        True when the span contains a call to the recall tool, or a record this package wrote.
    """
    if RECALL_TOOL_NAME in _called_function_names(messages, group):
        return True
    return any(
        _is_written_record(message)
        for message in messages[group["start_index"] : group["end_index"] + 1]
    )


def _preserve_records(messages: list[Message]) -> int:
    """Mark every recall record in the conversation as protected from removal, and count them.

    Re-applied on every pass rather than set once, because compaction runs against a freshly
    loaded conversation and the annotations a previous pass wrote are not there when the next
    one starts -- the same reason the framework re-derives its own exclusion flags each time.

    *Every* record, not only the newest. An older record is the sole account of the groups
    behind it, and :meth:`ToolResultAnchoredSummarizationCompactionStrategy._drop_before`
    already refuses to delete one; without this the fallback would shorten it instead, which
    loses the same facts more quietly.

    Both halves of the identity are required. The call must name the recall tool and the result
    must carry :data:`RECORD_MARKER`, matching :func:`find_record_index`, so that an uninvited
    call the gate refused -- which returns ordinary text and preserves nothing -- does not get
    itself protected as though it had recorded something. A record this package wrote carries
    both in one message (:func:`_is_written_record`).

    Args:
        messages: The conversation, whose messages are annotated in place.

    Returns:
        How many records the conversation carries. Counted here rather than by a second walk
        because this is already the one place that applies both halves of the identity, and a
        counter disagreeing with what is protected would report a floor the prompt does not
        actually have. The walk is :func:`active_record_groups`, so a record a consolidated
        one replaced is neither counted nor re-protected.
    """
    groups = active_record_groups(messages)
    for group in groups:
        for message in messages[group["start_index"] : group["end_index"] + 1]:
            set_preserved(message, preserved=True, reason=PRESERVE_REASON)
    return len(groups)


def active_record_groups(messages: list[Message]) -> list[dict[str, Any]]:
    """Return the spans of every record still being sent, oldest first.

    The identity :func:`_preserve_records` applies -- a call naming the recall tool, and a result
    carrying :data:`RECORD_MARKER`, or a record this package wrote -- over messages that are not
    excluded, because
    :func:`record_text` reads nothing off an excluded one. Public because the composed row reads
    it to decide whether there is more than one record to merge; the standalone row reads it
    only through :func:`_preserve_records`.

    Args:
        messages: The conversation, already grouped.

    Returns:
        One span from :func:`group_messages` per record.
    """
    return [
        group
        for group in group_messages(messages)
        if _is_recall_group(messages, group)
        and any(
            record_text(message)
            for message in messages[group["start_index"] : group["end_index"] + 1]
        )
    ]


def consolidatable_record_groups(messages: list[Message]) -> list[dict[str, Any]]:
    """Return the active records a consolidation may replace on this pass, oldest first.

    :func:`active_record_groups`, less any record whose call and result would not both keep the
    exclusion -- :func:`~._preserve.removable_whole`. On the live path that is the record whose
    result the current model call carried in: the record the model has just written, on the call
    right after the one that wrote it. Replacing it there would store its result as excluded and
    leave its call standing, which a provider refuses. It stays out of this pass's merge
    or rewrite and is in the next one's, once its result has been stored. It remains a record to
    everything else -- preserved, counted, and the anchor if it is the newest.

    Args:
        messages: The conversation, already grouped.

    Returns:
        The spans :meth:`ToolResultAnchoredSummarizationCompactionStrategy.consolidate_records`
        may be handed.
    """
    spans = group_messages(messages)
    return [
        group for group in active_record_groups(messages) if removable_whole(messages, spans, group)
    ]


def record_body(messages: list[Message], group: dict[str, Any]) -> str:
    """Return what the model wrote in one record, without the marker and the tool's preamble.

    What a summarizer is handed when records are merged or rewritten: the marker and the
    preamble are this module's framing rather than content, and sending them would invite a
    summary that repeats them or, worse, paraphrases the instruction they carry.

    Args:
        messages: The conversation the span indexes into.
        group: One span from :func:`active_record_groups`.

    Returns:
        The record's own text, several results in one span joined by newlines.
    """
    parts: list[str] = []
    for message in messages[group["start_index"] : group["end_index"] + 1]:
        text = record_text(message)
        for part in text.split(RECORD_MARKER)[1:]:
            parts.append(part.strip().removeprefix(_RECORD_PREAMBLE).strip())
    return "\n".join(part for part in parts if part)


def build_record_message(text: str) -> Message:
    """Return the message a written record is inserted as: an assistant turn, not a tool call.

    **Not a tool call, because a provider validates tool calls.** A recall call and its result under
    a client-minted call id, shaped as the recall tool shapes one, is refused by Foundry on the next
    request with ``400 invalid_payload``. A call the model made carries the provider's own identity
    for it; a fabricated one does not, so the module docstring's argument against synthesising the
    first record reaches every record. An ordinary message is what the user half already inserts for
    its summaries, and no provider validates one against its own history.

    **An assistant message rather than a user one.** A record stands for results of tool calls
    the model already made. Read in an assistant turn it is the model's own earlier statement;
    the same words in a user turn are the user speaking, and the preamble -- treat these values as
    authoritative -- would read as an instruction. The user half also reads user turns and
    nothing else: a record in one would sit in its band, and a record written after the newest
    real turn would take the live request's place in the tail it keeps verbatim. As an assistant
    message the record is outside that half altogether, and the wait's response clock, which
    counts assistant messages, counts it as it counted the synthesised call it replaces. The
    cached prefix is the same either way: the record goes where the newest record it replaces
    stood, and everything from there on is re-billed whatever its role. Neither role keeps
    strict alternation at that position. The message after a record is normally the model's own
    reply to the recall result, so this one can sit beside another assistant message; a user
    record would sit beside the user turn in front of it just as often. The clients this package
    runs against accept consecutive messages of one role; a provider that does not would need
    its client to merge them, and that is unmeasured.

    **Assistant prose is what the fallback sheds**, which the module docstring gives as the
    reason the first record is a tool result. That reason is about a record nothing protects.
    This one is preserved on the pass that inserts it, by the walk that protects every record
    (:func:`_preserve_records`), and the anchored fallback skips a preserved message on each of
    its removal paths, narration included.

    **The text is the recall tool's result, byte for byte**: the marker, the preamble, then the
    record. :func:`record_body` reads it the way it reads the tool's, and
    :func:`_is_written_record` recognises it by that opening. What it no longer carries is the
    second copy a recall call holds in its arguments, and that is the trap in the composed row's
    acceptance rule, "smaller than what it replaces": measured against a record the model made,
    call and all, a replacement in this form is about half the size whatever its text says, and
    the rule would pass a rewrite that had shortened nothing. So the rule is not measured against
    the messages being replaced. Both sides are measured in this form -- the candidate, and each
    replaced record's body rebuilt through this function -- so a replacement is kept only when
    its text is shorter than theirs.

    Args:
        text: The record's own text, without marker or preamble.

    Returns:
        The message, with no id: nothing reads one, and the framework assigns it on grouping.
    """
    return Message(role="assistant", contents=[f"{_WRITTEN_RECORD_PREFIX}\n{text}"])


def _claimed_elsewhere(messages: Sequence[Message]) -> bool:
    """Return whether something other than this strategy's own hold protects any of ``messages``.

    The hold this strategy puts on an uncovered group is the one preservation that must *not*
    take the group out of the running: it is there so the fallback cannot shorten the group
    while another record is asked for, and the record that then quotes the group's values has
    to be able to release it and drop it. The hold put on an unrecorded group before a
    post-record fallback, :data:`PRESERVE_REASON_UNRECORDED`, is the same kind of hold and is
    released the same way. Every other reason -- a record's own mark, or one another strategy
    left -- means the group is not this strategy's to decide about.

    Args:
        messages: One group's span.

    Returns:
        True when a member is preserved under any reason outside :data:`_OWN_HOLDS`.
    """
    return any(
        is_preserved(message)
        and message.additional_properties.get(PRESERVE_REASON_KEY) not in _OWN_HOLDS
        for message in messages
    )


def _hold_unrecorded(messages: list[Message]) -> int:
    """Hold every tool group no record covers out of the fallback's reach, and count the holds.

    Run immediately before the fallback that follows a record, on every pass that reaches it.
    Any tool group still in the prompt then is one no record covers: a covered group was
    excluded by ``_drop_before``, and a record is a record. The coverage check has already held
    the uncovered groups it weighed, under :data:`PRESERVE_REASON_UNCOVERED`; what it never
    weighs is every group after the newest record and the head, and those are what this marks.
    The fallback may then remove only what is not a tool group -- assistant narration, in the
    default -- and either that frees enough or the prompt stays over the ceiling. A shortened
    tool group is the one outcome ruled out, because it is the one nobody sees.

    A group something already protects is left under its mark: a layer-one or layer-two hold
    keeps the reason that says which layer holds it, and a record or another strategy's claim
    is not this strategy's to relabel. A group the fallback has already shed whole is skipped,
    since it is not in the prompt to protect.

    Args:
        messages: The conversation, whose messages are annotated in place.

    Returns:
        How many tool groups carry this hold after the call, whether put on now or on an
        earlier pass. Zero means the rule held nothing back this time.
    """
    held = 0
    for group in group_messages(messages):
        if group.get("kind") != "tool_call" or _is_recall_group(messages, group):
            continue
        members = messages[group["start_index"] : group["end_index"] + 1]
        if all(message.additional_properties.get(EXCLUDED_KEY, False) for message in members):
            continue
        if any_preserved(members):
            held += any(
                message.additional_properties.get(PRESERVE_REASON_KEY) == PRESERVE_REASON_UNRECORDED
                for message in members
            )
            continue
        for message in members:
            set_preserved(message, preserved=True, reason=PRESERVE_REASON_UNRECORDED)
        held += 1
    return held


@dataclass(slots=True)
class _Reforce:
    """One outstanding ask for another record, and what it is being judged against.

    ``targets`` are the uncovered groups the ask was made for, so the next record can be read
    for progress as "did any of these come back covered". ``records`` is how many records the
    conversation held when the ask was made, so that record's arrival can be told from its
    absence without trusting a message id. ``passes`` counts the passes since without one,
    against :data:`_REFORCE_ARRIVAL_PASSES`. ``taken`` says the middleware has consumed the ask,
    so a second call exit before the next pass -- a probe answered from a restored snapshot, for
    one -- cannot pin a second call for the same ask.
    """

    targets: frozenset[str]
    records: int
    passes: int = 0
    taken: bool = False


@dataclass(frozen=True, slots=True)
class RecordDecisions:
    """What the record strategy has decided so far, taken as a value so it can be put back.

    The strategy carries its decisions across calls on the instance, not in the conversation:
    which groups it found uncovered, which of those it has stopped asking for and preserves, and
    the ask for another record that is still outstanding. A harness that re-enters a
    conversation from a snapshot -- a probe asked several times from one state, a turn re-sent
    after a disconnect -- restores the conversation and must restore these with it, or the
    decision the first re-entry advanced governs every later one and the re-entries are not
    repeats of each other. ``reforce`` is copied, so a restored ask starts with the passes it
    had and not the passes a discarded re-entry added.
    """

    uncovered: frozenset[str]
    settled: frozenset[str]
    preserved: frozenset[str]
    reforce: _Reforce | None


def _droppable_groups_after(messages: list[Message], record_index: int | None) -> int:
    """Count the tool-call groups a record would be asked to cover.

    Args:
        messages: The conversation to measure.
        record_index: Index of the newest record, or None when there is none, in which case
            the count runs from the beginning of the conversation.

    Returns:
        How many non-recall tool-call groups sit after the record.
    """
    boundary = -1 if record_index is None else record_index
    count = 0
    for group in group_messages(messages):
        if group.get("kind") != "tool_call" or group["start_index"] <= boundary:
            continue
        # A record is not work that needs recording. Counting one would make every record
        # bring the next one closer, and a bound of one would force a record on every call.
        if _is_recall_group(messages, group):
            continue
        count += 1
    return count


class RecallGate:
    """One-shot permission for the recall tool.

    The tool cannot be hidden from the model. It has to be registered with the agent for the
    function-invocation layer to execute it, and that layer wraps the middleware layer, so a
    tool supplied per call reaches the model but never the executor: measured, the model's
    call simply went unanswered. A registered tool is advertised on every request, and this
    one was called uninvited on the unpinned follow-up call in every run.

    Permission is therefore separated from visibility. The middleware arms the gate
    immediately before the request it forces, and the tool records only while armed. An
    uninvited call still runs and still answers honestly; it just produces no record.
    """

    def __init__(self) -> None:
        """Start disarmed, so nothing is recorded until something asks for it."""
        self._armed = False

    def arm(self) -> None:
        """Permit the next call to record."""
        self._armed = True

    def take(self) -> bool:
        """Consume the permission.

        Returns:
            True if this call may record. One-shot: a single arming cannot licence a second
            record, which would drop results the first had already replaced.
        """
        armed, self._armed = self._armed, False
        return armed


def make_recall_tool(
    gate: RecallGate | None = None,
    *,
    target_tokens: int | None = DEFAULT_RECORD_TARGET_TOKENS,
) -> Callable[[str], str]:
    """Build the tool :class:`ToolResultAnchoredSummarizationCompactionStrategy` anchors on.

    It echoes what it is given straight back. That is the whole point: the value of the call
    is not what the tool computes but that the model's own recollection ends up in the
    transcript as a tool result, which the provider issued and which survives strategies that
    shed assistant prose.

    The docstring built here is the entire prompt for the record. The middleware sends no
    message -- one appended there would carry no history provider's source tag, so per-call
    persistence would store it and an instruction of ours would surface in the conversation
    the application replays to its user -- so the description, the ``values`` guidance and the
    stated target are the only three things steering what the model writes.

    Args:
        gate: Permission to record. Without one the tool always records, which is only right
            for a caller driving it deliberately.

    Keyword Args:
        target_tokens: Length to aim the record at, stated in the description. ``None`` states
            none. This is the only bound that makes the model *plan* to fit: the middleware's
            cap merely truncates, and truncating a tool call destroys the arguments JSON
            rather than shortening the record it carries.

    Returns:
        A callable named :data:`RECALL_TOOL_NAME`.
    """

    def tool(values: str) -> str:
        if gate is not None and not gate.take():
            return "Not required right now: nothing was recorded, and no results have been removed."
        return f"{RECORD_MARKER} {_RECORD_PREAMBLE}\n{values}"

    tool.__name__ = RECALL_TOOL_NAME
    sections = [RECALL_DESCRIPTION]
    if target_tokens is not None:
        # Spent where it is needed, rather than as a flat budget: the four clauses below are
        # not equally compressible, and a bare length would be read as licence to shorten the
        # verbatim half, which is the half that cannot be rewritten from anything else.
        sections.append(
            f"Aim for about {target_tokens:,} tokens in total, spent on what cannot be "
            "reconstructed rather than on the summaries."
        )
    sections.append(f"Args:\n    values: {RECALL_VALUES_DESCRIPTION}")
    # The whole docstring becomes the tool description: the framework builds the parameter
    # schema from the annotations and does not lift the Args section into it, so the guidance
    # for ``values`` only reaches the model as part of this text.
    tool.__doc__ = "\n\n".join(sections)
    return tool


class ToolResultAnchoredSummarizationCompactionStrategy:
    """Ask the agent to record the facts in a tool call, then drop what is behind it.

    Args:
        max_input_tokens: Ceiling the included prompt must stay under.
        tokenizer: Token counter, shared with whatever measures the result.

    Keyword Args:
        keep_head_groups: Groups at the start never touched, carrying the task and its
            requirements. Dropping these is what makes other strategies lose the labelling
            that gives surviving values their meaning.
        keep_tail_groups: Recent groups the fallback keeps verbatim. Read only by the default
            fallback built below; pass a ``fallback`` of your own and it is that strategy's
            business instead.
        trigger_fraction: Fraction of the ceiling at which the record is first requested.
            Below it nothing happens: a record that is not needed costs an agent turn and
            buys nothing. See :data:`DEFAULT_TRIGGER_FRACTION` for where the default sits.
        fallback_fraction: Fraction of the ceiling at which waiting stops and the conversation
            is compacted without a record. Must be greater than ``trigger_fraction``, and by
            enough for a record asked for at the trigger to arrive before this is crossed --
            it arrives one call late by construction. See :data:`DEFAULT_FALLBACK_FRACTION`.
        coverage_share: Share of a group's distinctive values the record must quote verbatim
            before that group may be deleted. Exposed because the right value depends on how
            many values a workload's results carry, which this module cannot know; see
            :data:`DEFAULT_COVERAGE_SHARE` for where the default sits and why. Zero means any
            group holding a distinctive value at all counts as covered, which restores the
            behaviour this check replaced and is there so the two can be run side by side.
    """

    def __init__(
        self,
        *,
        max_input_tokens: int,
        tokenizer: TokenizerProtocol,
        keep_head_groups: int = 3,
        keep_tail_groups: int = 4,
        trigger_fraction: float = DEFAULT_TRIGGER_FRACTION,
        fallback_fraction: float = DEFAULT_FALLBACK_FRACTION,
        coverage_share: float = DEFAULT_COVERAGE_SHARE,
        fallback: CompactionStrategy | None = None,
    ) -> None:
        """Validate and store the configuration.

        Raises:
            ValueError: If a bound is out of range, or the two thresholds are the wrong way
                around -- a fallback at or below the trigger would fire before the model had
                any chance to answer, and the recording step would never happen at all.
        """
        if max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive.")
        if not 0.0 < trigger_fraction <= 1.0:
            raise ValueError("trigger_fraction must be in (0.0, 1.0].")
        if not 0.0 < fallback_fraction <= 1.0:
            raise ValueError("fallback_fraction must be in (0.0, 1.0].")
        if fallback_fraction <= trigger_fraction:
            raise ValueError("fallback_fraction must be greater than trigger_fraction.")
        if keep_head_groups < 0 or keep_tail_groups < 0:
            raise ValueError("keep_head_groups and keep_tail_groups must be >= 0.")
        # Zero is admissible and 1.0 is admissible; a share above 1.0 is not, because no record
        # can quote more of a group's values than the group contains, so the strategy would
        # silently never delete anything and would read as a model that never complied.
        if not 0.0 <= coverage_share <= 1.0:
            raise ValueError("coverage_share must be in [0.0, 1.0].")
        self.max_input_tokens = max_input_tokens
        self.tokenizer = tokenizer
        self.keep_head_groups = keep_head_groups
        self.keep_tail_groups = keep_tail_groups
        self.trigger_fraction = trigger_fraction
        self.fallback_fraction = fallback_fraction
        self.coverage_share = coverage_share
        self.fallback = fallback or AnchoredCompactionStrategy(
            max_input_tokens=max_input_tokens,
            tokenizer=tokenizer,
            keep_head_groups=keep_head_groups,
            keep_tail_groups=keep_tail_groups,
        )
        self._records = 0
        self._records_in_conversation = 0
        self._fallbacks = 0
        self._fallbacks_after_record = 0
        self._fallbacks_held_after_record = 0
        # The group ids the most recent pass declined to drop, replaced rather than added to.
        # See :attr:`groups_kept_uncovered`.
        self._uncovered: set[str] = set()
        # The ask for another record that is outstanding, if one is. See :meth:`take_reforce`.
        self._reforce: _Reforce | None = None
        # Group ids whose asking has ended -- uncovered still, after a record that covered none
        # of them or after an ask that produced no record -- and which are preserved for good
        # while they stay uncovered. Accumulated, because an ended chain must not restart.
        self._settled: set[str] = set()
        # The uncovered groups the most recent pass preserved for good, rebuilt every pass like
        # ``_uncovered``. See :attr:`groups_preserved_uncovered`.
        self._preserved: set[str] = set()

    def decision_state(self) -> RecordDecisions:
        """Return the decisions carried on this instance, as a value independent of it."""
        return RecordDecisions(
            uncovered=frozenset(self._uncovered),
            settled=frozenset(self._settled),
            preserved=frozenset(self._preserved),
            reforce=None if self._reforce is None else replace(self._reforce),
        )

    def restore_decisions(self, decisions: RecordDecisions) -> None:
        """Put back decisions taken with :meth:`decision_state`, discarding those made since."""
        self._uncovered = set(decisions.uncovered)
        self._settled = set(decisions.settled)
        self._preserved = set(decisions.preserved)
        self._reforce = None if decisions.reforce is None else replace(decisions.reforce)

    @property
    def fallbacks_used(self) -> int:
        """Passes that gave up waiting for a record and truncated instead.

        Non-zero means no record ever arrived, and that row is measuring the fallback rather
        than this design. Reported rather than hidden: a strategy that quietly degrades into
        another one produces a number that belongs to neither.

        This path only. A record that did arrive and did not free enough is the same
        degradation reached by the other route, and is counted by
        :attr:`fallbacks_after_record` -- for a while it was counted nowhere, which is how a
        row measuring the fallback came to carry no flag at all.

        Counted on the decision rather than on its effect, which is where this differs from
        :attr:`fallbacks_after_record`. Reaching this line at all means the record never came
        and the strategy is no longer waiting for one, and that is true of the row whether or
        not the fallback then found anything to shed.
        """
        return self._fallbacks

    @property
    def fallbacks_after_record(self) -> int:
        """Passes that had a record, dropped what it covered, and fell back anyway.

        A different event from :attr:`fallbacks_used`, which counts the passes that gave up
        waiting for a record that never arrived. This counts the passes where one did arrive
        and did not free enough, so the fallback ran behind it and shortened whatever was
        still in the prompt -- which, since the coverage check went in, is exactly the groups
        the record failed to carry and this strategy had just declined to delete.

        Kept apart from ``fallbacks_used`` because the two ask for opposite responses: no
        record at all is a model that will not comply, while a record that did not free
        enough is a ceiling, a bound, or a record too partial to be worth its size. What the
        two mean for the *row* is the same, and is why this is reported at all: a non-zero
        value says part of what that row measured is the fallback strategy rather than this
        one. Nothing said so until it was counted -- a seed reporting four uncovered groups
        was measured losing the same facts as the control, three messages shorter and 16,617
        tokens lighter, which is shortening rather than deletion and had no flag anywhere.

        Counted per pass, like ``fallbacks_used`` and unlike :attr:`groups_kept_uncovered`:
        each pass shortens whatever is in the prompt at the time rather than taking a second
        look at material already accounted for, so two passes are two losses.

        **Effects, not attempts.** Only a fallback that reported having changed something is
        counted. A fallback with nothing left to shed returns False and touches nothing, and
        counting the call rather than its answer made ``RECFALLBACK:5`` mean anything between
        five losses and none -- an unreadable number on a flag whose entire purpose is to say
        that part of a row was measured by another strategy.

        **What it can take is now narrower than the paragraphs above describe.** Every tool
        group no record covers is held before this fallback runs -- see
        :attr:`fallbacks_held_after_record` -- so on the default fallback a counted pass is one
        that shed assistant narration, not one that shortened a tool result.
        """
        return self._fallbacks_after_record

    @property
    def fallbacks_held_after_record(self) -> int:
        """Passes whose post-record fallback ran with tool groups held out of its reach.

        The rule behind it: once a record exists, the fallback may not shorten or shed a tool
        group no record covers -- whether it sits in front of the record, where the coverage
        check already holds it, or after it, where nothing did. It was reached on a live seed.
        With four uncovered groups preserved in front of the record the prompt stayed near the
        ceiling, the fallback fired thirty-three times, and it shortened the one tool group
        after the record that sat inside its band until all eight of its codes were gone; the
        row finished 5,000 tokens under the limit, not ``DQ``, eight facts short, and reported
        nothing. :data:`PRESERVE_REASON_UNRECORDED` is the hold that closes it.

        Counts attempts, where :attr:`fallbacks_after_record` counts effects, because the two
        answer different questions. This one says the fallback was needed and was held back;
        that one says it then found something it was still allowed to take. Zero here with
        records in the conversation is a row whose fallback never had to act. Non-zero beside
        ``DQ`` is the rule standing between the fallback and a quiet loss, which is its intended
        reading; non-zero without ``DQ`` is the narration the fallback may still shed having
        been enough.

        Counted per pass, like ``fallbacks_after_record``, and only on a pass where the rule
        actually held a group -- which, since the fallback keeps a tail, is nearly every pass
        that reaches it.
        """
        return self._fallbacks_held_after_record

    @property
    def records_found(self) -> int:
        """Recall tool results seen in the history. Zero means the model never complied.

        Saturates at one: it answers whether the model ever complied, not how often. How many
        records a conversation ended up carrying is :attr:`records_in_conversation`, and the
        two are different questions now that the middleware may ask more than once.
        """
        return self._records

    @property
    def records_in_conversation(self) -> int:
        """Records the conversation carries, at the most this strategy has seen it hold.

        A count rather than the flag :attr:`records_found` is, because records accumulate and
        nothing removes them. Every record observed is preserved -- neither shortened nor
        dropped, by this strategy or by the fallback behind it -- so each one raises a floor
        under the prompt that no later pass can lower, and a row whose unshrinkable part has
        grown can be told apart from a row whose compaction simply stopped working.

        **Read it against the number of times a record was asked for**, which is
        :attr:`ToolResultRecallMiddleware.forced_calls`, and not against one. With
        ``repeat_records`` on, several records is the middleware asking several times, which is
        the design working rather than a symptom; with it off there should be a single ask and
        a single record. What no setting explains is more records than asks: this read 2 for a
        single trigger event until the middleware stopped re-deciding on the exit of a call it
        had itself pinned, and while it did, a description of one record as "the cost of the
        design" described a number no run had ever produced.

        Not consolidated by this strategy. Merging two records rewrites the evidence rather
        than the bulk -- an older record is the sole surviving account of the groups behind
        *it* -- so a partial merge loses facts with nothing left to trace them to, and on the
        standalone strategy the count is the whole of the warning. The composition merges them
        as a last resort once the prompt is over the ceiling with both of its halves spent: see
        :meth:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy.__call__`.
        A consolidated record is a record to this count -- the walk is
        :func:`active_record_groups` -- and the records it replaced are not; the maximum below
        then keeps the peak, so a composition reading ``RECORDS:3`` beside ``RECMERGE:1`` held
        three and merged them, rather than holding three at the end.

        A maximum over passes rather than a running total, for the reason
        :attr:`groups_kept_uncovered` is counted by group id: the same conversation is
        re-examined on every later compaction, so a per-pass tally would report one record as
        eighteen.
        """
        return self._records_in_conversation

    @property
    def groups_kept_uncovered(self) -> int:
        """Tool groups a record failed to carry, and which were therefore not dropped.

        Non-zero means the model wrote a partial record and this strategy declined to delete
        what that record does not account for. The row then costs more than a complete record
        would have cost and loses nothing, which is the trade the check makes deliberately;
        :meth:`_drop_before` says why the alternative was silent loss.

        **The state as it now stands, not a history of it.** The set is rebuilt by every pass
        that reads a record rather than added to, because the question this answers is how much
        of the conversation is sitting in the prompt uncovered *at the end* -- which is what the
        row's cost is made of. Accumulating instead reported groups a later record went on to
        cover and this strategy then deleted, so a run that recovered completely could still
        finish carrying ``UNCOVERED:4``: a flag saying "this row kept four groups it should not
        have needed to" against a row that kept none.

        A set of group ids rather than a tally, for the reason
        :attr:`ToolResultRecallMiddleware.records_volunteered` had to be fixed: the same group
        is re-examined on every later compaction, and counting each look would report one
        uncovered group as eighteen.

        Every group counted here is held out of the fallback's reach for as long as it is
        counted, so beside ``RECFALLBACK`` it does not mean the fallback may have shortened
        these groups. Which of the two layers is holding a group is
        :attr:`groups_preserved_uncovered`.
        """
        return len(self._uncovered)

    @property
    def groups_preserved_uncovered(self) -> int:
        """Uncovered tool groups preserved for good, as the conversation now stands: layer two.

        Non-zero says asking for another record stopped helping -- the record it brought
        covered none of these, or no record came -- and these groups are now held out of the
        fallback's reach for the rest of the run, counted against the ceiling in full. Read it
        beside ``UNCOVERED`` and ``REFORCED``: ``REFORCED`` without this is the re-force fixing
        the shortfall; ``REFORCED`` with this equal to ``UNCOVERED`` is the re-force failing
        and the preservation standing in; neither flag is a record that covered what it was
        asked to. An ``UNCOVERED`` count larger than this is the difference still waiting on an
        ask, which the run ended before it was answered.

        The state as it now stands rather than a history of it, for the reason
        :attr:`groups_kept_uncovered` is: a settled group a later record quotes is released and
        dropped, and a count that kept reporting it would say the row was carrying a floor it
        no longer carries.
        """
        return len(self._preserved)

    def take_reforce(self) -> bool:
        """Consume the outstanding ask for another record, if there is one.

        Layer one's channel between the two halves, and the mirror of :meth:`RecallGate.take`
        on the other side of the middleware. The strategy decides that a record is wanted, on
        the pass that found groups its standing record does not cover; the middleware, which
        holds no reference to the strategy and cannot read coverage, asks this on the exit of
        every call it did not pin -- see :meth:`ToolResultRecallMiddleware._record_due` -- and
        pins the next call when the answer is yes. One-shot for the reason the gate is: a call
        exit that finds the ask already taken must not pin a second call for it, and a probe
        answered from a restored snapshot is exactly such an exit.

        Returns:
            True once per ask, on the first call after the pass that made it.
        """
        request = self._reforce
        if request is None or request.taken:
            return False
        request.taken = True
        return True

    def record_pending(self, messages: list[Message]) -> bool:
        """Return whether this conversation has tool work a record is due for or already asked for.

        Read by the composed row, which holds its user half back while this is true; this
        strategy never calls it, and nothing here changes because it exists. It states no rule
        of its own. "Due" is what :meth:`ToolResultRecallMiddleware._record_due` counts as
        *pending* -- the non-recall tool groups after the newest record, or all of them before
        the first -- and "asked for" is an outstanding :meth:`take_reforce` ask. The middleware
        also wants the prompt over its trigger before a pending count asks for anything; that
        half is a size, and the caller holds the reading, so the caller applies it.

        The count errs where the middleware's does: it includes tool groups ``keep_head_groups``
        or ``keep_tail_groups`` protect, which a record would not free. A caller that waits on
        this may therefore wait for a record that drops nothing, and that is the direction to
        err in: the middleware asks for that record anyway, and a caller that bounds its wait
        loses at most the bound.

        Args:
            messages: The conversation, already grouped.

        Returns:
            True when a record is due or already asked for.
        """
        if self._reforce is not None:
            return True
        return _droppable_groups_after(messages, find_record_index(messages)) > 0

    async def __call__(self, messages: list[Message]) -> bool:
        """Request a record, or drop what an existing record covers.

        The size and the line this pass is judged by are read off the conversation in front of
        it, which is what a row does. :meth:`compact_against` is the same pass with that pair
        supplied by the caller, and is how
        :class:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` gives
        every half of one pass the same reading.

        Returns:
            True if the outgoing messages changed.
        """
        if not messages:
            return False
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        return await self.compact_against(
            messages,
            prompt_tokens=included_token_count(messages),
            trigger_tokens=int(self.max_input_tokens * self.trigger_fraction),
        )

    async def compact_against(
        self,
        messages: list[Message],
        *,
        prompt_tokens: int,
        trigger_tokens: int,
        fallback_after_record: bool = True,
    ) -> bool:
        """Run one pass, judged against a size and a line the caller read rather than this pass.

        The seam a composition needs and a row does not. What it moves is the *trigger* only:
        the give-up line below is still this strategy's own ``fallback_fraction`` of its own
        ceiling, because that line is not a question about when to start compacting but about
        how long a record may be waited for, and nothing a composition does changes how long
        that is. The ceiling the fallback is reached for is likewise read live, since it is a
        statement about whether the prompt now fits.

        Args:
            messages: The conversation, mutated in place. Already grouped and token-annotated
                by the caller.
            prompt_tokens: Included tokens the trigger and the give-up line are judged against.
            trigger_tokens: Included tokens the prompt must exceed for a pass to run.

        Keyword Args:
            fallback_after_record: Run :meth:`fall_back_after_record` here when a record has
                not freed enough. True, the default, is what this strategy's own row does and
                what :meth:`__call__` passes. The composed row passes False and calls that
                method itself, last, after everything else it can try: there the fallback is the
                end of a chain rather than the step straight after the record. The give-up path
                taken when no record ever arrived is not governed by this and runs here either
                way.

        Returns:
            True if the outgoing messages changed.
        """
        if prompt_tokens <= trigger_tokens:
            return False

        anchor = find_record_index(messages)
        if anchor is not None:
            self._records = max(self._records, 1)
            # Before anything is deleted on the strength of a record, the record is put out of
            # reach of the fallback that runs below. Not folded into ``changed``: annotating a
            # message is not a change to the conversation the model sees, and reporting one
            # would make a pass that did nothing else look as though it had compacted.
            #
            # The count comes back from the same walk. Taken as a maximum because this runs on
            # every later pass over the same conversation; see ``records_in_conversation``.
            records = _preserve_records(messages)
            self._records_in_conversation = max(self._records_in_conversation, records)
            changed = self._drop_before(messages, anchor)
            # Layer one and layer two, in that order, over what ``_drop_before`` has just found
            # uncovered -- and before the fallback below, which is what both exist to keep away
            # from those groups.
            self._reforce_or_settle(records)
            # Even a good record may not be enough on its own: the groups after it are
            # untouched by design, and they can exceed the ceiling by themselves.
            if fallback_after_record and included_token_count(messages) > self.max_input_tokens:
                changed = await self.fall_back_after_record(messages) or changed
            return changed

        if prompt_tokens < int(self.max_input_tokens * self.fallback_fraction):
            # Still waiting for the middleware's forced call to come back. Nothing may be
            # dropped yet: the record is the only thing that would replace it.
            return False

        # Out of room to keep waiting. A model that has not answered by now may never answer,
        # and the alternative to compacting without a record is a provider error. The tool
        # results are lost either way at this point; at least the conversation survives.
        self._fallbacks += 1
        return await self.fallback(messages)

    async def fall_back_after_record(
        self, messages: list[Message], *, ceiling: int | None = None
    ) -> bool:
        """Hand what a record did not free to the fallback, with every unrecorded tool group held.

        The step :meth:`compact_against` takes when the prompt is still over the ceiling behind a
        record, and a method of its own so the composed row can take it at the end of its chain
        instead of straight after the record; the rule and the counters are the same wherever
        it is called from.

        Counted, and counted apart from the give-up fallback. This is the other strategy running
        over what the record did not free -- which includes every group the record did not
        carry -- so the row is partly measuring that other strategy. Until this counter existed
        only the pre-record path incremented anything, so a run that fell back here reported no
        ``FALLBACK`` at all and read as this design working.

        Counted after the await and on its answer. Incrementing before it counted the attempt,
        and a fallback with nothing left to shorten returns False and touches nothing:
        ``RECFALLBACK:5`` could be five no-ops, which is the opposite of what the flag is read as
        meaning.

        Every tool group no record covers is held first, so what the fallback may take here is
        narration and nothing else. Its False then means "held back", never "it fits": the prompt
        is left as it is, over the ceiling, and the row reads ``DQ`` rather than a shortened
        result. See :attr:`fallbacks_held_after_record`.

        ``ceiling`` is the composed row's: its chain compacts to a target below the input budget
        rather than to the budget, and hands the target down here. It reaches a fallback that is
        an :class:`~._anchored.AnchoredCompactionStrategy` -- the one this strategy builds, or
        one its caller builds for it -- through
        :meth:`~._anchored.AnchoredCompactionStrategy.compact_to`; any other fallback is run as
        it is and compacts to its own ceiling. None, the default, is the fallback's own ceiling,
        which is every caller but that chain.

        Args:
            messages: The conversation, mutated in place, with a record in it.

        Keyword Args:
            ceiling: Included tokens to shed down to, or None for the fallback's own ceiling.

        Returns:
            True if the fallback changed the outgoing messages.
        """
        if _hold_unrecorded(messages):
            self._fallbacks_held_after_record += 1
        if ceiling is not None and isinstance(self.fallback, AnchoredCompactionStrategy):
            shortened = await self.fallback.compact_to(messages, ceiling=ceiling)
        else:
            shortened = await self.fallback(messages)
        if shortened:
            self._fallbacks_after_record += 1
        return shortened

    def shed_again_after_record(
        self, messages: list[Message], message_ids: Collection[str]
    ) -> bool:
        """Take off ``messages`` the groups an earlier fallback behind a record shed, by message id.

        The composed row's seam for keeping its chain's fallback consistent across the live
        path's two lists; see :meth:`~._anchored.AnchoredCompactionStrategy.shed_again`, which
        this hands to. Nothing is held first, as :meth:`fall_back_after_record` holds every
        unrecorded tool group: the ids handed in are of what that fallback shed with the holds
        in place, which is narration, and a group is shed here only if all of its messages are
        among them. Not counted as a fallback: nothing is decided here, only repeated. A
        fallback that is not an :class:`~._anchored.AnchoredCompactionStrategy` sheds nothing
        this way.

        Args:
            messages: The conversation, mutated in place.
            message_ids: The ids of the messages the earlier fallback shed.

        Returns:
            True if any group was shed.
        """
        if not message_ids or not isinstance(self.fallback, AnchoredCompactionStrategy):
            return False
        return self.fallback.shed_again(messages, message_ids)

    def consolidate_records(
        self, messages: list[Message], groups: list[dict[str, Any]], text: str
    ) -> None:
        """Put one record carrying ``text`` in place of the records ``groups`` span.

        The composed row's seam for merging records and for rewriting one shorter; this
        strategy never calls it itself. The replaced records are released from their
        preservation and excluded with :data:`CONSOLIDATE_EXCLUDE_REASON`, call and result
        together so no call is left without its answer, and the replacement is inserted
        directly behind the newest of them. That position is what makes it a record to
        everything downstream without a second rule anywhere: it is the newest record, so
        :func:`find_record_index` and the recall middleware anchor on it and count pending tool
        work from it exactly as they did from the newest record it replaced; every group the
        replaced records stood in front of is in front of it, so ``_drop_before`` reads the same
        conversation against its text alone; it is not a tool group, so :func:`_hold_unrecorded`
        and :func:`_droppable_groups_after` never count it; and it is a record group
        (:func:`_is_recall_group`), so :func:`_preserve_records` protects and counts it on the
        spot and a later merge or rewrite can replace it in turn.

        **The replacement is an ordinary assistant message, not a recall call.** A call the model
        never made is one a provider may refuse, and Foundry does; see
        :func:`build_record_message` for that, and for why the role is the assistant's.

        An outstanding ask for another record is re-based as well. It judges arrival as a record
        count that grew, and a merge lowers the count; left alone, the record the ask was for
        would arrive into a count no higher than the one it was made at, read as never having
        come, and settle its groups for good.

        Args:
            messages: The conversation, mutated in place. Already grouped.
            groups: The records being replaced, from :func:`consolidatable_record_groups`, oldest
                first.
            text: The replacement's own text, without marker or preamble.

        Raises:
            ValueError: When a group is not :func:`~._preserve.removable_whole`: excluding it would
                leave a call standing without its result, or the reverse, which the provider
                refuses. Refused here as well as by the walk that picks the groups, so no caller
                can reach the half-removal by handing over a list of its own.
        """
        spans = group_messages(messages)
        if not all(removable_whole(messages, spans, group) for group in groups):
            raise ValueError(
                "A record handed to consolidate_records cannot be removed with its call and result."
            )
        for group in groups:
            for message in messages[group["start_index"] : group["end_index"] + 1]:
                # Released before the exclusion, as ``_drop_before`` releases a hold: the promise
                # the mark made passes to the replacement, and "preserved" and "included" keep
                # meaning one thing on a conversation read back.
                set_preserved(message, preserved=False)
                set_excluded(message, excluded=True, reason=CONSOLIDATE_EXCLUDE_REASON)
        insertion_index = int(groups[-1]["end_index"]) + 1
        messages.insert(insertion_index, build_record_message(text))
        annotate_message_groups(messages, from_index=insertion_index)
        annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=insertion_index)
        records = _preserve_records(messages)
        self._records_in_conversation = max(self._records_in_conversation, records)
        if self._reforce is not None:
            self._reforce.records = max(self._reforce.records - (len(groups) - 1), 0)

    def _drop_before(self, messages: list[Message], anchor: int) -> bool:
        """Exclude the tool groups the record demonstrably covers, and only those.

        Excluding every tool group ending before the record assumes the record replaced them,
        and that does not hold on every model: one writes a record covering two of six tool
        groups; another covers all of them. On the first, four groups would go behind a record
        that never mentioned them and nothing would say so, so the loss would arrive in the
        scores as compaction damage rather than as an instrument that stopped early. Raising the
        response cap, raising the stated target and rewriting the prompt each leave coverage
        where it was, so the rule has to be here, in what the strategy is willing to delete.

        **Coverage is checked in values, because models do not write tool names.** The first
        version of this check asked whether the record contained the group's function name, on
        the reading that :data:`RECALL_VALUES_DESCRIPTION` asks for the results "grouped by the
        tool that produced it". Measured on both models, that is not the clause they comply
        with. Luna's record reads *"extra0 deployment lookup returned codes: AB-123456, ..."*
        and never writes ``lookup_extra0`` at all; gpt-5.4-mini, whose records carry every value
        from every group, was scored ``UNCOVERED:4`` by the name rule and its compaction fell
        from a 20% reduction to 5-6% in exchange for nothing. A check that penalises the model
        that complied is a net negative, and this one was.

        So the test is the clause the description actually leads with -- "Quote verbatim any
        value that cannot be reconstructed or guessed" -- applied to what the group's tool
        *results* contained. :func:`_distinctive_tokens` finds those values and states the rule
        and its blind spots; a group is covered when the record quotes at least
        ``coverage_share`` of them, case-insensitively. That also dissolves the repeated-name
        ambiguity the count rule below was built for: two calls to one tool return two different
        sets of values, and a record quoting both has demonstrably accounted for both.

        **Both sides are tokenised, and the comparison is membership rather than substring.**
        Asking whether a value occurs anywhere in the record's text is not a test of whether the
        record carries it: a group holding ``2026`` and ``1234`` was "covered" by a record
        reading *"ZZ-999999 was recorded on 2034-05-12 as AB-123456"*, in which neither value
        appears as a value at all, and the group was then deleted. Any short number is a
        substring of some longer identifier or date, so the check built to stop silent deletion
        was itself licensing it. The record is therefore read with the same tokeniser as the
        results and a value counts only when it is one of the record's own tokens. The tool-name
        test below counts whole tokens for the same reason: ``record.count("get")`` is satisfied
        by ``get_status``, and prefix-sharing names like ``read_file`` and ``read_file_lines``
        are the normal case rather than a contrived one.

        **A group with no distinctive values falls back to the tool-name test.** By this rule
        nothing in such a group is unreconstructable, so the value check has no evidence either
        way -- and the two available shortcuts are both wrong. Calling it covered would let a
        record that mentions nothing delete a group of prose findings, which is the silent loss
        this whole method exists to stop. Calling it uncovered would make every prose-only tool
        permanently undroppable, which is not a conservative choice but a broken one. The name
        rule is a weaker instrument, and a weaker instrument is the right answer where the
        stronger one has nothing to read.

        **Repeated calls to the same tool cannot be told apart by name.** Six calls to
        ``lookup_eu`` produce six groups and one name, and a record grouped by tool mentions
        that name once, so the name alone cannot say which of the six it accounted for. The
        count rule therefore demands as many mentions as there are groups and keeps all of them
        when it does not get them. The demand is counted over every candidate, including those
        the value rule will settle, so a value-covered group raises the bar for a name-checked
        sibling sharing its tool. That over-demands, and it over-demands in the keeping
        direction: the failure it buys is "compacted less than hoped", which shows up as cost
        on a row anyone can read, against "lost facts silently", which shows up as a wrong
        answer with no trace of where the fact went.

        **Every record at or before the anchor is read, not only the newest.** They are all
        preserved -- :func:`_preserve_records` protects each one, and the loop below refuses to
        delete any of them -- so every one of them is still in the prompt and still answering
        for what it carries. Reading only the newest made coverage depend on which record
        happened to be last: a model that writes *"already recorded above"* leaves every group
        reading as uncovered while a complete account of them sits preserved one message
        earlier. The union is what the conversation actually still holds, so it is what the
        deletion is licensed against.

        **An uncovered group is held, and a covered group that was held is released.** Keeping a
        group is not enough on its own: the fallback that runs when the prompt is still over the
        ceiling shortens and sheds ordinary tool groups, and an unmarked uncovered group is one.
        So every group this method declines to delete is marked with
        :data:`PRESERVE_REASON_UNCOVERED` on the same pass, before the fallback can see it, and a
        group carrying that mark is still a candidate here -- :func:`_claimed_elsewhere` is what
        skips a group, and it skips only marks that are not this one -- so the record asked for
        on its behalf can cover it, at which point the mark comes off and the group is dropped
        like any other. What becomes of a group no record ever covers is
        :meth:`_reforce_or_settle`'s question, and it is answered after this method returns.
        A group held under :data:`PRESERVE_REASON_UNRECORDED` -- one that sat after the record
        when a post-record fallback ran -- is read here the same way once a newer record puts it
        in front: covered, it is released and dropped; not, it is re-held as uncovered and
        joins the ask.

        Returns:
            True if anything was excluded.
        """
        groups = group_messages(messages)
        record = "\n".join(
            text for message in messages[: anchor + 1] if (text := record_text(message))
        )
        # Tokenised once, and kept in both shapes the two rules need: a set for "is this value
        # in the record", a count for "is this name mentioned as often as it was called".
        record_tokens = _tokens(record)
        quoted = set(record_tokens)
        mentions = Counter(record_tokens)

        candidates: list[tuple[dict[str, Any], set[str], set[str]]] = []
        for position, group in enumerate(groups):
            if position < self.keep_head_groups or group.get("kind") != "tool_call":
                continue
            if group["end_index"] >= anchor:
                continue
            if _is_recall_group(messages, group):
                # An older record. Deleting it would destroy the only surviving account of the
                # groups behind *it* -- the same loss this strategy exists to prevent, one
                # level removed, and quieter, because the newer record looks like coverage.
                continue
            if not removable_whole(messages, groups, group):
                # Its call and its result would not leave together -- see
                # :func:`~._preserve.removable_whole` -- so it is not a candidate on this pass, and
                # not reported as uncovered either: the record may cover it perfectly well.
                continue
            if _claimed_elsewhere(messages[group["start_index"] : group["end_index"] + 1]):
                # Something else has already declared this group irreplaceable. Not counted as
                # uncovered: it was never a candidate for deletion, so reporting it would put a
                # protected message in a diagnostic that means "the record fell short". A hold
                # of this strategy's own is not that: the group is a candidate still, so the
                # record it was held for can release it -- see ``PRESERVE_REASON_UNCOVERED``.
                continue
            candidates.append(
                (
                    group,
                    _called_function_names(messages, group),
                    _distinctive_tokens(_group_result_text(messages, group)),
                )
            )

        # How many groups each name has to account for, counted over the droppable candidates
        # alone. A group the head protects, or one sitting after the record, is not being
        # replaced by this record and must not raise the bar for the groups that are.
        demand: dict[str, int] = {}
        for _, names, _ in candidates:
            for name in names:
                demand[name] = demand.get(name, 0) + 1
        named = {name for name, needed in demand.items() if mentions[name.lower()] >= needed}

        changed = False
        # Rebuilt every pass rather than accumulated, so the count answers "how much is the
        # record still failing to carry" rather than "how much has it ever failed to carry".
        # See ``groups_kept_uncovered``.
        uncovered: set[str] = set()
        for group, names, values in candidates:
            members = messages[group["start_index"] : group["end_index"] + 1]
            if not self._is_covered(quoted, names=names, values=values, named=named):
                uncovered.add(str(group["group_id"]))
                # Held from the pass that finds it uncovered, not from the pass that gives up
                # on it: the ask for another record is made now and answered two passes later,
                # and the fallback can run in between. A group shortened while its record is
                # in flight is a group that record can no longer quote. Re-applied every pass,
                # as the record's own mark is, because annotations do not survive a reload. Not
                # folded into ``changed``, for the reason the record's mark is not.
                for message in members:
                    set_preserved(message, preserved=True, reason=PRESERVE_REASON_UNCOVERED)
                continue
            for message in members:
                # A hold of ours, if any -- anything else's was skipped above. Released before
                # the exclusion, so a covered group leaves no protection behind it for another
                # strategy to trip over.
                if is_preserved(message):
                    set_preserved(message, preserved=False)
                changed = (
                    set_excluded(message, excluded=True, reason="tool_summary_anchored") or changed
                )
        self._uncovered = uncovered
        return changed

    def _reforce_or_settle(self, records: int) -> None:
        """Advance the ask for another record, and settle what asking has stopped helping.

        Layer one and layer two of the uncovered-group defence, in that order, run once per
        pass over what :meth:`_drop_before` has just found uncovered.

        **Layer one asks while asking works.** An uncovered group that no ask is outstanding
        for gets one: the middleware takes it through :meth:`take_reforce`, pins the next call,
        and the record that call writes is read on the pass after -- :data:`_REFORCE_ARRIVAL_PASSES`
        says why that is the second pass and not the third. The ask is judged on its targets:
        if any of them came back covered the record helped, and whatever is still uncovered is
        asked for again; if none did, or no record came at all, the targets still uncovered are
        settled.

        **The bound is progress, and it is a bound because groups are finite.** Every ask that
        continues the chain has removed at least one group from it for good -- a covered group
        is dropped, not re-examined -- and every ask that does not ends it, settling every
        target it had. So each ask retires at least one group from ever being a target again,
        the asks made over a run number at most the tool groups ever found uncovered, and a
        shortfall a later record opens on new material gets its own chain on the same terms. A
        fixed count of asks is wrong in both directions. One further ask is measured to be
        enough on a model whose record covers two of six groups and whose second covers the
        rest, and a chain that stops on the first ask that helps nothing costs that model
        nothing. Any larger constant is a constant number of pinned calls spent on a model that
        has already shown it will not cover them: three records, and five to six groups still
        uncovered.

        **Layer two settles, and settled means preserved for good.** A settled group keeps the
        hold layer one put on it for as long as it stays uncovered, and nothing here lifts it:
        not the fallback, which honours the mark, and not a further ask, because the chain has
        ended. The one thing that lifts it is a later record quoting its values, which is a
        deletion the record licenses and not an escape. The prompt may then sit over the
        ceiling with nothing left the fallback may take; the fallback stops there rather than
        looping, the caller sends the prompt as it stands, and the row reads ``DQ``. That is the
        accepted consequence, and it is not softened here.

        Args:
            records: How many records the conversation holds on this pass, from
                :func:`_preserve_records`, so an arrival is a count that grew.
        """
        uncovered = self._uncovered
        request = self._reforce
        if request is not None:
            if records > request.records:
                # The record came. Progress is a target that is no longer uncovered; a record
                # that covered none of them is the model declining, and the chain ends there.
                if not request.targets - uncovered:
                    self._settled |= request.targets & uncovered
                self._reforce = None
            else:
                request.passes += 1
                if request.passes >= _REFORCE_ARRIVAL_PASSES:
                    # Nothing came by the pass that could have seen it: the model ignored the
                    # pin, the call was cut, or the option was refused. Ended rather than
                    # retried, because the bound has to hold against a model that declines
                    # every time.
                    self._settled |= request.targets & uncovered
                    self._reforce = None
        if self._reforce is None:
            wanted = uncovered - self._settled
            if wanted:
                self._reforce = _Reforce(targets=frozenset(wanted), records=records)
        self._preserved = uncovered & self._settled

    def _is_covered(
        self, quoted: set[str], *, names: set[str], values: set[str], named: set[str]
    ) -> bool:
        """Return whether one group's contents demonstrably survive in the record.

        Args:
            quoted: Every token the record contains, lowercased.

        Keyword Args:
            names: The functions called inside the group.
            values: The distinctive tokens its results contained, lowercased.
            named: Function names the record mentions as often as they are called.

        Returns:
            True when the group may be deleted.
        """
        if values:
            # ``ceil`` rather than rounding, so the constant is a genuine floor on the share:
            # seven of eight values clears 0.8 and six does not. It also makes 1.0 mean every
            # value and 0.0 mean none, which is what those two ends have to mean for the
            # keyword to be usable as a dial across its whole range.
            return len(values & quoted) >= ceil(len(values) * self.coverage_share)
        # No names either means a span of results whose declaration sits outside it, so there
        # is nothing at all to check the record against. Kept, on the same principle as
        # everything else here.
        return bool(names) and names.issubset(named)

    def _excluded(self, messages: list[Message]) -> int:
        """Return how many messages are currently excluded, for tests and diagnostics."""
        return sum(
            1 for message in messages if message.additional_properties.get(EXCLUDED_KEY, False)
        )


class ToolResultRecallMiddleware(ChatMiddleware):
    """Force the recall call once, so phase 2 has something to anchor on.

    Args:
        max_input_tokens: Ceiling the prompt must stay under, matching the strategy's.
        tokenizer: Token counter, matching the strategy's.

    Keyword Args:
        arm: Called immediately before the forced request, arming the recall tool for one
            call. The tool is inert otherwise, which is what stops the model producing a
            record on its own initiative -- it cannot be hidden, only disabled.
        trigger_fraction: Fraction of the ceiling at which the record is forced. Comfortably
            below the strategy's fallback threshold, because the decision is made one call
            late -- see :meth:`process`. Defaults to :data:`DEFAULT_TRIGGER_FRACTION`, the
            same constant the strategy defaults to, so the two halves cannot silently disagree
            about when a record is wanted.
        repeat_records: Let the size trigger ask again once there is new tool work to record.
            **On by default.** Off, the strategy compacts exactly once: the first record is written
            when the prompt passes the trigger, and every tool result after it is never recorded
            and, since it is held from the fallback, never shortened, so a conversation that keeps
            going grows until it passes the window -- at three times the window it overflows on
            every seed of every model measured. Off is right only for a conversation that ends soon
            after it first outgrows its window, which is not a default a framework can assume.

            What on costs where one record is already complete: a second one is duplication,
            and duplication here is preserved, unshrinkable prompt: on a model whose records
            carry every value from every group, repeats measure as a small negative shrink.
            Pass ``False`` for such a model and a bounded conversation. Records still
            accumulate with this on and nothing here merges them; the composition's last-resort
            chain is what does.

            Repeating cannot be done by reading size alone: the size that fired the trigger
            does not go away when a record arrives,
            because the record is added to the conversation rather than subtracted from it. A
            trigger reading size alone would therefore pin every remaining call in the run.
            What re-arms it is new *material* -- see :meth:`_record_due` for the rule, which is
            stated there once and nowhere else.

            This governs the size trigger only. ``max_groups_before_record`` is a caller
            asking for repeats outright, so it keeps forcing them whatever this says.
        record_max_tokens: Cap put on the forced call's response, and on no other call.
            ``None`` leaves whatever cap the run already sets, so the record inherits the cap
            sized for an ordinary answer and a record asked to summarise everything has no
            bound of its own at all.
        max_groups_before_record: How many tool-call groups one record may be asked to cover
            before another is forced. ``None`` switches this bound off and leaves the size
            trigger as the only thing that asks. It is a second trigger beside
            ``trigger_fraction`` rather than a replacement for it: whichever fires first
            forces the call. It is a bound, not a policy: whether records repeat at all is
            ``repeat_records``.

            It exists because coverage does not scale with how much there is to cover.
            Measured: gpt-5.6-luna's record covered two of six tool groups, and raising the
            response cap, raising the stated target and rewriting the prompt each left that
            unchanged. What was still within reach was asking each record for less, which is
            what this bounds. The strategy will now keep whatever a record does not cover, so
            an unbounded ask degrades into compacting almost nothing rather than into losing
            facts -- this is the parameter that buys the compaction back.

            **The count is an approximation, and the direction it errs in is chosen.** This
            middleware holds no reference to the strategy, so it cannot read
            ``keep_head_groups`` and cannot tell a group the strategy protects from one it
            would drop. It counts every non-recall tool-call group after the newest record,
            which over-counts by at most the number of tool groups inside the head -- usually
            none, since the head carries the task rather than tool work. Over-counting forces
            a record slightly early and costs an agent turn; under-counting would let a record
            be asked to cover more than the model will, which is the thing this prevents.
        reforce: Asked on the exit of every call this middleware did not pin, and consumed by
            the asking. True means the strategy has found tool groups its standing record does
            not cover and wants another record for them before its fallback runs, and this
            middleware then pins the next call whatever the other rules say. Wired to
            :meth:`ToolResultAnchoredSummarizationCompactionStrategy.take_reforce`, and the
            bound on how often it can say yes lives on that side; ``None`` leaves the strategy
            to preserve those groups without asking. Independent of ``repeat_records`` on
            purpose: that flag is off because a repeat on a complete record can only cost, and
            this asks only on a measured shortfall, so it cannot fire on the case the flag
            protects. :attr:`reforced_calls` counts the calls it pinned.
    """

    def __init__(
        self,
        *,
        max_input_tokens: int,
        tokenizer: TokenizerProtocol,
        arm: Callable[[], None],
        trigger_fraction: float = DEFAULT_TRIGGER_FRACTION,
        record_max_tokens: int | None = DEFAULT_RECORD_MAX_TOKENS,
        max_groups_before_record: int | None = None,
        repeat_records: bool = True,
        reforce: Callable[[], bool] | None = None,
    ) -> None:
        """Validate and store the configuration.

        Raises:
            ValueError: If a bound is out of range.
        """
        if max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive.")
        if not 0.0 < trigger_fraction <= 1.0:
            raise ValueError("trigger_fraction must be in (0.0, 1.0].")
        if record_max_tokens is not None and record_max_tokens <= 0:
            raise ValueError(
                "record_max_tokens must be positive, or None to leave the run's cap in place."
            )
        if max_groups_before_record is not None and max_groups_before_record <= 0:
            raise ValueError(
                "max_groups_before_record must be positive, or None to leave the bound off. "
                "Zero groups per record is a record forced on every call, which is not a bound."
            )
        self.max_input_tokens = max_input_tokens
        self.tokenizer = tokenizer
        self.arm = arm
        self.trigger_fraction = trigger_fraction
        self.record_max_tokens = record_max_tokens
        self.max_groups_before_record = max_groups_before_record
        self.repeat_records = repeat_records
        self.reforce = reforce
        self._force_next = False
        self._reforce_next = False
        self._forced = 0
        self._reforced = 0
        self._records_forced = 0
        self._records_volunteered = 0
        self._records_truncated = 0
        self._seen_record = False
        self._awaiting_record = False

    def forget_pending(self) -> None:
        """Drop the decision to force a record on the next call.

        The decision is taken on one call and applied to the next, which makes it part of the
        conversation rather than of this object: a decision taken while the conversation was
        being seeded would fire on the first question asked of the snapshot and on none of the
        others, so that one probe would carry a prompt the rest do not. Restoring the snapshot
        has to restore this too.

        The outstanding ask goes with it, and for the same reason. A restore rewinds the
        conversation past the forced call, so the record that call was writing is not in the
        state being restored to; leaving the middleware waiting for it would suppress the next
        ask on the evidence of a turn that no longer exists.
        """
        self._force_next = False
        self._reforce_next = False
        self._awaiting_record = False

    @property
    def forced_calls(self) -> int:
        """How many times the recall tool was forced. Zero means phase 1 never fired."""
        return self._forced

    @property
    def reforced_calls(self) -> int:
        """Forced calls made at the strategy's request, for groups its standing record missed.

        A subset of :attr:`forced_calls`, and layer one's count: each is one pinned call spent
        asking for a record aimed at the groups the last one failed to cover. Zero on a run
        whose records were complete, which is what makes the flag readable -- it appears only
        where a shortfall was measured. Counted when the call is pinned rather than when the
        ask is taken, so an ask a restored snapshot discarded is not reported as a call made.
        """
        return self._reforced

    @property
    def records_forced(self) -> int:
        """Records that appeared on a call this middleware pinned."""
        return self._records_forced

    @property
    def records_volunteered(self) -> int:
        """Records the model produced without being pinned.

        Not a success. A turn's pinned ``tool_choice`` applies only to its first call, so on
        the follow-up after a tool result the model may call any registered tool. A record
        arriving that way is the model choosing to, which is not something a strategy can rely
        on, and reporting it as though the design had worked would be the difference between a
        mechanism and a coincidence.
        """
        return self._records_volunteered

    @property
    def records_truncated(self) -> int:
        """Forced calls the provider cut short because they reached the cap.

        Non-zero means a record may be partial, and a partial record is the one failure mode
        this design does not otherwise show. A tool call cut mid-arguments produces no record
        at all, which is loud: the strategy waits, falls back and flags ``FALLBACK``. A call
        cut just after a closing brace, or repaired by the provider, yields a record that
        parses and looks complete -- and the strategy then drops every tool group behind
        something that covers only part of them, scoring the loss as compaction damage rather
        than as an instrument that ran out of room.
        """
        return self._records_truncated

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        """Force the recall tool when the last call showed the conversation is large enough.

        The decision is made *after* ``call_next`` and applied on the *following* call, which
        is not a convenience. Before the pipeline runs, ``context.messages`` holds only the new
        turn: the history middleware sits deeper and replaces it with the loaded conversation
        during the call. There is no earlier point at which the size of the history can be
        known, so the check reads it on the way out and the option is set on the way in next
        time. The trigger sits well below the strategy's fallback threshold to absorb that
        one-call delay.

        **The exit of a forced call decides nothing.** The same delay that makes the decision
        late makes the forced call's own history stale: the record the model has just written
        is not in ``context.messages``, so the condition that fired still reads as true, and
        re-deciding there pins the next call as well. That is one trigger event and two
        records, measured -- ``records_in_conversation=2`` with repeats switched off, four to
        five with them on -- each one an agent turn, a broken prefix and a permanent addition
        to the floor under the prompt, and each one taking its call's own pinned tool choice
        away from it. So a forced call leaves the decision alone and the ask stays outstanding.
        The call after it is the first that can see the record, and it decides on that: if the
        record arrived, :meth:`_record_due` reads it and settles; if the model was cut off
        before writing one, nothing is there, and the trigger fires again one call later than
        it otherwise would have.

        The outstanding ask is also what makes the attribution honest. A record surfaces on the
        call *after* the one that was pinned, so crediting the call it became visible on would
        report every forced record as volunteered -- and, before this, the count was right only
        because the second forced call was there to be credited.

        Whether the *next* call is pinned is the whole of the decision, and it is taken in
        :meth:`_record_due`, which is the one place the rule is written down.
        """
        forced_this_call = self._force_next
        if forced_this_call:
            # Replaced rather than mutated: options may be shared with the caller's own dict,
            # and pinning a tool choice into it would outlive this call.
            # The tool is offered on this call and no other. Registering it on the agent
            # would put its schema in every request, and its description reads as sensible
            # hygiene right after a lookup -- which is exactly what happened: the model called
            # it unprompted on the unpinned follow-up call, and the run then measured the
            # model's initiative rather than this middleware. Options replace the tool list
            # rather than adding to it, so offering it here also hides everything else, which
            # is harmless on a call whose only purpose is to make this one call.
            self.arm()
            options: dict[str, Any] = {
                **dict(context.options or {}),
                "tool_choice": {"mode": "required", "required_function_name": RECALL_TOOL_NAME},
            }
            if self.record_max_tokens is not None:
                # Only here. The run's own cap is sized for an answer to the user and every
                # other call needs it; this one call writes a record instructed to summarise
                # everything, which is the only place a runaway is affordable at all.
                options["max_tokens"] = self.record_max_tokens
            context.options = options
            self._force_next = False
            self._forced += 1
            if self._reforce_next:
                self._reforce_next = False
                self._reforced += 1

        await call_next()

        if (
            forced_this_call
            and isinstance(context.result, ChatResponse)
            and context.result.finish_reason == "length"
        ):
            # Taken from the provider rather than inferred from the record's length: the model
            # is free to write a short record, and a short record is not a cut one. This is
            # the provider saying it stopped generating because it hit the ceiling, which is
            # the only statement that separates the two.
            #
            # A streamed result is not a ChatResponse yet, so it is not read here. Recording is
            # a one-shot forced call whose answer nobody displays, so there is nothing to
            # stream it for -- but a caller that did would lose this count, not get a wrong one.
            self._records_truncated += 1

        messages = list(context.messages)
        record_index = find_record_index(messages) if messages else None
        # The transition is tracked on the instance, not read from the messages on the way in.
        # Before the pipeline runs, context.messages holds only the new turn, so a pre-call
        # check reports "no record" on every call and every later call counts as a fresh one --
        # which is how an 18 appeared here for a single record.
        if record_index is not None and not self._seen_record:
            self._seen_record = True
            # Attributed to the ask, not to the call the record became visible on: a forced
            # call cannot see its own record, so the record surfaces one call later and
            # crediting that call would report every forced record as volunteered. A record
            # that arrived with nothing outstanding came from the model volunteering on an
            # unpinned follow-up call, and that is a different claim.
            if forced_this_call or self._awaiting_record:
                self._records_forced += 1
            else:
                self._records_volunteered += 1
        # One trigger event, one record. A forced call's own history predates the record it
        # asked for, so there is nothing here to decide on; see :meth:`process`.
        self._awaiting_record = forced_this_call
        if forced_this_call or not messages:
            self._force_next = False
            return
        annotate_message_groups(messages)
        annotate_token_counts(messages, tokenizer=self.tokenizer)
        self._force_next = self._record_due(messages, record_index)

    def _record_due(self, messages: list[Message], record_index: int | None) -> bool:
        """Return whether the next call should be pinned to the recall tool.

        **The rule, written once so nobody has to derive it from three booleans.** Write
        *pending* for the droppable tool work no record accounts for: the non-recall tool-call
        groups after the newest record, or all of them when there is no record yet. Then

        - the strategy's own ask for another record, ``reforce``, pins whenever one is
          outstanding, whatever else is true: it is made on a measured shortfall and bounded
          where it is made, so nothing here needs to second-guess it;
        - the group bound asks whenever ``pending`` reaches ``max_groups_before_record``,
          whatever else is true, because setting that bound is asking for repeats outright;
        - the size trigger asks for the *first* record as soon as the prompt passes
          ``trigger_fraction`` of the ceiling;
        - it asks again only when ``pending`` is at least one -- and not at all when
          ``repeat_records`` is off, which is what every run up to 63 did.

        **Why the second record needs ``pending`` and the first does not.** The size that fires
        the trigger does not go away once a record exists: the record is *added* to the
        conversation, and it is preserved, so the prompt is if anything larger afterwards. A
        repeat reading size alone would therefore stay true for the rest of the run and pin
        every remaining call. That is the regression to watch for: a conversation
        sitting above the trigger with nothing recorded since its last record is *settled* --
        there is nothing a second record could carry that the first does not -- and must be
        left alone until the agent does more tool work.

        Args:
            messages: The loaded conversation, already grouped and token-annotated.
            record_index: Index of the newest record, or None when there is none.

        Returns:
            True when the next call should be forced.
        """
        if self.reforce is not None and self.reforce():
            self._reforce_next = True
            return True
        pending = _droppable_groups_after(messages, record_index)
        if self.max_groups_before_record is not None and pending >= self.max_groups_before_record:
            return True
        if record_index is not None and (not self.repeat_records or pending < 1):
            return False
        return included_token_count(messages) > int(self.max_input_tokens * self.trigger_fraction)
