#!/usr/bin/env bash
# Wait until every gpt-6-luna fill 3.0 cell of run 67 is done, then run the gpt-5.6-luna half of the
# grid alone: five streams, one per seed, fills 0.9 / 1.5 / 3.0 each. Alone, because gpt-6-luna's
# fill 3.0 cells saturate the resource and evict gpt-5.6-luna's cached prefixes, which inflates the
# control's cost by 30-100% without any compaction involved.
set -u
S="/tmp"
P="$S/run67/progress.txt"
until [ "$(grep -c 'DONE gpt-6-luna f3.0' "$P" 2>/dev/null)" -ge 5 ]; do sleep 120; done
echo "$(date -Is) gpt-6-luna complete; starting gpt-5.6-luna half" >> "$P"
for s in 0 1 2 3 4; do
  "$S/run67-stream.sh" gpt-5.6-luna "s$s" "0.9:$s" "1.5:$s" "3.0:$s" &
  sleep 5
done
wait
echo "$(date -Is) gpt-5.6-luna half complete" >> "$P"
