# Two-axis sandbox policy: a minimum-isolation floor and a capability match

> **Status: PROPOSED** — tracking issue [#85](https://github.com/sokolaidev/maf-extensions/issues/85). Prerequisite defect, filed separately: [#84](https://github.com/sokolaidev/maf-extensions/issues/84) (sandbox identity must incorporate the kind) — landed, alongside Rollout items 1 (the isolation ladder, the floor, and `deployed`'s removal), 2 (capabilities and matching), 4 (`FILES_OUT` with caps, specified separately in [`files-out.md`](files-out.md) and shipped from there) and 5's safety-contract half ([#133](https://github.com/sokolaidev/maf-extensions/issues/133) part A — see the status note on the `HOST_TOOLS` section below); the rest of this document is still proposed, and its code blocks outside those items stay design sketches. The baseline this evolves — the architecture as it ships today — is [`sandbox-architecture.md`](sandbox-architecture.md).

## What this replaces

`SandboxRouter`'s whole policy today is one boolean crossed with one frozenset: `deployed=True` requires the selected backend's isolation to be in `DEPLOYED_ISOLATION = {Isolation.VM}`. That collapses two independent questions — *how strong must the boundary be in this environment* and *what must the sandbox be able to do for this workload* — into a binary that cannot express "a micro-VM is enough for dev", "this host accepts in-process execution on a developer machine", or "this workload needs a language runtime, not a shell". The redesign replaces it with two independent checks the router applies at construction/attach time, keeping the package's fail-loud posture — refuse early, never degrade silently — and removes `deployed` entirely.

## Axis 1 — isolation, as an ordered ladder

```python
class Isolation(StrEnum):                     # str-valued: serializes and compares as its value,
    PROCESS = "process"                       # so existing declarations and config keep working;
                                              # literal same-process execution, no boundary at all
    RUNTIME = "runtime"                       # software boundary in the host process: a restricted
                                              # interpreter or WASM fault isolation (Monty, Wasmtime)
    CONTAINER = "container"                   # shared-kernel namespaces/cgroups
    HARDENED_CONTAINER = "hardened_container" # userspace-kernel syscall interception (gVisor-class)
    MICROVM = "microvm"                       # hypervisor boundary, minimal or no guest OS, host-adjacent
    VM = "vm"                                 # dedicated, full VM on remote infrastructure — stricter than the standard requires

ISOLATION_RANK: Mapping[Isolation, int] = {
    level: rank
    for rank, level in enumerate(
        (
            Isolation.PROCESS,
            Isolation.RUNTIME,
            Isolation.CONTAINER,
            Isolation.HARDENED_CONTAINER,
            Isolation.MICROVM,
            Isolation.VM,
        )
    )
}  # the ordering lives HERE and nowhere else; an exhaustiveness test asserts every member is ranked

def meets_floor(declared: Isolation, floor: Isolation) -> bool:
    return ISOLATION_RANK[declared] >= ISOLATION_RANK[floor]
```

- `runtime` sits between `process` and `container`, and exists to draw the line between *no boundary at all* and *a software boundary*: a literal in-process function call (the testing fake) is `process`; a sandboxing language runtime — Monty's restricted interpreter, a Wasmtime-class WASM runtime's software fault isolation with capability-based imports — is `runtime`. The boundary is real (OS access rejected by construction, linear-memory confinement), but it is enforced by software in the host process's own address space: an escape is a runtime bug and lands *inside the host process*, beside its memory and credentials, with no second privilege domain in the way — which is why the rung sits below `container`, whose enforcement lives in the kernel, an independent domain. The ordering ranks trust bases, not implementations — it is not a claim that every kernel beats every verified SFI runtime — and the capability axis carries the rest of the honesty: Monty declares no filesystem and no network capabilities at all, which the ladder alone could never express.
- `hardened_container` sits between `container` and `microvm`: syscall interception in a userspace kernel is genuinely stronger than namespaces and genuinely weaker than a hardware boundary, and giving it its own rung makes admitting it somewhere an explicit policy value rather than a backend rounding itself up.
- `microvm` covers Kata, Firecracker, Hyperlight-class embedded VMMs, Docker Sandbox — a real hypervisor boundary with a minimal or absent guest OS. **This rung is the default floor, and it is a defined standard, not a self-assigned label — next section.**
- `vm` stays above `microvm` as a dedicated, full VM on remote infrastructure — a guest provisioned per workload or per tenant, where an escape lands on machinery that exists only for that purpose. Keeping it distinct keeps the ladder a total order, which `min` comparison needs, and preserves a stricter posture for hosts that want one. **Classification note: ACA Sandboxes declare `microvm` on this ladder.** They are hardware-isolated micro-VMs; their current `Isolation.VM` declaration is an artifact of the three-rung ladder, where `vm` was the only hypervisor rung, and the reclassification rides the same `feat!` release that introduces the ladder.
- Unknown values refuse, and the enum is the mechanism: backends declare `-> Isolation`, not `-> str`, and at every deserialization boundary the value crosses through `Isolation(raw)`, whose `ValueError` on an unknown *is* the refusal.

## The micro-VM standard

Production's floor is only as strong as the weakest backend allowed to claim the rung, so `microvm` is a conformance bar. A backend claims it (or above) only if **all four** hold:

1. **A hardware virtualization boundary.** The guest executes behind a hypervisor — not shared-kernel namespaces, not userspace-kernel syscall interception. The host kernel is out of the attack surface.
2. **No ambient identity reachable from inside.** No credential material, token store, or cloud metadata endpoint is reachable from the guest — by construction (no network device at all) or by enforced block (a deny-all proxy that blackholes link-local and metadata ranges; a NetworkPolicy on Kata). Precisely: **no identity other than one explicitly attached to this sandbox by declared spec is reachable — the host's above all.**
3. **Confinable egress**: declared `Egress.ALLOWLIST` or `Egress.CLOSED`. A backend that cannot confine egress is capped below `microvm` outright.
4. **An explicit guest↔host surface.** The only channels are the declared ones — files in, results out, declared host tools. No host filesystem mounts beyond declared ones, no host socket passthrough, no shared writable state beyond the backend's own transport.

Consequences: gVisor-class backends cap at `hardened_container` by definition — that is the standard working, not a gap; a runtime-sandboxed interpreter (Monty-class, `runtime`) stays a local-floor backend however honest its no-I/O construction; Kata qualifies **only as configured** (per-pod VM runtime class plus the metadata/link-local block), so conformance is a property of a backend package, never of Kata in the abstract; ACA Sandboxes are the reference conformant backend at `microvm` itself — a hardware virtualization boundary, no ambient identity (the control-plane credential never enters the guest), Deny-default allowlist egress, a declared surface — and remote into the bargain, which is more than the standard asks.

Enforcement is layered: the standard is normative text, and the declarations (`isolation`, `egress`, `capabilities`) are the machine-readable claims the router checks. An in-sandbox conformance probe suite — attempting exactly what the standard forbids (metadata-endpoint fetch, link-local and private-range reach, host-path reads, host-socket presence, undeclared egress with an allowed-host positive control, plus authority probes) — is designed but **parked**; when picked up, its teeth are a release gate on packages claiming `microvm` and a host-runnable entry point for deploy-time verification.

**One slice of it exists.** `maf_sandbox.conformance` is the same idea for a *capability* rather than for an isolation rung: the attacks any backend serving `FILES_OUT` must survive, planted through the backend's own public surface and run against a real instance, specified in [`files-out.md`](files-out.md#confinement). It was taken out of the park early because the parked suite's premise — that a standard enforced by normative text alone is enforced by each author's reading of it — stopped being hypothetical when two backends independently shipped the same escape ([#142](https://github.com/sokolaidev/maf-extensions/issues/142), [#214](https://github.com/sokolaidev/maf-extensions/issues/214)). It is not the isolation suite and does not become one: the probes above run *inside* a sandbox and attack the boundary, while these run *outside* one and attack a capability's contract. What the slice does establish is the shape — a probe carries the reason it exists, a failure names every probe that failed, a probe requiring an undeclared capability is skipped rather than passed — which the isolation suite can adopt rather than reinvent.

## The floor — `deployed` is gone

```python
router = SandboxRouter(backends)                                  # default floor: MICROVM — the production posture
router = SandboxRouter(backends, min_isolation=Isolation.VM)      # stricter: dedicated full-VM infrastructure only
router = SandboxRouter(backends, min_isolation=Isolation.PROCESS) # local machine, opted all the way down
```

- **The default is `MICROVM`.** A host that configures nothing gets the production posture; a developer machine *opts down explicitly*; there is nothing to forget. Strictly safer than the current default (`deployed=False`, everything permitted).
- **A spec may raise the floor, never lower it**: `SandboxSpec.min_isolation` (default `None` = no opinion); effective floor = `max(host, spec)`. The two owners stay separate: how strong the boundary must be *here* is the host's policy; "this kind refuses to run below `microvm` anywhere" is a workload property.
- **Refusal stays at construction/attach time** (`SandboxBackendNotPermitted`), same exception, same fail-loud rationale.
- **Migration**: `deployed=True` maps to `min_isolation=Isolation.MICROVM` — a host using ACA Sandboxes behaves identically, and in the same release the ACAS backend's declaration becomes `Isolation.MICROVM`, its truthful rung on the five-level ladder. The parameter is removed rather than deprecated (0.x, `feat!`).

## Axis 2 — capabilities, declared and matched

```python
class Capability(StrEnum):
    EXEC = "exec"             # run a shell command line / argv
    RUN_CODE = "run_code"     # evaluate code in a language runtime (the CodeAct verb)
    HOST_TOOLS = "host_tools" # dispatch host-registered functions from inside the sandbox
    FILES_IN = "files_in"     # write files into the sandbox before execution
    FILES_OUT = "files_out"   # read files back out after execution
    NETWORK = "network"       # any egress at all — its quality stays in Egress
    SNAPSHOT = "snapshot"     # snapshot/restore reuse
    ATTACHED_IDENTITY = "attached_identity"  # platform-attached, sandbox-scoped identity

DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})
```

- A backend declares `capabilities: frozenset[Capability]`; undeclared defaults to `DEFAULT_CAPABILITIES` — exactly what today's `Sandbox` protocol already obligates (`exec` + `write_file`), so existing backends keep working without lying. Unlike egress, silence here is a functionality claim, not a safety one.
- A spec declares `requires: frozenset[Capability]` (default `DEFAULT_CAPABILITIES`). `ensure_can_serve` refuses when `spec.requires ⊄ backend.capabilities`.
- Selection becomes real routing: `_resolve` generalizes from "first registered backend" to "first registered backend satisfying floor ∧ capabilities ∧ egress" — one router can hold an in-process `run_code` backend for local CodeAct and a remote VM backend for compiler validation, selected per spec.

## `HOST_TOOLS`, layered

> **Status:** the backend-agnostic contract landed via [#133](https://github.com/sokolaidev/maf-extensions/issues/133) part A — `HostToolRegistry`, the `@sandbox_tool` decorator with a mandatory `identity` leg pulled forward from step 6, the `require_declared` gate at registration and at dispatch — with the declaration captured at registration and the registry sealed when its aggregate is taken, so the surface a host classified is the surface that dispatches — a per-run dispatch cap, host-side argument validation at the registry's one door, response size caps reusing `TransferLimits`, and `denied_capabilities` / `denied_identities` on the router. The transport an `EXEC` backend can implement honestly (part B) and the kind integration (part C) remain proposed, and nothing declares the capability yet — which is the intended order: the contract exists before anything can use it. One sentence to read before registering anything, because a declaration reads like a control and is not one: **`Identity.APP` is not the safe option, only the declared one** — it is the application's full authority, and the only real bounds on it are the emptiness of the registry and the dispatch cap. `Identity.USER` is declarable but not servable: registering such a tool raises the whole `execute_code` surface to approval-gated, and dispatching one is refused with the prerequisites named (per-run minting, audience ⊆ egress, the ephemeral `exec` env channel).

Dispatching host functions from inside a sandbox is the CodeAct pattern's differentiator and the one capability where trust crosses *outward* — the function body runs in the host process with the host's privileges, driven by model-written code, and each dispatched call bypasses whatever middleware the host runs. It ships as six layers:

1. **Nothing is dispatchable by default.** The sandbox tool registry starts empty; every function reachable from inside is one a developer explicitly registered.
2. **Registering emits a one-time, suppressible warning** (the experimental-warning shape) naming the property that surprises people: dispatched calls bypass the middleware chain and the boundary sees only the aggregate result.
3. **A role-explicit decorator carries each tool's information-flow declarations.** A dispatched function is a **source** (output brings external data in), a **sink** (conversation-derived data flows out or drives an effect), **both**, or **neither** (pure computation). The decorator makes the developer answer every leg explicitly, with no defaults — `@sandbox_tool(source=..., sink=..., identity=...)`, each leg's `None` a considered "not that role" — and the values are the host's own vocabulary (`agent_framework.security`-shaped constants for MAF hosts, verbatim passthrough otherwise).
4. **The registry derives the `execute_code` tool's own classification — per leg, over the relevant subset.** Result integrity = weakest over *sources only* (a sink-only or pure tool must not drag the result to untrusted). Sink caps are collected **verbatim and unfolded**: confidentiality values are the host's own vocabulary and this package owns no ordering for them — the repository's rule is that an ordering is data before anything ranks by it — so more than one distinct cap is the host's to reconcile against its own egress cap, never this package's to guess between. The aggregates refine, never replace, the host's classification of `execute_code` itself as an exec sink under untrusted taint.
5. **A `require_declared` gate** (library default `False`). "Declared" is a stamped sentinel (`FLOW_DECLARED_KEY` — one literal, one place), distinguishing *considered* from *never considered*. The structural move is **one door**: the bridge resolves tool names exclusively through the registry, so registration is the only way in — and registration is where the declaration is *captured*, read from the function once and never again. Enforcement fires there (raises — a host configuration error) and at dispatch (belt-and-braces, sanitized error into the sandbox). Mutation is answered by making it ineffective rather than by re-gating against it: deriving the aggregate seals the registry, so a later `register` is refused and a stamp swapped for another complete one afterwards reaches nothing — which the re-gate could not have caught, since a swapped-in declaration passes every check. With the gate off, an undeclared tool fails safe — untrusted source, `Identity.APP`, and a flag on the aggregate so a host sees the degrade without diffing the folds.
6. **Router-level denial** — `denied_capabilities={Capability.HOST_TOOLS}`, `denied_identities={...}` — for hosts whose posture wants a hard stop rather than awareness.

Declarations are carried claims; enforcement is the host's middleware. A host without `agent_framework.security` loses nothing structural — the registry, warning, gate, and denials all function identically — and gains classifications that are ready the day it turns enforcement on. Declaring `source=trusted` protects nothing by itself; a claim without a reader is documentation, and the docs say so.

## Identity — whose authority does sandbox work carry?

The token exchange is host plumbing in every case; what differs is where the resulting authority is exercised.

- **A. Control plane.** Backend configs take a **credential factory** — a callable read at call time, the `CallerContext` pattern — so acquire/dispose can run under the app's identity or a per-request exchanged one, the protocol indifferent to which.
- **B. Dispatched host tools acting as the user — recommended for user authority.** The function body runs host-side and reaches the host's on-behalf-of plumbing; the credential never enters the sandbox, only results do. The decorator's `identity` leg (`Identity.APP | Identity.USER | None`) makes it declarable; any `Identity.USER` tool raises the aggregate (user-confidential sources, approval-gated call).
- **C. In-guest provisioned credentials — vocabulary only, discouraged.** Declarations and refusal machinery exist so the pattern can be refused, audited, and reasoned about; hard-refuse under untrusted taint.
- **C′. The single-audience egress cell — a designed benefit case.** A logged-in user's on-behalf-of call runs inside a sandbox whose `egress_allow` is *exactly* the target service: token audience **=** egress, so the token is spendable only at its audience, the response leaves only through the middleware-visible tool result, and attacker-shaped influence has nowhere else to go — network-enforced least privilege the host process cannot provide (host-side, only middleware stands between a confused tool body and the open network). Taint softens to approval: the residual risk is misuse of the user's authority *at the one legitimate service*, bounded and visible. The token rides a per-exec ephemeral channel — `exec(..., env=...)`, a protocol addition — never `write_file`, which persists across warm reuse, shows up in listings, and can echo back out.
- **D. Platform-attached, sandbox-scoped identity — recommended for service authority.** A per-sandbox managed identity exercised through platform connectors: token material exists in nobody's code. Not the forbidden ambient — the standard bars reaching identity that belongs to someone else; an attached identity is the sandbox's own, declared, least-privilege, disposed with it. Obligations: **granularity is declared and matched** (`IdentityScope.SHARED | PER_SCOPE | PER_SANDBOX`; a backend that would silently share where the spec asked for partitioned is refused — sharing is the widening direction); **every authority channel out of a sandbox is spec-declared** — platform connectors need not traverse the sandbox's network path, so `egress_allow` alone does not bound them; **a spec carrying `ATTACHED_IDENTITY` must set a finite platform-side auto-delete** (refused otherwise), so the worst-case window a leaked sandbox retains authority is time-bounded even if every host replica dies; purge stays never-fail-the-delete, but failed *authority* reclamation is loudly observable — disposal now reclaims authority, not just compute.

Composition rules: **`Identity.USER` never attaches** (managed identities are workload identities; the closest D gets is `PER_SCOPE` — an identity per user/tenant whose RBAC is that user's resource partition); a provisioned user token has no `IdentityScope` at all — it is call-time material, per-sandbox-per-run by construction, dead before the sandbox is.

## `FILES_OUT`

> **Superseded by [`files-out.md`](files-out.md)** ([#109](https://github.com/sokolaidev/maf-extensions/issues/109)), which is the specification for this item. The sketch below is kept as the record of what it grew from; where the two disagree, that document wins. Most importantly it **splits the capability in two** — `FILES_OUT` reads paths a spec declared, while open-ended discovery becomes `FILES_LIST`, because Docker has no engine-level listing primitive and requiring one would make that backend either image-dependent or cap-hostile. It also types the listing's entries, adds count and total caps, makes enforcement stream-counted rather than pre-stat, and answers what this paragraph leaves open: symlink confinement, cross-platform path and encoding rules, and where a collected artefact lands.

The protocol grows the pull pair `list_files(path) -> list[str]` / `read_file(path) -> bytes` (bytes — artefacts will not stay text; decoding is the kind's job), and the glue grows `collect_outputs(sandbox, spec)` over the spec's declared output subdir — the shape sync-mount backends map to naturally and attachment-shaped backends buffer into. Size is declare-and-match at two levels: **the spec carries per-direction byte caps** (`files_in_max_bytes` / `files_out_max_bytes`, defaults are named constants — a workload property), **backends declare their own maxima in their limits**, `ensure_can_serve` refuses a spec whose cap exceeds the backend's maximum, and the backend enforces the spec's cap at runtime. Reads are confined to `work_dir`. An opt-in base64-over-exec helper lets an `EXEC`-capable backend implement reads honestly and *then* declare `FILES_OUT` — no router emulation, no laundered claims.

## Sandbox identity is `(key, kind)` — #84

`acquire` keys sandboxes by `SandboxKey` alone today, so two kinds on one agent would share a sandbox and union their egress lists — filed as [#84](https://github.com/sokolaidev/maf-extensions/issues/84) and sequenced first: it blocks every second kind, cell or not.

## Vocabulary discipline — no magic strings, no magic numbers

Every value the package accepts or emits is a `StrEnum` member or named constant defined in exactly one place (the Python floor is ≥3.12): `Isolation`, `Egress`, `Capability`, `SourceIntegrity`, `Identity`, `IdentityScope`, sentinel keys, kind names, capability defaults, size-cap defaults. Bare strings exist only at serialization boundaries and cross into the typed world through the enum constructor, whose `ValueError` *is* the refuse-unknown policy. Orderings are data (`ISOLATION_RANK`) with exhaustiveness tests; nothing numeric appears inline.

## The map — where known systems sit

| System | Fits as | Isolation | Capabilities | Egress |
|---|---|---|---|---|
| ACA Sandboxes (`maf-sandbox-acas`) | backend (shipped) | `microvm` (reclassified from the three-rung ladder's `vm`) | `EXEC, FILES_IN` (+`FILES_OUT` when built; +`ATTACHED_IDENTITY`) | `allowlist` |
| `wslc` (`maf-sandbox-wslc`) | backend (shipped) | `container` | `EXEC, FILES_IN` | `allowlist` with proxy image, else `closed` |
| `InProcessSandboxBackend` (`maf_sandbox.testing`) | backend (shipped) | `process` (overridable) | anything a test claims | overridable |
| Monty (`agent-framework-monty`, re-seamed) | backend | `runtime` | `RUN_CODE, HOST_TOOLS` — no `EXEC`, no I/O by construction | `closed` |
| Wasmtime-class WASM runtimes | backend | `runtime` | `RUN_CODE` + capability-gated imports | `closed` (WASI capabilities are opt-in) |
| Hyperlight (`agent-framework-hyperlight`, re-seamed) | backend | `microvm` | `RUN_CODE, HOST_TOOLS, FILES_IN, SNAPSHOT` | `closed` |
| [hyperlight-sandbox](https://github.com/hyperlight-dev/hyperlight-sandbox) | backend | `microvm` | `RUN_CODE, HOST_TOOLS, FILES_IN, FILES_OUT, NETWORK, SNAPSHOT` | `allowlist` |
| [mxc](https://github.com/microsoft/mxc) | backend *family* — declarations derive from the configured containment | per containment | per containment | per containment |
| Docker Sandbox | backend (dev machine) | `microvm` | `EXEC, FILES_IN, FILES_OUT, NETWORK` | `allowlist` (deny-all proxy) |
| Kata on AKS | backend | `microvm` only as configured per the standard | `EXEC, FILES_IN` + image contents | per NetworkPolicy |
| `bicep_validate` (`maf-sandbox-bicep`) | kind (shipped) | no raise | `EXEC, FILES_IN` | four AVM-restore hosts |
| CodeAct (proposed) | kind | no raise | see worked example | `()` |
| Single-audience cell (C′) | kind pattern | no raise | `EXEC, FILES_IN` + env channel | exactly the token's audience |

## Worked example: a CodeAct kind on ACA Sandboxes

A hypothetical `maf-sandbox-codeact`, written to show every axis doing work. The agent gets one tool, `execute_code`; the model writes a short Python program; the program runs *inside* the sandbox, orchestrating the sandbox's own files and runtime; artefacts come back through `FILES_OUT`.

**The spec — every design decision is a field, and every field is a workload property:**

```python
CODEACT_KIND = "codeact"
EXECUTE_CODE_TOOL_NAME = "execute_code"
_WORK_DIR = "/maf-sandbox/work"
_OUTPUT_SUBDIR = "out"
_FILES_IN_CAP = 4 * 1024 * 1024    # named constants — the caps are workload statements,
_FILES_OUT_CAP = 8 * 1024 * 1024   # and the backend refuses specs above its own maxima

def codeact_spec(image: str) -> SandboxSpec:
    return SandboxSpec(
        kind=CODEACT_KIND,                 # part of the sandbox's identity (#84): never shares
                                           # a sandbox with another kind on the same agent
        image=image,                       # a Python runtime and nothing else
        egress_allow=(),                   # CLOSED: the program computes, it does not fetch —
                                           # with no sources registered below, nothing external
                                           # can enter, and nothing can leave except the result
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        files_in_max_bytes=_FILES_IN_CAP,
        files_out_max_bytes=_FILES_OUT_CAP,
        # min_isolation not set: no raise — the host's floor governs. A kind that ran
        # code influenced by untrusted web content might pin MICROVM here instead.
    )
```

`requires` uses `EXEC` — the ACA Sandboxes road: `write_file` the program, `exec` the interpreter. The same kind could later ship a `RUN_CODE` variant served by an embedded-interpreter backend; *the spec is where that choice lives*, and the router picks whichever registered backend satisfies it.

**The router, per environment — the floor is the whole deployment story:**

```python
prod  = SandboxRouter([acas_backend])                                    # default floor MICROVM; ACAS conforms at the floor
local = SandboxRouter([wslc_backend], min_isolation=Isolation.CONTAINER) # a developer opting down, explicitly
wired = SandboxRouter([wslc_backend])                                    # raises SandboxBackendNotPermitted at
                                                                         # construction: container < microvm floor
```

And the one-line wiring test a host runs in its own suite: `router.ensure_can_serve(codeact_spec(image))` — which refuses, before any tool attaches, a backend that cannot confine egress, lacks `FILES_OUT`, or caps files below the spec's ask.

**The tool — attach-nothing, host-keyed, sanitized, exactly the existing factory shape:**

```python
tools = sandboxed_tool(
    build_execute_code,            # writes program.py via write_file, execs the interpreter,
                                   # collects stdout + collect_outputs(sandbox, spec)
    router=router, context=context, agent_dir=agent_dir,
    spec=codeact_spec(image), name=EXECUTE_CODE_TOOL_NAME,
)
```

An unconfigured host gets `[]` — no tool, not a failing one. The sandbox key derives from the host's request context; a warm sandbox is reused across the model's fix rounds (`acquire` is get-or-create), and #84's kind-aware identity keeps this sandbox separate from, say, an IaC-validation kind on the same agent — their egress lists never merge.

**The registry — empty, and that emptiness is the security story:** no host tools are registered, so `HOST_TOOLS` is neither required nor granted, nothing is dispatchable, the registration warning never fires, and the middleware-bypass channel simply does not exist for this kind. With no registered *sources*, no external data can enter the sandbox (egress is closed too); with no registered *sinks* and an empty `egress_allow`, the derivation writes no confidentiality cap — the sandbox cannot exfiltrate what it is given. The one flow that remains is the model-facing `execute_code` call itself, which rides the middleware chain like any tool call and stays classified host-side as an exec sink under untrusted taint.

**Optionally widening it — and what each widening costs, visibly:** registering a documentation-lookup host tool takes `@sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=None)` under `require_declared=True` — an unstamped function is refused at registration; the aggregate result-integrity now derives from the registered source set; `Capability.HOST_TOOLS` joins `requires`, and a host whose router denies that capability refuses the widened kind at attach time, unchanged code everywhere else. If the program instead needed to call one external service on behalf of the logged-in user, that call does **not** get grafted onto this kind — it becomes a separate single-audience cell (C′): its own kind, its own sandbox (again #84), `egress_allow` = exactly the token's audience, approval-gated under taint.

The point of the example: every posture question — where may this run, what may it do, what may enter and leave, whose authority does it carry — is answered by a declared field checked at construction or attach time, and every widening is a visible diff to a spec or a registration, never an ambient side effect.

## Rollout

Each step an issue, sequenced — **(1) to (4) have landed, and (5)'s safety contract with them**: (1) ladder + floor + rank + `deployed` removal (`feat!`); (2) capabilities + matching; (3) kind-aware sandbox identity (#84 — first, it blocks every kind); (4) `FILES_OUT` with caps, specified in [`files-out.md`](files-out.md); (5) `HOST_TOOLS` registry + decorator + gates — the contract landed as #133 part A, the transport and the kind integration have not; (6) the **rest** of the identity vocabulary: `Identity`, the decorator's declaration leg, `SandboxSpec.identities` and `denied_identities` came forward with (5), leaving A–D's plumbing, `IdentityScope` and `ATTACHED_IDENTITY` here, with C′'s prerequisites (`exec` env channel, audience ⊆ egress check); (7) still parked: the **in-sandbox** conformance probe suite, the one that attacks an isolation rung from inside the boundary. Its capability-side counterpart landed early as `maf_sandbox.conformance` (#214) — see above for why, and for why it does not stand in for this one.
