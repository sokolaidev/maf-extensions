# Hyperlight inside Azure Container Apps

> Exploration for [#1229](https://github.com/sokolaidev/maf-extensions/issues/1229), inspected on 2026-09-14. This record evaluates standard managed ACA Linux application containers. It contains a reproducible API/source audit, not an ACA execution measurement. A subsequent [live Consumption/D4 probe](hyperlight-aca-live-probe.md) records actual device and SDK failures separately. The hosting decision is summarized in the [backend document](../backends/hyperlight.md#azure-container-apps).

## Decision

**Direct execution is unsupported by the published ACA platform contract inspected here.** No supported mechanism was found to supply `/dev/kvm` or `/dev/mshv` to an application container. This conclusion applies to Consumption and Dedicated workload profiles, including the documented Flex preview and confidential-compute offerings. It is a deployment decision from official documentation and API source, not a claim that every underlying ACA node lacks virtualization hardware or that an undocumented experiment could never run.

The current packaged adapter independently refuses Linux. Completing [#1228](https://github.com/sokolaidev/maf-extensions/issues/1228) would remove that adapter blocker only for its validated Linux/KVM family; it would not grant ACA device access. MSHV is outside that milestone. Keep ACA out of the adapter's supported configurations unless Microsoft documents a usable device facility and the adapter passes conformance on it.

The smallest alternative is an ACA application calling a separate Hyperlight worker service on a suitable dedicated host. That is a new remote integration, with authentication, owner routing and cleanup semantics to implement. It is not the existing local backend executing inside ACA. A single Windows x86-64 WHP host can use the existing validated adapter family; a Linux worker depends on #1228, and AKS integration is separately investigated in [#1230](https://github.com/sokolaidev/maf-extensions/issues/1230).

## Evidence baseline and limits

| Item | Inspected version or result |
| --- | --- |
| Repository source | [`da40a37ba85d0a33690320836e7e0fb8aa6a6ba7`](https://github.com/sokolaidev/maf-extensions/tree/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7), after the Windows implementation in [#1223](https://github.com/sokolaidev/maf-extensions/pull/1223) |
| Packaged adapter | `maf-sandbox-hyperlight` development version `0.0.0`; host Python requirement 3.12–3.14; native Wasm/Python guest dependencies selected only on Windows AMD64 |
| SDK and guest | Exact `hyperlight-sandbox`, `hyperlight-sandbox-backend-wasm`, `hyperlight-sandbox-python-guest` `0.7.0`; upstream source [`6ae78065617d5603c1dd5fdbb63d62d8201ac68c`](https://github.com/hyperlight-dev/hyperlight-sandbox/tree/6ae78065617d5603c1dd5fdbb63d62d8201ac68c) |
| ACA API source | `Azure/azure-rest-api-specs` at [`004ab012286e97007331e00ebe67aa802f3aa3d1`](https://github.com/Azure/azure-rest-api-specs/tree/004ab012286e97007331e00ebe67aa802f3aa3d1/specification/app/resource-manager/Microsoft.App/ContainerApps): newest listed stable `2026-07-01`, newest listed preview `2026-03-02-preview` |
| Documentation cross-check | [ARM template reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/containerapps) listed `2026-01-01` and `2025-10-02-preview`; the audit therefore also inspected the newer API-source versions above. A published schema does not establish regional rollout. |
| Kubernetes comparison | [`fc71b4501d23977fcc54f7be144d884fc8210667`](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/tree/fc71b4501d23977fcc54f7be144d884fc8210667); the [example host](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/hyperlight-app/host/Cargo.toml) declares `hyperlight-host` `0.12` with `kvm`/`mshv3`, not our Python SDK trio |
| Audit tools | PowerShell 7.6.6, GitHub CLI 2.97.0, Windows; local documentation validation uses host CPython 3.13.12 |
| Azure execution | No resources provisioned; no region, deployed workload profile, container image digest, replica, kernel or hypervisor selected or measured |
| Runtime results | Device presence/open, KVM/MSHV VM creation, pinned SDK execution, Linux adapter conformance, native-memory enforcement, non-root execution, shutdown, scale-in and revision replacement were **not tested** on ACA |

The only executed probe in this investigation reads public API source and enumerates its configuration surface. Historical WHP measurements in #1223 are evidence for the separate Windows family, not ACA measurements. No SDK smoke result, Azure deployment rejection or device error is inferred from the source audit.

## Why the platform blocks direct execution

[Hyperlight's SDK quick start](https://github.com/hyperlight-dev/hyperlight-sandbox/tree/6ae78065617d5603c1dd5fdbb63d62d8201ac68c#quick-start) requires KVM, MSHV or Hyper-V. The [Linux prerequisites](https://hyperlight.org/guides/getting-started/#prerequisites) require enabled KVM and permission to access its device. In a container, the host must provide the driver and virtualization facility, and the runtime must grant device access; installing a wheel or creating a device filename cannot supply those authorities.

[ACA's container limitations](https://learn.microsoft.com/en-us/azure/container-apps/containers#limitations) rule out privileged containers with host-level access. That restriction alone is insufficient: the pinned upstream [device plugin](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/device-plugin/main.go) generates CDI device nodes with read/write permission and configurable UID/GID, defaulting to 65534. Its [application manifest](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/manifests/examples/deployment-kvm.yaml) requests `hyperlight.dev/hypervisor` and runs without root, with capabilities dropped. The [plugin DaemonSet](https://github.com/hyperlight-dev/hyperlight-on-kubernetes/blob/fc71b4501d23977fcc54f7be144d884fc8210667/deploy/manifests/device-plugin.yaml) itself runs as root with writable host mounts for the kubelet device-plugin directory and CDI directory, plus a host `/dev` mount. Those infrastructure operations are the missing part of the ACA comparison.

Microsoft explicitly documents that [ACA does not expose the underlying Kubernetes APIs](https://learn.microsoft.com/en-us/azure/container-apps/compare-options#azure-container-apps). The inspected [stable schema](https://github.com/Azure/azure-rest-api-specs/blob/004ab012286e97007331e00ebe67aa802f3aa3d1/specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2026-07-01/openapi.json) and [preview schema](https://github.com/Azure/azure-rest-api-specs/blob/004ab012286e97007331e00ebe67aa802f3aa3d1/specification/app/resource-manager/Microsoft.App/ContainerApps/preview/2026-03-02-preview/openapi.json) provide no application device mapping, CDI selection, Kubernetes extended-resource request, `hostPath`, runtime-class selection or container security-context facility for this integration. This combined evidence is the documented platform blocker. Adding arbitrary Kubernetes fields to ACA YAML is not a documented device-injection mechanism.

| Workload profile or adjacent feature | Evidence and consequence |
| --- | --- |
| Consumption, including legacy Consumption-only environments | [Serverless compute and scaling](https://learn.microsoft.com/en-us/azure/container-apps/plans) do not provide a customer-managed node or device attachment surface. No KVM/MSHV access mechanism was found. |
| Dedicated | [Reserved compute pools](https://learn.microsoft.com/en-us/azure/container-apps/workload-profiles-overview) change capacity and tenancy. Dedicated capacity does not grant kubelet access, host mounts or device injection. |
| Flexible profile, preview | The same [profile documentation](https://learn.microsoft.com/en-us/azure/container-apps/workload-profiles-overview) describes a single-tenant pool, maintenance/networking options and larger replica sizes. It documents no hypervisor device facility. |
| Confidential DC-series profiles | [Confidential compute](https://learn.microsoft.com/en-us/azure/container-apps/confidential-compute) protects data in use through the underlying confidential VM infrastructure. Assigning a DC profile does not document nested virtualization or a hypervisor device inside the application. |
| GPU profiles and preview `resources.gpu` | GPU-specific provisioning is evidence for GPUs only; the resource object is not a general Kubernetes resource-name map. |
| ACA managed sessions/sandboxes | The API also contains session resources. They are separate service interfaces, not a way to inject a host hypervisor into an ordinary application container; this record makes no new claim about the ACAS backend. |

Absence from a schema alone cannot prove a service implementation lacks a hidden feature. Here it supports a narrower conclusion: neither the public API nor the inspected official hosting documentation provides the prerequisites for a supported deployment. Revisit on an official facility announcement or a Microsoft-supported configuration specifying the device, eligible profiles/regions, permissions and runtime constraints.

## Reproduce the source audit

Run this read-only PowerShell 7 audit with a configured GitHub CLI. It downloads no private repository data and creates no Azure resources. It checks the actual inherited container shape, the resource and volume shapes, and the shutdown setting; it is not a runtime test or a generic OpenAPI validator.

```powershell
$specRef = '004ab012286e97007331e00ebe67aa802f3aa3d1'
$specRoot = 'specification/app/resource-manager/Microsoft.App/ContainerApps'
foreach ($channel in @('stable', 'preview')) {
    gh api "repos/Azure/azure-rest-api-specs/contents/${specRoot}/${channel}?ref=$specRef" --jq '.[].name'
    if ($LASTEXITCODE -ne 0) { throw 'API version listing failed' }
}
foreach ($apiPath in @('stable/2026-07-01', 'preview/2026-03-02-preview')) {
    $raw = gh api "repos/Azure/azure-rest-api-specs/contents/${specRoot}/${apiPath}/openapi.json?ref=$specRef" -H 'Accept: application/vnd.github.raw+json'
    if ($LASTEXITCODE -ne 0) { throw 'API schema fetch failed' }
    $schema = ($raw -join "`n") | ConvertFrom-Json -AsHashtable
    foreach ($name in @('BaseContainer', 'Container', 'ContainerResources', 'Volume', 'VolumeMount', 'Template', 'WorkloadProfile')) {
        [pscustomobject]@{
            Version = $schema.info.version
            Definition = $name
            Properties = ($schema.definitions[$name].properties.Keys | Sort-Object) -join ', '
            Inherits = ($schema.definitions[$name].allOf | ForEach-Object { if ($null -ne $_) { $_['$ref'] } }) -join ', '
        } | ConvertTo-Json -Compress
    }
    $schema.definitions.StorageType.enum | ConvertTo-Json -Compress
}
```

The audit completed successfully on 2026-09-14. Observed fields, with names sorted for comparison:

| Definition | Stable `2026-07-01` | Preview `2026-03-02-preview` |
| --- | --- | --- |
| `BaseContainer` | `args`, `command`, `env`, `image`, `name`, `resources`, `volumeMounts` | Same, plus `imageType` |
| `Container` | `probes`; inherits `BaseContainer` | Same |
| `ContainerResources` | `cpu`, `ephemeralStorage`, `memory` | Same, plus `gpu` |
| `Volume` | `mountOptions`, `name`, `secrets`, `storageName`, `storageType` | Same |
| `VolumeMount` | `mountPath`, `subPath`, `volumeName` | Same |
| `StorageType` | `AzureFile`, `EmptyDir`, `Secret`, `NfsAzureFile` | Same, plus `Smb` |
| `Template` | `containers`, `initContainers`, `revisionSuffix`, `scale`, `serviceBinds`, `terminationGracePeriodSeconds`, `volumes` | Same |
| `WorkloadProfile` | `maximumCount`, `minimumCount`, `name`, `workloadProfileType` | Same, plus `enableFips` |

`ephemeralStorage` is read-only in both resource definitions. `mountPath` names the destination inside the container; it is not a host-device selector. `StorageType` is an extensible string enum, so its listed values are documented options, not proof that every other string is rejected. Whole-file searches also found no KVM/MSHV, host-path, device-injection or security-context definition. No live ARM request was submitted to test unknown-field rejection.

## Device diagnostics if the platform contract changes

A usable Linux target requires x86-64 hardware virtualization exposed to its host, a working KVM driver/device (or a separately validated MSHV family), runtime permission for that character device, and read/write access for the actual non-root worker identity. If the host is itself a VM, its platform must expose the required virtualization support. Seccomp/device policy must permit the native API operations. A visible device does not establish any of the later stages.

| Stage | Evidence to collect | Meaning of a failure |
| --- | --- | --- |
| Platform support | Official facility, API version, region/profile and deployment configuration | Without a supported attachment mechanism, stop the deployment claim here. This investigation stopped here. |
| Device discovery | Character-device type, major/minor number, owner/mode for `/dev/kvm` and `/dev/mshv`; effective worker UID/GIDs | `ENOENT` means the path is absent in this container, not that the node CPU lacks virtualization. A regular file with the name is not a device. |
| Device open | Open the selected device read/write as the worker and record the syscall and errno | `EACCES`/`EPERM` can involve filesystem permissions or runtime/security policy. Other errors require driver/platform diagnosis; do not collapse them into “device missing.” |
| Native VM creation | For KVM, `KVM_GET_API_VERSION` followed by `KVM_CREATE_VM`, then close the VM/device descriptors; retain exact stage and errno | API/device open success does not establish VM creation. Failure can involve virtualization availability, policy, resources or driver compatibility; diagnose from evidence. MSHV needs its own native API probe. |
| Pinned Python guest | Exact 0.7.0 trio in a bounded disposable worker, `backend="wasm"`, `module="python_guest.path"`, `heap_size="400Mi"`, `stack_size="200Mi"`, `HYPERLIGHT_MAX_SURROGATES=0`; run `print(6 * 7)`, verify stdout `42`, successful exit, snapshot/restore | Separate import/wheel/cache failures from native initialization and guest failures. Record image digest, Python/kernel/architecture, device and native dependency versions. No such probe was run here because the platform prerequisite was not established. |
| Adapter conformance | The #1228 suite on the actual supported deployment | SDK hello-world is insufficient: cover state/reset, ordinary errors, queue/program deadlines, cancellation, output/native-memory limits, owner death, disposal/purge, egress and CodeAct fixed/per-spec routing. |

The [KVM API documentation](https://docs.kernel.org/virt/kvm/api.html) defines the device and VM operations. This staged recipe is a future validation plan, not fabricated ACA error output. No installation or deployment recipe can currently fill the missing supported device-attachment step.

## Resource and lifecycle assessment

These are source-based requirements for any future direct hosting experiment and for the separate worker alternative. They do not change the current backend declarations.

| Boundary | Current source and hosting consequence |
| --- | --- |
| Native memory | [`HyperlightSandboxConfig`](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_config.py) defaults to 1,536 MiB per worker; [`Job`](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_windows.py) enforces committed-memory and process-tree lifetime limits before initialization. The guest's 400 MiB heap and 200 MiB stack are not the whole worker budget. An ACA container memory allocation does not prove equivalent per-worker containment or delegated cgroup control. |
| Aggregate admission | Budget the application, all simultaneously resident workers, native/hypervisor overhead and a measured margin: `container budget > application + N * worker budget + overhead`. Warm idle sandboxes count in `N`. The current configuration limits each worker, not total resident sandboxes. A container-level OOM can lose every in-process registry and must not be advertised as an isolated program timeout. CPU throttling also consumes wall-clock deadlines. |
| Cache and non-root operation | The pinned guest's [`_default_cache_root` / `_materialize`](https://github.com/hyperlight-dev/hyperlight-sandbox/blob/6ae78065617d5603c1dd5fdbb63d62d8201ac68c/src/sdk/python/wasm_guests/python_guest/python_guest/path.py) uses `XDG_CACHE_HOME`, otherwise Linux home `.cache`, under `hyperlight_sandbox_guests/python_guest`; it creates directories and atomically replaces materialized guest files. Provision a private writable cache and temporary directory for the worker UID. The current Windows [`Worker` environment](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_process.py) does not preserve Linux `HOME`/`XDG_CACHE_HOME`; #1228 must supply minimal Linux runtime variables while excluding application credentials. Non-root operation requires device permission as well as writable directories. |
| Ephemeral storage | [ACA storage](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts) supports temporary container/replica storage and Azure Files. A replica-local cache can be rematerialized after replacement; it cannot persist a live VM, native snapshot or owner registry. No host directory should become a guest file channel. |
| Graceful shutdown | [ACA sends SIGTERM on scale-in/deactivation/deletion, then SIGKILL after the documented default grace period](https://learn.microsoft.com/en-us/azure/container-apps/application-lifecycle-management#shutdown). Both newer audited schemas expose template `terminationGracePeriodSeconds`, default 30 seconds. That configurable allowance is distinct from the probe-level field. Stop admission, cancel/drain active work, call each backend object's `aclose()`, retain cleanup failures, and finish all worker reaping within the deployment's total grace period. |
| Cleanup budget | Default `cleanup_timeout` is 3 seconds per worker attempt; `aclose()` walks its owned sandboxes sequentially. Three seconds is not an upper bound for shutting down an arbitrary number of workers. Admission and drain design must bound the total. Graceful shutdown cannot establish cleanup on abrupt owner death, OOM or SIGKILL; #1228 must supply and validate the Linux equivalent of the Windows job's lifetime containment. |
| Ownership and routing | [`claim_host`](https://github.com/sokolaidev/maf-extensions/blob/da40a37ba85d0a33690320836e7e0fb8aa6a6ba7/packages/maf-sandbox-hyperlight/src/maf_sandbox_hyperlight/_windows.py) admits one host process per Windows machine. Backend objects share a process-local key/kind registry; a foreign owner must refuse cleanup. A Linux namespace-local lock would not establish cross-container ownership. Requests, reset, exact-instance disposal and scope purge must reach the same authoritative owner. |
| Revision replacement and scale-in | [Even single-revision rollout starts the replacement before deactivating the old revision](https://learn.microsoft.com/en-us/azure/container-apps/revisions#zero-downtime-deployment). `minReplicas=maxReplicas=1` does not prevent old/new overlap or host replacement. Readiness and traffic switching do not migrate native state. Owner generation changes must invalidate old handles, and disappearance must report session loss rather than silently recreating a supposedly warm sandbox. |
| Session affinity | [ACA affinity uses HTTP cookies and can route to a new replica when the old one disappears](https://learn.microsoft.com/en-us/azure/container-apps/sticky-sessions). It does not route by full `SandboxKey`/kind/`instance_id` or cover a purge sent by another client. Revision labels identify revisions, not worker ownership. Durable request/purge routing requires a separate design. |

## Smallest separate-worker alternative and follow-up boundary

Keep kinds on the existing core protocol. Place one authenticated remote service around one owning backend process on a dedicated, validated Hyperlight host, and let the ACA application reach it over TLS. Start with one service endpoint and explicit session loss on worker-host replacement. This avoids inventing a distributed owner registry for the first implementation, but still requires owner generation and exact-instance checks. Do not run multiple backend-owning web-server processes on that machine. A Windows worker can use the existing WHP family; selecting a Linux VM requires #1228 and actual KVM validation, not merely a VM SKU name.

The additional implementation should be limited to a single-owner remote `RUN_CODE`/`SNAPSHOT` prototype, with these acceptance boundaries:

- Authenticate the ACA application with a service-scoped audience and authorized principal (for example an Entra token obtained by the application identity). Derive tenant/scope authority from trusted authentication and authorization; reject cross-scope acquire, execution and purge. TLS and private connectivity alone do not authorize callers. Keep application credentials out of the native worker environment.
- Keep policy evaluation in the existing router/backend boundary. Preserve the complete key, kind, owner generation and `instance_id`; refuse stale or foreign handles. Implement reset, exact-instance disposal and scope purge on that same owner and return existing cleanup failures honestly.
- Bound requests, responses and aggregate workers. Carry deadlines across the network without granting a fresh full budget at each hop. Propagate cancellation/disconnect to bounded native termination. Do not automatically replay execution after an ambiguous network failure: the program may already have run.
- Drain and reap on shutdown, fence a replaced owner, report state loss explicitly, and test purge retry and lost acknowledgements. A newly empty registry cannot prove that a lost owner has stopped running guests.
- Validate a real ACA-client-to-dedicated-worker round trip separately from Windows/Linux guest conformance. Keep file channels, native host tools, multiple workers, automatic failover and VM-state migration outside the prototype.

This is a viable architecture to implement because the application need not receive a hypervisor device; the worker uses an already validated host family. Remote transport and an ACA round trip remain unimplemented and unmeasured. The bounded prototype is tracked in [#1236](https://github.com/sokolaidev/maf-extensions/issues/1236) under #382; no ACA device-injection implementation task was opened against a mechanism the platform has not published. Existing #1228 and #1230 retain their independent scopes.
