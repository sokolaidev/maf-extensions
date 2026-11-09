# maf-cachebench

[![PyPI](https://img.shields.io/pypi/v/maf-cachebench)](https://pypi.org/project/maf-cachebench/) [![Python](https://img.shields.io/pypi/pyversions/maf-cachebench)](https://pypi.org/project/maf-cachebench/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafCachebenchExperimentalWarning`.

A benchmark for [Microsoft Agent Framework](https://aka.ms/AgentFramework) compaction strategies. It measures what each strategy costs in provider prompt-cache hits and what it destroys while doing it: the framework's own strategies and the ones in [`maf-compaction`](https://pypi.org/project/maf-compaction/), twenty in all, on the same conversation, priced at the provider's cached and uncached rates.

This is an independent package. It is not affiliated with or endorsed by Microsoft.

## What it measures

Provider prompt caches match on an exact prefix, and every compaction strategy rewrites history inside that prefix, so compaction breaks the cache by construction. The question is what that costs and what it saves. The benchmark plants facts in a conversation's tool results, runs the conversation under each strategy, and then asks for the facts back from the compacted context. Each strategy gets a cost, a cache hit rate, and a count of facts it kept, and the strategies are ranked on cost behind an accuracy bar.

Two harnesses answer two questions:

- `cachebench` replays a scripted transcript. Every provider and strategy replays the same scripted structure, with a fixed-width per-cell salt isolating its cache prefix. These controlled workloads support comparisons across providers. It answers how a *prompt* caches.
- `cachebench_live` drives a real agent, with real replies and real tool calls, so compaction acts on the history an agent would accumulate. Its numbers compare strategies within one model. It answers what a strategy costs an agent.

## Install

```bash
pip install maf-cachebench[openai]        # Azure OpenAI, OpenRouter and the Azure Responses route
pip install maf-cachebench[foundry]       # Microsoft Foundry project endpoints
pip install maf-cachebench[mistral]
pip install maf-cachebench[ollama]
pip install maf-cachebench[tiktoken]      # then pass --tokenizer tiktoken for exact counts
```

Five commands land on the path: `cachebench`, `cachebench_live`, `cachebench_advise`, `cachebench_recall` and `cachebench_summary`. Each prints its own `--help`.

## Pointing it at a provider

A provider is selected as `provider` or `provider:model`, so two models on one provider can sit in one run. Credentials come from the environment:

| Provider | Variables |
|---|---|
| `azure` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_CHAT_COMPLETION_MODEL` |
| `azure-responses` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_RESPONSES_MODEL`, and a working `DefaultAzureCredential` (Entra authentication) |
| `foundry` | `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`, and a working `DefaultAzureCredential` |
| `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`; pin `OPENROUTER_PROVIDER_ORDER`, or you measure the router |
| `mistral` | `MISTRAL_API_KEY`, `MISTRAL_CHAT_MODEL` |
| `ollama` | `OLLAMA_MODEL`, optionally `OLLAMA_HOST` and `OLLAMA_API_KEY`; this provider caches and never reports it |

Prices are passed as `--price-input`, `--price-cached` and `--price-output` per million tokens, except on OpenRouter where they are discovered.

## A first run

```bash
# see the plan and the prompt sizes without spending anything
cachebench_live foundry:gpt-5.6-luna --dry-run --strategies none,anchored,tool_summary_anchored

# a live cell: a 120K window, a conversation sized to 1.5 times it, five seeds, records kept
cachebench_live foundry:gpt-5.6-luna --agent harness --context-window 120000 --fill 1.5 --repeats 5 \
  --strategies none,tool_summary_anchored,tool_and_user_summary_anchored --summarizer-provider foundry:gpt-5.6-luna \
  --price-input 0.20 --price-cached 0.02 --price-output 1.20 --results-jsonl runs/cell.jsonl

# render the table again from the records, calling nothing
cachebench_live --from-jsonl runs/cell.jsonl
```

`--results-jsonl` is not optional in practice: a cell runs for hours, and the file is appended one record per seed the moment it is scored, so a stopped run keeps what it paid for. `--from-jsonl` rebuilds the table and the verdict from the records with no provider configured, and files that measured the same cell merge into one table.

## Compaction controls and session experiments

`cachebench_live` supports `maf-compaction>=0.12.0,<0.13`, including the `SessionPersistence` API used by record-bearing `roundtrip` and `restart` experiments and the per-part media selector. Persistence support is checked before any model calls.

```bash
cachebench_live foundry:gpt-5.6-luna --dry-run \
  --strategies none,tool_and_user_summary_anchored --summarizer-provider foundry:gpt-5.6-luna \
  --preserve-prompt-cache --session-mode restart --history-mode replay --stream \
  --max-summary-input-tokens 12000 --summary-input-policy chunk \
  --summary-options-json '{"max_tokens": 1024}' --summary-timeout-seconds 60
```

| Area | Controls |
|---|---|
| Recall | `--preserve-prompt-cache`, `--record-instruction`, `--no-supports-tool-choice`, `--forced-call-timeout-seconds`, `--min-groups-before-repeat` |
| Summary input | `--max-summary-input-tokens`, `--summary-input-policy skip\|chunk`, `--chunk-cache-size` (zero disables the cache) |
| Summary requests | `--summary-options-json`, `--retry-without-summary-options`, `--summary-timeout-seconds` |
| Summary backoff | `--max-consecutive-summary-failures` and `--summary-backoff-seconds`, set together |
| Summary prompts | `--user-summary-prompt`, `--user-fold-prompt`, `--record-merge-prompt` |
| Composed overflow | `--overflow-policy shed\|raise`, `--estimate-margin` |
| Request lifecycle | `--session-mode memory\|roundtrip\|restart`, `--history-mode incremental\|replay`, `--stream` |
| Simulated storage | `--persistence-scenario none\|fenced\|stale\|branch\|migrate\|invalid_migration\|after_save\|save_failure\|after_save_failure` |
| Synthetic host hooks | `--media-profile none\|preserve\|describe\|decline\|select_pdf`, `--media-tokens`, `--result-rewrite-profile none\|preserve\|invalid` |

`roundtrip` serializes and reloads the whole framework session between requests. `restart` also reconstructs the agent and compaction objects. Record-bearing strategies use the native persistence adapter with detached JSON snapshots and an exclusive local lease, saving only completed requests; other strategies restore their available decision state. These experiments use an in-memory store and rebuild objects in the same process: they do not measure disk latency, process crashes or distributed locking. `replay` resubmits the original conversation, including previously summarized messages, so replayed summaries and restored recall identities are exercised without duplicating stored history. `--stream` consumes the complete response within the persistence lifetime.

Storage scenarios require `roundtrip` or `restart` and apply to recall-bearing strategies. Other rows, including the required `none` control, keep their ordinary local restoration and report `storage_evidence=not_applicable_no_recall_stack`. Applicable rows report events, commit outcome and notification outcome in `compaction_diagnostics.persistence_requests`, including when a request fails. `branch` saves each request under a new ID and leaves its source snapshot intact; live probes branch independently from the seed snapshot. Fault scenarios stop the affected row after the injected failure. A post-save failure can leave a committed snapshot, so request success and commit success are recorded separately.

The host-hook profiles are synthetic fixture policies. `describe` supplies a fixed description without fetching media or calling a model; `decline` returns no description; `select_pdf` describes PDFs and preserves other media turns while allowing later turns to compact. `preserve` installs only the media estimator. `--media-tokens` is a host estimate per part, never billed provider usage. The result `preserve` profile authorizes shortening a single text item while retaining its metadata and non-text items; it declines multiple text items or raw provider representations. `invalid` deliberately drops media to exercise validation. These policies are for fixtures and controlled experiments; their fixed descriptions do not interpret arbitrary user media. Defaults install none of these hooks.

### Offline capability scenarios

`cachebench_capabilities` supplies the content, model responses and storage faults needed to exercise these paths without credentials or external I/O. Persistence cases use the same agent builder and session runner as `cachebench_live`; media and result cases use the shared strategy builders directly. The command writes one JSON object per scenario and exits nonzero if any contract check fails. Expected injected failures pass only when their observed commit, cleanup and ordering outcomes match the contract.

```bash
uv run cachebench_capabilities --scenario all --output capabilities.jsonl
uv run cachebench_capabilities --scenario all --stream --output capabilities-stream.jsonl
uv run cachebench_capabilities --scenario branch --session-mode roundtrip --history-mode incremental
uv run cachebench_capabilities --scenario media_select_pdf --media-tokens 800
uv run cachebench_capabilities --scenario result_invalid
```

Output files must not already exist. Each row identifies itself as `cachebench_capability`, schema 1, with `offline_simulated` evidence, fixture version, package/Python versions, resolved controls, observations and individual checks. These records are separate from cost/cache benchmark records and must not be averaged with them. Live records use schema 24 for the new profile settings; the reader still accepts schemas 2–23, and omitted profile settings retain their default behavior.

| Scenario | Observation |
| --- | --- |
| `fenced`, `stale` | Non-None token forwarding and atomic simulated stale-token rejection |
| `branch` | Separate source/destination IDs, unchanged source, retained history and restorable compaction state |
| `migrate`, `invalid_migration` | Migration before validation, including rejection before model work |
| `after_save`, `save_failure`, `after_save_failure` | Post-save notification inside the lease, failure before commit, and failure after commit |
| `request_failure`, `interrupted_stream` | Failed model response or early consumer exit closes the stream and leaves storage unchanged |
| `media_preserve`, `media_describe`, `media_decline`, `media_select_pdf` | Protected/selected media, description input, retained source parts and estimated tokens before/after compaction |
| `result_preserve`, `result_rewrite`, `result_invalid` | Default protection, authorized shortening with retained labels/media, and rejected invalid rewriting |

`--stream` applies to persistence cases. `all` always exercises `interrupted_stream` with streaming enabled; selecting that case alone requires `--stream`. Media/result cases apply the strategy directly and do not measure provider streaming. The persistence model fixtures report no billable usage. No result here qualifies real storage durability, distributed lease enforcement, provider cache behavior, model quality or live multimodal handling. Live provider measurements still require `cachebench_live` and suitable provider/workload qualification.

Programmatic callers pass `CompactionOptions(...)` through `StrategyOptions(compaction=...)`. Existing record, user-summary, fold, coverage and chain controls remain available. Each option and the installed CacheBench, compaction and framework versions become part of the recorded cell identity. Schema 23 records also contain `compaction_diagnostics`, including forced-call failures, ignored pins, summary replay/skips and final recall-disabled state. Older records remain readable with unknown new settings and diagnostics; they are grouped separately.

Each completed provider attempt is metered, including an attempt rejected and retried by recall middleware. Independent probes restore the complete supported strategy, middleware and gate state, including pin disablement and summary backoff. Costs from older schemas can undercount internal retries and should not be pooled with new runs.

## Reading the table

Rows that keep at least `--min-correctness` of the control's accuracy come first, cheapest first; the rest follow below a line. The columns to read, in order:

- `seed$` is what the conversation cost, the strategy's own summariser calls included and the probes left out. The ranking and the verdict are on it.
- `seed$+-` is the spread between the cheapest and dearest seed. A gap smaller than this is not a result, and the verdict line says `NOT SUPPORTED` when that happens.
- `seed hit%` is the share of the conversation's input the provider served from its cache, probes excluded. This is what compaction did to the cache.
- `facts` and `lost` are the planted facts that survived into the compacted context, and the ones compaction removed.
- `acc1` and `acc2` are the model's accuracy on the scoped and the combined closing questions, asked from the same restored snapshot.
- `dq` marks a row that sent a prompt larger than the window it stood in for. A cell that disqualifies at all leaves the ranking.
- `flags` is where a row says it is not measuring what its name claims. Read it before the money columns.

The full option reference, the flags legend and the measurement design are in the [benchmark documentation](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/cachebench.md), and the findings in the [compaction overview](https://github.com/sokolaidev/maf-extensions/blob/main/docs/compaction/README.md).

## What it depends on

`agent-framework-core`, pinned to one minor because the benchmark reads the framework's private `_compaction` helpers; `maf-compaction` for the strategies it was built to measure; and `httpx` for the advisor's price lookup. The provider clients are extras.

## Licence

MIT. See the [LICENSE](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE).

All commands default to the dependency-free estimator. Install the `tiktoken` extra and pass `--tokenizer tiktoken` for BPE counts. Explicit price rates must be finite and non-negative.
