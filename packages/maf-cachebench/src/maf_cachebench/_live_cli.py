"""Command line entry point for the live-agent compaction comparison."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any, Final, cast

from maf_compaction import (
    DEFAULT_BAND_SHARE,
    DEFAULT_CHAIN_GAIN_FRACTION,
    DEFAULT_COVERAGE_SHARE,
    DEFAULT_FALLBACK_FRACTION,
    DEFAULT_HARDER_ATTEMPTS,
    DEFAULT_KEEP_HEAD_USER_TURNS,
    DEFAULT_KEEP_TAIL_USER_TURNS,
    DEFAULT_MIN_BAND_SHARE,
    DEFAULT_MIN_GAIN_FRACTION,
    DEFAULT_RECORD_MAX_TOKENS,
    DEFAULT_RECORD_TARGET_TOKENS,
    DEFAULT_SUMMARY_MODE,
    DEFAULT_TRIGGER_FRACTION,
    DEFAULT_USER_TRIGGER_FRACTION,
    SUMMARY_MODES,
)

from ._advisor import ModelPricing, fetch_openrouter_pricing
from ._cli_selection import (
    validate_pricing_options,
    validate_recall_counts,
    validate_summarizer_selector,
    validate_unique_selection,
)
from ._fill import ASSUMED_REPLY_TOKENS, FillPlan, plan_fill
from ._live import (
    AGENT_KINDS,
    DEFAULT_COMBINED_REPEATS,
    DEFAULT_PROBE_REPEATS,
    DEFAULT_TOOL_RESULT_TOKENS,
    LiveOutcome,
    MeteredClient,
    ModelCall,
    SummarizerUsage,
    build_live_scenario,
    probe_count,
    run_live,
    score_combined_samples,
    score_samples,
    unretrieved_facts,
    wants_client_side_history,
)
from ._providers import build_provider, parse_provider_selector, provider_names
from ._recall import COMBINED_SCOPE, RecallScenario, RecallScore
from ._records import (
    CellParams,
    SeedRecord,
    StrategySettings,
    WorkloadSettings,
    append_seed_record,
    group_by_cell,
    read_seed_records,
)
from ._run_identity import new_run_id
from ._strategies import (
    STRATEGIES_NEEDING_SUMMARIZER,
    StrategyOptions,
    build_strategy,
    forces_records,
    needs_summarizer,
    strategy_names,
)
from ._summary import (
    DEFAULT_MIN_CORRECTNESS,
    JointOutcome,
    JointVerdict,
    recommend,
    relative_correctness,
    validate_min_correctness,
)
from ._tokenizers import TOKENIZER_NAMES, build_tokenizer

if TYPE_CHECKING:
    from agent_framework._clients import SupportsChatGetResponse

__all__ = ["build_parser", "main", "run_live_comparison"]

#: Every strategy that needs no extra client, in a deliberate order: the control, then the
#: single-mechanism strategies, then the composed family that all share one token ceiling.
_DEFAULT_STRATEGIES = (
    "none,truncation,sliding_window,tool_result,selective_tool_call,"
    "context_window,context_window_aggressive,"
    "token_budget_fallback,token_budget_tools_first,token_budget_truncate_first,token_budget_window_first"
)

#: How far the achieved fill may sit from the target before the cell stops being the cell it
#: claims to be. The one term the analytic sizing cannot compute is the model's own replies,
#: so some deviation is expected; beyond this the fill fraction is no longer the variable it
#: is being read as.
FILL_TOLERANCE: Final[float] = 0.05

#: Share of the fill target that is tool-result text when ``--tool-share`` is not given.
#:
#: 0.6 rather than the 0 this CLI shipped with, and that is a deliberate break. 0 selects the
#: fixed path, where ``--tool-result-tokens`` pins each result at an absolute size and only the
#: filler grows to reach the target -- so the tool payload stayed near 22,000 tokens whether
#: the window was 60,000 or 300,000, while the conversation the strategy pays cache costs
#: across grew without limit. That caps what a strategy that compacts tool results and nothing
#: else can possibly save, and the cap tightens as the window widens, which reads in a window
#: sweep as the strategy degrading. Measured on ``tool_summary_anchored`` at a fixed
#: 3,500-token payload across 60,000, 100,000 and 170,000-token windows, the removed share fell
#: 28.8%, 22.6% and 17.0% of the control's snapshot while its seeding-phase cache hit rate fell
#: 88.0%, 87.5% and 76.2% against a control climbing from 96% to 98%. Deriving the payload from
#: the fill target instead keeps the workload's proportions as the window moves, so two windows
#: are one cell at two scales.
#:
#: 0.6 and 0.8 are the levels this project has treated as realistic payloads; 0.6 is the
#: conservative one and so the one that becomes the default. The cost is comparability: every
#: cell measured before this used the fixed path, ``tool_share`` is part of the cell key, and
#: the two will not pool. That is the intended outcome -- they are different workloads -- but
#: it means a sweep spanning the change has to state which side each cell came from.
DEFAULT_TOOL_SHARE: Final[float] = 0.6


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the live comparison.
    """
    parser = argparse.ArgumentParser(
        prog="cachebench-live",
        description=(
            "Compare compaction strategies against a real agent that generates its own replies "
            "and calls a real tool. The conversation is seeded, snapshotted, and then probed: "
            "every closing question is asked from the snapshot rather than appended to the "
            "conversation, so no answer contaminates another. Within-model only: real replies "
            "differ per model, so these numbers do not compare across models."
        ),
    )
    parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider or provider:model. Omitted only with --from-jsonl, which runs nothing.",
    )
    parser.add_argument(
        "--strategies", default=_DEFAULT_STRATEGIES, help=f"Available: {','.join(strategy_names())}"
    )
    parser.add_argument(
        "--agent", default="plain", choices=list(AGENT_KINDS), help="How to assemble the agent."
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=(
            "Seeds per strategy: whole conversations, driven from scratch. This is the axis "
            "that measures compaction's own reliability, since a different seed puts the facts "
            "in a different place relative to a retention boundary. 3 or more is what makes a "
            "ranking defensible."
        ),
    )
    parser.add_argument(
        "--seed-offset",
        type=int,
        default=0,
        help=(
            "Number seeds from this offset. The seed number joins the run timestamp and "
            "strategy in the scenario salt; use an offset to give resumed runs distinct "
            "recorded seed numbers."
        ),
    )
    parser.add_argument(
        "--probe-repeats",
        type=int,
        default=DEFAULT_PROBE_REPEATS,
        help=(
            "Times each per-scope closing question is asked of the same snapshot. The facts "
            "and their positions are identical across these, so whatever they disagree about "
            "is the model's own willingness to enumerate rather than anything compaction did. "
            "This is the acc1 half of the probing. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--combined-repeats",
        type=int,
        default=DEFAULT_COMBINED_REPEATS,
        help=(
            "Times the one combined question -- every value at once -- is asked of the same "
            "snapshot, independently of --probe-repeats. Its own count because one acc1 "
            "reading averages every scoped question while one acc2 reading is a single "
            "answer, so at --probe-repeats 1 acc2 was one sample per seed against seven and "
            "was the noisier of the two for that reason alone. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--fill",
        type=float,
        default=0.70,
        help=(
            "Share of --context-window the seeded conversation is sized to reach, measured on "
            "an uncompacted run. Solved analytically from the payload and filler sizes, so the "
            "user-side turn list is identical across strategies without having to run one "
            "first. The filler is the dial and the payload is held fixed, which is what makes "
            "this 'how much irrelevant context surrounds a fixed set of facts'. Up to 10.0: above "
            "1.0 the uncompacted conversation is sized past the window on purpose, so the control "
            "is expected to disqualify and the cell asks which compacting row keeps the run under "
            "the limit, and at what cost; far above it, a small window under a long conversation "
            "is what reaches a strategy's last resorts. The control must still fit the model's "
            "real limit to finish. Pass 0 to size manually from --filler-turns and "
            "--filler-tokens instead. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--filler-turns",
        type=int,
        default=6,
        help=(
            "Padding turns between planted facts. Ignored unless --fill is 0, where the sizing "
            "is manual: with a fill fraction the count is solved for."
        ),
    )
    parser.add_argument(
        "--filler-tokens",
        type=int,
        default=2_000,
        help=(
            "Size of each filler turn. Under --fill this is the size the solver aims to keep "
            "them near while it picks how many there are, so that a long conversation is many "
            "ordinary turns rather than a handful of implausibly large ones."
        ),
    )
    parser.add_argument(
        "--tool-result-tokens",
        type=int,
        default=DEFAULT_TOOL_RESULT_TOKENS,
        help=(
            "Absolute size of each tool result, in tokens: the fixed payload. Ignored when "
            "--tool-share is above 0, which it now is by default, since the two state one "
            "quantity two ways and --tool-share wins when both are given; --tool-share 0 is "
            "what selects this path. Fixed is right inside one window and wrong across two: "
            "the payload stays this size while --context-window grows, so it is a shrinking "
            "share of the conversation and a window sweep becomes a sweep over two variables. "
            "Part of the payload either way, which is a run-level parameter: vary it between "
            "runs and compare across them, never inside one matrix, or the fill fraction stops "
            "meaning what it says. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--tool-share",
        type=float,
        default=None,
        help=(
            "Share of the seeded conversation that is tool-result text: the scaling payload, "
            "and the default. Derives the size of each result from the fill target instead of "
            "--tool-result-tokens stating it, so the workload keeps its proportions as "
            "--context-window grows and two window sizes are the same cell at two scales. It "
            "wins when both are given, and --tool-result-tokens is then ignored entirely. 0 "
            "selects the fixed path and hands the sizing back to --tool-result-tokens, the "
            "same convention as --fill 0. Covers every tool result including the code-free "
            "ones --filler-tool-turns adds, so turning those on divides one budget over more "
            "results rather than adding to it. Needs --fill, since the share is a share of its "
            "target; under --fill 0 it is refused if asked for and off if it was not. The share "
            "is part of the cell key, so a cell measured with a fixed payload never pools with "
            "one measured with a scaled one. Default 0.6."
        ),
    )
    parser.add_argument(
        "--narration",
        default="neutral",
        choices=["prompted", "neutral", "suppressed"],
        help=(
            "How hard the scenario pushes the model to restate tool values. 'neutral' says "
            "nothing either way, leaving the framework's own guidance as the only driver -- "
            "the configuration a typical caller gets. Default neutral."
        ),
    )
    parser.add_argument(
        "--fact-placement",
        default="spread",
        choices=["spread", "buried", "head"],
        help=(
            "Where the verifiable codes sit inside each tool result, which decides what is "
            "being measured. 'spread' puts each on its own labelled line, so the score is how "
            "much compaction preserved. 'buried' puts them inline in prose, so the score is "
            "retrieval under noise as well -- a real property, and one where compaction can "
            "score above the uncompacted control by deleting the haystack. 'head' puts them "
            "all at the front, inside the 4,096 characters a collapsed tool result keeps, so "
            "every tool-oriented strategy preserves them for free."
        ),
    )
    parser.add_argument(
        "--no-retrieval-guidance",
        action="store_true",
        help=(
            "Drop the clause telling the model to quote every identifier it is asked for. "
            "Measures how much of the closing answer is the model's willingness to enumerate "
            "rather than what compaction left behind. Off by default. Note that dropping it "
            "is only safe with an adequate --answer-max-tokens: at 900 the control scored "
            "33%% without the clause and 100%% with it, which measures the cap, not retrieval."
        ),
    )
    parser.add_argument(
        "--sweeping-question",
        action="store_true",
        help=(
            "Close with one question demanding every code at once, instead of several "
            "targeted ones. Needs a large --answer-max-tokens: enumerating 53 codes is "
            "~640 tokens before prose, and a truncated answer is scored as lost facts."
        ),
    )
    parser.add_argument(
        "--markers-per-tool",
        type=int,
        default=2,
        help=(
            "Verifiable codes each tool result carries. Two is easy for a model to echo into "
            "its reply, which lets narration preserve what a strategy discards. More codes "
            "raise the resolution of the accuracy measure and make narration a weaker substitute."
        ),
    )
    parser.add_argument(
        "--filler-tool-turns",
        type=int,
        default=0,
        help=(
            "Extra tool calls whose results carry no codes. Adds calls and bulk without "
            "adding anything to remember, which is what separates 'the agent made more calls' "
            "from 'the agent has more values to recall'. Without them, raising --tool-turns "
            "moves both at once and no comparison across it is honest."
        ),
    )
    parser.add_argument(
        "--tool-turns",
        type=int,
        default=6,
        help=(
            "Tool-call groups to plant. Must exceed the strategies' keep_last_tool_call_groups "
            "(4) or tool-oriented compaction never fires. Default 6."
        ),
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=32_000,
        help=(
            "The context limit this run stands in for: the fill fraction is a share of it, the "
            "strategies budget against it, and any call whose prompt exceeds it disqualifies "
            "that row. The limit is simulated -- the model itself accepts far more -- so it has "
            "to be enforced here or a row that a model this size would have refused is ranked "
            "anyway. That happened: the 60,000 control ran at 78,003 tokens and every "
            "'cheaper than not compacting' at that size was measured against it."
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=2_048,
        help=(
            "The output reservation for ordinary calls: subtracted from --context-window to "
            "give the input budget every threshold is a fraction of, and sent as max_tokens on "
            "every seeding call. One number, reserved and sent: were the arithmetic to use this "
            "while the request carried --answer-max-tokens, at a 60,000 window with a 12,000 "
            "answer cap the strategies would believe 57,952 tokens of input were available when "
            "48,000 were. Size it to the longest reply "
            "a seeding turn may write, since a reply cut here is a turn the conversation "
            "carries short: measured at ~150 tokens on gpt-5.4-mini and ~602 on gpt-5.6-luna. "
            "Too low also inflates the budget and can push a trigger above what the service "
            "will accept, which disables compaction with no warning. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--answer-max-tokens",
        type=int,
        default=4_000,
        help=(
            "Cap sent as max_tokens on the closing questions, and on no other call. Those are "
            "the only calls nothing follows -- the snapshot is restored before the next probe, "
            "so an answer's length is never re-sent -- and the only ones that have to enumerate "
            "everything the run planted: at roughly 12 tokens per labelled code, 53 of them "
            "cost ~640 tokens before any prose, and a truncated answer is scored as lost facts "
            "and reads as compaction damage. It is reserved out of --context-window for those "
            "calls, so a cell whose seeded conversation leaves less headroom than this asks for "
            "is warned about before anything is spent. It was formerly sent on every request "
            "while --max-output-tokens was the number reserved; see that flag. Default "
            "%(default)s."
        ),
    )
    parser.add_argument(
        "--record-max-tokens",
        type=int,
        default=DEFAULT_RECORD_MAX_TOKENS,
        help=(
            "Cap sent as max_tokens on the one call tool_summary_anchored forces, and on no "
            "other. Without it that call inherits --max-output-tokens, so the one call asked "
            "to summarise every earlier tool result is the one call with no bound of its own. "
            "It bounds the bill and nothing else: a model does not plan to fit a cap, and a "
            "tool call cut at one loses its arguments rather than shortening them, which is "
            "why the size is asked for by --record-target-tokens instead. 0 to leave the run's "
            "ordinary cap, --max-output-tokens, in place."
        ),
    )
    parser.add_argument(
        "--record-target-tokens",
        type=int,
        default=DEFAULT_RECORD_TARGET_TOKENS,
        help=(
            "Length the recall tool's own description asks the record to aim for. The only "
            "channel that makes the model plan for a size: the middleware sends no message, "
            "because one appended there would be persisted into the user's own conversation. "
            "Keep it comfortably under --record-max-tokens, so overshooting the target is not "
            "the same event as being cut. 0 to state no target."
        ),
    )
    parser.add_argument(
        "--max-groups-before-record",
        type=int,
        default=0,
        help=(
            "Force a fresh recall record every N tool-call groups, alongside the size trigger "
            "that asks for the first one. One record asked to cover a whole conversation is a "
            "record a model may only partly write: gpt-5.6-luna named two of six tool groups, "
            "and raising --record-max-tokens, raising --record-target-tokens and rewriting the "
            "tool's own guidance each left that unchanged. What is left is to ask for less per "
            "record, which is what this bounds. It is worth having because "
            "tool_summary_anchored now keeps every group its record does not name, so an "
            "unbounded ask degrades into compacting almost nothing: this is what buys the "
            "compaction back. Each record costs an agent turn, so a small number is not free. "
            "0 to leave the bound off, which is what the run did before this existed."
        ),
    )
    parser.add_argument(
        "--record-repeats",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Let the size trigger ask for a further recall record once the agent has done tool "
            "work no existing record accounts for. On by default. Off, "
            "tool_summary_anchored compacts once and then grows: every tool result after its "
            "first record stays whole, and at three times the window it disqualifies on "
            "every seed of every model measured. Where one record already covers everything "
            "(UNCOVERED:0) a second is duplication added as preserved, unshrinkable tokens, a "
            "small negative shrink, which only matters for a conversation "
            "that ends soon after it outgrows its window. --max-groups-before-record "
            "is unaffected: setting a group bound is asking for repeats outright, and it keeps "
            "forcing them either way. tool_and_user_summary_anchored repeats whatever this says: "
            "recording every new batch of tool work is part of that row's design, and the flag "
            "moves tool_summary_anchored only."
        ),
    )
    parser.add_argument(
        "--trigger-fraction",
        type=float,
        default=DEFAULT_TRIGGER_FRACTION,
        help=(
            "Share of the input budget at which tool_summary_anchored asks for its record, on "
            "both halves at once: the strategy waits at this line and the middleware reads the "
            "strategy's own value, so the ask and the wait cannot be set apart. Every archived "
            "run used this value; it was briefly 0.8 on the argument that 0.6 fires at 58%% of "
            "a 60,000-token window, which was arithmetic rather than a measured cost. What is "
            "measured points the other way: the record degrades with the bulk it must read -- "
            "53/53 facts at 8,000-token results, 18/53 at 25,200 -- so a later ask is a bigger "
            "ask and a worse record, and it also leaves fewer turns for the compaction to repay "
            "itself over. Must be below --fallback-fraction. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--fallback-fraction",
        type=float,
        default=DEFAULT_FALLBACK_FRACTION,
        help=(
            "Share of the input budget at which tool_summary_anchored stops waiting for a "
            "record and compacts without one. The gap above --trigger-fraction is what the "
            "record has to arrive in, and it is a whole turn wide by construction: the "
            "middleware can only read the history on the way out of a call and can only pin "
            "the next one, so the conversation grows by a turn between the ask and the answer. "
            "At the 0.6 default trigger that gap is three tenths of the budget, which is "
            "several turns rather than one. It was briefly 0.95, to widen the gap under a 0.8 "
            "trigger against a give-up that no archived run has ever taken -- no run carries a "
            "FALLBACK flag. It cannot go to 1.0 either: past this line the fallback still has "
            "to fit the conversation under the ceiling. Must exceed --trigger-fraction. "
            "Default %(default)s."
        ),
    )
    parser.add_argument(
        "--coverage-share",
        type=float,
        default=DEFAULT_COVERAGE_SHARE,
        help=(
            "Share of a group's distinctive values the recall record must quote before "
            "tool_summary_anchored will delete that group. The default is a threshold rather "
            "than a derivation, and the right value depends on how many values a workload's "
            "results carry: at the eight per result these runs use, 0.8 tolerates exactly one "
            "unrecognisable value, while at two values per group the share can only be 0, 0.5 "
            "or 1 and inheriting this is meaningless. 1.0 is as brittle as the tool-name rule "
            "it replaced -- one value the model reformatted keeps a whole group, which cost a "
            "complete-record model its compaction, 20%% down to 5-6%%. 0 restores the older "
            "behaviour, where any group holding a distinctive value at all counted as covered, "
            "so the two can be run side by side. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--user-trigger-fraction",
        type=float,
        default=DEFAULT_USER_TRIGGER_FRACTION,
        help=(
            "Share of the input budget at which user_summary_anchored summarises the user's "
            "own turns. Its own flag rather than --trigger-fraction, which belongs to "
            "tool_summary_anchored: the two thresholds answer different questions for the two "
            "single rows, and sharing one flag would make a sweep of either a sweep of both. It "
            "sits higher than that one's 0.6 because this strategy pays only in a broken cached "
            "prefix, so it can wait. It decides when the first compaction happens and not how "
            "many there are -- that is --user-min-band-share, and firing late was measured not "
            "to bound the count at all. It moves the single row only: "
            "tool_and_user_summary_anchored judges both of its halves at --trigger-fraction -- "
            "the record half against the prompt the pass began with, the user half against the "
            "prompt the record half left -- so there the user half acts only when tool "
            "compaction alone did not bring the prompt under that line, and USERUNDER counts "
            "the passes it stayed idle because it did. Read USERCOMPACT in the flags column for "
            "how often it fired and USERHELD for how often the band was not worth a pass. "
            "Default %(default)s."
        ),
    )
    parser.add_argument(
        "--user-min-band-share",
        type=float,
        default=DEFAULT_MIN_BAND_SHARE,
        help=(
            "Share of the included prompt the user band must be worth before "
            "user_summary_anchored will compact it. This is the hysteresis, and it is what "
            "bounds the number of passes: without it the strategy fires once per turn for the "
            "rest of a run that stays above --user-trigger-fraction, because after its first "
            "pass the band is its own summary plus the turns since -- measured at "
            "USERCOMPACT:31 (double-counted; about fifteen passes) with USERREPLACED:2 and a "
            "seeding-phase cache hit rate of 77%% against the control's 95%%. The default is the "
            "break-even share for a conversation of this "
            "benchmark's own length at the measured cached and uncached prices; raise it for "
            "shorter runs. 0 restores the unbounded behaviour every archived row was measured "
            "with, so the two can be run side by side, and USERHELD in the flags column says "
            "how many passes it refused. One share for every --user-summary-mode: in the "
            "boundary and fold modes the band excludes the standing summaries and the prompt "
            "includes them, so it clears less often there, and in the fold mode it is also the "
            "fold's own threshold. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--record-harder-attempts",
        type=int,
        default=DEFAULT_HARDER_ATTEMPTS,
        help=(
            "How many times per pass tool_and_user_summary_anchored may rewrite its record "
            "harder once the prompt is still over the input budget after both halves and after "
            "merging its records and its user summaries: step c of that row's last-resort chain, "
            "read by no other row. Each attempt asks for more compression than the last and "
            "tells the summarizer to keep every identifier, number and name verbatim and cut "
            "wording; each is kept only if it comes back smaller than the record it replaces. "
            "Each costs a summarizer call, and a kept one rewrites a preserved message the cached "
            "prefix runs through, so the default is small -- one attempt, and one more at a "
            "harder target in case the first was kept and was not enough. RECHARDER counts "
            "attempts and RECHARDERREJ the ones refused; an attempt refused on a record is "
            "not made "
            "again, nor any milder one, until the record changes, and RECHARDERSKIP counts those. "
            "0 switches the step off. "
            "Default %(default)s."
        ),
    )
    parser.add_argument(
        "--chain-gain-fraction",
        type=float,
        default=DEFAULT_CHAIN_GAIN_FRACTION,
        help=(
            "How far tool_and_user_summary_anchored's last-resort chain compacts once it has "
            "started, which it does only when the prompt is over the input budget: until it has "
            "removed this share of the tokens behind the earliest edit it made on that pass, past "
            "the budget if need be, rather than stopping as soon as the prompt fits. Every firing "
            "re-bills what stands behind its earliest edit, so one that stops just under the "
            "budget fires again on the next turn; the default is the break-even share an edit "
            "must remove to repay that re-bill, the same as --min-gain-fraction's. Read by no "
            "other row. CHAINTARGET counts firings that reached the target and CHAINSHORT the "
            "ones every step left above it. 0 stops the chain at the budget, as before schema 19. "
            "Default %(default)s."
        ),
    )
    parser.add_argument(
        "--user-summary-mode",
        choices=SUMMARY_MODES,
        default=DEFAULT_SUMMARY_MODE,
        help=(
            "What user_summary_anchored does with the summary its previous pass left behind. "
            "recompact re-reads it: the next pass's band is the previous summary plus the turns "
            "since, one message stands for everything behind it, and every pass rewrites a "
            "message just behind the head -- which breaks the cached prefix from there to the "
            "end; measurement has not separated the three modes. boundary never re-reads it: the "
            "summary is preserved as a boundary, "
            "the next pass compacts only the turns newer than it, and the prefix up to the "
            "newest boundary is byte-identical across passes -- at the price of one standing "
            "summary per pass, a floor no later pass lowers, which USERSUMMARIES and "
            "USERSUMMTOKENS in the flags column report. fold is boundary plus a bound: once the "
            "band has stopped yielding and the standing summaries are worth "
            "--user-min-band-share of what is behind them, all of them are collapsed into one, "
            "counted as USERFOLD. The band share clears less often in the two boundary modes, "
            "so expect more USERHELD there; it is not re-tuned per mode, because the three are "
            "the arms of one comparison. The default is the mode every archived row ran and the "
            "live run in progress is measuring, and it does not move until a run has measured "
            "the arms against it. Moves the single row only: tool_and_user_summary_anchored "
            "runs its user half in boundary whatever this says. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-head-user-turns",
        type=int,
        default=DEFAULT_KEEP_HEAD_USER_TURNS,
        help=(
            "User turns at the start of the conversation user_summary_anchored never "
            "summarises. Counted in user turns, not in message groups, so it is not "
            "--keep-head-groups: that flag protects a prefix of groups of every kind and is "
            "read by four other strategies. One is the default because one is what carries "
            "the task and its requirements, which every deleting strategy measured here "
            "throws away first. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-tail-user-turns",
        type=int,
        default=DEFAULT_KEEP_TAIL_USER_TURNS,
        help=(
            "User turns at the end of the conversation user_summary_anchored never "
            "summarises. One is the default because the last user turn is the live request, "
            "and a model answering a summary of the question it was just asked answers the "
            "wrong question; the turn before it has already been answered and has no such "
            "claim, so raising this buys nothing and costs the band its newest material. "
            "Default %(default)s."
        ),
    )
    parser.add_argument(
        "--assumed-reply-tokens",
        type=int,
        default=ASSUMED_REPLY_TOKENS,
        help=(
            "How large the model's own replies are assumed to be when sizing the conversation "
            "to --fill. It is the one term the solver cannot compute, and it is per model: the "
            "default is gpt-5.4-mini's, and gpt-5.6-luna writes about 602 tokens a reply, which "
            "over ninety turns overshot a 200,000-token cell by 24%% and disqualified its own "
            "control. Measure it from a one-seed probe -- output tokens over turns -- before "
            "sizing a matrix on a model this has not been run against. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--band-share",
        type=float,
        default=DEFAULT_BAND_SHARE,
        help=(
            "Share of the input budget the anchored family's oldest banded tool result may "
            "keep, the n-th keeping an n-th of that. Unreachable before this flag existed, "
            "which hid that the default leaves a small payload untouched: at a 117,952-token "
            "ceiling the oldest result may keep 29,488 tokens, so 3,500-token results are "
            "never trimmed. Lowering it makes the strategy act, but measured against the "
            "break-even it still cannot pay on such a payload -- even at 0.01, shedding 94%% "
            "of every result, the 18,114 tokens removed fall short of the ~29,900 the edit "
            "re-bills. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-tokens",
        type=int,
        default=0,
        help=(
            "Fix the anchored family's retention at this many tokens per collapsed tool "
            "result, split between its head and its tail, instead of deriving it from "
            "--band-share and the result's position. A fixed budget is what this did "
            "originally and it cannot work across window sizes: 600 characters is 0.9%% of a "
            "result at a 60,000-token window and 0.3%% at 272,000, and the strategy scored 32 "
            "of 53 facts in the first case and 11 in the second -- the 11 being the five "
            "non-tool facts plus the one code per result that happened to fall inside the "
            "surviving head. It is exposed to make that comparison runnable again, not because "
            "it is a good setting. 0 derives it, which is the default."
        ),
    )
    parser.add_argument(
        "--min-gain-fraction",
        type=float,
        default=DEFAULT_MIN_GAIN_FRACTION,
        help=(
            "Share of the tokens *behind* a collapse that anchored_min_gain must remove before "
            "it will make the collapse. The one setting that distinguishes that row from "
            "anchored, and unreachable until this flag existed, so the pair could only ever be "
            "compared at one value of the thing being tested. Derived rather than chosen: a "
            "strict-prefix cache makes an edit re-bill everything behind it once at the "
            "uncached price and save the removed tokens on every later turn at the cached one, "
            "which repays when R > B*(p-c)/(p+T*c). The default is that at the measured prices "
            "with twenty turns remaining. T is the term nobody knows at decision time and it "
            "divides -- ten remaining turns need 43%% of B and forty need 17%% -- so a caller "
            "expecting shorter conversations should raise this rather than trust it. Default "
            "%(default)s."
        ),
    )
    parser.add_argument(
        "--keep-head-groups",
        type=int,
        default=3,
        help=(
            "Message groups at the start of the conversation the anchored family and "
            "tool_summary_anchored never touch. These carry the task, its requirements and the "
            "corrections to them, which every deleting strategy measured here throws away "
            "first and which are the cheapest facts in a conversation to keep: truncation left "
            "29 of 53 planted facts in the prompt and the model used none of them, because the "
            "codes survived while the turns saying which deployment each belonged to did not. "
            "Lower it to measure what that labelling is worth. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-tail-groups",
        type=int,
        default=4,
        help=(
            "Recent groups the anchored family keeps verbatim: the working set. Too small and "
            "the model loses the thread of what it is doing; too large and every new turn "
            "shifts a large block out of the tail and re-bills it, which is cache spent for "
            "nothing. Reaches tool_summary_anchored's fallback as well, since that is an "
            "anchored strategy. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-last-groups",
        type=int,
        default=6,
        help=(
            "Message groups sliding_window keeps, and the target count summarization compacts "
            "to. Unreachable before this flag existed, which fixed the worst-performing row in "
            "the table at one setting: sliding_window drops the oldest group every turn, so it "
            "changes the *start* of the prompt each time and measured a 1-9%% cache hit rate, "
            "the worst of anything tested. How much of that is the mechanism and how much is "
            "this number is not answerable without being able to move it. Default %(default)s."
        ),
    )
    parser.add_argument(
        "--keep-last-tool-groups",
        type=int,
        default=4,
        help=(
            "Tool-call groups the tool-oriented strategies retain verbatim. The framework "
            "default is 4; with fewer groups than that in the scenario they collapse nothing "
            "at all. Lower it to make them do real work."
        ),
    )
    parser.add_argument(
        "--budget-fraction",
        type=float,
        default=0.5,
        help=(
            "Fraction of the input budget the token_budget_* family compacts down to. Default 0.5."
        ),
    )
    parser.add_argument(
        "--min-correctness",
        type=float,
        default=None,
        help=(
            "Fraction of the control's correctness a strategy must retain to be eligible. "
            f"Defaults to {DEFAULT_MIN_CORRECTNESS}, and under --from-jsonl to whatever the run "
            "that wrote the records used, so a rebuilt verdict is the verdict that was measured."
        ),
    )
    parser.add_argument(
        "--summarizer-provider",
        default=None,
        help="Summarizer must resolve to the tested provider and model for pricing.",
    )
    parser.add_argument(
        "--price-input", type=float, default=None, help="Input price per million tokens."
    )
    parser.add_argument(
        "--price-cached", type=float, default=None, help="Cached-read price per million tokens."
    )
    parser.add_argument(
        "--price-output", type=float, default=None, help="Output price per million tokens."
    )
    parser.add_argument(
        "--long-context-threshold",
        type=int,
        default=None,
        help=(
            "Input tokens above which a model bills a whole request at its long-context rates "
            "(gpt-6-luna: 272000). Each call is priced by its own size, so a run is charged the "
            "long rates only on the calls that crossed. Needs --price-long-input."
        ),
    )
    parser.add_argument(
        "--price-long-input", type=float, default=None, help="Long-context input price per million."
    )
    parser.add_argument(
        "--price-long-cached",
        type=float,
        default=None,
        help="Long-context cached-read price per million.",
    )
    parser.add_argument(
        "--price-long-output",
        type=float,
        default=None,
        help="Long-context output price per million.",
    )
    parser.add_argument(
        "--price-long-cache-write",
        type=float,
        default=None,
        help="Long-context cache-write price per million.",
    )
    parser.add_argument(
        "--price-cache-write",
        type=float,
        default=None,
        help=(
            "Cache-write price per million tokens, for a model that bills the uncached part of a "
            "prompt above the input rate (gpt-6-luna: 1.25x). When set, every uncached input "
            "token, "
            "the summarizer's included, is charged at this rate instead of --price-input. Omit it "
            "for a model that charges no write premium."
        ),
    )
    parser.add_argument(
        "--tokenizer", default="estimator", choices=list(TOKENIZER_NAMES), help="Token counter."
    )
    parser.add_argument(
        "--no-force-tool-calls",
        action="store_true",
        help=(
            "Let the model decide its own tool calls. Needed for routes that reject a pinned "
            "tool_choice, and it must then be set for the whole run: a run where some rows were "
            "pinned and others were not is comparing different conversations."
        ),
    )
    parser.add_argument(
        "--server-history",
        action="store_true",
        help=(
            "Let the service keep the conversation server-side. Compaction then has nothing to "
            "act on, because the agent only sends the new turn. Off by default so that what is "
            "measured is actually compaction."
        ),
    )
    parser.add_argument(
        "--no-temperature", action="store_true", help="Omit temperature for models that reject it."
    )
    parser.add_argument(
        "--show-answers", action="store_true", help="Print each final answer in full."
    )
    parser.add_argument(
        "--results-jsonl",
        default=None,
        help=(
            "Append one JSON record per seed to this file, as each seed is scored rather than "
            "when the cell finishes. A cell is every strategy times --repeats seeds and can run "
            "for hours; without this, anything that stops the process before the table prints "
            "discards every seed already completed and already paid for. The file is appended "
            "to, never truncated, so a resumed run extends it. Rebuild the table with --from-jsonl."
        ),
    )
    parser.add_argument(
        "--from-jsonl",
        nargs="+",
        metavar="PATH",
        default=None,
        help=(
            "Render the table and verdict from --results-jsonl files instead of running "
            "anything. Handles a file whose cells are incomplete, and states which strategies "
            "and how many seeds each cell holds, so a partial result cannot be read as a "
            "finished one. Several paths, or a directory of them, are read as one body of "
            "records: they group into cells by what they measured rather than by which file "
            "they came from, and a body holding more than one cell is followed by the "
            "cross-cell comparison."
        ),
    )
    parser.add_argument(
        "--dump-record",
        default=None,
        help=(
            "Write the full text of every recall record the run produces into this directory, "
            "one file per strategy and seed. Diagnostic only, and observation only: the record "
            "is read back out of the finished conversation after it is over, so nothing is "
            "added to any prompt, no extra call is made, and the tokens, the cache hits and the "
            "cost of the run are byte for byte what they would have been without it. Off by "
            "default and inert when off. tool_summary_anchored writes a record, and so does the "
            "tool_and_user_summary_anchored row that runs it as its first phase; no other "
            "strategy writes one at all, and "
            "a seed whose model never wrote one produces no file rather than an empty one. Use "
            "it to read what the model actually preserved, which is the question an UNCOVERED "
            "flag raises and no count can answer."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and its rough size, call nothing."
    )
    return parser


def _resolve_pricing(args: argparse.Namespace, provider: str, model: str) -> ModelPricing:
    """Resolve pricing from the command line or OpenRouter's catalogue.

    Returns:
        The model's rates.

    Raises:
        SystemExit: If prices are neither supplied nor discoverable.
    """
    if args.price_long_input is None and any(
        value is not None
        for value in (
            args.price_long_cached,
            args.price_long_output,
            args.price_long_cache_write,
        )
    ):
        raise SystemExit(
            "Long-tier prices require --price-long-input and --long-context-threshold."
        )
    if (args.long_context_threshold is None) != (args.price_long_input is None):
        raise SystemExit(
            "--long-context-threshold and --price-long-input go together; set both or neither."
        )
    validate_pricing_options(args)
    if args.price_input is not None:
        output = args.price_output if args.price_output is not None else args.price_input
        long_context = None
        if args.price_long_input is not None:
            long_context = ModelPricing(
                input_per_million=args.price_long_input,
                cached_read_per_million=(
                    args.price_long_cached
                    if args.price_long_cached is not None
                    else args.price_long_input
                ),
                output_per_million=args.price_long_output
                if args.price_long_output is not None
                else output,
                cache_write_per_million=args.price_long_cache_write,
            )
        return ModelPricing(
            input_per_million=args.price_input,
            cached_read_per_million=args.price_cached
            if args.price_cached is not None
            else args.price_input,
            output_per_million=output,
            cache_write_per_million=args.price_cache_write,
            long_context_threshold=args.long_context_threshold,
            long_context=long_context,
        )
    if args.long_context_threshold is not None:
        raise SystemExit(
            "--long-context-threshold needs --price-input: catalogue rates carry no "
            "long-context tier."
        )
    if provider == "openrouter":
        try:
            return fetch_openrouter_pricing(model)
        except (KeyError, OSError, ValueError) as error:
            raise SystemExit(
                f"Could not fetch pricing for {model!r}: {error}. Pass --price-input."
            ) from error
    raise SystemExit(
        f"--price-input is required for provider {provider!r} (only OpenRouter pricing is "
        f"auto-fetched)."
    )


def _cost(outcome: LiveOutcome, pricing: ModelPricing) -> float:
    """Return what one live run was billed, seeding and probes together.

    What the invoice says, and not what a strategy is ranked on. One number would invite
    reading the cheap half, and that held while the closing questions were ordinary turns,
    since a strategy that seeds cheaply and then needs an enormous prompt to answer is not
    cheap. It stops holding when the questions are probes: each is asked from a restored
    snapshot, so the snapshot is re-sent once per
    probe, and a strategy that compacts hard collects that discount twelve times over on a
    phase no deployed agent has. :attr:`SeedRecord.seeding_cost` is the half that is the
    workload, and the ranking is taken there.

    Summarization additionally bills calls the agent never sees; those are added here so that
    the strategy which spends money to preserve information is not scored as though preserving
    it were free.
    """
    long_input, long_cached, long_output = _long_totals(outcome.calls, pricing)
    agent_cost = pricing.tiered_cost(
        outcome.input_tokens,
        outcome.cached_tokens,
        outcome.output_tokens,
        long_input_tokens=long_input,
        long_cached_tokens=long_cached,
        long_output_tokens=long_output,
    )
    return agent_cost + _summarizer_cost(outcome, pricing)


def _long_totals(
    calls: Sequence[ModelCall | SummarizerUsage], pricing: ModelPricing
) -> tuple[int, int, int]:
    """Return the input, cached and output tokens of the calls billed at the long-context rates.

    Per call, because the tier is decided per request: a run whose last ten calls crossed the
    threshold pays the long rates on those ten and the ordinary ones on the rest. All zero for
    a model with one rate.
    """
    long_calls = [call for call in calls if pricing.tier(call.input_tokens) is not pricing]
    return (
        sum(call.input_tokens for call in long_calls),
        sum(call.cached_tokens for call in long_calls),
        sum(call.output_tokens for call in long_calls),
    )


def _summarizer_cost(outcome: LiveOutcome, pricing: ModelPricing) -> float:
    """Price summarizer requests using their cache usage and individual context tiers."""
    long_input, long_cached, long_output = _long_totals(outcome.summarizer_usage, pricing)
    return pricing.tiered_cost(
        outcome.summarizer_input_tokens,
        sum(call.cached_tokens for call in outcome.summarizer_usage),
        outcome.summarizer_output_tokens,
        long_input_tokens=long_input,
        long_cached_tokens=long_cached,
        long_output_tokens=long_output,
    )


def _sample_scores(outcome: LiveOutcome, scenario: RecallScenario) -> tuple[RecallScore, ...]:
    """Score every independent reading of one seed's snapshot.

    Each probe repeat is scored on its own, against the same snapshot. That is what makes the
    two spreads separable: everything these disagree about happened after the conversation
    stopped changing.

    Args:
        outcome: The finished run.
        scenario: The scenario it was driven from.

    Returns:
        One score per repeat that answered.
    """
    answered = [
        repeat for repeat in range(1, outcome.probe_repeats + 1) if outcome.sample(repeat)[1]
    ]
    return tuple(
        RecallScore(
            outcomes=facts,
            answer=chr(10).join(outcome.sample(repeat)[1]) if answered else outcome.answer,
            messages_left=outcome.messages_left,
            messages_total=outcome.messages_peak,
            contradictions=scenario.contradictions,
            error=outcome.error,
        )
        for repeat, facts in zip(answered or [1], score_samples(outcome, scenario), strict=False)
    )


def _seed_record(
    outcome: LiveOutcome,
    scenario: RecallScenario,
    pricing: ModelPricing,
    cell: CellParams,
    seed: int,
) -> SeedRecord:
    """Reduce one finished seed to the durable record everything downstream reads.

    This is where scoring happens, and it happens once. The live table and a table rebuilt
    from the file months later are the same aggregation over the same records, so the two
    cannot quietly disagree about what a cell means -- there is only one path, and this is
    its input.

    Scoring needs the scenario, whose markers are salted per seed, so it has to happen while
    the seed is still in hand. That is also why the record stores results rather than the run:
    the scenario is gone the moment the process is.

    Note that correctness comes off the *scored* result and is not an attribute of the run.
    Reading it from ``LiveOutcome`` passes ruff, pyright and the whole suite, then raises
    ``AttributeError`` on the first live call, after the run has been paid for -- which has
    happened twice, and is why this is a function with a test rather than a line in the loop.

    Args:
        outcome: The finished run.
        scenario: The scenario it was driven from.
        pricing: Rates to cost it at.
        cell: The parameters the seed was measured under.
        seed: 1-based index of this seed within its strategy.

    Returns:
        The record, ready to be appended to the results file.
    """
    scores = _sample_scores(outcome, scenario)
    facts_total = len(scores[0].outcomes) if scores else 0
    facts_left = scores[0].facts_left if scores else 0
    nofetch = len(unretrieved_facts(outcome, scenario))
    long_input, long_cached, long_output = _long_totals(outcome.calls, pricing)
    probe_long_input, probe_long_cached, probe_long_output = _long_totals(
        [call for probe in outcome.probes for call in probe.calls], pricing
    )
    return SeedRecord(
        cell=cell,
        strategy=outcome.strategy,
        seed=seed,
        cost=_cost(outcome, pricing),
        summarizer_cost=_summarizer_cost(outcome, pricing),
        input_tokens=outcome.input_tokens,
        cached_tokens=outcome.cached_tokens,
        output_tokens=outcome.output_tokens,
        probe_input_tokens=outcome.probe_input_tokens,
        probe_cached_tokens=outcome.probe_cached_tokens,
        probe_output_tokens=outcome.probe_output_tokens,
        probe_input_samples=outcome.probe_input_samples,
        probe_cached_samples=outcome.probe_cached_samples,
        long_input_tokens=long_input,
        long_cached_tokens=long_cached,
        long_output_tokens=long_output,
        probe_long_input_tokens=probe_long_input,
        probe_long_cached_tokens=probe_long_cached,
        probe_long_output_tokens=probe_long_output,
        calls=len(outcome.calls),
        messages_left=outcome.messages_left,
        messages_peak=outcome.messages_peak,
        prompt_tokens_final=outcome.prompt_tokens_final,
        prompt_tokens_peak=outcome.prompt_tokens_peak,
        seed_prompt_tokens=outcome.seed_prompt_tokens,
        facts_total=facts_total,
        # Survival is a property of the snapshot, which every probe was answered from, so it
        # is the same in every sample of a seed and the first one speaks for all of them.
        facts_left=facts_left,
        facts_lost=max(facts_total - facts_left - nofetch, 0),
        nofetch=nofetch,
        correctness_samples=tuple(score.correctness_score for score in scores),
        ignored_samples=tuple(score.ignored_by_model for score in scores),
        combined_samples=score_combined_samples(outcome, scenario),
        disqualified=outcome.disqualified(cell.context_window),
        context_drift=outcome.context_drift,
        rate_limit_retries=outcome.rate_limit_retries,
        throttled_seconds=outcome.throttled_seconds,
        connection_retries=outcome.connection_retries,
        connection_seconds=outcome.connection_seconds,
        turns_completed=outcome.turns_completed,
        turns_total=outcome.turns_total,
        probe_repeats=outcome.probe_repeats,
        summarizer_calls=outcome.summarizer_calls,
        summarizer_failures=outcome.summarizer_failures,
        groups_kept_uncovered=outcome.groups_kept_uncovered,
        fallbacks_after_record=outcome.fallbacks_after_record,
        fallbacks_held_after_record=outcome.fallbacks_held_after_record,
        reforced_calls=outcome.reforced_calls,
        groups_preserved_uncovered=outcome.groups_preserved_uncovered,
        records_in_conversation=outcome.records_in_conversation,
        user_compactions=outcome.user_compactions,
        user_messages_replaced=outcome.user_messages_replaced,
        user_summaries_in_conversation=outcome.user_summaries_in_conversation,
        user_summary_tokens=outcome.user_summary_tokens,
        user_folds=outcome.user_folds,
        records_merged=outcome.records_merged,
        record_merges_rejected=outcome.record_merges_rejected,
        user_summaries_merged=outcome.user_summaries_merged,
        user_merges_rejected=outcome.user_merges_rejected,
        record_rewrites=outcome.record_rewrites,
        record_rewrites_rejected=outcome.record_rewrites_rejected,
        record_rewrites_skipped=outcome.record_rewrites_skipped,
        last_resort_fallbacks=outcome.last_resort_fallbacks,
        user_passes_waited=outcome.user_passes_waited,
        strategy_notes=outcome.strategy_notes,
        dropped_options=outcome.dropped_options,
        answer=outcome.answer,
        error=outcome.error,
    )


def _dump_record(directory: Path, strategy: str, seed: int, text: str) -> Path | None:
    """Write one seed's recall record to its own file, for a human to read.

    The only thing in this module that exists for a reader rather than for a table. Every
    count the run reports about the record -- how many were found, how many were forced, how
    many tool groups they failed to cover -- describes the record without quoting it, and the
    question those counts raise is what the model actually wrote down. That is not a number,
    so it goes in a file.

    Writes nothing when there is no record, rather than an empty file. An empty file and a
    record the model wrote as an empty string would be indistinguishable, and the first is the
    ordinary case: every strategy but ``tool_summary_anchored``, and the composed row that
    runs it as a phase, takes no record at all.

    Args:
        directory: Where to write, created if it does not exist. One run's worth: the name
            below identifies a seed within a run, so a second cell pointed at the same
            directory overwrites the first cell's files rather than sitting beside them.
        strategy: The row this seed belongs to.
        seed: 1-based index of the seed within its strategy.
        text: The record, as the model wrote it.

    Returns:
        The file written, or None when the seed produced no record.
    """
    if not text:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{strategy}-seed{seed}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _seed_spread(samples: Sequence[Sequence[float]]) -> float:
    """Return the points between the least and most correct seed.

    Compaction's own reliability. A different seed is a different conversation, so this is
    where "the strategy cleared a retention boundary this time and not last time" shows up:
    measured at 78 points for one strategy while the uncompacted control moved 7.

    Args:
        samples: One group of per-repeat correctness readings per seed.

    Returns:
        The gap in percentage points, or 0.0 for a single seed, where nothing is known.
    """
    if len(samples) < 2:
        return 0.0
    means = [fmean(seed or (0.0,)) for seed in samples]
    return (max(means) - min(means)) * 100


def _probe_spread(samples: Sequence[Sequence[float]]) -> float:
    """Return the average points between the least and most correct probe repeat within a seed.

    The model's own enumeration variance, and nothing else: the repeats averaged here were all
    answered from one restored snapshot, so the facts in front of the model and their positions
    were identical. Reported beside the between-seed spread rather than folded into it, because
    as a single number a strategy that scores 52, 52, 52 and 22 with exactly 27 facts preserved
    every time is indistinguishable from one that lost different facts each time.

    Averaged over seeds rather than maximised, so one unlucky seed does not stand for all of
    them; the between-seed column is where an unlucky seed belongs.

    Serves both accuracy columns, since both are means over repeated readings of one snapshot:
    the per-scope samples give ``rep+-`` and the combined ones ``rep2+-``. Seeds read once
    contribute nothing either way, which is how a merged file holding both can be spread.

    Args:
        samples: One group of per-repeat readings per seed, of one accuracy measure.

    Returns:
        The mean within-seed gap in percentage points, or 0.0 when each seed was read once.
    """
    ranges = [(max(seed) - min(seed)) * 100 for seed in samples if len(seed) > 1]
    return fmean(ranges) if ranges else 0.0


def _spread(costs: Sequence[float]) -> float:
    """Return the relative gap between the cheapest and dearest seed.

    Zero for a single seed, which is exactly when nothing is known about stability, so the
    report says so rather than showing a reassuring 0%.
    """
    median = sorted(costs)[len(costs) // 2]
    return (max(costs) - min(costs)) / median if len(costs) > 1 and median > 0 else 0.0


def _measured(values: Sequence[float | None]) -> list[float] | None:
    """Return the readings when every seed took one, and None when any seed did not.

    All or nothing on purpose. A row is a mean over its seeds, and a mean over whichever of
    them happened to record a quantity is a mean over a different row than the one the table
    names -- silently, since nothing about the printed figure says how many seeds are behind
    it. A cell merged from a run before the probe split and a run after it therefore reads as
    unsplit, which is what it is.

    Args:
        values: One reading per seed, or None from a seed that did not take it.

    Returns:
        The readings, or None.
    """
    if any(value is None for value in values):
        return None
    return [value for value in values if value is not None]


@dataclass(frozen=True, slots=True)
class CellStats:
    """One strategy's cell, aggregated over its seeds and their probe repeats.

    Every figure here is a mean over the cell rather than one chosen run. The table used to
    show the median-*cost* seed on every column, which is a defensible choice for cost and an
    arbitrary draw for accuracy: with a two-valued accuracy distribution it reported whichever
    of the two values happened to sit on the median cost.
    """

    strategy: str
    records: tuple[SeedRecord, ...]
    """The seeds this row is a mean over.

    Records rather than runs, so that the row a live cell prints and the row rebuilt from the
    results file are produced by one function from one kind of input. Anything the table needs
    that is not here is a way for the two to disagree.
    """
    cost: float
    """What this cell was billed: the ``run$`` column, seeding and probing together."""
    cost_spread: float
    input_cost: float
    """What the prompt side of this cell cost, output excluded.

    The same money as ``cost`` minus its output and summarizer halves, and the one worth
    ranking a mechanism on: output is priced 57 times a cache read here, so a reply the model
    happened to run long on moves the total further than compaction does.
    """
    seeding_cost: float | None
    """What the conversation cost, with the probe phase taken out: the ``seed$`` column.

    The workload, and what the ranking is on. None when any seed of this row predates the
    split, because a row is a mean over its seeds and a mean over the ones that happened to
    record it would be a different row from the one the table names.
    """
    probe_cost: float | None
    """What the probing cost: the ``probe$`` column, and the instrument's own price."""
    seeding_input_cost: float | None
    """The prompt side of ``seeding_cost``: the ``seed in$`` column."""
    seeding_cost_spread: float | None
    """Spread between the cheapest and dearest seed on ``seeding_cost``.

    Its own figure rather than ``cost_spread`` read across, because the probe phase is the
    steadier half -- the same snapshot, the same twelve questions -- so a spread taken on the
    total understates how much the part being ranked actually moved.
    """
    summarizer_cost: float
    input_tokens: float
    cached_tokens: float
    output_tokens: float
    seeding_input_tokens: float | None
    """Input tokens the conversation billed per seed, the probes taken out.

    The denominator of ``seed hit%``. None when any seed of this row predates the phase
    split, on the rule ``seeding_cost`` follows: a mean over the seeds that happened to record
    it would be a different row from the one the table names.
    """
    seeding_cached_tokens: float | None
    """How many of those the provider served from cache, per seed: ``seed hit%``'s numerator."""
    probe_input_tokens: float | None
    """Input tokens the probe phase billed per seed: the denominator of ``probe hit%``."""
    probe_cached_tokens: float | None
    """How many of those the provider served from cache, per seed: ``probe hit%``'s numerator."""
    calls: float
    messages_left: float
    messages_peak: float
    prompt_tokens_final: float
    prompt_tokens_peak: float
    seed_prompt_tokens: float
    facts_left: float
    facts_total: int
    nofetch: float
    ignored: float
    correctness: float
    """The ``acc1`` column: the mean over every per-scope probe repeat of every seed."""
    seed_spread: float
    probe_spread: float
    combined: float
    """The ``acc2`` column: the mean over every combined attempt of every seed.

    A mean over the attempts that happened rather than over a fixed count, so a cell holding
    seeds asked the combined question once and seeds asked it three times weights each answer
    once -- which is what makes a resumed or merged file aggregate as one measurement.
    """
    combined_spread: float
    """The ``rep2+-`` column: ``acc2``'s within-seed spread, averaged over seeds."""
    disqualified: float
    """Share of this cell's seeds that sent a prompt larger than the tried limit."""
    rate_limit_retries: int
    """Calls this cell re-sent after the provider refused them for rate reasons."""
    throttled_seconds: float
    """Seconds this cell spent waiting those refusals out.

    Summed over its seeds rather than averaged: this is time the cell took, and a sweep
    reading its logs back wants the total it paid, not a per-seed rate.
    """
    connection_retries: int
    """Calls this cell re-sent because the request never came back with an answer."""
    connection_seconds: float
    """Seconds this cell spent waiting for the provider to answer again, summed over its seeds."""
    samples: tuple[tuple[float, ...], ...]
    """Per-sample ``acc1``: one tuple per seed, one value per probe repeat."""
    combined_samples: tuple[tuple[float, ...], ...]
    """Per-sample ``acc2``: one tuple per seed, one value per combined attempt."""

    @property
    def hit_rate(self) -> float | None:
        """Share of input tokens served from the provider's cache, over the whole run.

        The ``run hit%`` column: seeding and probes together, which is what ``hit%`` was
        before the split. Pooled over the cell's tokens rather than meaned over its seeds, as
        it always was, and the two halves below are pooled the same way so that this is
        exactly their token-weighted mix.
        """
        return self.cached_tokens / self.input_tokens if self.input_tokens > 0 else None

    @property
    def seeding_hit_rate(self) -> float | None:
        """Share of the conversation's input tokens served from cache: ``seed hit%``.

        None when the row cannot split its phases, or when the seeding half billed nothing.
        """
        if (
            self.seeding_input_tokens is None
            or self.seeding_cached_tokens is None
            or self.seeding_input_tokens <= 0
        ):
            return None
        return self.seeding_cached_tokens / self.seeding_input_tokens

    @property
    def probe_hit_rate(self) -> float | None:
        """Share of the probe phase's input tokens served from cache: ``probe hit%``.

        None when the row cannot split its phases, or when the probe half billed nothing.
        """
        if (
            self.probe_input_tokens is None
            or self.probe_cached_tokens is None
            or self.probe_input_tokens <= 0
        ):
            return None
        return self.probe_cached_tokens / self.probe_input_tokens


def _aggregate(strategy: str, records: Sequence[SeedRecord]) -> CellStats:
    """Reduce every seed of one strategy to the row the table shows.

    The only aggregation in the package. A live run reaches it through records it has just
    written; ``--from-jsonl`` reaches it through records it has just read; there is no second
    implementation for the two to drift apart in.

    Args:
        strategy: The strategy these seeds measured.
        records: Every seed of it, in any order.

    Returns:
        The aggregated cell.

    Raises:
        ValueError: If no seeds were supplied, since a row is a mean over something.
    """
    if not records:
        raise ValueError(f"No seeds recorded for {strategy!r}; a row is a mean over at least one.")
    samples = tuple(record.correctness_samples for record in records)
    flat = [value for seed in samples for value in seed]
    ignored = [float(value) for record in records for value in record.ignored_samples]
    combined_samples = tuple(record.combined_samples for record in records)
    combined = [value for seed in combined_samples for value in seed]
    seeding = _measured([record.seeding_cost for record in records])
    probing = _measured([record.probe_cost for record in records])
    seeding_input = _measured([record.seeding_input_cost for record in records])
    seeding_billed = _measured([record.seeding_input_tokens for record in records])
    seeding_cached = _measured([record.seeding_cached_tokens for record in records])
    probe_billed = _measured([record.probe_input_tokens for record in records])
    probe_cached = _measured([record.probe_cached_tokens for record in records])
    return CellStats(
        strategy=strategy,
        records=tuple(records),
        cost=fmean(record.cost for record in records),
        cost_spread=_spread([record.cost for record in records]),
        input_cost=fmean(record.input_cost for record in records),
        seeding_cost=None if seeding is None else fmean(seeding),
        probe_cost=None if probing is None else fmean(probing),
        seeding_input_cost=None if seeding_input is None else fmean(seeding_input),
        seeding_cost_spread=None if seeding is None else _spread(seeding),
        summarizer_cost=fmean(record.summarizer_cost for record in records),
        input_tokens=fmean(record.input_tokens for record in records),
        cached_tokens=fmean(record.cached_tokens for record in records),
        output_tokens=fmean(record.output_tokens for record in records),
        seeding_input_tokens=None if seeding_billed is None else fmean(seeding_billed),
        seeding_cached_tokens=None if seeding_cached is None else fmean(seeding_cached),
        probe_input_tokens=None if probe_billed is None else fmean(probe_billed),
        probe_cached_tokens=None if probe_cached is None else fmean(probe_cached),
        calls=fmean(record.calls for record in records),
        messages_left=fmean(record.messages_left for record in records),
        messages_peak=fmean(record.messages_peak for record in records),
        prompt_tokens_final=fmean(record.prompt_tokens_final for record in records),
        prompt_tokens_peak=fmean(record.prompt_tokens_peak for record in records),
        seed_prompt_tokens=fmean(record.seed_prompt_tokens for record in records),
        facts_left=fmean(record.facts_left for record in records),
        # The largest, not the mean: every seed of a cell plants the same number of facts, so
        # a smaller one is a seed that failed before scoring rather than an easier scenario.
        facts_total=max((record.facts_total for record in records), default=0),
        nofetch=fmean(record.nofetch for record in records),
        ignored=fmean(ignored or [0.0]),
        correctness=fmean(flat) if flat else 0.0,
        seed_spread=_seed_spread(samples),
        probe_spread=_probe_spread(samples),
        combined=fmean(combined) if combined else 0.0,
        combined_spread=_probe_spread(combined_samples),
        disqualified=fmean(1.0 if record.disqualified else 0.0 for record in records),
        rate_limit_retries=sum(record.rate_limit_retries for record in records),
        throttled_seconds=sum(record.throttled_seconds for record in records),
        connection_retries=sum(record.connection_retries for record in records),
        connection_seconds=sum(record.connection_seconds for record in records),
        samples=samples,
        combined_samples=combined_samples,
    )


def _control_message_gap(cells: Sequence[CellStats], control: str = "none") -> int | None:
    """Return how far the control's conversation sits from the one every strategy row ran.

    Every row of a cell is driven from the same user-side turn list, and compaction only ever
    adds messages to the stored history -- it excludes and rewrites in place rather than
    deleting, and what it adds is its own: a summary, or the call that fetches a record back.
    So the leanest strategy row is the turn list at its own size, and the control, which adds
    nothing, has to equal it. It cannot legitimately come in under it.

    When it does, the control ran a shorter conversation than everything it is the baseline
    for, and every ``vs none`` in the cell is a comparison between two different workloads.
    Measured at 120,000/0.86 before :class:`IdentifiedHistoryProvider`: the control peaked at
    82 messages where every strategy row peaked at 109, and ``anchored``, inert at that cell,
    finished with a snapshot 5.4% larger than the baseline it should have matched.

    Compared against the *minimum* rather than every row, because a strategy is free to sit
    above the turn list and two of them do. The reading assumes the cell measured at least one
    strategy that adds nothing of its own, which every default strategy list does.

    Args:
        cells: Every aggregated cell.
        control: Name of the uncompacted baseline.

    Returns:
        The control's peak message count less the leanest strategy row's, or None when the two
        agree or when the cell holds no control or no strategy row to check it against.
    """
    baseline = next((cell for cell in cells if cell.strategy == control), None)
    others = [cell for cell in cells if cell.strategy != control]
    if baseline is None or not others:
        return None
    gap = round(baseline.messages_peak) - round(min(cell.messages_peak for cell in others))
    return gap or None


def _excluded_cells(
    cells: Sequence[CellStats], control: str = "none"
) -> tuple[set[str], set[str], set[str]]:
    """Return the cells that did not finish, overran the tried limit, or ran another conversation.

    Disqualified rather than starred. A row whose prompt exceeded the limit it stands in for
    is not a slightly worse row: it is a row a model of that size would have refused. Ranking
    against one is what made every "+18% versus not compacting" at 60,000 tokens a comparison
    with a baseline that ran at 78,003 and could not have existed.

    A run that stopped early is excluded for the opposite reason: it spent almost nothing and
    answered almost nothing, so it ranks as "100% cheaper" for having died.

    The third exclusion is the same objection aimed at the baseline itself. When
    :func:`_control_message_gap` fires, the control is not the conversation the strategies ran,
    so it is excluded -- and since every ranking in this table is relative to it, excluding it
    is what stops the cell being ranked at all. The alternative, excluding the strategy rows
    instead, would put the flag on every row but the one that is wrong.

    Args:
        cells: Every aggregated cell.
        control: Name of the uncompacted baseline.

    Returns:
        The names that did not finish, the names that were disqualified, and the name of the
        control when its conversation diverged from the strategy rows'.
    """
    incomplete = {
        cell.strategy
        for cell in cells
        if any(
            record.turns_completed < record.turns_total
            or record.error is not None
            or record.summarizer_failures > 0
            for record in cell.records
        )
    }
    oversized = {cell.strategy for cell in cells if cell.disqualified > 0}
    diverged = {control} if _control_message_gap(cells, control) is not None else set[str]()
    return incomplete, oversized, diverged


def _verdict_outcomes(
    cells: Sequence[CellStats],
    incomplete: set[str],
    oversized: set[str],
    diverged: set[str],
    *,
    split: bool,
    control: str = "none",
) -> tuple[list[JointOutcome], bool]:
    """Return the rows the verdict is taken over, and whether the control is one of the options.

    Every row out of the ranking is out of the verdict, with one exception: a control that
    overflowed the limit but otherwise ran the strategies' whole conversation. It is no longer
    an option -- a model of this size would have refused it -- but its accuracy is still the only
    reading of what that conversation held, so it goes to the verdict as the anchor of the
    retention bar and nothing else. A control that did not finish, or that ran a different
    conversation, anchors nothing and stays out.

    Args:
        cells: Every aggregated cell.
        incomplete: What :func:`_excluded_cells` found did not finish.
        oversized: What it found overran the tried limit.
        diverged: The control, when its conversation diverged.

    Keyword Args:
        split: What :func:`_split_measured` said about the cell.
        control: Name of the uncompacted baseline.

    Returns:
        The outcomes to hand :func:`recommend`, and its ``baseline_admissible``.
    """
    excluded = incomplete | oversized | diverged
    ranked = [_to_joint(cell, split=split) for cell in cells if cell.strategy not in excluded]
    if control in oversized and control not in incomplete | diverged:
        ranked += [_to_joint(cell, split=split) for cell in cells if cell.strategy == control]
        return ranked, False
    return ranked, True


def _split_measured(cells: Sequence[CellStats]) -> bool:
    """Return whether this cell can say what its probing cost, and so be ranked on its workload.

    A property of the cell and not of a row. Rows ranked on two different halves of the money
    would be ordered on two different questions, and nothing in the printed column would say
    which row was which, so one seed anywhere in the cell that predates the split takes the
    whole cell back to its billed total.

    Args:
        cells: Every row of one cell.

    Returns:
        True when every row counted its probe phase apart from its seeding.
    """
    return bool(cells) and all(cell.seeding_cost is not None for cell in cells)


def _ranked_cost(stats: CellStats, *, split: bool) -> float:
    """Return the cost this row is ordered, compared and recommended on.

    Args:
        stats: The row.

    Keyword Args:
        split: What :func:`_split_measured` said about the cell this row belongs to.

    Returns:
        The seeding cost -- the workload -- when the cell measured it, and the billed total
        when it did not, which is the older and dirtier reading and is labelled as such
        wherever it appears.
    """
    return stats.cost if not split or stats.seeding_cost is None else stats.seeding_cost


def _to_joint(stats: CellStats, *, split: bool = True) -> JointOutcome:
    """Convert an aggregated cell into the shape the joint verdict already understands.

    The verdict ranks on ``correctness``, which here is the mean over every sample of every
    seed. ``score`` carries no outcomes: every count the table prints comes from
    :class:`CellStats`, and ``JointOutcome`` consults its score only when there are no samples
    to rank on -- which cannot happen here, since scoring yields at least one reading even for
    a seed that never answered. The field exists for the replay paths, which read a cell once.

    ``cost`` is the workload rather than the invoice, so ``recommend`` and its saving fraction
    describe the money a deployed agent would move. The verdict is a general function of
    whatever cost it is handed; deciding which cost that is belongs here, beside the table that
    has to say the same thing.

    Args:
        stats: The row.

    Keyword Args:
        split: What :func:`_split_measured` said about the cell this row belongs to.

    Returns:
        The row in the verdict's own shape.
    """
    return JointOutcome(
        strategy=stats.strategy,
        cost=_ranked_cost(stats, split=split),
        input_tokens=round(stats.input_tokens),
        cached_tokens=round(stats.cached_tokens),
        messages_left=round(stats.messages_left),
        # The peak, not an uncompacted total: with real replies there is no single "what it
        # would have been" shared across rows, and the peak is what this run actually reached.
        messages_total=round(stats.messages_peak),
        score=RecallScore(
            outcomes=(),
            answer=stats.records[0].answer,
            messages_left=round(stats.messages_left),
            messages_total=round(stats.messages_peak),
            error=stats.records[0].error,
        ),
        correctness_samples=tuple(value for seed in stats.samples for value in seed),
    )


#: Correctness range, in points, above which the accuracy column cannot rank anything.
#: Measured on the uncompacted control at 60,000 tokens: 78 points under the harness's own
#: default narration guidance, 9 with narration suppressed and 15 with it demanded. A control
#: that swings by more than this is choosing between two behaviours, not measuring one.
MAX_USABLE_CORRECTNESS_RANGE: Final[float] = 20.0


def _accuracy_note(correctness_range: dict[str, float], control: str, repeats: int) -> list[str]:
    """Return a warning when the control's own correctness is too unstable to rank against.

    The cost axis has been policed by :func:`_stability_note` since the beginning; the
    accuracy axis was not, and it silently produced three unusable matrices. The accuracy
    column is a mean, which is honest about the middle and says nothing about the shape: a
    control scoring 100, 22 and 22 prints an unremarkable 48 unless something says otherwise.

    Args:
        correctness_range: Points between the least and most correct seed, per strategy.
        control: Name of the uncompacted baseline.
        repeats: Seeds per strategy.

    Returns:
        Zero or two lines, matching the shape of the cost warning.
    """
    if repeats < 2:
        return []
    swing = correctness_range.get(control, 0.0)
    if swing <= MAX_USABLE_CORRECTNESS_RANGE:
        return []
    return [
        "",
        (
            f"ACCURACY NOT RANKABLE: acc1 seeds of the uncompacted control varied by "
            f"{swing:.0f} points, over the {MAX_USABLE_CORRECTNESS_RANGE:.0f}-point limit. "
            "Nothing can be compared against a baseline that unstable. The cost columns are "
            "unaffected."
        ),
    ]


def _fill_note(stats: dict[str, CellStats], plan: FillPlan | None, control: str) -> list[str]:
    """Return what the uncompacted run actually filled, and a warning if it missed.

    The fill fraction is only a variable if the conversation lands on it. It is measured on
    the uncompacted control because that is the one row whose context is whatever the
    conversation put there; every other row is by definition somewhere below it.

    The tool share is reported the same way and against the same denominator, so the two lines
    can be read together. Its numerator is the plan's count of the tool results rather than a
    billed figure: nothing on the wire separates a tool result from the turn around it, and
    the results are the one part of the conversation this package generates itself and can
    therefore count exactly. The denominator is billed, so a share that misses is the same
    kind of miss as a fill that does -- the conversation was not the size it was solved for.

    Args:
        stats: Aggregated cells.
        plan: The sizing that was solved for, or None when sizing was manual.
        control: Name of the uncompacted baseline.

    Returns:
        One line per targeted quantity, each followed by a warning when it missed.
    """
    if plan is None or control not in stats:
        return []
    achieved = stats[control].seed_prompt_tokens
    if achieved <= 0:
        return [
            "",
            (
                "FILL UNKNOWN: the provider reported no prompt sizes, so the achieved fill cannot "
                "be checked."
            ),
        ]
    deviation = (achieved - plan.target_tokens) / plan.target_tokens
    lines = [
        "",
        (
            f"Fill: {achieved:,.0f} tokens seeded against a target of {plan.target_tokens:,} "
            f"({plan.fill_fraction:.0%} of {plan.context_limit:,}), {deviation:+.1%}. "
            f"Payload {plan.payload_tokens:,} tokens, of which {plan.tool_payload_tokens:,} is "
            f"tool results."
        ),
    ]
    if abs(deviation) > FILL_TOLERANCE:
        lines.append(
            f"FILL OFF TARGET: {deviation:+.1%} is outside the {FILL_TOLERANCE:.0%} tolerance, "
            f"so this "
            "cell is not the fill fraction it is labelled with and does not sit on the same "
            "axis as "
            "the others. The replies are the one term the sizing cannot compute; adjust "
            "--filler-tokens or re-solve against a measured reply size."
        )
    # A fill past the window is sized so that the control overflows it, and every other reading
    # of this cell depends on that having happened on every seed: a seed that stayed under the
    # limit is an ordinary cell's control mixed into one that is not.
    if plan.target_tokens > plan.context_limit and stats[control].disqualified < 1.0:
        overflowed = round(stats[control].disqualified * len(stats[control].records))
        lines.append(
            f"FILL PAST WINDOW NOT REACHED: this cell was sized to overflow the "
            f"{plan.context_limit:,}-token "
            f"limit, and the control exceeded it on {overflowed} of "
            f"{len(stats[control].records)} seeds. "
            "The seeds that fit ran the regime the cell was sized to leave, so it is not the "
            "cell it is "
            "labelled with; raise --fill or re-solve against a measured reply size."
        )
    if plan.tool_share <= 0:
        return lines
    achieved_share = plan.tool_payload_tokens / achieved
    share_deviation = (achieved_share - plan.tool_share) / plan.tool_share
    lines.append(
        f"Tool share: {plan.tool_payload_tokens:,} tokens of tool results is "
        f"{achieved_share:.1%} of "
        f"what was seeded, against {plan.tool_share:.0%} requested, {share_deviation:+.1%}. Each "
        f"result was built to ~{plan.tool_result_tokens:,} tokens."
    )
    if abs(share_deviation) > FILL_TOLERANCE:
        lines.append(
            f"TOOL SHARE OFF TARGET: {share_deviation:+.1%} is outside the {FILL_TOLERANCE:.0%} "
            "tolerance, so the payload is not the share of the context this cell is labelled with "
            "and does not compare with cells at other window sizes. The tool results are sized "
            "exactly; a share that misses means the conversation around them did, so read the fill "
            "line above first."
        )
    return lines


def _divergence_note(message_gap: int | None, control: str) -> list[str]:
    """Return the lines saying the control did not run the strategies' conversation.

    Its own block rather than a flag alone, because what it invalidates is not one row. Every
    cost figure in the cell is relative to this baseline, so a baseline carrying a different
    number of messages makes all of them comparisons between two workloads -- and a reader
    scanning the ``vs none$`` column would find question marks with nothing saying why.

    Args:
        message_gap: What :func:`_control_message_gap` found, or None when it found nothing.
        control: Name of the uncompacted baseline.

    Returns:
        Zero lines when the conversations matched, otherwise the finding and what it costs.
    """
    if message_gap is None:
        return []
    direction = "fewer" if message_gap < 0 else "more"
    return [
        "",
        (
            f"CONTROL DIVERGED: {control!r} carried {abs(message_gap)} {direction} messages at its "
            f"peak than the"
        ),
        "leanest strategy row, on the same user-side turn list. Compaction only ever adds to the",
        "stored history, so those two counts have to match; they do not, which means the baseline",
        (
            "and the rows measured against it are two different conversations. Every cost "
            "comparison in"
        ),
        (
            "this cell is withdrawn and the control is out of the ranking. This is not "
            "correctable after"
        ),
        (
            "the fact -- the conversation the control ran is the one on the record -- so the cell "
            "has to"
        ),
        "be re-run to be read on cost.",
    ]


def _split_note(cells: Sequence[CellStats], split: bool) -> list[str]:
    """Return the lines saying which money the cost columns describe.

    Stated once per cell rather than left to the legend, because two tables printed by one
    version of this code can be ranked on two different quantities, and nothing inside the
    columns says which. A reader placing an old cell beside a new one has to be told.

    Args:
        cells: The rows, in the order they appear in the table.
        split: What :func:`_split_measured` said about them.

    Returns:
        Two or more lines, always: a table that says nothing here is the ambiguity this exists
        to remove.
    """
    if split:
        return [
            "",
            (
                "Cost: seed$ is the conversation and probe$ is the instrument, priced apart. The "
                "ranking,"
            ),
            (
                "the verdict and vs none$ are all on seed$, because every probe re-sends the "
                "whole snapshot"
            ),
            (
                "and a strategy that compacted hard would otherwise collect that discount once "
                "per probe."
            ),
            (
                "Cache: seed hit% and probe hit% split the same tokens the same way. Quote seed "
                "hit% for"
            ),
            (
                "what compaction did; probe hit% is the instrument's own draw and says nothing "
                "about the"
            ),
            "workload, and run hit% mixes the two.",
        ]
    missing = ", ".join(sorted(cell.strategy for cell in cells if cell.seeding_cost is None))
    return [
        "",
        (
            f"NO PHASE SPLIT ({missing}): these records counted seeding and probing in one total, "
            f"so seed$,"
        ),
        (
            "probe$, seed hit% and probe hit% cannot be recovered from them -- the per-probe "
            "prompts are"
        ),
        (
            "not on the record, and pricing twelve probes at the final prompt's size would be a "
            "model of"
        ),
        (
            "the run rather than the run. The ranking above is therefore on run$, which includes "
            "twelve"
        ),
        (
            "re-reads of the snapshot that no deployed agent pays for and which favour whichever "
            "strategy"
        ),
        "compacted hardest, and the only cache figure is run hit%, which mixes those re-reads in.",
    ]


def _throttle_note(cells: Sequence[CellStats]) -> list[str]:
    """Return the lines reporting what throttling cost this cell in time.

    The flag says a row met a rate limit; this says how much of the row's wall clock went
    into it. Worth its own lines because the retries are invisible in every other column
    while being able to move one of them: a prompt cache that expired during a minute of
    backoff is a miss the hit-rate column reads as compaction breaking the prefix.

    Args:
        cells: The rows, in the order they appear in the table.

    Returns:
        Zero lines when nothing was throttled, otherwise a heading and one line per row.
    """
    throttled = [cell for cell in cells if cell.rate_limit_retries]
    if not throttled:
        return []
    return [
        "",
        "Throttled: the provider refused these calls for rate reasons and they were re-sent",
        "after a wait. The measurement is unchanged; the wall clock is not, and neither is the",
        "cache hit rate if a prefix expired while a call was waiting.",
        *(
            f"  {cell.strategy:<28}{cell.rate_limit_retries} retries, "
            f"{cell.throttled_seconds:,.0f}s waiting"
            for cell in throttled
        ),
    ]


def _reconnect_note(cells: Sequence[CellStats]) -> list[str]:
    """Return the lines reporting what connection failures cost this cell in time.

    Its own note rather than a line in the throttling one, because the reading is different.
    Throttling is the account being at its limit and says something about how the sweep was
    scheduled; a dropped connection says nothing about the measurement at all, only that the
    run survived something that would otherwise end it with every row ``ERR`` and no turns
    completed.

    The waits are seconds rather than minutes, so unlike a throttled row this one is unlikely
    to have lost its cached prefix. Unlikely is not the same as measured, which is why the
    count is on the row and the seconds are here.

    Args:
        cells: The rows, in the order they appear in the table.

    Returns:
        Zero lines when nothing was re-sent, otherwise a heading and one line per row.
    """
    reconnected = [cell for cell in cells if cell.connection_retries]
    if not reconnected:
        return []
    return [
        "",
        "Reconnected: these calls never came back with an answer and were re-sent from the",
        "state the turn began in. Each one survived a seed that would otherwise have ended at",
        "the turn it happened on, taking every turn already paid for with it.",
        *(
            f"  {cell.strategy:<28}{cell.connection_retries} retries, "
            f"{cell.connection_seconds:,.0f}s waiting"
            for cell in reconnected
        ),
    ]


def _runner_up_note(verdict: JointVerdict, spread: dict[str, float]) -> list[str]:
    """Return the noise warning for a verdict whose control overflowed the limit.

    With no affordable control there is no saving to test, so the margin a recommendation rests
    on is the one to the next cheapest row that also cleared the bar -- the alternative a reader
    would actually pick instead. The same guard the cross-cell ranking applies.

    Args:
        verdict: The recommendation, taken with an inadmissible baseline.
        spread: Seed spread on the ranked cost, per strategy.

    Returns:
        Zero or two lines.
    """
    chosen, base = verdict.chosen, verdict.baseline
    if verdict.recommended is None:
        return []
    runner_up = next(
        (
            outcome
            for outcome in verdict.outcomes
            if outcome.strategy not in {chosen.strategy, base.strategy}
            and relative_correctness(outcome, base) >= verdict.min_correctness
        ),
        None,
    )
    if runner_up is None or runner_up.cost <= 0:
        return []
    margin = (runner_up.cost - chosen.cost) / runner_up.cost
    worst = max(spread.get(chosen.strategy, 0.0), spread.get(runner_up.strategy, 0.0))
    if worst <= margin:
        return []
    return [
        "",
        (
            f"NOT SUPPORTED: repeats of one strategy varied by {worst:.0%}, wider than the "
            f"{margin:.0%} "
            f"gap between {chosen.strategy!r} and the next cheapest row that cleared the bar, "
            f"{runner_up.strategy!r}. Treat the choice between them as unresolved."
        ),
    ]


def _stability_note(verdict: JointVerdict, spread: dict[str, float], repeats: int) -> list[str]:
    """Return a warning when the recommendation's margin is inside the measured noise.

    A ranking is only worth reporting if the gap between the options is larger than the gap
    between repeats of the same option. Live cost was measured swinging about 20% on
    identical configuration, mostly from reply length, which is wider than most of the
    differences between strategies.
    """
    if repeats < 2:
        return [
            "",
            (
                "Single seed: nothing here measures compaction's own reliability. Re-run with "
                "--repeats 3."
            ),
        ]
    if not verdict.baseline_admissible:
        return _runner_up_note(verdict, spread)
    chosen, base = verdict.chosen, verdict.baseline
    if chosen.strategy == base.strategy or base.cost <= 0:
        return []
    margin = abs(base.cost - chosen.cost) / base.cost
    worst = max(spread.get(chosen.strategy, 0.0), spread.get(base.strategy, 0.0))
    if worst > margin:
        note = (
            f"NOT SUPPORTED: repeats of one strategy varied by {worst:.0%}, wider than the "
            f"{margin:.0%} gap this recommendation rests on. Treat the cost ranking as unresolved."
        )
        return ["", note]
    return []


def _flags(
    stats: CellStats, control: CellStats | None, *, message_gap: int | None = None
) -> list[str]:
    """Return the short tokens the flags column carries for one row.

    Args:
        stats: The row.
        control: The uncompacted baseline, or None when the file being read does not hold it.

    Keyword Args:
        message_gap: What :func:`_control_message_gap` found for this cell, when it found
            anything. Carried in rather than derived here because it is a fact about the cell
            and this function sees one row of it.
    """
    flags: list[str] = []
    # A row that gathered a different set of facts than the control is not comparable to
    # it on either axis: it has a different denominator for correctness and a different
    # token volume for cost. Measured at 25% more input for runs that fetched every tool.
    if control is not None and round(stats.nofetch) != round(control.nofetch):
        flags.append("FETCH")
    # The same objection, one level up: this row *is* the control, and the conversation it ran
    # is not the one the strategies ran. Sits on the control rather than on the rows that
    # differ from it, because the rows that differ from it are all of them.
    if message_gap is not None and control is not None and stats.strategy == control.strategy:
        flags.append(f"MSGS:{message_gap:+d}")
    # A row that cannot say what its probing cost cannot be ranked on its workload, so its
    # money columns are the invoice: seeding, twelve re-reads of the snapshot, and no way to
    # tell which is which.
    if stats.seeding_cost is None:
        flags.append("NOSPLIT")
    dropped = {option for record in stats.records for option in record.dropped_options}
    if dropped:
        flags.append("NO:" + ",".join(sorted(option[:4] for option in dropped)))
    if any(record.error for record in stats.records):
        flags.append("ERR")
    drift = sum(record.context_drift for record in stats.records)
    if drift:
        flags.append(f"DRIFT:{drift}")
    if stats.rate_limit_retries:
        flags.append(f"THROTTLED:{stats.rate_limit_retries}")
    if stats.connection_retries:
        flags.append(f"RECONNECTED:{stats.connection_retries}")
    failures = sum(record.summarizer_failures for record in stats.records)
    if failures:
        flags.append(f"S{failures}")
    for note in sorted({note for record in stats.records for note in record.strategy_notes}):
        flags.append(note)
    incomplete = [record for record in stats.records if record.turns_completed < record.turns_total]
    if incomplete:
        flags.append(f"{incomplete[0].turns_completed}/{incomplete[0].turns_total}t")
    return flags


def _sample_groups(samples: Sequence[Sequence[float]]) -> str:
    """Return one seed's readings per bracket, for the blocks printed under the table.

    Args:
        samples: One group of readings per seed.

    Returns:
        The groups, or an empty string when no seed was read.
    """
    return "  ".join(
        "[" + " ".join(f"{value:.0%}" for value in seed) + "]" for seed in samples if seed
    )


def _facts_groups(records: Sequence[SeedRecord], total: int) -> str:
    """Return each seed's surviving facts, for the block printed under the table.

    The column is a mean over seeds, and on the record row the seeds do not spread around
    it: a seed holds every fact or drops a whole group's worth, so a cell at 50/53 is four
    seeds at 53 and one at 37 and no seed near 50. Printed for a row whose seeds disagree,
    in the shape the acc blocks use, so the reader can see which seed it was.

    Args:
        records: The seeds, in the order they were recorded.
        total: Facts the cell planted, so every reading is against one denominator.

    Returns:
        One bracketed group of per-seed counts, followed by the denominator.
    """
    return "[" + " ".join(str(record.facts_left) for record in records) + f"] of {total}"


def _money(value: float | None) -> str:
    """Return a cost, or ``?`` when the records behind the row never measured it.

    A question mark rather than a dash, and never a number: the dash in this table means "not
    applicable to this row", which the control's own ``vs none`` is, and a run that did not
    count something is a different statement from a run to which it does not apply.

    Args:
        value: The cost, or None when it was not measured.

    Returns:
        The rendered cell.
    """
    return "?" if value is None else "$" + format(value, ".4f")


def _rate(cached: float | None, billed: float | None) -> str:
    """Return a cache hit rate, or ``?`` when the records behind it never measured it.

    The money convention applied to the cache columns: ``?`` is a record that counted its
    phases in one total, ``n/a`` a phase that billed nothing, and the two are different
    statements. One decimal rather than none, because the probe half takes values a whole
    percent cannot separate -- 33.3% is four probes of twelve and 99.6% is all of them, and
    a seeding half that moved by a point is a finding at five seeds.

    Args:
        cached: Tokens the provider served from cache, or None when not measured.
        billed: Input tokens billed, or None when not measured.

    Returns:
        The rendered cell.
    """
    if cached is None or billed is None:
        return "?"
    return "n/a" if billed <= 0 else f"{cached / billed:.1%}"


def _hit_pairs(records: Sequence[SeedRecord]) -> str:
    """Return each seed's seeding and probe hit rates, for the block printed under the table.

    The column is a mean over seeds and the probe half is not a spread but a draw between two
    values, so a row at 46.6% on it is seeds at 33.3% and seeds at 99.6% and nothing at 46.6%.
    Beside it, the seeding half of the same seeds, so the reader can see that it did not move
    with the draw.

    Args:
        records: The seeds, in the order they were recorded.

    Returns:
        One bracketed ``seeding/probe`` pair per seed, in the shape the acc blocks use.
    """
    return "  ".join(
        f"[{_rate(record.seeding_cached_tokens, record.seeding_input_tokens)}"
        f"/{_rate(record.probe_cached_tokens, record.probe_input_tokens)}]"
        for record in records
    )


def _probe_groups(records: Sequence[SeedRecord]) -> str:
    """Return each seed's per-probe hit rates, in the order the probes were asked.

    The observation behind the probe half: on a record at 33.3% these read eight cold and four
    warm, or they read twelve at a third, and the totals cannot tell the two apart.

    Args:
        records: The seeds, in the order they were recorded.

    Returns:
        One bracketed group per seed, or ``not recorded`` for a seed written before the
        per-probe counts existed.
    """
    groups: list[str] = []
    for record in records:
        samples = record.probe_hit_samples
        if samples is None:
            groups.append("not recorded")
        else:
            groups.append(
                "["
                + " ".join("n/a" if value is None else f"{value:.0%}" for value in samples)
                + "]"
            )
    return "  ".join(groups)


def _row(
    stats: CellStats,
    control: CellStats | None,
    excluded: bool,
    limit: int,
    *,
    message_gap: int | None = None,
) -> str:
    """Render one strategy's line of the table.

    Args:
        stats: The row.
        control: The uncompacted baseline, or None when the file being read does not hold it,
            in which case both relative columns read as unknown rather than being computed
        limit: The tried context window, which ``snap%`` is a share of
            against whichever row happened to be first.
        excluded: Whether this row is out of the ranking.

    Keyword Args:
        message_gap: What :func:`_control_message_gap` found for this cell, when it found
            anything. Suppresses the cost comparison, which is the column it invalidates.
    """
    ranked, control_ranked = stats.seeding_cost, None if control is None else control.seeding_cost
    if control is None or stats.strategy == control.strategy or message_gap is not None:
        cost_delta = "-" if message_gap is None else "?"
    elif control.disqualified > 0:
        # A control over the limit is a conversation no model of this size would have run, so
        # not compacting is not an option and there is no affordable baseline to price against.
        # Its accuracy still anchors ``vs none``; its money anchors nothing.
        cost_delta = "?"
    elif ranked is None or control_ranked is None:
        # One of the two rows cannot say what its probing cost, so the only comparison
        # available is between two invoices, and that is the comparison being withdrawn.
        cost_delta = "?"
    else:
        cost_delta = "-" if control_ranked <= 0 else f"{ranked / control_ranked - 1:+.0%}"
    if control is None or stats.strategy == control.strategy or control.correctness <= 0:
        relative = "-"
    else:
        relative = f"{stats.correctness / control.correctness:.0%}"
    # The seeding half first, because it is the number that describes a deployed agent; the
    # probe half beside it, because a whole-run figure is unreadable without knowing which
    # of its two values the instrument drew; and the whole-run figure last, kept for the
    # reason run$ is.
    seed_hit = _rate(stats.seeding_cached_tokens, stats.seeding_input_tokens)
    probe_hit = _rate(stats.probe_cached_tokens, stats.probe_input_tokens)
    run_hit = _rate(stats.cached_tokens, stats.input_tokens)
    flags = _flags(stats, control, message_gap=message_gap)
    # ``DQ`` is the dq column crossing zero and nothing else, not "excluded from the ranking",
    # which is a wider set: a row that failed a turn is excluded too, and under one name a table
    # shows rows flagged DQ beside a dq of 0% because every one of them died on a rate limit.
    # Two exclusions with one name make the flag unreadable exactly when it matters, so the
    # other reason has its own token.
    if stats.disqualified > 0:
        flags.insert(0, "DQ")
    elif excluded:
        flags.insert(0, "EXCL")
    summ = stats.summarizer_cost
    lost = max(stats.facts_total - stats.facts_left - stats.nofetch, 0.0)
    # What compaction actually left standing when the questions began, as a share of the
    # window the strategies were configured against. The absolute token counts beside it do
    # not say that on their own: a strategy is only reading as "compacted hard" relative to
    # the ceiling it was told about, and that ceiling differs per cell.
    snap = f"{stats.seed_prompt_tokens / limit:.0%}" if limit else "n/a"
    spread_text = "?" if stats.seeding_cost_spread is None else f"{stats.seeding_cost_spread:.0%}"
    return (
        f"{stats.strategy:<28}{f'{stats.messages_left:.0f}/{stats.messages_peak:.0f}':>9}"
        f"{f'{stats.prompt_tokens_final:,.0f}/{stats.prompt_tokens_peak:,.0f}':>16}"
        f"{snap:>7}"
        f"{stats.calls:>7.0f}{stats.input_tokens:>12,.0f}{seed_hit:>11}{probe_hit:>12}{run_hit:>10}"
        f"{stats.output_tokens:>10,.0f}{_money(stats.seeding_input_cost):>10}"
        f"{_money(stats.seeding_cost):>9}{_money(stats.probe_cost):>9}{_money(stats.cost):>9}"
        f"{spread_text:>8}"
        f"{('-' if not summ else '$' + format(summ, '.4f')):>8}{cost_delta:>10}"
        f"{f'{stats.facts_left:.0f}/{stats.facts_total}':>9}{lost:>6.0f}"
        f"{stats.nofetch:>8.0f}{stats.ignored:>8.0f}"
        f"{stats.correctness:>8.0%}"
        f"{'*' if control is not None and stats.strategy == control.strategy else ' '}"
        f"{f'{stats.seed_spread:.0f}pp':>8}{f'{stats.probe_spread:.0f}pp':>7}"
        f"{stats.combined:>6.0%}{f'{stats.combined_spread:.0f}pp':>8}"
        f"{relative:>9}{stats.disqualified:>5.0%}"
        f"{(','.join(flags) or '-'):>10}"
    )


_LEGEND: Final[tuple[str, ...]] = (
    "msgs      = messages in a probe's prompt, out of the most any call carried. Every probe",
    "            is asked from the same restored snapshot, so this no longer drifts downwards",
    "            through the questions the way it did when they were ordinary turns",
    "snap%     = the snapshot every question was asked from, as a share of the tried context",
    "            window. This is how hard compaction acted: the control sits at the fill the",
    "            cell was sized to, and a strategy below it removed that difference",
    "tok       = billed tokens in that same prompt, and at the peak. Watch this rather than",
    "            msgs: a strategy that rewrites content in place removes tokens without",
    "            removing messages, and msgs cannot see it",
    "calls     = model calls, seeding and probes together",
    "in        = input tokens billed across the whole run",
    "seed hit%  = share of the conversation's input tokens served from the provider's cache:",
    "            seeding only, the probes taken out. This is what compaction did to the cache,",
    "            and the number to quote for it -- a deployed agent continues its conversation",
    "            and has no probe phase. Compaction breaks the cached prefix by construction,",
    "            so this is what it gives up to save tokens",
    "probe hit% = the same share over the probes: the instrument. Every probe re-sends the",
    "            snapshot from a restored copy, so this measures whether the provider served",
    "            that snapshot to a prompt it had already seen, and on the archived luna cells",
    "            from 100,000 tokens up it took one of two values and nothing between: about",
    "            99%, or 33.3%. 33.3% is four of twelve probes served whole and eight served",
    "            cold. The only four with a byte-identical predecessor are repeats two to five",
    "            of the combined question, so it is those four hitting each other and nothing",
    "            else -- the provider served the prefix to an identical prompt and not to a",
    "            sibling that merely shared it. Which value a seed draws goes with whether the",
    "            strategy was still acting on the store as seeding ended: a row at rest by then",
    "            drew high on every seed, a summarizer that fires every turn drew 33.3% on",
    "            every seed, and the rows between drew by the seed. A low value is a fact about",
    "            the instrument and a warning about run hit%, not a cost: seed$, seed hit%,",
    "            retention and accuracy never read it, and nothing is withdrawn by it. The",
    "            per-probe block under the table shows which probes hit, where the record can",
    "run hit%   = seeding and probes together, cached over input across the whole run: what",
    "            hit% was before the split. Here because it is the number every earlier",
    "            write-up quotes, not because it says what compaction did. The probes are a",
    "            third of a seed's input on the archived cells, so two seeds of one row read",
    "            73.5% and 88.8% here with seeding halves of 85.7% and 85.0%, and a row whose",
    "            seeds all drew low sits eight to thirteen points under its seeding half",
    "out       = output tokens billed across the whole run. Its own column because a total",
    "            driven by how much the model wrote is a different finding from one driven",
    "            by how much context it was sent, and one number cannot show which",
    "seed in$  = the prompt side of seed$: uncached and cached together, with output, the",
    "            summarizer and the probes all left out. The low-variance view of what",
    "            compaction changes: on a clean five-seed control the total moved 38% while",
    "            the input side moved 13%, because output is priced 57x a cache read and the",
    "            model's verbosity swamps the axis compaction acts on",
    "seed$     = what the conversation cost: seeding, plus the strategy's own summarizer",
    "            calls, and nothing else. The workload, and what the ranking, the verdict and",
    "            vs none$ are all taken on. This is the money a deployed agent moves",
    "probe$    = what the probing cost: the instrument. Every probe is asked from the restored",
    "            snapshot, so each one re-sends the whole snapshot, and a strategy that",
    "            compacted hard collects that discount once per probe -- twelve times over on",
    "            a phase no deployed agent has, since an agent continues the conversation",
    "            rather than being interrogated from a frozen state. Folded into the ranking",
    "            it turned one cell's -14.1% into -3.5% and flipped the sign on two others",
    "run$      = seed$ + probe$: what the run was actually billed. Here because it is the",
    "            number every earlier write-up quotes, not because it ranks anything",
    "seed$+-   = spread between the cheapest and dearest seed, on seed$. A gap smaller than",
    "            this is not a result. 0% with one seed means stability is unknown, not that",
    "            it is stable. Taken on seed$ rather than run$ because probing is the steadier",
    "            half -- one snapshot, the same questions -- so a spread on the total",
    "            understates how far the ranked half moved",
    "summ$     = what this strategy's own summarization calls cost, of seed$",
    "vs none$  = seed$ against the uncompacted control's seed$: what compaction moved on the",
    "            workload. '?' means the comparison is unavailable -- either a row could not",
    "            separate its probing from its seeding (NOSPLIT), or the control did not run",
    "            the strategies' conversation (MSGS) and there is nothing to compare against,",
    "            or the control exceeded the tried limit (DQ). Then not compacting is not an",
    "            option a model of this size allows, so there is no affordable baseline and",
    "            every row reads '?': a saving against a run that could not happen is not one",
    "'?'       = in any money or cache column, the records behind this row never measured",
    "            that quantity. Records written before schema 4 counted their calls in one",
    "            total, and no arithmetic over what they stored can separate the phases --",
    "            pricing twelve probes at the final prompt's size is a model of the run, not",
    "            the run. Those rows carry NOSPLIT, show run$ and run hit% alone, and are",
    "            ranked on run$",
    "facts     = planted facts surviving compaction into the snapshot: recall's ceiling.",
    "            Scored against the snapshot, which is exactly the context every probe was",
    "            answered from. Scored against a closing prompt instead, this was circular:",
    "            each answer re-listed codes into the history, so a code compaction had",
    "            destroyed came back because the model had recited it two questions earlier.",
    "            A mean over seeds, and where the seeds disagree it is a value no seed took:",
    "            the record row's retention is a draw between every fact and a whole group's",
    "            worth fewer, so 50/53 there is four seeds at 53 and one at 37. A row whose",
    "            seeds disagree gets a per-seed line under the table, in the acc blocks'",
    "            shape; a row whose seeds agree does not, because the column already says",
    "            what every seed did",
    "lost      = compaction removed it, so the model could not use it  <- the damage",
    "nofetch   = the agent never called that tool, so the fact never entered the history at",
    "            all. Not compaction damage: an uncompacted run shows these too",
    "ignored   = still in the snapshot but unused: the model's failing, not compaction's",
    "acc1      = the scoped questions -- requirements plus one per tool lookup. Mean share of",
    "            checks passed across every probe repeat of every seed, each reply scored only",
    "            against the values its own question asked for. A star marks the uncompacted",
    "            control, which is ordered by the same rule as every other row and can",
    "            therefore land below the line -- unless it exceeded the tried limit, when it",
    "            is not ranked at all and is printed apart, under the ranked rows, as a",
    "            reference for what the whole conversation cost and retained",
    "seed+-    = points between the least and most correct seed, on acc1. Different",
    "            conversations, so this is compaction's own reliability: whether it cleared a",
    "            retention boundary this time and not last time",
    "rep+-     = points between the least and most correct acc1 repeat *within* one seed,",
    "            averaged over seeds. Identical facts in identical positions, so this is the",
    "            model's willingness to enumerate and nothing else. Kept apart from seed+-: as",
    "            one number, a strategy scoring 52, 52, 52 and 22 with exactly 27 facts",
    "            preserved every time reads the same as one that lost different facts each time",
    "acc2      = the one combined question: share of all planted values present in its answer,",
    "            where the model is asked for everything at once, meaned over every attempt of",
    "            every seed. The same run measured a second way, not a second run -- one",
    "            question against acc1's seven, from a context the values are scattered",
    "            through. Asked from the snapshot like every other probe, so it is no longer",
    "            penalised for having been asked last, and asked --combined-repeats times",
    "            rather than --probe-repeats, since one answer is a whole reading of it",
    "rep2+-    = the same within-seed spread for acc2, over its own attempts. Read it beside",
    "            rep+-: at --probe-repeats 1 that column is 0pp by construction and this one",
    "            is the only within-seed variance the cell measures",
    "vs none   = acc1 against the uncompacted control's acc1, which is also what the ranking",
    "            and the verdict are judged on. Read it together with vs none$ on the left or",
    "            not at all -- cheaper and less correct is not a saving. Still computed when",
    "            the control exceeded the tried limit: its acc1 is the only reading of what the",
    "            whole conversation held, so the bar stays anchored on it, though it was",
    "            measured on a prompt no model of this size would accept",
    "dq        = share of this cell's seeds that sent a prompt larger than the tried limit.",
    "            The limit is simulated, so it is enforced here or not at all. A cell that",
    "            disqualifies at all is excluded from the ranking rather than starred: a row",
    "            a model that size would have refused is not an option and not a price for",
    "            anything. A disqualified control keeps one role, the acc1 anchor above",
    "flags     = DQ the dq column above is not zero, so this row sent a prompt a model of",
    "            this size would have refused. EXCL out of the ranking for the other reason:",
    "            it did not finish its turns. Two names, so a table never shows rows flagged",
    "            DQ beside a dq of 0%. ERR failed turn,",
    "            THROTTLED:<n> calls re-sent after the provider refused them for rate",
    "            reasons; the seconds spent waiting are printed below the table, and they",
    "            matter because a cached prefix that expired during a wait is a miss the seed",
    "            hit% column charges to compaction. RECONNECTED:<n> calls re-sent because the",
    "            request never came back with an answer -- the connection dropped, or the",
    "            provider answered 5xx. Counted apart from THROTTLED because the waits are",
    "            seconds rather than a quota window, so this row's cached prefix is very",
    "            likely intact; without the retry it would not be a row at all, but a seed",
    "            that ended at the turn it happened on. DRIFT:<n> probes whose prompt was",
    "            not the snapshot verbatim, because the strategy acted again on the",
    "            restored state.",
    "            Those probes saw slightly less than survival was scored against, so a row",
    "            carrying this overstates what reached the model. S<n> summarizer failures,",
    "            <n>/<n>t turns",
    "            completed, REC:<n> whether the model ever wrote a record at all -- it",
    "            saturates at 1 and answers compliance, not quantity. RECORDS:<n> how many",
    "            records the conversation ended up carrying, which is a different question",
    "            and became one when the size trigger was allowed to ask more than once.",
    "            Every record is preserved: it may be neither shortened nor dropped, by this",
    "            strategy or by the fallback behind it, and nothing merges them -- an older",
    "            record is the sole account of the groups behind it, so a merge would rewrite",
    "            the evidence rather than the bulk. So each one raises a floor under the",
    "            prompt that no later pass can lower, and a row above 1 here is a row whose",
    "            money columns are partly that floor rather than the workload. Runs up to",
    "            39 were single-record by construction and carry no such number; the default",
    "            reproduces them and --record-repeats is what asks for more. FORCED:<n>",
    "            times it asked for",
    "            one, TRUNCATED:<n> forced calls the provider cut at --record-max-tokens, so",
    "            that record may cover only part of what it was asked to preserve and the",
    "            missing part is scored as compaction damage. UNCOVERED:<n> tool-call groups",
    "            the record never named, which the strategy therefore refused to delete. This",
    "            is the coverage check holding the row back. Alone it is a cost rather than a",
    "            loss: those groups are still in the prompt, so the row paid for tokens a",
    "            complete record would have replaced and lost nothing -- five archived",
    "            records carry it alone and every one held every fact. Beside RECFALLBACK it",
    "            is the opposite. The check keeps an uncovered group but does not preserve",
    "            it, so the fallback that runs when the prompt is still over the ceiling may",
    "            shorten it in place or shed it whole like any other group in its band, and",
    "            the four record rows in the archive that lost a fact all read UNCOVERED:4",
    "            beside RECFALLBACK, while no record carrying either flag alone has lost",
    "            one; three carrying both held, all at 60,000 tokens. Read the flag alone as",
    "            a cost and the pair as where a loss can happen. Read the count as",
    "            how far the model fell short of what the recall tool asked for -- the record",
    "            is required to group its content by the tool that produced it, so a tool it",
    "            never names is a tool it did not account for. Before the check existed those",
    "            groups were deleted anyway and the facts in them arrived in acc1 as",
    "            compaction damage, with nothing in any column saying where they went. A row",
    "            with UNCOVERED alone is not measuring this strategy working; it is measuring",
    "            it declining to guess, and a row with both is measuring the fallback",
    "            deciding for it -- on a row written before schema 13; since then the check",
    "            holds the group, see REFORCED and PRESERVED. The archived losses that",
    "            reading rested on have been re-attributed, and that is stated under REFORCED.",
    "            USERCOMPACT:<n> passes where user_summary_anchored replaced a band of the",
    "            user's own turns with one summary of them, and USERREPLACED:<n> how many",
    "            turns the most recent of those passes stood in for. Read them together: the",
    "            second is how much of the conversation the newest summary is carrying instead",
    "            of verbatim, which is what moved snap%, and the first is what it cost to get",
    "            there, because every pass re-bills the prompt from its own edit to the end.",
    "            USERREPLAY:<n> passes that replaced the same band with the summary already in",
    "            hand: the live path runs the strategy twice per crossing, inside the model",
    "            call on the loaded copies and after the turn on the store, and the second",
    "            pass replays the first's answer instead of asking again, so it costs no",
    "            summarizer call and no new break. One per crossing is the ordinary reading,",
    "            two on a turn that called a tool. Before it existed both passes counted as",
    "            USERCOMPACT, so an archived USERCOMPACT:2 is one compaction.",
    "            Where that edit lands is --user-summary-mode. In the recompact mode the",
    "            strategy re-reads its own earlier summary, so every pass rewrites a message",
    "            just behind the head and USERCOMPACT above 1 is that happening; in the",
    "            boundary and fold modes the earlier summary stands and the edit lands only on",
    "            the turns newer than it. USERSUMMARIES:<n> summaries the conversation was",
    "            carrying at the strategy's last reading, and USERSUMMTOKENS:<n> the tokens",
    "            they occupy: the boundary mode's floor, one summary per pass that no later",
    "            pass can lower, which is RECORDS:<n>'s accumulation on the user half. Reads 1",
    "            in the recompact mode after any pass. USERFOLD:<n> passes of the fold mode",
    "            that collapsed every standing summary into one, each a rewrite of the prefix",
    "            from the oldest summary's position -- the break the recompact mode pays on",
    "            every pass, paid here only when the standing summaries had grown to",
    "            --user-min-band-share of what is behind them. A run that folded twice and",
    "            one that never folded differ by two of those breaks and a floor lowered",
    "            twice, and only this flag separates them.",
    "            A row with USERCOMPACT:0 never fired and is the uncompacted control",
    "            under another name -- and exactly one of the next three flags says why.",
    "            USERUNDER:<n> passes where the prompt never reached",
    "            --user-trigger-fraction, so the band was not even read. USERHELD:<n>",
    "            passes where it did and the band was not worth a pass under",
    "            --user-min-band-share: empty, holding nothing but the strategy's own",
    "            earlier summary, or too small a share of the prompt to pay for the",
    "            prefix a pass rewrites -- and, in the fold mode, the standing summaries",
    "            were not worth a fold either. That flag is the hysteresis working, and a row",
    "            with USERHELD and no USERCOMPACT is one whose band never cleared the",
    "            share -- a setting to change, not a strategy that failed. Before the",
    "            share existed this row fired on most turns: USERCOMPACT:31 (double-counted)",
    "            with USERREPLACED:2 and a seeding-phase hit rate of 77% where the control",
    "            held 95%.",
    "            USERSUMMFAIL:<n> passes where the summarizer raised or returned",
    "            nothing, so the band was left exactly as it was found and those passes",
    "            are the control too. USERSTARVED:<n> (retired at schema 16; only on",
    "            older records) passes of tool_and_user_summary_anchored where the record",
    "            half's removals kept the prompt under the user half's trigger. From schema",
    "            16 that row judges its user half after the record half on purpose, so the",
    "            user half staying idle because tool compaction was enough is the design",
    "            working, and USERUNDER counts it.",
    "            USERWAIT:<n> passes where tool_and_user_summary_anchored held its user half",
    "            back although the prompt was over the line, because the record half had",
    "            tool work a record was due for. The record half compacts in two steps --",
    "            it asks, and drops what the record covers on a later call -- and the user",
    "            half in one, so a user half judged on the asking pass acted first and, by",
    "            taking the prompt under the line, stopped the record ever being asked for:",
    "            RECORDS:0 beside USERCOMPACT:1 on every seed, before schema 17. The wait is",
    "            bounded at two model responses, the deciding call's and the pinned call's,",
    "            and is never begun at the record half's give-up line. Per pass, so one wait",
    "            reads several. Beside a RECORDS that grew it is the wait ending in a record,",
    "            and any USERCOMPACT then acted on what the record left; with no record it",
    "            ran out, and the USERCOMPACT after it is the user half acting anyway.",
    "            tool_and_user_summary_anchored's last-resort chain, which starts only when",
    "            the prompt is over the input budget after both halves and, from schema 19,",
    "            then works down to a target below it (--chain-gain-fraction), reads in order:",
    "            RECMERGE:<n> passes that merged the records into one, and RECMERGEREJ:<n>",
    "            merges refused as no smaller than the records; USERMERGE:<n> passes that",
    "            folded the user summaries into one, and USERMERGEREJ:<n> folds refused;",
    "            RECHARDER:<n> harder rewrites of the record tried, up to",
    "            --record-harder-attempts per pass, and RECHARDERREJ:<n> of them refused;",
    "            RECHARDERSKIP:<n> attempts not made because the same record was already",
    "            refused at them, and RECMERGESKIP:<n> merges not asked for on records a merge",
    "            was refused on: a refusal stands until the record changes, from schema 18;",
    "            RECSUMMFAIL:<n> merges or rewrites the summarizer did not answer;",
    "            LASTFALLBACK:<n> passes that reached the record half's fallback, which on",
    "            this row runs there and nowhere else and may drop narration only. Every",
    "            replacement is kept on size alone -- non-empty and smaller than what it",
    "            replaces -- and never checked against content, so what a merge lost is",
    "            read in facts and acc1, not here. LASTFALLBACK beside DQ is the chain",
    "            exhausted: the intended loud failure. CHAINTARGET:<n> passes the chain",
    "            started on and brought down to its target, and CHAINSHORT:<n> passes every",
    "            step left above it; CHAINKEPT:<n> passes that put back a merge, rewrite, fold",
    "            or shed the chain had made on the live path's other list, which from schema",
    "            19 is kept on both, so RECMERGE and USERMERGE count each decision once.",
    "            REFORCED:<n> calls the recall middleware pinned at the strategy's own request,",
    "            because the standing record left tool groups uncovered: layer one of the",
    "            answer to the gap UNCOVERED beside RECFALLBACK names, where the fallback could",
    "            shorten or shed the very groups the coverage check had kept. The gap is real",
    "            whenever the fallback genuinely fires behind a partial record; how often a",
    "            genuine firing reaches it has not been measured.",
    "            The groups are held out of the fallback's reach from the pass that finds them,",
    "            another record is asked for while they are still whole, and the ask repeats",
    "            only while each record covers at least one of them. PRESERVED:<n> uncovered",
    "            groups the strategy has preserved for good because asking stopped helping --",
    "            the re-forced record covered none of them, or none came: layer two. Read the",
    "            three together. REFORCED without PRESERVED is the re-force clearing the",
    "            shortfall; REFORCED with PRESERVED equal to UNCOVERED is the re-force failing",
    "            and the preservation standing in; UNCOVERED larger than PRESERVED is the",
    "            difference still waiting on an ask when the run ended. A preserved group still",
    "            counts against the ceiling and cannot be shed, so PRESERVED beside DQ is the",
    "            row failing loudly where a fallback free to shorten the group would lose its",
    "            values quietly; that is the intended reading, not a defect in the flag.",
    "            UNCOVERED beside RECFALLBACK does not mean the fallback may have shortened",
    "            those groups: it ran behind the record, over material other than the held",
    "            groups. Layer two protects only what sits in front of the record; a tool",
    "            group after it is covered by no record, which is what RECHELD is for.",
    "            RECHELD:<n> post-record fallback passes that ran with every",
    "            tool group no record covers held out of reach, wherever it sat, so the",
    "            fallback could take narration and nothing else. It counts",
    "            attempts, where RECFALLBACK counts effects: RECHELD with no RECFALLBACK is a",
    "            fallback that found nothing it was allowed to take, which beside DQ is the",
    "            rule standing between it and a quiet loss; RECHELD with RECFALLBACK and no",
    "            DQ is narration having been enough; no RECHELD on a record row is a fallback",
    "            that never had to act. The fallback behind a record cannot",
    "            shorten or shed a tool group no record covers: the row fits on narration or",
    "            reads DQ. What a record row can still lose is what a record's coverage share",
    "            let it drop, which is the record's doing and not the fallback's.",
    "            FALLBACK:<n> times it gave up",
    "            and compacted another way. A row with",
    "            FALLBACK is measuring that other strategy, not the one named.",
    "            RECFALLBACK:<n> passes where a record did exist, was anchored on, and the",
    "            row still fell back: what the record freed left the prompt over the ceiling.",
    "            Counted apart from FALLBACK because the two say different things about the",
    "            model -- FALLBACK is a model that never wrote a record, this is a model that",
    "            wrote one that did not go far enough -- but read them the same way, because",
    "            a non-zero value here means part of what this row measured is the fallback",
    "            strategy and not the one named. It is the quieter of the two: the fallback",
    "            shortens tool results in place, so the row keeps its message count and loses",
    "            its values, and beside UNCOVERED it would be shortening or shedding exactly",
    "            the groups the coverage check had just declined to delete, were those groups",
    "            not held -- see RECHELD. The fallback here may shed narration only, so",
    "            RECFALLBACK does not mean a tool result was shortened. NO:<opt> the",
    "            provider rejected that option so it was dropped; a run that dropped",
    "            tool_choice chose its own tool calls and is not comparable with one that did",
    "            not. FETCH this row gathered a different set of facts than the control.",
    "            MSGS:<+-n> this row is the control and its conversation was n messages away",
    "            from the leanest strategy row's on the same turn list. Compaction only adds",
    "            to the stored history, so the control has to match that row and cannot come",
    "            in under it; when it does, every vs none$ in the cell compares two different",
    "            workloads, and the control is excluded so that none of them is ranked.",
    "            NOSPLIT this row cannot say what its probing cost, so its money columns are",
    "            the invoice rather than the workload, and its only cache figure is run hit%,",
    "            which mixes the probes in",
)


def _table_order(
    cells: Sequence[CellStats], control: CellStats | None, min_correctness: float
) -> tuple[list[CellStats], int]:
    """Return the rows in the order the table prints them, and how many cleared the bar.

    Cost ascending on its own ranks the strategy that threw the conversation away above the one
    that kept it: the cheapest row of a cell is reliably
    the one that destroyed the most. So the rows that still answer come first and the rest
    follow, each group cheapest first on the workload -- ``seed$``, not the invoice, because
    the probe phase discounts a small snapshot once per probe and would order the rows on how
    hard they were interrogated.

    The bar is the verdict's own eligibility test applied to the verdict's own numbers, so the
    split and the recommendation underneath it cannot disagree about which rows are usable.

    Args:
        cells: The rows, in any order.
        control: The uncompacted baseline, or None when the records do not hold it -- in which
            case no row can be judged against it and cost is the whole order again.
        min_correctness: Share of the control's correctness a row must retain to rank first.

    Returns:
        The ordered rows, and how many leading rows cleared the bar.
    """
    split = _split_measured(cells)
    # Without a control there is nothing to be accurate *relative to*, so every row stays in
    # one group and the order is cost alone, as it was before there were two groups.
    base = None if control is None else _to_joint(control, split=split)
    cleared = {
        cell.strategy
        for cell in cells
        if base is None
        or relative_correctness(_to_joint(cell, split=split), base) >= min_correctness
    }
    # The strategy name settles a tie in cost, so that a file read back in a different order
    # from the one the run wrote it in cannot order two rows differently from the live table.
    ordered = sorted(
        cells,
        key=lambda cell: (
            cell.strategy not in cleared,
            _ranked_cost(cell, split=split),
            cell.strategy,
        ),
    )
    return ordered, len(cleared)


def _ranking_note(
    cleared: int,
    total: int,
    control: CellStats | None,
    min_correctness: float,
    *,
    split: bool,
    overflowed_limit: int | None = None,
) -> str:
    """Return the line that says what the table's order means.

    Printed whether or not the split line appears below it: a cell where every row clears the
    bar and one where none does both render as a single block, and without this the reader
    cannot tell which of the two they are looking at, or on what threshold.

    Args:
        cleared: How many rows cleared the bar.
        total: How many rows there are.
        control: The uncompacted baseline, or None when the records do not hold it.
        min_correctness: The bar those rows were judged against.

    Keyword Args:
        split: What :func:`_split_measured` said about this cell. Named in the line rather
            than left to the legend, because the two orders are different rankings and a
            reader comparing this table with another has to know which one they have.
        overflowed_limit: The limit the control exceeded, when it did. The bar is still its
            acc1, and the line says what that acc1 was measured on rather than re-basing it.

    Returns:
        One line.
    """
    basis = "seed$" if split else "run$, probes and all"
    if control is not None and overflowed_limit is not None:
        return (
            f"Ranking: {cleared} of {total} rows kept at least {min_correctness:.0%} of the "
            f"control's "
            f"acc1 and are ranked first, cheapest {basis} first; the rest follow below the "
            f"line. The "
            f"control exceeded the {overflowed_limit:,}-token limit, so it is not ranked and "
            f"is printed "
            "apart as a reference; the bar stays on its acc1, the only reading of what the whole "
            "conversation held, which was measured on a prompt no model of this size would accept."
        )
    if control is None:
        return (
            f"Ranking: {basis} ascending. These records hold no uncompacted control, so no row "
            "can be judged accurate enough to rank above another."
        )
    return (
        f"Ranking: {cleared} of {total} rows kept at least {min_correctness:.0%} of the control's "
        f"acc1 and are ranked first, cheapest {basis} first; the rest follow below the line."
    )


def _render(
    verdict: JointVerdict | None,
    cells: Sequence[CellStats],
    excluded: set[str],
    control: str = "none",
    *,
    show_answers: bool,
    min_correctness: float = DEFAULT_MIN_CORRECTNESS,
) -> str:
    """Render cost and correctness side by side, then the recommendation.

    Everything about the run itself -- the model, the rates, the sizing, how many seeds were
    asked for -- is read off the cells rather than passed in beside them. A caller cannot then
    label a table with a model or a price the numbers were not produced under, which is the
    one way a rebuilt table could have lied while every column in it was correct.

    Args:
        verdict: The recommendation, or None when the records hold no admissible control and
            there is therefore nothing to recommend against.
        cells: The rows, in any order. Ordering them is this function's own job, so that the
            live table and one rebuilt from a file cannot be ordered by two different rules.
        excluded: Strategies that are out of the ranking.
        control: Name of the uncompacted baseline.

    Keyword Args:
        show_answers: Print each cell's first answer in full.
        min_correctness: Share of the control's correctness a row must retain to rank above
            the split line. The bar the verdict applied, so the two agree.

    Returns:
        The rendered table.
    """
    baseline = next((cell for cell in cells if cell.strategy == control), None)
    message_gap = _control_message_gap(cells, control)
    # A control over the limit is out of the ranking like any disqualified row, but it is not
    # printed among the ranked rows: at the top of them it read as the row to beat. It is set
    # apart as a reference instead -- what the conversation cost and kept with nothing removed
    # -- while its acc1 still sets the bar. One that also diverged stays where it was, because
    # the divergence already withdraws everything it anchors.
    reference = (
        baseline
        if baseline is not None and baseline.disqualified > 0 and message_gap is None
        else None
    )
    ranked_rows, cleared = _table_order(
        [cell for cell in cells if cell is not reference], baseline, min_correctness
    )
    # Every block under the table still reads the control, at the end where it is printed.
    ordered = [*ranked_rows, *([reference] if reference is not None else [])]
    split = _split_measured(cells)
    cell_params = cells[0].records[0].cell
    pricing = cell_params.pricing
    header = (
        f"{'strategy':<28}{'msgs':>9}{'tok left/peak':>16}{'snap%':>7}{'calls':>7}{'in':>12}"
        f"{'seed hit%':>11}{'probe hit%':>12}{'run hit%':>10}"
        f"{'out':>10}{'seed in$':>10}{'seed$':>9}{'probe$':>9}{'run$':>9}{'seed$+-':>8}"
        f"{'summ$':>8}{'vs none$':>10}"
        f"{'facts':>9}{'lost':>6}{'nofetch':>8}{'ignored':>8}{'acc1':>9}{'seed+-':>8}{'rep+-':>7}"
        f"{'acc2':>6}{'rep2+-':>8}{'vs none':>9}{'dq':>5}{'flags':>10}"
    )
    lines = [
        "",
        (
            f"Model: {cell_params.model}   agent: {cell_params.agent_kind}   "
            f"probe repeats: {cell_params.probe_repeats} (acc1), "
            f"{cell_params.combined_repeats} (acc2)"
        ),
        f"Pricing: {pricing.describe()}",
        _ranking_note(
            cleared,
            len(ranked_rows),
            baseline,
            min_correctness,
            split=split,
            overflowed_limit=None if reference is None else cell_params.context_window,
        ),
        "",
        header,
        "-" * len(header),
    ]
    for index, cell in enumerate(ranked_rows):
        if index == cleared:
            lines.append(
                f" below {min_correctness:.0%} of the control's acc1 ".center(len(header), "-")
            )
        lines.append(
            _row(
                cell,
                baseline,
                cell.strategy in excluded,
                cell_params.context_window,
                message_gap=message_gap,
            )
        )
    if reference is not None:
        rule = (
            f" reference, not ranked: the control, over the {cell_params.context_window:,}-token "
            "limit "
        )
        lines += [
            rule.center(len(header), "="),
            _row(reference, baseline, True, cell_params.context_window, message_gap=message_gap),
        ]
    lines += ["", *_LEGEND, "", "per-sample acc1, one group per seed:"]
    for cell in ordered:
        lines.append(f"  {cell.strategy:<28}{_sample_groups(cell.samples)}")
    # Its own block rather than a second figure inside the acc1 groups: the two have different
    # numbers of readings per seed, so a reader pairing them position by position would be
    # pairing a repeat with an attempt that is not the same probe.
    lines += ["", "per-sample acc2, one group per seed:"]
    for cell in ordered:
        lines.append(f"  {cell.strategy:<28}{_sample_groups(cell.combined_samples)}")
    # The facts column is a mean too, and where the seeds disagree the mean is a value no
    # seed took: the record row's retention is a draw between every fact and a whole group
    # fewer, so 50/53 there is four seeds at 53 and one at 37. Only the rows whose seeds
    # disagree are printed -- a row at one value on every seed says so in the column already,
    # and a block of those would bury the row this exists to expose.
    divided = [cell for cell in ordered if len({record.facts_left for record in cell.records}) > 1]
    if divided:
        lines += ["", "per-seed facts, one reading per seed, rows whose seeds disagree only:"]
        for cell in divided:
            lines.append(f"  {cell.strategy:<28}{_facts_groups(cell.records, cell.facts_total)}")
    # The probe half is a draw between two values, so its column is a mean over a mixture and
    # the per-seed pairs are what say which seeds drew which -- and that the seeding half
    # beside them did not move with the draw.
    lines += ["", "per-seed cache hit, seeding/probe, one pair per seed:"]
    for cell in ordered:
        lines.append(f"  {cell.strategy:<28}{_hit_pairs(cell.records)}")
    # And under those, which probes the cached tokens landed on, where the record can say. A
    # cell written before the per-probe counts existed says so in one line rather than
    # printing twenty rows of "not recorded".
    if any(record.probe_hit_samples is not None for cell in ordered for record in cell.records):
        lines += ["", "per-probe cache hit, one group per seed, probes in the order asked:"]
        for cell in ordered:
            lines.append(f"  {cell.strategy:<28}{_probe_groups(cell.records)}")
    else:
        lines += [
            "",
            (
                "per-probe cache hit: not on these records. They were written before schema 12 "
                "and carry"
            ),
            (
                "  the probe phase's totals only, so which probes hit is arithmetic here, not "
                "observation."
            ),
        ]
    lines += _fill_note({cell.strategy: cell for cell in ordered}, cell_params.plan, control)
    lines += _divergence_note(message_gap, control)
    lines += _split_note(ordered, split)
    lines += _throttle_note(ordered)
    lines += _reconnect_note(ordered)
    if verdict is None:
        reason = (
            f"the {control!r} row ran a different conversation from the strategies"
            if message_gap is not None
            else f"these records hold no admissible {control!r} row"
        )
        lines += [
            "",
            (
                f"NO VERDICT: {reason}, and every ranking here is relative to one. The columns "
                "above still describe what was measured."
            ),
        ]
    else:
        lines += [
            "",
            (
                "VERDICT: "
                f"{'no row qualifies' if verdict.recommended is None else verdict.recommended}"
            ),
            verdict.rationale,
            *_stability_note(
                verdict,
                # The spread of the money the verdict was taken on, so the margin and the noise
                # it is judged against are two readings of one column.
                {
                    cell.strategy: (
                        cell.cost_spread
                        if cell.seeding_cost_spread is None
                        else cell.seeding_cost_spread
                    )
                    for cell in ordered
                },
                # Seeds actually present, not the --repeats that was asked for. One cell is
                # often several single-seed invocations merged, and reading the request would
                # have this announce "single seed" over five of them.
                min((len(cell.records) for cell in ordered), default=0),
            ),
            *_accuracy_note(
                {cell.strategy: cell.seed_spread for cell in ordered}, control, cell_params.repeats
            ),
        ]
    failed = [
        cell.strategy
        for cell in ordered
        if any(record.summarizer_failures for record in cell.records)
    ]
    if failed:
        lines += [
            "",
            (
                f"WARNING: the summarizer failed for {', '.join(failed)}. SummarizationStrategy "
                f"swallows"
            ),
            "those errors and skips compaction, so those rows describe a run that barely compacted",
            "and their high correctness is not evidence that summarization preserves information.",
        ]
    if show_answers:
        for cell in ordered:
            lines += [
                "",
                (
                    f"--- {cell.strategy}: every probe answer of its first seed, acc1 and acc2 "
                    f"together ---"
                ),
                cell.records[0].answer or "(no answer)",
            ]
    return "\n".join(lines)


def _resolve_tool_share(requested: float | None, *, fill_fraction: float) -> float:
    """Return the share of the fill target the tool payload is sized to reach.

    The flag parses to None when it was not given, rather than to :data:`DEFAULT_TOOL_SHARE`,
    for one interaction: manual sizing. ``--fill 0`` sets no target, so there is nothing for a
    share to be a share of, and a default of 0.6 applied blindly would refuse every ``--fill 0``
    invocation -- a mode that predates the parameter and has nothing to do with it. Defaulting
    late separates "asked for a share with no target", which is a contradiction and is refused
    by the caller, from "did not ask", which falls back to the fixed path exactly as before.

    Args:
        requested: What ``--tool-share`` parsed to, or None when it was not given.

    Keyword Args:
        fill_fraction: What ``--fill`` parsed to. 0 means the sizing is manual.

    Returns:
        The share to size the payload to, 0 to size it from ``--tool-result-tokens`` instead.
    """
    if requested is not None:
        return requested
    return DEFAULT_TOOL_SHARE if fill_fraction > 0 else 0.0


def _plan_or_exit(
    args: argparse.Namespace, tokenizer: Any, workload: WorkloadSettings
) -> FillPlan | None:
    """Solve the fill sizing, or exit explaining why this cell cannot be built.

    Args:
        args: Parsed command line arguments.
        tokenizer: The run's token counter.
        workload: The resolved workload flags, two of which change how large the conversation
            is: the retrieval clause lengthens the instructions and a sweeping close replaces
            several question turns with one.

    Returns:
        The plan, or None when --fill 0 asked for manual sizing.

    Raises:
        SystemExit: If the payload does not fit inside the target, or if the tool share was
            asked for without a fill target to be a share of.
    """
    tool_share = _resolve_tool_share(args.tool_share, fill_fraction=args.fill)
    if args.fill <= 0:
        if tool_share > 0:
            raise SystemExit(
                "--tool-share is a share of the fill target, and --fill 0 sets no target. Either "
                "give --fill a fraction, or state the payload directly with --tool-result-tokens."
            )
        return None
    try:
        return plan_fill(
            tokenizer=tokenizer,
            context_limit=args.context_window,
            fill_fraction=args.fill,
            tool_turns=args.tool_turns,
            filler_tool_turns=args.filler_tool_turns,
            markers_per_tool=args.markers_per_tool,
            tool_result_tokens=args.tool_result_tokens,
            tool_share=tool_share,
            narration=args.narration,
            fact_placement=args.fact_placement,
            reply_tokens=args.assumed_reply_tokens,
            retrieval_guidance=workload.retrieval_guidance,
            subset_questions=workload.subset_questions,
            filler_turn_tokens=args.filler_tokens,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


def _strategy_options(
    args: argparse.Namespace, tokenizer: Any, summarizer: Any = None
) -> StrategyOptions:
    """Return the parameters every strategy in this run is built from.

    One function rather than a literal at each call site, because there are three of them --
    the pre-flight below, the dry run's plan, and the per-seed build -- and a flag threaded
    into two of the three produces a run that validates one configuration, describes a second
    and measures a third. The dry run did exactly that: it built every strategy from bare
    defaults and printed "every strategy builds cleanly" about a configuration the run was not
    going to use.

    Args:
        args: Parsed command line arguments.
        tokenizer: The run's token counter.
        summarizer: Metered client for the strategies that need one, per seed.

    Returns:
        The options.
    """
    return StrategyOptions(
        tokenizer=tokenizer,
        max_context_window_tokens=args.context_window,
        max_output_tokens=args.max_output_tokens,
        keep_last_groups=args.keep_last_groups,
        keep_last_tool_call_groups=args.keep_last_tool_groups,
        keep_head_groups=args.keep_head_groups,
        keep_tail_groups=args.keep_tail_groups,
        # 0 is the absence of a bound, the same convention --record-max-tokens and --fill use:
        # no fixed retention, so the anchored family derives one from --band-share instead.
        keep_tokens=args.keep_tokens or None,
        band_share=args.band_share,
        min_gain_fraction=args.min_gain_fraction,
        trigger_fraction=args.trigger_fraction,
        fallback_fraction=args.fallback_fraction,
        coverage_share=args.coverage_share,
        keep_head_user_turns=args.keep_head_user_turns,
        keep_tail_user_turns=args.keep_tail_user_turns,
        user_trigger_fraction=args.user_trigger_fraction,
        user_min_band_share=args.user_min_band_share,
        user_summary_mode=args.user_summary_mode,
        record_harder_attempts=args.record_harder_attempts,
        chain_gain_fraction=args.chain_gain_fraction,
        token_budget_fraction=args.budget_fraction,
        summarizer=summarizer,
    )


def _strategy_settings(
    args: argparse.Namespace, options: StrategyOptions, *, summarizer: str | None
) -> StrategySettings:
    """Return the settings block the cell records, read off what the run will actually build.

    Taken from the built :class:`StrategyOptions` rather than from ``args``, for the reason
    :func:`_strategy_options` exists at all: the flag and the value disagree wherever the CLI
    resolves one into the other, and a block copied from the flags would describe a
    configuration next to a table produced by another. ``--keep-tokens 0`` is the plain case --
    zero is the absence of a fixed retention, and reading the flag would record a cell as
    keeping no tokens when it derived its retention from the band.

    The four record settings have no home on ``StrategyOptions``: they are handed to
    :func:`run_live` rather than to a strategy constructor. They are resolved here, once, and
    the seed loop passes *these* values on -- so the run cannot be configured with one number
    and record another.

    Args:
        args: Parsed command line arguments.
        options: The options every strategy of this run is built from.

    Keyword Args:
        summarizer: ``provider:model`` of the summarizer client, resolved, or None when the run
            built none.

    Returns:
        The settings.
    """
    return StrategySettings(
        keep_last_groups=options.keep_last_groups,
        keep_last_tool_call_groups=options.keep_last_tool_call_groups,
        keep_head_groups=options.keep_head_groups,
        keep_tail_groups=options.keep_tail_groups,
        keep_tokens=options.keep_tokens,
        band_share=options.band_share,
        min_gain_fraction=options.min_gain_fraction,
        trigger_fraction=options.trigger_fraction,
        fallback_fraction=options.fallback_fraction,
        coverage_share=options.coverage_share,
        keep_head_user_turns=options.keep_head_user_turns,
        keep_tail_user_turns=options.keep_tail_user_turns,
        user_trigger_fraction=options.user_trigger_fraction,
        user_min_band_share=options.user_min_band_share,
        user_summary_mode=options.user_summary_mode,
        record_harder_attempts=options.record_harder_attempts,
        chain_gain_fraction=options.chain_gain_fraction,
        token_budget_fraction=options.token_budget_fraction,
        max_output_tokens=options.max_output_tokens,
        answer_max_tokens=args.answer_max_tokens,
        tokenizer=args.tokenizer,
        summarizer=summarizer,
        # 0 is the absence of a bound on all three, the convention --fill and --keep-tokens
        # share. Resolved here so that the value recorded and the value passed to run_live are
        # one expression rather than two that have to agree.
        record_max_tokens=args.record_max_tokens or None,
        record_target_tokens=args.record_target_tokens or None,
        max_groups_before_record=args.max_groups_before_record or None,
        repeat_records=args.record_repeats,
    )


def _workload_settings(args: argparse.Namespace) -> WorkloadSettings:
    """Return the workload block the cell records, resolved once for the whole run.

    The five flags that change the conversation rather than the strategies, and the last part of
    the command line that was on no key at all: two runs differing in any of them keyed as one
    cell and were meaned into one row. Resolved here rather than at each use for the reason
    :func:`_strategy_settings` is -- ``not args.sweeping_question`` appears at three call sites
    and ``not args.no_retrieval_guidance`` at three more, and a run that inverts one of them in
    one place and not another measures a conversation no record describes.

    Built before the provider, because everything in it is decided by the command line alone and
    the dry run needs the same values the real run will use.

    Args:
        args: Parsed command line arguments.

    Returns:
        The flags.
    """
    return WorkloadSettings(
        force_tool_calls=not args.no_force_tool_calls,
        retrieval_guidance=not args.no_retrieval_guidance,
        subset_questions=not args.sweeping_question,
        # The value rather than the flag: --no-temperature names an omission, and what is sent
        # is either 0.0 or no field at all. Recording the boolean would leave a reader to know
        # which number the other branch meant.
        temperature=None if args.no_temperature else 0.0,
        server_history=args.server_history,
    )


class _PreflightSummarizer:
    """The client a summarizer-needing strategy is built against when the run configured none.

    It exists so :func:`_build_or_exit` can run a strategy's constructor, and with it every
    parameter check that constructor makes, before a summarizer has been chosen. Constructors
    store a client and do not call it, so this is never asked anything; if it ever is, that is
    a pre-flight doing work it must not do, and it says so.
    """

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: nothing built for a pre-flight is ever run."""
        raise RuntimeError(
            "the pre-flight summarizer stand-in was called; a pre-flight must not run a strategy"
        )


def _build_or_exit(strategies: Sequence[str], options: StrategyOptions) -> None:
    """Build every selected strategy once, before the run spends anything.

    This is where the numeric flags are range-checked, and it is deliberately not a second
    copy of the checks. Each strategy validates its own bounds in its own constructor --
    ``compaction/`` ships without this package, so the constraint has to live there -- and a
    duplicate here would give a sweep two places to disagree about what is legal. What this
    adds is *when*: without it a bad ``--band-share`` surfaced on the first seed, after the
    provider was built, the pricing fetched and the first call paid for, and a bad one under
    ``--dry-run`` surfaced not at all, because the dry run built from defaults rather than
    from the flags it was printing a plan for.

    A strategy needing a summarizer is built against a stand-in client when the run configured
    none, rather than skipped. Skipping it left its parameters unchecked in exactly the case a
    dry run is most often used -- planning a cell before choosing a provider -- and it hid more
    than ranges: on ``tool_and_user_summary_anchored`` a ``--trigger-fraction`` at or above the
    record half's ``--fallback-fraction`` is refused by the constructor, and a dry run without
    ``--summarizer-provider`` printed a clean plan for it. The stand-in is never called; the
    missing client is still reported separately, where the run needs it, and says what to do.

    Args:
        strategies: The selected strategy names.
        options: What they will be built from.

    Raises:
        SystemExit: If any strategy rejects the configuration.
    """
    for name in strategies:
        built_from = options
        if name in STRATEGIES_NEEDING_SUMMARIZER and options.summarizer is None:
            built_from = replace(
                options, summarizer=cast("SupportsChatGetResponse[Any]", _PreflightSummarizer())
            )
        try:
            build_strategy(name, built_from)
        except ValueError as error:
            raise SystemExit(
                f"{name} rejects this configuration: {error} Each parameter named there is the "
                "flag of the same name, with underscores written as dashes."
            ) from error


def _progress(record: SeedRecord) -> str:
    """Return the line printed the moment a seed lands.

    A cell prints its table only at the end and takes hours to get there, so without this the
    only difference between a run that is working and one whose rows have collapsed is elapsed
    time. Cost, facts and the two accuracies are what move first: a strategy that has stopped
    preserving anything shows it here, hours before the table would. Both accuracies, because
    they disagree in the direction that matters -- one seed of ``anchored`` read 91% on acc1
    and 21% on acc2, and a watcher told only the first would think it was fine.

    Args:
        record: The seed that just finished.

    Returns:
        One line, already indented to sit under the strategy heading.
    """
    parts = [
        f"   {record.strategy} seed {record.seed}/{record.cell.repeats}",
        f"${record.cost:.4f}",
        f"facts {record.facts_left}/{record.facts_total}",
        f"acc1 {record.correctness:.0%}",
        f"acc2 {record.combined:.0%}",
    ]
    if record.disqualified:
        parts.append("DQ")
    if record.context_drift:
        parts.append(f"DRIFT:{record.context_drift}")
    if record.rate_limit_retries:
        parts.append(f"THROTTLED:{record.rate_limit_retries} ({record.throttled_seconds:,.0f}s)")
    if record.connection_retries:
        parts.append(f"RECONNECTED:{record.connection_retries} ({record.connection_seconds:,.0f}s)")
    if record.error:
        parts.append(record.error)
    return "  ".join(parts)


def _exclusion_notes(
    incomplete: set[str], oversized: set[str], diverged: set[str], limit: int
) -> list[str]:
    """Return the lines naming what was dropped from the ranking, and why.

    Args:
        incomplete: Strategies that did not finish their turns.
        oversized: Strategies that overran the tried limit.
        diverged: The control, when it did not run the strategies' conversation.
        limit: The context limit the cell stands in for.

    Returns:
        Zero or more lines.
    """
    lines: list[str] = []
    if incomplete:
        lines += [
            "",
            f"Excluded from the verdict ({len(incomplete)} failed or incomplete): "
            + ", ".join(sorted(incomplete)),
        ]
    if oversized:
        lines += [
            "",
            (
                f"Excluded from the verdict ({len(oversized)} exceeded the {limit:,}-token "
                "limit this run stands in for): "
            )
            + ", ".join(sorted(oversized)),
        ]
    if diverged:
        lines += [
            "",
            (
                "Excluded from the verdict (the control ran a different conversation from the "
                "rows it is the baseline for): "
            )
            + ", ".join(sorted(diverged)),
        ]
    return lines


def _coverage(cell: CellParams, records: Sequence[SeedRecord]) -> list[str]:
    """Return what a cell read back from file actually holds, and whether that is all of it.

    A file is written seed by seed precisely so that an interrupted cell keeps what it had, so
    an incomplete cell is the normal case here rather than the exception. Every mean in the
    table below is over whatever is present, and the difference between a mean over fifteen
    strategy-seeds and one over four is invisible in the table itself -- so it is stated here,
    against what the run said it was going to take.

    Args:
        cell: The cell's parameters.
        records: Its records.

    Returns:
        The heading, what is present, and a PARTIAL line when something is missing.
    """
    seeds: dict[str, list[int]] = {}
    for record in records:
        seeds.setdefault(record.strategy, []).append(record.seed)
    # Intent is unioned over the records rather than read off the first, because a cell
    # abandoned partway and resumed for the rest is written by two runs that each asked for
    # part of it. Taking the first record's list would report the resumed half as unwanted.
    intended = sorted({name for record in records for name in record.cell.strategies} | set(seeds))
    wanted = max(record.cell.repeats for record in records)
    present = ", ".join(f"{name} {len(seeds.get(name, ()))}/{wanted}" for name in intended)
    lines = ["", f"Cell: {cell.label}", f"  seeds present: {present}"]
    missing = [name for name in intended if name not in seeds]
    short = [name for name, found in seeds.items() if len(found) < wanted]
    if not missing and not short:
        return lines
    detail: list[str] = []
    if missing:
        detail.append(
            f"{len(missing)} of {len(intended)} strategies never recorded a seed "
            f"({', '.join(missing)})"
        )
    if short:
        detail.append(
            f"{', '.join(sorted(short))} recorded fewer than the {wanted} seeds asked for"
        )
    lines.append(f"  PARTIAL: {'; '.join(detail)}. Every mean below is over what is present.")
    return lines


def _cells_from_records(records: Sequence[SeedRecord]) -> list[CellStats]:
    """Aggregate one cell's records into one row per strategy.

    Unordered on purpose: the table orders its own rows, and a second ordering here is a
    second rule for the rebuilt table to disagree with the live one about.

    Args:
        records: Every record of one cell.

    Returns:
        One row per strategy present.
    """
    by_strategy: dict[str, list[SeedRecord]] = {}
    for record in records:
        by_strategy.setdefault(record.strategy, []).append(record)
    return [_aggregate(strategy, seeds) for strategy, seeds in by_strategy.items()]


def _setting_text(value: Any) -> str:
    """Return one setting's value as the comparison column shows it.

    Args:
        value: The recorded value.

    Returns:
        The rendered value. Booleans read as on/off, because ``repeat_records=False`` beside
        ``repeat_records=True`` is two words a reader has to diff character by character.
        ``None`` reads as ``none``, which is what every field allowing it means by it: no
        bound, no fixed retention, no client of its own.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _differing_settings(cells: Sequence[CellParams]) -> tuple[str, ...]:
    """Return the settings that are not the same across every cell that recorded any.

    The comparison column shows these and nothing else. Twenty identical fields beside two that
    differ is a column nobody reads, and the one question it exists to answer -- what is
    different between these rows -- is the one the identical fields bury.

    Cells with no settings at all take no part in deciding this. They cannot agree or disagree
    with anything, so folding them in would mark every field as differing and print the whole
    block against rows that are not comparable on it anyway; they are labelled unrecorded
    instead.

    Args:
        cells: The cells being compared.

    Returns:
        The field names, in the order the settings block declares them.
    """
    recorded = [cell.settings.to_dict() for cell in cells if cell.settings is not None]
    if len(recorded) < 2:
        return ()
    return tuple(
        name
        for name, value in recorded[0].items()
        if any(other[name] != value for other in recorded[1:])
    )


def _settings_label(cell: CellParams, names: Sequence[str], *, mixed: bool) -> str:
    """Return how one cell's settings read in a comparison against others.

    Args:
        cell: The cell.
        names: The settings that differ across the comparison, from :func:`_differing_settings`.

    Keyword Args:
        mixed: Whether any cell in the comparison recorded no settings at all. It changes what
            silence means for the cells that did: among recorded cells alone, nothing differing
            means they agree, and beside an unrecorded one it means only that this cell said
            what it ran.

    Returns:
        The differing settings and their values; ``not recorded`` for a cell written before the
        settings reached the file, which is a statement about the record rather than about the
        run; and ``same settings`` when every cell recorded them and none differs.
    """
    if cell.settings is None:
        return "not recorded"
    if names:
        values = cell.settings.to_dict()
        return " ".join(f"{name}={_setting_text(values[name])}" for name in names)
    return "recorded" if mixed else "same settings"


@dataclass(frozen=True, slots=True)
class _Combination:
    """One ``(strategy, settings)`` pair, as one cell measured it.

    The unit the cross-cell section ranks. A strategy name is not enough on its own: the point
    of the section is that one name measured under two configurations is two findings, and the
    per-cell tables cannot show that, because those two rows are now in different tables.
    """

    cell: CellParams
    stats: CellStats
    bar: float
    """The accuracy threshold this row's own cell was read under, applied to this row."""
    relative: float
    """This row's ``acc1`` as a share of its own cell's control."""

    @property
    def cost(self) -> float:
        """Return the workload cost this row is ranked on: ``seed$``, never the invoice."""
        return self.stats.seeding_cost if self.stats.seeding_cost is not None else 0.0

    @property
    def spread(self) -> float:
        """Return the gap between this row's cheapest and dearest seed, on the ranked cost."""
        return self.stats.seeding_cost_spread or 0.0

    @property
    def eligible(self) -> bool:
        """Return whether this row cleared the same accuracy bar its own cell's verdict used."""
        return self.relative >= self.bar


def _combinations(
    cells: Sequence[CellStats], params: CellParams, excluded: set[str], bar: float
) -> tuple[list[_Combination], list[str]]:
    """Reduce one cell's rows to the combinations the cross-cell section can rank.

    Args:
        cells: The cell's rows.
        params: The cell's parameters.
        excluded: Rows this cell already dropped from its own verdict.
        bar: The accuracy threshold this cell was read under.

    Returns:
        The rankable combinations, and one line per row set aside saying why it was. Set aside
        rather than ranked with a caveat: a row excluded from its own cell's verdict is
        excluded from this one on the same grounds, and a row that cannot say what its probing
        cost would otherwise be ranked on its invoice against rows ranked on their workload.
    """
    split = _split_measured(cells)
    control = next((cell for cell in cells if cell.strategy == "none"), None)
    base = None if control is None else _to_joint(control, split=split)
    combinations: list[_Combination] = []
    aside: list[str] = []
    for stats in cells:
        if stats.strategy in excluded:
            aside.append(f"{stats.strategy} was already out of its own cell's verdict")
        elif stats.seeding_cost is None:
            aside.append(
                f"{stats.strategy} never counted its probing apart, so it has no seed$ to be "
                f"ranked on"
            )
        elif base is None:
            aside.append(
                f"{stats.strategy} sits in a cell holding no control, so nothing judges its "
                f"accuracy"
            )
        else:
            combinations.append(
                _Combination(
                    cell=params,
                    stats=stats,
                    bar=bar,
                    relative=relative_correctness(_to_joint(stats, split=split), base),
                )
            )
    return combinations, aside


def _combination_row(combination: _Combination, names: Sequence[str], *, mixed: bool) -> str:
    """Render one combination's line of the cross-cell table.

    Args:
        combination: The row.
        names: The settings that differ across the comparison.

    Keyword Args:
        mixed: Whether any cell in the comparison recorded no settings.

    Returns:
        One line.
    """
    stats = combination.stats
    control = "*" if stats.strategy == "none" else " "
    # Both halves, on the rule the per-cell table follows: a row compared on cache at all is
    # shown its seeding half, and the probe half beside it so that nobody reaches for run hit%.
    return (
        f"    {stats.strategy:<28}{_money(stats.seeding_cost):>9}{combination.spread:>8.0%} "
        f"{_rate(stats.seeding_cached_tokens, stats.seeding_input_tokens):>10}"
        f"{_rate(stats.probe_cached_tokens, stats.probe_input_tokens):>12} "
        f"{stats.correctness:>6.0%}{control}{combination.relative:>8.0%}{len(stats.records):>7}  "
        f"{_settings_label(combination.cell, names, mixed=mixed)}"
    )


def _workload_finding(eligible: Sequence[_Combination]) -> list[str]:
    """Return what may be said about the cheapest combination, or why nothing may be.

    The spread guard is :func:`_stability_note`'s, applied to the two cheapest rows rather than
    to a recommendation and its control. A ranking is worth reporting only when the gap between
    the options is wider than the gap between repeats of one option, and these gaps are
    routinely narrower than that: two arms of one strategy can differ by 7% on ``seed$`` while
    the seeds inside one of them differ by 35%.

    Args:
        eligible: The rows that cleared their own cells' bars, cheapest first.

    Returns:
        The lines.
    """
    if not eligible:
        return [
            "",
            "    Nothing here cleared the accuracy bar its own cell applied, so nothing is ranked.",
        ]
    best = eligible[0]
    if len(eligible) < 2:
        return [
            "",
            (
                f"    Only {best.stats.strategy} cleared the bar, so there is nothing to rank "
                "it against."
            ),
        ]
    second = eligible[1]
    if min(len(best.stats.records), len(second.stats.records)) < 2:
        return [
            "",
            "    NOT SUPPORTED: one of the two cheapest rows rests on a single seed and measures",
            (
                "    no spread at all, so the gap between them cannot be told from noise. Nothing "
                "is named best."
            ),
        ]
    margin = 0.0 if second.cost <= 0 else (second.cost - best.cost) / second.cost
    worst = max(best.spread, second.spread)
    if margin <= 0:
        return [
            "",
            "    NOT SUPPORTED: the two cheapest rows that clear the bar cost the same, so the",
            "    order between them is the tie-break and not a finding. Nothing is named best.",
        ]
    if worst > margin:
        return [
            "",
            (
                f"    NOT SUPPORTED: seeds of one of these varied by {worst:.0%}, wider than the "
                f"{margin:.0%} gap"
            ),
            "    between the two cheapest rows that clear the bar. Nothing is named best.",
        ]
    names = _differing_settings([best.cell, second.cell])
    mixed = best.cell.settings is None or second.cell.settings is None
    # Named only when they differ. Two rows of one configuration are separated by the strategy
    # and nothing else, and printing "at same settings" twice would suggest otherwise.
    best_at = f" at {_settings_label(best.cell, names, mixed=mixed)}" if names else ""
    second_at = f" at {_settings_label(second.cell, names, mixed=mixed)}" if names else ""
    lines = [
        "",
        f"    BEST: {best.stats.strategy}{best_at}, {margin:.0%} cheaper on seed$ than",
        f"    {second.stats.strategy}{second_at}, and wider than the {worst:.0%} either varied by.",
    ]
    if mixed:
        lines.append(
            "    One of the two did not record its settings, so this is a gap between two rows and "
            "not between two configurations."
        )
    return lines


def _setting_effects(combinations: Sequence[_Combination]) -> list[str]:
    """Return what each strategy's own settings did to it, or why that cannot be said.

    The ranking above answers which row is cheapest; this answers the question the settings
    were varied to ask. They are not the same question, and the ranking alone reads badly when
    a setting no strategy in the cell consults has split its rows anyway -- two ``none`` rows
    ten percent apart under ``repeat_records`` are the noise floor, not an effect, and the only
    thing that says so is the guard applied to that pair.

    So the guard is applied per strategy as well as to the two cheapest overall: same rule,
    same numbers, asked of one strategy's arms. Only arms that cleared their own cell's bar are
    compared, because a cheaper arm that stopped answering is not a cheaper arm.

    Args:
        combinations: Every rankable row of one model at one workload.

    Returns:
        The lines, or none at all when no strategy here was measured under two settings.
    """
    arms: dict[str, list[_Combination]] = {}
    for combination in combinations:
        if combination.eligible:
            arms.setdefault(combination.stats.strategy, []).append(combination)
    compared = {
        name: sorted(rows, key=lambda row: row.cost) for name, rows in arms.items() if len(rows) > 1
    }
    if not compared:
        return []
    lines = ["", "    Per strategy, what its own settings did, under the same guard:"]
    for name, rows in sorted(compared.items()):
        best, second = rows[0], rows[1]
        names = _differing_settings([best.cell, second.cell])
        mixed = best.cell.settings is None or second.cell.settings is None
        cheaper = _settings_label(best.cell, names, mixed=mixed)
        dearer = _settings_label(second.cell, names, mixed=mixed)
        margin = 0.0 if second.cost <= 0 else (second.cost - best.cost) / second.cost
        worst = max(best.spread, second.spread)
        verdict = "resolved" if margin > 0 and worst <= margin else "NOT RESOLVED"
        lines.append(
            f"      {name:<28}[{cheaper}] {margin:.0%} cheaper than [{dearer}], seeds varied "
            f"by {worst:.0%}: {verdict}"
        )
    return lines


def _workload_ranking(combinations: Sequence[_Combination]) -> list[str]:
    """Return the ranked table and the finding for one model at one workload.

    Ranked on ``seed$`` behind each row's own accuracy bar, which is the per-cell verdict's own
    rule applied across cells rather than a second one: a row its own table put below the line
    is below the line here too, so the two cannot disagree about which rows are usable.

    Args:
        combinations: Every rankable row of one model at one workload.

    Returns:
        The lines.
    """
    cells = [combination.cell for combination in combinations]
    names = _differing_settings(cells)
    mixed = any(cell.settings is None for cell in cells)
    ordered = sorted(
        combinations, key=lambda combination: (not combination.eligible, combination.cost)
    )
    eligible = [combination for combination in ordered if combination.eligible]
    header = (
        f"    {'strategy':<28}{'seed$':>9}{'seed$+-':>9}{'seed hit%':>11}{'probe hit%':>12}"
        f"{'acc1':>7}{'vs none':>9}{'seeds':>7}  settings"
    )
    lines = [header, "    " + "-" * (len(header) - 4)]
    for index, combination in enumerate(ordered):
        if index == len(eligible):
            lines.append(
                "    " + " below the bar their own cells applied ".center(len(header) - 4, "-")
            )
        lines.append(_combination_row(combination, names, mixed=mixed))
    unrecorded = sum(1 for combination in combinations if combination.cell.settings is None)
    if unrecorded:
        lines += [
            "",
            (
                f"    {unrecorded} of these rows come from cells written before the settings "
                "reached the file."
            ),
            (
                "What those runs were configured with is unknown, so their costs are comparable "
                "and no"
            ),
            "    difference between them and any other row here can be attributed to a setting.",
        ]
    return lines + _setting_effects(combinations) + _workload_finding(eligible)


#: What the cross-cell section is and, more to the point, what it refuses to be.
_ACROSS_PREAMBLE: Final[tuple[str, ...]] = (
    "Across cells: the cheapest combination that still answers, per model and per workload.",
    "",
    "A combination is a strategy and the settings it ran under, which is what the per-cell",
    "tables above cannot show: two settings are two cells and so two tables, leaving the reader",
    "to diff them by eye. Ranked on seed$ behind the same accuracy bar each cell's own verdict",
    "applied, and refused whenever the gap between the two cheapest is inside the spread of the",
    "seeds it rests on.",
    "",
    "Never ranked across workloads. A different window, fill, payload, narration or workload",
    "flag is a different conversation, so a smaller number under one of them is a smaller job",
    "rather than a better strategy -- this project has already read one such comparison the",
    "wrong way. The flags are named on each workload heading, and read 'flags not recorded' for",
    "a cell written before they reached the file, which is a statement about the record.",
    "Models are kept apart for that reason and one more: they are priced differently, and every",
    "ranking here is on money.",
)

#: One cell as the cross-cell section receives it: parameters, rows, what its own verdict
#: excluded, and the bar it was read under. A tuple rather than a fourth dataclass because
#: :func:`_render_from_records` already holds all four and this is the handoff, not a new fact.
_CellGroup = tuple[CellParams, Sequence[CellStats], set[str], float]


def _across_cells(groups: Sequence[_CellGroup]) -> str:
    """Return the comparison that answers what is best for a model, over the cells in hand.

    Args:
        groups: One entry per cell.

    Returns:
        The rendered section.
    """
    lines = ["", "=" * 100, *_ACROSS_PREAMBLE]
    by_model: dict[tuple[Any, ...], list[_CellGroup]] = {}
    for group in groups:
        by_model.setdefault(group[0].model_key, []).append(group)
    for model_groups in by_model.values():
        first = model_groups[0][0]
        pricing = first.pricing
        lines += [
            "",
            (
                f"Model: {first.provider}:{first.model}  agent {first.agent_kind}  "
                f"at {pricing.describe()}"
            ),
        ]
        by_workload: dict[tuple[Any, ...], list[_CellGroup]] = {}
        for group in model_groups:
            by_workload.setdefault(group[0].workload_key, []).append(group)
        if len(by_workload) > 1:
            lines.append(
                f"  {len(by_workload)} workloads below, each ranked on its own. They are different "
                "conversations and are never ranked against each other."
            )
        for workload_groups in by_workload.values():
            lines += _workload_section(workload_groups)
    return "\n".join(lines)


def _workload_section(groups: Sequence[_CellGroup]) -> list[str]:
    """Return one model's one workload: its heading, what was set aside, and its ranking.

    Args:
        groups: The cells of one model at one workload.

    Returns:
        The lines.
    """
    combinations: list[_Combination] = []
    aside: list[str] = []
    bars: set[float] = set()
    for params, cells, excluded, bar in groups:
        found, skipped = _combinations(cells, params, excluded, bar)
        combinations += found
        aside += skipped
        bars.add(bar)
    bar_text = (
        f"{min(bars):.0%}" if len(bars) == 1 else ", ".join(f"{bar:.0%}" for bar in sorted(bars))
    )
    lines = [
        "",
        f"  Workload: {groups[0][0].workload_label}",
        (
            f"  {len(groups)} cell(s), {len(combinations)} rankable row(s), acc1 bar {bar_text} "
            "of each cell's control"
        ),
    ]
    lines += [f"    set aside: {reason}" for reason in aside]
    return lines + (_workload_ranking(combinations) if combinations else [])


def _results_paths(entries: Sequence[str]) -> tuple[Path, ...]:
    """Return the results files named by ``--from-jsonl``, expanding any directory among them.

    A sweep writes one file per cell, so the material for a comparison is routinely a directory
    or a shell glob rather than a single path. Concatenating them by hand works and is what was
    done -- and it is also how two arms of one experiment came to be merged into one table, so
    reading them here is the safer half of the same convenience: records group into cells by
    what they measured, and the file they arrived in decides nothing.

    Sorted within a directory, so a rebuild is reproducible; left in the order given otherwise,
    since the order named is the order meant.

    Args:
        entries: The paths as given.

    Returns:
        The files to read, in order and without repeats.

    Raises:
        SystemExit: If a path is neither a file nor a directory, or a directory holds no
            ``.jsonl`` files at all -- an empty comparison is a mistyped path far more often
            than it is a finding.
    """
    paths: list[Path] = []
    for entry in entries:
        path = Path(entry)
        if path.is_dir():
            found = sorted(path.glob("*.jsonl"))
            if not found:
                raise SystemExit(f"No .jsonl files in {path}.")
            paths += found
        elif path.is_file():
            paths.append(path)
        else:
            raise SystemExit(f"No results file at {path}.")
    return tuple(dict.fromkeys(paths))


def _sources(paths: Sequence[Path]) -> str:
    """Return how the header names where the records came from.

    One path reads as itself, which is what it did before several were allowed and is what the
    only line printed above a single cell's table should say.

    Args:
        paths: The files read.

    Returns:
        The description.
    """
    return str(paths[0]) if len(paths) == 1 else f"{len(paths)} files"


def _render_from_records(args: argparse.Namespace) -> int:
    """Rebuild the tables from results files, running nothing.

    One cell renders exactly as it did when there was only ever one file: the header names the
    path, the table is the table, and nothing about settings is printed, because a single cell
    is not a comparison and has nothing to be comparable with. More than one cell adds the
    cross-cell section, which is where a settings comparison is either made or refused.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.

    Raises:
        SystemExit: If the files cannot be read or hold no records.
    """
    paths = _results_paths(args.from_jsonl)
    records: list[SeedRecord] = []
    for path in paths:
        try:
            records += read_seed_records(path)
        except (OSError, ValueError) as error:
            raise SystemExit(str(error)) from error
    if not records:
        raise SystemExit(f"{_sources(paths)} holds no records.")

    groups = group_by_cell(records)
    print(f"{len(records)} seed records from {_sources(paths)}, in {len(groups)} cell(s).")
    rendered: list[_CellGroup] = []
    for cell_params, cell_records in groups:
        cells = _cells_from_records(cell_records)
        incomplete, oversized, diverged = _excluded_cells(cells)
        excluded = incomplete | oversized | diverged
        for line in _coverage(cell_params, cell_records):
            print(line)
        for line in _exclusion_notes(incomplete, oversized, diverged, cell_params.context_window):
            print(line)
        split = _split_measured(cells)
        ranked, admissible = _verdict_outcomes(cells, incomplete, oversized, diverged, split=split)
        # The bar the run set, unless this invocation names one: a rebuilt verdict that
        # silently applied a different threshold would rank rows the original never ranked,
        # while every column above it stayed identical.
        bar = cell_params.min_correctness if args.min_correctness is None else args.min_correctness
        verdict: JointVerdict | None = None
        if any(outcome.strategy == "none" for outcome in ranked):
            try:
                verdict = recommend(ranked, min_correctness=bar, baseline_admissible=admissible)
            except ValueError as error:
                print(f"Cannot summarize: {error}")
        print(
            _render(verdict, cells, excluded, show_answers=args.show_answers, min_correctness=bar)
        )
        rendered.append((cell_params, cells, excluded, bar))
    if len(rendered) > 1:
        print(_across_cells(rendered))
    return 0


#: Opening of the framework's warning for a compaction summary it will not merge into the
#: caller's message list. Matched on the format string, which is what a logging filter sees.
_UNROOTED_SUMMARY_WARNING: Final = "Rejected %d compaction summary message(s)"


def _is_not_unrooted_summary_warning(record: logging.LogRecord) -> bool:
    return not str(record.msg).startswith(_UNROOTED_SUMMARY_WARNING)


def mute_unrooted_summary_warnings() -> None:
    """Keep the framework's per-call "Rejected ... not fully rooted" warning out of the run log.

    The framework reconciles compaction summaries at its chat-middleware boundary and warns
    about every summary whose sources are not among the messages the caller passed in. Under
    the harness that is every summary: the agent passes only the new turn, and the conversation
    is loaded further in, by the per-service-call history middleware, so no summary of it can be
    rooted there. The warning says nothing about the row -- the history provider persists the
    compacted conversation regardless -- and it fires once per call, which in a cell of this
    size puts tens of identical lines between the row lines that are the log. Installed on the
    framework's logger as a filter, so every other warning it raises still reaches the log.
    """
    logger = logging.getLogger("agent_framework")
    if _is_not_unrooted_summary_warning not in logger.filters:
        logger.addFilter(_is_not_unrooted_summary_warning)


async def run_live_comparison(args: argparse.Namespace) -> int:
    """Run every selected strategy against a live agent and print the comparison.

    Args:
        args: Parsed command line arguments.

    Returns:
        A process exit code.

    Raises:
        SystemExit: If the arguments do not describe a runnable cell.
    """
    if args.min_correctness is not None:
        try:
            validate_min_correctness(args.min_correctness)
        except ValueError as error:
            raise SystemExit(f"--min-correctness: {error}") from error
    if args.from_jsonl is not None:
        return _render_from_records(args)
    if args.provider is None:
        raise SystemExit(
            "A provider is required, unless --from-jsonl is rebuilding a table from a results file."
        )
    validate_recall_counts(args)
    validate_pricing_options(args)
    for name in ("repeats", "probe_repeats", "combined_repeats"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be greater than 0.")
    provider, model_override = parse_provider_selector(args.provider)
    if provider not in provider_names():
        raise SystemExit(f"Unknown provider {provider!r}. Available: {', '.join(provider_names())}")
    strategies = [entry.strip() for entry in args.strategies.split(",") if entry.strip()]
    validate_unique_selection("strategy", strategies)
    validate_summarizer_selector(args.summarizer_provider)
    unknown = set(strategies) - set(strategy_names())
    if unknown:
        raise SystemExit(f"Unknown strategies: {', '.join(sorted(unknown))}.")
    if "none" not in strategies:
        raise SystemExit("The 'none' control must be included; every comparison is relative to it.")
    mute_unrooted_summary_warnings()

    min_correctness = (
        DEFAULT_MIN_CORRECTNESS if args.min_correctness is None else args.min_correctness
    )
    tokenizer = build_tokenizer(args.tokenizer)
    retained = args.keep_last_tool_groups
    # Resolved once, above everything that reads it: the sizing, the dry run, the provider, each
    # seed's scenario and each seed's run all take their values from here, so the conversation
    # the record describes is the conversation that was had.
    workload = _workload_settings(args)
    plan = _plan_or_exit(args, tokenizer, workload)
    # The other half of "reserved and sent". --max-output-tokens is deducted from the window by
    # the strategies, so its reservation is structural; --answer-max-tokens is deducted from the
    # same window on the closing calls alone, and nothing compacts to it -- compacting the
    # snapshot on the way into a probe would move the material the answers are scored against,
    # which the drift counter exists to catch rather than to cause. So the reservation is
    # checked here instead, against the size the cell is being sized to reach, and it is a
    # warning rather than a refusal because every archived cell fails it and has to stay
    # reproducible: at 60,000 and 0.86 the seeded prompt aims at 51,600 tokens and a 12,000
    # answer does not fit beside it.
    answer_headroom = args.context_window - args.answer_max_tokens
    if plan is not None and plan.target_tokens > args.context_window:
        # Past the window on purpose, so "lower --fill" would undo the cell. The control cannot
        # answer from its snapshot in any model this size and is out of the verdict anyway; the
        # rows that are judged compact to their own ceiling, which is where the reservation bites.
        print(
            f"NOTE: this cell is sized to {plan.target_tokens:,} tokens, past the "
            f"{args.context_window:,}-token window, so the uncompacted control is expected to "
            "disqualify. Its accuracy is still measured and still anchors the retention bar. "
            f"--answer-max-tokens {args.answer_max_tokens:,} reserves the window down to "
            f"{answer_headroom:,} on the closing calls, which the compacting rows' snapshots have "
            "to sit under to be answerable in a model of this size.",
            flush=True,
        )
    elif plan is not None and plan.target_tokens > answer_headroom:
        print(
            f"WARNING: --answer-max-tokens {args.answer_max_tokens:,} reserves the window down to "
            f"{answer_headroom:,} tokens on the closing calls, and this cell is sized to reach "
            f"{plan.target_tokens:,}. A model whose window covers input and output together "
            "cannot write that answer from that snapshot, and it is the closing answers that "
            "carry the accuracy columns. Lower --fill, lower --answer-max-tokens, or raise "
            "--context-window.",
            flush=True,
        )
    # Record replies persist in history, so their cap must fit the reserved output budget.
    if forces_records(strategies) and args.record_max_tokens > args.max_output_tokens:
        print(
            f"WARNING: --record-max-tokens {args.record_max_tokens:,} is above the "
            f"--max-output-tokens {args.max_output_tokens:,} the input budget reserves. The "
            "record is written into the conversation and preserved there, so it is re-sent on "
            "every later turn like any other reply, and the budget holds room for the smaller "
            "number. It is meant to bound that one call below the run's cap, not above it: "
            "raise --max-output-tokens, or lower --record-max-tokens.",
            flush=True,
        )
    filler_turns = plan.filler_turns if plan else args.filler_turns
    filler_tokens = plan.filler_tokens if plan else args.filler_tokens
    # The plan's size rather than the flag's, because --tool-share derives one and then this
    # is the only place it exists. Reading the flag here would build the conversation the run
    # was not asked for while every printed line described the one it was.
    tool_result_tokens = plan.tool_result_tokens if plan else args.tool_result_tokens
    # And the plan's share for the same reason, now that the flag parses to None when it was
    # not given: the resolved share is what sized the payload, it is part of the cell key, and
    # a cell recording None -- or recording 0 for a run that scaled its payload -- would pool
    # with cells that are not the same workload. Without a plan there is no share by
    # construction, since _plan_or_exit refuses one.
    tool_share = plan.tool_share if plan else 0.0
    probe = build_live_scenario(
        salt="probe",
        filler_turns=filler_turns,
        filler_tokens=1,
        tool_turns=args.tool_turns,
        filler_tool_turns=args.filler_tool_turns,
    )
    planted_groups = len(probe.tool_lookups)
    # Every tool-oriented strategy keeps the last `retained` groups verbatim. With no more
    # groups than that, it evicts nothing, changes no tokens, and scores a perfect result for
    # having done nothing at all -- which reads as the best row in the table. Measured: at 3
    # groups against a retention of 4, tool_result and selective_tool_call were exact no-ops
    # while carrying 55% of the planted facts.
    tool_strategies_inert = planted_groups <= retained
    if needs_summarizer(strategies) and args.summarizer_provider is None and not args.dry_run:
        raise SystemExit("Summarization strategies require --summarizer-provider.")
    # Before the provider, the pricing and the first paid call, because a range error in a
    # strategy parameter is a typo and should cost nothing to find.
    _build_or_exit(strategies, _strategy_options(args, tokenizer))

    if args.dry_run:
        scenario = build_live_scenario(
            salt="dry",
            filler_turns=filler_turns,
            filler_tokens=filler_tokens,
            tool_turns=args.tool_turns,
            markers_per_tool=args.markers_per_tool,
            filler_tool_turns=args.filler_tool_turns,
            narration=args.narration,
            subset_questions=workload.subset_questions,
        )
        questions = max(scenario.answer_turn_count, 1)
        # The same fallback run_live applies: a scenario that declares no scopes closes with
        # sweeping questions, so every one of them is the combined question.
        scopes = scenario.answer_scopes or (COMBINED_SCOPE,) * questions
        probes = probe_count(
            scopes, probe_repeats=args.probe_repeats, combined_repeats=args.combined_repeats
        )
        print(
            f"strategies: {len(strategies)}  turns: {len(scenario.transcript.turns)}  facts: "
            f"{len(scenario.facts)}"
        )
        print(
            f"tool-call groups: {planted_groups} planted, {retained} retained by tool-oriented "
            f"strategies"
        )
        if plan is not None:
            print(
                f"fill: {plan.predicted_tokens:,} predicted against {plan.target_tokens:,} target "
                f"({plan.fill_fraction:.0%} of {plan.context_limit:,}), {plan.deviation:+.1%}"
            )
            if plan.tool_share > 0:
                print(
                    f"tool share: {plan.achieved_tool_share:.1%} predicted against "
                    f"{plan.tool_share:.0%} requested, {plan.tool_share_deviation:+.1%}"
                )
            print(
                f"sizing: {plan.filler_turns} filler turns of ~{plan.filler_tokens:,} tokens and "
                f"{planted_groups} tool results of ~{plan.tool_result_tokens:,} tokens; payload "
                f"{plan.payload_tokens:,} tokens, tool results {plan.tool_payload_tokens:,}"
            )
        else:
            print(f"fill: manual, {filler_turns} filler turns of ~{filler_tokens:,} tokens")
        scoped = sum(1 for scope in scopes if scope != COMBINED_SCOPE)
        print(
            f"probes: {scoped} scoped questions x {args.probe_repeats} repeats + "
            f"{questions - scoped} combined x {args.combined_repeats} = {probes} per seed"
        )
        seed_calls = len(scenario.transcript.turns) - questions
        total_calls = len(strategies) * args.repeats * (seed_calls + probes)
        print(f"model calls: >= {total_calls} (more whenever a tool is used)")
        if plan is not None:
            # Every probe carries the whole snapshot, so the probes cost the full prompt each
            # while the seeding averages about half of it. Worth printing before anything is
            # spent: raising either repeat count multiplies the expensive half, not the cheap
            # one -- though a combined repeat is one probe where a probe repeat is one per
            # scoped question, so the two dials are far from the same size.
            seeding = seed_calls * plan.predicted_tokens // 2
            probing = probes * plan.predicted_tokens
            per_run = seeding + probing
            print(
                f"prompt tokens: ~{per_run * len(strategies) * args.repeats:,} in total, "
                f"~{per_run:,} per strategy-seed (~{seeding:,} seeding, ~{probing:,} probing). "
                "Cache reads take most of this off; probing is the half the repeat counts scale."
            )
        # Already built, from the flags this run would use rather than from defaults, by the
        # pre-flight above.
        print("every strategy builds cleanly")
        return 0

    runtime = build_provider(
        provider,
        temperature=workload.temperature,
        response_max_tokens=args.max_output_tokens,
        model=model_override,
    )
    pricing = _resolve_pricing(args, provider, runtime.model)
    if plan is not None:
        # Printed on every run, not only the dry one. These logs are archived and read back
        # months later against runs made with different sizing, and a cell that cannot say
        # what it was aiming at cannot be placed on an axis with the others.
        share = (
            f" Tool share {plan.achieved_tool_share:.1%} predicted against {plan.tool_share:.0%} "
            f"requested, {plan.tool_share_deviation:+.1%}."
            if plan.tool_share > 0
            else ""
        )
        print(
            f"sizing: {plan.filler_turns} filler turns of ~{plan.filler_tokens:,} tokens and "
            f"{planted_groups} tool results of ~{plan.tool_result_tokens:,} tokens, "
            f"predicting {plan.predicted_tokens:,} against a target of {plan.target_tokens:,} "
            f"({plan.fill_fraction:.0%} of {plan.context_limit:,}); payload "
            f"{plan.payload_tokens:,} "
            f"tokens, of which {plan.tool_payload_tokens:,} is tool results.{share}",
            flush=True,
        )

    # Client-side history is required for local compaction to see the full conversation.
    stores_by_default = bool(getattr(runtime.client, "STORES_BY_DEFAULT", False))
    if wants_client_side_history(runtime.client, allow_server_history=workload.server_history):
        print(
            f"note: {runtime.model} keeps history server-side by default. Forcing store=False so "
            "the history is sent by the client and compaction actually applies.",
            flush=True,
        )
    elif stores_by_default:
        print(
            "WARNING: --server-history means the service owns the conversation. The agent sends "
            "only the new turn, so no strategy can compact anything and every row will match the "
            "control. This measures the service, not compaction.",
            flush=True,
        )
    summarizer_client: Any = None
    summarizer_selector: str | None = None
    if args.summarizer_provider is not None:
        sum_provider, sum_model = parse_provider_selector(args.summarizer_provider)
        summarizer_runtime = build_provider(
            sum_provider, temperature=0.0, response_max_tokens=1_024, model=sum_model
        )
        if sum_provider != provider or summarizer_runtime.model != runtime.model:
            raise SystemExit(
                "The summarizer must use the same provider and model as the agent: "
                "this benchmark records one set of prices for both."
            )
        summarizer_client = summarizer_runtime.client
        # The model the provider settled on, not the selector that was typed. A run naming only
        # a provider records which model summarized for it, which is the difference between two
        # cells whose summarizing rows disagree.
        summarizer_selector = f"{sum_provider}:{summarizer_runtime.model}"

    # Built once, above the seed loop, and both recorded and passed on from here. The loop
    # rebuilds StrategyOptions per seed only to hand it a fresh metered summarizer; every
    # number in it is fixed by the command line, so reading them here cannot describe a
    # different configuration from the one each seed runs under.
    settings = _strategy_settings(
        args, _strategy_options(args, tokenizer), summarizer=summarizer_selector
    )

    cell_params = CellParams(
        provider=provider,
        model=runtime.model,
        agent_kind=args.agent,
        context_window=args.context_window,
        fill=args.fill,
        probe_repeats=args.probe_repeats,
        combined_repeats=args.combined_repeats,
        repeats=args.repeats,
        strategies=tuple(strategies),
        narration=args.narration,
        fact_placement=args.fact_placement,
        tool_result_tokens=tool_result_tokens,
        tool_share=tool_share,
        filler_turns=filler_turns,
        filler_tokens=filler_tokens,
        tool_turns=args.tool_turns,
        filler_tool_turns=args.filler_tool_turns,
        markers_per_tool=args.markers_per_tool,
        price_input=pricing.input_per_million,
        price_cached=pricing.cached_read_per_million,
        price_output=pricing.output_per_million,
        price_cache_write=pricing.cache_write_per_million,
        long_context_threshold=pricing.long_context_threshold,
        price_long_input=None
        if pricing.long_context is None
        else pricing.long_context.input_per_million,
        price_long_cached=None
        if pricing.long_context is None
        else pricing.long_context.cached_read_per_million,
        price_long_output=None
        if pricing.long_context is None
        else pricing.long_context.output_per_million,
        price_long_cache_write=None
        if pricing.long_context is None
        else pricing.long_context.cache_write_per_million,
        min_correctness=min_correctness,
        plan=plan,
        workload=workload,
        settings=settings,
    )
    results_path = Path(args.results_jsonl) if args.results_jsonl is not None else None
    # None unless asked for, and read once here so the seed loop below has nothing to decide.
    # Dumping observes the finished conversation and changes none of it; the flag only decides
    # whether anyone looks.
    dump_path = Path(args.dump_record) if args.dump_record is not None else None

    cells: list[CellStats] = []
    for name in strategies:
        print(f"-> {name}", flush=True)
        # Each seed needs an independent meter so summarizer charges cannot cross cells.
        seeds: list[SeedRecord] = []
        for repeat in range(args.seed_offset + 1, args.seed_offset + args.repeats + 1):
            if args.repeats > 1 or args.seed_offset:
                print(f"   seed {repeat}", flush=True)
            summarizer = MeteredClient(summarizer_client) if summarizer_client is not None else None
            scenario = build_live_scenario(
                salt=f"{new_run_id()}-{name}-{repeat}",
                filler_turns=filler_turns,
                filler_tokens=filler_tokens,
                tool_turns=args.tool_turns,
                markers_per_tool=args.markers_per_tool,
                filler_tool_turns=args.filler_tool_turns,
                narration=args.narration,
                subset_questions=workload.subset_questions,
            )
            options = _strategy_options(
                args,
                tokenizer,
                # A recording proxy, not a client: see MeteredClient for why it is cast.
                summarizer=cast("SupportsChatGetResponse[Any] | None", summarizer),
            )
            outcome = await run_live(
                runtime,
                strategy_name=name,
                options=options,
                scenario=scenario,
                agent_kind=args.agent,
                tool_result_tokens=tool_result_tokens,
                force_tool_calls=workload.force_tool_calls,
                narration=args.narration,
                retrieval_guidance=workload.retrieval_guidance,
                fact_placement=args.fact_placement,
                # Preserve the selected history ownership mode.
                allow_server_history=workload.server_history,
                probe_repeats=args.probe_repeats,
                combined_repeats=args.combined_repeats,
                # The cap for the closing answers, and the only calls that carry it. Ordinary
                # calls carry --max-output-tokens, which is the number the strategies reserved
                # out of the window; see the flag's help for why the two are not one.
                answer_max_tokens=settings.answer_max_tokens,
                # Off the recorded settings rather than off the flags, so what the record says
                # this cell was configured with and what the seed was configured with are one
                # expression. Resolving "0 means no bound of my own" twice is how a cell comes
                # to be labelled with a configuration it did not run.
                record_max_tokens=settings.record_max_tokens,
                record_target_tokens=settings.record_target_tokens,
                max_groups_before_record=settings.max_groups_before_record,
                repeat_records=settings.repeat_records,
            )
            # Scored, written and reported here rather than when the cell ends. A seed that
            # has been paid for is durable the moment it exists, and the line that follows is
            # the only sign of progress a cell gives in the hours before its table.
            record = _seed_record(outcome, scenario, pricing, cell_params, repeat)
            if results_path is not None:
                append_seed_record(results_path, record)
            if dump_path is not None:
                _dump_record(dump_path, name, repeat, outcome.record_text)
            seeds.append(record)
            print(_progress(record), flush=True)
        cells.append(_aggregate(name, seeds))

    # A run that stopped early spent almost nothing and answered almost nothing. Ranking it
    # produces "100% cheaper" for a strategy that simply died, and counts it as clearing the
    # correctness bar because a near-zero control makes every ratio look enormous.
    incomplete, oversized, diverged = _excluded_cells(cells)
    if all(any(record.error for record in cell.records) for cell in cells):
        first = next(record.error for cell in cells for record in cell.records if record.error)
        raise SystemExit(
            f"Every strategy failed. First error: {first}"
            + chr(10)
            + "No comparison is possible; nothing below would mean anything."
        )
    if not any(cell.cost > 0 for cell in cells):
        raise SystemExit("No strategy reported any billed tokens, so there is nothing to compare.")

    excluded = incomplete | oversized | diverged
    for line in _exclusion_notes(incomplete, oversized, diverged, args.context_window):
        print(line)
    split = _split_measured(cells)
    ranked, admissible = _verdict_outcomes(cells, incomplete, oversized, diverged, split=split)

    verdict: JointVerdict | None = None
    if diverged:
        # Printed rather than raised, unlike the two reasons below. Those are cells that could
        # not be measured; this one was measured and cannot be ranked, and the table still
        # carries every column the divergence does not touch -- which is most of them, and all
        # of them paid for.
        print()
        print(
            "The uncompacted control ran a different conversation from the rows it is the baseline "
            "for, so this cell has no ranking. See CONTROL DIVERGED below."
        )
    elif not any(outcome.strategy == "none" for outcome in ranked):
        # Only a control that did not finish reaches this. One that overflowed the limit and
        # finished is the cell compaction exists for, and anchors the verdict below; one that
        # overflowed and did *not* finish was most likely refused by the model's real window,
        # which is a fill past what the model accepts rather than past what the cell stands in for.
        advice = (
            "It also exceeded the tried limit, so the model most likely refused the prompt itself: "
            "lower --fill to a size the model accepts, even if that is past --context-window."
            if "none" in oversized
            else "Re-run the cell."
        )
        raise SystemExit(
            f"The uncompacted control did not finish, so at {args.context_window:,} tokens "
            f"there is no "
            f"measurement of what the whole conversation held and nothing can be judged "
            f"against it. {advice}"
        )
    else:
        try:
            verdict = recommend(
                ranked, min_correctness=min_correctness, baseline_admissible=admissible
            )
        except ValueError as error:
            raise SystemExit(f"Cannot summarize: {error}") from error
    if tool_strategies_inert:
        affected = (
            ", ".join(cell.strategy for cell in cells if "tool" in cell.strategy)
            or "the tool strategies"
        )
        print()
        print(
            f"WARNING: {planted_groups} tool-call groups were planted but tool-oriented strategies "
            f"retain the last {retained}, so {affected} evicted nothing. Their scores measure "
            "a no-op, not information preservation. Raise --tool-turns above the retention."
        )
    print(
        _render(
            verdict,
            cells,
            excluded,
            show_answers=args.show_answers,
            min_correctness=min_correctness,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the live comparison.

    Args:
        argv: Argument vector. Defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    return asyncio.run(run_live_comparison(build_parser().parse_args(argv)))
