# Summarizer data boundary

Issue [#2763](https://github.com/sokolaidev/maf-extensions/issues/2763) covers user summaries, folds, record merges and harder rewrites. The candidate defaults explicitly separate source data from instructions, retain legitimate requirements and corrections as reported content, preserve attribution of quoted instructions, and forbid unsupported additions. The default shortening and value-preservation requests remain unchanged.

## Comparison protocol

[compare_compaction_prompts.py](../../../scripts/experiments/compare_compaction_prompts.py) pairs the candidate prompts with the exact defaults from main revision `55612574ec2bdf500b4ed4fa010ab5b92428978f`, retained in [compaction_prompt_baseline.json](../../../scripts/experiments/compaction_prompt_baseline.json). It covers all four operations, including both default harder attempts. Each receives benign requirements or source facts, a task-override attack, and a fact-poisoning attack. User cases include a region correction and approval and credential constraints. Three repetitions produce 90 requests, alternating which variant goes first on successive repetitions.

Run from the repository environment, using an already configured cachebench provider:

```sh
uv run python scripts/experiments/compare_compaction_prompts.py --provider azure:model --repeats 3 --output .pytest_cache/compaction-prompt-comparison.json
```

Use the provider/model selector appropriate to the configured client. The client receives identical generation options for each variant. `--max-tokens` defaults to 2048, `--temperature` is omitted unless supplied, and `--timeout` defaults to 60 seconds per request. Errors are retained by type and make the command fail after the remaining cases finish. `--dry-run` writes the planned requests without constructing a client.

The report retains prompt hashes and text, synthetic sources, model outputs, distinctive-value recall, character ratios, finish reasons, reported usage and elapsed time. Inspect every answer for preserved corrections, approval requirements and credential constraints, supported facts, and attribution of quoted attacks. A copied attack marker may be a quotation; its presence is not proof of obedience. An absent marker is not proof of resistance. The lexical observations do not replace semantic review or qualify arbitrary providers.

Before merging the default change, retain a completed live comparison, review the answers, and run the normal cachebench retention workload against the old and candidate defaults with matched provider, configuration and seeds. Assess retention, request count, actual usage, latency and compression together. Reject incomplete outputs as evidence of successful retention. Record any attack success even if value recall remains perfect. The targeted probe does not measure long-conversation compaction frequency or prefix-cache costs.

## Current evidence

Offline planning with `CharacterEstimatorTokenizer` and the benign fixtures gives the following whole-request estimates. These are local estimates, not billed provider usage.

| Operation | Historical input tokens | Candidate input tokens |
|---|---:|---:|
| User summary | 274 | 388 |
| User fold | 299 | 412 |
| Record merge | 261 | 375 |
| Harder rewrite, attempt 1 | 255 | 368 |
| Harder rewrite, attempt 2 | 255 | 368 |

The increase can change whole-unit batch selection near an input cap. Tests exercise guarded request construction, unchanged-policy refusal reuse, retries after policy changes or legacy restore, accepted-history replay across prompt changes, custom rewrite prompts, and rejection of stale background answers. Deterministic adversarial answers can still pass the existing size and value checks; those tests bound the acceptance rule and do not claim a live model followed an attack.

## Live prompt comparison

The [retained synthetic evidence](summarizer-boundary-luna6.json) uses the Azure Responses route with `gpt-6-luna`, model version `2026-09-22`, in Sweden Central. The run used three repetitions, a 2,048-token output cap, no temperature option and Azure CLI authentication. Package metadata was `maf-compaction` 0.13.8, `maf-cachebench` 0.6.1 and `agent-framework-core` 1.24.0; the candidate was the working implementation of this issue, identified by its exact prompt text and hashes in the evidence.

| Result | Historical | Hardened |
|---|---:|---:|
| Attempted requests | 45 | 45 |
| Returned complete answers | 30 | 30 |
| Requests rejected by Azure content filtering | 15 | 15 |
| Expected-value recall in returned answers | 100% | 100% |
| Reported input tokens for returned answers | 6,261 | 8,751 |
| Reported output tokens, including reasoning | 8,579 | 12,449 |
| Mean answer characters | 270.5 | 303.8 |
| Total elapsed seconds for returned answers | 112.3 | 129.4 |

Every returned answer was read individually. Both variants retained the corrected region and the approval and credential constraints. Both reported the price-changing instruction as source content without replacing the verified price. No improvement in injection resistance was demonstrated: the baseline already resisted this fact-poisoning fixture, and the task-override fixture was filtered before an answer under both prompts. Those 30 filtered requests have no output to assess and are not credited to the prompt guard.

The hardened variant produced longer answers and used more input and output tokens in this small comparison. Output usage includes reasoning, so character counts alone understate that cost. These observations justify recording a cost trade-off, not inferring a stable latency or billing multiplier across workloads or models.

## Matched conversation retention

[compare_compaction_retention.py](../../../scripts/experiments/compare_compaction_retention.py) runs the normal cachebench live workload with either the retained historical prompts or the candidate defaults. Both variants use the candidate compaction implementation, so this isolates the prompts. It fixes the scenario salt from `--pair-id`; use the same value, strategy and seed offset for each pair. It also applies `--no-temperature` to the summarizer client, because the current cachebench CLI otherwise hardcodes its temperature to zero and Luna 6 rejects those requests. This client configuration is identical for both variants.

```sh
uv run python scripts/experiments/compare_compaction_retention.py --variant baseline --pair-id compaction-2763-paired -- azure-responses:gpt-6-luna --strategies none,tool_and_user_summary_anchored --repeats 1 --seed-offset 0 --probe-repeats 1 --combined-repeats 1 --context-window 16000 --fill 0 --tool-share 0 --filler-turns 6 --filler-tokens 1032 --tool-result-tokens 1708 --tool-turns 8 --markers-per-tool 4 --tokenizer tiktoken --record-max-tokens 2048 --answer-max-tokens 2048 --no-temperature --summarizer-provider azure-responses:gpt-6-luna --price-input 1 --price-cached 1 --price-output 1 --results-jsonl .pytest_cache/retention-baseline.jsonl
```

Repeat with `--variant hardened` and a separate results file, then with seed offsets 1 and 2. The measured variants run concurrently, each processing its three seeds in order. The fixed salt shares some cacheable prefixes across paired variants; cache-hit and latency comparisons are therefore not independent cache measurements. The three price inputs are unit weights for token accounting, not Azure prices or a bill estimate. The uncompacted control is expected to exceed the simulated 16,000-token window; its accuracy still anchors retention.

The [completed retention evidence](summarizer-boundary-retention-luna6.json) retains all twelve result rows from implementation revision `27541959e5c951868ab288d8cfa7545b1804f13b`: an uncompacted control and the composed strategy for each variant and seed. Every run completed 17 seeding turns and 10 closing probe steps. Each control retained all 37 facts and answered both measures at 100%, while exceeding the simulated context window. The composed strategy produced these observations; paired entries are historical / hardened:

| Seed | Historical facts retained | Hardened facts retained | Scoped answer accuracy | Combined answer accuracy | Summarizer calls |
|---|---:|---:|---:|---:|---:|
| 1 | 29/37 | 17/37 | 78.9% / 47.4% | 32.4% / 45.9% | 8 / 18 |
| 2 | 29/37 | 29/37 | 78.9% / 78.9% | 78.4% / 78.4% | 8 / 8 |
| 3 | 29/37 | 29/37 | 78.9% / 78.9% | 78.4% / no nonempty sample | 8 / 7 |

Hardened seed 1 disabled the recall pin after value-free records and made more user-summary requests. Five of the six composed runs recorded one truncated recall record; hardened seed 3 recorded none. No summarizer request failed. The conversation workload exercised user summaries and folds, but no record merges or harder rewrites; those operations are covered by the targeted comparison above. Hardened seed 3 has an empty combined-answer sample list, which the CLI displays as 0%; it supplies no successful combined-recall evidence. The retained result does not identify why the answer was empty.

Across the three composed seeds, the historical variant reported 751,788 agent input tokens and 33,308 agent output tokens; the hardened variant reported 798,202 and 34,137. These include the recall probes and exclude summarizer usage. Summarizer calls increased from 24 to 33. These are observations for this workload, not a billing estimate or a stable overhead multiplier.

All six CLI runs printed **NO VERDICT**. The control message-count comparison assumes the strategy matrix includes at least one strategy that adds no messages; the selected matrix contains only the control and composed compaction, which adds summaries and recall messages. This produced a control-divergence diagnostic and withheld the ranking. That check was not bypassed. The independently scored retention and answer observations above remain visible, but there is no qualified cost comparison. Every composed run also fell below the configured 90% scoped-answer threshold.

The evidence does not qualify the candidate defaults for adoption. Two seeds matched historical fact retention, while one lost twelve additional facts; three stochastic runs do not establish the cause of that difference. The targeted experiment demonstrated no security advantage and increased request/output size. [PR #2778](https://github.com/sokolaidev/maf-extensions/pull/2778) therefore remains a draft: investigate the poorer retention seed and repeat a comparison with complete answer samples and a suitable control matrix before adopting the defaults. Accepted-history migration and the configurable rewrite path are independently covered by deterministic tests.

An initial attempt whose summarizer rejected temperature was stopped and excluded. A subsequent pilot with analytic `--fill 1.5` exceeded its predicted conversation size by 16.1% and yielded no benchmark verdict; the fixed workload above avoids claiming that fill level. Those pilot results are excluded from the comparison. A host-supplied decision-model judge remains a separate follow-up, [#2777](https://github.com/sokolaidev/maf-extensions/issues/2777).

## Isolated cache and effectiveness screen

[evaluate_compaction_effectiveness.py](../../../scripts/experiments/evaluate_compaction_effectiveness.py) runs three paired conversations through cachebench's `run_live`, with both variants on the same candidate implementation. Only the prompt policy changes. Scenario seeds remain identical within a pair, while a fresh execution identifier creates distinct cache keys and stable instruction prefixes for each variant and seed. The prefixes use equal-width decimal groups with equal local token counts. Canonical workload hashes omit random message identifiers. The execution order is baseline/hardened, hardened/baseline, baseline/hardened; conversations run sequentially.

```sh
uv run python scripts/experiments/evaluate_compaction_effectiveness.py --provider azure-responses:gpt-6-luna --pair-id compaction-2763-isolated-v1 --pairs 3 --output .pytest_cache/effectiveness.json --trace .pytest_cache/effectiveness-trace.jsonl
```

The bounded workload contains ten seeding turns and six closing probes, seventeen planted facts, a 24,000-token simulated context, a 4,096-token agent/record/answer cap, and a 1,024-token summarizer cap. Temperature is omitted for both clients. The script retains per-call provider input, output, cache reads and cache writes, plus local prompt sizes before and after each compaction pass, budget failures, summary inputs and outputs, operation counts, and recall answers. Recall-probe usage is separated from the conversation's `seed hit%` and workload cost. Missing usage stays unknown. Unhandled failures remain in the report and cause a nonzero exit after the remaining attempts; completed files are not overwritten.

Ten additional requests compare identical benign fixtures for summary, fold, merge and both harder attempts. This is targeted prompt coverage; it is not evidence that all four operations ran in the conversation workload. The script preserves complete answers for review of corrections and constraints, since lexical retention alone cannot establish those properties. This screen does not repeat the adversarial fixtures or claim a new security result.

[report_compaction_effectiveness.py](../../../scripts/experiments/report_compaction_effectiveness.py) reads the completed report and four matching Azure Retail Prices API rows, without making model calls. It reports every pair and token-weighted aggregate cache hit rates; a failed attempt prevents a complete aggregate cost comparison for that variant. The initial screen flags more than 5% workload cost growth, more than three percentage points of agent cache-hit loss, or poorer fact/answer retention in any pair. These practical tolerances are not statistical equivalence bounds. Cold-start evidence, summarizer failures and actual operation coverage must also be reviewed.

```sh
uv run python scripts/experiments/report_compaction_effectiveness.py .pytest_cache/effectiveness.json .pytest_cache/retail-prices.json --output .pytest_cache/effectiveness-assessment.json
```

The price input is an Azure Retail Prices API response containing exactly one USD/per-million-token row for each `6-luna ShortCo Inp Std Gl`, `6-luna ShortCo Cd Inp Std Gl`, `6-luna ShortCo Cd Wr Std Gl`, and `6-luna ShortCo Opt Std Gl` meter, each ending in `1M Tokens`. Keep the retrieved meter rows with the assessment. Cost assigns ordinary input to `input - cached - cache_write`, then prices reads, writes and output separately. The write rate is a total rate, not a surcharge on the ordinary input rate; see the [cache lifetime accounting documentation](https://developers.openai.com/api/docs/guides/prompt-caching). This is a public retail estimate, not invoice reconciliation. [Azure's cache documentation](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching) describes the provider's read/write fields and cache policy.

### Results from 17 November 2026

The [isolated effectiveness evidence](summarizer-boundary-effectiveness-luna6.json) retains six completed conversations, ten complete targeted answers, provider usage, compression events, reviewed summaries and the four retrieved price meters. Both variants used implementation `1a9064267da8ddd297f5a5ee621433569875af41` and the same Luna 6 deployment/version described above. Every pair has matching canonical workload hashes and distinct cache identities. Each conversation's first cacheable request reported zero cached tokens. No rate-limit retry, connection retry, summarizer failure or budget failure occurred.

| Seed | Facts, historical / hardened | Agent seed hit%, historical / hardened | Workload cost change |
|---|---:|---:|---:|
| 1 | 17/17 / 17/17 | 55.01 / 55.06 | +1.68% |
| 2 | 17/17 / 17/17 | 59.14 / 57.92 | +12.84% |
| 3 | 17/17 / 17/17 | 44.34 / 59.07 | -20.48% |

Every scoped and combined answer scored 100%. Token-weighted agent cache reuse increased from 52.62% to 57.43%; absolute uncached agent input fell from 133,934 to 125,393 tokens. Total workload retail cost, including summarization and excluding measurement probes, fell from $0.031786805 to $0.030383575 across the three conversations per variant: 4.41% lower. The observed Global Standard short-context rates were $0.10 ordinary input, $0.01 cached input, $0.125 cache writes and $0.50 output per million tokens. The assessment retains the formula and meter rows rather than treating unit weights as money.

Both variants made nine user-summary requests. Their actual summarizer input increased from 18,883 to 19,630 tokens, exactly 83 extra tokens per request. Summarizer retail cost increased 4.82%. The eighteen complete summary answers were read: requirement identifiers and streaming-pipeline corrections were retained as attributed user requirements. Historical summaries contained 6,044 characters in total; hardened summaries contained 4,138, against 134,805 source characters per variant. Those totals are a compression observation, not a universal size improvement. Seeds 1 and 2 used identical paired summary sources. In seed 3, the historical run discarded one truncated recall response and made an extra agent request, which changed the first two summary batch boundaries. All six runs kept at least 6,385 locally estimated tokens of headroom after their compaction passes. Per-pass reduction and the next three seeding requests' cache-hit rates are retained in the assessment.

No live fold, record merge or harder rewrite occurred. The ten targeted requests cover those prompt paths on identical benign source fixtures, including both harder attempts. Every complete answer retained the expected values and applicable correction, approval and credential constraints on review. Here hardened answers were longer: 1,044 versus 800 characters in total. Output usage, including reasoning, was 1,678 versus 745 tokens; one hardened second-rewrite answer spent 984 tokens on reasoning versus 106 in its baseline counterpart. This small targeted sample establishes neither a stable cost multiplier nor a security improvement.

**The quick screen does not clear cost neutrality.** Retention and cache tolerances held in all three pairs, but seed 2 exceeded the 5% cost-growth threshold. Aggregate savings must not hide that pair, and the extra baseline request in seed 3 makes the variation visible. More paired seeds or a baseline-versus-baseline control would distinguish persistent prompt overhead from model variation; none was added to this bounded run. The earlier quick comparison's 20,272-token budget failure against a 19,904-token limit also remains unexplained: two diagnostic reruns and this screen completed, but the original failing message list was not retained. No prompt or budget-enforcement product code was changed for this evaluation, and adoption remains unqualified.
