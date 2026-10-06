#!/usr/bin/env bash
# Run 67: the final grid re-measured on agent-framework-core 1.20.0 (lab at f86e07248). All 20
# strategies at 120,000 tokens, fills 0.9 / 1.5 / 3.0, five seeds, on gpt-5.6-luna (Foundry project
# endpoint) and gpt-6-luna (azure-responses on the resource endpoint). Shape as run 63 (6 tool turns,
# reply 384, output 2048, answer 12000). gpt-6-luna is priced with its cache-write rate and the
# >272K long-context tier, as in runs 65 and 66; gpt-5.6-luna as in run 63.
# Usage: run67-stream.sh MODEL STREAM fill:seed [fill:seed ...]
set -u
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com"
MODEL="$1"; STREAM="$2"; shift 2
export FOUNDRY_MODEL="$MODEL"
S="/tmp"
OUT="$S/run67/$MODEL"
mkdir -p "$OUT"
BIN="cachebench_live"
ALL="none,context_window,context_window_aggressive,context_window_lazy,truncation,anchored,tool_summary_anchored,anchored_no_assistant,anchored_min_gain,sliding_window,tool_result,selective_tool_call,summarization,token_budget_fallback,token_budget_tools_first,token_budget_truncate_first,token_budget_window_first,token_budget_summarize,user_summary_anchored,tool_and_user_summary_anchored"
case "$MODEL" in
  gpt-5.6-luna)
    PROVIDER="foundry"
    PRICES="--price-input 0.20 --price-cached 0.02 --price-output 1.20"
    ;;
  gpt-6-luna)
    PROVIDER="azure-responses"
    PRICES="--price-input 0.10 --price-cached 0.01 --price-output 0.50 --price-cache-write 0.125 --long-context-threshold 272000 --price-long-input 0.20 --price-long-cached 0.02 --price-long-output 0.75 --price-long-cache-write 0.25"
    ;;
  *) echo "unknown model $MODEL" >&2; exit 2 ;;
esac
for job in "$@"; do
  f="${job%%:*}"; s="${job##*:}"
  echo "$(date -Is) START $MODEL f$f s$s stream=$STREAM" >> "$S/run67/progress.txt"
  "$BIN" "$PROVIDER:$MODEL" --agent harness --strategies "$ALL" \
    --summarizer-provider "$PROVIDER:$MODEL" \
    --repeats 1 --seed-offset "$s" --probe-repeats 1 --combined-repeats 5 \
    --fill "$f" --context-window 120000 \
    --assumed-reply-tokens 384 --max-output-tokens 2048 --answer-max-tokens 12000 \
    --record-max-tokens 2048 \
    --markers-per-tool 8 --tool-turns 6 \
    --narration neutral --fact-placement spread \
    --results-jsonl "$OUT/f${f}-s${s}.jsonl" \
    $PRICES ${DRY:-} \
    > "$OUT/f${f}-s${s}${DRY:+-dry}.log" 2>&1
  echo "$(date -Is) DONE $MODEL f$f s$s${DRY:+ dry} exit=$?" >> "$S/run67/progress.txt"
done
