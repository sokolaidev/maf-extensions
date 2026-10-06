# Compaction that keeps the prompt cache

`maf-compaction` is a set of compaction strategies for Microsoft Agent Framework (MAF) shaped around the provider's prompt cache. It exists because every compaction strategy the framework ships was measured costing more than not compacting at all, and the reason is structural rather than a matter of tuning.

## Introduction

### Microsoft Agent Framework (MAF)

[MAF](https://github.com/microsoft/agent-framework) is an open-source framework for building AI agents. An agent keeps a conversation history and sends it to the model on every turn; when that history outgrows the model's context window, a *compaction strategy* decides what to drop, shorten or summarise so the next request fits. The framework ships several: a context-window strategy that evicts tool results and then truncates, a sliding window, a summariser, and a token-budget composition of them.

### The problem

Providers cache the prompt's prefix and charge a fraction of the input rate to re-read it. The cache matches on an exact prefix, so an edit at position K re-bills everything behind K at the uncached price. Compaction edits the prompt by construction, and the framework's strategies decide from the *current* size of the conversation: a rule such as "when over 80% of the budget, compact down to 50%" re-decides the whole history every time it trips, so the same old message is rewritten differently on turn 12 and turn 15 and the cache is lost from the head each time.

That gives a break-even an edit has to clear. With `p` the input rate, `c` the cached rate, `R` the tokens removed, `B` the tokens behind the edit and `T` the turns remaining, compaction pays when `R > B(p − c) / (p + T·c)`. With twenty turns left that is 29% of the tokens behind the edit. Removing less than that costs more than it saves, however sensible the removal looks.

### What this family does about it

Three constraints follow, and every strategy here is built on them. Decide from a message's *position*, never from the conversation's current size, so the same prefix compacts to the same bytes on every later turn. Make mutations march forward, never backward, so each turn invalidates only the small suffix that newly aged out. And carry *information* forward rather than *positions*: head-and-tail truncation of a tool result keeps the first and last slice of it, which keeps none of the values spread through the middle.

The family holds two packages. [`maf-compaction`](../../packages/maf-compaction/README.md) is the strategies and the middleware they need, written against the framework's `CompactionStrategy` protocol so they attach wherever the framework takes one. [`maf-cachebench`](../../packages/maf-cachebench/README.md) is the benchmark they were measured with: it replays a scripted conversation that plants facts in tool results, asks for them back after compaction, and prices every request at the provider's cached and uncached rates. The measurements below are its, and the records behind them ship with it.

## What was measured

Twenty strategies, the five here and fifteen from the framework, on a harness agent with a 120,000-token window, five seeds per cell, six tool lookups carrying 60% of the tokens and 53 planted facts, on two models. The conversation is sized to 0.9, 1.5 and 3.0 times the window. `seed$` is what the conversation cost, summariser included; a strategy disqualifies (`DQ`) when its prompt goes over the window at any point. The uncompacted control disqualifies past the window and is a price reference there, not a baseline.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
|---|---|---|---|---|---|---|
| none (control) | $0.068 | $0.150 (5 DQ) | $0.443 (5 DQ) | $0.036 | $0.075 (5 DQ) | $0.313 (5 DQ) |
| `tool_and_user_summary_anchored` | $0.073 | $0.143 | $0.792 (1 DQ) | $0.035 | **$0.059** | **$0.109** |
| `tool_summary_anchored` | $0.070 | $0.308 (2 DQ) | $0.942 (5 DQ) | $0.036 | $0.051 | $0.310 (5 DQ) |
| `user_summary_anchored` | $0.085 | $0.221 (5 DQ) | $0.590 (5 DQ) | $0.044 | $0.111 (5 DQ) | $0.301 (5 DQ) |
| `anchored_min_gain` | $0.079 | lost facts | lost facts | $0.034 | lost facts | lost facts |

Every priced cell kept all 53 facts on every seed, and *lost facts* marks a cell that did not; the models are gpt-5.6-luna (5.6) and gpt-6-luna (6). Past the window, the composition is the only strategy that keeps every fact on both models and stays inside the window on all but one seed: on gpt-6-luna 22% below the control at 1.5 times the window and 65% below at 3 times, on every seed; on gpt-5.6-luna 18-35% below on four seeds of five at 1.5 times, and at 3 times it holds on four seeds and fails loudly on the fifth. What decides the row on gpt-5.6-luna is the quality of the record the model writes. The framework's own strategies either lose most of the facts past the window or disqualify.

## When to use which

- **Inside the context window, do not compact.** The best strategy here costs within 3-8% of not compacting, inside the seed spread, and the whole conversation stays cached.
- **Past the window, use the composition.** It is the only strategy that keeps every fact on both models. It is cheaper than an unlimited model in every cell but gpt-5.6-luna at three times the window, where it costs more and overflows on one seed of five.
- **The record strategy alone** suits a conversation whose bulk is tool output and that ends soon after it outgrows the window; it cannot touch user turns, so a conversation that keeps going grows until it overflows.
- **The anchored strategies alone** keep a conversation admissible and the cache intact, but they discard values past the window; use one as the fallback the record strategy needs rather than on its own.
- **The user-turn strategy alone** is never enough, since it cannot reach the tool half; it is the composition's second half.

## Documentation map

| Page | Read it for |
|---|---|
| [Strategies](strategies.md) | Each strategy's mechanism, defaults, wiring, and where it fails |
| [The benchmark](cachebench.md) | Running `maf-cachebench`: providers, every option, reading the table and the flags |
| [Research records](research/) | The design argument, the run write-ups and the test philosophy as written during development |

Package READMEs cover installation and configuration. [Authoring guidance](../AUTHORING.md) defines the documentation structure.
