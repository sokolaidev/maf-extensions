"""Measure what compaction costs in information, not money.

The cost benchmark answers whether compaction saves tokens. It cannot answer the question
that actually matters: whether the agent still knows what it was told. Compaction works by
throwing history away, so the real risk is that it discards something the agent needed.

This probe plants verifiable facts at known positions in a conversation — requirements at
the start, a direction-changing correction in the middle, tool results in the middle and
near the end — then lets the model answer a final question that can only be answered
correctly by using all of them. Each fact carries a unique marker, so scoring is exact
string matching rather than a judgement call.

The important part is the two-way split. For every fact we record both whether it *survived
compaction into the final prompt* and whether the model *used it in the answer*:

- survived and used -> fine
- survived and unused -> the model's failing, not compaction's
- evicted and unused -> information loss caused by compaction, the thing being measured
- evicted and used -> the model produced it without being told; treat with suspicion

Without that split, a model that simply ignores its context would look identical to a
compaction strategy that deleted the context.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from agent_framework import Content, Message

from ._transcripts import TRUE_CHARS_PER_TOKEN, filler_text, sized_text
from ._types import Transcript, TranscriptTurn

__all__ = [
    "COMBINED_SCOPE",
    "Contradiction",
    "FactOutcome",
    "PlantedFact",
    "RecallScenario",
    "RecallScore",
    "build_recall_scenario",
    "render_code",
    "render_codes",
    "score_answer",
    "score_scoped",
]

#: The scope of the one closing question that asks for everything at once.
#:
#: Named rather than spelled ``"*"`` at each site, because three separate rules key off it:
#: it is excluded from the per-scope scoring, it is scored on its own as ``acc2``, and it has
#: its own repeat count. A literal in three files is three chances for one of them to drift.
COMBINED_SCOPE: Final[str] = "*"

_FIRST_QUESTION: Final[str] = (
    "Quote verbatim every requirement code you were given, the change-of-direction "
    "reference, and which pipeline we settled on. List them plainly, one per line, no "
    "preamble. If any of them appears nowhere in this conversation, say so for that one."
)


def combined_question(lookups: Mapping[str, tuple[str, ...]]) -> str:
    """Return the closing question that asks for every planted value at once.

    Explicit totals and per-scope counts disambiguate complete enumeration from one code per
    lookup. This combined retrieval task is scored separately from the per-scope questions.

    Args:
        lookups: Codes per scope, including any code-free filler scopes, which are ignored.

    Returns:
        The question text.
    """
    bearing = {name: codes for name, codes in lookups.items() if codes}
    total = sum(len(codes) for codes in bearing.values())
    sizes = {len(codes) for codes in bearing.values()}
    each = f", {sizes.pop()} from each" if len(sizes) == 1 else ""
    return (
        f"Final question. Combine everything now into one list: every requirement code, the "
        f"change-of-direction reference, which pipeline we settled on, and all {total} codes "
        f"returned by the {len(bearing)} deployment lookups{each}. Group them under the tool "
        "or requirement they came from. Take as long as you need and work through the "
        "conversation systematically. If any value appears nowhere in this conversation, say "
        "so for that one rather than inventing it."
    )


_SWEEPING_QUESTION: Final[str] = (
    "Write the final report summary now. It must contain, verbatim: every requireme"
    "nt code you were given; the change-of-direction reference and which pipeline w"
    "e settled on; and every code returned by the lookups. List them plainly, no preamble."
)


@dataclass(frozen=True, slots=True)
class PlantedFact:
    """A verifiable fact placed at a known point in the conversation."""

    marker: str
    kind: str
    turn: int
    description: str
    #: Which closing question owns this fact. A tool's label for a code it returned, and
    #: empty for the requirements and the correction, which the first closing question asks
    #: about. Scoring every fact against every reply lets a code answered under the wrong
    #: heading count as recalled, which measures emission rather than attribution.
    scope: str = ""

    def appears_in(self, text: str) -> bool:
        """Return whether this fact's marker occurs in ``text``."""
        return self.marker.casefold() in text.casefold()


@dataclass(frozen=True, slots=True)
class Contradiction:
    """A superseded claim the answer must not assert.

    Recall alone cannot tell a correct answer from a confidently wrong one: a model that
    writes "we are using the batch pipeline" contains the marker it was asked to quote and
    scores full recall while stating the decision that was explicitly retracted. This
    checks for that directly, which is the actual harm from losing a mid-conversation
    correction — not a gap in the answer, but a wrong answer delivered with confidence.
    """

    superseded: str
    corrected: str
    description: str

    def asserted_in(self, answer: str) -> bool:
        """Return whether the answer states the superseded claim instead of the corrected one.

        Mentioning the superseded term is not itself an error — "not the batch pipeline,
        use streaming" is a correct answer that names both. Only naming the superseded one
        while omitting the correction counts.
        """
        lowered = answer.casefold()
        return self.superseded.casefold() in lowered and self.corrected.casefold() not in lowered


@dataclass(frozen=True, slots=True)
class FactOutcome:
    """Whether one fact survived compaction, and whether the model used it."""

    fact: PlantedFact
    survived: bool
    recalled: bool

    @property
    def verdict(self) -> str:
        """Classification used to separate compaction damage from model failure."""
        if self.survived and self.recalled:
            return "ok"
        if self.survived:
            return "ignored_by_model"
        if self.recalled:
            return "recalled_without_context"
        return "lost_to_compaction"


@dataclass(frozen=True, slots=True)
class RecallScore:
    """Aggregate outcome of one recall probe."""

    outcomes: tuple[FactOutcome, ...]
    answer: str
    messages_left: int
    """Messages surviving compaction into the final prompt."""
    messages_total: int
    """Messages the conversation would have had with no compaction."""
    contradictions: tuple[Contradiction, ...] = ()
    error: str | None = None

    @property
    def asserted_superseded(self) -> bool:
        """Whether the answer states a decision that was explicitly retracted."""
        return any(entry.asserted_in(self.answer) for entry in self.contradictions)

    @property
    def checks_total(self) -> int:
        """Number of independent checks the answer is graded on.

        One per planted fact, plus one per contradiction. Grading them uniformly avoids
        inventing weights, and a retracted assertion is penalised twice over anyway: the
        answer both fails the contradiction check and, in practice, misses the correction
        fact that would have stated the right decision.
        """
        return len(self.outcomes) + len(self.contradictions)

    @property
    def checks_passed(self) -> int:
        """Checks the answer satisfied: facts it used, plus retractions it avoided."""
        avoided = sum(1 for entry in self.contradictions if not entry.asserted_in(self.answer))
        return self.recalled + avoided

    @property
    def correctness_score(self) -> float:
        """Share of checks passed, the graded correctness figure.

        Distinct from ``recall_rate``, which counts only whether facts reached the answer
        and so cannot fall when the answer asserts something explicitly retracted.
        """
        return self.checks_passed / self.checks_total if self.checks_total else 0.0

    @property
    def is_correct(self) -> bool:
        """Whether the answer passed every check.

        ``recall_rate`` measures whether facts reached the answer, which a wrong answer can
        also achieve; this cannot be satisfied by a confidently wrong one.
        """
        return self.checks_total > 0 and self.checks_passed == self.checks_total

    @property
    def recalled(self) -> int:
        """Facts present in the answer."""
        return sum(1 for outcome in self.outcomes if outcome.recalled)

    @property
    def facts_left(self) -> int:
        """Planted facts surviving compaction, which is the ceiling on recall."""
        return sum(1 for outcome in self.outcomes if outcome.survived)

    @property
    def lost_to_compaction(self) -> int:
        """Facts compaction removed and the model consequently could not use."""
        return sum(1 for outcome in self.outcomes if outcome.verdict == "lost_to_compaction")

    @property
    def ignored_by_model(self) -> int:
        """Facts still in context that the model failed to use anyway."""
        return sum(1 for outcome in self.outcomes if outcome.verdict == "ignored_by_model")

    @property
    def recall_rate(self) -> float:
        """Share of planted facts the answer used."""
        return self.recalled / len(self.outcomes) if self.outcomes else 0.0

    def by_kind(self, kind: str) -> tuple[FactOutcome, ...]:
        """Return the outcomes for one kind of fact."""
        return tuple(outcome for outcome in self.outcomes if outcome.fact.kind == kind)


@dataclass(frozen=True, slots=True)
class RecallScenario:
    """A scripted conversation with planted facts and a final question."""

    transcript: Transcript
    facts: tuple[PlantedFact, ...]
    contradictions: tuple[Contradiction, ...] = ()
    answer_turn_count: int = 1
    #: Scope asked about by each closing turn, in order. ``""`` is the requirements turn and
    #: :data:`COMBINED_SCOPE` the combined one, which is asked for everything at once.
    answer_scopes: tuple[str, ...] = ()
    """Closing turns whose replies are scored, counted from the end of the transcript."""
    tool_turn_scopes: Mapping[int, str] = field(default_factory=dict[int, str])
    """Turn index mapped to the scope that turn asks for.

    A live run forces that exact tool on that exact turn. Left to itself, a model may ignore
    the instruction or revisit a scope, changing both the available facts and token volume
    independently of compaction.
    """
    tool_lookups: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    """Scope label to ``(region_code, fallback_host)``, for driving a real tool.

    The replayed transcript bakes these into scripted tool-result messages. A live agent
    has to call a real function instead, and it must return the same markers or the two
    modes would be scoring different conversations.
    """


def render_codes(codes: tuple[str, ...]) -> str:
    """Render a tool result's codes as ``code_N=VALUE`` pairs.

    Shared by the replayed transcript and the live tool so both modes hand the model exactly
    the same text, and the scorer looks for exactly the same markers.

    Args:
        codes: The verifiable codes this tool result carries.

    Returns:
        A semicolon-separated list of labelled codes.
    """
    return "; ".join(render_code(index, code) for index, code in enumerate(codes))


def render_code(index: int, code: str) -> str:
    """Render one labelled code, as it appears wherever a tool result carries it.

    Args:
        index: Zero-based position of the code within its tool result.
        code: The verifiable code itself.

    Returns:
        A single ``code_N=VALUE`` pair.
    """
    return f"code_{index + 1}={code}"


def _markers(salt: str, count: int, prefix: str) -> list[str]:
    """Return unique, deterministic markers derived from the cell salt.

    Deriving them per cell keeps two cells from scoring against each other's markers, and
    makes it implausible that a model reproduces one from anything but the conversation.

    Each marker gets its own digest rather than a slice of one shared digest. Slicing a
    single sha256 in six-character chunks runs out after ten markers and then yields empty
    ones, which match any text at all and silently score as recalled. A tool-heavy scenario
    needs more markers than that.
    """
    return [
        f"{prefix}-{hashlib.sha256(f'{salt}:{index}'.encode()).hexdigest()[:6].upper()}"
        for index in range(count)
    ]


def build_recall_scenario(
    *,
    salt: str,
    filler_turns: int = 6,
    filler_tokens: int = 4_000,
    chars_per_token: float = TRUE_CHARS_PER_TOKEN,
    bulk_in_user: bool = False,
    tool_turns: int = 3,
    tool_result_tokens: int = 600,
    narration: str = "prompted",
    markers_per_tool: int = 2,
    filler_tool_turns: int = 0,
    subset_questions: bool = False,
) -> RecallScenario:
    """Build a conversation whose final question needs facts from throughout the history.

    The shape is deliberate. Requirements land in the first two turns, where oldest-first
    strategies evict soonest. The correction lands mid-conversation, where it is easy to
    lose and most damaging to lose, since it reverses an earlier decision. Tool results
    land mid and late, so a strategy that evicts tool output specifically can be told apart
    from one that trims by age.

    Keyword Args:
        salt: Cell-unique string; markers and filler are derived from it.
        filler_turns: Padding turns inserted between the planted facts. More filler means
            a longer history and more for compaction to act on.
        filler_tokens: Approximate size of each filler exchange.
        chars_per_token: Sizing basis for the filler.
        bulk_in_user: Put the filler in the user turns rather than the scripted replies,
            for live runs where the assistant writes its own replies.
        subset_questions: Close with several targeted questions instead of one sweeping
            one. Their union covers every planted fact, but each asks for a handful of
            codes, which measures retrieval rather than willingness to enumerate.
        filler_tool_turns: Extra tool calls whose results carry no codes. They add calls and
            bulk without adding anything to remember, which is what separates "the agent made
            more calls" from "the agent has more values to recall" -- two variables that move
            together unless one is held still deliberately.
        markers_per_tool: Verifiable codes each tool result carries. Two is easy for a model
            to echo into its reply, which lets narration preserve everything a strategy
            discards. More codes raise the resolution of the accuracy measure and make
            narration a weaker substitute for keeping the result.
        narration: How hard the scenario pushes the model to restate tool values.
            ``"prompted"`` asks for them, ``"neutral"`` says nothing either way -- the
            configuration a typical caller gets -- and ``"suppressed"`` forbids it. The
            middle one is the only setting under which the framework's own guidance is
            the sole driver of narration.
        tool_result_tokens: Approximate size of each tool result in tokens, so tool output
            remains a material share of the workload.
        tool_turns: How many tool-call groups to plant. Must exceed a strategy's
            ``keep_last_tool_call_groups`` or tool-oriented compaction never fires and
            those strategies score a perfect result for doing nothing.

    Returns:
        The scenario, whose transcript's final turn is the question to be answered.
    """
    requirement_markers = _markers(salt, 3, "RQ")
    correction_marker = _markers(salt + "c", 1, "FB")[0]
    tool_count = max(tool_turns, 3)
    per_tool = max(markers_per_tool, 1)
    tool_markers = _markers(salt + "t", tool_count * per_tool, "TL")

    facts: list[PlantedFact] = []
    turns: list[TranscriptTurn] = []
    lookups: dict[str, tuple[str, ...]] = {}
    tool_turn_scopes: dict[int, str] = {}

    system = Message(
        role="system",
        contents=[
            "You are a meticulous engineering assistant. Follow every stated requirement exactly."
        ],
    )

    # Which side of the exchange carries the bulk. Replay scripts the assistant reply, so
    # padding it is free and keeps user turns realistic. A live agent writes its own reply
    # and will not produce thousands of tokens on request, so the padding has to move to
    # the user side or the history never grows enough for any strategy to fire.
    user_filler = filler_tokens if bulk_in_user else 200
    reply_filler = 200 if bulk_in_user else filler_tokens

    def _filler_pair(index: int) -> TranscriptTurn:
        return TranscriptTurn(
            request=(
                Message(
                    role="user",
                    contents=[
                        sized_text(
                            f"[note {index}] Background: ", index * 17, user_filler, chars_per_token
                        )
                    ],
                ),
            ),
            reply=(
                Message(
                    role="assistant",
                    contents=[
                        sized_text(
                            f"[note {index}] Understood: ",
                            index * 23,
                            reply_filler,
                            chars_per_token,
                        )
                    ],
                ),
            ),
        )

    def _tool_turn(label: str, seed: int, position: int, *, bearing: bool = True) -> TranscriptTurn:
        """Emit a lookup, optionally one whose result carries no facts at all.

        A code-free lookup is what makes "more tool calls" separable from "more values to
        remember". Adding calls that also carry codes changes both at once, and a comparison
        drawn across that is not a comparison of one variable.
        """
        call_id = f"call_{label}"
        codes = (
            tuple(tool_markers[position * per_tool : (position + 1) * per_tool]) if bearing else ()
        )
        lookups[label] = codes
        tool_turn_scopes[len(turns)] = label
        facts.extend(
            PlantedFact(
                code, "tool_result", len(turns) + 1, f"{label} code {index + 1}", scope=label
            )
            for index, code in enumerate(codes)
        )
        return TranscriptTurn(
            request=(
                Message(
                    role="user",
                    contents=[
                        # Imperative and unambiguous. A softer instruction lets the model
                        # decide it has enough context and skip the call, which changes both
                        # how many facts exist to score and how many tokens the run carries,
                        # in a way that has nothing to do with the strategy under test.
                        (
                            (
                                f'Call the lookup_deployment tool with scope="{label}" now. '
                                "Acknowledge in three words or fewer and do not restate "
                                "anything it returns. "
                            )
                            if narration == "suppressed"
                            else f'Call the lookup_deployment tool with scope="{label}" now. '
                            if narration == "neutral"
                            else (
                                f'Call the lookup_deployment tool with scope="{label}" now and '
                                "report the values it returns. Do not skip the call or answer "
                                "from memory. "
                            )
                        )
                        + filler_text(seed, 300)
                    ],
                ),
            ),
            reply=(
                Message(
                    role="assistant",
                    contents=[
                        Content.from_function_call(
                            call_id=call_id,
                            name="lookup_deployment",
                            arguments=f'{{"scope": "{label}"}}',
                        )
                    ],
                ),
                Message(
                    role="tool",
                    contents=[
                        Content.from_function_result(
                            call_id=call_id,
                            result=(
                                render_codes(codes) + "; "
                                "all of these values must appear in the final report. "
                                + sized_text(
                                    f"[{label} notes] ",
                                    seed + 1,
                                    tool_result_tokens,
                                    chars_per_token,
                                )
                            ),
                        )
                    ],
                ),
                Message(role="assistant", contents=[f"Recorded the {label} deployment facts."]),
            ),
        )

    # Turns 1-2: the requirements, placed where oldest-first eviction reaches them first.
    turns.append(
        TranscriptTurn(
            request=(
                Message(
                    role="user",
                    contents=[
                        (
                            "For the deployment, use the batch pipeline. "
                            f"Two hard requirements for the final report. "
                            f"Requirement {requirement_markers[0]}: every section must be "
                            f"numbered. Requirement {requirement_markers[1]}: the report must "
                            "end with a risk table. "
                            "Quote both requirement codes verbatim in your final answer. "
                        )
                        + filler_text(1, 400)
                    ],
                ),
            ),
            reply=(
                Message(
                    role="assistant",
                    contents=["Noted both requirements; I will quote them at the end."],
                ),
            ),
        )
    )
    facts += [
        PlantedFact(requirement_markers[0], "requirement", 1, "numbered sections"),
        PlantedFact(requirement_markers[1], "requirement", 1, "risk table"),
    ]
    turns.append(
        TranscriptTurn(
            request=(
                Message(
                    role="user",
                    contents=[
                        (
                            f"One more. Requirement {requirement_markers[2]}: cite the "
                            "deployment region in the summary line. Quote this code verbatim "
                            "too. "
                        )
                        + filler_text(2, 400)
                    ],
                ),
            ),
            reply=(
                Message(role="assistant", contents=["Understood, three requirements in total."]),
            ),
        )
    )
    facts.append(PlantedFact(requirement_markers[2], "requirement", 2, "cite region"))

    # An early lookup, before any filler: its results sit alongside the requirements in
    # the oldest part of the history, so a strategy that trims by age loses them too.
    turns.append(_tool_turn("early", 39, 0))

    # Extra lookups beyond the early/mid/late anchors, spread through the filler so that a
    # tool-heavy trace can be built without disturbing where the anchors land.
    extras = [f"extra{n}" for n in range(tool_count - 3)]
    # Interleaved with the code-bearing ones so they age into the band together; a block of
    # them at one end would be evicted as a group and stop being a fair share of the bulk.
    fillers = [f"aside{n}" for n in range(max(filler_tool_turns, 0))]
    next_position = 1

    section_turns, remainder = divmod(max(filler_turns, 0), 3)

    def _filler_section(base: int, section: int) -> None:
        nonlocal next_position
        for offset in range(section_turns + (section < remainder)):
            turns.append(_filler_pair(base + offset))
            if extras:
                turns.append(_tool_turn(extras.pop(0), 50 + next_position, next_position))
                next_position += 1
            if fillers:
                turns.append(_tool_turn(fillers.pop(0), 70 + len(turns), 0, bearing=False))

    _filler_section(0, 0)

    # Middle: a correction that reverses the earlier plan. Losing this is worse than losing
    # a requirement, because the agent then confidently acts on superseded information.
    correction_turn = len(turns) + 1
    turns.append(
        TranscriptTurn(
            request=(
                Message(
                    role="user",
                    contents=[
                        (
                            f"Important change of direction, reference {correction_marker}: we "
                            "are NOT using the batch pipeline discussed earlier. Switch entirely "
                            "to the streaming pipeline. "
                            f"State the reference {correction_marker} and the words 'streaming "
                            "pipeline' in your final answer. "
                        )
                        + filler_text(3, 400)
                    ],
                ),
            ),
            reply=(
                Message(
                    role="assistant",
                    contents=["Acknowledged; switching to the streaming pipeline."],
                ),
            ),
        )
    )
    facts += [
        PlantedFact(correction_marker, "correction", correction_turn, "direction change reference"),
        PlantedFact("streaming pipeline", "correction", correction_turn, "the corrected direction"),
    ]

    _filler_section(30, 1)

    mid_position, next_position = next_position, next_position + 1
    turns.append(_tool_turn("mid", 41, mid_position))

    _filler_section(60, 2)

    turns.append(_tool_turn("late", 43, next_position))

    # Closing questions. One sweeping question demanding every code at once measures the
    # model's willingness to enumerate rather than whether the information survived:
    # identical runs returned 42 of 53 and 5 of 53. Splitting the same coverage across
    # several targeted questions asks for a handful of codes at a time, which is retrieval
    # rather than stamina, and their union still covers every planted fact.
    if subset_questions:
        turns.append(
            TranscriptTurn(
                request=(Message(role="user", contents=[_FIRST_QUESTION]),),
                reply=(),
            )
        )
        for label in [name for name in lookups if lookups[name]]:
            turns.append(
                TranscriptTurn(
                    request=(
                        Message(
                            role="user",
                            contents=[
                                (
                                    f"Next question. The {label} deployment lookup returned "
                                    f"{len(lookups[label])} codes. Quote all {len(lookups[label])} "
                                    "verbatim, one per line, no preamble. If any of them appears "
                                    "nowhere in this conversation, say so for that one."
                                )
                            ],
                        ),
                    ),
                    reply=(),
                )
            )
        # One last question asking for everything at once. The per-scope turns measure
        # whether each value survived where it belonged; this one measures whether the model
        # can assemble them all from a context they are scattered through, which is a
        # materially harder task and one it has usually failed. Scored separately so a
        # failure here cannot drag down the per-scope figure, and vice versa.
        turns.append(
            TranscriptTurn(
                request=(Message(role="user", contents=[combined_question(lookups)]),),
                reply=(),
            )
        )
        answer_scopes = ("", *[name for name in lookups if lookups[name]], COMBINED_SCOPE)
        answer_turns = 2 + sum(1 for name in lookups if lookups[name])
    else:
        turns.append(
            TranscriptTurn(
                request=(Message(role="user", contents=[_SWEEPING_QUESTION]),),
                reply=(),
            )
        )
        # The sweeping form is one turn asked for everything, so it is the combined scope
        # and nothing else.
        answer_scopes = (COMBINED_SCOPE,)
        answer_turns = 1

    return RecallScenario(
        contradictions=(
            Contradiction(
                superseded="batch pipeline",
                corrected="streaming pipeline",
                description="answered with the pipeline the user explicitly retracted",
            ),
        ),
        transcript=Transcript(
            name="recall",
            system=system,
            turns=tuple(turns),
            approx_final_prompt_tokens=0,
        ),
        facts=tuple(facts),
        tool_lookups=lookups,
        tool_turn_scopes=tool_turn_scopes,
        answer_turn_count=answer_turns,
        answer_scopes=answer_scopes,
    )


def score_scoped(
    answers: Sequence[str],
    scopes: Sequence[str],
    facts: tuple[PlantedFact, ...],
    context_prompt: str,
) -> tuple[FactOutcome, ...]:
    """Score each closing reply against only the facts its question asked for.

    Joining the replies and matching every fact against the whole string lets a code answered
    under the wrong heading count as recalled, which measures whether the value was emitted
    rather than whether it was attributed. A strategy that preserves values but loses the
    labelling that says which tool returned them scores identically to one that preserves
    both.

    The combined scope ``"*"`` is excluded here and scored on its own: it asks for everything,
    so counting it would restore the loose behaviour through the back door.

    Args:
        answers: One reply per closing turn, in order.
        scopes: The scope each of those turns asked about.
        facts: Every planted fact.
        context_prompt: The context these answers were written from, for the survival check.
            A live run passes the snapshot every probe was asked from. Passing a prompt that
            already carried an answer makes the check circular: each answer re-lists codes,
            so a code compaction destroyed scores as having survived.

    Returns:
        One outcome per fact.
    """
    by_scope: dict[str, str] = {}
    for scope, answer in zip(scopes, answers, strict=False):
        if scope != COMBINED_SCOPE:
            by_scope[scope] = by_scope.get(scope, "") + "\n" + answer
    return tuple(
        FactOutcome(
            fact=fact,
            survived=fact.appears_in(context_prompt),
            recalled=fact.appears_in(by_scope.get(fact.scope, "")),
        )
        for fact in facts
    )


def score_answer(
    answer: str, facts: tuple[PlantedFact, ...], final_prompt: str
) -> tuple[FactOutcome, ...]:
    """Score an answer against the planted facts.

    Args:
        answer: The model's final response.
        facts: Every fact planted in the conversation.
        final_prompt: The serialized prompt actually sent on the final turn, used to decide
            whether each fact survived compaction.

    Returns:
        One outcome per fact.
    """
    return tuple(
        FactOutcome(
            fact=fact, survived=fact.appears_in(final_prompt), recalled=fact.appears_in(answer)
        )
        for fact in facts
    )
