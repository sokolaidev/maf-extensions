# maf-sandbox-bubblewrap

[![PyPI](https://img.shields.io/pypi/v/maf-sandbox-bubblewrap)](https://pypi.org/project/maf-sandbox-bubblewrap/) [![Python](https://img.shields.io/pypi/pyversions/maf-sandbox-bubblewrap)](https://pypi.org/project/maf-sandbox-bubblewrap/) [![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sokolaidev/maf-extensions/blob/main/packages/maf-sandbox-bubblewrap/LICENSE)

> **Experimental.** Not yet released. `maf-sandbox-bubblewrap` may change or be removed without notice and emits `MafSandboxBubblewrapExperimentalWarning` on import.

Experimental Linux sandbox backend using Bubblewrap and cgroup v2, without Docker, an OCI image or a container engine. The runtime is a separately provisioned, trusted Linux root directory. Runtime downloads never happen during a tool call.

## Prerequisites

- Linux with unprivileged user, mount, PID, network, IPC, UTS and cgroup namespaces; Bubblewrap supporting `--disable-userns`, `--assert-userns-disabled`, `--as-pid-1` and bounded tmpfs mounts. Missing support refuses startup.
- A private cgroup v2 delegation with `cpu`, `memory` and `pids` enabled, `memory.swap.max` and `cgroup.kill`. The host process must already belong to that delegation so it can move children into sibling groups. A systemd service with `Delegate=cpu memory pids` can provide it; the host must place itself in a leaf and enable the controllers on its empty parent. This package does not grant privileges or change host-wide policy.
- A trusted runtime directory containing `/usr/bin/python3` and `/bin/sh`, with no host credentials or sockets. Keep it immutable while sandboxes run. Runtime, cgroup and state paths and their ancestors must be controlled by the host administrator. The state directory must be owned by the service user and mode `0700`.

## Use

```python
from pathlib import Path
from maf_sandbox import Isolation, SandboxRouter
from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig

async def make_router() -> SandboxRouter:
    backend = await BubblewrapSandboxBackend.create(BubblewrapSandboxConfig(
        runtime_root=Path("/opt/maf-runtime"),
        state_root=Path("/var/lib/my-service/sandbox-state"),
        cgroup_root=Path("/sys/fs/cgroup/my-service"),
        runtime_id="drawio-native",
    ))
    return SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
```

`SandboxSpec.image` is an optional exact match against the configured `runtime_id`, not an OCI reference. `image_id` is unsupported. The storage base is `/maf-sandbox/work`; only that explicit override or `None` is accepted. Relative working directories address the base. Commands are opaque; a string runs through `/bin/sh -c`, while a sequence executes directly.

The protocol's `Isolation.CONTAINER` means Linux namespaces plus cgroups. It describes the boundary even though no container engine is used. The router's default `MICROVM` floor still refuses this backend. Keep a separate router or use per-spec routing for a native renderer alongside a stronger CodeAct backend; never lower a shared host floor implicitly.

## Boundary and lifecycle

Every sandbox gets mandatory private namespaces, no capabilities, disabled nested user namespaces, a new session and a cleared environment. The runtime is read-only. `/tmp`, `/run` and `/maf-sandbox` are separate bounded tmpfs mounts; aggregate memory, swap, process count and CPU are constrained before any guest starts. Default limits are 1 GiB memory, no swap, 256 processes, two CPU equivalents and 128 MiB per writable mount. No host home, display, network or cgroup filesystem is mounted. Only `Egress.CLOSED`, POSIX `EXEC`, `FILES_IN` and `FILES_OUT` are declared. This is a shared-kernel boundary, not a microVM or a claim of protection against kernel exploits. No seccomp syscall filter is currently supplied.

A private PID 1 broker uses no-follow directory descriptors for transfers. It runs at guest filesystem authority; its stat responses are guest-side metadata, not host attestations. Its control descriptors are protected from ptrace, and commands receive separate stdin/stdout/stderr. Individual transfers are capped at 8 MiB; the router applies its collection count and aggregate byte limits. Combined command output is capped at 1 MiB by default; excess output fails explicitly. A command's descendants, including detached processes, are killed before its reply. Timeout or cancellation destroys the entire sandbox. There is no reclamation or snapshot capability; the router uses disposal.

Key identity includes scope, thread, agent, call and kind. An exclusive host file lock prevents another backend object or process from acquiring a live instance. Repeated acquire within one backend can reuse a matching spec; another owner is refused. Files live only in tmpfs and disappear on owner death. Records allow a subsequent host to purge dead owners' cgroups; this is cleanup recovery, not restoration of guest state. Stale instance disposal cannot delete a replacement. A scope purge reports busy or failed records and retains them for retry. Hosts must stop new work across replicas before purging a conversation.

Lock files remain after disposal so a concurrent owner cannot lock a replacement inode under the same name. Remove the state directory only after stopping all owners; long-lived deployments should account for those small files in retention policy.

Ownership records are published atomically before cgroup creation, so a host that dies during startup leaves either no cgroup or a complete cleanup record. Failed setup removes its record after cgroup cleanup succeeds. Commands whose requested timeout exceeds `max_timeout` (180 seconds by default) are rejected with `ValueError` before execution; accepted deadlines are not shortened.

## Offline Draw.io

The [runtime provisioning script](https://github.com/sokolaidev/maf-extensions/blob/main/images/drawio-export/build-runtime.sh) uses debootstrap and the pinned Draw.io Desktop package directly, with the same offline assets, fonts and exporter as the image profile. Provisioning needs network access and root; rendering needs neither. Run the host outside that runtime and register this backend with `maf-sandbox-drawio`, passing the configured runtime id as `image`. Linux is the only supported host platform; native Windows and macOS backends are separate work.
