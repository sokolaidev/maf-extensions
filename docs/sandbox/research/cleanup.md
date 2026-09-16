# Cleanup and reuse research

> Consolidated research record for sandbox cleanup after calls, owner-process failure and deployment shutdown. It combines the process cleanup/reuse and orphan-cleanup ownership investigations. The decided operating contract lives in [`../tool-call.md`](../tool-call.md) and [`../operations.md`](../operations.md); this record keeps the evidence, ownership boundary and unresolved guarantees.

## Decision at a glance

The extension does not start a daemon, lease service or fleet controller. The router and backends provide bounded cleanup operations, resource identity and safe retry behavior. The deployment owns the scheduler, target engine or sandbox group, credentials, retention policy, failure alerts and response to missed cleanup.

The default is disposal after each call. Reuse is an explicit host choice: `Cleanup.RECLAIM` or `Cleanup.RESET` requires a backend declaration, a kind-compatible confinement claim where applicable, and cleanup that meets the backend's reach contract. A cleanup failure does not make the sandbox warm and reusable; the key/instance remains refused until cleanup succeeds or the host explicitly chooses the documented failure policy.

Cleanup after an application process dies is not the same promise as cleaning an active call. Age-based expiry, owner-lifecycle cleanup and renewable leases have different evidence and owners. The current suite supplies maximum-age/operator primitives and platform examples; it does not claim crash-perfect destruction or arbitrary active-use preservation.

## Ownership boundary

| Responsibility | Owner |
|---|---|
| Dispose a key/kind during normal host operation | Router and backend, with failures reported through cleanup records/events |
| Discover resources after process memory is gone | Backend/provider API using labels, immutable IDs and backend-specific ownership rules |
| Choose retention and maximum age | Deployment operator |
| Schedule independent cleanup and provide credentials | Deployment infrastructure |
| Alert, retry and decide what to do after incomplete cleanup | Deployment operator |
| Run a universal recovery daemon or maintain a fleet inventory | Not owned by this extension |

`SandboxPurger` participates in the host's conversation-delete path; it is not a background supervisor. `dispose_scope` must discover resources through the service/engine so a delete arriving on another replica can work without the creator's in-memory registry. A separate executable can call a backend sweep without an agent or conversation and fits the existing architecture, but a suite-owned highly available controller would introduce its own durable state, authority, fencing and availability contract.

## Three different cleanup promises

| Promise | Evidence required | Appropriate policy |
|---|---|---|
| Remove resources older than a chosen maximum lifetime | Creation time, ownership, immutable identity and explicit permission to interrupt eligible active work | Backend primitive plus deployment retention policy |
| Remove resources after their exclusive owner ended | Authoritative owner lifecycle and a rule preventing another owner from adopting the resource | Platform or controller that owns both lifecycles |
| Preserve arbitrarily long active use while removing abandoned resources | Renewable lease, expiry clock, fencing and recovery when the controller fails | Separate platform/controller design |

A periodic sweep provides an eligibility threshold, not a hard destruction deadline. With maximum age $T$ and sweep interval $S$, removal normally occurs after $T$ and within the next successful sweep plus execution time; missed schedules, clock errors, inventory refusal and deletion failure extend retention. Calling this “orphan detection” would overstate the guarantee.

A heartbeat/lease design is not a small addition. It needs durable lease state, an expiry clock, renewal behavior during long calls, a decision about whether missed renewal stops work, generation/fencing against late owners, and recovery when the controller itself fails. Warm sandboxes shared across replicas make a creator-death rule unsafe: replica A may create a sandbox that replica B is legitimately serving. Nothing in the current evidence justifies adding that machinery to every backend.

## Cleanup ladder and reuse

The process-cleanup measurement found that deleting a directory, signalling a process group and taking before/after process snapshots cannot prove a sandbox is clean. Guests can write outside the call directory, leave their group, reparent descendants between observations, race numeric process-ID reuse, alter the collector or keep a process alive after a scan.

The cleanup ladder therefore treats reuse as a policy, not proof:

- `RECLAIM` removes the call directory or resource using a backend-established reach contract. It is never available merely because a path name looks safe.
- `RESET` restores a trusted pre-input baseline when a backend declares snapshot/reset semantics and the baseline really predates guest input.
- `DISPOSE` destroys the sandbox and is the default for the next call after cleanup uncertainty.

Shared instances drain active sibling calls before whole-instance reset or disposal. A call-scoped instance is cleaned independently. A timed-out, cancelled or failed call still runs bounded cleanup; if the result is uncertain, the physical instance is disposed rather than reused. `FailedReclaimPolicy.KEEP` is a separate explicit host choice and does not make an uncertain sandbox safe.

The router tracks physical `instance_id` and serving backend records. If a restarted router sees an unfamiliar instance, it does not trust it as warm: it cleans it before serving a call, using reset only when the backend can establish the baseline and disposal otherwise. A backend without a create-or-reuse signal cannot establish that a new conversation is clean without paying its normal create path.

## Process cleanup evidence

The transport records a versioned launcher receipt containing the program PID and optional dedicated process-group ID. The helper releases the guest only after the host receives a complete receipt, and cleanup never falls back to guest-writable PID/session files. Process snapshots are taken before launch, after launch, before cleanup and after cleanup; they retain PID/start-tick identity, ancestry observed during the run, process state and attribution. Numeric IDs are never treated as stable handles without start-tick verification.

The portable Linux collector runs isolated guest-interpreter code and is explicitly guest-observed evidence, not an independent trusted engine measurement. It has bounds on process count, fields, bytes and time. Missing, truncated or unavailable observations mark the run unclean. It does not collect environment variables. A transport close alone does not prove guest processes stopped.

Process cleanup has one bounded budget for observation, signalling and verification; pre-signal observation has a smaller bound. Directory reclamation has its own bound and still runs after the process budget expires. Cleanup records retain the last observed attribution through partial scans and distinguish a signal attempt from a mere process observation. Audit events include run/instance correlation, phase, identities, outcomes and bounded diagnostics without exposing sensitive command/path data by default.

Four snapshots cannot prove the absence of every descendant: an intermediate parent may exit before a scan, a guest may alter the collector and a numeric PID may be reused between checking and signalling. Cgroups and stable kernel process handles would improve the guarantee but add platform-specific lifecycle machinery not owned by the current design.

## Backend cleanup primitives

### Docker

Docker's detached `sleep infinity` workload containers survive the Python process unless explicitly removed. Generic `docker container prune` handles stopped containers only; it does not find running orphan workloads. A surviving proxy can also keep its network referenced, so network prune is not a complete group cleanup.

The backend's `reap(older_than, scope=None)` is an operator primitive, not a scheduler. It inventories workload containers, proxies and networks using identity labels and creation times, rechecks immutable IDs immediately before removal, refuses unreadable or incomplete inventories, and reports workload/proxy/network counts plus per-resource failures. It uses maximum creation age and may interrupt an eligible active sandbox; it does not claim crash detection. An explicit scope prevents deleting another deployment's resources, but Docker daemon authority itself is not tenant isolation.

### WSLC

WSLC retention uses stopped time rather than creation time. Running workloads are retained; stopped workloads expire after the operator's configured interval. Orphan networks and partial infrastructure use separate age rules. A sweep pauses acquisition, restart and other mutations for selected scopes because name-based WSLC deletion has no atomic identity/retention precondition. Windows Task Scheduler or a deployment runner can invoke the separate process; the backend does not start it.

### ACAS

ACAS is cleaned by service lifecycle policies and deployment-owned reconciliation. Auto-suspend is not auto-delete. Sandbox creation and later lifecycle-policy configuration are separate operations, and the measured gap can leave a sandbox with suspension but no confirmed deletion timer. A reconciliation script can inventory the selected group, retain running/transitioning/unknown resources, select only `Stopped` sandboxes older than a strict stop-time cutoff, recheck immutable IDs before deletion, wait for absence, report each failure and return a failing exit status when cleanup is incomplete.

The reference example uses a dedicated group, explicit subscription/resource-group/group configuration, a positive stopped-retention duration and Azure CLI/OIDC credentials. It is not a universal group-wide sweep and does not delete disk images, snapshots, volumes or groups. A resume between final recheck and delete remains a service race; deployments needing atomic resumption exclusion need platform coordination.

### Host and conversation disposal

`SandboxRouter.dispose`, `dispose_kind`, `dispose_scope` and `dispose_unclean` operate during normal host lifecycle. They use finite positive timeouts, preserve unrelated kind/instance failures and retry recorded targets. `dispose_scope` discovers resources through backend labels rather than process memory. The host must stop new work across replicas before a conversation purge; a local backend barrier cannot coordinate replicas by itself.

A successful disposal result means the requested sweep reported success, not that every resource under the host's broader deployment is gone. An incomplete cleanup is logged/returned, not silently treated as a clean sandbox. Operators use the backend-specific sweep for resources a restarted host cannot reconstruct locally.

## Deployment examples

The deployment owns installing and enabling its scheduler. Existing platform options are sufficient:

| Deployment | Example | Boundary |
|---|---|---|
| Persistent Docker host | systemd timer or equivalent invokes the Docker backend sweep | Requires explicit engine target, scope, age policy and failure reporting |
| Developer WSLC/Docker Desktop | Windows Task Scheduler, maintenance task or explicit operator command | A sleeping/unavailable machine cannot promise wall-clock cleanup |
| ACAS | Scheduled Container Apps Job, GitHub Actions workflow or equivalent invokes the group cleanup | Requires OIDC/credentials, dedicated group policy and monitoring |
| Kubernetes | CronJob or native lifecycle ownership | Kubernetes does not automatically own external Docker resources |
| Disposable CI environment | Destroy the environment after the job and handle failed jobs | Destroying the application container does not necessarily destroy resources on a remote/shared engine |

Schedulers can be missed, duplicated or delayed. Cleanup commands must therefore be idempotent, use immutable resource IDs, recheck state immediately before deletion and report failure for retry. A scheduler's successful process exit is not proof when the result contains failures or incomplete inventory.

## Required acceptance evidence

A cleanup primitive should have deterministic tests for identity filtering, scope isolation, replacement/race safety, partial-create recovery, incomplete inventory, cancellation, timeout, immutable-ID deletion, already-absent resources and retry. A live deployment example should kill or stop the serving application, start the independent cleanup process with an empty registry, verify eligible resources disappear, demonstrate an eligible active sandbox may be removed under the selected age policy, and verify incomplete cleanup produces a failed job.

The tests must distinguish:

- nothing was found;
- the resource was already absent;
- the resource was found and deleted;
- the resource was retained by policy/state;
- inventory, authorization or deletion was incomplete.

No result should call an age threshold a crash guarantee, call an empty process scan proof of cleanliness, or call a stopped-retention policy an authority-revocation deadline.

## Remaining limits

- There is no suite-owned daemon, durable fleet inventory, renewable lease service or global cross-replica lock.
- A backend can provide safe cleanup of resources it can identify; it cannot by itself decide the deployment's acceptable retention or scheduler availability.
- Process snapshots and directory removal remain conservative evidence with known races; cgroups/stable kernel handles are outside the current portable design.
- Docker/WSLC/ACAS have different cleanup primitives and retention clocks; a host must use the backend-specific operator contract.
- Attached identity has a separate platform-side retention obligation. Stopped retention, best-effort sweeps and token lifetime alone do not establish it.
