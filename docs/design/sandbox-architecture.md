# The maf-sandbox architecture, as built

> **Status: AS BUILT** — describes what ships on `main` today: `maf-sandbox` 0.3.0, `maf-sandbox-acas` 0.3.2, `maf-sandbox-bicep` 0.3.1, `maf-sandbox-wslc` 0.2.1. The proposed evolution of this design is [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) (tracking [#85](https://github.com/sokolaidev/maf-extensions/issues/85)); this document is its baseline.

## The problem, and the shape of the answer

An agent that writes code should not be the thing that runs it. These packages give the work somewhere else to run — reached as an **ordinary tool call**, so the framework's middleware (approvals, information-flow policy, budgets) still sees the call and classifies its result; only the *work* leaves the process. The alternative — surfacing the sandbox as a remote *agent* — silently exits the middleware chain's security context, and everything it returns has to be re-treated as untrusted ingress; the tool shape was chosen precisely to avoid that.

The split into protocol, backend, and kind exists because the workload was the small part. The first production workload's tool logic was a few hundred lines; the undifferentiated harness around it — backend lifecycle, keying, disposal, egress wiring, injection handling — was roughly three times that, and none of it was workload-specific. The protocol is that harness, written once, with the recurring bugs (the multi-replica sandbox leak, the model-supplied-key confused deputy, the command-string injection, the never-reused warm sandbox) turned into invariants.

## Layering

```
app  ->  maf_sandbox (protocol + router)  ->  a backend  ->  the sandbox
              ^ a kind calls the router; kinds and backends never import each other
```

A new backend implements `SandboxBackend`. A new workload — a *kind* — is written against the protocol only. That separation is what makes a workload portable (the same `bicep_validate` runs on ACA Sandboxes, a wslc container, or an in-process fake, unchanged), and it is test-enforced, not aspirational: `TestZeroDependencies` pins the protocol modules to the standard library, `TestNoDirectAzureImport` pins the Bicep kind to the protocol, and `TestOnlyDeclaredDependencies` pins every module to imports its own `pyproject.toml` declares — the class of defect that only reproduces on the first clean `pip install`.

## The vocabulary

| Type | What it is | The load-bearing detail |
|---|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir)` — the one sandbox a caller may reach | Derived from the host's request context, never from model input; `agent_dir` keeps two agents in one conversation off each other's filesystem |
| `SandboxSpec` | What a sandbox of a given *kind* needs: image, egress allowlist, work dir | `egress_allow` is stated positively — **everything unlisted is denied**, so a spec that forgets egress gets the closed configuration, not the open one |
| `Sandbox` | `write_file` + `exec` — all a workload gets | `exec` takes a **sequence** (quoted for you — the safe default) or a **string** (a shell line, only for fixed templates that genuinely need `\|\|`/redirection, with nothing but an already-validated path interpolated) |
| `ExecResult` | `stdout`, `stderr`, `exit_code` | — |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope` | `acquire` is get-or-create with warm reuse; `dispose_scope` must consult the *service*, not process memory |
| `WorkspaceContext` | How the host identifies the caller and enumerates their files | Scope and thread are **callables read at call time**, not values — see keying below |
| `Isolation` | `vm` / `container` / `process`, declared by the backend | Read by the router's deployed check; the value lives with the backend because that is who knows the truth about itself |
| `Egress` | `allowlist` / `closed` / `unrestricted`, declared by the backend | Exists because `egress_allow` was otherwise a contract nothing checked |

The protocol layer draws no isolation boundary itself — it is protocol and policy over whatever a backend actually provides. It holds no credentials, executes nothing, and reaches no network; its job is to make an unsafe backend selection fail loudly at construction rather than silently at first use.

## The two policy rules

**1. The deployed-isolation rule.** `DEPLOYED_ISOLATION = frozenset({Isolation.VM})`: when the host reports it is running deployed, the router refuses to select anything weaker than a VM boundary — `SandboxBackendNotPermitted`, raised at construction, so a misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe. It refuses rather than degrades, because both alternatives are worse: falling back to a stronger backend would hide the misconfiguration, and proceeding with the weaker one would break the posture claims a deployment makes about every execution surface. A hardened container runtime (gVisor, Kata, Firecracker) is deliberately *not* in the permitted set — admitting one is a decision for whoever owns those posture claims.

**2. The egress-honesty rule.** `ensure_can_serve(spec)` — called by the tool factory, and also the whole of a host's wiring test — refuses a backend that cannot confine egress to what the spec allows. Silence is read as enforcing nothing: a backend that never declared `egress` is treated as `UNRESTRICTED` and refused, because one written before the property existed cannot have been enforcing an allowlist it never read. The asymmetry is deliberate: a backend that confines **less** than the spec asks silently widens what the workload was designed to reach (refused); one that confines **more** only makes the workload fail loudly at whatever it could not fetch (permitted, with a warning).

The two rules answer to different owners, and merging them would be a design error: how strong the boundary must be is the **host's** policy, read from `deployed`; what a sandbox may reach is a property of the **workload**, stated in its spec. A single "required capabilities" list would let a workload ask for a weaker boundary than the deployment mandates.

## Keying, and the confused-deputy defense

A sandbox is addressed by `SandboxKey`, and every component of that key comes from the host: `WorkspaceContext.current_scope` and `.current_thread_id` are callables — typically `ContextVar` lookups — read per call. Captured as values at tool-build time, one conversation could reach another's sandbox on any host that builds an agent once and serves many conversations with it; read per call, the key stays a property of the request. Nothing anywhere accepts a key from the model. A call with no bound conversation is *refused* rather than served from a placeholder key, because a shared fallback key is exactly the cross-conversation reach the key exists to prevent. A sandbox's full identity is `(key, spec.kind)` — one key may own one sandbox per *kind*, and a backend never serves two kinds from one sandbox, because the first spec to arrive would decide the image and the egress policy for both (#84).

The same context supplies `list_files` — the workload's **injection-pinning boundary**: only a name present in that listing is ever substituted into a command, so a name the model invented, or read out of a poisoned file, has nowhere to go. A failure to enumerate is a refusal, not an empty listing — an empty list would look like "the workspace has no files" and refuse each name individually with the wrong reason.

## Lifecycle

`acquire` is get-or-create with warm reuse: a workload's fix-round loop calls it every iteration, and a cold create per round turns a seconds-long loop into a minutes-long one. Two acquires for one key can be in flight at once — the function calls in a single assistant message execute concurrently — so a backend must serialise its get-or-create or derive a name the provider rejects duplicates of; an unguarded read-then-create hands out two sandboxes and remembers one.

Disposal is best-effort by contract — purge must never fail a conversation delete — and the router asks **every** registered backend, not only the selected one, because a conversation may have been served while a different backend was configured, and a sandbox nobody reclaims is a sandbox somebody pays for. `SandboxPurger` wraps this as a duck-typed `purge_scoped_thread(scope, thread_id)` so a host's delete path awaits it without importing anything. The last line of defense is the platform: auto-suspend/auto-delete lifecycle policies bound the cost when the client process dies entirely.

The multi-replica trap gets its own sentence because it is invisible on every dev box: a conversation delete lands on whichever replica serves it, which is usually not the replica that created the sandbox. A backend that consults only its own memory leaks billable compute. The ACAS backend labels sandboxes at create time and purges **from the service, by label** — and hashes rather than truncates label values, because a truncation collision would let one user's purge delete another's sandbox.

## The MAF glue — the one deliberate exception

Everything in `maf_sandbox` is stdlib-only except `maf_sandbox.maf`, the single module that imports `agent_framework` — lazily, inside the tool decorator — and it is *not* re-exported from `__init__`, so `import maf_sandbox` stays cheap and MAF-free for backends and protocol-only consumers. Three things live there:

- **`sandboxed_tool`** — the shape every sandbox workload's factory has, answering once the questions each workload would otherwise re-derive and get one wrong: attach `[]` when unconfigured (a host with nothing configured keeps its ungrounded behaviour; the model is never shown a capability it does not have) but *raise* when a backend cannot honour the spec (nothing-configured is a choice; can't-confine is a misconfiguration, and the quiet degrade would ship a workload without its containment); key from the host; sanitize failures; declare information flow.
- **`SandboxToolSession`** — the failure ladder, whose four branches draw a security line: a missing SDK and an unconfigured backend are safe to name (actionable, no account detail); a `ValueError` from image resolution is a message this stack authored, surfaced verbatim; **anything else is a provider/transport failure whose text can carry endpoint, subscription and tenant ids** — that detail goes to the log, and the model gets a fixed sentence saying only that the run degraded, because tool results are persisted into transcripts. The session's accessors *return* the refusal string rather than raising: a MAF tool answers with `str`, and a refusal the model never sees ends the turn mute.
- **`sandbox_tool_declarations`** — the information-flow declarations on the tool's `additional_properties`, where `agent_framework.security` reads them. `source_integrity="trusted"` is the default because a sandbox result is deterministic first-party output from an environment with no ambient identity and deny-default egress. The confidentiality cap is **opt-in and off by default**, deliberately: writing one participates in a policy leg that may be dormant in the host, so declaring it can change which calls are gated — the host's decision, never a library default — and even when passed it is written only if the spec actually permits egress, because a sandbox with no network cannot carry anything out.

One documented gotcha worth repeating: the build callback's **docstring is the tool's description**, passed through verbatim, indentation included — define it at module level, because nesting re-indents every line and silently rewrites what the model reads.

## The shipped backends

| | `maf-sandbox-acas` | `maf-sandbox-wslc` | `maf_sandbox.testing` |
|---|---|---|---|
| Isolation | `vm` — hardware-isolated micro-VM (a property of the Azure service, not of this code) | `container` — and the deployed refusal "is the feature": a backend for the machine you are sitting at, which the router will not be argued into treating as anything else | `process` (constructor-overridable, as are all declarations) |
| Egress | `allowlist`: `default_action: Deny` plus one `Allow` per host **from the spec, not from configuration** | `closed` by default (`--network none`); `allowlist` when an `egress_proxy_image` is configured — an internal network and a dual-homed CONNECT proxy, enforcement by topology, no TLS decryption, no external DNS from the sandbox | overridable |
| Identity | Control-plane credential (`DefaultAzureCredential`) never travels into the guest; no path from inside back to the host's identity or another conversation's sandbox | none | n/a |
| Reuse & purge | Warm resume over cold create; labels at create, purge by label from the service; acquire-race guard | ~half-second creates; egress scaffolding re-ensured every acquire, so a proxy a reboot stopped is rebuilt rather than left declared-but-unenforced | records every key/spec/dispose/purge; `acquire_error` for exercising a kind's degrade path |

## The shipped kind: `bicep_validate`

The first workload, and the template for the pattern: **fixed command templates** with no agent-authored text interpolated — the only substitution is a filesystem path, validated against the workspace listing *and* against resolving inside the workspace before it reaches a template; **sanitized error surfaces**, so a compiler or shell error cannot smuggle sandbox-internal detail into the conversation; an egress allowlist of exactly the four hosts an AVM module restore reads, pinned in the spec; and zero Azure imports, test-enforced. Its framing names the tiers: T2 (compiler truth) instead of T0 (the model checking its own work) — and every degrade path in the glue returns the workload to T0 *visibly*. The hard-won CLI behaviours (SARIF on stderr for `build` but stdout for `lint`, `build-params` for `.bicepparam`, config discovery only by walking up from the source file) are documented where they bite, in the tool source.

## Known limits — what the proposal addresses

Held against real workloads, the seams that bind are: the policy is one boolean crossed with one frozenset, so "a micro-VM is enough for dev" and "in-process is fine on my machine" are inexpressible; there is no capability vocabulary, so kind↔backend fit is implicit in prose; there is no way to get a file *out* of a sandbox; and there is no identity vocabulary for on-behalf-of or platform-attached authority. Each of these is taken up, with this document as the baseline, in [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md).
