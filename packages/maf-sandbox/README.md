# maf-sandbox

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox.MafSandboxExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568), written for use with [Microsoft Agent Framework](https://aka.ms/AgentFramework) but with no dependency on it in its protocol layer.

## Quickstart

```bash
pip install maf-sandbox
```

```python
from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec, WorkspaceContext

# Implement SandboxBackend against your own provider — or install maf-sandbox-acas for a
# ready-made Azure Container Apps Sandboxes backend — then wire it into a router. Configuring
# nothing gets the production posture (the default floor is Isolation.MICROVM); a developer
# machine opts down explicitly:
router = SandboxRouter([my_backend], min_isolation=Isolation.CONTAINER)
sandbox = await router.acquire(SandboxKey(scope="tenant-1", thread_id="t-1", agent_dir="devops"), SandboxSpec(kind="bicep", image="bicep-sandbox:0.46.1", egress_allow=("mcr.microsoft.com",), work_dir="/workspace"))
```

This snippet never calls `ensure_can_serve` (below) and is checked anyway: `acquire` runs the same floor, capability and egress refusals itself before it ever reaches the backend, so the only thing calling `ensure_can_serve` first buys you is the closed-egress-vs-allowlist-spec warning, which `acquire` deliberately stays silent about.

[`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) is that wiring as a runnable program, including the part no snippet shows well: building the `WorkspaceContext` out of callables rather than values, which is what keeps a `SandboxKey` a property of the host's request.

## Threat model

This package draws no isolation boundary itself — it is protocol and policy over whatever a `SandboxBackend` implementation actually provides. `Isolation` is a six-rung ladder a backend declares itself onto, weakest to strongest: `process` (no boundary at all — same process as the host), `runtime` (a software boundary inside the host process, e.g. a restricted interpreter or a WASM runtime's fault isolation), `container` (shared-kernel namespaces and cgroups), `hardened_container` (syscall interception in a userspace kernel — gVisor-class), `microvm` (a hypervisor boundary with a minimal or absent guest OS and no ambient identity reachable from inside — the default floor), and `vm` (a dedicated, full VM provisioned for the workload). `SandboxRouter` enforces the checks below on top of that declaration; the package's job is to make an unsafe backend selection fail loudly at construction or attach, not silently at first use. Beyond backend selection, this layer has nothing else to get wrong: it holds no credentials, executes nothing, and reaches no network — everything security-relevant about a *specific* sandbox lives in the backend that implements it.

## The vocabulary

| | |
|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir)` — the one sandbox a caller may reach |
| `SandboxSpec` | what a sandbox of a given *kind* needs: image, egress allowlist, work dir, `requires` capabilities, and an optional `min_isolation` that may raise the host's floor |
| `Sandbox` | `write_file` + `exec` — all a workload gets |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope`, plus the `isolation`, `egress` and `capabilities` it declares |
| `SandboxRouter` | picks the backend, enforces the minimum-isolation floor, the capability match, and the egress rule |
| `SandboxPurger` | duck-typed `purge_scoped_thread(scope, thread_id)` for a host's delete path |

`Isolation`, weakest to strongest: `process < runtime < container < hardened_container < microvm < vm`. `SandboxRouter`'s default `min_isolation` is `microvm`; an unrecognised rung refuses rather than guesses which side of the floor it falls on.

`SandboxKey`'s scope and thread come from the host's request context through `WorkspaceContext`, whose fields are **callables read at call time** rather than values. That is deliberate: a key a caller can supply is a key a *model* can supply, and that would let one conversation address another's sandbox.

`SandboxSpec.egress_allow` is an allowlist — everything not named is denied, so an empty tuple means no network. Stating it positively means a spec that forgets to mention egress gets the closed configuration rather than the open one.

## Two axes, three checks that are not conveniences

```python
router = SandboxRouter(backends)                                   # default floor: Isolation.MICROVM
router = SandboxRouter(backends, min_isolation=Isolation.VM)       # stricter: dedicated full-VM only
router = SandboxRouter(backends, min_isolation=Isolation.PROCESS)  # a developer machine, opted down
```

**1. The minimum-isolation floor.** A backend declares its own `isolation`, ranked on the ladder above. The router refuses, at construction, any backend below `min_isolation` — or one whose declared value is not a rung this package recognises, because nothing here can tell whether an unrecognised boundary is stronger or weaker than the floor. A spec may also carry its own `min_isolation`; the effective floor is the *stricter* of the host's and the spec's — a spec may raise the floor for itself and never lower it.

It refuses rather than degrades. Falling back to a stronger backend would hide a misconfiguration; proceeding with the weaker one would break claims the host's security posture makes about every execution surface. Neither is better than an error.

**2. The capability match.** A backend declares `capabilities: frozenset[Capability]` (`EXEC`, `RUN_CODE`, `HOST_TOOLS`, `FILES_IN`, `FILES_OUT`, `NETWORK`, `SNAPSHOT`, `ATTACHED_IDENTITY`) — what it can actually do — and a spec declares `requires`, what its workload cannot run without. `ensure_can_serve(spec)` raises `SandboxCapabilityNotSupported` when the backend is missing something the spec requires. Unlike the floor, silence here is a functionality claim rather than a safety one: an undeclared `capabilities` reads as exactly `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` — what this package's own `Sandbox` protocol already obligates, so a backend written before `Capability` existed does not have to start lying to keep working.

**3. The egress rule**, unchanged in substance. `egress_allow` was a contract nothing checked, so a backend that reads it and one that ignores it have the same type, the same methods and the same passing tests — each one declares an `Egress` level instead: `allowlist` (deny by default, allow the named hosts), `closed` (all or nothing), or `unrestricted` (cannot confine egress at all). `ensure_can_serve(spec)` refuses the last one. Here silence is *not* read charitably: an undeclared `egress` is treated as `unrestricted` and refused, because a backend written before the property existed cannot have been enforcing an allowlist it never read.

Which direction a backend misses egress by decides the outcome, and it is not symmetrical. A backend that confines **less** than the spec asks silently widens what the workload was designed to reach — refused. One that confines **more** is permitted, with a warning: the sandbox reaches nothing it should not, and the workload fails visibly at whatever it could not fetch.

Note that the checks answer to different owners. How strong the boundary must be *here* is the *host's* policy, read from `min_isolation` — and a spec may raise that floor for itself, never lower it. What a sandbox may reach, and what it must be able to do, are properties of the *workload*, stated in its spec. Keeping the axes apart is deliberate: merging isolation into a "required capabilities" list would let a workload ask for a weaker boundary than the deployment mandates.

`ensure_can_serve` is also the whole of a wiring test, in your own repository, against your own backend choice:

```python
router.ensure_can_serve(bicep_sandbox_spec())
```

## Upgrading from 0.4.x

`0.5.0` replaced the `deployed` boolean with a declared isolation floor, and added a capability axis. Four changes need an edit; nothing else moves.

**`SandboxRouter(..., deployed=...)` is gone — pass `min_isolation` instead.** `deployed=True` becomes `min_isolation=Isolation.MICROVM`, which is also the default, so a deployed host can drop the argument entirely. `deployed=False` on a developer machine becomes the rung that host actually accepts, stated explicitly — `min_isolation=Isolation.CONTAINER` for a container backend, `Isolation.PROCESS` for an in-process fake. There is no longer a value meaning "anything goes": a host that wants the weakest rung names it.

**`DEPLOYED_ISOLATION` is removed.** The policy it expressed is `min_isolation`'s default.

**`Isolation` and `Egress` are `StrEnum`s, and the ladder grew.** Values are unchanged, so `backend.isolation == "vm"` and any stored configuration keep working. The ladder is now `process < runtime < container < hardened_container < microvm < vm`; a declared value outside it is refused at construction rather than silently permitted.

**`AcasSandboxBackend` now declares `microvm`, not `vm`.** ACA Sandboxes are hardware-isolated micro-VMs; `vm` now means a dedicated, full VM on remote infrastructure. A host that pinned `min_isolation=Isolation.VM` expecting ACA Sandboxes to satisfy it should use `Isolation.MICROVM` — the default, and the rung the micro-VM standard defines.

Backends need no edit to keep working: one that declares no `capabilities` is read as declaring `DEFAULT_CAPABILITIES` (`exec` + `files_in`), which is what the `Sandbox` protocol already obliges. Declare a wider set to serve workloads that require more.

## Writing a backend

Implement `name`, `isolation`, `egress`, `acquire`, `dispose`, `dispose_scope`. `capabilities` is optional. Four things worth knowing before you start:

**Declare `egress` honestly.** It is read before any workload's tool is attached, and a backend that omits it is treated as `unrestricted` and refused: one written before the property existed cannot have been enforcing an allowlist it never read, so silence is read as enforcing nothing rather than excused.

**`capabilities` is optional, and silence is the opposite of `egress`'s.** Omitting it reads as `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}`, so a backend that has always offered exec and file-write does not need to add the property to keep working — only declare it once you offer more, or less.

**`acquire` is get-or-create.** A workload's fix-round loop calls it every iteration; returning a cold sandbox each time turns a seconds-long loop into a minutes-long one.

**`dispose_scope` must not consult only your process's memory.** A multi-replica host serves a conversation delete wherever it lands, so the replica that created the sandbox is usually not the one deleting it. Derive the set from the service — labels, a listing, whatever your provider offers. A backend that skips this leaves billable compute running and the bug is invisible on a single-replica dev box.

Both `dispose` methods are best-effort by contract: purge must never fail a delete.

## Provenance

Extracted from a production agent application, where this seam was written for its first execution surface: a tool that compiles agent-authored infrastructure code in a sandbox. The minimum-isolation floor above is not a preference — it is what a security review concluded when it worked through what a shared-kernel boundary does *not* close for code an agent wrote.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
