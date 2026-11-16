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

This matched conversation comparison is in progress. An initial attempt whose summarizer rejected temperature was stopped and excluded. A subsequent pilot with analytic `--fill 1.5` exceeded its predicted conversation size by 16.1% and yielded no benchmark verdict; the fixed workload above avoids claiming that fill level. Those pilot results are excluded from the comparison. The default change remains a draft pending the completed retention results. A host-supplied decision-model judge remains a separate follow-up, [#2777](https://github.com/sokolaidev/maf-extensions/issues/2777).
