# Measured results

> **Record.** The run write-ups of the benchmark's development series, in the tense they were written. The final report on agent-framework-core 1.20.0 (run 67) is the one the published numbers come from, and its records and run 65's ship with `maf-cachebench`; earlier runs' records do not. [cachebench.md](../cachebench.md) is the decided tool and the [front door](../README.md) carries the decided numbers.


**Start with the [final report](#final-report-runs-63-and-64-26-september) (runs 63 and 64, 26 September 2026).**

Live-agent runs (`cachebench-live`). Each row of each table is the **median of 3 repeats**; the `+-` column is the spread between cheapest and dearest repeat, and a ranking is only meaningful where the gap between strategies exceeds it.

**Source data.** Runs 7 onward keep their verbatim output and exact invocation in `runs/`, one pair of files per run. The tables below are a selection of those columns, copied rather than recomputed, so any figure here can be checked against the log of the same run number. Runs 1-6 predate that practice and exist only as the tables below.

## Shared configuration

Unless a run says otherwise:

| Setting | Value |
| --- | --- |
| Scenario | 16 turns, 17 planted facts, 6 tool-call groups |
| Tool output | ~4,000 tokens per result, ~49% of all material |
| Filler | ~2,000 tokens per padding turn |
| Simulated context window | 32,000, minus 2,048 reserved for the reply |
| Working budget | 29,952 tokens |
| Tool calls | pinned (see below) |
| Repeats | 3 |
| Temperature | 0 |

**Tool pinning** matters for comparability. Each tool turn forces its own no-argument tool via `tool_choice={"mode": "required", "required_function_name": ...}`, and every other turn is closed with `tool_choice="none"`. Without it, models gather different numbers of facts between runs, which moves both axes for reasons unrelated to compaction. Runs marked *unpinned* below predate this and are not comparable with the pinned ones.

## Runs

| # | Model | Route | API | Pinned | Verdict |
| --- | --- | --- | --- | --- | --- |
| 1 | `openai/gpt-5.6-luna` | OpenRouter | Chat Completions | yes | `none` |
| 2 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` |
| 3 | `gpt-5.6-luna` | Azure Foundry | Responses | yes | `none` |
| 4 | `z-ai/glm-5.3-flash` | OpenRouter | Chat Completions | no | *partly usable* |
| 5 | `z-ai/glm-5.2` | OpenRouter / Crusoe | Chat Completions | no | `none` (5x discount) |
| 6 | `z-ai/glm-5.2` | OpenRouter / BaseTen | Chat Completions | no | `none` (10x discount) |
| 7 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (60K window, harness) |
| 8 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (120K window, harness) |
| 9a | `gpt-5.4-mini` | Azure Foundry | Responses | yes | *aborted — 400K window does not exist* |
| 9 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (272K real limit, 86% full) |
| 10-12 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | *buried arm — retrieval under noise* |
| 13 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (60K, 16 strategies, spread) |
| 14 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (272K, focused, spread) |
| 15 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (anchored, scaled retention) |
| 16 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `tool_summary_anchored` (60K, unsupported) |
| 17 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | *(120K, crossover)* |
| 18 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (272K, `anchored` best on accuracy) |
| 19 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | *(16 calls, confounded — kept as a series point)* |
| 20 | `gpt-5.4-mini` | Azure Foundry | Responses | yes | `none` (16 calls, controlled with code-free asides) |

Runs 7 to 9 are a **separate experiment** on the harness agent at wider windows, with 53 planted facts instead of 17. The shared configuration above does not describe them; their own section below does. Runs 7 and 8 overflow the budget, by 135% and 127% of it measured at the uncompacted peak, so compaction is forced to fire; Run 9 is the opposite regime, with the conversation fitting inside the window at 86%.

---

### Run 1 — `openai/gpt-5.6-luna` via OpenRouter (pinned, backend pinned)

Pricing $0.20/M in, $0.02/M cached (10x), $1.20/M out (6x). Backend pinned to OpenAI with `allow_fallbacks: false` — this model has **five OpenRouter backends spanning $0.100-$0.400/M input**, so unpinned routing can move cost 4x on its own. Every run gathered 17/17 facts with 0 unfetched, no errors.

| strategy | in | hit% | cost | +- | vs none | lost | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| token_budget_truncate_first | 156,393 | 54% | $0.0191 | 28% | -11% | 13 | 28% |
| token_budget_tools_first | 160,996 | 48% | $0.0212 | 23% | -2% | 15 | 17% |
| **none** | **450,590** | **90%** | **$0.0216** | **9%** | — | **0** | **100%** |
| context_window_aggressive | 113,571 | 9% | $0.0236 | 4% | +9% | 15 | 17% |
| truncation | 247,146 | 65% | $0.0247 | 4% | +14% | 13 | 28% |
| token_budget_window_first | 170,597 | 34% | $0.0268 | 14% | +24% | 15 | 17% |
| token_budget_fallback | 169,143 | 22% | $0.0302 | 12% | +40% | 13 | 28% |
| sliding_window | 144,408 | 1% | $0.0315 | 1% | +46% | 15 | 17% |
| tool_result | 435,044 | 78% | $0.0319 | 12% | +47% | 0 | 100% |
| context_window | 188,808 | 23% | $0.0332 | 16% | +54% | 13 | 28% |
| selective_tool_call | 436,465 | 78% | $0.0333 | 15% | +54% | 0 | 100% |
| token_budget_summarize | 158,759 | 39% | $0.0360 | 11% | +67% | 0 | 67% |
| context_window_lazy | 245,506 | 27% | $0.0408 | 19% | +89% | 13 | 28% |
| summarization | 135,744 | 2% | $0.0442 | 19% | +105% | 7 | 61% |

Effective input against `none`: `tool_result` **+51%** and `selective_tool_call` **+52%**, both on 3% *fewer* tokens; `token_budget_truncate_first` **-6%** on 65% fewer.

**Same model on two routes, both pinned — an unusually direct comparison:**

| | OpenRouter | Foundry |
| --- | ---: | ---: |
| `none` cost | $0.0216 | $0.0215 |
| `none` hit rate | 90% | 91% |
| `tool_result` vs none | +47% | +34% |
| `context_window` vs none | +54% | +23% |
| `context_window` hit rate | 23% | **38%** |

The uncompacted baseline is the same to within 0.5%. The *compaction penalties* differ, and the hit-rate column says why: **Foundry's cache survives compaction better than OpenRouter's** (38% vs 23% under the same strategy). The direction of every conclusion is identical; the magnitude is route-dependent.

### Run 2 — `gpt-5.4-mini` via Azure Foundry (pinned)

Pricing EUR 0.66/M in, 0.07/M cached (9.4x), 3.96/M out (6x). Every run gathered 17/17 facts with 0 unfetched, so all rows describe the same conversation.

| strategy | in | hit% | cost | +- | vs none | lost | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| context_window_aggressive | 148,685 | 47% | $0.0613 | 10% | -13% | 13 | 28% |
| **none** | **433,829** | **89%** | **$0.0702** | **8%** | — | **0** | **100%** |
| token_budget_tools_first | 205,931 | 53% | $0.0768 | 21% | +9% | 13 | 17% |
| token_budget_truncate_first | 229,435 | 57% | $0.0785 | 12% | +12% | 13 | 28% |
| token_budget_window_first | 197,605 | 47% | $0.0813 | 12% | +16% | 13 | 28% |
| truncation | 345,178 | 73% | $0.0855 | 21% | +22% | 4 | 56% |
| sliding_window | 140,985 | 4% | $0.0940 | 9% | +34% | 15 | 17% |
| token_budget_summarize | 186,719 | 48% | $0.0956 | 11% | +36% | 11 | 39% |
| context_window | 230,409 | 42% | $0.0993 | 12% | +41% | 13 | 28% |
| token_budget_fallback | 225,892 | 36% | $0.1059 | 1% | +51% | 13 | 28% |
| tool_result | 437,288 | 71% | $0.1143 | 68% | +63% | 0 | 100% |
| selective_tool_call | 468,607 | 70% | $0.1257 | 34% | +79% | 0 | 100% |
| summarization | 130,434 | 0% | $0.1268 | 5% | +81% | 1 | 56% |
| context_window_lazy | 309,358 | 42% | $0.1367 | 24% | +95% | 11 | 39% |

Effective input against `none`: `tool_result` **+80%**, `selective_tool_call` **+98%**, `context_window_aggressive` **-3%**. Break-even needs a **44%** cut at 71% hit rate and **65%** at 47%.

**Observation unique to this run:** `tool_result` and `selective_tool_call` sent *more* total tokens than `none` (+1%, +8%) despite ending with smaller prompts (32,988 vs 39,870). Each took an extra model call, and at this conversation size one extra call costs more than the trimming saves. Shrinking the prompt does not always reduce spend.

**`summarization` reached a 0% hit rate** — not low, zero. It rewrites history thoroughly enough that nothing is reusable, then pays for its own summary calls on top.

---

### Run 3 — `gpt-5.6-luna` via Azure Foundry (pinned)

Pricing $0.20/M in, $0.02/M cached (10x), $1.20/M out (6x); OpenRouter list rates used as a stand-in, since only the ratios affect the relative figures. `temperature` is rejected by this deployment and was dropped (`NO:temp` on every row, applied uniformly). Every run gathered 17/17 facts with 0 unfetched.

**The tightest measurement in the set**: the control varied by 1% across three repeats, so the cost differences below are unusually trustworthy.

| strategy | in | hit% | cost | +- | vs none | lost | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| token_budget_tools_first | 159,921 | 65% | $0.0167 | 19% | -22% | 13 | 28% |
| token_budget_truncate_first | 164,176 | 65% | $0.0182 | 15% | -15% | 13 | 28% |
| **none** | **453,004** | **91%** | **$0.0215** | **1%** | — | **0** | **100%** |
| context_window_aggressive | 123,374 | 20% | $0.0229 | 10% | +6% | 15 | 17% |
| truncation | 261,730 | 72% | $0.0233 | 13% | +8% | 10 | 33% |
| token_budget_window_first | 170,460 | 40% | $0.0251 | 1% | +17% | 15 | 17% |
| context_window | 179,512 | 38% | $0.0265 | 18% | +23% | 13 | 28% |
| token_budget_fallback | 181,009 | 39% | $0.0270 | 27% | +25% | 13 | 28% |
| tool_result | 419,919 | 78% | $0.0288 | 33% | +34% | 0 | 100% |
| selective_tool_call | 421,435 | 78% | $0.0306 | 14% | +42% | 0 | 100% |
| sliding_window | 144,743 | 1% | $0.0316 | 0% | +47% | 15 | 17% |
| token_budget_summarize | 165,895 | 48% | $0.0319 | 5% | +48% | 6 | 56% |
| context_window_lazy | 253,705 | 25% | $0.0428 | 31% | +99% | 13 | 28% |
| summarization | 135,247 | 1% | $0.0440 | 10% | +104% | 7 | 50% |

Effective input against `none`: `tool_result` and `selective_tool_call` both **+53%** on 7% *fewer* tokens; `token_budget_tools_first` **-19%** on 65% fewer. Break-even needs a **39%** cut at 78% hit rate, **56%** at 65%, **72%** at 38%.

**Same model as run 1, different route and API, and it agrees**: information-preserving strategies cost 34-42% more, the correctness cliff is identical, and the only cheaper options lose 13 of 17 facts.

---

### Run 4 — `z-ai/glm-5.3-flash` via OpenRouter, Z.AI pinned (cost axis only)

Pricing $0.075/M in, $0.015/M cached (**5x**), $0.25/M out. Forcing is unavailable: every backend returns 404 for any `tool_choice` other than `auto`, despite all six advertising `tool_choice` support. So the run is unpinned throughout, which at least makes its rows comparable with each other.

**Only six of fourteen strategies completed.** Every strategy built on `TokenBudgetComposedStrategy` — all `token_budget_*` and all three `context_window*` — failed at turn 2 with a 400. This is not a provider fault: Z.AI, Novita and GMICloud all reject it, with different error text. At small scale the same strategies succeed, and their projected message shapes and annotations are byte-identical to `truncation`, which works. Unresolved.

**The correctness axis is unusable.** The control scored 6%, with all 17 facts present and unused. That is this model's answer discipline, not compaction.

The cost axis, for the six that completed:

| strategy | in | hit% | cost | vs none | lost |
| --- | ---: | ---: | ---: | ---: | ---: |
| truncation | 322,640 | 70% | $0.0118 | **-44%** | 3 |
| sliding_window | 245,614 | 15% | $0.0171 | -19% | 15 |
| **none** | 947,039 | 90% | $0.0210 | — | 0 |
| selective_tool_call | 686,993 | 69% | $0.0241 | +15% | 0 |
| tool_result | 740,922 | 70% | $0.0258 | +23% | 0 |
| summarization | 220,588 | 17% | $0.0313 | +49% | 3 |

---

### Runs 5 and 6 — `z-ai/glm-5.2`: the discount tested under control

Every other run confounds the cache discount with the model. This pair does not. **OpenRouter serves glm-5.2 on two backends at the same $1.40/M input and $4.40/M output, differing only in the cached-read price**: Crusoe at $0.26 (5x) and BaseTen at $0.14 (10x). Same model, same weights, same input and output rates. The discount is the only variable.

Both runs: 0 errors, 17/17 facts, control 100% correct, control spread 1% and 10%. Unpinned tools (glm-5.2 also returns 404 for forcing), 10 strategies.

| strategy | 5x (Crusoe) | 10x (BaseTen) | lost |
| --- | ---: | ---: | ---: |
| token_budget_tools_first | -41% | -22% | 13 |
| context_window_aggressive | -41% | -19% | 13 |
| truncation | -35% | -19% | 11-13 |
| context_window | -35% | -7% | 13 |
| token_budget_fallback | -23% | +0% | 13 |
| sliding_window | -21% | +9% | 15 |
| summarization | -11% | +22% | 13 |
| **none** | — | — | **0** |
| selective_tool_call | **+2%** | **+22%** | 0 |
| tool_result | **+3%** | **+24%** | 0 |

**Halving the discount moved every strategy against compaction, and none moved the other way.** The information-preserving pair goes from roughly free (+2%, +3%) to clearly expensive (+22%, +24%). `truncation`'s saving halves.

**The prediction was made before the second run.** From the 5x run's own token counts and hit rates, the formula projected `tool_result` at +17% and `truncation` at -24% under a 10x discount. Measured: **+24%** and **-19%**. Both errors sit inside the run-to-run spread this model shows (2-26%), and every strategy moved in the predicted direction.

This is the strongest evidence in the set that **the cached-read price, not the strategy and not the model, decides whether compaction pays.**

Note also that `none` remained the *only* setting keeping all 17 facts in both runs, at both discounts. The discount moves the cost axis. It does not touch the correctness axis.

---

## Window sweep on the harness agent (Runs 7 and 8)

Runs 1-6 all used the `plain` agent at a 32,000-token window with 17 planted facts. Runs 7 and 8 re-ask the question on the **harness** agent — `create_harness_agent`, the wiring a typical caller gets, including the framework's own narration guidance — at wider windows and with a much finer accuracy measure.

**Everything below differs from Runs 1-6 except the model and the route**, so these are a separate experiment, not two more rows in the cross-model table:

| | Runs 1-6 | Runs 7-8 |
| --- | --- | --- |
| Agent | `plain` | **`harness`** |
| Planted facts | 17 | **53** (8 codes per tool result) |
| Turns | 16 | **22** |
| Closing question | one sweeping question | **7 targeted questions**, union covers all 53 |
| System prompt | no retrieval guidance | **retrieval guidance added** |

The last two rows exist because identical repeats of the *uncompacted control* once returned 42 of 53 and 5 of 53, and a control that unstable makes every accuracy number meaningless. Both changes were made in response, and both appeared to work.

**Neither was the cause.** A later 2x2 on the control settled it — the reply cap was:

| retrieval guidance | `--answer-max-tokens` | correct | facts present but unlisted | spread |
| --- | ---: | ---: | ---: | ---: |
| on | 900 | 100% | 0 | 10% |
| **off** | **900** | **33%** | **36** | **43%** |
| on | 4,000 | 100% | 0 | 34% |
| **off** | **4,000** | **100%** | **0** | **3%** |

Enumerating 53 labelled codes costs roughly 640 tokens before any prose, so a 900-token cap truncated the answer, and the scorer counted the missing tail as facts the model ignored. The retrieval guidance worked by pushing codes ahead of prose so more of them fit inside the truncation; the targeted questions worked by never asking for more than about eight at a time. Both were compensating for a cap, and at 4,000 tokens neither is needed.

The instrument lesson is general: **a reply cap converts silently into apparent accuracy loss**, and it looks exactly like compaction damage — facts present in context, absent from the answer. The `ignored` column is what distinguishes them only if the control is checked at the same cap.

Runs 7 to 9 are unaffected. They ran the targeted format at the 900-token cap, where each answer lists about eight codes, and their controls scored 53/53 with 0 ignored — which is the direct evidence that no answer was truncated. The default cap is now 4,000 regardless.

**Material is scaled with the window on purpose.** Holding the conversation fixed while widening the window would leave every strategy inert — nothing to evict, and a tool-oriented strategy that evicts nothing scores a perfect result for doing nothing, which is a fault this harness has already produced once. Both runs therefore hold the *pressure* constant at the sizing rule as Runs 1-6, and vary only the absolute window. Measured on the uncompacted peak, that came out at 135% of budget for Run 7 and 127% for Run 8; the estimates in the table below are the planning figures, which overstate the real prompt by about 25% because they count characters rather than tokens for the user-side material.

| | Run 7 | Run 8 |
| --- | ---: | ---: |
| Window / reply reservation | 60,000 / 2,048 | 120,000 / 2,048 |
| Working budget | 57,952 | 117,952 |
| Filler per padding turn | 4,000 | 8,000 |
| Tool result size | 8,000 | 16,000 |
| Material (tool share) | ~96,600 (50%) | ~192,000 (50%) |
| Planning estimate vs budget | 167% | 163% |
| **Measured peak vs budget** | **135%** | **127%** |

Exact invocation, Run 7. Run 8 differs only in the three sizing flags above.

```sh
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_MODEL="gpt-5.4-mini"

cachebench_live foundry:gpt-5.4-mini \
  --agent harness \
  --strategies none,truncation,sliding_window,tool_result,selective_tool_call,context_window,context_window_aggressive,context_window_lazy,summarization,token_budget_fallback,token_budget_tools_first,token_budget_truncate_first,token_budget_window_first,token_budget_summarize \
  --summarizer-provider foundry:gpt-5.4-mini \
  --repeats 3 \
  --context-window 60000 --max-output-tokens 2048 \
  --markers-per-tool 8 --tool-turns 6 \
  --filler-tokens 4000 --tool-result-tokens 8000 \
  --price-input 0.66 --price-cached 0.07 --price-output 3.96
```

Authentication is `DefaultAzureCredential` against an `az login` session, and the deployment is `gpt-5.4-mini` on GlobalStandard. Prices are the same EUR rates as Run 2 — 0.66/M in, 0.07/M cached (9.4x), 3.96/M out — so the costs are on the same scale as Run 2's even though the conversations are not the same.

### Run 7 — 60,000-token window, harness agent

Wall clock 54 minutes for the full 14x3 matrix. **Control: 53/53 facts, 100% correct, 0 unfetched, +-5%.** This is the first run in the series whose accuracy baseline is stable enough to rank against.

| strategy | peak tok | hit% | in | cost | +- | vs none | facts | lost | ignored | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| truncation | 42,855 | 83% | 713,463 | $0.1352 | 26% | -17% | 29/53 | 24 | 24 | 11% |
| context_window_aggressive | 17,301 | 56% | 388,128 | $0.1385 | 2% | -15% | 8/53 | 45 | 0 | 17% |
| token_budget_tools_first | 25,613 | 67% | 523,828 | $0.1459 | 13% | -10% | 16/53 | 37 | 16 | 2% |
| token_budget_window_first | 32,927 | 66% | 505,106 | $0.1495 | 6% | -8% | 16/53 | 37 | 0 | 31% |
| token_budget_truncate_first | 25,823 | 67% | 528,565 | $0.1512 | 3% | -7% | 16/53 | 37 | 8 | 17% |
| **none** | **78,307** | **93%** | **1,309,273** | **$0.1621** | **5%** | — | **53/53** | **0** | **0** | **100%** |
| sliding_window | 21,233 | 7% | 289,092 | $0.1880 | 6% | +16% | 0/53 | 53 | 0 | 2% |
| token_budget_fallback | 25,802 | 62% | 600,575 | $0.1885 | 10% | +16% | 16/53 | 37 | 0 | 31% |
| token_budget_summarize | 26,411 | 59% | 462,546 | $0.1925 | 5% | +19% | 34/53 | 19 | 0 | 65% |
| context_window | 26,937 | 60% | 597,304 | $0.1963 | 7% | +21% | 21/53 | 32 | 0 | 41% |
| selective_tool_call | 61,330 | 84% | 1,129,087 | $0.1978 | 58% | +22% | 53/53 | 0 | 0 | 100% |
| tool_result | 63,274 | 84% | 1,156,724 | $0.2076 | 28% | +28% | 53/53 | 0 | 0 | 100% |
| context_window_lazy | 39,574 | 67% | 836,137 | $0.2368 | 1% | +46% | 24/53 | 29 | 0 | 46% |
| summarization | 17,236 | 0% | 268,482 | $0.2514 | 4% | +55% | 26/53 | 27 | 0 | 50% |

Verdict `none`, for the seventh time: the only two settings that keep the answer intact both cost more than not compacting.

**Four rows are not rankable on cost.** A gap smaller than the spread is not a result, and here that disqualifies `truncation` (-17% at +-26%), `token_budget_tools_first` (-10% at +-13%), `selective_tool_call` (+22% at +-58%) and `tool_result` (+28% at +-28%). Note *which* rows those are: the two accuracy-preserving strategies are precisely the two whose cost cannot be pinned down. They behaved the same way on this model at 32,000 (+-68% and +-34% in Run 2), so it is a property of `gpt-5.4-mini` on this route rather than a fluke. Rows that are safely rankable: `context_window_aggressive` (-15%, +-2%), `token_budget_truncate_first` (-7%, +-3%), `context_window_lazy` (+46%, +-1%), `summarization` (+55%, +-4%).

**The break-even formula predicted the cost column before it was read.** Using `d = 0.070/0.66 = 0.106` with each row's own hit rate and token count:

| strategy | tokens vs none | hit rate | predicted | measured |
| --- | ---: | ---: | ---: | ---: |
| `truncation` | -45% | 93% -> 83% | -17% | **-17%** |
| `tool_result` | -12% | 93% -> 84% | +30% | **+28%** |
| `selective_tool_call` | -14% | 93% -> 84% | +27% | +22% |

At 93% -> 84% the cut required to break even is **32%**; `tool_result` cut 12%. This is the third independent confirmation of the formula, and the first at a window other than 32,000.

**New finding: surviving compaction is not the same as being usable.** The `facts` column counts planted codes still present in the closing prompts, and it has been read as the ceiling on accuracy. Run 7 shows the ceiling is not tight:

| strategy | facts surviving | ignored | correct |
| --- | ---: | ---: | ---: |
| `truncation` | 29/53 | **24** | 11% |
| `token_budget_tools_first` | 16/53 | **16** | 2% |
| `token_budget_summarize` | 34/53 | 0 | 65% |

`truncation` left 29 codes in front of the model and the model used none of them. The codes survive as strings while the surrounding turns saying *which deployment each belongs to* are deleted, so a question about the staging deployment cannot be answered from a bare list of identifiers. `token_budget_summarize` keeps 13 more facts than `context_window` and scores 24 points better, for the same reason in reverse: a summary preserves the labelling.

The practical consequence is that **fact-survival counts overstate what message-deleting strategies preserve**, and the two axes have to be measured separately. Earlier runs could not show this, because their controls were never stable enough to trust the accuracy column.

### Run 8 — 120,000-token window, harness agent

Wall clock 1 h 34 min. Same instrument as Run 7, with the material doubled so the pressure on the window is unchanged. **Control: 53/53 facts, 100% correct, 0 unfetched, +-7%**, peak prompt 150,112 tokens — still well inside this model's real 400,000-token limit, so `none` is a genuinely available option here and not an artefact of a small target.

| strategy | peak tok | hit% | in | cost | +- | vs none | facts | lost | ignored | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| context_window_aggressive | 33,574 | 69% | 761,131 | $0.2006 | 1% | -33% | 16/53 | 37 | 0 | 31% |
| token_budget_truncate_first | 57,655 | 75% | 1,013,942 | $0.2328 | 6% | -23% | 19/53 | 34 | 8 | 22% |
| token_budget_tools_first | 64,953 | 71% | 951,341 | $0.2406 | 4% | -20% | 16/53 | 37 | 16 | 2% |
| token_budget_window_first | 58,272 | 77% | 1,133,946 | $0.2443 | 0% | -19% | 24/53 | 29 | 24 | 2% |
| truncation | 91,456 | 83% | 1,513,282 | $0.2690 | 2% | -11% | 29/53 | 24 | 8 | 41% |
| **none** | **150,112** | **93%** | **2,498,779** | **$0.3009** | **7%** | — | **53/53** | **0** | **0** | **100%** |
| context_window | 58,541 | 69% | 1,287,983 | $0.3421 | 36% | +14% | 27/53 | 26 | 8 | 37% |
| sliding_window | 41,014 | 6% | 553,299 | $0.3557 | 6% | +18% | 0/53 | 45 | 0 | 17% |
| selective_tool_call | 117,970 | 85% | 2,182,901 | $0.3670 | 4% | +22% | 53/53 | 0 | 16 | 70% |
| tool_result | 118,114 | 84% | 2,168,925 | $0.3681 | 5% | +22% | 53/53 | 0 | 0 | 100% |
| token_budget_fallback | 58,075 | 62% | 1,318,701 | $0.3966 | 17% | +32% | 25/53 | 28 | 9 | 31% |
| token_budget_summarize | 58,423 | 60% | 1,287,404 | $0.4267 | 35% | +42% | 21/53 | 32 | 0 | 41% |
| context_window_lazy | 75,529 | 67% | 1,608,395 | $0.4401 | 11% | +46% | 35/53 | 18 | 0 | 67% |
| summarization | 132,022 | 51% | 2,105,284 | $0.7972 | 59% | +165% | 47/53 | 6 | 5 | 80% |

Verdict `none` again. Only four settings score 70% or better, and three of them cost more: `tool_result` (+22%, 100%), `selective_tool_call` (+22%, 70%), `summarization` (+165%, 80%).

**The measurement is much tighter here than at 60,000.** Only `context_window` (+14% at +-36%), `token_budget_summarize` (+42% at +-35%) and `summarization` (+165% at +-59%) are disqualified by spread. In particular `tool_result` lands at +-5% and `selective_tool_call` at +-4%, where at 60,000 they were +-28% and +-58% — so this run, unlike Run 7, can actually rank the accuracy-preserving strategies on cost.

**Formula check, second window.** Same `d = 0.106`:

| strategy | tokens vs none | hit rate | predicted | measured |
| --- | ---: | ---: | ---: | ---: |
| `context_window_aggressive` | -70% | 93% -> 69% | -31% | **-33%** |
| `truncation` | -39% | 93% -> 83% | -7% | -11% |
| `tool_result` | -13% | 93% -> 84% | +28% | +22% |
| `selective_tool_call` | -13% | 93% -> 85% | +24% | **+22%** |
| `context_window_lazy` | -36% | 93% -> 67% | +53% | +46% |
| `summarization` | -16% | 93% -> 51% | +172% | **+165%** |

Every prediction is within 7 points and **all six err in the same direction**, which is explained rather than hand-waved: the formula models input tokens only, and every compacting strategy also generates fewer output tokens than `none` (2,775-4,191 against 4,058) at 6x the input price. The residual is the output term the formula omits.

### What the two windows say together

Runs 7 and 8 differ only in window size and a proportional material scale, so they can be compared directly with each other. Run 2 is shown for context but used a different agent, fact count and question format, so its column is not a clean third point.

| | Run 2 — 32K *(plain, 17 facts)* | Run 7 — 60K | Run 8 — 120K |
| --- | ---: | ---: | ---: |
| `none` peak prompt | 39,870 | 78,307 | 150,112 |
| `none` hit rate | 89% | 93% | 93% |
| `tool_result` vs none | +63% *(+-68%)* | +28% *(+-28%)* | **+22%** *(+-5%)* |
| `truncation` vs none | +22% | -17% *(+-26%)* | **-11%** *(+-2%)* |
| cheapest setting | -13% | -15% | **-33%** |
| best accuracy under `none` | 100% (`tool_result`) | 100% (`tool_result`) | 100% (`tool_result`) |

Two things move with the window, in opposite directions:

- **Keeping the answer intact gets cheaper.** The `tool_result` penalty falls from +63% to +22%. Its hit rate barely moves (71% -> 84%), but the prefix it preserves is a larger share of a larger prompt, so less is re-billed.
- **Throwing the answer away gets much cheaper.** The cheapest setting improves from -13% to -33%, because at 120,000 there is far more absolute volume to delete.

So the **gap between "cheap and wrong" and "correct and expensive" widens with scale**: at 32,000 the two ends of the table were 76 points apart, at 120,000 they are 55 points apart on cost but the accuracy difference is 69 points (31% against 100%). Bigger windows do not make compaction safer; they make the wrong choice cheaper and therefore more tempting.

The conclusion does not change at any window tested: **no setting beat `none` on both axes.**

**`selective_tool_call` regressed on accuracy at scale** — 53/53 facts surviving but 70% correct, with 16 ignored, where at 60,000 the same strategy scored 100%. This is the survives-but-unused effect from Run 7 appearing in a strategy that deletes no messages at all. With 16,000-token tool results, collapsing an old tool-call group removes enough surrounding context that codes still present in the prompt stop being attributable to a deployment. It is the clearest single demonstration that the `facts` column is a ceiling and not a prediction.

### Run 9a — aborted: `gpt-5.4-mini` does not have a 400,000-token input window

The intent was to measure the regime Runs 7 and 8 do not cover: the conversation staying *inside* the window rather than overflowing it. The model card says 400,000 tokens, so the run was configured with `--context-window 400000` and material sized to about 85% of it.

**Four of the fourteen strategies died with HTTP 400 `context_length_exceeded`,** and which four is the whole finding:

| strategy | first threshold at a 397,952 budget | failed at |
| --- | ---: | --- |
| `none` | never compacts | turn 13, all 3 repeats |
| `truncation` | `max_n` = 0.8 -> **318,361** | turn 13, 13, 12 |
| `context_window_lazy` | tool eviction 0.7 -> **278,566** | turn 13, all 3 repeats |
| `summarization` | none — keeps the last N *groups* | turn 15, all 3 repeats |

**The three token-driven failures are exactly the strategies whose first threshold exceeds 272,000.** Everything that fires at or below 50% of the budget survived, including the shipped `context_window`, whose tool eviction runs at 0.5 -> 198,976. `context_window_lazy` differs from it only in thresholds, and that difference is the whole distance between working and failing.

`truncation` failed at **the same turn as `none`**, which is the proof it never compacted once: the service refused the request long before the local count reached its trigger.

`summarization` is the exception that is not about thresholds. `SummarizationStrategy` is count-based — it keeps the last N message groups with no token target at all — so it fired every turn and still could not get under the limit, because at this scale two surviving tool results are 50,000 tokens on their own. It failed two turns later than the rest, which is what compaction that runs but cannot keep up looks like.

Local token counts for the conversation, which the failures bracket precisely:

| turn | cumulative prompt | outcome |
| ---: | ---: | --- |
| 12 | 253,266 | accepted |
| 13 | 271,291 | **rejected** |

A direct probe on the same deployment, one prompt per size, pins it:

| prompt | result |
| ---: | --- |
| 260,278 | accepted — service counted 260,284 |
| 270,288 | accepted — service counted 270,294 |
| 275,292 | **`context_length_exceeded`** |
| 290,309 | **`context_length_exceeded`** |

So the advertised 400,000 is **total** context: **272,000 input + 128,000 output**, the documented GPT-5-class split. There is no configuration in which 400,000 tokens of history reach this model.

**The error names the offending parameter.** HTTP 400, `{"type": "invalid_request_error", "code": "context_length_exceeded", "param": "input", "message": "Your input exceeds the context window of this model."}` — `param: input`, not the request as a whole.

**And the two limits are independent, not a pool you can allocate between.** Every probe above ran with `max_tokens=16`, so a tiny output cap was already in force when 275,292 tokens of input were refused. Varying the cap at fixed input changes nothing:

| input | `max_tokens` | result |
| ---: | ---: | --- |
| 270,293 | 16 | accepted |
| 270,293 | 100,000 | accepted |
| 270,293 | 128,000 | accepted |
| ~280,000 | 16 | **`context_length_exceeded`** |

A large output cap does not consume input allowance, and a tiny one does not buy any. The service also does not range-check `max_tokens` — 200,000 is accepted without complaint on a short prompt — so it is a generation cap, not a reservation.

**This matters for how MAF's budget is configured.** `input_budget = max_context_window_tokens - max_output_tokens` models a *shared* pool, and this model does not have one. Passing the advertised 400,000 is therefore wrong regardless of what the output argument is set to. The correct value for `max_context_window_tokens` is **the model's input limit**, 272,000, whenever a provider states input and output ceilings separately; the subtraction then just adds a safety margin for the reply.

**The real cause was the output reservation, and it is worth being precise about it.** Every threshold in MAF is a fraction of an *input budget*, and both layers compute that budget the same way:

```python
# agent_framework/_compaction.py, ContextWindowCompactionStrategy.__init__
input_budget         = max_context_window_tokens - max_output_tokens
tool_eviction_tokens = int(input_budget * tool_eviction_threshold)   # default 0.5
truncation_tokens    = int(input_budget * truncation_threshold)      # default 0.8
```

This run passed `max_context_window_tokens=400_000` with `max_output_tokens=2_048`, giving an input budget of **397,952** — 46% above what the service will accept. Had it passed the model's real output cap instead, the arithmetic would have landed exactly on the limit:

| configuration | input budget | eviction (0.5) | truncation (0.8) |
| --- | ---: | ---: | ---: |
| 400,000 window, **2,048** reservation | 397,952 | 198,976 | **318,361 — unreachable** |
| 400,000 window, **128,000** cap | **272,000** | 136,000 | 217,600 — reachable |

`400,000 - 128,000 = 272,000` is the measured limit to the token. **MAF's formula is correct; the argument it was given was not.** `max_output_tokens` is documented as *"Maximum output tokens per response"* — the model's ceiling — not the size of reply you intend to ask for. Under-setting it inflates the input budget by the difference, and on a GPT-5-class model that difference is 126,000 tokens, which is enough to push `TruncationStrategy` and any `ContextWindowCompactionStrategy` above 0.68 out of reach.

The failure mode is still worth knowing, because nothing reports it: the agent runs, compacts nothing, and dies of an HTTP 400 rather than a large bill. It belongs with the harness installing no strategy at all when the token arguments are omitted — same class, in that a plausible configuration silently produces no compaction.

Note which rows survived: everything triggering at or below 0.5 of the inflated budget, which is why the shipped `context_window` completed and `context_window_lazy` at 0.7 did not. That is not a defence of the shipped thresholds so much as a demonstration of how much headroom a wrong reservation eats.

Runs 7, 8 and 9 are unaffected. They set a deliberately *simulated* window with the same 2,048 reservation, and their budgets — 57,952, 117,952 and 269,952 — all sit far below the 272,000 hard limit, so every threshold was reachable. Run 9 is in fact equivalent to the correctly configured real-world case: its 232,748-token peak is 86% of its 269,952 budget and 86% of the model's true 272,000 input limit alike.

#### The reservation is arithmetic; the cap is a request option, and they are separate

`max_output_tokens` on a compaction strategy only subtracts. Whether the model is actually held to it depends on a different value reaching the request. Measured on this deployment, one prompt asking for a long essay:

| request | output produced |
| --- | ---: |
| `max_tokens=32` | exactly 32 |
| `max_tokens=200` | exactly 200 |
| **omitted** | **1,444** |

So the cap works and is honoured exactly — and **there is no modest default**. Omit it and the model generates whatever it likes, bounded only by its 128,000-token output ceiling.

`create_harness_agent` connects the two, but weakly:

```python
# agent_framework/_harness/_agent.py, create_harness_agent
if max_output_tokens is not None:
    default_opts.setdefault("max_tokens", max_output_tokens)
```

`setdefault`, so a caller who supplies their own `max_tokens` — in `default_options` or in per-run options — silently keeps the reservation and the cap out of step. **A hand-built `Agent` carrying a `ContextWindowCompactionStrategy` gets no cap at all**, and then the reservation is fiction: the strategy compacts to leave room for 2,048 tokens of reply and the model is free to emit 40,000, overflowing the window the strategy exists to protect.

These runs sit on the safe side of that. The reservation was 2,048 while the request cap was 900 (`--answer-max-tokens`, applied in `_providers.py` and travelling per turn), so more was reserved than could ever be used. The 1,148-token gap understates the input budget by 2% at 60,000 and 0.4% at 272,000, which changes nothing. Replies averaged ~147 tokens against that 900-token cap, so nothing was truncated — the control scoring 53/53 is the proof.

**One caveat this raises about earlier work.** The withdrawn *sweeping question* variant asked for all 53 codes in a single reply. At roughly 12 tokens per labelled code that is ~640 tokens before any prose, against a 900-token cap — close enough that truncation is a live alternative explanation for the 42/53 and 5/53 results previously attributed solely to missing retrieval guidance. The two cannot be separated from the data already collected. Re-running that variant with a raised cap would settle it, and the targeted-question format now in use is nowhere near the cap either way.

Note also what is *not* at fault. The bundled `tiktoken` counter agreed with the service to within **6 tokens on a 270,294-token prompt** (0.002%), so local counting is sound; the error was entirely in the assumed window.

The corrected run configures the real input limit instead:

```sh
  --context-window 272000 --max-output-tokens 2048 \
  --filler-tokens 12600 --tool-result-tokens 25200
```

which puts the material at ~228,400 tokens and the peak prompt at ~233,000, or **86% of the 272,000 limit** — the 80-90% band the sweep was asking for, measured against the window the model actually has.

### Run 9 — 86% of the real limit, the conversation fitting inside the window

Wall clock 1 h 45 min, no failed turns, no dropped options. **Control: 53/53 facts, 100% correct, 0 unfetched, +-0%** — the tightest control in the series, and its 94% hit rate is the highest.

This is the regime Runs 1-8 never covered. There the conversation overflowed the budget, so compaction was forced to fire and the only question was what it cost. Here it **fits**: window set to the model's true 272,000-token input limit, peak prompt 232,748.

| strategy | peak tok | hit% | in | cost | +- | vs none | facts | lost | ignored | correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| token_budget_window_first | 128,262 | 87% | 2,310,063 | $0.3483 | 1% | **-19%** | 26/53 | 27 | 0 | 50% |
| **none** | **232,748** | **94%** | **3,875,490** | **$0.4317** | **0%** | — | **53/53** | **0** | **0** | **100%** |
| truncation | 207,338 | 89% | 3,057,321 | $0.4365 | 3% | +1% | 37/53 | 16 | 0 | 70% |
| token_budget_truncate_first | 129,314 | 80% | 2,310,122 | $0.4498 | 0% | +4% | 29/53 | 24 | 24 | 11% |
| token_budget_tools_first | 129,766 | 80% | 2,322,751 | $0.4568 | 6% | +6% | 29/53 | 24 | 8 | 41% |
| sliding_window | 64,503 | 9% | 858,120 | $0.5322 | 13% | +23% | 0/53 | 45 | 0 | 17% |
| context_window_aggressive | 78,184 | 61% | 1,780,463 | $0.5500 | 0% | +27% | 23/53 | 30 | 2 | 41% |
| tool_result | 183,119 | 84% | 3,372,103 | $0.5632 | 2% | +30% | 53/53 | 0 | 0 | 100% |
| selective_tool_call | 182,732 | 84% | 3,379,748 | $0.5667 | 4% | +31% | 53/53 | 0 | 0 | 100% |
| context_window_lazy | 183,299 | 84% | 3,400,267 | $0.5815 | 41% | +35% | 53/53 | 0 | 0 | 100% |
| context_window | 129,745 | 76% | 2,684,893 | $0.5841 | 2% | +35% | 37/53 | 16 | 8 | 56% |
| token_budget_fallback | 130,746 | 76% | 2,708,528 | $0.5956 | 43% | +38% | 37/53 | 16 | 32 | 11% |
| token_budget_summarize | 129,363 | 67% | 2,679,770 | $0.7354 | 1% | +70% | 37/53 | 16 | 32 | 11% |
| summarization | 206,143 | 46% | 3,286,712 | $1.3286 | 10% | +208% | 53/53 | 0 | 0 | 100% |

**Thirteen of fourteen strategies cost more than not compacting.** The single exception, `token_budget_window_first` at -19% (+-1%, so the gap is real), loses 27 of 53 facts and scores 50%. There is no setting on this table that is both cheaper and correct.

Only `context_window_lazy` (+-41%) and `token_budget_fallback` (+-43%) are disqualified by spread; every other row is a real difference.

**This is the most realistic configuration tested and the starkest result.** The reason is visible in one column: at 232,748 tokens the control reaches a **94% hit rate**, the highest anywhere in this work. The longer the conversation, the more of it is discounted, and the more a mutation to its prefix costs. Compaction here cuts 20-40% of tokens and pays for it with 5-18 points of hit rate, which at a 9.4x discount is a losing trade almost every time.

**Do not read Run 9 as a fourth point on the Run 7-8 curve.** Those two held the conversation over the budget and varied the window; this one changes the regime. Grouped properly:

| regime | window | `tool_result` vs none | cheapest that keeps 53/53 |
| --- | ---: | ---: | ---: |
| overflowing, 135% of budget | 60,000 | +28% | +22% (`selective_tool_call`) |
| overflowing, 127% of budget | 120,000 | +22% | +22% (`selective_tool_call`) |
| **fitting, 86% of the limit** | **272,000** | **+30%** | **+30% (`tool_result`)** |

The penalty falls with window size while the conversation overflows, and comes back when it fits — because a conversation that fits has a nearly perfect cache to lose.

**The shipped default is the worst of both.** `context_window` at its 0.5/0.8 thresholds cost **+35%** and lost 16 of 53 facts, scoring 56%. It is beaten on cost by seven settings and on accuracy by four. This is now the fourth run in which the shipped default costs more than no compaction at all.

**`summarization` bought correctness at 3x the price.** It is the only deleting-class strategy to keep all 53 facts at 100%, and it cost **+208%**. At this size its own summary calls are almost free ($0.0474 of $1.3286); the bill is the 46% hit rate.

---

## Runs 10 to 15 — the instrument rebuilt, and a strategy of our own

Runs 7 to 9 put every planted code at the **head** of its tool result, inside the 4,096 characters a collapsed result keeps, so tool-oriented strategies preserved all of them for free. Runs 10 onward fix that and three other faults, and add `anchored`, a strategy designed against what the earlier runs measured.

### What changed, and why each mattered

| change | why |
| --- | --- |
| Codes **spread** through each result, on labelled lines | Head placement made `tool_result` keep 53/53 for nothing. Spread, it keeps 46/53 at 120,000 — the free pass is gone |
| Reply cap 900 -> **4,000** | 53 labelled codes cost ~640 tokens to enumerate; the answer was being truncated and scored as lost facts |
| Closing questions **state the expected count** | "Every code returned by the X lookup" left the model to guess how many. Repeats swung 78 points on that guess |
| A **`c+-` column** and an `ACCURACY NOT RANKABLE` guard | The `correct` column shows the median-*cost* repeat, so 100/22/22 printed as an unremarkable 100 |

### Calibration first

Before spending on a matrix, `samples/probe_narration.py` runs the uncompacted control alone, five repeats per configuration, and reports which hold still. On this model:

| narration | median | range | recalled |
| --- | ---: | ---: | --- |
| `neutral` — the harness's own guidance is the only driver | 98% | 6pp | 52/49/52/52/51 |
| `prompted` — every value demanded in the final report | 100% | 0pp | 53/53/53/53/53 |
| `suppressed` — restating forbidden | 98% | 4pp | 52/50/52/52/52 |

All three are usable. The matrices below use **`neutral`** rather than the perfectly stable `prompted`, because `prompted` copies tool values into assistant prose and lets a strategy delete the tool results while still scoring well: it is the most stable configuration and the least informative one. Stability is a precondition, not the goal.

**The first calibration attempt was invalid and is worth recording.** It called `run_live` directly without forcing `store=False`, so the Foundry client kept the conversation server-side and the model answered from a history the client had never compacted. Every mode came back stable — 6, 0 and 7 points against the 78 measured properly on the same model an hour earlier. `run_live` now forces it itself rather than leaving it to callers.

### Run 13 — 60,000-token window, 16 strategies

Control 53/53 facts, 98% correct, **6pp** — matching calibration exactly.

| strategy | cost | vs none | +- | hit% | facts | correct | c+- |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| anchored_no_assistant | $0.1193 | **-24%** | 5% | 82% | 46/53 | 22% | 78pp |
| **anchored** | **$0.1210** | **-23%** | 2% | **82%** | 32/53 | 61% | 39pp |
| truncation | $0.1339 | -14% | 19% | 83% | 29/53 | 56% | 44pp |
| token_budget_tools_first | $0.1413 | -10% | 5% | 70% | 24/53 | 46% | 43pp |
| context_window_aggressive | $0.1435 | -8% | 2% | 52% | 16/53 | 19% | 2pp |
| token_budget_window_first | $0.1440 | -8% | 7% | 68% | 16/53 | 31% | 26pp |
| token_budget_truncate_first | $0.1488 | -5% | 9% | 66% | 12/53 | 2% | 17pp |
| **none** | **$0.1562** | — | 13% | **93%** | **53/53** | **98%** | **6pp** |
| sliding_window | $0.1864 | +19% | 8% | 9% | 0/53 | 2% | 0pp |
| token_budget_summarize | $0.1916 | +23% | 4% | 59% | 18/53 | 11% | 30pp |
| context_window | $0.1945 | +25% | 11% | 60% | 17/53 | 33% | 30pp |
| token_budget_fallback | $0.1989 | +27% | 8% | 60% | 19/53 | 24% | 22pp |
| tool_result | $0.2041 | +31% | 16% | 84% | 53/53 | 100% | 67pp |
| selective_tool_call | $0.2049 | +31% | 9% | 84% | 53/53 | 100% | 26pp |
| context_window_lazy | $0.2338 | +50% | 2% | 67% | 29/53 | 56% | 33pp |
| summarization | $0.2456 | +57% | 1% | **0%** | 0/53 | 11% | 15pp |

### Run 14 — 86% of the real 272,000-token input limit

| strategy | cost | vs none | +- | hit% | facts | correct | c+- |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **anchored** | $0.3047 | **-30%** | 3% | **82%** | 11/53 | 22% | **0pp** |
| anchored_no_assistant | $0.3053 | -30% | 2% | 82% | 11/53 | 22% | 26pp |
| **none** | **$0.4346** | — | 2% | **94%** | **53/53** | **100%** | 15pp |
| truncation | $0.4355 | +0% | 1% | 89% | 30/53 | 11% | 0pp |
| selective_tool_call | $0.5555 | +28% | 6% | 84% | 39/53 | 31% | 41pp |
| tool_result | $0.5655 | +30% | 4% | 84% | 39/53 | 48% | 52pp |
| context_window | $0.5820 | +34% | 1% | 76% | 30/53 | 11% | 15pp |

### What `anchored` achieved, and what it did not

It was built on three measurements: decisions must not depend on current size, mutations must march forward, and *what* is shed matters more than how much. Two of its goals were met.

**Cache retention is best in class.** 82% at both windows while cutting 54-62% of tokens. Every other strategy saving comparable volume sits at 52-76%. Position-only decisions keep the prefix byte-identical, which is exactly what it was for.

**It is the only strategy whose answer is reproducible.** 0pp accuracy range at 272,000, where `tool_result` swings 52 and `selective_tool_call` 41. Frozen decisions produce the same prompt every run, so they produce the same answer.

**It did not escape the trade-off.** At 272,000 it kept 11 of 53 facts — exactly the five non-tool facts plus the one code per result falling inside the surviving head fragment, because `keep_chars` was a fixed 600 characters against results that scale with the window: 1.9% of an 8,000-token result, 0.6% of a 25,200-token one. The design worked as specified; the specification did not scale.

### Run 15 — the same strategy with retention scaled to the window

The collapsed band now takes a share of the ceiling divided between its tool results: 30% of each result at 60,000 and 45% at 272,000.

| | keep per result | vs none | hit% | facts | correct |
| --- | ---: | ---: | ---: | ---: | ---: |
| `anchored`, fixed 600 chars | 0.6% | **-30%** | 82% | 11/53 | 22% |
| `anchored`, scaled retention | 45% | **-3%** | 88% | 25/53 | 48% |
| `none` | — | — | 94% | 53/53 | 80% |

**The dial moves the trade-off; it does not remove it.** Keeping 75x more of each result recovered 14 facts and gave back 27 points of cost saving. At neither setting does `anchored` beat `none` on both axes, and the verdict in both matrices remains `none` — now for the ninth and tenth time.

That is the honest result for a strategy designed specifically to win: **the constraint is not which messages you choose to drop, it is that dropping any of them forfeits a discount worth more than the tokens saved.** What a better design buys is a more favourable *shape* — higher hit rate for the same cut, and reproducibility — not an escape.

### The accuracy guard is still not sufficient

`ACCURACY NOT RANKABLE` checks the control, and in Run 13 the control was excellent at 6pp. But individual strategy rows reached 78, 67 and 52 points. **A stable baseline does not certify a stable comparison**: compaction changes the replies, which become the history, which compacts differently. Rows whose `c+-` exceeds 20 points should be read as unresolved on accuracy regardless of how steady the control was.

---

## Runs 16 to 18 — two new strategies across three window sizes

The measurement questions of runs 10 to 15 are settled: values are spread on labelled lines, the reply cap is 12,000 tokens, closing questions state their expected count, each reply is scored only against the values its own question asked for, and a calibration probe confirms the control holds still before anything is spent. What changes here is *what* is being compared: two strategies written against the earlier measurements, over three window sizes.

| | mechanism |
| --- | --- |
| `anchored` | fixed head and tail kept verbatim, the band between shortened to a share of the ceiling, decisions taken from position alone so they never change on a later turn |
| `tool_summary_anchored` | a middleware forces one recall tool call; the strategy drops every tool group in front of the resulting record |

Five repeats each, pinned, neutral narration, spread placement. Tool results scale with the window, which is the variable that turns out to matter.

### The result: the two invert

| tool result size | `tool_summary_anchored` facts | `anchored` facts |
| ---: | ---: | ---: |
| 8,000 (60K window) | **53/53** | 27/53 |
| 16,000 (120K window) | 46/53 | **53/53** |
| 25,200 (272K window) | 18/53 | **53/53** |

**A model-written record thins out as there is more to record.** The mechanism worked in every run — `REC:1, FORCED:2, RECFORCED:1`, so the middleware forced the call and the forced call produced the record — but the content degraded. What survives is the model's judgement rather than a policy, and that judgement gets worse exactly where compaction is most needed.

**Proportional retention improves for the mirror-image reason.** `anchored` gives each collapsed result a share of the ceiling, so it keeps 30% of each result at 60,000 and 44% at 272,000. The arithmetic is the same one that explains the framework's tool-result collapse losing values: head-and-tail retention of a fraction f preserves *n* evenly spread values only when f exceeds 2/n, which for eight values means retaining over 25%.

### Run 16 — 60,000-token window

| strategy | cost | vs none | +- | hit% | peak | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| truncation | $0.1417 | -14% | 8% | 84% | 43,108 | 29/53 | 56% | 48pp | 55% |
| **tool_summary_anchored** | $0.1507 | -9% | 10% | 90% | 54,608 | **53/53** | **100%** | 59pp | 100% |
| **none** | $0.1649 | — | 6% | 94% | 78,003 | 53/53 | 100% | 7pp | 100% |
| anchored | $0.1939 | +18% | 13% | 82% | 54,882 | 27/53 | 22% | 78pp | 21% |
| tool_result | $0.2115 | +28% | 9% | 85% | 63,591 | 46/53 | 85% | 63pp | 85% |

Verdict `tool_summary_anchored`, immediately disowned: 9% cheaper against a 10% spread, so the cost ranking is unresolved. What holds is a **30% smaller peak prompt at no measurable cost difference, losing nothing**.

### Run 17 — 120,000-token window

| strategy | cost | vs none | +- | hit% | peak | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tool_summary_anchored | $0.2715 | -8% | 9% | 90% | 102,652 | 46/53 | 48% | 52pp | 47% |
| truncation | $0.2792 | -5% | 12% | 84% | 92,158 | 27/53 | 52% | 48pp | 51% |
| **none** | $0.2945 | — | 5% | 94% | 149,562 | 53/53 | 93% | 15pp | 92% |
| **anchored** | $0.3541 | +20% | 3% | 84% | 107,562 | **53/53** | **100%** | 48pp | 100% |
| tool_result | $0.3758 | +28% | 6% | 85% | 118,899 | 39/53 | 67% | 41pp | 66% |

### Run 18 — 272,000-token window, 86% full

| strategy | cost | vs none | +- | hit% | peak | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tool_summary_anchored | $0.4049 | -10% | 7% | 89% | 194,041 | 18/53 | 31% | 72pp | 30% |
| truncation | $0.4484 | -1% | 3% | 89% | 207,591 | 30/53 | 56% | 22pp | 55% |
| **none** | $0.4517 | — | 36% | 94% | 233,332 | 53/53 | **76%** | 17pp | 83% |
| **anchored** | $0.4975 | +10% | 3% | 89% | 203,654 | **53/53** | **100%** | 31pp | 100% |
| tool_result | $0.5740 | +27% | 3% | 86% | 184,147 | 39/53 | 22% | 78pp | 21% |

**The most surprising row in the whole project is the control.** At 233,332 tokens `none` scored **76%**, while `anchored` scored **100%** from a prompt 13% smaller. The conversation fits the window, nothing was lost, and the uncompacted agent still answered worse. Above some size a shorter prompt is easier to answer from, and compaction stops being purely a cost question.

### The instrument findings behind these runs

Five, each of which would have produced a plausible wrong number:

**Pinning is per call, not per turn.** A turn's `tool_choice` reaches only its first call; the follow-up after a tool result is unconstrained. That is how the recall tool was called uninvited in every early run, and it is a source of fact-count variance in every pinned run ever taken here.

**A tool passed through per-call options is never executed.** `FunctionInvocationLayer` wraps `ChatMiddlewareLayer` and builds its tool map first, so the tool reaches the model and nothing answers its call. Tools must be registered with the harness; visibility cannot be controlled per call, only *permission* — hence the one-shot gate that makes the recall tool inert unless the middleware armed it.

**A middleware cannot see the history before the call.** `context.messages` holds only the new turn until the history middleware replaces it during the call, so the size check reads on the way out and the option is set on the way in next time.

**Attribution has to be tracked as a transition.** Counting "is there a record" per call reported 18 records for one, because the pre-call check never sees the history.

**Unpinned runs cannot be measured at this scale.** Five repeats of the uncompacted control varied **102%** in cost, and the strategy row gathered eight fewer facts than the control. Three repeats had shown 3%, which was luck.

---

## Runs 19 and 20 — what actually breaks the record

Run 18 showed `tool_summary_anchored` preserving 18 of 53 facts where run 16 preserved all 53, and the window series had scaled per-result size and total tool output together. Two more runs separate them.

**Run 19 raised the call count the cheap way and is confounded.** Sixteen tool calls of 8,000 tokens, but `--markers-per-tool` dropped from 8 to 3 to hold the fact count at 53. That changed the number of calls, the size of each result *and* the codes each result carried. The record scored 53/53 — but three variables had moved.

**Run 20 is the honest form.** `--filler-tool-turns` adds lookups whose results carry no codes, so six code-bearing results with eight codes each stay exactly as they were and ten code-free asides supply the extra calls and bulk. Closing questions skip the code-free scopes, so the denominator is unchanged at 53.

| run | bearing results | codes each | per result | asides | material | facts recalled |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 18 | 6 | 8 | 25,200 | 0 | 228,400 | 18/53 |
| **20** | **6** | **8** | **8,000** | **10** | 220,690 | **36/53** |
| 19 | 16 | 3 | 8,000 | 0 | 221,130 | 53/53 |

**Both variables matter.** Shrinking each result from 25,200 to 8,000 with the code count held at eight lifts recall from 18 to 36. Dropping the codes per result from eight to three lifts it from 36 to 53. Neither alone accounts for the collapse, and **total tool output is ruled out** as the driver: runs 19 and 20 carry comparable totals to run 18 and score very differently.

### Run 20 — 272,000-token window, 16 calls, six of them bearing

| strategy | cost | vs none | +- | hit% | peak | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tool_summary_anchored | $0.5930 | **-18%** | 4% | 94% | 162,881 | 36/53 | 61% | 39pp | 60% |
| **none** | $0.7244 | — | 5% | 97% | 228,556 | 53/53 | 100% | 15pp | 100% |
| truncation | $0.7387 | +2% | 14% | 94% | 207,822 | 21/53 | 41% | 2pp | 40% |
| anchored | $0.7701 | +6% | 2% | 94% | 209,030 | 43/53 | 78% | 4pp | 77% |
| tool_result | $0.8720 | +20% | 7% | 83% | 141,348 | 53/53 | 100% | 52pp | 100% |

`tool_result` reaching 53/53 here is worth noting: with sixteen groups it keeps the last four verbatim and head-truncates the rest, which happens to suit this shape. Its poor showings elsewhere were about result size rather than the mechanism.

A longer session is expensive whatever the strategy: sixteen calls mean 61-62 model calls against 29, and every row costs more than the equivalent six-call run.

---

## Run 21 — the freeze flag works, and measures nothing yet

Compaction had been running *through* the closing questions in every run above, because they go through the same `agent.run()` loop as the conversation. So the first scope was answered from a fuller context than the last, the combined question from the most compacted context of the run, and `survived` was computed against a prompt still being edited while it was scored. `--freeze-during-answers` stops the strategy at the first closing turn so that all the closing answers come from one history.

This run only checks the mechanism fires. One repeat, 120,000-token window, two strategies and the mandatory control:

| strategy | cost | vs none | hit% | peak | facts | correct | all | flags |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tool_summary_anchored | $0.2700 | **-19%** | 90% | 103,322 | 32/53 | 26% | 25% | `REC:1,FROZEN:16,FORCED:2,RECFORCED:1` |
| **none** | $0.3341 | — | 94% | 165,590 | 53/53 | 100% | 100% | — |
| anchored | $0.4101 | +23% | 84% | 113,489 | 32/53 | 56% | 58% | `FROZEN:16` |

**The flag does what it claims.** Sixteen compaction calls suppressed per strategy, so the closing turns were genuinely still compacting before this and the arms will differ. The recall mechanism still completes *ahead* of the freeze — `REC:1` with `RECFORCED:1` and no `RECVOLUNTEERED` — which is the ordering the design needs: phase 1 must have produced its record before phase 2 is switched off. `none` correctly carries no note, having no strategy to freeze.

**Do not read the accuracy columns as freeze damage.** One repeat means no error bars, and the run is unpinned (`--no-force-tool-calls`), which alone moves this figure: `anchored` scored 27/53 at 60,000 in run 16, 53/53 at 120,000 in run 17, and 22/53 unpinned in the void run. 32/53 sits inside that spread. Sizing the freeze costs a paired frozen/unfrozen run at three or more repeats, which has not been done.

**Found by running it:** `cachebench_live --help` crashed on an unescaped `%` in two help strings. argparse `%`-formats help text, so one bare percent sign makes the whole parser unprintable while parsing and running continue to work perfectly — the CLI was simply undiscoverable. Now covered by a test that formats the help.

---

## Run 22 — the freeze hypothesis, refuted, and a worse problem behind it

Paired arms at 272,000, three repeats each, run back to back. The profile is run 18's exactly apart from the flag and the dropped `tool_result` row, so **the unfrozen arm is also a replication of run 18**. That turned out to matter more than the flag.

### Unfrozen (the default, and a replication of run 18)

| strategy | cost | vs none | +- | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tool_summary_anchored | $0.4402 | -3% | 9% | 53/53 | 100% | **0pp** | 100% |
| truncation | $0.4472 | -1% | 2% | 30/53 | 17% | 59pp | 17% |
| **none** | $0.4527 | — | 1% | 53/53 | 100% | 26pp | 100% |
| anchored | $0.4873 | +8% | 11% | 39/53 | 70% | 13pp | 72% |

### Frozen

| strategy | cost | vs none | +- | facts | correct | c+- | all |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tool_summary_anchored | $0.4055 | -10% | 4% | 18/53 | 35% | 13pp | 34% |
| truncation | $0.4434 | -2% | 1% | 30/53 | 30% | 59pp | 30% |
| **none** | $0.4505 | — | 1% | 53/53 | 81% | 19pp | 85% |
| anchored | $0.4911 | +9% | 2% | 44/53 | 54% | 37pp | 57% |

### The hypothesis is dead

Freezing was supposed to narrow the correctness spread, on the theory that eviction firing mid-sequence in some repeats and not others produced the bimodality. **It narrowed nothing.**

| strategy | c+- unfrozen | c+- frozen |
| --- | ---: | ---: |
| none | 26pp | 19pp |
| truncation | 59pp | 59pp |
| anchored | 13pp | **37pp** |
| tool_summary_anchored | 0pp | **13pp** |

Two rows widened, one was unchanged, and the control moved less than its own repeat range. Mid-answer eviction is not the source of the spread. The confound is real — compaction does keep firing through the closing turns, 16 suppressed calls per strategy prove it — but it is not what makes the accuracy figure jump.

### The finding that displaces it: one sample per configuration is not enough

The unfrozen arm and run 18 are the same command. They disagree by more than the freeze does:

| strategy | run 18 (5 repeats) | run 22 unfrozen (3 repeats) | run 22 frozen |
| --- | ---: | ---: | ---: |
| tool_summary_anchored | 18/53, **31%** | 53/53, **100%** | 18/53, 35% |
| truncation | 30/53, **56%** | 30/53, **17%** | 30/53, 30% |
| none | 53/53, **76%** | 53/53, **100%** | 53/53, 81% |
| anchored | 53/53, **100%** | 39/53, **70%** | 44/53, 54% |

Every row moves, in both directions, by up to 69 points. Note also where the frozen arm lands: on `tool_summary_anchored` it matches run 18 — 18/53 facts, about a third correct, -10% cost -- while the *unfrozen* arm is the outlier on all three. If the freeze were driving this, the frozen arm should be the one that differs from run 18. It is not.

> **Corrected by run 23.** This section first concluded that the answering mode is fixed per > *invocation*, because `tool_summary_anchored` scored 100% three times running in one arm and > about 35% three times running in the other. That over-read a small sample. Run 18's own `c+-` > for that strategy is **72pp**, which is large within-invocation spread, and with a roughly > even two-sided distribution three repeats land on the same side a quarter of the time. Run 23 > then measured both spreads directly at 60,000 and found them the same size. The variance is > **per repeat**, and `c+-` does report it — it was simply not being respected.

The claim that survives is the weaker and more useful one: **a single sample of a configuration says very little about its accuracy**, and the accuracy tables in runs 16 to 20 are one sample each. Cost is unaffected: `+-` runs 1-11% and the ordering held across all three runs.

Cost: about $10.9, roughly EUR 10, for both arms.

---

## Run 23 — the spread is per repeat, and it is compaction's, not the model's

Six independent invocations of one command at 60,000, one repeat each, on run 16's exact profile. Run 16 ran the same command with five repeats *inside* one invocation, so the two measure the same thing at different levels.

| strategy | inv 1 | 2 | 3 | 4 | 5 | 6 | across invocations | run 16, within one |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 98% | 93% | 100% | 96% | 96% | 93% | **7pp** | **7pp** |
| tool_summary_anchored | 100% | 100% | 100% | 74% | 74% | 100% | **26pp** | **59pp** |
| anchored | 52% | 98% | 100% | 52% | 22% | 52% | **78pp** | **78pp** |

**The two spreads are the same size**, and for `tool_summary_anchored` the within-invocation one is larger. There is no invocation-level effect to find: repeats inside a run vary as much as runs do, so `c+-` is the honest error bar and always was.

### The spread belongs to compaction, not to the model

The control varies by 7 points across six invocations while `anchored` varies by 78. Whatever drives the instability, it is not the model deciding how thoroughly to answer — that would move the uncompacted row too.

**`facts` is discrete, which is what a cliff looks like.** Across six invocations the survival counts take a handful of values and nothing between them:

| strategy | facts survived, by invocation | distinct values |
| --- | --- | --- |
| none | 53, 53, 53, 53, 53, 53 | one |
| tool_summary_anchored | 53, 53, 53, **39**, **39**, 53 | **two** |
| anchored | **27**, **52**, 53, 27, 27, 27 | **three** |

Correctness follows them: `tool_summary_anchored` is 100% whenever 53 survive and 74% whenever 39 do, with no intermediate result in six tries. The scenario salt changes on every invocation, which moves where the planted facts fall relative to a retention boundary; the strategy then either clears that boundary or does not.

A second, smaller source sits on top. `anchored` scored 52%, 52%, 52% and 22% on the four invocations where exactly 27 facts survived — same material available, different amount of it quoted. That is the model's enumeration varying, and it is worth about 30 points against compaction's 78.

### Cost is steadier, but not uniformly

| strategy | median | across six | spread | position |
| --- | ---: | ---: | ---: | --- |
| tool_summary_anchored | $0.1560 | $0.1488–$0.1855 | **25%** | cheapest in 5 of 6 |
| none | $0.1664 | $0.1596–$0.1700 | 6.5% | middle in 5 of 6 |
| anchored | $0.1927 | $0.1879–$0.2017 | 7.3% | dearest in 6 of 6 |

The control and `anchored` sit around 7%, which is the ordinary run-to-run figure. But `tool_summary_anchored` swings **25%**, and on invocation 4 it came out 10% *dearer* than the control rather than 9% cheaper — the one invocation where its saving reversed sign.

That is the same defect showing up in the other column. A strategy whose record is sometimes complete and sometimes not writes a different amount of text and carries a different prompt, so its cost inherits the bimodality of its accuracy. **`anchored`'s cost claim is safe at one sample; `tool_summary_anchored`'s is not**, even though its accuracy and cost do not move together — invocations 4 and 5 both scored 74% at $0.1855 and $0.1488.

Cost: about $3.1, roughly EUR 2.8.

---

## Runs 26 and 27 — the rebuilt instrument, four cells

The first results from the seed/snapshot/probe design, on `gpt-5.4-mini-2`. Five seeds per cell, one probe repeat, five attempts at the combined question, payload fixed at 3,500-token tool results with eight codes each. **120 records across the four cells, no errors, no throttling, no disqualifications.** `acc1` is the seven per-scope questions, `acc2` the one combined question, `snap%` the share of the tried window the probes were answered from.

| cell | none | truncation | anchored | anchored_min_gain | tool_summary_anchored | context_window |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 60K / 0.86 | — | +2% | +17% | +12% | **+10%** | +58% |
| 120K / 0.50 | — | -1% | -1% | +3% | +26% | +27% |
| 120K / 0.70 | — | +0% | +2% | -2% | +20% | **+113%** |
| 120K / 0.86 | — | -1% | +6% | +3% | +22% | **+141%** |

*Cost against the uncompacted control. Read against control spreads of 9-21%: only the `context_window` figures and `tool_summary_anchored`'s premium are outside the noise.*

### The shipped default gets worse the more it is needed

`context_window` is the strategy `create_harness_agent` installs. Its cost against not compacting rises with fill, and its cache hit rate falls as it does:

| fill | vs none | hit% | snap% | facts | acc1 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.50 | +27% | 88% | 46% | 41/53 | 72% |
| 0.70 | +113% | 66% | 47% | 23/53 | 43% |
| 0.86 | **+141%** | **57%** | 47% | 21/53 | 38% |

It compacts to the same 47% of the window at every fill, so the fuller the conversation the more it throws away -- and the more of the surviving prefix it rewrites, which is what the hit rate is measuring. At 0.86 it costs **two and a half times** not compacting and answers 38% of what the control answers. Both axes, worst row, every cell.

### The two anchored rows swap places with the window

`anchored` keeps a share of the *ceiling*, so a bigger window makes it more generous:

| cell | anchored | facts | acc1 | acc2 |
| --- | ---: | ---: | ---: | ---: |
| 60K / 0.86 | +17% | 51/53 | 94% | 87% |
| 120K / 0.70 | +2% | 53/53 | 98% | 68% |
| 120K / 0.86 | **+6%** | **53/53** | **99%** | **100%** |

At 120K/0.86 it is the best faithful row in the sweep: everything preserved, both accuracy measures at or above the control, six percent dearer.

`anchored_min_gain` declines a collapse whose saving cannot repay the cache invalidation. At 60K it declined every one and came 5% under plain `anchored`; at 120K the two are within a point or two of each other and of the control. **The floor is worth having and is not worth much** -- it prevents a specific waste rather than making compaction pay.

### The record is reliable until the bulk defeats it

`tool_summary_anchored` was the only row perfect on both accuracy measures in three of four cells -- 53/53, 100%, 100%, with zero spread on either measure -- and the only row that ever beat the control on `acc2`. It costs 10 to 26% more, and the premium is its own output: 17,628 tokens against the control's 10,417 at 120K/0.70.

At 120K/0.86 it breaks: 47/53 facts, `acc1` 90%, and a seed spread of 52 points. That is the same failure the window series found before -- the record degrades with the material it must read -- arriving here at about 96,000 tokens of context.

### What no cell shows

**No strategy is cheaper than not compacting with its answers intact.** Every row that reads below the control -- `truncation` at -1%, `anchored_min_gain` at -2% -- is inside a control spread of 9 to 21%, and `truncation` pays for its at 0.86 by losing 30 of 53 facts.

Compaction here buys the ability to continue past the window, not a lower bill.

Cost: about $34, roughly EUR 31.

---

## Runs 28 and 29 — upstream #7912, before and after

[#7912](https://github.com/microsoft/agent-framework/pull/7912) rewrote the tool-eviction phase of `ContextWindowCompactionStrategy`, the strategy `create_harness_agent` installs. It was a `TokenBudgetComposedStrategy` called unconditionally, whose built-in fallback evicts whole groups oldest-first; it is now a `ToolResultCompactionStrategy` called only when the prompt exceeds the eviction threshold. Runs 26 and 27 were taken before the change and kept as the before arm.

Run 29 is the controlled comparison: the same cell, the same lab code, five seeds either side, and 50 upstream commits between them of which one touches compaction.

| 120,000 / 0.86 | before | after |
| --- | ---: | ---: |
| `snap%` | 47% | 57% |
| cache hit rate | **57%** | **91%** |
| cost | $0.9821 | $0.4579 |
| vs none | **+141%** | **+14%** |
| facts | 21/53 | 28/53 |
| `acc1` | 38% | 54% |
| *control cost* | *$0.4069* | *$0.4029* |

**The control moved 1%**, which is what makes the rest of the column readable.

**The cost result is solid; the recall result is not.** `snap%` moved 47% to 57%, so the eviction phase discards less -- it fires only above its threshold now and no longer sheds whole groups -- and less discarded is less prefix rewritten, which is the cache recovery. That chain holds.

Recall does not. Run 29 shows 21/53 becoming 28/53, but **run 28 shows no such gain**: at a 100,000-token window, also post-fix, `context_window` reads 21/53 and `acc1` 40%, exactly where the before arm sat. The two after-arm cells disagree, and `context_window`'s `acc1` seed spread in run 29 is 59 points. Treat the recall half as unresolved.

Two things about the cost figure that the number alone hides. **The distance is the result, not the destination**: +14% sits inside the after-arm control's own 15% cost spread and inside `context_window`'s 26%, so what is licensed is "no longer measurably dearer than not compacting", not "14% dearer". And the control moved 1% *on cost* -- its `acc1` moved 98% to 94% with a 20-point spread, so the accuracy rows are read against a baseline that moved too.

Run 28 agrees on cost and cache: +14% against its control at a 90% hit rate. It does not agree on recall.

**It also moved a strategy of ours.** `tool_summary_anchored` went from 47/53 with `acc1` 90% and a 52-point seed spread to 53/53, 100%, and no spread, at +16% rather than +22%. #7912 also made compaction results persist across the chat-middleware boundary, which is a plausible cause and is not isolated here. `truncation`, `anchored` and `anchored_min_gain` are unchanged within their spreads.

Cost: about $24 for the two cells, roughly EUR 22.

---

## Runs 32 and 33 — the whole matrix, on a repaired instrument

Everything before this is withdrawn on cost. An adversarial review found two defects that sat under every `vs none` figure in the project — the control silently ran a shorter conversation than every strategy row, and `cost` summed the workload with twelve probe re-reads that scale with whatever compaction had just removed. See `REVIEW-2026-09-02.md`.

Both are fixed, along with two strategy defects and the combined question's wording. These nine cells are the first measurement where the control runs the conversation the strategies run, and where **cost is split into the workload (`seed$`) and the instrument (`probe$`), with the ranking on the workload.** 300 records, five seeds a cell, $84.69.

### Cost against not compacting, on the workload axis

| cell | tool_summary | anchored | min_gain | truncation | context_window | control spread |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed 60K / 0.86 | +47% | +10% | +1% | +15% | +29% | 17% |
| fixed 120K / 0.50 | +25% | -3% | +5% | +5% | +40% | 14% |
| fixed 120K / 0.70 | +27% | -1% | +3% | +7% | +34% | 21% |
| fixed 100K / 0.86 | +13% | -7% | +7% | +17% | +17% | 11% |
| fixed 120K / 0.86 | +9% | +3% | +4% | -2% | +11% | 21% |
| share 0.60, 120K / 0.86 | +8% | -1% | -1% | +10% | +30% | 16% |
| share 0.80, 120K / 0.50 | +12% | -2% | -2% | -2% | +17% | 31% |
| share 0.80, 120K / 0.70 | +11% | +3% | +6% | -2% | +32% | 3% |
| share 0.80, 120K / 0.86 | **-3%** | -2% | +1% | +7% | +25% | 7% |

**Not one resolvable saving anywhere.** Every negative figure sits inside its own control's spread. Read the last column before any other.

### The shipped default is dearer in all nine cells

`context_window` runs **+11% to +40%** against not compacting, in every cell, on the workload axis, **after #7912**. The fix halved its old penalty and it is still the most expensive row in seven of nine cells. It is also worst or near-worst on recall throughout: 25 to 50 of 53 facts.

That is the clearest result the project has, and it is the one a reader should act on.

### The record reaches parity, once, at the most favourable cell

`tool_summary_anchored` costs **+8% to +47%** in eight cells and reads **-3%** in the ninth — highest fill, highest tool share — against a 7% control spread, so parity rather than a saving. Its cost falls monotonically as fill and tool share rise, which is a real gradient, and it never resolvably crosses zero.

**The earlier claim that it pays when tool output dominates is withdrawn.** That -14% was a combined-column figure inflated by twelve cheap probes.

What it does buy is headroom. At share 0.80 and 0.86 fill it answered from **45% of the window where the control needed 88%**, with all 53 facts and full marks on both accuracy measures. Same work, half the context, the same price.

### The anchored family is the free option, mostly by doing little

`anchored` is within ±7% everywhere and `anchored_min_gain` within ±7% too. Neither costs anything; neither saves anything. The repaired floor now behaves as designed — at 60K it declines every collapse (`NOGAIN:54`), matches the control on cost and keeps all 53 facts, where the unfloored parent acts and loses one for +10%.

### The combined question now measures recall

Stating its counts moved the control's `acc2` at 60K/0.86 from 58% to **100%**, and `acc2` now tracks `acc1` instead of contradicting it. The old bimodality -- 226 of 240 samples at either every value or exactly eleven -- was the model reading "every code returned by every lookup" as *the* return code of each, answering that correctly, and being scored as a recall failure.

### The one damaged cell, repaired

`fixed 120K / 0.70` lost 8 of 30 records to a connection outage longer than the retry budget. Its two seeds were re-run at the same offsets, so the scenario salt rebuilt the conversations that had failed rather than fresh ones, and the cell is now five clean seeds like every other. The retry behaved as designed throughout: it survives blips, not outages.

Re-reading it moved nothing that matters -- `anchored` -1% rather than -3%, `truncation` +7% rather than +12% -- but the control's own spread went from 7% to **21%**, which is the more useful correction: the three surviving seeds had understated the noise, and on five seeds nothing in that cell is resolvable at all.

Cost: $84.69, roughly EUR 78.

---

## Runs 34 and 35 — the same matrix on a second model

`gpt-5.6-luna`, same deployment, same repaired instrument, nine cells at 60,000, 120,000 and 200,000 tokens. 270 records, five seeds a cell, no errors, $36.08. Both models carry the same **10x cache discount**, which is what the break-even says should govern the outcome.

| cell | tool_summary | anchored | min_gain | truncation | context_window | control spread |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed 60K / 0.86 | +56% | +83% | +55% | +31% | +29% | 22% |
| fixed 120K / 0.50 | +37% | -7% | -6% | -2% | +27% | 29% |
| fixed 120K / 0.70 | +27% | +6% | +16% | +7% | +34% | 35% |
| fixed 120K / 0.86 | +47% | +51% | +52% | +10% | +0% | 20% |
| fixed 200K / 0.86 | +39% | +67% | +101% | +11% | +4% | 35% |
| share 0.60, 120K / 0.86 | +11% | +23% | +51% | +5% | +39% | 26% |
| share 0.80, 60K / 0.86 | +6% | +31% | +18% | +14% | +17% | 12% |
| share 0.80, 120K / 0.86 | +9% | -2% | +5% | +13% | +20% | 6% |
| share 0.80, 200K / 0.86 | **-8%** | +3% | +4% | +1% | +22% | 12% |

### What transfers between the models

**Nothing is resolvably cheaper than not compacting.** Eighteen cells now, across two models and four window sizes. Every negative figure sits inside its control's spread, on both.

**The cache penalty is the mechanism, on both.** Whenever a strategy edits the prefix its hit rate falls -- to **56-97%** on luna against controls at 94-98% -- and the cost follows. (An earlier version of this line said 75-91%. Recomputed from the 270 records: the compacting rows span 56% to 97%, so the penalty is both deeper and more variable than stated, and the ranges overlap the controls at the top end.)

**`context_window` is dearest or near-dearest almost everywhere**, +17% to +39% on luna in seven of nine cells. The two exceptions are instructive rather than reassuring: at 120,000/0.86 and 200,000/0.86 it reads +0% and +4% *because it discards so much that it stops paying for what it kept* -- 20 of 53 facts in both, `acc1` 40%.

### What does not transfer, and it is the useful half

**Which strategy is least bad is model-specific.** At the cell where `gpt-5.4-mini` gave its one parity result -- share 0.80, 120,000, 0.86 fill -- the two models disagree completely:

| | `gpt-5.4-mini` | `gpt-5.6-luna` |
| --- | ---: | ---: |
| `tool_summary_anchored` vs none | **-3%** | +9% |
| facts kept | **53/53** | 40/53 |
| `acc1` | **100%** | 76% |

The record is only as good as the model writing it, and luna writes a much worse one: 21 of 53 facts at the fixed payload where 5.4-mini kept all 53. So the break-even predicts the **direction** on both models at the same discount, and predicts **nothing** about which strategy to choose. Any recommendation naming a strategy has to be measured on the model being deployed.

### `tool_summary_anchored` on luna writes one record covering two lookups of six

The row that led the 5.4-mini matrix collapses on luna -- 21 of 53 facts where mini kept 53. **An earlier version of this section blamed `TRUNCATED`, and that was wrong.** Run 36 measured it and the cap is not the cause. The correlation was real and the causation was not; what follows is the measured version.

**Run 36, fixed 60K / 0.86, three seeds an arm, `truncation` carried as a reference row:**

| arm | record facts | record `vs none$` | truncation facts | truncation `vs none$` |
| --- | ---: | ---: | ---: | ---: |
| cap 4,000 / target 2,000 (runs 34/35, x5) | 21/53 | +56% | 19.6/53 | +31% |
| cap **24,000** / target 2,000 (x3) | 21/53 | +57% | 17.7/53 | **-1%** |
| cap 24,000 / target **8,000** (x3) | 21/53 | **+95%** | 19.0/53 | +10% |

**Eleven records at this cell, one number.** A 6x cap and a 4x stated target changed nothing about what the record covers. Raising the target only made it dearer.

Three findings pin the mechanism, and none of them is the cap:

1. **Truncation and fact loss are decoupled.** One seed at cap 24,000 still tripped `TRUNCATED` -- luna ran past **24,000 tokens** -- and returned the same 21/53 as the seeds that finished cleanly. No mini record ever hit the ceiling at a 4,000 cap in 45 records, so every mini record was under 4,000 tokens; luna's is more than six times that bound and covers a third as much.
2. **Loss does not track how much was compacted.** Across all 45 luna records, facts kept against snapshot shrink correlates at **r = 0.05**. One row shrank 8% and lost 32 facts; another shrank 64% and lost none.
3. **`REC:1` in all 45 records** -- exactly one record is ever written -- and every fact count in the set is `5 + 8k`, five non-tool facts plus a whole number of eight-code lookups. What survives is exactly what that one record enumerated: six lookups, two, or one.

So luna writes at great length about the first two lookups and never reaches the other four. That is a property of the model's writing, not of a setting, and no bound reachable from the CLI moved it.

## Final report on agent-framework-core 1.20.0: run 67, 6 October

**Inside the context window, don't compact. Past it, `tool_and_user_summary_anchored` is the only row that keeps every fact inside a 120K window on both models: on gpt-6-luna 22% below an unlimited model at 1.5x the window and 65% below at 3x on every seed; on gpt-5.6-luna 18-35% below on four seeds of five at 1.5x, and at 3x it holds on four seeds and fails loudly on the fifth. What decides the row on gpt-5.6-luna is the quality of the record the model writes.** This section supersedes the run 63 report for every number; the archived records and per-run notes are in [`runs/run-67-core-1.20-grid/`](../../../packages/maf-cachebench/runs/run-67-core-1.20-grid/) and [`runs/README.md`](runs-history.md), and [`reports/run67-vs-run63.md`](../../../packages/maf-cachebench/runs/run-67-core-1.20-grid/reports/run67-vs-run63.md) puts every row of every cell beside its run 63 counterpart.

### Setup

The run 63 cell, unchanged: all twenty strategies, the MAF harness agent, a simulated 120,000-token window, five seeds per cell, six tool lookups carrying 60% of the tokens and 53 planted facts, filler user turns carrying the rest, reservations of 2,048 output tokens and 12,000 for the closing answers.

| Setting | Value |
| --- | --- |
| Framework | agent-framework-core 1.20.0, agent-framework-foundry 1.14.0, agent-framework-openai 1.15.0 |
| Lab | `14ded49cb` |
| Models | gpt-5.6-luna (Foundry project endpoint); gpt-6-luna (Azure resource endpoint via `azure-responses`) |
| Prices per 1M tokens | 5.6-luna $0.20 in / $0.02 cached / $1.20 out; 6-luna $0.10 / $0.01 / $0.50, cache writes $0.125, and above 272K input per request $0.20 / $0.02 / $0.75, cache writes $0.25 |
| Fills | 0.9, 1.5, 3.0 |
| Spend | $126.85 including probes |

`seed$` is what the conversation cost, summarizer included, probes excluded. `DQ` is a prompt over 120K at any point. The control disqualifies at 1.5 and 3.0 and is a price reference, not a baseline. A row keeps the facts when its acc1 is at least 90% of the control's. gpt-6-luna's fill-3.0 control is priced at its long-context rates for the calls above 272K, which run 63 did not do; that is the one pricing difference between the two reports.

### Results

Every row kept 53/53 on every seed where it is shown; `-` is below the accuracy bar. Seed$ against the control, mean of five seeds, disqualified seeds in brackets.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none (control) | $0.068 | $0.150 (5) | $0.443 (5) | $0.036 | $0.075 (5) | $0.313 (5) |
| tool_and_user_summary_anchored | $0.073 | $0.143 | $0.792 (1) | $0.035 | **$0.059** | **$0.109** |
| tool_summary_anchored | $0.070 | $0.308 (2) | $0.942 (5) | $0.036 | $0.051 | $0.310 (5) |
| user_summary_anchored | $0.085 | $0.221 (5) | $0.590 (5) | $0.044 | $0.111 (5) | $0.301 (5) |
| anchored_min_gain | $0.079 | - | - | $0.034 | - | - |

- **0.9:** the best complete rows cost within 8% of not compacting on gpt-5.6-luna and within 3% on gpt-6-luna, inside the seed spread. Not compacting stays the default inside the window.
- **1.5, gpt-6-luna:** `tool_summary_anchored` is cheapest at -32% and the composed row -22%, both complete on every seed. **1.5, gpt-5.6-luna:** the composed row is the only complete row with no disqualification; it ran 18-35% under the control on four seeds and 93% over it on the fifth, whose records left five tool groups uncovered, so its mean is -4%. The record row disqualified on two seeds for the same reason.
- **3.0, gpt-6-luna:** the composed row kept 53/53 inside the window on every seed at 63-68% under the control. **3.0, gpt-5.6-luna:** three seeds 30-56% under, one 13% over, one disqualified at $2.74 with every fact after 172 fallbacks: the loud failure, not a quiet loss, and the mean it drags to +79% is that one seed.
- The other sixteen strategies lose facts past the window or disqualify. Core's `summarization`, bounded to the cell, is the best of them on gpt-6-luna at 47 / 38 / 30 facts, for 18-20% more than the control at 0.9 and 1.5 and an 11% cache share.

### All twenty strategies

Each cell reads facts kept (mean of 53) / acc1 / disqualified seeds of 5. The same rows laid out per fill with both models side by side are under run 67 in [`runs/README.md`](runs-history.md).

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none | 53 / 100% / 0 | 53 / 97% / 5 | 53 / 90% / 5 | 53 / 100% / 0 | 53 / 100% / 5 | 53 / 84% / 5 |
| tool_and_user_summary_anchored | 53 / 100% / 0 | 53 / 94% / 0 | 53 / 94% / 1 | 53 / 100% / 0 | 53 / 98% / 0 | 53 / 100% / 0 |
| tool_summary_anchored | 53 / 100% / 0 | 53 / 100% / 2 | 53 / 97% / 5 | 53 / 100% / 0 | 53 / 97% / 0 | 53 / 97% / 5 |
| user_summary_anchored | 53 / 100% / 0 | 53 / 97% / 5 | 53 / 92% / 5 | 53 / 99% / 0 | 53 / 96% / 5 | 53 / 89% / 5 |
| anchored_min_gain | 53 / 94% / 0 | 46 / 87% / 0 | 13 / 26% / 5 | 53 / 100% / 0 | 28 / 54% / 0 | 13 / 26% / 5 |
| anchored | 53 / 96% / 0 | 40 / 75% / 0 | 13 / 26% / 5 | 47 / 88% / 0 | 31 / 59% / 0 | 13 / 26% / 5 |
| anchored_no_assistant | 52 / 97% / 0 | 28 / 53% / 0 | 25 / 46% / 5 | 47 / 89% / 0 | 29 / 55% / 0 | 25 / 52% / 5 |
| summarization | 20 / 38% / 0 | 17 / 25% / 0 | 17 / 26% / 0 | 47 / 75% / 0 | 38 / 74% / 0 | 30 / 31% / 0 |
| token_budget_summarize | 29 / 46% / 0 | 24 / 47% / 0 | 20 / 36% / 0 | 28 / 46% / 0 | 34 / 61% / 0 | 44 / 74% / 0 |
| context_window | 27 / 52% / 0 | 15 / 29% / 0 | 16 / 32% / 0 | 42 / 80% / 0 | 22 / 42% / 0 | 13 / 30% / 0 |
| context_window_lazy | 53 / 95% / 0 | 22 / 43% / 0 | 14 / 28% / 0 | 42 / 79% / 0 | 22 / 41% / 0 | 15 / 30% / 0 |
| context_window_aggressive | 20 / 39% / 0 | 14 / 28% / 0 | 3 / 8% / 3 | 24 / 46% / 0 | 10 / 20% / 0 | 0 / 2% / 1 |
| tool_result | 50 / 90% / 0 | 47 / 87% / 5 | 43 / 76% / 5 | 39 / 74% / 0 | 39 / 71% / 5 | 39 / 71% / 5 |
| selective_tool_call | 50 / 94% / 0 | 47 / 89% / 5 | 44 / 70% / 5 | 44 / 84% / 0 | 40 / 76% / 5 | 41 / 70% / 5 |
| truncation | 35 / 66% / 0 | 18 / 35% / 0 | 17 / 33% / 0 | 31 / 59% / 0 | 19 / 36% / 0 | 15 / 30% / 0 |
| sliding_window | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 |
| token_budget_fallback | 21 / 41% / 0 | 18 / 36% / 0 | 9 / 19% / 0 | 23 / 45% / 0 | 17 / 34% / 0 | 9 / 18% / 0 |
| token_budget_tools_first | 19 / 37% / 0 | 18 / 34% / 0 | 8 / 17% / 0 | 19 / 36% / 0 | 14 / 27% / 0 | 8 / 17% / 0 |
| token_budget_truncate_first | 20 / 39% / 0 | 17 / 34% / 0 | 10 / 20% / 0 | 18 / 35% / 0 | 16 / 32% / 0 | 8 / 17% / 0 |
| token_budget_window_first | 10 / 20% / 0 | 11 / 22% / 0 | 8 / 17% / 0 | 16 / 31% / 0 | 10 / 20% / 0 | 8 / 17% / 0 |

Seed$ per cell for the same rows, mean of five seeds. A cheap row that lost facts or disqualified is not a saving.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none | 0.0676 | 0.1499 (5) | 0.4429 (5) | 0.0364 | 0.0746 (5) | 0.3134 (5) |
| tool_and_user_summary_anchored | 0.0733 | 0.1434 | 0.7916 (1) | 0.0353 | 0.0585 | 0.1088 |
| tool_summary_anchored | 0.0700 | 0.3078 (2) | 0.9415 (5) | 0.0357 | 0.0511 | 0.3101 (5) |
| user_summary_anchored | 0.0850 | 0.2213 (5) | 0.5898 (5) | 0.0436 | 0.1109 (5) | 0.3012 (5) |
| anchored_min_gain | 0.0785 | 0.1916 | 0.5946 (5) | 0.0343 | 0.0915 | 0.3487 (5) |
| anchored | 0.0754 | 0.1847 | 0.6082 (5) | 0.0374 | 0.0937 | 0.3259 (5) |
| anchored_no_assistant | 0.0760 | 0.2114 | 0.3600 (5) | 0.0375 | 0.0933 | 0.3095 (5) |
| summarization | 0.1379 | 0.2131 | 0.4077 | 0.0896 | 0.1318 | 0.2535 |
| token_budget_summarize | 0.0927 | 0.1527 | 0.3093 | 0.0440 | 0.0811 | 0.1673 |
| context_window | 0.0904 | 0.1254 | 0.2667 | 0.0486 | 0.0641 | 0.1222 |
| context_window_lazy | 0.0942 | 0.1608 | 0.3012 | 0.0483 | 0.0785 | 0.1654 |
| context_window_aggressive | 0.0655 | 0.0994 | 0.1511 (3) | 0.0337 | 0.0496 | 0.0785 (1) |
| tool_result | 0.0946 | 0.1784 (5) | 0.4934 (5) | 0.0488 | 0.0934 (5) | 0.3053 (5) |
| selective_tool_call | 0.0998 | 0.1954 (5) | 0.5220 (5) | 0.0491 | 0.0978 (5) | 0.2897 (5) |
| truncation | 0.0758 | 0.1365 | 0.2372 | 0.0397 | 0.0665 | 0.1210 |
| sliding_window | 0.0756 | 0.1214 | 0.2358 | 0.0447 | 0.0719 | 0.1400 |
| token_budget_fallback | 0.1286 | 0.2629 | 0.4296 | 0.0739 | 0.1276 | 0.2404 |
| token_budget_tools_first | 0.0917 | 0.1408 | 0.1935 | 0.0467 | 0.0662 | 0.0949 |
| token_budget_truncate_first | 0.0911 | 0.1241 | 0.1926 | 0.0473 | 0.0696 | 0.0935 |
| token_budget_window_first | 0.0583 | 0.0834 | 0.1476 | 0.0268 | 0.0451 | 0.0746 |

### What changed since run 63, and why

Run 63 measured this grid on core 1.16.0. Between 1.16 and 1.20 four things moved the numbers, and one did not:

1. **Core's summarizer sees tool text (1.18, #8087).** The framework's summarizer transcript rendered a tool result as the bare word `function_result`, so the `summarization` and `token_budget_summarize` rows summarised conversations whose facts were already gone. Since 1.18 the transcript carries the result text, and the framework's 8,000-token summarizer input budget then skips any group larger than that, which on this cell is every tool result: the row never compacts, re-summarises the small groups on every call and loses the cache (run 66: $0.98 for a row the control ran at $0.11). The lab now bounds the summarizer input by the cell's input budget, so the rows measure the strategy as it would be configured. On gpt-6-luna that is 47 / 38 / 30 facts against 29 / 25 / 30; on gpt-5.6-luna no change.
2. **The context-window strategy re-measures before it truncates (1.17, #7912).** The default `context_window` row kept 27 / 15 / 16 facts on gpt-5.6-luna against 43 / 19 / 17; unchanged on gpt-6-luna.
3. **The Foundry client keeps a copy of the replayed reasoning item (1.20, #8670)**, encrypted payload included, under the content's `additional_properties`, below where core strips it. On gpt-5.6-luna every assistant message counted at about ten times its billed size and every compacting row shed far too early; the first measurement of that half is set aside. The lab's stamp wrapper now drops the nested payload before counting.
4. **The harness restores a strategy's carried decisions with the session before every probe.** The composed chain's wait for a record and the record half's outstanding ask lived on the instance and survived the probe restore, so a wait that expired on the first probe acted on every probe after it. Measured as context drift on 11 of 12 probes on one row; now zero.
5. **Our strategies' code did not change**, and their gpt-6-luna rows reproduce run 63 to the cent at 0.9 and 1.5. The composed row's 3.0 result on gpt-6-luna improved from four seeds to five; its gpt-5.6-luna results moved with the model's record quality, which varies by seed.

### Why strategies fail

Unchanged from the run 63 report: user text outgrows the window at 3x and only a row that summarises user turns can fit; an incomplete record leaves its lookups whole rather than lost, and at 3x three missed lookups are ~107K nothing may shed, which is the composed row's loud failure on one gpt-5.6-luna seed and `tool_summary_anchored`'s disqualifications; and the built-in rows evict by age, re-decide the prefix, or never touch user text.

### Measurement notes

- The gpt-5.6-luna half ran alone, after gpt-6-luna's fill-3.0 cells had finished. Run beside them, its control's cache share fell to 77-93% from 96% with no throttling of its own: the shared prompt cache evicting under the neighbour's 360K prompts. Cost on one deployment is not a measurement while another saturates the resource.
- gpt-5.6-luna's fill-3.0 cells were throttled 900-1,600 seconds each on five streams, the control's cache share at 98% throughout; 1,812 retries in the run, none exhausted.
- One row failed, `tool_result` at gpt-5.6-luna fill 3.0 on seed 2, a Foundry service error at turn 78; it was re-run with its control and the originals set aside.
- Three rows had a summarizer call fail: one of fifteen on the composed row's fifth seed at gpt-5.6-luna 1.5, two of about 98 on `summarization` at gpt-5.6-luna 3.0. The summarizer path swallows a throttled call rather than retrying it; none changed a verdict.

## Final report: runs 63 and 64, 26 September

**Superseded for its numbers by the run 67 section above, which re-measured this grid on agent-framework-core 1.20.0.** The verdicts below were measured on core 1.16.0; where the two disagree, run 67 is current.

**Inside the context window, don't compact. Past it, only `tool_and_user_summary_anchored` keeps every fact inside a 120K window: 19-23% below an unlimited model at 1.5x the window, and at 3x it holds on four seeds of five per model and fails loudly on the fifth.** This section supersedes the verdicts of the earlier runs where they conflict; the archived records and per-run notes are in `runs/run-63-final-grid/`, `runs/run-64-record-repeats/` and [`runs/README.md`](runs-history.md).

### Setup

All twenty strategies, the MAF harness agent, a simulated 120,000-token window, five seeds per cell. Six tool lookups carry 60% of the tokens and 53 planted facts; filler user turns carry the rest. Reservations: 2,048 output, 12,000 for the closing answers.

| Setting | Value |
| --- | --- |
| Models | gpt-5.6-luna (Foundry project endpoint); gpt-6-luna (Azure resource endpoint via `azure-responses`, same Responses API -- the project endpoint returned HTTP 500 for it on every request) |
| Prices per 1M tokens | 5.6-luna $0.20 in / $0.02 cached / $1.20 out; 6-luna $0.10 / $0.01 / $0.50, cache writes $0.125 (`--price-cache-write`) |
| Fills | 0.9, 1.5, 3.0 |
| Spend | $140.56 (run 63) + $15.80 (run 64) |

`seed$` is what the conversation cost, summarizer included, probes excluded. `DQ` is a prompt over 120K at any point. The control disqualifies at 1.5 and 3.0 and is a price reference, not a baseline. A row keeps the facts when its acc1 is at least 90% of the control's.

### Results

Seed$ means over five seeds, disqualified seeds in brackets. Every row shown kept 53/53; `-` is below the accuracy bar.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none (control) | $0.067 | $0.147 (5) | $0.467 (5) | $0.035 | $0.075 (5) | $0.226 (5) |
| tool_and_user_summary_anchored | $0.075 | **$0.120** | $0.438 (1) | $0.035 | $0.058 | $0.205 (1) |
| tool_summary_anchored, repeats off | $0.074 | $0.234 (1) | $2.51 (5) | $0.035 | **$0.049** | $1.29 (5) |
| tool_summary_anchored, repeats on | not run | $0.302 (2) | $0.788 (5) | not run | $0.053 | $0.307 (5) |
| user_summary_anchored | $0.088 | $0.219 (4) | $0.601 (5) | $0.046 | $0.115 (5) | $0.301 (5) |
| anchored_min_gain | $0.078 | - | - | $0.035 | - | - |

- **0.9:** the best complete rows cost 10-15% more than not compacting on 5.6-luna, inside its 29% seed spread, and within 1% on 6-luna.
- **1.5:** on 5.6-luna the composed row is the only complete row with no disqualification, -19% on the unlimited control. On 6-luna `tool_summary_anchored` is cheapest (-35%), the composed row -23%.
- **3.0:** the composed row kept 53/53 inside the window on four seeds of five per model, at seed$ 0.17-0.43 (5.6) and 0.10-0.13 (6) against the control's 0.47 and 0.23, and disqualified on the fifth.
- The other fifteen strategies lose facts past the window (truncation, sliding-window and the token-budget rows keep 8-22 of 53) or disqualify.

### All twenty strategies

Run 63, record repeats off for `tool_summary_anchored` (its repeats-on row is above). Each cell reads facts kept (mean of 53) / acc1 / disqualified seeds of 5.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none | 53 / 100% / 0 | 53 / 97% / 5 | 53 / 85% / 5 | 53 / 100% / 0 | 53 / 99% / 5 | 53 / 87% / 5 |
| tool_and_user_summary_anchored | 53 / 100% / 0 | 53 / 100% / 0 | 53 / 90% / 1 | 53 / 100% / 0 | 53 / 100% / 0 | 53 / 100% / 1 |
| tool_summary_anchored | 53 / 100% / 0 | 53 / 94% / 1 | 53 / 87% / 5 | 53 / 100% / 0 | 53 / 100% / 0 | 53 / 84% / 5 |
| user_summary_anchored | 53 / 97% / 0 | 53 / 100% / 4 | 53 / 90% / 5 | 53 / 100% / 0 | 53 / 100% / 5 | 53 / 85% / 5 |
| anchored | 50 / 92% / 0 | 36 / 66% / 0 | 13 / 26% / 5 | 46 / 87% / 0 | 26 / 50% / 0 | 13 / 26% / 5 |
| anchored_min_gain | 53 / 98% / 0 | 43 / 81% / 0 | 13 / 26% / 5 | 53 / 100% / 0 | 25 / 47% / 0 | 13 / 26% / 5 |
| anchored_no_assistant | 50 / 94% / 0 | 32 / 61% / 0 | 40 / 73% / 5 | 49 / 91% / 0 | 35 / 67% / 0 | 21 / 39% / 5 |
| tool_result | 50 / 90% / 0 | 45 / 84% / 5 | 47 / 81% / 5 | 45 / 84% / 0 | 45 / 84% / 5 | 42 / 72% / 5 |
| selective_tool_call | 50 / 94% / 0 | 43 / 82% / 5 | 45 / 73% / 5 | 39 / 74% / 0 | 40 / 75% / 5 | 41 / 75% / 5 |
| context_window | 43 / 80% / 0 | 19 / 37% / 0 | 17 / 34% / 0 | 43 / 82% / 0 | 21 / 41% / 0 | 17 / 34% / 0 |
| context_window_lazy | 53 / 100% / 0 | 25 / 47% / 0 | 11 / 23% / 0 | 42 / 83% / 0 | 26 / 50% / 0 | 14 / 29% / 0 |
| context_window_aggressive | 22 / 43% / 0 | 15 / 30% / 0 | 3 / 8% / 1 | 20 / 39% / 0 | 9 / 19% / 0 | 0 / 2% / 4 |
| truncation | 35 / 67% / 0 | 17 / 33% / 0 | 17 / 34% / 0 | 31 / 60% / 0 | 20 / 39% / 0 | 14 / 29% / 0 |
| sliding_window | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 | 8 / 17% / 0 |
| summarization | 18 / 36% / 0 | 18 / 31% / 0 | 17 / 31% / 0 | 29 / 53% / 0 | 25 / 43% / 0 | 30 / 50% / 0 |
| token_budget_fallback | 22 / 42% / 0 | 17 / 34% / 0 | 10 / 20% / 0 | 23 / 44% / 0 | 18 / 35% / 0 | 8 / 17% / 0 |
| token_budget_tools_first | 18 / 36% / 0 | 19 / 36% / 0 | 9 / 18% / 0 | 19 / 36% / 0 | 15 / 29% / 0 | 8 / 17% / 0 |
| token_budget_truncate_first | 19 / 36% / 0 | 17 / 34% / 0 | 9 / 19% / 0 | 19 / 36% / 0 | 15 / 31% / 0 | 8 / 17% / 0 |
| token_budget_window_first | 11 / 23% / 0 | 10 / 19% / 0 | 9 / 18% / 0 | 13 / 25% / 0 | 10 / 20% / 0 | 8 / 17% / 0 |
| token_budget_summarize | 32 / 61% / 0 | 22 / 42% / 0 | 10 / 20% / 0 | 34 / 64% / 0 | 16 / 32% / 0 | 9 / 19% / 0 |

Seed$ per cell for the same rows, mean of five seeds. A cheap row that lost facts or disqualified is not a saving.

| Strategy | 5.6 / 0.9 | 5.6 / 1.5 | 5.6 / 3.0 | 6 / 0.9 | 6 / 1.5 | 6 / 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| none | 0.0671 | 0.1474 | 0.4665 | 0.0348 | 0.0749 | 0.2258 |
| tool_and_user_summary_anchored | 0.0747 | 0.1199 | 0.4384 | 0.0353 | 0.0578 | 0.2051 |
| tool_summary_anchored | 0.0735 | 0.2343 | 2.5080 | 0.0347 | 0.0485 | 1.2905 |
| user_summary_anchored | 0.0875 | 0.2186 | 0.6008 | 0.0456 | 0.1145 | 0.3011 |
| anchored | 0.0784 | 0.1699 | 0.5198 | 0.0375 | 0.0958 | 0.3178 |
| anchored_min_gain | 0.0775 | 0.1888 | 0.5795 | 0.0345 | 0.0935 | 0.3135 |
| anchored_no_assistant | 0.0782 | 0.1955 | 0.3470 | 0.0369 | 0.0961 | 0.1925 |
| tool_result | 0.0926 | 0.1957 | 0.5416 | 0.0491 | 0.1037 | 0.2696 |
| selective_tool_call | 0.0932 | 0.1854 | 0.5487 | 0.0495 | 0.0980 | 0.2651 |
| context_window | 0.0924 | 0.1361 | 0.2428 | 0.0492 | 0.0642 | 0.1215 |
| context_window_lazy | 0.0900 | 0.1515 | 0.3190 | 0.0498 | 0.0781 | 0.1667 |
| context_window_aggressive | 0.0670 | 0.0997 | 0.1339 | 0.0340 | 0.0515 | 0.0977 |
| truncation | 0.0765 | 0.1361 | 0.2401 | 0.0396 | 0.0644 | 0.1178 |
| sliding_window | 0.0762 | 0.1232 | 0.2425 | 0.0448 | 0.0719 | 0.1402 |
| summarization | 0.1146 | 0.1763 | 0.3337 | 0.0713 | 0.1051 | 0.2028 |
| token_budget_fallback | 0.1428 | 0.2578 | 0.4362 | 0.0800 | 0.1341 | 0.2451 |
| token_budget_tools_first | 0.0975 | 0.1392 | 0.1861 | 0.0474 | 0.0679 | 0.0999 |
| token_budget_truncate_first | 0.0896 | 0.1299 | 0.1892 | 0.0479 | 0.0662 | 0.1017 |
| token_budget_window_first | 0.0569 | 0.0899 | 0.1360 | 0.0270 | 0.0454 | 0.0749 |
| token_budget_summarize | 0.0771 | 0.1985 | 0.3540 | 0.0371 | 0.0964 | 0.1833 |

### Why strategies fail

1. **User text outgrows the window (3x only).** Fill 3.0 carries 69 filler user turns of ~1,636 tokens, ~114K. Every anchored row keeps every user turn. The simulator's replay of the cell put the peak prompt at 113,931 tokens of user turns plus one 35,977-token tool result awaiting its record -- the ~155K plateau measured live. Only a row that also summarises user turns can fit.
2. **An incomplete record.** A lookup the record leaves out is kept whole rather than lost; at 3x each is ~36K, so three missed lookups are ~107K nothing may shed. That is the composed row's one failed seed per model at 3.0 (153-162K, 53/53 kept) and `tool_summary_anchored`'s two on 5.6-luna at 1.5 (168K, five missed). A loud failure, not a quiet loss.
3. **One record per conversation.** With repeats off, `tool_summary_anchored` compacts once and keeps every later tool result, growing to 262-334K at 3.0. Repeats are on by default since `d3aca3450`; they cut that row's 3.0 cost by 69-76% but leave cause 1.

### gpt-6-luna against gpt-5.6-luna

gpt-6-luna costs about half on every row, write premium included, and ranks the strategies the same way. Seeding cache hit on the control at 0.9: 94.1% against 93.6%. Both answer the full ~360K conversation less reliably (control acc1 76-91% across runs 63 and 64) than the composed row's compacted one (87-100%). gpt-6-luna answered rate limits with a one-second `Retry-After` while the minute's quota stayed spent; under the old retry policy that failed 17 rows, re-run after `d85540c12`.

### Recommendations

| Conversation vs window | Use |
| --- | --- |
| Fits (up to ~0.9) | No compaction: the cache already serves ~94% of the prompt |
| Up to ~1.5x | `tool_and_user_summary_anchored`: the only row complete and within the window on both models |
| Up to ~1.5x, a model whose single record covers every lookup (`UNCOVERED:0`) | `tool_summary_anchored`: cheapest on 6-luna |
| ~3x and beyond | `tool_and_user_summary_anchored`, knowing its incomplete-record failure |

### Caveats and open questions

- One workload shape; facts narrated in replies are not tested.
- The composed row's seed spread reaches +-380% at 3.0 because of its failed seed; 3-25% at 1.5.
- 17 gpt-6-luna rows were re-run later under lighter load; the failed records are in `set-aside/`.
- The 2x input rate above 272K is not modelled; only the control and 3.0 disqualifiers cross it.
- **Next fix:** a chain step for lookups kept whole because a record missed them, measured at 3.0.
- Beyond 3x at 120K is unmeasured; at 30K and 6.5x (runs 61-62) the composed row kept 197/197 on every seed at 1.3x an unlimited model's cost.

### Runs 41 to 48 -- the record row's retention is bimodal, 13 September

Re-reading the archive after run 48 printed the record row at 50/53 on the cell where run 47 had printed 53/53. `tool_summary_anchored` has 58 records in runs 41 to 48 (47a included) and four of them lost facts:

| run | seed | facts | flags | cell |
| --- | ---: | ---: | --- | --- |
| 43 | 3 | 21/53 | `UNCOVERED:4 RECFALLBACK:43` | 170,000/0.9, fixed payload |
| 46 | 1 | 44/53 | `UNCOVERED:4 RECFALLBACK:8` | 300,000/0.9, scaled payload |
| 46 | 5 | 44/53 | `UNCOVERED:4 RECFALLBACK:2` | 300,000/0.9, scaled payload |
| 48 | 3 | 37/53 | `UNCOVERED:4 RECFALLBACK:3` | 170,000/0.9, scaled payload, recompact arm |

Split by the two counters: neither, 30 records, none lost; `RECFALLBACK` alone, 16, none lost; `UNCOVERED` alone, 5, none lost; both, 7, four lost and three held -- the three at 60,000 tokens. **Every loss carries `UNCOVERED:4` beside `RECFALLBACK`, no record carrying either alone has lost a fact, and the pair is where a loss can happen rather than a loss in itself.** The mechanism is in the source: the coverage check keeps an uncovered group but does not preserve it, a kept group is why the prompt stays over the ceiling, and over the ceiling the fallback shortens and sheds every tool group in its band that is not a record -- the uncovered ones and the ones behind the record alike. `STATE.md` §3u has the budgets and the arithmetic of each loss.

What this changes in the sections below, each marked in place:

- **Run 41's "53/53 in all 25 rows" and run 42's "matches all 25 rows"** are 30 draws at 60,000 tokens, and 60,000 is not a floor: the fallback's budget behind the record there is 2,897 and 2,414 tokens against 3,500-token results, and driven offline it shortened one and lost three.
- **Run 44's "retention survives to 100,000 and breaks after"** reads a threshold into five draws that all carry `UNCOVERED:0`, where the pair could not occur.
- **Run 43's 4 of 5** is the pair, and 32 lost facts is four whole eight-code groups.
- **Runs 45 and 46's "three of five seeds keep every fact"** stands, and the other two are the pair: the gate shutting is why the fallback ran, and the fallback is what took the nine facts.
- **Run 47's "four rows keep every fact"** is, on the record row, five draws; run 48 on the same cell drew 37/53 on one seed of five, and its section below did not say so.

Seeds are not paired across runs -- the scenario salt carries a timestamp, so run 47's seed 3 and run 48's seed 3 share no codes -- and at 7% a record (14% from 100,000 tokens up) two five-seed runs of one cell disagree on "held every fact" about 42% (50%) of the time. A five-seed 53/53 on this row is a draw. The table now prints a per-seed `facts` line for any row whose seeds disagree, so a 50/53 that is four 53s and a 37 says so under the table.

### Run 47 -- all twenty at 170,000/0.9, and the two user-turn rows measured

> **Correction, 13 September.** Two things in this section are withdrawn; the text is kept as > written, with the two sentences concerned marked, so the correction can be checked against it. > > **`USERCOMPACT` in the run 47 records is inflated by up to 2x.** Wired the way the harness wires > it, the user-band strategy ran twice per crossing -- inside the model call, on copies the store > never saw, and again on the store -- and both passes counted until `8c463f0e7`. `USERCOMPACT:2` > on the standalone row is one compaction; the composed row's 4 to 7 is about three (run 48's > boundary and fold arms, whose `USERSUMMARIES` counter was wired, read 3 standing summaries > against `USERCOMPACT` 4 to 8 on the same cell). > > **The per-seed claim that the composed row's hit rate tracked its passes is false.** `hit%` is > the whole run, seeding and probes together, and the probe phase's hit rate is a two-valued draw > -- 33.3% or about 99.5%, never between (`STATE.md` §3t). Split by phase, the five seeds read > (`USERREPLACED`, seeding hit, probe hit, `hit%`): 16 / 85.7% / 33.3% / 73%; 8 / 85.0% / 99.6% / > 89%; 16 / 82.2% / 33.3% / 70%; 14 / 86.4% / 33.3% / 75%; 14 / 81.0% / 33.3% / 70%. The 89% was > the one seed that drew the high probe; the seeding half sits between 81% and 86% on every seed > and bears no relation to how often the summary was rewritten. What stands: the composed row's > seeding-phase hit, 84.1%, is below the record row's 92.3% and the control's 95.5% on every seed, > so rewriting the user summary does cost cache -- about eight points against the record half at > this cell, not the twenty-one the mixed column shows. Of the four rows in the table below, only > the composition drew the low probe on any seed, so the other three `hit%` figures are honest. The > `boundary` and `fold` modes were built against the withdrawn measurement; the strict-prefix > argument for them stands on its own, and run 48 (next section) could not rank them. > > **Qualified, 13 September.** The record row's 53/53 is five draws of a row whose retention is > bimodal across the archive: run 48's recompact arm, the same cell, drew 37/53 on one seed of > five at `UNCOVERED:4 RECFALLBACK:3`. See the runs 41 to 48 section above this one.

`gpt-5.6-luna` through the harness agent, 170,000-token window, 0.9 fill, the scaled payload -- six results of ~15,125 tokens at a 60% tool share -- every registered strategy, five seeds, 100 records, one file per seed, two invocations at a time. No throttling, retries or errors; $18.22 across the run. It is the first archived cell to carry `user_summary_anchored` and `tool_and_user_summary_anchored` as they now stand -- the band share on, the composed row on its shared line -- and run 47a beside it in `runs/` is the aborted attempt that measured neither.

**Four rows keep every fact on all five seeds, and one of them compacts to 30% of the window.** On the record row that is a draw (qualification above); on the composed row it has since held on all 21 of its archived records.

| row | `msgs` | `tok` left/peak | `snap%` | `hit%` | `seed$` | `seed$+-` | `summ$` | `vs none$` | facts | `acc1` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `none` | 85/85 | 146,425/146,425 | 86% | 97% | $0.1113 | 19% | -- | -- | 53/53 | 100% |
| `tool_summary_anchored` | 81/89 | 84,651/86,421 | 50% | 95% | $0.0908 | 22% | -- | -18% | 53/53 | 100% |
| `user_summary_anchored` | 62/86 | 120,992/120,992 | 71% | 93% | $0.1471 | 24% | $0.0118 | +32% | 53/53 | 100% |
| `tool_and_user_summary_anchored` | 49/90 | 51,923/81,784 | **30%** | **76%** | $0.1357 | 30% | $0.0169 | +22% | 53/53 | 100% |

`acc2` is 100% on all four as well, on every attempt of every seed. The composition is the deepest-compacting row in the cell that loses nothing: the four rows below it in `snap%` -- `context_window_aggressive` at 27%, `token_budget_window_first` at 24%, `sliding_window` and `summarization` at 11% -- lost 33 to 45 facts and landed between 17% and 38% on `acc1`. Seven of twenty rows cleared the correctness bar, the control among them; the three not in the table are `anchored`, `anchored_no_assistant` and `selective_tool_call`, at 49 to 50 facts and 91% to 94%.

**The user half's hysteresis holds it back on almost every pass.** `user_summary_anchored` read `USERCOMPACT:2` on every seed against `USERHELD` 26 to 39 and `USERUNDER` 62 to 75, with `USERREPLACED:24` -- two passes a run, the second standing for twenty-four turns, and every other pass past the trigger refused because the band was not worth a tenth of the prompt. That is `--user-min-band-share` doing what it was written to do on a workload where the band is the minority of the prompt, and it is a finding about this cell's sizing under the scaled payload rather than about the row. `snap%` 71 against 86 is what two passes buy.

**The composition's user half fires, and the price is the cache.** No `USERSTARVED` on any seed, which is the aligned trigger behaving as designed; `USERCOMPACT` 4 to 7 against `USERHELD` 2 to 4, because the record half's removal leaves the band a larger share of what remains. Every one of those passes rewrites the summary just behind the head turn, and the hit rate tracks them -- **withdrawn, see the correction above**; the two phase columns are added to the table, and the `USERCOMPACT` column is the double-counted figure:

| seed | `USERCOMPACT` | `USERREPLACED` | `hit%` | seeding hit | probe hit | `snap%` | `seed$` | `vs none$` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| s0 | 5 | 16 | 73% | 85.7% | 33.3% | 30% | $0.1295 | +14% |
| s1 | 4 | 8 | 89% | 85.0% | 99.6% | 34% | $0.1166 | +3% |
| s2 | 6 | 16 | 70% | 82.2% | 33.3% | 31% | $0.1506 | +28% |
| s3 | 5 | 14 | 75% | 86.4% | 33.3% | 28% | $0.1257 | +31% |
| s4 | 7 | 14 | 70% | 81.0% | 33.3% | 29% | $0.1558 | +35% |

76% against the record row's 95% and the control's 97% is the cost of rewriting the user summary in place on every pass, and it is the measurement behind the `boundary` and `fold` modes of `--user-summary-mode`, neither of which this run carried. **Withdrawn as a per-seed measurement** (correction above): what survives is the seeding-phase gap, 84.1% against 92.3% and 95.5%, and run 48 carried both modes without separating them.

**On money the cell resolves nothing, and this is the part to read carefully.** `seed$+-` runs 19% to 30% across the four rows. The instrument's rule is that a gap is a result only when it is wider than the seed spread of either row it is taken between, and it printed `NOT SUPPORTED` on its own verdict: `tool_summary_anchored` at -18% against a 22% spread. The composition's +22% sits inside its 30%. The one figure that clears the rule is `user_summary_anchored` at +32% against 24% on the row and 19% on the control, with every seed dearer than its control, +23% to +45% -- so what the cell supports there is the direction, not the size. Paired by seed the record row is cheaper on all five, -8% to -30%, and the composition dearer on all five, +3% to +35%; those are signs, and five seeds do not put a number on either. Nothing in this cell separates the record row or the composition from not compacting on money, and no figure from that column is to be quoted as a result -- `REVIEW-2026-09-06.md` §3 is what happened the last time one was.

**One seed is off target, and it is in the aggregate.** s3 seeded 137,396 tokens against the 153,000 target, -10.2%, where the other four sit between -2.1% and -3.9%; the merged cell reads -4.6% and prints no `FILL OFF TARGET`, s3 rendered alone does. Its control is the cheapest of the five at $0.0962, 15% under the next, which is why it is also the record row's worst seed at -8%. Re-rendered over the other four files with `--from-jsonl`: facts, `acc1` and `acc2` unchanged on every row; the composition still at 76% hit and 31% snap; the control's spread falls from 19% to 4% and the user row's from 24% to 15%; `tool_summary_anchored` reads -20% against a 21% spread and the instrument still prints `NOT SUPPORTED`; the composition +20% against 26%; the user row +32% against 15%. Dropping it changes nothing the paragraphs above say.

### Run 50 -- all twenty strategies at 120K/0.8: the verdict is the control

Run 47's measurement moved to a smaller window and the first all-strategies cell on the corrected reasoning counter (`434a77da5`): `gpt-5.6-luna`, 120,000 tokens, 0.8 fill, scaled payload, five seeds, one invocation per seed, 100 records, four concurrent ($10.45, `runs/run-50-120k-*`, one report over the five files). Fill -5.5% and tool share +5.8%, both just outside tolerance and on the usual side; every row shares the one operating point. Mild throttling on ten rows (1-21 retries) from the four-way schedule; `dq` 0; every invocation exited 0.

**Test results overview** -- all twenty rows, pooled over the five seeds, sorted by `usd`.

| strategy | cmp% | hit% | usd | +-$ | fct | acc1 | acc2 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `token_budget_window_first` | 35% | 91.8% | $0.0489 | **-13%** | **21/53** | 21.6/53 | 21.0/53 |
| `none` (control) | 76% | 96.2% | $0.0562 | ±0% | 53/53 | 53.0/53 | 53.0/53 |
| `truncation` | 63% | 86.3% | $0.0574 | +2% | 43/53 | 43.6/53 | 43.4/53 |
| `anchored_min_gain` | 77% | 96.1% | $0.0575 | +2% | 53/53 | 53.0/53 | 53.0/53 |
| `anchored_no_assistant` | 73% | 95.2% | $0.0622 | +11% | 53/53 | 52.6/53 | 52.2/53 |
| `token_budget_summarize` | 35% | 76.5% | $0.0625 | +11% | 28/53 | 26.3/53 | 27.8/53 |
| `anchored` | 73% | 95.2% | $0.0625 | +11% | 52/53 | 52.0/53 | 51.9/53 |
| `tool_summary_anchored` | 41% | 92.8% | $0.0636 | +13% | 53/53 | 53.0/53 | 53.0/53 |
| `sliding_window` | 10% | 11.7% | $0.0639 | +14% | 8/53 | 8.8/53 | 8.0/53 |
| `user_summary_anchored` | 65% | 94.2% | $0.0660 | +17% | 53/53 | 53.0/53 | 53.0/53 |
| `context_window_aggressive` | 35% | 87.7% | $0.0672 | +20% | 20/53 | 20.2/53 | 19.6/53 |
| `token_budget_tools_first` | 40% | 86.8% | $0.0723 | +29% | 28/53 | 28.3/53 | 27.8/53 |
| `tool_result` | 60% | 90.5% | $0.0725 | +29% | 50/53 | 48.9/53 | 46.3/53 |
| `context_window` | 61% | 90.6% | $0.0757 | +35% | 45/53 | 43.8/53 | 42.4/53 |
| `selective_tool_call` | 62% | 90.4% | $0.0790 | +41% | 44/53 | 43.6/53 | 44.0/53 |
| `tool_and_user_summary_anchored` | 59% | 90.4% | $0.0812 | +44% | 53/53 | 53.0/53 | 53.0/53 |
| `context_window_lazy` | 63% | 89.8% | $0.0833 | +48% | 52/53 | 48.9/53 | 45.7/53 |
| `token_budget_truncate_first` | 39% | 70.4% | $0.0867 | +54% | 24/53 | 24.1/53 | 23.6/53 |
| `token_budget_fallback` | 46% | 70.0% | $0.1025 | +82% | 28/53 | 28.9/53 | 28.4/53 |
| `summarization` | 10% | 12.3% | $0.1030 | +83% | 22/53 | 19.0/53 | 21.3/53 |

`cmp%` is the snapshot after all compaction as a share of the 120,000 window; `hit%` is `run hit%` (cached over total input, seeding and probes together); `usd` is mean `seed$` per seed (the conversation and summarizer, probes left out -- the half every verdict ranks on); `+-$` is that against the control; `fct` is the mean facts in the snapshot per seed, `acc1`/`acc2` the recall share converted to facts (scoped questions / the single combined question). Per-seed detail, spreads and flags are in the report beside the records.

Six rows hold every fact on every seed (`none`, `anchored_min_gain`, `anchored_no_assistant`, `tool_summary_anchored`, `user_summary_anchored` and the composed row; `anchored_min_gain` is a measured no-op -- `NOGAIN` on every pass, 77% snap), three more lose facts on single seeds (`anchored`, `context_window_lazy`, `tool_result`), and the eleven below the bar delete.

**Nothing overflows, and nothing pays.** The uncompacted conversation peaked at 91,117 billed tokens -- 76% of the window -- so at 0.8 fill of 120K compaction is solving a problem the workload does not have: every strategy that keeps the answer intact costs 2-48% more than `none`, the only cheaper row (`token_budget_window_first`, -13%) deletes to 21/53, and the verdict is **`none`**. This is the first cell in the archive whose verdict is the control; run 47's at 170K/0.9 named `tool_summary_anchored` at -7%. The comparison across the two cells is not a strategy comparison (workloads differ, and run 47 is pre-counter-fix), but the shape is consistent: as the window grows past the conversation, the record row goes from parity to a saving, and below it there is nothing to save.

**The cache-rate column, read once more.** `run hit%` (cached ÷ total input) is 96.2% on the control, 90-96% on the retainers, and collapses with the prefix: 11.7% on `sliding_window`, 12.3% on `summarization`, 70% on the two `token_budget` rows that drew the cold probe. At a cached-read price of a tenth of fresh input, losing the cache is what makes the deleters dear, not the tokens they saved.

**The layers again.** `tool_summary_anchored`: `UNCOVERED:0`, `RECFALLBACK` zero, 53/53 on all five seeds, peaks 75,106-77,878 against the 70,771 trigger (+6-10% -- a wider gap than run 49's 0-2%, unexplained). The composed row fired the chain once: `UNCOVERED:5`, `REFORCED:1`, the re-forced record `TRUNCATED:1` -- it hit the 2,048-token cap and covered none of the groups -- and layer 2 held them `PRESERVED:5` at 53/53. `user_summary_anchored` crossed once per seed (`USERCOMPACT:1`) at the default 0.8 trigger: the run-48 single-crossing caveat applies to this row here.

**Not resolved.** The next cell up -- 120K at 0.95-1.0 fill, where the control disqualifies -- is where a compaction payoff could first appear on this window, and nothing here measures it; the +6-10% peak-over-label on the record row; and `--assumed-reply-tokens` is wrong for luna at every cell tried (every seed here and in run 49 seeded low).

### Run 49 -- the three modes at a trigger that crosses twice, and the re-force measured live

Run 48's measurement redone where the arms can differ: the same three `--user-summary-mode` (`recompact`, `boundary`, `fold`), the same cell (`gpt-5.6-luna`, 170,000/0.9, scaled payload), five seeds an arm, one invocation per seed and mode, 75 records, $11.91 (`runs/run-49-luna-170k-fill90-usermodes60-*`, one report per arm plus the cross-cell render over all fifteen). Two changes from run 48: `--user-trigger-fraction 0.6` instead of 0.8, and the full five strategies in every arm so each arm carries its own reference record row. No throttling, no reconnects, no errors, `dq` 0 everywhere; fills -0.2% to -4.7% on thirteen of fifteen seeds with recompact `-s0` and `-s2` at -7.0% and -7.9% (the recompact arm is the least exact operating point of the three). First live run on the reasoning-counter fix of `434a77da5`, whose stamp was proven to survive the harness store the same day (`STATE.md` 3v). Seeds are not paired across arms; every comparison below is an arm against its own control.

| arm | row | `snap%` | seed hit | `seed$` | `seed$+-` | `vs none$` | facts | user-half flags |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| recompact | `user_summary_anchored` | 66% | 89.5% | $0.1440 | 18% | +30% | 53/53 | `USERCOMPACT:2`, `USERSUMMARIES:1` (rewritten) |
| boundary | `user_summary_anchored` | 69% | 88.8% | $0.1549 | 36% | +30% | 53/53 | `USERCOMPACT:2` (one seed 3), `USERSUMMARIES:2` |
| fold | `user_summary_anchored` | 65% | 89.6% | $0.1411 | 23% | +21% | 53/53 | `USERCOMPACT:2`, `USERSUMMARIES:2`, **`USERFOLD:0` on all five** |
| recompact | `tool_and_user_summary_anchored` | 40% | 90.1% | $0.1308 | 23% | +18% | 53/53 | one record `UNCOVERED:4 PRESERVED:4`, one `UNCOVERED:1` pending |
| boundary | `tool_and_user_summary_anchored` | 34% | 90.2% | $0.1300 | 27% | +9% | 53/53 | one record `UNCOVERED:5 PRESERVED:5` |
| fold | `tool_and_user_summary_anchored` | 34% | 89.5% | $0.1323 | 22% | +13% | 53/53 | one record `UNCOVERED:5 PRESERVED:5` |
| recompact | `tool_summary_anchored` | 51% | 92.8% | $0.1101 | 32% | -1% | 53/53 | `UNCOVERED:0`, `REFORCED:0` on all five |
| boundary | `tool_summary_anchored` | 53% | 92.9% | $0.1121 | 6% | -6% | 53/53 | `UNCOVERED:0` on all five |
| fold | `tool_summary_anchored` | 50% | 92.8% | $0.1082 | 17% | -7% | **52/53** | one seed 52, no flag; `UNCOVERED:0` on all five |

The controls read 95.5-95.6% seed hit at $0.1110-$0.1193; `truncation` sits below the correctness bar in all three arms (acc1 68% / 66% / 57%, per-seed facts bimodal as ever: 37/37/37/30/37, 37/37/37/37/26, 37/26/21/29/37). No probe drew the low value anywhere in the run -- every seed of every row read 99% or better on the probes, the settled-row case of `STATE.md` §3t.

**The crossing premise held.** The standalone user row read `USERCOMPACT:2` on fourteen of fifteen records (one 3) under the post-`8c463f0e7` counter, which counts a crossing once: two genuine boundaries per seed where run 48 had one and the modes were identical by construction. Recompact ends with one standing summary, boundary and fold with two, and the modes are now *measured* rather than structurally equal -- and **still not separable**: the mode gaps on `seed$` are 1-5% against seed spreads of 15-36%, the cross-cell section refuses every ranking, and the fold arm's own verdict prints NOT SUPPORTED (17% spread against a 7% gap). The fold made **zero** folds on all fifteen fold records, so at two crossings a standing summary was never worth its prefix break and fold ran as boundary mechanically; its +21% against boundary's +30% is noise, not a saving.

**The re-force and preserve layers acted, and nothing was lost.** The standalone record row reads `UNCOVERED:0` and `REFORCED:0` on all fifteen records -- its record covered every group on the first ask, so the new layers never needed to act; run 48's fifth seed lost 37/53 at `UNCOVERED:4 RECFALLBACK:3`, and every archived loss on that row carries that pair (§3u). The composed row did find groups its record could not cover, on four of fifteen records: three re-forced, wrote a second record (`REFORCED:1`, `RECORDS:2`), and it covered none of the targets -- `UNCOVERED:4`/`:5` with `PRESERVED` equal to it, layer 2 holding the groups in the prompt rather than shedding them; the fourth (recompact `-s0`) ended `UNCOVERED:1`, `REFORCED:1`, `PRESERVED:0`, an ask still pending when the run ended. Either way `dq` is 0 and facts 53/53 on every seed of every arm but one (fold `-s3`'s record row, 52/53 at `acc1` 98% with no flag: a fact the record's own text dropped, not a group the strategy shed).

**Thresholds fire where they are labelled.** The record trigger is 0.6 of the 167,952-token input budget -- 100,771 -- and the record row's billed peaks read 98,490-102,024 on thirteen of fifteen records, with two at 109,938 and 113,524 (the growth between the crossing and the call the record was forced on). `RECFALLBACK` is zero on all 75 records and the 0.9 fallback gate, 151,157, was never approached. Pre-fix, luna tripped the same labelled trigger at 0.61-0.64 of the billed ceiling (`STATE.md` 3v's commit); post-fix the label sits on the number.

**Not resolved.** The three modes' cost ranking; the fold mode's value on any cell measured (zero folds here); whether the record row is cheaper than the control (-1% / -6% / -7% against spreads of 32% / 6% / 17% -- the boundary arm's -6% is the closest and still inside its own spread, so no saving is claimed); and the standalone user row remains dearer than the control on every arm, +21% to +30%.

### Run 48 -- the three user-summary modes, and why it cannot rank them

The three `--user-summary-mode` arms on run 47's cell: `gpt-5.6-luna`, 170,000/0.9, scaled payload, five seeds an arm, one invocation per seed and mode, 65 records, $11.42 (`runs/run-48-luna-170k-fill90-usermodes-*`, one report per arm). Every arm carries `none`, `truncation`, `user_summary_anchored` and `tool_and_user_summary_anchored`; the recompact arm also carries `tool_summary_anchored`, which does not read the mode. Two throttled calls on the boundary arm's control, nothing else; fills -2.8% to -3.5%. The run was made to choose between the modes and **it cannot**, which is the result. Seeding-phase hit is `(cached - probe_cached) / (input - probe_input)` from the records; a low probe is a seed whose probe phase drew 33.3% (`STATE.md` §3t). Retention is 53/53 on every user-half row of every arm; the recompact arm's `tool_summary_anchored` row is not -- 37/53 on seed 3 (`-s2`, `UNCOVERED:4 RECFALLBACK:3`) and 53/53 on the other four, printed as 50/53 at `acc1` 94% and `seed+-` 30pp -- the fourth archived loss on that row and the same flag pair as the other three (runs 41 to 48 section above).

| arm | row | `snap%` | `hit%` | seeding hit | low probe | `seed$` | `seed$+-` | `vs none$` | user-half flags |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| boundary | `user_summary_anchored` | 71% | 93% | 89.2% | 0 of 5 | $0.1506 | 23% | +32% | `USERCOMPACT:2`, `USERREPLACED:24`, `USERSUMMARIES:1`, `USERHELD` 29-38 |
| fold | `user_summary_anchored` | 72% | 93% | 89.8% | 0 of 5 | $0.1517 | 12% | +30% | same, `USERHELD` 35-40, `USERFOLD:0` |
| recompact | `user_summary_anchored` | 70% | 93% | 89.3% | 0 of 5 | $0.1485 | 35% | +30% | same on four seeds, `USERHELD` 26-37; `-s3` `USERCOMPACT:3`, `USERREPLACED:11` |
| boundary | `tool_and_user_summary_anchored` | 34% | 72% | 84.1% | 5 of 5 | $0.1571 | 83% | +37% | `USERCOMPACT` 5-8, `USERSUMMARIES:3` |
| fold | `tool_and_user_summary_anchored` | 39% | 81% | 84.8% | 3 of 5 | $0.1411 | 49% | +21% | `USERCOMPACT` 4-7, `USERSUMMARIES` 2-3, `USERFOLD:0` |
| recompact | `tool_and_user_summary_anchored` | 31% | 83% | 85.1% | 2 of 5 | $0.1320 | 17% | +15% | `USERCOMPACT` 4-9, `USERSUMMARIES:1` |

The controls read 97% `hit%` and 95.5% seeding hit in all three arms, at `seed$` $0.1145 to $0.1164; `truncation` 93-94% and $0.1002 to $0.1265.

**The standalone row made one persistent crossing per seed in every arm.** `USERSUMMARIES:1`, `USERREPLACED:24` and `USERHELD` 26 to 40 on fourteen of fifteen records: at trigger 0.8 and fill 0.9 the band never regrows to a tenth of the prompt after the first pass, and the three modes only differ from the second pass on. The arms were identical there by construction, and the table says so -- seeding hit within 0.6 of a point, `seed$` within 2%, retention level.

**The composed row fired three times and its `hit%` spread is the probe draw.** Three standing summaries in the boundary and fold arms (`USERSUMMARIES:3`), so the modes did act differently there; but the eleven-point spread in `hit%` is five, three and two seeds drawing the low probe, and the seeding half -- the number that describes the strategy -- is 84.1% / 84.8% / 85.1%, one point apart. Whether the boundary mode's byte-identical prefix buys anything on the wire, this cell cannot say.

**`USERCOMPACT` is double-counted throughout**, as in run 47: `8c463f0e7` found the strategy running once inside the model call and once on the store per crossing, both passes counting and both asking the summarizer, so the model was sent two different summaries at one position on consecutive calls in every arm alike. That is a cache break none of the modes was designed around, and a second reason the arms could not separate; the archived counts stand as recorded, halved when read.

**Nothing here is a cost result.** The composed row's `seed$+-` runs 83%, 49% and 17%; the recompact arm's verdict names `tool_summary_anchored` at -7% and prints `NOT SUPPORTED` against a 59% spread. What a run that could rank the arms needs is at least two persistent passes on the standalone row -- a lower trigger, or a fill the band regrows under -- and per-probe cache accounting in the record so the probe draw can be read off rather than inferred (`STATE.md` §3s, §3t).

### What runs 45 and 46 say about the benchmark itself

**Turn count drives cost, not context size.** The two arms seed the same conversation size and cost utterly different amounts, and this is the *uncompacted control* in both, with no strategy involved:

| arm | filler turns | seeded | calls | input tokens | control cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| fixed payload | 120 | 256,776 | 147 | 20,220,087 | $0.5826 |
| scaled payload | 51 | 255,897 | 78 | 12,840,933 | **$0.3396** |

Same destination, **72% apart**, because one conversation re-sends its prefix over 147 calls and the other over 78. Input tokens are the product of size and calls, and this project has been varying size while treating calls as incidental.

That reframes the central question. Compaction reduces size, but it can only act **after** the conversation has grown -- by which time most of the re-sending has already been paid for. A cell labelled by its window says little about the bill; a cell labelled by its turn count says a lot. Every "vs none$" figure in this document is measured within one arm, so none of them is wrong, but comparisons *across* cells with different turn counts have been comparing two things at once.

**And an aggregate row can hide two behaviours rather than average one.** Run 46's cell reads `49/53` facts at `acc1 85%` with a 55% spread. **No seed scored 49.** Three scored 53 and two scored 44, and the cell mean is a number the experiment never produced. The control's own spread is 16% against the strategy's 55%, so seed conditions were comparable and the variance belongs to the coverage gate flipping. Read the per-seed table above; the aggregate is the wrong summary for a bimodal row, and the instrument has no way to know a distribution is bimodal.

### Runs 45 and 46 -- scaling the payload with the window, and the first real win

Two arms at one cell, 300,000 tokens at 0.9 fill, `none` against `tool_summary_anchored`, luna. Same window, same fill, same strategies. Run 45 uses the **fixed** payload every earlier cell used; run 46 uses the payload **scaled to the window**, which is now the default. Run 45 is one seed, run 46 five. No throttling, retries or errors in either.

**The fixed arm completes the degradation series and confirms its mechanism.**

| window | tool payload | removed | hit% | `vs none$` |
| --- | ---: | ---: | ---: | ---: |
| 60,000 | 21,967 | 28.8% | 88% | -1% to -6% |
| 100,000 | 21,967 | 22.6% | 74% | +21% |
| 170,000 | 21,967 | 17.0% | 66% | +113% |
| 300,000 | 21,967 | **11.7%** | **62%** | **+212%** |

The payload the strategy can act on never grows; the conversation it pays cache costs across does.

**Scaling the payload changes the answer, and the mechanism is the cache.** At 300,000 with tool results of ~26,814 tokens instead of ~3,500:

| seed | keeps | hit% | facts | `vs none$` | gate |
| --- | ---: | ---: | ---: | ---: | --- |
| s0 | 98% | 96% | 44/53 | +29% | failed, `UNCOVERED:4` |
| s1 | 61% | 96% | 53/53 | **-21%** | opened |
| s2 | 64% | 96% | 53/53 | **-23%** | `UNCOVERED:1`, still acted |
| s3 | 52% | 96% | 53/53 | **-33%** | opened |
| s4 | 94% | 96% | 44/53 | +19% | failed, `UNCOVERED:4` |

**Three of five seeds are 21-33% cheaper than not compacting while keeping every fact.** That is the first resolvable saving with full retention anywhere in this project. The hit rate tells you why: **96% against the control's 98%**, a two-point gap, where the fixed payload at the same window cost thirty-five. Large contiguous tool results can be replaced without disturbing much of the cached prefix; 22,000 tokens scattered through a 270,000-token conversation cannot.

**What decides it is one threshold.** The two failures are the coverage gate shutting: `--coverage-share 0.8` asks the record to quote 80% of every digit-bearing token in a group, and a 26,814-token result holds the same eight planted codes among 7.7x more filler. The gate has no notion of result size and was calibrated when results were 3,500 tokens. It is the difference between -33% and +29%. It is also the difference between 53 and 44: `UNCOVERED:4` sits beside `RECFALLBACK` on both failed seeds, the pair every archived loss on this row carries, because a group the gate keeps is not preserved and the fallback that runs over the ceiling shortens the band's results to fixed budgets -- the same nine facts on both seeds (runs 41 to 48 section above).

**Two caveats, both mine.**

The cell's cost ranking is **withdrawn by the instrument** -- `CONTROL DIVERGED, MSGS:-2` -- because both runs carried only `none` and `tool_summary_anchored`, so the leanest strategy row is the one adding the record's own two messages. `STATE.md` section 3h records this trap and says to include a message-neutral row such as `truncation`; I did not. It is verifiably a false positive here (control peak 133, strategy peak 135, record contributes exactly 2), and the per-seed figures above are computed from the records rather than the withdrawn column -- but the spread guard is lost, so treat the cell as unranked.

The fixed-and-scaled comparison is **not one variable**. Filling 270,000 tokens with larger results needs 51 filler turns instead of 120, so conversation length moves with payload composition. That is intrinsic to the change rather than an error, but "scaling helped" and "fewer turns helped" cannot be separated by this design.

### Run 44 -- 100,000 at 0.9 fill, and the sizing bug finally diagnosed

Same shape as run 43 at a 100,000-token window: all 18 strategies, five seeds, 90 records, three then two invocations at a time. **No throttling, retries or errors.**

**The sizing error is a constant, not a compounding one.** Set against run 43:

| cell | turns | mean shortfall | spread across seeds |
| --- | ---: | ---: | ---: |
| 100,000 / 0.9 | 47 | **-8.3%** | 27% of mean |
| 170,000 / 0.9 | 80 | -7.6% | 22% of mean |

Nearly double the turns, the same shortfall. So run 43's explanation -- reply variance compounding across a long conversation -- **is wrong and is withdrawn**. Measuring the seeding replies says what is actually happening:

seed replies, tokens each: 103, 201, 348, 448, 581      assumed: 602

`--assumed-reply-tokens 602` is above **every** seed, so the solver always lays down too few filler turns: that is the constant -8%. And luna's reply length varies **5.6x between seeds**, which is the 22-27% spread and which no constant can fix. The fix for the mean is to re-measure the flag per model; the fix for the spread is for the solver to iterate against observed replies, or for the analysis to normalise on achieved fill rather than trusting the label.

**Results.** `VERDICT: none`, as at every cell.

| strategy | snap% | `vs none$` | `seed$+-` | facts | acc1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `none` | 83% | -- | 54% | 53/53 | 100%* |
| `tool_summary_anchored` | 64% | **+21%** | 61% | **53/53** | 100% |
| `selective_tool_call` | 76% | +31% | 30% | 47/53 | 89% |
| `tool_result` | 80% | +42% | 26% | 48/53 | 91% |
| `anchored_no_assistant` | 67% | +68% | 50% | 48/53 | 91% |
| `anchored` | 65% | +92% | 69% | 32/53 | 61% |
| `token_budget_fallback` | 34% | +162% | 25% | 19/53 | 37% |
| `sliding_window` | 7% | -17% | 15% | 8/53 | 17% |
| `token_budget_window_first` | 20% | -38% | 34% | 13/53 | 26% |

**`tool_summary_anchored` is again the only strategy that keeps everything** -- 5 of 5 seeds, where the next best manage 48 of 53. Across the three window sizes now measured:

| window | `vs none$` | facts | seeds holding 53/53 |
| --- | ---: | ---: | --- |
| 60,000 | -1% to -6% | 53/53 | 25 of 25 rows |
| 100,000 | +21% | 53/53 | 5 of 5 |
| 170,000 | +113% | 47/53 | 4 of 5 |

Retention survives to 100,000 and breaks after it; **cost degrades monotonically and steeply** the whole way. Whatever case the strategy has, it is a small-window case, and nothing in the 60K work suggested where the edge was.

**Qualified, 13 September.** The retention column of that series is a count of draws, not a property of the window: all five 100,000 records carry `UNCOVERED:0`, so the flag pair every archived loss carries could not occur here, and at the archive's per-record loss rate five clean draws happen about two times in three (runs 41 to 48 section above). The cost column stands.

### Run 43 -- 170,000 tokens at 0.9 fill, where the sizing stops holding

All 18 strategies, `gpt-5.6-luna`, 170,000-token window, 0.9 fill, five seeds, 90 records, run one or two at a time rather than five: a single invocation runs near 1.3M tokens a minute, and the quota rise to 4M is what allowed even two. **No throttling, no retries, no errors** -- the pacing worked where run 42's did not.

**The cell's own sizing did not.** The five seeds seeded 124,636 to 155,530 tokens against a 153,000 target -- **73% to 91% of the window, a 25% range** -- for a mean of -7.6%, flagged `FILL OFF TARGET`. (An earlier version of this paragraph blamed 80 turns compounding reply variance. Run 44 refutes that: at 47 turns the shortfall is -8.3%, the same. See run 44 for the measured cause.) These five seeds are not the same operating point, which is the first thing to fix before anyone measures here again.

**No strategy retains reliably at this size.**

| seed | control fill | `tool_result` | `tool_summary_anchored` | `anchored_no_assistant` | `selective_tool_call` |
| --- | ---: | ---: | ---: | ---: | ---: |
| s4 | 124,636 (73%) | 53 | 53 | **18** | 53 |
| s1 | 137,135 (81%) | 47 | 53 | 53 | 45 |
| s0 | 143,586 (84%) | 53 | 53 | 53 | 53 |
| s3 | 146,273 (86%) | 53 | 53 | **13** | 53 |
| s2 | 155,530 (91%) | 53 | **21** | 53 | 39 |

Every one of the four drops at least one seed, and they drop different seeds. **The failures are bimodal collapses, not gradual decay** -- a strategy keeps about 53 or falls to 13-21. Fill does not explain it: `r = -0.22`, the worst collapse sits at 86% fill and a clean 53 at 91%.

That bounds a claim this project has been making. At 60,000 tokens `tool_summary_anchored` held 53/53 in **all 25 rows** of run 41 and in run 42. At 170,000 and 0.9 fill it holds 4 of 5 and costs **+113%** with a 92% spread, against -1% to -6% in the small cells. **Reliable retention was a property of the cells it was measured in, not of the strategy.** The lost seed reads `UNCOVERED:4 RECFALLBACK:43`, and 32 facts is four whole eight-code groups: with 3,500-token results the fallback's budget at this window is at least 6,998 at every band position, so nothing could be shortened and the shed phase took the oldest tool groups in the band, which are the four the record left uncovered (runs 41 to 48 section above).

| strategy | snap% | `vs none$` | `seed$+-` | facts | acc1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `none` | 83% | -- | 36% | 53/53 | 100%* |
| `tool_result` | 73% | **+8%** | 43% | 52/53 | 98% |
| `selective_tool_call` | 82% | +30% | 20% | 49/53 | 91% |
| `anchored_no_assistant` | 73% | +56% | 16% | 38/53 | 72% |
| `tool_summary_anchored` | 69% | +113% | 92% | 47/53 | 88% |
| `anchored_min_gain` | 69% | +131% | 54% | 26/53 | 50% |
| `anchored` | 70% | +163% | 69% | 26/53 | 50% |
| `token_budget_fallback` | 35% | +241% | 14% | 20/53 | 39% |
| `sliding_window` | 4% | -46% | 16% | 8/53 | 17% |
| `summarization` | 4% | +15% | 6% | 20/53 | 39% |

`VERDICT: none`. Nothing is cheaper except rows that deleted most of the material, which is the same answer at every cell this project has measured -- now including the largest and fullest.

**Read the cost column against the fill spread.** Seeds 25% apart in size inflate every `seed$+-` here, and the two widest (92% and 69%) are the rows whose retention also collapsed. The ordering of the extremes is safe; neighbouring rows are not separated.

### Run 42 -- every strategy at one high-fill cell, accuracy only

All 18 registered strategies in one cell, `gpt-5.6-luna`, 60,000/0.86, five seeds, 90 records. **The cost axis of this run is not usable and the accuracy axis is.**

**Why the cost axis is out.** Five concurrent invocations of eighteen strategies is roughly 4.5x the per-invocation load of run 41's five arms of four, so about 2.7M tokens a minute against a 2M ceiling. 18 of 90 records were throttled with rate-limit retries -- including **the control in all five seeds**, which is the baseline every `vs none$` is taken against. For the one strategy with both kinds of seed, the throttled records cost 49% more than the clean one on 4% more input (n=1, so suggestive rather than conclusive). The cell also seeded -5.3% off its fill target. The table prints `VERDICT: tool_summary_anchored` at -6% with `NOT SUPPORTED` against a 37% spread; that verdict is not being claimed.

**Why the accuracy axis stands.** Throttling delays a request; it does not change what the model is sent or what it writes. Facts and `acc1` are unaffected.

| strategy | facts | acc1 |
| --- | ---: | ---: |
| `none`, `tool_summary_anchored` | **53/53** | 100% |
| `anchored_no_assistant` | 51/53 | 94% |
| `selective_tool_call`, `anchored` | 50/53 | 92-94% |
| `tool_result` | 49/53 | 93% |
| `anchored_min_gain` | 46/53 | 88% |
| `token_budget_summarize` | 40/53 | 76% |
| `context_window_lazy` | 36/53 | 69% |
| `context_window` | 24/53 | 46% |
| `truncation`, `token_budget_fallback`, `token_budget_tools_first`, `token_budget_truncate_first`, `context_window_aggressive`, `summarization` | 16-20/53 | 31-39% |
| `sliding_window` | **8/53** | 17% |

`tool_summary_anchored` is the only compacting strategy that retains everything, which matches all 25 rows of run 41 on a field four times wider -- five more draws at the same window, see the runs 41 to 48 section above. `summarization` -- the ladder the framework leads with -- sits at 18/53, level with blind truncation.

**Method note for the next run.** Concurrency has to be sized against strategies-per-invocation, not invocations. Run 41's five-way parallelism was clean at four rows each; the same parallelism at eighteen rows each was not.

> **2026-09-12 -- the payload default changed, and every run in this file predates it.** > `--tool-share` now defaults to 0.6 instead of 0.0, so the tool payload is derived from the fill > target and scales with `--context-window`. Every run recorded here, and every large-window cell > this project has measured, used the fixed path: `--tool-result-tokens 3500` recorded a tool payload > of **21,967 tokens at all three of 60,000, 100,000 and 170,000**, while only the filler grew. That caps > what a strategy compacting tool results and nothing else can save, and tightens the cap as the > window widens. Across 60,000/0.86 (run 41), 100,000/0.9 (run 44) and 170,000/0.9 (run 43), > `tool_summary_anchored` removed **28.8% -> 22.6% -> 17.0%** of the control's snapshot while its cache > hit rate fell **88% -> 74% -> 66%** against a control climbing **96% -> 97% -> 98%** -- read > here as a property of the strategy, and substantially a property of the workload. (**Corrected > 13 September**: those three are whole-run `hit%`; the seeding half alone reads **88.0% -> 87.5% > -> 76.2%**, and the 100,000 point's fall is entirely the probe-phase draw of `STATE.md` §3t -- > `tool_summary_anchored` drew 33.3% on four of five seeds there and on all five at 170,000. The > removed-share series stands.) `tool_share` > is part of the cell key, so cells from either side of this date do not pool; re-running any > command line in `runs/` needs `--tool-share 0` to reproduce what it measured.

### Run 41 -- the clean baseline

The first measurement on code with the coverage check repaired, the record protected, one record per trigger, repeats off by default, and the output reservation paid off. 60,000/0.86, five seeds, five arms, four rows in each invocation so `anchored` is compared on the same conversations rather than across runs. **100 records, no throttling, retries or errors.** Not comparable with runs 26-40: the reservation fix moved every threshold.

| arm | `vs none$` | facts | acc1 | seed spread |
| --- | ---: | ---: | ---: | ---: |
| luna, fixed payload | **-1%** | 53/53 | 100% | 32% |
| luna, share 0.80 | **-3%** | 53/53 | 100% | 19% |
| luna, share 0.80, repeats | +7% | 53/53 | 100% | 17% |
| mini, fixed payload | +25% | 53/53 | 100% | 79% |
| mini, share 0.80 | +10% | 53/53 | 96% | 33% |

**Retention is the finding.** The record row holds 53/53 in **all 25 rows** -- both models, both payloads, repeats on and off, coverage succeeding and failing, fallback firing and not. On the same conversations `anchored` reads 35-53 and `truncation` 21-45. No other strategy retains reliably, and this is the first run where that comparison is made inside single invocations. **Qualified, 13 September:** 25 draws at 60,000 tokens of a row that is bimodal across the archive, and 60,000 is not a floor -- the fallback can shorten a 3,500-token result behind the record there and did offline (runs 41 to 48 section above). Three of these 25 carry the flag pair every later loss carries, and held.

**Cost still refuses to resolve.** Two arms name `tool_summary_anchored` and both print `NOT SUPPORTED` -- a 1% gap against a 36% spread, and 3% against 19%. Three arms name `none`. So on luna the record is at parity with not compacting and on mini it costs 10-25% more, which is the same answer this project has given at every cell it has measured.

**More compaction is not less money, stated as plainly as the data allow.** The repeats arm cuts the snapshot 62-66% against the no-repeat arm's 29-40%, and the cross-cell report puts it **10% dearer** on the same workload -- while printing `NOT RESOLVED` on that 10% against a 19% seed spread, so read the direction rather than the size:

tool_summary_anchored  $0.0240  repeat_records=off tool_summary_anchored  $0.0266  repeat_records=on

Two records break the cached prefix twice and write more output, and output is priced at six times input. Anyone reading `snap%` as a proxy for cost -- as I did twice during this run -- gets the sign wrong.

**The coverage threshold sits on a cliff.** `unc` and shrink move together one-for-one, and which side a seed lands on flips within an arm: mini at share 0.80 read `unc` of 4, 3, 0, 4, 3 with shrink of -2%, 9%, 41%, -1%, 8%. Coverage clears the 0.8 bar in **15 of the 25 rows** -- luna-fixed 5/5, luna-share80-repeats 5/5, mini-fixed 4/5, luna-share80 3/5, mini-share80 1/5 -- so the margin narrows as results grow and as one record is asked to cover more, but it is a cliff rather than a threshold anyone has calibrated.80 rows, so the margin narrows as results grow, but it is a cliff rather than a threshold anyone has calibrated. **`--coverage-share` is the sweep worth running next**, and it is newly reportable.

**Caveats.** The two share-0.80 cells are not the same workload: matched tool results (41,273 tokens) but different filler, so luna's tool share is 84% and mini's 94% -- cross-model reading at that cell is not like-for-like. **One arm seeded outside tolerance: `luna-fixed` at -7.8%**, the only arm the instrument flagged. `mini-fixed` came in at +4.2%, `mini-share80` +2.7%, and both share-0.80 luna arms at -2.3%, all inside. (An earlier version of this paragraph named `mini-fixed` at +5.3% and `luna-share80-repeats` at -6.5%. Neither figure exists: -5.3% is run 42's fill, transcribed onto the wrong arm with its sign flipped.) Within-arm comparisons, which is where every number above comes from, are unaffected.

### Run 40 -- the repaired check, measured, and the cost axis says no

60,000 / 0.86, three seeds, four rows in one invocation so `anchored` -- the strategy this one falls back to -- is compared on the same conversations rather than across runs. Thresholds back at 0.6/0.9, so this is comparable with the archive. 28 records, no throttling, retries or errors.

**The repairs hold.** `unc=0` on every no-repeat row, `recs=1` per trigger where every archived row carried two, and `recfb=0` on all three mini no-repeat seeds -- the first rows in this project where the record covered its groups, they were dropped on its authority, the prompt landed under the ceiling, and the fallback never ran. Mini's shrink returned to 17-22% from the 5-6% the broken check had collapsed it to.

**And the cost axis contradicts the snapshot axis, which is the point of having both.**

| arm | `vs none$` | facts | snap% | control hit | record hit |
| --- | ---: | ---: | ---: | ---: | ---: |
| luna, no repeats | +9% | 53/53 | 60% | 96% | 85% |
| luna, repeats | **-7%** | 53/53 | 51% | 96% | 92% |
| mini, no repeats | **+44%** | 53/53 | 72% | 81% | 68% |
| mini, repeats | **+98%** | 50/53 | 91% | 80% | 71% |

Mini's no-repeat arm removes 17-22% of the snapshot and costs **44% more**. The cache explains it: the hit rate falls 81% -> 68% and output rises 9,278 -> 13,261 tokens at 6x the input price. A smaller prompt bought with a broken prefix and a longer answer is not a cheaper prompt. Any reading of these arms on `snap%` alone -- including the one I published mid-run, that mini's record was "doing four times the compaction of anchored for the same retention" -- is wrong.

**What the record is actually good at is not being cheap.** Across all twelve seeds it holds 53/53 in eleven of them. On the same conversations `anchored` reads 39, 44, 52, 52 and `truncation` 20, 20, 29, 29. It is the only row that retains reliably, and it charges +9% to +98% for it.

**Repeats are model-dependent, and there is no good global default.** On luna they are better on every axis -- 40-42% shrink against 18-32%, `recfb=0`, and the only sub-zero cost figure in the matrix. On mini they are worse on every axis: negative shrink on all three seeds (-1%, -4%, -2%), because a second record on a model whose first one was already complete is pure duplication, added as preserved unshrinkable tokens. The mechanism is coherent -- repeats help exactly when one record cannot cover everything -- and it is conditional on the model, which the framework cannot know.

Neither sub-zero figure is resolvable. Luna's -7% carries `NOT SUPPORTED` against a 35% spread, and mini-repeat's `anchored` at -11% carries it against 46%. **Nothing here is resolvably cheaper than not compacting**, which is the same answer this project has given at every cell it has measured.

### Runs 38 and 39 -- WITHDRAWN in large part, see REVIEW-2026-09-06.md

> **Correction, 6 September.** Three adversarial reviewers took this section apart and I verified > what they found. The central claim below -- that the strategy was destroying its own record, and > that this explains the archived luna results -- **does not reach the archive**. Every archived > peak prompt sits 30-45% under its ceiling, so the post-record fallback could not have fired in > runs 26-35, and "runs 26-35 cannot be trusted" is withdrawn: they are the cleanest measurement > of record-then-drop as designed. The archived luna 21/53 means what section 3h said. The record > trim was real, but only under run-38 code. > > The coverage check this section credits is **broken and can itself lose data**: it tokenises > `code_1=VALUE` as one string, so a record quoting every value scores zero, and it matches by > substring, so a group holding `2026` is "covered" by any date. Every UNCOVERED figure below is > an artefact. > > Run 39's "-2%, ranking above the control" is not like-for-like -- 77% fill against 85%, half the > swing is the control moving, and the instrument printed NOT SUPPORTED. Against run 34's control > it is +25%. "The first row in this project to hold every fact without costing more" is false; > run 33 did it thirty lines above. > > The text below is kept as written so the correction can be checked against it.

### Runs 38 and 39 -- the defect was in the strategy, and fixing it changes the answer

Four levers failed because all four acted on what the model *writes*. The thing destroying the data was downstream of the record entirely.

**`--dump-record` settled it.** Luna's record, read off disk, covers **four lookups and all 32 identifiers** -- a competent record. The prompt the questions were answered from held two lookups' worth. The strategy calls a fallback when a record does not free enough, that fallback defaults to `AnchoredCompactionStrategy`, which shortens tool results in place, and **the record is a tool result**. `_anchored.py` had no concept of a record, so it trimmed the one artefact the strategy exists to produce. 16,617 tokens left the prompt while three messages did, which is shortening rather than deletion.

**Run 38 caught it happening on the good model.** A coverage check keyed on the group's function name -- which no model writes -- held back four of `gpt-5.4-mini`'s groups, left the prompt over the ceiling, and fired the fallback. Mini lost **nine facts**, the first fact it has lost in 45 records, on the one seed where the newly added `RECFALLBACK` flag fired. The regression made the pre-existing bug reproduce on the model that never fails.

**Run 39, both fixes in, three seeds a model, same cell:**

| | `vs none$` | facts | acc1 | snap% |
| --- | ---: | ---: | ---: | ---: |
| luna, archived (x5) | +56% | 21/53 | 41% | 48% |
| luna, run 39 (x3) | **-2%** | **53/53** | **100%** | 50% |
| mini, archived (x5) | +47% | 53/53 | 100% | 71% |
| mini, run 39 (x3) | +32% | 53/53 | 100% | 83% |

`tool_summary_anchored` ranks **above the uncompacted control on luna** -- the first row in this project to hold every fact without costing more. At a 24% spread that is parity rather than a saving, and the movement is what matters: +56% at 41% accuracy to parity at 100%.

`RECFALLBACK` fires five times a seed on luna now and costs nothing, which is the protection working: the fallback shortens the ordinary content around the record, and the record survives.

### What is still wrong, and it is the useful half

**Retention is fixed. Usefulness is not.** Value-based coverage succeeded on **one mini seed of three**; the other two compacted **nothing at all** (-0% and -1% shrink) while still paying for a record and an extra model call. Luna's coverage never succeeded in three seeds -- all its compaction now comes from the fallback shortening around a protected record, not from the record-and-drop design the strategy is named for.

So the failure mode has moved from "loses facts silently" to "costs a call and does nothing", which is the right direction and not the destination. The design earns its extra call only when coverage succeeds, and coverage is currently unstable seed to seed on the same model with the same prompt -- it turns on how verbatim that particular record happened to be.

`DEFAULT_COVERAGE_SHARE = 0.8` is a chosen threshold, not a derived one, and is not plumbed to the CLI, so no sweep can tune it yet. That is the first thing to try before concluding anything about the design.

### Instruction does not move it either -- run 37

The obvious response to "luna expounds on two lookups of six" is to tell it not to. That was tried and it does nothing. `RECALL_VALUES_DESCRIPTION` was rewritten to put breadth before depth -- "give every result its own group before expanding on any of them" -- to cap prose at "a sentence or two for each result", and to split the tie-break so exactness governed only the quoted values while coverage governed between results. The old tie-break, "keep exactness over brevity", was the clause most suspected of licensing the sprawl.

Same cell, default cap and target, so the prompt was the only difference:

| | facts | shrink (mean) | output (mean) | truncated |
| --- | --- | ---: | ---: | ---: |
| mini, old prompt (x5) | 53,53,53,53,53 | 20% | 18,167 | 0/5 |
| mini, new prompt (x3) | 53,53,53 | 21% | 16,311 | 1/3 |
| luna, old prompt (x5) | 21,21,21,21,21 | 38% | 20,398 | 5/5 |
| luna, new prompt (x3) | 21,21,21 | 43% | 23,148 | 3/3 |

No accuracy change on either model, and shrink and output both moved less than the seed range already spans -- mini's shrink runs 12-30% across five seeds of the *old* prompt alone. **The change was reverted**, and the suspected clause is cleared: removing "keep exactness over brevity" changed nothing, so it was not causing the sprawl either.

So four levers have now failed on the same number, fourteen records at this cell:

| lever | range tried | luna facts |
| --- | --- | ---: |
| `--record-max-tokens` | 4,000 -> 24,000 | 21/53 |
| `--record-target-tokens` | 2,000 -> 8,000 | 21/53 |
| record prompt | depth-first -> breadth-first | 21/53 |
| both bounds together | -- | 21/53 |

**What is left is structural, not textual.** `REC:1` in every record: one call is asked to cover every result, and `tool_choice` is pinned for one call only, a turn's pin applying to its first call alone. A design that guaranteed coverage would have to force per group, or check the record names each group it is about to drop and re-force for the remainder -- the middleware already forces twice, so the machinery exists. That is unbuilt and unmeasured, and it trades one model call for several.

### And on luna the record is beaten by blind truncation

Measured against `truncation` in the same cells, which is the comparison that asks whether the extra model call earns anything:

| cell | record facts | record shrink | truncation facts | truncation shrink |
| --- | ---: | ---: | ---: | ---: |
| mini 120K / 0.86 | **53.0** | 12% | 21.8 | 35% |
| mini 60K / 0.86 | **53.0** | 20% | 32.4 | 34% |
| mini share 0.80, 120K | **53.0** | 49% | 23.6 | 55% |
| luna 200K / 0.86 | 21.0 | 20% | 20.4 | **50%** |
| luna 120K / 0.86 | 21.0 | 24% | 19.0 | **51%** |
| luna 60K / 0.86 | 21.0 | 38% | 19.6 | **53%** |
| luna share 0.80, 60K | **53.0** | 40% | 41.2 | 35% |
| luna share 0.80, 120K | **40.2** | 59% | 38.0 | 33% |

On mini the strategy does its job: at share 0.80 it shrinks 49% against truncation's 55% while keeping **53 facts against 23.6**. On luna's fixed-payload cells it is **dominated** -- at 200K/0.86 truncation keeps the same facts with two and a half times the shrink, so the record buys nothing for an extra model call. It earns its keep on luna only where tool output dominates.

**The usable statement:** the record's advantage over deleting the oldest messages is model-dependent, and on luna it survives only in the share-0.80 cells. Nothing in the CLI recovers it elsewhere.

### Corrections carried out of the 6 September review

Three statements in the sections above were wrong or unsupported and are corrected here rather than silently edited, so the review can be checked against them.

- **Hit rates.** Stated as 75-91% for compacting rows; actually **56-97%**. Corrected in place above.
- **Five rows in this matrix carry `DRIFT`** and are quoted with no caveat: `anchored` at 200K/0.86 seeds 2 and 5 (5 and 12), `anchored_min_gain` at 200K/0.86 seed 4 (12), `context_window` at 200K share 0.80 seed 3 (5), and `anchored` at 60K share 0.80 seed 4 (9). `DRIFT` means a probe's prompt was not the snapshot verbatim, so those rows overstate what reached the model, and their accuracy figures should be read as upper bounds.
- **"Which strategy is least bad is model-specific ... the two models disagree completely"** rests on a 12-point gap that sits inside both rows' own spreads (25% and 22%), and luna's mean of 40/53 at that cell is `[21, 53, 53, 53, 21]` -- three of five seeds matched mini exactly. The defensible version is that the models **do not resolve the same at that cell**, not that they disagree.
- **The `control spread` column** in the matrix tables mixes axes: it is neither the control row's `seed$+-` nor exactly its `run$` spread, while `vs none$` is on `seed$`. Treat it as indicative of scale rather than as a matched error bar. The conclusion it supports -- every negative sits inside its control's spread -- survives on either axis.

### The luna cells are less full than their labels, and the instrument said so

Seven of the nine cells tripped `FILL OFF TARGET`. The seeded conversation landed **-3.1% to -12.0%** below its fill target, where all nine `gpt-5.4-mini` cells landed **+2.1% to +4.5%** and none tripped it. The filler sizing solves for the turns it sends and cannot predict how much the model writes back, and luna writes back less than 5.4-mini on the same turns.

So the control's real fill runs 4 to 9 points under the label:

| cell | label | luna actual | 5.4-mini actual |
| --- | ---: | ---: | ---: |
| fixed 60K / 0.86 | 86% | **77%** | 89% |
| fixed 120K / 0.70 | 70% | **62%** | 71% |
| fixed 200K / 0.86 | 86% | **79%** | -- |
| share 0.80, 120K / 0.86 | 86% | 83% | 88% |

**Within a cell this changes nothing** -- all six rows share one turn list and one actual fill, so every `vs none$` in this write-up stands. **Between the models it is a caveat**: the paired cells are close but not identical, and the two furthest off -- fixed 120K/0.70 at -12.0% and fixed 200K/0.86 at -8.7% -- should not be read as the same operating point on both models. The cell carrying the cross-model conclusion, share 0.80 at 120,000, is -3.6% against mini's +2.1%, which is the closest pairing in the matrix and the reason that comparison is the one quoted.

### Luna is a noisier subject

Control spreads run 6-35% against 5.4-mini's 3-31%, and single strategies reach 101% and 141%. Five seeds resolve less here, and no luna cost figure under roughly 40% should be read as meaning anything.

Cost: $36.08, roughly EUR 33.

---

## Models that could not be measured

**`google/gemini-3.7-flash` — excluded.** Its turns fail partway through a conversation with `Invalid thought signature.` (Google, HTTP 400). Gemini's reasoning models require the thought signature attached to earlier assistant turns to survive intact into later requests, and that is exactly what compaction rewrites. Simple calls succeed, and calls with tools succeed; the failure appears once a multi-turn history is replayed. Upstream rate limiting (HTTP 429 from Google AI Studio) was also observed on the same route.

This is not a harness defect and repeats would not fix it. It is a real constraint worth knowing: **client-side compaction and reasoning models that sign their thoughts are not straightforwardly compatible.** Any strategy that rewrites or drops an assistant turn risks invalidating the signature chain, which fails the request outright rather than degrading the answer.

## Known gap: every planted fact sits at the head of its tool result

`ToolResultCompactionStrategy` collapses an old tool-call group by **head-truncating its result at 4,096 characters** — it does not extract, summarise or select. Our markers sit in the first ~100 characters of each result, so they survive that cut unconditionally.

**So "tool_result kept 17 of 17 facts" is a fact about our data layout, not about the strategy.** The same strategy on a workload whose salient content trails its filler would score zero, and every table here would look identical while measuring the opposite thing.

Planned test: return 6,000-8,000 token results from some tool calls with their facts at the **end**, keeping others head-placed, so one run measures both. At 4,096 characters the truncation keeps roughly the first 6-8% of such a result, so the tail-placed facts should be destroyed while the head-placed ones survive — a strategy that currently scores 100% should land near 50%. If it does not, the mechanism is not what this note claims.

This is also the argument for content-aware extraction over positional truncation: the information a strategy keeps should not depend on where in the payload a tool happened to put it.

## Observations holding across runs

> **Qualified, 26 September 2026.** "No strategy has been both cheaper and as accurate" held for > every cell where the whole conversation fit. Past the window it no longer holds: see the final > report above (runs 63 and 64), where `tool_and_user_summary_anchored` keeps every fact for less > than an unlimited model would pay.

**The correctness result is a cliff, not a gradient.** Strategies that never evict messages (`none`, `tool_result`, `selective_tool_call`) lost **0** of 17 facts in every run. Strategies that evict lost **4 to 15**. Nothing lands in between.

**No strategy has been both cheaper and as accurate**, on any model, provider or API tested. The only cheaper options destroy most of what the agent was told.

**The cost result depends on the cache discount, and only on that.** At the 10x discount shared by luna and 5.4-mini, compaction loses. At glm-5.3-flash's 5x, `truncation` saves 51%. The break-even formula predicts which side a model falls on before running it — the discount and the achieved hit rate are the only inputs it needs.

**The governing quantity is the cache discount, not prompt size.** Compaction pays only when the volume cut exceeds the discount forfeited:

> `T2/T1 < (1 - h1(1-d)) / (1 - h2(1-d))`

where `h` is cache hit rate and `d` is the cached-read price as a fraction of input price. At the 10x discount common to these models, that means cutting **42-65%** of tokens just to break even — and strategies cutting that hard are exactly the ones that lose the facts.

**The shipped harness default (`context_window`, 0.5/0.8) has never won.** +16% on run 1, +41% on run 2, losing 13 of 17 facts in both.
