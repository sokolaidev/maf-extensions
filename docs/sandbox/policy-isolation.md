# Policy and isolation

> The router's policy surface — the isolation ladder, the micro-VM standard, the floor, and the four checks every sandbox request passes. Source of record: [`research/two-axis-sandbox-policy.md`](research/two-axis-sandbox-policy.md).

## Two questions, kept apart

A sandbox request asks two independent things, and the policy answers them separately: **how strong must the boundary be in this environment**, which is the host's question, and **what must the sandbox be able to do for this workload**, which is the spec's. The first is an ordered ladder with a floor; the second is a set match. Merging them would be a design error — a capability list cannot say "a micro-VM is enough for dev", and a boundary strength cannot say "this kind needs a language runtime, not a shell".

The policy that preceded this was one boolean, `deployed=True`, crossed with one frozenset that required `Isolation.VM`. It collapsed both questions into a binary, and it was removed rather than deprecated when the ladder landed.

## Axis 1 — isolation, as an ordered ladder

`Isolation` is a `StrEnum` of seven rungs, declared by the backend because that is who knows the truth about itself, and read by the router.

| Rung | Value | What it means |
|---|---|---|
| `NONE` | `none` | No boundary at all: the workload runs in the host process, with the host's authority. Tests and local fakes. |
| `RUNTIME` | `runtime` | A software boundary inside the host process — a restricted interpreter, a WASM runtime's fault isolation with capability-gated imports. |
| `PROCESS` | `os_process` | A separate OS process: a kernel-enforced address space, sharing the host's kernel and filesystem, with no namespaces. |
| `CONTAINER` | `container` | Shared-kernel namespaces and cgroups — the host kernel is in the attack surface. |
| `HARDENED_CONTAINER` | `hardened_container` | Syscall interception in a userspace kernel (gVisor-class), between namespaces and hardware. |
| `MICROVM` | `microvm` | A hypervisor boundary with a minimal or absent guest OS, and no ambient identity reachable from inside. |
| `VM` | `vm` | A dedicated, full VM provisioned for this workload on remote infrastructure. |

**The order lives in `ISOLATION_RANK` and nowhere else** — a `Mapping[Isolation, int]` built by enumerating the rungs weakest-first, pinned by an exhaustiveness test that asserts every member is ranked, with `meets_floor(declared, floor)` the one comparison. Orderings are data in this package; nothing ranks two rungs by reading them.

**`runtime` sits below `container` because it ranks trust bases, not implementations.** A restricted interpreter's boundary is real — OS access rejected by construction, linear-memory confinement — but it is enforced by software in the host process's own address space, so an escape lands *inside* the host process, beside its memory and credentials, with no second privilege domain in the way. A container's enforcement lives in the kernel, an independent domain. This is not a claim that every kernel beats every verified SFI runtime; the capability axis carries the rest of the honesty, and a runtime backend declaring no filesystem and no network at all says something the ladder never could.

**`os_process` exists with no backend behind it, deliberately.** Without the rung, a backend running untrusted code in a subprocess would have to understate itself as `runtime` or overstate itself as `container`, and the ladder's whole value is that neither is available. It is above `runtime` because enforcement moves into the kernel; below `container` because a container is this plus namespaces and cgroups.

**The bottom rung was renamed, and the old string was not reused.** `NONE` was spelled `PROCESS` until 0.14, where it read as a real boundary and meant the absence of one. The *name* `PROCESS` has since been taken back for the genuine rung two ranks above; the *string* `"process"` has not and never will be, and `Isolation("process")` raises `ValueError` in every release from the rename onward. A name is resolved where the code is written, so reusing it is a decision someone makes; a string is resolved at run time out of configuration nobody re-reads, so reusing it would have been a silent two-rung promotion.

**`vm` stays above `microvm`** as a dedicated, full VM on remote infrastructure — a guest provisioned per workload or per tenant, where an escape lands on machinery that exists only for that purpose. Keeping it distinct is what keeps the ladder a *total* order, which the `max` of two floors needs, and it preserves a stricter posture for hosts that want one. ACA Sandboxes declare `microvm`, not `vm`: they are hardware-isolated micro-VMs, and the `vm` they declared before the ladder was an artifact of a three-rung scale where `vm` was the only hypervisor rung.

**A local hypervisor is where both hypervisor rungs become reachable from one package**, and how it declares them without a rung that varies — one guest template family per backend *instance*, so `isolation` stays the constant this ladder needs and a deployment serving both registers two instances — is settled as Decision 5 of [`guest-platform-and-commands.md`](guest-platform-and-commands.md).

**Unknown values refuse rather than rank.** Backends declare `-> Isolation`, and every deserialization boundary crosses through `Isolation(raw)`, whose `ValueError` *is* the refusal — the router turns it into `SandboxBackendNotPermitted` naming the ladder, because nothing here can tell whether an unrecognised boundary is stronger or weaker than the floor. The same discipline governs the whole policy surface: every value it accepts or emits is a `StrEnum` member or a named constant defined in exactly one place, bare strings exist only at serialization boundaries, and orderings are data with exhaustiveness tests. Nothing numeric appears inline.

## The micro-VM standard

Production's floor is only as strong as the weakest backend allowed to claim the rung, so `microvm` is a conformance bar rather than a self-assigned label. A backend claims it, or above, only if **all four** hold:

1. **A hardware virtualization boundary.** The guest executes behind a hypervisor — not shared-kernel namespaces, not userspace-kernel syscall interception. The host kernel is out of the attack surface.
2. **No ambient identity reachable from inside.** No credential material, token store, or cloud metadata endpoint is reachable from the guest — by construction (no network device at all) or by enforced block. Precisely: **no identity other than one explicitly attached to this sandbox by declared spec is reachable — the host's above all.**
3. **Confinable egress**: declared `Egress.ALLOWLIST` or `Egress.CLOSED`. A backend that cannot confine egress is capped below `microvm` outright.
4. **An explicit guest↔host surface.** The only channels are the declared ones — files in, results out, declared host tools. No host filesystem mounts beyond declared ones, no host socket passthrough, no shared writable state beyond the backend's own transport.

Consequences follow mechanically. gVisor-class backends cap at `hardened_container` by definition — that is the standard working, not a gap. A runtime-sandboxed interpreter stays a local-floor backend however honest its no-I/O construction. Kata qualifies **only as configured** (a per-pod VM runtime class plus the metadata and link-local block), so conformance is a property of a backend *package*, never of a technology in the abstract. ACA Sandboxes are the reference conformant backend at `microvm` itself — a hardware virtualization boundary, no ambient identity (the control-plane credential never enters the guest), deny-default allowlist egress, a declared surface — and remote into the bargain, which is more than the standard asks.

**Enforcement is layered.** The standard is normative text; the declarations (`isolation`, `egress`, `capabilities`) are the machine-readable claims the router checks. A capability-side conformance slice exists and is shipped: `maf_sandbox.conformance` is the attacks any backend serving `FILES_OUT` must survive, planted through the backend's own public surface and run against a real instance ([`research/files-out.md`](research/files-out.md)). It was taken out of the park early because the premise — that a standard enforced by normative text alone is enforced by each author's reading of it — stopped being hypothetical when two backends independently shipped the same escape. It is not the isolation suite and does not become one: these probes run *outside* a sandbox and attack a capability's contract. The **in-sandbox** suite — attempting exactly what the standard forbids, from inside the boundary — is designed and **parked**; when picked up, its teeth are a release gate on packages claiming `microvm` and a host-runnable entry point for deploy-time verification. What the shipped slice establishes is the shape it can adopt rather than reinvent: a probe carries the reason it exists, a failure names every probe that failed, and a probe requiring an undeclared capability is skipped rather than passed.

## The floor

```python
router = SandboxRouter(backends)                                  # default floor: MICROVM — the production posture
router = SandboxRouter(backends, min_isolation=Isolation.VM)      # stricter: dedicated full-VM infrastructure only
router = SandboxRouter(backends, min_isolation=Isolation.NONE)    # local machine, opted all the way down
```

- **The default is `MICROVM`.** A host that configures nothing gets the production posture; a developer machine *opts down explicitly*; there is nothing to forget.
- **A spec may raise the floor, never lower it.** `SandboxSpec.min_isolation` defaults to `None` — no opinion — and the effective floor is the stricter of the host's and the spec's. The two owners stay separate: how strong the boundary must be *here* is the host's policy; "this kind refuses to run below `microvm` anywhere" is a workload property.
- **Refusal is at construction and at attach**, with `SandboxBackendNotPermitted`. A misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe.

Registering a `container` backend on a router that configures nothing raises at construction, before any tool attaches — `container` is two rungs below the default floor, and the fix is one explicit keyword rather than a silent promotion.

## The four checks

`ensure_can_serve(spec)` is the whole of a host's wiring test — one line in its own suite — and `acquire` runs the same checks before ever reaching the backend, minus the warning, because `acquire` is called every iteration of a warm fix-round loop and must not repeat it. With no backend configured both return: nothing runs, so nothing reaches anything.

Two host denials run first. `denied_capabilities` and `denied_identities` on the router are statements about this host's posture rather than about what a backend could do, so no backend property softens them; they raise `SandboxCapabilityDenied` and `SandboxIdentityDenied`. They are a hard stop rather than a missing feature — whatever backend is registered, the posture refuses.

**1. The minimum-isolation floor.** A backend below the effective floor — or declaring a rung this package does not recognise — is refused with `SandboxBackendNotPermitted`. It refuses rather than degrades, because both alternatives are worse: falling back to a stronger backend would hide the misconfiguration, and proceeding with the weaker one would break the posture claims a deployment makes about every execution surface.

**2. The capability match.** A backend declares `capabilities`, a spec declares `requires`, and a missing member raises `SandboxCapabilityNotSupported`. Unlike isolation, silence is read charitably: an undeclared `capabilities` defaults to `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what the `Sandbox` protocol already obligates — so no backend written before the vocabulary existed had to start lying. It is a functionality mismatch, not a safety one, which is why it gets its own exception: the fix is its own, register a backend that implements the capability or ask for less. The members and their semantics live in [`capabilities.md`](capabilities.md).

**3. The egress-honesty rule.** A backend that cannot confine egress to what the spec allows is refused with `SandboxEgressNotEnforced`. Only `Egress.ALLOWLIST` and `Egress.CLOSED` pass. Silence is read as enforcing nothing: a backend with no `egress` at all resolves to `Egress.UNDEFINED` — a fourth member that is not a rung on the scale — and is refused, because one written before the property existed cannot have been enforcing an allowlist it never read. `UNDEFINED` rather than `UNRESTRICTED` so the refusal reports silence as silence: same verdict, different facts, and only one of the two is a claim the backend actually made. A backend may also set `UNDEFINED` deliberately, to say the question is unanswered rather than answered badly. The asymmetry is the point: confining **less** than the spec asks silently widens what the workload was designed to reach (refused); confining **more** only makes the workload fail loudly at whatever it could not fetch (permitted, with a warning naming the hosts that will be unreachable). The egress model is [`network.md`](network.md).

**4. The transfer-limit match.** A spec carries `TransferLimits` per direction; a backend may declare its own ceilings as `limits`, and a spec asking above them raises `SandboxTransferLimitsNotPermitted` — refused rather than clamped, because a workload served a smaller cap than it declared fails part-way through a collection, and a partial artifact set is worse than none. Silence follows the `Egress` rule rather than the `Capability` one: a limit is a safety claim, so an undeclared `limits` resolves to the conservative default rather than to "no ceiling". Caps and their enforcement are in [`capabilities.md`](capabilities.md).

Each refusal is its own exception, because each has its own fix:

| Refusal | Raised when | The fix |
|---|---|---|
| `SandboxCapabilityDenied` | the spec requires a capability this host denies outright | narrow the workload, or serve it on a host that permits it |
| `SandboxIdentityDenied` | the spec's `identities` carry one this host denies | drop the tools declaring that identity from the registry |
| `SandboxBackendNotPermitted` | the backend is below the effective floor, or declares a rung nobody ranked | register a stronger backend, or lower the floor explicitly |
| `SandboxCapabilityNotSupported` | the backend cannot do what the spec requires | register a backend that implements it, or require less |
| `SandboxTransferLimitsNotPermitted` | the spec's caps exceed the backend's ceilings, or `limits` is the wrong shape | lower the spec's caps, or declare nothing and take the defaults |
| `SandboxEgressNotEnforced` | the backend declares neither `allowlist` nor `closed` | give the backend an egress mechanism, or register one that has it |

Three of these declarations — `capabilities`, `limits`, `egress` — are read off the backend with `getattr`, so a backend written before each existed keeps working. That is the last one that should be added that way; a fourth is the signal to collapse all of them into one optional declarations object.

**All four answer to different owners**, and that is why they are four checks rather than one list. How strong the boundary must be *here* is the **host's** policy, read from `min_isolation`, and a spec may raise it and never lower it. What a sandbox may reach, and what it must be able to do, are properties of the **workload**, stated in its spec. Who registers a dispatchable host function, and whose authority it carries, is the host's again — see [`hosts.md`](hosts.md).

## The map — where known systems sit

Shipped rows are the declarations in the code; the rest is orientation.

| System | Fits as | Isolation | Capabilities | Egress |
|---|---|---|---|---|
| ACA Sandboxes (`maf-sandbox-acas`) | backend, shipped | `microvm` | `EXEC, FILES_IN, FILES_OUT, FILES_LIST, HOST_TOOLS` | `allowlist` |
| Docker (`maf-sandbox-docker`) | backend, shipped | `container` | `EXEC, FILES_IN, FILES_OUT, FILES_DELETE, HOST_TOOLS` | `closed`; `allowlist` when an egress proxy image is configured |
| `wslc` (`maf-sandbox-wslc`) | backend, shipped | `container` | `EXEC, FILES_IN` | `closed`; `allowlist` when an egress proxy image is configured |
| `InProcessSandboxBackend` (`maf_sandbox.testing`) | backend, shipped | `none` | `DEFAULT_CAPABILITIES` | `allowlist` — and every declaration is constructor-overridable, which is what makes it a policy test fixture |
| `bicep_validate` (`maf-sandbox-bicep`) | kind, shipped | no raise | `EXEC, FILES_IN` | four AVM-restore hosts, fixed in the spec |
| CodeAct (`maf-sandbox-codeact`) | kind, shipped | no raise | assembled from the wired channels: `EXEC, FILES_IN`, plus `FILES_OUT` when outputs are collected, plus `HOST_TOOLS` **and** `FILES_OUT` when host tools are wired — the transport stats and reads its own request files back | `()` by default; the caller may name hosts |
| Monty-class restricted interpreters | backend | `runtime` | `RUN_CODE, HOST_TOOLS` — no `EXEC`, no I/O by construction | `closed` |
| Wasmtime-class WASM runtimes | backend | `runtime` | `RUN_CODE` + capability-gated imports | `closed` (WASI capabilities are opt-in) |
| Hyperlight ([`research/hyperlight-backend-proposal.md`](research/hyperlight-backend-proposal.md)) | backend *family* — declarations derive from the configured guest | `microvm`, measured against the standard on wasm × WHP | `RUN_CODE, FILES_IN, FILES_OUT, FILES_LIST, SNAPSHOT` (+`HOST_TOOLS` pending [#369](https://github.com/sokolaidev/maf-extensions/issues/369)) | `allowlist` — `allowed_domains` is native per-entry enforcement |
| [mxc](https://github.com/microsoft/mxc) | backend *family* | per containment | per containment | per containment |
| Docker Sandbox (the micro-VM product) | backend, dev machine | `microvm` | `EXEC, FILES_IN, FILES_OUT, NETWORK` | `allowlist` (deny-all proxy) |
| Kata on AKS | backend | `microvm` **only as configured** per the standard | `EXEC, FILES_IN` + image contents | per NetworkPolicy |

What the shipped rows enforce backend-side is in [`backends/README.md`](backends/README.md); the identity that each declaration implies is in [`hosts.md`](hosts.md); the surface all of them implement is in [`architecture.md`](architecture.md).

## Status

| Decision | State | Tracking |
|---|---|---|
| The isolation ladder, `ISOLATION_RANK`, the floor, and `deployed`'s removal | shipped | [#96](https://github.com/sokolaidev/maf-extensions/pull/96) (merged, `maf-sandbox` 0.5.0); bottom-rung rename [#331](https://github.com/sokolaidev/maf-extensions/pull/331) (merged, 0.14.0); the `os_process` rung [#347](https://github.com/sokolaidev/maf-extensions/pull/347) (merged, 0.16.0) |
| The capability axis — declaration, `DEFAULT_CAPABILITIES`, the match | shipped | [#96](https://github.com/sokolaidev/maf-extensions/pull/96) (merged); per-capability state in [`capabilities.md`](capabilities.md) |
| Egress honesty refuses an undeclared backend **as undeclared** (`Egress.UNDEFINED`) | shipped | [#521](https://github.com/sokolaidev/maf-extensions/pull/521) (merged; on `main`, unreleased) |
| `HOST_TOOLS` contract, transport and kind integration; `denied_capabilities` / `denied_identities` | shipped | [#133](https://github.com/sokolaidev/maf-extensions/issues/133) (open as the tracking issue; parts A–C landed) |
| Identity axis remainder — `IdentityScope`, `ATTACHED_IDENTITY` plumbing, the C′ prerequisites | open | [#395](https://github.com/sokolaidev/maf-extensions/issues/395) (open), [#396](https://github.com/sokolaidev/maf-extensions/issues/396) (open); body in [`hosts.md`](hosts.md) |
| The in-sandbox isolation conformance probe suite | parked | untracked — nearest adjacent is [#402](https://github.com/sokolaidev/maf-extensions/issues/402) (open), shared egress probes |
| A guest-platform axis a kind can declare and match | open — design settled in [`guest-platform-and-commands.md`](guest-platform-and-commands.md); nothing implemented | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (open) — deliberately unbuilt while every shipped backend runs a Linux guest |
