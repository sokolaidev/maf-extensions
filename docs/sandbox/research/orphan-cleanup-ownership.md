# Who operates cleanup after the host dies?

> Exploration for [#1008](https://github.com/sokolaidev/maf-extensions/issues/1008), under [#808](https://github.com/sokolaidev/maf-extensions/issues/808): whether orphan cleanup warrants a service owned by this extension suite, or belongs to deployment infrastructure. Written on 2026-09-08; the argument is preserved below. The ownership decision and the ACAS example chosen for live verification now live in [`../operations.md`](../operations.md).

## Recommendation

Keep scheduling and the retention decision with the deployment. Provide a bounded cleanup operation in each backend that needs one, and a tested example of calling it from an existing scheduler. Do not introduce a mandatory daemon, a lease store, or a fleet controller into `maf-sandbox` for this problem.

This still requires suite work. The backend knows which workload, proxy, and network belong together, how to discover them after process memory is gone, and how to remove an inspected resource without deleting a replacement. Infrastructure knows which engine or sandbox group belongs to this deployment, how long resources may survive, which identity may delete them, and who responds when cleanup fails. That is a useful boundary rather than a choice between implementing everything and leaving adopters to invent cleanup.

## The existing extension boundary

The [suite introduction](../README.md) explicitly promises no service, daemon, or control plane to operate. The [architecture](../architecture.md) places the host above a protocol and router, with replaceable backends beneath them. `SandboxBackend` serves a key through `acquire`, `dispose`, and `dispose_scope`. `SandboxPurger` is an explicit participant in the host's conversation-delete path; it is not a background supervisor.

Neither the protocol nor the router owns a deployment identity, a persistent resource inventory, a scheduler, or a record of which application replica is alive. Conversation scope identifies what a caller may reach, not which replica owns a sandbox for its lifetime. Warm reuse and service-based scope purge already accommodate more than one replica.

A separate executable that calls a backend once would fit this architecture. It can run without an agent or a conversation. A library starting a task in the application would also be small, but it would die with that application and therefore fail the requirement. A suite-owned service that promises recovery and continued availability would create a new operational product. It would not necessarily require changing every existing protocol member, but its state, authority, and availability obligations would extend well beyond the current extension contract.

## Three different promises

| Promise | Evidence needed | Appropriate owner |
| --- | --- | --- |
| Remove resources older than a chosen maximum lifetime | Creation time, ownership, immutable resource identity, and explicit permission to interrupt old active work | Backend mechanism, deployment policy |
| Remove resources after their exclusive owner has ended | An authoritative owner lifecycle and a rule preventing another owner from adopting those resources | Infrastructure that owns both lifecycles |
| Remove abandoned resources while preserving arbitrarily long active use | A renewable lease or another authoritative activity contract, plus coordination with users of the resource | A platform or a separately justified controller |

The first is enough for many development and CI deployments. It is expiry, not proof of a crash. The third should not be smuggled into an age filter by calling it orphan detection.

A periodic sweep also gives an eligibility threshold rather than a hard expiration deadline. With maximum age `T` and interval `S`, removal is normally after `T`, within the next successful sweep and its execution time; missed schedules, clock error, inventory refusal, or deletion failure extend retention. A host that needs a strict destruction deadline needs enforcement that remains available at that deadline, not a stronger promise in the Python API.

## What a lease design would cost

A heartbeat is only the first write. Someone must own durable lease state, choose its clock and renewal interval, define expiry under connectivity loss, and decide whether failure to renew stops work or merely risks deletion. A paused process and a dead one both stop renewing. Keeping active work safe during an outage and guaranteeing prompt deletion after a crash are different policies; a timer cannot decide between them.

Shared warm sandboxes make the owner question harder. Replica A can create a sandbox and replica B can legitimately use it later. Deleting on A's death is then wrong. A lease may need to represent a resource generation and every permitted active user, or an exclusive owner that has acquired the right to serve it. An active call that outlasts one lease period needs renewal throughout the call, not just a timestamp at `acquire`.

Rechecking a lease immediately before deletion is insufficient if another process can renew or acquire it between that read and the engine delete. Strong protection requires coordinated ownership transfer or a fencing mechanism that prevents the old owner from continuing to use a resource after losing its authority. It also needs a recovery policy when the controller itself fails. These requirements touch acquisition and use, deployment state, and availability semantics; they are not a helper that belongs in a workload kind.

There is no demonstrated need in #1008 for that machinery. Revisit it when a concrete deployment needs long-lived active sandboxes, cannot accept maximum-age expiry, and cannot delegate lifecycle enforcement to its platform. Prefer a platform backend or optional operational component then; do not make a Docker cleanup requirement mandatory for every backend, including an in-process implementation.

## Infrastructure options

| Deployment | Practical approach | Limit that must be stated |
| --- | --- | --- |
| CI with a disposable, dedicated engine and storage | Destroy the execution environment after the job, with infrastructure responsible for failed jobs and abandoned environments | Destroying an application container or a CI workspace does not destroy resources on a shared or remote Docker engine |
| Persistent Docker host | Operator-managed systemd timer or other existing scheduler calls a backend sweep once | Requires an explicit engine target, allowed scope, age policy, and failure reporting; it may interrupt eligible active work |
| Developer machine using WSLC or Docker Desktop | Scheduled maintenance or explicit operator cleanup; accept documented residual storage if automation is unnecessary | A sleeping or unavailable machine cannot supply a wall-clock cleanup guarantee |
| ACAS | Service lifecycle policy as the primary mechanism, with a deployment-owned reconciliation job for missing policies | Auto-suspend does not establish auto-delete; creation and later policy configuration leave the measured gap tracked by #1011 |
| Kubernetes-based deployment | Use an existing CronJob for the sweep, or native lifecycle ownership where the sandbox is actually represented by Kubernetes resources | Kubernetes does not automatically own Docker resources created through an unrelated daemon |

The schedulers already exist. [systemd timers](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.timer.xml) activate service units and provide scheduling outside the application. [Windows Task Scheduler](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-start-page) runs programs on time and system-event triggers. [Azure Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs) supports scheduled executions and is one possible host for an ACAS reconciliation command. These are deployment alternatives, not three new integrations the suite must ship before cleanup is useful.

[Kubernetes CronJobs](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) can create duplicate or missed executions, so the cleanup operation still needs safe retries. Kubernetes [TTL-after-finished](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/) applies to completed or failed Jobs and their dependents. It is not a lifetime limit for running sandboxes and cannot reclaim an external Docker container merely because its creating application ran in Kubernetes.

An infrastructure teardown hook is sufficient only when that infrastructure owns the complete resource set and its lifetime. A supervisor can clean resources on an exclusively owned application's exit, but a replica-exit hook must not purge a conversation still served by another replica. Events can accelerate cleanup; periodic reconciliation or destruction of the enclosing environment provides recovery from missed events.

## Why a generic Docker prune job is not the whole answer

The current Docker backend creates detached workload containers with `sleep infinity`. Killing the Python process does not stop them. Docker's [container prune](https://docs.docker.com/reference/cli/docker/container/prune/) removes stopped containers, while [`--rm`](https://docs.docker.com/reference/cli/docker/container/run/) removes a container when it exits. Neither makes an orphaned running workload exit. [Network prune](https://docs.docker.com/reference/cli/docker/network/prune/) removes networks unreferenced by containers; a surviving proxy can keep one referenced.

A custom infrastructure script could handle those cases, but it would duplicate the backend's resource grouping, partial-create recovery, ownership checks, and deletion races. Reusing a backend sweep is worth maintaining because those rules follow the resource layout the backend creates. Reusing a scheduler is worth doing because those rules follow the deployment that operates it.

Scope selection is a guard against mistakes, not a credential boundary. Docker explains the broad power granted by [daemon access](https://docs.docker.com/engine/security/). A cleanup process with unrestricted engine credentials is not technically confined to one tenant just because its command supplies a scope. Where that distinction matters, separate engines, platform authorization, or a genuinely constrained API must provide it. Guest-controlled metadata must not be the authority for extending retention or selecting another deployment's resources.

## The bounded deliverable

[PR #1012](https://github.com/sokolaidev/maf-extensions/pull/1012), open when this record was written, already proposes `DockerSandboxBackend.reap(older_than, scope=...)` and `DockerReapResult`. Its description and README specify maximum-age semantics, immutable-ID deletion, complete-inventory checks, and workload/proxy/network accounting. That is the right architectural location for the primitive. This exploration is not a review of its implementation or an independent verification of its reported tests.

The remaining operator example should perform one sweep and exit. Require an explicit engine target and scope, require an explicit positive age, report successful removals and each failure, and return a failing exit status when inventory or cleanup was incomplete. Do not inherit an interactive user's changing Docker context as the deployment's identity, and do not turn a successful process exit into evidence of complete cleanup when `DockerReapResult.failures` is nonempty. A host can install that example with its existing scheduler and logging system. A small backend CLI is an option if the example reveals recurring wrapper code; a new runtime package is not necessary at the outset.

The deployment owns installing and enabling the schedule, its credentials, schedule health, and alerts. The example documents missed executions, retry behavior, and machine downtime. Backend tests own resource selection and deletion safety. One reference integration test should kill the serving application, start the separate cleanup process with an empty registry, and verify eligible resources disappear. Under the chosen maximum-age policy, also demonstrate that an eligible active sandbox may be removed. Test incomplete cleanup propagating as failure and a later retry succeeding. This proves the composition for that tested deployment; it does not establish an availability guarantee for every scheduler.

Documentation also needs to stop promising a universal timer. The lifecycle paragraph in `architecture.md` and the `SandboxPurger` docstrings describe platform auto-delete as a fallback without stating that Docker and WSLC lack it and ACAS must successfully install it. Correcting that claim is necessary even if the chosen deployment leaves cleanup manual. The suite should describe which mechanisms it supplies and which operational guarantee the host has configured.

## Proposed issue disposition

The live #1008 body now owns Docker cleanup and deferred runner design; #1009 also owns Docker cleanup. That overlap should be resolved before both become implementation queues. One coherent split would leave the backend primitive and its tests with #1009/#1012, and use #1008 for the ownership decision, corrected lifecycle guidance, and one tested operator deployment example. Alternatively, consolidate the Docker tracking under #1008. Neither choice needs a fifth subissue or an additional service.

Keep WSLC metadata and cleanup in [#1010](https://github.com/sokolaidev/maf-extensions/issues/1010), and ACAS policy recovery in [#1011](https://github.com/sokolaidev/maf-extensions/issues/1011). The latter records a live creation with 300-second auto-suspend and no auto-delete, unchanged after an HTTP 400 lifecycle update. It verifies effective policy, not elapsed-time destruction or a hidden service retention rule. No cloud-wide sweeper or lease service follows automatically from that result: first check whether policy can be installed with creation, then supply scoped recovery for the residual gap.

Completing #1008 should be allowed to mean that the suite deliberately delegates scheduling to infrastructure and proves one supported composition. Shipping a daemon should not be a prerequisite imposed by the word "runner" in an issue. The maintenance value is in the backend's safe cleanup and an honest operating contract; a general lease controller is not justified by the present evidence.

## Evidence and limits

Repository analysis used checkout `40b3430`, the current issue bodies, and PR #1012's description and README diff as of 2026-09-08. Platform behavior above is linked to primary documentation. The prior ACAS probe is recorded in #1011. This analysis did not run another live probe, deploy a scheduler, implement a lease, or independently review the full pending reaper diff. Scheduler examples, platform-native atomic policy installation, and any stronger timing guarantees require validation in their delivery work.
