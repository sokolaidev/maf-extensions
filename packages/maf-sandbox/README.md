# maf-sandbox

> **Experimental.** This package is early-stage (`0.1.0`, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox.MafSandboxExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568), written for use with [Microsoft Agent Framework](https://aka.ms/AgentFramework) but with no dependency on it in its protocol layer.

## Quickstart

```bash
pip install maf-sandbox
```

```python
from maf_sandbox import Isolation, SandboxKey, SandboxRouter, SandboxSpec, WorkspaceContext

# Implement SandboxBackend against your own provider — or install maf-sandbox-aca for a
# ready-made Azure Container Apps Sandboxes backend — then wire it into a router:
router = SandboxRouter([my_backend], deployed=False)
sandbox = await router.acquire(SandboxKey(scope="tenant-1", thread_id="t-1", agent_dir="devops"), SandboxSpec(kind="bicep", image="bicep-sandbox:0.46.1", egress_allow=("mcr.microsoft.com",), work_dir="/workspace"))
```

[`samples/01_acas_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/01_acas_bicep) is that wiring as a runnable program, including the part no snippet shows well: building the `WorkspaceContext` out of callables rather than values, which is what keeps a `SandboxKey` a property of the host's request.

## Threat model

This package draws no isolation boundary itself — it is protocol and policy over whatever a `SandboxBackend` implementation actually provides. `Isolation` states three tiers a backend can declare, from strongest to weakest: `vm` (a VM boundary — the whole guest, not just a process, is untrusted), `container` (a shared-kernel boundary), and `process` (no boundary beyond the OS's own process isolation). `SandboxRouter` enforces the one rule below on top of that declaration; the package's job is to make an unsafe backend selection fail loudly at construction, not silently at first use. Beyond backend selection, this layer has nothing else to get wrong: it holds no credentials, executes nothing, and reaches no network — everything security-relevant about a *specific* sandbox lives in the backend that implements it.

## The vocabulary

| | |
|---|---|
| `SandboxKey` | `(scope, thread_id, agent_dir)` — the one sandbox a caller may reach |
| `SandboxSpec` | what a sandbox of a given *kind* needs: image, egress allowlist, work dir |
| `Sandbox` | `write_file` + `exec` — all a workload gets |
| `SandboxBackend` | `acquire` / `dispose` / `dispose_scope` |
| `SandboxRouter` | picks the backend, enforces the deployed rule |
| `SandboxPurger` | duck-typed `purge_scoped_thread(scope, thread_id)` for a host's delete path |

`SandboxKey`'s scope and thread come from the host's request context through `WorkspaceContext`, whose fields are **callables read at call time** rather than values. That is deliberate: a key a caller can supply is a key a *model* can supply, and that would let one conversation address another's sandbox.

`SandboxSpec.egress_allow` is an allowlist — everything not named is denied, so an empty tuple means no network. Stating it positively means a spec that forgets to mention egress gets the closed configuration rather than the open one.

## The two rules that are not conveniences

```python
DEPLOYED_ISOLATION = frozenset({Isolation.VM})
```

A backend declares its own `isolation` (`vm` / `container` / `process`). When the host reports it is running **deployed**, the router refuses to select anything weaker than a VM boundary — raising `SandboxBackendNotPermitted` at construction, not at first use, so a misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe.

It refuses rather than degrades. Falling back to a stronger backend would hide a misconfiguration; proceeding with the weaker one would break claims the host's security posture makes about every execution surface. Neither is better than an error.

A hardened container runtime (gVisor, Kata, Firecracker) is deliberately *not* in the permitted set. Admitting one is a decision for whoever owns those posture claims, taken there first.

The second rule is about egress, and it exists because `egress_allow` was a contract nothing checked. A backend that reads it and one that ignores it have the same type, the same methods and the same passing tests, so each one declares an `Egress` level — `allowlist` (deny by default, allow the named hosts), `closed` (all or nothing), or `unrestricted` (cannot confine egress at all) — and `SandboxRouter.ensure_can_serve(spec)` refuses the last one where a workload attaches its tool. That is the first moment a backend and a spec are both in hand; the router is built before any workload exists.

Which direction a backend misses by decides the outcome, and it is not symmetrical. A backend that confines **less** than the spec asks silently widens what the workload was designed to reach. One that confines **more** is permitted, with a warning: the sandbox reaches nothing it should not, and the workload fails visibly at whatever it could not fetch.

Note that the two rules answer to different owners. How strong the boundary must be is the *host's* policy, read from `deployed`. What a sandbox may reach is a property of the *workload*, stated in its spec. Merging them into one "required capabilities" list would let a workload ask for a weaker boundary than the deployment mandates.

`ensure_can_serve` is also the whole of a wiring test, in your own repository, against your own backend choice:

```python
router.ensure_can_serve(bicep_sandbox_spec())
```

## Writing a backend

Implement `name`, `isolation`, `egress`, `acquire`, `dispose`, `dispose_scope`. Three things worth knowing before you start:

**Declare `egress` honestly.** It is read before any workload's tool is attached, and a backend that omits it is treated as `unrestricted` and refused — a backend written before the property existed cannot have been enforcing an allowlist it never read, so the closed reading is the true one.

**`acquire` is get-or-create.** A workload's fix-round loop calls it every iteration; returning a cold sandbox each time turns a seconds-long loop into a minutes-long one.

**`dispose_scope` must not consult only your process's memory.** A multi-replica host serves a conversation delete wherever it lands, so the replica that created the sandbox is usually not the one deleting it. Derive the set from the service — labels, a listing, whatever your provider offers. A backend that skips this leaves billable compute running and the bug is invisible on a single-replica dev box.

Both `dispose` methods are best-effort by contract: purge must never fail a delete.

## Provenance

Extracted from a production agent application, where this seam was written for its first execution surface: a tool that compiles agent-authored infrastructure code in a sandbox. The deployed-isolation rule above is not a preference — it is what a security review concluded when it worked through what a shared-kernel boundary does *not* close for code an agent wrote.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
