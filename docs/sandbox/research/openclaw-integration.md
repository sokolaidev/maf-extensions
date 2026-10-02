# OpenClaw integration research and first-delivery proposal

> Research and initial design recorded on 2026-10-02. The proposed first deliverable is a closed-network Bicep validator exposed as an OpenClaw tool. No OpenClaw adapter, MCP service or runtime qualification is delivered by this record.

The suite can add focused workload tools, enforced guest networking and reusable conformance scenarios to OpenClaw. Begin with a bounded validation operation through MCP or a thin tool plugin. A complete sandbox backend is a separate feasibility project because its interactive process and workspace contracts exceed the suite's current common execution interface.

## Evidence and scope

The investigation inspected [OpenClaw PR #97086](https://github.com/openclaw/openclaw/pull/97086), its review discussion and merged tree `008f04a65650f364ddbe40f1697f3c1b020bba97`; OpenClaw main at `df93a28f0bc58c41023326a734e3838bb356eea4`; Microsoft MXC tag `v0.9.0`; and suite source at `8161cfdc761557800d18d918a79f2731deb1a488`. The documentation branch starts at `63c0e97dbf39a0d976b07ffb9437317170f1a26d`; the intervening suite change reports credential-expired proxy streams as interrupted. Live OpenClaw documentation was read on 2026-10-02 and may evolve independently of those source snapshots.

The evidence is source, documentation and published author reports. No OpenClaw or MXC runtime was installed or executed during this investigation. No performance, cost, containment, cross-platform compatibility or production-readiness result was measured. Existing suite tests and upstream reports identify available evidence; they do not qualify the proposed integration.

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

The initial proposal exposes one fixed validation operation to OpenClaw, with a Python service calling the existing Bicep kind through the router. Prefer local stdio MCP for the first single-owner deployment; a thin TypeScript tool plugin remains the alternative if trusted caller context or cancellation cannot be preserved through that route. OpenClaw already supports [MCP servers](https://docs.openclaw.ai/tools/mcp), but the suite does not yet provide this service. Transport selection is an acceptance decision, not an implemented interface.

```text
OpenClaw tool policy and approvals
  -> MCP adapter or thin tool plugin
  -> Python workload service with trusted ownership and fixed policy
  -> SandboxRouter admission
  -> configured backend and prepared Bicep image
  -> bounded completion/verdict, diagnostics and declared artifact references
```

Start with Docker and an explicitly admitted `Isolation.CONTAINER` policy for that deployment. Preserve the suite's default isolation floor; do not silently lower it. A host requiring microVM isolation needs a separately qualified compatible backend before this workload is admitted. No new MXC backend is required for the Bicep prototype.

Use the existing [prepared AVM profile](../../../images/bicep-sandbox/README.md#prepared-avm-profile), an immutable deployed image reference, the host's selected Bicep configuration with the prepared cache location, and `Egress.CLOSED`. The current profile contains a small selected set of pinned modules, not the full AVM catalog. Runtime validation must retain `--no-restore`; an unprepared module must produce an incomplete result without a verdict. Build-time downloads and compiler/image provenance remain separate from runtime network closure.

### Initial contract

| Surface | Proposed boundary |
|---|---|
| Operation | One fixed Bicep validation operation; no shell command argument, deployment or arbitrary compiler flags |
| Inputs | Explicit bounded source set and entry point; define supported file kinds, relative path grammar, count/byte ceilings and snapshot semantics in the first design |
| Host policy | Host selects image, compiler configuration, backend, isolation floor, timeouts, transfer budgets and closed networking; model arguments cannot widen them |
| Ownership | Bind the service to a trusted local owner initially; derive finer session/agent/call context only from a trusted host channel, never model-supplied identifiers |
| Results | Preserve `completed` separately from validation verdict, bound diagnostics and distinguish untrusted compiler text from service-authored control fields |
| Files | Confine staging and output collection to adapter-owned roots; no arbitrary host path, home-directory or repository mount |
| Lifecycle | Propagate cancellation and deadline, dispose after each call, expose unsuccessful cleanup and refuse reuse of uncertain state |
| Distribution | Explicit Python/runtime/image prerequisites; packed installation and installed execution must be tested |

Plain serialized labels do not reproduce MAF information-flow enforcement in OpenClaw. Result provenance can be preserved as metadata, but any use as authority requires host enforcement. Similarly, sandboxing this workload does not isolate the OpenClaw Gateway or other plugins. OpenClaw's [capability matrix](https://docs.openclaw.ai/gateway/sandboxing/supported-capability-matrix) describes that boundary.

### Design decisions and proof required

The first design must resolve the public operation schema, source transport, entry-point selection, default limits, error mapping, cancellation acknowledgment, cleanup reporting and supported OpenClaw version. Compare direct use of existing framework tools with a supported framework-neutral workload runner. The protocol/router modules use the standard library, but the [installed core distribution](../../../packages/maf-sandbox/pyproject.toml) still depends on `agent-framework-core`; the packaged kinds expose framework tools. Do not describe the current distribution as dependency-free or directly embeddable in TypeScript.

Before implementation is called complete, demonstrate valid and invalid Bicep, a missing prepared dependency with no verdict, malformed/incomplete diagnostics, oversized input/output, path escapes and file replacement, concurrent caller isolation, timeout/cancellation, cleanup failure, and clean installed-package execution. Verify actual guest networking is closed and compiler policy is applied. A denied external request alone does not prove an allowlist, but this initial workload deliberately requests no outbound destinations. Keep model behavior, service control fields, runtime evidence and CI results distinguishable.

No Terraform plan/apply/destroy, general CodeAct, network credentials, guest host-tools, interactive shell, warm reuse, remote multi-tenant service or complete OpenClaw backend is included in this first delivery. Subsequent designs must establish their own authority and lifecycle boundaries.

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
| First closed-network Bicep validation tool | Open; initial contract proposed | [#1638](https://github.com/sokolaidev/maf-extensions/issues/1638) (open) |
| Additional workload tools and artifact delivery | Open; design follows the Bicep contract | [#1639](https://github.com/sokolaidev/maf-extensions/issues/1639) (open) |
| Enforced egress and credential mediation | Open; backend-specific design required | [#1640](https://github.com/sokolaidev/maf-extensions/issues/1640) (open) |
| Conformance and installed-package qualification | Open; scenario and harness design required | [#1641](https://github.com/sokolaidev/maf-extensions/issues/1641) (open) |
| Full sandbox provider | Open; feasibility and supported SDK decision required | [#1642](https://github.com/sokolaidev/maf-extensions/issues/1642) (open) |
