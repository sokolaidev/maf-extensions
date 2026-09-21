# maf-sandbox-wslc

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-wslc)](https://pypi.org/project/maf-sandbox-wslc/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/LICENSE)

> **Experimental.** Releases before 1.0 may change or remove APIs. Importing this package emits `MafSandboxWslcExperimentalWarning`.

Run sandbox commands in Linux containers managed by `wslc`, the container CLI included with WSL. This backend transfers input files and returns command output. It has no Azure dependency.

This is an independent package, not a Microsoft product.

## Quickstart

```bash
pip install maf-sandbox-wslc
```

```python
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

backend = WslcSandboxBackend(WslcSandboxConfig())
router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
```

Containers share the WSL kernel. The explicit minimum permits container isolation, below the router's default microVM minimum.

Use Windows with WSL 2.9.3 or later. `wsl --version` reports the installed version. The backend's CLI contract has been measured on WSLC 2.9.4.0 and 2.9.12.0.

The Python event loop must support subprocesses. Windows' default Proactor loop does; `WindowsSelectorEventLoopPolicy` does not.

See the [Bicep sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/02_wslc_bicep) or [CodeAct sample](https://github.com/sokolaidev/maf-extensions/tree/main/samples/04_wslc_codeact) for complete applications.

## Supported operations

| Setting | Behavior |
|---|---|
| Isolation | `CONTAINER` |
| Capabilities | `EXEC`, `FILES_IN` |
| Guest OS | POSIX |
| Network | `CLOSED`; `ALLOWLIST` with a configured proxy |
| Lifetime | Conversation or separate sandbox per call |
| Transfer ceiling | 8 MiB per file, 32 MiB total, 64 files per direction |
| Cleanup | Disposal; no reclaim or snapshot reset |

Output reads, directory listing, file deletion, runtime `run_code` and host-tool calls are unavailable. A kind requiring one is refused before attachment.

Acquisition checks `sh` for commands. Input transfer also needs the external `/usr/bin/test` command, `mkdir`, `cat`, `wc`, `mv` and `rm` for the image user, and a resolved image user. Both acquisition and root path probes invoke that absolute executable, without searching the guest's `PATH`. Failed prerequisite checks are retryable; another `test` on `PATH` is not a fallback. The image must protect `/usr/bin/test`, its dependencies and ancestor directories from the runtime user.

## Input files and their limits

Acquisition prepares the storage base for workloads using commands or files. `work_dir=None` selects `/maf-sandbox/work`; an explicit path requests that exact base. Existing directories keep their contents, ownership and modes.

A missing base is created as root, and the base itself goes to the image user. An unset `Config.User` means root. Named users or an omitted group need working `id` commands. Unresolved ownership refuses `FILES_IN`.

**Writes run as the image user.** The file and any missing parents belong to that user. A destination it cannot write raises `PermissionError`; nothing falls back to root. The path check is separate from the write, so a guest can swap a checked parent for a symlink first. The write then reaches only what the image user could write anyway.

Setup refuses such a swap. It creates each missing directory inside a directory it holds and has confirmed with `pwd -P`. Cancelling a write after it starts is not a rollback: short content is refused, but content that fully arrived still lands.

Some path classification runs inside the guest, as root, with the image's `test`. Its answer can pick which refusal a caller sees. A write it lets through still runs as the image user.

Path inspection can also copy an existing guest file into a private host temporary directory. **Its disk use is not bounded by input limits or stdout limits.** Normal exits remove the temporary copy, but a host crash or cleanup failure can leave it behind. Use an enforced temporary-filesystem quota when that consumption is unacceptable.

See the [backend guide](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/backends/wslc.md) for the precise file contract and remaining limits.

## Network access

Without a proxy, only `Egress.CLOSED` is supported. Containers use `--network none`, and an allowlist request is refused.

Build the packaged proxy and select it in configuration:

```python
from maf_sandbox_wslc import WslcSandboxConfig, proxy_build_context

print(f"wslc build -t maf-egress-proxy:local {proxy_build_context()}")
config = WslcSandboxConfig(egress_proxy_image="maf-egress-proxy:local")
```

Each allowlisted sandbox gets an internal network and a filtering proxy. The proxy is its only route out and permits only the spec's hosts. TLS is not decrypted. Unrestricted access and method-scoped rules are unsupported.

The router's observer can receive proxy decisions after confirmed removal. Failed removal can leave a window unreported. See [observability](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/observability.md).

<a id="operator-retention"></a>

## Cleanup and retention

Router-managed calls dispose their sandbox even when a kind claims to keep changes inside its call directory. WSLC cannot establish the ancestry needed for privileged reclaim and declares no snapshot reset.

`dispose(key, kind=...)` removes a selected kind and its infrastructure. `dispose_scope(scope, thread_id)` discovers conversation resources through engine labels, not only process memory.

An operator can clean up stopped workloads and orphan infrastructure:

```python
from datetime import timedelta

result = await backend.reap(timedelta(hours=24), scope="my-app")
print(result.disposed, result.proxies_removed, result.networks_removed)
for failure in result.failures:
    print(failure)
```

Run this under the Windows account that owns the WSLC engine. The backend starts no timer. An external scheduler must select the environment, prevent overlapping sweeps and monitor failures.

Pause and drain acquisition, restarts and other resource changes in the selected scope before sweeping. Engine rechecks cannot make name-based network deletion atomic. Keep Windows and WSL clocks synchronized.

| Resource | Age used for retention |
|---|---|
| Stopped workload | Time since its inspected stop; a restart and stop resets it. |
| Never-started workload | Creation time. |
| Proxy without a workload | Creation time, including for a running orphan proxy. |
| Network alone | Its backend creation-time label; missing labels retain it for manual handling. |

Running or uncertain workload states retain their whole group. Resources exactly at the cutoff are retained. An incomplete inventory prevents deletion; individual failures are reported. Retention does not infer that a host has died or impose a maximum running lifetime.

See [operations](https://github.com/sokolaidev/maf-extensions/blob/main/docs/sandbox/operations.md) for scheduling and cleanup ownership. The [live suite](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-wslc/tests/test_wslc_e2e.py) covers writes, networks and separate-process retention using explicitly configured images.

Maintained by [SOKOLAI BV](https://www.sokol.ai).
