# sandbox-router

One seam between an application and any sandbox provider.

```
app  ->  SandboxRouter  ->  backend  ->  the sandbox
```

A **workload** asks for a sandbox and runs a command in it. A **backend** decides what actually boots. Neither knows about the other, which is what lets the same tool run against an Azure Container Apps Sandbox, a local Docker container, or an in-process fake without changing a line.

This package has **no dependencies** — not on a backend, not on an agent framework, not on a host application. It is protocol and policy; giving it a dependency would make it the thing it exists to keep apart.

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

## The one rule that is not a convenience

```python
DEPLOYED_ISOLATION = frozenset({Isolation.VM})
```

A backend declares its own `isolation` (`vm` / `container` / `process`). When the host reports it is running **deployed**, the router refuses to select anything weaker than a VM boundary — raising `SandboxBackendNotPermitted` at construction, not at first use, so a misconfigured deployment cannot start with the feature apparently enabled and quietly unsafe.

It refuses rather than degrades. Falling back to a stronger backend would hide a misconfiguration; proceeding with the weaker one would break claims the host's security posture makes about every execution surface. Neither is better than an error.

A hardened container runtime (gVisor, Kata, Firecracker) is deliberately *not* in the permitted set. Admitting one is a decision for whoever owns those posture claims, taken there first.

## Writing a backend

Implement `name`, `isolation`, `acquire`, `dispose`, `dispose_scope`. Two things worth knowing before you start:

**`acquire` is get-or-create.** A workload's fix-round loop calls it every iteration; returning a cold sandbox each time turns a seconds-long loop into a minutes-long one.

**`dispose_scope` must not consult only your process's memory.** A multi-replica host serves a conversation delete wherever it lands, so the replica that created the sandbox is usually not the one deleting it. Derive the set from the service — labels, a listing, whatever your provider offers. A backend that skips this leaves billable compute running and the bug is invisible on a single-replica dev box.

Both `dispose` methods are best-effort by contract: purge must never fail a delete.

## Provenance

Built for issue [#663](https://github.com/sokolaidev/ats-maf/issues/663) of the application this currently ships inside, extracted from the first execution surface that shipped ([#408](https://github.com/sokolaidev/ats-maf/issues/408), `bicep_validate`). The reasoning behind the deployed-isolation rule is in that repository under `docs/work-in-progress/issue-408-exec-surface-security.md` — specifically §1, the escalation chain that a shared-kernel boundary does not close.
