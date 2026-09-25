# Hyperlight

Hyperlight runs the packaged Python guest inside a microVM. Each sandbox has a killable worker process, a warm reset baseline and optional output files.

Use the [package README](../../../packages/maf-sandbox-hyperlight/README.md) for installation and configuration. CodeAct selects `CodeactRuntime` and the backend's `RUNTIME_INSTRUCTIONS`.

## Supported contract

| Setting | Value |
|---|---|
| Host | x86-64 Windows with WHP, or x86-64 Linux with KVM, including suitable WSL2 hosts |
| Isolation | `MICROVM` |
| Runtime | SDK, Wasm backend and Python guest pinned together at 0.7.0 |
| Capabilities | `RUN_CODE`, `SNAPSHOT`, `EGRESS_METHODS`; `FILES_OUT`, `FILES_LIST` when `file_outputs=True` |
| Network | `CLOSED`, exact-host `ALLOWLIST`; HTTP 80 and HTTPS 443; GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS method rules |
| Guest OS | None declared; this is a language runtime |
| Sharing | `CONVERSATION`; one owning host process, optionally one scoped pod |
| Admission | One call per sandbox through execution, delivery and cleanup |
| Cleanup | Restore the original baseline; dispose on failure |

Custom guests, custom images, ARM64 and Linux MSHV are refused. The adapter declares no `EXEC`, `FILES_IN`, `FILES_DELETE`, `RECLAIM`, `HOST_TOOLS` or `ATTACHED_IDENTITY`. It makes no egress observation claim.

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
| Explicit Linux pod mode | Container limits cover the application and one native worker. Namespace PID 1 and an external controller retire the whole pod on active failure. |

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

`list_dir(".", working_directory=".")` enumerates the prepared base through a verified host directory descriptor or Windows handle. It returns sorted direct child names and trusted metadata, including links and special entries that collection refuses. Listing a child directory or following a link is unsupported. Enumeration allows at most 64 entries and 64 KiB of UTF-8 filenames; overflow, replacement or inspection failure refuses the whole result. Admission prevents execution and reset during listing, and storage deletion waits for enumeration to finish.

Execution and restore clear previous outputs. Collection and delivery must finish before either operation. Reset keeps the directory root; disposal removes it only after the worker stops. Direct file access requires the backend's `call_admission` scope. See the [output examples](../../../packages/maf-sandbox-hyperlight/README.md#output-files).

## Network boundary

The runtime's HTTP boundary enforces scheme, exact host, port, path and method before connecting. It covers the guest's raw wasi-http requests as well as its helpers. The adapter grants HTTP port 80 and HTTPS port 443 for each allowed hostname. `EGRESS_METHODS` is declared for GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. The runtime refuses TRACE, CONNECT and custom methods even under an all-methods rule, so a rule naming one is refused. Redirects are returned to the guest rather than followed, so each hop is checked again. Wildcards, other ports, path rules and authority rules are unsupported.

HTTP uses the host network. An allowed internal or loopback hostname grants access there; hostname policy does not filter resolved IP addresses. Application credentials and proxy settings are not forwarded. The host must choose destinations accordingly.

## AKS deployment design

The explicit pod integration builds on [hyperlight-on-kubernetes](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/tree/fc71b4501d23977fcc54f7be144d884fc8210667). Its device-plugin DaemonSet and CDI registration supply the existing node hypervisor device. The application requests `hyperlight.dev/hypervisor: 1` and runs the pinned Python SDK in a non-root container. The deployment targets Linux KVM; the plugin's MSHV discovery does not extend this backend's supported family. The [deployment instructions](../../../images/hyperlight-sandbox/README.md) cover the image, overlay, controller permissions and recovery.

| Layer | Responsibility |
|---|---|
| Upstream device plugin and CDI | Discover and expose the node device; advertise scheduling allocations. |
| Deployment overlay | Pin images, render device UID/GID and count, select validated nodes, set resource budgets and restrict infrastructure authority. |
| Scoped pod | Run the application, protocol adapter and one resident Hyperlight VM for one `(scope, thread_id, agent_id, kind)`. |
| External pod controller | Reserve ownership, acknowledge deadlines, retain cleanup state, retire failed pods and gate replacement. |

The trusted host supplies the complete ownership scope. The controller binds it to a pod UID and owner generation; PID 1 pins the application process and its first execution policy. The pod object carries only a digest of the scope. The controller sends the scope itself over the attach stream, and PID 1 accepts it only when it matches that digest. Exact-instance checks remain in the local adapter. A human identity alone is not a sandbox key, and an individual tool call is not a pod allocation. One agent in one conversation may make several calls in its pod. Different scopes receive different pods and private storage. Warm reuse requires `Cleanup.RESET`; the router defaults to disposal. Reassigning a warm pod or changing its execution policy is refused. The application and backend stay together, and kinds continue using the local protocol. There is no remote-worker API.

The workload container is the aggregate CPU and memory boundary, and the whole pod is the retirement boundary. Its budget includes the application, worker, baseline snapshot and output buffers. Construct the adapter with `HyperlightSandboxConfig(pod=HyperlightPodConfig.from_environment(), max_worker_memory_bytes=None)` inside the supervised application. Container mode requires that explicit choice and rejects a delegated cgroup root. The existing Windows job and delegated Linux cgroup paths keep their per-worker guarantees and refuse missing controls. There is no fallback from failed cgroup delegation to container containment.

The host controller runs outside the session pod's resource budget, with only the Kubernetes authority needed for session-pod lifecycle. The application has no Kubernetes service-account token, host PID access, runtime socket or writable host cgroup mount. It runs non-root with all capabilities dropped, RuntimeDefault seccomp, no privilege escalation, a read-only root filesystem and private writable cache/temp/output volumes. CPU, memory and ephemeral-storage requests and limits are explicit. The upstream plugin remains trusted node infrastructure because it writes kubelet/CDI host directories; application restrictions do not remove that trust requirement. A custom node helper for cgroup delegation is outside this design.

### Session failure and replacement

The controller acknowledges each native deadline before submission. Namespace PID 1 watches the application, worker, OOM events and a five-second controller lease independently of the guest thread. Queue expiry before submission preserves the worker. An ordinary Python error permits reuse after successful reset. Active timeout/cancellation, native failure, worker or owner OOM/death, failed reset and controller loss retire the whole pod. The owning application can die before returning a tool error; its host must treat nonzero exit or lost connectivity as lost state and potentially uncertain execution. No successful tool result is inferred from cleanup.

CPU throttling is not a program deadline, and an OOM event is not evidence that every worker has stopped. Kubernetes [resource enforcement](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) and [forced deletion](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#forced-pod-termination) therefore do not replace lifecycle verification. Controller-created pods use `restartPolicy: Never`, without automatic replay. A ConfigMap reserves each ownership scope before pod creation. Cleanup requires exact-UID runtime termination, persists that receipt, then removes the pod finalizer and reservation. Missing pods without saved proof, synthetic terminal states and unreachable nodes keep cleanup pending and block replacement. Node fencing is an operator responsibility. Controller namespaces are separate ownership authorities; generic distributed routing remains unimplemented.

Supported node platforms are a measured matrix of VM size, node OS, Kubernetes minor and runtime, currently Ubuntu 24.04 and Azure Linux 3.0 on Kubernetes 1.35; the [deployment instructions](../../../images/hyperlight-sandbox/README.md#supported-platforms) list it. Operators label eligible node pools; the integration never changes a node. Before the application starts, PID 1 checks the requirements visible from the pod: x86-64, cgroup v2, the declared limits, no swap and a `/dev/kvm` that can create a VM. A failure raises `HyperlightPodPlatformError` from `supervise` after confirmed cleanup.

The [AKS research and validation plan](../research/hyperlight-backend.md#aks-upstream-basis-and-evidence-2026-09-22) separates the measured device/delegation path from this container-based design. Per-worker cgroup isolation remains an independent option when several workers must share a container or the owner must survive a worker's resource failure.

## Validation and deployment limits

The real-guest tests cover WHP and KVM execution, reset, queue deadlines, cancellation, worker reaping, output bounds and network policy. Linux tests also cover process-tree containment. The [research record](../research/hyperlight-backend.md) carries environments and measurements.

AKS probes cover both the historical delegated-cgroup path and this explicit container integration. The research record distinguishes them and lists remaining operational checks. Standard Azure Container Apps does not provide the required local hypervisor device in the evaluated hosting setup. The backend has no remote-worker mode.

## Status

| Area | State | Tracking |
|---|---|---|
| Packaged runtime, reset and worker containment | Implemented on the supported WHP/KVM family | [Package README](../../../packages/maf-sandbox-hyperlight/README.md) |
| Method-scoped egress | Implemented for GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS; path rules not declared | [#377](https://github.com/sokolaidev/maf-extensions/issues/377) (open); [#1448](https://github.com/sokolaidev/maf-extensions/pull/1448) (merged) |
| Additional channels | Separate work; runtime support is available | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open) |
| AKS hosting | Feasibility measured; deployment work remains open | [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230) (open) |
| Upstream AKS device deployment | Pinned overlay implemented; production operational validation remains | [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237) (open) |
| Supported AKS platform matrix | Ubuntu 24.04 and Azure Linux 3.0 on Kubernetes 1.35 measured; unsupported nodes refused before the application starts | [#1425](https://github.com/sokolaidev/maf-extensions/issues/1425) (open) |
| One ownership scope per AKS pod | Explicit integration implemented; operational acceptance remains open | [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) (open) |
| Distributed owner routing and purge | Conditional follow-up; not implemented | [#1239](https://github.com/sokolaidev/maf-extensions/issues/1239) (open) |
| Writable inputs | Not implemented | [#1218](https://github.com/sokolaidev/maf-extensions/issues/1218) (open) |
| Output collection | Flat `FILES_OUT` implemented by [#1344](https://github.com/sokolaidev/maf-extensions/pull/1344) (merged); listing completes the output scope | [#1219](https://github.com/sokolaidev/maf-extensions/issues/1219) (closed) by [#1397](https://github.com/sokolaidev/maf-extensions/pull/1397) (merged) |
| Flat output listing | Implemented for the prepared output base | [#1392](https://github.com/sokolaidev/maf-extensions/issues/1392) (closed) by [#1397](https://github.com/sokolaidev/maf-extensions/pull/1397) (merged) |
| File cleanup | Output-only reset and disposal implemented by [#1344](https://github.com/sokolaidev/maf-extensions/pull/1344) (merged); input and selective cleanup remain separate | [#1220](https://github.com/sokolaidev/maf-extensions/issues/1220) (open) |
| Native host tools | Not implemented | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
