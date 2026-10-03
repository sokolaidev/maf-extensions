# Backend-owned program channels

A kind declares `SandboxSpec.program=ProgramRequirements()` and supplies source to the channel retained by `SandboxToolSession.program_channel(key, sandbox)`. The backend owns execution, guest API installation, transport framing and publication. A kind does not choose an interpreter or construct the host-tool transport. `SandboxToolSession.prospective_program_channel(key)` previews the retained or initially selected channel without acquisition, so a kind can validate channel-dependent inputs before reading host files. Recheck against `program_channel(key, sandbox)` after acquisition because availability fallback or disposal can change the channel.

## Selection and lifetime

`SandboxRouter(program_channel_preference=("exec", "runtime"))` prefers exec channels by default. Reverse the tuple to prefer runtime channels. This orders compatible channels within the host's permitted backend set: `Selection.FIXED` still permits only its resolved backend, while `Selection.PER_SPEC` considers registered candidates. Isolation, capabilities, egress, identity and transfer limits remain admission requirements. Capability denials apply to the channel's internal requirements as well as the workload's requirements.

Only `SandboxBackendUnavailable` during initial acquisition permits fallback. A backend may raise it only after establishing that it retained no instance or uncertain work; policy, configuration, timeout and uncertain cleanup failures do not qualify. The shipped adapters do not reclassify arbitrary SDK errors as availability failures. An unchanged acquisition spec retains its selected backend until its instance is disposed. Sibling specs may select different backends and instances. Once acquisition identifies a reused instance, its retained channel is enforced before program preparation, even when a changed spec initially selected another channel. Later failures cannot move that instance's state to a different channel or replay work.

Program calls hold exclusive admission through execution and cleanup. Profile verification happens after acquisition and before workload execution. Verification failure disposes the instance and stops the call. Backend call admission and cleanup ownership move together when an initial availability failure selects another backend.

## Portable Python profile

`python-portable-v1` requires Python 3.11+ semantics, statements whose results are printed, and `json`, `math`, `re`, `sys` and `types`. It promises no other imports, third-party packages, process facilities or retained state between calls. The shipped exec channel verifies CPython 3.11+ and these imports in the acquired guest. A native implementation must verify equivalent profile guarantees independently.

`ProgramRequirements.max_program_bytes` defaults to 8 MiB of UTF-8 source. `host_tool_timeout_seconds` defaults to 30 seconds. These are host configuration, not model arguments. CodeAct's automatic path accepts this profile and renders its guarantees in the tool instructions. Its explicitly configured `CodeactRuntime` path remains available with host-supplied instructions and no host tools.

`files_in` and `files_out` describe shared files independently of the program. A channel declares its actual internal capabilities through `required_capabilities(spec)` and physical transfer demand through `transfer_limits(spec)`. The router compares the complete demand with backend ceilings without clamping it. The exec channel accounts for the program, a bounded shim when needed, requests, responses, refusals and framing. A future native channel need not invent file capabilities for callback traffic.

## Implementing a channel

Publish a `ProgramChannel` in `BackendDeclarations.program_channels`. Declare its name, mode, profiles and host-tool support; implement capability and transfer admission, guest working directory, profile verification and execution. `run` receives the current call's source, requirements, guest call path, deadline budget and optional `HostToolPolicy`. It must revoke that policy on every exit, including validation or staging failure. Retaining a registry or a previous run's callback authority across calls is forbidden.

Docker, ACAS and Docker Sandboxes declare `ExecProgramChannel()` for programs and host tools. WSLC declares `ExecProgramChannel(host_tools=False)` for programs only. The exec mechanism uses `EXEC` and `FILES_IN`, adding `FILES_OUT` for host tools. Its host-tool launcher also requires the existing POSIX transport utilities, including `sh` and `nohup`. `HOST_TOOLS` without an explicit supporting channel is refused; no adapter is inferred from file and exec capabilities.

The shared guest API remains `maf_host_tools`, including its argument, value and refusal behavior. A native implementation must expose that same API through its own mechanism and dispatch every call through the live `HostToolPolicy.call`. The guest helper carries no policy authority.

`assert_program_channel_conformance` exercises profile verification, the shared guest API, two fresh policy runs, confirmed publication and authority revocation against an acquired sandbox. The caller owns acquisition, admission and disposal. This helper supplements backend-specific framing, timeout, confinement and lifecycle tests; it does not establish them by itself.

## Publication and timeout

`HostToolRun.call` now requires `publish=`. Policy validates the request, reserves response capacity and executes the host body. It then awaits a trusted transport callback that accepts a `HostToolCallResult` and returns only after the complete response has been accepted or published into the channel. The exec transport confirms its bounded response-file write. A native callback return, a guest acknowledgement or successful program completion is not an equivalent confirmation point.

`HostToolCalled.host_started` and `host_completed` report host execution separately from delivery. `delivered` and confirmed response bytes are recorded only after successful publication. A publication exception or cancellation produces `delivery_uncertain`, preserves the reservation and revokes the run. No automatic retry follows an uncertain response. Publication proves transport acceptance, not that guest code consumed the value.

The exec channel bounds each asynchronous callback by its configured timeout and the remaining supervised program deadline. It allows one second to drain cancellation. Only a cleanly cancelled host call before publication can yield a bounded timeout refusal and allow the guest to continue. Otherwise authority closes; a task that outlives cleanup marks the sandbox unclean for retirement and is retained until it stops. Cancellation cannot roll back external effects. A synchronous host body that blocks the event loop cannot be forcibly interrupted by this mechanism; hosts must supply cooperative, bounded functions.

## Migration

Upgrade core and the affected CodeAct, backend and observer packages together. CodeAct callers using shipped exec backends can keep their factory wiring; optional `program=ProgramRequirements(...)` configures the independent program and callback budgets. Program bytes no longer consume the automatic path's shared-file allowance. The explicit `CodeactRuntime` path retains its existing file-budget behavior.

Custom backends must declare a channel explicitly, and custom kinds must declare program requirements before requesting host tools. Low-level callers of `HostToolRun.call` must supply a real publication callback; returning from a no-op callback is appropriate only for an actual in-memory acceptance endpoint such as a test. A callback preparing bytes for a later, unacknowledged handoff must not report them as delivered.

Hyperlight's production native channel is still pending under [#369](https://github.com/sokolaidev/maf-extensions/issues/369). The research harness records unacknowledged native responses as uncertain. Native acceptance after serialization and bounded containment of direct oversized requests remain prerequisites; this migration does not declare Hyperlight host-tool capability.

## Status

| Area | Status | Tracking |
|---|---|---|
| Core admission, exec channels and confirmed publication | Implemented; native integration remains open | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open); core/exec by [#1664](https://github.com/sokolaidev/maf-extensions/pull/1664) (merged) |
| Hyperlight native framing and acceptance confirmation | Not implemented | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
| Backend-specific guest APIs | Deferred to a separate issue; not filed | untracked |
