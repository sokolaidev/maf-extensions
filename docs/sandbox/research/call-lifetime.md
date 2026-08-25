# The tool call: what owns it, what it owns, and what happens when it ends

> The proposal that asked what a tool call is, what it owns, and what has to happen when it ends. It is kept in the tense it was written, as the record of the argument rather than a description of what shipped. The decided content now lives in [`../tool-call.md`](../tool-call.md), and in [`../hosts.md`](../hosts.md) for where the storage base comes from.

## The question

Several open problems turn out to be one: how a kind cleans up after itself, which layer knows how to delete, what happens when two calls share a sandbox, and who decides where a workload's files go. None can be answered without first answering **what a tool call is, what it owns, and how long anything lives.**

The problem in one sentence:

> **A sandbox lives for the conversation, what a call writes should live for the call, and nothing bridges the two.**

`acquire` is get-or-create, so everything a kind writes survives the whole conversation and is readable by every later call in the same sandbox. Both shipped kinds make a fresh per-call directory and leave it there. Nothing obliges them to clean up.

## Four lifetimes

```
binding    one per tool               process           SandboxToolBinding
  └ call   one per tool call          the call          ← owns its own guest path
      └ run   0..1, transport only    inside a call     GuestRunLayout, reclaim_run

sandbox    one per (scope, thread_id, agent_dir, kind)   conversation
```

The nesting is real; the sandbox is **not its root**. It sits beside the chain:

- A binding is built once, before any key exists, and reads scope and thread per call — so **one binding reaches as many sandboxes as it serves conversations.**
- A workload shipping two tools builds two bindings against one sandbox.

Neither is "per" the other. They get confused because they coincide in the single-tool, single-conversation case every test exercises.

## Vocabulary

**`call`** — one execution of the tool function. The framework's unit, and the boundary a `finally` sits on. Qualify anything else: `transport call`, `backend call`, `control-plane call`.

**`run`** — the transport's unit: one supervised guest program with a `GuestRunLayout`. Published API, so it is not moving. codeact states the relationship exactly: *"A fresh run per call."*

**Not `invocation`** — already spent on an external `docker` or `wslc` subprocess.

The concept with no word is the framework's own place in the guest, and both kinds invented one: codeact's `run_dir`, used even on the path where no transport run exists, and bicep's `round_dir`. Neither is the framework's, because the framework never had this concept.

**It will be `guest_call_path()`**, and each word carries something:

- **`call`**, not `run` — bicep runs several guest commands in one place and has no run at all. "One per call" holds for both kinds; "one per run" describes neither.
- **`guest`** — a bare name reads as a path on the machine the agent runs on, the one place it is not. This repository already spends `guest` on that distinction and leads with it: `guest_path`, `guest_path_relative_to`, `guest_run_layout`, `confine_guest_path`.
- **`path`**, not `directory` — a path is all the protocol promises. `Sandbox.remove`'s contract is *"`path`, and everything under it"*, and `FILES_LIST` is a separate capability precisely because enumeration is not universal. A backend serving its store from memory addresses a place the same way.

Not `guest_call_prefix`: a prefix invites `f"{prefix}name"` with no separator.

### Where each term may live

| term | layer | visible as |
| --- | --- | --- |
| `run` | `_host_tools_over_exec.py`, stdlib-only core | public: `GuestRunLayout`, `reclaim_run` |
| `binding`, `call` | `maf.py`, the MAF glue | by name only: `from maf_sandbox.maf import ...` |
| — | `_protocol.py` | **nothing** |

`maf.py` is the one module that may import `agent_framework`, kept out of `__init__` with two tests enforcing it. So `call` cannot become a protocol term without breaking the layering — and the protocol holding none of this vocabulary is not an oversight. There is no protocol for kinds, and nothing here invents one.

## What the binding may hold

Every field the binding carries is host configuration: the router, the caller context, the agent directory, the spec, the name, the logger, the sink. Not one is derived from a caller. The context is the tell — it takes *callables* rather than a scope and a thread, so nothing caller-shaped is ever stored, because a captured scope would let one conversation reach another's sandbox on a host that builds an agent once and serves many.

That has never been written down, and it generalises past the case it was built for:

> **The binding holds host configuration only. Anything derived from a caller — a scope, a thread, a path generated for one call — lives on the call, or nowhere.**

The distinction is finer than per-tool versus per-call. The binding **already gives per-call answers**: the key, the file listing and the acquired sandbox differ every call. What it has never done is *store* one — each is **read** from the host's context when asked. `guest_call_path()` will be the first that must be generated and then remembered, which is why it needs a home of its own.

It is called `SandboxToolSession` today, and that name is what invites the mistake: nothing about the object is temporal. Renaming it to `SandboxToolBinding` is mechanical — eight signatures across codeact, bicep and `samples/07` — and belongs on its own, after the semantics settle, so a reviewer is not reading a rename on top of them.

## The call, and what it owns

The binding is shared by every concurrent call, so per-call state cannot be an instance attribute. Put the path there and two parallel calls receive the same one: the second writes its program over the first's, shares its files beside the first's inputs, finishes first, and reclaims a place the first is still executing in. A cleanup contract that introduced a worse leak than the one it closes.

A `ContextVar` bound by the wrapper is the only object whose lifetime is the call:

```python
_CALL: ContextVar[_SandboxToolCall | None] = ContextVar("maf_sandbox_call", default=None)

@dataclass
class _SandboxToolCall:
    """What one tool call has done that the `finally` has to undo."""
    owner: object                   # the binding whose wrapper opened this call
    name: str | None = None         # set by the first guest_call_path()
    acquired: dict[SandboxKey, Sandbox] = ...   # every sandbox this call reached
    closed: bool = False            # set before the removal walks `acquired`
```

Four fields rather than one path, and each answers a way the record was wrong about *which*
sandbox owns a call's files: `owner`, because one `ContextVar` serves every binding in the
process; `acquired` as a mapping, because `acquire` takes a key and a call can reach two;
`closed`, because a task the body left running still holds the record after the removal has
run. Only `name` is the thing a kind asked for.

Correct under asyncio: each task starts from a copy of its parent's context, so a body that spawns children has them *read* the right record and nothing a child sets reaches a sibling.

- **It is mutable, and the only mutable thing in the chain** — because its lifetime is one call. The binding is immutable because its lifetime is the process. One rule, two objects.
- **It is private.** A kind calls `binding.guest_call_path()`; the record answers.
- **Asking outside a call raises**, and so does asking after one returned. There is no call, or there is no longer one: returning a path nothing will reclaim is the leak wearing the API meant to close it.

Both alternatives are closed rather than unattractive. Passing a per-call object into the body would put it in the tool's JSON schema, because the body's signature *is* the schema. Rebuilding the binding per call contradicts the guarantee that `build` runs once, when a tool is attached.

## Where the base comes from

A guest path is relative to something, and today that something is owned by nobody. A workload declares `work_dir` in its spec, no backend reads it, no backend creates it, and the protocol does not promise it exists. Every kind then composes absolute paths from a base the stack only hopes is there.

The target rule:

> **A backend allocates the storage base and resolves every path against it. Kinds address everything relative to that base and never compose an absolute path.**

It is the only shape that serves a backend whose store has no filesystem under it: such a backend cannot honour a POSIX base a workload invented, but it can resolve a relative name however its store works. It also matches the surface as it already stands — four of the five `Sandbox` methods take a `working_directory` and a path relative to it.

**The override is first-class, not an escape hatch.** A kind shipping an image that pre-populates the base needs the base to be exactly where the image put it: bicep's `bicepconfig.json` is copied to a fixed root and found only by walking *up* from the file being compiled. So the override belongs on the spec, beside the image it travels with — a base of `None` means the backend chooses, a value means *this image requires this exact base*.

Two things it depends on, both real work:

- **`write_file` now takes a working directory and participates in confinement** — the surface asymmetry is closed, so every path-taking method refuses one that escapes. What has not moved is the base those paths are relative to: it is still owned by nobody, as above.
- **Something still needs a real absolute path inside the guest.** The transport puts the shim's directory on `PYTHONPATH`, which the interpreter resolves against nothing. Either the launcher works relative to its own working directory, or the protocol gains a resolution step. This is the open question.

`guest_call_path()` is correct either way: a path relative to a base is still a path, so nothing renames when the base moves.

## Cleanup, as a consequence

With the call owning a path, reclamation is the framework's: it removes what the call owns in a `finally`, and a kind cannot forget a path it never held. **What it removes with will not be core's choice.**

The capability gates **confinement, not deletion**. A backend's `remove` is three confinement checks and one delete; the backend that withholds the capability withholds it for want of the parent walk, not the shell. A path *this stack created*, under a base, with an unguessable name, has no attacker-chosen component to walk — so the method that removes it needs no confinement and can be mandatory on every backend. And it must be the backend's, because core can only dispatch to mechanisms core can name: a backend offering a language runtime and no shell deletes through that runtime, which no capability check reaches.

Escalation when it fails is disposal, because no removal is a guarantee — a stop reaches a process group at most, and anything outliving one can write a path back. Disposal is the host's call to loosen, the same way the isolation floor is: a workload may ask for stricter, never for weaker.

## Concurrency, as the other consequence

A per-call path is tidiness, not isolation. Two calls in one conversation share one sandbox and one store; a call holding `EXEC` can read whatever its sibling is writing. Same user and same conversation, so not a tenancy breach — but the disposal above would kill a sibling mid-run.

The answer is a **lifetime or a lock**, never a capability:

- a lock keyed by sandbox identity, serialising calls that share one — honest only about in-process concurrency, since two replicas serving one conversation hold two locks;
- or a sandbox per call, which removes the sharing rather than scheduling around it, at a cold start per call.

## Rejected

**A ledger of calls on the binding, or a central one.** The binding is process-global across tenants, so it grows forever, and its entries cannot be interpreted without a key they do not carry — store the key beside each and it is per-sandbox state in a per-binding container. Worse, it would be safe only because the sandbox key isolates tenants two layers away, undocumented, so the next person to add "retry the leftover next call" turns it unsafe without touching what made it safe. If one is ever wanted, it is `dict[SandboxKey, set[str]]` on the router, cleared with the scope.

**Per-call isolation as a `Capability`.** It is a boundary property, and the two axes are kept apart deliberately: merged into `requires`, a workload could ask for a weaker boundary than the deployment mandates with the host's floor having no say. It is not a capability in the ordinary sense either — there is nothing for a backend to implement, since a per-call component in the key makes every backend produce a fresh sandbox with no code change.

**Disposing the sandbox after every call, by default.** Correct and expensive, and it takes a decision the host owns — fourteen of the fifteen samples already dispose the scope in a `finally`. As an opt-in for workloads needing the guarantee, it is the per-call sandbox above.

**Core dispatching cleanup on declared capabilities.** It can only choose between mechanisms core can name, and leaves a third case — a backend with neither — no answer at all.

## Rollout

1. **The call owns a path.** `guest_call_path()`, the per-call record, the wrapper and its `finally`, the failure callback, and the invariant above in the binding's docstring. Core alone; the three call sites — codeact's two paths and bicep's per-round directory — are wired after.
2. **A mandatory reclaim method on `Sandbox`.** Every backend implements it, core stops choosing. Breaking: the protocol is `@runtime_checkable`.
3. **Serialising calls that share a sandbox**, which unblocks the disposal escalation.
4. **The backend owns the base.** Answers the `PYTHONPATH` question above; the `write_file` asymmetry is closed.
5. **`SandboxToolBinding`**, once the semantics have settled.

Two pieces belong in code rather than here, where someone will trip over them: the vocabulary rule in `AGENTS.md`, so the next kind does not invent a fourth word, and the binding invariant in its class docstring, where the mistake it prevents is made.
