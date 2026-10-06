"""Summarise the user's own turns: re-summarising the summary, or leaving it standing as a boundary.

**The half of the conversation nothing here was touching.** Every other strategy in this
subpackage sheds tool output: :class:`~._anchored.AnchoredCompactionStrategy` shortens tool
results, :class:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy` replaces
whole tool groups with a record of them, and both of them keep user turns verbatim on the
stated ground that the turns are what give surviving values their meaning. That is true of the
*first* user turn, which carries the task and the requirements, and it is true of the *last*,
which is what the model is being asked to do now. It is not true of the seventy in between.

Measured on a 170,000-token conversation filled to 90% with a mixed workload, the conversation
divides as **57% user-turn text, 28% assistant replies and 14% tool results**: 87,551 tokens
of user turns against 21,978 of tool payload across 72 turns. A strategy that may only touch
the tool half is working on a seventh of the prompt, which is why the anchored strategies on
that workload land at 72% of the window where the uncompacted control lands at 85%. The user
half is the rest.

**What this does.** Past ``trigger_fraction`` of the ceiling it takes the user turns between a
fixed head and a fixed tail, sends them to a summarizer, and puts the summary back in their
place as a single user message. Tool-call groups, tool results and assistant messages are not
read, not annotated and not excluded -- see
:meth:`UserTurnAnchoredSummarizationCompactionStrategy._band`, which is the one place the
selection rule is written down. That independence is not tidiness:
the point of the strategy is to be comparable with the tool-side strategies, and a
strategy that shed both halves would answer neither question.

**Three modes, one selection rule.**
``summary_mode`` decides what a pass does with the summary the previous pass left behind, and
nothing else about the strategy moves with it: the trigger, the anchors, the band share, the
summarizer and the replacement mechanics are the same in all three.

- :data:`SUMMARY_MODE_RECOMPACT` re-reads its own output. The summary is a user message, the
  next pass's band is the previous summary plus the turns arrived since, and one message
  stands for everything behind it. This is the default; see :data:`DEFAULT_SUMMARY_MODE`.
- :data:`SUMMARY_MODE_BOUNDARY` never re-reads it. The summary a pass emits is a *boundary*:
  it is marked with :data:`~._preserve.PRESERVED_KEY`, the next pass's band starts after the
  newest boundary and runs to the tail, and each pass emits a new summary beside the standing
  ones rather than folding them in. :meth:`UserTurnAnchoredSummarizationCompactionStrategy._band`
  is where the boundary rule is written down, and it is written down nowhere else.
- :data:`SUMMARY_MODE_FOLD` is the boundary mode with a bound on the accumulation. Once the
  ordinary band has stopped yielding and the standing summaries are worth what a fold would
  cost, all of them are collapsed into one summary, which becomes the new boundary -- a rare
  major collection behind the frequent minor ones.
  :meth:`UserTurnAnchoredSummarizationCompactionStrategy._fold_due` is the rule, and it is
  derived from the same break-even as everything else here.

**What the two sides of that choice buy.** Prompt caching is strict-prefix: a mutation at position K
re-bills everything behind K at the uncached price. Recompaction rewrites a message that sits just
behind the head turn on every pass it makes, so every pass is a break of very nearly the whole
cached prefix. Measured over a conversation's seeding, a composition recompacting its user summary
hits the cache at 84% against 92% for a record half whose one preserved message is immutable once
written, and 95.5% for the uncompacted control. The boundary mode makes the user half behave the way
the record half already does. The prefix up to the newest boundary is byte-identical before and
after every later pass, so a later pass breaks the cache only from the band's first position, which
is the newest part of the prompt rather than the oldest.

What it costs is the thing recompaction exists to prevent, and choosing against recompaction
does not make the objection go away: **recompaction is what bounds the prompt.** A boundary is
never re-read, so N passes leave N standing summaries, each one a floor under the prompt that
no later pass can lower -- exactly the accumulation
:attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.records_in_conversation`
reports on the record row, and the reason that row consolidates nothing. In the recompacting
mode the floor is one summary; in the boundary mode it is one summary per pass, and
:attr:`UserTurnAnchoredSummarizationCompactionStrategy.user_summaries_in_conversation` with
:attr:`UserTurnAnchoredSummarizationCompactionStrategy.user_summary_tokens` is what says how
high it has risen, because nothing else in a table would -- the message count keeps rising and
every pass still reports having acted. The fold mode is the trade between the two: it pays the
whole-prefix break occasionally instead of on every pass, and it pays it only when the
standing summaries have grown large enough for the break to repay itself, by the same
arithmetic the band share is derived from. What a fold cannot be measured for is stated at the
end of this docstring, because it is the one cost the table cannot show.

**It recompacts its own output in the default mode, and that is the opposite of what
``_shorten`` does.**
:meth:`~._anchored.AnchoredCompactionStrategy._shorten` refuses to touch a tool result that
already carries :data:`~._anchored.REMOVAL_MARKER`, and the comment there says why: the
replacement carries the marker's own tokens on top of the budget, so a second pass would
shorten it again and a third again, each one a fresh mutation at the same position, and a
strict-prefix cache is re-billed from that position every time. That reasoning is sound *for
that strategy*, because its trigger is the band's geometry: a result sits in the band from the
turn it ages out of the tail until the end of the run, so "trim whatever is in the band" is an
instruction that fires on every single pass.

**The threshold is not self-limiting, though it looks it.** The tempting argument is that the
trigger is a threshold the compaction itself moves away from: a pass only runs past
``trigger_fraction`` of the ceiling, a pass removes tens of thousands of tokens, so the next
pass cannot happen until the conversation has grown all of that back -- once or twice in a run
rather than once per turn. Measured on gpt-5.6-luna at a 170,000-token window and 0.9 fill,
the strategy without the share rule below makes about fifteen passes a conversation, one on
most turns past the trigger, each replacing the previous summary and the turns since, with the
cache hit rate over the seeding at 77% to 81% against the uncompacted control's 95%.

The argument has two holes and either one is enough on its own.

- **The band is not the prompt.** It is the user turns between the anchors, and the assistant
  replies and tool results around them are not this strategy's to touch. On the run above the
  band was about 28% of the prompt, so a pass that removed *all* of it need not take the
  prompt back under the line -- and on a workload whose bulk is tool output it certainly does
  not. "A compaction moves away from its own trigger" is true only while the band is most of
  what there is to remove, which is not the ordinary case.
- **After the first pass the band is not even that.** What is left between the anchors is this
  strategy's own summary plus whatever turns arrived since, a fraction of a percent of the
  prompt. Every later pass then rewrites the prefix at the summary's position to free almost
  nothing, which is exactly the thrash ``_shorten`` refuses.

The size that fired the trigger does not go away by itself -- the sentence
:meth:`~._toolsummary.ToolResultRecallMiddleware._record_due` is built around -- and the
"something in the band is not my own summary" rule below is satisfied by every new user turn,
so above the trigger the condition stays true for the rest of the run.

**Hysteresis, and it is one rule: a pass has to be worth what a pass costs.** The band must be
worth at least ``min_band_share`` of the included prompt before anything is summarised: see
:data:`DEFAULT_MIN_BAND_SHARE` for the number, and
:meth:`UserTurnAnchoredSummarizationCompactionStrategy._worth_compacting` for the rule and for
the two shapes of hysteresis it was chosen over. That is what bounds the firing count, and the
bound is geometric rather than a cap: the band can only regrow from user turns added since the
last pass, so for the band to be worth a share ``f`` of the prompt again the prompt itself must
have grown by a factor of at least ``1 / (1 - f)``. A conversation that grows from the size of
its first compaction to ``k`` times that size therefore compacts at most
``ceil(log(k) / log(1 / (1 - f)))`` times -- one pass per 11% of prompt growth at the default,
so seven over a conversation that doubles, against one per turn before. What it costs is stated
with the constant: a band that never clears the share is a band never compacted at all, and
:attr:`UserTurnAnchoredSummarizationCompactionStrategy.user_passes_declined` is what says so
rather than leaving the strategy looking like the uncompacted control.

So the same argument that makes re-trimming wrong in ``_shorten`` makes re-summarising
affordable here only once a pass is required to be worth something, and the two are now
decided by one rule read from opposite ends rather than by one of them forgetting the other.

**The share bites harder in the boundary and fold modes, and that is measured rather than
tuned away.** In the recompacting mode the band the share is taken of includes the previous
summary, which inflates it; after a boundary the band is only the turns newer than the
boundary, and the prompt it is weighed against is larger by every standing summary. So the
same share clears less often, ``user_passes_declined`` rises, and the boundary modes fire
fewer passes than the recompacting one on the same conversation. The tests beside this module
hold the three modes to one share on one fixture and record the numbers, because the point of
the flag is that the arms be comparable, and a share re-tuned per mode would compare nothing.

**It will not run on nothing new.** A pass whose band holds only this strategy's own earlier
summary would rewrite one message at one position and free exactly nothing, which is precisely
the thrash ``_shorten`` refuses. So the band has to contain at least one turn that is not a
summary -- the same shape as
:meth:`~._toolsummary.ToolResultRecallMiddleware._record_due`, which re-arms its trigger on new
material rather than on size. That rule is necessary and, as the measurement above shows, not
sufficient: one new turn satisfies it, so it is the ``f = 0`` corner of the share rule and both
are checked in the one place. After a boundary the same statement is the empty band: no turn
between the newest boundary and the tail is nothing new, and it is declined by the same rule.

**The replacement is a user message, where the framework's own summarizer writes an assistant
one.** ``SummarizationStrategy`` summarises whole groups of every kind, so its output belongs to
neither speaker and assistant is the neutral choice. This replaces user turns only, and the
replacement has to be readable *as* those turns on the next pass: a summary written as
assistant prose would be invisible to the selection rule above, so in the recompacting mode
nothing could ever recompact it, and in the boundary modes it could not be found as the
boundary at all. It also keeps the conversation's shape legal -- an assistant message inserted
between a user turn and the assistant reply to it puts two assistant messages in a row, which
several providers reject.

**A boundary is protected by the same mark the record is, and found by a different one.**
:data:`~._preserve.PRESERVED_KEY` is this subpackage's one vocabulary for "no strategy may
shorten, drop or shed this", and a standing summary is exactly that: the sole surviving copy
of the turns behind it, which no later pass will stand for again. Every removal path in
``_anchored`` and ``_toolsummary`` already honours the mark, so marking the summary is what
turns "the record half cannot reach a user group" from an accident of group kinds into a
stated contract. The mark is *not* how the boundary is found, because the mark is also set by
other strategies on turns that are not boundaries, and because it does not survive storage:
the boundary is the newest included message :func:`_is_summary` recognises, by its summary links
and text marker. Hosts must retain those links through storage, and the mark is re-applied to
every standing summary on every pass, as :func:`~._toolsummary._preserve_records` re-applies
it to every record. In the recompacting mode the summary is deliberately *not* marked --
:meth:`UserTurnAnchoredSummarizationCompactionStrategy._band` skips preserved turns, so a
marked summary could never be recompacted, and that mode would silently become this one.

**Live, every pass runs twice, and the second time has to be free.** The framework runs one
strategy at two sites on one conversation: inside the model call, on the messages the history
provider has just loaded (``Agent.compaction_strategy``, which is where the harness puts its
before phase), and after the turn, on the messages the provider stored
(``CompactionProvider.after_strategy``). The loaded messages are copies --
``SessionContext.extend_messages`` gives each one its own ``additional_properties`` -- so nothing
the in-call pass excludes or inserts reaches the store, and the after-turn pass finds the same
band and, left to itself, summarises it again: two summarizer calls for one standing summary,
and the model sends two different summaries at one position on consecutive calls -- a second
whole-suffix cache break per crossing that no mode is designed around, paid by all three alike.
The strategy cannot tell which list it is on and does not try; what it can do is never send one
request twice. The last summarizer request and its answer are kept, and a pass whose request is
byte-identical replays the answer under the same id: no summarizer call, no counted compaction,
and the store ends up carrying exactly the message the model was already sent, so the crossing
call's prefix survives into the call after it.
:attr:`UserTurnAnchoredSummarizationCompactionStrategy.user_summaries_replayed` counts those
passes. The same memory serves a turn re-sent after a throttled attempt, which is the same
request from a restored state, and a tool turn's second model call, which loads the store afresh.

**How it marks what it replaced is the framework's mechanism and not a new one.**
``SummarizationStrategy`` inserts its summary at the first index it superseded, annotates the
summary with :data:`~agent_framework._compaction.SUMMARY_OF_MESSAGE_IDS_KEY` and
:data:`~agent_framework._compaction.SUMMARY_OF_GROUP_IDS_KEY`, writes the reverse link onto
each superseded message, and excludes them with a reason. All five steps are repeated here
verbatim, so a conversation compacted by this strategy reads back through the same trace
metadata as one compacted by the framework's. Replacing rather than deleting is what makes the
recompaction honest as well: the band that the second pass reads is a summary that still
*stands for* the turns behind it, so nothing is lost by a route nobody can follow.

**Retention of the turns' content is explicitly not measured by this package's benchmark.**
The question the benchmark answers for this strategy is how much of a conversation is user-side
and therefore how much a strategy that may touch it can remove. What the summariser managed to
keep is a separate question, and one an instrument whose planted facts live in tool results
cannot ask of filler turns that carry none.

**And a fold is where that blindness matters most, so it is stated rather than implied.** A
fold summarises summaries, and fidelity degrades across generations: what the second summary
keeps of the first is bounded by what the first kept of the turns, and a benchmark whose
planted facts live in tool results can see neither loss: its accuracy is structurally blind to
anything done to a user turn, and for the user half it measures compaction percentage and cost
only. That scope is accepted, not overlooked. The numbers a fold shows are therefore its price
and its size, never its fidelity, and accuracy reading as the control's is not evidence that
folding is free -- it is the instrument declining to look.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

from agent_framework import Message
from agent_framework._compaction import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    annotate_message_groups,
    annotate_token_counts,
    group_messages,
    included_token_count,
    set_excluded,
)

from ._preserve import any_preserved, set_preserved
from ._summary_links import has_summary_links

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_framework import TokenizerProtocol
    from agent_framework._clients import SupportsChatGetResponse

__all__ = [
    "DEFAULT_KEEP_HEAD_USER_TURNS",
    "DEFAULT_KEEP_TAIL_USER_TURNS",
    "DEFAULT_MIN_BAND_SHARE",
    "DEFAULT_SUMMARY_MODE",
    "DEFAULT_USER_FOLD_PROMPT",
    "DEFAULT_USER_SUMMARY_PROMPT",
    "DEFAULT_USER_TRIGGER_FRACTION",
    "EXCLUDE_REASON",
    "FOLD_EXCLUDE_REASON",
    "FOLD_ID_PREFIX",
    "FOLD_OUTCOMES",
    "SUMMARY_ID_PREFIX",
    "SUMMARY_MODES",
    "SUMMARY_MODE_BOUNDARY",
    "SUMMARY_MODE_FOLD",
    "SUMMARY_MODE_RECOMPACT",
    "USER_SUMMARY_MARKER",
    "FoldOutcome",
    "UserTurnAnchoredSummarizationCompactionStrategy",
]

#: What :meth:`UserTurnAnchoredSummarizationCompactionStrategy.fold_if_smaller` did: collapsed the
#: standing summaries, refused an answer that was no smaller than them, got no answer at all, or
#: had fewer than two summaries to collapse.
FoldOutcome = Literal["folded", "rejected", "failed", "skipped"]

#: Every :data:`FoldOutcome`, for a caller that wants to count them.
FOLD_OUTCOMES: Final[tuple[FoldOutcome, ...]] = ("folded", "rejected", "failed", "skipped")

logger = logging.getLogger(__name__)

#: Fraction of the ceiling at which the user band is summarised.
#:
#: **0.8, and it decides when compaction starts, not how often it happens.** It is tempting to
#: read this as the whole of what makes recompaction affordable, on the argument that a pass
#: cannot follow another pass until the conversation has grown back everything the first one
#: removed. That argument is wrong whenever the band is a minority of the prompt, which is the
#: ordinary case, and a strategy relying on it measures thirty passes in a conversation -- see
#: the module docstring. What bounds the passes is :data:`DEFAULT_MIN_BAND_SHARE`; this number
#: only decides
#: how large the prompt is before the first one.
#:
#: Lowering it does not produce per-turn mutation, because the share rule is what refuses
#: that; it produces a first compaction on a smaller prompt, which is the thing the flag is for.
#:
#: It sits above :data:`~._toolsummary.DEFAULT_TRIGGER_FRACTION`, which is 0.6, and the two are
#: different decisions rather than an inconsistency. That one asks a *model* for a record and
#: has to ask early, because the record degrades with the bulk it is given to read; this one
#: asks a summarizer for prose whose quality is not what the row measures, and pays instead in
#: a broken cached prefix, so it wants to fire as late as it can while still leaving the
#: conversation room to continue.
#:
#: **This is the single row's line, and the composed row does not use it.**
#: :class:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` judges this
#: strategy at the record strategy's fraction instead, against the prompt as the record phase
#: left it: one line, and the user half acts only when tool compaction alone did not bring the
#: prompt under it. Nothing about this constant or the strategy that reads it changes; the
#: composition supplies the line rather than the object.
DEFAULT_USER_TRIGGER_FRACTION: Final[float] = 0.8

#: Share of the included prompt the band must be worth before a pass may run.
#:
#: **0.1, and it is the hysteresis.** Without it the strategy fires once per turn for the rest
#: of a run that stays above the trigger, because the band it reads after its first pass is its
#: own summary plus the turns arrived since -- 0.4% to 0.9% of the prompt on the fixture in
#: ``tests``, and ``USERREPLACED:2`` on the live run in the module docstring. Each of those
#: passes spends a summarizer call and re-bills the prompt from the summary's position to the
#: end of the conversation in order to free a few hundred tokens.
#:
#: **Where 0.1 comes from: this package's own break-even, at a measured conversation length.**
#: :data:`~._anchored.DEFAULT_MIN_GAIN_FRACTION` derives when an edit repays the prefix it
#: breaks -- ``R > B * (p - c) / (p + T * c)`` for ``R`` tokens removed, ``B`` tokens behind the
#: edit, ``T`` turns still to come and the measured prices ``p = 0.66`` and ``c = 0.07`` per
#: million. This strategy's edit sits at the band's first position, just behind the head turn,
#: so ``B`` is very nearly the whole prompt and the share is a share of the prompt. Solved for
#: ``T`` instead of for ``R``, a band worth ``f`` of the prompt repays itself within
#: ``((p - c) / f - p) / c`` turns: **75 turns at 0.1**, which is the length of the
#: conversations the strategies were measured on (72 turns at the 170,000-token cell). So a
#: pass this rule permits can repay inside the conversation it is part of, and the 0.9% passes
#: measured above would have needed about 900 turns to.
#:
#: **Why not 0.29, which is the same formula.** That is the twenty-turns-remaining figure, and
#: at this strategy's geometry it refuses a 28% band -- the strategy would then be the
#: uncompacted control, which is not a fix for a strategy that fires too often. It is also a
#: floor this package deliberately did *not* make a default: it ships as
#: :class:`~._anchored.MinimumGainAnchoredCompactionStrategy`, a separate row measured against
#: its own parent, because a break-even floor changes what a strategy measures rather than only
#: how often it fires. This constant is sized to remove passes that cannot repay under any
#: conversation length anyone here runs, and to leave the rest to that row.
#:
#: **What it bounds.** A band regrows only from user turns added since the last pass, so for the
#: band to be worth ``f`` of the prompt again the prompt must have grown by a factor of at least
#: ``1 / (1 - f)`` -- 1.111 here. Compactions over a conversation growing from ``P`` to ``kP``
#: are therefore at most ``ceil(log(k) / log(1.111))``: seven on a conversation that doubles
#: after its first compaction, three on one that grows by a third, against one per turn before.
#:
#: **What it costs.** A band that never reaches a tenth of the prompt is never compacted, and on
#: a workload whose bulk is tool output that can be every band in a run -- measured on such a
#: workload, one crossing a conversation against 26 to 39 passes held. The strategy is then the
#: uncompacted control on its user half -- but it says so, in
#: :attr:`UserTurnAnchoredSummarizationCompactionStrategy.user_passes_declined`, which is the
#: difference between this and the silent degradation the counters in this package exist to
#: rule out. ``0.0`` switches the rule off, so the two can be run side by side.
#:
#: **One share for all three modes, and it is also the fold's threshold.** After a boundary the
#: band is smaller -- the previous summary is not in it -- and the prompt it is weighed against
#: is larger by every standing summary, so the same share clears less often there; that is
#: measured on the fixture in ``tests`` rather than compensated for, because a share re-tuned
#: per mode would make the modes incomparable. In the fold mode the same number decides the
#: fold as well, with the fold's own ``R`` and ``B``: see
#: :meth:`UserTurnAnchoredSummarizationCompactionStrategy._fold_due`, which is the break-even
#: above with the terms renamed and not a second constant.
DEFAULT_MIN_BAND_SHARE: Final[float] = 0.1

#: What a pass does with the summary the previous pass left behind.
#:
#: Re-read it and replace it, so one message stands for everything behind it.
SUMMARY_MODE_RECOMPACT: Final[str] = "recompact"

#: Leave it standing as a boundary, never re-read and never replaced. Each pass emits a new
#: summary beside the standing ones; the prefix up to the newest boundary is byte-identical
#: before and after every later pass, and N passes leave N standing summaries.
SUMMARY_MODE_BOUNDARY: Final[str] = "boundary"

#: The boundary mode, plus a fold: once the ordinary band has stopped yielding and the standing
#: summaries are worth what a fold costs, all of them are collapsed into one, which becomes the
#: new boundary. See :meth:`UserTurnAnchoredSummarizationCompactionStrategy._fold_due`.
SUMMARY_MODE_FOLD: Final[str] = "fold"

#: Every mode the constructor accepts, in the order the three were written.
SUMMARY_MODES: Final[tuple[str, ...]] = (
    SUMMARY_MODE_RECOMPACT,
    SUMMARY_MODE_BOUNDARY,
    SUMMARY_MODE_FOLD,
)

#: The mode a caller inherits, and it is the recompacting one on purpose.
#:
#: Not because it measures best -- the strict-prefix argument in the module docstring says it
#: breaks the cached prefix on every pass -- but because measurement has not separated the three
#: modes: one persistent crossing a conversation on the standalone strategy, and a draw inside
#: the seed spread on the composition. This package's standing rule is that a default does not
#: move until a run has measured both arms. Flipping it later is this one line.
DEFAULT_SUMMARY_MODE: Final[str] = SUMMARY_MODE_RECOMPACT

#: User turns kept verbatim at the start.
#:
#: One, because one is what is load-bearing: the opening turn carries the task and the
#: requirements, and every deleting strategy measured in this package throws them away first --
#: truncation left 29 of 53 planted facts in the prompt and the model used none of them,
#: because the codes survived while the turns saying which deployment each belonged to did not.
#: The turns after it are the conversation, not its terms of reference, and summarising them is
#: the point of the strategy rather than a cost of it.
DEFAULT_KEEP_HEAD_USER_TURNS: Final[int] = 1

#: User turns kept verbatim at the end.
#:
#: One, and for a reason that does not generalise upward: the last user turn is the live
#: request, and a model answering a summary of the question it was just asked is answering the
#: wrong question. A second-to-last turn has no such claim -- it has already been answered --
#: so a larger tail buys nothing and costs the band its newest and most quotable material.
DEFAULT_KEEP_TAIL_USER_TURNS: Final[int] = 1

#: Marks the message this strategy leaves in place of the turns it replaced.
#:
#: Two jobs, as :data:`~._anchored.REMOVAL_MARKER` has two. It tells the model that what it is
#: reading stands for turns that are no longer present, which a model shown a silently reduced
#: conversation cannot know; and together with the summary links it identifies earlier output,
#: which decides whether there is new material to compact at all -- or, in the boundary modes,
#: where the band begins.
USER_SUMMARY_MARKER: Final[str] = "[earlier turns in this conversation, compacted]"

#: Prefix of the ``message_id`` given to every summary this strategy inserts.
#:
#: For the framework's trace metadata, which is keyed on ids. It does not identify a summary:
#: a store may assign its own ids, so :func:`_is_summary` reads the marker and summary links.
SUMMARY_ID_PREFIX: Final[str] = "user_summary_"

#: Prefix of the ``message_id`` given to the summary a fold inserts.
#:
#: Under :data:`SUMMARY_ID_PREFIX`, and numbered by fold rather than by pass so that an id can
#: never collide with an ordinary summary's.
FOLD_ID_PREFIX: Final[str] = f"{SUMMARY_ID_PREFIX}fold_"

#: Reason recorded on the turns this strategy supersedes.
#:
#: Named after the framework's own ``"summarized"``, and distinct from it, so a conversation
#: read back says which of the two strategies claimed a message.
EXCLUDE_REASON: Final[str] = "user_turn_summarized"

#: Reason recorded on the standing summaries a fold supersedes, distinct from
#: :data:`EXCLUDE_REASON` so a conversation read back says whether a message was a turn the
#: strategy summarised or a summary it folded.
FOLD_EXCLUDE_REASON: Final[str] = "user_summaries_folded"

#: Reason recorded on a standing summary when it is protected as a boundary, so a caller
#: reading the conversation back can tell this strategy's boundaries from the record
#: ``_toolsummary`` preserves under its own reason.
PRESERVE_REASON: Final[str] = "user_summary_boundary"

#: What the summarizer is asked for.
#:
#: Deliberately not the framework's ``DEFAULT_SUMMARIZATION_PROMPT``, which asks for "the
#: entire conversation" in "no more than five sentences". Both halves are wrong here. The
#: input is one side of the conversation rather than all of it, so a prompt describing both
#: invites the model to invent the assistant's half; and a fixed five-sentence bound is a
#: length, which is the one thing a summary of seventy turns must be allowed to vary.
DEFAULT_USER_SUMMARY_PROMPT: Final[str] = (
    "You are compacting a conversation to save space. Below are the user's own turns, in "
    "order, with the assistant's replies and any tool output removed. Rewrite them as a "
    "single, much shorter account of what the user asked for, in the order they asked for "
    "it. Keep every requirement, constraint, correction and preference exactly as stated, "
    "and quote verbatim any value that could not be reconstructed or guessed: identifiers, "
    "codes, names, numbers, paths, URLs, versions, states, timestamps. Drop pleasantries, "
    "restatements and anything a later turn superseded. Write about the user in the third "
    "person and add nothing that is not in the text you were given."
)

#: What the summarizer is asked for when a fold collapses the standing summaries.
#:
#: The same instruction as :data:`DEFAULT_USER_SUMMARY_PROMPT` with one difference stated up
#: front: the input is not the user's turns but earlier summaries of them, each already
#: standing for turns that are gone. Saying so matters, because the ordinary prompt tells the
#: model to drop pleasantries and restatements, and a summary has none of the first and is
#: made of the second -- a model told it is reading turns would compress what is already
#: compressed as though it were padding.
DEFAULT_USER_FOLD_PROMPT: Final[str] = (
    "You are compacting a conversation to save space. Below are earlier compaction summaries "
    "of the user's own turns, in the order those turns were spoken; each one already stands "
    "for turns that are no longer present, and none of it is padding. Rewrite them as a "
    "single, shorter account of what the user asked for, in the order they asked for it. Keep "
    "every requirement, constraint, correction and preference exactly as stated, and quote "
    "verbatim any value that could not be reconstructed or guessed: identifiers, codes, names, "
    "numbers, paths, URLs, versions, states, timestamps. Drop only what a later summary "
    "superseded. Write about the user in the third person and add nothing that is not in the "
    "text you were given."
)


def _mark_summarized_by(message: Message, summary_id: str) -> None:
    """Record on ``message`` which summary now stands for it.

    This is ``agent_framework._compaction._set_group_summarized_by_summary_id`` written out
    rather than called. The key, the value and the placement are the framework's, because a
    caller reading the conversation back -- including the framework's own summary
    reconciliation -- looks for exactly this annotation; what is not borrowed is the helper,
    which is private even by the standard of a module that is already private, and this
    subpackage is meant to be lifted out and pinned against a framework version rather than to
    follow one symbol by symbol. Reaching one level deeper for four lines would buy a
    dependency that a rename could break silently.

    The guard is the framework's too: the annotation is normally the mapping ``group_messages``
    wrote, but a message arriving with something else under that key must not have it mutated
    in place.

    Args:
        message: The superseded message, annotated in place.
        summary_id: Id of the summary that replaced it.
    """
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    if not isinstance(annotation, dict):
        annotation = {}
        message.additional_properties[GROUP_ANNOTATION_KEY] = annotation
    annotation[SUMMARIZED_BY_SUMMARY_ID_KEY] = summary_id


def _is_summary(message: Message) -> bool:
    """Recognize user summaries by their text marker and persisted source links.

    Hosts must preserve the links through storage and keep them separate from user text.
    """
    return (
        message.role == "user"
        and has_summary_links(message)
        and (message.text or "").startswith(USER_SUMMARY_MARKER)
    )


def _is_standing_summary(message: Message) -> bool:
    """Return whether ``message`` is a summary this strategy wrote that is still being sent.

    A superseded summary -- one the recompacting mode replaced, or one a fold collapsed -- is
    excluded and stands for nothing on the wire, so it is neither a boundary nor part of the
    floor the standing count reports. Only user messages are read, since every summary is one.

    Args:
        message: The message to inspect.

    Returns:
        True when it is an included summary.
    """
    return (
        message.role == "user"
        and not message.additional_properties.get(EXCLUDED_KEY, False)
        and _is_summary(message)
    )


def _summary_body(message: Message) -> str:
    """Return a summary's text without the marker line a fold would otherwise re-summarise.

    Args:
        message: A summary this strategy wrote.

    Returns:
        The summarizer's own text.
    """
    return (message.text or "").removeprefix(USER_SUMMARY_MARKER).strip()


def _next_summary_id(messages: list[Message], *, prefix: str, minimum: int) -> str:
    """Return an id under ``prefix`` that no message in the conversation already carries.

    Numbered by this instance's own count, and never below one past the highest number the
    conversation already holds under the prefix. The count alone is not unique: a strategy is
    built per run and a conversation outlives one -- restored from a store, or handed to a
    fresh instance over the same list -- so an instance counting from zero over a conversation
    that already holds ``user_summary_0`` would mint it again, and every back-reference written
    under that id would then name two messages. The whole conversation is read, superseded
    summaries included, because a superseded summary is still the message the turns it stood
    for point back at; it matters more now that the boundary modes leave every summary standing
    rather than replacing it. Conversation length, which the framework's own strategy numbers
    by, is not unique either: see
    :meth:`UserTurnAnchoredSummarizationCompactionStrategy.compact_against`.

    The suffix has to be all digits, so the fold prefix -- which sits under the summary prefix
    -- is neither counted against ordinary summaries nor confused with them.

    Args:
        messages: The conversation, superseded messages included.

    Keyword Args:
        prefix: :data:`SUMMARY_ID_PREFIX` or :data:`FOLD_ID_PREFIX`.
        minimum: The number this instance would use on its own count.

    Returns:
        The id.
    """
    highest = -1
    for message in messages:
        message_id = message.message_id or ""
        suffix = message_id.removeprefix(prefix)
        if suffix != message_id and suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{max(minimum, highest + 1)}"


def _format_turns(
    turns: list[Message], *, text: Callable[[Message], str | None] | None = None
) -> str:
    """Return the user turns as the numbered transcript the summarizer reads.

    Numbered because order is part of what has to survive: a later turn may correct an earlier
    one, and a summary that cannot tell which came first will keep the correction and the thing
    it corrected as though both still stood.

    Args:
        turns: The user messages about to be replaced, in conversation order.

    Keyword Args:
        text: How to read one message. None reads the turn's own text; a fold passes
            :func:`_summary_body` so the marker is not sent to the summarizer as content.

    Returns:
        One line per turn.
    """
    lines: list[str] = []
    for index, message in enumerate(turns, start=1):
        body = message.text if text is None else text(message)
        lines.append(f"{index}. {body or ''}")
    return "\n".join(lines)


class _Remembered(NamedTuple):
    """One summarizer request and its answer, kept so a repeat of the request is not sent.

    One entry by default, because on this strategy's own row the repeat always follows at once:
    the after-turn pass over the store comes straight after the in-call pass over the copies,
    and a tool turn's second call comes between them. Nothing else this strategy asks for can
    intervene -- a fold is reached only through a declined band, which is a pass that asks for
    nothing. The composed row breaks that last premise: its last-resort chain may ask for a
    fold on the same pass the band was summarised on, so it requires two outside the recompacting
    mode -- see ``remembered_requests``.
    """

    prompt: str
    transcript: str
    summary_id: str
    text: str


class UserTurnAnchoredSummarizationCompactionStrategy:
    """Replace the user turns between a fixed head and tail with one summary of them.

    Args:
        max_input_tokens: Ceiling the included prompt is measured against. This is the model's
            real input limit rather than its advertised context window; the two differ by
            128,000 tokens on GPT-5-class deployments, and configuring the larger one puts the
            trigger above what the service will accept.
        tokenizer: Token counter, shared with whatever measures the result.

    Keyword Args:
        client: Chat client that writes the summary. **Security:** its output permanently
            replaces the user's own turns in the conversation and is trusted from then on like
            any other message, so point this only at a service trusted as much as the primary
            model -- the same indirect-prompt-injection caveat the framework's
            ``SummarizationStrategy`` carries, and it applies harder here, because the text it
            replaces is the user's.
        keep_head_user_turns: User turns at the start left verbatim. See
            :data:`DEFAULT_KEEP_HEAD_USER_TURNS`.
        keep_tail_user_turns: User turns at the end left verbatim. See
            :data:`DEFAULT_KEEP_TAIL_USER_TURNS`.
        trigger_fraction: Fraction of ``max_input_tokens`` the included prompt must pass before
            anything happens. Below it the strategy returns without reading the conversation's
            shape at all: a compaction that was not needed spends a summarizer call and breaks
            a cached prefix for nothing. See :data:`DEFAULT_USER_TRIGGER_FRACTION`.
        min_band_share: Share of the included prompt the band must be worth before a pass may
            run. This is the hysteresis: without it the strategy fires once per turn for the
            rest of a run that stays above the trigger. ``0.0`` disables it. See
            :data:`DEFAULT_MIN_BAND_SHARE`.
        summary_mode: What a pass does with the summary the previous pass left behind: one of
            :data:`SUMMARY_MODES`. The recompacting default re-reads and replaces it; the
            boundary mode leaves it standing and compacts only what is newer; the fold mode does
            that and collapses the standing summaries into one once they are worth the break.
            See the module docstring for what each buys, and :data:`DEFAULT_SUMMARY_MODE` for
            why the default is the one it is.
        prompt: What the summarizer is asked for. See :data:`DEFAULT_USER_SUMMARY_PROMPT`.
        fold_prompt: What the summarizer is asked for when a fold collapses the standing
            summaries. Read in the fold mode and by :meth:`fold_if_smaller`. See
            :data:`DEFAULT_USER_FOLD_PROMPT`.
        remembered_requests: How many of the most recent summarizer requests are remembered
            for replay on the other list the live path runs this on. One, the default, is what
            every row of this strategy has run with and is enough for it: a pass makes at most
            one request. The composed row makes two on a pass that is over its ceiling -- the
            band, then the fold its last-resort chain asks for -- and refuses fewer than two
            outside the recompacting mode, so the second list replays both rather than paying
            for both again. See
            :class:`_Remembered`.
    """

    def __init__(
        self,
        *,
        max_input_tokens: int,
        tokenizer: TokenizerProtocol,
        client: SupportsChatGetResponse[Any],
        keep_head_user_turns: int = DEFAULT_KEEP_HEAD_USER_TURNS,
        keep_tail_user_turns: int = DEFAULT_KEEP_TAIL_USER_TURNS,
        trigger_fraction: float = DEFAULT_USER_TRIGGER_FRACTION,
        min_band_share: float = DEFAULT_MIN_BAND_SHARE,
        summary_mode: str = DEFAULT_SUMMARY_MODE,
        prompt: str | None = None,
        fold_prompt: str | None = None,
        remembered_requests: int = 1,
    ) -> None:
        """Validate and store the configuration.

        Raises:
            ValueError: If the ceiling is not positive, either anchor is negative, the trigger
                is outside ``(0.0, 1.0]``, the band share is outside ``[0.0, 1.0)``, or the
                mode is not one of :data:`SUMMARY_MODES`. A trigger of zero would fire on an
                empty conversation, where the band is empty and the only thing a pass can
                produce is a summarizer call; above one it can never fire, which is a row that
                silently measures the uncompacted control under another name. A band share of
                one demands a band that is the whole prompt, which is the same never-fires row
                by the other route; zero is legal and is the behaviour this class had before
                the share existed, kept so the two can be run side by side. An unknown mode is
                refused rather than read as the default, because a misspelt mode that quietly
                ran the default would be a row measuring the wrong arm under the right name.
        """
        if max_input_tokens <= 0:
            raise ValueError("max_input_tokens must be positive.")
        if keep_head_user_turns < 0 or keep_tail_user_turns < 0:
            raise ValueError("keep_head_user_turns and keep_tail_user_turns must be >= 0.")
        if remembered_requests < 1:
            raise ValueError("remembered_requests must be >= 1.")
        if not 0.0 < trigger_fraction <= 1.0:
            raise ValueError("trigger_fraction must be in (0.0, 1.0].")
        if not 0.0 <= min_band_share < 1.0:
            raise ValueError("min_band_share must be in [0.0, 1.0).")
        if summary_mode not in SUMMARY_MODES:
            raise ValueError(f"summary_mode must be one of {SUMMARY_MODES}, not {summary_mode!r}.")
        self.max_input_tokens = max_input_tokens
        self.tokenizer = tokenizer
        self.client = client
        self.keep_head_user_turns = keep_head_user_turns
        self.keep_tail_user_turns = keep_tail_user_turns
        self.trigger_fraction = trigger_fraction
        self.min_band_share = min_band_share
        self.summary_mode = summary_mode
        self.prompt = prompt or DEFAULT_USER_SUMMARY_PROMPT
        self.fold_prompt = fold_prompt or DEFAULT_USER_FOLD_PROMPT
        self._compactions = 0
        self._replaced = 0
        self._failures = 0
        self._below_trigger = 0
        self._declined = 0
        self._folds = 0
        self._summaries_in_conversation = 0
        self._summary_tokens = 0
        self._replayed = 0
        self.remembered_requests = remembered_requests
        self._remembered: list[_Remembered] = []
        # Folds :meth:`fold_if_smaller` kept, by the transcript of the summaries they replaced,
        # for the whole run. See :meth:`refold`.
        self._kept_folds: dict[str, _Remembered] = {}

    @property
    def recompacts_summaries(self) -> bool:
        """Whether a pass re-reads the previous summary, which is the recompacting mode alone.

        The one question the two boundary modes answer alike, read off the mode in one place so
        that :meth:`_band` and :meth:`_observe_summaries` cannot disagree about it.
        """
        return self.summary_mode == SUMMARY_MODE_RECOMPACT

    @property
    def user_compactions(self) -> int:
        """Passes that replaced a band of user turns with a summary.

        The count that says whether this row is measuring the design at all. Zero means the row
        is the uncompacted control wearing another name -- which is the reading this package has
        twice had to add a counter to rule out -- and which of the three ways it got there is
        said by :attr:`user_passes_below_trigger`, :attr:`user_passes_declined` and
        :attr:`user_summary_failures`, exactly one of which is non-zero on such a row.

        Read it against :attr:`user_messages_replaced` rather than alone. One compaction that
        replaced seventy turns and seven that replaced ten each cost very different amounts of
        cache: every pass re-bills the prompt from its own edit to the end, so the number of
        passes is the number of times that was paid.

        A pass that replaced the band with a summary already in hand is not counted here but
        under :attr:`user_summaries_replayed`: it produced no summary and paid no new break.
        Live, every crossing is one of each.
        """
        return self._compactions

    @property
    def user_messages_replaced(self) -> int:
        """User turns the most recent compaction superseded.

        The most recent, not the total, and the distinction is the same one
        :attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.groups_kept_uncovered`
        draws: this answers how much of the conversation the strategy is standing in for *as it
        now stands*, which is what the prompt's size is made of. A running total would double
        count by construction, because a later pass reads the earlier pass's summary and
        replaces it again -- so a run that compacted seventy turns, then recompacted the
        summary of them plus ten more, would report eighty-one turns replaced out of eighty-one
        that ever existed, while the prompt carries one message.

        In the boundary modes the most recent pass's band holds no earlier summary, so this is
        the turns behind the *newest* boundary only, and the whole of what the standing
        summaries stand for is spread across :attr:`user_summaries_in_conversation` of them. A
        fold does not move it: a fold supersedes summaries, not turns.

        It is not reset by a pass that declines, because what it describes is the summary
        sitting in the prompt and that summary is still standing for those turns. Zero
        therefore means one thing only: no compaction has happened, which is the same reading
        :attr:`user_compactions` gives and is why the two are reported together.
        """
        return self._replaced

    @property
    def user_summaries_in_conversation(self) -> int:
        """Summaries this strategy wrote that the conversation is still sending, as it now stands.

        The floor. In the boundary mode every one of them is preserved -- neither re-read by
        this strategy nor shortened or dropped by any other -- and nothing merges them, so each
        raises a floor under the prompt that no later pass can lower: N passes, N of these. That
        is the accumulation
        :attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.records_in_conversation`
        reports on the record row, and it is reported here for the same reason: a row whose
        compaction has stopped paying for a good reason and one whose unshrinkable part has
        quietly grown are otherwise the same row. In the recompacting mode it reads one after
        the first pass and never more, which is the bound that mode buys.

        **As it now stands, not at its peak**, which is where this differs from the record
        count. Records never decrease; standing summaries do, because a fold collapses them, so
        a maximum would report a floor the prompt no longer has. Read it beside
        :attr:`user_folds`: a low count with folds is a floor that was lowered, and a high count
        without them is the boundary mode's cost arriving.

        Read at the start of every pass over the trigger and again after every pass that
        inserts a summary, so the last state a run leaves is counted whether or not a later
        pass reads it. Zero means no summary is being sent, whichever mode wrote or replaced
        or folded it.
        """
        return self._summaries_in_conversation

    @property
    def user_summary_tokens(self) -> int:
        """Tokens the standing summaries occupy in the prompt, at the same reading as the count.

        The floor in the unit a reader actually needs. A count of standing summaries says the
        floor exists; this says how much of the prompt it is, which is what decides whether a
        fold would repay itself and what the boundary mode is costing against the recompacting
        one. Measured with the same annotations
        :func:`~agent_framework._compaction.included_token_count` reads, so it is a share of the
        same number the trigger is judged against.
        """
        return self._summary_tokens

    @property
    def user_folds(self) -> int:
        """Passes that collapsed every standing summary into one, which became the new boundary.

        The fold mode's whole cost, in the unit it is paid in: each fold rewrites the prompt at
        the oldest summary's position, which is just behind the head turn, so it re-bills very
        nearly the whole cached prefix once -- the same break the recompacting mode pays on
        every pass, paid here only when :meth:`_fold_due` says the standing summaries have grown
        large enough to repay it. A run that folded twice and one that never folded differ by
        two of those breaks and by a floor that was twice lowered, and no other counter
        separates them.

        A fifth outcome of a pass, beside the four :attr:`user_passes_below_trigger` lists. A
        fold is a pass whose ordinary band was not worth compacting and whose standing
        summaries were, so it is counted here and *not* under :attr:`user_passes_declined`, and
        the five still partition every pass over a non-empty conversation. A fold whose
        summarizer did not answer is a :attr:`user_summary_failures`, exactly as an ordinary
        pass's is. With :attr:`user_summaries_replayed` the six still partition every pass over
        a non-empty conversation.
        """
        return self._folds

    @property
    def user_summaries_replayed(self) -> int:
        """Passes that replaced a band with a summary already in hand, asking for nothing new.

        The live path runs one strategy at two sites on one conversation -- inside the model
        call on loaded copies, and after the turn on the store; see the module docstring -- so
        every crossing presents the same band twice, and a fold the same summaries twice. The
        first presentation is summarised and counted under :attr:`user_compactions` or
        :attr:`user_folds`; this counts the rest, which paid nothing: no summarizer call, and no
        cache break beyond the one the first presentation already paid, because the replay puts
        the identical message, under the identical id, at the identical position.

        A sixth outcome of a pass, and the one that is not a decision: the band was worth a
        pass and the pass had already been made. On a conversation driven from a list it is
        zero, because a list is one view; live it is one per crossing on the plain path and two
        on a turn that called a tool, whose second model call loads the store afresh. Read it
        beside :attr:`user_compactions`: their sum is every crossing's cost in passes.
        """
        return self._replayed

    @property
    def user_summary_failures(self) -> int:
        """Passes where the summarizer raised, returned nothing, or returned a summary no smaller
        than what it would replace, and the band was left alone.

        Non-zero means part of what this row measured is a summarizer that was not answering,
        and the row is then the uncompacted control for those passes -- reported rather than
        hidden for the reason
        :attr:`~._toolsummary.ToolResultAnchoredSummarizationCompactionStrategy.fallbacks_used`
        is: a strategy that quietly degrades into a different one produces a number belonging
        to neither.

        Counted on both failures, because they are the same event to the conversation. The
        framework's own strategy separates them only in its log line and treats an empty
        summary exactly as it treats an exception, for the same reason: what the caller has in
        either case is no replacement text, and the one thing that must not happen is
        superseding the turns anyway.
        """
        return self._failures

    @property
    def user_passes_below_trigger(self) -> int:
        """Passes that returned at the trigger check, having read nothing but the prompt's size.

        The ordinary state of a conversation that has not grown yet, and the reason it is
        counted at all is that ``user_compactions == 0`` has several causes and a reader needs
        to tell them apart from the numbers rather than from a story. This one says the
        conversation never reached the line; :attr:`user_passes_declined` says it did and the
        band was not worth a pass; :attr:`user_summary_failures` says the band was and the
        summarizer was not. Together with :attr:`user_compactions` the four partition every
        pass over a non-empty conversation -- five with :attr:`user_folds`, which is a pass the
        band declined and the standing summaries did not, and six with
        :attr:`user_summaries_replayed`, which is a pass already made on the other view of the
        same conversation -- so a row where the user half did nothing always has exactly one
        non-zero number saying why.

        On a composed row this counts two things that are both the design working: a
        conversation that had not grown yet, and one the record phase had already brought under
        the shared line on this pass. The composed row judges this half after the record phase
        on purpose -- user compaction breaks the cached prefix, so it is the second line of
        defence -- and neither case is starvation; see the ``_composed`` module docstring.
        """
        return self._below_trigger

    @property
    def user_passes_declined(self) -> int:
        """Passes over the trigger where the band was not worth what a pass costs.

        The hysteresis counter, and the price of having one. Every pass counted here is a pass
        ``min_band_share=0.0`` would have spent: a summarizer call, a rewritten prefix billed
        from the summary's position to the end of the conversation, and a few hundred tokens
        freed.

        It is one number for three refusals, because they are one statement -- there was not
        enough here to be worth a pass. The band was empty (the anchors cover the conversation,
        or after a boundary nothing but the tail is newer than it), or it held only this
        strategy's own earlier summary (nothing new has been said), or it was worth less than
        ``min_band_share`` of the prompt (something new has been said and it is not enough).
        :meth:`_worth_compacting` is where all three are written down. In the fold mode a pass
        counted here is one where the standing summaries were not worth a fold either; a pass
        where they were is a :attr:`user_folds`, not a decline.

        **Read it as a ratio against :attr:`user_compactions`, not alone.** Non-zero beside a
        non-zero compaction count is the mechanism working. Non-zero beside *zero* compactions
        is a row whose user half never acted, and the share is then too high for that workload's
        band -- which is a configuration to change, not a strategy that failed, and it is
        visible here rather than looking like a conversation that never grew.
        """
        return self._declined

    async def __call__(self, messages: list[Message]) -> bool:
        """Summarise the user band when the prompt is over the trigger and the band is worth it.

        Two conditions, and the second one is why this is not once per turn: the trigger says
        the prompt is large enough to act on, and :meth:`_worth_compacting` says this band is
        worth the pass. Each refusal increments the counter that names it, so the six outcomes
        -- under the line, declined, summarizer failed, compacted, folded, replayed -- partition
        the passes and a row that did nothing says which. The fold is reached only through a
        declined band, and only in the fold mode: see :meth:`_fold_due`.

        Nothing is mutated until the summary is in hand. That ordering is the whole of the
        "degrade safely" contract: exclusion flags and the summary's back-references are
        written in one step after the call returns text, so a summarizer that raises, times out
        or answers with whitespace leaves a conversation that is byte-identical to the one it
        was given. The alternative -- excluding first and restoring on failure -- is the
        mutate-and-roll-back that ``MinimumGainAnchoredCompactionStrategy`` already refused to
        do for its projection, on the ground that a rollback missing one field is a silent
        wrong answer rather than a loud one.

        **The two numbers a pass is judged by are read here, and a composition reads them for
        it.** The size and the line this method compares are both taken off the conversation in
        front of it, which is what a standalone strategy does. :meth:`compact_against` is the
        same pass with that pair supplied, and is how
        :class:`~._composed.ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` judges
        this half against its own shared line and the size the record phase left, rather than
        against this strategy's own trigger.

        Args:
            messages: The conversation, mutated in place.

        Returns:
            True if the outgoing messages changed. False does not imply the prompt now fits:
            this strategy has no shed step and no fallback, so a conversation whose bulk is tool
            output is one it can legitimately leave over the ceiling. Compose it with a
            strategy that sheds if that matters.
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
        self, messages: list[Message], *, prompt_tokens: int, trigger_tokens: int
    ) -> bool:
        """Run one pass, judged against a size and a line the caller read rather than this pass.

        The seam a composition needs and a row does not, and the whole of what it changes is
        *whether* the pass runs -- everything a running pass then does is read off the
        conversation as it now stands. :meth:`__call__` supplies the pair it read itself, so a
        row's behaviour is exactly what it was.

        **The hysteresis is deliberately not judged against ``prompt_tokens``.** The share
        :meth:`_worth_compacting` takes is a share of what a pass would *cost*, which is the
        prompt behind the edit as it now stands, so it is taken against a fresh count. Judging
        it against a larger stale number would make every band look like a smaller share of the
        prompt than it is and decline passes that are worth running -- which is the same
        suppression the pass-entry trigger exists to remove, arriving by the other route. The
        two counts are equal when :meth:`__call__` is the caller.

        Args:
            messages: The conversation, mutated in place. Already grouped and token-annotated
                by the caller, which is what lets the two readings differ at all.
            prompt_tokens: Included tokens the trigger is judged against.
            trigger_tokens: Included tokens the prompt must exceed for a pass to run.

        Returns:
            True if the outgoing messages changed.
        """
        if prompt_tokens <= trigger_tokens:
            self._below_trigger += 1
            return False

        # Before the band is read, so a boundary is protected before anything is decided
        # against it, and the floor is counted on every pass over the trigger. Not folded into
        # the return value: annotating a message is not a change to what the model sees.
        standing = self._observe_summaries(messages)
        band = self._band(messages, standing)
        if not self._worth_compacting(messages, band, included_token_count(messages)):
            # The band has stopped yielding. That is the signal a fold waits for and not, on
            # its own, a reason to fold: the fold has its own condition, and a pass that fails
            # both is declined and counted as one.
            if self._fold_due(messages, standing):
                return await self._fold(messages, standing)
            self._declined += 1
            return False

        transcript = _format_turns([messages[span["start_index"]] for span in band])
        remembered = self._recall(self.prompt, transcript)
        if remembered is None:
            summary = await self._summarize(transcript, prompt=self.prompt)
            if summary is None:
                return False
            # The id is numbered by compaction rather than by conversation length, which is what
            # the framework uses. Length is not unique across passes here: in the recompacting
            # mode this strategy's own summary is superseded by the next one, so a conversation
            # can be compacted at the same length twice and the second summary would claim the
            # first one's id, silently pointing every back-reference at the wrong message. Nor
            # is this instance's count unique on its own -- a conversation outlives the instance
            # that first compacted it -- so the number is lifted past whatever the conversation
            # already carries: see :func:`_next_summary_id`.
            summary_id = _next_summary_id(
                messages, prefix=SUMMARY_ID_PREFIX, minimum=self._compactions
            )
            self._remember(_Remembered(self.prompt, transcript, summary_id, summary))
        else:
            # The same band, seen again on the other list the live path runs this on. The answer
            # and the id are the ones the model has already been sent.
            summary_id, summary = remembered.summary_id, remembered.text
        replaced = [
            message
            for span in band
            for message in messages[span["start_index"] : span["end_index"] + 1]
        ]
        if not self._smaller(replaced, band, summary, summary_id=summary_id):
            # Remembered all the same, so the other list replays the refusal instead of asking.
            self._failures += remembered is None
            return False
        if remembered is None:
            self._compactions += 1
        else:
            self._replayed += 1
        self._replace(messages, band, summary, summary_id=summary_id)
        self._replaced = len(band)
        return True

    def _band(
        self, messages: list[Message], standing: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the user groups this pass may replace, oldest first.

        **User groups and nothing else.** ``group_messages`` gives every user message a group of
        its own with ``kind == "user"``, so the selection needs no heuristics: tool-call groups,
        their results and assistant narration are simply never candidates. That is what keeps
        this row independent of the tool-side rows it is meant to be compared with, and it is
        the reason the rule lives in one method rather than being spread across the caller.

        **Excluded turns are not candidates.** A message another strategy has already dropped is
        not in the prompt, so superseding it would cost a summarizer call to remove nothing --
        and it would put a message that is not being sent into this pass's replaced count, where
        it would read as compaction this strategy performed.

        **Preserved turns are not candidates either, but they do count towards the anchors.** The
        mark in :mod:`._preserve` means a message no strategy may shorten, drop or shed, and a
        superseded message is dropped in every sense that matters, so a preserved turn is never
        in the band. It stays in the count the anchors are taken over, because the anchors are
        positions in the prompt and a preserved turn is in the prompt: the first user turn is
        the head whether or not another party has marked it. Counting only the unprotected
        turns would move an anchor onto the neighbour of any preserved turn -- and after a first
        pass in the recompacting mode the head's neighbour is this strategy's own summary. That
        summary would then be held as the head on every later pass and never re-read, while a
        second summary was written beside it and recompacted in its place from then on: two
        summaries standing for the rest of the run, in the mode whose whole point is that one
        does. By the same arithmetic a preserved last turn would shift the tail onto the turn
        before it, protecting a turn that had already been answered; under the rule here the
        last turn is the tail, marked or not, which is what it is anyway.

        **The boundary rule, which is the whole difference between the modes.** In the
        recompacting mode, and on any pass with no standing summary, the band is the turns
        between the head and the tail, previous summary included -- the head is honoured on the
        first pass in every mode. In the boundary modes, once a summary stands, the band is the
        turns *newer than the newest standing summary*, less the tail: the boundary is never
        re-read, everything in front of it is already behind a boundary and out of reach by
        construction, and the head needs no second reading because the start of the
        conversation was protected on the pass that wrote the first boundary. A standing summary
        is preserved, so it is not in ``turns`` at all; the boundary is taken from ``standing``
        rather than searched for here, so that this method and :meth:`_observe_summaries` read
        one list. On an ordinary conversation the two rules select the same band -- everything
        in front of the boundary is either a head turn or superseded -- and the explicit rule
        is what keeps that true when it stops being ordinary: a head turn another strategy has
        excluded would otherwise shift the re-counted head onto the first turn after the
        boundary and protect it for the rest of the run.

        **Whether the band is worth replacing is not decided here.** This returns what a pass
        *may* touch; :meth:`_worth_compacting` decides whether a pass runs at all. The two were
        one method until the band's own size had to be weighed, and separating them is what
        keeps the selection rule readable as a selection rule -- and puts all three reasons a
        pass declines in one place, behind one counter.

        Args:
            messages: The conversation, already grouped.
            standing: The standing summaries' spans, oldest first, as
                :meth:`_observe_summaries` returned them. Empty on a first pass.

        Returns:
            The spans this pass may replace, empty when the anchors leave nothing between them.
        """
        turns = [
            span
            for span in group_messages(messages)
            if span.get("kind") == "user"
            and not messages[int(span["start_index"])].additional_properties.get(
                EXCLUDED_KEY, False
            )
        ]
        if self.recompacts_summaries or not standing:
            last = len(turns) - self.keep_tail_user_turns
            between = (
                turns[self.keep_head_user_turns : last] if last > self.keep_head_user_turns else []
            )
        else:
            boundary = int(standing[-1]["start_index"])
            newer = [span for span in turns if span["start_index"] > boundary]
            last = len(newer) - self.keep_tail_user_turns
            between = newer[:last] if last > 0 else []
        return [
            span
            for span in between
            if not any_preserved(messages[span["start_index"] : span["end_index"] + 1])
        ]

    def _worth_compacting(
        self, messages: list[Message], band: list[dict[str, Any]], prompt_tokens: int
    ) -> bool:
        """Return whether this band is worth what a pass costs.

        **The hysteresis, in the one place it is written down.** A pass spends a summarizer call
        and rewrites the prompt at the band's first position, which re-bills everything from
        there to the end of the conversation at the uncached rate. Three refusals, which are one
        statement -- there is not enough here to pay for that:

        - **An empty band.** The anchors cover the whole conversation, or every turn between
          them is preserved, so there is nothing to stand for.
        - **A band holding only this strategy's own earlier summary.** Replacing it frees
          exactly nothing: one message is rewritten at one position and the prompt is the size
          it was. This is :meth:`~._toolsummary.ToolResultRecallMiddleware._record_due`'s rule,
          re-arming on new material rather than on size. Vacuous in the boundary modes, whose
          band never holds a summary, where the same statement is the empty band above.
        - **A band worth less than ``min_band_share`` of the included prompt.** The rule the
          other two are corners of, and the one the measurement forced. The condition that fires
          a pass is the prompt's size, and the prompt does not shrink to the size of the band --
          so once the band is down to the previous summary plus a turn or two, every new turn
          makes the second rule true again while freeing a fraction of a percent. Thirty passes
          in a run, measured; see the module docstring and :data:`DEFAULT_MIN_BAND_SHARE`.

        **The band is measured, not counted.** A minimum number of new *turns* since the last
        pass would bound nothing: it divides the firing count by a constant and leaves it
        growing with the conversation. A minimum *growth of the prompt* would bound it, but on
        growth this strategy may not touch -- a run whose bulk is tool output would re-arm the
        band without adding anything to it. Tokens in the band against tokens in the prompt is
        the one measure that is both what a pass would free and what it would cost.

        The share is taken against the prompt as the trigger read it, before anything was
        removed, and the summary's own size is not deducted from the band: it is not known until
        the summarizer has answered, and it is one message against a band that has to clear a
        fifth of the prompt to get here.

        Args:
            messages: The conversation, already grouped and token-annotated.
            band: The spans :meth:`_band` selected.
            prompt_tokens: Included tokens, as the trigger check read them.

        Returns:
            True when the pass may go on to spend a summarizer call.
        """
        if not band:
            return False
        replaced = [messages[int(span["start_index"])] for span in band]
        if all(_is_summary(message) for message in replaced):
            return False
        return included_token_count(replaced) >= prompt_tokens * self.min_band_share

    def _observe_summaries(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Protect every standing summary as a boundary, and read the floor they make.

        Re-applied on every pass rather than set once, for the reason
        :func:`~._toolsummary._preserve_records` is: compaction may run against a freshly loaded
        conversation, and an annotation a previous pass wrote is not promised to be there when
        the next one starts. Every standing summary, not only the newest -- an older boundary is
        the sole surviving copy of the turns behind *it*, and the newest one does not stand for
        them.

        Only the boundary modes mark anything. In the recompacting mode the summary must stay
        an ordinary candidate, and :meth:`_band` skips a preserved turn, so marking it there
        would turn that mode into this one without anyone having asked.

        The count and the tokens come back from the same walk, so the floor reported is the
        floor that was protected and not a second reading that could disagree with it. Both are
        the state as it now stands rather than a maximum: a fold lowers them, and a peak would
        report a floor the prompt no longer carries.

        Args:
            messages: The conversation, already grouped and token-annotated. Mutated in place
                in the boundary modes, by annotation only.

        Returns:
            The standing summaries' spans, oldest first, which is what :meth:`_band` takes the
            boundary from and what a fold collapses.
        """
        standing = [
            span
            for span in group_messages(messages)
            if span.get("kind") == "user"
            and _is_standing_summary(messages[int(span["start_index"])])
        ]
        summaries = [messages[int(span["start_index"])] for span in standing]
        if not self.recompacts_summaries:
            for message in summaries:
                set_preserved(message, preserved=True, reason=PRESERVE_REASON)
        self._summaries_in_conversation = len(summaries)
        self._summary_tokens = included_token_count(summaries)
        return standing

    def _fold_due(self, messages: list[Message], standing: list[dict[str, Any]]) -> bool:
        """Return whether the standing summaries are worth collapsing into one.

        Reached only from a pass whose ordinary band was declined, which is the signal and not
        the decision: a band that has stopped yielding while the standing summaries are a
        couple of percent of the prompt is not a reason to break the whole cached prefix for a
        couple of percent. Three conditions, and all three are required.

        - **The fold mode.** The boundary mode never folds; that is what makes it the arm whose
          floor is the pure cost of never re-reading, against which the fold is measured.
        - **At least two standing summaries.** Folding one summary is a rewrite of one message
          at one position -- the recompacting mode with extra steps -- so the fold path cannot
          be reached with fewer. This is also the guard against fold thrashing: a fold leaves
          one standing summary, so the next fold cannot happen until an ordinary boundary pass
          has left a second one, and that pass needs the prompt to have grown by
          ``1 / (1 - min_band_share)`` since the last. Folds are therefore at most one per
          ordinary pass, and each of those is already bounded geometrically.
        - **The fold repays the break.** This is the anchored family's break-even,
          :data:`~._anchored.DEFAULT_MIN_GAIN_FRACTION`'s ``R > B * (p - c) / (p + T * c)``, with
          the fold's own terms and no new constant. ``R`` is what the fold would remove: the
          standing summaries' tokens less the largest of them, on the stated assumption that a
          summary of summaries is no larger than the largest piece it folds -- the neutral
          estimate is their mean, and the largest is the one that errs towards keeping, which
          is the direction this package errs in everywhere. ``B`` is what the fold would
          re-bill: the included prompt from the oldest standing summary to the end, which is
          very nearly the whole prompt, because the oldest summary sits just behind the head
          turn. ``(p - c) / (p + T * c)`` at the measured prices and conversation length is
          :data:`DEFAULT_MIN_BAND_SHARE` -- that constant's docstring
          carries the derivation -- so the condition is ``R >= B * min_band_share``, and a
          caller who moves the share moves both decisions together. The formula fits this
          decision exactly, since a fold is a single edit at one position re-billing everything
          behind it once and saving its removal on every turn after; what it says is that a
          fold pays only once the summaries are a tenth of what is behind them, which on a
          long conversation with small summaries is rarely, and that is the finding rather than
          a defect.

        What the threshold does not deduct is the replacement's real size, because it is not
        known until the summarizer has answered; the largest-summary deduction is the stated
        stand-in for it.

        Args:
            messages: The conversation, already grouped and token-annotated.
            standing: The standing summaries' spans, as :meth:`_observe_summaries` returned them.

        Returns:
            True when a fold may go on to spend a summarizer call.
        """
        if self.summary_mode != SUMMARY_MODE_FOLD or len(standing) < 2:
            return False
        sizes = [included_token_count([messages[span["start_index"]]]) for span in standing]
        removable = sum(sizes) - max(sizes)
        behind = included_token_count(messages[int(standing[0]["start_index"]) :])
        return removable >= behind * self.min_band_share

    async def _fold(self, messages: list[Message], standing: list[dict[str, Any]]) -> bool:
        """Collapse every standing summary into one, which becomes the new boundary.

        The mechanics are :meth:`_replace`'s, applied to summaries instead of turns: the fold's
        output is inserted where the oldest summary stood, carries the ids of every summary and
        group it folds, and each folded summary carries the fold's id back and is excluded with
        :data:`FOLD_EXCLUDE_REASON`. The messages between the standing summaries -- assistant
        replies, tool groups, the record half's preserved record on a composed row -- are not
        read, not moved relative to each other and not excluded; the fold touches the summaries
        it collapses and inserts one message, exactly as an ordinary pass inserts one. The
        fold's output is a summary by :func:`_is_summary`'s test and is marked preserved by the
        observation :meth:`_replace` ends with, so the next pass's band starts after it and the
        cycle continues.

        The folded summaries' preservation is released before they are excluded. The mark
        promised that no strategy would drop them; the strategy that made the promise is the
        one superseding them, and the replacement carries the promise forward. Releasing it
        keeps "preserved" and "included" meaning one thing on a conversation read back.

        Nothing is mutated until the summary is in hand, on the same contract as an ordinary
        pass: a summarizer that raises or answers with nothing leaves the conversation as it
        was and is counted under :attr:`user_summary_failures`. And a fold presented again on
        the other list the live path runs this on is replayed, as an ordinary pass is: see
        :meth:`_recall`.

        Args:
            messages: The conversation, mutated in place.
            standing: The standing summaries' spans, oldest first, at least two of them.

        Returns:
            True if the outgoing messages changed.
        """
        answer = await self._fold_answer(messages, standing)
        if answer is None:
            return False
        remembered, replayed = answer
        summaries: list[Message] = [messages[span["start_index"]] for span in standing]
        if not self._smaller(
            summaries, standing, remembered.text, summary_id=remembered.summary_id
        ):
            self._failures += not replayed
            return False
        self._apply_fold(messages, standing, remembered, replayed)
        return True

    async def fold_if_smaller(self, messages: list[Message]) -> FoldOutcome:
        """Collapse every standing summary into one, but only into one smaller than they are.

        The composed row's seam, and not a pass of this strategy's own: nothing here reads the
        trigger, the band or :meth:`_fold_due`, because the caller has already decided that the
        prompt is over its ceiling and a fold is the next thing to try. What it shares with a
        fold-mode pass is everything else -- the request, the replay on the second list, the id,
        the replacement and the counters -- through :meth:`_fold_answer` and :meth:`_apply_fold`,
        which :meth:`_fold` is built from too.

        **The one rule added is generic on purpose.** The answer is kept only if it is shorter,
        in tokens, than the standing summaries it would replace; otherwise nothing is mutated
        and the caller moves on. Nothing is checked against the summaries' content: a fold
        judged by overlap with what it folds would be a fold tuned to one workload's wording,
        and what it loses is a benchmark's to measure, not the strategy's to guess at.

        Args:
            messages: The conversation, mutated in place when the fold is taken. Already grouped
                and token-annotated.

        Returns:
            ``"folded"`` when the summaries were collapsed, ``"rejected"`` when the answer was no
            smaller than them, ``"failed"`` when the summarizer gave none -- counted under
            :attr:`user_summary_failures` like any other -- and ``"skipped"`` when fewer than
            two summaries stand, which is the rewrite-of-one the fold mode refuses as well.
        """
        standing = self._observe_summaries(messages)
        if len(standing) < 2:
            return "skipped"
        answer = await self._fold_answer(messages, standing)
        if answer is None:
            return "failed"
        remembered, replayed = answer
        summaries: list[Message] = [messages[span["start_index"]] for span in standing]
        if not self._smaller(
            summaries, standing, remembered.text, summary_id=remembered.summary_id
        ):
            return "rejected"
        self._apply_fold(messages, standing, remembered, replayed)
        self._kept_folds[remembered.transcript] = remembered
        return "folded"

    def refold(self, messages: list[Message]) -> bool:
        """Put back a fold :meth:`fold_if_smaller` kept, wherever the summaries it replaced stand.

        The composed row's seam for keeping its chain's fold consistent across the live path's
        two lists. That chain runs only while the prompt is over its budget, and the two lists
        can disagree about that: a fold kept on a call's copies was sent to the model, and if
        the store's pass is under the budget, the store keeps the unfolded summaries and the
        next call is sent them again. So a kept fold is remembered for the run, by the summaries'
        transcript -- which both lists present identically -- and put back here, on any list,
        whatever its size, with the same text under the same id. Nothing is asked and nothing is
        judged: the fold was judged smaller when it was kept, and the same summaries give the
        same sizes. A fold never kept is not put back, and :meth:`fold_if_smaller` stays the only
        way to make one. Counted as a replay, as a fold replayed on the second list always was.

        Args:
            messages: The conversation, mutated in place. Already grouped and token-annotated.

        Returns:
            True if a fold was put back.
        """
        if not self._kept_folds:
            return False
        standing = self._observe_summaries(messages)
        if len(standing) < 2:
            return False
        transcript = _format_turns(
            [messages[span["start_index"]] for span in standing], text=_summary_body
        )
        kept = self._kept_folds.get(transcript)
        if kept is None:
            return False
        self._apply_fold(messages, standing, kept, True)
        return True

    async def _fold_answer(
        self, messages: list[Message], standing: list[dict[str, Any]]
    ) -> tuple[_Remembered, bool] | None:
        """Return the fold's answer and whether it was replayed, or None when there is none.

        Asks the summarizer only when this exact request was not the last one made; the answer
        and the id it will carry are remembered either way, so the other list the live path
        runs on gets the same text under the same id. A failure is counted under
        :attr:`user_summary_failures` by :meth:`_summarize`.

        Args:
            messages: The conversation, read for the ids it already carries.
            standing: The standing summaries' spans, oldest first.

        Returns:
            The remembered request with its answer, and True when it was replayed rather than
            asked; None when the summarizer gave nothing.
        """
        transcript = _format_turns(
            [messages[span["start_index"]] for span in standing], text=_summary_body
        )
        remembered = self._recall(self.fold_prompt, transcript)
        if remembered is not None:
            return remembered, True
        summary = await self._summarize(transcript, prompt=self.fold_prompt)
        if summary is None:
            return None
        summary_id = _next_summary_id(messages, prefix=FOLD_ID_PREFIX, minimum=self._folds)
        remembered = _Remembered(self.fold_prompt, transcript, summary_id, summary)
        self._remember(remembered)
        return remembered, False

    def _smaller(
        self,
        replaced: list[Message],
        spans: list[dict[str, Any]],
        summary: str,
        *,
        summary_id: str,
    ) -> bool:
        """Return whether ``summary``, as inserted, counts fewer tokens than what it replaces.

        A summarizer can answer at length, and a summary no smaller than its band would grow the
        prompt while the pass reported success.
        """
        candidate = [self._summary_message(replaced, spans, summary, summary_id=summary_id)]
        annotate_token_counts(candidate, tokenizer=self.tokenizer, force_retokenize=True)
        return included_token_count(candidate) < included_token_count(replaced)

    def _apply_fold(
        self,
        messages: list[Message],
        standing: list[dict[str, Any]],
        answer: _Remembered,
        replayed: bool,
    ) -> None:
        """Put the fold's answer in place of the standing summaries, and count it.

        Counted here rather than when the answer arrived, so that an answer a caller refuses
        -- :meth:`fold_if_smaller`'s ``"rejected"`` -- is neither a fold nor a replay.

        Args:
            messages: The conversation, mutated in place.
            standing: The standing summaries' spans, oldest first.
            answer: The remembered request and its answer, from :meth:`_fold_answer`.
            replayed: Whether the answer was replayed rather than asked for.
        """
        if replayed:
            self._replayed += 1
        else:
            self._folds += 1
        folded: list[Message] = [messages[span["start_index"]] for span in standing]
        for message in folded:
            set_preserved(message, preserved=False)
        self._replace(
            messages,
            standing,
            answer.text,
            summary_id=answer.summary_id,
            reason=FOLD_EXCLUDE_REASON,
        )

    def _recall(self, prompt: str, transcript: str) -> _Remembered | None:
        """Return the remembered answer to exactly this request, or None if it was never asked.

        Byte-identical is the test, on the prompt and the transcript both: a fold's request
        differs from a band's in the prompt alone, a band that has gained one turn differs in
        the transcript alone, and neither may borrow the other's answer.

        Args:
            prompt: The system prompt the request would carry.
            transcript: The numbered turns it would carry.

        Returns:
            The remembered request when it matches, else None.
        """
        for remembered in self._remembered:
            if remembered.prompt == prompt and remembered.transcript == transcript:
                return remembered
        return None

    def _remember(self, request: _Remembered) -> None:
        """Keep ``request`` for replay, dropping the oldest beyond :attr:`remembered_requests`.

        Args:
            request: The request just answered.
        """
        self._remembered = [*self._remembered, request][-self.remembered_requests :]

    async def _summarize(self, transcript: str, *, prompt: str) -> str | None:
        """Return the summary of ``transcript``, or None when the summarizer did not produce one.

        Args:
            transcript: The numbered turns, as :func:`_format_turns` renders them.

        Keyword Args:
            prompt: What to ask for: :attr:`prompt` for a band, :attr:`fold_prompt` for a fold.

        Returns:
            The summary text, stripped, or None on either failure.
        """
        try:
            response = await self.client.get_response(
                [
                    Message(role="system", contents=[prompt]),
                    Message(role="user", contents=[transcript]),
                ],
                stream=False,
            )
        except Exception as error:
            # Broad on purpose, and the framework's own summarizing strategy is broad in the
            # same place. A compaction strategy runs inside the chat client's own call path, so
            # anything this does not catch fails the user's turn -- which trades a conversation
            # that is merely too long for one that does not happen.
            logger.warning("Skipping user-turn compaction: summary generation failed (%s).", error)
            self._failures += 1
            return None
        summary = response.text.strip() if response.text else ""
        if not summary:
            logger.warning("Skipping user-turn compaction: the summarizer returned no text.")
            self._failures += 1
            return None
        return summary

    @staticmethod
    def _summary_message(
        replaced: list[Message], band: list[dict[str, Any]], summary: str, *, summary_id: str
    ) -> Message:
        """Return the message a summary is inserted as, linked to what it stands for.

        One constructor for the message :meth:`_replace` inserts and the one
        :meth:`fold_if_smaller` measures before deciding, so the size a fold is judged at is the
        size it would have.

        Args:
            replaced: The messages the summary stands for, oldest first.
            band: Their spans.
            summary: The summarizer's text.

        Keyword Args:
            summary_id: The ``message_id`` the summary carries.

        Returns:
            The summary message, carrying its source links.
        """
        return Message(
            role="user",
            contents=[f"{USER_SUMMARY_MARKER}\n{summary}"],
            message_id=summary_id,
            additional_properties={
                GROUP_ANNOTATION_KEY: {
                    SUMMARY_OF_MESSAGE_IDS_KEY: [
                        message.message_id for message in replaced if message.message_id
                    ],
                    SUMMARY_OF_GROUP_IDS_KEY: [str(span["group_id"]) for span in band],
                }
            },
        )

    def _replace(
        self,
        messages: list[Message],
        band: list[dict[str, Any]],
        summary: str,
        *,
        summary_id: str,
        reason: str = EXCLUDE_REASON,
    ) -> None:
        """Put one user message in place of the band, linked to what it supersedes.

        Every step here is ``SummarizationStrategy``'s, performed in its order: the summary
        carries the ids of the messages and the groups it stands for, each superseded message
        carries the summary's id back, the supersession is recorded as an exclusion with a
        reason, the message is inserted at the first index it replaced, and the groups are
        re-annotated from there. Matching it is worth more than tidiness -- a caller reading a
        compacted conversation back, or the framework's own summary reconciliation, finds the
        annotations it already knows how to follow rather than a second convention that means
        the same thing.

        The insertion index is taken from the band's own first span rather than by searching for
        the message, because messages compare by value: two user turns with the same text are
        equal, and a search would find the earlier one and insert the summary in front of a turn
        the head was protecting.

        The token counts are re-annotated from the insertion as well, and not only the groups.
        The next pass would do exactly this on entry -- the inserted summary is the first
        untokenized message, and the framework's incremental annotation runs from there to the
        end -- so the cost is moved rather than added; what it buys is that the observation this
        ends with counts the summary just inserted, so the floor a run leaves is on the counters
        whether or not a later pass reads it.

        The counters a pass moves are the caller's business, because two callers share this: an
        ordinary pass numbers its summary by compaction and reports the turns it replaced, and a
        fold numbers by fold and reports nothing about turns. What both leave behind is one
        message that :func:`_is_summary` recognises, which is the one property every mode reads.

        Args:
            messages: The conversation, mutated in place.
            band: The spans being replaced, oldest first.
            summary: The summarizer's text.

        Keyword Args:
            summary_id: The ``message_id`` the replacement carries and the superseded messages
                point back to.
            reason: Recorded on each superseded message with its exclusion.
        """
        replaced: list[Message] = [messages[span["start_index"]] for span in band]
        summary_message = self._summary_message(replaced, band, summary, summary_id=summary_id)
        for message in replaced:
            _mark_summarized_by(message, summary_id)
            set_excluded(message, excluded=True, reason=reason)
        insertion_index = int(band[0]["start_index"])
        messages.insert(insertion_index, summary_message)
        # Groups first, then tokens: annotate_token_counts re-annotates the groups from the same
        # index before it counts, so this is the framework's own order.
        annotate_message_groups(messages, from_index=insertion_index, force_reannotate=False)
        annotate_token_counts(messages, tokenizer=self.tokenizer, from_index=insertion_index)
        self._observe_summaries(messages)
