# The tool call: what owns it, what it owns, and what happens when it ends

> The lifetime model of one sandboxed tool call — binding, call and run — what each owns, and what is reclaimed when the call ends. It is universal to every kind, not a CodeAct feature. Its source of record is [`research/call-lifetime.md`](research/call-lifetime.md); its siblings are [`architecture.md`](architecture.md), the structure a call lives in, and [`hosts.md`](hosts.md), the outward direction where a guest dispatches host tools.

Several problems turn out to be one: how a kind cleans up after itself, which layer knows how to delete, what happens when two calls share a sandbox, and who decides where a workload's files go. None can be answered without first answering what a tool call is, what it owns, and how long anything lives. In one sentence:

> **A sandbox lives for the conversation, what a call writes should live for the call, and nothing bridges the two.**

`acquire` is get-or-create, so anything a kind writes survives the whole conversation and is readable by every later call in the same sandbox.

## Four lifetimes

```
binding    one per tool               process           SandboxToolBinding
  └ call   one per tool call          the call          ← owns its own guest path
      └ run   0..1, dispatch only     inside a call     GuestRunLayout, reclaim_run

sandbox    one per (scope, thread_id, agent_dir, kind)   conversation
```

The nesting is real; **the sandbox is not its root.** It sits beside the chain. A binding is built once, before any key exists, and reads scope and thread per call — so one binding reaches as many sandboxes as it serves conversations; and a workload shipping two tools builds two bindings against one sandbox. Neither is "per" the other. They get confused because they coincide in the single-tool, single-conversation case every test exercises.

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

**What it removes with is not core's choice.** The capability gates **confinement, not deletion**: a backend's `remove` is three confinement checks and one delete, and a backend withholds it for want of the parent walk, not the shell. A path *this stack created*, under a base, with an unguessable name, has no attacker-chosen component to walk — so the method that removes it needs no confinement and can be mandatory on every backend. And it must be the backend's, because core can only dispatch to mechanisms core can name: a backend offering a language runtime and no shell deletes through that runtime, which no capability check reaches. Until that method exists, core removes with `rm -rf` over `EXEC` — the one removal every backend serving a kind can do today, since the protocol's own delete is gated by a capability no shipped spec requires — guarded by confinement plus two independent depth guards, because a recursive delete is irreversible and `/` and `/tmp` are the shapes that turn a cleanup into an outage. No failure of it raises: it runs in a `finally`, where an exception would replace whatever the call was already reporting. Cancellation is not such a failure and propagates — containing it would let the call return past a bound the host thought it had. What did not get removed reaches the host as a `ReclaimFailure`, a data-retention failure rather than a tidiness one.

Escalation when removal fails is disposal, because no removal is a guarantee — a stop reaches a process group at most, and anything outliving one can write a path back. Disposal is the host's call to loosen, the same way the isolation floor is: a workload may ask for stricter, never for weaker.

## Concurrency, as the other consequence

A per-call path is tidiness, not isolation. Two calls in one conversation share one sandbox and one store; a call holding `EXEC` can read whatever its sibling is writing. Same user and same conversation, so not a tenancy breach — but the disposal escalation above would kill a sibling mid-run. The answer is a **lifetime or a lock, never a capability**: a lock keyed by sandbox identity, serialising calls that share one — honest only about in-process concurrency, since two replicas serving one conversation hold two locks; or a sandbox per call, which removes the sharing rather than scheduling around it, at a cold start per call. Per-call isolation is not a capability in either sense — it is a boundary property, and there is nothing for a backend to implement, since a per-call component in the key makes every backend produce a fresh sandbox with no code change.

## Where the base comes from

A guest path is relative to something, and today that something is owned by nobody: a workload declares `work_dir` in its spec, no backend reads it, no backend creates it, and the protocol does not promise it exists. The decided rule is that **a backend allocates the storage base and resolves every path against it**, with a spec-level override for an image that pre-populates a fixed root — the only shape that also serves a backend whose store has no filesystem under it. [`hosts.md`](hosts.md) owns the question and what a host configures today.

## Status

| Decision | State | Tracking |
|---|---|---|
| The call owns a guest path; the per-call record, and the `finally` that reclaims it | shipped | [#496](https://github.com/sokolaidev/maf-extensions/pull/496), kinds wired in [#500](https://github.com/sokolaidev/maf-extensions/pull/500), sample 15 aligned in [#499](https://github.com/sokolaidev/maf-extensions/pull/499) |
| A host learns of a reclaim that did not happen | partial — `on_reclaim_failure` ships; no sample wires it, so the one call-lifetime decision a host must make has no worked example | [#520](https://github.com/sokolaidev/maf-extensions/issues/520) |
| A mandatory reclaim method on `Sandbox`; core stops choosing the mechanism | open — core removes with `rm -rf` over `EXEC` in `_reclaim.py` | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) |
| Calls that share a sandbox are serialised, which unblocks the disposal escalation | open — nothing serialises them today | [#476](https://github.com/sokolaidev/maf-extensions/issues/476) |
| A backend allocates the storage base and resolves every path against it | open — a spec names `work_dir`, no backend reads or creates it | [#480](https://github.com/sokolaidev/maf-extensions/issues/480) |
| `SandboxToolSession` is renamed `SandboxToolBinding`, once the semantics settle | open | untracked |
