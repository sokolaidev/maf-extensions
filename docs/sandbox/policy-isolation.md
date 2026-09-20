# Policy and isolation

The host sets the minimum acceptable isolation and limits sandbox sharing. A workload's `SandboxSpec` states the capabilities, guest platform and network policy it needs. The router checks the backend's declarations before serving it.

These are separate checks. Strong isolation does not supply a missing capability, and a capability does not establish isolation.

## Isolation levels

`Isolation` has seven levels, ordered by `ISOLATION_RANK`. `meets_floor` compares a backend's level with the required floor.

| Level | Value | Boundary |
|---|---|---|
| `NONE` | `none` | Host process and host authority; useful for test fakes |
| `RUNTIME` | `runtime` | Restricted interpreter or software runtime inside the host process |
| `PROCESS` | `os_process` | Separate address space; shared host kernel and filesystem, without namespaces |
| `CONTAINER` | `container` | Shared-kernel namespaces and resource controls |
| `HARDENED_CONTAINER` | `hardened_container` | Additional syscall isolation, such as a userspace kernel |
| `MICROVM` | `microvm` | Hardware virtualization with a minimal or absent guest OS |
| `VM` | `vm` | Dedicated full VM on remote infrastructure |

The order describes where the boundary is enforced. It does not rank every implementation's security. Capabilities, exposed authority and deployment configuration still matter.

Unknown isolation values are refused. Use `os_process` for the process level; `"process"` is not a valid serialized value.

## The floor

The default host floor is `Isolation.MICROVM`. `SandboxSpec.min_isolation` can raise it but cannot lower it.

```python
router = SandboxRouter(backends)  # Default: MICROVM.
router = SandboxRouter(backends, min_isolation=Isolation.VM)
router = SandboxRouter(backends, min_isolation=Isolation.CONTAINER)
```

![The seven isolation levels run from none through runtime, OS process, container, hardened container, micro-VM and full VM. The effective floor is the stronger of the host floor and the spec's optional floor. In the example the default micro-VM floor refuses every lower level and admits micro-VM or VM to the remaining checks. Meeting the floor alone does not satisfy capability, guest, network or authority requirements.](assets/isolation-floor.svg)

With fixed selection, a selected backend below the host floor is refused at router construction. With per-spec selection, construction refuses when no registered backend clears the floor. Weaker backends remain registered for disposal and produce a warning, but never serve work below the floor.

## The micro-VM standard

A backend declaring `MICROVM` or stronger must provide all four:

1. A hardware virtualization boundary around guest execution.
2. Only identity and grants intended for that workload, without the host's control-plane credential in the guest.
3. Enforcement of `CLOSED` or `ALLOWLIST` egress, not only unrestricted access.
4. Explicit guest-to-host channels, without undeclared mounts, socket passthrough or shared writable state.

The router checks declarations. It does not inspect or certify the deployment behind them. For example, [ACAS group identity](backends/acas.md#sandbox-group-identity) is trusted host configuration outside the core attachment declarations.

Shared capability and egress conformance checks exercise parts of this contract. They are not a complete isolation audit. The broader in-sandbox isolation probe suite remains unimplemented; backend guides describe their evidence and remaining limits.

## Admission checks

<a id="the-six-checks"></a>
<a id="the-seven-checks"></a>

`ensure_can_serve(spec)` checks configuration before tool attachment. Acquisition uses the same policy. With no backend, `ensure_can_serve` returns without attaching anything; a direct `acquire` raises `NoSandboxBackend`.

Host denials apply to every candidate. Requiring a denied capability raises `SandboxCapabilityDenied`; declaring a denied identity raises `SandboxIdentityDenied`.

| Backend check | Required behavior | Refusal |
|---|---|---|
| Isolation | Meet the stronger host/spec floor | `SandboxBackendNotPermitted` |
| Capabilities | Include every required capability | `SandboxCapabilityNotSupported` |
| Guest family | Include the spec's requested family, when set | `SandboxOsFamilyNotSupported` |
| Transfer limits | Permit the spec's file and byte budgets | `SandboxTransferLimitsNotPermitted` |
| Egress mode | Enforce the spec's exact mode | `SandboxEgressNotEnforced` |
| Isolation scope | Serve the stricter host/spec scope | `SandboxScopeNotEnforced` |
| Attached authority | Fit explicit opt-in, channel, sharing and retention bounds | `SandboxAttachedIdentityNotPermitted` |

Per-spec routing tries the next candidate after a declaration check refuses one. Fixed routing does not switch backends to hide a mismatch. Method restrictions also require capability and token support. Capability/declaration inconsistencies are configuration errors.

Cleanup is resolved separately: `RECLAIM < RESET < DISPOSE`. The host defaults to `DISPOSE`; reuse requires explicit opt-in. The spec can raise that floor. Missing cleanup support selects a stronger operation. Call-scoped work always disposes. See [call cleanup](tool-call.md).

## Backend declarations

`BackendDeclarations` groups the optional claims. Each field has its own default; leaving one unspecified does not imply support.

| Field | Default |
|---|---|
| `capabilities` | `DEFAULT_CAPABILITIES`: `EXEC` and `FILES_IN` |
| `limits` | `DEFAULT_SANDBOX_LIMITS` |
| `egress_modes` | Empty: every requested mode is refused |
| `os_families` | Empty: specs requiring a family are refused |
| `isolation_scopes` | `{CONVERSATION}`; an explicitly empty set is also read as conversation scope |
| `observes_egress` | `False` |
| `egress_method_tokens` | Empty; consulted with `EGRESS_METHODS` |
| `attached_identity` | `NO_ATTACHED_IDENTITY`; not a discovery result for ACAS group configuration |
| `requires_exclusive_admission` | `False`; an optional backend admission hook also requires exclusive calls |

`isolation` remains a required backend property. The separate legacy declaration attributes are refused; backend authors must use `BackendDeclarations`.

## Backend choices

| Backend | Declared isolation | Main constraint |
|---|---|---|
| [ACAS](backends/acas.md) | `MICROVM` | Service-backed POSIX guest; host owns group configuration |
| [Hyperlight](backends/hyperlight.md) | `MICROVM` | Packaged Python runtime; supported host platform required |
| [Docker](backends/docker.md) | `CONTAINER` | Explicitly lower the default host floor |
| [WSLC](backends/wslc.md) | `CONTAINER` | Explicitly lower the floor; narrower file and command support |
| [In-process fake](backends/in-process.md) | `NONE` | Tests; its declarations do not establish containment |

The [backend comparison](backends/README.md) owns capability and network support. [Guest platform](guest-platform-and-commands.md) explains how backend instances declare a fixed guest family and isolation level.

## Status

| Decision | State | Tracking |
|---|---|---|
| Seven isolation levels and a raise-only floor | Implemented | [#96](https://github.com/sokolaidev/maf-extensions/pull/96) (merged); [#331](https://github.com/sokolaidev/maf-extensions/pull/331) (merged); [#347](https://github.com/sokolaidev/maf-extensions/pull/347) (merged) |
| Required capabilities matched to declarations | Implemented | [#96](https://github.com/sokolaidev/maf-extensions/pull/96) (merged); [`capabilities.md`](capabilities.md) |
| Exact egress mode or refusal | Implemented | [#265](https://github.com/sokolaidev/maf-extensions/issues/265) (closed); [network.md](network.md) |
| Host-tool capability and identity denials | Implemented | [#133](https://github.com/sokolaidev/maf-extensions/issues/133) (closed); [hosts.md](hosts.md) |
| Core attached-authority admission | Implemented; backend and credential integration remain partial | [`hosts.md`](hosts.md) |
| Broad in-sandbox isolation probes | Unimplemented; shared capability and egress probes are separate | untracked; [`backends/acas.md`](backends/acas.md); [`network.md`](network.md) |
| Guest-family matching | Implemented | [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged); [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed); [`guest-platform-and-commands.md`](guest-platform-and-commands.md) |
| Fixed or per-spec backend selection | Implemented | [capabilities.md](capabilities.md#backend-selection) |
| One optional backend declarations object | Implemented | [#591](https://github.com/sokolaidev/maf-extensions/issues/591) (closed) |
