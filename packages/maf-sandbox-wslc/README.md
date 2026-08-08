# maf-sandbox-wslc

> **Experimental.** This package is early-stage (`0.1.0`, `Development Status :: 4 - Beta`) — its API may change or be removed in a future release without notice. Importing it emits a one-time `MafSandboxWslcExperimentalWarning`; suppress it with `warnings.filterwarnings("ignore", category=maf_sandbox_wslc.MafSandboxWslcExperimentalWarning)` once you've read the notice.

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
from maf_sandbox import SandboxRouter
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

router = SandboxRouter([WslcSandboxBackend(WslcSandboxConfig())])
```

[`samples/02_wslc_bicep`](https://github.com/sokolaidev/maf-extensions/tree/main/samples/02_wslc_bicep) runs those two lines end to end: a one-turn agent that validates a Bicep file against the compiler and takes the container down afterwards. Its sibling `samples/01_acas_bicep` is the same program on a VM-isolated Azure backend, and the diff between them is two imports and one constructor.

## Requirements

**Windows with WSL 2.9.3 or later.** `wslc` is part of WSL; `wsl --version` reports the version and `wsl --update` moves it forward. There is nothing else to install. The command-line contract this backend depends on — argv passed to `exec` natively, `cp` from a tar on stdin, label filters on `list`, `WSLC_E_*` codes on stderr — was verified against **wslc 2.9.4.0**.

## What this backend declares

**`Isolation.CONTAINER`.** A container shares the host kernel and sits next to whatever the host process holds, so `SandboxRouter(..., deployed=True)` refuses this backend outright, at construction. That refusal is the feature: this is a backend for the machine you are already sitting at, and the router will not be argued into treating it as anything else. Use a VM-isolated backend where a deployment's credentials are in the picture.

**`Egress.CLOSED`.** Every container is created `--network none`, and nothing in the configuration can widen it. The CLI cannot allow one host and deny the rest, so a spec's allowlist is honoured by denying everything — confining *more* than a workload asked for, which the router permits with a warning precisely because the failure is loud: whatever the workload could not fetch, it could not fetch, and a workload built for this reports the shortfall rather than passing an incomplete result off as a clean one.

Allowlisted egress is a known follow-up rather than an oversight. The topology is already verified — an internal network isolates a container from the internet, and a second container attached to both networks reaches it — so what is missing is an allowlisting proxy image to put on the dual-homed hop, not a mechanism.

## The backend

`WslcSandboxBackend` implements `maf_sandbox.SandboxBackend`:

| | |
|---|---|
| `acquire(key, spec)` | get-or-create, keyed `(scope, thread, agent)`. A running container is reused, a stopped one started, a missing one created — so a fix-round loop does not pay a cold start per iteration |
| `write_file(path, content)` | a one-entry tar on stdin to `cp - <container>:/`, which creates the parent directories from the entry name |
| `dispose(key)` | `remove -f` on the one container the key names |
| `dispose_scope(scope, thread)` | delete every container for a conversation — **by label, read back from wslc**, not from process memory |
| `isolation` | `container` — which is what makes the router refuse it in a deployed environment |

Container names are derived from the key rather than remembered, so `acquire` and `dispose` agree on one without a registry to keep in sync. Labels are the durable record `dispose_scope` selects on, and their values are digested when they are long or carry a separator — the same mapping on both sides, because transforming one and not the other makes a purge quietly select nothing.

`stop` is never used. A container whose init process ignores `SIGTERM` takes ten seconds to stop and under a quarter of a second to remove, and there is nothing in a sandbox worth waiting for.

---

Maintained by [SOKOLAI BV](https://www.sokol.ai).
