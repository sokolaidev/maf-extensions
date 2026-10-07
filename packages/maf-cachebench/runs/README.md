# Archived runs

The records behind the published measurements, as `cachebench_live --results-jsonl` wrote them: one JSON line per strategy and seed, carrying the cell parameters, the cost and token components, what survived and what was lost, every per-probe correctness sample, and any error. `cachebench_live --from-jsonl <path>` renders the table and the verdict from them again with no provider configured, and the per-cell `reports/` hold the tables as rendered when the run finished.

| Directory | What it holds |
|---|---|
| `run-67-core-1.20-grid/` | The final grid on agent-framework-core 1.20.0: all twenty strategies, a 120,000-token window at fills 0.9, 1.5 and 3.0, five seeds, gpt-5.6-luna on a Foundry project endpoint and gpt-6-luna on an Azure resource endpoint. `records/` has one file per model, fill and seed; `reports/` the rendered table per cell, across all six cells, and every row beside the run that preceded it. |
| `run-65-long-context-line/` | gpt-6-luna at a 1,000,000-token window, fill 0.4, trigger 0.2, priced per request with the long-context tier above 272,000 input tokens: the control against the record strategy and the composition, five seeds. |

The shell scripts beside each archive are the exact invocations, with endpoints replaced by placeholders and development commit identifiers omitted. Their CLI arguments retain the recorded workload and prices. Scripts for a re-run name the seeds they repeat and why the originals were set aside; the set-aside records themselves are not shipped.

Reading the records needs the schema the package reads: `maf_cachebench.read_seed_records` refuses a line from a schema it does not understand rather than averaging a different measurement into the table, and a test in the package's suite opens every archived file to keep that promise honest.
