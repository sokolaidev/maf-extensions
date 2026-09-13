# Hyperlight on AKS: device access is the first gate

> An investigation for [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230), recorded on 2026-09-14. It evaluates the upstream Kubernetes integration, measures Linux Python packaging locally, and proposes an AKS proof. It does not establish AKS, KVM or MSHV adapter support. [#382](https://github.com/sokolaidev/maf-extensions/issues/382) remains the umbrella; the shipped family is described in [Hyperlight](../backends/hyperlight.md).

## Decision

Proceed with a dedicated Ubuntu/KVM AKS experiment using the existing sandbox protocol. Reuse the device-plugin/CDI approach, with reviewed manifests and immutable images. Do not adopt the upstream setup script or its resource defaults unchanged. Keep Azure Linux/MSHV conditional until the current AKS node image, device ABI and exact Python wheel have independent execution evidence. Neither path is admitted by the current adapter.

There are three separate gates: device injection and VM creation; the packaged Python SDK executing and restoring a guest; and the adapter preserving deadlines, ownership, memory and cleanup. The first two can be investigated before [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228). The third needs its Linux implementation and container-specific containment. A normal application pod does not have a writable delegated cgroup subtree merely because Kubernetes limits its memory. Do not solve that gap by granting the application host cgroup write access.

The active Azure subscription returned no AKS clusters and the only local Kubernetes context was Docker Desktop. No Azure resources were created or changed. This record therefore supplies a measured packaging result and an unexecuted AKS recipe. Keep #1230 open for the device, guest and disruption evidence; the absence of an available cluster is not evidence that AKS cannot run Hyperlight.

## Source and artifact baseline

| Component | Recorded identity | What it establishes |
| --- | --- | --- |
| This repository | [`da40a37ba85d0a33690320836e7e0fb8aa6a6ba7`](https://github.com/sokolaidev/maf-extensions/tree/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7) | `maf-sandbox-hyperlight` source version 0.0.0; Windows-only admission and lifecycle |
| Upstream Kubernetes integration | [`fc71b4501d23977fcc54f7be144d884fc8210667`](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/tree/fc71b4501d23977fcc54f7be144d884fc8210667), also remote HEAD when inspected | Exact plugin, manifests, setup and example source audited here |
| SDK, Wasm backend, Python guest | All 0.7.0; source [`6ae78065617d5603c1dd5fdbb63d62d8201ac68c`](https://github.com/hyperlight-dev/hyperlight-sandbox/tree/6ae78065617d5603c1dd5fdbb63d62d8201ac68c) | Same trio as the adapter; Linux distribution availability is distinct from adapter support |
| SDK native dependencies | [Lockfile](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/Cargo.lock): `hyperlight-host` 0.17.0, `hyperlight-wasm` 0.15.0 | Different native stack from the Kubernetes hello-world |
| Native host source | [0.17.0, commit `388ea8ebd88639a062f7a87f477db2930605a307`](https://github.com/hyperlight-dev/hyperlight/tree/388ea8ebd88639a062f7a87f477db2930605a307) | Hypervisor detection and Windows surrogate-mode implementation |
| Proof base image | `python:3.13.12-slim-bookworm@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6` | Linux/amd64 child manifest, resolved from Docker Hub on the investigation date |
| Local proof image | OCI index `sha256:0ad9cf2898437e24eaadd2a8f447fa620f4fda8468a11b8d7ad45b9e12d486de` | Locally built image used for the measurements below; not a published registry reference |

The proof's [Dockerfile](hyperlight-aks/Dockerfile), [requirements](hyperlight-aks/requirements.txt), [probe](hyperlight-aks/probe.py) and [Job](hyperlight-aks/job.yaml) are research artifacts, not deployment support for the backend. PyPI's version-specific JSON endpoints for [core](https://pypi.org/pypi/hyperlight-sandbox/0.7.0/json), [Wasm](https://pypi.org/pypi/hyperlight-sandbox-backend-wasm/0.7.0/json) and [guest](https://pypi.org/pypi/hyperlight-sandbox-python-guest/0.7.0/json) supplied these non-yanked artifacts; the image build verified the hashes:

| Wheel | SHA-256 |
| --- | --- |
| `hyperlight_sandbox-0.7.0-py3-none-any.whl` | `7f4e52c7ef6013f461d19db60ec537c18e9cf41045ac819d4af1bb9cb72bdca3` |
| `hyperlight_sandbox_backend_wasm-0.7.0-cp313-cp313-manylinux_2_28_x86_64.whl` | `9f432799c2275bf57c0da32db922ff52855d83e587fda2128b76b65419065f30` |
| `hyperlight_sandbox_python_guest-0.7.0-py3-none-any.whl` | `9eff31341b61830b9b699dbd4fba65f5d62e878050db5db331f688ee0e561367` |

The Wasm package also supplies CPython 3.10–3.14 Linux x86-64 wheels. This recipe selects CPython 3.13 and glibc, not the example's static musl/scratch image. An ARM64 or musl host is outside this proof. A container's glibc comes from its image, not its AKS node distribution.

## What the upstream integration actually supplies

[`NewHyperlightDevicePlugin`](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/main.go) prefers an existing `/dev/mshv` over `/dev/kvm`. Detection and the 30-second health check use `os.Stat`; neither opens the device, validates a character-device type, nor creates a VM. `Allocate` returns the same CDI name for every container allocation, without validating requested device IDs. No Kubernetes API client or node-label update exists in this source. The architecture document's claim that the plugin automatically labels nodes is therefore misleading: the setup scripts supply the placement labels.

`writeCDISpec` writes CDI 0.6.0 at `/var/run/cdi/hyperlight.json`, mapping one character device with `rw` access and configurable UID/GID, defaulting to 65534/65534. The injected `HYPERLIGHT_HYPERVISOR` and `HYPERLIGHT_DEVICE_PATH` variables are metadata, not enforcement or Python SDK configuration. The [native host detector](https://github.com/hyperlight-dev/hyperlight/blob/388ea8ebd88639a062f7a87f477db2930605a307/src/hyperlight_host/src/hypervisor/virtual_machine/mod.rs) selects compiled hypervisor support itself. Our KVM probe refuses a visible `/dev/mshv` to keep its result attributable to KVM.

The [Go module](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/go.mod) declares Go 1.25.0, kubelet API package 0.35.0, gRPC 1.79.3 and fsnotify 1.8.0. That dependency does not establish a minimum cluster version: registration uses the device-plugin `v1beta1` API. The [plugin Dockerfile](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/Dockerfile) uses floating `golang:1.25-alpine` and `alpine:3.19` tags. No plugin image digest or build provenance was validated in this run.

The [hello-world host](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/host/src/main.rs) calls guest functions `Echo` and `PrintOutput`; it is not Python or CodeAct. Its [lockfile](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/Cargo.lock) resolves Hyperlight host/guest/common to 0.12.0. Its [Dockerfile](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/Dockerfile) does not copy that workspace lockfile, uses semver dependencies, installs an unversioned `cargo-hyperlight`, and builds with floating Rust 1.89 image tags. A source revision alone does not pin the resulting example binary.

The [test script](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/scripts/test.sh) skips absent node families successfully. Its default `all` path checks device presence but does not call the guest application test. The host's loop mode logs native errors and keeps running. Require a one-shot guest process that exits successfully and whose output matches; a Running pod or a log saying which device exists is insufficient.

The [setup script](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/azure/setup.sh) creates both families: one D2s_v3 system node and two D4s_v3 nodes per user pool, each pool autoscaling from one to five. It does not pin Kubernetes or node-image versions, prove device usability or validate CDI configuration. Its separate [deploy script](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/azure/deploy.sh) substitutes only `IMAGE`, leaving the three device settings literal; invalid values fall back to plugin defaults. The [justfile](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/justfile) substitutes all four. Render and review explicitly, including the count.

## Current AKS prerequisites: two independent families

| Requirement | Ubuntu/KVM candidate | Azure Linux/MSHV candidate |
| --- | --- | --- |
| Nodes | Dedicated Linux x86-64 user pool; start with D4s_v5, Ubuntu 24.04 | Dedicated Azure Linux 3 pool on a supported generation-2, nested-virtualization SKU; actual MSHV image/kernel must be recorded |
| Device | `/dev/kvm`, non-root read/write, API version 12, successful `KVM_CREATE_VM`, then actual guest execution | `/dev/mshv`, non-root open plus a real VM/guest through the exact wheel; KVM results do not satisfy this |
| Kubernetes | Candidate 1.36.3, listed by the regional API in `westeurope` on 2026-09-14 | Select a currently available version compatible with the validated MSHV node image; not measured |
| Runtime | Ordinary containerd/runc pod with CDI enabled; inspect actual runtime/configuration | Ordinary containerd/runc pod on the MSHV-capable node; do not add a Kata runtime class to the Hyperlight app |
| Preview status | No Kata preview required for the KVM candidate | Upstream's `KataMshvVmIsolation` plus `aks-preview` is not a verified current contract; resolve the service-supported path before creation |
| Admission | Conditional experiment; adapter blocked by #1228 and container containment | Conditional research only; separately blocked by node/device ABI and SDK/adapter conformance |

Microsoft's [Dsv3](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv3-series) and [Dsv5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series) pages mark nested virtualization supported. D4s_v5 has four vCPUs and 16 GiB RAM; it needs a managed OS disk rather than the AKS skill's generic ephemeral-disk recommendation. The [AKS engineering KubeVirt example](https://blog.aks.azure.com/2026/02/06/kubevirt-on-aks) independently demonstrates the intended nested-virtualization hosting pattern. This is platform evidence, not a support statement for our plugin or Python adapter. Verify regional SKU restrictions, available quota, VM generation/security configuration and the actual device on the allocated node.

Current [AKS Pod Sandboxing guidance](https://learn.microsoft.com/en-us/azure/aks/use-pod-sandboxing) specifies Azure CLI 2.80.0+, AzureLinux and `--workload-runtime KataVmIsolation`; it allows normal pods alongside Kata pods. It does not prescribe the upstream script's `KataMshvVmIsolation` name. An [SDK preview enum](https://learn.microsoft.com/en-us/dotnet/api/azure.resourcemanager.containerservice.models.workloadruntime.katamshvvmisolation?view=azure-dotnet-preview) containing that name is not proof of regional availability or raw-device support. No MSHV feature was registered, no preview extension was installed, and no MSHV pool was created here. Confirm the supported node-image path with AKS before spending on this family; merely replacing the enum is not a validated fix.

The Kubernetes [feature history](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates-removed/) records `DevicePluginCDIDevices` as beta/on in 1.29 and stable in 1.31. The plugin emits CDI 0.6.0. Containerd's [1.7.29 configuration](https://github.com/containerd/containerd/blob/v1.7.29/docs/cri/config.md) defaults `enable_cdi` to false, while [2.0.0](https://github.com/containerd/containerd/blob/v2.0.0/docs/cri/config.md) defaults it to true; both search `/etc/cdi` and `/var/run/cdi`. AKS images can override defaults. Its [version table](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions) currently lists Ubuntu 24.04/containerd 2.3.1 for 1.36 and Azure Linux 3/containerd 2.2.4. Record actual kubelet, containerd, runc, kernel, node-image release and effective CDI settings. The [OS SKU guide](https://learn.microsoft.com/en-us/azure/aks/upgrade-os-version) and component table disagree around unversioned Ubuntu defaults for 1.35; use the explicit Ubuntu2404 SKU and inspect the resulting image.

## Infrastructure permissions versus application permissions

| Surface | Upstream evidence | Adoption requirement |
| --- | --- | --- |
| Plugin host authority | [DaemonSet](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/manifests/device-plugin.yaml) runs UID 0 with writable kubelet device-plugin and CDI host directories, and read-only host `/dev` | Treat as trusted node infrastructure. A writable CDI directory can influence other containers; `privileged: false` and read-only rootfs do not remove this authority. Restrict deployment/update permissions and node placement |
| Plugin hardening | No explicit capability drop or seccomp profile; service account exists, without token automount disabled; source has no API calls | Validate drop ALL and RuntimeDefault; disable token automount. No node-label RBAC is needed for the inspected implementation. Host paths require an infrastructure admission exception, separate from application policy |
| Application | [Rust app manifest](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/k8s/deployment-kvm.yaml) uses UID/GID 65534, RuntimeDefault, drop ALL, no escalation, read-only rootfs | Preserve these controls for Python; explicitly disable service-account token mount and service links. No hostPath, hostPID, runtime socket, Kubernetes API role or host cgroup mount |
| Device identity | One node-wide CDI UID/GID setting | Match 65534/65534 in the proof; `fsGroup` makes the work volume writable, not the device mapping. A different workload UID requires an explicit mapping strategy, not chmod on host devices |
| Device access enforcement | CDI asks the runtime for the device and `rw` permissions | Verify the OCI device rule/runtime enforcement and non-root VM creation. A control pod without the extended resource must fail to open/create through the device. File mode alone is not the boundary |

RuntimeDefault is a starting point, not a portable promise that every hypervisor ioctl or executable-memory operation is admitted. Record the effective runtime/seccomp policy and test under it. Do not widen to privileged or unconfined on failure. Diagnose the denied operation, and justify any smaller change separately. Device access grants a trusted host process access to the hypervisor API; Hyperlight's guest boundary is not a sandbox for arbitrary malicious native application code in that process.

The pinned [Python guest path resolver](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/sdk/python/wasm_guests/python_guest/python_guest/path.py) materializes its AOT module under `XDG_CACHE_HOME/hyperlight_sandbox_guests/python_guest`, falling back to the user's home cache. Supply a private writable work volume and explicit `HOME`, `XDG_CACHE_HOME` and `TMPDIR`. The proof imports the native extension and materializes the 43,890,120-byte AOT file under UID 65534 with a read-only image and no network. It exposes no guest filesystem channels. Neither SDK source inspected here nor host 0.17.0 requires `XDG_RUNTIME_DIR`; an adapter lock/runtime directory is a design obligation for #1228, not an invented SDK requirement.

The current adapter's [worker](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_worker.py) unconditionally loads `WinHvPlatform.dll`; its [process launcher](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_process.py) uses Windows jobs and a Windows environment allowlist. Its package metadata installs the Wasm/guest dependencies only on Windows AMD64. A successful standalone Linux SDK import does not repair those differences. `HYPERLIGHT_MAX_SURROGATES=0` belongs to the pinned host's Windows surrogate implementation; it is not Linux process containment.

## Resource and ownership model

`DEVICE_COUNT=2000` means 2,000 scheduling allocations sharing one device. One allocation can create many VMs; it does not cap VM count or memory. Set it to 1 for the initial single-pod proof, then to the deliberately admitted pod count for scale experiments. The upstream app's 128 MiB limit belongs to its small Rust guest and is unsuitable evidence for Python.

For an adapter pod, admission must satisfy `host reserve + N * worker budget + cache/buffer reserve <= container memory limit`, with `N` the maximum simultaneous resident workers, including warm idle sandboxes and workers awaiting cleanup. The current worker budget is 1,536 MiB, while guest heap/stack settings alone total 600 MiB and do not include snapshots, native output or host overhead. The proof starts at one SDK sandbox, one CPU and 2 GiB; that is a proposed experiment budget, not a measured adapter sizing recommendation. A worker budget must have its own enforcing mechanism or share an explicitly accepted pod failure domain. A process virtual-address limit is not equivalent to committed-memory accounting.

Kubernetes [resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) schedules requests and enforces CPU/memory limits through the runtime/kernel. CPU throttling can consume the adapter's wall-clock deadline. Memory pressure can OOM-kill a worker or owner before Python can return a useful error. Pod limits cap aggregate use; they do not ensure each worker stays within its individual budget. Disk-backed `emptyDir` consumes ephemeral storage and needs a size/ephemeral-storage budget; a memory-backed volume also consumes memory. Measure actual cgroup accounting, peak RSS, snapshot/native buffers and cleanup concurrency before increasing density.

The first adapter deployment must expose exactly one owning application process, with one replica and no overlapping rolling replacement, within the Linux ownership namespace that #1228 validates. A pod-local lock does not serialize other pods, and node sharing does not make registries shared. Use `Recreate` for that initial host service, and route every call and purge to it. Losing that process loses sessions; creating a replacement is a new owner generation.

Multiple replicas require a separate ownership protocol. Map the complete `SandboxKey` plus kind, selected backend and execution-policy identity to an authenticated owner generation (pod UID plus process generation), and carry exact sandbox instance IDs. Route calls, resets and purges through that same authority. A scope purge must enumerate all owners with matching allocations and aggregate their cleanup results. Sticky HTTP sessions, a StatefulSet ordinal or hashing the key without a lease/fencing protocol cannot stop an old owner from executing during failover. An unreachable owner is unknown/unclean until fenced or its worker destruction is established. Never report a successful purge just because the replacement's registry is empty. Do not automatically replay submitted code after an ambiguous disconnect; external effects may already have occurred. None of this requires another agent/provider layer or changes the kinds' dependency on the sandbox protocol.

## Failure assessment and required measurements

These are expected boundaries and acceptance checks, not AKS results. The [pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) supplies graceful termination and eventual runtime cleanup on a reachable node; deleting an API object or losing contact with a node is not proof that its processes stopped.

| Event | Required behavior and evidence |
| --- | --- |
| Queued deadline | Preserve the active worker; prove expired code was never submitted |
| Program timeout / cancellation | Kill and reap the worker tree within the adapter cleanup allowance, retire the instance, and prove no later output or execution; Kubernetes Job deadlines are only a proof-level backstop |
| Ordinary guest exception | Return a failed result; prove safe warm reuse and reset separately |
| Native abort, broken pipe, output or memory limit | Retire the worker, sanitize native diagnostics, preserve cleanup failure for retry; distinguish guest failure from owner death |
| Graceful pod deletion | Stop admission, finish bounded cleanup, then exit within the grace period; observe worker PIDs/cgroups/device FDs disappearing |
| SIGKILL / abrupt owner exit | Prove worker-tree death even while kubelet is healthy and has not restarted the container; a signal handler alone cannot do this |
| OOM | Exercise both worker and owner victims; capture `memory.events`, container termination reason, lost sessions and replacement generation without replaying uncertain calls |
| Node drain / restart / partition | Drain stops new admission; node restart loses in-memory sessions. Capture fencing and cleanup certainty before replacement accepts affected keys; test an unreachable old owner |
| Plugin restart | Existing VM/device FDs should remain owned by their application processes, an inference to measure; check new allocation recovery and unchanged existing guest execution |
| Kubelet restart / CDI deletion | The plugin watches socket removal and re-registers; its CDI file is written at construction, not every re-registration. Test recovered capacity, stale/missing CDI, bounded registration failure and whether a new pod can actually execute |

The plugin is not a VM supervisor. Its `Stop` removes the socket but does not dispose application VMs or remove the CDI file. Existing-device health based on `stat` cannot detect permission, ABI or VM-creation failure. The manifest's socket-existence liveness probe cannot establish successful kubelet registration. These are reasons to validate restart behavior before deployment support, not claims that failures were reproduced here.

## Minimal reproduction

Run the local stages from the repository root with Docker's Linux/amd64 engine:

```sh
docker build --platform linux/amd64 -t maf-hyperlight-aks-proof:1230 docs/sandbox/research/hyperlight-aks
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 1 --tmpfs /work:rw,nosuid,nodev,noexec,size=256m,uid=65534,gid=65534,mode=0700 maf-hyperlight-aks-proof:1230 --stage packages
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 1 --tmpfs /work:rw,nosuid,nodev,noexec,size=256m,uid=65534,gid=65534,mode=0700 maf-hyperlight-aks-proof:1230 --stage device
```

The second run intentionally has no device and must exit 1 with a failed `device` record. The probe never treats absence as a skip. `--stage sdk` first requires successful KVM VM creation, then checks output, persistent globals, ordinary exception/reuse and snapshot restore. All native objects stay on one Python thread. GNU `timeout` bounds the proof process at 90 seconds with a further five-second kill allowance; the Job has no retry and a 120-second active deadline. These bounds do not implement adapter cancellation, per-worker memory or owner-exit guarantees.

For AKS, use an approved disposable cluster/resource group and a registry the nodes can pull from. The proposed topology is one D4s_v5 system node plus one dedicated D4s_v5 Ubuntu2404 KVM user node, Kubernetes 1.36.3, managed OS disks, AKS Free tier for this experiment, Azure CNI Overlay, no application ingress and no autoscaler. Validate networking against the selected environment before creating it. Region, quota and the actual image remain preflight requirements. Do not run the upstream two-family setup to obtain a KVM proof.

1. Record `az version`, `az aks get-versions --location westeurope`, VM SKU restrictions/quota, chosen Kubernetes patch, OS SKU and each node pool's `nodeImageVersion`. Record the node's OS image, kernel, kubelet, containerd/runc versions, effective CDI configuration, cgroup mode, device type/mode/ownership and successful native virtualization probe. If CDI needs an unsupported node configuration override, stop and track the supported configuration path; do not silently patch managed hosts.
2. Check out the recorded upstream commit. Build the plugin with recorded digest-pinned base images and an exact Go toolchain; retain its module graph and source provenance. Resolve the pushed image to a registry digest. If comparing hello-world, also pin the cargo-hyperlight tool, copy/use the workspace lockfile with `--locked`, and run the host once rather than in loop mode. Keep its result separate from Python. No such plugin or Rust image was built in this investigation.
3. Render `deploy/manifests/device-plugin.yaml` with all four variables: `IMAGE` as an immutable plugin reference, `DEVICE_COUNT=1`, `DEVICE_UID=65534`, `DEVICE_GID=65534`. Disable service-account token automount, set seccomp RuntimeDefault and drop ALL capabilities; validate those changes before claiming they work. Keep the required root host-path writes within the infrastructure namespace. Ensure the kubelet node labels match the observed device. Save and review the complete manifest before applying it to the intended context.
4. Push the proof image to the approved registry and record its registry digest, distinct from the local image ID above. Set `PROOF_IMAGE` to `registry/repository@sha256:<observed-digest>` and render only that variable into the provided Job. Use a dedicated application namespace with restricted pod admission and deny-default network policy. Apply the Job only after server-side dry-run; preserve logs and termination state even on failure. Re-running requires a new Job name or deletion of that completed proof Job.
5. Run a negative control with a different Job name and the `hyperlight.dev/hypervisor` entries removed from both requests and limits, leaving the same node selector and application security context. It must fail at `device` rather than reach `sdk`. For the positive Job, require every stage to pass and the container exit code to be zero; `kubectl wait --for=condition=complete` timing out is a failure to investigate, not a skip. Record the actual image ID, node and pod UID privately with the raw evidence.
6. After #1228 and the container containment follow-up land, build a second image from the exact adapter commit with all compatible packages. Run its real `RUN_CODE`/`SNAPSHOT`, deadlines, cancellation, memory/output, owner death, disposal/purge, CLOSED/ALLOWLIST and fixed/per-spec CodeAct tests. Require nonzero executed live tests rather than a suite that skipped on Linux. Then run the disruption matrix above, including plugin and kubelet restart separately. The provided SDK probe is not this suite.
7. Retain redacted evidence, then delete the proof Jobs and their namespace. Remove only the experiment-owned plugin installation and infrastructure; confirm worker/cgroup disappearance before removing node access. Account for residual disks, registry, networking and logs. No cleanup command should target an unrelated pre-existing resource group.

For example, after steps 1–3, using an already selected context and namespace:

```sh
: "${PROOF_IMAGE:?Set an immutable registry image reference}"
: "${PROOF_NAMESPACE:?Set the dedicated proof namespace}"
envsubst '${PROOF_IMAGE}' < docs/sandbox/research/hyperlight-aks/job.yaml > proof-job.rendered.yaml
kubectl -n "$PROOF_NAMESPACE" apply --dry-run=server -f proof-job.rendered.yaml
kubectl -n "$PROOF_NAMESPACE" apply -f proof-job.rendered.yaml
kubectl -n "$PROOF_NAMESPACE" wait --for=condition=complete job/hyperlight-kvm-proof --timeout=150s
kubectl -n "$PROOF_NAMESPACE" logs job/hyperlight-kvm-proof
kubectl -n "$PROOF_NAMESPACE" get pods -l job-name=hyperlight-kvm-proof -o json
```

MSHV does not become a claim by substituting a node label in this KVM Job. Establish the supported AKS MSHV image first, use a family-specific VM/guest probe and verify the packaged wheel's actual compiled/loaded ABI. Repeat the negative control, SDK checks, adapter conformance and lifecycle matrix independently. Do not substitute the Rust example's MSHV success, if obtained, for Python evidence.

## Measured results and reproduction cost

| Check on 2026-09-14 | Result |
| --- | --- |
| Source audit | Completed at the revisions above; no upstream tests or cluster deployment performed |
| Azure read-only discovery | CLI 2.89.0; no AKS clusters in the active subscription; `westeurope` API listed 1.36.0, 1.36.1, 1.36.2 and 1.36.3 |
| Pinned Linux image build | Passed; all three wheel hashes accepted; no native compilation needed |
| Non-root packaging stage | Passed on Docker engine 29.7.2, Linux/amd64, kernel `7.0.12-linuxkit`, CPython 3.13.12, Debian Bookworm glibc 2.36, UID/GID 65534; native extension imported, private AOT cache materialized without network |
| AOT materialization | 43,890,120 bytes; SHA-256 `029a131ffaa07a48a70d4b75e29d84e719bb110636ec8361415569b13b116e4d` |
| Missing-device control | Packaging passed, then `FileNotFoundError` for `/dev/kvm`; expected failed device stage. This was a local container with no injected device, not an AKS device-enforcement test |
| AKS injection / KVM VM creation / Python guest execution | Not run; no usable AKS cluster was available |
| MSHV, adapter conformance, CodeAct and disruption tests | Not run; no support claim |
| AKS node image, plugin registry digest and deployed proof registry digest | Not measured; must be filled before a reproducible cloud execution claim |

The [Azure retail pricing API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) returned Linux D4s_v5 consumption in `westeurope` at USD 0.23 per hour (meter effective 2021-11-01), queried on 2026-09-14 with `serviceName eq 'Virtual Machines' and armRegionName eq 'westeurope' and armSkuName eq 'Standard_D4s_v5' and priceType eq 'Consumption'`, excluding Windows/Spot/Low Priority. Two nodes therefore budget USD 0.46/hour, or USD 1.84 for four hours, **compute only**. Confirm current subscription pricing; managed OS disks, load balancer/public IP, registry, egress and optional logs add charges. This run incurred no AKS compute charges.

The experiment requires eight regional and Dsv5-family vCPUs plus capacity for any upgrade surge; regional availability was not a quota reservation. Two D4s_v5 nodes provide 32 GiB nominal RAM before system reservations. One worker pod requests 2 GiB/one CPU and 512 MiB ephemeral storage; the node also needs room for image layers and platform workloads. A second independent MSHV node adds its own SKU quota and cost; do not provision it until the supported node path is resolved. The upstream defaults would start five nodes and permit eleven, which is unnecessary for the first proof.

## Follow-through

Keep the general Linux implementation in [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228). The bounded implementation follow-ups are [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237) for reviewed KVM infrastructure manifests and device-plugin recovery, [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238) for container-compatible adapter containment and conformance, and [#1239](https://github.com/sokolaidev/maf-extensions/issues/1239) for replica owner fencing and distributed scope purges. The existing [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236) owns the authenticated single-owner remote prototype; #1239 extends that foundation only when multiple owners are required. MSHV remains a separate family investigation within #1230 until AKS image/device support is established. File capabilities and native host tools remain independent and do not gate these runtime experiments.
