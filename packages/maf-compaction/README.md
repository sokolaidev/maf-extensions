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

Last-resort shedding keeps a completed turn's final text-only reply when its text is at most 64 tokens under the configured tokenizer. Longer final replies leave an explicit marker that the preceding user request was already answered, so removing the tool exchange does not make the request look unanswered. Intermediate narration and tool exchanges retain neutral markers. These rules also apply to the composition's anchored fallback and session replay. Kept replies still count against the budget; protected content that cannot fit remains an overflow rather than being silently removed.

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

The composed strategy defaults to `overflow_policy="shed"`. After its existing merge, rewrite and protected fallback steps, it may remove whole unrecorded tool groups. Once shedding starts, its target is the effective message budget minus `ceil(chain_gain_fraction * tokens_behind_first_removal)`, with those tokens measured before shedding. This leaves room for subsequent calls at the cost of losing more unrecorded history in one pass; `chain_gain_fraction=0` stops at the budget. Protected material can prevent reaching the target without causing an error when the remaining prompt fits the budget. A WARNING names the lost message IDs, and removal markers tell the model that history was shed. These decisions replay on stored history and across session restores. Use `overflow_policy="raise"` to keep uncovered results instead. Both policies raise `CompactionBudgetExceeded` locally if the remaining anchors, records or other protected messages cannot fit. The host must handle that error by reducing protected context or choosing a larger budget; it is not a successful model response. The budget counts messages with your tokenizer: reserve additional room for tool schemas and provider overhead.

Without a record, the composition also uses this overflow policy: it keeps tool results intact while they fit, then sheds whole eligible groups with the same headroom target. It does not shorten results as they age out of the tail. The standalone record strategy retains its configured give-up fallback at `fallback_fraction`. On a cache with only system and end-of-request breakpoints, an edit anywhere before the previous request end can miss back to the system prefix; avoiding repeated tail-exit edits is therefore necessary even when each edit is near the end.

Named `tool_choice` support lets the middleware pin recall. Routes that reject it can use a temporary recall instruction instead; neither mechanism proves that a record was written. Reports in [#1872](https://github.com/sokolaidev/maf-extensions/issues/1872) describe OpenRouter routes for DeepSeek V4 Flash and GLM 5.3 Flash ignoring the pin with reasoning enabled, while GPT 6 Luna and Tencent Hy3 honoured it. Those are reported observations, not provider qualification by this package; verify the selected route and reasoning settings.

### Optional instruction on pinned record requests

Some models keep only titles or reply formatting in their record even when the recall tool asks for values. Pass `record_instruction=DEFAULT_RECORD_INSTRUCTION` (imported from `maf_compaction`), or your own string, to `ToolResultRecallMiddleware` to append a user message after the loaded history on pinned recall requests only. The default is `None`, which leaves requests unchanged. The instruction does not change prompted recall, including `preserve_prompt_cache=True` and the fallback for routes without tool-choice pinning. It is removed on completion, failure and cancellation, and is absent from ordinary requests, stored history and compaction snapshots. As with prompted recall, hosts taking independent copies of in-flight message lists must filter messages whose `additional_properties["_maf_recall_prompt"]` is true before displaying or persisting them.

`DEFAULT_RECORD_INSTRUCTION` asks for tool-returned information, excludes earlier assistant text, explains that titles and URLs alone are insufficient, and retains exact numbers, limits, quotas, sizes, selected table rows and identifiers with their headings while keeping access and confidentiality restrictions. This can increase output cost, latency and the uncached share of requests; select it per model. Appending it leaves the earlier request prefix unchanged.

Size the record deliberately: value-bearing records may need much more than the default 4,000-token response cap. Set `make_recall_tool(target_tokens=...)` and the strategy's `record_target_tokens` to the same desired target, and set `record_max_tokens` above that target with room for JSON and provider overhead. For example, an 8,000-token target with a 16,000-token cap is a starting configuration to measure, not a guarantee of fit. Enabling the instruction changes none of these numbers automatically. A cap truncates output rather than shortening it. A length-limited response without usable recall values retries as an ordinary call and queues a shorter record request for a later call; other malformed arguments disable recall with a `failed` warning carrying the exception and cause chain. Monitor `records_truncated` as well as failures and coverage diagnostics.

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

Provider hooks resolve from their `session` argument, chat middleware from `context.session`, and the registered tool from the framework-injected `FunctionInvocationContext.session`. Each phase sets a scoped context variable and clears it on exit. No session identifier is a model-supplied tool argument, and missing sessions are refused. The inner middleware retains its single-session guard.

Persist the whole `AgentSession` using the framework's session serialization and restore it before the next request. The registry reads session state even on cache hits and writes it after provider, middleware and tool phases, including exceptional exits. Cache eviction and `max_cached_sessions=0` discard only cached stacks; the session retains its decisions. Removing the registry's state entry resets its stack. Use a distinct `state_key` for each independently configured registry and keep the builder configuration stable across restores; unsupported versions or stack-kind mismatches raise instead of silently dropping decisions. New snapshots use version 5 with versioned recall identities, media descriptions and media-aware summary replay keys. Compatible versions 1 through 4 remain readable, with legacy transcript-keyed kept folds migrated to summary-ID keys. Unversioned `call:` identities in a composed snapshot's `anchor` or `declined_behind` are rejected before restoration because their pending recall decisions cannot be mapped uniquely to occurrences. Resume those sessions with the original reader or explicitly rebuild from retained history after reviewing pending decisions; do not automatically discard rejected state. Older readers reject version-5 snapshots. Replicas sharing a session must run a schema-compatible build before a version-5 snapshot is written.

The snapshot carries record decisions and outstanding re-force asks; the chain's wait, kept replacements, shed IDs and refusals; remembered user-summary requests and kept folds; pending recall and awaiting-record flags; and the one-shot gate permission. User compaction and fold counts also travel because they allocate summary IDs. Other counters remain local diagnostics and restart after cache eviction or a replica switch. `ChainDecisions.to_dict()` and `RecordDecisions.to_dict()` serialize the existing narrower replay snapshots; `CompactionStack.to_dict()` includes the content-keyed state as well. Kept folds and record replacements also retain their source counts, so replay validates the selected prefixes without rendering every possible prefix. Record decisions retain the transcript and source count together, so differently sized sources with identical numbered text retain separate answers. Legacy transcript maps migrate on restore, and entries without counts learn them when their transcripts first match. Refusals also retain source counts; ambiguous legacy refusals without counts are discarded on restore so they can be retried. Content-keyed entries contain conversation text and can grow with the conversation, so retain them under the same storage and retention policy as history.

Serialize requests for the same session in the host, including across replicas. The registry supports concurrent distinct sessions and rejects overlapping independent phases for the same session in one process; it does not supply a distributed lock or persist to an external store. Model quality, multi-process storage coordination and recovery from a crash between a model response and the host's session save are outside this adapter's guarantees.

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

Only successfully completed requests are saved. Streaming requires `async with` and one consumer; full iteration or `get_final_response()` completes the response, including its final validation and provider hooks. The context closes the stream before saving and releasing the lease. An unused context never constructs the agent stream. Breaking early, explicitly closing an incomplete stream, leaving it unused, cancellation before saving, or a request/validation/cleanup error leaves the durable snapshot unchanged. A consumption failure prevents saving even if caught inside the block. Do not keep the stream or spawn consumers that outlive the block. Abandoning the entire context without closing it or terminating the process has no cleanup guarantee.

Storage and serialization failures propagate. A streaming save failure can arrive after output has reached the caller. Once saving starts, cancellation, a lost acknowledgement or a lease-release failure can surface after the backend committed; the adapter does not retry model work. The host must define recovery for that uncertainty and for a crash between model work and saving. Failed or abandoned requests discard their in-memory compaction changes, including newly accepted summaries, so a later retry may repeat that work. Completed requests that internally recover from a refused recall still save their validated state; rejected history does not bypass the existing recall or enclosing agent gates.

Use this as the single writer of the whole session snapshot. History providers can update the in-memory session during the request, but independent external session/history stores must participate in the host's lease and consistency policy. The adapter does not roll back tool side effects, provider-side conversations, or custom external writes. For hosts that already own the complete session lifecycle, the existing registry/provider/middleware wiring remains sufficient; do not add a second load/save owner. `SessionPersistence` is an outer request adapter rather than chat middleware because returning a `ResponseStream` from middleware does not mean its consumption, validation, or persistence has finished.

The user strategy's `max_summary_input_tokens` bounds each summarizer request, including instructions and the numbered transcript, using the configured tokenizer. It defaults to half `max_input_tokens` (at least one token) and also bounds user-summary folds and the composition's record merges and harder rewrites. Set it for the summarizer's own input budget when that differs from the main model, reserving provider overhead and output space separately. A pass replaces only the oldest whole turns, summaries or records that fit; newer material stays for later passes. If no eligible prefix fits, the oldest item is retained without truncation or a request; a fold or merge needs at least two items to fit. The minimum user-band share is checked on the eligible band before limiting the request, so long histories can still make progress in bounded passes. Ordinary fold mode checks its break-even rule on the selected summary prefix; later summaries cannot fund that fold. The composition's emergency folds keep their smaller-result rule without this profitability gate. A partial replacement is kept only when it shrinks what it replaces; standalone compaction can still leave an oversized conversation, and the composition retains its configured overflow policy.

Set `summary_timeout_seconds` to a positive finite number to time out each user-summary or fold request. A timeout cancels the request and counts as a failure; the client must honour cancellation, and cancellation cleanup can extend the elapsed time. Configure `max_consecutive_summary_failures` (a positive integer) together with `summary_backoff_seconds` (positive and finite) to skip fresh requests after that many consecutive failures. Exceptions, empty replies and rejected oversized summaries contribute to the streak. After the cooldown expires, the next eligible request retries; another failure renews the cooldown, while a fresh accepted summary resets the streak. Cached answers still replay during cooldown without resetting it. `user_summary_skips`, also exposed by the composition, counts withheld requests separately from `user_summary_failures`. All three settings default to `None` (off). Session snapshots retain the failure streak and a wall-clock cooldown deadline, so the cooldown follows a session across replica changes, restarts and stack-cache eviction when the host persists its state. Older snapshots default to no failures or cooldown. Expired deadlines or deadlines more than the configured backoff ahead are discarded, limiting the effect of clock differences between hosts. These settings cover user bands and user-summary folds, including emergency folds; record merges/rewrites and media-description hooks retain their own policies. A skipped or timed-out summary leaves its source material intact, and the composition continues to its configured overflow policy.

Hosts that supply the original transcript instead of loading stored history must still persist the framework session. Accepted user summaries and folds survive in compaction state independently of `remembered_requests` and `chunk_cache_size`. Before checking the user trigger, the strategy restores each accepted edit only when its source window still matches uniquely, in order, by message identity and content, and the selected turns remain eligible. Keep stable message IDs and immutable or versioned media references. Edited, removed or reordered sources are not replaced by a stale summary. Replay also works below the trigger and during cooldown; new trailing turns can then form a new band. `user_summaries_replayed` counts restored replacements, so one pass may increment it more than once. Retained edits contain summary text and source fingerprints and follow the session history retention policy. Legacy snapshots without these edits retain their existing exact-request replay behavior until a new edit is accepted.

## Per-call request budgets and chunked summaries

The composition's sending ceiling is `floor(max_input_tokens * (1 - estimate_margin)) - request_overhead`. `estimate_margin` defaults to `0.0`; set, for example, `estimate_margin=0.05` on `ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy` to reserve five percent of the input budget for estimation error. The overhead is the instructions and serialized tool schemas in the current chat options, counted with the composition's tokenizer. Schema token counts are cached by their serialized contents, so changing a description or schema changes the count. This remains an estimate: reserve output space in `max_input_tokens` and account separately for provider-specific framing and other request fields.

`ToolResultRecallMiddleware` supplies the live options when the per-call `compaction_strategy` is the composition, including the harness's `before_compaction_strategy`. Session wiring uses `SessionToolResultRecallMiddleware(registry, compact_before=True)`. Custom integrations can call `await strategy.compact_before(messages, options=context.options or {})`. Calling `await strategy(messages)` directly, including an after-phase history pass, keeps the configured history budget and does not subtract request overhead or margin. Keep the middleware on calls with no tools too: their smaller overhead leaves more room for messages. Function tools, callables and dictionary tool specifications are supported; unknown tool objects raise rather than silently count as zero.

A refused sending-budget estimate during post-call recall bookkeeping leaves the completed response intact and queues no new pin. Before-call compaction still enforces its sending ceiling; hosts choosing to send anyway can handle that refusal before contacting the model.

Set `summary_input_policy="chunk"` on `UserTurnAnchoredSummarizationCompactionStrategy` to cover the entire eligible user band when its request is too large. The default `"skip"` retains the existing bounded-prefix behavior. Chunk mode closes chunks at turn boundaries when the next turn would exceed `max_summary_input_tokens`, then summarizes each with the user-summary prompt. It merges the partial summaries with `fold_prompt`, in bounded batches until one summary remains. A chunk whose answer is no smaller keeps its original text for the merge, including a short final turn. A single oversized turn, an empty answer, or partials that cannot fit a reducing merge leave the band intact and count as one failure. Standing-summary folds and record merges retain their existing bounded-prefix behavior.

Closed chunk requests keep identical bytes as turns are appended to the same band. An independent LRU cache retains up to `chunk_cache_size=128` successful chunk and merge answers; set zero to disable it. The cache survives session snapshots and can reuse completed chunks after a failed pass. The final answer still must shrink the band before any source is excluded. `summary_timeout_seconds` and the failure cooldown apply once to the entire chunked pass, including merges and retries, rather than once per model request.

Optional `summary_options={"max_tokens": 2000}` passes chat options to user-band summaries, chunk merges, standing-summary folds, record merges and record rewrites. All of these requests share the same options and retirement state. With `retry_without_summary_options=True`, an exception or empty response with those options retries once without them and retires those options for the rest of the strategy instance's life. Cancellation and deadline expiry do not trigger that retry. The default is no options and no retry; the disabled-options verdict is instance-local, not session state.

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

A pure `estimate_media(content) -> int` callback can override both fixed costs. Estimates must be positive integers; they are not provider usage measurements. `count_message_tokens(message)` exposes the estimate to host code. `count_tokens(text)` delegates to the underlying text tokenizer; framework token gates outside this package retain their own accounting. The composition can reserve instructions and tool schemas per sending call as described above; reserve other provider overhead separately.

To compact media-bearing user turns, pass an async `describe_media(content, message) -> str | None` hook to `UserTurnAnchoredSummarizationCompactionStrategy`. It receives copies of the media part and full user turn. Return a description or `None` for an unsupported part. The host chooses and calls the vision, transcription or document service, including its timeout and request/output limits. The strategy retains part order and marks descriptions as inferred context, then applies `max_summary_input_tokens` to the complete summarizer request. Host-preserved turns and unknown non-text types remain protected. Failed, empty or oversized descriptions retain the turn; cancellation propagates. The original turn is excluded only after accepting a smaller summary.

Successful descriptions persist in session snapshots and are reused across loaded copies, stored history and replica switches. Identities include message ID, ordered content and surrounding text, so different media do not reuse an answer merely because their text matches. Keep message IDs, media references and hook configuration stable across restores. Use immutable or versioned URLs: the package does not fetch them to detect remote changes. Descriptions are lossy conversation data; retain original media separately when it must remain recoverable. If protected media cannot fit, the composition raises `CompactionBudgetExceeded`. See the [media design decisions](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/strategies.md#media-handling).

## Provider support for forced recall

`ToolResultRecallMiddleware` stops requesting recall for a session after two consecutive forced responses contain no `recall_earlier_tool_results` call. A response containing that call resets the streak; ordinary responses do not. The two ignored responses still carry the record cap, but subsequent requests retain the caller's own options. Existing records remain usable; the composition stops waiting for a new record and continues through its user half and configured anchored fallback/overflow policy.

When a pinned request raises `ChatClientException` (including its subclasses) with `tool_choice` in its message or explicit `__cause__` chain, the middleware records `pin_disabled_reason="unsupported"` and retries the recall request with `tool_choice="auto"` and a temporary user message at the end of the request, after the last tool result or user question. That request advertises only `recall_earlier_tool_results`; the caller's system instructions stay unchanged. This mode persists for later recall requests and across session restores. Set `supports_tool_choice=False` on the middleware to select it up front. The gate, record cap, targeted re-ask guidance, argument validation, stream buffering and backoff still apply. The trailing message is removed from middleware-owned lists after completion, failure or cancellation, and the caller's tools and options are restored for ordinary calls. Messages flagged by `additional_properties["_maf_recall_prompt"]` are stripped at middleware entry and again after downstream history loading, before compaction and model execution, so retained copies cannot become stale instructions on later requests. Framework layers and hosts can copy a prompt before cleanup; hosts that persist or display complete agent message lists must exclude messages whose `additional_properties.get("_maf_recall_prompt")` is truthy, including when replaying older checkpoints. Middleware cleanup cannot retroactively edit those independent copies. Prompt recall depends on the model following the instruction; this package's offline Ollama adapter checks do not establish live recall quality.

Set `preserve_prompt_cache=True` to ask by prompt without changing the request prefix. The forced call is never pinned and keeps the caller's own `tools` and `tool_choice`; only the record cap (`max_tokens`) changes, which is not part of a provider's prompt-cache key. The ask is the same temporary trailing message, and re-ask guidance is appended to it instead of to a per-call copy of the recall tool's description. Providers that cache by exact prefix, such as Anthropic, invalidate every cached message block when `tool_choice` or thinking changes and match nothing when `tools` changes, so a pinned or recall-only ask rewrites the whole conversation at the cache-write price; with this option the ask reads the previous request from cache. The session records `pin_disabled_reason="prompt_cache"` on its first call and logs it at INFO, since the mode is configured rather than a failed pin. It takes precedence over `supports_tool_choice`. With every tool on offer the model may answer with another tool instead of a record; the two-ignored-asks rule above bounds that, and the value-free and failure handling are those of the prompted mode.

A call without the recall tool or with `tool_choice="none"` keeps a pending recall ask for a later tool-enabled call. It does not count as an ignored pin. The dictionary form `{"mode": "none"}` is treated the same way.

A forced response must carry recall arguments as a JSON object with a `values` field. The advertised schema remains a string. List values are normalized by joining string items with newlines and JSON-serializing other items; object values are JSON-serialized with Unicode preserved. Normalized arguments reach both the completed response and buffered stream updates before tool execution. Except when the response ends with `finish_reason="length"`, malformed JSON, non-object arguments, missing `values`, and scalar values other than strings are handled as failed forced calls. On non-truncated responses, empty lists, empty objects, and empty or whitespace-only text count toward `value_free_record_streak` once per forced response, including across session restores. The recall tool consumes its gate permission but returns no record marker for blank text, so the response cannot anchor removal or count as a recorded result. Set `forced_call_timeout_seconds` to a positive finite number to bound the complete forced attempt, including stream consumption and finalization. Timeout cancels the attempt and takes the same unpinned retry path; cancellation cleanup may extend elapsed time. This option defaults to `None`.

A forced response ending with `finish_reason="length"` is counted before argument validation. If it has no recall call, malformed arguments or blank values, the middleware discards the response and its buffered updates, drops deferred framework-managed history writes, disarms recording and retries once with the original options. It queues another recall ask with a shorter target (one quarter of `record_max_tokens` when configured), preserving that ask and guidance across session restores. Truncation does not increment failed, ignored or value-free counters and does not disable recall. Parseable nonblank values remain usable under the normal coverage checks; the length flag alone does not grant removal authority.

A forced request that raises any other ordinary exception, including during stream consumption or finalization, disables recall immediately and retries once with the original request options, including an absent `max_tokens`. A failure of the prompt fallback takes this same ordinary retry path; a failure of that ordinary retry propagates. The forced deadline covers both the pin and its prompt fallback. Cancellation and framework middleware abort signals propagate without another retry or a new provider-support verdict, leaving the recall ask queued. Forced streams, including prompt fallback, are buffered through completion before their updates reach the caller, so a failed attempt cannot expose partial text or tool calls. Ordinary responses and ordinary retries keep normal streaming behavior. This can delay the first visible update from an ignored forced request.

The recall request and tool description ask for task-relevant information from tool results, explicitly excluding earlier assistant text. Exact identifiers, numbers, paths and other unreconstructable values are retained alongside the meaning of tool-returned findings, conclusions and summaries. Access and confidentiality restrictions still apply. A re-ask advertises a per-call copy of the tool description naming the missed results by tool name, call ID and message ID, without adding a message or mutating the registered tool. `records_without_values` on the record strategy and composition counts distinct observed records quoting no distinctive value from eligible target groups; records with no distinctive target values are not counted. When bound to recall middleware, two consecutive eligible value-free records disable forcing for that session. A record quoting any target value resets the streak while forcing is enabled; a nonblank record with no eligible target values leaves it unchanged. Blank forced responses advance the streak independently of target eligibility and are not included in `records_without_values`, which counts observed record anchors. Re-reading a record does not advance the streak, including after restore. This check does not guarantee that a record preserved every fact.

A forced response carrying the framework's `model_output_kind="refusal"` text marker is rejected as a whole, even if it also contains complete recall arguments. The middleware recognizes both the `core` namespace and the legacy flat marker, checking buffered stream updates as well as the finalized response. It disarms recording, increments `failed_forced_calls` and `value_free_record_streak` once, and retries once with the caller's original options. Refused tool calls and stream updates are not returned for execution. Framework-managed per-call history writes wait for recall validation, and accepted writes remain subject to any enclosing persistence guard. This uses the pinned MAF 1.22 persistence hooks; custom persistence outside those hooks and provider-side conversation storage are not rolled back. Two consecutive refused/blank responses or eligible value-free records stop recall with reason `"value_free"`, including across restores. A successful record with target values resets this shared streak. Ordinary responses outside a requested recall are unchanged; plain text is not classified by wording.

The strategy retains the pending recall's distinctive target tokens before compaction can shed their groups. These tokens survive session snapshots until the next recall answer is judged, so overflow cleanup cannot erase the basis for value-free backoff. Consolidation replacements do not count as recall answers, and unjudged outcomes survive replay and session restores. They support judgment only and never authorize removing a group. Older snapshots without saved targets use the remaining eligible groups. `last_record_value_judgment` on the record strategy reports `has_values`, `value_free`, `no_candidates`, or `no_values`, and is `None` before a local observation. Unjudged records emit an INFO message with `no_candidates` or `no_values`, without record text or target values.

Hosts can inspect `pin_disabled`, `recall_disabled`, `pin_disabled_reason`, `value_free_record_streak`, `ignored_forced_calls`, and `failed_forced_calls` alongside `forced_calls` and `records_truncated`. The module logger `maf_compaction._toolsummary` emits a WARNING on each disablement transition with the bound session id (`None` when no session is bound) and reason; the configured `"prompt_cache"` transition is logged at INFO instead. A `failed` warning also includes the exception traceback and explicit cause chain, including provider error text or the reason recall arguments were rejected; messages and tool arguments are not logged separately. The reason is `"unsupported"`, `"prompt_cache"`, `"ignored"`, `"failed"`, or `"value_free"`; it is `None` while enabled or when restoring a legacy snapshot without a reason. `"unsupported"` and `"prompt_cache"` disable only pinning; `recall_disabled` stays false until recall stops entirely. Forced counters include prompt requests; the unsupported-pin negotiation is not counted as a failed recall attempt. An `ignored` warning also reports `mode=pinned` or `mode=prompted` and classifies the final response as `text`, `other_tool`, `other_tool+text` or `other_or_empty`, without tool names or arguments. Length-limited responses count in `records_truncated`, not the ignored counter. The disabled flag, reason, both streaks and identities of evaluated records persist in session snapshots; diagnostic counters remain local to the stack. Older readers conservatively stop recall on the unsupported state. Reset the session's compaction state to try pinning again after changing providers; legacy `"failed"` sessions are not automatically reclassified. `forget_pending()` clears queued work, not the session's provider-support verdict.

The following observations were reported in [#1876](https://github.com/sokolaidev/maf-extensions/issues/1876) from OpenRouter probes on 2026-10-12 with maf-compaction 0.2.0 and agent-framework-core 1.20. They are historical route observations, not a current compatibility guarantee or a built-in model allow-list.

| Route | Reported behavior |
| --- | --- |
| DeepSeek V4 Flash; GLM 5.3 Flash, reasoning enabled | Returned plain text despite the named tool choice, streamed and non-streamed. |
| GPT 6 Luna | Honoured the named tool choice in those probes. |
| Tencent Hy3 | Honoured the pin in initial probes, but streamed reasoning at about 65k prompt tokens errored on two of three forced requests; unpinned requests passed eight of eight. |

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
