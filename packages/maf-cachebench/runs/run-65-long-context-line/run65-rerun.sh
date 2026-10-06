#!/usr/bin/env bash
# Run 65: does compacting below gpt-6-luna's 272K long-context line pay? 1M window, fill 0.4
# (a ~400K conversation), trigger 0.2 (~200K, under the line), control against the two strategies
# that held past the window. Long-context tier priced per request ([revision omitted]).
# Usage: run65-stream.sh seed [seed ...]
set -u
S="/tmp"
OUT="$S/run65"
mkdir -p "$OUT"
export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com"
BIN="cachebench_live"
ALL="${STRATS}"
for s in "$@"; do
  "$BIN" azure-responses:gpt-6-luna --agent harness --strategies "$ALL" --summarizer-provider azure-responses:gpt-6-luna --repeats 1 --seed-offset "$s" --probe-repeats 1 --combined-repeats 5 --fill 0.4 --context-window 1000000 --trigger-fraction 0.2 --assumed-reply-tokens 384 --max-output-tokens 2048 --answer-max-tokens 12000 --record-max-tokens 2048 --markers-per-tool 8 --tool-turns 6 --narration neutral --fact-placement spread --results-jsonl "$OUT/s${s}-rerun.jsonl" --price-input 0.10 --price-cached 0.01 --price-output 0.50 --price-cache-write 0.125 --long-context-threshold 272000 --price-long-input 0.20 --price-long-cached 0.02 --price-long-output 0.75 --price-long-cache-write 0.25 ${DRY:-} > "$OUT/s${s}-rerun.log" 2>&1
  echo "$(date -Is) DONE-RERUN s${s} exit=$?" >> "$OUT/progress.txt"
done
