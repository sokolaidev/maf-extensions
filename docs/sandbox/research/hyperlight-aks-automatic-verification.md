# Hyperlight on AKS Automatic: admission blocks device injection

> A live investigation for [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230), recorded on 2026-09-14 after a cluster became available. It measures application packaging, resource restrictions and plugin admission on AKS Automatic. It does not establish hypervisor-device usability, guest execution or adapter support. The [earlier source audit](hyperlight-aks-integration.md) preserves the initial assumptions and local results; [Hyperlight](../backends/hyperlight.md) tracks the backend's support boundary.

## Result

The pinned Python packages load in a restricted, non-root AKS pod. The infrastructure plugin cannot be admitted under this cluster's existing policy: `aks-managed-baseline-hostpath-volumes` rejects its kubelet, CDI and device host paths. The SDK negative control stops at missing `/dev/kvm`, with exit code 1. That is a successful missing-device control, not a failed attempt to create a VM on the host: the host device was never inspected or injected.

The next gate is a supported, explicitly approved exception for the dedicated infrastructure namespace, followed by host-device inspection, CDI allocation and VM/guest execution on one selected workload node. AKS documents [namespace exclusions from Deployment Safeguards and Pod Security Standards](https://learn.microsoft.com/en-us/azure/aks/deployment-safeguards#excluding-namespaces). No exclusion or other cluster security change was made in this run. Application admission remains restricted. Azure Linux alone does not establish MSHV availability; KVM and MSHV remain separate execution claims.

## Measured environment

| Surface | Observed value |
| --- | --- |
| AKS | Automatic SKU, Standard tier; Kubernetes/kubelet 1.35.7; Sweden Central |
| Existing placement | Three managed system nodes and one system-surge node; no workload nodes initially |
| Workload provisioned by AKS | Default Karpenter NodePool selected one `Standard_D4als_v6`: four vCPUs, 8 GiB nominal RAM |
| Workload node | Microsoft Azure Linux 3.0; kernel `6.6.150.1-1.azl3`; containerd 2.2.4; amd64 |
| Node image | NodeClaim image ID resolved to Azure Linux `V3gen2`, version `202608.26.0`; private resource identifiers omitted |
| Allocatable on that node | CPU `3860m`, memory `5935068Ki`, ephemeral storage `121156790891` bytes |
| Network | Azure CNI Overlay, Cilium; no application ingress created |
| Cluster admission | Deployment Safeguards `Enforce`, Pod Security Standards `Baseline`; no customer namespace exclusions configured |
| Application admission | Namespace additionally enforced Kubernetes restricted Pod Security Standards v1.35 |

The control plane omits managed system pools from the Azure agent-pool list. Kubernetes still showed those nodes. [AKS Automatic managed-system restrictions](https://learn.microsoft.com/en-us/azure/aks/automatic/aks-automatic-managed-system-node-pools-about#security-restrictions-for-managed-system-node-pools) prohibit customer workload placement and host access there. This experiment used the default customer workload pool and did not modify or enter system nodes. The automatically selected Azure Linux/AMD node differs from the earlier proposed Ubuntu/D4s_v5 topology; these results do not validate that proposed topology.

## Application proof

The [preflight manifest](hyperlight-aks/automatic-preflight.yaml) uses the same immutable Python base and [hash-pinned requirements](hyperlight-aks/requirements.txt) as the original Dockerfile. An unprivileged init container installs them into an `emptyDir` virtual environment, with the unchanged [probe](hyperlight-aks/probe.py) supplied through a read-only ConfigMap. This avoided creating a registry. It did not deploy the previously built local proof image. Both observed container image IDs were `docker.io/library/python@sha256:3121f8b0804aa3698ab750d9a39ea4a42657a385c9b133722b915e55c51551a6`.

Installation could reach PyPI and completed with exit code zero. Once the pod was ready, a deny-all ingress/egress NetworkPolicy was applied before running the SDK probe through `kubectl exec`. A TCP connection to `1.1.1.1:443` succeeded before that policy and timed out after it with a three-second socket timeout. This checks one external TCP destination; it is not the adapter's CLOSED/ALLOWLIST conformance suite.

| Check | Result |
| --- | --- |
| Package hashes | All three 0.7.0 wheels installed with `--only-binary=:all: --require-hashes` |
| Native extension and guest cache | Passed on CPython 3.13.12, glibc 2.36, x86-64, UID/GID 65534 |
| Python AOT | 43,890,120 bytes; SHA-256 `029a131ffaa07a48a70d4b75e29d84e719bb110636ec8361415569b13b116e4d`, matching the earlier local result |
| Process restrictions | `CapEff=0000000000000000`, `NoNewPrivs=1`, `Seccomp=2`; root filesystem read-only; no service-account token mount |
| cgroup v2 | `memory.max=2147483648`, `cpu.max=100000 100000`; cgroup filesystem read-only |
| Other observed limits | `pids.max=9483`; no custom per-worker PID limit; sampled `memory.events` counters all zero |
| Storage | Manifest requests/limits 1 GiB ephemeral storage and caps disk-backed `emptyDir` at 768 MiB; storage-limit enforcement was not stressed |
| CPU feature exposure | `svm` visible through `/proc/cpuinfo`; this does not prove usable nested virtualization |
| Application devices | Neither `/dev/kvm` nor `/dev/mshv` visible; no hypervisor extended resource advertised on the workload node |
| Full SDK command | Packaging passed; device stage returned `FileNotFoundError` for `/dev/kvm` and exit code 1; SDK stage was not reached |

The container limits were effective aggregate limits, not writable per-worker cgroup delegation. A ready pod meant its source file was present; the liveness/readiness probes do not assert guest execution. The proof process retained its 90-second timeout plus five-second kill allowance. The holding pod had a 1,200-second active deadline; its init installation had a 300-second timeout. No native guest worker was started, so no worker-lifetime or VM-cleanup result can be inferred from deleting this pod.

## Infrastructure admission

An initial inert host-access pod and then the exact [pinned plugin proof pod](hyperlight-aks/device-plugin-proof.yaml) were both submitted with server-side dry-run in a separate namespace without an application restricted label. Both returned `Forbidden`, exit code 1, from the cluster's baseline policy. The rejected volumes were:

- `device-plugin`: writable `/var/lib/kubelet/device-plugins`.
- `cdi`: writable `/var/run/cdi`, with `DirectoryOrCreate`.
- `dev`: read-only host `/dev`.

The error named `aks-managed-baseline-hostpath-volumes` and its corresponding binding; the only stated host-path exception was read-only `/var/log`. No plugin pod or host path was created. The candidate runs as root with dropped capabilities, no privilege escalation, a read-only root filesystem, RuntimeDefault seccomp, no token, one scheduling allocation and device UID/GID 65534. Its selector requires one explicitly supplied node and its deadline is 900 seconds. These hardening settings were admission-tested but have not been runtime-validated.

The candidate resolves the upstream `fc71b45` tag to immutable Linux/amd64 manifest `sha256:dcb786825c83615c95ad5e95d25f8668efe032454c2fec623b5ed3806bb3ac98`, within OCI index `sha256:8e8b788c2327e272317c456c85c8296922ccb8b9b70e5b866451bec11ed02472`. The [upstream publish run](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/actions/runs/34271126569) succeeded for `fc71b4501d23977fcc54f7be144d884fc8210667`. Attached BuildKit provenance reports the same source revision, Go builder `golang:1.25-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59` and runtime base `alpine:3.19@sha256:6baf43584bcb78f2e5847d1de515f23499913ac9f12bdf834811a3145eb11ca1`. This is registry/provenance inspection, not a local rebuild, signature verification or a deployed plugin result. The audited Dockerfile still has floating inputs; no reproducible-build claim follows from recording this artifact.

## Reproduction and cleanup

Use an explicit, authenticated cluster context on every command. This probe can cause the default workload pool to provision a billable node. Create the two experiment namespaces named in the preflight manifest, apply its restricted labels to the application namespace, and create `proof-source` there from the existing `probe.py` and `requirements.txt`. Server-dry-run the preflight manifest, then apply it. Wait for the init container to finish and the pod to become ready; provisioning took longer than the first 45-second wait in this run. A short wait expiring was not treated as a packaging failure.

```sh
kubectl --context "$PROOF_CONTEXT" -n maf-hyperlight-proof-1230 create configmap proof-source --from-file=docs/sandbox/research/hyperlight-aks/probe.py --from-file=docs/sandbox/research/hyperlight-aks/requirements.txt
kubectl --context "$PROOF_CONTEXT" apply --dry-run=server -f docs/sandbox/research/hyperlight-aks/automatic-preflight.yaml
kubectl --context "$PROOF_CONTEXT" apply -f docs/sandbox/research/hyperlight-aks/automatic-preflight.yaml
kubectl --context "$PROOF_CONTEXT" -n maf-hyperlight-proof-1230 wait --for=condition=Ready pod/sdk-preflight --timeout=300s
```

After readiness, create a `networking.k8s.io/v1` NetworkPolicy in the application namespace with `podSelector: {}`, `policyTypes: [Ingress, Egress]` and no allow rules. Preserve init logs, the admitted pod specification, actual image IDs and node/NodeClaim metadata privately. Run the command below and retain its nonzero exit status together with both JSON stage records. Do not add the policy before installation unless the wheels are already packaged in an image.

```sh
kubectl --context "$PROOF_CONTEXT" -n maf-hyperlight-proof-1230 exec sdk-preflight -c proof -- timeout --signal=TERM --kill-after=5s 90s /work/venv/bin/python -I -u /proof/probe.py --stage sdk
```

For the independent admission check, render only `${PROOF_NODE}` in the plugin manifest to a selected customer workload node, then submit it with `--dry-run=server`. Its expected result under the measured unchanged policy is refusal. Installing it requires an approved infrastructure exception and further inspection of the actual host device and CDI configuration. A bounded pod is used here for one-node experimentation; it does not replace the recovery-capable manifests required by [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237). Before any admitted plugin run, establish cleanup for the experiment's CDI file and socket: the upstream plugin does not remove its CDI file on exit.

The executed application pod, ConfigMap, NetworkPolicy and both experiment namespaces were removed. Subsequent reads found neither namespace, the default NodePool returned to zero nodes, and the experiment's NodeClaim disappeared. The pre-existing system-surge NodeClaim remained. No node pool definition, safeguard setting, registry, VM size or cluster configuration was changed. One temporary D4als_v6 workload node incurred transient compute/storage use; no billing amount was measured and the earlier two-node Ubuntu cost estimate does not describe this run.

## Remaining evidence

Host `/dev/kvm` and `/dev/mshv` presence/ownership, effective CDI configuration, runtime device enforcement, VM creation and Python guest execution remain unmeasured. Adapter/CodeAct conformance still requires [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228) and [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238). Guest cancellation, OOM, owner death, plugin/kubelet restart, node disruption and replica fencing were not exercised. The blocked admission establishes a prerequisite for this AKS Automatic configuration; it does not establish that AKS or Azure Linux cannot run Hyperlight.
