# Record-merge coverage comparison

Deterministic offline cachebench replay; no provider requests or provider cache-hit measurements. Each cell replays eight records with 64 distinctive values over six turns, at a 1,200-token budget and 0.5 record share. The controlled summarizer retains six of eight values from the first four records and five of eight from the rest. Splitting stays at depth two. The starting bar is 0.8 and the step is 0.05.

Budget failures are counted before cachebench measures the retained projection. Projected tokens include these refused passes for comparison; a real host would not send them. Token and reusable-prefix counts use the character estimator, not provider billing. Missing values are audited independently in the final projection; omitted values count accepted consolidation losses, excluding replay.

| Floor | Over-budget passes | Projected tokens | Reusable prefix tokens | Summary requests | Summary input tokens | Values omitted | Final missing / 64 | Accepted by bar |
|---|---|---|---|---|---|---|---|---|
| 0.8 | 6/6 | 24810 | 20535 | 7 | 9848 | 0 | 0 | none |
| 0.75 | 1/6 | 4349 | 1654 | 8 | 11554 | 20 | 20 | 0.75: 2 |
| 0.7 | 1/6 | 4349 | 1654 | 8 | 11554 | 20 | 20 | 0.75: 2 |
| 0.6 | 1/6 | 4349 | 1654 | 8 | 11554 | 20 | 20 | 0.75: 2 |

Run from the repository root: `uv run python scripts/experiments/compare_merge_coverage.py`. These results demonstrate the policy's token/loss tradeoff on controlled answers; they do not establish semantic retention or live model quality.
