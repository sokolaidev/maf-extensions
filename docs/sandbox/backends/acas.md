# ACAS

ACAS runs Linux microVMs in Azure Container Apps Sandboxes. The host selects a sandbox group and image. The backend manages execution, files, network policy and disposal through the service API.

Use the [package README](../../../packages/maf-sandbox-acas/README.md) for installation and configuration, and [ACAS credentials](acas-credentials.md) for host authentication.

## Supported contract

| Setting | Value |
|---|---|
| Isolation | `MICROVM` |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_LIST`, `FILES_DELETE`, `HOST_TOOLS` |
| Network | `CLOSED`, host `ALLOWLIST`; no egress observation |
| Guest OS | POSIX |
| Sharing | `CONVERSATION`, `CALL` |
| Transfers | 32 MiB per file, 128 MiB total, 128 files in each direction |
| Cleanup | Disposal; no `RECLAIM` or `SNAPSHOT` |

Capabilities are a ceiling for compatible images. Acquisition checks the commands needed by the requested capabilities. It also checks removal compatibility before serving some workloads, as described below.

`run_code` raises `NotImplementedError`. A kind that needs an interpreter invokes it through `exec` and owns the image requirement. `HOST_TOOLS` supports guest processes that outlive the launching exec; the image still needs the launcher's commands and a writable run directory.

## File authority

![ACAS workload writes and removals pass through guest execution and use the guest's permissions. Native read, stat and list operations pass through the service file API with host authority. Their path checks and access are separate, leaving a window for a guest to replace a path. Working-directory setup also runs as the guest; the removal probe uses the service file API. Neither guest ownership nor a successful probe makes native reads atomic.](../assets/acas-file-authority.svg)

### `write_file` always runs as the guest

`write_file` uses `write_file_over_exec`. It requires guest write permission at the destination and the transfer commands, including `sh`, `mkdir`, `mv`, `rm` and `base64`. There is no privileged upload fallback.

The helper stages data beside the destination and publishes it after transfer. It uses 48 KiB chunks. `read_timeout_seconds` bounds the whole write, including its control commands, so a large file can time out even when each command is quick.

A completed permission refusal leaves the sandbox reusable. Expiry between commands attempts staged-file cleanup and raises `OSError`. Failure during an active exec follows the execution invalidation rules below.

Path checks reject existing links and escapes. A later parent swap can still redirect a guest write, but it cannot grant more permission than the guest already has. A root guest already has broad authority inside its microVM.

Working-directory setup runs as the guest too. Missing directories are created with `mkdir -p` run under the guest's own authority, never the file plane's, so a parent replaced between the ancestry check and creation can only redirect the creation to where the guest could already have made one. Acquisition refuses when the guest cannot create a missing directory, such as a non-root guest creating a missing base under a root-owned tree. Existing directories are preserved without checking whether the guest could create or write to them. For a workload that needs to write, bake a guest-writable base into a non-root image, or place `work_dir` under a writable parent such as `/tmp`.

Preparation failure on a new sandbox invalidates it and attempts disposal. Failed disposal blocks acquisition until cleanup succeeds. A completed permission refusal during warm repair preserves the existing sandbox; interruption of a preparation command follows the [execution invalidation rules](#execution-and-failure).

<a id="live-write-authority-verification"></a>

The [ACAS research record](../research/acas-backend.md) contains the live write-authority checks and their controls.

### Native reads retain a confinement residual

`stat_file`, `read_file` and `list_dir` use the service file API. They check ancestors without following links, but the service resolves the path again during access. A concurrent guest can replace a checked component between those steps.

| Operation | Remaining path race |
|---|---|
| `read_file` | A replaced parent or final file can redirect the read. |
| `stat_file` | A replaced parent can expose metadata outside the checked directory. A final link is described as a link. |
| `list_dir` | A replaced parent or target directory can redirect enumeration. |

These capabilities remain available with this explicit limit. Transfer caps, timeouts and later disposal do not close the window. Suspending the sandbox does not help: the file API refuses access while it is stopped.

The adapter reads raw `isSymlink` and `isDir` fields because the SDK's typed model omits them. Missing fields raise `AcasEntryPayloadIncomplete`. Listing also requires an `entries` field and valid direct-child paths; an incomplete response is not treated as an empty directory.

The service cannot reliably distinguish regular files from FIFOs, sockets and devices. Here `EntryKind.FILE` means neither directory nor symlink. A FIFO read is bounded by the read timeout, rather than rejected from its type.

The SDK buffers file reads before returning them. The backend checks size before the read and counts returned bytes again afterwards. It cannot enforce a streaming memory bound while the SDK is reading.

### Removal

`remove` runs guest `rm` from `/` and verifies absence through the service API. Missing paths succeed. Directories require `recursive=True`; the working directory itself is refused. A final link is unlinked, not followed.

Every removal uses guest authority. Passing the acquisition probe does not authorize a privileged service-side delete.

## Acquisition checks

Command checks run for the requested capabilities and are cached only after success for that physical sandbox. Exec capture also requires writable `/tmp` and `sh`, `mkdir`, `mkfifo`, `head`, `cat`, `wc`, `dd`, `base64`, `rm` and `rmdir`.

The removal probe creates a file through the service in a fresh root-owned directory under `/`. It then runs guest `rm`. The backend checks that the file existed, that it disappeared, and that the directory remains. It ignores stdout. Work and probe cleanup each have a 30-second bound.

| Probe result | `FILES_OUT` and `HOST_TOOLS` | `FILES_DELETE` | `EXEC` |
|---|---|---|---|
| Removal observed; exit 0 | Allowed | Allowed | Allowed |
| File remains; exit 1 | Refused | Refused | Allowed with warning |
| Inconclusive, including failed probe cleanup | Allowed | Refused | Allowed |

The probe measures compatibility, not uid or a security boundary. A privileged image wrapper can pass while ordinary guest code has less authority. Each operation still uses its own checks and permissions.

It runs for specs requiring `EXEC`, `FILES_OUT`, `HOST_TOOLS` or `FILES_DELETE`. Warm sandboxes use their own completed verdict. Inconclusive results are retried. An image-level refusal hint can avoid a cold create for 60 seconds; it never proves a new sandbox is compatible.

## Execution and failure

Exec captures exact stdout and stderr bytes through guest FIFO readers. Encoded retrieval and scratch cleanup also run as the guest. No service-side file read or delete participates in capture. The default is 1 MiB per stream.

| Outcome | Sandbox state |
|---|---|
| Program completes, including a nonzero exit | Return its bytes and exit status. |
| Complete capture, but scratch cleanup returns nonzero or unexpected output | Warn and return the original result; scratch may remain until disposal. |
| Overflow, capture failure, interrupted retrieval, or an exception during scratch cleanup | Invalidate and attempt whole-sandbox deletion. |
| Execution timeout or cancellation | Invalidate and attempt whole-sandbox deletion, including concurrent commands. |
| Deadline expires during an SDK HTTP 429 `Retry-After` sleep, before any retry starts | Retain the sandbox. Direct cancellation or another retry status does not receive this exception. |

The command deadline includes retrieval and scratch cleanup. Failure deletion has up to `min(30, read_timeout_seconds)` extra seconds. Failed deletion stays retryable; an invalidated instance cannot be reused until deletion succeeds.

## Network policy

Each create sets full traffic inspection and default deny. `ALLOWLIST` adds the hosts in `spec.egress_allow`; `CLOSED` adds none. `UNRESTRICTED` and `EGRESS_METHODS` are refused.

A held sandbox records its mode and case-insensitive host set. An equivalent policy reuses it. A changed policy raises `AcasEgressPolicyConflict` before resume and preserves the old instance.

To change policy, coordinate active calls, dispose that kind and require successful completion, or choose a new key. `router.dispose_kind(...)` must return `True`; direct `backend.dispose(..., kind=...)` must return `None`.

## Lifecycle

Acquisition serializes get-or-create per event loop, key and kind. It resumes a held sandbox or creates one with ownership labels. `CALL` scope includes the call ID in both the registry and labels. A conversation purge reaches all of its calls.

Authentication failures during lifecycle configuration refuse acquisition and attempt deletion. Other configuration failures warn and continue, so the operator must not assume a service retention timer was applied. [Operations](../operations.md) covers recovery and retention scheduling.

Disposal queries service labels, verifies ownership and awaits deletion. Failed IDs remain tracked for retry. A scope purge excludes local overlapping acquisition; the host must also stop new work across replicas.

`reclaim` and `reset` refuse. Router cleanup disposes the sandbox. The [lifecycle measurements](../research/acas-backend.md) document the service behavior behind this contract.

## Sandbox group identity

Managed identity configured on the sandbox group is supported host configuration. The adapter trusts that configuration and adds no ARM inspection, management-read permission or identity-free acquisition requirement.

Ordinary workloads can access that configured authority without opting into the core `ATTACHED_IDENTITY` capability. ACAS does not advertise that finer-grained contract, so its channel and retention checks do not constrain group identity. A spec requiring the capability is refused.

The host must route workloads to groups with the intended permissions. Use separate groups when workloads need separate authority. Sandbox deletion is not principal revocation. The host's [control-plane credential](acas-credentials.md) stays outside the guest and is separate from group identity and host-tool user credentials.

## Verification

Offline tests cover request construction, capability checks, file handling and failure cleanup. The live ACAS suite checks the service, including detachment, call scope, file behavior and allowed/denied hosts. It requires Azure credentials and suitable images.

The broader metadata, private-network, host-path and host-socket isolation probes are not established by that suite. See the [microVM standard](../policy-isolation.md) and [ACAS evidence](../research/acas-backend.md).

## Status

| Area | State | Tracking |
|---|---|---|
| Execution, files, call scope and disposal | Implemented with the limits above | [Package README](../../../packages/maf-sandbox-acas/README.md) |
| Group-configured identity | Supported through trusted host configuration | [#1170](https://github.com/sokolaidev/maf-extensions/issues/1170) (open) |
| Native read/stat/list path race | Open; no atomic service primitive | [microsoft/azure-container-apps#1831](https://github.com/microsoft/azure-container-apps/issues/1831) (open) |
| Typed SDK file metadata | Open; adapter requires raw flags | [#136](https://github.com/sokolaidev/maf-extensions/issues/136) (open) |
| Special-file classification | Open; regular files cannot be distinguished reliably | [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807) (open) |
| Working-directory preparation authority | Bounded; setup creates missing directories as the guest and refuses if that creation fails | [#1339](https://github.com/sokolaidev/maf-extensions/issues/1339) (closed) by [#1379](https://github.com/sokolaidev/maf-extensions/pull/1379) (merged) |
| Method-level network policy | Withheld pending full validation | [#377](https://github.com/sokolaidev/maf-extensions/issues/377) (open) |
| Broader isolation probes | Not implemented | untracked |
