# The benchmark

`maf-cachebench` measures what Microsoft Agent Framework's compaction strategies cost in provider prompt-cache hits, and what they destroy while doing it. This page is about **running it**: installing, pointing it at a provider, every command-line option, reading the table it prints, and re-rendering or comparing results you already paid for. The findings are on the [front door](README.md) and the strategies in [strategies.md](strategies.md).

The run write-ups, the development-time notes on the twenty strategies and the test philosophy are kept as [research records](research/).

## Install

One extra per provider route; a run needs only the client it points at.

```bash
pip install maf-cachebench[openai]      # Azure OpenAI, OpenRouter and the Azure Responses route
pip install maf-cachebench[foundry]     # Microsoft Foundry project endpoints
pip install maf-cachebench[mistral]
pip install maf-cachebench[ollama]
pip install maf-cachebench[tiktoken]    # exact token counts; the estimator is the default
```

That puts five executables on the path. Note the **underscores** — `pyproject.toml` declares them that way, so `cachebench-live` is not a command even though argparse prints it in the usage line.

| command | what it does |
| --- | --- |
| `cachebench` | the replay harness: byte-identical scripted transcripts, comparable **across** providers |
| `cachebench_live` | the live-agent harness: a real agent, real replies, real tool calls, one model at a time |
| `cachebench_advise` | one model against several strategies, with a cheapest-strategy recommendation |
| `cachebench_recall` | what each strategy destroys, on one planted conversation |
| `cachebench_summary` | cost and correctness together on one conversation, with a recommendation |

Everything below covers the first two. The other three take a subset of the same flags and print their own `--help`.

## Credentials and provider selection

A provider is selected as `provider` or `provider:model`. The model rides in the selector rather than only in the environment because comparing two models on one provider is a first-class case — cache behaviour differs by model family at least as much as by provider. Only the first colon separates, so `openrouter:z-ai/glm-5.2:free` works.

```bash
# azure — a direct Azure OpenAI deployment, distinct from the foundry project route
AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_CHAT_COMPLETION_MODEL
#   optional: AZURE_OPENAI_API_VERSION, AZURE_OPENAI_MODEL (fallback for the model name)

# foundry — also needs a working DefaultAzureCredential (`az login`, or a managed identity
#   when deployed). A project endpoint on its own is rejected by the client.
FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL

# openrouter — the model must be a real slug, e.g. z-ai/glm-5.2
OPENROUTER_API_KEY, OPENROUTER_MODEL
#   optional: OPENROUTER_BASE_URL, OPENROUTER_PROVIDER_ORDER (e.g. "openai")

# mistral — MISTRAL_MODEL is accepted as a fallback for the model name
MISTRAL_API_KEY, MISTRAL_CHAT_MODEL

# ollama (cloud) — the model drops the ":cloud" suffix on the direct API
OLLAMA_MODEL
#   optional: OLLAMA_HOST (defaults to https://ollama.com), OLLAMA_API_KEY
```

**Pin `OPENROUTER_PROVIDER_ORDER` if you use OpenRouter.** It dispatches to an upstream provider that can change between requests, and a different upstream is a different cache; without the pin you are measuring the router. Setting it also disables fallbacks.

Under `cachebench` (replay), a provider that fails to construct is skipped with a warning rather than aborting the sweep, so one missing credential does not cost you every other provider's cells.

### What each provider reports

| provider | cache reporting | notes |
| --- | --- | --- |
| `azure` | yes | automatic caching. 1,024-token minimum; 128-token increments before GPT-5.6 |
| `openrouter` | yes | returns `cached_tokens` and `cache_discount`. Pin the provider order |
| `mistral` | yes | automatic but **intermittent** — use `--repeats` and read the spread, not the value. Cache reads billed at 10% of input, so pass `--cache-read-ratio 0.1` |
| `foundry` | unknown | depends on the deployed model |
| `ollama` | **no** | caches and never reports it. Judge by `reuse%` and latency only |

`prompt_cache_key` is not the switch on any provider measured so far — every one of them caches without it — so nothing sends one unless you pass `--prompt-cache-key`. An older deployment can reject the unknown field.

## The two harnesses, and when to use which

**`cachebench` replays a scripted transcript.** Each turn appends fixed request messages, compaction runs over the history exactly as `CompactionProvider.before_run` would, the projection goes to the provider, and then a *scripted* reply is appended — the model's real answer is discarded. That is what lets every provider and every strategy replay a byte-identical conversation, and it is the only mode whose numbers compare **across** providers.

```bash
# validate the matrix and see prompt sizes without spending anything
cachebench --dry-run --providers azure --sizes mid,large --strategies none,context_window,truncation

# the defaults: one provider, the mid transcript, four strategies, one repeat
cachebench --providers azure

# two models on one provider, plus a third elsewhere
cachebench --providers "openrouter:openai/gpt-5.4-mini,openrouter:z-ai/glm-5.2,foundry:gpt-5.6-luna"
```

**`cachebench_live` drives a real agent.** It writes its own replies and calls a real tool, so the history compaction acts on is the history an agent would actually accumulate. Two things only exist here: replies become history, so a strategy that compacts badly produces a worse reply which becomes worse history which it compacts again; and a turn is no longer one model call, since a turn that uses a tool bills several prompts of different sizes.

```bash
cachebench_live foundry:gpt-5.6-luna --agent harness --strategies none,truncation,anchored
```

The cost of that realism is comparability. Two models write different replies, so their histories diverge from the first turn. **Live numbers compare strategies within one model, never models with each other.**

### How a live run is structured

Three phases, and the split is the measurement design rather than plumbing.

- **Seed** drives every turn except the closing questions, exactly as an agent in use would. Only the user-side turn list is shared between strategies; the replies, and so the histories, diverge from the first turn.
- **Snapshot** is a deep copy of the session state taken once seeding ends. Deep, because compaction records its decisions by mutating the messages themselves.
- **Probe** restores the snapshot, asks one closing question, and throws the answer away. Each per-scope question is asked `--probe-repeats` times and the one combined question `--combined-repeats` times, independently.

So no probe's answer can reach another probe's context, no question is asked from a context an earlier question compacted further, and survival is scored against the snapshot — which is by construction exactly the context every probe was answered from. None of the three held when the closing questions were ordinary appended turns: each answer re-listed codes into the history, and the same strategy read 53/53 on a run that emitted 10,941 output tokens and 18/53 on one that emitted 4,873.

## Live options, by purpose

Defaults are as `cachebench_live --help` prints them. Every constructor parameter a sweep would want to vary is a flag, because a knob that is only a default cannot be measured.

### Choosing what runs

| flag | default | |
| --- | --- | --- |
| `provider` (positional) | — | `provider` or `provider:model`. Omitted only with `--from-jsonl` |
| `--strategies` | 11 of the 20 | comma-separated; see [strategies.md](strategies.md) |
| `--agent` | `plain` | `harness` swaps in `create_harness_agent`, which is what production code calls. Its optional providers are switched off, because each adds tools and system-prompt text to every measured prompt |
| `--repeats` | 1 | seeds per strategy — whole conversations driven from scratch. This is the axis that measures compaction's own reliability. 3 or more is what makes a ranking defensible |
| `--seed-offset` | 0 | number the seeds from here. The seed number goes into the scenario salt, so two invocations that both start at seed 1 build byte-identical conversations; offsetting is what makes several single-seed invocations into different seeds rather than one seed measured repeatedly |

### Sizing the workload

| flag | default | |
| --- | --- | --- |
| `--context-window` | 32,000 | the limit the run stands in for. Simulated, so it is enforced here: any call whose prompt exceeds it disqualifies that row, and a cell that disqualifies at all leaves the ranking |
| `--fill` | 0.7 | share of that limit the seeded conversation is sized to reach, solved analytically from the payload and filler sizes. Pass 0 to size manually from `--filler-turns` and `--filler-tokens` |
| `--filler-turns` | 6 | padding turns between planted facts. Ignored unless `--fill` is 0 |
| `--filler-tokens` | 2,000 | size of each filler turn; under `--fill`, the size the solver keeps them near while it picks how many |
| `--tool-result-tokens` | 4,000 | absolute size of each tool result: the fixed payload. **Read only under `--tool-share 0`**, since `--tool-share` wins when both are given |
| `--tool-share` | 0.6 | share of the seeded conversation that is tool-result text, deriving the result size from the fill target so the payload scales with the window. **Wins when both are given**; `0` selects the fixed path above. Needs `--fill`: under `--fill 0` it is refused if asked for and off if it was not. |
| `--assumed-reply-tokens` | 150 | how large the model's own replies are assumed to be when solving for the fill. The one term the solver cannot compute, and it is per model: ~150 on `gpt-5.4-mini`, ~602 on `gpt-5.6-luna`. Measure it from a one-seed probe before sizing a matrix on a new model |
| `--tool-turns` | 6 | tool-call groups to plant. Must exceed `--keep-last-tool-groups` or tool-oriented compaction never fires |
| `--filler-tool-turns` | 0 | extra tool calls whose results carry no codes: bulk without anything to remember |
| `--markers-per-tool` | 2 | verifiable codes each tool result carries. More codes raise the resolution of the accuracy measure and make narration a weaker substitute for preservation |

**Absolute is right within one window and wrong across two.** 3,500-token results are 6% of a 60,000-token context and 3% of a 120,000-token one, so a sweep over window sizes with `--tool-result-tokens` is a sweep over two variables. That disabled a strategy once: the anchored family shortens each banded result to a share of the *ceiling*, so its allowance grew from ~2,900 to ~5,900 tokens while the results stayed at 3,500, and at 120,000 it planned nothing while its rows were read as measurements. `--tool-share` holds the proportions.

**The payload scales with the window by default, and a fixed payload is a different workload.** With a fixed payload the tool results are the same size at every window and only the filler grows to reach the target, which systematically penalises a strategy that compacts tool results and nothing else: its largest possible saving is capped at the payload while the conversation it pays cache costs across grows without limit, so the cap tightens as the window widens. Measured on `tool_summary_anchored` at a fixed 3,500-token payload across 60,000, 100,000 and 170,000-token windows, the removed share fell **28.8% → 22.6% → 17.0%** while its seeding-phase cache hit rate fell **88.0% → 87.5% → 76.2%** against a control climbing from 96% to 98%. That reads as a property of the strategy and is substantially a property of the workload.

`tool_share` is part of the cell key, so a fixed-payload cell and a scaled one never pool into one row: they are different workloads. To run a fixed payload, pass `--tool-share 0` alongside `--tool-result-tokens`. 0.6 and 0.8 are the levels treated as realistic, and 0.6 is the conservative one.

The achieved fill and the achieved share are both measured on the uncompacted run and flagged if they land more than 5% from target. A payload that will not fit inside the smallest cell is refused with an error rather than quietly overshooting.

### Shaping the scenario

| flag | default | |
| --- | --- | --- |
| `--narration` | `neutral` | how hard the scenario pushes the model to restate tool values. `neutral` says nothing either way, leaving the framework's own guidance as the only driver — the configuration a typical caller gets. Also `prompted`, `suppressed` |
| `--fact-placement` | `spread` | where the codes sit inside each tool result, which decides what is being measured. `spread` gives each its own labelled line, so the score is how much compaction preserved. `buried` puts them inline in prose, so retrieval under noise is scored too — and compaction can then beat the control by deleting the haystack. `head` puts them all at the front, where every tool-oriented strategy preserves them for free |
| `--no-retrieval-guidance` | off | drop the clause telling the model to quote every identifier it is asked for. Only safe with an adequate `--answer-max-tokens`: at 900 the control scored 33% without the clause and 100% with it, which measures the cap and not retrieval |
| `--sweeping-question` | off | close with one question demanding every code at once instead of several targeted ones. Needs a large `--answer-max-tokens` — enumerating 53 codes is ~640 tokens before prose, and a truncated answer is scored as lost facts |

### Output caps, which are three different reservations

| flag | default | reserved by | sent on |
| --- | --- | --- | --- |
| `--max-output-tokens` | 2,048 | subtracted from `--context-window` to give the input budget every threshold is a fraction of | every seeding call |
| `--answer-max-tokens` | 4,000 | checked against the fill target before the run spends anything | the closing questions, and nothing else |
| `--record-max-tokens` | 4,000 | the seeding reservation | the one call `tool_summary_anchored` forces, and the composed row's record half with it. `0` leaves the run's own cap in place |

These are separate because a seeding reply is appended to the history and re-sent on every later turn, while nothing follows a closing answer — the snapshot is restored before the next probe, so its length is never re-sent, and it is the one call that has to enumerate everything planted. **One number per call path, reserved and sent.** Were the arithmetic to use `--max-output-tokens` while the request carried `--answer-max-tokens`, at a 60,000-token window with a 12,000-token answer cap the strategies would believe 57,952 tokens of input were available when 48,000 were.

Sizing `--max-output-tokens` too low also inflates the budget and can push a trigger above what the service will accept, which disables compaction with no warning.

### Tuning the strategies

Ranges are checked by the strategies themselves, and every selected strategy is **built before the run spends anything**, so a value out of range fails at the command line rather than on the first paid call. `--dry-run` builds from the flags it is printing a plan for.

**Anchored family** — `anchored`, `anchored_no_assistant`, `anchored_min_gain`, and the fallback inside `tool_summary_anchored`:

| flag | default | |
| --- | --- | --- |
| `--keep-head-groups` | 3 | groups at the start never touched: the task, its requirements, the corrections to them |
| `--keep-tail-groups` | 4 | recent groups kept verbatim — the working set. Too large and every new turn shifts a large block out of the tail and re-bills it |
| `--band-share` | 0.25 | share of the input budget the band's oldest tool result may keep, the n-th keeping an n-th of that. Lowering it makes the strategy act, and measured against the break-even it still cannot pay on a small payload: even at 0.01, shedding 94% of every result, the 18,114 tokens removed fall short of the ~29,900 the edit re-bills |
| `--keep-tokens` | 0 (derive) | fix retention at a flat number of tokens per collapsed result instead. Exposed to make the old comparison runnable, not because it is a good setting: 600 characters is 0.9% of a result at a 60,000-token window and 0.3% at 272,000, and the strategy scored 32 of 53 facts in the first case and 11 in the second |
| `--min-gain-fraction` | 0.29 | share of the tokens *behind* a collapse that `anchored_min_gain` must remove before making it. Derived from `R > B(p−c)/(p+T·c)` at the measured prices with twenty turns remaining. `T` divides — ten remaining turns need 43% of `B`, forty need 17% — so raise it for shorter conversations |

**The record strategy** — `tool_summary_anchored`:

| flag | default | |
| --- | --- | --- |
| `--trigger-fraction` | 0.6 | share of the input budget at which it asks for its record. Reaches both halves at once: the middleware reads the strategy's own value, so the ask and the wait cannot be set apart. Must be below `--fallback-fraction` |
| `--fallback-fraction` | 0.9 | share at which it stops waiting and compacts without a record. The gap is what the record has to arrive in, and it is a whole turn wide by construction. Cannot be 1.0 — past that line the fallback still has to fit the conversation under the ceiling |
| `--coverage-share` | 0.8 | share of a group's distinctive values the record must quote before the group may be deleted. A threshold, not a derivation: at eight values per result 0.8 tolerates exactly one unrecognisable value, and at two values per group the share can only be 0, 0.5 or 1. `1.0` is as brittle as the tool-name rule it replaced; `0` restores the older any-value-at-all behaviour, so the two can be run side by side |
| `--record-target-tokens` | 2,000 | length the recall tool's own description asks the record to aim for. The only channel that makes the model plan for a size, since the middleware sends no message. `0` states no target |
| `--max-groups-before-record` | 0 (off) | force a fresh record every N tool groups, alongside the size trigger. One record asked to cover a whole conversation is a record a model may only partly write — `gpt-5.6-luna` named two of six groups, and raising the cap, raising the target and rewriting the tool's guidance each left that unchanged. Each record costs an agent turn |
| `--record-repeats` / `--no-record-repeats` | on | let the size trigger ask again once new tool work has accumulated. On by default: off, `tool_summary_anchored` compacts once and then grows, and at three times the window it disqualifies on every seed of every model measured. Pass `--no-record-repeats` for a model whose one record is already complete (`UNCOVERED:0`) on a conversation that ends soon after it outgrows its window, where a second record is only duplication |

`0.6` and `0.9` hold against the argument for later lines, that 0.6 fires at 58% of a 60,000-token window: that is arithmetic about where the line falls rather than a measured cost. What is measured points the other way: the record degrades with the bulk it must read, 53/53 facts at 8,000-token results against 18/53 at 25,200, so a later ask is a bigger ask and a worse record, and it leaves fewer turns for the edit to repay itself over.

**The user-turn strategy** — `user_summary_anchored`, and the user half of `tool_and_user_summary_anchored`. Both rows need `--summarizer-provider`:

| flag | default | |
| --- | --- | --- |
| `--user-trigger-fraction` | 0.8 | share of the input budget at which `user_summary_anchored` summarises the user's own turns. Its own flag rather than `--trigger-fraction`, which belongs to `tool_summary_anchored`: the two thresholds answer different questions for the two single rows, and one flag would make a sweep of either a sweep of both. Higher than that one's 0.6 because this strategy pays only in a broken cached prefix, so it can wait. It decides when the *first* compaction happens and not how many there are — that is `--user-min-band-share`, and firing late was found not to bound the count at all. **Moves the single row only**: `tool_and_user_summary_anchored` judges both halves at `--trigger-fraction` -- the record half against the prompt the pass began with, the user half against what the record half left, so the user half acts only when tool compaction was not enough |
| `--user-min-band-share` | 0.1 | share of the included prompt the user band must be worth before `user_summary_anchored` will compact it. The hysteresis, and what bounds the passes: at `0` the strategy fires once per turn for the rest of a run that stays above the trigger, because after its first pass the band is its own summary plus the turns since, and each pass is a summarizer call and a rewritten prefix to free a few hundred tokens. 0.1 is the break-even share for a conversation of this benchmark's own length at the measured cached and uncached prices; raise it for shorter runs. `0` restores the unbounded behaviour so the two can be run side by side, and `USERHELD` in the flags column says how many passes it refused |
| `--user-summary-mode` | `recompact` | what `user_summary_anchored` does with the summary its previous pass left behind. `recompact` re-reads it: the next pass's band is the previous summary plus the turns since, one message stands for everything behind it, and every pass rewrites a message just behind the head — the strict-prefix argument; `boundary` never re-reads it: the summary is preserved as a boundary, the next pass compacts only the turns newer than it, and the prefix up to the newest boundary is byte-identical across passes — at the price of one standing summary per pass, a floor no later pass lowers, reported as `USERSUMMARIES` and `USERSUMMTOKENS`. `fold` is `boundary` plus a bound: once the band has stopped yielding and the standing summaries are worth `--user-min-band-share` of what is behind them, all of them are collapsed into one, counted as `USERFOLD`. The band share clears less often in the two boundary modes, so expect more `USERHELD` there; it is not re-tuned per mode, because the three are the arms of one comparison. **The default stays `recompact` because measurement has not separated the three modes.** Reaches the composed row's user half through the same builder. Nothing here measures what a fold keeps: the planted facts live in tool results, so `facts` and `acc1` are blind to the user half, and a fold row's accuracy reading as the control's is the instrument declining to look |
| `--record-harder-attempts` | 2 | how many times per pass `tool_and_user_summary_anchored` may rewrite its record harder once the prompt is still over the input budget after both halves and after merging its records and its user summaries -- step c of its last-resort chain, read by no other row. Each attempt asks for more compression, keeps identifiers, numbers and names verbatim, and is kept only if smaller than what it replaces. `0` switches the step off. `--user-summary-mode` does not reach that row, whose user half runs in `boundary`, and `--record-repeats` is always on for it |
| `--keep-head-user-turns` | 1 | user turns at the start of the conversation never summarised. Counted in user turns, not message groups, so it is not `--keep-head-groups`, which protects a prefix of groups of every kind and is read by four other strategies. One because one is what carries the task and its requirements, which every deleting strategy measured here throws away first |
| `--keep-tail-user-turns` | 1 | user turns at the end never summarised. One because the last user turn is the live request, and a model answering a summary of the question it was just asked answers the wrong question; the turn before it has already been answered and has no such claim, so raising this buys nothing and costs the band its newest material. Separate from the head so the two ends can be moved apart, which is the only way to measure the opening statement against the recent turns |

The composed row reads `--trigger-fraction`, `--fallback-fraction`, `--coverage-share` and the record flags above for its record half and the four flags here for its user half, so a sweep of any of them moves that half of it exactly as it moves the single row.

**Everything else:**

| flag | default | |
| --- | --- | --- |
| `--keep-last-groups` | 6 | groups `sliding_window` keeps, and the target `summarization` compacts to. The summarizer's input is bounded by the cell's input budget (window minus output reservation), not the framework's 8,000-token default, which skips every tool result larger than itself and leaves the row unable to compact a cell of this shape |
| `--keep-last-tool-groups` | 4 | tool-call groups the tool-oriented strategies retain verbatim. The framework's own default; with fewer groups than this in the scenario they collapse nothing at all |
| `--budget-fraction` | 0.5 | fraction of the input budget the `token_budget_*` family compacts down to |
| `--summarizer-provider` | — | required by `summarization`, `token_budget_summarize`, `user_summary_anchored` and `tool_and_user_summary_anchored`. Prefer the same model as the one under test: summarizer tokens are priced at the tested model's rates, so a cheaper summarizer is billed at the wrong price. Those calls never reach the agent's middleware, and charging them at zero would score the one strategy that spends money to preserve information as though preserving it were free |

### Probing and the accuracy bar

| flag | default | |
| --- | --- | --- |
| `--probe-repeats` | 3 | times each per-scope closing question is asked of the same snapshot. The facts and their positions are identical across these, so whatever they disagree about is the model's own willingness to enumerate. This is the `acc1` half |
| `--combined-repeats` | 5 | times the one combined question — every value at once — is asked, independently of `--probe-repeats`. Its own count because one `acc1` reading averages every scoped question while one `acc2` reading is a single answer |
| `--min-correctness` | 0.9 | fraction of the control's `acc1` a strategy must retain to be ranked. Under `--from-jsonl` it defaults to whatever the run that wrote the records used, so a rebuilt verdict is the verdict that was measured |

The two need different numbers of attempts to be equally settled, which is why the counts are separate. Probes are nearly all cache reads, so extra attempts are cheap: measured on the 25 recorded seeds of the 60,000/0.86 cell, going from one combined attempt to three added 6.6-9.3% of a seed's cost and EUR 0.36 to a cell that cost EUR 4.58. `acc2` is the mean over every combined attempt of every seed, so a file merged from runs that asked it once and runs that asked it three times weights each answer once rather than each seed once.

### Pricing

| flag | |
| --- | --- |
| `--price-input` | input price per million tokens |
| `--price-cached` | cached-read price per million tokens |
| `--price-output` | output price per million tokens |

**Only OpenRouter is auto-discovered; everywhere else these must be passed** or the money columns have nothing to work from. Pass the same currency throughout — the tool does no conversion.

### Provider quirks

| flag | |
| --- | --- |
| `--no-force-tool-calls` | let the model choose its own tool calls. Needed for routes that reject a pinned `tool_choice`, and it must then be set for the **whole** run: a run where some rows were pinned and others were not is comparing different conversations |
| `--server-history` | let the service keep the conversation server-side. Compaction then has nothing to act on, because the agent only sends the new turn. Off by default so that what is measured is actually compaction |
| `--no-temperature` | omit temperature for models that reject the parameter |
| `--tokenizer` | `tiktoken` (default) or `estimator`. The estimator is fast and runs about 2x a real BPE count; use `tiktoken` whenever thresholds must land on real token values |

**Tool pinning matters for comparability.** By default each tool turn forces its own no-argument tool and every other turn is closed with `tool_choice="none"`. Without it, models gather different numbers of facts between runs, which moves both axes for reasons unrelated to compaction. A row whose provider rejected the option carries `NO:temp` or a similar flag and is not comparable with one that did not.

### Output, durability and diagnostics

| flag | |
| --- | --- |
| `--results-jsonl PATH` | append one JSON record per seed, written and closed the moment that seed is scored |
| `--from-jsonl PATH [PATH ...]` | render the table and verdict from those files and run nothing. Needs no provider |
| `--dump-record DIR` | write the full text of every recall record to `DIR/<strategy>-seed<n>.txt` |
| `--dry-run` | print the plan and its rough size, call nothing |
| `--show-answers` | print each final answer in full |

**`--results-jsonl` is not optional in practice.** A cell is every strategy times `--repeats` seeds and can run for hours; its table only exists once all of it has finished, so anything that stops the process in between discards every seed already completed and already paid for. That happened: the 60,000/0.86 cell ran fifteen strategy-seeds over three and a half hours, died before printing, and left nothing. Each line carries the cell parameters, the cost and token components, what survived and what was lost, every per-probe correctness sample, and any error — everything the table reads. The file is appended to, never truncated, so a sweep can point every cell at one path and a resumed run extends what is there. Each seed also prints a one-line summary as it lands, so a row that has stopped preserving anything shows up hours before the table would.

**`--dump-record` is observation only.** The record is read back out of the finished conversation after the run is over, so nothing is added to any prompt, no extra call is made, and the tokens, the cache hits and the cost are byte for byte what they would have been without it. Only `tool_summary_anchored` — and `tool_and_user_summary_anchored`, which runs it as a phase — writes a record at all, and a seed whose model never wrote one produces no file rather than an empty one. Use it to read what the model actually preserved, which is the question an `UNCOVERED` flag raises and no count can answer.

## Reading the live table

Two rows from an archived cell at a 60,000-token window, with `summ$`, `nofetch`, `ignored`, `rep+-` and `rep2+-` cut out so the rest fits on a page. They predate the cache split: the `hit%` there is what today's table prints as `run hit%`, with `seed hit%` and `probe hit%` in front of it.

```text
strategy                 msgs   tok left/peak  snap%  calls        in  hit%     out  seed in$    seed$   probe$     run$ seed$+-  vs none$  facts  lost   acc1  seed+-  acc2  vs none   dq  flags
tool_summary_anchored   35/40   33,156/35,804    59%     31   742,980   85%  13,601   $0.0171  $0.0240  $0.0275  $0.0515     19%       -3%  53/53     0   100%     0pp   99%     100%   0%  NO:temp,FORCED:1,REC:1,RECFALLBACK:1,RECFORCED:1,RECORDS:1,UNCOVERED:3
none                    37/37   50,914/50,914    84%     30 1,020,578   94%  13,812   $0.0176  $0.0248  $0.0223  $0.0472     15%         -  53/53     0   100%*    0pp  100%        -   0%  NO:temp
```

That is a strategy that kept every fact, removed about a third of the control's snapshot, and came in 3% under it on cost — and the run's own verdict line reads `NOT SUPPORTED`, because 3% is inside a 19% seed spread. Reading the columns in order is how you arrive at that rather than at "-3%". Its flags carry `UNCOVERED:3` beside `RECFALLBACK:1`: a record that left three groups uncovered, and a fallback that ran behind it. Reading the flags is how you know a 53/53 was a draw and not a property, and a row whose seeds disagree on `facts` gets its own line under the table.

The tool prints the full legend under every table; this is the short form.

| column | what it is |
| --- | --- |
| `msgs` | messages in a probe's prompt, out of the most any call carried. Every probe is asked from the same restored snapshot, so this no longer drifts down through the questions |
| `tok left/peak` | billed tokens in that same prompt, and at the peak. **Watch this rather than `msgs`**: a strategy that rewrites content in place removes tokens without removing messages, and `msgs` cannot see it |
| `snap%` | the snapshot every question was asked from, as a share of the tried window. How hard compaction acted: the control sits at the fill the cell was sized to, and a strategy below it removed that difference |
| `calls` | model calls, seeding and probes together |
| `in` / `out` | input and output tokens billed across the whole run. Output has its own column because a total driven by how much the model *wrote* is a different finding from one driven by how much context it was *sent* |
| `seed hit%` | share of the conversation's input served from the provider's cache — seeding only, the probes taken out. **What compaction did to the cache, and the number to quote for it**: a deployed agent continues its conversation and has no probe phase. Compaction breaks the cached prefix by construction, so this is what it gives up to save tokens |
| `probe hit%` | the same share over the probes — the instrument. On the archived luna cells from 100K up it takes one of two values: about 99%, or 33.3%, which is four of twelve probes served whole and eight cold — repeats two to five of the combined question hitting each other and nothing else. A row draws the low value when its strategy was still acting on the store as seeding ended. A fact about the instrument, not a cost: nothing else in the row reads it. The per-seed and per-probe blocks under the table show which seeds and which probes |
| `run hit%` | seeding and probes together: what `hit%` was until schema 12. Kept because earlier write-ups quote it, not because it says what compaction did — the probes are about a third of a seed's input, so a seed that drew 33.3% on them reads a dozen points under one that drew 99% with the same seeding half |
| `seed in$` | the prompt side of `seed$` — uncached and cached together, output and summarizer and probes left out. The low-variance view: on a clean five-seed control the total moved 38% while the input side moved 13%, because output is priced 57x a cache read and the model's verbosity swamps the axis compaction acts on |
| `seed$` | what the conversation cost: seeding plus the strategy's own summarizer calls, and nothing else. **The ranking, the verdict and `vs none$` are all on this.** The money a deployed agent moves |
| `probe$` | what the probing cost — the instrument. Every probe re-sends the whole snapshot, so a strategy that compacted hard collects that discount once per probe, on a phase no deployed agent has. Folding it in turned one cell's -14.1% into -3.5% and flipped the sign on two others |
| `run$` | `seed$` + `probe$`: what was actually billed. Here because it is the number earlier write-ups quote, not because it ranks anything |
| `seed$+-` | spread between the cheapest and dearest seed, on `seed$`. **A gap smaller than this is not a result.** `0%` with one seed means stability is unknown, not that it is stable |
| `summ$` | what this strategy's own summarization calls cost, of `seed$` |
| `vs none$` | `seed$` against the control's. `?` means the comparison is unavailable |
| `facts` | planted facts surviving compaction into the snapshot: recall's ceiling, scored against exactly the context every probe was answered from. A mean over seeds; a row whose seeds disagree gets a per-seed line under the table, because 50/53 on the record row is four seeds at 53 and one at 37 |
| `lost` | compaction removed it, so the model could not use it — **the damage** |
| `nofetch` | the agent never called that tool, so the fact never entered the history. Not compaction damage; an uncompacted run shows these too |
| `ignored` | still in the snapshot but unused: the model's failing, not compaction's |
| `acc1` | the scoped questions — requirements plus one per tool lookup — each reply scored only against the values its own question asked for. A star marks the uncompacted control, which is ordered by the same rule as every other row and can land below the line |
| `seed+-` | points between the least and most correct seed, on `acc1`. Different conversations, so this is compaction's own reliability |
| `rep+-` | points between `acc1` repeats *within* one seed. Identical facts in identical positions, so this is the model's willingness to enumerate and nothing else. `0pp` by construction at `--probe-repeats 1` |
| `acc2` | the one combined question: share of all planted values present in one answer, meaned over every attempt of every seed |
| `rep2+-` | the same within-seed spread for `acc2`. At `--probe-repeats 1` this is the only within-seed variance the cell measures |
| `vs none` | `acc1` against the control's. **Read it together with `vs none$` or not at all** — cheaper and less correct is not a saving |
| `dq` | share of this cell's seeds that sent a prompt larger than the tried limit. A cell that disqualifies at all is excluded from the ranking rather than starred |

**Rows are ranked on both axes.** Rows retaining at least `--min-correctness` of the control's `acc1` come first, cheapest `seed$` first; the rest follow below a line naming the threshold. That ordering exists because ranking on cost alone puts the strategy that threw the conversation away at the top — the cheapest row of a cell is reliably the one that destroyed the most.

Below the table: every per-sample `acc1` and `acc2`, grouped by seed; the achieved fill and tool share against target; any throttling, with the seconds spent waiting; and the verdict. A verdict whose margin is inside the seed spread is printed with **`NOT SUPPORTED`**, and that line is load-bearing — every negative `vs none$` this project has measured carries it.

### The flags legend

The `flags` column is where a row says it is not measuring what its name claims. Read it before the money columns.

| flag | meaning |
| --- | --- |
| `DQ` | this row sent a prompt a model of this size would have refused |
| `EXCL` | out of the ranking for the other reason: it did not finish its turns |
| `ERR` | a failed turn |
| `THROTTLED:<n>` | calls re-sent after the provider refused them for rate reasons. The seconds waited are printed below the table, and they matter: a cached prefix that expired during a wait is a miss the `seed hit%` column charges to compaction |
| `RECONNECTED:<n>` | calls re-sent because the request never came back — connection dropped, or a 5xx. Counted apart from throttling because the waits are seconds rather than a quota window, so the prefix is very likely intact |
| `DRIFT:<n>` | probes whose prompt was not the snapshot verbatim, because the strategy acted again on the restored state. **A row carrying this overstates what reached the model** |
| `S<n>` | summarizer failures. The strategy catches its own errors and returns `False`, so a broken summarizer produces a run that never compacted and therefore scores *perfect* recall. A row with this flag is not evidence that summarization preserves anything |
| `<n>/<n>t` | turns completed |
| `REC:<n>` | whether the model ever wrote a record at all. Saturates at 1 and answers compliance, not quantity |
| `RECORDS:<n>` | how many records the conversation ended up carrying. Every record is preserved — unshrinkable, undroppable, never merged — so each one raises a floor under the prompt that no later pass can lower, and a row above 1 has money columns that are partly that floor rather than the workload |
| `FORCED:<n>` | how many times a record was asked for. One record per ask is the mechanism working; more records than asks is a defect, and it was one |
| `TRUNCATED:<n>` | forced calls the provider cut at `--record-max-tokens`, so that record may cover only part of what it was asked to preserve, and the missing part is scored as compaction damage |
| `UNCOVERED:<n>` | tool-call groups the record never named, which the strategy therefore refused to delete. **Alone, a cost rather than a loss**: those groups are still in the prompt, so the row paid for tokens a complete record would have replaced and lost nothing. **Beside `RECFALLBACK`, the opposite**: a kept group is not preserved, and the fallback that runs when the record leaves the prompt over the ceiling shortens and sheds the band's tool groups. A row with `UNCOVERED` alone is measuring the strategy declining to guess; a row with both is measuring the fallback deciding for it |
| `FALLBACK:<n>` | times it gave up and compacted another way. **A row with this is measuring that other strategy, not the one named** |
| `RECFALLBACK:<n>` | passes where a record did exist, was anchored on, and the row still fell back — what the record freed left the prompt over the ceiling. The quieter of the two: the fallback shortens tool results in place, so the row keeps its message count and loses its values |
| `NOGAIN:<n>` | collapses `anchored_min_gain` declined as below its break-even floor. Distinguishes "never fired" from "fired to no effect" |
| `USERCOMPACT:<n>` | passes where `user_summary_anchored` replaced a band of the user's own turns with one summary of them. Every pass re-bills the prompt from its edit to the end, and where that edit lands is `--user-summary-mode`: in the `recompact` mode above 1 is the strategy rewriting its own earlier summary just behind the head, and in the `boundary` and `fold` modes the earlier summary stands and the edit lands only on the turns newer than it. `USERCOMPACT:0` is the uncompacted control under another name, and exactly one of the next three says why |
| `USERREPLACED:<n>` | turns the most recent of those passes stands in for — how much of the conversation the row carries as a summary rather than verbatim, which is what moved `snap%` |
| `USERUNDER:<n>` | passes where the prompt never reached the user trigger, so the band was not even read |
| `USERHELD:<n>` | passes where it did and the band was not worth a pass under `--user-min-band-share`: empty, holding only the strategy's own earlier summary, or too small a share of the prompt to pay for the prefix a pass rewrites. The hysteresis working; `USERHELD` with no `USERCOMPACT` is a share set too high for the workload, not a strategy that failed |
| `USERSUMMFAIL:<n>` | passes where the summarizer raised or returned nothing, so the band was left exactly as found and those passes are the control too |
| `USERSUMMARIES:<n>` | summaries `user_summary_anchored` left standing in the conversation, at its last reading. The `boundary` mode's floor: one per pass, preserved, and nothing but a fold merges them — `RECORDS:<n>`'s accumulation on the user half, and no other column says the unshrinkable part has grown. Reads 1 in the `recompact` mode after any pass |
| `USERSUMMTOKENS:<n>` | tokens those standing summaries occupy: the floor in the unit that matters |
| `USERFOLD:<n>` | passes of the `fold` mode that collapsed every standing summary into one, each a rewrite of the prefix from the oldest summary's position — the break the `recompact` mode pays on every pass, paid only when the standing summaries had grown to `--user-min-band-share` of what is behind them. A run that folded twice and one that never folded differ by two whole-prefix breaks and a floor lowered twice, and only this flag separates them |
| `USERSTARVED:<n>` | **retired at schema 16; only on older records.** Passes of `tool_and_user_summary_anchored` where the record half's removals kept the prompt under the user half's line. That row now judges its user half after the record half on purpose, so the user half staying idle because tool compaction was enough is the design working, and `USERUNDER` counts it |
| `RECMERGE:<n>` / `RECMERGEREJ:<n>` | `tool_and_user_summary_anchored`'s last-resort chain, step a: passes that merged the records into one, and merges refused because they came back no smaller. The chain runs only while the prompt is over the input budget after both halves |
| `USERMERGE:<n>` / `USERMERGEREJ:<n>` | step b: passes that folded the standing user summaries into one, and folds refused as no smaller |
| `RECHARDER:<n>` / `RECHARDERREJ:<n>` | step c: harder rewrites of the record tried, up to `--record-harder-attempts` per pass, and those refused as no smaller |
| `RECHARDERSKIP:<n>` / `RECMERGESKIP:<n>` | from schema 18: rewrite attempts and record merges not asked for, because the same records were already refused at them. A refusal stands until the record changes, and a refused attempt forecloses the milder ones on that record too. Before 18 the live path's second list replayed a refusal and counted it again under `RECHARDER`/`RECHARDERREJ` and `RECMERGEREJ` |
| `CHAINTARGET:<n>` / `CHAINSHORT:<n>` / `CHAINKEPT:<n>` | from schema 19: passes the composed row's chain started on and brought down to its target (`--chain-gain-fraction` of the tokens behind its earliest edit, below the budget), passes every step left above it, and passes that put back a merge, rewrite, fold or shed the chain made on the live path's other list |
| `RECSUMMFAIL:<n>` | chain merges or rewrites the summarizer did not answer |
| `LASTFALLBACK:<n>` | step d: passes that reached the record half's fallback, which on this row runs there and nowhere else and may drop narration only. Beside `DQ` it is the chain exhausted, the intended loud failure. Every replacement the chain keeps is kept on size alone and never checked against content, so what a merge lost is read in `facts` and `acc1` |
| `NO:<opt>` | the provider rejected that option so it was dropped. A run that dropped `tool_choice` chose its own tool calls and is not comparable with one that did not |
| `FETCH` | this row gathered a different set of facts than the control |
| `MSGS:<+-n>` | this row is the control and its conversation was n messages away from the leanest strategy row's. Compaction only adds to the stored history, so the control has to match that row; when it does not, every `vs none$` in the cell compares two different workloads and the control is excluded so that none of them is ranked |
| `NOSPLIT` | this row cannot say what its probing cost, so its money columns are the invoice rather than the workload |

## Re-rendering and comparing archived results

`--from-jsonl` rebuilds the table and verdict from records and runs nothing, so it needs no provider and costs nothing. It is the same aggregation the live path uses over the same records, which is what makes a recovered cell *the* cell that was measured rather than a second reading of it — and there is a test asserting the two render identically, including where the ranking splits.

```bash
# one seed
cachebench_live --from-jsonl runs/run-67-core-1.20-grid/records/gpt-6-luna-f1.5-s0.jsonl

# several files, or a directory, read as one body of records
cachebench_live --from-jsonl runs/run-67-core-1.20-grid/records/gpt-6-luna-f1.5-s*.jsonl
cachebench_live --from-jsonl runs/run-67-core-1.20-grid/records/
```

Records **group into cells by what they measured**, not by which file they came from. So one file can hold a whole sweep and each cell still gets its own table, and several files that measured the same thing merge into one cell — which is also the guard: two arms that differ in a workload flag or a strategy setting stay apart rather than silently averaging. A cell missing strategies or seeds still renders and says which of each it holds against what the run set out to take, because nothing in the table itself would otherwise distinguish four strategy-seeds from fifteen.

### The cross-cell settings report

When the records hold **more than one cell**, a comparison section follows the per-cell tables. This is the question the per-cell tables cannot answer: two settings are two cells and so two tables, leaving the reader to diff them by eye — which is how two arms of one experiment came to be merged once already.

```text
Across cells: the cheapest combination that still answers, per model and per workload.

  Workload: window 60,000  fill 86%  payload 6,714x6 at share 80%  narration neutral  ...
  2 cell(s), 8 rankable row(s), acc1 bar 90% of each cell's control
    strategy                        seed$  seed$+-   acc1  vs none  seeds  settings
    -------------------------------------------------------------------------------
    tool_summary_anchored         $0.0240     19%   100%     100%      5  repeat_records=off
    none                          $0.0248     30%    98%*    100%      5  repeat_records=on
    ...
    Per strategy, what its own settings did, under the same guard:
      tool_summary_anchored       [repeat_records=off] 10% cheaper than [repeat_records=on],
                                  seeds varied by 19%: NOT RESOLVED

    NOT SUPPORTED: seeds of one of these varied by 30%, wider than the 3% gap
    between the two cheapest rows that clear the bar. Nothing is named best.
```

A *combination* is a strategy plus the settings it ran under. Rows are ranked on `seed$` behind the same accuracy bar each cell's own verdict applied, and only the settings that **differ** are shown. Two guards apply, and both fire often:

- **Never ranked across workloads.** A different window, fill, payload, narration or workload flag is a different conversation, so a smaller number under one of them is a smaller job rather than a better strategy. The flags are named on each workload heading, and a cell written before they reached the file reads `flags not recorded` — which is a statement about the record, not about the run. Models are kept apart for the same reason and one more: they are priced differently.
- **A gap inside the spread is refused.** Both the overall ranking and the per-strategy lines print `NOT RESOLVED` / `NOT SUPPORTED` rather than naming a winner.

## The replay harness: options and output

`cachebench` answers a narrower question — how a *prompt* caches — and is the only mode whose numbers compare across providers.

| flag | default | |
| --- | --- | --- |
| `--providers` | `azure` | comma-separated, each `provider` or `provider:model` |
| `--strategies` | `none,context_window,truncation,tool_result` | comma-separated |
| `--sizes` | `mid` | transcript presets: `small`, `mid`, `large`, `xl`, `xxl`. `mid` is ~20 turns and ~50 messages; `large` is ~100 turns and ~270 messages and costs roughly 20x more per cell |
| `--repeats` | 1 | independent replays per cell |
| `--response-max-tokens` | 16 | cap on generated tokens. Answers are discarded, so keep it small — you are only paying for prompts |
| `--temperature` / `--no-temperature` | 0.0 | sampling temperature, or omit the field entirely |
| `--context-window` | 60% of the transcript | simulated window driving compaction budgets. A 20-turn transcript never approaches 128k, so a real window would mean no strategy ever fires |
| `--max-output-tokens` | 512 | subtracted from the window to give the input budget. The model's ceiling, not the reply size you want |
| `--tokenizer` | `estimator` | `estimator` is fast and runs ~2x a real BPE count; use `tiktoken` whenever thresholds must land on real token values |
| `--keep-last-groups` | 6 | groups kept by `sliding_window` |
| `--keep-tool-groups` | 2 | tool-call groups kept verbatim |
| `--cache-read-ratio` | 0.25 | price of a cached input token relative to a fresh one, for the `eff_in` column |
| `--no-cost` | off | omit that column |
| `--prompt-cache-key` | off | send a per-cell key to providers whose caching is automatic |
| `--request-timeout` | 300 | seconds a single call may take. `0` disables |
| `--turn-delay` | 0 | seconds between turns, for strict rate limits |
| `--summarizer-provider` | — | client for the `summarization` strategy |
| `--out` | `cachebench-results` | output directory: per-turn JSONL plus a summary CSV |
| `--run-id` | timestamp | run identifier |
| `--dry-run` | off | run the whole matrix locally with no API calls, reporting prompt sizes and prefix reuse only |

Two independent measurement channels, and the output shows both:

| column | what it is |
| --- | --- |
| `sent_tok` | total prompt tokens the strategy sent across the session |
| `in_tok` / `cached` | provider-reported input and cached tokens — exact |
| `reuse%` | share left byte-identical to the previous prompt, recomputed locally. **The cache ceiling** — a provider can never serve more than the prefix that survived. Matching is at message granularity, so a message that changed at all contributes zero and the oracle never overstates |
| `hit%` | what the provider actually served from cache |
| `real%` | `hit%` ÷ `reuse%`. Below 100% means misses compaction does *not* explain: eviction, TTL expiry, minimum-size floors, intermittent engagement, or upstream re-routing. Deliberately not `cached ÷ reusable_tokens` — those totals use different tokenizers and dividing them halves the answer. Above 1.0 is possible and means partial-message token-level matching |
| `breaks` | turns where the prompt was not a pure extension of the previous one. Each is a forced re-prefill |
| `no_in` | turns that reported cached tokens but no input count. Some providers drop `input_token_count` on a hit; when this is non-zero, `hit%` is suppressed rather than divided by a denominator the provider never sent |
| `eff_in@<r>` | fresh tokens plus cached tokens priced at `--cache-read-ratio`. Set it to your provider's real discount to compare strategies on real cost |

`in_tok` should be **identical across repeats** for a given strategy — that is the byte-identical replay working, and it is what makes a varying `cached` column attributable to the provider rather than to the harness. The baseline to compare against is always `none`: it sends the most tokens and breaks the prefix zero times.

The harness enforces its own controls: a unique cell salt at the front of the system message so cells cannot serve each other cache hits, a system anchor sized above 1,024 tokens so prompts clear the provider minimum from turn 1, sequential execution so cells do not contend, cached tokens clamped to the input count they are a subset of, and turn 1 counted as a write since a real session pays for it too.

### One output pattern that is easy to misread

On `foundry` / `gpt-5.4-mini`, at a small transcript:

```text
strategy         per-turn input tokens        cached
none             668 → 4,096 (growing)        0 until turn 7, then 1280/1792/2304/…
truncation       667–1,400 (oscillating)      0 on every turn   (9/20 turns below 1,024)
context_window   666–850  (pinned)            0 on every turn  (20/20 turns below 1,024)
```

Compaction did not break the cache here — it shrank prompts **below the provider's minimum cacheable size**, so caching never engaged at all. The uncompacted control proves the mechanism is size and not compaction: with no compaction whatsoever, the same model reported 0 cached at 1,216 / 1,325 / 1,434 tokens and only began caching at 1,769.

So a `hit%` of 0 on a heavily compacting row is ambiguous, and the way to resolve it is to read `eff_in` rather than `hit%`, and to check the per-turn sizes against the provider's floor before concluding the strategy broke anything.

### Scope

Every provider here is driven statelessly — the full projected message list goes up on each turn. Routes where the **service** owns the conversation (hosted agent threads, Responses-style stores) are a different regime and are out of scope: the client uploads only a delta and the service maintains a stable prefix of its own, so local compaction works against it. Applications that let the service own context should expect the opposite conclusion from the one this benchmark reaches, and the useful comparison there is compaction on versus off, not strategy versus strategy. `--server-history` exists to demonstrate that, not to measure it.

## Controlling spend

Cost scales with `sizes` × `strategies` × `providers` × `repeats` in replay, and with `strategies` × `repeats` × probes in live. **`--dry-run` prints the arithmetic before you spend anything; start there.** In live mode the probe phase is usually the larger half — every probe re-sends the whole snapshot, and there are `scoped questions × --probe-repeats` of them plus `--combined-repeats` — so those two counts scale the dominant term.

Concurrency has to be sized against **strategies per invocation**, not invocations. Five parallel runs of four rows each was clean; the same parallelism at eighteen rows each rate-limited 18 of 90 records, including the control in every seed, and cost that run its entire cost axis.

## Development

```bash
uv run pytest -q packages/maf-cachebench/tests
```

Tests are offline; the provider call is stubbed. The [testing record](research/testing.md) explains what they defend and why. The strategies the benchmark was built to measure live in [`maf-compaction`](../../packages/maf-compaction/README.md); the benchmark depends on that package and the framework's private `_compaction` helpers, which is why both pin one minor of `agent-framework-core`.

## Status

| Decision | State | Tracking |
|---|---|---|
| Replay harness: byte-identical transcripts, comparable across providers | Implemented | [maf-cachebench](../../packages/maf-cachebench/README.md) |
| Live harness: seed, snapshot and probe, with the control as a price reference past the window | Implemented | [maf-cachebench](../../packages/maf-cachebench/README.md) |
| Records keyed on every workload and strategy setting, with the schema refusing what it cannot read | Implemented | [maf-cachebench](../../packages/maf-cachebench/README.md) |
| Cost split into seeding and probing, ranking on the seeding half behind an accuracy bar | Implemented | [maf-cachebench](../../packages/maf-cachebench/README.md) |
| Per-request long-context pricing and cache-write rates | Implemented | [maf-cachebench](../../packages/maf-cachebench/README.md) |
| Retrying the summariser path on rate limits rather than counting a failure | Not started | untracked |
| Fact retention measured for the user half of a conversation | Not started | untracked — the planted facts live in tool results |

