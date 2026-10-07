"""Compaction and prompt-cache interaction benchmark for Agent Framework.

Provider prompt caches match on exact prefixes, and every compaction strategy works by
excluding or rewriting messages inside an existing history. Compaction therefore breaks
the cached prefix by construction, and the question this package answers empirically is
what that costs: how much of each prompt stays reusable under a given strategy, how much
of that reusable prefix a given provider actually serves from cache, and whether
compacting earlier saves more prompt tokens than it loses in cache reads.

Measurements replay deterministic scripted content and structure across providers and
strategies, with fixed-width per-cell salts isolating their cache prefixes.
"""

import importlib.metadata

from ._advise_cli import main as advise_main
from ._advisor import ModelPricing, StrategyCost, Verdict, advise, cost_of, fetch_openrouter_pricing
from ._cli import build_parser, main, run_benchmark
from ._fill import FillPlan, plan_fill
from ._live import (
    AGENT_KINDS,
    DEFAULT_COMBINED_REPEATS,
    DEFAULT_PROBE_REPEATS,
    IdentifiedHistoryProvider,
    LiveOutcome,
    MeteredClient,
    ModelCall,
    ProbeOutcome,
    SummarizerUsage,
    UsageRecorder,
    build_live_agent,
    build_live_scenario,
    find_nested_strategy,
    make_lookup_tool,
    probe_count,
    restore_state,
    run_live,
    score_combined_samples,
    score_samples,
    serialize_history,
    snapshot_state,
    unretrieved_facts,
    wants_client_side_history,
)
from ._live_cli import main as live_main
from ._metrics import (
    common_message_prefix,
    percentile,
    serialize_message,
    summarize_cell,
    token_counts,
)
from ._providers import (
    PROVIDER_SPECS,
    ProviderRuntime,
    ProviderSpec,
    build_provider,
    parse_provider_selector,
    prompt_cache_key,
    prompt_cache_key_options,
    provider_names,
)
from ._recall import (
    Contradiction,
    FactOutcome,
    PlantedFact,
    RecallScenario,
    RecallScore,
    build_recall_scenario,
    score_answer,
)
from ._recall_cli import main as recall_main
from ._records import (
    SCHEMA_VERSION,
    CellParams,
    SeedRecord,
    StrategySettings,
    WorkloadSettings,
    append_seed_record,
    group_by_cell,
    read_seed_records,
)
from ._report import render_summary_table, write_records_jsonl, write_summary_csv
from ._runner import CallOutcome, ProviderCaller, TurnCaller, run_cell, unsupported_option
from ._strategies import (
    STRATEGIES_FORCING_RECORDS,
    STRATEGIES_NEEDING_SUMMARIZER,
    STRATEGY_BUILDERS,
    StrategyOptions,
    build_strategy,
    forces_records,
    resolve_context_window,
    strategy_names,
)
from ._summary import JointOutcome, JointVerdict, recommend, relative_correctness
from ._summary_cli import main as summary_main
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer
from ._transcripts import (
    DEFAULT_SYSTEM_TOKENS,
    TRANSCRIPT_PRESETS,
    TranscriptPreset,
    build_preset,
    build_transcript,
)
from ._types import CellKey, CellSummary, Transcript, TranscriptTurn, TurnRecord

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.0"  # Fallback for development mode

__all__ = [
    "AGENT_KINDS",
    "DEFAULT_COMBINED_REPEATS",
    "DEFAULT_PROBE_REPEATS",
    "DEFAULT_SYSTEM_TOKENS",
    "PROVIDER_SPECS",
    "SCHEMA_VERSION",
    "STRATEGIES_FORCING_RECORDS",
    "STRATEGIES_NEEDING_SUMMARIZER",
    "STRATEGY_BUILDERS",
    "TOKENIZER_NAMES",
    "TRANSCRIPT_PRESETS",
    "CallOutcome",
    "CellKey",
    "CellParams",
    "CellSummary",
    "Contradiction",
    "FactOutcome",
    "FillPlan",
    "IdentifiedHistoryProvider",
    "JointOutcome",
    "JointVerdict",
    "LiveOutcome",
    "MeteredClient",
    "ModelCall",
    "SummarizerUsage",
    "ModelPricing",
    "PlantedFact",
    "ProbeOutcome",
    "ProviderCaller",
    "ProviderRuntime",
    "ProviderSpec",
    "RecallScenario",
    "RecallScore",
    "SeedRecord",
    "StrategyCost",
    "StrategyOptions",
    "StrategySettings",
    "Transcript",
    "TranscriptPreset",
    "TranscriptTurn",
    "TurnCaller",
    "TurnRecord",
    "UsageRecorder",
    "Verdict",
    "WorkloadSettings",
    "MafCachebenchExperimentalWarning",
    "__version__",
    "advise",
    "advise_main",
    "append_seed_record",
    "build_live_agent",
    "build_live_scenario",
    "build_parser",
    "build_preset",
    "build_provider",
    "build_recall_scenario",
    "build_strategy",
    "build_tokenizer",
    "build_transcript",
    "common_message_prefix",
    "cost_of",
    "fetch_openrouter_pricing",
    "find_nested_strategy",
    "forces_records",
    "group_by_cell",
    "live_main",
    "main",
    "make_lookup_tool",
    "parse_provider_selector",
    "percentile",
    "plan_fill",
    "probe_count",
    "prompt_cache_key",
    "prompt_cache_key_options",
    "provider_names",
    "read_seed_records",
    "recall_main",
    "recommend",
    "relative_correctness",
    "render_summary_table",
    "resolve_context_window",
    "restore_state",
    "run_benchmark",
    "run_cell",
    "run_live",
    "score_answer",
    "score_combined_samples",
    "score_samples",
    "serialize_history",
    "serialize_message",
    "snapshot_state",
    "strategy_names",
    "summarize_cell",
    "summary_main",
    "token_counts",
    "unretrieved_facts",
    "unsupported_option",
    "wants_client_side_history",
    "write_records_jsonl",
    "write_summary_csv",
]

# Experimental package: importing it emits a UserWarning rather than a FutureWarning, so a host
# running under `python -W error` can still import it.
import warnings as _warnings


class MafCachebenchExperimentalWarning(UserWarning):
    """Warning category for maf-cachebench's experimental-package notice."""


def _warn_experimental() -> None:
    message = (
        "maf_cachebench is experimental and may change or be removed in future versions "
        "without notice."
    )
    try:
        _warnings.warn(message, category=MafCachebenchExperimentalWarning, stacklevel=2)
    except MafCachebenchExperimentalWarning:
        # Deliberate: under `-W error` an informational notice must not fail the import.
        pass


_warn_experimental()
