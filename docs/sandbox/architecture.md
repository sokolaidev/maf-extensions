# The maf-sandbox architecture

> The decided shape of the suite: why the work leaves the process as a tool call, how the layers divide, the protocol's vocabulary, how a sandbox is keyed, and how long anything lives. Its sources of record are [`research/sandbox-architecture.md`](research/sandbox-architecture.md) and [`research/call-lifetime.md`](research/call-lifetime.md).

## The problem, and the shape of the answer

An agent that writes code should not be the thing that runs it. These packages give the work somewhere else to run — reached as an **ordinary tool call**, so the framework's middleware (approvals, information-flow policy, budgets) still sees the call and classifies its result; only the *work* leaves the process. The alternative — surfacing the sandbox as a remote *agent* — silently exits the middleware chain's security context, and everything it returns has to be re-treated as untrusted ingress. The tool shape was chosen precisely to avoid that.

The split into protocol, backend, and kind exists because the workload was the small part. The first production workload's tool logic was a few hundred lines; the undifferentiated harness around it — backend lifecycle, keying, disposal, egress wiring, injection handling — was roughly three times that, and none of it was workload-specific. The protocol is that harness, written once, with the recurring bugs (the multi-replica sandbox leak, the model-supplied-key confused deputy, the command-string injection, the never-reused warm sandbox) turned into invariants.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend` ([`backends/README.md`](backends/README.md)). A new workload — a *kind* — is written against the protocol only ([`kinds/README.md`](kinds/README.md)). That separation is what makes a workload portable: the same `bicep_validate` runs on ACA Sandboxes, a wslc container, a Docker container or an in-process fake, unchanged. It is test-enforced, not aspirational — `TestZeroDependencies` pins the protocol modules to the standard library, `TestNoDirectAzureImport` pins the Bicep and codeact kinds to the protocol, and `TestOnlyDeclaredDependencies` pins every module to imports its own `pyproject.toml` declares, which is the class of defect that otherwise only reproduces on the first clean install.

## The vocabulary

| Type | What it is | The load-bearing detail |
|---|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir)` — the one sandbox a caller may reach | Derived from the host's request context, never from model input; `agent_dir` keeps two agents in one conversation off each other's filesystem |
| `SandboxSpec` | What a sandbox of a given *kind* needs: image, egress allowlist, work dir, `requires` capabilities, an optional `min_isolation` | `egress_allow` is stated positively — **everything unlisted is denied**, so a spec that forgets egress gets the closed configuration, not the open one; `min_isolation` may only *raise* the host's floor, never lower it |
| `Sandbox` | `write_file` + `exec`, the pull surface `stat_file` / `read_file` / `list_dir`, and `remove` | `exec` takes a **sequence** (quoted for you — the safe default) or a **string** (a shell line, only for fixed templates that genuinely need `\|\|` or redirection, with nothing but an already-validated path interpolated). Every path-taking method takes `working_directory` for the same reason `exec` does: no sandbox object knows the spec's `work_dir`, so without it the confinement rule would have no layer able to enforce it |
| `SandboxEntry` / `EntryKind` | What `stat_file` and `list_dir` return: `path`, `kind`, `size_bytes` | `kind` is typed rather than a mode string to parse, and only `FILE` is ever read — a symlink is refused whether or not its target would have resolved somewhere legitimate. `SYMLINK` is a member of its own so a *parent* component that is a link can be told from one that is merely not a directory: both are refused, and only one is an escape. `size_bytes` is `int \| None`, and `None` **fails closed**, because coercing an unknown size to `0` makes a size cap read that file as free |
| `DeclaredOutput` / `OutputDisposition` | A literal relative path a kind expects to produce, and whether it `LAND`s or is `CONSUME`d | Literal, never a glob: resolving `*.png` needs enumeration, which is exactly what `FILES_LIST` exists to gate. The disposition puts the two information flows in the spec — a landed artifact is a sink, a consumed one is a source |
| `TransferLimits` | `max_bytes_per_file` / `max_total_bytes` / `max_files`, one set per direction | Bytes alone do not bound a collection: ten thousand files one byte under the ceiling cost what the ceiling was written to prevent. The spec-side and backend-side defaults are **the same constant**, so nothing pre-existing is refused at attach |
| `ExecResult` | `stdout`, `stderr`, `exit_code` | — |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope` | `acquire` is get-or-create with warm reuse; `dispose_scope` must consult the *service*, not process memory |
| `CallerContext` | How the host identifies the caller and enumerates their files | Scope and thread are **callables read at call time**, not values — see keying below |
| `Isolation` | Seven rungs, weakest to strongest, declared by the backend: `none` / `runtime` / `os_process` / `container` / `hardened_container` / `microvm` / `vm` | The ladder, the host floor and the spec's raise-only override are [`policy-isolation.md`](policy-isolation.md) |
| `Capability` | Ten members a backend declares and a spec requires — what a sandbox can *do*, matched at attach | Each member, the silence rule and the splits behind them are [`capabilities.md`](capabilities.md) |
| `Egress` | Four values a backend declares: `allowlist` / `closed` / `unrestricted` / `undefined` | The honesty rule, and what enforcement each backend actually has, are [`network.md`](network.md) |

The protocol layer draws no isolation boundary itself — it is protocol and policy over whatever a backend actually provides. It holds no credentials, executes nothing, and reaches no network; its job is to make an unsafe backend selection fail loudly at construction rather than silently at first use. Four checks do that work at attach — the minimum-isolation floor, the capability match, the egress-honesty rule and the transfer-limit match — and [`policy-isolation.md`](policy-isolation.md) owns all four, because they answer to different owners and merging them would be a design error: how strong the boundary must be *here* is the **host's** policy; what a sandbox may reach, and what it must be able to do, are properties of the **workload**.

## Keying, and the confused-deputy defense

A sandbox is addressed by `SandboxKey`, and every component of that key comes from the host: `CallerContext.current_scope` and `.current_thread_id` are callables — typically `ContextVar` lookups — read per call. Captured as values at tool-build time, one conversation could reach another's sandbox on any host that builds an agent once and serves many conversations with it; read per call, the key stays a property of the request. Nothing anywhere accepts a key from the model. A call with no bound conversation is *refused* rather than served from a placeholder key, because a shared fallback key is exactly the cross-conversation reach the key exists to prevent.

A sandbox's full identity is **`(key, spec.kind)`**. One key may own one sandbox per kind, and a backend never serves two kinds from one sandbox: the first spec to arrive would decide the image and the egress policy for both, and the second workload would then run under the first one's network policy.

The same context supplies `list_files` — the workload's **injection-pinning boundary**: only a name present in that listing is ever substituted into a command, so a name the model invented, or read out of a poisoned file, has nowhere to go. A failure to enumerate is a refusal, not an empty listing — an empty list would look like "the file store has no files" and refuse each name individually with the wrong reason.

## Lifecycle

`acquire` is get-or-create with warm reuse: a workload's fix-round loop calls it every iteration, and a cold create per round turns a seconds-long loop into a minutes-long one. Two acquires for one key can be in flight at once — the function calls in a single assistant message execute concurrently — so a backend must serialise its get-or-create or derive a name the provider rejects duplicates of; an unguarded read-then-create hands out two sandboxes and remembers one.

Disposal is best-effort by contract — purge must never fail a conversation delete — and the router asks **every** registered backend, not only the selected one, because a conversation may have been served while a different backend was configured, and a sandbox nobody reclaims is a sandbox somebody pays for. `SandboxPurger` wraps this as a duck-typed `purge_scoped_thread(scope, thread_id)` so a host's delete path awaits it without importing anything. The last line of defense is the platform: auto-suspend and auto-delete lifecycle policies bound the cost when the client process dies entirely.

The multi-replica trap gets its own sentence because it is invisible on every dev box: a conversation delete lands on whichever replica serves it, which is usually not the replica that created the sandbox. A backend that consults only its own memory leaks billable compute. The shipped backends label sandboxes at create time and purge **from the service, by label** — and hash rather than truncate label values, because a truncation collision would let one user's purge delete another's sandbox. [`hosts.md`](hosts.md) is where a host wires the delete path.

## The tool call: what owns it, what it owns, and what happens when it ends

Several problems turn out to be one: how a kind cleans up after itself, which layer knows how to delete, what happens when two calls share a sandbox, and who decides where a workload's files go. None can be answered without first answering what a tool call is, what it owns, and how long anything lives. In one sentence:

> **A sandbox lives for the conversation, what a call writes should live for the call, and nothing bridges the two.**

`acquire` is get-or-create, so anything a kind writes survives the whole conversation and is readable by every later call in the same sandbox.

### Four lifetimes

```
binding    one per tool               process           SandboxToolBinding
  └ call   one per tool call          the call          ← owns its own guest path
      └ run   0..1, dispatch only     inside a call     GuestRunLayout, reclaim_run

sandbox    one per (scope, thread_id, agent_dir, kind)   conversation
```

The nesting is real; **the sandbox is not its root.** It sits beside the chain. A binding is built once, before any key exists, and reads scope and thread per call — so one binding reaches as many sandboxes as it serves conversations; and a workload shipping two tools builds two bindings against one sandbox. Neither is "per" the other. They get confused because they coincide in the single-tool, single-conversation case every test exercises.

### The words

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

### What the binding holds, and what the call owns

Every field the binding carries is host configuration: the router, the caller context, the agent directory, the spec, the name, the logger, the sink. Not one is derived from a caller. The context is the tell — it takes *callables* rather than a scope and a thread, so nothing caller-shaped is ever stored. Generalised past the case it was built for:

> **The binding holds host configuration only. Anything derived from a caller — a scope, a thread, a path generated for one call — lives on the call, or nowhere.**

The distinction is finer than per-tool versus per-call. The binding **already gives per-call answers**: the key, the file listing and the acquired sandbox differ every call. What it never does is *store* one — each is **read** from the host's context when asked. `guest_call_path()` is the first value that must be generated and then remembered, which is why it needs a home of its own.

The binding is shared by every concurrent call, so per-call state cannot be an instance attribute. Put the path there and two parallel calls receive the same one: the second writes its program over the first's, finishes first, and reclaims a place the first is still executing in — a cleanup contract that introduces a worse leak than the one it closes. A `ContextVar` bound by the wrapper is the only object whose lifetime is the call. It carries four fields rather than one path, each answering a way the record was wrong about *which* sandbox owns a call's files: `owner`, because one `ContextVar` serves every binding in the process; `name`, the only field a kind asked for; `acquired` as a **mapping**, because `acquire` takes a key and one call can reach two sandboxes; and `closed`, because a task the body left running still holds the record after the removal has run. It is correct under asyncio — each task starts from a copy of its parent's context, so a body that spawns children has them *read* the right record and nothing a child sets reaches a sibling.

- **It is mutable, and the only mutable thing in the chain**, because its lifetime is one call. The binding is immutable because its lifetime is the process. One rule, two objects.
- **It is private.** A kind calls `binding.guest_call_path()`; the record answers.
- **Asking outside a call raises**, and so does asking after one returned. There is no call, or there is no longer one: returning a path nothing will reclaim is the leak wearing the API meant to close it. It raises rather than returning a message, unlike the binding's three accessors, because reaching it wrongly is a wiring mistake in a kind, not something a model can cause or should be told about.

Both alternatives are closed rather than unattractive. Passing a per-call object into the body would put it in the tool's JSON schema, because the body's signature *is* the schema. Rebuilding the binding per call contradicts the guarantee that `build` runs once, when a tool is attached.

### Cleanup, as a consequence

With the call owning a path, reclamation is the framework's: it removes what the call owns in a `finally`, after a result, a refusal and an exception alike, so a kind cannot forget a path it never held. A body that never asked for a path costs nothing, and a synchronous body is not held to the rule at all — it cannot `await acquire`, so it holds no sandbox and owns nothing. A spec whose `work_dir` is the guest root is refused for an async body, because a path one component from the root is one the removal will not perform.

**What it removes with is not core's choice.** The capability gates **confinement, not deletion**: a backend's `remove` is three confinement checks and one delete, and a backend withholds it for want of the parent walk, not the shell. A path *this stack created*, under a base, with an unguessable name, has no attacker-chosen component to walk — so the method that removes it needs no confinement and can be mandatory on every backend. And it must be the backend's, because core can only dispatch to mechanisms core can name: a backend offering a language runtime and no shell deletes through that runtime, which no capability check reaches. Until that method exists, core removes with `rm -rf` over `EXEC` — the one removal every backend serving a kind can do today, since the protocol's own delete is gated by a capability no shipped spec requires — guarded by confinement plus two independent depth guards, because a recursive delete is irreversible and `/` and `/tmp` are the shapes that turn a cleanup into an outage. No failure of it raises: it runs in a `finally`, where an exception would replace whatever the call was already reporting. Cancellation is not such a failure and propagates — containing it would let the call return past a bound the host thought it had. What did not get removed reaches the host as a `ReclaimFailure`, a data-retention failure rather than a tidiness one.

Escalation when removal fails is disposal, because no removal is a guarantee — a stop reaches a process group at most, and anything outliving one can write a path back. Disposal is the host's call to loosen, the same way the isolation floor is: a workload may ask for stricter, never for weaker.

### Concurrency, as the other consequence

A per-call path is tidiness, not isolation. Two calls in one conversation share one sandbox and one store; a call holding `EXEC` can read whatever its sibling is writing. Same user and same conversation, so not a tenancy breach — but the disposal escalation above would kill a sibling mid-run. The answer is a **lifetime or a lock, never a capability**: a lock keyed by sandbox identity, serialising calls that share one — honest only about in-process concurrency, since two replicas serving one conversation hold two locks; or a sandbox per call, which removes the sharing rather than scheduling around it, at a cold start per call. Per-call isolation is not a capability in either sense — it is a boundary property, and there is nothing for a backend to implement, since a per-call component in the key makes every backend produce a fresh sandbox with no code change.

### Where the base comes from

A guest path is relative to something, and today that something is owned by nobody: a workload declares `work_dir` in its spec, no backend reads it, no backend creates it, and the protocol does not promise it exists. The decided rule is that **a backend allocates the storage base and resolves every path against it**, with a spec-level override for an image that pre-populates a fixed root — the only shape that also serves a backend whose store has no filesystem under it. [`hosts.md`](hosts.md) owns the question and what a host configures today.

## The MAF glue — the one deliberate exception

Everything in `maf_sandbox` is stdlib-only except `maf_sandbox.maf`, the single module that imports `agent_framework` — lazily, inside the tool decorator — and it is *not* re-exported from `__init__`, so `import maf_sandbox` stays cheap and MAF-free for backends and protocol-only consumers. Artifact landing lives in `_outputs`, on the **stdlib-only** side of that line: `collect_outputs` needs nothing from the framework, and putting it in the glue would have denied it to protocol-only consumers for no reason — registering it as a protocol module is also what makes `TestZeroDependencies` enforce the rule on it automatically. Three things live in the glue:

- **`sandboxed_tool`** — the shape every sandbox workload's factory has, answering once the questions each workload would otherwise re-derive and get one wrong: attach `[]` when unconfigured (a host with nothing configured keeps its ungrounded behaviour, and the model is never shown a capability it does not have) but *raise* when a backend cannot honour the spec (nothing-configured is a choice; can't-confine is a misconfiguration, and the quiet degrade would ship a workload without its containment); key from the host; sanitize failures; declare information flow; and reclaim what the call owned.
- **`SandboxToolSession`** — the binding, and the failure ladder, whose branches draw a security line: a missing SDK and an unconfigured backend are safe to name (actionable, no account detail); a `ValueError` from image resolution is a message this stack authored, surfaced verbatim; **anything else is a provider or transport failure whose text can carry endpoint and tenant identifiers** — that detail goes to the log, and the model gets a fixed sentence saying only that the run degraded, because tool results are persisted into transcripts. The session's accessors *return* the refusal string rather than raising: a MAF tool answers with `str`, and a refusal the model never sees ends the turn mute.
- **`sandbox_tool_declarations`** — the information-flow declarations on the tool's `additional_properties`, where `agent_framework.security` reads them. `source_integrity="trusted"` is the default because a sandbox result is deterministic first-party output from an environment with no ambient identity and deny-default egress. The confidentiality cap is **opt-in and off by default**, deliberately: writing one participates in a policy leg that may be dormant in the host, so declaring it can change which calls are gated — the host's decision, never a library default. Even when passed, it is written only when something can actually carry data out: the spec permits egress, **or** a sink is attached and the spec declares an output that lands. It is one value from one source and never a fold of two — a cap is an opaque host-vocabulary string, and this package requires orderings to be data with an exhaustiveness test, so two of them cannot be ranked.

One documented gotcha worth repeating: the build callback's **docstring is the tool's description**, passed through verbatim, indentation included — define it at module level, because nesting re-indents every line and silently rewrites what the model reads.

## Where shared code lives

**Rule 1 — what kind of shared thing it is.** An invariant is enforced where it cannot be skipped, not merely shared as code: operational content — transport, containers, processes — goes to a sibling package, convenience goes into `maf_sandbox` itself, and the gate for either move is two independent consumers today, not a hypothetical third.

**Rule 2 — where in core.** A promoted piece is reached by name — `import maf_sandbox.paths`, never re-exported from `__init__` — when it carries **a dependency core lacks**, the way `maf.py` carries `agent_framework`, or when **reaching for it is a foreseeable mistake**: `testing.py` in production code, `maf_sandbox.paths` against a host filesystem path, where the lexical answer is wrong and the caller wants `Path.resolve()`. Anything else is re-exported, because forcing a by-name import on code every consumer of core can already reach buys nothing. The criterion is a hazard, not an audience — kinds and backends *are* core's audience, so "not for apps" would send half the protocol behind a by-name import.

Applied: `maf_sandbox.paths` — `confine_guest_path`, `guest_path_relative_to`, `guest_directory_chain`, and `refuse_symlinked_parents`, the ancestor walk's one consumer, which takes a backend's own unconfined no-follow stat and is what the implementations of the pull surface call instead of writing the walk again — is reach-by-name for the hazard: pointed at a *host* path its answer is lexical, and lexical is wrong there, the mistake sample 08's landing sink resolves before writing to avoid, since a symlink already sitting in `out/` carries a write straight out of it. A floor governs the *published* wheel, not the workspace — which resolves in-tree — so the adoption never waited on a release ([#214](https://github.com/sokolaidev/maf-extensions/issues/214)). `maf_sandbox.conformance` is the third by-name module, on the same hazard as `testing.py`: it is a test harness, and a backend that imported it outside its own suite would ship attack-planting code to every consumer. A store-listing helper sits in `maf.py`, the dependency case, since it reads `FileStoreEntry.type` and that type is `agent_framework`'s — not because `make_caller_context` needs the framework, which it deliberately does not. A landing sink is re-exported from `__init__`, since its audience is every app and its only dependency is the standard library.

The pattern this rule reacts to is real. Guest-path confinement existed three times with byte-identical error strings. The ancestor walk was written twice independently, each time as the fix for the same confinement escape — a symlinked parent that stats through to whatever it points at — and the duplication is what fixing it twice produced, not the bug being fixed. The CONNECT proxy is a byte copy.

A sibling operational package for that transport-and-container content is deferred, not rejected. It has two tenants today: the CONNECT proxy, and the one-entry tar `write_file` builds to push a file in. Two does not beat the cost of another sibling to distribute, plus the version matrix across five dependents that made [#174](https://github.com/sokolaidev/maf-extensions/issues/174) painful while it stood open. The trigger to revisit is a third operational tenant — tar-*out* for wslc ([#125](https://github.com/sokolaidev/maf-extensions/issues/125)) would be one if it lands — or the mirrored-copy test firing in anger. Hoisting the proxy into core instead is rejected outright rather than deferred, for the reason [`network.md`](network.md) gives: it is operational content, and core is stdlib-only.

Two things that look shared are deliberately not hoisted:

- **A caller context built from constants.** `CallerContext.current_scope` and `.current_thread_id` are callables read at call time; a convenience that took plain values instead would hand a host the exact confused-deputy bug the keying rule above exists to prevent, so no such helper exists.
- **`require_env_vars` as a published helper.** It is duplicated once per sample, in each `_scaffold.py` (for example `samples/01_acas_bicep/_scaffold.py:37`), but it is sample scaffolding — an app's own startup-validation policy, not a library obligation. Two consumers is the gate for library code, and fifteen copies of a sample's front matter is not the same claim.

## Vocabulary discipline — no magic strings, no magic numbers

Every value the package accepts or emits is a `StrEnum` member or a named constant defined in exactly one place (the Python floor is ≥3.12): `Isolation`, `Egress`, `Capability`, `SourceIntegrity`, `Identity`, `EntryKind`, `OutputDisposition`, sentinel keys, kind names, capability defaults, size-cap defaults. Bare strings exist only at serialization boundaries and cross into the typed world through the enum constructor, whose `ValueError` *is* the refuse-unknown policy. Orderings are data — `ISOLATION_RANK`, `INTEGRITY_RANK` — each with an exhaustiveness test, so a new member cannot be added without being ranked. Nothing numeric appears inline.

## Status

| Decision | State | Tracking |
|---|---|---|
| A tool call rather than a remote agent; protocol / backend / kind layering, test-enforced | shipped | — |
| A sandbox's identity is `(key, spec.kind)` | shipped | [#84](https://github.com/sokolaidev/maf-extensions/issues/84) (closed) |
| Seven-rung isolation ladder, host floor, spec may only raise; the capability match | shipped | [#85](https://github.com/sokolaidev/maf-extensions/issues/85) (closed); depth in [`policy-isolation.md`](policy-isolation.md) |
| An undeclared `egress` is refused as `undefined`, not read as `unrestricted` | shipped | [#521](https://github.com/sokolaidev/maf-extensions/pull/521) |
| `FILES_OUT` / `FILES_LIST` split, declared outputs, artifact landing | shipped | [#113](https://github.com/sokolaidev/maf-extensions/pull/113) |
| `HOST_TOOLS` dispatch from inside a sandbox | partial — parts A–C landed and the acas and docker backends declare it; wslc does not, and the umbrella's remaining parts are open | [#133](https://github.com/sokolaidev/maf-extensions/issues/133) |
| The call owns a guest path; the per-call record, and the `finally` that reclaims it | shipped | [#496](https://github.com/sokolaidev/maf-extensions/pull/496), kinds wired in [#500](https://github.com/sokolaidev/maf-extensions/pull/500), sample 15 aligned in [#499](https://github.com/sokolaidev/maf-extensions/pull/499) |
| A host learns of a reclaim that did not happen | partial — `on_reclaim_failure` ships; no sample wires it, so the one call-lifetime decision a host must make has no worked example | [#520](https://github.com/sokolaidev/maf-extensions/issues/520) |
| A mandatory reclaim method on `Sandbox`; core stops choosing the mechanism | open — core removes with `rm -rf` over `EXEC` in `_reclaim.py` | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) |
| Calls that share a sandbox are serialised, which unblocks the disposal escalation | open — nothing serialises them today | [#476](https://github.com/sokolaidev/maf-extensions/issues/476) |
| A backend allocates the storage base and resolves every path against it | open — a spec names `work_dir`, no backend reads or creates it | [#480](https://github.com/sokolaidev/maf-extensions/issues/480) |
| `SandboxToolSession` is renamed `SandboxToolBinding`, once the semantics settle | open | untracked |
| A guest-platform axis, so a kind that execs `python3` declares what it needs | open — design settled in [`guest-platform-and-commands.md`](guest-platform-and-commands.md); nothing implemented | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) |
| A sibling operational package for transport-and-container content | open — deferred at two tenants; tar-out for wslc would be the third | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) |
