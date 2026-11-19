# Measuring whether a merge preserves a value’s role

All 18 synthetic candidates retain both distinctive values from their two source records. The question asks for either the next deployment target or the amount approved for payment. For each pair, a faithful candidate reorders the fields, a faithful paraphrase restates them in prose, and a swap assigns each value to the other role. Four pairs with different wording and values form the held-out split (12 candidates). Two pairs form the development split (six candidates).

The missing-value policy accepts all candidates at full coverage. The structured control accepts candidates whose colon-delimited label–value pairs match the sources. The candidate judge receives the question, sources, candidate and fixed rubric once per candidate. Only the scorer sees the expected answer and which variant is under test. Refusing retains the source answer. Accepting uses the answer specified by the candidate template.

The held-out criterion is to refuse every swap, accept every faithful candidate and paraphrase in both tasks, and return a usable verdict on every case. A timeout, malformed reply, oversized request, uncertain label or confidence below 0.8 uses the count fallback and fails the gate. Each split records false accepts, false refusals, abstentions, total fallbacks, requests per candidate and median request latency. The timeout is five seconds and the synthetic judge input is capped at 4,096 UTF-8 JSON bytes. [Criteria recorded on #2784 before live inference](https://github.com/sokolaidev/maf-extensions/issues/2784#issuecomment-6995630192)

## Reproducing the comparison

Run the offline controls from the repository root with `uv sync --locked` and `uv run cachebench_role_preservation`.

For a live pass, sign in to Azure CLI with access to the existing deployment, set `FOUNDRY_ENDPOINT` to its Azure resource endpoint and `FOUNDRY_DEPLOYMENT` to its deployment name. Run the following command once per deployment, setting `--model` to its underlying model name and choosing a fresh output path. The runner sends 18 synthetic requests, verifies the returned model and saves the native choice, probabilities, usage and per-case policy decisions.

```sh
uv run python packages/maf-cachebench/runs/role-preservation-v1/run.py --model Microsoft-Decision-1 --output decision-1.json
```
