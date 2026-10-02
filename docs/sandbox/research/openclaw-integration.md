# OpenClaw integration research and first-delivery proposal

> Research and initial design recorded on 2026-10-02, followed by an adversarial review that narrows the first deliverable and identifies four design prerequisites. The proposed first deliverable is a closed-network Bicep validator exposed as an OpenClaw tool. No OpenClaw adapter, MCP service or runtime qualification is delivered by this record.

The suite can add focused workload tools, enforced guest networking and reusable conformance scenarios to OpenClaw. Begin with a bounded validation operation through MCP or a thin tool plugin. A complete sandbox backend is a separate feasibility project because its interactive process and workspace contracts exceed the suite's current common execution interface.

## Evidence and scope

The investigation inspected [OpenClaw PR #97086](https://github.com/openclaw/openclaw/pull/97086), its review discussion and merged tree `008f04a65650f364ddbe40f1697f3c1b020bba97`; OpenClaw main at `df93a28f0bc58c41023326a734e3838bb356eea4`; Microsoft MXC tag `v0.9.0`; and suite source at `8161cfdc761557800d18d918a79f2731deb1a488`. The documentation branch starts at `63c0e97dbf39a0d976b07ffb9437317170f1a26d`; the intervening suite change reports credential-expired proxy streams as interrupted. Live OpenClaw documentation was read on 2026-10-02 and may evolve independently of those source snapshots.

The evidence is source, documentation and published author reports. No OpenClaw or MXC runtime was installed or executed during this investigation. No performance, cost, containment, cross-platform compatibility or production-readiness result was measured. Existing suite tests and upstream reports identify available evidence; they do not qualify the proposed integration.

Three independent reviewers subsequently challenged the proposal at suite commit `386e711bbaf9e0456254db89d71b30aad46d3d56`. Their findings were checked against source, and a controlled asynchronous fake-execution probe confirmed the Bicep cancellation behavior described below. That probe executed the Python wrapper only; it did not launch Docker, Bicep or OpenClaw. The review found four P2 design gaps and no demonstrated P1 vulnerability. The source links in the review section identify the assessed snapshot rather than making claims about future versions.

This record owns the OpenClaw host integration and delivery sequence. It does not design a new MXC backend beneath the suite router. The existing [policy architecture](two-axis-sandbox-policy.md), [Hyperlight research](hyperlight-backend.md) and [backend authoring guide](../backends/writing-a-backend.md) own those different concerns.

## What the MXC integration delivers

PR #97086 merged on 2026-07-13. Historical remaining-gate paragraphs in its body are not current merge blockers. It adds a separately installable Windows ProcessContainer plugin, with readiness checks, policy construction, command launching, a host-side filesystem bridge, packaging and tests. Each command gets an ephemeral container; session or agent scope controls workspace reuse rather than a continuously running container.

The merged plugin pinned MXC SDK `0.7.0` and emitted schema `0.7.0-alpha`. The inspected [current package](https://github.com/openclaw/openclaw/blob/df93a28f0bc58c41023326a734e3838bb356eea4/extensions/mxc/package.json) pins SDK `0.8.0`; the policy builder still emits `0.7.0-alpha`. MXC [v0.9.0](https://github.com/microsoft/mxc/releases/tag/v0.9.0) is a newer prerelease. SDK support for another engine or policy does not make it a capability of OpenClaw's ProcessContainer adapter.

The author reports 109 native Windows extension tests, packed installation, runtime loading and actual ProcessContainer checks for workspace modes and refusal cases. Linux reports distinguish passing tests from Windows-only skips. These are upstream-reported results, not runs reproduced here. See the [repair and evidence report](https://github.com/openclaw/openclaw/pull/97086#issuecomment-4939634739).

## Lessons for adapters

| Constraint | Evidence from the integration | Consequence for this suite |
|---|---|---|
| Preserve each workspace mode | Review found that `none` and `ro` had not correctly distinguished the isolated workspace from the actual agent workspace | Test the meaning of every translated mode; similar option names do not establish equivalence |
| Protect every file route | Commands execute under ProcessContainer, while bridge reads and mutations execute on the host | Apply confinement, no-follow access and race handling independently to the host file plane |
| Refuse unrepresentable restrictions | A writable parent grant cannot safely preserve a nested read-only protected skill root in this configuration | Reject overlapping grants instead of approximating the requested policy |
| Fail closed on explicit configuration | Missing configured policy files or paths previously risked falling back to defaults | Distinguish an omitted optional setting from an explicitly requested setting that cannot be honored |
| Prove the installed package | Checkout execution concealed missing native SDK dependencies in distribution | Install the packed adapter and worker into a clean environment and execute through that installed surface |
| Separate resource lifetimes | Stable runtime metadata coexists with per-command containers | Keep workspace retention, execution ownership, cancellation and disposal separate |
| Keep discovery inert | Non-full registration modes must return before probes and service registration | Import and discovery must not provision a backend or launch a worker |

The primary references are the [workspace and filesystem review](https://github.com/openclaw/openclaw/pull/97086#issuecomment-4931066422), [merged file bridge](https://github.com/openclaw/openclaw/blob/008f04a65650f364ddbe40f1697f3c1b020bba97/extensions/mxc/src/fs-bridge.ts), [policy builder](https://github.com/openclaw/openclaw/blob/008f04a65650f364ddbe40f1697f3c1b020bba97/extensions/mxc/src/mxc-container-config.ts), [packaging review](https://github.com/openclaw/openclaw/pull/97086#issuecomment-4813463771), [registration correction](https://github.com/openclaw/openclaw/pull/97086#issuecomment-4917736996) and [execution implementation](https://github.com/openclaw/openclaw/blob/008f04a65650f364ddbe40f1697f3c1b020bba97/extensions/mxc/src/mxc-backend.ts).

Environment handling also belongs to the contract. The Windows adapter constructs separate launcher and workload environments, handles case-insensitive names and assigns per-command temporary directories. A replacement environment must preserve the OS prerequisites needed to execute without importing application credentials. See [environment construction](https://github.com/openclaw/openclaw/blob/008f04a65650f364ddbe40f1697f3c1b020bba97/extensions/mxc/src/windows-env.ts).

### Configuration on process argv

OpenClaw's temporary launcher payload file does not remove exposure later in the chain: MXC's config-based executor path encodes the request onto process argv. [MXC #626](https://github.com/microsoft/mxc/issues/626#issuecomment-5027041631) was closed because upstream accepted the same-user observation boundary and declined additional transport complexity, not because that path was replaced. The tagged [v0.9.0 helper](https://github.com/microsoft/mxc/blob/v0.9.0/sdk/node/src/helper.ts) still constructs `--config-base64`, and [spawnSandboxFromConfig](https://github.com/microsoft/mxc/blob/v0.9.0/sdk/node/src/sandbox.ts) uses the executor path. The separate native API has different behavior; switching APIs is an adaptation requiring qualification. Credential claims must cover the whole launch chain.

## Existing OpenClaw capabilities and useful additions

OpenClaw already supports Docker/Podman, SSH and OpenShell execution, tool policy and approvals, credential management, and Code Mode with Node and QuickJS executors. A generic execution wrapper or a second approval system would duplicate existing responsibilities. See [sandbox configuration](https://docs.openclaw.ai/gateway/config-agents/sandbox), [OpenShell](https://docs.openclaw.ai/gateway/openshell), [policy](https://docs.openclaw.ai/start/why-openclaw/policy-as-code) and [Code Mode](https://docs.openclaw.ai/tools/code-mode/executors).

| Opportunity | Added value | Constraint and priority |
|---|---|---|
| Bicep validation | Fixed compiler operations, explicit completion/verdict semantics, selected prepared dependencies and closed runtime networking | First delivery; define and qualify one transport and backend |
| Terraform/OpenTofu, draw.io and optional CodeAct tools | Reuse workload-specific validation and explicit artifact delivery | Follow Bicep; retain each kind's constraints and separate proposed file edits from saving them |
| Enforced egress and credential mediation | Apply destination/method/path restrictions around sandboxed work while retaining upstream credentials outside the guest | Separate design; preserve OpenClaw authority and prove a concrete backend configuration |
| Conformance scenarios | Reusable adversarial probes for files, lifecycle, policy, authority and distribution | Early contribution path; a passing subset is not a complete security audit |
| Full sandbox provider | Expose suitable suite backends through OpenClaw's execution/workspace model | Later feasibility decision; interactive process contracts and SDK support are unresolved |

OpenClaw's [secret egress proxy](https://docs.openclaw.ai/gateway/secrets/secret-store-and-egress) already provides destination-bound secrets and per-process grants. Its documented automatic integration covers Gateway-hosted execution, not sandbox or remote-node execution, and cooperative proxy settings can be bypassed by direct sockets. The suite's configured Docker and WSLC topology removes the guest's direct external route and supplies host/method/path rules plus optional gateway credentials. This is a specific integration opportunity, not a superiority claim over every OpenClaw backend: OpenShell also has policy enforcement. The suite's [network contract](../network.md) retains backend-specific DNS, TLS, observation and authority limits. GET-only or path-restricted access is not a confidentiality guarantee.

## Proposed first deliverable: closed-network Bicep validation

The revised proposal exposes one fixed validation operation to OpenClaw, with a Python service calling the existing Bicep kind through the router. Prefer local stdio MCP with uniform policy for one trusted local operator. OpenClaw already supports [MCP servers](https://docs.openclaw.ai/tools/mcp), but the suite does not yet provide this service. Prove one end-to-end operation before committing to a broader framework-neutral API or additional tools. Bicep is a bounded integration experiment; demand from OpenClaw users has not been established.

The inspected general MCP [materialization path](https://github.com/openclaw/openclaw/blob/df93a28f0bc58c41023326a734e3838bb356eea4/src/agents/agent-bundle-mcp-materialize.ts) forwards cancellation to the [runtime](https://github.com/openclaw/openclaw/blob/df93a28f0bc58c41023326a734e3838bb356eea4/src/agents/agent-bundle-mcp-runtime.ts), whose tool call sends the tool name and arguments without trusted agent/session identity. The first service can generate request-local ownership internally and apply the same policy to every admitted caller. Per-agent authorization and cross-call artifact access remain outside its contract. A tool plugin is an alternative only if a later requirement needs a trusted host integration that MCP does not supply. The proposed operation follows OpenClaw's MCP tool authorization; it does not inherit command, working-directory or file-bound host-exec approvals.

```text
OpenClaw MCP tool authorization
  -> local stdio MCP service with fixed policy
  -> immutable request snapshot and service-generated ownership
  -> SandboxRouter admission
  -> one Docker container per call and prepared Bicep image
  -> bounded completion/verdict and diagnostics for the submitted snapshot
```

Start with Docker, an explicitly admitted `Isolation.CONTAINER` policy and `min_isolation_scope=IsolationScope.CALL`, so every request receives its own physical sandbox. Preserve the suite's default isolation floor; do not silently lower it. A host requiring microVM isolation needs a separately qualified compatible backend before this workload is admitted. No new MXC backend is required for the Bicep prototype.

Use the existing [prepared AVM profile](../../../images/bicep-sandbox/README.md#prepared-avm-profile), an immutable deployed image reference, the host's selected Bicep configuration with the prepared cache location, and `Egress.CLOSED`. The current profile contains a small selected set of pinned modules, not the full AVM catalog. Runtime validation must retain `--no-restore`; an unprepared module must produce an incomplete result without a verdict. Build-time downloads and compiler/image provenance remain separate from runtime network closure.

### Initial contract

| Surface | Proposed boundary |
|---|---|
| Operation | One fixed Bicep validation operation; no shell command argument, deployment or arbitrary compiler flags |
| Inputs | Bounded relative names plus inline UTF-8 contents for `.bicep` and `.bicepparam`; validate the entire supplied set, with no separate entry point or auxiliary JSON/text assets |
| Host policy | Host selects image, compiler configuration, backend, isolation floor, timeouts, transfer budgets and closed networking; model arguments cannot widen them |
| Ownership | Uniform authorization for one trusted local operator; service-generated request identities and call-scoped containers; no model-supplied owner, agent or session authority |
| Results | Diagnostics-only structured response; preserve `completed` separately from verdict, bound compiler text and identify the submitted snapshot, compiler policy and image |
| Files | Copy supplied bytes into an immutable request snapshot in an adapter-owned root; no automatic workspace read, host/guest path translation, repository mount or downloadable artifact |
| Lifecycle | Specify cancellation requested, guest stopped and cleanup completed separately; bound the overall request and recovery; reconcile abandoned owned resources before admitting work after restart |
| Distribution | Explicit Python/runtime/image prerequisites; packed installation and installed execution must be tested |

Plain serialized labels do not reproduce MAF information-flow enforcement in OpenClaw. Result provenance can be preserved as metadata, but any use as authority requires host enforcement. Similarly, sandboxing this workload does not isolate the OpenClaw Gateway or other plugins. OpenClaw's [capability matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix) describes that boundary.

OpenClaw's inspected [MCP metadata validator](https://github.com/openclaw/openclaw/blob/df93a28f0bc58c41023326a734e3838bb356eea4/src/agents/mcp-tool-metadata.ts) supports `outputSchema` and `structuredContent`; its [result projection](https://github.com/openclaw/openclaw/blob/df93a28f0bc58c41023326a734e3838bb356eea4/src/agents/mcp-content.ts) preserves structured results for Code Mode and renders them for ordinary model consumption. Use a shallow fixed schema rather than invent a result transport. The schema should carry a canonical source-set digest and compiler-policy/image identity: the verdict describes those submitted bytes under that profile, not the repository's current state. Canonicalization and the mapping from existing framework results still need design and tests.

### Adversarial review findings

The review supports the narrowed experiment but does not establish an implementation-ready design. The four findings below concern reuse assumptions and missing lifecycle requirements, not exploitable defects demonstrated in an existing OpenClaw adapter.

| Finding | Existing behavior and failure scenario | Required design correction |
|---|---|---|
| Cancellation is not confirmed termination | Bicep `_run_phase` shields the active execution and drains it after cancellation; Docker cancellation of the host CLI alone leaves the guest command running until disposal | Define acknowledgment, execution stop and cleanup completion separately; choose bounded draining or supervised exact-instance disposal and prove its interaction with router cleanup |
| Worker death bypasses cleanup | Docker containers run a persistent `sleep infinity`; worker death bypasses Python `finally`, and `reap` installs no background timer | Assign a recovery owner, dedicated adapter scope/generation and startup reconciliation; define maximum resource lifetime without sweeping unrelated owners or unexpired live work |
| Disposal is not concurrent call isolation | Bicep's spec does not request call scope, and the router defaults to conversation scope; distinct call directories can still share one container | Require call scope with internally generated identities; test simultaneous requests and cleanup ownership rather than inferring separation from disposal policy |
| Entry-point semantics exceed the existing tool | The Bicep tool accepts only `.bicep`/`.bicepparam` and builds and lints every supplied file; supporting JSON is rejected | Adopt whole-set validation initially; separating compilation targets from staged support assets needs a distinct workload API design |

These findings follow from the reviewed [Bicep tool](https://github.com/sokolaidev/maf-extensions/blob/386e711bbaf9e0456254db89d71b30aad46d3d56/packages/maf-sandbox-bicep/src/maf_sandbox_bicep/_tool.py), [Docker execution and reaper](https://github.com/sokolaidev/maf-extensions/blob/386e711bbaf9e0456254db89d71b30aad46d3d56/packages/maf-sandbox-docker/src/maf_sandbox_docker/_backend.py), [router defaults](https://github.com/sokolaidev/maf-extensions/blob/386e711bbaf9e0456254db89d71b30aad46d3d56/packages/maf-sandbox/src/maf_sandbox/_router.py), [call identity and cleanup wrapper](https://github.com/sokolaidev/maf-extensions/blob/386e711bbaf9e0456254db89d71b30aad46d3d56/packages/maf-sandbox/src/maf_sandbox/maf.py) and [isolation-scope contract](https://github.com/sokolaidev/maf-extensions/blob/386e711bbaf9e0456254db89d71b30aad46d3d56/packages/maf-sandbox/src/maf_sandbox/_protocol.py).

The controlled cancellation probe supplied `_run_phase` with a fake `exec` that signaled entry and waited on an event. After cancelling the phase and yielding to the event loop, the phase remained pending and `exec` had received no cancellation. Releasing the fake execution let the phase finish with `CancelledError`. This confirms the wrapper's drain behavior only; guest termination and transport settlement were not measured. In the reviewed code, the default 120-second execution timeout is spent independently for each build/lint phase, so it is not an end-to-end request deadline. The service must also define time reserved for termination and cleanup, and where cleanup failure remains observable after the client has cancelled or disconnected.

Safe staged filenames do not confine paths embedded in compiler source: Bicep supports compile-time file-loading functions. Call scope prevents sibling requests from sharing a filesystem; it does not replace control of the image's readable contents. No sibling-data exfiltration was demonstrated. Similarly, setting a host timeout does not prove recovery after process death. A scoped age-based reaper is an operator maximum-lifetime policy and can remove running work; it must not be treated as an idle-resource detector.

The review did not count already-disclosed choices such as numeric resource limits, framework packaging, result-label enforcement or SDK support as new defects. Resolve them in the first design rather than treating their presence in an issue as implementation evidence. Docker CPU/memory defaults are not finite workload budgets; the host profile must select and verify the actual limits.

### Design decisions and proof required

The first design must resolve the public operation schema, source-set canonicalization, concrete input/output and resource limits, error mapping, total deadline, cancellation acknowledgment, recovery ownership and supported OpenClaw version. Compare direct use of existing framework tools with the smallest supported typed-result interface; do not make a general framework-neutral redesign a prerequisite without showing why the existing surface cannot serve the prototype. The protocol/router modules use the standard library, but the [installed core distribution](../../../packages/maf-sandbox/pyproject.toml) still depends on `agent-framework-core`; the packaged kinds expose framework tools. Do not describe the current distribution as dependency-free or directly embeddable in TypeScript.

Before implementation is called complete, demonstrate valid and invalid Bicep, a missing prepared dependency with no verdict, malformed/incomplete diagnostics, oversized input/output, rejected auxiliary assets, path escapes and file replacement, concurrent call isolation, timeout/cancellation, cleanup failure, and clean installed-package execution. Match the result's digest to the submitted bytes and prove that later workspace edits do not change what the verdict claims. Verify actual guest networking is closed and compiler policy is applied. A denied external request alone does not prove an allowlist, but this initial workload deliberately requests no outbound destinations. Keep model behavior, service control fields, runtime evidence and CI results distinguishable.

Cancellation tests must interrupt an active compiler phase and measure guest stop and exact-container removal, not just host-future cancellation. Crash tests must kill the worker or Gateway during acquisition, staging, execution and disposal, then verify that recovery removes owned containers and staging roots without deleting another service's resources. Supervised prototype use may precede that recovery qualification; unattended use must wait. A real OpenClaw call through the installed service must establish successful, invalid, incomplete and cancellation outcomes before expanding the workstream.

No automatic workspace access, artifact download, persistent result store, formatting, Terraform plan/apply/destroy, general CodeAct, network credentials, guest host-tools, interactive shell, warm reuse, remote multi-tenant service or complete OpenClaw backend is included in this first delivery. Subsequent designs must establish their own authority and lifecycle boundaries. The five design workstreams are investigation options, not a commitment to implement all of them.

## Subsequent designs

For additional workload tools, assess [Terraform](../kinds/terraform.md), [draw.io](../kinds/drawio.md) and [CodeAct](../kinds/codeact.md) independently. Formatting returns proposed content for a separate authorized save. Diagram validation is not sanitization of every embedded reference. General code execution requires a larger authority and artifact contract than a fixed validator. Avoid duplicating OpenClaw Code Mode without a concrete confined-workload benefit.

The egress design should map OpenClaw-approved authority to finite grants for a trusted caller, exact destination and live sandbox generation. Specify secret ownership, grant/revocation behavior, cancellation, expiry during streaming, and cleanup. Prove a permitted request succeeds while wrong destinations, methods/paths, direct-route attempts and stale grants fail. Qualify DNS and alternate protocols explicitly rather than inferring them from HTTP proxy tests. Reuse the existing [host credential contract](../hosts.md) without inventing a second source of user authorization.

The conformance design should choose cases applicable to both projects: workspace modes, protected-root overlaps, host-side file escapes and races, bounded results, process descendants, cancellation, stale runtime generations, policy drift and installed-package loading. A qualified MXC 0.9 upgrade is an optional upstream experiment, not a dependency of the Bicep prototype. Native runtime evidence belongs to each exact backend/version combination. Creating issues here does not authorize publishing a third-party PR.

The full-backend design must map OpenClaw execution specifications, filesystem bridge, manager, workspace retention and lifecycle generations to the suite. The current common interface returns final execution results and does not provide a complete PTY/stdin/background-process/streaming-session contract. The [Deep Agents adapter](../../../packages/maf-sandbox-deepagents/README.md) is a useful precedent with explicit limitations. Native Hyperlight guest-to-host callbacks remain tracked separately by [#369](https://github.com/sokolaidev/maf-extensions/issues/369).

Current OpenClaw [SDK documentation](https://docs.openclaw.ai/plugins/sdk-subpaths) marks `plugin-sdk/sandbox` private-local, although [host hooks](https://docs.openclaw.ai/plugins/sdk-overview/host-hooks#sandbox-backends) describe registration. Verify the installed public interface and obtain a supported extension direction before committing to a third-party backend. July's official-plugin imports are not evidence of a stable current third-party contract.

## Status

All integration opportunities remain proposed; this record delivers research and initial design only. The Bicep design is the first actionable workstream. Closing a design issue requires decisions and implementation follow-ups, not a claim that the runtime has shipped.

| Decision | State | Tracking |
|---|---|---|
| OpenClaw integration direction and delivery sequence | Open; research recorded | [#1637](https://github.com/sokolaidev/maf-extensions/issues/1637) (open) |
| First closed-network Bicep validation tool | Open; narrowed proposal, four review prerequisites and runtime qualification remain | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| Additional workload tools and artifact delivery | Open; design follows the Bicep contract | [#1639](https://github.com/sokolaidev/maf-extensions/issues/1639) (open) |
| Enforced egress and credential mediation | Open; backend-specific design required | [#1640](https://github.com/sokolaidev/maf-extensions/issues/1640) (open) |
| Conformance and installed-package qualification | Open; scenario and harness design required | [#1641](https://github.com/sokolaidev/maf-extensions/issues/1641) (open) |
| Full sandbox provider | Open; feasibility and supported SDK decision required | [#1642](https://github.com/sokolaidev/maf-extensions/issues/1642) (open) |
