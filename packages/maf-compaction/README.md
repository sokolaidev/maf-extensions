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
| `ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` | Runs the two summarising strategies over one conversation, record phase first, with a bounded last-resort chain that merges and rewrites while the prompt is over budget. If still over budget, sheds whole unrecorded groups with a warning by default, or raises locally in strict mode. |

All of them implement the framework's `CompactionStrategy` and attach wherever the framework takes one: `create_harness_agent`, or a `CompactionProvider` on a plain `Agent`.

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

The size trigger asks for the first record immediately. Later size-triggered records wait for `min_groups_before_repeat` new tool groups (four by default); `max_groups_before_record` remains a separate bound that can force records below the trigger. Re-force chains retry only their original uncovered batch and cannot recruit groups arriving during an ask. Wiring the strategy's bound `take_reforce` method also shares the middleware's non-consuming `record_due` preview with the composed wait: the user half waits only for a configured trigger or an outstanding request, including after session restore. If you wrap `take_reforce` in another callable, explicitly call `record_strategy.bind_recall(recall)` after constructing the middleware. Whole-group deletion still requires the configured share of exact values. When a string result group exceeds `record_target_tokens` (2,000 by default), a partial record can instead release pipe-delimited Markdown table rows containing a distinctive value and quoted verbatim under matching column headings. Headings and uncovered rows remain, and a marker identifies the omitted rows. Bare values, changed rows, code fences, rows containing backslashes and non-string results do not qualify for partial removal. Rows repeated under the same headings anywhere in the loaded tool results remain because their source is ambiguous. Small groups keep the existing coverage rule.

Set the record strategy's `record_target_tokens` to the target passed to `make_recall_tool(target_tokens=...)`; the middleware's `record_max_tokens` is a separate response cap. Setting both targets to `None` disables partial removal and the target-based diagnostic while keeping whole-group coverage checks. `groups_held_unreachable_coverage`, available on both the record strategy and composition, counts held groups on the last record pass whose estimated required value text exceeds that target. The estimate sums standalone token counts for the cheapest `ceil(value_count * coverage_share)` distinct values, excluding labels, formatting and other groups. It is a diagnostic estimate, not proof that a tokenizer or model cannot fit a record. Partial rows are recomputed from the loaded history and records; no extra session state is required.

The composed strategy defaults to `overflow_policy="shed"`. After its existing merge, rewrite and protected fallback steps, it may remove whole unrecorded tool groups to meet `max_input_tokens`. A WARNING names the lost message IDs, and removal markers tell the model that history was shed. These decisions replay on stored history and across session restores. Use `overflow_policy="raise"` to keep uncovered results instead. Both policies raise `CompactionBudgetExceeded` locally if the remaining anchors, records or other protected messages cannot fit. The host must handle that error by reducing protected context or choosing a larger budget; it is not a successful model response. The budget counts messages with your tokenizer: reserve additional room for tool schemas and provider overhead.

Named `tool_choice` support is required for reliable recall. Reports in [#1872](https://github.com/sokolaidev/maf-extensions/issues/1872) describe OpenRouter routes for DeepSeek V4 Flash and GLM 5.3 Flash ignoring the pin with reasoning enabled, while GPT 6 Luna and Tencent Hy3 honoured it. Those are reported observations, not provider qualification by this package; verify the selected route and reasoning settings. A pin does not prove that a record was written.

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


def build_compaction() -> CompactionStack:
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

The harness in framework 1.20 loads and persists history per model call. Its provider `before_run` hook therefore has no history to compact. `compact_before=True` supplies the session's strategy to the chat client after history loads, including when a stream is consumed later. `before=False` avoids a second provider pass. The provider's `after_run` compacts stored history. Additional providers are appended by `create_harness_agent`, so the final two lines put compaction where the harness puts its own: after history, before the remaining providers; after hooks run in reverse order. A plain `Agent` using per-service-call history persistence uses the same middleware and provider. For a host that supplies loaded context to provider hooks itself, leave `compact_before=False` and enable the provider's `before` phase.

Provider hooks resolve from their `session` argument, chat middleware from `context.session`, and the registered tool from the framework-injected `FunctionInvocationContext.session`. Each phase sets a scoped context variable and clears it on exit. No session identifier is a model-supplied tool argument, and missing sessions are refused. The inner middleware retains its single-session guard.

Persist the whole `AgentSession` using the framework's session serialization and restore it before the next request. The registry reads session state even on cache hits and writes it after provider, middleware and tool phases, including exceptional exits. Cache eviction and `max_cached_sessions=0` discard only cached stacks; the session retains its decisions. Removing the registry's state entry resets its stack. Use a distinct `state_key` for each independently configured registry and keep the builder configuration stable across restores; unsupported versions or stack-kind mismatches raise instead of silently dropping decisions. New snapshots use version 4 to retain media descriptions and media-aware summary replay keys alongside the recall state; versions 1, 2 and 3 remain readable, with legacy transcript-keyed kept folds migrated to summary-ID keys. Older readers reject version-4 snapshots. Replicas sharing a session must run a schema-compatible build before a version-4 snapshot is written.

The snapshot carries record decisions and outstanding re-force asks; the chain's wait, kept replacements, shed IDs and refusals; remembered user-summary requests and kept folds; pending recall and awaiting-record flags; and the one-shot gate permission. User compaction and fold counts also travel because they allocate summary IDs. Other counters remain local diagnostics and restart after cache eviction or a replica switch. `ChainDecisions.to_dict()` and `RecordDecisions.to_dict()` serialize the existing narrower replay snapshots; `CompactionStack.to_dict()` includes the content-keyed state as well. Kept folds and record replacements also retain their source counts, so replay validates the selected prefixes without rendering every possible prefix. Record decisions retain the transcript and source count together, so differently sized sources with identical numbered text retain separate answers. Legacy transcript maps migrate on restore, and entries without counts learn them when their transcripts first match. Refusals also retain source counts; ambiguous legacy refusals without counts are discarded on restore so they can be retried. Content-keyed entries contain conversation text and can grow with the conversation, so retain them under the same storage and retention policy as history.

Serialize requests for the same session in the host, including across replicas. The registry supports concurrent distinct sessions and rejects overlapping independent phases for the same session in one process; it does not supply a distributed lock or persist to an external store. Model quality, multi-process storage coordination and recovery from a crash between a model response and the host's session save are outside this adapter's guarantees.

The user strategy's `max_summary_input_tokens` bounds each summarizer request, including instructions and the numbered transcript, using the configured tokenizer. It defaults to half `max_input_tokens` (at least one token) and also bounds user-summary folds and the composition's record merges and harder rewrites. Set it for the summarizer's own input budget when that differs from the main model, reserving provider overhead and output space separately. A pass replaces only the oldest whole turns, summaries or records that fit; newer material stays for later passes. If no eligible prefix fits, the oldest item is retained without truncation or a request; a fold or merge needs at least two items to fit. The minimum user-band share is checked on the eligible band before limiting the request, so long histories can still make progress in bounded passes. Ordinary fold mode checks its break-even rule on the selected summary prefix; later summaries cannot fund that fold. The composition's emergency folds keep their smaller-result rule without this profitability gate. A partial replacement is kept only when it shrinks what it replaces; standalone compaction can still leave an oversized conversation, and the composition retains its configured overflow policy.

Set `summary_timeout_seconds` to a positive finite number to time out each user-summary or fold request. A timeout cancels the request and counts as a failure; the client must honour cancellation, and cancellation cleanup can extend the elapsed time. Configure `max_consecutive_summary_failures` (a positive integer) together with `summary_backoff_seconds` (positive and finite) to skip fresh requests after that many consecutive failures. Exceptions, empty replies and rejected oversized summaries contribute to the streak. After the cooldown expires, the next eligible request retries; another failure renews the cooldown, while a fresh accepted summary resets the streak. Cached answers still replay during cooldown without resetting it. `user_summary_skips`, also exposed by the composition, counts withheld requests separately from `user_summary_failures`. All three settings default to `None` (off). The streak and monotonic cooldown belong to the strategy instance and are not persisted or shared between instances. These settings cover user bands and user-summary folds, including emergency folds; record merges/rewrites and media-description hooks retain their own policies. A skipped or timed-out summary leaves its source material intact, and the composition continues to its configured overflow policy.

## Images and other media

User turns containing media stay verbatim by default, including their text. All compaction strategies and recall triggers estimate 1,024 tokens per `data`, `uri` or `hosted_file` part, including media inside tool-result items. Inline base64 and remote URLs are replaced for counting, so their string lengths do not determine the estimate. Configure provider-appropriate costs by passing the same estimator to every strategy and recall middleware:

```python
from agent_framework import CharacterEstimatorTokenizer
from maf_compaction import MediaTokenEstimator

tokenizer = MediaTokenEstimator(
    CharacterEstimatorTokenizer(),
    image_tokens=1_024,
    other_media_tokens=2_048,
)
```

A pure `estimate_media(content) -> int` callback can override both fixed costs. Estimates must be positive integers; they are not provider usage measurements. `count_message_tokens(message)` exposes the estimate to host code. `count_tokens(text)` delegates to the underlying text tokenizer; framework token gates outside this package retain their own accounting. Reserve tool schemas and provider overhead separately.

To compact media-bearing user turns, pass an async `describe_media(content, message) -> str | None` hook to `UserTurnAnchoredSummarizationCompactionStrategy`. It receives copies of the media part and full user turn. Return a description or `None` for an unsupported part. The host chooses and calls the vision, transcription or document service, including its timeout and request/output limits. The strategy retains part order and marks descriptions as inferred context, then applies `max_summary_input_tokens` to the complete summarizer request. Host-preserved turns and unknown non-text types remain protected. Failed, empty or oversized descriptions retain the turn; cancellation propagates. The original turn is excluded only after accepting a smaller summary.

Successful descriptions persist in session snapshots and are reused across loaded copies, stored history and replica switches. Identities include message ID, ordered content and surrounding text, so different media do not reuse an answer merely because their text matches. Keep message IDs, media references and hook configuration stable across restores. Use immutable or versioned URLs: the package does not fetch them to detect remote changes. Descriptions are lossy conversation data; retain original media separately when it must remain recoverable. If protected media cannot fit, the composition raises `CompactionBudgetExceeded`. See the [media design decisions](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/strategies.md#media-handling).

## Provider support for forced recall

`ToolResultRecallMiddleware` stops forcing recall for a session after two consecutive forced responses contain no `recall_earlier_tool_results` call. A response containing that call resets the streak; unpinned responses do not. The two ignored responses still carry the record cap, but subsequent requests retain the caller's own `tool_choice` and `max_tokens`. No extra instruction is inserted. Existing records remain usable; the composition stops waiting for a new record and continues through its user half and configured anchored fallback/overflow policy.

A forced request that raises an ordinary exception, including during stream consumption or finalization, disables forcing immediately and retries once with the original request options, including an absent `max_tokens`. A failure of that unpinned retry propagates. Cancellation and framework middleware abort signals propagate without a retry or a provider-support verdict. Only forced streams are buffered through completion before their updates reach the caller, so a failed attempt cannot expose partial text or tool calls before the retry. Ordinary and retry responses keep normal streaming behavior. This can delay the first visible update from an ignored forced request.

Hosts can inspect `pin_disabled`, `ignored_forced_calls`, and `failed_forced_calls` alongside `forced_calls` and `records_truncated` and log them with their configured provider/model. The ignored counter includes truncated responses without a recall call; it measures noncompliance, not its cause. The disabled flag and consecutive-ignore streak persist in session snapshots; diagnostic counters remain local to the stack. Reset the session's compaction state to try forcing again after changing providers. `forget_pending()` clears queued work, not the session's provider-support verdict.

The following observations were reported in [#1876](https://github.com/sokolaidev/maf-extensions/issues/1876) from OpenRouter probes on 2026-10-12 with maf-compaction 0.2.0 and agent-framework-core 1.20. They are historical route observations, not a current compatibility guarantee or a built-in model allow-list.

| Route | Reported behavior |
| --- | --- |
| DeepSeek V4 Flash; GLM 5.3 Flash, reasoning enabled | Returned plain text despite the named tool choice, streamed and non-streamed. |
| GPT 6 Luna | Honoured the named tool choice in those probes. |
| Tencent Hy3 | Honoured the pin in initial probes, but streamed reasoning at about 65k prompt tokens errored on two of three forced requests; unpinned requests passed eight of eight. |

## When to use it

Below the context window, not compacting is cheaper than any strategy here: the whole conversation stays cached. These strategies pay off once a conversation outgrows its window, where the framework's own strategies either lose the facts in the tool results or re-bill the prompt on every turn. The measurements behind that, and what each strategy does at the edges, are in the [compaction documentation](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/README.md).

## What it depends on

The strategies call `agent_framework._compaction`, the framework's private compaction helpers, for grouping, token annotation and the exclusion flags. Nothing public exposes those, so the dependency on `agent-framework-core` is pinned to one minor and moved only after the new minor has been read against.

## Licence

MIT. See the [LICENSE](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE).
