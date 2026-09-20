# Hyperlight

Hyperlight runs the packaged Python guest inside a microVM. Each sandbox has a killable worker process, a warm reset baseline and optional output files.

Use the [package README](../../../packages/maf-sandbox-hyperlight/README.md) for installation and configuration. CodeAct selects `CodeactRuntime` and the backend's `RUNTIME_INSTRUCTIONS`.

## Supported contract

| Setting | Value |
|---|---|
| Host | x86-64 Windows with WHP, or x86-64 Linux with KVM, including suitable WSL2 hosts |
| Isolation | `MICROVM` |
| Runtime | SDK, Wasm backend and Python guest pinned together at 0.7.0 |
| Capabilities | `RUN_CODE`, `SNAPSHOT`; `FILES_OUT` when `file_outputs=True` |
| Network | `CLOSED`, exact-host `ALLOWLIST`; HTTP 80 and HTTPS 443 |
| Guest OS | None declared; this is a language runtime |
| Sharing | `CONVERSATION`; one owning host process |
| Admission | One call per sandbox through execution, delivery and cleanup |
| Cleanup | Restore the original baseline; dispose on failure |

Custom guests, custom images, ARM64 and Linux MSHV are refused. The adapter declares no `EXEC`, `FILES_IN`, `FILES_LIST`, `FILES_DELETE`, `RECLAIM`, `HOST_TOOLS`, `EGRESS_METHODS` or `ATTACHED_IDENTITY`. It makes no egress observation claim.

The guest is CPython 3.14 with a reduced standard library. It supports statements, persistent globals and separate stdout/stderr. `json`, `math` and `re` are available; `datetime`, `statistics`, `pickle` and `__future__` are absent. This is not the full desktop Python environment.

## Worker ownership and cleanup

![One host process owns the shared Hyperlight registry. Acquisition creates a worker inside a Windows job or delegated Linux cgroup, prepares the Python guest and records a baseline. An admitted call holds ownership through execution, output collection, delivery and reset. Queue expiry before submission preserves the worker. Timeout or cancellation after submission, and native faults, terminate and reap it. A successful reset restores the baseline for reuse.](../assets/hyperlight-worker-lifecycle.svg)

Construction starts no worker. Acquisition checks hypervisor access, starts containment, initializes the guest and records its warmed baseline. All native objects belong to the worker's main thread. The worker environment excludes application credentials.

All backend objects in one process share a key/kind registry. A Windows machine-wide named event or Linux lock under `/run/lock` permits one owner within the shared namespace. Another process refuses acquisition and reports disposal as unclean.

Requests and purges must reach the owner. Separate Linux mount, PID or cgroup namespaces do not automatically share that ownership guarantee. Multiple owners behind one logical backend are unsupported.

| Host | Worker containment |
|---|---|
| Windows | A job limits committed memory and kills the worker tree when the host exits. |
| Linux | An operator-delegated cgroup v2 subtree limits memory, disables swap and groups OOM cleanup. A separate trusted supervisor watches host and worker pidfds, kills the group, waits for it to empty and removes it. |

The Linux supervisor inherits the owner lock before creating resources. The worker joins containment before execution. The supervisor stays outside the worker's memory group and retains ownership until cleanup finishes. Acquisition refuses unavailable controls.

The default memory ceiling is 1.5 GiB on Windows and 3 GiB on Linux. Linux rounds down to whole pages and sets `memory.swap.max=0`. Returned stdout/stderr have a separate byte limit; retained diagnostics have a 64 KiB limit.

## Execution outcomes

Queue time counts toward `run_code`'s deadline. Separate sandboxes can run concurrently, but a sandbox serializes execution and reset. Backend admission also covers output delivery and cleanup across routers and event loops.

| Outcome | Result |
|---|---|
| Deadline expires before submission | `SandboxQueuedTimeout`; preserve the worker. |
| Guest code raises an ordinary exception | Failed `ExecResult`; worker can be reused. |
| Deadline expires after submission | Terminate and reap the worker; raise `TimeoutError`. |
| Active execution is cancelled | Terminate and reap before propagating cancellation. |
| Native or transport failure | Retire the worker. |
| Reset succeeds | Restore the original warmed state and change `instance_id`. |

Cleanup has a separate bounded allowance. It uses its own thread so blocked pipe readers cannot prevent worker termination.

Changing the allowlist or execution contract requires disposal or a new key. Exact-instance disposal ignores stale IDs and retains failed targets for retry. Scope purge uses the owner's registry. Cancellation finishes the active target's bounded cleanup attempt, then leaves remaining targets registered.

## Optional output files

With `file_outputs=True`, each sandbox has a private host directory exposed as `/output`. The only accepted explicit working directory is `/output`. Input directories are never configured.

Collection accepts flat relative names and bounded raw bytes. It rejects traversal, links and Windows reparse points. The pinned guest cannot create directories or links. Core collection also enforces file-count and total-byte limits.

Execution and restore clear previous outputs. Collection and delivery must finish before either operation. Reset keeps the directory root; disposal removes it only after the worker stops. Direct file access requires the backend's `call_admission` scope. See the [output examples](../../../packages/maf-sandbox-hyperlight/README.md#output-files).

## Network boundary

Native HTTP helpers enforce scheme, exact host, port and path. The adapter grants HTTP port 80 and HTTPS port 443 for each allowed hostname. Wildcards, other ports and narrower method or authority requirements are unsupported. CONNECT and TRACE are blocked by the pinned runtime.

HTTP uses the host network. An allowed internal or loopback hostname grants access there; hostname policy does not filter resolved IP addresses. Application credentials and proxy settings are not forwarded. The host must choose destinations accordingly.

## Validation and deployment limits

The real-guest tests cover WHP and KVM execution, reset, queue deadlines, cancellation, worker reaping, output bounds and network policy. Linux tests also cover process-tree containment. The [research record](../research/hyperlight-backend.md) carries environments and measurements.

AKS hosting remains under investigation. Device access alone does not establish cgroup delegation, worker cleanup or ownership across pods. Standard Azure Container Apps does not provide the required local hypervisor device in the evaluated hosting setup. The backend has no remote-worker mode.

## Status

| Area | State | Tracking |
|---|---|---|
| Packaged runtime, reset and worker containment | Implemented on the supported WHP/KVM family | [Package README](../../../packages/maf-sandbox-hyperlight/README.md) |
| Additional channels | Separate work; runtime support is available | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open) |
| AKS hosting | Investigation | [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230) (open) |
| Writable inputs | Not implemented | [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) (open) |
| Output collection and listing | Flat `FILES_OUT` implemented; listing withheld | [#1219](https://github.com/sokolaidev/maf-extensions/issues/1219) (open) |
| File cleanup | Not implemented | [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220) (open) |
| Native host tools | Not implemented | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
