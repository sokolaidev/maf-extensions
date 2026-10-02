# MXC integration research and initial backend design

> A research record and initial proposal, recorded on 2026-10-02: what MXC v0.9.0 adds to the suite, what the suite can offer MXC users, and how an experimental adapter could fit the existing protocol. The maintainer selected rich Python through MXC Hyperlight, with code/text execution, file inputs and output artifacts, on both Windows and Linux for the first usable version. Optional persistent Python state must be recoverable after host restart, with a durable checkpoint after every successful tool call. Host configuration may also permit recovery on another compatible machine. Allowlisted networking is desired where enforceable; closed networking remains the baseline. Transport and runtime distribution remain implementation decisions to validate. No MXC adapter is implemented or qualified by this record.
> The agreed scope and recovery decisions now live in [MXC Hyperlight design decisions](../backends/mxc.md). This record retains the original assessment and design reasoning; the main document owns the decisions and their implementation status.

MXC would extend the suite's execution environments. The proposed integration places it beneath `SandboxRouter`, with a separately qualified profile for each concrete MXC backend. Workload kinds, admission, result labels and application ownership remain in the suite. A common configuration schema does not make all MXC backends interchangeable.

## Evidence and scope

The original assessment was made on 2026-09-29 and 2026-09-30. This record checks the design against MXC tag `v0.9.0`, resolved on 2026-10-02 to commit `86fb3d2abaf9c431556692037bff881830b543a5`. It compares the suite at commit `240e62f91be648a0214ea8cdc08f67f96045c620`; unrelated working-tree edits are not evidence of delivered behavior. Links to current suite guides explain the contracts, while those commit identifiers identify the snapshots assessed here.

The evidence consists of release notes, tagged backend guides, selected MXC source and contract tests, and the suite's protocol, CodeAct runtime contract and backend implementations. No MXC binary was installed or executed. No performance, cost, cross-platform compatibility, isolation or production-readiness result was measured.

The [v0.9.0 release](https://github.com/microsoft/mxc/releases/tag/v0.9.0) calls `0.9.0-alpha` the stable policy schema and includes WSLC and IsolationSession lifecycle APIs without experimental opt-in. Hyperlight, NanVix and Windows Sandbox use the development `0.10.0-alpha` contract and remain experimental. The release is marked as a prerelease. The [tagged README](https://github.com/microsoft/mxc/blob/86fb3d2abaf9c431556692037bff881830b543a5/README.md) warns that some generated policies are overly permissive and that MXC profiles should not currently be treated as security boundaries. Stable schema support is not a security qualification, and a wrapper cannot create missing enforcement.

The earlier [two-axis policy record](two-axis-sandbox-policy.md) mentions MXC as a backend family. It does not assess this release or design an adapter. This record owns that narrower investigation. The existing [Hyperlight record](hyperlight-backend.md) and [Hyperlight guide](../backends/hyperlight.md) describe the suite's separate direct integration.

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

## Design decisions still open

| Decision | Working recommendation | What changes the choice |
|---|---|---|
| Transport | Out-of-process native executor experiment; typed helper if needed | Byte/status separation, bounded buffering and reproducible deployment |
| Runtime distribution | Explicit operator-provisioned pinned artifacts during the experiment | Verified release contents, licenses, installation and upgrade experience |

Adapter work and live validation are tracked by [#1648](https://github.com/sokolaidev/maf-extensions/issues/1648), beginning with feasibility spike [#1649](https://github.com/sokolaidev/maf-extensions/issues/1649). This record does not establish implementation or live validation. Rich Python through Hyperlight is the selected priority; the remaining recommendations are not approvals or measured guarantees.
