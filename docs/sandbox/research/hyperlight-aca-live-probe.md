# Hyperlight: live ACA Consumption and Dedicated probes

> Exploration for [#1229](https://github.com/sokolaidev/maf-extensions/issues/1229), measured on 2026-09-14 in Europe/Amsterdam (2026-09-13 UTC). This follows the [source-only feasibility investigation](hyperlight-aca-feasibility.md) with actual execution inside standard managed ACA application containers. The hosting decision is summarized in the [backend document](../backends/hyperlight.md#azure-container-apps).

## Measured result

**The pinned Hyperlight Python guest could not execute in either tested ACA profile: Consumption and Dedicated D4 in Sweden Central.** Both `/dev/kvm` and `/dev/mshv` were absent for root and UID 65534. SDK import, guest-cache materialization and `Sandbox` construction succeeded, but the first `Sandbox.run()` attempted VM creation and raised:

```text
RuntimeError: Failed to create sandbox: failed to build ProtoWasmSandbox: No Hypervisor was found for Sandbox
```

These are runtime observations, not inferred deployment failures. Both apps deployed successfully and ran the probe. The SDK constructor alone does not establish native VM creation because this version defers it until execution. No Python source ran inside a Hyperlight guest, and snapshot/restore was consequently not reached.

Dedicated D4 exposed the CPU `svm` flag while Consumption exposed neither `svm` nor `vmx`. The missing device and identical SDK failure on D4 show why CPU virtualization flags alone are insufficient. These observations do not prove what every ACA node, region or future profile exposes; Flex, confidential compute, GPU profiles and MSHV-enabled infrastructure were not tested.

## Exact configuration

| Item | Value |
| --- | --- |
| Resource type | `Microsoft.App/containerApps`, not ACA jobs or managed sessions |
| Region | `swedencentral` |
| Environment | Newly created, isolated workload-profiles environment; no custom VNet; logs destination `none`; console collected through the ACA log-stream API |
| Profiles | Default `Consumption`; added Dedicated `D4` with minimum zero and maximum one profile instance |
| Each app | One `probe` container; 1 vCPU, `2Gi` memory; minimum and maximum one replica; single revision mode; no ingress; 30-second termination grace |
| App deployment API | ARM `2026-01-01`; Azure CLI 2.89.0 |
| Container image index digest | `sha256:6cbc5ecd3afd8d1fc4211133eb51c538c6c2bf015eb6e2812c80bfcef84877d0` |
| Linux/amd64 image manifest digest | `sha256:70e0f9026ad9add8a7e087eb5c8dccfd3af9b580cbe6703e58e1d6755da0dad5` |
| Base image | `python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2` |
| Runtime reported inside both apps | Host CPython 3.13.15; Debian GNU/Linux 13 (trixie); x86-64; kernel `6.6.150.1-1.azl3` |
| Installed dependencies | `hyperlight-sandbox==0.7.0`, `hyperlight-sandbox-backend-wasm==0.7.0`, `hyperlight-sandbox-python-guest==0.7.0`; binary wheels only |
| Native settings | `backend="wasm"`, `module="python_guest.path"`, `heap_size="400Mi"`, `stack_size="200Mi"`, `HYPERLIGHT_MAX_SURROGATES=0` |
| Identities tested | UID/GID 0 and 65534, each with empty supplementary groups; separate disposable processes |
| Worker environment | Only executable search path, private home/cache/temp directories and the surrogate setting; no application environment or credentials inherited |
| Image pull | Temporary user-assigned identity with only an `AcrPull` assignment on the test registry; no guest host-tool registration or file channels |
| Measured probe source SHA-256 | `b7e791cad81aa382f8f22749b594094e93df4ffc87cf4314b3a6a3ce6e6a637c` |
| Consumption observation window, UTC | `2026-09-13T23:37:44.9623243+00:00` through `2026-09-13T23:37:45.4394877+00:00` |
| Dedicated observation window, UTC | `2026-09-13T23:39:00.8695743+00:00` through `2026-09-13T23:39:01.3549786+00:00` |

The registry and resource identifiers are omitted from this public record. Both deployed app configurations were checked against the same image digest before cleanup. A local Docker run exercised the same probe/image as a control; that control's LinuxKit kernel is not the ACA kernel above.

## Observations

The [published issue evidence](https://github.com/sokolaidev/maf-extensions/issues/1229#issuecomment-5657176298) and the table below summarize the collected console observations. Each profile returned one environment record, cgroup observations, four child results and a completion record. The child results included stdout/stderr and the process exit/timeout outcome; stdout contained the individual device and SDK observations.

| Check | Consumption | Dedicated D4 |
| --- | --- | --- |
| CPU flags | `vmx=false`, `svm=false`, `hypervisor=true` | `vmx=false`, `svm=true`, `hypervisor=true` |
| `stat` and read/write `open` of both devices | `ENOENT` for both identities | `ENOENT` for both identities |
| KVM API/version and `KVM_CREATE_VM` ioctl | Not reached: no device descriptor | Not reached: no device descriptor |
| SDK imports and exact package versions | Succeeded for both identities; all three 0.7.0 | Same |
| Writable guest cache | Succeeded for both identities; AOT file 43,890,120 bytes | Same |
| `Sandbox` construction | Succeeded for both identities | Same |
| First guest run | Native creation failed with the error above for both identities | Same |
| Snapshot/restore | Not reached | Not reached |
| Memory allocation visible through cgroup | v1 `memory.limit_in_bytes=2147483648`; v2 files absent | v2 `memory.max=2147483648` |
| CPU quota observation | v2 file absent; v1 CPU quota not queried | v2 `cpu.max="100000 100000"` |
| Non-root security fields | Effective capabilities zero; `NoNewPrivs=1`, `Seccomp=2` | Same |
| Root security fields | `CapEff=00000000a80425fb`; `NoNewPrivs=1`, `Seccomp=2` | Same |
| Probe children | All exited zero without a timeout; collected failure observations | Same |

Exit zero means the diagnostic process collected its observations. It does not mean Hyperlight executed successfully. `ENOENT` is device absence in the container, distinct from `EACCES`/`EPERM`, a failing VM-creation ioctl, or a guest-language error. This probe reached a native SDK creation failure independently of the device checks by attempting `run()` even after device discovery failed.

The Dedicated console endpoint omitted the fixed opening `{"stage": ` from each top-level payload; the Consumption endpoint retained a leading `F ` before the JSON. Nested child stdout was complete JSON in both cases. A direct `az containerapp exec` rerun of the same script inside the Dedicated replica returned complete JSON and independently confirmed the same device failures, CPU flags, cache success and SDK failure for both identities. The cause of the console formatting difference was not investigated.

## Reproduce

The [probe](hyperlight-aca-probe.py) and [Dockerfile](hyperlight-aca-probe.Dockerfile) are committed together. The source used for the ACA measurements above is pinned at [d23b24f](https://github.com/sokolaidev/maf-extensions/blob/d23b24f1c81df9143090e843f486afe7d08086c2/docs/sandbox/research/hyperlight-aca-probe.py), matching the recorded source hash. The current probe additionally requires a matching completion marker from every child and exits nonzero on incomplete collection. That collector revision is covered by regression tests and a local Docker control; the ACA observations and image digests above belong to the pinned measured version.

From the repository root, build and check the image:

```powershell
docker build --platform linux/amd64 -f docs/sandbox/research/hyperlight-aca-probe.Dockerfile -t hyperlight-aca-probe:1229 docs/sandbox/research
docker run --rm --memory 2g --cpus 1 --pids-limit 128 --network none hyperlight-aca-probe:1229
```

The local command intentionally supplies no hypervisor device. It validates the probe's negative path; it is not the cloud measurement. Rebuilding can produce a different image digest; record the pushed digest and deploy that immutable reference to both profiles.

For an ACA reproduction, use a disposable resource group/environment and an existing registry you control. Publish the image, create a temporary identity for image pull and grant it `AcrPull` on that registry. Wait for environment provisioning to finish, add `D4` with `--min-nodes 0 --max-nodes 1`, and wait for that update too. The measured environment rejected an app update while its profile update was still in progress; that control-plane sequencing error is unrelated to Hyperlight execution.

Set these PowerShell variables to the resource IDs and immutable image reference of that disposable deployment, then generate the app bodies. The environment must contain `Consumption` and `probe-d4` (a D4 profile); the image-pull identity must already exist and have registry access.

```powershell
# Supply $probeEnvironmentId, $probeIdentityId, $probeRegistryServer, $probeImage,
# $probeSubscriptionId and $probeResourceGroup from your disposable deployment.
foreach ($case in @(@{name='hl-probe-consumption';profile='Consumption'}, @{name='hl-probe-d4';profile='probe-d4'})) {
    $body = @{
        location = 'swedencentral'
        identity = @{type='UserAssigned';userAssignedIdentities=@{$probeIdentityId=@{}}}
        properties = @{
            managedEnvironmentId = $probeEnvironmentId
            workloadProfileName = $case.profile
            configuration = @{
                activeRevisionsMode = 'Single'
                registries = @(@{server=$probeRegistryServer;identity=$probeIdentityId})
            }
            template = @{
                terminationGracePeriodSeconds = 30
                containers = @(@{name='probe';image=$probeImage;args=@('--hold-seconds','1800');resources=@{cpu=1;memory='2Gi'}})
                scale = @{minReplicas=1;maxReplicas=1}
            }
        }
    }
    $manifestPath = "$($case.name).json"
    $body | ConvertTo-Json -Depth 16 | Set-Content $manifestPath -Encoding utf8
    $url = "https://management.azure.com/subscriptions/$probeSubscriptionId/resourceGroups/$probeResourceGroup/providers/Microsoft.App/containerApps/$($case.name)?api-version=2026-01-01"
    az rest --method PUT --url $url --body "@$manifestPath"
    if ($LASTEXITCODE -ne 0) { throw 'App deployment request failed' }
}
```

Wait for each actual replica's `probe` container to be running, not merely the app resource's `Succeeded` state. Record the deployed image, profile, resource limits and revision, then collect each revision's console with `az containerapp logs show --container probe --revision <revision> --tail 300 --format json`. If the console transport changes the JSON prefix, preserve that output and cross-check directly with `az containerapp exec --container probe --revision <revision> --command 'python -I -u /probe/hyperlight-aca-probe.py'`. Require all four child results with `collection_complete=true` and `probe_complete` before calling collection complete. A missing or malformed child completion marker, timeout, nonzero child exit, or launch/cleanup failure produces `probe_incomplete`, exit status 1 and no log-collection hold. A captured Hyperlight failure remains a valid observation when its diagnostic child completes normally.

Each child has a 45-second deadline, bounded retained stdout/stderr and process-group termination/reaping. The private writable cache is removed after its child exits. The 2 GiB container allocation bounds the whole probe, not each native worker independently; this is not the Linux adapter's committed-memory/owner-death conformance. The optional 1,800-second hold only keeps console logs accessible: **it does not clean up Azure resources**, and an ACA app can restart after the process exits. Delete the disposable apps/environment/resource group, the exact temporary role assignment and the probe image repository after collection. Do not delete a reused environment or registry.

## Limits and disposition

This establishes a measured failure for the tested standard Consumption and D4 application containers, including a non-root process with a working guest cache. It does not establish a successful Linux adapter, per-worker memory enforcement, guest timeout/cancellation, egress, owner death, revision state transfer, scale-in cleanup or CodeAct conformance. Those stages could not be exercised because no guest was created. The source-based assessment remains applicable to untested profiles without being presented as measurements of them.

The live result reinforces the separate-worker alternative in [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236). Linux adapter work [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228) cannot itself supply missing ACA devices. AKS device injection remains independently investigated in [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230).

The [issue evidence](https://github.com/sokolaidev/maf-extensions/issues/1229#issuecomment-5657176298) records these measurements, and #382 has been updated. Cleanup was requested at `2026-09-13T23:42:34Z`. The temporary apps and pull identity were removed; deletion of the exact registry role assignment and probe image repository was verified. The environment initially remained in `ScheduledForDelete`; a subsequent check on 2026-09-14 UTC confirmed that the entire disposable resource group no longer existed. Cleanup of all cloud resources created by this probe is complete.
