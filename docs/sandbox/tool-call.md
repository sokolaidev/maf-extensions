# The tool call: what owns it, what it owns, and what happens when it ends

> The lifetime model of one sandboxed tool call — binding, call and run — what each owns, and what is reclaimed when the call ends. It is universal to every kind, not a CodeAct feature. Its source of record is [`research/call-lifetime.md`](research/call-lifetime.md); its siblings are [`architecture.md`](architecture.md), the structure a call lives in, and [`hosts.md`](hosts.md), the outward direction where a guest calls host tools.

Several problems turn out to be one: how a kind cleans up after itself, which layer knows how to delete, what happens when two calls share a sandbox, and who decides where a workload's files go. None can be answered without first answering what a tool call is, what it owns, and how long anything lives. In one sentence:

> **A sandbox lives for the conversation, what a call writes should live for the call, and nothing bridges the two.**

`acquire` is get-or-create, so anything a kind writes survives the whole conversation and is readable by every later call in the same sandbox.

## Four lifetimes

```
binding    one per tool               process           SandboxToolBinding
  └ call   one per tool call          the call          ← owns its own guest path
      └ run   0..1, transport only    inside a call     GuestRunLayout, reclaim_run

sandbox    one per (scope, thread_id, agent_dir, kind)   conversation
```

The nesting is real; **the sandbox is not its root.** It sits beside the chain. A binding is built once, before any key exists, and reads scope and thread per call — so one binding reaches as many sandboxes as it serves conversations; and a workload shipping two tools builds two bindings against one sandbox. Neither is "per" the other. They get confused because they coincide in the single-tool, single-conversation case every test exercises.

Four lines is the terse form; the bars below put the same four lifetimes on one time axis, where the coincidence that confuses them comes apart.

![Four lifetimes drawn as bars on one left-to-right time axis. The binding is the topmost bar and spans the whole axis — one per tool, built once before any key exists, holding host configuration only and reading scope and thread per call — while two teal sandbox bars with different spans sit beside it rather than under it, one per conversation, keyed by scope, thread, agent dir and kind. Inside the first conversation's span three amber call bars run in sequence over the one warm sandbox, each ending in a tick, the finally that removes the guest path that call owns; exactly one of them carries a thinner, sharp-cornered run bar — 0..1 per call, host-tools transport only — and the other two carry none. The second conversation carries a call of its own, because one binding reaches as many sandboxes as it serves conversations. Both sandbox bars outlive every call in them and end only with the conversation, where disposal is best-effort and purge asks every registered backend.](assets/four-lifetimes.svg)

## The words

**`call`** — one execution of the tool function. The framework's unit, and the boundary a `finally` sits on. Anything else is qualified: `transport call`, `backend call`, `control-plane call`. **`run`** — the transport's unit: one supervised guest program with a `GuestRunLayout`. Published API, so it is not moving; codeact states the relationship exactly, *"a fresh run per call."* Not **`invocation`**, which is already spent on an external `docker` or `wslc` subprocess.

The concept with no word was the framework's own place in the guest, and both kinds invented one — codeact's `run_dir`, used even where no transport run exists, and bicep's `round_dir`. It is **`guest_call_path()`**, and each word carries something:

- **`call`**, not `run` — bicep runs several guest commands in one place and has no run at all. "One per call" holds for both kinds; "one per run" describes neither.
- **`guest`** — a bare name reads as a path on the machine the agent runs on, the one place it is not. This repository already spends `guest` on that distinction: `guest_path_relative_to`, `guest_run_layout`, `confine_guest_path`.
- **`path`**, not `directory` — a path is all the protocol promises. `Sandbox.remove`'s contract is *"`path`, and everything under it"*, and `FILES_LIST` is a separate capability precisely because enumeration is not universal. A backend serving its store from memory addresses a place the same way.

Not `guest_call_prefix`: a prefix invites `f"{prefix}name"` with no separator.

| Term | Layer | Visible as |
| --- | --- | --- |
| `run` | `_host_tools_over_exec.py`, stdlib-only core | public: `GuestRunLayout`, `reclaim_run` |
| `binding`, `call` | `maf.py`, the MAF glue | by name only: `from maf_sandbox.maf import ...` |
| — | `_protocol.py` | **nothing** |

`maf.py` is the one module that may import `agent_framework`, kept out of `__init__` by two tests. So `call` cannot become a protocol term without breaking the layering — and the protocol holding none of this vocabulary is not an oversight. There is no protocol for kinds, and nothing here invents one.

## What the binding holds, and what the call owns

Every field the binding carries is host configuration: the router, the caller context, the agent directory, the spec, the name, the logger, the sink. Not one is derived from a caller. The context is the tell — it takes *callables* rather than a scope and a thread, so nothing caller-shaped is ever stored. Generalised past the case it was built for:

> **The binding holds host configuration only. Anything derived from a caller — a scope, a thread, a path generated for one call — lives on the call, or nowhere.**

The distinction is finer than per-tool versus per-call. The binding **already gives per-call answers**: the key, the file listing and the acquired sandbox differ every call. What it never does is *store* one — each is **read** from the host's context when asked. `guest_call_path()` is the first value that must be generated and then remembered, which is why it needs a home of its own.

The binding is shared by every concurrent call, so per-call state cannot be an instance attribute. Put the path there and two parallel calls receive the same one: the second writes its program over the first's, finishes first, and reclaims a place the first is still executing in — a cleanup contract that introduces a worse leak than the one it closes. A `ContextVar` bound by the wrapper is the only object whose lifetime is the call. It carries four fields rather than one path, each answering a way the record was wrong about *which* sandbox owns a call's files: `owner`, because one `ContextVar` serves every binding in the process; `name`, the only field a kind asked for; `acquired` as a **mapping**, because `acquire` takes a key and one call can reach two sandboxes; and `closed`, because a task the body left running still holds the record after the removal has run. It is correct under asyncio — each task starts from a copy of its parent's context, so a body that spawns children has them *read* the right record and nothing a child sets reaches a sibling.

- **It is mutable, and the only mutable thing in the chain**, because its lifetime is one call. The binding is immutable because its lifetime is the process. One rule, two objects.
- **It is private.** A kind calls `binding.guest_call_path()`; the record answers.
- **Asking outside a call raises**, and so does asking after one returned. There is no call, or there is no longer one: returning a path nothing will reclaim is the leak wearing the API meant to close it. It raises rather than returning a message, unlike the binding's three accessors, because reaching it wrongly is a wiring mistake in a kind, not something a model can cause or should be told about.

Both alternatives are closed rather than unattractive. Passing a per-call object into the body would put it in the tool's JSON schema, because the body's signature *is* the schema. Rebuilding the binding per call contradicts the guarantee that `build` runs once, when a tool is attached.

## Cleanup, as a consequence

With the call owning a path, reclamation is the framework's: it removes what the call owns in a `finally`, after a result, a refusal and an exception alike, so a kind cannot forget a path it never held. A body that never asked for a path costs nothing, and a synchronous body is not held to the rule at all — it cannot `await acquire`, so it holds no sandbox and owns nothing. A spec whose `work_dir` is the guest root is refused for an async body, because a path one component from the root is one the removal will not perform.

**What it removes with is not core's choice.** The capability gates **confinement, not deletion**: a backend's `remove` is three confinement checks and one delete, and a backend withholds it for want of the parent walk, not the shell. A path *this stack created*, under a base, with an unguessable name, has no attacker-chosen component to walk — so the method that removes it needs no confinement and can be mandatory on every backend. And it must be the backend's, because core can only dispatch to mechanisms core can name: a backend offering a language runtime and no shell deletes through that runtime, which no capability check reaches. That method is `Sandbox.reclaim(directory, *, working_directory, timeout)`, mandatory on every backend and behind no `Capability` at all. It is not core picking one delete over another: `remove` is gated by `FILES_DELETE`, no shipped spec requires that capability, and calling it from a `finally` would risk a `NotImplementedError` over a result the call had already produced — so the framework's cleanup was never in a position to reach it. What the method changes is that the one mechanism core can reach is **dispatched** rather than spelled as a shell line. `timeout` travels with the call because the deadline over a `finally` is the framework's — 30 seconds by default, and 2 when the call was cancelled, which no backend could have derived for itself. Core keeps the policy: the path is confined, the working directory itself is refused, and so is anything fewer than two components from the root, because a recursive delete is irreversible and `/` and `/tmp` are the shapes that turn a cleanup into an outage. A backend's `reclaim` is the mechanism, never the policy. No failure of it raises: it runs in a `finally`, where an exception would replace whatever the call was already reporting. Cancellation is not such a failure and propagates — containing it would let the call return past a bound the host thought it had. What did not get removed reaches the host as a `ReclaimFailure`, a data-retention failure rather than a tidiness one.

Three rules a caller depends on, and the first is what pays for the absent walk:

- **The caller created the directory** — under `working_directory`, with an unguessable name. Statable, not enforceable: a backend takes it on the contract, and that is the whole of what lets the method skip confinement.
- **A path that is not there is success.** Cleanup runs in a `finally` and must not report a second failure over the first.
- **Anything else raises**, so the caller can escalate.

**It does not replace `remove`.** `remove` takes a path a model named, so it owes the parent walk and stays gated by `FILES_DELETE`; `reclaim` takes a directory this stack created and owes none, which is why it can be mandatory where `remove` could never be. A backend that must refuse one can serve the other honestly, and [`backends/wslc.md`](backends/wslc.md) is that backend.

**When the framework cannot clean a call, it disposes that call's sandbox.** No removal is a guarantee: a stop reaches a process group at most, and anything outliving one can write a path back after the removal returns. A sandbox the framework could not clean is not left warm for the next call to reuse. It is disposed — `router.dispose(key)` from the same `finally`, bounded the way the reclaim is — and the conversation's next call starts cold. **Better a failed run than leaked data.**

"Cannot clean" is two cases and one rule. A reclaim that reported failure. And a program that had to be stopped and whose stop did not reach its whole process group — a `SignalReach` of `"program"` or `"nothing"` — because what it spawned is still running, and a survivor can write a path back. A run that finished on its own needed no stop, so it is not this case.

**Loosening is the host's, on the router, and explicit.** The same shape as the isolation floor: one keyword beside `min_isolation`, the strict posture when nothing is configured, nothing to forget. A kind cannot lower it — a kind never sees the router's construction, and `sandboxed_tool` carries no switch. A workload may ask for stricter, never for weaker.

**`on_reclaim_failure` tells the host what happened.** It runs after the disposal and says whether the disposal landed, so the guarantee never waits on a host callback's time budget. It is where a host logs, alerts or counts. It is not where safety is wired.

**A disposal that itself fails is logged loudly, and the router refuses that key** until a disposal lands — the ledger on the router that the record said to build if one were ever wanted. A sandbox that holds data nobody can remove is not served again.

**A backend says the disposal failed by returning, not by raising.** `dispose` is contractually best-effort and never raises — which is what makes it safe in a `finally`, and why reading only the raise left the refusal above unreachable: every compliant backend swallowed its delete error and was read as having disposed. It returns a `DisposalFailure` when a sandbox may still be there, or `None`, and `dispose_scope` carries the same answer beside its count as a `ScopePurge`.

**A code and a detail, not a sentence.** `DisposalCode` is a closed set — `unreachable`, `timeout`, `refused`, `unlisted`, `unknown` — so a caller deciding whether to retry, alert or escalate branches on a value this package keeps stable rather than on a backend's prose. `unknown` is always available, so no backend has to invent a more precise code to stay in the vocabulary, and several failures fold to the most actionable with every detail kept. `None` also covers a backend with no way to check: the conflation is with success, because refusing every key served by a backend that cannot answer is the wrong direction to fail in.

**The refusal carries the code, not the detail.** A host branches on `SandboxUnclean.code` — the value, not a substring of the message. The detail stays in the router's log, because a backend's sentence can carry an endpoint or a raw response body and `acquire` reaches hosts that do not sanitize. The model reads a fixed line naming no backend, id or endpoint.
## Concurrency, as the other consequence

A per-call path is tidiness, not isolation. Two calls in one conversation share one sandbox and one store; a call holding `EXEC` can read whatever its sibling is writing. Same user and same conversation, so not a tenancy breach — and the disposal above kills a sibling mid-run. That is the accepted cost: a killed sibling is a failed run, and a failed run beats leaked data. Serialising the calls makes the sibling wait instead of die; it narrows what the escalation costs and is not what permits it. The answer is a **lifetime or a lock, never a capability**: a lock keyed by sandbox identity, serialising calls that share one — honest only about in-process concurrency, since two replicas serving one conversation hold two locks; or a sandbox per call, which removes the sharing rather than scheduling around it, at a cold start per call. Per-call isolation is not a capability in either sense — it is a boundary property, and there is nothing for a backend to implement, since a per-call component in the key makes every backend produce a fresh sandbox with no code change.

## Where the base comes from

A guest path is relative to something, and today that something is owned by nobody: a workload declares `work_dir` in its spec, no backend reads it, no backend creates it, and the protocol does not promise it exists. The decided rule is that **a backend allocates the storage base and resolves every path against it**, with a spec-level override for an image that pre-populates a fixed root — the only shape that also serves a backend whose store has no filesystem under it. [`hosts.md`](hosts.md) owns the question and what a host configures today.

## Status

| Decision | State | Tracking |
|---|---|---|
| The call owns a guest path; the per-call record, and the `finally` that reclaims it | shipped | [#496](https://github.com/sokolaidev/maf-extensions/pull/496), kinds wired in [#500](https://github.com/sokolaidev/maf-extensions/pull/500), sample 15 aligned in [#499](https://github.com/sokolaidev/maf-extensions/pull/499) |
| A host learns of a reclaim that did not happen | partial — `on_reclaim_failure` ships and no sample wires one, deliberately: it is a notification rather than a veto, since the body has returned by the time it runs and a handler that raises is contained. A kind prevents the failure instead (sample 07), and what a host does with the notification — count it, page when `disposal` is `"failed"` and the router is refusing the conversation — is operations code rather than a wiring a sample can show. Two gaps behind that: neither packaged kind passes the callback through, so a host on `codeact` or `bicep` cannot wire one at all; and nothing in-tree can stage a removal that fails, which waits on what [#680](https://github.com/sokolaidev/maf-extensions/issues/680) decides | [#520](https://github.com/sokolaidev/maf-extensions/issues/520), the pass-through in [#677](https://github.com/sokolaidev/maf-extensions/issues/677) |
| A call's directory can be removed by whoever will be asked to remove it | open — `write_file` creates it as the backend's writer while the removal runs as the guest's `exec` user, so on a non-root image it can never be reclaimed. A kind can settle it for itself by making the directory from the guest first, which is what sample 07 does; the protocol says nothing about ownership and no backend promises anything | [#680](https://github.com/sokolaidev/maf-extensions/issues/680) |
| The framework disposes a sandbox it could not clean — a failed reclaim, or a stop that did not reach the process group — and the host loosens that on the router | shipped — `sandboxed_tool` disposes from its `finally` before `on_reclaim_failure` runs, `SandboxRouter(keep_unclean=True)` is the opt-down, and a disposal that does not land makes the router refuse the key with `SandboxUnclean` | [#617](https://github.com/sokolaidev/maf-extensions/issues/617) |
| A backend reports a delete that failed without raising, so the refusal fires on a silent failure | shipped — `dispose` returns a `DisposalFailure` (a `DisposalCode` to branch on, a detail that stays in the log) and `dispose_scope` a `ScopePurge`; the router reads both beside the raise, keeps a conversation's keys refused when its purge did not land, and quotes the code back in `SandboxUnclean`; all four in-tree backends report | [#641](https://github.com/sokolaidev/maf-extensions/issues/641) |
| A mandatory, un-gated `Sandbox.reclaim`; core dispatches the removal rather than spelling it | shipped — `_reclaim.py` and `_host_tools_over_exec.py` both dispatch, and all four in-tree backends implement the member | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) |
| Calls that share a sandbox are serialised, so a disposal finds no sibling to kill | open — nothing serialises them today; this narrows what the escalation costs and does not gate it | [#476](https://github.com/sokolaidev/maf-extensions/issues/476) |
| A backend allocates the storage base and resolves every path against it | open — a spec names `work_dir`, no backend reads or creates it | [#480](https://github.com/sokolaidev/maf-extensions/issues/480) |
| `SandboxToolSession` is renamed `SandboxToolBinding`, once the semantics settle | open | untracked |
