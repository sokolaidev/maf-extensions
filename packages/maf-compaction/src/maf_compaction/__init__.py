"""Compaction strategies that keep the prompt cache.

Provider prompt caches match on exact prefixes, and compaction rewrites history by
construction. Each strategy here is shaped to break the cached prefix as rarely and as late
as it can:

- :class:`AnchoredCompactionStrategy` keeps a fixed head and tail verbatim and collapses the
  band between them by a rule that reads a group's *position* and nothing else, so the same
  prefix compacts to the same bytes on every later turn and stays cached.
- :class:`MinimumGainAnchoredCompactionStrategy` adds a floor: it projects the reduction
  before mutating anything and declines a collapse too small to repay the prompt cache it
  would invalidate.
- :class:`ToolResultAnchoredSummarizationCompactionStrategy` has the agent write the facts
  from its tool results into a record, then drops the tool groups that record covers.
  :func:`make_recall_tool` is the tool the model writes the record with, :class:`RecallGate`
  keeps that tool inert until it is asked for, and :class:`ToolResultRecallMiddleware` asks.
- :class:`UserTurnAnchoredSummarizationCompactionStrategy` summarises the *user's* turns
  between a fixed head and tail, in one of three modes: recompact its earlier summary once the
  band is worth a pass again, leave it standing as a boundary the next pass compacts only
  behind, or do that and fold the standing summaries into one.
- :class:`ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` runs those two over
  one conversation, the record phase first, with the user half judged against the prompt the
  record phase left. While the prompt is still over the input budget it runs a last-resort
  chain: merge the records, merge the user summaries, rewrite the record harder, then the
  record phase's fallback. Every replacement is kept only if it is smaller than what it
  replaces, and nothing is checked against content.
- :func:`set_preserved` and :func:`is_preserved` mark a message no strategy may shorten, drop
  or shed, which is how a record survives the fallback that trims tool results.
- :func:`find_nested_strategy` and :func:`record_text` are for the code that wires a strategy
  up: finding the record half inside a composition, and reading the record it wrote.

The docstrings keep the vocabulary of the benchmark the strategies were measured with: a *row*
is one strategy run over one conversation, a row reads *DQ* when its prompt went out over the
window, and the *flags* are the counters each strategy reports about its own run.

**This depends on ``agent_framework._compaction``, which is private API.** Grouping, token
annotation and the exclusion flags all come from there; nothing public exposes them. The
dependency is pinned to one minor of the framework for that reason, and each new minor is
read against before the pin moves.

What each strategy does, why it is shaped that way and where it fails is in the compaction
documentation of the maf-extensions repository.
"""

from ._anchored import (
    DEFAULT_BAND_SHARE,
    DEFAULT_KEEP_TOKENS,
    DEFAULT_MIN_GAIN_FRACTION,
    MARKER_ID_PREFIX,
    REMOVAL_MARKER,
    AnchoredCompactionStrategy,
    MinimumGainAnchoredCompactionStrategy,
)
from ._composed import (
    DEFAULT_CHAIN_GAIN_FRACTION,
    DEFAULT_HARDER_ATTEMPTS,
    ChainDecisions,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
)
from ._nested import find_nested_strategy
from ._preserve import (
    PRESERVE_REASON_KEY,
    PRESERVED_KEY,
    any_preserved,
    is_preserved,
    set_preserved,
)
from ._toolsummary import (
    DEFAULT_COVERAGE_SHARE,
    DEFAULT_FALLBACK_FRACTION,
    DEFAULT_RECORD_MAX_TOKENS,
    DEFAULT_RECORD_TARGET_TOKENS,
    DEFAULT_TRIGGER_FRACTION,
    PRESERVE_REASON_UNCOVERED,
    PRESERVE_REASON_UNRECORDED,
    RECALL_TOOL_NAME,
    RECORD_MARKER,
    RecallGate,
    RecordDecisions,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    find_record_index,
    make_recall_tool,
    record_text,
)
from ._usersummary import (
    DEFAULT_KEEP_HEAD_USER_TURNS,
    DEFAULT_KEEP_TAIL_USER_TURNS,
    DEFAULT_MIN_BAND_SHARE,
    DEFAULT_SUMMARY_MODE,
    DEFAULT_USER_FOLD_PROMPT,
    DEFAULT_USER_SUMMARY_PROMPT,
    DEFAULT_USER_TRIGGER_FRACTION,
    FOLD_EXCLUDE_REASON,
    FOLD_ID_PREFIX,
    SUMMARY_MODE_BOUNDARY,
    SUMMARY_MODE_FOLD,
    SUMMARY_MODE_RECOMPACT,
    SUMMARY_MODES,
    USER_SUMMARY_MARKER,
    UserTurnAnchoredSummarizationCompactionStrategy,
)

__all__ = [
    "DEFAULT_BAND_SHARE",
    "DEFAULT_CHAIN_GAIN_FRACTION",
    "DEFAULT_COVERAGE_SHARE",
    "DEFAULT_FALLBACK_FRACTION",
    "DEFAULT_HARDER_ATTEMPTS",
    "DEFAULT_KEEP_HEAD_USER_TURNS",
    "DEFAULT_KEEP_TAIL_USER_TURNS",
    "DEFAULT_KEEP_TOKENS",
    "DEFAULT_MIN_BAND_SHARE",
    "DEFAULT_MIN_GAIN_FRACTION",
    "DEFAULT_RECORD_MAX_TOKENS",
    "DEFAULT_RECORD_TARGET_TOKENS",
    "DEFAULT_SUMMARY_MODE",
    "DEFAULT_TRIGGER_FRACTION",
    "DEFAULT_USER_FOLD_PROMPT",
    "DEFAULT_USER_SUMMARY_PROMPT",
    "DEFAULT_USER_TRIGGER_FRACTION",
    "FOLD_EXCLUDE_REASON",
    "FOLD_ID_PREFIX",
    "MARKER_ID_PREFIX",
    "PRESERVED_KEY",
    "PRESERVE_REASON_KEY",
    "PRESERVE_REASON_UNCOVERED",
    "PRESERVE_REASON_UNRECORDED",
    "RECALL_TOOL_NAME",
    "RECORD_MARKER",
    "REMOVAL_MARKER",
    "SUMMARY_MODES",
    "SUMMARY_MODE_BOUNDARY",
    "SUMMARY_MODE_FOLD",
    "SUMMARY_MODE_RECOMPACT",
    "USER_SUMMARY_MARKER",
    "AnchoredCompactionStrategy",
    "ChainDecisions",
    "MafCompactionExperimentalWarning",
    "MinimumGainAnchoredCompactionStrategy",
    "RecallGate",
    "RecordDecisions",
    "ToolResultAnchoredSummarizationCompactionStrategy",
    "ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy",
    "ToolResultRecallMiddleware",
    "UserTurnAnchoredSummarizationCompactionStrategy",
    "any_preserved",
    "find_nested_strategy",
    "find_record_index",
    "is_preserved",
    "make_recall_tool",
    "record_text",
    "set_preserved",
]

# Experimental package: importing it emits a UserWarning rather than a FutureWarning, so a host
# running under `python -W error` can still import it.
import warnings as _warnings


class MafCompactionExperimentalWarning(UserWarning):
    """Warning category for maf-compaction's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_compaction is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafCompactionExperimentalWarning, stacklevel=2)
    except MafCompactionExperimentalWarning:
        # Deliberate: under `-W error` an informational notice must not fail the import.
        pass


_warn_experimental()
