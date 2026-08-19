# The tool call: what owns it, what it owns, and what happens when it ends

> **Status: PROPOSED** — tracking issue [#438](https://github.com/sokolaidev/maf-extensions/issues/438). Two consequences are split out and tracked separately: [#477](https://github.com/sokolaidev/maf-extensions/issues/477) (a mandatory `Sandbox.reclaim`) and [#476](https://github.com/sokolaidev/maf-extensions/issues/476) (serialising calls that share a sandbox). The isolation and capability axes this leans on are [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md); the baseline it evolves is [`sandbox-architecture.md`](sandbox-architecture.md).

## Why this document exists

Three issues now share one set of decisions and none of them owns it. #438 asks how a kind cleans up after itself, #477 asks which layer knows how to delete, #476 asks what happens when two calls share a sandbox — and all three turn on the same question, which nothing in the repository currently answers: **what is a tool call, what does it own, and how long does anything live?**

The whole problem states in one sentence:

> **The sandbox's lifetime is the conversation, the directory's should be the call, and nothing bridges the two.**

`acquire` is get-or-create, so everything a kind writes survives for the life of the conversation and is readable by every later call in the same sandbox. Both shipped kinds create a fresh per-call directory and leave it there. Nothing obliges them to clean up, and until #452 the protocol offered no way to.

## Four lifetimes

```
binding    one per tool               process           SandboxToolSession (see rename, below)
  └ call   one per tool call          the call          ← owns the call directory
      └ run   0..1, dispatch only     inside a call     GuestRunLayout, reclaim_run

sandbox    one per (scope, thread_id, agent_dir, kind)   conversation
```

The nesting is real; the sandbox is **not its root**. It sits beside the chain, reached by many calls and many bindings, with a lifetime of its own:

- A binding is built once in `sandboxed_tool`, before any key exists, and reads scope and thread per call. So **one binding reaches as many sandboxes as it serves conversations.**
- A workload that ships more than one tool calls `sandboxed_tool` once per tool, so **one sandbox is reached by several bindings.**

Neither side is "per" the other, and the two get confused easily because they coincide in the single-tool, single-conversation case that every test exercises.

## Vocabulary

Three words, and the same concept currently wears two of them.

**`call`** — one execution of the tool function. The framework's unit, and the boundary a `finally` sits on. Qualify anything else: `transport call`, `backend call`, `control-plane call`, as `_host_tools_over_exec` already does.

**`run`** — the transport's unit: one supervised guest program with a `GuestRunLayout`. Published API — `reclaim_run`, `guest_run_layout` — so it is not moving. codeact states the relationship exactly: *"A fresh run per call."*

**Not `invocation`.** This repository spends it on an external `docker` or `wslc` subprocess. It is the one word in the set that is already ambiguous, and importing MAF's usage would make it worse.

The concept that has no word is the framework's directory, and both kinds invented one: codeact calls it `run_dir` even on the plain path where no transport run exists, and bicep calls it `round_dir`, from its fix-round vocabulary. Neither is the framework's, because the framework never had this concept — which is the gap #438 closes. **The accessor is `call_directory()`**, because bicep runs several guest commands in one directory and has no run at all, so "one per call" is the invariant that holds for both kinds and "one per run" describes neither.

### Where each term may live

| term | layer | visible as |
| --- | --- | --- |
| `run` | `_host_tools_over_exec.py`, stdlib-only core | public: `GuestRunLayout`, `reclaim_run` |
| `binding`, `call` | `maf.py`, the MAF glue | by name only: `from maf_sandbox.maf import ...` |
| — | `_protocol.py` | **nothing** |

`maf.py` is the one module that imports `agent_framework` and is deliberately kept out of `__init__`, with two tests enforcing it. So `call` cannot become a protocol term without breaking the layering — and `_protocol.py` holding none of this vocabulary is not an oversight to correct. It is #438's opening sentence, *"There is no protocol for kinds,"* and the shape chosen below deliberately does not invent one.

## What the binding may hold

Everything on `SandboxToolSession` today is host configuration: `router`, `context`, `agent_dir`, `spec`, `name`, `logger`, `output_sink`. Not one field is derived from a caller. `context` is the clearest tell — it takes *callables* rather than a scope and a thread precisely so that nothing caller-shaped is ever stored, because a captured scope would let one conversation reach another's sandbox on a host that builds an agent once and serves many.

That has never been written down, and the rule generalises past the case it was built for:

> **The binding holds host configuration only. Anything derived from a caller — a scope, a thread, a path generated for one call — lives on the call, or nowhere.**

The distinction that matters is finer than per-tool versus per-call. The binding **already gives per-call answers** — `key()`, `list_files()` and `acquire()` return something different every call. What it has never done is *store* one: every per-call answer is **read** from the host's context at the moment it is asked.

`call_directory()` is the first per-call answer that must be **generated and then remembered** for the rest of the call. That, not the binding's lifetime, is why it needs a home of its own.

### The name is wrong, and the name is what caused the mistake

Nothing describes this object as temporal. It is an immutable bundle of everything the tool body needs that is not model input — a per-tool **binding**. `SandboxToolSession` invites exactly the error of hanging a mutable per-call attribute off it.

Rename to `SandboxToolBinding` as its own breaking change, **after** #438: eight signatures across codeact, bicep and `samples/07`, all mechanical. Folding it into #438 would put a rename diff on top of the semantics a reviewer needs to check, and would make the wiring change breaking when it should be dull.

## The call, and what it owns

The binding is shared by every concurrent call, so per-call state cannot be an instance attribute. Put the directory there and two parallel calls receive the same path: the second writes its `program.py` over the first's, shares its files beside the first's inputs, finishes first, and reclaims a directory the first is still executing out of. Silent cross-call exposure, an overwritten program, and a reclaimed live run — a cleanup contract that introduced a worse leak than the one it closes.

A `ContextVar` set by the wrapper is the only object whose lifetime is the call:

```python
_CALL: ContextVar[_SandboxToolCall | None] = ContextVar("maf_sandbox_call", default=None)

@dataclass(slots=True)
class _SandboxToolCall:
    """What one tool call has done that the `finally` has to undo."""
    directory: str | None = None    # set by the first call_directory()
    sandbox: Sandbox | None = None  # set by acquire(), when it succeeds
    key: SandboxKey | None = None   # set alongside it, for the failure report
```

Correct under asyncio because each task starts from a copy of its parent's context: a body that spawns children has them *read* the right record, and nothing a child sets leaks to a sibling.

Three consequences worth stating rather than discovering:

- **It is mutable, and it is the only mutable thing in the chain** — because its lifetime is one call. The binding is immutable because its lifetime is the process. One rule, two objects.
- **It is private.** A kind calls `binding.call_directory()`; the record is what answers. Naming it in the vocabulary rather than `_State` keeps a reader oriented without making it API.
- **`call_directory()` outside a call raises.** There is no call. Returning a path nothing will reclaim is the leak wearing the API meant to close it.

The two alternatives are closed rather than unattractive. Passing a per-call object into the body would put it in the tool's JSON schema, because the body's signature *is* the schema. Rebuilding the binding per call contradicts `build`'s documented guarantee that it runs once, when a tool is attached.

## The wrapper, and three silent failures

`sandboxed_tool` ends with `return [decorate(build(session))]`; the body reaches MAF untouched. The `finally` needs a wrapper between them, and MAF's function middleware cannot serve — it is an *agent* parameter, so a library attaching one tool cannot register it, and a host that did would wrap every tool it has.

Three MAF lookups read the function object, and each will read the wrapper instead of the body:

| what | reads |
| --- | --- |
| tool description | `f.__doc__` |
| parameter schema | `inspect.signature(func)` + `typing.get_type_hints(func)` |
| context injection | `inspect.signature(func)`, then injects by name |

`functools.wraps` covers all three: it copies `__doc__` and `__annotations__` and sets `__wrapped__`, which `inspect.signature` follows and which `get_type_hints` walks explicitly to find `globalns`. So the schema is built from the body's real parameters, resolved against the *kind's* module.

**Every failure here is silent and reaches the model rather than the log**, which is why it is documented rather than merely done:

- Miss it and **the description vanishes** — the model is handed the wrapper's docstring or an empty string, and for `execute_code` that is a description assembled from twelve combinations of the channels a host wired.
- Miss it and **the schema collapses to nothing** — MAF filters `VAR_POSITIONAL` and `VAR_KEYWORD` out of the fields, so a bare `(*args, **kwargs)` wrapper yields a model with no parameters at all.
- Half-do it and **every parameter degrades to `str`** — `get_type_hints` is wrapped in a bare `except`, so annotations that fail to resolve against `maf.py`'s globals are swallowed and fall through to the default.

Nothing else in the stack detects any of it, so a test asserts the attached tool's description and input-model fields against the unwrapped body. It couples core's tests to MAF's shape, knowingly, because the alternative is a failure class with no detector.

**And the wrapper never raises from its `finally`** — an exception there replaces the one in flight, reporting a cleanup failure over the run's actual reason. That fixes two signatures rather than expressing a preference: the reclaim helper returns a reason instead of raising, and the host callback runs inside its own `try`. `_remove_tree` already holds the rule one layer down, documented as *"never a raise."*

## Cleanup, as a consequence

With the call owning a directory, reclamation is the framework's: `sandboxed_tool` removes what the call owns in a `finally`, and a kind cannot forget a path it never held. What it removes with is **not** core's choice — see #477.

The short version of that argument: the capability gates **confinement, not deletion**. `maf-sandbox-docker`'s `remove` is three confinement checks and one `rm`; wslc's refusal is *"Not for want of `rm`"* — it lacks the parent walk, not the shell. A directory *this stack created*, under `work_dir`, with an unguessable name, has no attacker-chosen component to walk, so the method that removes it needs no confinement and can be mandatory. And it must be the backend's, because core can only dispatch to mechanisms core can name: a `RUN_CODE`-only backend ([#425](https://github.com/sokolaidev/maf-extensions/issues/425)) deletes through its own runtime, which no capability check reaches.

Escalation when it fails is disposal, since neither mechanism is a guarantee — `reclaim_run` concedes that `True` *"narrows the window rather than closing it."* Disposal is the host's call to loosen, the same way `min_isolation` is: a workload may ask for stricter and never for weaker.

## Concurrency, as the other consequence

A per-call directory is tidiness, not isolation. Two calls in one conversation share one sandbox and one filesystem; a call holding `EXEC` can read whatever its sibling is writing. Same user and same conversation, so this is not a tenancy breach — but it also means the disposal escalation above would kill a sibling mid-run, which is what #476 exists to unblock.

The answer is a **lifetime or a lock**, never a capability:

- a lock keyed by sandbox identity, serialising calls that share one (#476), honest only about in-process concurrency;
- or a sandbox per call ([#436](https://github.com/sokolaidev/maf-extensions/issues/436)), which removes the sharing rather than scheduling around it, at a cold start per call.

## Rejected

**A ledger of calls on the binding, or a central one.** The binding is process-global across tenants, so it would grow forever, and its entries cannot be interpreted without a key they do not carry — store the key beside each and it is per-sandbox state in a per-binding container. Worst, it would be safe only because the sandbox key isolates tenants two layers away, undocumented, so the next person to add "retry the leftover next call" turns it unsafe without touching what made it safe. If one is ever wanted it is `dict[SandboxKey, set[str]]` on the router, cleared by `dispose_scope`.

**`CALL_ISOLATION` as a `Capability`.** Per-call isolation is a boundary property, and the two axes are kept apart deliberately: merged into `requires`, a workload could ask for a weaker boundary than the deployment mandates, with the host's floor having no say. It is also not a capability in the ordinary sense — there is nothing for a backend to implement, since a per-call component in the key makes every backend produce a fresh sandbox with no code change.

**Disposing the sandbox at the end of every call, by default.** Correct and expensive, and it takes from the host a decision the host owns — fourteen of the fifteen samples end with `dispose_scope` in a `finally`. As an opt-in for workloads that need the guarantee, it is #436.

**Core dispatching on declared capabilities.** Superseded by #477; it can only choose between mechanisms core can name, and leaves a third branch with no answer at all.

## Rollout

1. **#438** — `call_directory()`, the per-call record, the wrapper and its `finally`, the failure callback, and the invariant above written into the class docstring. Core alone, then the three call sites wired: codeact dispatch, codeact plain, bicep's `round_dir`.
2. **#477** — mandatory `Sandbox.reclaim`; every backend implements it, and core stops choosing. Breaking: `Sandbox` is `@runtime_checkable`.
3. **#476** — serialising calls that share a sandbox, which unblocks the disposal escalation.
4. **`SandboxToolBinding`** — the rename, on its own, once the semantics above have settled.

Two pieces of this belong in code rather than here, because they need to be where someone trips over them: the vocabulary rule in `AGENTS.md`, so the next kind does not invent a fourth word for a call directory, and the binding invariant in the class docstring, where the mistake it prevents is made.
