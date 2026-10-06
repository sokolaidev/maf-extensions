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
