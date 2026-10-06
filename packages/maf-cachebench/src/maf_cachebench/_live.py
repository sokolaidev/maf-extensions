"""Drive the recall scenario through a real agent instead of a scripted replay.

The replay harness preserves scripted content and structure across providers, with a
fixed-width salt isolating each cell's cache prefix. It scripts the assistant's replies, and a
scripted reply is not what an agent accumulates: real replies carry information, vary in
length, and are themselves candidates for eviction. Replay also cannot produce a genuine
tool-calling loop, so a turn is always exactly one model call.

This module uses live replies to capture those effects. The scenario's user turns go to a
real ``Agent`` with a real tool, and the model writes its own replies into the history that
compaction then acts on. The consequence is that live numbers are **within-model only**:
two models write different replies, so their histories diverge from the first turn and
cannot be placed side by side the way replayed ones can.

There is a second consequence, and it is the interesting one. Because each strategy's
history contains that strategy's own replies, a strategy that compacts badly produces a
worse reply, which becomes worse history, which it compacts again. Replay cannot show that
compounding at all; here it is the thing being measured.

Compaction is wired the way ``create_harness_agent`` wires it, which is not the obvious
way. A ``CompactionProvider``'s ``before_strategy`` is a no-op under per-service-call
history persistence: the agent skips ``HistoryProvider.before_run``, so the provider only
ever sees an empty context. The before phase has to travel as the agent's
``compaction_strategy`` instead, which runs per model call inside the client. Only the
after phase belongs on the provider.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from agent_framework import (
    Agent,
    AgentSession,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    CompactionProvider,
    HistoryProvider,
    InMemoryHistoryProvider,
    Message,
    create_harness_agent,
)

# Exported by ``agent_framework._compaction.__all__`` but not re-exported at package level.
# A chat middleware cannot otherwise see what compaction actually kept.
from agent_framework._compaction import project_included_messages
from maf_compaction import (
    DEFAULT_RECORD_MAX_TOKENS,
    DEFAULT_RECORD_TARGET_TOKENS,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    find_nested_strategy,
    find_record_index,
    make_recall_tool,
)
from maf_compaction import record_text as _record_text

from ._metrics import clamp_cached_tokens, serialize_message
from ._recall import (
    COMBINED_SCOPE,
    FactOutcome,
    RecallScenario,
    build_recall_scenario,
    render_code,
    render_codes,
    score_answer,
    score_scoped,
)
from ._runner import is_connection_error, is_rate_limited, retry_after_seconds, unsupported_option
from ._strategies import StrategyOptions, build_strategy
from ._tokenizers import stamp_reasoning_tokens
from ._transcripts import TRUE_CHARS_PER_TOKEN, sized_text

if TYPE_CHECKING:
    from agent_framework import CompactionStrategy, TokenizerProtocol

    from ._providers import ProviderRuntime

__all__ = [
    "AGENT_KINDS",
    "COMPACTION_GUIDANCE",
    "CONNECTION_ATTEMPTS",
    "CONNECTION_BASE_DELAY",
    "CONNECTION_MAX_DELAY",
    "CONNECTION_MAX_WAIT",
    "DEFAULT_COMBINED_REPEATS",
    "DEFAULT_PROBE_REPEATS",
    "DEFAULT_TOOL_RESULT_TOKENS",
    "NEUTRAL_INSTRUCTIONS",
    "RATE_LIMIT_ATTEMPTS",
    "RATE_LIMIT_BASE_DELAY",
    "RATE_LIMIT_MAX_DELAY",
    "RATE_LIMIT_MAX_WAIT",
    "RETRIEVAL_GUIDANCE",
    "RETRY_JITTER",
    "TERSE_INSTRUCTIONS",
    "IdentifiedHistoryProvider",
    "LiveOutcome",
    "MeteredClient",
    "ModelCall",
    "SummarizerUsage",
    "ProbeOutcome",
    "UsageRecorder",
    "build_live_agent",
    "build_live_scenario",
    "find_nested_strategy",
    "make_lookup_tool",
    "make_scope_tools",
    "probe_count",
    "recall_record_text",
    "resolve_instructions",
    "restore_state",
    "run_live",
    "score_combined_samples",
    "score_samples",
    "serialize_history",
    "snapshot_state",
    "unretrieved_facts",
    "wants_client_side_history",
]

#: How the agent under test is assembled.
#:
#: ``plain`` builds the smallest agent that still exercises compaction: history, tools and
#: the strategy, nothing else. ``harness`` builds the real ``create_harness_agent``, which
#: is what production code calls, at the cost of adding its own tools and system prompt to
#: every measured prompt.
AGENT_KINDS: Final[tuple[str, ...]] = ("plain", "harness")

#: How many times each closing question is put to the same snapshot.
#:
#: Three, because accuracy here is often two-valued and one reading of it is a draw rather
#: than a measurement: the same strategy scored 52, 52, 52 and 22 on four runs that preserved
#: exactly the same 27 facts. Asking repeatedly against material that cannot have changed is
#: what separates the model's own enumeration variance from compaction's.
DEFAULT_PROBE_REPEATS: Final[int] = 3

#: How many times the one combined question is put to the same snapshot.
#:
#: Its own count, independent of :data:`DEFAULT_PROBE_REPEATS`, because the two accuracy
#: measures are averages over different numbers of questions. ``acc1`` averages every scoped
#: question per repeat -- seven of them in the cells recorded so far, one per tool lookup plus
#: the requirements -- while ``acc2`` is one question, so at one probe repeat it was a single
#: sample per seed against seven, which is why it was the noisier of the two. The runs that
#: matter use ``--probe-repeats 1``, the per-scope repeat spread having measured 0 to 2
#: points, and this keeps the combined question sampled while that is true.
#:
#: Five rather than three, because three was measured to be too few. Asked of a byte-identical
#: restored snapshot the combined question is close to pass or fail: one attempt in fifteen
#: collapsed from 100% to 21% on the uncompacted control, and ``rep2+-`` read 12 to 16 points
#: on four of six rows. Two runs of one cell reported the control at 37% and at 95%. Nothing
#: about the context differs between those attempts, so the sampling has to absorb it.
DEFAULT_COMBINED_REPEATS: Final[int] = 5

#: Default size of each tool result, in tokens. Set high on purpose: in a real agent
#: trace tool output is usually the bulk of the context, and a benchmark whose tool
#: results are a rounding error cannot say anything about tool-oriented compaction.
DEFAULT_TOOL_RESULT_TOKENS: Final[int] = 4_000

#: Attempts one call makes against provider throttling before the turn is failed.
#:
#: Six, which with the schedule below spans about two minutes of waiting and, under the
#: bounds, at most five. The deployment these runs are made against is a 200,000 TPM /
#: 200 RPM quota, and a quota window refills on a fixed period rather than gradually, so the
#: question a retry asks is only ever "has the next window started". Five minutes covers
#: several of them, which is far more than a spike and short enough that a quota that is
#: genuinely exhausted fails its turn in minutes rather than absorbing hours of a sweep.
#:
#: Not free to raise: every attempt re-sends the whole prompt, and at the sizes measured here
#: that is 50,000 to 230,000 tokens the provider will charge for if it accepts it.
#:
#: Ten, because a provider may answer every 429 with a one-second ``Retry-After`` while the
#: minute's quota stays spent, and a smaller count then runs out in seconds of a 300-second
#: allowance. A refused call is not billed, so the count guards no money; with the backoff a
#: floor under the requested wait (see ``_retry_delay``), ten attempts reach the 300-second
#: budget, which is the bound that is meant to decide.
RATE_LIMIT_ATTEMPTS: Final[int] = 10

#: First backoff in seconds, doubled per attempt: 2, 4, 8, 16, 32.
#:
#: Small on purpose. Most 429s here are one call arriving inside a window another call has
#: just filled, and the window is seconds wide; starting at a minute would turn a two-second
#: problem into a two-minute one on every occurrence.
RATE_LIMIT_BASE_DELAY: Final[float] = 2.0

#: Ceiling on any single wait, in seconds, including one the provider asked for.
#:
#: One minute, because the quota window is one minute. Waiting longer than the window cannot
#: buy more headroom than waiting for the window, and a provider that answers ``Retry-After:
#: 3600`` -- which is what a daily cap looks like -- should fail the turn honestly rather
#: than silently park a sweep for an hour.
RATE_LIMIT_MAX_DELAY: Final[float] = 60.0

#: Ceiling on the total one turn may spend waiting, in seconds.
#:
#: Bounded per turn rather than per run: a long sweep that meets throttling on many turns
#: should survive all of them, but no single turn should be able to stall indefinitely by
#: being handed a large delay repeatedly. The seconds are counted and reported either way, so
#: a run that spent its wall clock here says so instead of looking merely slow.
RATE_LIMIT_MAX_WAIT: Final[float] = 300.0

#: Attempts one call makes against a dropped connection before the turn is failed.
#:
#: Five, and they are the operative bound: the schedule below spends about 15 seconds over the
#: four re-sends, well inside the wait budget, so what ends a turn is running out of attempts
#: rather than running out of clock. A cell of 30 seed records was lost outright to
#: ``APIConnectionError`` with every row ``ERR`` and no turns completed, and three cells of an
#: earlier sweep went the same way, so the first fifteen seconds of a network blip are the
#: whole point of this.
#:
#: Cheaper to spend than the throttling attempts above: a request the transport never delivered
#: is a request the provider never billed. That is an argument for retrying promptly, not for
#: retrying forever -- a 5xx counts as transient here too, and one of those may well have been
#: billed for the work it failed at.
CONNECTION_ATTEMPTS: Final[int] = 5

#: First backoff in seconds, doubled per attempt: 1, 2, 4, 8.
#:
#: Half the rate limit's, because the two are asking different questions. A 429 waits for a
#: quota window to refill, which happens on a fixed period and cannot be hurried; a dropped
#: socket has no such structure, and the honest question -- is the path back -- can be asked
#: as soon as a reconnect could plausibly have succeeded, which is under a second.
CONNECTION_BASE_DELAY: Final[float] = 1.0

#: Ceiling on any single wait, in seconds, including one the provider asked for.
#:
#: Twenty rather than the rate limit's sixty, for the same reason the base is smaller: sixty
#: is the length of the quota window, and there is no equivalent unit here to wait out. A path
#: still down after twenty seconds is an outage rather than a blip, and the remaining attempts
#: should establish that quickly instead of parking the sweep on it.
CONNECTION_MAX_DELAY: Final[float] = 20.0

#: Ceiling on the total one turn may spend waiting out connection failures, in seconds.
#:
#: A minute, against the rate limit's five. Five minutes is several quota windows; a minute is
#: already four reconnection attempts, and anything that survives it is not transient. The
#: budget binds only when the provider names its own waits -- a 503 answering ``Retry-After``
#: at the 20-second cap three times over exhausts it before the attempts run out.
CONNECTION_MAX_WAIT: Final[float] = 60.0

#: Share of each computed wait that is randomised away, so waits land between 75% and 100%.
#:
#: The quota is per deployment, not per process, and it is shared with whatever else is
#: running against the same account. A fixed schedule makes two throttled clients re-collide
#: on every attempt; this is enough to break that without making the schedule unreadable. The
#: connection schedule is jittered by the same fraction and for the same reason: a gateway
#: coming back up is met by everything that was talking to it when it went down.
#: Not applied to a delay the provider asked for, which is an instruction rather than a guess.
RETRY_JITTER: Final[float] = 0.25

#: Source of the jitter above. Not a security control; see ``_retry_delay``.
_JITTER_SOURCE: Final[random.SystemRandom] = random.SystemRandom()

TERSE_INSTRUCTIONS: Final[str] = (
    "You are a meticulous engineering assistant. Follow every stated requirement exactly. "
    "When the user asks for a deployment lookup, call the matching tool. "
    "Acknowledge each tool result in three words or fewer, and do not restate its values in "
    "that acknowledgement. This applies only to acknowledgements: when you are asked for the "
    "final report, include every value you were asked for, in full."
)
"""Instructions that stop the model narrating tool results back into the conversation.

Separates a strategy's contribution from the model's. When the model restates every value, a
strategy can discard the tool results entirely and still appear to preserve them. The
final-report exemption is load-bearing: without it the model applies the rule to its answer
too, and the recall score measures the instruction rather than the compaction.
"""

COMPACTION_GUIDANCE: Final[str] = (
    "Earlier tool results may have been shortened, summarised or replaced by a compaction "
    "record. Treat values in such a record as authoritative for the tool it names, and treat "
    "information as absent only if it appears nowhere, including there."
)
"""How to read what compaction left behind.

Given to *every* row, including the uncompacted control, which is what keeps it a measurement
device rather than an advantage for the strategies that leave artefacts. Without it a strategy
that removed the original results is scored by a question naming a tool whose result it
deleted, against an instruction inviting the answer "no longer present" -- so the score partly
measures whether the model thought to look at the record rather than whether the record
preserved anything.

Legitimate by the same test applied to the narration guidance: it changes *whether the model
looks*, not *where the information is*, and cannot resurrect a value the record does not
contain. It is inert for the control, which has no artefacts to interpret, and that asymmetry
belongs in any report of the numbers.
"""

RETRIEVAL_GUIDANCE: Final[str] = (
    "When asked for codes or identifiers, quote them exactly as they appear earlier in this "
    "conversation, and list every one you are asked for. If a value is not present in the "
    "conversation, say so plainly for that item instead of guessing or inventing one. "
    + COMPACTION_GUIDANCE
)
"""The retrieval clause, isolated so a run can measure what it is worth.

Kept separate from the instructions it is appended to because it is the only sentence
suspected of holding the closing answer stable, and a suspicion that cannot be switched off
cannot be tested.
"""

NEUTRAL_INSTRUCTIONS: Final[str] = (
    "You are a meticulous engineering assistant. Follow every stated requirement exactly. "
    + RETRIEVAL_GUIDANCE
)
"""Agent instructions that say nothing about narrating tool results.

Paired with ``narration="neutral"`` and the harness, this is the configuration a typical
caller gets: the framework's own ``DEFAULT_HARNESS_INSTRUCTIONS`` are then the only thing
telling the model to explain what it learned between tool calls.

The retrieval guidance is deliberate, and it is a different kind of instruction from the
narration guidance. Narration changes *where the information is*, copying tool output into
assistant prose, which lets a strategy delete the original and still appear lossless.
Retrieval guidance only changes *whether the model looks* for what is already there; it
cannot resurrect a fact compaction removed. Only the first kind can mask damage.

It is not, however, what made the closing answer stable. That was the reply cap. Measured on
the uncompacted control asking for all 53 codes at once: without this clause a 900-token cap
scored 33% with 36 facts present but unlisted, and raising the cap to 4,000 scored 100% with
no clause at all. The guidance had been compensating for a truncated answer by pushing codes
ahead of prose. At an adequate cap it changes nothing here, and it is kept for continuity with
the runs already measured rather than because it is doing work.

The "say so plainly" clause guards the other direction: a model that invents a plausible
code would score as recall without the fact ever being in context.
"""

_INSTRUCTIONS: Final[str] = (
    "You are a meticulous engineering assistant. Follow every stated requirement exactly. "
    "When the user asks you to look up deployment facts, call the lookup_deployment tool. "
    "Keep replies short unless asked otherwise."
)


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One model call, as the middleware observed it.

    A turn is not a call. A turn that triggers a tool produces at least two, each billed
    separately against a different prompt, so cost has to be summed per call rather than
    per turn.
    """

    messages_sent: int
    """Messages actually sent, after compaction removed what it removed."""
    prompt_text: str
    messages_before_compaction: int
    """Messages the history held when the call started, before compaction ran."""
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cache_reported: bool = True

    @property
    def fresh_tokens(self) -> int:
        """Input tokens that were not served from the provider's cache."""
        return max(self.input_tokens - self.cached_tokens, 0)


class UsageRecorder(ChatMiddleware):
    """Record what each model call actually sent, and what it was billed for.

    Read after history loading and compaction, then project included messages to recover
    the prompt actually sent.
    """

    def __init__(self) -> None:
        """Create a recorder holding no calls."""
        self.calls: list[ModelCall] = []

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        """Run the call, then read back what compaction left of the prompt.

        Args:
            context: The chat invocation being wrapped.
            call_next: Continuation that performs the call.
        """
        await call_next()
        # Read after the call, not before: the history middleware replaces this attribute
        # with the loaded history, and compaction then flags that list in place.
        outgoing = list(context.messages)
        sent = project_included_messages(outgoing)
        # UsageDetails is a TypedDict, so it is read with .get() rather than getattr:
        # attribute access on it silently yields None and reports every call as free.
        usage: dict[str, Any] = dict(getattr(context.result, "usage_details", None) or {})
        # The provider reports the reasoning it billed on the response's usage, and the content
        # that carries the encrypted payload says nothing about its size. Stamping the count
        # here is what lets ReasoningStampTokenizer charge the replayed reasoning what the
        # provider charges for it rather than the zero the framework counts an opaque payload
        # at: without it the local count is low by the decrypted reasoning in the prompt,
        # which on a reasoning model is not a rounding error.
        # Stamped after the call, on the messages the session goes on to persist, so the next pass
        # over this conversation sees it.
        if (reasoning_tokens := usage.get("reasoning_output_token_count")) and isinstance(
            context.result, ChatResponse
        ):
            stamp_reasoning_tokens(context.result.messages, int(reasoning_tokens))
        self.calls.append(
            ModelCall(
                messages_sent=len(sent),
                prompt_text="\n".join(serialize_message(message) for message in sent),
                messages_before_compaction=len(outgoing),
                input_tokens=usage.get("input_token_count") or 0,
                cached_tokens=clamp_cached_tokens(
                    usage.get("input_token_count") or 0, usage.get("cache_read_input_token_count")
                ),
                output_tokens=usage.get("output_token_count") or 0,
                cache_reported=usage.get("cache_read_input_token_count") is not None,
            )
        )


@dataclass(frozen=True, slots=True)
class SummarizerUsage:
    """Usage from one summarizer request, retained for per-request pricing tiers."""

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cache_reported: bool = True


class MeteredClient:
    """Wrap a chat client so calls made outside the agent are still counted.

    This is a proxy rather than a chat client: it forwards everything it does not record,
    and only ``get_response`` is intercepted, because that is the only call
    ``SummarizationStrategy`` makes. Structurally satisfying the client protocol would mean
    reproducing its four overloads for no benefit, so callers cast instead.

    ``SummarizationStrategy`` calls its client directly, so those calls never reach the
    agent's middleware. Left unmetered, the one strategy that spends extra money to do its
    job would be scored as though it were free, which is the same mistake as pricing an
    absent cache-read rate at zero.

    Failures matter as much as tokens. ``SummarizationStrategy`` catches its own errors,
    logs a warning and returns ``False``, so a broken summarizer produces a run with no
    compaction at all and therefore a *perfect* recall score. Counting failures here is
    what stops that being read as a win.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap a client.

        Args:
            inner: The client to delegate to.
        """
        self.inner = inner
        self.calls = 0
        self.failures = 0
        self.usage: list[SummarizerUsage] = []
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0

    @property
    def additional_properties(self) -> dict[str, Any]:
        """Delegate to the wrapped client, which the chat-client protocol requires."""
        properties: dict[str, Any] = getattr(self.inner, "additional_properties", {})
        return properties

    def __getattr__(self, name: str) -> Any:
        """Forward everything not overridden here to the wrapped client.

        Returns:
            The wrapped client's attribute.
        """
        return getattr(self.inner, name)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate to the wrapped client, recording usage and failures.

        Returns:
            Whatever the wrapped client returned.

        Raises:
            Exception: Whatever the wrapped client raised, after counting it.
        """
        self.calls += 1
        try:
            response = await self.inner.get_response(*args, **kwargs)
        except Exception:
            self.failures += 1
            raise
        usage: dict[str, Any] = dict(getattr(response, "usage_details", None) or {})
        self.usage.append(
            SummarizerUsage(
                input_tokens=usage.get("input_token_count") or 0,
                cached_tokens=clamp_cached_tokens(
                    usage.get("input_token_count") or 0, usage.get("cache_read_input_token_count")
                ),
                output_tokens=usage.get("output_token_count") or 0,
                cache_reported=usage.get("cache_read_input_token_count") is not None,
            )
        )
        self.input_tokens += usage.get("input_token_count") or 0
        self.cached_tokens += self.usage[-1].cached_tokens
        self.output_tokens += usage.get("output_token_count") or 0
        return response


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One closing question, asked once, from the snapshot.

    A probe is not a turn. Each one starts from a restored copy of the seeded conversation,
    so no probe's answer can reach another probe's context and no probe is asked from a
    context an earlier probe has already changed. That is the whole reason this type exists
    separately from the seeding turns.
    """

    scope: str
    """Which closing question this is, matching :attr:`RecallScenario.answer_scopes`."""
    question: str
    repeat: int
    """1-based index within this question's repeats, so a sample can be assembled across
    questions: repeat *n* of every question is one independent reading of the snapshot."""
    answer: str
    prompt_text: str
    """Serialized prompts of the calls this probe made, projected through compaction.

    Recorded per probe rather than summed with the rest, because the property this design
    exists to guarantee -- that no probe's answer appears in another probe's prompt -- is
    only checkable if the prompts are kept apart.
    """
    calls: tuple[ModelCall, ...]


@dataclass(frozen=True, slots=True)
class LiveOutcome:
    """Everything one live strategy run produced."""

    strategy: str
    calls: tuple[ModelCall, ...]
    """Every model call the run made, seeding and probes alike, in order.

    One sequence rather than two, because cost is one number: the seeding spend plus what
    each probe added. Splitting it invites a table that reports the cheap half.
    """
    answer: str
    snapshot_prompt: str
    """The seeded conversation as compaction left it, serialized.

    Survival is judged against this and nothing else. Judging it against a closing prompt is
    circular once several questions have been asked in sequence: each answer re-lists codes
    into the history as assistant text, so a code compaction destroyed reappears because the
    model recited it two questions ago. The same strategy read 53/53 on a run that emitted
    10,941 output tokens and 18/53 on one that emitted 4,873.
    """
    tool_calls_made: int
    turns_completed: int
    turns_total: int
    dropped_options: tuple[str, ...] = ()
    """Request options the provider rejected, dropped so the run could continue.

    A run that dropped ``tool_choice`` is not comparable with one that kept it: the model
    picked its own tool calls, so it gathered its own set of facts.
    """
    scopes_called: tuple[str, ...] = ()
    """Tool scopes the agent actually asked for.

    A fact the agent never fetched never entered the history, so compaction cannot have
    evicted it. Without this the uncompacted control reports losing facts to compaction,
    which is not a thing that can happen.
    """
    summarizer_calls: int = 0
    summarizer_failures: int = 0
    #: Free-form notes a strategy chose to report about its own run, shown in the flags
    #: column. A strategy that can silently degrade into a different one has to say so:
    #: this package has twice read a row that scored well for having done nothing.
    strategy_notes: tuple[str, ...] = ()
    groups_kept_uncovered: int = 0
    """Tool groups the recall record never mentioned, so the strategy declined to drop them.

    The same number ``strategy_notes`` carries as a flag, kept here as an integer as well
    because a flag is read and a column is measured. Non-zero says a row's cost is the price
    of a partial record rather than of the design working, and a mean over seeds cannot be
    taken on a string. Zero on every strategy that keeps no such count, which is all of them
    but ``tool_summary_anchored`` and the composed ``tool_and_user_summary_anchored`` that
    runs it as a phase -- the count is read off whichever of the two the row installed.
    """
    fallbacks_after_record: int = 0
    """Compaction passes where a record existed and the strategy fell back regardless.

    Reported apart from the ``FALLBACK`` count for the reason that count is reported at all:
    a strategy that degrades into another one produces a number belonging to neither, and
    this is the half of that degradation nobody could see. The pre-record fallback means the
    model never wrote a record; this means it wrote one that did not free enough, and the
    fallback then shortened the tool results still in the prompt -- the very groups a partial
    record left behind. Non-zero says part of this row measures the fallback strategy.

    Zero on every strategy that keeps no such count, which is all of them but
    ``tool_summary_anchored`` and the composed row that runs it as a phase.
    """
    fallbacks_held_after_record: int = 0
    """Post-record fallback passes that ran with tool groups no record covers held out of reach.

    The rule that closes what ``fallbacks_after_record`` beside ``groups_preserved_uncovered``
    leaves open: a tool group *after* the record is covered by no record, and a fallback free to
    shorten it would do so until its facts were gone while the preserved groups in front kept
    the prompt near the ceiling. Every such group is held before the fallback runs, so it may
    take narration and nothing else. Counts
    attempts: non-zero says the fallback was needed and held back, zero says it never had to
    act. ``RECHELD:<n>`` on ``strategy_notes`` carries the same number.

    Zero on every strategy that keeps no such count, which is all of them but
    ``tool_summary_anchored`` and the composed row that runs it as a phase.
    """
    reforced_calls: int = 0
    """Forced calls the recall middleware made at the strategy's request, for uncovered groups.

    Layer one of the answer to the loss ``groups_kept_uncovered`` beside
    ``fallbacks_after_record`` would otherwise permit: a record that failed to cover a group is
    asked for again, while the group is still whole and only while asking helps, before the
    fallback
    is allowed near it. Read it with ``groups_preserved_uncovered``: this without that is the
    re-force fixing the shortfall, this with that is the re-force failing and layer two standing
    in. Zero on every strategy that takes no record, and zero on a record row whose records
    were complete, which is what keeps the ``REFORCED:<n>`` flag readable.
    """
    groups_preserved_uncovered: int = 0
    """Uncovered tool groups the strategy has preserved for good, as the conversation last stood.

    Layer two. Non-zero says asking stopped helping -- the re-forced record covered none of
    them, or none came -- and these groups now sit in the prompt unshrinkable and undroppable,
    which is what keeps the fallback off them and is also a floor under the prompt that may
    put the row over the ceiling. A row carrying this beside ``DQ`` failed loudly where a
    fallback free to shorten the group would have lost its values quietly, and that is the
    intended reading. The ``PRESERVED:<n>`` flag on
    ``strategy_notes`` carries the same number; this is the column a cell can be meaned on.

    Zero on every strategy that keeps no such count, which is all of them but
    ``tool_summary_anchored`` and the composed row that runs it as a phase.
    """
    records_in_conversation: int = 0
    """Records the conversation ended up carrying, at the most the strategy saw it hold.

    Every record is a floor under the prompt: preserved, so it can be neither shortened nor
    dropped, and nothing merges them. Reported as a number beside the ``RECORDS`` flag for the
    reason ``groups_kept_uncovered`` is -- a flag says a row is affected, a number can be meaned
    over the seeds of a cell and asked how far the floor rose.

    Read it against ``FORCED``, not against one. A record per forced call is the mechanism
    working, however many times the middleware asked; more records than asks is a defect, and
    was one -- every trigger event wrote two, because the middleware re-decided on the exit of
    the call it had pinned, where the record it asked for is not yet in the loaded history.
    Runs taken before that was fixed carry the doubling, and ``FORCED:2, RECFORCED:1`` in the
    archived flags is what it looks like.

    Zero on every strategy that keeps no such count, which is all of them but
    ``tool_summary_anchored`` and the composed row that runs it as a phase.
    """
    user_compactions: int = 0
    """Passes where ``user_summary_anchored`` replaced a band of user turns with a summary.

    Zero says that row never acted, which for a strategy whose whole subject is how much of a
    conversation is user-side is the difference between a measurement and the control under
    another name. Non-zero is also the price: every pass rewrites the prefix at its own edit and
    the provider re-reads everything behind it, so two passes are two of those.

    Zero on every strategy that keeps no such count, which is all of them but
    ``user_summary_anchored`` and the composed row that runs it as a phase.
    """
    user_messages_replaced: int = 0
    """User turns the most recent such compaction superseded.

    Beside the count above for the reason ``groups_kept_uncovered`` sits beside its flag: this
    is the size of what the surviving summary stands for, and it is what makes the ``snap%``
    column attributable -- a row that compacted once and replaced seventy turns is a different
    finding from one that compacted seven times and replaced ten.

    Zero on every strategy that keeps no such count, which is all of them but
    ``user_summary_anchored`` and the composed row that runs it as a phase.
    """
    user_summaries_in_conversation: int = 0
    """Summaries ``user_summary_anchored`` left standing in the conversation, as it last stood.

    The boundary mode's floor: every standing summary there is preserved and only a fold merges
    them, so N passes leave N of these, each one a floor under the prompt that no later pass can
    lower. One in the recompacting mode after any pass. Reported as a number beside the
    ``USERSUMMARIES`` flag for the reason ``records_in_conversation`` is -- a flag says a row is
    affected, a number can be meaned over the seeds of a cell.

    Zero on every strategy that keeps no such count, which is all of them but
    ``user_summary_anchored`` and the composed row that runs it as a phase.
    """
    user_summary_tokens: int = 0
    """Tokens those standing summaries occupied at the same reading: the floor, in tokens.

    Beside the ``USERSUMMTOKENS`` flag, and zero on every strategy that keeps no such count.
    """
    user_folds: int = 0
    """Passes where ``user_summary_anchored`` collapsed every standing summary into one.

    The fold mode's cost: each one re-bills the prompt from the oldest summary's position, which
    is very nearly the whole of it. Beside the ``USERFOLD`` flag, and zero on every strategy
    that keeps no such count.
    """
    records_merged: int = 0
    """Passes where ``tool_and_user_summary_anchored``'s last-resort chain merged its records.

    Step a of the chain, kept because the merge came back smaller than the records it replaced.
    ``RECMERGE`` in ``strategy_notes``. This and the six fields after it are that row's chain,
    and zero on every other strategy: they say how far down the chain a row went.
    """
    record_merges_rejected: int = 0
    """Record merges discarded because they came back no smaller. ``RECMERGEREJ``."""
    user_summaries_merged: int = 0
    """Passes where the chain folded the standing user summaries into one: step b. ``USERMERGE``."""
    user_merges_rejected: int = 0
    """User-summary folds the chain discarded as no smaller. ``USERMERGEREJ``."""
    record_rewrites: int = 0
    """Harder rewrites of the record the chain tried, kept or not: step c. ``RECHARDER``."""
    record_rewrites_rejected: int = 0
    """Of those, the rewrites discarded because they came back no smaller. ``RECHARDERREJ``."""
    record_rewrites_skipped: int = 0
    """Rewrite attempts not made, because the same record had already been refused at them.

    Step c skipped rather than paid for: a record refused as no smaller is not asked for again
    until it changes. Neither tried nor refused, so not in ``record_rewrites``. ``RECHARDERSKIP``.
    """
    last_resort_fallbacks: int = 0
    """Passes on which the chain reached its fallback, step d, having tried everything above it.

    Counts the fallback being run, where ``fallbacks_after_record`` counts it changing something.
    ``LASTFALLBACK`` in ``strategy_notes``; beside ``DQ`` it is step e, the intended loud failure.
    """
    user_passes_waited: int = 0
    """Passes where the composed strategy held its user half back for a record that was due.

    The composed row's user half does not act while the record half has tool work a record is
    due for and the record still has time to arrive, because the record half compacts in two
    steps and the user half in one: judged on the pass that only *asked* for the record, the
    user half would act first and, by taking the prompt under the line, stop the record ever
    being asked for. Per pass, as ``USERUNDER`` is, so one wait on the live path reads several.
    Read it beside ``RECORDS``, which grows when the wait ended in a record, and beside
    ``USERCOMPACT``, which moves when it ran out or the record was not enough. ``USERWAIT`` in
    ``strategy_notes``; zero on every other strategy.
    """
    record_text: str = ""
    """The recall record the run produced, exactly as the model wrote it.

    Read back out of the finished conversation rather than captured while it was made, so
    nothing about the prompts, the token accounting or the cost depends on whether anyone
    wants to look at it. Empty when the run took no record, which is every strategy but
    ``tool_summary_anchored`` and the composed row that runs it, and any run of either where
    the model never complied.

    Here because the counter above says only *how many* groups a record failed to cover, and
    the question that follows is always what the record actually said. Nothing reads this by
    default: ``--dump-record`` writes it out for a human, and everything else ignores it.
    """
    #: Every probe, in the order they were asked: each question in turn, each asked as many
    #: times as its own scope calls for.
    probes: tuple[ProbeOutcome, ...] = ()
    probe_repeats: int = 1
    """How many times each per-scope closing question was asked.

    Reported rather than assumed, because it is the denominator of the within-seed spread:
    one repeat measures nothing about the model's own enumeration variance.
    """
    combined_repeats: int = 1
    """How many times the combined question was asked.

    Separate from ``probe_repeats`` because the two accuracy measures average over different
    numbers of questions: every scoped question together makes one ``acc1`` reading, and this
    one question is the whole of an ``acc2`` reading. One is the default here rather than three,
    so that an outcome assembled without the probe phase reads as the single attempt it is.
    """
    context_drift: int = 0
    """Probes whose prompt was not the snapshot verbatim.

    Restoring the snapshot stops compaction *accumulating* across the probes; it does not stop
    the strategy running once more on the restored state, and a strategy that then evicts or
    rewrites something has been asked its question from slightly less than the snapshot.
    Survival is scored against the snapshot, so those probes would be credited with facts the
    model was not shown. Counted rather than assumed away: it is zero whenever the strategy is
    already at rest by the end of seeding, which is the usual case and not one to rely on.
    """
    rate_limit_retries: int = 0
    """Calls re-sent after the provider refused them for rate reasons.

    Counted because a throttled run is not the same measurement as an unthrottled one even
    when every number above it matches. Each retry re-sends the whole prompt after a wait, and
    a prompt cache that expired during the wait is a miss the hit-rate column would otherwise
    charge to compaction.
    """
    throttled_seconds: float = 0.0
    """Seconds this run spent waiting out those refusals.

    The count alone does not say whether the run was inconvenienced or shaped by throttling:
    six retries of two seconds and six of a minute are different runs. This is the one that
    is comparable with the run's wall clock.
    """
    connection_retries: int = 0
    """Calls re-sent because the request never came back with an answer.

    Counted apart from the throttled ones because the two say different things about the run,
    and folding them together would give every row the worse reading of whichever it met. A
    throttled call waited out a quota window, which is a minute wide and long enough for the
    prompt cache to expire underneath it, so a throttled row's hit rate is suspect. A
    reconnected one waited seconds and re-sent the same prefix, so its cache is very likely
    intact and its numbers are the numbers.
    """
    connection_seconds: float = 0.0
    """Seconds this run spent waiting for the provider to answer again.

    Beside the count for the reason the throttled seconds are: four re-sends over eight seconds
    and four over a minute are different runs, and only this one is comparable with the wall
    clock a sweep was measured against.
    """
    seed_prompt_tokens: int = 0
    """Billed size of the last prompt the seeding phase sent.

    The achieved fill, against which the analytic sizing is checked. Taken from the last
    seeding call rather than from a probe, because a probe's prompt carries its question too.
    """
    summarizer_usage: tuple[SummarizerUsage, ...] = ()
    error: str | None = None
    replies: tuple[str, ...] = field(default_factory=tuple[str, ...])

    @property
    def cache_reported(self) -> bool:
        """Whether every billed call reported cache usage, including the summarizer."""
        return all(call.cache_reported for call in (*self.calls, *self.summarizer_usage))

    @property
    def summarizer_input_tokens(self) -> int:
        """Input tokens billed by the summarizer."""
        return sum(call.input_tokens for call in self.summarizer_usage)

    @property
    def summarizer_output_tokens(self) -> int:
        """Output tokens billed by the summarizer."""
        return sum(call.output_tokens for call in self.summarizer_usage)

    @property
    def input_tokens(self) -> int:
        """Input tokens billed across every model call."""
        return sum(call.input_tokens for call in self.calls)

    @property
    def cached_tokens(self) -> int:
        """Input tokens served from cache across every model call."""
        return sum(call.cached_tokens for call in self.calls)

    @property
    def output_tokens(self) -> int:
        """Output tokens billed across every model call."""
        return sum(call.output_tokens for call in self.calls)

    @property
    def probe_input_tokens(self) -> int:
        """Input tokens the probe phase billed.

        Summed off the probes themselves rather than derived from the last prompt. Every probe
        carries the same snapshot, but not at the same price: the first one warms a prefix the
        rest read back, so twelve probes at the final prompt's size is a different number from
        what the twelve of them were, and the difference is the whole cache discount.

        Whatever a failed probe spent is *not* here, because a probe that never answered
        produced no :class:`ProbeOutcome`. That spend lands in the seeding half by subtraction,
        which is the conservative direction: the workload is charged for it rather than the
        instrument, so no strategy is credited with a saving it did not make.
        """
        return sum(call.input_tokens for probe in self.probes for call in probe.calls)

    @property
    def probe_cached_tokens(self) -> int:
        """Input tokens of the probe phase that were served from the provider's cache."""
        return sum(call.cached_tokens for probe in self.probes for call in probe.calls)

    @property
    def probe_output_tokens(self) -> int:
        """Output tokens the probe phase billed: the answers, which nothing else reads."""
        return sum(call.output_tokens for probe in self.probes for call in probe.calls)

    @property
    def probe_input_samples(self) -> tuple[int, ...]:
        """Input tokens each probe billed, one entry per probe in the order they were asked.

        The totals above say what the probe phase cost; these say how it divided among the
        probes, which is a different question and the one the totals cannot answer. A probe
        phase whose cached total is four times one probe's prompt is four probes served whole
        and eight served cold, or twelve served a third each, and only the per-probe figures
        can tell those apart. The order is the order ``probes`` holds: each question in turn,
        each asked its own number of times, the combined question last.
        """
        return tuple(sum(call.input_tokens for call in probe.calls) for probe in self.probes)

    @property
    def probe_cached_samples(self) -> tuple[int, ...]:
        """Input tokens each probe was served from the provider's cache, in the same order."""
        return tuple(sum(call.cached_tokens for call in probe.calls) for probe in self.probes)

    @property
    def messages_left(self) -> int:
        """Messages in a probe's prompt: the snapshot as compaction left it, plus the question.

        Stable across probes by construction, since each one is asked from a restored copy of
        the same snapshot. Before the snapshot existed this was the last of a chain of closing
        turns and drifted downwards through the scoring.
        """
        return self.calls[-1].messages_sent if self.calls else 0

    @property
    def messages_peak(self) -> int:
        """Largest the history ever got, before compaction was applied to it.

        Measured pre-compaction on purpose. The post-compaction peak only says how hard a
        strategy trimmed; this says how much there was to trim, which is the denominator
        that makes ``messages_left`` mean anything.
        """
        return max((call.messages_before_compaction for call in self.calls), default=0)

    @property
    def prompt_tokens_final(self) -> int:
        """Billed size of the last prompt sent.

        Measured in tokens rather than messages because message counts are blind to
        strategies that rewrite content in place. ``ToolResultCompactionStrategy`` collapses
        tool results into summaries without excluding anything, so it leaves the message
        count untouched while removing real tokens — by the message count alone it looks
        like it did nothing at all.
        """
        return self.calls[-1].input_tokens if self.calls else 0

    @property
    def prompt_tokens_peak(self) -> int:
        """Billed size of the largest prompt any single call carried."""
        return max((call.input_tokens for call in self.calls), default=0)

    @property
    def messages_dropped(self) -> int:
        """Messages the final call's compaction removed from the history."""
        return (
            max(self.calls[-1].messages_before_compaction - self.calls[-1].messages_sent, 0)
            if self.calls
            else 0
        )

    @property
    def reply_tokens_in_history(self) -> int:
        """Output tokens the model wrote that then became history for later turns.

        The quantity replay cannot produce. Every one of these is a token some later turn
        had to pay to resend, or that compaction had to decide whether to keep.
        """
        return sum(call.output_tokens for call in self.calls[:-1]) if len(self.calls) > 1 else 0

    def disqualified(self, tried_limit: int) -> bool:
        """Whether any call sent a prompt larger than the context limit this run stands in for.

        The limit is simulated. The model under test accepts 272,000 tokens, so a 60,000-token
        cell means nothing unless our own code refuses what a 60,000-token model would have
        refused: the uncompacted control at 60,000 ran at 78,003 tokens and was ranked anyway,
        which made every "+18% versus not compacting" at that size a comparison against a
        baseline no 60,000-token model could have produced.

        Measured on billed prompt size, so a provider that reports no usage can never be
        policed by this. That case is visible in the table anyway, because its cost is zero.

        Args:
            tried_limit: The context limit this cell is standing in for.

        Returns:
            True when at least one call's prompt was over the limit.
        """
        return self.prompt_tokens_peak > tried_limit

    def sample(self, repeat: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return one independent reading of the snapshot: repeat ``repeat`` of every question.

        A sample, not a run. Every probe in it was answered from the same restored snapshot, so
        the samples differ only in what the model chose to write -- which is the point of
        having more than one.

        Args:
            repeat: 1-based repeat index.

        Returns:
            The scopes asked, and the answers given, in the order the questions were put.
        """
        chosen = [probe for probe in self.probes if probe.repeat == repeat]
        return tuple(probe.scope for probe in chosen), tuple(probe.answer for probe in chosen)


def _strategy_notes(strategy: Any) -> tuple[str, ...]:
    """Return what a strategy reports about its own run, if it reports anything.

    Read by duck typing rather than by isinstance so a strategy from outside this package can
    surface its own diagnostics without the runner knowing about it. Only counts that are
    non-zero are reported, so a clean run adds no noise to the flags column.

    Args:
        strategy: The strategy that was installed, or None for the control.

    Returns:
        Short tokens for the flags column.
    """
    notes: list[str] = []
    for attribute, label in (
        ("records_found", "REC"),
        ("records_in_conversation", "RECORDS"),
        ("fallbacks_used", "FALLBACK"),
        ("fallbacks_after_record", "RECFALLBACK"),
        ("fallbacks_held_after_record", "RECHELD"),
        ("forced_calls", "FORCED"),
        ("reforced_calls", "REFORCED"),
        ("records_forced", "RECFORCED"),
        ("records_volunteered", "RECVOLUNTEERED"),
        ("records_truncated", "TRUNCATED"),
        ("groups_kept_uncovered", "UNCOVERED"),
        ("groups_preserved_uncovered", "PRESERVED"),
        ("declined_collapses", "NOGAIN"),
        ("user_compactions", "USERCOMPACT"),
        ("user_messages_replaced", "USERREPLACED"),
        ("user_summaries_replayed", "USERREPLAY"),
        ("user_passes_below_trigger", "USERUNDER"),
        ("user_passes_declined", "USERHELD"),
        ("user_passes_waited", "USERWAIT"),
        ("user_summary_failures", "USERSUMMFAIL"),
        ("user_summaries_in_conversation", "USERSUMMARIES"),
        ("user_summary_tokens", "USERSUMMTOKENS"),
        ("user_folds", "USERFOLD"),
        ("records_merged", "RECMERGE"),
        ("record_merges_rejected", "RECMERGEREJ"),
        ("user_summaries_merged", "USERMERGE"),
        ("user_merges_rejected", "USERMERGEREJ"),
        ("record_rewrites", "RECHARDER"),
        ("record_rewrites_rejected", "RECHARDERREJ"),
        ("record_rewrites_skipped", "RECHARDERSKIP"),
        ("record_merges_skipped", "RECMERGESKIP"),
        ("record_summary_failures", "RECSUMMFAIL"),
        ("last_resort_fallbacks", "LASTFALLBACK"),
        ("chain_targets_reached", "CHAINTARGET"),
        ("chain_targets_missed", "CHAINSHORT"),
        ("chain_decisions_kept", "CHAINKEPT"),
    ):
        value = getattr(strategy, attribute, None)
        if isinstance(value, int) and value:
            notes.append(f"{label}:{value}")
    return tuple(notes)


def _count(strategy: Any, attribute: str) -> int:
    """Return an integer counter a strategy reports, or zero when it keeps no such count.

    Args:
        strategy: The strategy that was installed, or None for the control.
        attribute: The counter's name.

    Returns:
        The count.
    """
    value = getattr(strategy, attribute, 0)
    return value if isinstance(value, int) else 0


def wants_client_side_history(client: Any, *, allow_server_history: bool = False) -> bool:
    """Return whether ``store=False`` must be forced so compaction can act.

    Clients on the Responses API keep the conversation server-side. When they do, MAF skips
    ``HistoryProvider.before_run`` entirely -- the comment in the framework is explicit that
    "the service owns loading; the providers are write-only sinks" -- and the agent sends
    only the new turn. A compaction strategy then has nothing to compact, and every setting
    silently measures the same thing.

    Measured on Foundry before this was forced: a 16-turn conversation reported a one-message
    prompt on every row, while the service billed 82,708 input tokens for history the client
    never sent.

    Args:
        client: The chat client under test.

    Keyword Args:
        allow_server_history: Leave the service in charge, accepting that no compaction runs.

    Returns:
        True when ``store=False`` should be forced.
    """
    return bool(getattr(client, "STORES_BY_DEFAULT", False)) and not allow_server_history


def make_lookup_tool(
    lookups: Mapping[str, tuple[str, ...]],
    filler_tokens: int = DEFAULT_TOOL_RESULT_TOKENS,
) -> Callable[[str], str]:
    """Build the tool a live agent calls to obtain the planted tool-result facts.

    The replayed transcript scripts these values into a tool-result message. Live, the model
    has to ask for them, so the returned function must hand back the same markers or the two
    modes would be scoring different conversations.

    Args:
        lookups: Scope label mapped to the verifiable codes it carries.
        filler_tokens: Approximate size of each result, **in tokens**. This is the only thing
            that decides how much context tool output occupies, and therefore whether
            tool-oriented compaction has anything worth evicting. At the ~76 tokens a
            600-character default produced, six results came to under 2% of a 28,000-token
            prompt and ``tool_result`` could move only 1.2% of it.

    Returns:
        A callable suitable for passing to ``Agent(tools=...)``.
    """
    # Distinct text per scope: six identical results would share a prefix and let unrelated
    # messages match by accident, inflating measured cache reuse.
    bodies = {
        scope: sized_text(
            f"[{scope} deployment notes] ", index * 31 + 7, filler_tokens, TRUE_CHARS_PER_TOKEN
        )
        for index, scope in enumerate(sorted(lookups))
    }

    def lookup_deployment(scope: str) -> str:
        """Look up the deployment facts for one scope of the system.

        Args:
            scope: Which deployment to look up, such as "early", "mid" or "late".

        Returns:
            The region code and fallback host for that scope.
        """
        key = scope.strip().casefold()
        entry = lookups.get(key)
        if entry is None:
            return f"Unknown scope {scope!r}. Valid scopes are: {', '.join(sorted(lookups))}."
        return (
            f"{render_codes(entry)}; all of these values must appear in the final report. "
            f"{bodies[key]}"
        )

    return lookup_deployment


def _scope_tool(scope: str, result: str) -> Callable[[], str]:
    """Return one no-argument tool that hands back a fixed result.

    A closure rather than a default argument. Capturing the result as ``def tool(_result=...)``
    puts it in the function signature, and the framework turns the signature into the tool
    schema -- so the whole result body would be advertised to the model as a parameter it
    could set.

    Args:
        scope: The deployment scope this tool reports on.
        result: The text to return.

    Returns:
        A zero-argument callable named ``lookup_<scope>``.
    """

    def tool() -> str:
        return result

    tool.__name__ = f"lookup_{scope}"
    tool.__doc__ = (
        f"Look up the deployment facts for the {scope} deployment."
        + chr(10)
        + chr(10)
        + "Returns:"
        + chr(10)
        + f"    The region code and fallback host for the {scope} deployment."
        + chr(10)
    )
    return tool


def make_scope_tools(
    lookups: Mapping[str, tuple[str, ...]],
    filler_tokens: int = DEFAULT_TOOL_RESULT_TOKENS,
    narration: str = "prompted",
    placement: str = "spread",
) -> list[Callable[[], str]]:
    """Build one no-argument tool per scope, so the wrong scope cannot be requested.

    A single ``lookup_deployment(scope)`` tool leaves the choice of scope to the model, and
    ``tool_choice="required"`` cannot constrain an argument -- only which function is called.
    Measured on one model: forcing a call raised tool use from 4 to 7 calls per run but it
    still reached only 3 of 6 scopes, calling one twice and skipping another. Splitting the
    tool per scope makes ``required_function_name`` sufficient to pin exactly which fact the
    turn gathers.

    Args:
        lookups: Scope label mapped to the verifiable codes it carries.
        filler_tokens: Approximate size of each result, in tokens.
        narration: Whether the result text asks the model to restate its values.
        placement: ``"spread"`` distributes the codes on their own labelled lines;
            ``"buried"`` distributes them inline in prose, where finding them is itself part
            of the task; ``"head"`` puts them all at the front, which lets a head-truncating
            strategy preserve every fact for free.

    Returns:
        One callable per scope, named ``lookup_<scope>``.
    """
    tools: list[Callable[[], str]] = []
    for index, scope in enumerate(sorted(lookups)):
        codes = lookups[scope]
        body = sized_text(
            f"[{scope} deployment notes] ", index * 31 + 7, filler_tokens, TRUE_CHARS_PER_TOKEN
        )
        # This trailing instruction is what makes the model restate the values in its own
        # reply. Dropping it leaves the facts only in the tool result.
        preamble = (
            ""
            if narration != "prompted"
            else "all of these values must appear in the final report. "
        )
        if placement == "head":
            result = f"{render_codes(codes)}; {preamble}{body}"
        else:
            result = f"{preamble}{_spread_codes(codes, body, labelled=placement == 'spread')}"
        tools.append(_scope_tool(scope, result))
    return tools


def _spread_codes(codes: Sequence[str], body: str, *, labelled: bool) -> str:
    """Distribute labelled codes evenly through a tool result instead of heading it.

    Placement decides what a size-reducing strategy can destroy.
    ``ToolResultCompactionStrategy`` head-truncates a collapsed result at 4,096 characters,
    so codes sitting at the front survive that cut unconditionally no matter how large the
    result is. That flatters every tool-oriented strategy, and the flattery grows with the
    result size: at 25,200 tokens a head-placed code set is 0.6% of the text and 100% of the
    scored content.

    Spreading them makes the result behave like a real one, where the useful line is as
    likely to be in the middle as at the top.

    Args:
        codes: The verifiable codes this tool result carries.
        body: Filler text to distribute them through.

    Keyword Args:
        labelled: Give each code its own line with a ``[record N]`` prefix. Unlabelled, the
            codes go inline in running prose, which is a materially harder task: two
            independent controls read the first code of each tool result and none of the
            other seven, scoring exactly 11 of 53 both times.

    Returns:
        The body with one code inserted before each of ``len(codes)`` evenly spaced
        segments, at a word boundary so no code is glued to a partial word.
    """
    if not codes:
        return body
    words = body.split(" ")
    # One segment per code, so the first code stays near the front and the last sits near the
    # end. Anything less even would leave a head-heavy result and reproduce the problem.
    step = max(len(words) // len(codes), 1)
    parts: list[str] = []
    for index, code in enumerate(codes):
        start = index * step
        end = (index + 1) * step if index + 1 < len(codes) else len(words)
        segment = " ".join(words[start:end])
        if labelled:
            parts.append(f"\n[record {index + 1}] {render_code(index, code)}\n{segment}")
        else:
            parts.append(f"{render_code(index, code)}; {segment} ")
    return "".join(parts)


class IdentifiedHistoryProvider(InMemoryHistoryProvider):
    """Issue every stored message an id, so the control keeps the conversation it ran.

    ``filter_new_messages`` identifies a message by its ``message_id`` and falls back to
    ``(role, serialized contents)`` when there is none. Compaction assigns ids to everything it
    annotates, so a row carrying a strategy is always identified the first way. The control has
    no strategy and therefore no ids, and is identified the second way -- so its byte-identical
    replies to filler turns collide and the later ones are dropped from the stored history.

    Measured at 120,000/0.86: the control peaked at 82 messages where every strategy row peaked
    at 109 on the same turn list, and ``anchored``, which planned nothing at that cell, ended
    with a snapshot 5.4% larger than the baseline it was supposed to equal. Two rows that both
    did nothing are not the same conversation, so every ``vs none`` figure taken then is biased
    in the control's favour.

    Fixed here rather than in the framework because the framework's behaviour is the contract
    and not the defect: an application whose messages carry ids gets exact identity, and one
    whose messages do not gets a content hash that cannot tell a repeated turn from a resent
    one. This makes the benchmark the first kind of application on every row instead of only
    on the rows a strategy happened to annotate.

    The ids are issued at the point the history receives a message, which is the last moment
    before identity is decided and the only one every row passes through. They are never sent
    to the provider -- :func:`serialize_message` excludes them -- so no prompt, token count or
    cache prefix moves.
    """

    def __init__(self) -> None:
        """Create the provider, with the base class's defaults and a counter of its own.

        No parameters, because the benchmark wants exactly one configuration of this and taking the
        base class's six would be an option surface nothing chooses from.
        """
        super().__init__()
        self._issued = 0

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stamp anything that arrives without an id, then store it the usual way.

        The counter only ever climbs, and deliberately: a turn re-sent after a restore is a
        different message from the one the failed attempt produced, and the state it is stored
        into no longer holds that one. Reusing the id would be the only way to make the two
        collide again.

        Args:
            session_id: The session these messages belong to.
            messages: The messages to persist.

        Keyword Args:
            state: Provider-scoped session state.
            kwargs: Passed through.
        """
        for message in messages:
            if not message.message_id:
                self._issued += 1
                message.message_id = f"cachebench_{self._issued}"
        await super().save_messages(session_id, messages, state=state, **kwargs)


def build_live_agent(
    runtime: ProviderRuntime,
    *,
    kind: str,
    strategy: CompactionStrategy | None,
    tokenizer: TokenizerProtocol,
    tools: Sequence[Callable[..., Any]],
    recorder: UsageRecorder,
    extra_middleware: Sequence[Any] = (),
    instructions: str = _INSTRUCTIONS,
    max_context_window_tokens: int,
    max_output_tokens: int,
) -> Agent[Any]:
    """Assemble the agent under test, with compaction wired the way the harness wires it.

    Args:
        runtime: The provider's client, model and per-request options.

    Keyword Args:
        kind: One of :data:`AGENT_KINDS`.
        strategy: The compaction strategy, or ``None`` for the uncompacted control.
        tokenizer: Token counter shared with the strategy.
        tools: Tools the agent may call.
        recorder: Middleware capturing prompts and usage.
        extra_middleware: Further middleware a strategy needs, such as the one that forces the
            recall call. Installed after the recorder so the recorder still sees every call.
        instructions: System instructions for the agent.
        max_context_window_tokens: Window the harness variant sizes its default against.
        max_output_tokens: Output reservation.

    Returns:
        The configured agent.

    Raises:
        ValueError: If ``kind`` is not a known agent kind.
    """
    if kind not in AGENT_KINDS:
        raise ValueError(f"Unknown agent kind {kind!r}. Available: {', '.join(AGENT_KINDS)}")

    # Built before the branch and handed to both kinds, because the defect it exists to
    # prevent is not a property of either: whichever agent the cell is measured on, the
    # control is the row whose replies repeat and so the row that loses them.
    history = IdentifiedHistoryProvider()
    if kind == "harness":
        # The harness resolves both phases itself from the strategies handed in, so it gets
        # the same object twice. Its optional providers are switched off: each adds tools
        # and system-prompt text to every measured prompt, which would inflate every cell
        # and move the trigger points without saying anything about compaction.
        return create_harness_agent(
            runtime.client,
            name="cachebench",
            agent_instructions=instructions,
            tools=list(tools),
            max_context_window_tokens=max_context_window_tokens,
            max_output_tokens=max_output_tokens,
            history_provider=history,
            disable_compaction=strategy is None,
            before_compaction_strategy=strategy,
            after_compaction_strategy=strategy,
            tokenizer=tokenizer,
            disable_todo=True,
            disable_mode=True,
            disable_file_memory=True,
            disable_web_search=True,
            middleware=[recorder, *extra_middleware],
            # Deliberately empty: every option travels per turn instead. An option baked
            # in here cannot be dropped when a provider rejects it without rebuilding the
            # agent, which would discard the session the conversation lives in.
            default_options={},
        )

    providers: list[Any] = [history]
    if strategy is not None:
        # before_strategy is deliberately None: on a provider it would never run. The
        # before phase travels as the agent's compaction_strategy below.
        providers.append(
            CompactionProvider(
                before_strategy=None,
                after_strategy=strategy,
                tokenizer=tokenizer,
                history_source_id=history.source_id,
            )
        )
    return Agent(
        client=runtime.client,
        name="cachebench",
        instructions=instructions,
        tools=list(tools),
        context_providers=providers,
        compaction_strategy=strategy,
        require_per_service_call_history_persistence=True,
        middleware=[recorder, *extra_middleware],
        default_options={},
    )


def _turn_text(messages: Sequence[Message]) -> str:
    """Flatten a scenario turn's request messages into the text to send."""
    return "\n".join(
        text
        for message in messages
        for content in message.contents
        if (text := getattr(content, "text", None))
    )


def _stored_messages(agent: Agent[Any], state: Mapping[str, Any]) -> list[Message]:
    """Return every message a session state holds, exclusions included.

    The history is not at a fixed key. A ``HistoryProvider`` is handed
    ``state[provider.source_id]`` and stores its messages under ``"messages"`` inside that,
    and the harness installs its own provider instance rather than the one the plain agent
    builds. Reading a hard-coded key therefore reports an empty conversation for one of the
    two agent kinds, which reads as a strategy that deleted everything.

    Args:
        agent: The agent whose providers say where the history lives.
        state: Session state, live or snapshotted.

    Returns:
        The stored list, copied so that a caller cannot append to the session's own.
    """
    for provider in agent.context_providers:
        if isinstance(provider, HistoryProvider):
            stored: Mapping[str, Any] = state.get(provider.source_id) or {}
            return list(stored.get("messages", []))
    return []


def serialize_history(agent: Agent[Any], state: Mapping[str, Any]) -> str:
    """Return the conversation a session state holds, as compaction left it.

    The stored list is not the prompt. ``InMemoryHistoryProvider`` keeps excluded messages in
    state so that a strategy can still reconsider them, so serializing it without projecting
    reports that every strategy preserved every fact. That was measured, and it is why the
    projection here is not optional.

    Args:
        agent: The agent whose providers say where the history lives.
        state: Session state, live or snapshotted.

    Returns:
        The included messages, serialized the same way a recorded prompt is, so the two can
        be compared directly.
    """
    messages = _stored_messages(agent, state)
    return chr(10).join(
        serialize_message(message) for message in project_included_messages(messages)
    )


def recall_record_text(agent: Agent[Any], state: Mapping[str, Any]) -> str:
    """Return the newest recall record a session's history holds, as the model wrote it.

    A diagnostic, and only a diagnostic. It reads the finished conversation and writes
    nothing back, so whether anyone calls it makes no difference to the prompts that were
    sent, the tokens they were billed at, or what the run cost. That property is the whole
    reason it reads the history afterwards instead of the strategy capturing the text while
    it works: a capture is state threaded through the object under measurement, and this
    package has already had one measurement moved by an instrument it installed.

    Read off the *stored* messages rather than the projected ones. A projection would make "the
    model wrote no record" and "compaction removed the record" the same empty string, and those
    are opposite findings. The composed row does now exclude records, when its last-resort chain
    merges or rewrites them, and :func:`find_record_index` then answers with the replacement --
    the newest record still being sent -- so on that row this is the text the chain kept, which
    is the record the probes were answered from. That replacement is an ordinary assistant
    message rather than a tool result, and is read through the same function every record reader
    uses, so neither form is missed here.

    Args:
        agent: The agent whose providers say where the history lives.
        state: Session state, live or snapshotted.

    Returns:
        The record, or an empty string when the conversation holds none. Several results
        batched into one message are joined, which is what a provider that batches them
        produces; only results carrying :data:`~.compaction.RECORD_MARKER` are read, so an
        ordinary tool result sitting beside the record is not mistaken for part of it.
    """
    messages = _stored_messages(agent, state)
    index = find_record_index(messages)
    if index is None:
        return ""
    return _record_text(messages[index])


def snapshot_state(session: AgentSession) -> dict[str, Any]:
    """Deep-copy a session's state, so the conversation can be re-entered from here.

    Deep on purpose. ``apply_compaction`` records its decisions by mutating
    ``additional_properties`` on the ``Message`` objects rather than by shortening any list,
    so a snapshot sharing those objects would be silently rewritten by the first probe and
    every later probe would start somewhere else.

    Cheap enough to take on every turn, which is what the retry path does. ``deepcopy``
    returns immutable strings as themselves, so the copy rebuilds the message objects around
    the payloads rather than the payloads: measured at 6ms for a 232-message, 230,000-token
    conversation, against a call that spends tens of seconds sending it.

    Args:
        session: The session to snapshot.

    Returns:
        An independent copy of the session state.
    """
    return deepcopy(session.state)


def snapshot_decisions(strategy: Any) -> Any | None:
    """Take the decisions a strategy carries on its instance, to be put back with the session.

    A strategy's decisions are conversation state that the session does not hold: the
    composed chain's wait for a record, the record half's outstanding ask and the groups it
    has settled. Restoring the session without them re-enters the conversation with the
    decisions a discarded re-entry advanced -- measured as a probe that saw the snapshot once
    and a compacted conversation eleven times, on the same row.

    Args:
        strategy: The strategy under test, or None for the control.

    Returns:
        An opaque value for :func:`restore_state`, or None when the strategy carries nothing.
    """
    take = getattr(strategy, "decision_state", None)
    return take() if callable(take) else None


def restore_state(
    session: AgentSession,
    snapshot: Mapping[str, Any],
    recall_middleware: ToolResultRecallMiddleware | None = None,
    strategy: Any = None,
    decisions: Any | None = None,
) -> None:
    """Put a session back to a snapshot, before the next probe is asked or a turn re-sent.

    Copied again on every restore rather than assigned once: the run about to happen will
    mutate what it is given, and a shared copy would leak that into the one after it.

    The middleware is reset for the same reason the state is. Its pending decision to force a
    recall call is conversation state -- it is made on one call and applied to the next -- so a
    decision taken during seeding would fire on the first probe and on no other, giving that
    one probe a different prompt from the rest. That is precisely the difference between
    probes this design exists to remove. A retried turn wants it for the same reason: the
    decision standing after a failed attempt was taken by that attempt, and the middleware
    re-takes it at the end of the next call anyway, so clearing defers a forced record by one
    call rather than losing it.

    Args:
        session: The session to restore.
        snapshot: The state to restore it to.
        recall_middleware: The middleware to clear, when the strategy under test installs one.
        strategy: The strategy whose decisions ``decisions`` were taken from.
        decisions: What :func:`snapshot_decisions` returned beside ``snapshot``, or None.
    """
    session.state = deepcopy(dict(snapshot))
    if recall_middleware is not None:
        recall_middleware.forget_pending()
    if decisions is not None:
        strategy.restore_decisions(decisions)


def _retry_delay(attempt: int, requested: float | None, *, base: float, maximum: float) -> float:
    """Return how long to wait before re-sending a call that did not come back with an answer.

    One schedule shape for both failures, parametrised rather than duplicated: they differ in
    their numbers -- how long a wait is worth taking, and how many -- and not in how a wait is
    computed. Two copies of this would be two places for a cap to be applied to the jitter
    rather than to the delay.

    Args:
        attempt: 0-based index of the attempt that failed.
        requested: Seconds the provider asked for, if it named any.

    Keyword Args:
        base: The first backoff, doubled per attempt.
        maximum: Ceiling on this wait, applied to a requested delay as well as a computed one.

    Returns:
        Seconds to wait, never more than ``maximum``.
    """
    delay = min(base * 2**attempt, maximum)
    if requested is not None:
        # Taken as given when it is longer than the backoff, only capped: the provider knows
        # when its window refills and we do not, so jittering an instruction downwards just
        # spends an attempt early. A shorter one is a floor rather than the answer -- a
        # one-second Retry-After repeated while the minute's quota stays spent would burn every
        # attempt in seconds -- so the backoff still grows underneath it.
        return min(max(requested, delay), maximum)
    # SystemRandom only because both linters reject the ordinary generator on sight, and a
    # backoff wait is worth neither an argument nor a pair of suppression comments.
    jitter = _JITTER_SOURCE.random()
    return delay * (1.0 - RETRY_JITTER * jitter)


@dataclass(slots=True)
class _RetryBudget:
    """What one kind of failure may spend on one turn, and what it has spent so far.

    One of these per failure kind, so that neither draws on the other's allowance. A turn that
    waits out two rate limits and then loses its connection should survive both: the events are
    independent, and a shared counter would make the second failure's chances depend on how
    unlucky the turn had already been with the first.

    Fresh per turn, and per option-drop attempt within it, since a re-send with a different
    option set is a different request.
    """

    attempts: int
    base_delay: float
    max_delay: float
    max_wait: float
    retries: int = 0
    """Re-sends charged to this budget, which is also the 0-based index of the next attempt."""
    seconds: float = 0.0

    def take(self, requested: float | None) -> float | None:
        """Return the next wait and charge it to this budget, or None when it has run out.

        Args:
            requested: Seconds the provider asked for, if it named any.

        Returns:
            Seconds to wait before re-sending, or None when the attempts or the wait budget are
            spent and the turn should fail.
        """
        remaining = self.max_wait - self.seconds
        if self.retries + 1 >= self.attempts or remaining <= 0:
            return None
        delay = min(
            _retry_delay(self.retries, requested, base=self.base_delay, maximum=self.max_delay),
            remaining,
        )
        self.retries += 1
        self.seconds += delay
        return delay


def _repeats_for_scope(scope: str, *, probe_repeats: int, combined_repeats: int) -> int:
    """Return how many times one closing question is asked.

    The one rule the probe loop, the dry run's arithmetic and the scoring all read, so a cell
    cannot be priced for one number of probes and then run with another.

    Args:
        scope: The question's scope, as declared by ``RecallScenario.answer_scopes``.

    Keyword Args:
        probe_repeats: Repeats for a per-scope question.
        combined_repeats: Repeats for the combined question.

    Returns:
        The repeat count, never below one.
    """
    return max(combined_repeats if scope == COMBINED_SCOPE else probe_repeats, 1)


def probe_count(scopes: Sequence[str], *, probe_repeats: int, combined_repeats: int) -> int:
    """Return how many probes one seed will send.

    No longer ``questions x repeats``: the combined question has its own count, so the total
    is a sum over the questions rather than a product. The probes are the expensive half of a
    seed -- each carries the whole snapshot -- so this is what a cost estimate rests on.

    Args:
        scopes: The scope of each closing question, in order.

    Keyword Args:
        probe_repeats: Repeats for each per-scope question.
        combined_repeats: Repeats for the combined question.

    Returns:
        Probes per seed.
    """
    return sum(
        _repeats_for_scope(scope, probe_repeats=probe_repeats, combined_repeats=combined_repeats)
        for scope in scopes
    )


async def run_live(
    runtime: ProviderRuntime,
    *,
    strategy_name: str,
    options: StrategyOptions,
    scenario: RecallScenario,
    agent_kind: str = "plain",
    tool_result_tokens: int = DEFAULT_TOOL_RESULT_TOKENS,
    force_tool_calls: bool = True,
    narration: str = "prompted",
    retrieval_guidance: bool = True,
    fact_placement: str = "spread",
    allow_server_history: bool = False,
    probe_repeats: int = DEFAULT_PROBE_REPEATS,
    combined_repeats: int = DEFAULT_COMBINED_REPEATS,
    answer_max_tokens: int | None = None,
    record_max_tokens: int | None = DEFAULT_RECORD_MAX_TOKENS,
    record_target_tokens: int | None = DEFAULT_RECORD_TARGET_TOKENS,
    max_groups_before_record: int | None = None,
    repeat_records: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LiveOutcome:
    """Seed a conversation against a real agent, snapshot it, then probe the snapshot.

    Three phases, and the split is the measurement design rather than an implementation
    detail.

    **Seed.** The scenario's non-closing turns are driven as an agent in use would drive them:
    a real tool, the model writing its own replies, the strategy compacting throughout. Only
    the user-side turn list is shared between strategies; the replies, and therefore the
    histories, diverge from the first turn, and that divergence is part of what is measured.
    A turn that has to be re-sent is re-sent from the state it started in, which is not an
    optimisation: see ``_send``.

    **Snapshot.** The session state is deep-copied. It has to be a real deep copy:
    ``apply_compaction`` marks exclusions by mutating ``additional_properties`` on the
    ``Message`` objects themselves, so a shallow copy would hand every probe a history the
    previous probe had already re-marked.

    **Probe.** Every closing question is asked from that snapshot, restored before each one,
    and asked as many times as its scope calls for -- ``probe_repeats`` for a per-scope
    question, ``combined_repeats`` for the combined one, which is the only difference between
    them. No probe's answer can reach another probe's context, no question is asked from a
    context an earlier question has already compacted further, and
    survival is scored against the snapshot -- which is by construction exactly the context
    every probe was answered from. None of the three held when the closing questions were
    ordinary turns appended to the conversation, and each moved the numbers: the first scope
    was answered from a fuller context than the last, the combined question from the most
    compacted context of the run, and a code compaction had destroyed came back because the
    model had recited it two questions earlier.

    Args:
        runtime: The provider's client, model and per-request options.

    Keyword Args:
        strategy_name: Strategy to install; ``"none"`` for the uncompacted control.
        options: Budget and tokenizer parameters for the strategy.
        scenario: The scenario to drive, built with ``bulk_in_user=True``.
        agent_kind: One of :data:`AGENT_KINDS`.
        tool_result_tokens: Approximate size of each tool result, in tokens.
        narration: How hard the scenario and instructions push the model to restate tool
            values. See :data:`_INSTRUCTIONS_BY_NARRATION`.
        force_tool_calls: Set ``tool_choice='required'`` on the turns that ask for a
            lookup. Without it a model that ignores the instruction gathers fewer facts
            and carries fewer tokens, which moves both axes for reasons unrelated to
            compaction: measured at 3 of 6 scopes reached and a 33% input swing between
            identical runs on one model, against 6 of 6 and 8% on another.
        retrieval_guidance: Append the clause telling the model to quote every identifier it
            is asked for. Dropping it measures the model's own willingness to enumerate,
            which is a different thing from what compaction left behind.
        fact_placement: Where the verifiable codes sit inside each tool result. ``"spread"``
            distributes them; ``"head"`` reproduces the earlier runs, in which every code sat
            inside the first 4,096 characters and so survived head-truncating compaction
            unconditionally.
        allow_server_history: Leave a Responses-API client in charge of the conversation,
            accepting that no compaction runs. Off by default, and forced here rather than
            left to the caller: a calibration probe that forgot it reported every narration
            mode as stable, because the service was feeding the model a history the client
            had never compacted.
        probe_repeats: How many times each per-scope closing question is asked, each time
            from the restored snapshot. Several, because accuracy is two-valued often enough
            that one reading is a draw rather than a measurement: one strategy scored 52, 52,
            52 and 22 on runs that preserved exactly the same 27 facts. Repeating the question
            against unchanged material is what separates that from compaction's own spread.
        combined_repeats: How many times the combined question is asked, in exactly the same
            way and from the same restored snapshot. Counted separately because one reading of
            ``acc1`` averages every scoped question while one reading of ``acc2`` is one answer,
            so the two need different numbers of attempts to be equally settled -- and the runs
            that matter set ``probe_repeats`` to 1, the per-scope repeat spread having measured
            0 to 2 points.
        answer_max_tokens: Cap put on the closing questions' own calls, and on no others.
            ``None`` leaves the run's ordinary cap in place on those too.

            The seeding calls carry ``runtime.options["max_tokens"]``, which the caller sets to
            the same number it reserved out of the window when it sized ``options``: the reply
            to a seeding turn is appended to the history and re-sent on every turn after it, so
            the budget the strategies threshold against has to hold room for one. There is
            exactly one number on that path and it is both reserved and sent.

            A closing answer is different in the one way that matters here: nothing follows it.
            The snapshot is restored before the next probe, so the answer is scored and thrown
            away and no later prompt pays for its length -- while the answer itself has to be
            long enough to enumerate what the run is asking for, at roughly 12 tokens per
            labelled code, because a truncated answer is scored as lost facts and reads as
            compaction damage. So the closing calls carry their own number, which the caller
            reserves out of the same window for that call and sends here.
        record_max_tokens: Cap on the recall record's own call, used only by
            ``tool_summary_anchored``. Without it that call inherits the run's ordinary cap,
            which is sized for a seeding reply, so the one call instructed to summarise
            everything is the one call with no bound of its own. ``None`` restores that.
        record_target_tokens: Length the recall tool's description asks the record to aim for.
            The cap above cannot do this job -- a model does not plan to fit one, and a cut
            tool call loses its arguments rather than shortening them -- and the middleware
            cannot send an instruction message, so the description is the only channel left.
            ``None`` states no target.
        max_groups_before_record: How many tool-call groups one record may be asked to cover
            before the middleware forces another, used only by ``tool_summary_anchored``.
            ``None`` leaves the bound off, so the size trigger is the only thing that asks.
            One ask covering everything is an ask a model may only partly answer, and the
            strategy keeps whatever a record does not name, so an unbounded ask degrades into
            compacting almost nothing; this is what buys the compaction back.
        repeat_records: Let the size trigger ask for a further record once the agent has done
            tool work no existing record accounts for, used only by ``tool_summary_anchored``.
            On by default: off, the row compacts once and then grows past three times its
            window on every seed measured. On, records accumulate and every one of them is
            preserved: on a model whose one record is already complete that is duplication,
            measured as a small negative shrink.
            It governs the size trigger alone; ``max_groups_before_record`` is a caller asking
            for repeats outright and keeps forcing them either way.
        sleep: How the backoff between re-sent attempts is taken, throttled and disconnected
            alike. Injectable only so that a test can prove the retries are bounded, and prove
            it against the schedule itself, without spending the bound in wall clock.

    Returns:
        The outcome. A turn that fails sets ``error`` and stops the run rather than raising,
        so a partial result is still reported instead of losing the spend already made.
    """
    runtime_options = dict(runtime.options)
    if wants_client_side_history(runtime.client, allow_server_history=allow_server_history):
        runtime_options["store"] = False
    strategy = build_strategy(strategy_name, options)
    recorder = UsageRecorder()
    summarizer = options.summarizer if isinstance(options.summarizer, MeteredClient) else None
    tool_calls = 0
    scopes_called: list[str] = []

    def _wrap(scope: str, inner: Callable[[], str]) -> Callable[[], str]:
        def recorded() -> str:
            nonlocal tool_calls
            tool_calls += 1
            scopes_called.append(scope)
            return inner()

        recorded.__name__ = inner.__name__
        recorded.__doc__ = inner.__doc__
        return recorded

    scope_tools = [
        _wrap(name.removeprefix("lookup_"), fn)
        for fn in make_scope_tools(
            scenario.tool_lookups, tool_result_tokens, narration=narration, placement=fact_placement
        )
        if (name := fn.__name__)
    ]

    # The recall tool is registered only for the strategy that asks the model to call it.
    # Adding it to every row would put an extra tool in every prompt and give unrelated
    # strategies something new to call, which is a difference between rows that has nothing
    # to do with compaction.
    recall_middleware: ToolResultRecallMiddleware | None = None
    # Kept as a narrowed reference rather than re-tested at the end of the run. The counters
    # this outcome reports are read off the strategy object once the conversation is over, and
    # a second test down there is a second place to keep in step with this one.
    #
    # Found rather than isinstance-tested, and that is the whole of what arms a composed row.
    # A composition is not an instance of its parts, so a plain test would leave the middleware
    # uninstalled for any strategy that merely *contains* the record one: no call pinned, no
    # record written, the strategy waiting and then falling back, and a row carrying the
    # composed name while measuring only the half of it that needs no middleware. Nothing in
    # the table would say so, because FALLBACK is also what a model that never complied looks
    # like. See ``find_nested_strategy``.
    recording = find_nested_strategy(strategy, ToolResultAnchoredSummarizationCompactionStrategy)
    # The same discovery for the other summarising strategy, and kept apart from the one above
    # rather than folded into a single "does it have counters" test: the two report different
    # numbers, and a composed row is both, so one shared reference could not answer for either.
    user_compacting = find_nested_strategy(
        strategy, UserTurnAnchoredSummarizationCompactionStrategy
    )
    if recording is not None:
        gate = RecallGate()
        # Registered like any other tool, because the harness must know it to run it, and
        # inert until the middleware arms it, because it cannot be hidden from the model.
        scope_tools = [*scope_tools, make_recall_tool(gate, target_tokens=record_target_tokens)]
        recall_middleware = ToolResultRecallMiddleware(
            max_input_tokens=recording.max_input_tokens,
            tokenizer=options.tokenizer,
            arm=gate.arm,
            trigger_fraction=recording.trigger_fraction,
            record_max_tokens=record_max_tokens,
            max_groups_before_record=max_groups_before_record,
            # The composed row asks for a record for every new batch of tool work, and says so
            # on the object rather than through the flag, so its halves are configured for its
            # purposes without moving the single row's default. Read off the outermost strategy:
            # the record half itself reports nothing, so ``tool_summary_anchored`` follows
            # --record-repeats / --no-record-repeats.
            repeat_records=repeat_records or bool(getattr(strategy, "repeat_records", False)),
            # The strategy's own ask for another record, made when a record leaves tool groups
            # uncovered. Wired here because the middleware holds no reference to the strategy
            # and the strategy none to the middleware; found through the same nested lookup as
            # everything else, so a composed row's record phase can ask too.
            reforce=recording.take_reforce,
        )

    agent = build_live_agent(
        runtime,
        kind=agent_kind,
        strategy=strategy,
        tokenizer=options.tokenizer,
        tools=scope_tools,
        recorder=recorder,
        extra_middleware=[recall_middleware] if recall_middleware else [],
        instructions=resolve_instructions(narration, retrieval_guidance=retrieval_guidance),
        max_context_window_tokens=options.max_context_window_tokens,
        max_output_tokens=options.max_output_tokens,
    )
    session = agent.create_session()

    turns = scenario.transcript.turns
    question_count = min(max(scenario.answer_turn_count, 1), len(turns))
    seed_turns = turns[: len(turns) - question_count]
    probe_turns = turns[len(turns) - question_count :]
    # The declared scopes line up with the closing turns one for one. A scenario that closes
    # with a single sweeping question declares one scope, so the pairing is exact either way.
    scopes = scenario.answer_scopes or (COMBINED_SCOPE,) * question_count

    error: str | None = None
    replies: list[str] = []
    forced: dict[int, str] = dict(scenario.tool_turn_scopes) if force_tool_calls else {}
    dropped: list[str] = []
    retries = 0
    throttled = 0.0
    reconnects = 0
    reconnected_seconds = 0.0

    async def _attempt(
        text: str,
        turn_options: dict[str, Any],
        before: Mapping[str, Any],
        before_decisions: Any | None,
    ) -> Any:
        """Send one turn, waiting out throttling and dropped connections while the bounds allow.

        Two failures, one loop, separate budgets. They are alike in what they need -- a wait, a
        restore, and a bounded number of goes -- and unlike in everything else: a quota window
        refills on a fixed period and a network path does not, so the schedules differ, and the
        counters differ because a throttled run and a reconnected one are not the same
        measurement. Anything the provider actually answered is re-raised on the first try.

        Args:
            text: The user turn.
            turn_options: Per-call request options.
            before: The session state this turn started from, restored before each re-send.
            before_decisions: The strategy's decisions at that point, restored with it.

        Returns:
            The agent response.

        Raises:
            Exception: Whatever the provider raised, once it is neither throttling nor a
                connection failure, or the matching attempt and wait budgets are spent. Failing
                here is deliberate: the caller fails the turn and abandons the seed, which is
                the honest outcome for a limit that did not lift or a provider that stayed
                unreachable. Continuing with a short conversation would report a cheap,
                forgetful strategy that was never run, and a retry that hid an outage would
                report the sweep as merely slow.
        """
        nonlocal retries, throttled, reconnects, reconnected_seconds
        rate_limit_budget = _RetryBudget(
            attempts=RATE_LIMIT_ATTEMPTS,
            base_delay=RATE_LIMIT_BASE_DELAY,
            max_delay=RATE_LIMIT_MAX_DELAY,
            max_wait=RATE_LIMIT_MAX_WAIT,
        )
        connection_budget = _RetryBudget(
            attempts=CONNECTION_ATTEMPTS,
            base_delay=CONNECTION_BASE_DELAY,
            max_delay=CONNECTION_MAX_DELAY,
            max_wait=CONNECTION_MAX_WAIT,
        )
        try:
            while True:
                try:
                    return await agent.run(text, session=session, options=turn_options)
                except Exception as exc:
                    # Throttling first. The two predicates disagree about a 429 by
                    # construction -- a status the provider answered with is a decision, and
                    # only 408 and the 5xx read as transient -- but the order says which
                    # reading wins if some gateway ever wraps one failure in the other.
                    if is_rate_limited(exc):
                        budget = rate_limit_budget
                    elif is_connection_error(exc):
                        budget = connection_budget
                    else:
                        raise
                    # ``Retry-After`` is read on both paths: a 503 is entitled to name one,
                    # and it is a better answer than any schedule invented here.
                    delay = budget.take(retry_after_seconds(exc))
                    if delay is None:
                        raise
                    await sleep(delay)
                    restore_state(session, before, recall_middleware, strategy, before_decisions)
        finally:
            # In a ``finally`` because the turn's spend is the turn's spend either way: a seed
            # that failed after four re-sends has to report them, and that is exactly the row
            # someone will be reading the counters on.
            retries += rate_limit_budget.retries
            throttled += rate_limit_budget.seconds
            reconnects += connection_budget.retries
            reconnected_seconds += connection_budget.seconds

    async def _send(
        text: str, *, turn_index: int, label: str, max_tokens: int | None = None
    ) -> Any:
        """Send one turn, dropping an option the provider rejects and retrying once.

        Args:
            text: The user turn.

        Keyword Args:
            turn_index: Position in the scenario's turn list, which decides the pinned tool.
            label: How this call is named in an error.
            max_tokens: Output cap for this call alone, replacing the run's ordinary one.
                ``None`` leaves it. Only the closing questions pass one; see ``run_live``'s
                ``answer_max_tokens`` for why they are the exception.

        Returns:
            The agent response, or None when the turn could not be sent at all.
        """
        nonlocal error, forced
        # ``agent.run`` is not idempotent, so every retry below starts from here rather than
        # from wherever the failed attempt stopped. A 429 that lands inside the tool-calling
        # loop leaves the session holding an assistant function call whose result never
        # arrived -- history is persisted per model call, so the call is durable and the
        # result that was still in flight is not -- and re-sending against that state is
        # refused outright: "No tool output found for function call". Measured live at 7
        # occurrences in one cell, every one on a throttled row and including the uncompacted
        # control, so the retry that exists to save seeds was destroying them instead.
        before = snapshot_state(session)
        before_decisions = snapshot_decisions(strategy)
        # Allow one correction per runtime option, plus tool_choice and max_tokens, then success.
        # Restore the session before resending a turn that may have persisted a partial tool call.
        for _ in range(len(runtime_options) + 3):
            # Per-turn options carry the runtime's own options too: this replaces the
            # per-call option set rather than adding to it.
            turn_options: dict[str, Any] = {
                k: v for k, v in runtime_options.items() if k not in dropped
            }
            # After the drop filter and gated on it, so a provider that rejected max_tokens
            # outright does not have it put straight back by the closing questions.
            if max_tokens is not None and "max_tokens" not in dropped:
                turn_options["max_tokens"] = max_tokens
            if "tool_choice" not in dropped:
                if turn_index in forced:
                    # Name the function, not just "required". Requiring *a* call still lets
                    # the model pick the scope, and it picks wrong: measured reaching 3 of 6
                    # scopes while calling one of them twice.
                    turn_options["tool_choice"] = {
                        "mode": "required",
                        "required_function_name": f"lookup_{forced[turn_index]}",
                    }
                elif force_tool_calls:
                    # Every other turn is closed to tools. Pinning only the wanted calls
                    # still leaves the model free to make unwanted ones: measured at 12 calls
                    # against the 6 asked for, on one repeat in three.
                    turn_options["tool_choice"] = "none"
            try:
                return await _attempt(text, turn_options, before, before_decisions)
            except Exception as exc:
                option = unsupported_option(exc)
                if option is None or option in dropped or option not in turn_options:
                    error = f"{label}: {type(exc).__name__}: {exc}"
                    return None
                dropped.append(option)
                if option == "tool_choice":
                    forced = {}
                restore_state(session, before, recall_middleware, strategy, before_decisions)
        return None

    seeded = 0
    for index, turn in enumerate(seed_turns):
        response = await _send(
            _turn_text(turn.request), turn_index=index, label=f"turn {index + 1}"
        )
        if response is None:
            if error is None:
                error = f"turn {index + 1}: no response"
            break
        seeded += 1
        replies.append(response.text or "")

    seed_prompt_tokens = recorder.calls[-1].input_tokens if recorder.calls else 0
    snapshot = snapshot_state(session)
    decided = snapshot_decisions(strategy)
    snapshot_prompt = serialize_history(agent, snapshot)

    probes: list[ProbeOutcome] = []
    questions_done = 0
    drift = 0
    if error is None:
        # Grouped by question rather than interleaved, so a question's repeats sit together
        # in the log and in ``LiveOutcome.sample``. The order is otherwise immaterial: every
        # probe is sent the same restored context whatever came before it.
        for offset, (scope, turn) in enumerate(zip(scopes, probe_turns, strict=False)):
            question = _turn_text(turn.request)
            # The combined question is asked more often than the rest, and that is the only
            # way it differs: same restored snapshot, same loop, same scoring. Giving it its
            # own path would be a second measurement to keep in step with this one.
            wanted = _repeats_for_scope(
                scope, probe_repeats=probe_repeats, combined_repeats=combined_repeats
            )
            answered = 0
            for repeat in range(1, wanted + 1):
                restore_state(session, snapshot, recall_middleware, strategy, decided)
                mark = len(recorder.calls)
                response = await _send(
                    question,
                    turn_index=len(seed_turns) + offset,
                    label=f"probe {offset + 1} repeat {repeat}",
                    max_tokens=answer_max_tokens,
                )
                if response is None:
                    if error is None:
                        error = f"probe {offset + 1} repeat {repeat}: no response"
                    break
                made = tuple(recorder.calls[mark:])
                prompt = chr(10).join(call.prompt_text for call in made)
                # The probe was sent the snapshot plus its question, so its prompt begins with
                # the snapshot verbatim -- unless the strategy acted again on the way in.
                if not prompt.startswith(snapshot_prompt):
                    drift += 1
                probes.append(
                    ProbeOutcome(
                        scope=scope,
                        question=question,
                        repeat=repeat,
                        answer=response.text or "",
                        prompt_text=prompt,
                        calls=made,
                    )
                )
                answered += 1
            if answered < wanted:
                break
            questions_done += 1

    usage_missing = any(call.input_tokens <= 0 for call in recorder.calls) or (
        summarizer is not None and any(call.input_tokens <= 0 for call in summarizer.usage)
    )
    if usage_missing and error is None:
        error = "Incomplete pricing: missing or non-positive input usage."

    return LiveOutcome(
        strategy=strategy_name,
        calls=tuple(recorder.calls),
        answer=chr(10).join(probe.answer for probe in probes),
        snapshot_prompt=snapshot_prompt,
        tool_calls_made=tool_calls,
        turns_completed=seeded + questions_done,
        turns_total=len(turns),
        dropped_options=tuple(dropped),
        scopes_called=tuple(scopes_called),
        summarizer_calls=summarizer.calls if summarizer else 0,
        probes=tuple(probes),
        probe_repeats=max(probe_repeats, 1),
        combined_repeats=max(combined_repeats, 1),
        context_drift=drift,
        rate_limit_retries=retries,
        throttled_seconds=throttled,
        connection_retries=reconnects,
        connection_seconds=reconnected_seconds,
        seed_prompt_tokens=seed_prompt_tokens,
        summarizer_failures=summarizer.failures if summarizer else 0,
        strategy_notes=_strategy_notes(strategy) + _strategy_notes(recall_middleware),
        groups_kept_uncovered=recording.groups_kept_uncovered if recording is not None else 0,
        fallbacks_after_record=recording.fallbacks_after_record if recording is not None else 0,
        fallbacks_held_after_record=recording.fallbacks_held_after_record
        if recording is not None
        else 0,
        reforced_calls=recall_middleware.reforced_calls if recall_middleware is not None else 0,
        groups_preserved_uncovered=recording.groups_preserved_uncovered
        if recording is not None
        else 0,
        records_in_conversation=recording.records_in_conversation if recording is not None else 0,
        user_compactions=user_compacting.user_compactions if user_compacting is not None else 0,
        user_messages_replaced=user_compacting.user_messages_replaced
        if user_compacting is not None
        else 0,
        user_summaries_in_conversation=(
            user_compacting.user_summaries_in_conversation if user_compacting is not None else 0
        ),
        user_summary_tokens=user_compacting.user_summary_tokens
        if user_compacting is not None
        else 0,
        user_folds=user_compacting.user_folds if user_compacting is not None else 0,
        # The chain is the composed row's own, so it is read off the outermost strategy by name,
        # the way the flags column reads it, and is zero for every row that has none.
        records_merged=_count(strategy, "records_merged"),
        record_merges_rejected=_count(strategy, "record_merges_rejected"),
        user_summaries_merged=_count(strategy, "user_summaries_merged"),
        user_merges_rejected=_count(strategy, "user_merges_rejected"),
        record_rewrites=_count(strategy, "record_rewrites"),
        record_rewrites_rejected=_count(strategy, "record_rewrites_rejected"),
        record_rewrites_skipped=_count(strategy, "record_rewrites_skipped"),
        last_resort_fallbacks=_count(strategy, "last_resort_fallbacks"),
        user_passes_waited=_count(strategy, "user_passes_waited"),
        # Taken from the snapshot rather than from the live session, so it is the record the
        # probes were answered from and not one a probe's own compaction pass moved.
        record_text=recall_record_text(agent, snapshot),
        summarizer_usage=tuple(summarizer.usage) if summarizer else (),
        error=error,
        replies=tuple(replies),
    )


def unretrieved_facts(outcome: LiveOutcome, scenario: RecallScenario) -> tuple[FactOutcome, ...]:
    """Return the facts the agent never fetched, so compaction never had them.

    Scored separately from compaction damage on purpose. A tool result only enters the
    history if the model chooses to call that tool; if it never does, the markers it would
    have carried were never in the conversation at all. Counting those as evicted makes the
    uncompacted control appear to lose information, and inflates the apparent damage of every
    strategy by the same amount.

    Args:
        outcome: The finished run.
        scenario: The scenario it was driven from.

    Returns:
        One outcome per fact that was never retrieved.
    """
    called = set(outcome.scopes_called)
    missing_markers = {
        marker
        for scope, pair in scenario.tool_lookups.items()
        if scope.casefold() not in called
        for marker in pair
    }
    return tuple(
        FactOutcome(fact=fact, survived=False, recalled=fact.appears_in(outcome.answer))
        for fact in scenario.facts
        if fact.marker in missing_markers
    )


def build_live_scenario(
    *,
    salt: str,
    filler_turns: int,
    filler_tokens: int,
    tool_turns: int = 6,
    narration: str = "prompted",
    markers_per_tool: int = 2,
    filler_tool_turns: int = 0,
    subset_questions: bool = True,
) -> RecallScenario:
    """Build the scenario in the shape a live run needs.

    Keyword Args:
        salt: Cell-unique string; markers and filler derive from it.
        filler_turns: Padding turns between the planted facts.
        filler_tokens: Approximate size of each filler exchange.
        tool_turns: Tool-call groups to plant. Defaults above the framework's
            ``keep_last_tool_call_groups`` of 4, so that tool-oriented strategies
            actually engage instead of scoring a perfect result for doing nothing.
        narration: How hard the scenario pushes the model to restate tool values.
        filler_tool_turns: Extra tool calls whose results carry no codes, so that adding
            calls does not also add values to remember.
        markers_per_tool: Verifiable codes each tool result carries.
        subset_questions: Close with several targeted questions rather than one sweeping
            one. On by default: the sweeping form measures stamina, not retrieval.

    Returns:
        A scenario whose padding sits in the user turns, because the assistant's replies are
        generated rather than scripted.
    """
    return build_recall_scenario(
        salt=salt,
        filler_turns=filler_turns,
        filler_tokens=filler_tokens,
        bulk_in_user=True,
        tool_turns=tool_turns,
        filler_tool_turns=filler_tool_turns,
        narration=narration,
        markers_per_tool=markers_per_tool,
        subset_questions=subset_questions,
    )


def score_samples(
    outcome: LiveOutcome, scenario: RecallScenario
) -> tuple[tuple[FactOutcome, ...], ...]:
    """Score every independent reading of the snapshot, one tuple of outcomes per repeat.

    A distribution rather than a number, because a single reading is a draw from it. The
    repeats share a snapshot, so the facts in front of the model are identical in all of them
    and whatever they disagree about is the model, not compaction.

    Scoped scoring matches each fact only against the reply to the question that asked for
    it. Joining the replies first lets a code answered under the wrong heading count as
    recalled, which measures whether the value was emitted rather than whether it was
    attributed -- and a strategy that keeps values while losing the labelling that says which
    tool returned them then scores like one that kept both.

    Survival is judged against ``snapshot_prompt`` for every sample, which is the context every
    probe was answered from.

    Args:
        outcome: The finished run.
        scenario: The scenario it was driven from.

    Returns:
        One tuple of fact outcomes per probe repeat, in repeat order.
    """
    if not outcome.probes:
        return (score_answer(outcome.answer, scenario.facts, outcome.snapshot_prompt),)
    samples: list[tuple[FactOutcome, ...]] = []
    for repeat in range(1, outcome.probe_repeats + 1):
        scopes, answers = outcome.sample(repeat)
        if not answers:
            continue
        if scenario.answer_scopes:
            samples.append(score_scoped(answers, scopes, scenario.facts, outcome.snapshot_prompt))
        else:
            samples.append(
                score_answer(chr(10).join(answers), scenario.facts, outcome.snapshot_prompt)
            )
    return tuple(samples)


def score_combined_samples(outcome: LiveOutcome, scenario: RecallScenario) -> tuple[float, ...]:
    """Return, per attempt, the share of planted facts the combined answer contained.

    The per-scope questions ask for a handful of values each from a nearby part of the conversation.
    This one asks for all 53 at once from a context they are scattered through, which is a
    materially harder task and the one a real user is more likely to pose. It is the question the
    old design punished hardest by construction, since it was asked last and so from the most
    compacted context of the run; asked from the snapshot it is on the same footing as every other
    probe.

    Taken from the combined probes themselves rather than by counting up to a repeat count, so
    a run that asked it once and a run that asked it three times both read as what they did.
    That is what lets records written before the combined question had its own count aggregate
    beside new ones instead of being read as a failed three.

    Args:
        outcome: The finished run.
        scenario: The scenario it was driven from.

    Returns:
        One fraction per combined attempt that answered, empty when the scenario has no
        combined question.
    """
    if not scenario.facts:
        return ()
    return tuple(
        sum(1 for fact in scenario.facts if fact.appears_in(probe.answer)) / len(scenario.facts)
        for probe in outcome.probes
        if probe.scope == COMBINED_SCOPE and probe.answer
    )


_INSTRUCTIONS_BY_NARRATION: Final[dict[str, str]] = {
    "prompted": _INSTRUCTIONS,
    "neutral": NEUTRAL_INSTRUCTIONS,
    "suppressed": TERSE_INSTRUCTIONS,
}


def resolve_instructions(narration: str, *, retrieval_guidance: bool = True) -> str:
    """Return the agent instructions for a narration mode.

    Args:
        narration: One of the keys of :data:`_INSTRUCTIONS_BY_NARRATION`.

    Keyword Args:
        retrieval_guidance: Append :data:`RETRIEVAL_GUIDANCE`, which tells the model to quote
            every identifier it is asked for. Dropping it measures how much of the closing
            answer is the model's own willingness to enumerate rather than what compaction
            left behind.

    Returns:
        The instructions to install on the agent.

    Raises:
        KeyError: If ``narration`` is not a known mode.
    """
    base = _INSTRUCTIONS_BY_NARRATION[narration]
    if retrieval_guidance:
        return base if RETRIEVAL_GUIDANCE in base else f"{base} {RETRIEVAL_GUIDANCE}"
    return base.replace(RETRIEVAL_GUIDANCE, "").strip()
