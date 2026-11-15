# Why the composed pass got slower in 0.13 (#2724)

> Exploration record for [#2724](https://github.com/sokolaidev/maf-extensions/issues/2724): where the per-call cost of the composed strategy's pass comes from in 0.13, which review findings each slow path answers, and which fixes keep what those findings protect. Sections 1 to 9 are the analysis as written before any change; [section 10](#10-what-the-first-change-implemented), [section 11](#11-what-the-second-change-implemented) and [section 12](#12-what-the-third-change-implemented) record the changes made from it and what they measured. Sections 10 and 11 measured a host that keeps its list between calls; section 12 is about the hosts that do not.

A steady per-call pass on a long conversation (about 5,000 messages, 1,890 included, 120 records) took 2.5 s on 0.12.4, 17.4 s on 0.13.0 and 17.0 s on 0.13.1. This note records why each slow path exists, which review findings shaped it, and which fixes keep the invariants those findings protect.

**Sources:** the `maf-compaction-v0.12.4`, `-v0.13.0` and `-v0.13.1` tag sources; the bodies, review threads and review summaries of #1759, #1880, #2149, #2590, #2634, #2648, #2651, #2652, #2682 and #2707; issues #2631, #2632, #2647, #2658, #2660, #2661, #2706 and #2724; and instrumented runs of the 0.13.1 package suite (2,289 tests).

**Conventions:**
- Symbols are named as they stand at `maf-compaction-v0.13.1` unless stated.
- `rNNN` is the review comment `https://github.com/sokolaidev/maf-extensions/pull/2652#discussion_rNNN` unless another PR is named.
- Labels: **[read]** = read in source or PR text; **[measured]** = run; **[inferred]** = reasoned from the code, not run; **[uncertain]** = not established.

## Summary

- **The shed recount is original design,** from the package's first commit (`ba6238c0`, #1759), not a review-driven safeguard. A running total is exactly equivalent under the conditions below. **[measured]** 0 mismatches over 3,467 loop checks and 661 drops in the 0.13.1 suite. Stopping early when the ceiling is unreachable would change pinned behaviour.
- **Replay runs from five places per pass, not four.** All of its call sites were created in one commit (`50682b0c`, answering r4544506204) as a mechanical substitution of older replay sites, plus `_consolidate` → `_keep_records` after every accepted merge or rewrite.
- **When any package-written record is held, each replay runs twice:** a private preview on a fork, then the real one (`_replay_record_edits`, for r4546467981).
- **[measured]** in the suite:
  - `_keep_records`' replay never changed anything (0 of 2,045 calls);
  - after the `_compact` entry ran, no later entry changed anything;
  - the `_keep_decisions` entry is needed when the `_compact` entry is skipped (no retained reasoning).
- **Replay is idempotent, and one call is a fixpoint.** **[measured]**
- **The steady state is a costly no-op.** **[measured]** with 120 records and n≈382, a steady pass costs 35 ms on 0.12.4 and 513–540 ms on 0.13.1, of which 480–496 ms is replay that changes nothing. It builds 49 full-history copies, 8 forks and about 2,070 SHA-256 message keys.
- **`chain_seconds` (#2651) never bounded any of this work.** Replay, shedding and tokenization run outside it in every version.

## 1. The shed recount (`_anchored._shed`, `shed_to`)

### Origin
- The `_shed` loop, with `if included_token_count(messages) <= ceiling: break` placed before the skip for already-excluded groups, dates from `ba6238c0` (#1759). **[read]**
- `shed_to` was split out of `compact_to` in `16a3c53b` (#1880); its body is still `ba6238c0`'s.
- Later touches did not change the count: the `removable_spans` precompute (`7c0194c2`, #1963) and the answered-note and short-reply skip (`ab0d4af1`, #2149).
- `_anchored.py` is byte-identical from 0.12.4 to 0.13.1. 0.13.0 reached it on every call because #2706 kept that version over budget.

### Was counting inside the loop deliberate?
- **As a stop test, yes; as a cost, no.** The docstring says "Excluding leaves every index in the band valid, so the selection loop can re-measure after each step; the notes are then inserted in reverse index order" (the `_shed` docstring). What it protects is the separation of exclusion from note insertion. **[read]**
- **Nothing in the selection loop rewrites token annotations.** `set_excluded` only flips the flag and reason, and `included_token_count` sums cached per-message counts over non-excluded messages. `annotate_token_counts` runs before the loops and again after notes are inserted. **[read]**
- **No review finding asked for the per-group recount.** **[measured]** a search of all threads and review bodies of #1759, #1880, #2590 and #2648.

### Invariants a fix must keep
1. **Order:** oldest band group first; tool-call groups before assistant narration (`test_anchored.py::test_assistant_narration_is_the_last_thing_dropped`).
2. **Stop test:** "included tokens ≤ ceiling", evaluated before each candidate of the kind; the loop may end over the ceiling (`test_the_ceiling_is_met`, `test_a_band_of_nothing_but_preserved_messages_stops_over_the_ceiling`).
3. **Best effort when the ceiling is unreachable:** shed everything allowed, then stop (`test_the_ceiling_is_best_effort_when_the_anchors_alone_exceed_it`).
4. **Skips, in this order:** fully excluded group; all-marker group; short answered reply (#2149); any preserved member; not removable whole (#1963).
5. **Notes go in after the selection loop,** in reverse index order, then counts are re-annotated.
6. **`shed_to` repeats until a pass sheds nothing,** because notes add tokens (`test_shedding_continues_while_each_pass_makes_progress`, which also makes the `_shed(messages, kind, *, ceiling)` signature test-visible).
7. **Note ids are deterministic,** an existing note is recognised by text plus group, and a colliding id takes the first free suffix (#1759 r4198887761; `test_a_note_is_inserted_even_when_a_stored_message_holds_its_id`).

### Is a running total equivalent?
- **Yes, within one `_shed` call.** Take the total once at loop entry, and on each drop subtract the tokens of exactly the members whose flag flipped. Notes are inserted only after the loop, and the next `_shed` or `shed_to` iteration takes a fresh count. **[read]**
- **Conditions:**
  1. Subtract a member only if it was included before the drop. A partly excluded group can be dropped; that branch is unpinned.
  2. No `Message` object or `additional_properties` dict appears twice in the list. With aliasing, one `set_excluded` would remove two entries from the true count. **[measured]** 0 of 639 `_shed` calls saw either; unpinned.
  3. Unannotated members count as 0 in both forms.
- **No concurrency:** `_shed` is synchronous.
- **[measured]** a differential probe over the 0.13.1 suite: 639 calls, 3,467 checks, 661 drops, 0 mismatches. 49% of the recounts were spent on already-excluded groups.

## 2. Why replay exists, and why it runs more than once per pass

### What replay is for
**Hosts hand the strategy lists that disagree with decisions already taken:**
- the before-call copies against the stored history;
- a retry with the pre-compaction request: same objects, exclusions kept, insertions lost (#2648);
- a host that rebuilds the transcript each run (#2632, #2647);
- the framework's tool loop un-excluding superseded summaries (#2648);
- stores that keep replacements but lose flags (r4545910650);
- a restored session or another replica. **[read]**

**0.12.4 replayed records by content only:** a payload digest for reshapes, transcript plus count for merges. #2652 let records be edited together with their reasoning (#2631), and editing the prefix of retained reasoning can be a provider 400 (`research/record-reasoning.md`). The first review found content-only replay unsafe: "accepting record A also authorizes reshaping a different record B with the same body" (r4543636253). Replay certificates (`_record_replay.py`) grow from that.

### The entry points

| Entry point | Origin | Evidence it is needed |
|---|---|---|
| `_compact`, first statement, only when reasoning is retained | r4544282869, r4544506204: restore the accepted prefix before user edits and shedding change its window | **[measured]** changed the history in 279 of the 297 passes that reached it |
| `_reshape_records(replay=True)`, reshape-only | the position predates 0.13: before the record phase counts records | **[measured]** changed only in passes where the first entry was skipped |
| `_keep_decisions`, first statement | r4544506204: replay must precede the global shedding replay | **[measured]** acted in 18 passes, all without the first entry; this is where merges and rewrites replay on histories without retained reasoning |
| `_keep_records`, first statement, called from `_keep_decisions` | left where an older exact branch sat | **[measured]** 0 changes in 1,359 calls |
| `_keep_records` from `_consolidate`, after every accepted merge or rewrite | 0.12.4's transcript loop: "the new record may read exactly as one an earlier pass went on to replace"; the exact replay is incidental | **[measured]** 0 changes in 686 calls |

Why the first entry is guarded by "any retained reasoning" specifically is **[uncertain]**.

### Is it safe to replay once per pass?
- **Not as a blanket rule:** histories without retained reasoning depend on the `_keep_decisions` entry. **[measured]**
- **No in-pass undo found.** No review finding describes a phase that undoes a replayed record edit within one pass; the known undoers act between passes (#2648). **[measured]** no later entry changed anything after a full first entry. **[uncertain]** for untested combinations.
- **Idempotence holds empirically.** **[measured]** over 5,670 calls, a `True` return coincided with a changed history and state fingerprint, and re-running after each of the 557 changing calls changed nothing.

### What a memo must cover
Replay's result depends on all of the following. **[read]**
1. The list's membership and order.
2. Per message:
   - role, author name, message id;
   - stand-in and lineage keys;
   - the exclusion flag and reason (owned or external);
   - the preservation flag and its reason (the record phase sets and releases its own holds within a pass);
   - summary links in the group annotation;
   - the recall-prompt mark;
   - the contents.
3. Contents the package mutates in place between entry points: result shortening (`_apply_shortenings` in `_anchored.py`, `_release_recorded_rows` in `_toolsummary.py`), `repair_compacted_items`, and arguments (`_observe_forced` in `_toolsummary.py`). An identity-based fingerprint does not see these, so those sites must bump a generation.
4. Strategy state:
   - `_record_order`, `_shed_ids`, `_shed_groups`, `_kept_records`, `_reshaped`;
   - the user half's `_replacements` and its pass-scoped `_replay_refusals` (`replay_scope` in `_usersummary.py`).
5. `reshape_only`: a full replay covers a later reshape-only one, not the reverse.

## 3. Why every edit is re-validated on every pass

All of these checks come from #2652.

| Check | Finding | Prevents |
|---|---|---|
| Source certificate: identities, exact content, window layout | r4543636253 | A different record with the same body inheriting an accepted edit |
| Complete accepted batch only | r4543636332 | Reasoning left over an edited prefix |
| Exclusion state inside the window; originally excluded later reasoning | r4543984582 | A re-included reasoning group silently accepting the old edit |
| No authority when later excluded reasoning lacks a durable id | r4544282832 | A metadata-only change evading the guard |
| Acceptance order plus per-edit prerequisites | r4544282869, r4544506204 | A window accepted between two other edits being unreproducible |
| Isolated candidate views | r4544709388, r4544935560, r4544935616 | Certificate misses on shared-object histories; duplicated or missing turns |
| Fast path only when nothing needs restoring | r4545427716 | The fast path running while prerequisite exclusions are missing |
| Intermediate supersession chains rebuilt | r4545427658, r4550632229 | Reconstructed summaries left beside a later fold |
| Held outputs validated and staged | r4545910650 | Flagless stores ending with originals beside replacements |
| Member fingerprints (snapshot v9) | r4546467949 | A revived source beside the merged record |
| Private preview of the staged chain, plus placement | r4546467981 | A partial commit of a chain |
| Held user summaries validated | r4546467995, r4549108571, r4549108612, r4549878106 | Original facts hidden behind an edited or moved summary |
| External protection respected | r4549108520 | A host hold overridden by replay |

- **Why "already applied" is not short-circuited.** Replay never records "applied". It rebuilds each edit's accepted window on a copy and checks the certificate there, so applied and pending edits take the same path. **[read]**
- **Why output identity is not enough.** A held output's identity (`replacement_identity`: the stand-in or lineage key) can be valid while a source lost its flag (r4546467949), the output was moved (r4546467981), a source gained a host hold (r4549108520), later reasoning was re-included (r4543984582), or a prerequisite summary is invalid (r4546467995). A sound per-edit "already applied" predicate needs all of those conditions; nothing in the code computes it today. **[inferred]**

## 4. `message_key` caches
- **Origin:** a cost finding, r4543984623. The author kept the cache "local to a pass so history mutations cannot reuse stale fingerprints"; later findings widened it while keeping invalidation on mutation (r4544709446, r4545114267, r4549878170). **[read]**
- **No shared cache was proposed and rejected.** **[read]**
- **Why the current cache can't simply be shared.** `ReplayView.message_keys` is keyed by list index, and the key includes the exclusion flag and the stand-in and lineage keys.
- **What makes a shared cache feasible.** Copy-on-exclude (#2634) and view copies share the `contents` list, which `SummaryReplayCache.message_key` already relies on.
- **[measured]** in the steady state, the hashing comes from fresh views (one per entry point, per preview fork, and per expected replacement), not from invalidation.

## 5. `_record_order`
- **Why it exists:** it is append-only and persisted (`50682b0c` for r4544506204; `members` for r4546467949, snapshot v9). It is ordered because an edit's window is the history as it stood at acceptance, and later edits consume earlier outputs. It is persisted so a restored session or another replica can reproduce the prefix on a rebuilt history. **[read]**
- **Growth is not new; per-edit cost is.** 0.12.4's maps also grow, but their cost did not.
- **Pruning is unsafe.** Superseded edits are prerequisites of later ones on rebuilt histories, and a pruned edit no longer validates its held output. **[inferred]**
- **Schema changes need a new snapshot version.** `RecordEdit(**edit)` in `restore_session_state` raises on an unknown key, so adding a field needs v10.

## 6. The chain deadline
- **0.12.4 (#2651):** the chain ran inside `asyncio.timeout(chain_seconds)`. That only fires at an await.
- **0.13.0 (#2682, for #2660):** the deadline became admission-only, so an in-flight request completes and its accepted answer runs `_consolidate` → replay.
- **In neither version is replay, shedding or tokenization inside the deadline.** **[read]**

## 7. Other per-pass costs new in 0.13
1. The fifth replay entry point after every accepted merge or rewrite.
2. The preview doubling every replay.
3. Per-edit `_keep_shedding` on the view when an edit has shed prerequisites (r4544506204, r4544709388).
4. User-summary replay validation on every pass (`invalid_replay_summaries`).
5. `_completed_user_boundary` and `_last_reasoning_index`, O(n) on every `consolidatable_record_groups` call (46–55 per pass, mostly from replay).
6. The snapshot write serializing `record_order` on every `activate` exit.

## 8. Fix candidates

| Candidate | Verdict |
|---|---|
| `_shed`: count once, keep a running total | Safe with the conditions in section 1 |
| `_shed`: skip excluded and marker groups before counting | Safe |
| `_shed`: stop when the ceiling is unreachable | Unsafe: changes pinned best-effort behaviour |
| Replay once per pass | Unsafe as a blanket rule |
| Memoize replay on a fingerprint or generation | Safe with the coverage in section 2 |
| Drop the replay inside `_keep_records` (both callers); keep its transcript loop | Safe with conditions; **[uncertain]** only for a chain merge that makes an older, different edit's window match for the first time |
| Skip the private preview when the real traversal has nothing to commit | Safe with conditions |
| Skip applied edits by output identity | Unsafe without the full predicate in section 3 |
| Cache `message_key` across views within one replay call | Safe if the key covers every non-content input and the value pins the contents object |
| Hoist per-edit scans to once per replay | Safe if the `_record_order[edit_index:]` suffix semantics are kept |
| Prune or compact `_record_order` | Unsafe; safe variants are skipping non-applicable edits by index and not re-appending the same decision |
| `_insert_note`: reuse the sets, splice once | Safe if the suffix choice sees notes inserted earlier in the same batch |

## 9. Invariants no test pins today
1. The cost of an applied steady state, and its growth with the number of edits.
2. The number of replay traversals per pass.
3. Replay idempotence as a general property.
4. That nothing inside replay mutates a `Content` in place.
5. The running-total preconditions in `_shed`.
6. The number of `included_token_count` calls per shed.
7. `_record_order` growth across passes.
8. Replay via `_keep_records` after `_consolidate`.
9. In-pass undo between entry points.

## 10. What the first change implemented

The first change made from this record took three of the candidates in section 8 and left the rest. Outputs are unchanged; what a pass costs is not yet back to 0.12.4's.

### Implemented
- **`_shed` counts once.** The included total is taken on entry and each drop subtracts the included tokens of the group it removes, counted before the flags flip, so an already excluded member and an unannotated one both subtract nothing. The skips for fully excluded and all-marker groups moved ahead of the stop test, which they cannot affect. A list in which two entries share one `additional_properties` mapping, which is also how one `Message` listed twice shows, is detected once per call and recounted at every stop test, as before. Invariants 1 to 7 of section 1 and the `_shed(messages, kind, *, ceiling)` signature are unchanged.
- **`_keep_records` no longer replays.** Its transcript loop is unchanged, and so is every other entry point. A pass now replays at three places, and an accepted merge or rewrite adds none.
- **One replay call shares its message fingerprints.** `MessageKeyCache` is created by `_replay_record_edits` and shared by the preview, the real traversal and every view they build. Its key is everything `_message_key` reads besides the contents (role, author name, message id, stand-in and lineage keys, the exclusion flag) plus the identity of the contents list; an entry holds that list and its items, is re-read when the items differ, and is bypassed for a key field that is not a string or `None`. Nothing keeps it between replay calls.

### The uncertain case for `_keep_records`
Section 8 left one case open: a chain merge that makes an older, different edit's window match for the first time. It can be constructed. A rewrite is accepted on a merged record; a history is then handed without either, and the chain makes the same merge again, so the rewrite's window exists for the first time inside `_consolidate`. The removed replay would have applied the rewrite there. The transcript loop applies it instead, with the same result and no summarizer request, because:
- the edit and its kept transcript are written together, in `_consolidate`, so every text edit has a transcript entry;
- the transcript match is content-only, so it accepts whatever the certificate accepts for a window of adjacent records;
- the one thing replay may do that the loop may not, edit a prefix that retained reasoning follows, the merge itself could not have done either: `consolidate_records` refuses such a group unless the guard is yielding, and then it yields for the loop too.

What the loop cannot do is rebuild a window that is not standing, which replay does on a copy. If a case needs that, the edit is applied by the next entry point, one pass later, since replay is a fixpoint and those entry points are unchanged. That is a delay and possibly one summarizer request, not a different decision. No such case was found.

### Measured
A profiling harness drove the composed strategy through its session middleware over a synthetic reasoning-model tool loop, six model calls per run, three processes at a time on an otherwise idle machine; figures are medians of calls 3 to 6 and vary by about a tenth between runs. The shapes are the two the issue used: about 2,020 messages with 985 included, and about 4,950 with 1,890 included, both with 120 records.

| History | Version | Steady pass | Replay in it | Shed in it | Over budget | First call of the run |
|---|---|---|---|---|---|---|
| 2,020 messages | 0.12.4 | 1.25 s | none | not reached | 0 of 4 | 14.3 s |
| 2,020 messages | 0.13.1 | 5.82 s | 4.76 s in 4 calls | not reached | 0 of 4 | 24.9 s |
| 2,020 messages | this change | 4.04 s | 2.79 s in 3 calls | not reached | 0 of 4 | 7.7 s |
| 4,950 messages | 0.12.4 | 3.16 s | none | not reached | 0 of 4 | 105.7 s |
| 4,950 messages | 0.13.1 | 18.03 s | 15.41 s in 4 calls | not reached | 0 of 4 | 129.2 s |
| 4,950 messages | this change | 12.11 s | 9.00 s in 3 calls | not reached | 0 of 4 | 34.9 s |

- **Fingerprints per steady pass** fell from 35,272 to 5,538 on the smaller history and from 128,676 to 11,337 on the larger.
- **The first call** is the one whose chain merges: each accepted merge or rewrite used to replay every edit accepted before it.
- **The shed is not reached in those runs,** since 0.13.1 fits its budget. 0.13.0 does not (#2706), and `_anchored.py` is the same file in both, so the shed was measured by running 0.13.0 with only that file replaced:

| History | Version | Steady pass | Shed in it | `included_token_count` | Over budget |
|---|---|---|---|---|---|
| 2,190 messages | 0.13.0 | 7.19 s | 2.59 s | 5,933 calls, 2.46 s | 4 of 4 |
| 2,190 messages | 0.13.0 with this `_shed` | 4.03 s | 0.23 s | 4,411 calls, 0.04 s | 4 of 4 |
| 5,080 messages | 0.13.0 | 19.16 s | 12.31 s | 13,627 calls, 11.99 s | 4 of 4 |
| 5,080 messages | 0.13.0 with this `_shed` | 7.64 s | 0.38 s | 10,193 calls, 0.09 s | 4 of 4 |

  The remaining calls each count one dropped group's members, not the conversation.

### Outputs
- **Call for call, the same.** On the smaller history, every call's sent messages, stored exclusion flags, record texts and accepted edits were identical between 0.13.1 and this change; so were 0.13.0's with and without the new `_shed`, on both histories.
- **The chain deadline is wall-clock, so a faster pass asks more.** On the larger history 0.13.1's first call spent its `chain_seconds` after 14 record requests and this change made 16, which is one more accepted edit. With the deadline removed from both, all six calls were identical, at 17.99 s against 10.59 s per steady pass.
- **Differential runs over the package suite:** the new `_shed` against the 0.13.1 one on a copy, 661 calls and 0 differences; every cached fingerprint against a fresh one, about 96,000 lookups and 0 differences; the removed replay, run on a copy where it stood, would have changed the history in 2 of 2,180 calls, both of them the constructed case above.

### Pinned by tests, of section 9
- Items 1 and 2: replay traversals and fingerprints per steady pass, and fingerprints not growing with the edits replayed.
- Item 3: replay on an applied history changes nothing, one replay of a handed history is a fixpoint, and two passes over one history agree.
- Items 5 and 6: the running total against a recount at each drop and stop, a partly excluded group, a shared exclusion flag, and the counts per `_shed` and per `shed_to` round.
- Item 8: the constructed case above.
- Item 4 only in part: every cached fingerprint equals a fresh one across a replay that rebuilds, restores and re-sheds. Items 7 and 9 are not pinned.

A group whose message or properties are listed twice is not removable today, because its two spans carry one group id, so the shared-flag test lifts that rule to reach the recount.

### Not done
- **The replay memo.** What remains of a steady pass is each edit's window being rebuilt and matched at every entry point: `_record_replay_view`, `_match_record_edit` and `_held_record_replacements` are 8.5 of the 9.0 s of replay on the larger history. A memo with the coverage in section 2 is the fix for that, and is separate work. [Section 11](#11-what-the-second-change-implemented) removes the rebuilds on an applied history without one.
- **Hoisting the per-edit scans.** `_held_record_replacements` is about 1 s of that, and part of `_record_replay_view` is scans; most of the latter is the copy each differing window needs. It was left for the memo to make moot. Section 11 hoists the scans of `_held_record_replacements`.
- **Everything section 8 calls unsafe,** the preview skip, `_insert_note`, and any snapshot change.

## 11. What the second change implemented

The second change ([#2735](https://github.com/sokolaidev/maf-extensions/pull/2735)) took the hoisting candidate of section 8 and, in place of the memo, a skip for an edit whose window cannot match. Outputs are unchanged.

### Implemented
- **Held outputs are indexed once per unchanged history.** `RecordReplayIndex` holds the held outputs, the known lineage identities, the reshape replacements, and each merge's replacement with its position in `_record_order`. `_held_record_replacements` reads an edit's suffix from it: reshapes count while the edit is at or before the last accepted reshape, and a merge counts from its own position on, which is the set the per-edit scan of `_record_order[edit_index:]` built. `ReplayView.invalidate()` drops the index and `fork()` does not carry it, so no view reads an index built for another history.
- **An edit none of whose sources can be included is skipped before its window is built.** `_available_record_members` collects, once per history, the fingerprint of every included message, and of every excluded one except a record-excluded one, keyed as if included: candidate staging can release shed and user-summary exclusions, but never record exclusions. When an edit's held outputs validate and none of its members is in that set, `_replay_record_candidates` moves to the next edit without calling `_record_replay_view` or `_match_record_edit`.

### Why the skip is not output identity
Section 8 calls skipping applied edits by output identity unsafe, and this skip is not that. It does not conclude that an edit is applied; it concludes that the full path would refuse the edit, and gives that refusal's answer. **[read]**
- With the held outputs validated, `_record_replay_view` stages them, so the view carries `record_replacements`.
- On that view, `_match_record_edit`'s first match fails, because no source is included, and its recovery path refuses, because replacements are staged and no member is included.
- The available set is a superset of what any view can include, so the skip fires only where that refusal does.
- A revived source, missing held outputs, and a legacy certificate without member fingerprints all take the full path.

**What it rests on:** that refusal in `_match_record_edit`. Without it, the full path releases the record exclusions and matches the edit again, and the skip hides the difference. **[measured]** With the refusal removed, all 2,338 earlier tests still pass; only `test_member_skip_agrees_with_the_full_matching_path`, added for this, fails, in both its cases: an applied history, and the same history with its earliest record-excluded output dropped. That test runs replay with the skip and with it forced off and requires the same result, and comments at both sites name the dependency.

### Measured
Section 10's harness and shapes, with this change and 0.13.3 run side by side, two processes at a time rather than section 10's three; figures are medians of calls 3 to 6. 0.13.3's figures here are lower than section 10's for the same code, so compare rows within this table, not across the two.

| History | Version | Steady pass | Replay in it | Windows rebuilt per pass | First call of the run |
|---|---|---|---|---|---|
| 2,020 messages | 0.13.3 | 3.33 s | 2.30 s in 3 calls | 31 | 6.13 s |
| 2,020 messages | this change | 1.55 s | 0.54 s in 3 calls | 0 | 6.18 s |
| 4,950 messages | 0.13.3 | 8.04 s | 5.77 s in 3 calls | 33 | 25.96 s |
| 4,950 messages | this change | 3.80 s | 1.40 s in 3 calls | 0 | 25.24 s |

- **What replay still spends** is `_held_record_replacements`, called once per edit and reading the index, and fingerprinting, the two overlapping: 0.53 s and 0.57 s of the 1.40 s on the larger history.
- **Fingerprints per steady pass rose** from 5,538 to 5,748 on the smaller history and from 11,337 to 14,547 on the larger. **[inferred]** `_available_record_members` fingerprints every shed or user-summary-excluded message as if included, once per replay, which is more than the window views it replaces took.
- **The first call is unchanged.** It starts with no accepted edits, so replay is not what it spends. On the larger history, 14.8 of its 25.2 s are the last-resort shed (`shed_to`), and 12.6 s of that is `_insert_note`: 1,309 calls, with 5.7 million `_is_marker` checks under them. On the smaller history the shed is 1.8 of 6.2 s and the merges 2.7 s.

### Outputs
- **Call for call, the same.** On both histories, every call's sent messages, stored exclusion flags, record texts and accepted edits were identical between 0.13.3 and this change, with the chain deadline in place; on the larger history both first calls made the same 16 record requests. With the deadline removed from both, all six calls were identical as well.
- **Differential replay over the package suite**, reported on the PR **[read]**: 1,577 comparisons across 731 record-reasoning tests gave identical return values, histories and persisted decisions.

### Pinned by tests, of section 9
- Item 1, further than section 10: an applied history replays without rebuilding a window, as the history grows and across reloads, and held sources are indexed once per replay.
- Item 4 stays pinned only in part, and items 7 and 9 not at all.
- Outside section 9's list: replay with the skip and with it forced off agree, which pins the refusal the skip rests on.

### Not done
- **The memo of section 2.** An edit whose sources can be included, or whose held outputs are missing, still rebuilds its window. **[inferred]** So the pass that re-applies edits to a history the host rebuilt without them costs about what it did in section 10, and the passes after it skip.
- **`_insert_note`,** now the largest cost of a run's first call on a long history, as measured above. Section 8's candidate for it, reusing the sets and splicing once, is safe if the suffix choice sees notes inserted earlier in the same batch.

## 12. What the third change implemented

Sections 10 and 11 timed a harness that hands every model call the list the previous pass left. A deployed host showed that 0.13.4 changed nothing for it ([#2743](https://github.com/sokolaidev/maf-extensions/issues/2743)): three consecutive model calls of one run held its event loop for 46 s, 79 s and 84 s, every stack sample in the per-call pass. The third change ([#2745](https://github.com/sokolaidev/maf-extensions/pull/2745)) is for the hosts that hand a rebuilt list.

### What a rebuilding host hands a pass
- **Per-service-call history persistence,** which the framework's harness agent turns on. **[read]** `PerServiceCallHistoryPersistingMiddleware` rebuilds each call's list from the history provider, and `SessionContext.extend_messages` makes a shallow copy of every stored message with its own `additional_properties`. **[measured]** in a harness agent run, no message object of one call was in the next call's list, and every stored message's `contents` list was. A message the call carries in is the object the store keeps, so a flag a pass sets on it is stored and a flag on a copy is not; nothing a pass inserts reaches the store.
- **The framework's reconciliation carries nothing to the next call on that path.** **[measured]** streaming, the message-injection middleware returns a stream that runs the rest of the pipeline when it is consumed, so `_reconcile_compaction_summaries` runs before the pass, over a list with nothing inserted. Not streaming, it runs after the pass over the list the pass worked on, which no later call is handed; it releases the superseded summaries there, and a resumed pass excludes them again before anything is sent.
- **A chat client with middleware and no per-call history** hands the pass a copy of the tool loop's list, sharing its message objects. **[measured]** the reconciliation then carries back what has summary links rooted in that list, the shed notes and user summaries, and not the records, which carry none. A record's sources stay included there, because the pass excludes copies of them.
- **Either way every call put back every decision.** **[measured]** on this note's profiling harness, with the list rebuilt as the harness rebuilds it, a steady pass took 5.4 s at about 2,020 messages and 18.8 s at about 4,950, against 1.3 s and 3.8 s for a kept list, and re-applied 14 and 15 merges on every call.

### Implemented
- **A per-call pass keeps the list it leaves.** `compact_before` records what it was handed, each message's `contents` list and host-visible state, and the list as the pass left it (`_carryover`). `__call__`, which the stored-history phase uses, neither keeps nor resumes.
- **The next call resumes from it** when it is handed those messages again, in order and unchanged, with new ones after them. A stored message is recognized by its `contents` list, which a shallow copy shares and the package never rebinds, and failing that by the list's items, the same objects or equal ones. Its state is everything in `additional_properties` except the group annotation and the attribution mark, with its role, author and durable id. A message the host kept from the view, as the reconciliation keeps a note, is skipped among the stored ones; everything after them is new, whatever object it is. Anything else gets the pass as it was.
- **The kept list is brought to what this call was handed.** The package reads the attribution mark to know where an exclusion lasts (`removable_whole`), and a view carried from an earlier call holds that call's marks, and for a carried-in message the stored object itself. So where the store holds a message exactly as the view does, this call's own message takes its place, under the id the view used; every other kept message takes this call's mark, and loses its token count if the host dropped this call's.
- **Positional ids.** `msg_N` ids are assigned to each handed list without the view's, so a new message whose id the view already uses has it assigned again.
- **The recall prompt is no stored message.** The prompt a forced record call ends with is handed for that call only, so it is left out of what was handed and of what is kept.
- **A list handed back as the pass left it** holds that pass's records and notes, which no rebuilt list will. What was handed before it stays the account of the stored messages, so a retry over the same list does not cost the next call its resume.
- **A refused budget keeps the list.** The closing budget check raises on a list every phase has already worked on, which is every call of a conversation that cannot fit. A phase that raises leaves nothing kept.
- **Bounded and local.** The list is held by one strategy instance, never serialized, and released at the end of a turn by the stored-history phase and by the record-merge middleware. A registry keeps one for its `max_carried_views` most recently used sessions, and lets one go when the snapshot it restores is not the one it last wrote: the list belongs to the decisions that made it.

### What it rests on
1. **A stored message's contents are the same objects, or equal ones, across calls.** **[read]** `extend_messages` copies with `copy.copy`, and no site in the package assigns `message.contents` on a message of the list. **[measured]** the framework's `_filter_approval_control_messages` hands a message that mixes a resolved approval with other content under a new list of the same items on every load, and a host that rewrites or deserializes a stored message hands equal items; both resume, the second at the cost of comparing those messages' contents.
2. **Flags are per copy and summary links are not.** **[measured]** a loaded copy shares its group annotation with the stored message, so a summary link a pass writes on a copy shows on the store while its exclusion flag does not. Links are therefore compared only to decide whether two messages are interchangeable, never as evidence that a host changed one.
3. **A handed message the pass replaced can still change.** The pass holds a record pair in place before swapping in copies, so the state it left on the handed message is kept and accepted; any other state on a message the view does not hold ends the resume.

If the framework stops handing the same or equal contents, no call resumes and every pass puts its decisions back: the cost returns and the output does not change. The harness test below fails then.

### Which framework versions
**[measured]** Everything in this section holds on `agent-framework-core` 1.24.0 and 1.25.0, where the tests of this change pass. It does not hold on 1.26.0, and the failure there is not the one above.

- **1.26.0 stores what a pass inserts.** **[read]** since [agent-framework #9877](https://github.com/microsoft/agent-framework/pull/9877) the per-service-call middleware stores the request as it stands after the model call, and takes every message without an attribution mark as input to store. A record or a note a pass inserted has no mark, so it is written to the session history; the exclusions, set on loaded copies, are not.
- **So the next call is handed the insertions as stored messages.** **[measured]** in the harness test's scenario on 1.26.0 no call resumes, and a seven-call turn leaves 95 stored messages where the conversation and the run's own messages come to 76. With no call resuming, those passes are the ones the package made before this change: the stored insertions come from the per-call pass itself, not from the carry-over.
- **That changes the store and what is sent, not only the cost.** The package's dependency ceiling, `agent-framework-core>=1.24.0,<1.25`, keeps it off that release. Raising the ceiling past 1.25 needs a decision first on what the store should hold and on how a pass treats its own stored insertions.

### Outputs
- **What a kept list sends.** **[measured]** over five conversation shapes and eight calls each, a host handing fresh copies was sent exactly what a host keeping its list was, content for content, and so was a chat client with middleware. **[measured]** in a harness agent run, streaming and not, every call was sent what it is sent with the resume disabled. An independent review of the change measured the same with pinned and refused record calls, reasoning items and several turns, and over 60 random conversations found no call sent a tool call or a result alone and no stored pair half excluded.
- **Not always what a rebuilt pass sent.** **[measured]** in three of those shapes the two agree on every call. In the two with the tightest budgets they differ from the second call: on one, a rebuilt pass sends 40 messages and a resumed pass 31, having folded ten user turns into a summary that a rebuilt pass never writes in eight calls, at five summarizer requests against four. **[read]** the user phase refuses a band worth less than `min_band_share` of the included prompt, and **[measured]** it is offered 6,494 tokens on a rebuilt list, before the closing shed is redone, and 3,059 on a kept one. The difference is the one that already stood between a host that keeps its list and one that does not.
- **The store.** **[measured]** a resumed pass changes the host-visible state of no stored message; a flag reaches the store only on a message the call carried in, as before.
- **On the profiling harness** every call sent the same tokens with and without the change, at both sizes.

### Measured
This note's harness, with the list rebuilt for every call as per-service-call persistence rebuilds it. Medians of the calls after the second: three calls in one process at the smaller size, four calls in two processes side by side at the larger.

| History | Version | Steady pass | Second call | Merges re-applied per call | First call |
|---|---|---|---|---|---|
| 2,020 messages | 0.13.4 | 5.44 s | 7.50 s | 14 | 5.32 s |
| 2,020 messages | this change | 1.41 s | 4.29 s | 0 | 5.26 s |
| 4,950 messages | 0.13.4 | 18.80 s | 20.82 s | 15 | 18.90 s |
| 4,950 messages | this change | 3.84 s | 6.47 s | 0 | 18.96 s |

The first call has nothing to resume from, and the second resumes and then makes the one user-summary request both versions make there.

### Pinned by tests
- A rebuilt list sends what a kept list sends and rebuilds no record's window after the first call, over the five shapes, behind a chat middleware, and when stored messages are handed under new lists of the same or equal items.
- A harness agent run with per-service-call persistence, streaming and not, resumes on every later call and is sent what it is sent with the resume disabled. With it disabled, windows are rebuilt on each.
- A resumed pass writes on no stored message, leaves no stored pair half excluded, and its list carries the marks the call was handed.
- A list that drops, moves or changes a stored message, stops short, or holds another conversation's, is compacted as it is today, and a host's exclusion is kept.
- The call after a recall prompt resumes, as does the call after a list compacted twice, and one object handed as a stored and as a new message is sent as both.
- Ids stay unique and each shed group keeps one note, a count the host dropped is taken again, a refused budget keeps the list, a failed pass keeps none, and nothing is kept or reported below every trigger.
- The registry's bound, its release at the end of a turn and on a snapshot it did not write, and the snapshot being the same with and without a kept list.

### Not done
- **The first call of a run.** The list is released when a turn ends, and a host that rebuilds its transcript for every run hands messages without the flags the last run's passes left on its own. Nothing is carried from one run to the next.
- **The call after the agent stamps a stored message.** **[measured]** in that review: not streaming, the agent sets `author_name` on the stored responses when a run ends, so a host that keeps the list across turns departs on the next turn's first call.
- **The stored-history phase** at the end of a turn still puts every decision back, once per turn.
- **`_insert_note`** remains what the first call of a long run spends most on ([#2736](https://github.com/sokolaidev/maf-extensions/issues/2736)).
