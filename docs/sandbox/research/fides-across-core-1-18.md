# FIDES across agent-framework-core 1.18, measured

> A measurement, not a proposal. [#1074](https://github.com/sokolaidev/maf-extensions/issues/1074) listed five things to check when the four upstream FIDES pull requests reached an installable release, and said to re-measure rather than re-read. 1.18.0 published on 2026-09-10, so this is that release driven beside 1.17.0 and the answers compared. The decided content lives in [`../information-flow.md`](../information-flow.md) and [`../kinds/bicep.md`](../kinds/bicep.md).

## What was run

`agent-framework-core` 1.17.0 and 1.18.0, each in an environment of its own with this branch's packages installed into it, on 2026-09-12. Every row below is one probe driving the real `LabelTrackingFunctionMiddleware` — and, for the policy rows, the real `PolicyEnforcementFunctionMiddleware` — over a hand-built `FunctionInvocationContext`, the shape [`laundering-into-the-file-store.md`](laundering-into-the-file-store.md) established. Both suites were then run whole against both cores.

Nothing here is a live-model run. What a model does with a blocked call, and whether the block changes the shape of an attack rather than removing it, is not measured.

## The four upstream changes, as they arrive

`__all__` is identical, 22 names either side, and no top-level definition was removed. The `context.metadata` contract is additive: `result_label`, `context_label`, `original_arguments_for_messages` and `user_approved_violation` all survive, and `argument_label`, `effective_invocation_label` and `security_original_runtime_kwargs` join them. So nothing this suite imports or reads by name moved, which is what the issue predicted from upstream `main` and is worth confirming against the release rather than the branch.

What did move is behaviour, in five places that matter here.

| | 1.17.0 | 1.18.0 |
|---|---|---|
| `get_current_middleware()` from a `def` body, off the loop | `None` | the middleware, and its store |
| a `[var_id]` inside a `list[str]` argument | contributes no label; result `via=default` | contributes the stored label; result `via=input_labels_join` |
| a tool declaring `source_integrity="trusted"`, `confidentiality="public"`, over a private hidden argument | result trusted/**public** | result trusted/**private** |
| a stored payload naming a `response` | reduced to that field | substituted whole unless `quarantined_llm` stored it |
| that reference passed to a tool declaring nothing, with the policy middleware wired at its defaults | expanded, body runs | `untrusted_arguments`, call blocked |

The variable store is per `AgentSession` from 1.18, and a reference owned by another session is left literal rather than expanded — measured directly, with one middleware instance serving two sessions.

## 1. The accessor answers off the loop now, and the reason a comment gave is gone

`_current_middleware` was a `threading.local` and is a `ContextVar`. `asyncio.to_thread` copies the calling context, so the accessor and the variable store behind it both reach the worker thread the framework dispatches a synchronous tool body to. Measured: `accessor_answers` and `store_reachable` are both false on 1.17 and both true on 1.18, with the body confirming it really ran off the loop.

Two things in this package rested on the old behaviour and neither was a choice that changed. `_MIDDLEWARE_RAN_KEY` is read from the call's metadata rather than from the accessor, and its comment gave the thread-local as the reason; the reason is now one of two, and the choice is *more* defensible for it — a package accepting the whole 1.x range cannot rest on the newer behaviour, so carrying the tell on the context is what makes the guarantee the same on either core. And `test_a_synchronous_body_is_answered_from_the_record_where_the_fallback_gives_up` asserted the fallback answers nothing off the loop, in as many words saying that *"if the framework ever makes its accessor reachable here the contrast is worth revisiting"*. It did, and the test now derives its expectation from the accessor the body actually reached rather than pinning either core's number.

## 2. Session scoping is indifferent to this package, and the suite says so

`argument_provenance_middleware` holds no framework object, publishes the call context and nothing else, so per-session scoping has nothing of ours to scope. That was the reasoning; the measurement is one tracker serving an `alpha` and a `beta` session, with a payload hidden in each and `alpha`'s call naming first its own reference and then `beta`'s.

```
1.18.0   ['PAYLOAD_alpha', 'main.bicep']        # alpha's own, expanded
         ['[var_7e9630fafe384154]', ...]        # beta's, left literal
         ['PAYLOAD_beta', 'main.bicep']         # beta's own, in beta's call

1.17.0   ['PAYLOAD_alpha', ...]                 # one store per middleware instance,
         ['PAYLOAD_beta', ...]                  # so every reference resolves
         ['PAYLOAD_beta', ...]                  # in every session
```

The record reported exactly the entries the framework rewrote in all six calls, on both cores. That is the assertion the suite carries now — derived from what the body received rather than pinned to a number — because *which* references a core expands is the framework's decision and the guarantee is that the answer follows it.

One consequence for a caller worth naming: `hidden_content_candidates` asks the middleware for a store without naming a session, and on 1.18 that resolves through the scope the tracker activated for the duration of the call. Inside a call it is the right session's store. The existing advice to snapshot before the first await is unchanged by this, and gains a second reason.

## 3. The label facts, re-measured

**The tier-3 join now sees a hidden reference.** This suite's arguments are plain strings and a plain string still carries no label — `files=["main.bicep"]` is `via=default` on both cores. But a `[var_id]` in one is no longer invisible: 1.18 keeps the stored label of every reference it expands and joins it with the labels the arguments carried themselves, so the same argument holding a reference gives `via=input_labels_join` and the result takes the payload's confidentiality. That is the shape [#802](https://github.com/sokolaidev/maf-extensions/issues/802) described, closed upstream rather than here, and it does not change what the shipped kinds declare — both declare `untrusted` explicitly, which is a tier-2 declaration that overrides the join either way.

**Tier 2 still overrides the join on integrity, and no longer does on confidentiality.** A tool declaring `source_integrity="trusted"` over an untrusted hidden argument still gets a trusted result, `via=source_integrity`, on both cores. So the derivation rule — a result not based on 100 % trusted input cannot claim `trusted` — is exactly as unenforced by the framework as it was, and [#801](https://github.com/sokolaidev/maf-extensions/issues/801) and [#807](https://github.com/sokolaidev/maf-extensions/issues/807) are as necessary as they were. What 1.18 removed is the other half: a tool declaring `confidentiality="public"` beside that `trusted` used to return public over a private argument, and now returns private. A declaration can no longer declassify what the call was fed.

**A per-item label must now name both axes or it is discarded.** 1.18 parses an embedded `security_label` through a check requiring `integrity` *and* `confidentiality`; a label naming integrity alone raises, the framework logs *"Failed to parse security_label from Content"*, and the item silently falls back to the invocation label — which for an undeclared tool is untrusted, so a would-be trusted item ends up hidden. This suite is unaffected because `_result_label` already refuses to write a label unless the kind declared both, and the committed sentences carry a complete `ContentLabel`. That was [#876](https://github.com/sokolaidev/maf-extensions/pull/876)'s doing for a different reason, and it is the whole of why this change is a non-event here.

**A labelled item also inherits the call's confidentiality now**, rather than replacing the whole label: 1.18 takes the embedded integrity and the *most restrictive* of the two confidentialities. The finding behind [#804](https://github.com/sokolaidev/maf-extensions/issues/804) — that labelling every item drops the call's classification — is fixed upstream from 1.18 and still live below it.

**Hidden items still do not taint the conversation's integrity.** An untrusted result is replaced by a variable reference and the context label stays trusted on both cores. What is new is that the conversation's *confidentiality* does move when a private hidden payload is expanded into an argument, because the result label carries it and the context label is a join.

## 4. The payload reduction moved, and this package mirrors it

`_extract_primary_tool_content` reduced any stored payload naming a `response` to that field. From 1.18 it is applied only where the stored variable's metadata says `quarantined_llm` produced it, and every other payload is substituted whole. `maf._reduced_form` mirrors that rule for `positions_holding_hidden_content`'s fallback, so the mirror is now right on one core and wrong on the other.

The suite caught it, which is what it was built to do: eight tests went red on 1.18, and the six parametrised over payload shapes printed the sentence the alarm was written to print — *"the framework's payload reduction has changed — `maf._reduced_form` mirrors it and must be updated to match, or an argument carrying this shape is not reported"*.

Mirroring the new rule instead would only move the failure to the older core, which the declared range still admits. So the candidate set offers **both** forms — the payload and its reduction — and the alarm asserts the safety property rather than one core's answer: whatever the framework substitutes is one of the forms offered, and a third shape fails loudly.

The under-report this closes is narrow and real. Containment usually rescues the reduced form, because `str()` of a mapping embeds the repr of its values — but not when the value needs escaping:

```
stored          {'response': 'a\'b"c.bicep'}
1.18 delivers   {'response': 'a\'b"c.bicep'}.bicep
candidate       a'b"c.bicep                      # the reduction alone
contained       False
```

So a kind about to render a refusal was told nothing was rewritten and would have quoted that value back. A row for it is in the suite.

## 5. What the new metadata keys do and do not answer

`argument_label` and `effective_invocation_label` are the closest upstream has come to publishing per-invocation provenance, and [#826](https://github.com/sokolaidev/maf-extensions/issues/826) asks for the mirrored payload reduction to be replaced by something the framework publishes. They do not retire it. Both are `ContentLabel`s: they say what the arguments *carry*, never which positions were rewritten, which is the question a refusal has to answer before it quotes one. They are also bare `context.metadata` string keys with exactly the unpublished status `original_arguments_for_messages` already has, so reading them would add a second key to keep alive rather than replace the first. #826 stays open, and the combination does not change its shape.

What *does* narrow the problem is the policy middleware. From 1.18 the tracker publishes `argument_label` and the policy middleware raises `untrusted_arguments` on any call whose arguments resolve to untrusted content, unless the tool is named in `allow_untrusted_tools` or declares `accepts_untrusted`, in which case no violation is raised at all. Upstream's own model-facing instructions now say so in as many words — *"Forwarding is allowed only when the destination tool declares `accepts_untrusted=True`; otherwise the call is blocked, audited, or sent for policy approval."*

**Measured in the default configuration, which is one of three outcomes and the only one that refuses.** The same call that ran to completion on 1.17, with the expanded payload in the body's hands, terminates on 1.18 with the body never entered and one `untrusted_arguments` entry in the audit log. The other two are read from the source rather than measured, and neither refuses the call outright. With `approval_on_violation=True` the *first* attempt raises `MiddlewareTermination("Policy approval required")`, so nothing runs until a user answers; an approved retry then falls through to `call_next` with the payload, and a denied one re-requests and terminates again — so it is a held call rather than a served one, and what serves it is the approval. With `block_on_violation=False` and no approval mode the violation is logged and the call proceeds unconditionally. So wiring the middleware is not on its own a refusal — leaving its defaults alone is.

Neither shipped kind opts in, and neither should: opting in is what would reopen the channel. So on 1.18 with both middleware wired *and its defaults left alone*, the chain in [`laundering-into-the-file-store.md`](laundering-into-the-file-store.md) is refused at the step where the model forwards the reference. What that leaves untouched is every other configuration, a host wiring the tracker alone, and every core below 1.18 with both wired. All of them are inside the range this suite accepts, so nothing retires.

## What this cost, and what it did not

Nine tests in `maf-sandbox` went red against 1.18 — eight on the substitution, one on the accessor — and every other package's suite was green, both kinds' FIDES tests included. All nine were the alarms doing their job rather than defects reaching an adopter. The one defect is the escaped-payload case above, which no test covered and which this record's own probe found; it is fixed, with a row of its own.

Not measured, and named rather than assumed: **the two non-default policy configurations above** — an approved retry under `approval_on_violation=True`, and a logged-and-served call under `block_on_violation=False`. Both are read from the source, and both are why the block above is qualified to the default everywhere it is stated. With them, [#8142](https://github.com/microsoft/agent-framework/pull/8142)'s approval binding, which nothing here touches — no module in this repository reads `user_approved_violation`, requests an approval, or constructs a `PolicyEnforcementFunctionMiddleware` outside a test. The durable-state encoding a session-backed scope applies to stored payloads is exercised only through the in-memory path, since no probe here serialized a session. And nothing was run against a live model.
