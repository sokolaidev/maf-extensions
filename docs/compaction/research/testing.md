# How the benchmark is tested

> **Record.** What the benchmark's tests defend and why they are shaped as they are, written during development; the counts are the suite's at the time of writing.

474 test functions, 588 cases after parametrisation, about 13,700 lines of test against 16,600 lines of source. Everything runs offline in under a minute. This file says what is being defended and why the tests are shaped the way they are, because most of them exist to prevent a specific failure that already happened and cost either a paid-for run or a wrong number in a write-up.

```bash
uv run pytest -q packages/maf-cachebench/tests
```

> `poe test-cachebench`, declared in `python/packages/lab`, runs the same suite from that > directory with coverage on, and the whole suite passes there too. One did not until > `test_narration_probe_declares_every_flag_it_reads` was changed to load > `samples/probe_narration.py` by file path: `samples` only resolves by name when the package > directory is on the path, which it is from here and was not from there.

| file | tests | what it defends |
| --- | ---: | --- |
| `tests/test_live.py` | 214 | the live runner: the agent pipeline, retries, probing, scoring, the table, the records file |
| `tests/test_cachebench.py` | 51 | the replay harness, the prefix oracle, providers, cost |
| `tests/test_recall.py` | 23 | the recall scenario and the scorer |
| `tests/test_fill.py` | 14 | the fill solver and the payload sizing |
| `tests/test_advisor.py` | 12 | the advisor's arithmetic |
| `tests/test_summary.py` | 7 | the summary CLI |
| `packages/maf-compaction/tests/test_toolsummary.py` | 60 | the record strategy, its tool and its middleware |
| `packages/maf-compaction/tests/test_anchored.py` | 28 | the anchored family |
| `packages/maf-compaction/tests/test_composed.py` | 23 | the composed row: one line for both halves, the order, what the starvation counter may and may not count, and that a fold in the user half leaves the record where it was |
| `packages/maf-compaction/tests/test_usersummary.py` | 38 | the user-turn strategy: the band, the hysteresis that bounds its passes, and the three summary modes — recompaction of its own output, the boundary that is never re-read and keeps the prefix byte-identical, and the fold that bounds the accumulation |
| `packages/maf-compaction/tests/test_packaging.py` | 4 | that `compaction/` never imports the lab |

## The split, and why `tests/compaction/` is separate

`maf_cachebench/compaction/` holds the five strategies written here, the recall tool and the gate that keeps it inert when unasked. It is meant to be **lifted out whole** into a repository of its own, so nothing in it may import from the benchmark that measures it, and its tests travel with it.

That boundary is checked rather than remembered, by `packages/maf-compaction/tests/test_packaging.py`. It reads every module under the subpackage with `ast`, resolves relative imports the way `importlib` does, and fails if any of them names the lab. Reading the source rather than searching the text is the whole point:

- a bare `from .. import LiveOutcome` names nothing a text search for `maf_cachebench` would find;
- an import inside a function body is still a dependency, it just fails later;
- an import under `if TYPE_CHECKING:` never executes, so no runtime check would ever see it.

`test_the_check_catches_every_way_across` parametrises all seven forms and asserts the walker catches each; `test_the_check_permits_what_the_subpackage_is_allowed` asserts it permits the framework, the private `agent_framework._compaction`, siblings and the subpackage's own root. A walker that misses a form is worth exactly as much as no walker.

`test_the_strategies_tests_never_import_the_lab_either` applies the same rule to the tests, for the same reason one step removed: the strategies would move out and arrive untested, which is the state that makes the first change to them after the move unverifiable.

`_strategies.py` deliberately stays in the lab and is tested from `tests/test_live.py`. It is the `--strategies` registry, which is benchmark configuration rather than a strategy.

## Naming, and what a docstring is for

Test names are long, descriptive sentences and the docstring names the regression prevented, with the measurement that motivated it where there is one:

```
test_a_prompt_over_the_tried_limit_disqualifies_the_run
test_the_ranking_is_on_the_workload_and_not_on_the_invoice
test_a_record_written_before_the_settings_block_reads_back_as_unknown
test_decisions_are_frozen_as_the_conversation_grows
test_a_value_that_is_only_a_substring_of_the_record_does_not_count_as_quoted
```

The convention costs nothing and buys two things. A failure names the property that broke in the pytest output, without anyone opening the file. And the docstring is where the *reason* lives, so a later reader deciding whether a test still earns its maintenance has the evidence in front of them — `test_the_help_can_actually_be_printed` records that argparse `%`-formats every help string, and that two help strings quoting percentages had made the whole CLI undiscoverable while parsing and running fine.

## The stub is composed from the real layers

`StubChatClient` in `tests/test_live.py` subclasses `FunctionInvocationLayer`, `ChatMiddlewareLayer` and `BaseChatClient` — the same composition `OpenAIChatClient` uses — so the agent's middleware pipeline, its history persistence and the compaction hook all execute for real and only the network call is replaced.

That is not fastidiousness. Every question worth asking here is about *ordering* between those layers, and a hand-rolled mock that skipped them would answer none of them: that a middleware cannot see the history before `call_next()`, that a turn's `tool_choice` reaches only its first call, that a tool passed through per-call options is never executed because `FunctionInvocationLayer` builds its tool map first. Each of those cost a real run to discover.

The stub can be told to obey `tool_choice` like a real model, to report a `finish_reason`, to answer with a tool call on named turns, and to give replies with body to them — because a reply becomes history and is re-sent on every later turn, so a test about how large a conversation gets cannot use a six-word one.

## Guard tests that pin whole defect classes

Four tests exist not to check a behaviour but to make a *category* of omission fail at the command line rather than on a paid call. Each was written after the category bit.

**Every argument the runner reads must be declared.** `test_every_argument_the_runner_reads_is_defined` parses a minimal command line and asserts the namespace carries all forty attributes the run function reads. A flag referenced but never declared raises `AttributeError` only once a live run is under way; that happened twice, on an `add_argument` edit that silently failed to apply while the code using it did not.

**Every boolean flag must be sorted into a bucket.** `test_every_boolean_flag_that_changes_a_run_is_recorded_somewhere` collects every `store_true` flag from the parser and asserts the set equals exactly three named buckets — flags that change the conversation (recorded in `CellParams.workload`), flags that are a strategy setting (recorded in `StrategySettings`), and flags that change nothing measured. A new flag fails here until somebody sorts it deliberately. The failure it prevents is silent and late: a flag that changes the conversation and is left out of the workload block lets two different workloads merge into one cell months later. It also asserts `len(fields(WorkloadSettings)) == len(changes_the_conversation)`, so a bucket entry with no recorded field fails too.

**Every `StrategyOptions` field must reach the recorded settings.** `test_every_strategy_option_that_changes_behaviour_reaches_the_recorded_settings` diffs `fields(StrategyOptions)` against `fields(StrategySettings)`, allowing exactly two documented exceptions — the window, already on the cell, and the tokenizer and summarizer, which are objects recorded by name. A knob that reaches a constructor without reaching the file moves rows while the record says nothing, and two cells that differ in it merge. That is what happened to the whole settings block before it existed.

**Every declared column must render.** `test_the_table_renders_every_column_it_declares` and `test_narration_probe_declares_every_flag_it_reads` do the same job for output: a column in the header with nothing under it, or a sample script reading a flag it never declared, both pass every other check and die on first use.

**A mean must not hide a two-valued row.** `test_a_row_whose_seeds_disagree_on_facts_gets_a_per_seed_line` renders five seeds of the record row, four holding every fact and one short, and asserts the `per-seed facts` block under the table carries that row and only that row; `test_rows_whose_seeds_agree_on_facts_print_no_per_seed_block` asserts a cell where every row held on every seed prints no such block at all. Run 48's recompact arm printed the record row at 50/53 -- four seeds at 53 and one at 37 -- and the column read as uniform mild loss.

Two more in the same spirit pin a *value* rather than a shape. `test_the_default_thresholds_leave_a_whole_turn_for_the_record_to_arrive_in` asserts `(DEFAULT_TRIGGER_FRACTION, DEFAULT_FALLBACK_FRACTION) == (0.6, 0.9)` with the note "runs 26-39 were taken at 0.6/0.9", and separately that the middleware and the strategy read the *same* constant, so the ask and the wait cannot be configured apart. A default that moves silently makes the next run a new variant rather than a comparison — which is exactly what the previous version of that test permitted, having been rewritten to assert 0.8 on an argument nobody had measured. `test_context_window_matches_the_framework_tool_retention_default` pins the shipped row to the framework's own `keep_last_tool_call_groups`, so the row standing for "what a caller gets" cannot quietly become harsher than what a caller gets.

## Schema round-trips, and the rule about old records

The `.jsonl` records in `runs/` are the archive this project's write-ups are read from. They are paid-for measurements that cannot be re-taken for free, so the reader has to keep opening them across schema changes — and it has to refuse the ones it can no longer interpret rather than silently reinterpreting them.

- `test_records_from_another_schema_are_refused` parametrises over schema versions. Version 1 predates the seed/snapshot/probe rebuild, when survival was scored against a prompt the closing answers had written — the same strategy read 53/53 on one run and 18/53 on another — so averaging across that line would be a mean over two different questions. Version 1 is refused; version 2 is not.
- `test_the_recorded_cells_on_disk_still_read` reads **every** `runs/*.jsonl` and asserts each parses and keeps its correctness samples. Not a fixture: these are the actual records behind `RESULTS.md` and the reports, and the reader refuses a bad line rather than skipping it, so a change that makes them unreadable makes every recorded cell unrecoverable at once. It also asserts the glob found something, so it cannot pass by reading nothing.
- `test_the_raw_archive_still_reads_and_still_refuses_the_records_it_should` holds two opposite claims about one directory: the `oldprompt-*` cells must keep opening, and the `void-*` ones must keep raising `ValueError` on schema 1.

**The rule for a field an old record does not carry is that it reads back as *unknown*, never as the current default.** A record written before the settings block must produce `record.cell.settings is None`, not a `StrategySettings` full of today's values — because a cell labelled with settings it never ran under merges with cells that did, and the merge is invisible. The tests say so by name:

```
test_a_record_written_before_the_settings_block_reads_back_as_unknown
test_a_record_written_before_the_workload_block_reads_back_as_unknown
test_the_post_record_fallback_count_survives_the_file_and_an_older_record_reads_as_unknown
test_records_without_settings_load_and_are_reported_as_not_settings_comparable
test_a_record_written_before_repeats_existed_reports_no_count_rather_than_one
```

There is one deliberate exception, and it is argued rather than assumed. `test_a_record_written_before_the_connection_counters_still_reads` asserts those two fields read back as **zero**, because the code that wrote them failed the turn on a dropped connection instead of re-sending it — so no call was re-sent and none was waited on, and zero is what that run did. It then asserts the rendered table carries no `RECONNECTED` flag, so a run that never reconnected is not reported as having.

## Identity of the rendered output

`--from-jsonl` rebuilding a cell has to reproduce the table that cell printed, exactly, or recovering a dead run is a second measurement rather than the one that was paid for.

- `test_the_table_rebuilt_from_the_file_matches_the_live_one` runs a cell live, captures the table, rebuilds it from the records file and asserts the two strings are equal — then does it again under `--min-correctness 1.5`, so the *split* between ranked and unranked rows is part of what has to match. Row order is part of that identity: rows of equal cost are ordered by name precisely because the live path builds them in the order the strategies ran while the file is grouped in the order the records were written.
- `test_aggregation_survives_the_round_trip_through_the_file` is the stronger version, comparing the aggregated cell field for field rather than the rendered text, because rendering rounds and a difference in the fourth decimal of a cost — or a tuple that came back as a list — is invisible in a table and is exactly what makes two paths disagree later.
- `test_a_single_cell_renders_exactly_as_it_did_before_the_comparison_existed` pins the one-cell output against what it printed before the cross-cell section was added, so a new section cannot appear under one path and not the other on the one question the file exists to answer.
- On the strategy side, `test_decisions_are_frozen_as_the_conversation_grows` compacts an eight-turn conversation and a ten-turn one and asserts the shared prefix renders **byte-identically, message by message**. That is the whole design of the anchored family — a strict-prefix cache is worth nothing to a strategy that re-decides an old group — so it is asserted rather than described.

## Anti-vacuity, which is the practice this suite actually turns on

A passing test that cannot fail is worse than no test: it occupies the slot where the real one would go. Three habits are in force, each adopted after a vacuous test shipped a defect.

**A fixture must render values the way the live harness does.** The coverage check in `tool_summary_anchored` decides whether the model's record quotes a tool group's values. The live tool renders them as `code_1=TL-BA44A9`. The fixtures rendered bare `AB-123456`. So the check shipped comparing compound `code_1=…` tokens against bare values, **a record quoting every value scored 0/3, and 443 tests said nothing** — the check was measuring whether the model had copied the benchmark's own label format. Both sides are fixed now, and the fix is structural rather than a one-off correction:

- In the lab's own tests (`tests/test_live.py`), the fixture calls the real `render_codes()`, with a comment saying why: a fixture cannot then drift into a shape the benchmark never emits.
- In `tests/compaction/`, which may not import the lab, `_render_values()` replicates the shape by hand, and its docstring carries the whole account of what the drift cost.
- `_covering_record()` writes the values **bare**, without the `code_N=` labels, because that is what "quote verbatim any value that cannot be reconstructed" actually produces. A record has to cover a labelled result while writing plain values, or the check is measuring formatting compliance rather than preservation.

**A fixture must sit where the code under test actually does something.** `_WAITING_CEILING = 22_000` in `packages/maf-compaction/tests/test_toolsummary.py` is chosen from the fixture's measured size — 17,011 tokens, 17,137 with a record, 78% of the ceiling, between the 0.6 trigger and the 0.9 give-up line and close to neither. The comment on it says: *recompute it whenever a default moves, and check the margin rather than the sign*. It had been 19,000 while the thresholds were briefly 0.8/0.95; at 0.6/0.9 that puts the fixture at 90.2%, **past** the give-up line, so those sixty tests would have been measuring the fallback strategy instead of the one they name. The failure in the other direction is quieter and worse: a fixture below the trigger asserts against a strategy that returned without doing anything, and passes.

The same trap is recorded in `test_decisions_are_frozen_as_the_conversation_grows`, which used a 3,000-token ceiling where every per-result budget clamped to the 150-token floor, so both conversations trimmed to the same number whatever rule produced it — and it therefore passed against a strategy whose retention moved by a factor of six with the band's width, which is the one thing it exists to forbid.

**A fix is mutation-tested: break it deliberately and confirm the tests go red.** The 6 September repair round applied nine mutations and confirmed all nine were caught. That practice exists because a commissioned test failed it. `test_the_record_count_is_a_maximum_rather_than_a_running_total` still passed when `max(...)` was mutated to `+=`: its fixture used records that covered their groups, so the first pass dropped enough to put the conversation below the trigger, and every later pass returned at the first line of `__call__` without ever reaching the counter. The rebuilt fixture uses records that quote nothing, so nothing is dropped and all four passes reach the count — and the test now **asserts its own premise**, so it cannot go vacuous silently again:

```python
assert included_token_count(messages) > int(_TWO_RECORD_CEILING * DEFAULT_TRIGGER_FRACTION), (
    "a fixture that falls below the trigger stops reaching the counter, and this stops testing it"
)
```

The summary-mode round applied ten mutations to `_usersummary.py` and `_live.py` -- the boundary rule removed, the boundary left unmarked, the fold allowed with one summary, the fold's threshold and its deduction removed, the folded summaries left preserved, the default flipped, the floor counters not read after a replace, the flags unwired, and every mode made to read as recompacting. Two survived on the first run and both were redundancy in the code rather than blindness in the tests: the preserved mark alone already keeps a boundary out of the band, and the fold's deduction alone already refuses a one-summary fold at any share above zero. Each got a test on the one case where the removed rule is load-bearing -- a head turn another strategy preserved, and a share of zero -- and the ten are now all caught.

The reasoning-stamp round (13 September) was mutation-tested the same way: the stamp removed from `_live.py`, the test red, the stamp restored. That test (`test_the_harness_store_keeps_the_reasoning_stamp_the_replay_is_charged_for`) is the load-bearing one for every post-fix live count on a reasoning model, and its fixture asserts its own premise twice over -- the stored content must carry the stamp, and the second call's charge must equal the stamped history to the token while sitting strictly between the stripped (stamp-less) count and the raw base64 count, so no single wrong number can satisfy it by coincidence.

Two other tests assert their own premise the same way — `test_the_recorded_cells_on_disk_still_read` asserts the glob was non-empty, and `test_the_strategies_never_import_the_lab` asserts modules were found — because "found nothing, therefore passed" is the commonest way a suite quietly stops checking anything.

## What is not covered

**No live-provider tests, in CI or anywhere.** Every test here stubs the network. There are no `integration` markers in this package and no test needs a credential.

**So cost and accuracy are not tested at all, and cannot be.** Everything this project actually claims — what a strategy retains, what it costs, what the cache does — comes only from paid runs against real deployments, and lives in `runs/` with the invocation that produced it. The suite defends the *instrument*: that the flags reach the code, that the arithmetic is the arithmetic it claims, that the records round-trip, that the table renders what it declares, and that a strategy's mechanism does what its docstring says on a synthetic conversation. It cannot tell you whether a number is true, only that the tool did not lie about how it got it.

**Nor the interaction with a real provider's cache.** Hit rates, minimum cacheable sizes, TTLs and intermittent engagement are provider behaviour, measured with the probes in `samples/` and recorded in [`README.md`](../cachebench.md) and [`RESULTS.md`](results.md).

**Nor the framework's own strategies.** Their tests are upstream, in `python/packages/core/tests/core/test_compaction.py`. What is tested here is how this package *builds* them — that the `token_budget_*` variants share one ceiling, that `--budget-fraction` moves it, that `context_window` matches the harness's own retention default — not what they then do.

---

Related: [`STRATEGIES.md`](twenty-strategies.md) for what is being tested, [`README.md`](../cachebench.md) for the CLI the guard tests defend, `REPORT-2026-09-07.md` for what the paid runs found.

