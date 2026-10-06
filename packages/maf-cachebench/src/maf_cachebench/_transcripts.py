"""Deterministic transcript generation.

Transcripts use deterministic scripted content and structure for cross-provider comparison,
with fixed-width per-cell salts isolating cache prefixes. Live model output is discarded.
Filler text is produced by a small linear congruential generator seeded from the message
position, which keeps generation reproducible across runs, machines, and Python versions
without pulling in :mod:`random`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from itertools import pairwise
from typing import Final

from agent_framework import CharacterEstimatorTokenizer, Content, Message, TokenizerProtocol

from ._types import Transcript, TranscriptTurn

__all__ = [
    "DEFAULT_SYSTEM_TOKENS",
    "TRANSCRIPT_PRESETS",
    "TRUE_CHARS_PER_TOKEN",
    "TranscriptPreset",
    "build_preset",
    "build_transcript",
    "filler_text",
    "sized_text",
]

# A cached input token is only worth measuring once the prompt clears the provider's
# minimum cacheable size. Azure OpenAI, OpenAI, and most OpenRouter upstreams use a
# 1,024-token floor, so the system anchor alone is sized above it. Without this, the
# first several turns of a mid-size transcript would report zero cached tokens for
# reasons that have nothing to do with compaction.
DEFAULT_SYSTEM_TOKENS: Final[int] = 1_200

_CHARS_PER_TOKEN: Final[float] = 4.0
"""Chars per token assumed by ``CharacterEstimatorTokenizer``, the default sizing basis."""

TRUE_CHARS_PER_TOKEN: Final[float] = 7.90
"""Chars per token this module's vocabulary actually costs a real BPE tokenizer.

Measured against ``o200k_base``: the filler is whole common English words, which encode far
more efficiently than the 4-chars/token heuristic assumes. Presets that need to hit a real
token target — where being 2x out would move a threshold by six figures — size with this
instead."""

# Held as one string purely for compactness; order is fixed so that filler stays stable.
_VOCABULARY: Final[str] = (
    "pipeline schema migration cluster latency throughput retry checkpoint partition index "
    "replica shard quorum consensus snapshot rollback telemetry histogram percentile saturation "
    "backpressure queue consumer producer offset compaction tombstone vacuum planner optimizer "
    "cardinality selectivity predicate join aggregate window watermark lineage provenance contract "
    "invariant idempotent reconcile drift quota throttle circuit bulkhead timeout deadline "
    "propagation serialization deadlock contention allocator fragmentation residency eviction "
    "prefetch locality coherence barrier fence epoch"
)

_WORDS: Final[tuple[str, ...]] = tuple(_VOCABULARY.split())


def filler_text(seed: int, char_target: int) -> str:
    """Return deterministic prose of roughly ``char_target`` characters.

    Args:
        seed: Position-derived seed. Distinct seeds yield distinct text, which matters
            because identical message bodies would let unrelated prefixes match by
            accident and inflate measured cache reuse.
        char_target: Approximate length of the returned string.

    Returns:
        A space-joined sequence of words drawn deterministically from a fixed vocabulary.
    """
    if char_target <= 0:
        return ""
    words: list[str] = []
    length = 0
    state: int = (seed * 2_654_435_761 + 1) % (2**31)
    while length < char_target:
        state = (state * 1_103_515_245 + 12_345) % (2**31)
        word: str = _WORDS[state % len(_WORDS)]
        words.append(word)
        length += len(word) + 1
    return " ".join(words)


def sized_text(
    prefix: str, seed: int, token_target: int, chars_per_token: float = _CHARS_PER_TOKEN
) -> str:
    """Return ``prefix`` followed by filler sized to approximately ``token_target`` tokens."""
    return f"{prefix}{filler_text(seed, int(token_target * chars_per_token) - len(prefix))}"


def build_transcript(
    *,
    name: str,
    turns: int,
    salt: str,
    system_tokens: int = DEFAULT_SYSTEM_TOKENS,
    user_tokens: int = 60,
    assistant_tokens: int = 120,
    tool_result_tokens: int = 350,
    tool_call_every: int = 3,
    prompt_targets: Sequence[int] | None = None,
    chars_per_token: float = _CHARS_PER_TOKEN,
    tokenizer: TokenizerProtocol | None = None,
) -> Transcript:
    """Build a scripted conversation with a stable system anchor and a growing history.

    Every ``tool_call_every``-th turn emits a full tool-call group (assistant function
    call, tool result, assistant summary) so that tool-oriented strategies such as
    ``ToolResultCompactionStrategy`` have something to evict.

    Keyword Args:
        name: Identifier recorded on every measurement taken from this transcript.
        turns: Number of model calls to script. One API call is issued per turn.
        salt: Unique string placed at the very start of the system message. Because
            provider caches match on exact prefixes, a distinct salt gives each cell its
            own cache namespace and stops cells from contaminating each other.
        system_tokens: Approximate size of the system anchor.
        user_tokens: Approximate size of each user message.
        assistant_tokens: Approximate size of each scripted assistant reply.
        tool_result_tokens: Approximate size of each scripted tool result.
        tool_call_every: Emit a tool-call group every N turns. Use 0 to disable.
        prompt_targets: Desired prompt size at each turn, which pins the growth curve
            directly instead of leaving it to accumulate from uniform message sizes. When
            given it supersedes ``turns``, ``system_tokens`` and the reply sizes: the anchor
            is sized to the first target and each turn's reply is sized to close the gap to
            the next one. Use it when the interesting thresholds are specific prompt sizes
            and the turn budget is tight.
        chars_per_token: Basis for converting the ``*_tokens`` targets into characters.
            Defaults to the estimator's 4.0; pass ``TRUE_CHARS_PER_TOKEN`` to size against
            a real BPE tokenizer.
        tokenizer: Token counter used to report the transcript's approximate final prompt
            size. Defaults to ``CharacterEstimatorTokenizer``.

    Returns:
        A fully scripted transcript.

    Raises:
        ValueError: If ``turns`` is not positive.
    """
    if prompt_targets is not None:
        if len(prompt_targets) < 1:
            raise ValueError("prompt_targets must contain at least one target.")
        if any(later <= earlier for earlier, later in pairwise(prompt_targets)):
            raise ValueError("prompt_targets must increase strictly; history only ever grows.")
        turns = len(prompt_targets)
        system_tokens = max(prompt_targets[0] - user_tokens, 1)
    if turns <= 0:
        raise ValueError("turns must be greater than 0.")

    resolved_tokenizer = tokenizer or CharacterEstimatorTokenizer()
    # The salt is hashed to a fixed width so that every cell's system anchor is exactly the
    # same length. A raw salt embeds the provider and strategy names verbatim, and those
    # differ in length — which shifts the transcript's token count, which shifts the
    # budget-derived compaction thresholds, which changes how much a strategy retains.
    # Measured: "openrouter" cells kept 663 fewer tokens under truncation than "mistral"
    # cells purely from the name length, breaking cross-provider comparability.
    fixed_salt = hashlib.sha256(salt.encode("utf-8")).hexdigest()[:16]
    system = Message(
        role="system",
        contents=[
            sized_text(
                (
                    f"[cachebench:{fixed_salt}] You are a systems engineering assistant. Reference "
                    f"notes: "
                ),
                seed=0,
                token_target=system_tokens,
                chars_per_token=chars_per_token,
            )
        ],
    )

    scripted: list[TranscriptTurn] = []
    for index in range(1, turns + 1):
        user = Message(
            role="user",
            contents=[
                sized_text(
                    f"[turn {index}] Question about ",
                    seed=index * 7,
                    token_target=user_tokens,
                    chars_per_token=chars_per_token,
                )
            ],
        )
        # Each turn's reply carries exactly the growth needed to reach the next target.
        # The final turn's reply is never sent to the model, so its size is arbitrary.
        is_tool_turn = tool_call_every > 0 and index % tool_call_every == 0
        turn_reply_tokens = assistant_tokens
        turn_tool_tokens = tool_result_tokens
        if prompt_targets is not None:
            gap = (
                prompt_targets[index] - prompt_targets[index - 1] - user_tokens
                if index < len(prompt_targets)
                else assistant_tokens
            )
            gap = max(gap, 2)
            if is_tool_turn:
                # Split the growth so tool-oriented strategies still have bulk to evict.
                turn_tool_tokens = max(int(gap * 0.8), 1)
                turn_reply_tokens = max(gap - turn_tool_tokens, 1)
            else:
                turn_reply_tokens = gap

        assistant = Message(
            role="assistant",
            contents=[
                sized_text(
                    f"[turn {index}] Answer: ",
                    seed=index * 13,
                    token_target=turn_reply_tokens,
                    chars_per_token=chars_per_token,
                )
            ],
        )

        reply: list[Message] = []
        if is_tool_turn:
            call_id = f"call_{index}"
            arguments = f'{{"subsystem": "module_{index}", "depth": {index % 5}}}'
            reply.append(
                Message(
                    role="assistant",
                    contents=[
                        Content.from_function_call(
                            call_id=call_id,
                            name="inspect_subsystem",
                            arguments=arguments,
                        )
                    ],
                )
            )
            reply.append(
                Message(
                    role="tool",
                    contents=[
                        Content.from_function_result(
                            call_id=call_id,
                            result=sized_text(
                                f"[turn {index}] report: ",
                                seed=index * 31,
                                token_target=turn_tool_tokens,
                                chars_per_token=chars_per_token,
                            ),
                        )
                    ],
                )
            )
        reply.append(assistant)
        scripted.append(TranscriptTurn(request=(user,), reply=tuple(reply)))

    all_messages = (
        system,
        *(message for turn in scripted[:-1] for message in (*turn.request, *turn.reply)),
        *scripted[-1].request,
    )
    approx_tokens = resolved_tokenizer.count_tokens(
        "".join(str(content.text or "") for message in all_messages for content in message.contents)
    )
    return Transcript(
        name=name,
        system=system,
        turns=tuple(scripted),
        approx_final_prompt_tokens=approx_tokens,
    )


class TranscriptPreset:
    """A named transcript shape with a documented target size.

    Presets exist so that runs stay comparable across machines and over time. ``mid``
    lands in the tens of messages and single-digit thousands of tokens; ``large`` lands
    in the hundreds of messages and tens of thousands of tokens.
    """

    def __init__(
        self,
        name: str,
        *,
        turns: int,
        tool_result_tokens: int = 350,
        tool_call_every: int = 3,
        system_tokens: int = DEFAULT_SYSTEM_TOKENS,
        user_tokens: int = 60,
        assistant_tokens: int = 120,
        chars_per_token: float = _CHARS_PER_TOKEN,
        prompt_targets: tuple[int, ...] | None = None,
    ) -> None:
        """Create a preset.

        Args:
            name: Preset identifier used on the command line.

        Keyword Args:
            turns: Number of model calls, and therefore API calls, per cell.
            tool_result_tokens: Approximate size of each scripted tool result.
            tool_call_every: Emit a tool-call group every N turns.
            system_tokens: Approximate size of the system anchor.
            user_tokens: Approximate size of each user message.
            assistant_tokens: Approximate size of each scripted assistant reply.
            chars_per_token: Sizing basis for all of the above.
            prompt_targets: Explicit prompt size at each turn. Supersedes ``turns`` and
                the size arguments when given.
        """
        self.name = name
        self.turns = turns
        self.tool_result_tokens = tool_result_tokens
        self.tool_call_every = tool_call_every
        self.system_tokens = system_tokens
        self.user_tokens = user_tokens
        self.assistant_tokens = assistant_tokens
        self.chars_per_token = chars_per_token
        self.prompt_targets = prompt_targets


TRANSCRIPT_PRESETS: Final[dict[str, TranscriptPreset]] = {
    "small": TranscriptPreset("small", turns=6, tool_result_tokens=250),
    "mid": TranscriptPreset("mid", turns=20, tool_result_tokens=350),
    "large": TranscriptPreset("large", turns=100, tool_result_tokens=600),
    # Sized against a real tokenizer, not the estimator: prompts start near 50k true
    # tokens, cross 100k around turn 7, and finish near 200k. This is the regime where
    # compaction is actually load-bearing and where provider cache floors are irrelevant.
    # 50k up to 350k: the regime where an agent is genuinely approaching its context window
    # and compaction has to earn its keep. The peak is 350k rather than 400k because these
    # targets count message *text* only, while providers also bill chat-template overhead —
    # role markers and message delimiters. Measured against gpt-5.4-mini, that overhead is
    # about 9%: a 380,539-token transcript by this count was billed as 414,786 and rejected
    # by a 400k endpoint. 350k here lands near 382k on the wire.
    "xxl": TranscriptPreset(
        "xxl",
        turns=9,
        user_tokens=1_000,
        tool_call_every=3,
        chars_per_token=TRUE_CHARS_PER_TOKEN,
        prompt_targets=(
            50_000,
            90_000,
            130_000,
            170_000,
            210_000,
            250_000,
            290_000,
            320_000,
            350_000,
        ),
    ),
    "xl": TranscriptPreset(
        "xl",
        turns=8,
        user_tokens=1_000,
        tool_call_every=3,
        chars_per_token=TRUE_CHARS_PER_TOKEN,
        # 50k at the first call, 100k by turn 5, 200k by turn 8. Front-loading the anchor
        # and accelerating late keeps the whole 50k-200k sweep inside 8 model calls.
        prompt_targets=(50_000, 62_500, 75_000, 87_500, 100_000, 133_333, 166_667, 200_000),
    ),
}


def build_preset(
    preset: str, *, salt: str, tokenizer: TokenizerProtocol | None = None
) -> Transcript:
    """Build the transcript for a named preset.

    Args:
        preset: One of the keys of ``TRANSCRIPT_PRESETS``.

    Keyword Args:
        salt: Unique cache-namespace salt for this cell.
        tokenizer: Optional token counter override.

    Returns:
        The scripted transcript for that preset.

    Raises:
        KeyError: If ``preset`` is not a known preset name.
    """
    if preset not in TRANSCRIPT_PRESETS:
        raise KeyError(
            f"Unknown transcript preset {preset!r}. Known presets: {sorted(TRANSCRIPT_PRESETS)}"
        )
    spec = TRANSCRIPT_PRESETS[preset]
    return build_transcript(
        name=spec.name,
        turns=spec.turns,
        salt=salt,
        system_tokens=spec.system_tokens,
        user_tokens=spec.user_tokens,
        assistant_tokens=spec.assistant_tokens,
        tool_result_tokens=spec.tool_result_tokens,
        tool_call_every=spec.tool_call_every,
        chars_per_token=spec.chars_per_token,
        prompt_targets=spec.prompt_targets,
        tokenizer=tokenizer,
    )
