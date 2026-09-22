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

The builder uses the lockfile, builds three workspace wheels and limits the Docker context to those wheels, hashed requirements and the probe application. Use an empty output directory initially. Publish the resulting application image to an approved registry and pass its immutable digest to the controller. The included image runs the verification application; an embedding application supplies its own code and command with the same pinned dependencies and supervisor.

Inspect the rendered plugin before applying it with an explicit kubeconfig/context. It pins the upstream revision and image digest, drops capabilities, disables the service-account token and uses `OnDelete` upgrades. It preserves upstream discovery, device allocation and CDI generation. The default `DEVICE_COUNT=1` advertises one allocation per eligible node; it is a scheduling choice, not measured VM capacity. Label only verified nodes with `hyperlight.dev/enabled=true` and `hyperlight.dev/hypervisor=kvm`. A cluster operator installs the plugin separately from application controllers. Restart, node replacement and stale-CDI recovery require operational validation before production use.

Create a dedicated application namespace with Restricted admission. Apply [controller-role.yaml](controller-role.yaml) in that namespace and bind it to the external controller's authenticated identity. Its namespace is one ownership authority: independent namespaces do not coordinate the same keys. The controller needs `kubectl` and an explicit kubeconfig/context; authentication and authorization remain the hosting application's responsibility. It needs no node, secret, exec or cluster-administration permission. Do not mount its credentials into application pods.

## Run an application

The public `maf_sandbox_hyperlight.kubernetes` module provides `HyperlightPodController`, `HyperlightPodTemplate` and `HyperlightPodCleanupPending`. Call `controller.run(key, kind, template)` from the trusted host, supplying a digest-pinned image and application argument vector. The controller creates the pod, supervises it and returns its runtime exit code and bounded diagnostics after cleanup. It does not transport guest source or expose a remote sandbox API. Execution results belong to the embedding application; diagnostics may contain application output and should be handled accordingly.

The application reads `HyperlightPodConfig.from_environment()` and constructs `HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)`. The PID 1 supervisor pins the application PID, ownership scope, generation and first execution policy. A different configuration or allowlist requires a new pod. The local Windows job and Linux delegated-cgroup paths remain the default and keep their per-worker memory guarantees. This explicit mode gives an aggregate container budget instead; the owner does not survive worker/container OOM.

The default template requests 500 millicores and limits CPU to one core, requests and limits memory to 4 GiB, and budgets 2 GiB of ephemeral storage. Private writable volumes hold caches, outputs and control files. The root filesystem is read-only, the user is non-root, capabilities are dropped and the pod has no service-account token or host mounts. PID 1 checks actual kernel controls before starting the application. Kubernetes enforces ephemeral storage asynchronously; it is not an immediate per-write filesystem quota.

The application is the sole host process. Normal worker disposal stops every other process in its private PID namespace before permitting another worker. Applications that need independent child services should put those services outside this container. Threads in the owning application are supported. Heap/stack sizes remain the adapter defaults.

The probe command supports `positive`, `codeact-fixed`, `codeact-per-spec`, `files`, `allowlist`, `timeout`, `cancel`, `owner-death`, `oom`, `output-limit`, `worker-death`, `native-hang` and `hold`:

```sh
uv run python scripts/hyperlight_aks.py run --namespace scoped-agents --kubeconfig /path/to/kubeconfig --context verified-cluster --scope tenant-user --thread conversation --agent analyst --image registry.example/hyperlight@sha256:REPLACE_WITH_DIGEST --mode positive
```

For registry-free development verification, the builder also emits `bundle.json.gz` and its SHA-256. Create an immutable ConfigMap with that exact binary file. Pass `--bundle-configmap NAME --bundle-sha256 DIGEST` and the pinned Python base image digest from the Dockerfile. An unprivileged init container verifies the bundle and installs hash-locked dependencies into the private volume. This path downloads packages during startup and is slower than a prebuilt application image; its measured bootstrap time is not Hyperlight initialization time.

## Failure and recovery

The authenticated attach stream carries lifecycle messages. Each native request has a deadline acknowledged by the external controller before submission. PID 1 watches owner/worker lifetime, deadlines, cgroup OOM events and a five-second controller lease. Losing the controller or active native execution retires PID 1; Linux kills the remaining processes in that PID namespace. Pods use `restartPolicy: Never` and a finite lifetime. There is no source replay.

An ordinary Python exception may reuse the worker. Queue expiry before submission preserves it. Active timeout/cancellation, native failure, failed reset, owner death and OOM retire the whole pod. The owning application can die before returning a tool error; its host must treat nonzero exit or lost connectivity as lost state and potentially uncertain execution, not a successful tool result.

Before pod creation, the controller atomically reserves a ConfigMap keyed by the complete ownership scope. Existing reservations refuse another owner. A finalizer and UID-preconditioned deletes retain cleanup state. A created pod requires a runtime termination record for the exact UID before releasing the reservation; API disappearance and synthetic `NodeLost`/unknown statuses do not count. A termination receipt is saved before deletion, allowing cleanup to resume after a controller restart. The ledger contains ownership lifecycle state, not tool source or a transcript.

A recognized server rejection of pod creation releases the reservation only after confirming no pod exists. The controller saves a rejection receipt before deleting the exact ledger UID; recovery can retry that deletion and returns startup failure code 71. Unknown kubectl errors, transport failures and `AlreadyExists` retain ownership. Fix the rejected configuration or quota before retrying the same scope.

On `HyperlightPodCleanupPending`, retain the scope and call `controller.recover(key, kind, retire=True)` or the CLI `recover` action with the same identity. Do not delete allocation records or force-remove finalizers to enable replacement. Missing pods without saved termination proof, unreachable nodes and ambiguous create failures need operator investigation and verified termination or node fencing. This integration does not provide node fencing or general distributed routing.

See the [backend guide](../../docs/sandbox/backends/hyperlight.md#aks-deployment-design) and [research record](../../docs/sandbox/research/hyperlight-backend.md) for measured evidence and remaining deployment work.
