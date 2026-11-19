# Measuring whether a merge preserves a value’s role

All 18 synthetic candidates retain both distinctive values from their two source records. The question asks for either the next deployment target or the amount approved for payment. For each pair, a faithful candidate reorders the fields, a faithful paraphrase restates them in prose, and a swap assigns each value to the other role. Four pairs with different wording and values form the held-out split (12 candidates). Two pairs form the development split (six candidates).

The missing-value policy accepts all candidates at full coverage. The structured control accepts candidates whose colon-delimited label–value pairs match the sources. The candidate judge receives the question, sources, candidate and fixed rubric once per candidate. Only the scorer sees the expected answer and which variant is under test. Refusing retains the source answer. Accepting uses the answer specified by the candidate template.

The held-out criterion is to refuse every swap, accept every faithful candidate and paraphrase in both tasks, and return a usable verdict on every case. A timeout, malformed reply, oversized request, uncertain label or confidence below 0.8 uses the count fallback and fails the gate. Each split records false accepts, false refusals, abstentions, total fallbacks, requests per candidate and median request latency. The timeout is five seconds and the synthetic judge input is capped at 4,096 UTF-8 JSON bytes. [Criteria recorded on #2784 before live inference](https://github.com/sokolaidev/maf-extensions/issues/2784#issuecomment-6995630192)

## Live results

On 19 November 2026 both existing Microsoft Decision deployments (model version 1, Global Standard) returned HTTP 200 for all 18 requests each. The fixtures, choice criteria, thresholds and runner were committed before either pass. [Frozen benchmark](https://github.com/sokolaidev/maf-extensions/tree/89803500ae9564ad1a3c7beca2a0eaee4ed3f5fe/packages/maf-cachebench/runs/role-preservation-v1)

| Held-out policy (12 candidates) | Unsafe swaps accepted / 4 | Faithful candidates refused / 8 | Abstentions / fallbacks | Requests per candidate | Median request latency |
|---|---:|---:|---:|---:|---:|
| Missing-value rule | 4 | 0 | 0 / 0 | 0 | — |
| Structured label–value pairs | 0 | 4 | 0 / 0 | 0 | — |
| Microsoft-Decision-1 | 2 | 0 | 7 / 7 | 1 | 137 ms |
| Microsoft-Decision-1.1 | 1 | 0 | 2 / 2 | 1 | 129 ms |

Both judges refused the two swaps of approved amounts. Decision-1 assigned 0.871 probability to “faithful” on the swapped cutover target. Decision-1.1 caught that swap. Both chose “changed” for the swapped rollback destination, with probabilities of 0.673 and 0.588 respectively. The count fallback accepted it in both runs. The structured control refused all four faithful paraphrases.

On the six development candidates each judge made six requests, refused both swaps, accepted all four faithful candidates and used zero fallbacks. Median request latency was 142 ms for Decision-1 and 156 ms for Decision-1.1. Across both splits Decision-1 used 3,400 input and 18 output tokens. Decision-1.1 used 3,768 input and 18 output tokens. The provider responses report usage in tokens.

Neither judge met the held-out criterion for runtime adoption. These measurements cover one pass on four held-out pairs and two task types. The CSV contains one row per provider request, including the native choice and its probabilities, the resulting policy decision and the answer available after that decision. [Per-request observations](observations.csv)

## Reproducing the comparison

Run the offline controls from the repository root with `uv sync --locked` and `uv run cachebench_role_preservation`.

For a live pass, sign in to Azure CLI with access to the existing deployment, set `FOUNDRY_ENDPOINT` to its Azure resource endpoint and `FOUNDRY_DEPLOYMENT` to its deployment name. Run the following command once per deployment, setting `--model` to its underlying model name and choosing a fresh output path. The runner sends 18 synthetic requests, verifies the returned model and saves the native choice, probabilities, usage and per-case policy decisions.

```sh
uv run python packages/maf-cachebench/runs/role-preservation-v1/run.py --model Microsoft-Decision-1 --output decision-1.json
```
