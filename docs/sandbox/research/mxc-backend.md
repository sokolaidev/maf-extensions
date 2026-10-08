# MXC integration research and initial backend design

> A research record and initial proposal, recorded on 2026-10-02: what MXC v0.9.0 adds to the suite, what the suite can offer MXC users, and how an experimental adapter could fit the existing protocol. The maintainer selected rich Python through MXC Hyperlight, with code/text execution, file inputs and output artifacts, on both Windows and Linux for the first usable version. Optional persistent Python state must be recoverable after host restart, with a durable checkpoint after every successful tool call. Host configuration may also permit recovery on another compatible machine. Allowlisted networking is desired where enforceable; closed networking remains the baseline. Transport and runtime distribution remain implementation decisions to validate. No MXC adapter is implemented or qualified by this record.
> The agreed scope and recovery decisions now live in [MXC Hyperlight design decisions](../backends/mxc.md). This record retains the original assessment and design reasoning; the main document owns the decisions and their implementation status.
> The follow-up logical-byte quota and local SQLite recommendations were subsequently accepted; their selected semantics now live under [Storage quotas](../backends/mxc.md#storage-quotas) and [Local storage implementation](../backends/mxc.md#local-storage-implementation). The maintainer subsequently selected [bounded clock forgiveness](../backends/mxc.md#clock-forgiveness) for result expiry and [session deletion](../backends/mxc.md#session-deletion) that preserves promised delivery while retiring the session identity. The owning design now also specifies [durable clock-budget accounting](../backends/mxc.md#durable-clock-budget-accounting) and [reservation recovery](../backends/mxc.md#reservation-recovery). The follow-up proposal below retains its pre-decision wording.

MXC would extend the suite's execution environments. The proposed integration places it beneath `SandboxRouter`, with a separately qualified profile for each concrete MXC backend. Workload kinds, admission, result labels and application ownership remain in the suite. A common configuration schema does not make all MXC backends interchangeable.

## Evidence and scope

The original assessment was made on 2026-09-29 and 2026-09-30. This record checks the design against MXC tag `v0.9.0`, resolved on 2026-10-02 to commit `86fb3d2abaf9c431556692037bff881830b543a5`. It compares the suite at commit `240e62f91be648a0214ea8cdc08f67f96045c620`; unrelated working-tree edits are not evidence of delivered behavior. Links to current suite guides explain the contracts, while those commit identifiers identify the snapshots assessed here.

The evidence consists of release notes, tagged backend guides, selected MXC source and contract tests, and the suite's protocol, CodeAct runtime contract and backend implementations. No MXC binary was installed or executed. No performance, cost, cross-platform compatibility, isolation or production-readiness result was measured.

The [v0.9.0 release](https://github.com/microsoft/mxc/releases/tag/v0.9.0) calls `0.9.0-alpha` the stable policy schema and includes WSLC and IsolationSession lifecycle APIs without experimental opt-in. Hyperlight, NanVix and Windows Sandbox use the development `0.10.0-alpha` contract and remain experimental. The release is marked as a prerelease. The [tagged README](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/README.md) warns that some generated policies are overly permissive and that MXC profiles should not currently be treated as security boundaries. Stable schema support is not a security qualification, and a wrapper cannot create missing enforcement.

The earlier [two-axis policy record](two-axis-sandbox-policy.md) mentions MXC as a backend family. It does not assess this release or design an adapter. This record owns that narrower investigation. The existing [Hyperlight record](hyperlight-backend.md) and [Hyperlight guide](../backends/hyperlight.md) describe the suite's separate direct integration.

## MXC 1.0 migration spike

[Spike #1780](https://github.com/sokolaidev/maf-extensions/issues/1780) assesses release `v1.0.0`, commit `7bf210247986cb73b1b314df60c2f1109c479c0b`, against the independently qualified 0.9 baseline from [#1778](https://github.com/sokolaidev/maf-extensions/pull/1778). The [release](https://github.com/microsoft/mxc/releases/tag/v1.0.0) stabilizes versioned Rust, .NET and Node SDK entrypoints, but its [Hyperlight backend](https://github.com/microsoft/mxc/blob/7bf210247986cb73b1b314df60c2f1109c479c0b/docs/backends/hyperlight/hyperlight-backend.md) remains experimental under `1.1.0-alpha`. Generic lifecycle APIs do not supply persistent Hyperlight interpreter semantics. The runner still rewinds its baseline; the Unikraft 0.17.0 HostPrint collector still accumulates an unbounded string, and snapshot export has no host-selected byte/file budget. All three experimental overlays therefore remain necessary in this candidate.

The candidate lives separately in [mxc_v1_patch](../../../scripts/experiments/mxc_v1_patch/README.md). It retains the old tools and evidence, uses the SDK's documented-as-internal compatibility exports only inside the generated probe wrapper, and ports session state transitions, bounded capture and bounded export without changing the SQLite store. Source, dependency and rootfs identities are pinned. The manager verifies all affected files across the complete prerequisite chain before applying or removing a layer. Future upstream replacement still belongs behind the wrapper and requires equivalent behavior tests.

The 2026-10-08 [native qualification run](https://github.com/sokolaidev/maf-extensions/actions/runs/37752153210) tested source `da064543023d2b95ae06735d2746f520caa4eb61`. Linux/KVM and Windows/WHP independently passed locked builds, helper/collector/budget unit tests, fresh-state isolation, NumPy/pandas/lambda/open-file continuity after a killed helper, malformed snapshot refusal, bounded console controls, bounded native export and all eight format-4 crash boundaries. Quota refusals happened before launch; retained results replayed after checkpoint collection. The [Linux report](../../../scripts/experiments/mxc_v1_patch/linux-result.json), [Windows report](../../../scripts/experiments/mxc_v1_patch/windows-result.json) and their linked [native state evidence](../../../scripts/experiments/mxc_v1_patch/README.md#hosted-qualification) retain exact identities and measurements. The workflow separately identifies dependency resolution as `dependencies-resolved`, never a native pass.

Old-to-new restore was explicitly refused on both platforms: the old snapshot key is `k5e9192dfed5c8dbb-c1`, while the new runtime requires `k485adb2bab362516-c3`. The rejected restore left every old checkpoint file unchanged, and the old-pin helper built in the same job subsequently recovered the Python state. Guest snapshot conversion remains unimplemented. The recommendation is to adopt these pins for new experimental sessions and subsequent adapter work, while retaining the old profile for existing sessions. All three removable overlays remain required; the public SDK stabilization does not replace them.

Guest snapshot compatibility and SQLite format compatibility are separate. A changed helper/kernel/rootfs profile must not silently reopen an old persistent session with fresh state. Keep the exact old helper binary and its profile artifacts available for existing sessions until an explicit migration or retirement policy is selected; a rebuild from the same source can have a different helper hash and is not automatically the same store profile. These checks do not qualify cross-platform snapshot transfer, remote takeover, physical power loss, physical disk quotas or production adapter conformance. Closed networking remains the baseline; enforced allowlisting stays under #1673.

## Capabilities MXC adds

The platform inventory comes from the tagged README and [schema guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/schema.md). These are upstream-described capabilities, not capabilities established for a suite adapter.

| MXC surface | Gap in the assessed suite | Potential use and constraint |
|---|---|---|
| Windows ProcessContainer | No Windows-native execution backend | Run Windows executables with native OS restrictions; capability availability depends on Windows servicing and the selected implementation |
| macOS Seatbelt | No native macOS containment backend | Run local macOS tools without a Linux VM; Linux images and path assumptions do not carry across |
| Linux Bubblewrap and LXC | No adapters for these mechanisms | Add Linux choices beyond Docker; Bubblewrap uses a host tool environment rather than a container image |
| Windows IsolationSession | No isolated-user session backend | Separate Windows session identity and state-aware execution; requires a supported preview OS and has restrictive-policy gaps |
| Windows Sandbox and NanVix | No adapters for these environments | Additional VM execution choices; both are experimental |
| Hyperlight with Unikraft | Existing Hyperlight exposes a narrower Python runtime | Richer Python libraries and additional language runtimes inside a microVM; this is a different guest and execution contract |

MXC also exposes ingress and host-loopback policy, desktop and clipboard restrictions, selected host-filesystem permissions, Learning Mode and denial capture, TypeScript/Rust/.NET SDK surfaces, and versioned JSON contracts. These vary by backend. The suite's egress policy is not an ingress policy, its bounded file plane is not an arbitrary host-mount policy, and its execution telemetry is not permission-learning support. See the [release](https://github.com/microsoft/mxc/releases/tag/v0.9.0), [schema](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/schema.md) and [Rust SDK](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/core/mxc-sdk/README.md).

The [Hyperlight runtime guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/hyperlight/hyperlight-backend.md) describes an `agent` image with Python and scientific/data-processing libraries, plus `python`, `python-shell`, `node`, `bash` and `dotnet-jit` choices. This would expand the suite's microVM workloads. Comparable libraries can already be installed in suitable Docker or ACAS images; their mere availability is not new suite functionality. No startup or memory advantage over those alternatives is established here.

## What the suite offers MXC users

MXC already provides backend selection, execution and lifecycle APIs. Another generic execution wrapper would duplicate that surface. The useful addition is an application contract above it:

| Suite surface | Benefit above MXC | Boundary |
|---|---|---|
| Workload kinds | CodeAct, Bicep, Terraform/OpenTofu and draw.io tool behavior | Each kind still needs a compatible runtime, tools and capabilities |
| Router admission | Check isolation, operations, guest family, file budgets, network modes, sharing and authority before serving work | Checks declarations; it does not certify the provider |
| Trusted request ownership | Bind resources to scope, conversation, agent, kind and call where required | Model input cannot choose another caller's sandbox |
| Files and artifacts | Confined input placement, bounded output collection and explicit output sinks | Mounting a directory alone does not meet these contracts |
| MAF result contract | Separate completion, finite verdicts and justified trusted text from untrusted workload output | FIDES behavior belongs to the MAF integration; other framework adapters do not inherit it automatically |
| Host tools and credential gateways | Expose authorized host services or retain upstream tokens outside the guest | Must be implemented and qualified for the selected profile; existing Docker/WSLC support does not transfer automatically |
| Cleanup and observability | Coordinate call completion, disposal, conversation purge and correlated events | Host-death cleanup and orphan recovery still need provider-specific mechanisms |
| Conformance probes | Exercise file confinement, execution, egress, ownership and disposal contracts | Passing a subset is not a complete isolation audit |

The owning contracts are [policy and isolation](../policy-isolation.md), [information flow](../information-flow.md), [host boundaries](../hosts.md), [operations](../operations.md) and [backend authoring](../backends/writing-a-backend.md).

## Network differences that affect adoption

MXC's [WSLC guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/wsl/wsl-container-getting-started.md) describes `runtimeConfig.networkProxy` as cooperative routing. A client can bypass those environment settings with direct connections. The suite's WSLC adapter instead constructs an internal workload network and a filtering proxy, with optional method/path restrictions and a credential gateway. Replacing the current adapter with MXC WSLC would lose that enforcement unless equivalent topology were supplied. For a future MXC WSLC profile, do not declare `ALLOWLIST` from proxy configuration alone.

That limitation does not describe every MXC backend. The [Bubblewrap guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/bwrap-support/bubblewrap-backend.md) describes a private network namespace and enforced proxy-only networking. The [Seatbelt guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/seatbelt/seatbelt-backend.md) describes kernel-enforced proxy access when host-loopback remains denied; allowing host-loopback broadens access beyond the proxy port. Admission must account for the complete configuration, not just the proxy URL.

IsolationSession's [one-shot contract](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/isolation-session/oneshot.md) requires an explicit unrestricted directional posture and rejects restrictive network policies. It is therefore unsuitable for an initial `CLOSED` workload, despite its stable lifecycle API. MXC also documents a single WSLC daemon worker that serializes execution across sandboxes; concurrent throughput needs verification before adoption. See the [state-aware WSLC guide](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/docs/wsl/wslc-state-aware.md).

For Hyperlight, distinguish runtime internals from the public contract. The tagged guide says the `network` section is rejected and execution runs without networking. `HyperlightScriptRunner` contains legacy allow/block-list translation, but `validate_runner` applies `NetworkPolicySupport::LEGACY`; the public development schema uses directional fields. The [migration regression script](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/tests/scripts/run_hyperlight_network_migration_test.ps1) expects old `network.allowedHosts` requests to fail exact parsing. Internal support is not evidence that an adapter can request allowlisting through the public API. The initial design would qualify the omitted-network request as `CLOSED`, including direct sockets and loopback probes, before declaring it. See the [runner](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/backends/hyperlight/common/src/lib.rs) and [policy validator](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/src/core/wxc_common/src/validator.rs).

## Proposed architecture

The proposed package name is `maf-sandbox-mxc`. No package or public API is created by this record.

```text
agent framework -> kind -> SandboxRouter -> concrete MXC profile -> MXC runtime
                               |
                               +-> existing independently configured backends
```

Each configured backend object would select one concrete containment mechanism, one runtime where applicable, and one verified binary/schema combination. Its isolation and `BackendDeclarations` would be fixed before tool attachment. Do not use MXC's abstract `process` selection to switch enforcement mechanisms after admission, and do not union capabilities across all MXC backends. Probe availability and refuse an unsupported host; do not silently select a weaker backend.

The suite's default `MICROVM` floor would remain unchanged. Native process and shared-kernel profiles require explicit host policy allowing their assessed isolation. A product's use of the word container, or WSLC's use of a shared VM, does not establish the suite's per-workload microVM contract. Windows Sandbox's local full VM also needs a deliberate mapping: the current `Isolation.VM` contract describes dedicated remote infrastructure, so its name alone is insufficient.

Keep the core protocol dependency-free. MXC schema translation, runtime checks and transport belong in the adapter package; kinds continue to depend only on the protocol. Do not add general ingress, desktop policy or unrestricted host mounts to `SandboxSpec` merely to mirror MXC. A future workload requiring those surfaces should motivate a separate contract design.

## Selected first target

The maintainer selected rich Python through MXC Hyperlight on 2026-10-02. It adds a useful runtime while targeting the existing isolation floor. Native Windows, Bubblewrap, Seatbelt and MXC WSLC adapters are outside this first target. The selection establishes workload priority; it does not approve unverified capability declarations or replace the suite's existing Hyperlight adapter.

The first usable version includes both code/text execution and bounded file inputs and output artifacts. It targets Windows x86-64 with WHP and Linux x86-64 with KVM; each host must pass its own live qualification before the first release. These scope choices do not establish AKS readiness or support for other architectures.

The proposed user-facing integration is CodeAct with an explicit Python runtime contract. A first compatibility demonstration would compute a small NumPy result and perform a pandas aggregation from data embedded in the program, returning bounded text. The first usable version's file acceptance example would stage a CSV through the file store, analyze it with pandas, render a chart with matplotlib and return the chart through the existing output sink. Both examples must pass on both target hosts. Those are acceptance workloads to run, not claims that the integration already works.

Runtime instructions would describe the verified Python version, imports, filesystem access, network restrictions and the selected state lifetime. Require dependencies to be present in the pinned image; do not install packages over the guest network during a tool call. Fresh mode would start each call from the documented baseline; optional persistent mode must retain Python variables and dataframes between calls in the same conversation. Host-mounted data needs its own lifecycle regardless of the guest snapshot.

## Optional persistent state

The maintainer requested optional persistence. The proposed default remains fresh state per call, with a host-selected conversation mode for workloads that need notebook-like continuity. Persistence is a first-version requirement to qualify on both hosts, not an existing MXC guarantee. Merely retaining a workspace or a warm native process does not meet the requirement to retain Python objects.

| Mode | Ownership and state | Proposed cleanup |
|---|---|---|
| Fresh, default | Call-scoped instance; no Python variables carried into another call | Deliver outputs, then dispose |
| Persistent, opt-in | Conversation-scoped instance, also separated by trusted scope, agent and kind; serialized access preserves Python state | Reclaim temporary call files without resetting Python; dispose on explicit session end, conversation purge or unrecoverable execution failure |

Mode selection would belong to trusted host configuration, participate in the execution-contract identity and remain fixed for a live instance. A model cannot select another session or enable persistence through tool arguments. Changing mode, runtime or policy requires a new instance. A host that requires call isolation must refuse persistent mode rather than silently widen sharing.

The pinned MXC `HyperlightScriptRunner.run_once` restores its baseline before subsequent executions. A long-lived helper around that unchanged runner would still erase Python state. Persistent mode therefore needs a supported execution path that can retain the guest interpreter, or an upstream change. The experiment must establish that path before the adapter advertises persistence. Do not substitute replayed code, file retention or serialized Python objects and call it equivalent interpreter continuity.

Conversation scope alone does not preserve state under the current suite's cleanup contract: default disposal destroys the instance, and reset erases the state. Persistent mode needs explicit host reuse opt-in and a qualified `RECLAIM` path, using CodeAct's per-call directories without requesting reset. The file design must distinguish temporary call inputs/outputs from any deliberate session workspace; paths retained in Python objects must have a documented lifetime. Background guest processes and threads can mutate files between calls, so exclusive admission by itself is not evidence that output collection is race-free. If safe reclamation or collection cannot coexist with the intended state, revise the lifecycle design before implementation rather than weaken a capability claim.

Timeout, cancellation and native failure may require destroying a persistent guest. Report execution failure and the recoverable checkpoint explicitly; do not silently continue with an empty replacement while implying variables survived. The maintainer revised the earlier live-session-only choice: optional persistent state must now be recoverable after host restart. Artifacts already delivered to application storage follow that storage's retention and durability policy; they do not depend on keeping the guest alive. Persistent mode also needs bounded session count, memory and idle retention, plus tests for conversation separation and purge.

### Recovery after restart

Recovery must restore the agreed Python state and session files from a consistent committed checkpoint. The selected requirement is not met by retaining artifact files alone. The first feasibility gate is whether MXC exposes a supported capture/restore path for a mutated guest, with usable snapshot serialization and compatibility rules. A prepared startup snapshot proves neither capture of user state nor durable recovery. The suite's `SNAPSHOT` capability means reset to a pre-input baseline; it must not be presented as a durable checkpoint contract.

The maintainer selected a durable checkpoint after every successful tool call in persistent mode. The adapter must commit the checkpoint before acknowledging the call as successful. The design must coordinate guest quiescence, Python state, session files, generation metadata and artifact delivery; a partially written checkpoint cannot replace the last complete one. A checkpoint failure after successful Python execution is an operation failure, not an acknowledged durable success. Preserve the last committed checkpoint and prevent later calls from silently building on uncommitted state. Fresh mode has no durable-state requirement.

An interrupted call must be reported as interrupted or outcome-unknown where appropriate. A checkpoint may commit before the caller receives its acknowledgment, so durable call identifiers and result/artifact records must distinguish retrying delivery from executing the program again. Do not replay code automatically to reconstruct state: allowlisted requests may already have produced external effects, and local recovery cannot undo them. Checkpointing after every success does not make external effects exactly-once.

Checkpoint metadata would bind trusted ownership, runtime/image/schema versions, architecture, policy and a generation identifier. Recovery needs exclusive ownership and fencing so an old owner cannot execute or commit after its replacement takes over. Reauthorize the restored session against current host policy before execution, exclude host credentials from checkpoints, and protect checkpoint access and retention like the data it contains. Refuse corrupt or incompatible checkpoints explicitly; do not claim Windows-to-Linux portability or upgrade compatibility without evidence. The persistence mechanism, supported Python objects and treatment of open files, threads and sockets remain to be qualified. If only a restricted serialization approach is feasible, present that limitation for a scope decision instead of silently substituting it for interpreter recovery.

The maintainer selected recovery on another compatible machine when the host requests it through configuration. The proposed default is same-machine recovery; enabling remote recovery is a trusted host decision, not a model tool argument. The receiving host must independently admit the workload and satisfy a tested compatibility profile for the checkpoint, including the native runtime build, guest artifacts, architecture and relevant OS/hypervisor constraints. Matching the Python version alone is insufficient. Supporting Windows and Linux as execution hosts does not promise checkpoint portability between them.

Remote recovery requires checkpoint data and metadata to remain available independently of the failed machine, together with atomic ownership transfer and fencing. Validate that configured storage and coordination can provide those guarantees before admitting persistent work with remote recovery enabled. A lease timeout alone is insufficient if the old guest can keep executing or making external requests; the design must retire or fence that execution authority before allowing a new owner to run. Unavailable storage, lost ownership or incompatible artifacts cause explicit refusal. Do not silently fall back to an uncheckpointed session or a fresh interpreter.

### Host configuration

The following are proposed policy semantics, not a published configuration API. Transport-specific fields and storage interfaces remain to be designed after the capture/restore experiment.

| Setting | Default | Host-selected alternative |
|---|---|---|
| Python state | Fresh per call | Persistent per conversation, isolated by trusted ownership |
| Persistent checkpoint timing | After every successful call, committed before success is acknowledged | No weaker timing selected for the first version |
| Recovery placement | Same machine | Another compatible machine, with independent durable storage and fenced ownership |
| Guest networking | Closed | Explicit allowlist where supported and verified; otherwise refuse that workload |

Storage credentials and recovery authority stay in the trusted host. Enabling persistence or remote recovery does not attach either authority to guest Python. Configuration validation must keep the existing isolation, cleanup and authority floors in force.

### Conditional allowlisted networking

The maintainer requested allowlisted access if possible. Closed networking stays the default; a host may opt into an allowlist only through a supported public MXC contract and a demonstrated enforcement mechanism on the selected platform. Until then, an allowlisted workload is refused rather than executed with unrestricted or silently closed networking. The offline profile remains independently useful.

Qualification must demonstrate both successful access to a permitted destination and denial of a forbidden one, plus bypass checks for raw sockets, direct addresses, IPv4/IPv6, DNS changes, redirects and host-local destinations. Declare method/path restrictions only if separately supported and tested. Proxy environment variables alone are insufficient. Keep credential injection and package installation outside this networking decision: permission to contact a service grants neither host credentials nor permission to mutate the prepared runtime. Persisted session recovery must reapply the currently authorized policy rather than restore stale networking authority.

## First compatibility experiment

For a Hyperlight prototype, begin with Python statements and bounded textual results: no input mount, output mount, attached identity, host tools or guest network access. The `agent` image is the candidate runtime. The host supplies `CodeactRuntime` instructions derived from verified imports and semantics; it does not infer compatibility from the image name. CodeAct remains Python-only even though MXC can execute other languages.

| Declaration | Candidate initial value | Evidence needed before advertising it |
|---|---|---|
| Isolation | `MICROVM` | Hardware boundary, bounded native process ownership, closed networking and explicit guest-to-host channels |
| Capabilities | Only `RUN_CODE` | Python statements, result streams, exit status, error behavior, timeouts and cancellation |
| Egress | Only `CLOSED` | Exact public request accepted and guest connections refused; no inferred support from legacy internals |
| Guest OS family | Empty | Runtime interface does not promise arbitrary POSIX `EXEC` |
| Isolation scopes | `CALL` | Physical execution belongs to the trusted call key; concurrent callers cannot share mutable guest state |
| Cleanup | Dispose | Native execution tree is terminated and reaped; no warm reuse promise |
| Admission | Exclusive through delivery and cleanup | One active call per sandbox object; overlapping owners cannot reset or delete its state |
| Egress observation | False | No attributable network-decision stream established |
| Attached identity | None | Native process environment and all guest channels exclude application credentials |

Defaults in `BackendDeclarations` include file capabilities, so the implementation would explicitly set the capability set rather than inherit defaults. Configure the proposed call-scoped experiment's router with `min_isolation_scope=IsolationScope.CALL`: ordinary CodeAct specs request conversation scope, and a call-only backend does not silently substitute a different scope. Set CodeAct's input/output channels off and use a runtime without a guest storage base. Requests requiring files, shell execution, snapshots, reclaim or host tools would refuse during admission. This text-only experiment is an intermediate gate; the selected first usable version must also qualify the file plane.

The next useful increment is bounded file input and artifact output, for example a staged CSV and a returned chart. Design those together with an adapter-owned private workspace. Never mount the application's repository or home directory as a shortcut. Qualify no-follow host access, symlinks and Windows reparse points, hard links, path replacement, special files, byte/count limits and deletion ordering. Output collection happens only after every guest writer is stopped or excluded. Host-backed mount contents are not reset by restoring the guest snapshot.

## Execution transport and ownership

Prefer a process boundary between Python and MXC native code. A native fault or blocked runtime should be terminable without taking down the agent host. An initial experiment can use the platform executor with a private request file and separate stdout/stderr pipes. Use argv directly and never interpolate model source into a host shell command. The child gets a minimal host-owned environment; it does not inherit application credentials or incidental proxy configuration.

The CLI is a candidate transport, not yet the selected production interface. The Hyperlight runner's `ScriptResponse.standard_out` stays empty while output is emitted through process streams. Capturing those streams is necessary but not sufficient: verify raw-byte preservation, separation, bounded buffering and whether diagnostics mix with guest output. Treat every such byte as untrusted. Parse status only from a trusted control channel or documented process status; guest text that looks like a timeout or error must not control exception classification.

If the CLI cannot preserve status and output independently, use a small out-of-process helper around MXC's typed Rust SDK/C ABI with a dedicated framed control channel. Bound frames and native-side buffering as well as Python reads. Do not parse guest stdout as control messages or silently merge streams to satisfy the protocol. A Node SDK helper remains an option, but adds its runtime/deployment requirement; direct in-process Python FFI is deferred because it shares the agent host's failure domain. The transport experiment must choose one approach before implementation proceeds.

Each acquired wrapper would own a generation identifier associated with `(SandboxKey, kind)`. A one-shot Hyperlight executor need not have an MXC state-aware sandbox ID, so do not invent a mapping to `provision/start/stop` APIs that this backend does not expose. Each new physical guest generation must get a new instance identity; stale cleanup cannot affect a replacement. If one call needs several program executions, document snapshot restoration between them and verify that this matches the kind's runtime assumptions.

`run_code` would use a monotonic deadline starting at method entry, including queueing, process launch and execution. Expiry before submission is `SandboxQueuedTimeout`; expiry after execution begins is `TimeoutError`. The native runtime's timeout receives the remaining budget, while the parent enforces an independent deadline. Cancellation and native failure retire the execution tree before releasing ownership. Cleanup has a separate bounded allowance and reports failures rather than pretending disposal succeeded.

On Windows, qualify a job that limits committed memory and kills the native process tree on owner death. On Linux, qualify cgroup ownership and a supervisor or another equivalent demonstrated owner-death mechanism. Guest scratch allocation is not a host memory limit. Runtime install, image download and snapshot preparation occur as explicit operator setup, outside tool calls; pin the executor and guest artifacts, select an explicit image home, and avoid workload-controlled runtime search paths. Check package licenses and supported distribution artifacts before deciding whether the adapter bundles binaries or requires operator installation.

The initial process-owned experiment would document that calls and purge reach the owning host process. Any residual request files, workspaces or helper resources need an independently verifiable cleanup policy after a crash. The selected optional persistent mode adds durable checkpoint ownership, policy fingerprints, exact-instance disposal and explicit recovery outcomes. A replacement host restores a validated checkpoint into a new physical generation after fencing the old owner; it must not adopt an orphan guest or confuse logical conversation identity with physical instance identity. MXC lifecycle APIs do not by themselves establish interpreter continuity, durable recovery or safe reuse.

## Validation and implementation sequence

1. Target rich Python through Hyperlight on both Windows WHP and Linux KVM, with code/text execution, input files, artifacts and optional persistent Python state recoverable after restart. Commit a checkpoint after each successful persistent call before acknowledging success. Permit recovery on another compatible machine only when host configuration requests it and the required storage, compatibility and ownership guarantees hold. Keep the fresh, text-only, closed-network experiment as the initial evidence gate; investigate allowlisting as a conditional addition.
2. Build a small experiment outside shipped package code against the pinned release. Verify runtime availability, imports, raw streams, structured failure status, deadline enforcement, memory/output bounds and descendant cleanup. Record exact binary and guest versions alongside outcomes.
3. For the selected profile, write the proposed declarations and refusals from those results. If a required property cannot be established, narrow the profile or keep it unimplemented; do not lower the workload's requirement.
4. Implement the adapter and focused regression tests, then exercise the real backend through the applicable shared conformance probes and backend-specific adversarial cases.
5. Add a CodeAct sample using closed networking and no file channels. Compare cold setup, first execution, repeated execution, memory and concurrency only after correctness is established; separate image preparation from execution timing.
6. Qualify bounded file input, artifacts and optional persistent Python state with restart recovery on both hosts, including host-configured recovery on a compatible replacement machine, before accepting the first usable version. Add allowlisted networking only where its public contract and enforcement are verified. Additional concrete MXC profiles remain deferred. Promote decided content into an owning backend guide and tracker when implementation is selected.

| Acceptance area | Required cases |
|---|---|
| Admission | Unsupported runtime/schema/host refuses; lower isolation cannot pass the default floor; undeclared operations and egress modes refuse |
| Python results | Known imports, printed output, stderr, arbitrary bytes, nonzero exit, exceptions, no expression echo, snapshot state semantics |
| Result integrity | Guest-crafted error/status text cannot become a trusted verdict or control message; native diagnostics remain untrusted |
| Deadlines | Queue expiry without execution, busy loop, sleeping code, setup delay, timeout/native-error distinction and cancellation |
| Resource bounds | Output flood including native buffering, host memory exhaustion, child processes and cleanup after native faults |
| Closed networking | Direct IPv4/IPv6, DNS, loopback, host services and inherited proxy routes; required probes cannot count as passing when skipped |
| Conditional allowlisting | Permitted requests succeed, forbidden requests fail, alternate connection paths cannot bypass policy, unsupported restrictions refuse and recovery reapplies current policy |
| Ownership | Concurrent calls, scope purge, stale generation disposal, owner death and absence of host credentials |
| Optional persistent mode | Python variables and dataframes survive successful calls; fresh mode stays fresh; conversations remain separate; cleanup preserves intended state; failures identify the recovery point; retention limits and purge hold |
| Restart recovery | Every acknowledged successful persistent call has a committed checkpoint; failed commits preserve the previous one and block uncommitted continuation; restore matching Python state and session files; corruption and incompatibility refuse; old owners are fenced; retries after lost acknowledgments do not replay committed work or duplicate artifact delivery |
| Host-configured remote recovery | Disabled by default; permitted only by trusted host configuration; restore on a tested compatible machine; reject mismatches and unavailable durable storage; concurrent owners, stale commits and old-guest execution cannot survive ownership transfer |
| First-version file plane | Exact bytes, parent swaps, links/reparse points, hard links, special files, oversized output, collection races and confined deletion on both hosts |

A proposed upstream contribution would be a Python/MAF reference integration and focused conformance cases, especially around runtime status, streams and network-policy semantics. This record does not publish an upstream issue or PR.

The maintainer subsequently selected extending MXC's Hyperlight API; the [owning design](../backends/mxc.md#selected-mxc-extension) records the responsibility boundary and proposed session operations. The lower-level helper remains a feasibility probe, not the production integration route.

## Design decisions still open

| Decision | Working recommendation | What changes the choice |
|---|---|---|
| Transport | Out-of-process native executor experiment; typed helper if needed | Byte/status separation, bounded buffering and reproducible deployment |
| Runtime distribution | Explicit operator-provisioned pinned artifacts during the experiment | Verified release contents, licenses, installation and upgrade experience |

Adapter work and live validation are tracked by [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648), beginning with feasibility spike [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649). This record does not establish implementation or live validation. Rich Python through Hyperlight is the selected priority; the remaining recommendations are not approvals or measured guarantees.

## Storage retention follow-up proposal

This section records a follow-up proposal on 2026-10-04 for [#1672](https://github.com/sokolaidev/maf-extensions/issues/1672). The accepted lifetime, retry and reservation policies live in the [owning design](../backends/mxc.md#session-lifetime). The implementation below remains proposed; it does not change the earlier feasibility assessment or establish runtime support.

### Findings in the existing experiment

At suite commit `b95508b3d579fbeb7e9504aec9d274623fedb08a`, [Store](../../../scripts/experiments/mxc_session_patch/host_store.py) owns one local database and retains every committed call, file inventory and compressed chunk. Its checkpoint and result limits bound individual publications, not cumulative storage. Independent counters in those databases would not provide atomic admission against a shared-store quota.

`Store.begin` treats an absent call ID as new execution. Collection therefore cannot delete call identity when it removes a result. `Store.restore` also requires the latest checkpoint's call to remain committed: result expiry must be represented separately from execution outcome. A checkpoint can remain current after its result expires, and an old result can remain deliverable after its checkpoint is superseded.

[The supervisor](../../../scripts/experiments/mxc_session_patch/host_call.py) materializes a restore directory and exports a candidate outside the database. Those copies, bounded diagnostics and interrupted work require ownership and cleanup accounting too. A sum of compressed database payload lengths would omit them. SQLite deletion also normally leaves reusable pages in the database; shrinking the file with `VACUUM` can require free space up to twice the original database size. Logical collection and physical reclamation need separate acceptance criteria. See [SQLite's VACUUM documentation](https://sqlite.org/lang_vacuum.html).

### Accounting decision pending

Recommend measuring both quotas in uncompressed retained checkpoint, result and artifact bytes, plus explicitly bounded metadata charges and outstanding reservations. Charge each retained object in full to its owning session; compression and content deduplication would save physical space without increasing admission capacity. Artifacts embedded in the serialized result would be counted there once; separately retained artifacts would have their own charge. Superseded checkpoints would remain charged until collection removes their references. Compact expired-call records would remain charged, so indefinitely retaining identity can eventually refuse new work even when old payloads have been collected.

This model would require separate physical-space safeguards for restored files, candidate export, compression overhead, SQLite pages and journals, and cleanup or compaction. Scratch byte/count limits and maximum concurrent reservations would bound expected temporary demand; a free-space check alone would not reserve disk against unrelated writers. Publication must still handle disk exhaustion without acknowledging success. The alternative is a strict physical allocation quota covering all these files, which requires filesystem-level enforcement or a separately qualified allocator. The logical model is a recommendation awaiting a maintainer decision, not an approved interpretation of the quotas.

### Proposed local transaction boundary

For the first storage experiment, prefer one local SQLite database for all admitted sessions in a configured store root. Session ownership would remain an operating-system lock per session, acquired before database transactions. Admission, reservation conversion and collection would transact in the same database; guest execution would run outside its write transaction. This avoids a separate quota ledger and session database disagreeing after a crash. Database writes would serialize, which needs measurement before promising throughput. Network filesystems, remote takeover and distributed fencing would remain the separate [#1671](https://github.com/sokolaidev/maf-extensions/issues/1671) qualification.

The proposed records would separate these responsibilities:

| Record | Purpose |
|---|---|
| Store configuration | Schema/accounting version and explicit aggregate quota |
| Session | Trusted identity, compatibility profile, owner generation, per-session quota and current checkpoint |
| Call identity | Session and call ID, request digest and durable execution outcome; retained after payload expiry |
| Delivery record | Result hash, retained artifact references, committed timestamp and fixed expiry |
| Checkpoint | Independently retained file inventory and chunk references |
| Reservation | Session, call, owner generation, maximum charges and cleanup state |
| Content/reference records | Verified bytes and explicit checkpoint/artifact reachability |

A new format should reject old databases explicitly until a migration is provided. Existing calls have no committed timestamps: opening a store must not fabricate an expiry or silently remove an earlier retry promise. Quota reductions below retained usage would block new admissions while preserving existing promises. Delivery would check identity and availability before attempting a new reservation, so a full store could still answer retries.

### Publication, restart and collection

| Boundary | Proposed durable behavior |
|---|---|
| Before execution | In one transaction, validate both available balances and persist call intent plus the complete reservation; refusal starts no guest |
| During execution/capture | Keep reservation and prior committed state; enforce the candidate/result bounds rather than relying only on the eventual commit check |
| Publication | Atomically publish checkpoint, result/artifact references and fixed expiry, advance the current checkpoint, and convert reservation into actual retained charges |
| Publication failure | Keep the previous recovery point; retain the failed call identity and cleanup charge until private data is reclaimed |
| Commit followed by lost acknowledgment | Return the saved delivery record on retry; create neither a new guest nor a new execution reservation |
| Owner death | Acquire exclusive ownership and a new generation, establish old execution termination, reconcile publication and private scratch, then release only proven-unused capacity |
| Expired delivery | Return `result_expired`; remove eligible payload references without changing the committed execution outcome or current checkpoint |
| Collection interruption | Recover a committed reference/accounting transaction or roll it back; retry physical cleanup idempotently with its charge retained |

A reservation age or missing PID alone would not authorize reclamation. Cleanup would use host-owned directory identities and confined no-follow access, and refuse unknown or corrupt inventory. A committed call could still have residual scratch after acknowledgment; that cleanup would retain its own charge without making the call eligible for execution again.

Collection would retain roots for the current checkpoint, every unexpired delivery artifact, active reservations and any active restore or delivery reader. It would validate the reference graph before deleting an unreachable inventory or chunk, and update references and charged usage atomically. Incremental batches would bound transaction work; unfinished batches would retain their remaining charge. A corrupt live root would refuse destructive collection rather than being treated as absent. Freshly verified checkpoint bytes must still replace differing stored bytes under the same hash inside publication, preserving the experiment's existing corruption-repair and rollback behavior.

Persisted expiry requires an explicit clock contract across restarts. The implementation plan must cover clock rollback and forward jumps before enabling destructive expiry; elapsed process time alone cannot establish time spent offline. Session deletion/idle expiry must also preserve promised deliveries and prevent an old session identity from being recreated as fresh while retries remain valid. These are remaining design questions, not silently selected policies.

### Implementation and qualification sequence

1. Settle quota accounting, then the local transaction boundary and expiry clock contract one decision at a time. Specify bounded metadata charges, scratch admission and session retirement before coding the storage format.
2. Extend the experimental store and focused tests with the new schema, atomic dual-quota admission and retry states. Keep production adapter and cross-machine claims out of this increment.
3. Add publication conversion, restart reconciliation and reference-based collection with process-death injection. Wire bounded private scratch and cleanup into the supervisor.
4. Exercise the integrated experiment on GitHub runners for Windows/WHP and Linux/KVM where the required hypervisors are available, recording the exact candidate and any unavailable platform. Retain reports and hashes rather than large checkpoint trees.

| Test area | Required evidence |
|---|---|
| Admission | Exact quota boundary; either limit exceeded; competing processes from different sessions; maximum reservation before guest launch; replay succeeds at full quota |
| Delivery | Fixed expiry across retry/restart/config changes; matching expired ID refuses execution; changed request digest refuses; retained artifacts remain byte-identical |
| Checkpoint independence | Latest checkpoint restores after its result expires; older result replays after newer checkpoint publication |
| Reservation recovery | Process death before/during/after publication and cleanup; stale generation refuses; unknown scratch does not free capacity; failed cleanup remains charged |
| Collection | Shared chunks and active readers remain pinned; corrupt root refuses deletion; interrupted batches recover; replacement of corrupt historical chunks remains transactional |
| Storage bounds | Incompressible data, metadata/tombstone growth, scratch duplication, journal overhead, filesystem exhaustion and physical reclamation measured separately from logical usage |
| Compatibility | Old format refuses without mutation; no invented historic expiry; unsupported remote store refuses rather than starting fresh |

## Bounded file-plane design follow-up (#1670)

On 2026-10-08, following the merge of [#1786](https://github.com/sokolaidev/maf-extensions/pull/1786), implementation planning started for [#1670](https://github.com/sokolaidev/maf-extensions/issues/1670). The baseline is suite commit `894d4b07d503cc1a99f163ef42c1cdada67281e8`, MXC `7bf210247986cb73b1b314df60c2f1109c479c0b` and hyperlight-unikraft `8f636e00cdf29e6c33ba0f578482c595d7827cf6`. This section records source inspection and proposed acceptance criteria; no file-plane native qualification or production capability is established.

### Mechanisms and constraints

The qualified session overlay restores without mounts. Guest-private files, including the existing open-file state probe, travel in VM memory. The pinned runtime separately provides [host-directory mounts](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/8f636e00cdf29e6c33ba0f578482c595d7827cf6/src/hostfs.rs) backed by `cap_std::fs::Dir`, with read-only flags and chunked operations. That confines host path access but does not itself reserve aggregate file bytes or count, include mounted contents in the VM checkpoint, or retain immutable artifact deliveries. The write callback accepts an offset and bytes; the truncate callback calls `set_len` without an aggregate storage allowance. Read chunking is not a write quota.

The hostfs protocol reopens paths on each read/write chunk. Its own source identifies handle-based operations as future work. Guest vnode open and close are no-ops in the pinned kernel's `lib/hostfs/hostfs_vnops.c`. Consequently, a mounted-workspace design must qualify retained open descriptors, rename/unlink/recreate and restore behavior explicitly; the existing guest-private open-file probe does not establish those properties for a mount.

The runtime's [AppSandbox boundary contract](https://github.com/hyperlight-dev/hyperlight-unikraft/blob/8f636e00cdf29e6c33ba0f578482c595d7827cf6/src/lib.rs) says the vCPU is halted and guest threads are blocked between entries. This gives a potential consistency boundary for host-managed workspace capture. Entering the VM again to run an artifact-reading script can schedule other guest work, so serialization of host calls alone does not prove a stable artifact view. Guest-produced filenames and bytes remain untrusted; guest text cannot authorize publication.

### Proposed implementation boundary

Keep the new experiment separate from the qualified streams profile and place any native extension behind its existing backend wrapper. Evaluate a bounded host-managed workspace with limits enforced before every growth operation, an immutable exported workspace inventory and a checkpoint that binds that inventory. A memory-backed file service could avoid exposing host directories and make write bounds explicit, but it would require a removable runtime extension and native filesystem-semantic tests. An ordinary writable mount without that accounting and recovery work is insufficient.

Reserve input staging, the live workspace, the exported candidate and retained delivery artifacts before guest execution. Bind the complete request identity to code, input names and bytes, selected output names and relevant policy. A retry with changed inputs or artifact selection must refuse, while a matching committed retry must deliver identical bytes without starting the helper. Publish the result, artifacts and corresponding recoverable file state atomically using the existing format-4 ownership and cleanup rules; preserve the previous checkpoint on any capture or publication failure.

The maintainer selected availability within a requested lifecycle. The owning design now records host-selected input lifetime, independently of whether Python state persists. The maintainer selected call and session lifecycles for the first version, aligning with the existing suite model. Explicit host release and time-based input expiry are deferred. A session-scoped input survives restart, while a call-scoped input is reclaimed before durable success. The maintainer selected call lifetime when the host omits a lifecycle; session retention requires an explicit host choice. The maintainer also selected refusal for an upload targeting an existing session filename unless the host explicitly requests replacement. The replacement choice must be included in request identity so a retry cannot change overwrite authority. The maintainer selected writable uploaded inputs by default within their requested lifecycle; this does not grant host uploads implicit replacement authority. These are selected requirements, not implemented or qualified behavior. Guest code can copy input bytes into Python objects or other guest state, so deleting the original file does not erase those copies; the sandbox lifetime remains the boundary for that state. Result artifacts retain their separate delivery promise.

### Host request contract

The initial [request module](../../../scripts/experiments/mxc_files_patch/request.py) validates immutable input byte snapshots, relative portable names, count and byte allowances, and host-selected call/session lifetime and replacement permission. It rejects duplicate, case-colliding and file/parent-colliding names. The canonical retry identity includes code and input hashes, names, lifecycles, replacement authority, selected artifacts and transfer limits. Offline tests exercise exact and exceeded bounds and changed-request refusal after reopening the existing SQLite store.

This module does not yet transfer files, enforce guest writes, apply replacement, collect artifacts or publish a file-aware checkpoint. Its transfer ceilings do not reserve runtime memory or extend the existing result-envelope capacity. Native workspace bounds and artifact storage admission remain implementation work. The maintainer selected host-configurable defaults of 64 files and 16 MiB total in each transfer direction. The request contract uses a 16 MiB per-file default as well, so one file may consume the entire directional byte allowance. The maintainer selected host-configurable live workspace defaults of 64 MiB and 256 files, covering retained inputs and guest-created files. Workspace limits also enter retry identity. The maintainer selected all-or-nothing artifact delivery: any missing, unsafe or oversized requested artifact fails the entire call, prevents successful result publication and preserves the prior committed checkpoint. Retained earlier deliveries keep their existing expiry promises. Qualification must prove that the failed call's Python and file changes cannot become the next committed state.

The host-side [inventory planner](../../../scripts/experiments/mxc_files_patch/inventory.py) applies upload replacement authority to an immutable file inventory and refuses aggregate workspace overages or path collisions before returning a new inventory. It does not expose a guest filesystem or establish native write enforcement. The maintainer selected a catchable filesystem capacity error without changing the affected file. The reference inventory model checks complete write/append/resize growth before allocation; an over-budget operation leaves the original inventory intact and later operations can proceed. Its completion planner checks all requested artifacts against one immutable inventory before selecting session-lifetime files for retention. These offline operations do not yet prove native enforcement or transactional checkpoint integration.

The pinned kernel's `hostfs_write` divides a guest write into chunks and calls `fs_write_bytes` once per chunk; the host callback immediately writes each received chunk. A quota check added only to that callback could accept early chunks and reject a later one after modifying the file. The selected refusal semantics therefore require a whole-operation admission/staging protocol or equivalent enforcement before the first mutation. Native qualification must include a write crossing multiple transfer chunks, failure near the final chunk, and continued execution after the error. Earlier completed writes and a separate successful truncate remain separate operations; a failed later write does not roll back the entire guest program unless the call itself fails.

### Qualification gates

| Area | Required evidence on Linux/KVM and Windows/WHP |
|---|---|
| Inputs | Empty and arbitrary bytes, nested names, exact and exceeded count/byte limits, changed-request retry refusal, no guest launch after admission refusal |
| Confinement | Absolute paths, traversal, alternate separators, duplicate or colliding names, parent swaps, symlinks, hard links, special files and host reparse points where applicable |
| Workspace | Growth through write, append, truncate and sparse offsets; file/directory count; rename/unlink/recreate; retained descriptors; restart with consistent names and contents |
| Artifact capture | Host-selected bounded names; missing/oversized outputs; mutation during capture; background writers; exact byte equality with the committed file state |
| Delivery | Lost acknowledgment, matching retry after later calls and checkpoint collection, expiry and forgiveness, quota pressure without premature artifact eviction |
| Failure | Helper death, cancellation, timeout, capture/publication failure and all existing publication/cleanup crash boundaries with files included |
| Workloads | Text analysis and CSV-to-matplotlib output with exact input verification and a validated returned image |

Until these gates pass, the experimental file plane remains unqualified and the production adapter must not advertise FILES_IN or FILES_OUT on its evidence.
