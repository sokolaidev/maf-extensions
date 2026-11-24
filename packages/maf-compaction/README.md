# maf-compaction

[![PyPI](https://img.shields.io/pypi/v/maf-compaction)](https://pypi.org/project/maf-compaction/) [![Python](https://img.shields.io/pypi/pyversions/maf-compaction)](https://pypi.org/project/maf-compaction/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafCompactionExperimentalWarning`.

Compaction strategies for [Microsoft Agent Framework](https://aka.ms/AgentFramework) that are shaped around the provider's prompt cache. A prompt cache matches on an exact prefix, and every compaction rewrites history inside that prefix, so compaction breaks the cache by construction. The strategies here break it as rarely and as late as they can: they keep a fixed head and tail of the conversation verbatim, change only the band between them, and change it by rules that produce the same bytes on every later turn.

This is an independent package. It is not affiliated with or endorsed by Microsoft.

## What is in it

| Strategy | What it does |
|---|---|
| `AnchoredCompactionStrategy` | Keeps the first and last groups of the conversation whole and shortens the oldest tool results between them, by position, so the compacted prefix is stable. |
| `MinimumGainAnchoredCompactionStrategy` | The same, with a floor: a collapse that would save too little to repay the cache it invalidates is declined. |
| `ToolResultAnchoredSummarizationCompactionStrategy` | Has the model write the facts from its tool results into a *record* through a recall tool, then drops the tool groups the record covers. Ships with `make_recall_tool`, `RecallGate` and `ToolResultRecallMiddleware`, which ask for the record at the right moment. |
| `UserTurnAnchoredSummarizationCompactionStrategy` | Summarises the user's turns between a fixed head and tail with a summarizer client, in one of three modes. |
| `ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` | Runs the two summarising strategies over one conversation, record phase first, with a bounded last-resort chain that, while the prompt is over budget, pays each model-written record once, keeps records within a share of the budget by merging them in bounded batches, and rewrites. If still over budget, sheds whole unrecorded groups with a warning by default, or raises locally in strict mode. |

All of them implement the framework's `CompactionStrategy` and attach wherever the framework takes one: `create_harness_agent`, or a `CompactionProvider` on a plain `Agent`.

Last-resort shedding keeps a completed turn's final text-only reply when its text is at most 64 tokens under the configured tokenizer. Longer final replies leave an explicit marker that the preceding user request was already answered, so removing the tool exchange does not make the request look unanswered. Shedding narration between a tool result and the next tool call leaves no reply marker. Other intermediate narration and tool exchanges leave neutral markers. These rules also apply to the composition's anchored fallback and session replay. Kept replies still count against the budget; protected content that cannot fit remains an overflow rather than being silently removed.

Hosts can call `is_compaction_echo(message)` before showing or storing a model reply. It recognizes text-only assistant messages that exactly repeat a shed note or begin with `RECORD_MARKER`. For streamed output, buffer the text of one assistant message and pass the complete string before emitting it. A string carries no provenance, so pass the framework `Message` when checking stored history. Compaction excludes matching replies from the outgoing prompt, respecting explicitly preserved messages, tool calls, and package-authored notes and records.

## Install

```bash
pip install maf-compaction
```

The package depends on `agent-framework-core` alone. The summarising strategies take any framework chat client as their summarizer, so install the client package for your provider separately.

## Quickstart

Every strategy sizes its decisions with a tokenizer. The one below counts text exactly and needs `tiktoken`, which this package does not install (`pip install tiktoken`); the framework's `CharacterEstimatorTokenizer` needs nothing extra and works too, at the cost of precision.

```python
import tiktoken


class Tokenizer:
    def __init__(self, encoding: str = "o200k_base") -> None:
        self._encoding = tiktoken.get_encoding(encoding)

    def count_tokens(self, text: str) -> int:
        return len(self._encoding.encode(text))


tokenizer = Tokenizer()
CONTEXT_WINDOW = 128_000  # the model's input limit
MAX_OUTPUT = 4_000  # reserved for the reply
BUDGET = CONTEXT_WINDOW - MAX_OUTPUT
```

The anchored strategy needs nothing else:

```python
from agent_framework import create_harness_agent
from agent_framework_openai import OpenAIChatClient
from maf_compaction import AnchoredCompactionStrategy

strategy = AnchoredCompactionStrategy(
    max_input_tokens=BUDGET,
    tokenizer=tokenizer,
    keep_head_groups=3,  # task and requirements, never touched
    keep_tail_groups=4,  # the working set, never touched
    band_share=0.25,  # the oldest banded result keeps 25% of the budget
)

client = OpenAIChatClient(model_id="gpt-6-luna")
agent = create_harness_agent(
    client,
    name="assistant",
    agent_instructions="Answer from the lookups you make. Quote values exactly.",
    tools=[lookup_deployment],
    max_context_window_tokens=CONTEXT_WINDOW,
    max_output_tokens=MAX_OUTPUT,
    before_compaction_strategy=strategy,
    after_compaction_strategy=strategy,
    tokenizer=tokenizer,
)
```

The record strategy needs the recall tool registered and its middleware installed, because the model cannot be asked for a record from inside a compaction pass:

```python
from maf_compaction import (
    AnchoredCompactionStrategy,
    RecallGate,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    make_recall_tool,
)

strategy = ToolResultAnchoredSummarizationCompactionStrategy(
    max_input_tokens=BUDGET,
    tokenizer=tokenizer,
    trigger_fraction=0.6,  # ask for a record past 60% of the budget
    fallback_fraction=0.9,  # stop waiting for one past 90%
    record_target_tokens=2_000,  # match the recall tool below
    fallback=AnchoredCompactionStrategy(max_input_tokens=BUDGET, tokenizer=tokenizer),
)

gate = RecallGate()
recall_tool = make_recall_tool(gate, target_tokens=2_000)
recall = ToolResultRecallMiddleware(
    max_input_tokens=BUDGET,
    tokenizer=tokenizer,  # the same tokenizer as the strategy
    arm=gate.arm,
    disarm=gate.disarm,
    trigger_fraction=strategy.trigger_fraction,
    record_max_tokens=4_000,
    repeat_records=True,
    min_groups_before_repeat=4,  # repeat only after four new groups above the trigger
    reforce=strategy.take_reforce,
)

agent = create_harness_agent(
    client,
    name="assistant",
    agent_instructions="Answer from the lookups you make. Quote values exactly.",
    tools=[lookup_deployment, recall_tool],
    middleware=[recall],
    max_context_window_tokens=CONTEXT_WINDOW,
    max_output_tokens=MAX_OUTPUT,
    before_compaction_strategy=strategy,
    after_compaction_strategy=strategy,
    tokenizer=tokenizer,
)
```

The summarising strategies, `RecallGate` and `ToolResultRecallMiddleware` keep one conversation's decisions on the instance. This direct wiring builds the stack and agent once per session; the middleware raises if a second session reaches it. For a shared agent, use the session wiring below.

The composed strategy takes a record half and a user-turn half built the same way, the user half with `remembered_requests=2` unless it runs in the recompacting mode; `find_nested_strategy` finds the record half inside it for the middleware's `reforce`, and `record_text` reads the record the model wrote.

The size trigger asks for the first record immediately. Later size-triggered records wait for `min_groups_before_repeat` new tool groups (four by default); `max_groups_before_record` remains a separate bound that can force records below the trigger. Re-force chains retry only their original uncovered batch and cannot recruit groups arriving during an ask. Wiring the strategy's bound `take_reforce` method also shares the middleware's non-consuming `record_due` preview with the composed wait: the user half waits only for a configured trigger or an outstanding request, including after session restore. `CompactionStack` binds value observation automatically for both record-only and composed strategies, including when `reforce=None` or `take_reforce` is wrapped. Outside a stack, explicitly call `record_strategy.bind_recall(recall)` when re-forcing is disabled or wrapped. Value-free back-off and target capture remain active without re-forcing; the trigger preview and re-force feedback are wired only when `reforce` is configured. Whole-group deletion still requires the configured share of exact values. When a string result group exceeds `record_target_tokens` (2,000 by default), a partial record can instead release pipe-delimited Markdown table rows containing a distinctive value and quoted verbatim under matching column headings. Headings and uncovered rows remain, and a marker identifies the omitted rows. Bare values, changed rows, code fences, rows containing backslashes and non-string results do not qualify for partial removal. Rows repeated under the same headings anywhere in the loaded tool results remain because their source is ambiguous. Small groups keep the existing coverage rule.

Set the record strategy's `record_target_tokens` to the target passed to `make_recall_tool(target_tokens=...)`; the middleware's `record_max_tokens` is a separate response cap. Setting both targets to `None` disables partial removal and the target-based diagnostic while keeping whole-group coverage checks. `groups_held_unreachable_coverage`, available on both the record strategy and composition, counts held groups on the last record pass whose estimated required value text exceeds that target. The estimate sums standalone token counts for the cheapest `ceil(value_count * coverage_share)` distinct values, excluding labels, formatting and other groups. It is a diagnostic estimate, not proof that a tokenizer or model cannot fit a record. Partial rows are recomputed from the loaded history and records; no extra session state is required.

Records are preserved for the whole run, and a record the model writes is paid twice: its values sit in the recall call's arguments and again in the result. When the composed strategy's chain starts, its first step rewrites each such record as a written record holding the same body, with no summarizer call: the payload after the record's framing moves byte for byte, indentation and trailing whitespace included. The written record keeps the original record's identity in `additional_properties["maf_compaction_record_identity"]`, which hosts must store with the rest of the written-record metadata, so the record wait, value diagnostics and the middleware's record counts treat it as the same record; head and tail group counts take the excluded original's place rather than adding one. Reasoning belonging only to a recall call leaves with that call and its result once a later genuine user request completes the turn. Active-loop reasoning stays, and no record before later retained reasoning is replaced; the reshape replaces the eligible completed records together so it never edits a retained reasoning prefix. Text and supported media-only requests complete a turn. Synthetic recall prompts, generated summaries and tool results do not complete a turn. A record is left as it is when its span holds other calls or output, when another output answers the same call id, when the current model call carried its result in, or when its span holds more than one recall result, a result not framed the way the recall tool frames it, or a result carrying item metadata, annotations, provider state or non-text items, which result shortening also skips by default. Record merges and harder rewrites also leave results carrying metadata, provider state, non-text items or differing item text whole. The result-shortening hook does not authorize merging these records. The reshape runs only when the chain does, because each one rewrites the prompt from that record onward; it replays on stored and rebuilt original history and after session restore, first before the record phase counts records and again after any required user-summary restoration, and never writes a record twice. Replay across retained reasoning requires the complete accepted batch, unchanged source identities, content and inclusion state across the complete intervening message window, with one exception: a message between the batch's records that a pass excluded of its own accord, a tool result a record covers (`RECORD_ANCHOR_EXCLUDE_REASON`) or one the over-budget shed removed, is fingerprinted as if included, since each pass marks those afresh and a record's answer does not depend on them. A background answer and a kept edit therefore still find their batch when the next pass covers or sheds those messages differently. Reasoning between the records, and any exclusion with another reason, still has to be unchanged; with nothing excluded a batch keeps the key it always had. Replay is refused if reasoning that was already excluded after the batch becomes included again. Excluded reasoning after the batch must have durable message IDs to grant replay authority; missing or positional IDs and older certificate formats retain only the guarded content cache. Accepted record edits retain their order and the IDs of summaries and shed decisions already applied to their source windows. Replay restores those prerequisites before matching each window, including record edits between a summary and its fold or between folds and shedding. Later owned exclusions and replacement messages are staged on isolated candidate views so prerequisite summaries can be restored, including missing intermediate summaries beneath successive folds. Missing prerequisite summaries or source exclusions also require an isolated candidate when no later edits exist. A matching certificate permits the record edit; staged messages and their source exclusions are restored before committing the view, then later user edits and shedding are reapplied. Held written records are validated against their source identity or lineage, content and placement before staging. Accepted replay reuses those outputs when a store retained them but lost the originals' exclusion flags; changed or externally protected replacements cannot authorize that restoration. Saved source-member fingerprints identify which package-owned exclusions to undo when only part of a batch lost its flags, including intermediate written records. The complete window certificate still has to match. Replay validates every staged output in the affected chain in a private pass before committing it, so a later invalid placement cannot leave an earlier replacement active beside it. Source-member fingerprints identify dependent outputs; unrelated held records can keep their already-pruned source histories. Only isolated record candidates may restore a held summary's missing source exclusions, on its accepted source window and subject to the record certificate. Held summaries must have unique IDs, unchanged content, matching source links and their accepted placement before the first source, including summaries outside the record window. An invalid owning fold prevents source exclusion through its intermediate summaries, even when storage pruned them. Replay retains these refusals while later edits are staged away. Externally excluded summaries do not hide their source turns. Record selection, user-summary replay and fresh folds retain host preservation marks, and reapplying package holds does not replace their owner. Unchanged prerequisite restoration is reused until history changes. External exclusions and changed content do not authorize that restoration. Consolidated records carry their source lineage in `additional_properties["maf_compaction_record_lineage"]`, which hosts must persist with history. Cached payload matches without these source certificates remain subject to current reasoning guards. `records_reshaped` counts the records the chain reshaped.

Then, while the active records exceed `record_budget_share` of the sending budget (0.5 by default) or the prompt is still short of the chain's target, the chain merges the oldest records in non-overlapping batches, each the longest run of whole records whose request fits the summarizer's output cap, `summary_options["max_tokens"]`, and never more than `max_summary_input_tokens`. `record_request_input_tokens` gives merge and rewrite requests an input limit of their own in place of that output cap, for a summarizer whose merges compress: with `summary_options={"max_tokens": 8_192}` and `record_request_input_tokens=16_384`, a batch may hold 16k tokens and its answer at most 8k. `record_merge_max_records` caps how many records one merge or rewrite request may hold, two or more; unset, the default, a batch is bounded by its request tokens alone. A batch of many small records can fit the request yet hold more values than one answer keeps, so it is refused and split, or split before it is asked when the output cap cannot hold its required values; with the cap a batch takes the oldest records up to it, a remembered search's batch is cut to it, and pending smaller batches above it are dropped and searched again. Each harder rewrite takes the oldest eligible records that fit both limits. For several records, the strategy skips a built-in rewrite attempt if its target percentage of their combined text is too short to hold even the shortest distinct values required by the current coverage share, separated by one character each. A merge or rewrite request sent with an output cap tells the summarizer that cap: `state_record_budget=True`, the default, appends `record_answer_budget(max_tokens)` to its prompt, which says the reply is cut off at the cap and a cut-off reply is discarded, and asks for a record within `RECORD_ANSWER_BUDGET_SHARE` (0.8) of it, keeping as many values as fit rather than running past it. The provider applies `max_tokens` as a hard stop the model never sees, so without the sentence a record longer than the cap was cut off and refused at the cost of a whole request. Requests sent without a cap, including after the options are retired, are unchanged: a retry that drops the options after a failed request is sent the plain prompt. The sentence is also left off a request it would take over `max_summary_input_tokens`, which then goes out as before rather than not at all, and batches are still sized by the prompt without the sentence. Without a configured `max_tokens` the package knows no output cap, and batches are bounded by `max_summary_input_tokens` alone; set it to the summarizer's real output cap to size batches to it. The cap includes reasoning on providers that count it as output, so an input batch below that cap can still produce a truncated answer. Refused batches retain their original records; the same parent request is not asked again until its records or merge prompt change. A record whose own request exceeds the cap is passed over without searching batches from it, and a sweep renders at most 128 batch prefixes, so records that each fit alone but never two together cannot make it scan every prefix from every record; a sweep that reaches the limit stops, and the next firing continues. Single-record fit results persist across passes and session restore while the record, merge prompt and cap stand unchanged, so oversized records need not be tokenized again on every pass. If a deadline or probe limit interrupts batch expansion, the next sweep can use the pair already known to fit. An interrupted sweep resumes at its next unhandled record while the record layout, merge prompt and cap remain unchanged; a change restarts the sweep. Unfinished smaller batches retain their depth and resume before fresh batches, including after a sibling merge and session restore; changed records, prompt or cap invalidate them. With `merge_same_tool_first=True`, the default, two or more adjacent records covering the same tools (the tool calls since the nearest active record before each, excluding recall; a merged record carries what its sources covered in `additional_properties["maf_compaction_record_tools"]`) form a run of their own, and the records between such runs form one together; no batch crosses from one run into another, so two or three records of one tool's results merge into one before records of different tools merge together. Records that cover no tools are one run, as before. The search itself does not see the runs: each batch it finds is cut back to the run of its first record, so the runs share the sweep's probes, search memory and resume position, and when a sweep merges nothing within the runs it takes the first batch across them it found rather than none. A start whose two-record request does not fit is not searched over longer runs: the search goes on to the runs starting at the next record, since appending a record only adds text (`run_past_limit_factor=1.0`, the default). `run_past_limit_factor=None` searches every longer run, for a tokenizer whose count can fall as records are appended, and a larger factor searches them while a run counts at most that many times the cap. A stop is remembered with the factor that made it, and is not reused under a larger factor or `None`. What the search from each start found, its batch or how far it got with nothing fitting, is remembered with the merge prompt, cap and a digest of the records that decided it, persisted with the session and kept while the start's record stands. A later sweep takes that batch, so passing over a refused batch again costs no prefixes, or resumes the search past those records, trying only the runs a record appended since makes possible; it searches afresh if any of those records changed, or if a record was appended after a search the end of the records decided, and goes on from where the last stopped; a sweep also reads the history once, and again only after a merge changes it. A summarizer failure ends the sweep for that pass. Set `record_budget_share=None` to select one initial merge batch per pass bounded only by `max_summary_input_tokens`.

The composition's `chain_seconds` defaults to 30 seconds (`DEFAULT_CHAIN_SECONDS`) for admitting last-resort record merges, smaller retries, user-summary folds and harder record rewrites on one pass. Set a positive finite duration or `None` to disable this deadline. It starts after the lossless reshape and does not cover earlier user-band summaries, media hooks or local fallback work. Expiry stops new requests, including retries without summary options; an in-flight request finishes and its answer is still evaluated and retained when acceptable. Elapsed time can therefore exceed `chain_seconds`. Caller cancellation and separately configured client or user-summary timeouts still cancel requests. `chain_timeouts` counts passes that stop admitting work after expiry. The configured overflow policy still runs and an oversized protected prompt still raises before sending. The foreground chain starts only above the sending budget; exceeding the record share alone does not start it.

A lossy or length-limited record merge is retried in smaller halves, with `record_merge_split_depth=2` by default (at most seven requests for one initial batch). Zero disables splitting. A merge request sent with an output cap is split the same way without being asked when the shortest text that holds the distinct values the current merge coverage requires, the shortest of them written once each with a space between, counted whole by the tokenizer, exceeds the answer budget `record_answer_budget` states, `RECORD_ANSWER_BUDGET_SHARE` (0.8) of the cap; a pair is only skipped. The values are read from the batch's own transcript, so the check reads no history. The skip is counted in `record_merges_skipped` and logged at INFO with `values_total`, `required`, `required_tokens` and `answer_tokens`. An answer holds more than those values, so a batch the check admits can still be refused, and it is made again on every attempt, so a lower coverage can admit a batch it held back. Requests without a cap are unchanged. Each child has at least two records and must fit the request limit independently; a singleton remains unchanged. Refused parents and child outcomes survive session restore, so later passes skip completed refusals and can resume children left unfinished by a deadline. Every refusal logs distinct value counts (`values_total`, `kept`, `missing`, `required`) and its reason, never the values. The case-sensitive check counts unique digit-bearing tokens of at least four characters and requires the current merge coverage (0.8 by default); it is not a complete semantic fact check, and reordering the same values does not affect it. Record-response INFO logs add elapsed seconds, finish reason and available input, output and reasoning token counts. A response marked `length` is not accepted even when its visible text meets coverage. An irreducible or protected record floor can still exceed the configured share.

Record merges and harder rewrites have a separate `merge_coverage_share`, inheriting the tool strategy's `coverage_share` when omitted. The tool-group grounding check keeps its own setting. `merge_coverage_floor` defaults to that starting bar, preserving existing decisions and request counts. To opt in, set a lower floor, for example `merge_coverage_share=0.8, merge_coverage_floor=0.7, merge_coverage_step=0.05` on the composed strategy. Coverage is checked against each candidate's current sources, not all original history: successive accepted merges can compound loss, and a 0.75 floor is not a promise to retain 75% of the conversation's original values.

Smaller split retries run first. Only a complete merge sweep with no accepted edits or unfinished work may lower the bar, and only while active records exceed `record_budget_share` of the current budget. Deadline expiry, exhausted search probes, provider failures and rate limits never authorize a step. At each step, previously refused answers that now qualify are reconsidered oldest-batch first without another request; the first step that changes records ends relaxation for that pass so new record combinations get a normal sweep next time. Coverage advances directly to the next configured step that can admit a cached answer, or to the floor when none can; intervening empty steps do not rescan history. Answers below the floor remain refused. The bar resets when records fit their share, including on a pass that needs no chain. `record_budget_share=None` disables automatic relaxation, and prompt-target pressure alone cannot lower the bar. Background workers prepare answers; only the foreground decides to relax.

With adaptation enabled, complete, smaller lossy merge answers retain their text and distinct-value counts under the same source fingerprints used by background answers. Changed sources, metadata, inclusion state, reasoning guards, accepted child merges, or a changed merge prompt invalidate them. Anonymous sources cannot authorize reuse. `merge_answer_cache_bytes` bounds the serialized UTF-8 answer list (1 MiB by default); oldest answers are evicted first, oversized answers are not retained, and zero disables retention. Eviction does not erase a refusal or trigger another request. Current tolerance and cached answers survive session restore; changing the start, floor, step or merge prompt resets this pending state, while a smaller byte limit trims it. Accepted historical edits keep their existing replay semantics. Rewrites use the current bar but their refused answers are not retained for later acceptance.

Refused cached answers are untrusted conversation data and can contain sensitive source content or model-added prose. Coverage and source fingerprints do not detect prompt injection or grant authority to act on a summary. Hosts must protect these answers with the same access, retention and storage controls as conversation history; the package does not redact them or persist them outside the host-provided session store. Set `merge_answer_cache_bytes=0` to disable retained answer text, or leave the floor at the starting bar to disable adaptation.

`current_merge_coverage_share` exposes the current bar. `record_consolidations_by_coverage` counts newly accepted merges and rewrites at each bar, and `record_values_omitted` sums their missing distinctive values. These diagnostic counters belong to the strategy instance and exclude accepted-edit replay; they are not restored with a session. Accepted answers that omit values and refused answers log counts at WARNING, never the values. Cached answers rerun the normal completion, size, coverage, source and preservation checks before acceptance.


A harder record rewrite is kept only when it removes at least `rewrite_gain_fraction` of the records it replaces (`DEFAULT_REWRITE_GAIN_FRACTION`, a tenth). A kept rewrite is a new record with every attempt open again, so without the floor an answer that trims a hundredth is asked for again on every pass over a prompt that stays over the budget. An answer that keeps the values and falls under the floor is logged as refused for `saving too little` and remembered like any other refusal; one that drops values is refused and counted as lossy first. Zero keeps any smaller rewrite.

The composed strategy defaults to `overflow_policy="shed"`. After its reshape, merge, rewrite and protected fallback steps, it may remove whole unrecorded tool groups. Once shedding starts, its target is the effective message budget minus `ceil(chain_gain_fraction * min(tokens_behind_first_removal, effective_budget))`, with those tokens measured before shedding. The reserve is bounded by that share of the effective budget; whole-group removal can cross the target by the last removed group's size; `chain_gain_fraction=0` stops at the budget. Protected material can prevent reaching the target without causing an error when the remaining prompt fits the budget. A WARNING names the lost message IDs, and removal markers tell the model that history was shed. These decisions replay on stored history and across session restores. Use `overflow_policy="raise"` to keep uncovered results instead. Both policies raise `CompactionBudgetExceeded` locally if the remaining anchors, records or other protected messages cannot fit. The host must handle that error by reducing protected context or choosing a larger budget; it is not a successful model response. The budget counts messages with your tokenizer: reserve additional room for tool schemas and provider overhead.

Last-resort shedding replays before the record and user phases, including on rebuilt history below the trigger. Stable message IDs retain removal decisions. Groups containing missing or positional `msg_N` IDs also retain a fingerprint of their complete original content, including tool call IDs, so the same group can replay when the host assigns UUIDs. Content replay requires a unique, unchanged whole group and respects external holds and call/result pairing. Changed or repeated identical groups are not matched by content. Legacy snapshots retain their stable shed IDs; positional IDs are discarded because their original content cannot be recovered from an ID alone, so those groups may need to be shed once more after upgrading.

Without a record, the composition also uses this overflow policy: it keeps tool results intact while they fit, then sheds whole eligible groups with the same headroom target. It does not shorten results as they age out of the tail. The standalone record strategy retains its configured give-up fallback at `fallback_fraction`. On a cache with only system and end-of-request breakpoints, an edit anywhere before the previous request end can miss back to the system prefix; avoiding repeated tail-exit edits is therefore necessary even when each edit is near the end.

Named `tool_choice` support lets the middleware pin recall. Routes that reject it can use a temporary recall instruction instead; neither mechanism proves that a record was written. Reports in [#1872](https://github.com/sokolaidev/maf-extensions/issues/1872) describe OpenRouter routes for DeepSeek V4 Flash and GLM 5.3 Flash ignoring the pin with reasoning enabled, while GPT 6 Luna and Tencent Hy3 honoured it. Those are reported observations, not provider qualification by this package; verify the selected route and reasoning settings.

### Optional instruction on pinned record requests

Some models keep only titles or reply formatting in their record even when the recall tool asks for values. Pass `record_instruction=DEFAULT_RECORD_INSTRUCTION` (imported from `maf_compaction`), or your own string, to `ToolResultRecallMiddleware` to append a user message after the loaded history on pinned recall requests only. With the default `None`, a pinned request adds a message only when there is retry feedback. The instruction does not change prompted recall, including `preserve_prompt_cache=True` and the fallback for routes without tool-choice pinning. It is removed on completion, failure and cancellation, and is absent from ordinary requests, stored history and compaction snapshots. As with prompted recall, hosts taking independent copies of in-flight message lists must filter messages whose `additional_properties["_maf_recall_prompt"]` is true before displaying or persisting them.

`DEFAULT_RECORD_INSTRUCTION` asks for tool-returned information, excludes earlier assistant text, explains that titles and URLs alone are insufficient, and retains exact numbers, limits, quotas, sizes, selected table rows and identifiers with their headings while keeping access and confidentiality restrictions. This can increase output cost, latency and the uncached share of requests; select it per model. Appending it leaves the earlier request prefix unchanged.

Size the record deliberately: value-bearing records may need much more than the default 4,000-token response cap. Set `make_recall_tool(target_tokens=...)` and the strategy's `record_target_tokens` to the same desired target, and set `record_max_tokens` above that target with room for JSON and provider overhead. For example, an 8,000-token target with a 16,000-token cap is a starting configuration to measure, not a guarantee of fit. Enabling the instruction changes none of these numbers automatically. A cap truncates output rather than shortening it. A length-limited response without usable recall values retries as an ordinary call and queues a shorter record request for a later call; other malformed arguments disable recall with a `failed` warning carrying the exception and cause chain. Monitor `records_truncated` as well as failures and coverage diagnostics.

## Summarizer instructions and upgrades

`MAF_COMPACTION_PROMPT_VERSION` selects the built-in user-summary, user-fold, record-merge and harder-rewrite prompts. The default keeps the previously shipped prompts; the hardened prompts are experimental and require an explicit opt-in.

| Environment value | Built-in prompts |
|---|---|
| Unset or `1` | Previously shipped prompts |
| `2` | Hardened prompts with an explicit source-data boundary |
| Any other value, including an empty string | Strategy construction raises `ValueError` |

Set the variable before constructing the user strategy or its compaction stack. For example, in PowerShell:

```powershell
$env:MAF_COMPACTION_PROMPT_VERSION = "2"
```

Set it to `1` to roll back. The user strategy captures the version at construction and exposes it as its read-only `prompt_version`; the composition inherits that selection. Changing the environment does not change an existing strategy. Recreate cached strategies and stacks, or restart the host, to switch versions. The version is host configuration and is not persisted in session state. Exported `DEFAULT_*` prompt constants and `harder_record_prompt()` always return version 1 text, independently of the environment.

Version 2 tells the summarizer that its next message is source data. Embedded instructions must not change its task or authorize actions; legitimate user requirements and corrections remain reported content, quoted instructions retain their attribution, and the answer must add nothing unsupported by the source. These instructions reduce ambiguity; they are not a semantic output validator. Record acceptance still checks size and retained distinctive values, and user summaries and folds still check usable output and size. Neither establishes that the output contains no invented prose or authorization.

Hosts can replace the user half's `prompt` and `fold_prompt`, and the composition's `merge_prompt`. The composition also accepts `rewrite_prompt: Callable[[int], str]`: a pure function of the one-based attempt that returns a nonempty system prompt. The built-in rewrite requests about 60%, then 36% of the original length, with the version 2 boundary when selected. Custom functions must ask for progressively stronger shortening because a refused harder attempt also forecloses milder attempts under the same prompt sequence. Nonempty custom prompt strings and a custom rewrite function take precedence over the selected version, remain unchanged, and own their data/instruction boundary.

Changing a prompt preserves accepted summaries, folds, record edits and their replay certificates. It does not revalidate their content or restore source history that storage already pruned. A host requiring reprocessing must rebuild from its retained original history and start fresh compaction state; clearing state alone is insufficient for history containing generated replacements or exclusions.

Fresh user-summary and chunk requests already include their prompt in the answer-cache key. Record refusals now include the merge prompt or complete rewrite prompt sequence. A changed prompt permits another request; an unchanged prompt retains refusal reuse across session restore. Legacy refusals without prompt identity are discarded, so an upgrade can retry eligible batches once under the new policy. Merge search progress and unfinished splits remain prompt-sensitive, and background answers prepared under a different merge prompt are discarded. Existing deadlines, input limits and retry bounds still apply.

The longer version 2 system prompts count against `max_summary_input_tokens` and the record batch cap. Near either limit, fewer whole source units may fit and more requests may be needed. [The comparison protocol](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/research/summarizer-boundary.md) separates measured prompt overhead from live retention and injection evidence. An optional host-configured model judge is tracked separately in [#2777](https://github.com/sokolaidev/maf-extensions/issues/2777).

## Serving many sessions

`SessionCompactionRegistry` builds a fresh `CompactionStack` for each session and stores its JSON-safe behavioral state in `AgentSession.state["maf_compaction"]`. The stack contains either the record strategy or the composition, its gate, and its recall middleware. One agent registers one dispatching middleware and one recall tool. Clients, tools and other expensive agent configuration can be shared; the builder must create fresh strategy, gate and middleware instances with identical configuration on every replica.

```python
from maf_compaction import (
    CompactionStack,
    RecallGate,
    SessionCompactionProvider,
    SessionCompactionRegistry,
    SessionToolResultRecallMiddleware,
    ToolResultAnchoredSummarizationCompactionStrategy,
    ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy,
    ToolResultRecallMiddleware,
    UserTurnAnchoredSummarizationCompactionStrategy,
    make_session_recall_tool,
)


def build_compaction(*, record_instruction: str | None = None) -> CompactionStack:
    records = ToolResultAnchoredSummarizationCompactionStrategy(
        max_input_tokens=BUDGET, tokenizer=tokenizer,
    )
    users = UserTurnAnchoredSummarizationCompactionStrategy(
        max_input_tokens=BUDGET,
        tokenizer=tokenizer,
        client=summarizer_client,
        summary_mode="boundary",
        remembered_requests=2,
    )
    strategy = ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy(
        tokenizer=tokenizer, tool_results=records, user_turns=users,
    )
    gate = RecallGate()
    recall = ToolResultRecallMiddleware(
        max_input_tokens=BUDGET,
        tokenizer=tokenizer,
        arm=gate.arm,
        disarm=gate.disarm,
        trigger_fraction=records.trigger_fraction,
        reforce=records.take_reforce,
        record_instruction=record_instruction,
    )
    return CompactionStack(strategy, gate, recall)


registry = SessionCompactionRegistry(build_compaction, max_cached_sessions=128)
compaction = SessionCompactionProvider(registry, tokenizer=tokenizer, before=False)
agent = create_harness_agent(
    client,
    name="shared-assistant",
    tools=[lookup_deployment, make_session_recall_tool(registry)],
    middleware=[SessionToolResultRecallMiddleware(registry, compact_before=True)],
    disable_compaction=True,
    context_providers=[compaction],
    max_output_tokens=MAX_OUTPUT,
    tokenizer=tokenizer,
)
# Match the harness's built-in placement immediately after its history provider.
agent.context_providers.remove(compaction)
agent.context_providers.insert(1, compaction)
```

To enable the pinned instruction for every fresh session stack, import `DEFAULT_RECORD_INSTRUCTION` and construct the registry with `lambda: build_compaction(record_instruction=DEFAULT_RECORD_INSTRUCTION)`. Apply the record target and cap together in the builder and `make_session_recall_tool(target_tokens=...)` as described above. The instruction belongs to builder configuration, so every replica must supply it again after a restore; it is never serialized into session state.

The harness in framework 1.20 loads and persists history per model call. Its provider `before_run` hook therefore has no history to compact. `compact_before=True` supplies the session's strategy to the chat client after history loads, including when a stream is consumed later. `before=False` avoids a second provider pass. The provider's `after_run` compacts stored history. Additional providers are appended by `create_harness_agent`, so the final two lines put compaction where the harness puts its own: after history, before the remaining providers; after hooks run in reverse order. A plain `Agent` using per-service-call history persistence uses the same middleware and provider. For a host that supplies loaded context to provider hooks itself, leave `compact_before=False` and enable the provider's `before` phase.

The list a model call is handed is rarely the list the previous call's pass left. The harness hands fresh copies of the stored messages, and a chat client with middleware compacts a copy of the tool loop's list, so the records, notes and exclusions of one call are gone on the next. The composed strategy therefore keeps the list each `compact_before` pass leaves. When the next call is handed the same stored messages, in the same order and state, with new ones after them, the pass starts from the kept list instead of putting every decision back, and the model is sent what a host that keeps its list would send it. If this list does not match, the strategy reapplies its decisions. The live list belongs to one process. A registry keeps one for its `max_carried_views` most recently used sessions, 16 by default and none at zero, and lets it go when the provider's `after_run` or the record-merge middleware ends the turn.

To reuse a completed pass across turns and replicas, set `saved_view_key="maf_compaction_view"` on `SessionCompactionRegistry` and enable `compact_before=True` on the session recall middleware. Give each configuration its own key, distinct from the decision and provider keys. The provider, record-merge middleware, or an explicit `registry.release(session.session_id)` at turn end writes a compact layout once under that key in `session.state`. Persist the session after release. The next turn consumes the layout on its first model call. It contains the text of generated records, removal notes and summaries, which can include sensitive conversation or tool data. Apply the same access, encryption and retention controls to this key as to conversation history, including in session exports and backups. Restore from trusted session storage, or authenticate the entire imported snapshot before loading its history, decisions and layout. The layout checksum detects corruption and stale decisions. Leave `saved_view_key=None` to store only the decisions. A missing or incompatible layout costs a full pass.

When a turn ends, including after cancellation or a pause for approval, the host can call `registry.release(session.session_id)` to drop that session's cached stack and carried view. The next phase builds a fresh stack from the saved session state. Repeated cleanup is safe. Calling `release` during an active compaction phase for that session raises `RuntimeError`.

Provider hooks resolve from their `session` argument, chat middleware from `context.session`, and the registered tool from the framework-injected `FunctionInvocationContext.session`. Each phase sets a scoped context variable and clears it on exit. No session identifier is a model-supplied tool argument, and missing sessions are refused. The inner middleware retains its single-session guard.

Persist the whole `AgentSession` using the framework's session serialization and restore it before the next request. The registry reads session state even on cache hits and writes it after provider, middleware and tool phases, including exceptional exits. Cache eviction and `max_cached_sessions=0` discard only cached stacks; the session retains its decisions. Removing the registry's state entry resets its stack. Use a distinct `state_key` for each independently configured registry and keep the builder configuration stable across restores; unsupported versions or stack-kind mismatches raise instead of silently dropping decisions. New snapshots use version 9, which adds source-member fingerprints for partial exclusion recovery to version 8's ordered source-bound replay certificates, prerequisite edit IDs and complete reshape batches, and version 7's record digests and version 6's durable shed-group fingerprints, versioned recall identities, media descriptions and media-aware summary replay keys. Compatible versions 1 through 8 remain readable; decisions without source-member fingerprints cannot recover partial batches that require undoing owned exclusions, and their cached record decisions remain subject to current reasoning guards without a matching source certificate, with legacy transcript-keyed kept folds migrated to summary-ID keys. Unversioned `call:` identities in a composed snapshot's `anchor` or `declined_behind` are rejected before restoration because their pending recall decisions cannot be mapped uniquely to occurrences. Resume those sessions with the original reader or explicitly rebuild from retained history after reviewing pending decisions; do not automatically discard rejected state. Older readers reject version-9 snapshots. Replicas sharing a session must run a schema-compatible build before a version-9 snapshot is written.

The snapshot carries record decisions and outstanding re-force asks; the chain's wait, kept replacements, source-bound replay certificates, complete reshape batches, reshaped-record digests, record-search digests, shed IDs and refusals; remembered user-summary requests and kept folds; pending recall and awaiting-record flags; and the one-shot gate permission. User compaction and fold counts also travel because they allocate summary IDs. Other counters remain local diagnostics and restart after cache eviction or a replica switch. `ChainDecisions.to_dict()` and `RecordDecisions.to_dict()` serialize the existing narrower replay snapshots; `CompactionStack.to_dict()` includes the content-keyed state as well. Kept folds and record replacements also retain their source counts, so replay validates the selected prefixes without rendering every possible prefix. Record decisions retain the transcript and source count together, so differently sized sources with identical numbered text retain separate answers. Legacy transcript maps migrate on restore, and entries without counts learn them when their transcripts first match. Refusals also retain source counts; ambiguous legacy refusals without counts are discarded on restore so they can be retried. Content-keyed entries contain conversation text and can grow with the conversation, so retain them under the same storage and retention policy as history.

Serialize requests for the same session in the host, including across replicas. The registry supports concurrent distinct sessions and rejects overlapping independent phases for the same session in one process; it does not supply a distributed lock or persist to an external store. Model quality, multi-process storage coordination and recovery from a crash between a model response and the host's session save are outside this adapter's guarantees.

### Merging records during tools

Add `make_session_record_merge_middleware(registry)` to the agent's middleware list before `SessionToolResultRecallMiddleware(registry, compact_before=True)` to prepare record merges while a reply's tool calls execute. The bundle supplies both agent-run lifetime management and chat-response observation. It requires a composed stack and an explicit `AgentSession`; installing it on a chat client alone is unsupported. Keep the provider and recall-tool wiring above.

```python
from maf_compaction import (
    SessionToolResultRecallMiddleware,
    make_session_record_merge_middleware,
)

middleware = [
    make_session_record_merge_middleware(registry),
    SessionToolResultRecallMiddleware(registry, compact_before=True),
]
```

A reply containing tool calls starts bounded record-batch search and merge requests on a private history and strategy snapshot. Work runs while records exceed their configured share or the snapshot exceeds the chain target, using the same request cap, split depth and `chain_seconds` admission deadline. User summaries, harder rewrites and overflow enforcement remain foreground work. Foreground passes consume completed background answers without waiting. While a request for this session is still running, the next pass defers its record merge search, including on the first model call of a new run.

Only a foreground pass can accept an answer. It matches source fingerprints and source count, checks current preservation and reasoning constraints, then applies the usual size and value checks. Accepted decisions replay into stored history through the existing source-bound replay mechanism. Search progress and pending split batches survive through normal session snapshots; tasks and queued background answers do not. With stepped coverage enabled, a complete lossy answer rejected by the foreground can enter the bounded session cache described above. A completed sweep permits foreground relaxation only while its source records, coverage policy, split depth and request limit still match. The final reply leaves ready answers for the end-of-run pass.

A successful run returns while admitted requests finish on the cached stack. The worker starts no further requests. Each outstanding request has until `summary_timeout_seconds` from the start of that request (120 seconds by default) to provide its answer. A later foreground pass in the same registry can apply the answer after restoring matching session state. The worker never writes session state after the run. Another process resumes from the persisted decisions and can ask an unfinished batch again. Cache eviction, explicit session release and replacement of authoritative compaction state discard pending work. Run cancellation, failure and early stream closure cancel and drain the worker. Clients must honour cancellation for prompt cleanup, and hosts must close abandoned response streams.

The worker inherits the context variables bound when the agent enters the record-merge middleware. Bind `observe_record_merges` and any conversation logging tags in an outer agent middleware to observe background requests, prepared answers and cleanup, including when another task reads or closes the stream.

Snapshots written before `split_merges` existed treat every counted refused merge as eligible for retries in smaller halves, including refusals that were no smaller. A present, empty `split_merges` list keeps its explicit meaning: none are eligible. Refusals without a source count remain ambiguous and are discarded on restore.

### Sharing summarizer capacity

Pass one `SummaryRequestLimiter` to the user-turn strategy in every stack using the same summarizer deployment and event loop. Create it outside the stack factory. The host supplies the configured client and divides the deployment's tokens-per-minute allowance between processes or replicas.

```python
from maf_compaction import SummaryRequestLimiter

summary_limiter = SummaryRequestLimiter(
    summary_tokens_per_minute=300_000,
    expected_request_seconds=40,
    max_parallel_summary_requests=12,
    rate_limit_cooldown_seconds=90,
)
```

Set `summary_limiter=summary_limiter`, `max_summary_input_tokens=12_000` and `summary_options={"max_tokens": 4_000}` when constructing each `UserTurnAnchoredSummarizationCompactionStrategy`. This starts at `floor(300000 * 40 / (60 * 16000)) = 12` concurrent requests, shared across stacks. The estimate uses the largest registered input-plus-output ceiling and an exponential moving average of successful request durations (20% weight for each observation). The optional maximum is an additional cap. An absent output cap restricts the shared limiter to one; omitting the limiter keeps the existing sequential behavior.

Foreground and background record merges select the oldest independent batches, including smaller refused-batch retries, and apply answers in selection order after checking current sources. Batches without durable source identities, including unidentified excluded reasoning, run sequentially in the foreground and are skipped by background preparation. A wave may finish more reductions than were needed to reach the target. New merged records wait for a later sweep. User summaries, chunks, folds and harder rewrites remain sequential within a stack but acquire capacity from the same limiter. `chain_seconds` still controls admission; admitted requests may finish after it expires. Cancellation and early stream closure drain outstanding requests. A successfully completed run can retain admitted background requests on its cached stack.

A 429 halves concurrency, down to one, for 90 seconds by default. Each further 429 restarts that cooldown; recovery adds one slot per cooldown interval without another 429. Numeric or HTTP-date `Retry-After` headers delay new requests and can extend the cooldown. Detection reads `status_code` on exceptions, their responses and cause chains, without a provider dependency. An optional `classify_rate_limit` callback returns a nonnegative wait in seconds for a rate limit (`0` if no provider wait), or `None` for other failures. Rate-limited batches remain eligible; they do not count as summary failures, retire summary options or become refusals. Already admitted successful answers are retained in order, and later sweeps retry remaining work.

`summary_limiter.stats` reports the admission limit, current and peak concurrency, started and finished requests (including failed or cancelled requests), rate-limit events and cooldowns. Reducing the limit allows existing requests to finish, so active requests may temporarily exceed the new limit. These counters belong to the shared limiter and are not session state. This is a throughput estimate, not a strict token bucket or requests-per-minute limit; hosts still configure provider-specific budgets and client timeouts.

### Keeping the event loop free

A pass over a long conversation spends seconds of CPU between its awaits: grouping, token counting, replaying earlier decisions, merging and shedding. The composition runs those synchronous steps on worker threads (`asyncio.to_thread`), so the host's event loop keeps serving other sessions and requests while a pass works. The pass takes as long as before; only the loop is free.

Two consequences for a host. The tokenizer is called from worker threads, so it must be safe to call from more than one thread at a time; `tiktoken` and the framework's `CharacterEstimatorTokenizer` are. Awaited collaborators, such as the summarizer client and media describers, are still awaited on the host's loop. Background record-merge work offloads its steps too: its worker is a copy of the strategy with state of its own, so its steps and the foreground pass's run side by side.

### Loading and saving a session

`SessionPersistence` wraps the whole agent request around the registry above. Supply asynchronous `load(session_id)` and `save(session_id, snapshot)` callbacks and a `lease(session_id)` asynchronous context manager. `load` returns a framework session snapshot or `None`; `save` atomically replaces that snapshot. The lease must exclude every writer to that session, across replicas when applicable, from before loading until after stream cleanup and saving. A storage backend with expiring leases must fence writes and reject saves from an expired lease. This package supplies neither a database client nor a distributed lock. A local `asyncio.Lock` is sufficient only when every writer runs in that same process.

```python
from maf_compaction import SessionPersistence

# `sessions` is the host's authorized storage and lease implementation.
persistence = SessionPersistence(
    registry,
    load=sessions.load,
    save=sessions.save,
    lease=sessions.lease,
)

response = await persistence.run(
    session_id,
    lambda session: agent.run(messages, session=session),
    create=True,
)

async with persistence.stream(
    session_id,
    lambda session: agent.run(messages, session=session, stream=True),
) as response_stream:
    async for update in response_stream:
        await send_update(update)
    final_response = await response_stream.get_final_response()
# A successful exit means the save completed; receiving the last update alone does not.
```

The host must authorize `session_id` before passing it to the adapter; it is not a model-supplied argument. Stored snapshots must carry that same ID. An absent session raises `KeyError` unless `create=True` explicitly permits creation. A failed load, malformed envelope or incompatible compaction snapshot raises before the request runs; none is treated as an absent session. The adapter restores through `AgentSession.from_dict()` and saves through `to_dict()`, taking detached JSON snapshots in both directions. Register any custom framework serialization codecs on every replica. Keep the registry configuration and snapshot schema compatible, preserve stable message IDs, and apply the same retention and access policies as conversation history. User-summary replacements, pending decisions and cooldowns travel with the rest of the session.

Four optional hooks extend that lifecycle; existing callbacks keep their signatures when these options are unused:

- **Fence writes:** `lease(session_id)` may yield an opaque token. When it yields anything other than `None` (including `0` or `False`), the adapter calls `save(session_id, snapshot, lease=token)`. When it yields `None`, `save` receives only its two positional arguments. The host must validate the token and commit the snapshot atomically in its storage backend; a separate in-process lease check cannot prevent a stale writer from overwriting a later holder's save. Callback failures propagate without a retry using a different signature.
- **Save a branch:** pass `save_as=new_id` to `run` or `stream`. The adapter loads `session_id`, calls `lease(session_id, save_as=new_id)`, and saves only under `new_id`, rewriting the saved envelope's `session_id` so that it can be resumed. The in-memory session keeps its original ID. The host must authorize both non-empty IDs and supply a lease callback accepting the `save_as` keyword, even when both IDs are equal. That callback must exclude conflicting readers/writers for the source and destination, for example with a shared conversation lock or ordered locks for both IDs. Existing destinations follow the host's atomic replacement or collision-rejection policy; this option does not itself enforce immutability. Omitting `save_as` keeps the one-ID lease call.
- **Finish under the lease:** supply an asynchronous constructor callback `after_save(saved_id, snapshot)`. It is awaited only after `save` returns successfully and before releasing the lease, including for a fully consumed stream. It receives the destination ID and saved envelope when `save_as` is used. Its return value is ignored; edits to the snapshot do not trigger another save. Failure or cancellation propagates without rollback or retry, even though the snapshot has already committed. The host owns lease renewal and any fencing or idempotency needed for external acknowledgements.
- **Migrate stored data:** supply `migrate(snapshot)`, returning a snapshot directly or asynchronously. It receives a detached JSON copy after loading and inside the lease, before envelope and compaction-state validation. It can remove obsolete provider fields or upgrade a known schema. It is skipped for missing sessions. Migration errors or results that remain invalid stop the request even with `create=True`; a migration is saved only as part of a successful request, under the destination ID if supplied.

For a host that chains response IDs, the callbacks can be composed as follows:

```python
persistence = SessionPersistence(
    registry,
    load=sessions.load,
    save=sessions.save,  # Accepts lease=token and atomically fences the write.
    lease=sessions.lease,  # Accepts save_as= and covers both IDs.
    after_save=sessions.acknowledge,
    migrate=sessions.migrate,
)
response = await persistence.run(
    previous_response_id,
    lambda session: agent.run(messages, session=session),
    save_as=new_response_id,
)
```

Only successfully completed requests are saved. Streaming requires `async with` and one consumer; full iteration or `get_final_response()` completes the response, including its final validation and provider hooks. The context closes the stream before saving and releasing the lease. An unused context never constructs the agent stream. Breaking early, explicitly closing an incomplete stream, leaving it unused, cancellation before saving, or a request/validation/cleanup error leaves the durable snapshot unchanged. A consumption failure prevents saving even if caught inside the block. Do not keep the stream or spawn consumers that outlive the block. Abandoning the entire context without closing it or terminating the process has no cleanup guarantee.

Storage, migration, serialization and post-save failures propagate. A streaming save or post-save failure can arrive after output has reached the caller. Once saving starts, cancellation, a lost acknowledgement, an `after_save` failure or a lease-release failure can surface after the backend committed; the adapter does not retry model work. The host must define recovery for that uncertainty and for a crash between model work and saving. Failed or abandoned requests discard their in-memory compaction changes, including newly accepted summaries, so a later retry may repeat that work. Completed requests that internally recover from a refused recall still save their validated state; rejected history does not bypass the existing recall or enclosing agent gates.

Use this as the single writer of the whole session snapshot. History providers can update the in-memory session during the request, but independent external session/history stores must participate in the host's lease and consistency policy. The adapter does not roll back tool side effects, provider-side conversations, or custom external writes. For hosts that already own the complete session lifecycle, the existing registry/provider/middleware wiring remains sufficient; do not add a second load/save owner. `SessionPersistence` is an outer request adapter rather than chat middleware because returning a `ResponseStream` from middleware does not mean its consumption, validation, or persistence has finished.

The user strategy's `max_summary_input_tokens` bounds each summarizer request, including instructions and the numbered transcript, using the configured tokenizer. It defaults to half `max_input_tokens` (at least one token) and also bounds user-summary folds and the composition's record merges and harder rewrites; with `record_budget_share` set, those record requests are further held to the summarizer's output cap. Set it for the summarizer's own input budget when that differs from the main model, reserving provider overhead and output space separately. A pass replaces only the oldest whole turns, summaries or records that fit; newer material stays for later passes. If no eligible prefix fits, the oldest item is retained without truncation or a request; a fold or merge needs at least two items to fit. The minimum user-band share is checked on the eligible band before limiting the request, so long histories can still make progress in bounded passes. Ordinary fold mode checks its break-even rule on the selected summary prefix; later summaries cannot fund that fold. The composition's emergency folds keep their smaller-result rule without this profitability gate. A partial replacement is kept only when it shrinks what it replaces; standalone compaction can still leave an oversized conversation, and the composition retains its configured overflow policy.

Set `summary_timeout_seconds` to a positive finite number to time out each user-summary or fold request. The timeout includes waiting for shared limiter capacity. Expiring during that wait counts as a skip without triggering failure backoff; expiring after provider admission cancels the request and counts as a failure. The client must honour cancellation, and cancellation cleanup can extend the elapsed time. Configure `max_consecutive_summary_failures` (a positive integer) together with `summary_backoff_seconds` (positive and finite) to skip fresh requests after that many consecutive failures. Exceptions, empty replies and rejected oversized summaries contribute to the streak. After the cooldown expires, the next eligible request retries; another failure renews the cooldown, while a fresh accepted summary resets the streak. Cached answers still replay during cooldown without resetting it. `user_summary_skips`, also exposed by the composition, counts withheld requests separately from `user_summary_failures`. All three settings default to `None` (off). Session snapshots retain the failure streak and a wall-clock cooldown deadline, so the cooldown follows a session across replica changes, restarts and stack-cache eviction when the host persists its state. Older snapshots default to no failures or cooldown. Expired deadlines or deadlines more than the configured backoff ahead are discarded, limiting the effect of clock differences between hosts. These settings cover user bands and user-summary folds, including emergency folds; record merges/rewrites and media-description hooks retain their own policies. A skipped or timed-out summary leaves its source material intact, and the composition continues to its configured overflow policy.

Hosts that supply the original transcript instead of loading stored history must still persist the framework session. Accepted user summaries and folds survive in compaction state independently of `remembered_requests` and `chunk_cache_size`. Before checking the user trigger, the strategy restores each accepted edit only when its source window still matches uniquely, in order, by message identity and content, and the selected turns remain eligible. Keep stable message IDs and immutable or versioned media references. Edited, removed or reordered sources are not replaced by a stale summary. Replay also works below the trigger and during cooldown; new trailing turns can then form a new band. `user_summaries_replayed` counts restored replacements, so one pass may increment it more than once. Retained edits contain summary text and source fingerprints and follow the session history retention policy. Legacy snapshots without these edits retain their existing exact-request replay behavior until a new edit is accepted.

Every model call of a run compacts the same message objects, so a turn excluded on one call stays excluded on the next, while a summary inserted on one call can be missing from the next call's list: the middleware's retry of a pinned call whose recall was refused, cut off, too large or failed, and the stored history of a harness whose host passes the transcript in, both start from the turns without it. The strategy puts a kept summary or fold back on turns still excluded for it, where it stood, without a summarizer request, and excludes again the summaries standing beside the fold or recompacted summary that superseded them, which the framework's tool loop releases. A removal note missing beside its shed group is put back the same way. Turns excluded for a summary that cannot be put back are sent whole again and logged at WARNING, and the user half may summarize them afresh.

## Per-call request budgets and chunked summaries

The composition's sending ceiling is `floor(max_input_tokens * (1 - estimate_margin)) - request_overhead`. `estimate_margin` defaults to `0.0`; set, for example, `estimate_margin=0.05` on `ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` to reserve five percent of the input budget for estimation error. The overhead is the instructions and serialized tool schemas in the current chat options, counted with the composition's tokenizer. Schema token counts are cached by their serialized contents, so changing a description or schema changes the count. This remains an estimate: reserve output space in `max_input_tokens` and account separately for provider-specific framing and other request fields.

`ToolResultRecallMiddleware` supplies the live options when the per-call `compaction_strategy` is the composition, including the harness's `before_compaction_strategy`. Session wiring uses `SessionToolResultRecallMiddleware(registry, compact_before=True)`. Custom integrations can call `await strategy.compact_before(messages, options=context.options or {})`. Calling `await strategy(messages)` directly, including an after-phase history pass, keeps the configured history budget and does not subtract request overhead or margin. Keep the middleware on calls with no tools too: their smaller overhead leaves more room for messages. Function tools, callables and dictionary tool specifications are supported; unknown tool objects raise rather than silently count as zero.

A refused sending-budget estimate during post-call recall bookkeeping leaves the completed response intact and queues no new pin. Before-call compaction still enforces its sending ceiling; hosts choosing to send anyway can handle that refusal before contacting the model.

Set `summary_input_policy="chunk"` on `UserTurnAnchoredSummarizationCompactionStrategy` to cover the entire eligible user band when its request is too large. The default `"skip"` retains the existing bounded-prefix behavior. Chunk mode closes chunks at turn boundaries when the next turn would exceed `max_summary_input_tokens`, then summarizes each with the user-summary prompt. It merges the partial summaries with `fold_prompt`, in bounded batches until one summary remains. A chunk whose answer is no smaller keeps its original text for the merge, including a short final turn. A single oversized turn, an empty answer, or partials that cannot fit a reducing merge leave the band intact and count as one failure. Standing-summary folds retain their existing bounded-prefix behavior; record merges use the composition's own batches.

Closed chunk requests keep identical bytes as turns are appended to the same band. An independent LRU cache retains up to `chunk_cache_size=128` successful chunk and merge answers; set zero to disable it. The cache survives session snapshots and can reuse completed chunks after a failed pass. The final answer still must shrink the band before any source is excluded. `summary_timeout_seconds` and the failure cooldown apply once to the entire chunked pass, including merges and retries, rather than once per model request.

Optional `summary_options={"max_tokens": 2000}` passes chat options to user-band summaries, chunk merges, standing-summary folds, record merges and record rewrites. All of these requests share the same options and retirement state. Its `max_tokens`, while those options are in force, is also the output cap the composition's record batches are sized to. With `retry_without_summary_options=True`, an exception or empty response with those options retries once without them and retires those options for the rest of the strategy instance's life. Cancellation and deadline expiry do not trigger that retry. The default is no options and no retry; the disabled-options verdict is instance-local, not session state.

## Images and other media

User turns containing media stay verbatim by default, including their text. With a text-only tokenizer, compaction strategies and recall triggers estimate 1,024 tokens per `data`, `uri` or `hosted_file` part, including media inside tool-result items. Inline base64 and remote URLs are replaced for counting, so their string lengths do not determine the estimate. Pass the same estimator to every strategy and recall middleware to configure provider-specific costs.

```python
from agent_framework import CharacterEstimatorTokenizer
from maf_compaction import MediaTokenEstimator

tokenizer = MediaTokenEstimator(
    CharacterEstimatorTokenizer(),
    image_tokens=1_024,
    other_media_tokens=2_048,
)
```

A pure `estimate_media(content) -> int` callback can override both fixed costs. Estimates must be positive integers. The composition can reserve instructions and tool schemas per sending call as described above.

For provider-specific message accounting, implement `count_message_tokens(message: Message) -> int` alongside `count_tokens(text: str) -> int` on your tokenizer. Pass it directly to count the original message, including its media. Wrap it in `MediaTokenEstimator` to receive a message with each media part replaced by a text marker and let the wrapper add its configured costs. Raw text, including tool results during shortening, always goes to `count_tokens`. Text-only tokenizers keep the framework serialization estimate and default media costs. Framework token gates outside this package use the framework's own accounting.

To compact media-bearing user turns, pass an async `describe_media(content, message) -> str | None` hook to `UserTurnAnchoredSummarizationCompactionStrategy`. It receives copies of the media part and full user turn. Return a description or `None` for an unsupported part. To describe only some formats, also pass `describes_media(content) -> bool`: a turn holding any media part it rejects stays preserved and the band folds past it, as without a hook, where a `None` answer stops the pass at that turn. The host chooses and calls the vision, transcription or document service, including its timeout and request/output limits. The strategy retains part order and marks descriptions as inferred context, then applies `max_summary_input_tokens` to the complete summarizer request. Host-preserved turns and unknown non-text types remain protected. Failed, empty or oversized descriptions retain the turn; cancellation propagates. The original turn is excluded only after accepting a smaller summary.

Successful descriptions persist in session snapshots and are reused across loaded copies, stored history and replica switches. Identities include message ID, ordered content and surrounding text, so different media do not reuse an answer merely because their text matches. Keep message IDs, media references and hook configuration stable across restores. Use immutable or versioned URLs: the package does not fetch them to detect remote changes. Descriptions are lossy conversation data; retain original media separately when it must remain recoverable. If protected media cannot fit, the composition raises `CompactionBudgetExceeded`. See the [media design decisions](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/strategies.md#media-handling).

## Provider support for forced recall

`ToolResultRecallMiddleware` stops requesting recall for a session after two consecutive forced responses contain no `recall_earlier_tool_results` call. A response containing that call resets the streak; ordinary responses do not. The two ignored responses still carry the record cap, but subsequent requests retain the caller's own options. Existing records remain usable; the composition stops waiting for a new record and continues through its user half and configured anchored fallback/overflow policy.

When a pinned request raises `ChatClientException` (including its subclasses) with `tool_choice` in its message or explicit `__cause__` chain, the middleware records `pin_disabled_reason="unsupported"` and retries the recall request with `tool_choice="auto"` and a temporary user message at the end of the request, after the last tool result or user question. That request advertises only `recall_earlier_tool_results`; the caller's system instructions stay unchanged. This mode persists for later recall requests and across session restores. Set `supports_tool_choice=False` on the middleware to select it up front. The gate, record cap, targeted re-ask guidance, argument validation, stream buffering and backoff still apply. The trailing message is removed from middleware-owned lists after completion, failure or cancellation, and the caller's tools and options are restored for ordinary calls. Messages flagged by `additional_properties["_maf_recall_prompt"]` are stripped at middleware entry and again after downstream history loading, before compaction and model execution, so retained copies cannot become stale instructions on later requests. Framework layers and hosts can copy a prompt before cleanup; hosts that persist or display complete agent message lists must exclude messages whose `additional_properties.get("_maf_recall_prompt")` is truthy, including when replaying older checkpoints. Middleware cleanup cannot retroactively edit those independent copies. Prompt recall depends on the model following the instruction; this package's offline Ollama adapter checks do not establish live recall quality.

Set `preserve_prompt_cache=True` to keep the caller's `tools` and `tool_choice` on recall requests. The middleware sets the configured record cap (`max_tokens`) and appends the ask after the conversation history. This leaves the tools and preceding messages available for reuse by providers with automatic or explicitly enabled prefix caching. The session records `pin_disabled_reason="prompt_cache"` on its first call and logs it at INFO, since the mode is configured rather than a failed pin. It takes precedence over `supports_tool_choice`. With every tool on offer the model may answer with another tool instead of a record; the two-ignored-asks rule above bounds that, and the value-free and failure handling are those of the prompted mode.

A call without the recall tool or with `tool_choice="none"` keeps a pending recall ask for a later tool-enabled call. It does not count as an ignored pin. The dictionary form `{"mode": "none"}` is treated the same way.

A forced response must carry recall arguments as a JSON object with a `values` field. The advertised schema remains a string. List values are normalized by joining string items with newlines and JSON-serializing other items; object values are JSON-serialized with Unicode preserved. Normalized arguments reach both the completed response and buffered stream updates before tool execution. Except when the response ends with `finish_reason="length"`, malformed JSON, non-object arguments, missing `values`, and scalar values other than strings are handled as failed forced calls. On non-truncated responses, empty lists, empty objects, and empty or whitespace-only text count toward `value_free_record_streak` once per forced response, including across session restores. The recall tool consumes its gate permission but returns no record marker for blank text, so the response cannot anchor removal or count as a recorded result. Set `forced_call_timeout_seconds` to a positive finite number to bound the complete forced attempt, including stream consumption and finalization. Timeout cancels the attempt and takes the same unpinned retry path; cancellation cleanup may extend elapsed time. This option defaults to `None`.

A forced response ending with `finish_reason="length"` is counted before argument validation. If it has no recall call, malformed arguments or blank values, the middleware discards the response and its buffered updates, drops deferred framework-managed history writes, disarms recording and retries once with the original options. If the ordinary sending budget has room, or no per-call budget was supplied, it queues a shorter target (one quarter of `record_max_tokens` when configured). The pending ask and guidance survive session restore. Truncation does not increment failed, ignored or value-free counters and does not disable recall. Parseable nonblank values remain usable under the normal coverage checks; the length flag alone does not grant removal authority.

A forced request that raises any other ordinary exception, including during stream consumption or finalization, disables recall immediately and retries once with the original request options, including an absent `max_tokens`. A failure of the prompt fallback takes this same ordinary retry path; a failure of that ordinary retry propagates. The forced deadline covers both the pin and its prompt fallback. Cancellation and framework middleware abort signals propagate without another retry or a new provider-support verdict, leaving the recall ask queued. Forced streams, including prompt fallback, are buffered through completion before their updates reach the caller, so a failed attempt cannot expose partial text or tool calls. Ordinary responses and ordinary retries keep normal streaming behavior. This can delay the first visible update from an ignored forced request.

The recall request and tool description ask for task-relevant information from tool results, explicitly excluding earlier assistant text. Exact identifiers, numbers, paths and other unreconstructable values are retained alongside the meaning of tool-returned findings, conclusions and summaries. Access and confidentiality restrictions still apply. A re-ask can identify up to eight missed results by tool name, call ID and message ID in a temporary trailing user message. Each field must start with an ASCII letter or digit, contain only ASCII letters, digits, `_`, `.`, `:` or `-`, and fit in 64 characters. Other fields are omitted. If no fields qualify, the model still receives the general coverage guidance. Session restore accepts the package’s coverage and shorter-record guidance, rechecks target identifiers, and drops unrecognized feedback. Pinned requests append the feedback to `record_instruction` when configured. The advertised tool descriptions stay the same. `records_without_values` on the record strategy and composition counts distinct observed records quoting no distinctive value from eligible target groups; records with no distinctive target values are not counted. When bound to recall middleware, two consecutive eligible value-free records disable forcing for that session. A record quoting any target value resets the streak while forcing is enabled; a nonblank record with no eligible target values leaves it unchanged. Blank forced responses advance the streak independently of target eligibility and are not included in `records_without_values`, which counts observed record anchors. Re-reading a record does not advance the streak, including after restore. This check does not guarantee that a record preserved every fact.

A forced response carrying the framework's `model_output_kind="refusal"` text marker is rejected as a whole, even if it also contains complete recall arguments. The middleware recognizes both the `core` namespace and the legacy flat marker, checking buffered stream updates as well as the finalized response. It disarms recording, increments `failed_forced_calls` and `value_free_record_streak` once, and retries once with the caller's original options. Refused tool calls and stream updates are not returned for execution. Framework-managed per-call history writes wait for recall validation, and accepted writes remain subject to any enclosing persistence guard. This uses the pinned MAF 1.23 persistence hooks; custom persistence outside those hooks and provider-side conversation storage are not rolled back. Two consecutive refused/blank responses or eligible value-free records stop recall with reason `"value_free"`, including across restores. A successful record with target values resets this shared streak. Ordinary responses outside a requested recall are unchanged; plain text is not classified by wording.

The strategy retains the pending recall's distinctive target tokens before compaction can shed their groups. These tokens survive session snapshots until the next recall answer is judged, so overflow cleanup cannot erase the basis for value-free backoff. Consolidation replacements do not count as recall answers, and unjudged outcomes survive replay and session restores. They support judgment only and never authorize removing a group. Older snapshots without saved targets use the remaining eligible groups. `last_record_value_judgment` on the record strategy reports `has_values`, `value_free`, `no_candidates`, or `no_values`, and is `None` before a local observation. Unjudged records emit an INFO message with `no_candidates` or `no_values`, without record text or target values.

Hosts can inspect `pin_disabled`, `recall_disabled`, `pin_disabled_reason`, `value_free_record_streak`, `ignored_forced_calls`, and `failed_forced_calls` alongside `forced_calls` and `records_truncated`. The module logger `maf_compaction._toolsummary` emits a WARNING on each disablement transition with the bound session id (`None` when no session is bound) and reason; the configured `"prompt_cache"` transition is logged at INFO instead. A `failed` warning also includes the exception traceback and explicit cause chain, including provider error text or the reason recall arguments were rejected; messages and tool arguments are not logged separately. The reason is `"unsupported"`, `"prompt_cache"`, `"ignored"`, `"failed"`, or `"value_free"`; it is `None` while enabled or when restoring a legacy snapshot without a reason. `"unsupported"` and `"prompt_cache"` disable only pinning; `recall_disabled` stays false until recall stops entirely. Forced counters include prompt requests; the unsupported-pin negotiation is not counted as a failed recall attempt. An `ignored` warning also reports `mode=pinned` or `mode=prompted` and classifies the final response as `text`, `other_tool`, `other_tool+text` or `other_or_empty`, without tool names or arguments. Length-limited responses count in `records_truncated`, not the ignored counter. The disabled flag, reason, both streaks and identities of evaluated records persist in session snapshots; diagnostic counters remain local to the stack. Older readers conservatively stop recall on the unsupported state. Reset the session's compaction state to try pinning again after changing providers; legacy `"failed"` sessions are not automatically reclassified. `forget_pending()` clears queued work, not the session's provider-support verdict.

The following observations were reported in [#1876](https://github.com/sokolaidev/maf-extensions/issues/1876) from OpenRouter probes on 2026-10-12 with maf-compaction 0.2.0 and agent-framework-core 1.20. They are historical route observations, not a current compatibility guarantee or a built-in model allow-list.

| Route | Reported behavior |
| --- | --- |
| DeepSeek V4 Flash; GLM 5.3 Flash, reasoning enabled | Returned plain text despite the named tool choice, streamed and non-streamed. |
| GPT 6 Luna | Honoured the named tool choice in those probes. |
| Tencent Hy3 | Honoured the pin in initial probes, but streamed reasoning at about 65k prompt tokens errored on two of three forced requests; unpinned requests passed eight of eight. |

Before accepting a forced recall response with per-call composed compaction, the middleware measures the outgoing history plus the returned call and the recall tool's expected result against the ordinary follow-up sending budget. An oversized pair is discarded before tool execution or gated framework history writes, for both streaming and non-streaming calls. An ordinary retry proceeds. If the history alone already fills the ordinary sending budget, recall waits until a later call has room before scheduling another record. An ask that was already pending, such as one restored with a session, is checked against the history its own pass leaves before the request is sent, and when that history fills the budget the call goes out once, unpinned, and the ask is not counted as made. Outstanding coverage requests retain their remaining targets across this pause and session restore. Their arrival timeout starts again when the request is taken. When the history has room and the pair pushes it over budget, the next ask requests a shorter record. This uses the actual returned values, including when `record_max_tokens=None` or a provider exceeds its cap; it may decline a record that another compaction pass could have made room for.

A changed follow-up budget, extra host-added content or another tool's fresh output can still exceed the budget. When a rejected pass carries fresh results answering calls loaded from framework history, the composed strategy raises `CompactionPendingResultBudgetExceeded`, a subclass of `CompactionBudgetExceeded`. Its `pending_results` tuple contains the carried-in message objects, preserving their content and metadata. Before another request, the host must persist these results through its history provider or roll back the corresponding stored calls. The exception does not write history or undo external tool effects. This detection uses the framework's history attribution; custom stores must retain that attribution or own their recovery boundary.

## Shortening labelled tool results

Anchored strategies and recorded-row removal skip results with metadata or non-text items by default. Pass `rewrite_result_text` to opt in using a host policy. This example handles one text item, including a labelled item alongside media:

```python
from agent_framework import CharacterEstimatorTokenizer, Content
from maf_compaction import AnchoredCompactionStrategy


def rewrite_single_text(content: Content, text: str) -> bool:
    items = [item for item in content.items or [] if item.type == "text"]
    if len(items) != 1 or items[0].raw_representation is not None:
        return False
    items[0].text = text
    return True


strategy = AnchoredCompactionStrategy(
    max_input_tokens=100_000,
    tokenizer=CharacterEstimatorTokenizer(),
    rewrite_result_text=rewrite_single_text,
)
```

The synchronous hook receives a copy of the result and the proposed shortened text. Return `True` after rewriting its text items, or `False` to skip shortening; only `items` are adopted, and the library sets `result` to the shortened text. Their nonempty text values joined with newlines must equal that text, or the rewrite raises `ValueError`. Retain the supplied non-text objects in their original order; dropping, replacing or reordering them raises `ValueError`. The library reuses the original non-text objects, so media payloads, metadata and provider state remain intact and retain their configured media token estimates. Changes to the copy's outer fields are ignored.

The host owns the meaning of text metadata. Keep each item's `additional_properties`, annotations and other fields, or merge them conservatively when replacing several text items with one stand-in. For security labels, that means the weakest integrity and highest confidentiality among all replaced items; the library imposes no label schema. A hook that supports provider `raw_representation` must keep it consistent with the new text. The example declines such items because it has no provider-specific rewrite rule. The copy includes raw state; an object that cannot be copied raises before that result is changed. Hook exceptions propagate as well.

Hooks must be deterministic and have no external effects. Minimum-gain compaction evaluates them on copies during planning and may discard the entire plan. Rejected hooks leave the source result untouched; existing whole-group shedding and preservation rules still apply. The record strategy accepts the same hook for partial table-row removal and passes it to its default anchored fallback; configure an explicitly supplied fallback separately. Bare-text results use the built-in rewrite without calling the hook. Reload repair of older mismatched `result`/`items` remains limited to bare text; a newly rewritten labelled result already has both fields synchronized. Reconfigure the hook when rebuilding a session stack; it is not serialized into session state.

## When to use it

Below the context window, not compacting is cheaper than any strategy here: the whole conversation stays cached. These strategies pay off once a conversation outgrows its window, where the framework's own strategies either lose the facts in the tool results or re-bill the prompt on every turn. The measurements behind that, and what each strategy does at the edges, are in the [compaction documentation](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/README.md).

## What it depends on

The strategies call `agent_framework._compaction`, the framework's private compaction helpers, for grouping, token annotation and the exclusion flags. Nothing public exposes those, so the dependency on `agent-framework-core` is pinned to one minor and moved only after the new minor has been read against.

## Licence

MIT. See the [LICENSE](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE).
