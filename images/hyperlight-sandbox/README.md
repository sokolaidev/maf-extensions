# Hyperlight on AKS

This deployment adds one application owner to the pinned upstream [Hyperlight device plugin](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/tree/fc71b4501d23977fcc54f7be144d884fc8210667). Each pod owns one `(scope, thread_id, agent_id, kind)`, one resident VM and one active call. The application and protocol adapter run together. The host establishes these identifiers before accepting tool calls; model arguments cannot choose them. Several calls can reuse the pod within that ownership scope. The router defaults to disposal; select `Cleanup.RESET` explicitly for warm reuse.

The integration requires Linux x86-64 KVM, cgroup v2, finite CPU/memory/PID limits, disabled swap, CDI support and an operator-approved device plugin. Its workload satisfies Kubernetes Restricted admission. The upstream plugin remains trusted node infrastructure: it runs as root and writes kubelet/CDI directories. Installation must fit the cluster's actual admission policies. AKS Automatic acceptance and production operations are not implied by the Standard AKS probe.

## Build and install

Run from the repository root:

```sh
uv sync --locked
uv run python scripts/build_hyperlight_aks_image.py --output /tmp/hyperlight-build --tag hyperlight-sandbox:local
uv run python scripts/hyperlight_aks.py plugin --namespace hyperlight-system > /tmp/hyperlight-plugin.json
```

The builder uses the lockfile, builds three workspace wheels and limits the Docker context to those wheels, hashed requirements, the probe and build metadata. Use an empty output directory outside the checkout initially. `--tag` builds locally and verifies the immutable image ID returned by Docker. A failed build or smoke check leaves no `image-verification.json` success record. Add `--require-clean` for a release candidate; local development builds record whether the checkout has uncommitted changes.

`source.json` records the public repository, source commit, dirty flag and lockfile hash. `build-inputs.json` hashes the prepared inputs, including the Dockerfile, verifier and wheels. The image retains these files. `image-verification.json` binds that input manifest to the local image ID, installed package versions and smoke results. Its registry digest remains unset: a local Docker ID is not evidence that an image was published. These are unsigned build records, not authenticated source provenance; retain the context and verification record with the candidate, and verify your approved publisher's identity and provenance before promotion.

The Linux Hyperlight CI job builds from a clean checkout and retains the three JSON records in the `hyperlight-image-verification` artifact for 14 days. It does not publish an image or sign these records. When a core release reaches a dependent's ceiling, CI reports the pending adoption in its job summary and defers the image build and artifact until the range admits that core. Linux worker and KVM checks still run. Invalid ranges, unmet floors and ceilings older than the current core line fail the preflight. The normal builder and image smoke check always enforce dependency consistency.

The smoke check runs as the image's non-root user with no network, a read-only root filesystem, dropped capabilities, no privilege escalation and finite CPU, memory and PID limits. It checks payload hashes, workspace versions, dependency consistency and imports. It opens no hypervisor device. Its 256 MiB limit is only for packaging verification; it does not size a Hyperlight VM or establish AKS containment.

Publish the verified image ID to an approved registry and retain the registry's immutable manifest digest. Configure pull authorization outside application containers, verify the published artifact and its provenance through the approved registry workflow, then pass that digest to the controller. Registry publishing and provenance verification are separate deployment gates; the builder does not push images. The included image runs the verification application; an embedding application supplies its own code and command with the same pinned dependencies and supervisor.

Inspect the rendered plugin before applying it with an explicit kubeconfig/context. It pins the upstream revision and image digest, drops capabilities, disables the service-account token and uses `OnDelete` upgrades. It preserves upstream discovery, device allocation and CDI generation. The default `DEVICE_COUNT=1` advertises one allocation per eligible node; it is a scheduling choice, not measured VM capacity. A cluster operator installs the plugin separately from application controllers. Restart, node replacement and stale-CDI recovery require operational validation before production use.

## Supported platforms

Give eligible nodes their own node pool and label the pool in two steps. `hyperlight.dev/enabled=true` admits the device plugin; `hyperlight.dev/hypervisor=kvm` admits application pods. Create the pool with the first label only, for example `az aks nodepool add ... --labels hyperlight.dev/enabled=true`, install the plugin and run the report below. Add the second label once the report passes: `az aks nodepool update ... --labels hyperlight.dev/enabled=true hyperlight.dev/hypervisor=kvm`. That update replaces the pool's labels, so repeat every label the pool keeps. Pool labels survive node reimage and scale-out; labels applied to a single node with `kubectl label` do not. The integration never labels, configures or changes a node.

| VM size | Node OS | Kubernetes | Runtime | Measured |
|---|---|---|---|---|
| `Standard_D2ads_v5`, `Standard_D4ads_v5` | Ubuntu 24.04 (`AKSUbuntu-2404gen2containerd`) | 1.35 | containerd 2.x | Kubernetes 1.35.7, kernel `6.8.0-1067-azure`, containerd 2.3.3; `D4ads_v5` on node images 202609.09.0 and 202609.15.0 |
| `Standard_D4ads_v5` | Azure Linux 3.0 (`AKSAzureLinux-V3gen2`) | 1.35 | containerd 2.x | Kubernetes 1.35.7, kernel `6.6.150.1-1.azl3`, containerd 2.2.4, node image 202609.15.0 |

Every row was measured on pools with the default security type; Trusted Launch and confidential VM pools are not measured. Each row requires x86-64 with nested virtualization, cgroup v2, no swap and CDI enabled in containerd. B-series sizes do not offer nested virtualization. AKS Automatic, MSHV and Arm64 are outside the matrix.

Report the plugin-enabled nodes against this matrix before making them schedulable:

```sh
uv run python scripts/hyperlight_aks.py nodes --kubeconfig /path/to/kubeconfig --context verified-cluster
```

The report reads each node labelled `hyperlight.dev/enabled=true`: its size, node image, security type, OS, kernel, runtime, kubelet version and advertised allocation, and whether it already carries the application label. It exits nonzero when any node is outside the matrix, has a non-default security type or advertises no allocation. It needs node read access, which the application controller does not have.

The controller enforces the requirements it can observe, from inside the pod. Before starting the application, PID 1 requires x86-64, cgroup v2, the declared memory limit, no swap, finite CPU and PID limits and a `/dev/kvm` that the pod user can open and create a VM on. A node that fails any of these makes `supervise` raise `HyperlightPodPlatformError` with the reason, after confirming cleanup. A pod that cannot be scheduled, for example because no labelled node advertises a free allocation, raises `TimeoutError` with the scheduler's reason after its startup budget. A successful result carries the controls PID 1 observed in `HyperlightPodResult.platform`.

Re-run the probes below on a fresh node pool, and extend this table, before accepting a Kubernetes minor, node OS or node-image family, containerd major or VM family not listed here. A Kubernetes minor also needs the never-started cleanup check described under failure and recovery, because it depends on kubelet status text. The cluster's node OS upgrade channel can move a pool's node image within its family; the `nodes` report shows the running version.

Create a dedicated application namespace with Restricted admission. Apply [controller-role.yaml](controller-role.yaml) in that namespace and bind it to the external controller's authenticated identity. Its namespace is one ownership authority: independent namespaces do not coordinate the same keys. The controller needs `kubectl` and an explicit kubeconfig/context; authentication and authorization remain the hosting application's responsibility. It needs no node, secret, exec or cluster-administration permission. Do not mount its credentials into application pods.

## Run an application

The public `maf_sandbox_hyperlight.kubernetes` module provides `HyperlightPodController`, `HyperlightPodTemplate`, `HyperlightPodCleanupPending` and `HyperlightPodPlatformError`. Call `controller.supervise(key, kind, template)` from the trusted host, supplying a digest-pinned image and application argument vector. The controller creates the pod, supervises it and returns its exit code and bounded diagnostics after confirmed cleanup. It does not transport guest source or expose a remote sandbox API. Execution results belong to the embedding application; diagnostics may contain application output and should be handled accordingly.

The application reads `HyperlightPodConfig.from_environment()` and constructs `HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)`. The PID 1 supervisor pins the application PID, ownership scope, generation and first execution policy. The pod object names its scope only by digest: the controller sends the scope, thread, agent and kind in its first attach message, and PID 1 refuses an identity that does not match that digest. A different configuration or allowlist requires a new pod. The local Windows job and Linux delegated-cgroup paths remain the default and keep their per-worker memory guarantees. This explicit mode gives an aggregate container budget instead; the owner does not survive worker/container OOM.

The default template requests 500 millicores and limits CPU to one core, requests and limits memory to 4 GiB, and budgets 2 GiB of ephemeral storage. Private writable volumes hold caches, outputs and control files. The root filesystem is read-only, the user is non-root, capabilities are dropped and the pod has no service-account token or host mounts. PID 1 checks actual kernel controls before starting the application. Kubernetes enforces ephemeral storage asynchronously; it is not an immediate per-write filesystem quota.

The application is the sole host process. Normal worker disposal stops every other process in its private PID namespace before permitting another worker. Applications that need independent child services should put those services outside this container. Threads in the owning application are supported. Heap/stack sizes remain the adapter defaults.

The probe command supports `positive`, `codeact-fixed`, `codeact-per-spec`, `files`, `allowlist`, `timeout`, `cancel`, `owner-death`, `oom`, `output-limit`, `worker-death`, `native-hang` and `hold`:

```sh
uv run python scripts/hyperlight_aks.py supervise --namespace scoped-agents --kubeconfig /path/to/kubeconfig --context verified-cluster --scope tenant-user --thread conversation --agent analyst --image registry.example/hyperlight@sha256:REPLACE_WITH_DIGEST --mode positive
```

For registry-free development verification, the builder also emits `bundle.json.gz` and its SHA-256. Create an immutable ConfigMap with that exact binary file. Pass `--bundle-configmap NAME --bundle-sha256 DIGEST` and the pinned Python base image digest from the Dockerfile. An unprivileged init container verifies the bundle and installs hash-locked dependencies into the private volume. This path downloads packages during startup and is slower than a prebuilt application image; its measured bootstrap time is not Hyperlight initialization time.

## Upgrade and rollback

Keep the previous runtime digest, its build record, the application command, template settings and plugin manifest until a replacement passes acceptance. Validate each new digest with a fresh ownership scope on the intended node pool: run positive execution/reset, files, allowlist and failure probes, then measure the actual application's image pull, startup and memory/storage peaks. The packaging smoke check cannot replace those probes.

Change the trusted host's template for newly created pods. Let existing owners finish or retire them through the controller and confirm termination before reusing their scopes. An image update does not transfer a running VM's state. Roll back by restoring the previous digest and compatible template for new pods; an unresolved cleanup ledger still blocks replacement after rollback. The controller and the image exchange unversioned lifecycle messages, so run both from the same `maf-sandbox-hyperlight` release and upgrade or roll them back together.

Treat device-plugin upgrades separately. The rendered DaemonSet uses `OnDelete`, so changing its manifest does not restart existing plugin pods. Cordon and drain affected nodes under the operator's maintenance process, confirm owner cleanup, then replace plugin pods and verify registration, CDI contents and actual VM creation before returning nodes to service. Restore the prior manifest and repeat verification to roll back. A healthy device count alone does not establish device usability: the pinned upstream plugin checks device-path presence and does not repair missing or stale CDI during its health loop.

## Failure and recovery

The authenticated attach stream carries lifecycle messages. PID 1 makes itself non-dumpable, so the pod's other processes, which share its UID, cannot open its stdin to hold the stream open or forge controller messages. Each native request has a deadline acknowledged by the external controller before submission. PID 1 watches owner/worker lifetime, deadlines, cgroup OOM events and a five-second controller lease. Losing the controller or active native execution retires PID 1; Linux kills the remaining processes in that PID namespace. Pods use `restartPolicy: Never` and a finite lifetime. There is no source replay.

An ordinary Python exception may reuse the worker. Queue expiry before submission preserves it. Active timeout/cancellation, native failure, failed reset, owner death and OOM retire the whole pod. The owning application can die before returning a tool error; its host must treat nonzero exit or lost connectivity as lost state and potentially uncertain execution, not a successful tool result.

Before pod creation, the controller atomically reserves a ConfigMap keyed by the complete ownership scope. Existing reservations refuse another owner. A finalizer and UID-preconditioned deletes retain cleanup state. A started pod requires a runtime termination record for the exact UID before releasing the reservation; API disappearance and `NodeLost` do not count. A termination receipt is saved before deletion, allowing cleanup to resume after a controller restart. The ledger contains ownership lifecycle state, not tool source or a transcript.

A recognized server rejection of pod creation releases the reservation only after confirming no pod exists. The controller saves a rejection receipt before deleting the exact ledger UID; recovery can retry that deletion and returns startup failure code 71. Unknown kubectl errors, transport failures and `AlreadyExists` retain ownership. Fix the rejected configuration or quota before retrying the same scope.

Recovery records startup failure code 71 for pods that never start once cleanup is proved. PID 1 exits 78 when it refuses the node; the termination receipt keeps its reason, and `recover` returns 78. A startup timeout still raises `TimeoutError` after successful cleanup. An unassigned pod requires a recorded deletion, which prevents subsequent node binding. An assigned pod requires the kubelet finalization marker with no container execution history, a failed phase, and confirmation that its pod sandbox is gone. That finalization evidence remains valid if deletion later advances the pod metadata generation. Bootstrap containers must also have termination proof. Waiting-container status, missing finalization evidence and other unknown-container states retain ownership. The narrow kubelet marker is covered on Kubernetes 1.35.7; its [finalization code](https://github.com/kubernetes/kubernetes/blob/v1.35.7/pkg/kubelet/status/status_manager.go) and [binding guard](https://github.com/kubernetes/kubernetes/blob/v1.35.7/pkg/registry/core/pod/storage/storage.go) define these checks.

On `HyperlightPodCleanupPending`, retain the scope and call `controller.recover(key, kind, retire=True)` or the CLI `recover` action with the same identity. Do not delete allocation records or force-remove finalizers to enable replacement. Missing pods without saved termination proof, unreachable nodes and ambiguous create failures need operator investigation and verified termination or node fencing. This integration does not provide node fencing or general distributed routing.

See the [backend guide](../../docs/sandbox/backends/hyperlight.md#aks-deployment-design) and [research record](../../docs/sandbox/research/hyperlight-backend.md) for measured evidence and remaining deployment work.
