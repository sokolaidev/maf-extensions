# Hyperlight KVM execution on AKS Automatic

> A live follow-up for [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230), recorded on 2026-09-14 after approval of a temporary infrastructure-namespace exception. It measures KVM device injection, VM creation, packaged Python SDK execution and controlled plugin replacement. It does not establish Linux adapter, CodeAct, MSHV or production lifecycle conformance. The [initial Automatic probe](hyperlight-aks-automatic-verification.md) preserves the earlier admission refusal; [Hyperlight](../backends/hyperlight.md) tracks the support boundary.

## Result

The device-plugin/CDI approach worked for the pinned Python SDK on one AKS Automatic workload node. A non-root application created a KVM VM, executed Python, preserved globals, recovered from an ordinary guest exception and restored a snapshot. Its control pod, on the same node without the extended-resource request, could not see the KVM device. A held guest survived one controlled plugin replacement, and a fresh application pod then obtained the device and repeated the full SDK proof successfully.

The approved exception covered only the dedicated infrastructure namespace. Application admission remained restricted. The exception was removed afterward, and the same plugin manifest was refused again. This establishes an experimental KVM path on the measured configuration; the suite's current Windows-only adapter still requires independent Linux implementation and container containment verification. Infrastructure recovery remains under [#1237](https://github.com/sokolaidev/maf-extensions/issues/1237), container containment under [#1238](https://github.com/sokolaidev/maf-extensions/issues/1238), and general Linux enablement under [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228).

## Configuration and artifacts

The platform and immutable image identities match the [initial Automatic record](hyperlight-aks-automatic-verification.md): Kubernetes/kubelet 1.35.7, Azure Linux 3.0, kernel `6.6.150.1-1.azl3`, containerd 2.2.4, and the publicly documented `Standard_D4als_v6` VM size. One customer workload node was automatically provisioned. The node-image version was `202608.26.0`. Customer resource identifiers, node names and host configuration paths are omitted from this public record.

The plugin used the previously audited upstream revision and immutable Linux/amd64 image. Its observed image ID matched the manifest on both instances. The application used the existing immutable Python base and [hash-pinned requirements](hyperlight-aks/requirements.txt), installed into a private virtual environment by an unprivileged init container. All three distributions were 0.7.0; application userspace was CPython 3.13.12 and glibc 2.36. This did not deploy the earlier locally built proof image.

The [plugin pod](hyperlight-aks/device-plugin-proof.yaml) ran as root with RuntimeDefault seccomp, all capabilities dropped, no privilege escalation, a read-only root filesystem and no service-account token. Its startup guard refused pre-existing Hyperlight socket or CDI state before starting the unmodified upstream binary. It targeted one explicitly selected workload node, advertised one `hyperlight.dev/hypervisor` allocation and had a 900-second deadline. Kubelet reported that allocation as available. It remains a scheduling allocation, not a VM or memory limit inside the consuming pod.

Read-only node inspection found a KVM character device, major 10/minor 232, mode `0666`, UID 0/GID 32, and no MSHV device. The observed containerd configuration document had format version 2, no imports and no explicit CDI fields. No host runtime configuration was edited. Successful runtime injection establishes that CDI worked here; the inspection was not a complete effective-daemon configuration dump, and no separate host `runc` version was measured.

The generated CDI specification used version 0.6.0 and mapped the KVM device read/write to UID/GID 65534. Its SHA-256 was `5ab0197e81db937ea96ab3fc1f258a7f0a1a59e0221381fc754473ffcc3502de` on both plugin instances. The application observed those device ownership values. The [positive pod](hyperlight-aks/kvm-positive-pod.yaml) adds a single hypervisor resource request/limit to the main container and selects the inspected node; its other application restrictions match the control.

## Execution evidence

Package installation had network access. The application's deny-all ingress/egress policy was applied before running the unchanged [SDK probe](hyperlight-aks/probe.py). Each full SDK command used a 90-second timeout and five-second kill allowance; holding pods had 1,200-second active deadlines.

| Check | Measured result |
| --- | --- |
| Packaging and guest cache | Passed as UID/GID 65534; 43,890,120-byte AOT with the same hash as the earlier record |
| KVM API | Device opened read/write; `KVM_GET_API_VERSION` returned 12; `KVM_CREATE_VM` returned a VM FD, which the probe closed |
| Execution and persistent state | Setting `value = 42` succeeded, and a later guest run read the same global |
| Ordinary exception and reuse | Expected `ValueError` returned a failed guest result; a subsequent run returned 43 |
| Snapshot restore | Restoring the baseline removed the global; its existence check printed `False` |
| Positive command | Packaging, device and SDK stages passed; process exit code 0 |
| Matched negative control | Packaging passed; absent KVM device produced `FileNotFoundError`, process exit code 1; SDK stage was not reached |
| Process restrictions after execution | UID/GID 65534, zero effective capabilities, no-new-privileges enabled, seccomp filtering enabled, read-only rootfs and no token mount |
| cgroup v2 | Effective 2 GiB memory limit, one-CPU quota, PID limit 9,483; cgroup filesystem read-only; sampled memory-event counters all zero |
| Network control | The external TCP probe timed out under the deny policy; this was one endpoint check, not adapter CLOSED/ALLOWLIST conformance |

Both successful full SDK runs emitted:

```jsonl
{"stage": "device", "result": "pass", "api": 12, "uid": 65534, "gid": 65534}
{"stage": "sdk", "result": "pass", "hypervisor": "kvm", "adapter_conformance": false}
```

These are aggregate pod limits and successful bounded commands. They do not establish per-worker cgroup delegation, timeout/cancellation guarantees or memory-limit failure recovery.

## Controlled plugin replacement

The [replacement probe](hyperlight-aks/plugin-replacement-probe.py) held the same live `Sandbox` and native thread in the original positive pod. It stored guest global `value = 41`, emitted a ready record and waited on its private coordination file. The following sequence ran once:

1. Delete only the experiment's plugin pod and wait for deletion. Its socket disappeared; its CDI file remained.
2. A narrowly mounted helper refused cleanup if the socket still existed, the CDI file was not a root-owned regular file, or its hash differed from the recorded value. It removed only that matching file.
3. Recreate the guarded plugin pod. Its pod UID changed, its image ID remained pinned, and it recreated the same CDI specification and registered with kubelet.
4. Release the application probe using its continuation file. The held guest returned 42 from `value + 1`; the probe exited zero.
5. Delete the original positive pod to release its allocation. A fresh pod on the same node, with a different UID, obtained a new allocation and passed all stages of the full SDK probe after installation and restoration of the deny policy.

The probe permitted 210 seconds for coordination and ran under a 240-second outer timeout plus five-second kill allowance. Use a fresh positive pod so existing coordination files cannot satisfy the wait. The positive pod's immutable image and security settings were unchanged across the replacement sequence.

This was an operator-assisted replacement cycle with verified CDI cleanup. It did not test automatic DaemonSet recovery, kubelet restart, plugin crash recovery, lost CDI recovery without assistance or node disruption. The retained CDI file also explains why the guarded experiment cannot simply restart over old state; recovery-capable manifests remain work for #1237.

## Reproduction and restoration

Start with the [preflight sequence](hyperlight-aks-automatic-verification.md#reproduction-and-cleanup). Save safeguard properties before any authorized exception, exclude only the dedicated infrastructure namespace, and verify Azure's completed update and Kubernetes admission. Inspect the selected customer node's device family before deploying the plugin. Render the node placeholder in the plugin and positive-pod manifests, server-dry-run each, and apply only after the prerequisites pass. Wait for kubelet registration and advertised capacity before starting the positive pod.

After package installation, apply the deny policy and run the SDK probe in both positive and control pods, retaining exit codes and stage records separately. For the replacement test, stream the replacement probe into the positive pod's virtual-environment interpreter under the outer timeout. Wait for its ready marker, perform the bounded replacement sequence above, and write its continuation marker through a separate transport call. Fresh-pod installation requires a temporary network allowance; reapply the deny policy before guest execution. That bootstrap allowance is not adapter egress conformance.

Cleanup removed the application pods first, then the plugin. The helper again verified socket absence and removed only the matching CDI file. Both absence checks passed. It and the read-only inspector were removed, followed by the test namespaces. The temporary exclusion was removed while preserving any other exclusions. Final Azure safeguard properties matched the saved baseline exactly: Enforce, Baseline, and no customer exclusions. A repeated server-side dry-run returned the original baseline host-path policy refusal; its empty test namespace was then removed. The default workload pool returned to zero nodes and the test NodeClaim disappeared. Pre-existing system resources remained.

No registry or permanent node-pool configuration was created. One temporary workload node incurred transient compute/storage use; no billing amount was measured. Raw infrastructure metadata was retained privately.

## Remaining boundaries

The result establishes Azure Linux/KVM SDK feasibility on this configuration. Ubuntu, MSHV, other VM sizes/images, the suite's Linux adapter, CodeAct, worker memory/output enforcement, timeout/cancellation, abrupt owner death, OOM, node disruption and replica fencing remain unverified. The application still had no writable delegated cgroup subtree. These measurements advance #1230 without closing the independent adapter, infrastructure recovery and owner-routing follow-ups.
