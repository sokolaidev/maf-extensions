"""Per-seed results, written when they exist rather than when the cell ends.

A cell is five strategies times ``--repeats`` seeds and runs for hours, and its table was
printed only once every one of them had finished. So anything that stopped the process in
between threw away every seed that had already completed and already been paid for. That is
not hypothetical: the 60,000/0.86 cell ran all fifteen strategy-seeds over three and a half
hours, died before printing, and left nothing at all behind.

A seed's result is therefore appended here the moment it is scored. The record carries the
scored numbers rather than a reference to the objects that produced them, because those
objects are what the run cannot keep: the scenario is salted per seed and dies with the
process, and re-scoring later would need it. What is stored is what the table reads, so a
table rebuilt from the file is the same aggregation over the same inputs as the live one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from statistics import fmean
from typing import TYPE_CHECKING, Any, Final

from maf_compaction import (
    DEFAULT_KEEP_HEAD_USER_TURNS,
    DEFAULT_KEEP_TAIL_USER_TURNS,
    DEFAULT_USER_TRIGGER_FRACTION,
    SUMMARY_MODE_RECOMPACT,
)

from ._advisor import ModelPricing
from ._fill import FillPlan
from ._summary import DEFAULT_MIN_CORRECTNESS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "CellParams",
    "SeedRecord",
    "StrategySettings",
    "WorkloadSettings",
    "append_seed_record",
    "group_by_cell",
    "read_seed_records",
]

#: Format of the records this module writes.
#:
#: Bumped whenever a field changes meaning, not merely whenever one is added: the point is
#: that a reader can tell a record describing the current measurement from one describing an
#: older one. Runs before this file existed have no records at all, and runs from before the
#: seed/snapshot/probe rebuild measured `survived` against a different prompt, so mixing their
#: numbers into one table would produce a mean over two different questions.
#:
#: 2 adds the throttling counters. Additive, and bumped anyway: a version 1 record cannot say
#: whether it was throttled or merely never asked, and reading its absent counters as zero
#: would put "not measured" and "did not happen" in the same column.
#:
#: ``combined_repeats`` was added later without a bump, which is the same rule applied to the
#: opposite case: what a version 2 record did is not in doubt. The combined question was one
#: of the closing questions then, so it was asked exactly ``probe_repeats`` times, and
#: :meth:`CellParams.from_dict` fills that in. There is no "not measured" to confuse with a
#: measurement, and the samples on the record say the same thing by their count.
#:
#: 3 adds the connection-retry counters, and this time the version is what separates a record
#: whose run could re-send a dropped call from one whose run could not.
#:
#: 4 adds the probe phase's own token counts, which split ``cost`` into the workload and the
#: instrument. Bumped for the version 2 reason and not the ``combined_repeats`` one: what a
#: version 3 record spent on probing is not merely absent, it is unknowable from the record,
#: and reading the absent counts as zero would report every one of those runs as having probed
#: for free -- which would put the whole of a twelve-probe discount into the seeding half and
#: make the correction this field exists for read as already applied.
#:
#: 5 adds ``groups_kept_uncovered``, and the version is what separates a run whose strategy
#: could decline to drop an unrecorded tool group from one whose strategy could not. Before
#: it, ``tool_summary_anchored`` dropped every tool group in front of the record on the
#: assumption that the record had replaced them, so no group was ever kept for want of
#: coverage and zero is what those runs did. That is the version 3 case exactly, not the
#: version 4 one: the number is knowable and it is zero, rather than unknowable and reported
#: as zero. The bump is there so a reader can tell which of the two zeroes a row is showing.
#:
#: 5 also adds ``fallbacks_after_record``, and deliberately without a further bump. There was no
#: version 5 record for a 6 to date it against when this was written: the bump above was
#: unreleased, and every recorded cell on disk was version 2, 3 or 4. (Runs since have written
#: 5 and 6, so the cells on disk are now 2 through 6; the argument is unaffected, because it is
#: about what the two fields' absence means and not about how many files carry them.) What
#: separates the two fields is not the version but what
#: their absence means, and the values say that themselves -- ``groups_kept_uncovered`` reads
#: back as 0, which is what those runs did, and ``fallbacks_after_record`` reads back as
#: ``None``, because those runs *could* fall back behind a record and counted nothing when they
#: did. That is the version 4 case: not absent but unknowable, and a 0 would tell a reader that
#: every older row stayed the strategy it is named for.
#:
#: 6 adds ``records_in_conversation``, and the bump is the version 3 argument: it separates a
#: record whose run could take more than one recall record from one whose run could not. Before
#: it the size trigger fired once per conversation by construction, so no run could accumulate
#: records, and a reader comparing a cell across the change has to be able to see which side of
#: it a row sits on -- the strategy's cost profile differs, because every record is preserved
#: and raises a floor under the prompt.
#:
#: Its *value* reads back as ``None`` rather than as 1, which is the version 4 argument applied
#: to a field the version 3 argument justified bumping for. What an older run could do is not
#: in doubt; what it did is. Those runs took either one record or none depending on whether the
#: model ever complied, and nobody wrote the number down. A 1 would credit every row with a
#: record, including the rows that flag ``FALLBACK`` precisely because none arrived; a 0 would
#: deny one to every row that took one. The flags on those records say which happened, and
#: reading a count back out of a flag string is exactly the kind of inference this module
#: refuses everywhere else.
#: 7 adds the resolved strategy settings, and the bump is the version 4 argument rather than
#: the version 3 one. Before it a record said what workload it measured and said nothing about
#: what the strategies were configured with, so two runs differing only in a setting keyed as
#: one cell, pooled into one row and were given one verdict: two ``repeat_records`` arms
#: concatenated into one file print a single row near the control, out of arms that measured
#: on opposite sides of it. The settings are part of the cell key from here, which is what
#: stops that.
#:
#: Their value reads back as ``None`` rather than as today's defaults, and that is the point of
#: the version. What an older run was configured with is unknowable from the record: the
#: defaults have moved under it -- ``repeat_records`` did not exist, and ``coverage_share``,
#: ``min_gain_fraction`` and the two record bounds were unreachable from the command line at
#: all -- so filling them in would credit every archived cell with a configuration it may never
#: have run, and would key it as though it shared one with a cell written today. ``None`` keys
#: as ``None``, which equals no other configuration, so an old cell refuses to merge with a new
#: one rather than quietly joining it.
#:
#: 8 adds the resolved workload flags, and it is version 7 again one category along. Version 7
#: put the strategies' configuration on the key and left five options that change the
#: *conversation* on no key at all: whether tool calls were pinned, whether the retrieval clause
#: was appended, whether the closing question was one sweeping ask or several scoped ones, what
#: temperature was sent, and whether the service was left holding the history. Two runs differing
#: in any of them ask the model a different thing, and until now they keyed as one cell and were
#: meaned into one row -- the same defect version 7 fixed for the settings, on the half of the
#: command line the settings block deliberately does not cover. They are not settings and are
#: not recorded as such: a strategy setting moves what compaction does to one conversation,
#: these move which conversation it is.
#:
#: Their value reads back as ``None`` rather than as today's defaults, which is the version 4
#: argument and not version 7's. The defaults have not moved here: every one of these flags
#: existed, with the value it has now, for every run that ever wrote a record. What has never
#: been on the record is whether a run *set* one, and runs did, with ``--no-force-tool-calls``,
#: ``--sweeping-question`` and ``--no-retrieval-guidance`` among them. A default here would
#: therefore not be a stale value but a
#: guess about a command line nobody wrote down, and it would key an archived cell as having
#: held the same conversation as one run today. ``None`` equals only ``None``.
#:
#: 9 adds ``user_compactions`` and ``user_messages_replaced``, and the bump is the version 5
#: argument rather than the version 4 one. Before it no strategy in this package was allowed to
#: touch a user turn at all -- the anchored family shortens tool results, the record-then-drop
#: strategy drops tool groups, and both keep the user side verbatim by construction -- so zero
#: user turns replaced is what every archived run did, and the number is knowable rather than
#: unknowable. The version is what lets a reader tell that zero from the zero a row of
#: ``user_summary_anchored`` shows when its trigger never fired.
#:
#: The three settings the same change adds to :class:`StrategySettings` are read leniently
#: rather than forcing the whole settings block to ``None``, and :func:`_settings_from_dict`
#: says why: they are consulted by one strategy no older record can carry a row for, so on an
#: older record they describe an inapplicable knob rather than an unrecorded measurement.
#:
#: 11 adds ``user_summary_mode`` to the settings block, and ``user_summaries_in_conversation``,
#: ``user_summary_tokens`` and ``user_folds`` to the record. The setting is the version 10
#: argument again: every run before it recompacted its own summary, because no other mode
#: existed, so an absent value reads back as ``recompact`` -- the value those runs demonstrably
#: ran, written as that literal and not as the strategy's default, which is allowed to move --
#: and it keys them apart from a rerun in either boundary mode, which is the point of recording
#: it. The three counters split two ways. ``user_folds`` reads back as zero on the version 9
#: argument: no strategy an older record could select could fold, so zero folds is what those
#: runs did. The standing-summary count and its tokens read back as ``None`` on the version 4
#: argument: those runs carried at most one standing summary, but whether they carried it is a
#: deduction from ``user_compactions`` rather than a number anybody took, and its size is not
#: recoverable from the record at all. Deducing the count from another field would be a model
#: of the run, which is the inference this module refuses everywhere else.
#:
#: 12 adds ``probe_input_samples`` and ``probe_cached_samples``, the probe phase's token counts
#: per probe rather than in one total, and the bump is the version 4 argument one level down.
#: Version 4 put the phase's totals on the record so the seeding half of the cache hit rate
#: could be told from the probe half; what the totals cannot say is how the probe half divided
#: among the twelve probes, and that turned out to be the finding. On the archived luna cells
#: the probe half is two-valued -- about 99%, or 33.3% -- and 33.3% is arithmetically four
#: probes served whole and eight served cold, the four being the only probes with a
#: byte-identical predecessor. Arithmetically, because no record could show it: the per-probe
#: figures were not written down, and the totals are consistent with twelve probes served a
#: third each. These fields make it observation.
#:
#: They read back as ``None`` on every earlier record and never as a division of the totals.
#: Spreading the total over twelve equal shares would be exactly the reading the fields exist
#: to refute, and picking the four-and-eight pattern instead would be writing the conclusion
#: into the evidence. Nobody took those numbers; the record says so.
#:
#: 13 adds ``reforced_calls`` and ``groups_preserved_uncovered``, and the bump is the version 5
#: argument: it separates a run whose record strategy could ask for another record on a
#: measured shortfall, and preserve what asking did not clear, from one whose strategy could do
#: neither. Before it a tool group the record failed to cover stayed in the prompt as an
#: ordinary group and the fallback behind the record could shorten or shed it -- ``UNCOVERED``
#: beside ``RECFALLBACK``. Both read back as zero on an older record, because zero is what
#: those runs did: no call was pinned on a shortfall and no uncovered group was held. The
#: version is what lets a reader tell that zero from the zero a schema 13 row shows when its
#: records were complete and nothing needed asking for.
#:
#: 15 adds ``fallbacks_held_after_record``, on the version 13 argument one gap further on. The
#: two layers 13 introduced protect only what is in front of the record; a tool group after it
#: is covered by no record, and the fallback behind the record could still shorten it: groups
#: preserved in front of the record keep the prompt near the ceiling, the fallback fires pass
#: after pass, and the one tool group after the record inside its band loses its codes on a row
#: that finishes under the limit and reads no ``DQ``. From 15 the fallback behind a record runs
#: with every tool group no record covers
#: held, and the field counts the passes it ran that way. It reads back as zero on an older
#: record, because zero is what those runs did: no group was held for this reason. The version
#: is what tells that zero -- a fallback free to shorten -- from a schema 15 zero, a fallback
#: that never had to act.
#:
#: 16 adds seven counts for ``tool_and_user_summary_anchored``'s last-resort chain --
#: ``records_merged``, ``record_merges_rejected``, ``user_summaries_merged``,
#: ``user_merges_rejected``, ``record_rewrites``, ``record_rewrites_rejected`` and
#: ``last_resort_fallbacks`` -- and ``record_harder_attempts`` to the settings block, and the bump
#: is the version 5 argument again. Before it that row had no chain: it merged nothing, rewrote
#: nothing, and ran its fallback straight behind the record rather than last, so every count
#: reads back as the zero those runs produced and the setting as the zero rewrites they could
#: make. The version is what tells that zero from a schema 16 zero, a chain that was never
#: needed. The same change moved that row's user half from being judged at pass entry to being
#: judged after the record phase, which no field records: the schema is the only mark of it, and
#: a composed row before 16 is a different design from one after it, not an earlier run of the
#: same one. It also retired ``USERSTARVED``, which was only ever a flag; notes already on disk
#: keep it.
#:
#: 17 adds ``user_passes_waited``, on the version 5 argument once more. Before it the composed row's
#: user half was judged on every pass the prompt was over the line, including the pass on which the
#: record half had only just asked for its record, so the user half acted first and -- when its
#: summary took the prompt under the line -- the record was never asked for: measured on
#: gpt-5.6-luna at 200,000 tokens and a 0.8 trigger, no record on five seeds of five. From 17 the
#: user half holds while a record is due, for a bounded number of model responses. The field reads
#: back as zero on an older record because zero is what those runs did: no pass was held. As at 16,
#: the schema is the only mark of the change in ordering itself, and a composed row before 17 is a
#: different design from one after it.
#:
#: 18 adds ``record_rewrites_skipped``, on the version 5 argument once more. Before it the
#: composed row's chain asked again for a rewrite it had already had refused, whenever the prompt
#: went back over the budget with the record unchanged, most of them refused again -- and from
#: 18 an attempt refused on a record is not made again, nor any milder one,
#: until the record changes. The field reads back as zero on an older record because zero is what
#: those runs did: nothing was skipped. It also moves ``record_rewrites`` and
#: ``record_rewrites_rejected``, which no field records: before 18 they counted the live path's
#: second list replaying a refusal as a second attempt and a second refusal, and from 18 that
#: replay is a skip, so a composed row's two counts before 18 read higher for the same work.
#: ``RECMERGESKIP``, the same rule on step a, is a flag and not a field.
#:
#: 19 adds ``chain_gain_fraction`` to the settings block, on the version 5 argument. Before it the
#: composed row's chain stopped as soon as the prompt fitted the budget, which left it just under
#: the budget for the next turn to push back over; from 19 a firing goes on until it has removed
#: that share of the tokens behind its earliest edit. The field reads back as zero on an older
#: record, because zero is what those runs did. The same version keeps every decision the chain
#: makes -- a merge, a rewrite, a fold, narration shed -- on both of the live path's lists, where
#: before it a decision made on a call's copies reached the store only if the store's pass was
#: over the budget too, and otherwise was undone on the next call. No field records that, and it
#: moves two flags: ``RECMERGE`` and ``USERMERGE`` counted the store's replay of a copies-pass
#: merge as a second one, and from 19 count each decision once, with ``CHAINKEPT`` counting the
#: passes that put one back.
#:
#: 20 adds ``price_cache_write`` to the cell, on the version 5 argument: before it no run charged
#: a write premium, so the field reads back as ``None``, the rate those runs used.
#:
#: 21 adds a long-context tier: the threshold and four rates on the cell, and on each record the
#: tokens of the calls billed above it, for the run and for its probes. On the version 5
#: argument: before it no run priced a tier, so the cell fields read back as ``None`` and the
#: token counts as zero -- every call was billed at the one rate, which is what those say.
SCHEMA_VERSION: Final[int] = 21

#: Versions this reader accepts, which is not only the current one.
#:
#: Version 2 is readable because its two absent counters are a measurement rather than a gap: a
#: version 2 record was written by code that failed the turn on a connection error instead of
#: re-sending it, so zero re-sends is what happened, and :meth:`SeedRecord.from_dict` fills it
#: in as such. That is the opposite of the version 1 case, which is still refused -- those
#: records predate the seed/snapshot/probe rebuild and scored ``survived`` against a different
#: prompt, so their accuracy columns are answers to another question.
#:
#: Refusing version 2 instead would have thrown away the six recorded cells on disk, 180 seeds
#: of paid-for measurement, to avoid a column of zeroes that are true.
#:
#: Versions 2 and 3 are readable in the same spirit, and their probe counts come back as
#: ``None`` rather than as zero. Everything those records measured they still measure; the one
#: thing they cannot say is how their cost divided between seeding and probing, and ``None`` is
#: how a reader is told that instead of being handed a number nobody took.
#:
#: Version 4 joins them on the version 2 argument rather than the version 3 one: what its runs
#: did about uncovered tool groups is not in doubt, because their code had no way to keep one.
#:
#: What none of the three can say is how often a fallback ran *behind* a record, so
#: ``fallbacks_after_record`` comes back as ``None`` for all of them, on the probe-count
#: argument rather than the retry one: that path existed in every one of those runs and
#: incremented nothing when it was taken.
#:
#: Version 5 joins them unconditionally: it is the immediately preceding version, it measured
#: everything this one does except how many records a conversation carried, and refusing it
#: would discard cells written by the code as it stood a commit ago for a single absent column.
#: Version 6 joins them on the probe-count argument rather than the retry one. Everything it
#: measured this version still measures; the one thing it cannot say is what its strategies were
#: configured with, and it says that by carrying no settings at all rather than by carrying a
#: plausible set. Refusing it would discard every cell on disk over a block none of them could
#: have written.
#:
#: Version 7 joins on that same argument, for the workload block instead of the settings one.
#: Everything it measured this version still measures; what it cannot say is which of the five
#: workload flags its run set, and it says so by carrying no workload block rather than one
#: filled in from today's defaults.
#:
#: Version 8 joins on the version 4 argument -- what its runs did about user turns is not in
#: doubt, because no strategy they could select was allowed to touch one -- and refusing it
#: would discard every cell on disk over two columns none of them could have written.
#:
#: Version 9 joins on that argument too, for the setting version 10 adds. A version 9 run could
#: select ``user_summary_anchored``, so unlike version 8 it may have compacted user turns -- but
#: the minimum band share did not exist, and a strategy with no such floor is exactly a strategy
#: whose floor is zero. So ``user_min_band_share`` reads back as ``0.0`` on those records: not a
#: default filled in for an unrecorded setting, which version 7's rule forbids, but the value
#: they demonstrably ran, and the value that keys them apart from anything measured since.
#:
#: Version 10 joins on the same two arguments one version along: what its runs did with their
#: summaries is not in doubt, because only one mode existed, so ``user_summary_mode`` reads back
#: as the value they ran and ``user_folds`` as the zero they did; and the two standing-summary
#: columns come back as ``None``, because nobody took those numbers. Refusing it would discard
#: the only cells of this row taken before it had arms to compare.
#:
#: Version 11 joins on the version 4 argument. Everything it measured this version still
#: measures, the probe phase's totals included, so its two hit rates split exactly as a record
#: written today; the one thing it cannot say is which probes the cached tokens landed on,
#: and it says so by carrying no per-probe counts rather than a plausible division of the
#: totals. Refusing it would discard every cell written at that version -- the cells the split
#: exists to re-read.
#:
#: Version 12 joins on the version 5 argument: what its runs did about an uncovered tool group
#: is not in doubt, because their code could neither ask for another record on its account nor
#: preserve it, and the two counters read back as the zero those runs produced.
#:
#: Version 14 joins on that argument one version along: its code could not hold a tool group
#: after the record out of the fallback's reach, so ``fallbacks_held_after_record`` reads back
#: as the zero those runs produced.
#:
#: Version 15 joins on the same argument: its composed row had no last-resort chain, so the
#: chain's seven counts read back as the zeroes those runs produced and ``record_harder_attempts``
#: as zero.
#:
#: Version 16 joins on the same argument: its composed row's user half never waited for a record,
#: so ``user_passes_waited`` reads back as the zero those runs produced.
#:
#: Version 17 joins on the same argument: its chain never skipped a rewrite, so
#: ``record_rewrites_skipped`` reads back as the zero those runs produced.
#:
#: Version 18 joins on the same argument: its chain stopped at the budget, so
#: ``chain_gain_fraction`` reads back as the zero those runs used.
#:
#: Version 19 joins on the same argument: it charged no write premium, so ``price_cache_write``
#: reads back as the ``None`` those runs used.
#:
#: Version 20 joins on the same argument: it priced no long-context tier, so the tier reads back
#: as absent and its token counts as zero.
_READABLE_SCHEMAS: Final[frozenset[int]] = frozenset({*range(2, 21), SCHEMA_VERSION})

#: The parameters that make two records the same cell, and so aggregable into one row.
#:
#: Deliberately excludes ``strategies`` and ``repeats``, which say what a run *intended* to
#: measure rather than what it measured: a cell abandoned after three strategies and resumed
#: for the other two is one cell, and has to aggregate as one. Includes the prices, because
#: two runs priced differently produce costs that cannot go in one column.
#:
#: ``tool_share`` is here although ``tool_result_tokens`` already carries what it derived, and
#: so already separates two workloads that differ: two shares reaching one per-result size
#: would have to build the same conversation. It is kept for the reason ``fill`` is, which is
#: likewise nearly implied by ``filler_turns`` and ``filler_tokens`` -- the key states the
#: parameter that was set and not only the number it produced, so a file holding a sweep can
#: be read back against the sweep's own axes.
#:
#: ``settings`` is the field the key was missing. Everything above it says what workload was
#: measured and none of it said what the strategies were configured with, so two runs differing
#: only in a setting were one cell -- see :data:`SCHEMA_VERSION` for the row that produced.
#: Every settings comparison this project has made stayed separate only because the files were
#: kept apart by hand.
#:
#: It is the whole settings block rather than the settings a cell's strategies happen to read.
#: Deriving the read set would need the strategy list, which is deliberately *not* in the key --
#: a cell abandoned and resumed names two different lists and has to stay one cell -- so the
#: read set is only knowable once the records are grouped, which is what this key decides. The
#: cost of taking all of it is real and points one way: two runs differing in a setting no
#: strategy in the cell consults will not pool, and will be reported as two combinations whose
#: difference the cross-cell section names. A split that is visible is undone by hand; a merge
#: that is silent is the defect.
#:
#: ``workload`` is the other half of the same omission, and sits above ``settings`` rather than
#: inside it because the two are read for different things. A setting says what compaction did
#: to a conversation; these say which conversation it was -- pinned tool calls or the model's
#: own, the retrieval clause or not, scoped closing questions or one sweeping ask, a temperature
#: or none, the history on the client or on the service. Being in this tuple and outside
#: :data:`_MODEL_KEY_FIELDS` also puts it in :attr:`CellParams.workload_key`, which is what stops
#: two of them being ranked against each other as though one strategy had beaten another.
_CELL_KEY_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "model",
    "agent_kind",
    "context_window",
    "fill",
    "probe_repeats",
    "combined_repeats",
    "narration",
    "fact_placement",
    "tool_result_tokens",
    "tool_share",
    "filler_turns",
    "filler_tokens",
    "tool_turns",
    "filler_tool_turns",
    "markers_per_tool",
    "price_input",
    "price_cached",
    "price_output",
    "price_cache_write",
    "long_context_threshold",
    "price_long_input",
    "price_long_cached",
    "price_long_output",
    "price_long_cache_write",
    "workload",
    "settings",
)

#: The part of the key that says who was asked, rather than what was asked of them.
#:
#: Split out of the key rather than listed twice, so a field added to one is added to the
#: other: :attr:`CellParams.workload_key` is the key less these and less ``settings``, and a
#: workload field that quietly landed in neither would let two conversations be ranked against
#: each other. The rates are here and not in the workload because every ranking below is on
#: cost, and a cost measured at two price lists is two numbers in one column.
_MODEL_KEY_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "model",
    "agent_kind",
    "price_input",
    "price_cached",
    "price_output",
    "price_cache_write",
    "long_context_threshold",
    "price_long_input",
    "price_long_cached",
    "price_long_output",
    "price_long_cache_write",
)


def _plan_from_dict(data: Mapping[str, Any]) -> FillPlan:
    """Rebuild the solved sizing from its serialized form.

    Field by field rather than by splatting the mapping, so a file carrying a key this version
    does not know about is refused here instead of raising somewhere less obvious.

    Args:
        data: The ``plan`` object from a record's cell parameters.

    Returns:
        The plan.
    """
    return FillPlan(
        filler_turns=int(data["filler_turns"]),
        filler_tokens=int(data["filler_tokens"]),
        context_limit=int(data["context_limit"]),
        fill_fraction=float(data["fill_fraction"]),
        target_tokens=int(data["target_tokens"]),
        predicted_tokens=int(data["predicted_tokens"]),
        payload_tokens=int(data["payload_tokens"]),
        tool_payload_tokens=int(data["tool_payload_tokens"]),
        # The two sizing fields are read leniently where the rest are not, because a plan
        # written before the payload could be derived has neither and its run is not in doubt:
        # the size was stated, which is what a share of 0 means, and it is on the cell beside
        # this plan. Demanding them here would refuse every record already on disk.
        tool_result_tokens=int(data.get("tool_result_tokens", 0)),
        tool_share=float(data.get("tool_share", 0.0)),
    )


@dataclass(frozen=True, slots=True)
class WorkloadSettings:
    """Which conversation was had, for the five options no other field on the cell records.

    Everything else on :class:`CellParams` describes the shape of the workload -- how long it
    was, how much of it was tool output, where the codes sat. These describe what was *asked*
    and how it was sent: whether the harness pinned each lookup or let the model choose, whether
    the retrieval clause was appended to the instructions, whether the conversation closed with
    several scoped questions or one sweeping one, what temperature the request carried, and
    whether the service was left holding the history. Each of them moves the answers, and none
    of them was on any key, so two runs differing in one were one cell and one mean.

    Not part of :class:`StrategySettings`, and the split is the point rather than tidiness. A
    strategy setting changes what compaction does to a conversation; these change which
    conversation it is. Filed together they would be ranked together, and the cross-cell section
    would offer to name a "best" across two different questions -- which it must never do, and
    which being in :attr:`CellParams.workload_key` is what prevents.

    Recorded from the resolved values, on the same rule the settings block follows: the flags
    are negative and the values are not, ``--no-temperature`` resolves to a temperature of
    ``None`` rather than to a boolean, and the run is configured from these fields rather than
    from a second reading of ``args``.

    Every field is required, so a live run cannot omit one and inherit a plausible value. The
    only way a record carries no workload block is by predating it, where the whole of it is
    ``None`` -- see :data:`SCHEMA_VERSION`.
    """

    force_tool_calls: bool
    """Whether each lookup turn pinned ``tool_choice`` to the function it wanted.

    Off, the model gathers its own facts and often fewer of them: measured at 3 of 6 scopes
    reached and a 33% input swing between identical runs on one model. A cell that let the model
    choose is not measuring the same conversation as one that did not.
    """
    retrieval_guidance: bool
    """Whether the instructions carried the clause telling the model to quote every identifier.

    It is worth up to the whole accuracy column: the control scored 33% without it and 100% with
    it at a 900-token answer cap.
    """
    subset_questions: bool
    """Whether the conversation closed with several scoped questions or one sweeping ask.

    Recorded as the resolved positive, which is what ``build_live_scenario`` is handed;
    ``--sweeping-question`` is the flag that turns it off. It changes the number of closing
    turns, the length of the answers and therefore what ``acc1`` is a mean over.
    """
    temperature: float | None
    """The sampling temperature sent, or ``None`` when the request omitted the field entirely.

    The value, not the flag, because ``--no-temperature`` names an omission rather than a
    number. A provider that rejects the option mid-run is a different event and is recorded per
    seed on ``dropped_options`` as ``NO:temp``: this says what the run asked for, that says what
    the provider allowed.
    """
    server_history: bool
    """Whether the service was allowed to keep the conversation.

    On, the agent sends only the new turn, so no strategy can compact anything and every row
    matches the control -- which measures the service rather than compaction. Recorded because
    a cell that measured the service must never be pooled with one that measured compaction,
    and nothing else on the record would say which it was.
    """

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of these flags."""
        return asdict(self)


def _workload_from_dict(data: Mapping[str, Any]) -> WorkloadSettings:
    """Rebuild the workload block from its serialized form.

    Field by field rather than by splatting, for the reason :func:`_plan_from_dict` is: a file
    carrying a key this version does not know about is refused here rather than somewhere less
    obvious. Nothing is read leniently, because only a record carrying the whole block carries
    any of it -- an older one has no ``workload`` object for this function to be given.

    Args:
        data: The ``workload`` object from a record's cell parameters.

    Returns:
        The flags.
    """
    return WorkloadSettings(
        force_tool_calls=bool(data["force_tool_calls"]),
        retrieval_guidance=bool(data["retrieval_guidance"]),
        subset_questions=bool(data["subset_questions"]),
        temperature=None if data["temperature"] is None else float(data["temperature"]),
        server_history=bool(data["server_history"]),
    )


@dataclass(frozen=True, slots=True)
class StrategySettings:
    """What the strategies of a cell were configured with, resolved rather than as typed.

    The knobs, beside the workload the fields above already describe. Every value here is one
    that was handed to a constructor, taken from the object the run built rather than from the
    flag that named it, so a cell whose retention was derived, whose ``0`` meant "no bound of
    my own" or whose summarizer was picked by a provider selector records what the strategies
    actually ran with. The flag and the value disagree often enough for that to matter:
    ``--keep-tokens 0`` is a derivation, not a retention of nothing.

    Every field is required. The missing default is the guard the retry counters have: a live
    run cannot omit one and quietly inherit a plausible number, and the only way a record
    carries no settings at all is by predating the block, where the whole of it is ``None``.

    A test pins this against :class:`~._strategies.StrategyOptions` field by field, because the
    failure the block exists to prevent is precisely a knob that reaches a constructor without
    reaching the record: the row moves, the file says nothing, and two cells merge.
    """

    keep_last_groups: int
    keep_last_tool_call_groups: int
    keep_head_groups: int
    keep_tail_groups: int
    keep_tokens: int | None
    """Fixed retention per collapsed tool result, or ``None`` when it was derived from the band."""
    band_share: float
    min_gain_fraction: float
    trigger_fraction: float
    fallback_fraction: float
    coverage_share: float
    keep_head_user_turns: int
    """User turns ``user_summary_anchored`` left verbatim at the start of the conversation."""
    keep_tail_user_turns: int
    """User turns it left verbatim at the end: the live request, and whatever else is asked for.

    Beside the head rather than folded into one number, because the two ends are not
    interchangeable and a cell that moved only one of them is a different measurement from one
    that moved both.
    """
    user_trigger_fraction: float
    """Share of the input budget at which ``user_summary_anchored`` summarises the user band.

    Its own field rather than ``trigger_fraction`` above, which belongs to
    ``tool_summary_anchored``. Recording one number for both would key two runs as one cell
    whenever a sweep moved either, which is the whole defect this block exists to stop.

    **It describes the single row only, and a reader of a composed row must not take it from
    here.** ``tool_and_user_summary_anchored`` judges both of its halves at
    ``trigger_fraction`` above -- the record half against the prompt the pass began with, the
    user half against the prompt the record half left, from schema 16; before it, both against
    the prompt the pass began with -- so a record whose row is that one is a record whose user
    half was judged at ``trigger_fraction`` while this field says something else about a row that
    was not being run. ``compaction/_composed`` carries the argument.
    """
    user_min_band_share: float
    """Share of the prompt the band had to be worth before ``user_summary_anchored`` acted.

    The hysteresis, and the field that decides what a row of that strategy means. At ``0.0`` the
    strategy compacts on every pass past its trigger -- one summarizer call and one rewritten
    prefix per turn, measured at ``USERCOMPACT:31`` (double-counted, about fifteen passes) with a
    seeding-phase cache hit rate of 77% against the control's 95% -- and above it
    the passes are bounded by how fast the band regrows. Two runs either side of that are not
    one cell, which is why it is here and not only in the strategy.

    ``0.0`` on a record written before schema 10, and that is a measurement rather than a
    default: those runs had no such floor, which is the same thing as a floor of zero. See
    :data:`SCHEMA_VERSION`.
    """
    user_summary_mode: str
    """What ``user_summary_anchored`` did with the summary its previous pass left behind.

    ``recompact``, ``boundary`` or ``fold``, and the field that decides the whole cache side of
    the row: the recompacting arm rewrites a message just behind the head on every pass -- a
    strict-prefix break -- the boundary arm never rewrites it and
    accumulates a floor, and the fold arm collapses that floor when it is worth the break. Two
    runs in different modes are not one cell.

    ``recompact`` on a record written before schema 11, and that is a measurement rather than a
    default: no other mode existed, so recompacting is what those runs did. Read as the literal
    rather than as the strategy's default, so that the default moving cannot relabel them. See
    :data:`SCHEMA_VERSION`.

    It describes the single row only from schema 16: ``tool_and_user_summary_anchored`` runs its
    user half in ``boundary`` whatever this says. Before 16 this field reached that row too.
    """
    record_harder_attempts: int
    """Harder rewrites of the record ``tool_and_user_summary_anchored``'s chain could try per pass.

    Read by that row alone. Zero on a record written before schema 16, and that is what those runs
    could do: the row had no chain.
    """
    chain_gain_fraction: float
    """Share of the tokens behind its earliest edit the composed row's chain removed once started.

    Read by ``tool_and_user_summary_anchored`` alone. Zero on a record written before schema 19,
    and that is what those runs did: the chain stopped at the budget. Two runs at different values
    are not one cell -- the setting decides how often the chain fires and so the whole cache side
    of the row.
    """
    token_budget_fraction: float
    max_output_tokens: int
    """The output reservation, which every anchored and composed ceiling is the window less.

    Also the cap sent on every ordinary call, and the one a forced record call falls back to.
    Reserved and sent are one number here, which they were not before schema 8: the arithmetic
    deducted this while the request carried ``answer_max_tokens``, so a cell recorded at a
    60,000 window and a 2,048 reservation was thresholding against 57,952 tokens of input while
    a reply could take 12,000 of them. A record from that era is separated from one after it by
    the schema, not by this field, since the pair of numbers is unchanged and only what was
    done with them moved.

    Not implied by ``context_window`` on the cell beside it: the strategies are sized against
    the difference, so two runs at one window and two reservations compacted to two different
    ceilings while every workload column matched.
    """
    answer_max_tokens: int
    """The cap sent on the closing questions, and reserved out of the window for them.

    Its own field rather than derived, because it is the only output number a run can set that
    no threshold is a fraction of: nothing follows a closing answer, so its length is never
    re-sent, and the calls that do get re-sent carry ``max_output_tokens`` instead.
    """
    tokenizer: str
    """Name of the counter every threshold was measured with.

    A strategy fires on a token count, so two runs counting differently fire at different
    points of the same conversation. Recorded as the name rather than the object, which is what
    a file can carry and what ``--tokenizer`` selects.
    """
    summarizer: str | None
    """``provider:model`` of the client the summarizing strategies used, or ``None`` for none.

    Resolved, so a run that named only a provider records the model that provider chose. Two
    summarizers are two strategies wearing one name, and only their rows move.
    """
    record_max_tokens: int | None
    record_target_tokens: int | None
    max_groups_before_record: int | None
    repeat_records: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of these settings."""
        return asdict(self)


def _settings_from_dict(data: Mapping[str, Any]) -> StrategySettings:
    """Rebuild the settings block from its serialized form.

    Field by field rather than by splatting, for the reason :func:`_plan_from_dict` is: a file
    carrying a key this version does not know about is refused here rather than somewhere less
    obvious. Nothing is read leniently, because only a record carrying the whole block carries
    any of it -- an older one has no ``settings`` object for this function to be given.

    Args:
        data: The ``settings`` object from a record's cell parameters.

    Returns:
        The settings.
    """
    return StrategySettings(
        keep_last_groups=int(data["keep_last_groups"]),
        keep_last_tool_call_groups=int(data["keep_last_tool_call_groups"]),
        keep_head_groups=int(data["keep_head_groups"]),
        keep_tail_groups=int(data["keep_tail_groups"]),
        keep_tokens=None if data["keep_tokens"] is None else int(data["keep_tokens"]),
        band_share=float(data["band_share"]),
        min_gain_fraction=float(data["min_gain_fraction"]),
        trigger_fraction=float(data["trigger_fraction"]),
        fallback_fraction=float(data["fallback_fraction"]),
        coverage_share=float(data["coverage_share"]),
        # The three user-band settings are read leniently where every other field here is not,
        # and the licence is narrow: they are consulted by exactly one strategy, and no record
        # written before schema 9 can carry a row for it, because it did not exist. So the
        # value on an older record is not an unrecorded measurement -- the version 7 case, where
        # ``None`` is the only honest answer -- but an inapplicable knob, and filling it in with
        # the defaults keys an archived cell as poolable with a rerun of the same cell instead
        # of splitting it over a setting that could not have applied to it. This is
        # ``_plan_from_dict``'s reasoning about its two sizing fields, one block along.
        keep_head_user_turns=int(data.get("keep_head_user_turns", DEFAULT_KEEP_HEAD_USER_TURNS)),
        keep_tail_user_turns=int(data.get("keep_tail_user_turns", DEFAULT_KEEP_TAIL_USER_TURNS)),
        user_trigger_fraction=float(
            data.get("user_trigger_fraction", DEFAULT_USER_TRIGGER_FRACTION)
        ),
        # Read leniently on a different licence from the three above, and the number is not this
        # version's default. A record written before schema 10 ran a strategy that had no
        # minimum band share, which is a minimum band share of zero -- so 0.0 here is what that
        # run did, and keying it apart from a run that set the default is the point.
        user_min_band_share=float(data.get("user_min_band_share", 0.0)),
        # The same licence as the band share, one schema along: a record written before 11 ran
        # a strategy that could only recompact, so that mode is what it did. The literal and
        # not DEFAULT_SUMMARY_MODE on purpose -- the default is allowed to move once the arms
        # have been measured, and an archived cell must not move with it.
        user_summary_mode=str(data.get("user_summary_mode", SUMMARY_MODE_RECOMPACT)),
        # The band-share licence once more, schema 16 along: an older composed row had no chain,
        # which is a harder-attempt count of zero, and the single rows never read it.
        record_harder_attempts=int(data.get("record_harder_attempts", 0)),
        # The same licence, schema 19 along: an older composed row's chain stopped at the budget,
        # which is a gain fraction of zero, and the single rows never read it.
        chain_gain_fraction=float(data.get("chain_gain_fraction", 0.0)),
        token_budget_fraction=float(data["token_budget_fraction"]),
        max_output_tokens=int(data["max_output_tokens"]),
        answer_max_tokens=int(data["answer_max_tokens"]),
        tokenizer=str(data["tokenizer"]),
        summarizer=None if data["summarizer"] is None else str(data["summarizer"]),
        record_max_tokens=None
        if data["record_max_tokens"] is None
        else int(data["record_max_tokens"]),
        record_target_tokens=None
        if data["record_target_tokens"] is None
        else int(data["record_target_tokens"]),
        max_groups_before_record=(
            None
            if data["max_groups_before_record"] is None
            else int(data["max_groups_before_record"])
        ),
        repeat_records=bool(data["repeat_records"]),
    )


@dataclass(frozen=True, slots=True)
class CellParams:
    """What was being measured, carried on every record rather than written once as a header.

    A header is a thing a partial file can be missing, and a file that needs one to be read is
    back to losing the seeds a crash left behind. Repeating the parameters per line also keeps
    two runs appended to one path separable, since the cell is read off the line rather than
    off the file.
    """

    provider: str
    model: str
    agent_kind: str
    context_window: int
    """The limit the cell stands in for, which is simulated and enforced in our own code."""
    fill: float
    """Share of that limit the seeded conversation was sized to reach, 0 for manual sizing."""
    probe_repeats: int
    repeats: int
    """Seeds per strategy the run set out to take.

    Stored so that a short cell reads as unfinished rather than as a cell whose strategies
    happened to disagree. Without it, three seeds of a five-seed cell are indistinguishable
    from a complete three-seed one, and every spread in the table is a spread over less
    material than it claims.
    """
    strategies: tuple[str, ...]
    """The strategies the run set out to measure, for the same reason."""
    narration: str
    fact_placement: str
    tool_result_tokens: int
    """Size each tool result was built to, whether stated or derived from ``tool_share``.

    What the run did, not what it was asked for, so a table rebuilt from the file describes
    the workload that was measured however it came to be chosen.
    """
    filler_turns: int
    filler_tokens: int
    tool_turns: int
    filler_tool_turns: int
    markers_per_tool: int
    price_input: float
    price_cached: float
    price_output: float
    combined_repeats: int = 1
    """Times the combined question was asked per seed: the denominator of ``acc2``.

    Part of what makes two records the same cell, for the same reason ``probe_repeats`` is:
    an ``acc2`` averaged over three attempts per seed and one averaged over a single attempt
    are readings of different precision and do not belong in one column.

    Defaulted to 1 for a record written before the combined question had its own count, where
    it was one of the closing questions and so asked ``probe_repeats`` times --
    :meth:`from_dict` fills that in, so an old file reads as the measurement it was.
    """
    tool_share: float = 0.0
    """Share of the fill target the tool results were sized to reach, 0 when the size was stated.

    The request beside its result: ``tool_result_tokens`` above is what this derived. Both are
    recorded because neither reconstructs the other -- the size cannot say what fraction of the
    context it was meant to be, and the share cannot be turned back into a size without the
    window, the fill and the tool-group count.

    Defaulted to 0 for a record written before the payload could be derived, where the size was
    stated outright and 0 is what that means.
    """
    price_cache_write: float | None = None
    """The cache-write rate every uncached input token was charged at, ``None`` for none.

    ``None`` on a record written before schema 20, and that is a measurement rather than a gap:
    those runs charged uncached input at ``price_input``, which is what ``None`` means here. In
    the key, like the other rates, so a cell priced with a write premium never pools with one
    priced without.
    """
    long_context_threshold: int | None = None
    """Input tokens above which a request was billed at the ``price_long_*`` rates, or ``None``.

    ``None`` on a record written before schema 21, and on a cell priced at one rate: either way
    every call was billed at ``price_input`` and its siblings. In the key with the rates.
    """
    price_long_input: float | None = None
    price_long_cached: float | None = None
    price_long_output: float | None = None
    price_long_cache_write: float | None = None
    min_correctness: float = DEFAULT_MIN_CORRECTNESS
    """The correctness bar the verdict applied.

    Recorded because it is the one input to the verdict that is not otherwise on the record: a
    table rebuilt under a different bar would rank rows the original never ranked while every
    column above the verdict stayed identical, which is the hardest kind of disagreement to see.
    """
    plan: FillPlan | None = None
    """The solved sizing, when ``--fill`` was used.

    Kept whole rather than reduced to its target, so the achieved fill can be checked against
    it from the file exactly as the live run checks it.
    """
    workload: WorkloadSettings | None = None
    """Which conversation was had, and part of what makes two records one cell.

    ``None`` on a record written before schema 8, where the five flags are unknowable rather
    than defaulted -- see :data:`SCHEMA_VERSION`. A reader must treat that as "not comparable on
    what was asked" and not as "the same question as everything else": ``None`` equals only
    ``None``, so those cells group with each other and with nothing that carries a block.
    """
    settings: StrategySettings | None = None
    """What the strategies were configured with, and part of what makes two records one cell.

    ``None`` on a record written before schema 7, where the settings are unknowable rather than
    defaulted -- see :data:`SCHEMA_VERSION`. A reader must treat that as "not comparable on
    settings" and not as "the same settings as everything else": ``None`` equals only ``None``,
    so those cells group with each other and with nothing that carries a block.
    """

    @property
    def key(self) -> tuple[Any, ...]:
        """Return what identifies this cell, for grouping records that belong in one table."""
        return tuple(getattr(self, name) for name in _CELL_KEY_FIELDS)

    @property
    def pricing(self) -> ModelPricing:
        """Return the rates the costs on these records were computed at."""
        return ModelPricing(
            input_per_million=self.price_input,
            cached_read_per_million=self.price_cached,
            output_per_million=self.price_output,
            cache_write_per_million=self.price_cache_write,
            long_context_threshold=self.long_context_threshold,
            long_context=(
                None
                if self.price_long_input is None
                else ModelPricing(
                    input_per_million=self.price_long_input,
                    cached_read_per_million=(
                        self.price_long_cached
                        if self.price_long_cached is not None
                        else self.price_long_input
                    ),
                    output_per_million=(
                        self.price_long_output
                        if self.price_long_output is not None
                        else self.price_output
                    ),
                    cache_write_per_million=self.price_long_cache_write,
                )
            ),
        )

    @property
    def label(self) -> str:
        """Return a one-line description of the cell, for a file holding more than one."""
        fill = f"fill {self.fill:.0%}" if self.fill > 0 else "fill manual"
        # The share, when there was one, sits beside the size rather than replacing it: the
        # size is what the conversation carried and the share is why it was that size, and a
        # sweep across window sizes needs both to be readable as one axis.
        payload = f"payload {self.tool_result_tokens:,}x{self.tool_turns}"
        if self.tool_share > 0:
            payload += f" at share {self.tool_share:.0%}"
        return (
            f"{self.provider}:{self.model}  agent {self.agent_kind}  "
            f"window {self.context_window:,}  {fill}  {payload}  "
            f"probes {self.probe_repeats}  combined {self.combined_repeats}"
        )

    @property
    def workload_label(self) -> str:
        """Return the conversation this cell measured, with the model and its settings left out.

        The axis a cross-cell comparison is allowed to rank within. Everything a strategy is
        judged on moves with these, so two cells that differ anywhere here are two jobs and not
        two answers to one question -- which is why the label names the narration, the placement
        and the filler sizing that :attr:`label` leaves to the file name.

        The workload flags are named for the same reason and are not optional here: they are in
        :attr:`workload_key`, so two cells differing in one become two sections of the cross-cell
        report, and a heading that did not say which is which would print the two identically.
        """
        fill = f"fill {self.fill:.0%}" if self.fill > 0 else "fill manual"
        payload = f"payload {self.tool_result_tokens:,}x{self.tool_turns}"
        if self.tool_share > 0:
            payload += f" at share {self.tool_share:.0%}"
        return (
            f"window {self.context_window:,}  {fill}  {payload}  "
            f"narration {self.narration}  facts {self.fact_placement}  "
            f"filler {self.filler_turns}x{self.filler_tokens:,}  "
            f"probes {self.probe_repeats}  combined {self.combined_repeats}  "
            f"{self.workload_flags}"
        )

    @property
    def workload_flags(self) -> str:
        """Return the five workload flags as the workload heading names them.

        Every one of them, always, rather than only the ones that differ from the defaults.
        Printing the non-default ones alone would make silence mean "the defaults", which is
        precisely what a record that predates the block cannot say -- and that record is the one
        this has to be distinguishable from.
        """
        if self.workload is None:
            return "flags not recorded"
        flags = self.workload
        return (
            f"tools {'pinned' if flags.force_tool_calls else 'free'} "
            f"guidance {'on' if flags.retrieval_guidance else 'off'} "
            f"questions {'scoped' if flags.subset_questions else 'sweeping'} "
            f"temp {'none' if flags.temperature is None else f'{flags.temperature:g}'} "
            f"history {'server' if flags.server_history else 'client'}"
        )

    @property
    def workload_key(self) -> tuple[Any, ...]:
        """Return what makes two cells the same conversation, model and settings aside.

        The cell key less the model, the rates and the strategy settings: what was asked of the
        agent, rather than who was asked or how the strategies were tuned. The workload block
        stays in, because pinning the tool calls or dropping the retrieval clause changes what
        was asked as surely as the fill fraction does.
        """
        return tuple(
            getattr(self, name)
            for name in _CELL_KEY_FIELDS
            if name not in _MODEL_KEY_FIELDS and name != "settings"
        )

    @property
    def model_key(self) -> tuple[Any, ...]:
        """Return what makes two cells the same model at the same rates.

        The rates are in here rather than in the workload because a cost measured at two price
        lists is two numbers in one column, and every ranking below is on cost.
        """
        return tuple(getattr(self, name) for name in _MODEL_KEY_FIELDS)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of these parameters."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CellParams:
        """Rebuild parameters from a mapping read back out of a record.

        Args:
            data: One record's ``cell`` object.

        Returns:
            The parameters.
        """
        known = {field.name for field in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["strategies"] = tuple(values.get("strategies") or ())
        # A record written before the combined question had its own count asked it as one of
        # the closing questions, so it was asked exactly probe_repeats times. Taking the
        # field's own default of 1 instead would report a three-attempt cell as a single
        # attempt, and taking today's default of 3 would report one attempt as three that
        # mostly failed. Neither is what the file says; this is.
        values.setdefault("combined_repeats", int(values.get("probe_repeats", 1)))
        plan: dict[str, Any] | None = values.get("plan")
        values["plan"] = _plan_from_dict(plan) if plan is not None else None
        # Absent means unknown, and unknown stays unknown. Filling in this version's defaults
        # would hand every archived cell a configuration nobody recorded and let it key as
        # though it shared one with a cell written today -- see SCHEMA_VERSION.
        settings: dict[str, Any] | None = values.get("settings")
        values["settings"] = _settings_from_dict(settings) if settings is not None else None
        # The same rule for the same reason, one category along. The defaults of these five have
        # not moved, but whether a run set one was never written down and runs did set them, so
        # filling them in would be a guess about a command line rather than a stale value.
        workload: dict[str, Any] | None = values.get("workload")
        values["workload"] = _workload_from_dict(workload) if workload is not None else None
        return cls(**values)


#: Fields stored as JSON arrays, which come back as lists and have to be re-tupled.
#: A list here would compare unequal to the tuple the live path produces, which is exactly
#: the kind of difference that makes "the file reproduces the table" untestable.
_TUPLE_FIELDS: Final[tuple[str, ...]] = (
    "correctness_samples",
    "ignored_samples",
    "combined_samples",
    "strategy_notes",
    "dropped_options",
)


@dataclass(frozen=True, slots=True)
class SeedRecord:
    """One seed of one strategy, reduced to everything a table needs and nothing else.

    Not a serialized ``LiveOutcome``. The outcome carries every prompt of every call, which is
    the bulk of a run and none of what the table reads, and it is unscored -- rebuilding a
    table from it would mean re-scoring against a scenario whose markers are salted per seed
    and which is gone the moment the process is. Scoring happens once, here, and both the live
    table and one rebuilt months later aggregate the result of it.
    """

    cell: CellParams
    strategy: str
    seed: int
    """1-based index of this seed within its strategy."""
    cost: float
    """The whole seed: seeding, every probe, and the strategy's own summarizer calls.

    What the run was billed, which is not what a strategy is ranked on: see
    :attr:`seeding_cost` for the half of it that is the workload.
    """
    summarizer_cost: float
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    probe_input_tokens: int | None
    """Input tokens the probe phase billed, of ``input_tokens``.

    The instrument's share. Every probe re-sends the whole snapshot, so a strategy with a small
    snapshot is discounted once per probe on a phase no deployed agent has: an agent continues
    the conversation, it is not interrogated twelve times from a frozen state. Recorded so the
    ranking can be taken on the other half.

    ``None`` on a record written before version 4, where the phases were never counted apart
    and no arithmetic over the stored fields can separate them: the per-probe prompts are not
    on the record, and pricing twelve of them at ``prompt_tokens_final`` is a model of the run
    rather than the run. Those records rank on their whole cost and say so.
    """
    probe_cached_tokens: int | None
    """How many of those the provider served from cache, or ``None`` before version 4."""
    probe_output_tokens: int | None
    """Output tokens the probe phase billed, or ``None`` before version 4."""
    probe_input_samples: tuple[int, ...] | None
    """Input tokens each probe billed, one entry per probe in the order they were asked.

    The order is the order the run asked them: each closing question in turn, each repeated
    its own number of times, the combined question last. So on the archived configuration --
    seven scoped questions once each, the combined question five times -- the last four
    entries are the only probes with a byte-identical predecessor, and a probe half that hit
    on those four and nothing else shows here as eight cold entries followed by four warm.

    ``None`` on a record written before version 12, where the phase's totals were taken and
    its division among the probes was not. Not a twelfth of the total each: that is the
    reading these fields exist to test, and a record that predates them cannot vouch for it.
    """
    probe_cached_samples: tuple[int, ...] | None
    """Input tokens each probe was served from cache, in the same order, or ``None`` before 12."""
    calls: int
    messages_left: int
    messages_peak: int
    prompt_tokens_final: int
    prompt_tokens_peak: int
    seed_prompt_tokens: int
    """Billed size of the last seeding prompt: the achieved fill."""
    facts_total: int
    facts_left: int
    """Planted facts that survived compaction into the snapshot, which is recall's ceiling."""
    facts_lost: int
    """Facts compaction removed: the total, less what survived, less what was never fetched.

    Stored per seed although the table derives its own ``lost`` column from the cell's means.
    The two agree whenever every seed planted the same number of facts, which is every cell
    this package builds; they can differ only if a seed's scenario had a different size.
    """
    nofetch: int
    """Facts the agent never fetched, so compaction never had them to lose."""
    correctness_samples: tuple[float, ...]
    """One reading per probe repeat, each scored against the same snapshot.

    A tuple rather than a mean, because the mean of the seed and the spread within it are
    different findings and the cell needs both. Flattened across seeds, these are also what
    the verdict ranks on.
    """
    ignored_samples: tuple[int, ...]
    """Facts still in the snapshot the model did not use, per probe repeat."""
    combined_samples: tuple[float, ...]
    """Share of all planted values present in the single combined answer, per repeat."""
    disqualified: bool
    """Whether any call sent a prompt larger than the limit this cell stands in for."""
    context_drift: int
    rate_limit_retries: int
    """Calls this seed re-sent after the provider refused them for rate reasons."""
    throttled_seconds: float
    """Seconds this seed spent waiting those refusals out.

    Recorded beside the count because the two answer different questions. The count says the
    limit was met; this says how much of the seed's wall clock went into meeting it, which is
    what makes a cache hit rate comparable with a cell that never waited at all.
    """
    connection_retries: int
    """Calls this seed re-sent because the request never came back with an answer.

    Its own field rather than added to the throttled count, because the reader's next question
    differs. A throttled seed waited out a quota window, which is wide enough that the prompt
    cache may have gone with it; a reconnected seed waited seconds and re-sent the same prefix.
    Summed into one column, every reconnected row would carry a caveat that belongs to the
    other failure.
    """
    connection_seconds: float
    """Seconds this seed spent waiting for the provider to answer again."""
    turns_completed: int
    turns_total: int
    probe_repeats: int
    summarizer_calls: int
    summarizer_failures: int
    groups_kept_uncovered: int
    """Tool groups the recall record never mentioned, which the strategy kept rather than dropped.

    The cost of a partial record, in the one unit that says how partial it was. A row carrying
    this spent tokens it should not have had to spend and lost nothing, which is the trade
    ``tool_summary_anchored`` makes on purpose; the alternative it declined was deleting tool
    results nothing had preserved and reporting that as compaction damage in the accuracy
    columns, where no reader could trace it back.

    Stored as a number beside the ``UNCOVERED:<n>`` flag on ``strategy_notes`` because the two
    are read for different things. The flag says a row is affected; this can be meaned across
    the seeds of a cell, which is what answers how much the coverage check is holding back.

    Zero for every strategy that keeps no such count, and zero on a record written before
    schema 5 -- see :data:`SCHEMA_VERSION` for why that zero is a measurement rather than a gap.
    """
    fallbacks_after_record: int | None
    """Compaction passes where a record existed and the strategy fell back anyway.

    The other way ``tool_summary_anchored`` stops being itself, and the quieter one: the
    fallback shortens the tool results a partial record left in the prompt, so the row keeps
    its messages and loses its values. ``strategy_notes`` carries it as the ``RECFALLBACK:<n>``
    flag; the number is here so a cell can be meaned on it and asked how often a record failed
    to free enough, rather than only whether one ever did.

    ``None`` on a record written before the counter existed, which is every readable schema
    below 5. Not zero: that path ran in those runs and incremented nothing, so the count is
    unknowable rather than known to be nought -- the probe-token case, not the retry-counter
    one. Zero from a live run means what it says, including on the four strategies that take no
    record at all.
    """
    reforced_calls: int
    """Forced calls the recall middleware made at the strategy's request, for uncovered groups.

    Layer one of the answer to ``groups_kept_uncovered`` beside ``fallbacks_after_record``: a
    record that left tool groups uncovered is asked for again while those groups are still
    whole, and the ask repeats only while each record covers at least one of them.
    ``strategy_notes`` carries it as ``REFORCED:<n>``; the number is here so a cell can be
    meaned on it. Read it with the field below: this without that is the re-force clearing
    the shortfall, this with that is the re-force failing and the preservation standing in.

    Zero for every strategy that takes no record, and zero on a record written before schema
    13 -- see :data:`SCHEMA_VERSION` for why that zero is a measurement rather than a gap.
    """
    groups_preserved_uncovered: int
    """Uncovered tool groups the strategy preserved for good, as the conversation last stood.

    Layer two. Non-zero says asking stopped helping -- the re-forced record covered none of
    these, or none came -- so the strategy holds them out of the fallback's reach for the rest
    of the run, counted against the ceiling in full. That is what stops the fallback shortening
    them, and it is also a floor the prompt cannot go under, so this beside ``disqualified`` is
    the row failing loudly where a schema 12 row's fallback could take the group's values
    quietly. ``strategy_notes`` carries it as ``PRESERVED:<n>``; the number is here so a cell
    can be meaned on it.

    Zero for every strategy that keeps no such count, and zero on a record written before
    schema 13, on the reading the field above gives.

    It is not by itself a guarantee that nothing was lost: layer two protects only what is in
    front of the record, and until schema 15 the fallback could still shorten a group after it.
    See the field below.
    """
    fallbacks_held_after_record: int
    """Post-record fallback passes that ran with every tool group no record covers held back.

    The rule schema 15 adds: behind a record the fallback may not shorten or shed a tool group
    no record covers, wherever it sits, so it may remove narration and nothing else -- and the
    row keeps every fact or reads ``DQ``. Counts attempts, where ``fallbacks_after_record``
    counts effects: non-zero says the fallback was needed and held back, zero on a record row
    says it never had to act. ``strategy_notes`` carries it as ``RECHELD:<n>``; the number is
    here so a cell can be meaned on it.

    Zero for every strategy that keeps no such count, and zero on a record written before
    schema 15 -- see :data:`SCHEMA_VERSION` for why that zero is a measurement rather than a gap.
    """
    records_in_conversation: int | None
    """Recall records the conversation ended up carrying, at the strategy's highest reading.

    The price of letting the size trigger ask more than once. Every record is preserved -- it
    may be neither shortened nor dropped, by this strategy or by the fallback behind it -- and
    nothing merges them, so each one raises a floor under the prompt that no later pass can
    lower. A cell whose mean here is above one is a cell where part of what the money columns
    show is that floor rather than the workload, and no other column says so: the message count
    keeps rising and the compaction keeps looking as though it fired.

    ``strategy_notes`` carries it as ``RECORDS:<n>``; the number is here so a cell can be meaned
    on it, which is what separates "one seed's model volunteered twice" from "this cell
    accumulates records".

    ``None`` on a record written before schema 6, and not 1. Those runs could take at most one
    record, but whether they took it depended on whether the model ever complied, and nobody
    stored the answer as a number -- see :data:`SCHEMA_VERSION`. Zero from a live run means what
    it says, including on the four strategies that take no record at all.
    """
    user_compactions: int
    """Passes where ``user_summary_anchored`` replaced a band of user turns with a summary.

    The count that says whether that row measured its own design or the uncompacted control
    under another name: zero means the trigger never fired, or fired with nothing between the
    anchors. It is also how the cost of the design is read, because each pass re-bills the
    prompt from its own edit to the end -- one pass and seven passes over the same band are the
    same reduction bought at very different prices, and no other column separates them.

    Stored beside the ``USERCOMPACT:<n>`` flag on ``strategy_notes`` for the reason
    ``groups_kept_uncovered`` is: the flag says a row is affected, the number can be meaned
    across a cell's seeds.

    Zero for every strategy that keeps no such count, and zero on a record written before
    schema 9 -- see :data:`SCHEMA_VERSION` for why that zero is a measurement and not a gap.
    """
    user_messages_replaced: int
    """User turns the most recent such compaction superseded.

    The state as it stands rather than a running total, which is the rule
    ``groups_kept_uncovered`` follows and for the same reason: in its default mode the strategy
    recompacts its own earlier summary, so a total would count a turn once when it was first
    replaced and again inside every summary of the summary. What this answers is how much of
    the conversation the newest surviving summary is standing in for, which is what the
    prompt's size is made of; in the boundary modes the older summaries stand for the rest, and
    ``user_summaries_in_conversation`` says how many of them there are.

    Zero for every strategy that keeps no such count, and zero on a record written before
    schema 9, on the same reading as the field above.
    """
    user_summaries_in_conversation: int | None
    """Summaries ``user_summary_anchored`` left standing in the conversation, as it last stood.

    The boundary mode's floor, in messages. Every standing summary there is preserved and
    nothing but a fold merges them, so in that mode this is one per pass and each is a floor
    under the prompt that no later pass can lower -- ``records_in_conversation``'s reading, one
    row along, and reported for the same reason: no other column says the unshrinkable part has
    grown. One in the recompacting mode after any pass, which is the bound that mode buys.
    ``strategy_notes`` carries it as ``USERSUMMARIES:<n>``; the number is here so a cell can be
    meaned on it.

    ``None`` on a record written before schema 11, and neither one nor zero. Those runs
    recompacted, so they carried one standing summary if they ever compacted and none if not --
    but which is a deduction from ``user_compactions`` rather than a number anybody took, and
    this module stores what was measured. See :data:`SCHEMA_VERSION`.
    """
    user_summary_tokens: int | None
    """Tokens those standing summaries occupied, at the same reading: the floor, in tokens.

    ``strategy_notes`` carries it as ``USERSUMMTOKENS:<n>``. ``None`` on a record written before
    schema 11, where it is not recoverable from the record at all.
    """
    user_folds: int
    """Passes where ``user_summary_anchored`` collapsed every standing summary into one.

    The fold mode's cost in the unit it is paid in: each fold re-bills the prompt from the oldest
    summary's position, which is very nearly the whole of it, and lowers the floor the two fields
    above report. ``strategy_notes`` carries it as ``USERFOLD:<n>``. Zero for every strategy
    that keeps no such count, and zero on a record written before schema 11 on the
    ``user_compactions`` reading: no strategy those runs could select could fold, so zero is
    what they did.
    """
    records_merged: int
    """Passes where ``tool_and_user_summary_anchored``'s last-resort chain merged its records.

    Step a of the chain: the prompt was over the input budget after both halves had run, the
    conversation held at least two records, and the merged record came back smaller than they
    were. This and the six fields after it say how far down that chain a row went, which no
    other column can: a row that fitted after merging and a row that needed its fallback read
    the same everywhere else until one of them reads ``DQ``. ``strategy_notes`` carries each as
    its flag -- ``RECMERGE``, ``RECMERGEREJ``, ``USERMERGE``, ``USERMERGEREJ``, ``RECHARDER``,
    ``RECHARDERREJ`` and ``LASTFALLBACK``.

    Zero for every strategy that keeps no such count, and zero on a record written before schema
    16 -- see :data:`SCHEMA_VERSION` for why that zero is a measurement rather than a gap.
    """
    record_merges_rejected: int
    """Record merges the chain discarded because they came back no smaller than the records."""
    user_summaries_merged: int
    """Passes where the chain folded the standing user summaries into one: step b."""
    user_merges_rejected: int
    """User-summary folds the chain discarded because they came back no smaller."""
    record_rewrites: int
    """Harder rewrites of the record the chain tried, kept or not: step c, per attempt."""
    record_rewrites_rejected: int
    """Of those, the rewrites discarded because they came back no smaller."""
    record_rewrites_skipped: int
    """Rewrite attempts not made because the same record had already been refused at them.

    A refused rewrite is not asked for again, nor any milder one, until the record changes.
    ``strategy_notes`` carries it as ``RECHARDERSKIP:<n>``. Zero for every strategy that keeps no
    such count, and zero on a record written before schema 18 -- see :data:`SCHEMA_VERSION` for
    why that zero is a measurement rather than a gap, and for how 18 moved the two counts above.
    """
    last_resort_fallbacks: int
    """Passes on which the chain reached its fallback, step d, with everything above it tried."""
    user_passes_waited: int
    """Passes where ``tool_and_user_summary_anchored`` held its user half back for a due record.

    The composed row's user half waits, a bounded number of model responses, while the record
    half has tool work a record is due for, because the record half compacts in two steps and
    the user half in one. ``strategy_notes`` carries it as ``USERWAIT:<n>``. Zero for every
    strategy that keeps no such count, and zero on a record written before schema 17 -- see
    :data:`SCHEMA_VERSION` for why that zero is a measurement rather than a gap.
    """
    strategy_notes: tuple[str, ...]
    dropped_options: tuple[str, ...]
    answer: str
    long_input_tokens: int = 0
    """Input tokens of the calls billed at the long-context rates, the probes' included.

    Zero on a cell with no long-context tier, and on a record written before schema 21: every
    call of those runs was billed at the one rate, which is what zero says. The live path always
    sets these, so a tiered cell never reads its default.
    """
    long_cached_tokens: int = 0
    long_output_tokens: int = 0
    probe_long_input_tokens: int = 0
    """The same three counts for the probe phase alone, so the seeding half can be priced apart."""
    probe_long_cached_tokens: int = 0
    probe_long_output_tokens: int = 0
    error: str | None = None
    schema: int = SCHEMA_VERSION

    @property
    def correctness(self) -> float:
        """Mean correctness over this seed's probe repeats: the seed's ``acc1``."""
        return fmean(self.correctness_samples) if self.correctness_samples else 0.0

    @property
    def combined(self) -> float:
        """Mean over this seed's combined attempts: the seed's ``acc2``.

        Over the attempts the seed actually made, so a record written when the combined
        question was asked once reads as that one answer rather than as a third of three.
        """
        return fmean(self.combined_samples) if self.combined_samples else 0.0

    @property
    def hit_rate(self) -> float | None:
        """Share of input tokens served from the provider's cache, None when nothing was billed.

        Over the whole run, seeding and probes together: the ``run hit%`` column. What
        compaction did to the cache is :attr:`seeding_hit_rate`; this mixes it with the
        instrument's own draw, and is kept because every earlier write-up quotes it.
        """
        return self.cached_tokens / self.input_tokens if self.input_tokens > 0 else None

    @property
    def seeding_input_tokens(self) -> int | None:
        """Input tokens the conversation billed, the probes taken out.

        Derived rather than stored, on the rule :attr:`input_cost` follows: the whole less the
        probes, so the two halves add back to ``input_tokens`` whatever else the run did, and
        a summarizer's calls or a probe that failed before it answered land in the seeding
        half -- the direction that cannot flatter a strategy.

        Returns:
            The count, or None when the phases were never counted apart.
        """
        if self.probe_input_tokens is None:
            return None
        return max(self.input_tokens - self.probe_input_tokens, 0)

    @property
    def seeding_cached_tokens(self) -> int | None:
        """How many of those the provider served from cache, or None before version 4."""
        if self.probe_cached_tokens is None:
            return None
        return max(self.cached_tokens - self.probe_cached_tokens, 0)

    @property
    def seeding_hit_rate(self) -> float | None:
        """Share of the conversation's input tokens served from cache: the ``seed hit%`` column.

        The number that says what compaction did to the cache, because a deployed agent has
        no probe phase: it continues the conversation rather than being interrogated twelve
        times from a frozen copy of it. Until it was split out, the whole-run figure carried
        the instrument's draw, and several archived rows read ten points high or low on it
        for no reason the strategy supplied.

        Returns:
            The share, or None when the phases were never counted apart or nothing was billed.
        """
        billed, cached = self.seeding_input_tokens, self.seeding_cached_tokens
        if billed is None or cached is None or billed <= 0:
            return None
        return cached / billed

    @property
    def probe_hit_rate(self) -> float | None:
        """Share of the probe phase's input tokens served from cache: the ``probe hit%`` column.

        The instrument. Every probe re-sends the snapshot from a restored copy, so this says
        whether the provider served that snapshot to a prompt it had seen before, and on the
        archived luna cells it takes one of two values. Kept beside the seeding half so a
        reader can see which half a whole-run figure was made of, never quoted for compaction.

        Returns:
            The share, or None when the phases were never counted apart or nothing was billed.
        """
        if (
            self.probe_input_tokens is None
            or self.probe_cached_tokens is None
            or self.probe_input_tokens <= 0
        ):
            return None
        return self.probe_cached_tokens / self.probe_input_tokens

    @property
    def probe_hit_samples(self) -> tuple[float | None, ...] | None:
        """Each probe's own share served from cache, in the order asked.

        The observation the totals could only infer: on a 33.3% probe half, eight of these
        read 0 and the last four read about 1, or they do not, and either is a finding.

        Returns:
            One share per probe, None for a probe that billed nothing; or None on a record
            written before the per-probe counts existed.
        """
        if self.probe_input_samples is None or self.probe_cached_samples is None:
            return None
        return tuple(
            cached / billed if billed > 0 else None
            for billed, cached in zip(
                self.probe_input_samples, self.probe_cached_samples, strict=True
            )
        )

    @property
    def input_cost(self) -> float:
        """What this seed's prompt side cost: uncached plus cached, with output left out.

        Derived rather than stored, so no record already on disk is missing it and the schema
        does not move: the tokens and the rates are both here, and the arithmetic is the same
        one ``cost`` uses for its input half.
        """
        return self.cell.pricing.tiered_input_cost(
            self.input_tokens,
            self.cached_tokens,
            long_input_tokens=self.long_input_tokens,
            long_cached_tokens=self.long_cached_tokens,
        )

    @property
    def probe_cost(self) -> float | None:
        """What the probing cost: the instrument, and the half a deployed agent never pays.

        Returns:
            The cost, or None when this record was written before the phases were counted
            apart, where no honest number exists to return.
        """
        if (
            self.probe_input_tokens is None
            or self.probe_cached_tokens is None
            or self.probe_output_tokens is None
        ):
            return None
        return self.cell.pricing.tiered_cost(
            self.probe_input_tokens,
            self.probe_cached_tokens,
            self.probe_output_tokens,
            long_input_tokens=self.probe_long_input_tokens,
            long_cached_tokens=self.probe_long_cached_tokens,
            long_output_tokens=self.probe_long_output_tokens,
        )

    @property
    def seeding_cost(self) -> float | None:
        """What the conversation cost: the workload, and the number a strategy is judged on.

        Taken as the whole seed less the probing rather than summed over the seeding calls, so
        that the two halves add back to ``cost`` exactly whatever else the run did. Anything
        that is neither an answered probe nor a seeding turn -- the summarizer's own calls, and
        a probe that failed before it produced an outcome -- therefore lands here. The
        summarizer belongs here on its merits: it runs while the conversation is being had, and
        a deployed agent pays for it.

        Returns:
            The cost, or None when the phases were never counted apart.
        """
        probing = self.probe_cost
        return None if probing is None else self.cost - probing

    @property
    def seeding_input_cost(self) -> float | None:
        """What the conversation's prompt side cost, output and the probes both left out.

        The quiet reading of the workload, for the reason :attr:`input_cost` is the quiet
        reading of the whole seed: output is priced many times a cache read here, so a reply
        the model ran long on moves a total further than compaction does.

        Returns:
            The cost, or None when the phases were never counted apart.
        """
        if self.probe_input_tokens is None or self.probe_cached_tokens is None:
            return None
        return self.cell.pricing.tiered_input_cost(
            max(self.input_tokens - self.probe_input_tokens, 0),
            max(self.cached_tokens - self.probe_cached_tokens, 0),
            long_input_tokens=max(self.long_input_tokens - self.probe_long_input_tokens, 0),
            long_cached_tokens=max(self.long_cached_tokens - self.probe_long_cached_tokens, 0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping of this record."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SeedRecord:
        """Rebuild a record from one parsed line.

        Args:
            data: The parsed JSON object.

        Returns:
            The record.

        Raises:
            ValueError: If the record was written by a different schema version, or is missing
                fields this one requires.
        """
        version = data.get("schema")
        if version not in _READABLE_SCHEMAS:
            readable = ", ".join(str(number) for number in sorted(_READABLE_SCHEMAS))
            raise ValueError(
                f"schema {version!r}, but this reader understands {readable}. Records from "
                "another version describe a different measurement and must not be averaged "
                "with these."
            )
        known = {field.name for field in fields(cls)} - {"cell"}
        values = {key: value for key, value in data.items() if key in known}
        for name in _TUPLE_FIELDS:
            values[name] = tuple(values.get(name) or ())
        # A version 2 record ran under code that had no connection retry at all: a dropped call
        # failed its turn on the spot. So nothing was re-sent and nothing was waited, and zero
        # is what that run did rather than a number this reader could not find. Left as a
        # required field with no default, so the live path cannot forget to record it and
        # quietly inherit the same zeroes for a run that could have re-sent.
        values.setdefault("connection_retries", 0)
        values.setdefault("connection_seconds", 0.0)
        # None, not zero, and for the opposite reason to the two above. Those record something
        # the run could not do; these record something the run did and did not count, and every
        # one of those runs probed twelve times. Zero here would hand a reader the exact
        # correction this field exists to make, already applied, and wrong.
        for name in ("probe_input_tokens", "probe_cached_tokens", "probe_output_tokens"):
            values.setdefault(name, None)
        # None on the same reading, one schema along, and re-tupled by hand rather than through
        # _TUPLE_FIELDS, whose rule turns an absent field into an empty tuple. Empty would say
        # the run asked no probes; every run before schema 12 asked twelve and wrote down only
        # their total, and the total must not be divided among them here -- see SCHEMA_VERSION.
        for name in ("probe_input_samples", "probe_cached_samples"):
            samples = values.get(name)
            values[name] = None if samples is None else tuple(samples)
        # Zero, and for the connection-retry reason rather than the probe-token one. A record
        # written before schema 5 ran under a strategy that dropped every tool group in front
        # of the record without checking what the record covered, so no group was kept for want
        # of coverage: zero groups is what that run did, not a number this reader could not
        # find. Required with no default on the class for the same reason as the retry
        # counters, so a live run cannot silently inherit the same zero.
        values.setdefault("groups_kept_uncovered", 0)
        # None, and for the probe-token reason rather than the one directly above. Every run
        # since this strategy existed could fall back behind a record that had not freed
        # enough; none of them counted it, so what an older record did on that path cannot be
        # recovered from the record. Zero would say the row stayed the strategy it is named
        # for, which is the exact claim the counter was added because nobody could make.
        values.setdefault("fallbacks_after_record", None)
        # Zero, on the ``groups_kept_uncovered`` reading. A record written before schema 13 ran
        # under a strategy that could neither ask for another record on a shortfall nor hold
        # what no record covered, so no call was pinned for one and no group was preserved:
        # zero is what those runs did, not a number this reader could not find. Required with
        # no default on the class for the same reason as the counters above, so a live run
        # cannot inherit the zero by forgetting to record it.
        values.setdefault("reforced_calls", 0)
        values.setdefault("groups_preserved_uncovered", 0)
        # Zero, on the same reading one schema along: a record written before schema 15 ran
        # under a fallback that nothing held back from an unrecorded tool group, so no pass was
        # held -- zero is what those runs did. Required with no default on the class, as above.
        values.setdefault("fallbacks_held_after_record", 0)
        # None again, and for a third reason worth stating apart from the two above. A record
        # written before schema 6 comes from a run whose size trigger fired once per
        # conversation, so it took one record or none -- but which of the two is on the record
        # only as a flag, and a count inferred from a flag string is not a count anybody took.
        values.setdefault("records_in_conversation", None)
        # Zero, and for the ``groups_kept_uncovered`` reason rather than the probe-token one. No
        # strategy a record written before schema 9 could select was allowed to touch a user
        # turn, so no run of that era replaced one: zero is what those runs did, not a number
        # this reader could not find. Required with no default on the class, so a live run
        # cannot inherit the same zero by forgetting to record it.
        values.setdefault("user_compactions", 0)
        values.setdefault("user_messages_replaced", 0)
        # Zero on the same reading, one schema along: no strategy a record written before
        # schema 11 could select could fold.
        values.setdefault("user_folds", 0)
        # Zero on the ``groups_kept_uncovered`` reading, schema 16 along: the composed row had no
        # last-resort chain before it, so nothing was merged, rewritten or run last.
        for name in (
            "records_merged",
            "record_merges_rejected",
            "user_summaries_merged",
            "user_merges_rejected",
            "record_rewrites",
            "record_rewrites_rejected",
            "last_resort_fallbacks",
        ):
            values.setdefault(name, 0)
        # Zero on the same reading, schema 17 along: the composed row's user half never waited
        # before it, so no pass was held.
        values.setdefault("user_passes_waited", 0)
        # Zero on the same reading, schema 18 along: the chain never skipped a rewrite before it.
        values.setdefault("record_rewrites_skipped", 0)
        # None, on the probe-token reading. A record written before schema 11 carried one
        # standing summary or none, and which is a deduction from ``user_compactions`` rather
        # than a number anybody took; the tokens are not recoverable at all.
        values.setdefault("user_summaries_in_conversation", None)
        values.setdefault("user_summary_tokens", None)
        try:
            return cls(cell=CellParams.from_dict(data["cell"]), **values)
        except (KeyError, TypeError) as error:
            raise ValueError(f"record is not readable: {error}") from error


def append_seed_record(path: Path, record: SeedRecord) -> None:
    """Append one seed's record to a JSON Lines file, closing the file again immediately.

    Opened and closed per record rather than held open across the cell. A handle held open
    buffers, and the record sitting in that buffer is exactly the one an interruption takes --
    which is the whole failure this file exists to prevent. Appending rather than truncating is
    load-bearing for the same reason: a run resumed after a crash has to extend the file, and
    two cells written to one path have to both survive.

    Args:
        path: Destination file. Parent directories are created as needed.
        record: The seed to append.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def read_seed_records(path: Path) -> tuple[SeedRecord, ...]:
    """Read every record in a JSON Lines file.

    Args:
        path: The file to read.

    Returns:
        The records, in the order they were written.

    Raises:
        ValueError: If any line is not a record this reader understands. Refused rather than
            skipped: a line that cannot be read is a seed that was paid for, and dropping it
            silently would leave a mean over fewer seeds than the table claims. A run killed
            mid-write can leave the last line half-finished, and that one line is safe to
            delete by hand.
    """
    records: list[SeedRecord] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(SeedRecord.from_dict(json.loads(line)))
        except (ValueError, TypeError) as error:
            raise ValueError(f"{path}, line {number}: {error}") from error
    return tuple(records)


def group_by_cell(records: Sequence[SeedRecord]) -> list[tuple[CellParams, tuple[SeedRecord, ...]]]:
    """Group records into the cells they were measured in.

    In first-appearance order rather than sorted, so a file written by a sweep reads back in
    the order the sweep ran.

    Args:
        records: Records from one or more cells.

    Returns:
        One entry per cell: its parameters, taken from the first record that named it, and
        every record belonging to it.
    """
    grouped: dict[tuple[Any, ...], list[SeedRecord]] = {}
    params: dict[tuple[Any, ...], CellParams] = {}
    for record in records:
        key = record.cell.key
        params.setdefault(key, record.cell)
        grouped.setdefault(key, []).append(record)
    return [(params[key], tuple(entries)) for key, entries in grouped.items()]
