# Guest platform and commands

The guest is the environment that runs the workload. It may be an image chosen by the host, a template prepared by an operator, or a runtime packaged with the backend.

The backend owns the execution boundary. Choosing an image does not choose or strengthen that boundary. The guest's commands, path rules and available operations are separate questions.

## Guest family

`OsFamily` describes path grammar and command quoting. It has two values: `POSIX` and `WINDOWS`. It does not identify a distribution or list installed commands.

A backend declares `os_families` per instance. A workload sets `requires_os_family` when it needs one. The router raises `SandboxOsFamilyNotSupported` if the requested family is absent.

An empty declaration serves workloads that require no family. A runtime-only backend can use it because code execution does not require a shell or guest filesystem. A workload that requests a family is still refused against an empty declaration.

| Backend | Family declaration |
|---|---|
| ACAS | `POSIX` |
| WSLC | `POSIX` |
| Docker | `POSIX` for a Linux daemon; empty for a Windows daemon |
| Hyperlight | Empty; uses a code runtime |

Docker's async factory reads the daemon type. Cold acquisition checks it again before creation or restart. The adapter's POSIX commands do not support Windows guests merely because a daemon can run them.

Register separate backend instances when different guest templates require different fixed declarations. An instance's isolation level and supported families must remain accurate for every workload it accepts.

## Who owns a command

| Command purpose | Owner | Contract |
|---|---|---|
| Workload tools, such as Bicep or Terraform | Kind | Choose commands and declare the required guest family |
| A language runtime | Runtime backend | Serve `RUN_CODE`; the kind need not name an interpreter |
| Infrastructure, such as file preparation or cleanup | Backend | Implement protocol methods through its own safe mechanism |

Core does not carry a list of commands a spec wants installed. The backend knows which commands support its operations. The kind owns tools needed by its workload.

`EXEC` accepts shell strings as well as argv. The shipped command backends therefore require a working `sh`. A shell-free image can still serve Docker file-only workloads.

The host-tool transport has its own POSIX launcher and supervisor. It needs its documented helpers and the kind's interpreter. `setsid` is optional. Declaring `WINDOWS` alone cannot make that transport work on a Windows guest.

<a id="decision-3--a-static-ceiling-matched-at-attach-and-a-probe-at-acquire"></a>

## Check declarations, then check the image

Before a sandbox exists, the router checks the backend's declared capabilities. For a backend that accepts images per spec, those declarations are the maximum it can provide with a compatible image.

At acquisition, the backend checks image-dependent requirements against the actual sandbox. An incompatible image raises `SandboxCapabilityNotSupported` before the workload runs. Cancellation propagates.

![At tool attachment the router checks static backend declarations against the workload. A fixed runtime or image relies on its tested contract. An image supplied per spec also passes backend-owned checks at acquisition. Successful checks are cached for that physical sandbox; a new instance or newly required command needs a check. An incompatible image is refused.](assets/guest-probe-flow.svg)

```
Does a capability depend on the guest image?
├─ No   Verify the backend's API or runtime contract.
└─ Yes  When is the image bound?
        ├─ At construction  Verify the fixed image in conformance tests.
        └─ Per spec         Match the static ceiling at attachment.
                            Check the actual sandbox at acquisition.
```

| Backend | Acquisition checks |
|---|---|
| Docker | `sh` for `EXEC`; `rm` invocation for `FILES_DELETE`; launcher helpers for `HOST_TOOLS` |
| WSLC | `sh` for `EXEC`; true and false cases of external `/usr/bin/test`, pinned independently of `PATH`, and the image user's `mkdir`, `cat`, `wc`, `mv` and `rm`, for `FILES_IN`; root `/bin/sh`, `mkdir` and `chown` for either, since both prepare a base; a resolved image user |
| ACAS | `sh` for `EXEC`; launcher helpers for `HOST_TOOLS`; a planted-file removal check for relevant file operations |

Docker and ACAS check `sh`, `mkdir`, `mv` and `nohup` when `HOST_TOOLS` is requested. Docker's engine-backed file transfers do not need command checks. A workload's interpreter remains the kind's responsibility.

Successful command checks are cached per physical sandbox, not per image reference. Warm acquisition checks newly required operations. A replacement sandbox is checked again. Failed or interrupted checks are not cached.

The command-check deadline is at most ten seconds per acquire, shortened by the backend's configured timeout. Docker and WSLC keep refused containers tracked for retry or disposal. ACAS disposes a new sandbox on capability refusal and retains a warm one.

## What a probe establishes

A command probe establishes invocation compatibility. It cannot prove every future operation will succeed, prevent a command changing later, or authorize a more privileged operation.

ACAS's removal probe checks through the file API that the planted file disappeared and its directory remains. A successful exit or printed marker alone is insufficient. Each actual removal still runs as the guest and has its own absence check.

An inconclusive ACAS removal probe permits `FILES_OUT` and `HOST_TOOLS`, but refuses `FILES_DELETE`. Incomplete probes can be retried. Image hints expire after 60 seconds; each sandbox retains its own measured result. The [ACAS guide](backends/acas.md) owns the detailed cache and lifecycle rules.

`RECLAIM` is separate from these workload-requested checks. The router selects cleanup from static declarations. A probe must not change a shared backend declaration to represent one image's result.

A backend with a fixed guest verifies its advertised operations in conformance tests. A successful probe or fake-backend test is not a substitute for those tests.

## Filesystem behavior

The file protocol defines behavior directly; there is no `FilesystemTraits` declaration.

| Concern | Contract |
|---|---|
| Case and Unicode collisions | Output names use conservative collision checks, independent of guest OS |
| Links | `EntryKind.SYMLINK` includes junctions and reparse points; confinement rules govern traversal |
| Storage base | Acquire prepares the backend-selected or explicitly requested base for exec and file workloads |
| Existing directories | Preserve contents, ownership and modes; refuse unsafe or obstructed ancestry |
| Runtime-only workloads | Need no filesystem |

Base preparation does not promise that guest code will leave it intact or that every guest principal can write there. Writes and the launcher create per-call children. See [storage ownership](hosts.md#where-the-storage-base-comes-from).

Core provides lexical path rules in `maf_sandbox.paths`. A backend supplies entry metadata. `stat_by_asking_the_guest` is an optional helper for backends without an authoritative metadata API; using it does not make a guest answer authoritative.

Safe file access also depends on what happens between a check and the operation. Each [backend guide](backends/README.md) documents its mechanism and remaining race windows.

## Status

| Decision | State | Tracking |
|---|---|---|
| Guest family declaration and matching | Implemented | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed); [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged) |
| Docker daemon-family checks | Implemented | [#587](https://github.com/sokolaidev/maf-extensions/issues/587) (closed); [#747](https://github.com/sokolaidev/maf-extensions/pull/747) (merged) |
| ACAS and WSLC POSIX declarations | Implemented | [#588](https://github.com/sokolaidev/maf-extensions/issues/588) (closed); [#946](https://github.com/sokolaidev/maf-extensions/pull/946) (merged) |
| Backend-owned infrastructure operations | Implemented; launcher still uses POSIX helpers | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) (closed); [#585](https://github.com/sokolaidev/maf-extensions/issues/585) (closed); [#735](https://github.com/sokolaidev/maf-extensions/issues/735) (closed) |
| Capability checks on the acquired image | Implemented on Docker, WSLC and ACAS | [#586](https://github.com/sokolaidev/maf-extensions/issues/586) (closed); [#1089](https://github.com/sokolaidev/maf-extensions/pull/1089) (merged) |
| Storage-base preparation and allocation | Implemented | [#466](https://github.com/sokolaidev/maf-extensions/issues/466) (closed); [#1086](https://github.com/sokolaidev/maf-extensions/pull/1086) (merged); [#480](https://github.com/sokolaidev/maf-extensions/issues/480) (closed); [#1090](https://github.com/sokolaidev/maf-extensions/pull/1090) (merged) |
| Separate guest shim | Implemented | [#357](https://github.com/sokolaidev/maf-extensions/issues/357) (closed); [#590](https://github.com/sokolaidev/maf-extensions/pull/590) (merged) |
| Additional host-tool transports | Unimplemented; transport negotiation remains open | [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
| Filesystem behavior in protocol contracts | Decided; no separate trait declaration | untracked |
