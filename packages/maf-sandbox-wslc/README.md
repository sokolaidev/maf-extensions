# maf-sandbox-wslc

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** This package is early-stage (pre-1.0, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxWslcExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_wslc.MafSandboxWslcExperimentalWarning)` once you've read the notice.

This package is not affiliated with, endorsed by, or a product of Microsoft — it is a third-party reference implementation of [microsoft/agent-framework#7568](https://github.com/microsoft/agent-framework/issues/7568) for [Microsoft Agent Framework](https://aka.ms/AgentFramework).

```
app  ->  maf_sandbox  ->  maf_sandbox_wslc  ->  the container
```

The developer-machine sandbox backend: a container created by `wslc`, the container CLI that ships with WSL, in about half a second — no subscription, no daemon, no login, and no dependency but [`maf-sandbox`](https://github.com/sokolaidev/maf-extensions/tree/main/packages/maf-sandbox) itself. A workload written against the protocol runs here unchanged, which is what makes it a workload rather than an integration.

## Quickstart

```bash
pip install maf-sandbox-wslc
```

```python
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

router = SandboxRouter([WslcSandboxBackend(WslcSandboxConfig())], min_isolation=Isolation.CONTAINER)
```

[`samples/02_wslc_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/02_wslc_bicep) runs those two lines end to end: a one-turn agent that validates a Bicep file against the compiler and takes the container down afterwards. Its sibling `samples/01_acas_bicep` is the same program on a microVM-isolated Azure backend, and the diff between them is two imports and one constructor.

## Requirements

**Windows with WSL 2.9.3 or later.** `wslc` is part of WSL; `wsl --version` reports the version and `wsl --update` moves it forward. There is nothing else to install. The command-line contract this backend depends on — argv passed to `exec` natively, `cp` from a tar on stdin, label filters on `list`, `WSLC_E_*` codes on stderr — was verified against **wslc 2.9.4.0**. Every call spawns `wslc.exe`, so the host's event loop has to be one that can start subprocesses — asyncio's default Proactor loop on Windows does, and a host that installs `WindowsSelectorEventLoopPolicy` has to undo that first, or every acquire fails with a message saying so.

## What this backend declares

**`Isolation.CONTAINER`.** A container shares the host kernel and sits next to whatever the host process holds, below `SandboxRouter`'s default `min_isolation=Isolation.MICROVM` floor — construct the router with `min_isolation=Isolation.CONTAINER` and it admits this backend; leave the floor at its default and construction raises `SandboxBackendNotPermitted`. That refusal is the feature: this is a backend for the machine you are already sitting at, and opting the floor down is the one thing that lets you use it — there is no flag left to forget. Use a microVM-isolated backend where a deployment's credentials are in the picture.

**`Egress.CLOSED` by default, `Egress.ALLOWLIST` on request.** With no proxy configured every container is created `--network none`: the CLI cannot allow one host and deny the rest, so a spec's allowlist is honoured by denying everything — confining *more* than a workload asked for, which the router permits with a warning precisely because the failure is loud, and a workload built for this reports the shortfall rather than passing an incomplete result off as a clean one.

Set `egress_proxy_image` and the declaration becomes `ALLOWLIST`: each sandbox gets its own internal network and a dual-homed filtering proxy, and the spec's allowlist is enforced by topology — the container has no route out except the proxy, which opens a CONNECT tunnel only to the hosts the spec names. TLS is not decrypted, and the sandbox never resolves an external name itself. The proxy is shipped as source, not as an image you must trust: build it from the packaged recipe, whose only pinned dependency is its Azure Linux base.

```python
from pathlib import Path
from maf_sandbox_wslc import proxy_build_context, WslcSandboxConfig

print(f"wslc build -t maf-egress-proxy:local {proxy_build_context()}")  # run this once
config = WslcSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
```

## The backend

`WslcSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A running container is reused, a stopped one started, a missing one created — so a fix-round loop does not pay a cold start per iteration |
| `write_file(path, content, *, working_directory)` | a confined one-entry tar on stdin to `cp - <container>:/`, which creates the parent directories from the entry name |
| `dispose(key)` | `remove -f` on the one container the key names |
| `dispose_scope(scope, thread)` | delete every container for a conversation — **by label, read back from wslc**, not from process memory |
| `isolation` | `container` — below the router's default `microvm` floor, so a host opts down explicitly with `min_isolation=Isolation.CONTAINER` |
| `declarations.egress_modes` | `{closed}`, or `{closed, allowlist}` when `egress_proxy_image` is set — an internal network behind a filtering proxy, torn down with the sandbox |
| `declarations.capabilities` | `{EXEC, FILES_IN}` — a command line and files written in; nothing more |

**The filesystem path check on a write is answered inside the guest — the file name check is host-side text arithmetic and is not, and that is the residual to know about before choosing this backend.** `write_file` refuses a path whose parents are links, which takes classifying every component from the filesystem root down. The `cp` tar header settles a directory and a missing path, and streams nothing for a regular file or a link — the two kinds that rule exists to catch — so those are settled by `test` run in the container being confined, through core's own `maf_sandbox.paths.stat_by_asking_the_guest_as_root`, which spells the probe and its ordering once so that no backend in this position invents a fourth version. The probe runs as `--user 0`, for the reason `reclaim` does: the file plane writes as root, so a probe as the image's user would be blind above a directory only root can search, and a `cp` still lands bytes there. Root is asked for reach and never for trust, and the helper checks that reach rather than assuming it, since a uid is not a capability set. A workload running as root can replace `test` in its own image and be believed, so the refusal is worth what the guest is. `maf-sandbox-docker` answers the same question out of its engine and this one has no equivalent until it can read an entry type without asking; [#495](https://github.com/sokolaidev/maf-extensions/issues/495) carries that decision.

Container names are derived from the key rather than remembered, so `acquire` and `dispose` agree on one without a registry to keep in sync. Labels are the durable record `dispose_scope` selects on, and their values are digested when they are long or carry a separator — the same mapping on both sides, because transforming one and not the other makes a purge quietly select nothing.

`stop` is never used. A container whose init process ignores `SIGTERM` takes ten seconds to stop and under a quarter of a second to remove, and there is nothing in a sandbox worth waiting for.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).

## Upgrading to 0.13

**The four optional declarations moved into one `BackendDeclarations`.** `maf-sandbox` 0.26 replaced `capabilities`, `limits`, `egress_modes` and `os_families` as backend attributes with one `declarations` object holding them as fields, and this backend follows it. A host that read them off the backend gets an `AttributeError`:

| Was | Is |
| --- | --- |
| `backend.capabilities` | `backend.declarations.capabilities` |
| `backend.egress_modes` | `backend.declarations.egress_modes` |

`limits` is not in that table because this backend never declared one — the router read its silence as `DEFAULT_SANDBOX_LIMITS`, and there was no `backend.limits` to read. `backend.declarations.limits` now answers with that same constant, so the ceiling is unchanged and the value is newly *reachable* rather than renamed.

Nothing about what this backend declares changed — the values, and how they are derived from the config, are exactly as they were. `maf-sandbox`'s own README carries the reasoning and what a backend author has to do.
