# The strategies

Five compaction strategies and one supporting middleware, the fifth a composition of two of the others. This page says what each one does, what its defaults are, how it is wired up, and where it fails. The [front door](README.md) has the problem they answer and the measurements; the [research record](research/strategies-as-designed.md) has the design argument as it was written.

## The break-even every default is derived from

With `p` the input rate, `c` the cached rate, `R` the tokens an edit removes, `B` the tokens behind the edit and `T` the turns still to come, an edit pays when `R > B(p − c) / (p + T·c)`. At the measured prices and twenty remaining turns the right-hand side is 29% of `B`, and `B` is nearly the whole prompt for an edit near the head. Every threshold below is that formula solved for one of its terms: the minimum-gain floor is it solved for `R`, the user-turn band share is it solved for `T`, and the composition's chain target is it applied to the chain's own earliest edit.

Two consequences run through every strategy. Decisions are made from a message's *position*, never from the conversation's current size, so the same prefix compacts to the same bytes on every later turn and stays cached. And shortening is preferred to removing, so the conversation stays legible and the edit stays local.

## `AnchoredCompactionStrategy`

Keeps a fixed number of head groups and tail groups verbatim and shortens the tool results in the band between them. Each banded result is trimmed to a share of the ceiling set by its position counting from the head, `max_input_tokens × band_share ÷ (position + 1)`, so a result keeps the same allowance on every pass. Only if shortening leaves the prompt over the ceiling does it remove whole groups, oldest first, and only as a last resort does it shed assistant narration. It never touches user turns. Defaults: three head groups, four tail groups, a band share of 0.25, narration collapse on.

A message another strategy has marked *preserved* is skipped by all three removal paths. Preserved is not excluded: the message is still sent and counted in full, so a pass can end over the ceiling with everything removable gone. That is the intended outcome; the strategy stops on "nothing moved" rather than looping against a band it may not touch, and an honest overflow the caller can see beats a silent loss.

Where it fails: when tool results are smaller than their allowance it plans nothing, and lowering the share does not rescue a small payload, since the whole payload is then under the break-even. Past the window it keeps the conversation admissible at the cost of the values it trims. Measured at 1.5 times a 120,000-token window it kept 40 of 53 facts on one model and 31 on the other; at 3 times, 13.

## `MinimumGainAnchoredCompactionStrategy`

The anchored strategy with a floor. Before mutating anything it projects the collapse it is about to perform and declines if the saving is below `min_gain_fraction` of the tokens the collapse puts back on the meter, the included prompt from its earliest rewrite to the end. The default is 0.29, the break-even rounded up. The floor does not apply when the prompt is already over the ceiling, where shortening is what keeps the conversation admissible rather than an optimisation. `declined_collapses` counts the refusals, so a run that never fired and one that fired to no effect can be told apart.

The turns still to come divide the formula and nobody knows them at decision time: ten remaining turns need 43% of `B` and forty need 17%, so a workload with conversations shorter than the twenty the default assumes should raise the floor.

## `ToolResultAnchoredSummarizationCompactionStrategy`

The one strategy that carries information forward instead of discarding it, in two phases split across a middleware and the strategy.

`ToolResultRecallMiddleware` reads the conversation size on each call's way out. Past `trigger_fraction` of the ceiling, or once `max_groups_before_record` tool groups have accumulated since the last record, it pins the *next* request to a single tool, `recall_earlier_tool_results`, whose description asks the model to write down every value from earlier tool results that later work could depend on. The tool echoes that text back as a tool result. It sends no message: the tool's description is the whole prompt, so nothing extra reaches the stored history. The strategy then finds that record in the loaded history and drops the tool groups in front of it that the record demonstrably carries.

Coverage is measured in values, not in tool names. For each candidate group the strategy extracts the distinctive tokens of its results, whitespace-delimited, punctuation stripped, at least four characters and one digit, and calls the group covered when the record quotes at least `coverage_share` of them, 0.8 by default. A group the record does not cover is first held out of the fallback's reach while the strategy asks the middleware for another record, and preserved for good once asking stops helping. A prompt that cannot then be brought under the ceiling overflows loudly, which is preferable to the quiet loss it replaces.

Records repeat by default. A record covers what existed when it was written, so the middleware asks again once new tool work has accumulated, and re-arms on new *material* rather than on size, since the size that fired the trigger never goes away: the record is added to the conversation, and preserved. Every record is preserved, so records accumulate as a floor under the prompt; this strategy never merges them, because an older record is the sole account of the groups behind it. The composition does, as a last resort.

Defaults: a trigger of 0.6 and a give-up line of 0.9 of the ceiling, past which the strategy stops waiting for a record and hands the conversation to its `fallback`, an anchored strategy. The record is written by a model reading the tool payload and degrades with the bulk it is given, so the trigger wants to be early; the give-up line is far enough above it for the record, which arrives one call late by construction, to land. The record is asked for at a stated target of 2,000 tokens and capped at 4,000 on the forced call alone, two separate bounds because a cap truncates a tool call inside its arguments rather than making the model aim for a size.

Four constraints the design holds to. The record must be a tool result the provider issued, since a client-minted call is refused by routes that track tool calls server-side. The middleware sends no message. The recall tool cannot be hidden from the model, so `RecallGate` keeps it inert unless the middleware armed it. And the forced call's reply is bounded by two numbers, the target and the cap, not one.

Where it fails: the quality of the record decides the row. On a model that writes a record naming every value, the strategy keeps every fact and runs 32% under an unlimited model at 1.5 times the window. On a model whose record covers two of six tool groups, the uncovered groups are preserved, the prompt cannot be brought under the ceiling, and the conversation overflows: two seeds of five at 1.5 times the window, every seed at 3 times. It also cannot touch user turns, so a conversation whose bulk is user text outgrows it.

## `UserTurnAnchoredSummarizationCompactionStrategy`

The mirror of the record strategy on the other half of the conversation. Past `trigger_fraction` of the ceiling it takes the user turns between a fixed head and a fixed tail, sends them to a summariser client, and puts the summary back in their place as a single *user* message, linked to the turns it replaced through the framework's own summary annotations. It reads nothing but user groups, so it and the tool-side strategies measure independent things.

Defaults: one head turn and one tail turn, since the first turn carries the task and the last is the live request; a trigger of 0.8, later than the record strategy's because a summary costs only a broken prefix and wants to fire as late as it can; and a `min_band_share` of 0.1, the break-even solved for turns, which refuses a pass whose band is worth less than a tenth of the prompt. The share is what bounds the pass count: a band regrows only from new user turns, so the prompt must grow by `1 / (1 − f)` before the band is worth the share again, one pass per 11% of growth at the default. A pass whose band holds only the strategy's own earlier summary does nothing.

`summary_mode` decides what a pass does with the summary the previous pass left. `recompact`, the default, re-reads it, so one message stands for everything behind it. `boundary` never re-reads it: the summary is preserved as a boundary, the next band starts after it, and the prefix up to the newest boundary is byte-identical on every later pass, at the price of one standing summary per pass. `fold` is the boundary mode with a bound: once the ordinary band is declined and at least two summaries stand and folding them repays the break by the same formula, all of them are collapsed into one. The three modes have not been separated by measurement, so the default stays where it is.

The summariser is a trust boundary sharper than the framework's own: its output stands in for the user's turns, which is the half a model treats as instructions. Point it only at a service trusted as much as the primary model. What the summary keeps of the turns is not something the benchmark can measure, since its planted facts live in tool results.

Where it fails: alone, everywhere past the window, since it cannot reach the tool half. Measured alone it disqualifies on every seed at 1.5 and 3 times the window on both models. It is the composition's second half.

## `ToolResultAndUserTurnAnchoredSummarizationCompactionStrategy`

The two summarising strategies over one conversation. It owns no selection rule and removes nothing itself; what it adds is an order, one line for both halves, and a last-resort chain.

The record half runs first and records every new batch of tool results. The user half is judged at the same line, the record half's trigger, against the prompt *as the record half left it*, in whichever `summary_mode` the caller built it with; the composition does not set or require one, and the measured row used the boundary mode. It acts only if tool compaction was not enough, since its edit sits just behind the head and breaks nearly the whole cached prefix. While a record is due and still has time to arrive it is not judged at all: the record half compacts in two steps, ask on one pass and drop on the next, and a user half judged on the asking pass would summarise the user turns, take the prompt under the line, and leave the middleware with nothing to ask for. The wait expires after two model responses with no record, so a model that never records cannot leave the conversation uncompacted.

Then, only while the prompt is still over the input budget, a chain: merge the active records into one; merge the standing user summaries into one; rewrite the record harder, up to `harder_attempts` times, two by default; then the record half's fallback, with every tool group no record covers held so it may drop narration only; then nothing, and the prompt goes out over the limit, loudly. Once started the chain works down to a target rather than to the budget, removing `chain_gain_fraction` of the tokens behind its earliest edit, 0.29 by default, so the next turns have room before the chain is needed again. Every replacement is kept only if it is non-empty and smaller than what it replaces, and nothing is checked against content, because an overlap check fitted to one workload's values would reject correct paraphrase on another. A refused request is remembered and not asked again while what it would rewrite is unchanged.

The framework runs a strategy twice per call, over the copies sent on the call and over the stored history, and the two lists need not reach the same step. Every decision the chain makes is therefore kept for the run and put back on every list that holds what it changed, before the chain decides anything new; `decision_state` and `restore_decisions` expose that state so a harness that replays a conversation can carry it along.

Where it fails: wherever the record quality fails the record half. At 3 times the window on the model whose records leave groups uncovered, one seed of five overflowed after 172 fallbacks; the other four ran 30-56% under the control, and the mean the one seed drags to +79% is that seed. Both parts' costs are spent, an agent turn for the record and a summariser call for the summary, so inside the window it is a few percent dearer than not compacting.

## Wiring

Every strategy implements the framework's `CompactionStrategy` and attaches wherever the framework takes one: `create_harness_agent`'s before and after strategy, or a `CompactionProvider` on a plain `Agent`. The record strategy and the composition also need the recall tool registered and `ToolResultRecallMiddleware` installed, with the same tokenizer and ceiling as the strategy; the [package README](../../packages/maf-compaction/README.md) shows both. `find_nested_strategy` finds the record half inside a composition, since a composition is not an instance of its parts and a plain `isinstance` would leave the middleware uninstalled, which is a row that waits, falls back and reads as a model that refused to comply. `record_text` reads the record a conversation holds, for diagnostics. The three summarising strategies, `RecallGate` and the middleware keep one conversation's decisions on the instance, so an application serving several sessions builds that stack once per session; the middleware raises if a second session reaches it.

## What none of them do

Inside the context window nothing here beats not compacting: the best strategy costs within 3-8% of the control, inside the seed spread, and the whole conversation stays cached. Past the window the composition keeps every fact and is cheaper than an unlimited model, except on gpt-5.6-luna at three times the window, where it costs more and overflows on one seed of five; the rest either lose facts or overflow. None of them measures what a user-turn summary kept of the turns. All of them call `agent_framework._compaction`, the framework's private compaction helpers, for grouping, token annotation and the exclusion flags, so the dependency is pinned to one minor and re-read before the pin moves.

## Status

| Decision | State | Tracking |
|---|---|---|
| Position-only anchored trimming that honours preserved messages | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| Minimum-gain floor at the break-even share | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| Record-then-drop with value coverage, re-force, preservation and repeated records | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| User-turn summaries in three modes, bounded by the band share | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| Composition judged after the record phase, with the chain working to a target | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| Chain decisions kept across both lists and restorable by a harness | Implemented | [maf-compaction](../../packages/maf-compaction/README.md) |
| The benchmark that produced the measurements | Pending | untracked — arrives as `maf-cachebench` |
| Separating the three user-summary modes by measurement | Not started | untracked |
| Summarising each tool result individually rather than all in one record | Not started | untracked |
| Private-API dependency re-read on every core minor | Ongoing | [release-compatibility.md](../release-compatibility.md) |
