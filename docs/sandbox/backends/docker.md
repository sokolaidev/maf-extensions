# Docker

With `credential_gateway=CredentialGateway(provider, max_lifetime_seconds=300)` and a rebuilt packaged proxy image, Docker supports [credentials for guest HTTP](../hosts.md#credentials-for-guest-http-requests). Each acquisition gets a fresh container and gateway bound to the trusted user scope, agent, call and runtime generation. The gateway holds the bearer tokens and enforces exact HTTPS origins, method/path rules and independent expiry. This requires the attached-authority opt-ins and a call-scoped workload; credential-bearing containers are never reused.

Docker runs image-based workloads through the Docker CLI and Engine. It supports command execution, file upload and collection, host tools, and optional directory reclamation.

Use the [package README](../../../packages/maf-sandbox-docker/README.md) for installation and configuration. Docker Desktop and Docker Engine are supported. Other compatible engines are best effort; this adapter does not invoke the Podman CLI.

## Supported contract

| Setting | Value |
|---|---|
| Isolation | `CONTAINER`; the host must set `min_isolation=Isolation.CONTAINER` |
| Capabilities | `EXEC`, `FILES_IN`, `FILES_OUT`, `FILES_DELETE`, `HOST_TOOLS`, `RECLAIM` |
| Network | `CLOSED`; `ALLOWLIST` and egress observation with a configured proxy image |
| Guest OS | Async factory declares POSIX for a Linux daemon; plain constructor declares none |
| Sharing | `CONVERSATION`, `CALL` |
| Transfers | 64 MiB per file, 256 MiB total, 256 files in each direction |
| Cleanup | Disposal by default; reclaim requires explicit host opt-in |

Docker's shared kernel is a container boundary, including when Docker Desktop runs the daemon in a VM. Workload containers receive no host bind mounts or Docker socket.

Acquisition checks commands needed by the requested capabilities. Successful checks are cached by physical container ID; failures are retried. File-only operations need no guest shell. `RUN_CODE`, `FILES_LIST`, `SNAPSHOT`, `EGRESS_METHODS` and `ATTACHED_IDENTITY` are withheld.

## Engine binding

`DockerSandboxBackend.create` resolves and retains the selected Docker context, endpoint and client environment. Later commands use that binding. Missing contexts are refused rather than replaced with the ambient default.

The factory reads the daemon OS. Before creating or restarting a container, a backend that declared POSIX checks again and refuses a changed OS. Already-running reuse does not repeat that check. Transient inspection failures leave the normal acquire path to report its result.

Retire the backend when changing its context definition, TLS configuration or daemon. The binding is not a guarantee that external configuration stays unchanged.

<a id="the-pull-surface-one-tar-read-twice"></a>

## Root-filesystem transfers

`stat_file` reads an engine-produced archive header. `read_file` reads the file body from the same archive mechanism. Neither asks guest commands to describe the file. Ancestors are checked without following links; a final link can be described but cannot be read.

Archive parsing is bounded. PAX/GNU metadata has a 64 KiB and 32-header budget. Invalid, sparse, truncated or oversized results are refused. A failed copy means absence only when the engine names that exact missing path; daemon and container failures remain errors.

This view covers the container's root filesystem. It does not cover guest mounts such as `/proc`, `/sys`, `/dev` or tmpfs. An absence result does not prove that a file is absent from those mounts. The backend does not discover and refuse every unsupported mount at runtime.

<a id="the-guest-is-frozen-around-the-tar-plane-members"></a>

## Pause, check, transfer, resume

![Docker holds ownership for one endpoint and container, pauses the guest, checks path components using engine archive headers, transfers the archive, and resumes the guest before releasing ownership. Once pause is issued, cleanup attempts resume even after timeout or cancellation unless pause was positively refused. Failed resume is retained for recovery and blocks unsafe reuse. Guest commands used for removal are outside this paused transfer sequence.](../assets/docker-file-freeze.svg)

`prepare_work_dir`, `write_file`, `read_file` and `stat_file` pause the guest across path checks and archive operations. This prevents guest processes from replacing a checked parent before the copy. It does not cover independent host-side mutation.

Default working-directory preparation also makes an EXEC-only acquire require pause support. Preparation that makes no engine call needs no pause, such as an already-satisfied root base or a runtime-only spec.

Ownership is shared by endpoint and container, including aliases that resolve to that endpoint. Another event loop is refused while the operation owns the pause. Coordinating multiple host processes on the same container is unsupported.

Once pause is issued, cleanup owes a resume attempt even if pause completion is uncertain. A positively refused pause does not authorize resuming someone else's pause. Cancellation waits for cleanup. Failed resume is retained for recovery; acquisition refuses reuse if recovery fails.

An exec attempt is retried only when a confirmed pause owned by this process covered the whole attempt and the daemon's response proves the command never started. Guest text that resembles a pause error is insufficient. Ordinary output limits still apply to running commands.

<a id="files_list-is-withheld-because-a-listing-transfers-the-subtree"></a>

## Why listing is unavailable

Docker's directory archive walks the whole subtree and transfers file bodies. It has no direct-child, headers-only listing operation. Returning just names would still incur that unbounded transfer work.

`list_dir` therefore raises `NotImplementedError`, and `FILES_LIST` is withheld. There is no guest `ls` fallback. See the [archive measurements](../research/docker-backend.md).

## Write ownership and removal

Uploads are extracted with the daemon's root authority. Archive entries carry the resolved guest uid/gid so the guest can edit its inputs. Ownership stamping does not change placement authority; the pause is what closes guest path replacement during transfer.

Missing directories at or below the working directory receive guest ownership. Existing directory metadata is preserved. Missing ancestors above that boundary use the daemon's ownership.

The backend resolves `Config.User` from numeric IDs, account files or a bounded guest `id` check. Empty user means root. An unresolved identity refuses `FILES_OUT` and `HOST_TOOLS`; other workloads may receive root-owned inputs with a warning. Unresolved facts are retried.

`remove` uses guest execution with `rm -f` or `rm -rf`. It rejects the working directory itself and requires `recursive=True` for directories. Missing paths succeed. A final link is unlinked. This operation is not part of the paused archive sequence.

Root removal is allowed only when engine metadata establishes that every relevant ancestor, including `/`, is root-owned and not writable by others. Otherwise removal runs as the image's user. A root refusal is retried as that user only when the actual container lacks `CAP_DAC_OVERRIDE` or its presence is unknown.

`reclaim` uses the acquisition-time ownership check and rejects unsafe placement, including shallow targets. Relative targets must be children of the working directory. Resolved facts are cached by container, image and working directory. Unknown ownership cannot authorize a raised recursive delete.

## Network policy

![With CLOSED, the workload has no network. With a nonempty ALLOWLIST, the workload joins an internal network and reaches destinations only through iron-proxy. The proxy also joins an outbound network and checks allowed hosts, HTTP methods, paths and resolved addresses. Proxy audit records are attributed to the sandbox before removal. Docker additionally requires an unaddressed internal bridge. The model's content labels remain a separate host-policy check.](../assets/container-egress.svg)

`CLOSED`, including an empty allowlist, uses `--network none`. A nonempty allowlist uses an internal network and a proxy connected to both internal and outbound networks. The proxy listens only on its internal-network address, so other containers on the outbound network cannot use it. Proxy environment variables configure clients; network separation enforces the route. The proxy runs with `no-new-privileges`, no capabilities, and the PID, memory and CPU limits configured for the workload.

This setup requires Docker Engine 28 or later. The backend verifies that the internal bridge has no host address in either address family. It checks actual driver, internal-network and IPAM state. An invalid adopted network is replaced with its workload; an invalid new network is removed before use.

The proxy terminates guest TLS, checks host, method and path, and validates the upstream certificate. Its per-sandbox CA certificate is written to the guest work directory and named in `SSL_CERT_FILE`, `CURL_CA_BUNDLE` and `REQUESTS_CA_BUNDLE`; its key stays in the proxy. Public HTTP is denied on every port. Listed private endpoints use TLS unless `allow_private_http=True` is set for development or test. The outbound dial checks the resolved address and denies loopback, link-local, metadata, gateway and proxy interface addresses. Docker's embedded DNS forwarding is a separate limit not covered by these proxy checks. See [network policy](../network.md) for the full contract.

Every acquire rebuilds the proxy. Before removal, the backend drains attributable JSON audit records. Engine labels allow recovery after host restart. Missing or invalid attribution produces no event, not a clean audit result. Failed removal publishes no event; overlapping cleanup can duplicate a window. See [egress observation](../observability.md).

## Lifecycle and retention

Acquisition serializes get-or-create per loop, key and kind. It reuses a running container, starts a stopped one, or creates a replacement when needed. Names and labels include call identity for `CALL` scope. The storage-base label is checked on every acquire; a changed or missing base is refused.

Disposal queries engine labels and verifies physical IDs. Local records are a fallback when listing fails. A scope purge covers all matching calls. Failed workload deletion is reported; infrastructure cleanup failures are logged for later recovery.

An exec timeout discards the container, and so does `exec` output past 8 MiB, stdout and stderr together, which raises `SandboxExecOutputLimitExceeded`. An `exec_bounded` caller's own budget keeps the container when it overflows. Cancellation terminates and reaps the host CLI process; it does not by itself establish that guest work stopped. Router cleanup still applies.

The operator `reap` helper uses creation age. It can expire a running workload and is not an inactivity timer. A selected workload carries its proxy and network into cleanup even if those resources are newer. Orphan infrastructure uses its own age.

Reaping revalidates IDs and refuses unreadable inventory. It removes the proxy, workload and network in that order. Failed proxy removal leaves the group for retry; network removal never forces endpoint disconnection. The operator owns scheduling and coordination. See [operations](../operations.md).

## Measuring a confinement claim

`DockerFingerprintSubject` observes the root filesystem through `docker diff`. A separate trusted Linux observer reads mounted storage and process identities without using guest tools. The observer image needs Python 3.12 and should be pinned by the host. Backend-provisioned proxy CAs must match the current trusted proxy on every observation. The observer measures their permissions, ownership, extended attributes and ancestor directories separately, so CA rotation does not hide guest residue.

Measurement requires a quiescent container and a clean baseline. It refuses unsupported mounts, privileged/shared-PID setups, changed inventory and exceeded budgets. It does not prove the absence of transient effects, kernel changes or socket activity. An unsupported result is not a passing measurement.

The live Docker suite exercises real transfers, hostile paths, pause recovery, network policy, call scope and cleanup. Offline tests cover adapter logic. Details and evidence are in the [package README](../../../packages/maf-sandbox-docker/README.md) and [research record](../research/docker-backend.md).

## Status

| Area | State | Tracking |
|---|---|---|
| Execution, file transfer, call scope and disposal | Implemented | [Package README](../../../packages/maf-sandbox-docker/README.md) |
| Paused archive operations | Implemented; prevents concurrent guest path replacement | [File confinement](../capabilities.md) |
| Root-filesystem scope | Supported; guest mounts remain outside the transfer view | [Archive evidence](../research/docker-backend.md) |
| Directory listing | Withheld because directory archives transfer the subtree | [Archive evidence](../research/docker-backend.md) |
| Reclamation | Declared; router use requires host opt-in and a compatible cleanup floor | [Cleanup policy](../tool-call.md) |
| Proxy enforcement and observation | Implemented with the limits above | [Network policy](../network.md), [observability](../observability.md) |
| Operator retention | Implemented; externally scheduled | [Operations](../operations.md) |
