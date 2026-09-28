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

The smoke check runs as the image's non-root user with no network, a read-only root filesystem, dropped capabilities, no privilege escalation and finite CPU, memory and PID limits. The host enforces a 60-second execution deadline and a combined 1 MiB stdout/stderr limit, disables container logging, and force-removes the named container after every attempt. Docker creation and removal each have a 10-second deadline; cleanup failure refuses verification. It checks payload hashes, workspace versions, dependency consistency and imports. It opens no hypervisor device. Its 256 MiB limit is only for packaging verification; it does not size a Hyperlight VM or establish AKS containment.

Publish the verified image ID to an approved registry and retain the registry's immutable manifest digest. Configure pull authorization outside application containers, verify the published artifact and its provenance through the approved registry workflow, then pass that digest to the controller. Registry publishing and provenance verification are separate deployment gates; the builder does not push images. The included image runs the verification application; an embedding application supplies its own code and command with the same pinned dependencies and supervisor.

Inspect the rendered plugin before applying it with an explicit kubeconfig/context. It pins the upstream revision and image digest, drops capabilities, disables the service-account token and uses `OnDelete` upgrades. It preserves upstream discovery, device allocation and CDI generation. The default `DEVICE_COUNT=1` advertises one allocation per eligible node; it is a scheduling choice, not measured VM capacity. A cluster operator installs the plugin separately from application controllers. Restart, node replacement and stale-CDI recovery require operational validation before production use.

## Verify a published runtime

Before promoting a runtime digest, run `scripts/verify_hyperlight_aks_image.py` on the trusted host. Supply the approved image reference, full source commit, source branch/tag ref, exact signing workflow certificate identity (including its ref), and the expected SHA-256 of the prepared `build-inputs.json`. Choose these values from the reviewed build and publisher policy, not from claims inside the candidate. The source repository is fixed to `sokolaidev/maf-extensions`; reusable signing workflows may live in a separately approved repository.

```sh
python scripts/verify_hyperlight_aks_image.py \
  --image "$IMAGE_DIGEST_REF" \
  --signer-identity "$SIGNER_IDENTITY" \
  --source-revision "$SOURCE_REVISION" \
  --source-ref "$SOURCE_REF" \
  --build-inputs-sha256 "$BUILD_INPUTS_SHA256" \
  --output "$EVIDENCE_DIR/runtime-provenance.json"
```

Use a GitHub CLI version supporting the [attestation verification policy flags](https://cli.github.com/manual/gh_attestation_verify), with GitHub and registry authentication configured on the host, and Docker with Linux/amd64 support. The verifier requires GitHub Actions SLSA v1 provenance, the exact signer identity, source revision/ref and GitHub OIDC issuer, and refuses self-hosted runners. It verifies the signature before running image code, then pulls the same digest, resolves its immutable local image ID and applies the builder's restricted packaging check. The payload must match the expected build-input hash and clean source revision. Missing attestations, unsupported CLI flags and verification failures refuse promotion; there is no unsigned fallback.

The output retains the verified attestation bundles, expected policy, image identities, timestamp and packaging report. A failed attempt removes any previous success record at that output path. Keep these records and their signed bundles in operator-controlled storage for the image's supported lifetime; the local JSON report itself is unsigned. Records may contain private registry identifiers and should not be committed to this public repository. This command does not publish/sign images, configure cluster admission, or establish KVM execution, lifecycle acceptance or plugin provenance. Admission must independently enforce the accepted policy and digest; a saved report is not an admission credential. The existing unsigned candidate and CI records cannot satisfy this gate until a trusted publishing workflow produces matching attestations.


### Exercise signing and verification in CI

Dispatch the workflow-test suite against a reviewed branch to build and attest a temporary candidate, then verify it on a fresh GitHub-hosted runner:

```sh
gh workflow run workflow-tests.yml --ref YOUR_BRANCH -f hyperlight_provenance=true
```

This explicit selection calls [the signed-runtime integration workflow](../../.github/workflows/hyperlight-provenance.yml). The build job publishes clean workspace wheels into a temporary loopback-only Docker registry and signs the image's published SHA-256 digest with GitHub OIDC. The registry's exact manifests and blobs transfer through an Actions artifact to a second temporary registry on a fresh runner. The verifier fetches the image by its original digest and obtains the real signature bundle from GitHub; it has read-only attestation permissions and no signing token.

The test requires the exact signer, source commit, source ref and prepared build-input hash. Refusal cases change each policy value and present an unsigned image with a modified manifest. Every refusal must remove a seeded success record, and a final positive check must still pass. Build and verification artifacts retain source metadata, build inputs, verified signature bundles and case results. Temporary registry containers are removed on success or failure, and the transferred registry artifact expires after one day.

The same job freshly verifies the candidate again and generates namespace admission rules, then tests them against a disposable KIND Kubernetes 1.35 API server. Server-side probes cover approved images, tags, changed digests/registries, extra containers, init containers, native sidecars, ephemeral-container updates, ordinary image updates, image volumes and namespace isolation. A scheduling gate prevents the persistent probe pod from running. The cluster is removed before a successful admission report is written.

This proves real GitHub OIDC signing, OCI image verification and runtime image admission in the isolated test cluster. It does not test ACR access, approve a production publisher, install or validate production AKS admission, or execute a Hyperlight guest.

## Prepare runtime admission

Use `scripts/prepare_hyperlight_admission.py` on the trusted operator host to turn reviewed publisher policy into an exact image allowlist for a dedicated application namespace. This targets Kubernetes 1.35 and its native [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/) API. The operator supplies a JSON policy with these fields; replace the example values with independently approved build metadata:

```json
{
  "namespace": "hyperlight-apps",
  "candidates": [
    {
      "image": "registry.example/runtime@sha256:APPROVED_DIGEST",
      "signer_identity": "https://github.com/OWNER/REPO/.github/workflows/build.yml@refs/heads/main",
      "source_revision": "APPROVED_FULL_COMMIT",
      "source_ref": "refs/heads/main",
      "build_inputs_sha256": "APPROVED_BUILD_INPUTS_HASH"
    }
  ]
}
```

```sh
python scripts/prepare_hyperlight_admission.py --policy operator-policy.json --output promotion.json
jq '.admission' promotion.json > admission.json
kubectl --kubeconfig /path/to/kubeconfig --context verified-cluster apply --dry-run=server -f admission.json
kubectl --kubeconfig /path/to/kubeconfig --context verified-cluster apply -f admission.json
```

Review the generated manifest before applying it. The generator freshly verifies every candidate's signature, source and restricted packaging check, and emits nothing unless all candidates pass. A failed attempt removes the previous bundle at the output path. The output contains the admission resources and fresh verification records with signed bundles; keep it in operator-controlled storage for the supported image lifetime. Its enclosing JSON is unsigned and must not be accepted as an authorization credential from an untrusted caller.

The policy and binding deny Pod creation and updates when any normal, init, native sidecar or ephemeral container names an image outside the verified registry/repository/digest allowlist. OCI image volumes are denied. Namespace matching uses the request namespace, so changing pod labels cannot opt out. Policy evaluation fails closed. Cluster administrators must protect both admission resources from application identities, and verify the policy's type-check status and actual positive/negative API requests before granting application access to the namespace. Other namespaces, including the upstream device plugin's namespace, are outside this runtime policy.

Admission does not evict existing pods. Keep both old and new verified candidates in the policy during an upgrade or rollback; removing a digest can also deny updates to pods that still use it. The generator accepts up to eight candidates and verifies all of them again. Kubelet registry pull authorization remains an operator prerequisite; this tool creates no registry credentials or role assignments. It neither approves a production publisher nor establishes provenance for the upstream plugin.

### Check admission on an existing AKS cluster

Run the same admission matrix against an explicit cluster context with `scripts/check_hyperlight_admission_aks.py`. Supply the existing operator policy with a fresh namespace name beginning `hyperlight-admission-` (at most 50 characters), and use an operator identity authorized to create the temporary resources and impersonate their service accounts:

```sh
uv run python scripts/check_hyperlight_admission_aks.py \
  --policy operator-policy.json \
  --kubeconfig /path/to/kubeconfig --context verified-cluster \
  --output admission-evidence.json
```

The command freshly verifies the candidate before cluster changes, refuses resource-name collisions, and creates two temporary Restricted namespaces plus the generated policy/binding. Temporary application and controller service accounts exercise authorization; the controller gets only the repository's existing namespace Role. The probe checks 30 authorization decisions: neither identity may alter the installed admission resources (including `deletecollection`) or create pods outside the test namespace. Secret checks require resource-type denial of `get`, `list` and `watch`; they do not exclude grants restricted to particular secret names. Admission-resource `update`, `patch` and `delete` checks target the installed policy and binding names, including name-restricted RBAC grants; `create` and `deletecollection` checks target the resource type. It uses the same 17 admission cases as KIND and keeps the probe pod behind a scheduling gate, so it neither pulls the runtime on a node nor executes a guest. Reuse separately recorded pull and guest-execution evidence for the relevant candidate instead of treating API admission as execution.

Cleanup verifies the probe ownership label and original creation UID, then conditions each deletion on that UID and the fetched resource version. A replaced resource is preserved even if it retains the probe label. A missing create response or UID also preserves any surviving resource for manual inspection and cleanup. Failures identify the resource and underlying error. A concurrent change fails cleanup without retrying the deletion; success is written only after all probe resources are absent. The output includes full signed verification/promotion material and can contain private registry identifiers; retain it in operator-controlled storage and publish only a redacted summary. The result covers the temporary namespaces and service accounts only. It does not validate the serving namespace or actual application/controller identities, including their identity-specific bindings. [#1539](https://github.com/sokolaidev/maf-extensions/issues/1539) still requires acceptance in the intended application namespace with its real identities and recorded cleanup or retention.

## Supported platforms

Give eligible nodes their own node pool and label the pool in two steps. `hyperlight.dev/enabled=true` admits the device plugin; `hyperlight.dev/hypervisor=kvm` admits application pods. Create the pool with the first label only, for example `az aks nodepool add ... --labels hyperlight.dev/enabled=true`, install the plugin and run the report below. Add the second label once the report passes: `az aks nodepool update ... --labels hyperlight.dev/enabled=true hyperlight.dev/hypervisor=kvm`. That update replaces the pool's labels, so repeat every label the pool keeps. Pool labels survive node reimage and scale-out; labels applied to a single node with `kubectl label` do not. The integration never labels, configures or changes a node.

| VM size | Node image | OS | Kernel | Kubelet | containerd | runc |
|---|---|---|---|---|---|---|
| `Standard_D4ads_v5` | `AKSUbuntu-2404gen2containerd-202609.15.0` | Ubuntu 24.04.5 LTS | `6.8.0-1067-azure` | v1.35.7 | 2.3.3-2 | 1.4.3-2 |
| `Standard_D4ads_v5` | `AKSUbuntu-2404gen2containerd-202609.09.0` | Ubuntu 24.04.5 LTS | `6.8.0-1067-azure` | v1.35.7 | 2.3.3-2 | 1.4.3-2 |
| `Standard_D4ads_v5` | `AKSAzureLinux-V3gen2-202609.15.0` | Microsoft Azure Linux 3.0 | `6.6.150.1-1.azl3` | v1.35.7 | 2.2.4 | 1.3.6 |

Each row is one live observation, and the `nodes` report verifies a node only when it matches a row exactly. Guest execution was also measured on `Standard_D2ads_v5` with Ubuntu 24.04 and Kubernetes 1.35.7, but that run did not record the node image, so it is not a row.

Every row was measured on pools with the default security type; Trusted Launch and confidential VM pools are not measured. Each row requires x86-64 with nested virtualization, cgroup v2, no swap and CDI enabled in containerd. B-series sizes do not offer nested virtualization. AKS Automatic, MSHV and Arm64 are outside the matrix.

Report the plugin-enabled nodes against this matrix before making them schedulable:

```sh
uv run python scripts/hyperlight_aks.py nodes --kubeconfig /path/to/kubeconfig --context verified-cluster
```

The report reads each node labelled `hyperlight.dev/enabled=true`: its size, node image, security type, OS, kernel, runtime, kubelet version and advertised allocation, and whether it already carries the application label. It exits nonzero when any node matches no row, has a non-default security type or advertises no allocation, and names the fields that differ from the nearest row. It needs node read access, which the application controller does not have.

Node status does not include runc, and OS patching can change it without a new node image, so the report cannot compare it. It prints the nearest row's value as `measured_runc`. Compare that with the node's own runc before adding the application label, using a one-off debug pod that runs the host binary:

```sh
kubectl debug node/NODE -n hyperlight-system --profile=sysadmin --image=python:3.13.12-slim-bookworm@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6 -- chroot /host runc --version
```

The `sysadmin` profile is privileged, so run it as a node operator in a namespace without Restricted admission, and delete the debug pod afterwards. A different runc version needs its own probe run and row.

The controller enforces the requirements it can observe, from inside the pod. Before starting the application, PID 1 requires x86-64, cgroup v2, the declared memory limit, no swap, finite CPU and PID limits and a `/dev/kvm` that the pod user can open and create a VM on. A node that fails any of these makes `supervise` raise `HyperlightPodPlatformError` with the reason, after confirming cleanup. A pod that cannot be scheduled, for example because no labelled node advertises a free allocation, raises `TimeoutError` with the scheduler's reason after its startup budget. A pod that is scheduled but cannot start names the blocked container and its reason instead, such as `bootstrap: ErrImagePull` on the bundle path. A successful result carries the controls PID 1 observed in `HyperlightPodResult.platform`.

Re-run the probes below on a node pool with the new version, and add its row, before accepting any node image, kernel, kubelet or containerd version not listed here. A Kubernetes minor also needs the never-started cleanup check described under failure and recovery, because it depends on kubelet status text. The cluster's node OS upgrade channel moves node images and kernels regularly, so the report goes red after an upgrade until the new version is measured.

Create a dedicated application namespace with Restricted admission. Apply [controller-role.yaml](controller-role.yaml) in that namespace and bind it to the external controller's authenticated identity. Its namespace is one ownership authority: independent namespaces do not coordinate the same keys. The controller needs `kubectl` and an explicit kubeconfig/context; authentication and authorization remain the hosting application's responsibility. It needs no node, secret, exec or cluster-administration permission. Do not mount its credentials into application pods.

## Run an application

The public `maf_sandbox_hyperlight.kubernetes` module provides `HyperlightPodController`, `HyperlightPodTemplate`, `HyperlightPodCleanupPending`, `HyperlightPodPlatformError`, `HyperlightPodProtocolMismatch` and `HyperlightPodReserved`. Call `controller.supervise(key, kind, template)` from the trusted host, supplying a digest-pinned image and application argument vector. The controller creates the pod, supervises it and returns its exit code, retirement reason and bounded diagnostics after confirmed cleanup. It does not transport guest source or expose a remote sandbox API. Execution results belong to the embedding application; diagnostics may contain application output and should be handled accordingly.

The application reads `HyperlightPodConfig.from_environment()` and constructs `HyperlightSandboxConfig(pod=binding, max_worker_memory_bytes=None)`. The PID 1 supervisor pins the application PID, ownership scope, generation and first execution policy. The pod object names its scope only by digest: the controller sends the scope, thread, agent and kind in its first attach message, and PID 1 refuses an identity that does not match that digest. That message also carries a secret the controller generated when it created the pod. The pod spec holds only the secret's digest, so another principal with `pods/attach` cannot bind the pod by attaching first. A different configuration or allowlist requires a new pod. The local Windows job and Linux delegated-cgroup paths remain the default and keep their per-worker memory guarantees. This explicit mode gives an aggregate container budget instead; the owner does not survive worker/container OOM.

The default template requests 500 millicores and limits CPU to one core, requests and limits memory to 4 GiB, and budgets 2 GiB of ephemeral storage. Private writable volumes hold caches, outputs and control files. The root filesystem is read-only, the user is non-root, capabilities are dropped and the pod has no service-account token or host mounts. PID 1 checks actual kernel controls before starting the application. Kubernetes enforces ephemeral storage asynchronously; it is not an immediate per-write filesystem quota.

The application is the sole host process. Normal worker disposal stops every other process in its private PID namespace before permitting another worker. Applications that need independent child services should put those services outside this container. Threads in the owning application are supported. Heap/stack sizes remain the adapter defaults.

The probe command supports `positive`, `codeact-fixed`, `codeact-per-spec`, `files`, `allowlist`, `timeout`, `cancel`, `owner-death`, `oom`, `output-limit`, `worker-death`, `native-hang`, `hold` and `continuity`, which counts guest calls for 150 seconds so an interruption can be checked against the count:

```sh
uv run python scripts/hyperlight_aks.py supervise --namespace scoped-agents --kubeconfig /path/to/kubeconfig --context verified-cluster --scope tenant-user --thread conversation --agent analyst --image registry.example/hyperlight@sha256:REPLACE_WITH_DIGEST --mode positive
```

For registry-free development verification, the builder also emits `bundle.json.gz` and its SHA-256. Create an immutable ConfigMap with that exact binary file. Pass `--bundle-configmap NAME --bundle-sha256 DIGEST` and the pinned Python base image digest from the Dockerfile. An unprivileged init container verifies the bundle and installs hash-locked dependencies into the private volume. This path downloads packages during startup and is slower than a prebuilt application image; its measured bootstrap time is not Hyperlight initialization time.

## Upgrade and rollback

Upgrade the runtime and the device plugin separately. Until a replacement passes, keep the previous runtime digest and its verification record, the controller release it ran with, the application command, the template settings and the plugin manifest. The steps below were measured on a Standard AKS pool from the Ubuntu row of the platform matrix, with runtime 0.5.0 and 0.6.0 and plugins `51d7dab` and `fc71b45`, in [#1512](https://github.com/sokolaidev/maf-extensions/issues/1512), [#1513](https://github.com/sokolaidev/maf-extensions/issues/1513) and [#1514](https://github.com/sokolaidev/maf-extensions/issues/1514). The times quoted are those runs, not guarantees.

### Runtime

1. Verify the candidate digest as described under [Verify a published runtime](#verify-a-published-runtime). This is a production gate the measured runs did not exercise: their candidates were unsigned ([#1424](https://github.com/sokolaidev/maf-extensions/issues/1424)), so they were pulled by digest and checked against their build records instead.
2. Validate it with fresh ownership scopes on the intended pool. `positive`, `files` and `allowlist` exit 0; `timeout`, `cancel`, `owner-death`, `worker-death` and `output-limit` retire the pod with 70; `oom` ends with 137. Measure the actual application's pull, startup and memory peaks as well. The probe measured a pull under 3 seconds, acquisition about 2.1 seconds and a memory peak about 1.91 GB of the 4 GiB limit. The packaging smoke check cannot replace these probes.
3. Switch the trusted host to the candidate image and to the controller from the same `maf-sandbox-hyperlight` release. The controller puts its lifecycle protocol number in the pod binding, and an image that carries the check refuses any other number before starting the application: PID 1 exits 76, and `supervise` raises `HyperlightPodProtocolMismatch` naming both numbers once cleanup is confirmed. Images from 0.7.0 and earlier have no check. The one such pair measured, a 0.5.0 image under a 0.6.0 controller, exited 71 with no reason, the same result as a pod that never started; another older pair may fail differently, or not at all.
4. Let existing owners finish, or retire them through their controller. A new owner on a scope that is still held is refused with `HyperlightPodReserved` before anything is created. Once the earlier owner's cleanup is confirmed, the scope accepts the candidate. New pods run the selected digest, and no VM state carries over from the previous pod.
5. To roll back, restore the previous digest, its template and its controller release. A reservation left by one release was recovered by the other release's `recover` in both directions between 0.5.0 and 0.6.0; a 0.5.0 controller cannot return the retirement reason, which it predates.

### Device plugin

The rendered DaemonSet uses `OnDelete`, so a changed manifest leaves every node on its old plugin until that node's plugin pod is deleted, and `kubectl rollout status` refuses to wait on it. Replace one node at a time:

1. Apply the new manifest. `plugin-status` (below) now fails each node, naming the pod's older revision and its digest.
2. Cordon the node and drain it with `kubectl drain NODE --ignore-daemonsets --delete-emptydir-data --force`. `--force` is required because the controller's pods declare no Kubernetes controller. Eviction retires each owner with exit 70 and reason `pod termination requested`, and the controller confirms cleanup; the drains measured took 10 to 13 seconds. If the owning controller is gone, the pod waits on its finalizer and the drain waits with it, until its own timeout. Run `recover` with the same identity; do not remove the finalizer. The plugin can be replaced before that: the reservation holds across the swap, a second owner on that scope is refused with `HyperlightPodReserved`, and `recover` completes the earlier owner afterwards.
3. Record the checksum of the CDI spec the running plugin wrote. `kubectl debug` does not attach without `-i`; it prints the name of the pod it created, `node-debugger-NODE-…`, which the next commands take as `DEBUG_POD`. Read `/host/run/cdi`, not `/host/var/run/cdi`, which is an absolute symlink.

   ```sh
   kubectl debug node/NODE -n hyperlight-system --profile=general --image=python:3.13.12-slim-bookworm@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6 -- sha256sum /host/run/cdi/hyperlight.json
   kubectl -n hyperlight-system wait --for=jsonpath='{.status.phase}'=Succeeded pod/DEBUG_POD --timeout=120s
   kubectl -n hyperlight-system logs DEBUG_POD
   kubectl -n hyperlight-system delete pod DEBUG_POD
   ```

4. Delete the node's plugin pod and run `plugin-status` until this node's row reports `verified`. The command exits nonzero until every plugin-enabled node has been replaced, so read the node's row and its `reasons` rather than the exit code. A ready plugin pod is not yet an advertised device, and one reading is not enough: one replacement read allocation 0 at 8 seconds and 1 at about 24, and another advertised at 5 seconds, dropped to 0 and returned within about 20.
5. While the node is still cordoned, read the checksum again with the commands from step 3 and compare. The plugin rewrites the spec at start; its content did not change between these two plugins.
6. Check guest execution on the node, which can only happen once it is uncordoned: a cordoned node refuses the controller's pods, and the probe then waits out its startup budget, about 200 seconds, and raises `TimeoutError` naming the node as unschedulable. First pause new sessions on the trusted hosts; otherwise an application pod can take the node's allocation before the probe does. The controller's pods select any node labelled `hyperlight.dev/hypervisor=kvm`, so on a pool with more than one node, also cordon the other plugin-enabled nodes, noting which were cordoned already; otherwise the probe can pass on an untouched node. Their running owners are unaffected. Then uncordon the node and run a `positive` probe. If it passes, uncordon only the nodes you cordoned for it and resume new sessions. If it fails, cordon the node again. The measured pools had one node, so the multi-node gate is untested.
7. To roll back, run `plugin-status --image PREVIOUS_REFERENCE` before restoring anything: it verifies each node still running the previous plugin, and those nodes need nothing. Then apply the previous manifest in place of step 1, and repeat steps 2 to 6 on every node that reports the new plugin.

Check which plugin image each node actually runs:

```sh
uv run python scripts/hyperlight_aks.py plugin-status --namespace hyperlight-system --kubeconfig /path/to/kubeconfig --context verified-cluster
```

The report lists every node labelled `hyperlight.dev/enabled=true` with its advertised allocation, cordon state and every plugin pod on it: the digest containerd resolved, readiness, restarts, whether it is terminating, controlled by the DaemonSet and on its newest revision. Only controlled pods count toward the verdict. It exits nonzero when a node has no single ready plugin pod, carries a plugin-labelled pod the DaemonSet does not control, runs a pod whose revision is older than the DaemonSet's newest one, resolved a digest other than the expected one, or advertises no allocation. The expected digest is the DaemonSet's. To check a node against a rollback target before the DaemonSet is restored, pass `--image` with its digest-pinned reference; the pod's older revision is then reported but does not fail the node. It reads pods, nodes, the DaemonSet and its ControllerRevisions only. Right after an apply it refuses until the DaemonSet controller has observed the new template; run it again. It does not read CDI files or create a VM, so a verified row is not device usability.

### When a candidate fails

- **A runtime digest that does not pull** fails the pod's startup: `supervise` raises `TimeoutError` after its startup budget, about 200 seconds, with cleanup confirmed. The reason names the container whose image did not pull, which on the bundle path is the `bootstrap` init container. The same scope then accepts the previous candidate.
- **A plugin digest that does not pull** fails closed: the node's allocation fell to 0 within 14 seconds, so no application pod lands there. `OnDelete` does not replace the stuck pod after the manifest is restored; delete it, and the node advertised again 16 seconds later.
- **A missing or stale CDI spec** is invisible to `plugin-status` and to the plugin's own health loop ([#1423](https://github.com/sokolaidev/maf-extensions/issues/1423)). Application pods fail with `CreateContainerError: … unresolvable CDI devices`, which `supervise` reports after its startup budget. Restarting the plugin pod rewrote the spec.

### Not yet measured

These runs did not cover Azure Linux pools, a kubelet restart or several nodes during maintenance, or a controller and image pair across 0.6.0 and a later release, whose lifecycle handshake has changed since. Plugin images carry no attestation ([#1424](https://github.com/sokolaidev/maf-extensions/issues/1424)).

## Failure and recovery

The authenticated attach stream carries lifecycle messages. PID 1 makes itself non-dumpable, so the pod's other processes, which share its UID, cannot open its stdin to hold the stream open or forge controller messages. Each native request has a deadline acknowledged by the external controller before submission. PID 1 watches owner/worker lifetime, deadlines, cgroup OOM events and a five-second controller lease. Losing the controller or active native execution retires PID 1; Linux kills the remaining processes in that PID namespace. Pods use `restartPolicy: Never` and a finite lifetime. There is no source replay.

### Controller interruptions

By default the first break in the controller's attach retires the pod. A template with `recovery_seconds` set (1 to 600, and shorter than the session) keeps the session when the attach breaks and a new one arrives within that many seconds, for example across a kubelet restart. The pod then keeps stdin open after the first attach ends (`stdinOnce: false`), so a later attach reaches the same PID 1 and the guest keeps its in-memory state.

Every attacher writes into that one stdin. So in this mode each controller message after `hello` carries an HMAC made with the pod's secret and a counter above every counter PID 1 has accepted. PID 1 retires on any other message and on two malformed lines in a row. It drops a single malformed line, because a dropped attach can leave a half-written frame, and the controller writes a newline before its first message on a new attach. A principal with `pods/attach` can still end the session this way, but it cannot keep the session alive or act as its controller.

PID 1 admits no new call once the controller has been silent for five seconds. The call fails with `HyperlightPodDetached` before it reaches the worker, and the sandbox stays usable. A call that is already running keeps its deadline. PID 1 enforces that deadline, and while detached the controller enforces it too, by deleting the pod if it passes before a new attach resumes. The controller attaches again only to the same pod UID, generation and container ID, with `restartCount` 0. Events PID 1 writes without a connected attach reach only the container log, so a new attach's resume carries PID 1's call sequence and active deadline, and the controller adopts them. A still-connected but stalled attach can deliver buffered events when traffic returns. The attach stream never carries guest source, so nothing is replayed, and the application receives its call results locally throughout.

The two sides count the window from different points. The controller counts from the moment its attach ends; when the window passes without a resume, it stops trying and deletes the pod. PID 1 counts from the last controller message it accepted, plus the five seconds that message kept it fresh (before the first `hello`, from the end of its 60-second startup allowance), and retires with `controller recovery window expired` if nothing has resumed it by then, or with `controller never sent its hello` if no `hello` ever arrived. After a promptly ended attach, the controller's bound usually passes a few seconds before PID 1's. A hung attach can reverse that order: PID 1's window keeps running while the controller waits for its transport to end. Whichever bound passes first ends the session. `HyperlightPodResult.interruptions` lists why each attach the session survived ended; a stall recovered on the same attach adds no entry. A restarted controller no longer holds the secret, so it cannot resume and retires the pod as before. On Standard AKS 1.35.7 a new attach reached PID 1 again 5 to 10 seconds after a kubelet restart was requested, so use a window well above that.

#### Measured controller-to-API partitions

The [#1518](https://github.com/sokolaidev/maf-extensions/issues/1518) measurements on 2026-09-28 used one temporary, tainted Standard AKS 1.35.7 `Standard_D2ads_v5` user node, Ubuntu 24.04.5, kernel `6.8.0-1067-azure`, containerd `2.3.3-2`, the pinned device plugin above and kubectl 1.35.7. The application ran as UID 65534 with the normal Restricted pod settings. Installed versions were core 0.44.0, Hyperlight adapter 0.6.0 with the withdrawn-call fix, CodeAct 0.21.1, Python 3.13.12 and the matching Hyperlight SDK/backend/guest 0.7.0 trio. The fixed adapter wheel replaced only the adapter in the original locked bundle; the measured core and dependencies stayed unchanged.

The controller's kubectl processes ran in a dedicated local container. Bidirectional `iptables DROP` rules targeted the API server's address inside that container's network namespace, including established connections. Packet counters confirmed loss; no attach was killed to induce it, and no API response was mocked. A separate API connection observed the pod and a node observer read its exact cgroups and process start times. These are local live measurements against AKS, not a hosted CI run.

| Case | Measured outcome |
| --- | --- |
| Short partition, recovery window 60 s | 15.6 s of packet loss. New calls were refused with `HyperlightPodDetached`; the first completed call after healing arrived 0.28 s later on the same attach. Pod UID, container ID, PID 1, application and worker process identities were unchanged, with `restartCount` 0. The guest completed 223 consecutive counter increments, with 24 refused attempts and no reset; exit 0 and an empty `interruptions` tuple. |
| Prolonged partition, recovery window 20 s | Two runs held the partition for about 129 s, one before and one after the withdrawn-call fix. PID 1 logged `controller recovery window expired` after 24.2–24.5 s, exited 70, and left no pod processes or cgroups. While traffic was still blocked, `supervise` raised `HyperlightPodCleanupPending`; an independently connected controller received `AlreadyExists` for the same scope and the reservation UID stayed unchanged. After healing, `recover_exit` confirmed exit 70 and released ownership; a replacement got a fresh pod UID and completed successfully in each run. |
| Hung attach and API reads | The attaches ended 55.5–56.0 s after packet loss began, well after PID 1 retired. The subsequent pod reads and cleanup ownership lookup each failed after about 10.3 s; `HyperlightPodCleanupPending` arrived after 76.3 s in the fixed run and 87.5 s in the baseline. The 15 s subprocess limit remained an outer bound, not the observed request duration. |

A stalled attach can retain buffered lifecycle frames. PID 1 emits the matching `end` when withdrawing an unacknowledged `begin`, so the controller clears that deadline before accepting another call on the same stream. Without it, the short partition retired the session with `invalid or overlapping controller deadline` after connectivity returned. The regression test buffers both directions of one surviving attach and verifies that the next call succeeds.

The observed attach delay is not a portable timeout guarantee: client transport, TCP retry state and intermediary behavior can change it. It does not extend the useful recovery window, which PID 1 measures independently from its last accepted controller message. Plan for that window to cover both packet loss and transport recovery. These measurements establish neither node-loss recovery nor cross-controller failover.

An ordinary Python exception may reuse the worker. Queue expiry before submission preserves it. Active timeout/cancellation, native failure, failed reset, owner death and OOM retire the whole pod. The owning application can die before returning a tool error; its host must treat nonzero exit or lost connectivity as lost state and potentially uncertain execution, not a successful tool result.

Before pod creation, the controller atomically reserves a ConfigMap keyed by the complete ownership scope. Existing reservations refuse another owner: `supervise` raises `HyperlightPodReserved` without creating anything, whether the earlier owner is still running or awaiting cleanup. Other failures to create the reservation keep their own errors. A finalizer and UID-preconditioned deletes retain cleanup state. A started pod requires a runtime termination record for the exact UID before releasing the reservation; API disappearance and `NodeLost` do not count. A termination receipt is saved before deletion, allowing cleanup to resume after a controller restart. PID 1 writes why it retired (for example `controller stream closed`) to the container termination message, and empties it when it has no reason, so text the application wrote there is never reported as one; the receipt keeps it after the pod and its log are gone, and `HyperlightPodResult.reason` returns it. The ledger contains ownership lifecycle state, not tool source or a transcript.

A recognized server rejection of pod creation releases the reservation only after confirming no pod exists. The controller saves a rejection receipt before deleting the exact ledger UID; recovery can retry that deletion and returns startup failure code 71. Unknown kubectl errors, transport failures and `AlreadyExists` retain ownership. Fix the rejected configuration or quota before retrying the same scope.

Recovery records startup failure code 71 for pods that never start once cleanup is proved. PID 1 exits 78 when it refuses the node and 76 when it refuses the controller's lifecycle protocol; the termination receipt keeps either reason, and `recover` returns the same code. A startup timeout still raises `TimeoutError` after successful cleanup. An unassigned pod requires a recorded deletion, which prevents subsequent node binding. An assigned pod requires the kubelet finalization marker with no container execution history, a failed phase, and confirmation that its pod sandbox is gone. That finalization evidence remains valid if deletion later advances the pod metadata generation. Bootstrap containers must also have termination proof. Waiting-container status, missing finalization evidence and other unknown-container states retain ownership. The narrow kubelet marker is covered on Kubernetes 1.35.7; its [finalization code](https://github.com/kubernetes/kubernetes/blob/v1.35.7/pkg/kubelet/status/status_manager.go) and [binding guard](https://github.com/kubernetes/kubernetes/blob/v1.35.7/pkg/registry/core/pod/storage/storage.go) define these checks.

On `HyperlightPodCleanupPending`, retain the scope and call `controller.recover(key, kind, retire=True)` or the CLI `recover` action with the same identity. `controller.recover_exit(...)`, which the CLI uses, also returns the saved reason. Do not delete allocation records or force-remove finalizers to enable replacement. Missing pods without saved termination proof, unreachable nodes and ambiguous create failures need operator investigation and verified termination or node fencing. This integration does not provide node fencing or general distributed routing.

See the [backend guide](../../docs/sandbox/backends/hyperlight.md#aks-deployment-design) and [research record](../../docs/sandbox/research/hyperlight-backend.md) for measured evidence and remaining deployment work.
