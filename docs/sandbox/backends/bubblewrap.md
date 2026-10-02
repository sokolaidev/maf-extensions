# Bubblewrap

`maf-sandbox-bubblewrap` runs a trusted Linux runtime directory inside private namespaces with mandatory cgroup v2 limits. It needs no Docker daemon, OCI image or container engine. See the [package README](../../../packages/maf-sandbox-bubblewrap/README.md) for configuration and delegation prerequisites.

| Setting | Supported contract |
|---|---|
| Host | Linux; tested on Ubuntu 24.04 under WSL 2 |
| Isolation | `CONTAINER`, meaning namespaces plus cgroups; explicit host opt-in |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT` |
| Network | `CLOSED`; no host network or sockets mounted |
| Guest | POSIX root directory containing Python 3 and the workload's native dependencies |
| Runtime selection | `image=None` or the configured `runtime_id`; no `image_id` |
| Storage | Private bounded tmpfs, base `/maf-sandbox/work` |
| Resource limits | Aggregate memory, swap disabled, PID count, CPU quota and tmpfs size |
| Transfers | 8 MiB per file; default router collection ceilings |
| Cleanup | Disposal; command descendants reaped before reply; timeout/cancellation destroys the sandbox |
| Ownership | Full key including call and kind, private persisted records, exclusive host file lock |

The runtime is read-only and the environment is cleared. The private PID 1 broker holds no-follow descriptors during file operations, at guest filesystem authority. It reports guest metadata rather than a host filesystem attestation. Nested user namespaces are disabled. The boundary shares the host kernel; no seccomp filter or microVM protection is claimed. Other platform policies are not silently substituted when Linux prerequisites fail.

Acquisition creates a fresh namespace after an owner dies; it does not recover the lost tmpfs contents. Scope disposal also discovers records from previous owners and reports locked or failed instances without deleting their records. Stop new work across host replicas before a scope purge. The host's default `MICROVM` floor remains authoritative; route native rendering separately when other workloads require it.

## Qualification

Provision the [offline Draw.io runtime](../../../images/drawio-export/README.md) once, then run the verifier as an unprivileged user inside the configured cgroup delegation:

```bash
uv run python scripts/check_bubblewrap_exports.py --runtime /opt/maf-runtime --state /var/lib/my-service/sandbox-state --cgroup /sys/fs/cgroup/my-service --output out/native-exports
```

The backend tests run real execution, file conformance, network denial, namespace restrictions, cgroup limits, cancellation, owner death, descendant cleanup and ownership checks when these environment variables are explicitly set. With no provisioned runtime they skip the live tests:

```bash
export MAF_BWRAP_RUNTIME=/opt/maf-runtime
export MAF_BWRAP_STATE=/var/lib/my-service/sandbox-state
export MAF_BWRAP_CGROUP=/sys/fs/cgroup/my-service
uv run pytest packages/maf-sandbox-bubblewrap/tests -q
```

Candidate-specific measurements and remaining platform work are recorded in the [Draw.io decision record](../research/drawio-export.md). The default repository gate does not provision this privileged host delegation or qualify every Linux distribution.

## Status

| Area | State | Reference |
|---|---|---|
| Native Linux rendering | Implemented; candidate qualification recorded | [#1656](https://github.com/sokolaidev/maf-extensions/issues/1656) (closed) by [#1667](https://github.com/sokolaidev/maf-extensions/pull/1667) (merged); [decision record](../research/drawio-export.md) |
| Windows and macOS native rendering | Not implemented | untracked; separate platform qualification required |
