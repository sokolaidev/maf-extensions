# Guest platform and command availability: what a kind may assume, and how it finds out

> The decided design for the guest-platform axis and for command availability: which guest facts are declared and matched, which are classified away, and which are probed. It extends the capability axis ([`capabilities.md`](capabilities.md)) and the isolation ladder ([`policy-isolation.md`](policy-isolation.md)), and rests on decisions recorded in [`architecture.md`](architecture.md) and in the records under [`research/`](research/).

## The guest, and who supplies it

**The guest is whatever sits on the far side of a backend's boundary — the environment a workload's commands actually execute in.** `Sandbox.exec` runs there, `write_file` writes there, and `work_dir` is a path in its namespace rather than the host's. `Isolation` ranks the boundary; the guest is what that boundary encloses.

**A backend owns the boundary. It does not necessarily author what is inside it.** That distinction is the reason this document exists, and the backends shipping today split three ways on it:

| How the guest arrives | Who chose it | Bound when |
| --- | --- | --- |
| The backend resolves an image reference carried in the spec | The host application, per workload | At `acquire` |
| The backend serves a disk image an operator imported ahead of time | The operator, per deployment | Before the process starts |
| The backend ships its own guest module | The backend author | At build time |

In the first row the backend meets its guest for the first time inside `acquire`, having never seen the inside of it. `SandboxSpec.image`'s docstring states the position plainly — the image is *"a reference the **backend** resolves, and nothing here parses it"* — so no layer in this stack knows whether the thing about to boot is Ubuntu, a distroless image, or Windows Server, and none of them can tell whether the commands a kind is about to run exist in it. [`research/sandbox-architecture.md`](research/sandbox-architecture.md) records the same arrangement from the path side: `work_dir` is *"guest-native, stated by the host to suit its image and rewritten by nobody"*.

**Not every backend has a guest in the operating-system sense.** A backend whose surface is a language runtime hands out a program environment with no shell, no filesystem layout and no argv. A backend whose surface is a data-plane API has a filesystem but reaches it through calls rather than commands. Both are real guests; neither has an OS to declare, and neither can be asked one of the questions below. This is why the axis is scoped to `EXEC` backends rather than applied to all of them.

**Two senses of "host", kept apart throughout.** The **host application** is the process that wires the router and configures specs — the sense `Identity.APP` carries, *"the host application's own authority"*. The **host machine** is the physical machine a backend's boundary is drawn on, which matters only in Decision 5, where which hypervisors exist depends on it. Where the difference matters below, the full phrase is used.

## The question

Two questions look like one, and separating them is most of this document.

**What shape is the guest?** Path grammar, argv quoting, the spelling of a script's first line, which names a filesystem refuses. A kind that plants `#!/bin/sh` and execs it is making a claim about its guest, and nothing checks it.

**What is installed in the guest?** `sh`, `rm`, `python3`, `setsid`, a compiler. A host that points a kind at an image without the tool that kind needs gets a failure deep inside a tool call, with no earlier signal.

Every other kind-to-backend fit question in this stack is declared by the backend, required by the spec, and refused by the router before a tool is ever attached. These two are not. Nothing declares them and nothing matches them: they ride entirely on the image, which — per the table above — the backend may itself never have looked inside.

The two questions get different answers. The first becomes a declaration. The second does not, and most of the value here is in saying why.

## The governing rule

> **Declare only what the router must refuse on. Everything else, remove the need to know.**

This is not a new rule. It is what this stack has already reached three times, independently, on three different surfaces:

| Unknown | The answer taken |
| --- | --- |
| Which delete mechanism the guest has | **The backend owns it.** [`tool-call.md`](tool-call.md) rules that reclamation *"must be the backend's, because core can only dispatch to mechanisms core can name: a backend offering a language runtime and no shell deletes through that runtime, which no capability check reaches."* |
| Where a workload's files live | **The backend owns it.** The settled direction is that a backend allocates the storage base and resolves every path against it, and kinds address everything relative to that base instead of composing absolute paths. |
| Which interpreter exists | **A capability owns it.** `RUN_CODE` means "run this in whatever runtime you have", so a kind stops naming `python3` at all. |

Two of the four candidate axes below dissolve under this rule. That is the point of stating it first.

## The state today

### What is already platform-neutral, and stays that way

The protocol has been kept deliberately additive on this axis, and the neutrality is pinned rather than assumed.

- **`work_dir` is guest-native and untranslated.** `SandboxSpec.work_dir` defaults to `/maf-sandbox/work`, and its docstring records that translating it is *"not possible — a kind derives absolute paths from this field and passes them into `Sandbox.exec`'s argv, and a backend cannot find a path inside an opaque argv without parsing arbitrary command lines. An argv sequence protects against quoting, not against paths within the arguments."* The default is a default, not a requirement.
- **Nothing infers a guest OS from a path.** The same docstring states the rule directly: *"A workload must not read the guest's platform out of this field, and nothing here validates it against one."*
- **The neutrality is a test, not an intention.** `TestWorkDirStaysGuestNative`, in `maf-sandbox`'s router tests, accepts `C:/agent/maf-sandbox/work` and a backslash-spelled Windows path, and asserts that a backend declaring no platform still serves. It matters more now that something else *is* a platform claim, so it also pins the inference the protocol refuses to make: a drive-rooted `work_dir` against a `POSIX`-only backend is **served**, because the path was never the ask. That is what makes everything below an additive change rather than a breaking one.
- **Declared output paths are POSIX-shaped and the names are conservative.** [`capabilities.md`](capabilities.md) fixes one path grammar for declared outputs, always UTF-8, with no newline translation — and explicitly retracts an earlier claim that backends would translate. In `_outputs.py`, `_nfc` composes to NFC, `_collision_key` keys on `str.lower` rather than `str.casefold`, and `_check_declared_names` is what refuses a case-only collision — with the reason recorded beside it. `portable_file_name` does none of those: it rewrites the segments Windows will not accept, and nothing else.
- **`EntryKind` was designed for guests that are not POSIX.** A junction or a reparse point maps to `SYMLINK`, so the vocabulary does not have to grow for a Windows guest.

### What is not neutral

Everything in this table is a live claim about the guest, made by code that has no way to check it.

| Where | The claim |
| --- | --- |
| `_host_tools_over_exec.py`, `launcher_script` | The guest runs `#!/bin/sh` |
| `_host_tools_over_exec.py`, `launcher_script` | `command -v setsid`, then `setsid nohup sh -c` — `setsid` optional, `nohup` and `sh` not |
| `_host_tools_over_exec.py`, `_stop_the_program` | `kill -KILL … 2>/dev/null` |
| `maf-sandbox-docker`, `_backend.py`, `remove` and `reclaim` | It is `rm -rf` / `rm -f`, *"since the engine has no delete primitive"* |
| `maf-sandbox-acas`, `_backend.py`, `_AcasSandbox.probe_guest_removal` | The guest has `rm`. The file plane plants a root-owned probe file and verifies its removal; stdout and a successful exit alone cannot enable `FILES_DELETE`. An inconclusive probe serves `FILES_OUT` and `HOST_TOOLS` and refuses `FILES_DELETE` |
| `maf-sandbox-acas`, `_backend.py`, `remove` | The guest has `rm`; the file plane checks paths and confirms absence after guest execution |
| `maf-sandbox-acas`, `_backend.py`, `reclaim` | Refuses with `NotImplementedError` before any service call: safe ancestry for a host-authority delete cannot be established. The router uses disposal |
| `maf-sandbox-wslc`, `_backend.py`, `reclaim` | Refused without running a guest command; router-managed cleanup disposes the kind through the engine |
| `maf-sandbox-codeact` | The interpreter is spelled `python3` |
| `maf-sandbox-bicep` | POSIX command templates |
| `maf-sandbox-wslc` | The write-path filesystem path check is answered by `test` run inside the guest, through core's spelling of it |
| `maf-sandbox`, `paths.py`, `stat_by_asking_the_guest` | The guest has `test`, and it answers 0, 1, or an error. Offered rather than used: nothing in core calls it, and the claim belongs to whichever backend reaches for it |

**Core's two removals are not in the table**, and Decision 2 below is why. `reclaim_guest_path` and `_remove_tree` spell no command at all: both dispatch to `Sandbox.reclaim`, so the claim about the guest lands one layer down, on the backend rows above, where it is a claim the backend is in a position to make. The `paths.py` row is the one place core does spell a command, and it is spelled without being made: core writes the argv and the parser, calls neither, and the claim still lands on the backend that reaches for it — which is why reaching for it is a declared posture with a README requirement rather than a helper like any other ([#733](https://github.com/sokolaidev/maf-extensions/issues/733)).

`launcher_script` already concedes the whole problem in its own docstring:

> POSIX shell, and a guest that has `nohup`. `setsid` is used when present and done without when not. **A Windows guest or a distroless image needs a different launcher; that is a backend's business, and this one is a helper rather than a protocol.**

That sentence is correct and no backend has taken the offer. It is the largest single item of work this document scopes, and it is named again under *What this does not solve*.

### The one static claim a backend already made

`exec` in `maf-sandbox-acas` carried it in prose until there was a field to put it in:

> The SDK's own `exec` takes a string only, so a sequence is quoted into one with `shlex.join` first. `shlex.join` produces POSIX quoting, which is correct here **because every sandbox this backend hands out is Linux.**

This was never a defect. It is a true statement a backend can make about itself, written in a comment because there was nowhere better. Decision 1 gave it somewhere: the backend declares `os_families = {POSIX}`, and the docstring names that declaration instead of standing in for it. `maf-sandbox-wslc` arrived at the same place from the other side — its guests were always Linux containers and it stated nothing at all.

### When the router decides, and how

The timing constrains every option below, so it is worth stating exactly.

- Backend declarations are read **synchronously**, with one `getattr` for the whole `BackendDeclarations`, and at more than one time. **At construction**, what is read depends on the selection: with a fixed selection, the object itself and `isolation`, for the one backend the router resolves; selecting per spec, the same for *every* registered backend, plus the shape of the four fields that refuse an unreadable value — `capabilities`, `egress_modes`, `isolation_scopes` and `limits` — because past construction such a refusal cannot be told from a backend honestly declining one spec. `os_families` is deliberately not among them: it normalises a value it cannot read rather than raising, so there is nothing there to mistake. **Then per spec**, inside `_refuse_unless_this_backend_can_serve`, every matching field is read for real: `capabilities`, `limits` through `_declared_limits`, `egress_modes` — the set of modes a backend can enforce, not a single declared posture — and `os_families` through `_declared_os_families`. The object must therefore be a plain attribute, or a property over configuration, settled by the time the router asks — never an `async` query.
- `ensure_can_serve` runs at **attach**, called from `sandboxed_tool` in `maf.py`, before any sandbox exists.
- The same checks run **again** inside `SandboxRouter.acquire`, immediately before it calls the serving backend's `acquire` — its docstring says *"before ever reaching the backend"*, so that a caller skipping `ensure_can_serve` is still refused.
- **Which backend those checks run against depends on the selection**, and neither answer changes the timing above. Under `Selection.FIXED` it is the one backend `_resolve` settled at construction. Under `Selection.PER_SPEC` construction reads *every* registered backend's declarations through `_eligible`, and the per-spec checks then run against each in registration order until one passes — so the match is also what chooses. Both still happen before any guest is running, which is the only property the consequence below rests on.

**Consequence, and it is the hinge of Decision 3: a capability set is matched twice before a sandbox with that image exists.** Nothing a backend learns by looking inside a running guest can reach the declaration the router matched.

## Decision 1 — `OsFamily`, declared per backend instance, matched at attach

```python
class OsFamily(StrEnum):
    """The guest's path grammar and argv quoting. Not its operating system, and not what is installed in it."""

    POSIX = "posix"
    WINDOWS = "windows"


# On the backend's `BackendDeclarations`, beside `capabilities`, `limits` and `egress_modes`:
os_families: frozenset[OsFamily]

# On SandboxSpec:
requires_os_family: OsFamily | None = None
```

`ensure_can_serve` refuses when `spec.requires_os_family` is set and is not in the backend's `declarations.os_families`. A spec that declares nothing is refused by nothing, exactly as `spec.requires` behaves against `declarations.capabilities` today, so the change is additive for every existing caller.

**Two values, not three.** Subtract command availability — Decision 2 sends it elsewhere — and nothing left branches on Linux versus macOS. What does branch is argv quoting (`shlex.join` versus `CommandLineToArgvW` rules), path grammar (a `/` root versus drive letters and UNC), the Windows reserved device names, and what `EntryKind.SYMLINK` maps from. All four split POSIX from Windows and none splits Linux from macOS.

**Case sensitivity is the trap that makes the point.** It looks like the one fact that would justify a separate `macos` value, and it is not an OS property at all: APFS can be case-sensitive and NTFS supports per-directory case sensitivity, so no OS name implies it. The stack already handles it by being conservative rather than by asking — `_collision_key` keys on `str.lower` and `_check_declared_names` refuses the collision, neither of them consulting the guest.

**Why `linux` would be a member that decides nothing.** The reason anyone reaches for `linux` over `posix` is to reason about which commands exist — and `linux` does not answer that either. A distroless image is Linux and has no `sh`, no `rm` and no `python3`. A macOS guest is POSIX and has no `setsid`. A member that feels like it decides something and decides nothing is the failure mode this stack has already met on other axes, and it is worth refusing here rather than deprecating later.

**Why a `frozenset`, not a scalar.** Every backend shipping today serves exactly one guest family, so a scalar would look sufficient. A local hypervisor does not: Hyper-V boots Windows and Linux from the same host, and so does KVM. Widening a scalar to a set later is a redefinition of the field, and this stack has a standing reason to fear those — `Isolation.PROCESS` keeps its value as `"os_process"`, leaving `Isolation("process")` raising `ValueError` forever, precisely because a declaration crosses into the vocabulary at run time, out of configuration nobody re-reads. Declaring a set from the first release costs nothing and removes that event.

**Why per backend *instance*, not per class.** The set is a property of what this instance was constructed to serve, resolved at construction. A backend over a local hypervisor pins one guest template family per instance; a Docker backend reads its daemon's `OSType` once at construction, which constrains the family absolutely — a Linux daemon cannot run a Windows image whatever reference it is handed. **It constrains it in one direction only, and the shipped reading says so:** a `linux` daemon entitles that backend to declare `POSIX`, and a `windows` one entitles it to declare nothing, because its own `exec` is `sh -c`, its removals are `rm -rf` and core's path arithmetic refuses a backslash — so `WINDOWS` read off a daemon would be a guarantee no code path backs. Several instances registered side by side, selected per spec, is how a deployment serves more than one. This is the same discipline that keeps `isolation` a constant, and Decision 5 shows it doing double duty there.

**Why `EXEC`-scoped.** [`research/hyperlight-backend-proposal.md`](research/hyperlight-backend-proposal.md) already rules that this whole question dissolves on a `run_code` path: *"the program is text, not a file — no interpreter sentence exists to go false"*. A backend whose surface is a runtime rather than a shell has no argv to quote and no script to plant, and simply does not declare the field. Undeclared is meaningful: it means "this question does not apply to me", and a spec that requires a family is refused against it, which is correct.

**Why the name is `os_family` and not `platform`.** Python has spent the word. `sys.platform` returns `linux`, `platform.system()` returns `Darwin`; a field named `platform` holding `posix` reads as a bug to every Python reader. "Family" is the standard word for the grouping, and the `os_` prefix keeps it clear of this repository's other uses of "family" for exception hierarchies and for the package set.

**What the field must never be read as.** It says nothing about what is installed. The docstring says so, and Decision 2 is the reason it has to.

**One honest note on its centre of gravity.** Path grammar is the most obvious justification for this field and it is the half most likely to shrink: once a backend allocates the storage base and kinds stop composing absolute paths, kinds need much less grammar than they need today. What does not shrink is command and script shape — the launcher's `#!/bin/sh`, a kind naming an executable, the reserved-name hazard. The field should be defended on that, not on path composition.

## Decision 2 — commands are classified, not declared

No backend can honestly declare what commands exist in a guest, because that is a property of the image and one backend instance may be handed many. So commands are sorted into three classes, and two of them stop being this axis's problem.

**Workload commands — `bicep`, `az`, a compiler — stay with the kind.** Running that tool *is* the kind; there is nothing to abstract. The kind declares `requires_os_family`, branches internally across the families it supports, and refuses the rest at attach. Core never learns that `bicep` is a command. This is also the first honest answer to the question this axis has always been held up by — *name the thing that lacks it* — because the thing that lacks it is the kind, which is where the mismatch always actually lived.

**Runtime commands — `python3` — are a capability, and it already exists.** `RUN_CODE` means "run this code in whatever runtime you have". A backend serves it with no shell, no OS and no interpreter name. This class does not take the `OsFamily` fork at all and should not be reasoned about alongside the others.

**Infrastructure commands — `rm`, `sh`, `test`, `kill`, `mkdir` — are raised into protocol methods.** Core dispatches to a backend mechanism rather than prescribing a guest command. `reclaim` is the worked example in [`tool-call.md`](tool-call.md): the method is required, but a backend without safe reclamation refuses it and withholds `RECLAIM`, letting the cleanup ladder select a stronger mechanism. An unguessable call-directory name does not establish safe reach through its ancestors. Working-directory creation likewise belongs to the backend. **The write-path filesystem path check is different:** it is core's rule in `maf_sandbox.paths` over a stat each backend supplies, not a command core spells. Core offers one spelling of that stat for a backend whose engine answers nothing — `test`, one argv and one parser, in `stat_by_asking_the_guest` — but the backend chooses it by name and declares that posture.

What remains after all three classes are placed is a backend that must decide whether a *specific image* can back the capabilities it wants to declare. That is Decision 3.

## Decision 3 — a static ceiling matched at attach, and a probe at acquire

The residue is not a kind-to-backend question. It is a **backend-to-image** question, and the fork tree is three deep.

```
Does this backend's capability set depend on commands inside the guest?
├─ No   A data-plane backend, a runtime backend, any API-surfaced backend.
│       No probe, no OsFamily. Capabilities are properties of the API.
└─ Yes  Any EXEC-surfaced backend.
    ├─ OsFamily decides the dialect the probe and the shims are written in.
    └─ When is the image bound?
        ├─ At construction  Capabilities are constants. Prove them with a
        │                   conformance suite in CI. Refusal stays at attach.
        └─ Per spec         Declared capabilities are a ceiling, matched at
                            attach. Probe at acquire, narrow, refuse there.
```

**The first fork concerns commands used to implement a capability.** ACAS implements `remove` through guest `rm`, so it takes the command branch. Its acquire-time compatibility probe uses the same command. `RECLAIM` is declared only when the backend establishes safe reclamation. Its conformance suite refuses an undeclared capability before touching the guest; a missing declaration sends router-managed cleanup to a stronger rung.

**The fork asks about commands, and a capability can depend on the image without being implemented by a guest command.** ACAS probes because its two planes act as two principals: a non-root guest cannot create files inside a root-owned directory the file plane made, which makes `FILES_OUT` and `HOST_TOOLS` unservable there ([#722](https://github.com/sokolaidev/maf-extensions/issues/722)). Its compatibility probe observes whether the image's `rm` removes a planted file. `FILES_DELETE` itself now uses guest `rm`, followed by file-plane observation. Everything below the third fork still applies: a declared ceiling, a probe at `acquire` when that sandbox has no completed result, and a refusal there. **The question the first fork should ask is whether a capability depends on the image at all**, and a guest command is one way for it to.

**A compatibility probe cannot authorize stronger execution.** ACAS previously gated a host-plane delete on a guest answer ([#950](https://github.com/sokolaidev/maf-extensions/issues/950)). Even observing deletion of a planted file does not establish the workload's authority: an image can ship a privileged wrapper that removes only probe paths. Each `remove` therefore runs as the guest. The probe narrows the declared ceiling for compatibility; it grants no additional authority.

**The probe's failure direction is specific to the capability.** ACAS serves `FILES_OUT` and `HOST_TOOLS` on an inconclusive result, allowing a working image to proceed when its probe cannot complete. `FILES_DELETE` conservatively requires an observed removal. This is an acquisition policy, not the security boundary: every requested removal still executes as the guest and verifies absence through the file plane.

**The second fork is `OsFamily`,** and it decides how to ask, not what is there. The current backends invoke POSIX commands and check their exit statuses; a Windows implementation would need checks for its own execution mechanism.

**The third fork is binding time, and stating it that way rather than as ownership is deliberate.** "Who owns the image" is a judgement; "when is the image bound" is mechanical, and it says exactly what each branch costs.

**Bound at construction** — the backend ships or imports its own guest, and the image is fixed before the router ever asks. Capabilities are constants, refusal stays at attach, and nothing is probed at run time. **The conformance requirement is discharged in CI, not by a documented promise.** `maf_sandbox.conformance` and its probe suites exist for exactly this: a backend asserting that its own image supports its own declared capabilities proves it with a suite, so a regression is caught by the maintainer rather than by a user's agent mid-run. That also keeps this branch clear of the standing rule that a backend must never launder an unchecked string into a guarantee — a claim about an artefact the backend ships and tests is a different thing entirely.

**Bound per spec** — the host supplies the image and the backend meets it for the first time inside `acquire`. Here the attach-time refusal is **not recoverable by any design**, because the image genuinely is not running when the router matches. The resolution is two-tier:

- The backend declares a **static ceiling**: the capabilities it can serve *given a conforming image*. This is matched at attach, exactly as today, and it still refuses everything structurally impossible — a backend that could never serve `FILES_DELETE` fails at attach whatever image arrives.
- The backend **probes at acquire** and narrows. A spec requiring `FILES_DELETE` against an image with no `rm` is refused there. Two refusals, at two times, both loud, and neither claiming knowledge it does not have.

**The probe list comes from the backend, never from the spec.** The backend knows which command backs which capability it wants to announce, so the list is derived from the capability set rather than configured. A spec naming commands would put a command back into core's vocabulary, which is the thing this decision exists to prevent.

**The command checks run on Docker, WSLC and ACAS.** All three require a working `sh` for `EXEC`, whose contract includes shell strings as well as argv. Docker additionally invokes `rm -rf --` without operands for `FILES_DELETE`; WSLC invokes the external `test` as the same root principal its write-path check uses, observing both true and false cases for `FILES_IN`. Docker and ACAS batch `sh`, `mkdir`, `mv` and `nohup` checks when `HOST_TOOLS` is required. `setsid` remains optional, and a workload's chosen interpreter is not prescribed by a capability probe. Docker's engine-backed file transfers need no command probe. WSLC also refuses `FILES_IN` at acquire when write ownership cannot be resolved.

These checks establish invocation compatibility, not conformance or authority: `rm` without operands does not prove that an arbitrary deletion will succeed, and an image can change commands after a check. ACAS retains its stronger observed-removal test and its existing capability-specific inconclusive-result policy. Neither a successful command check nor a removal marker grants additional authority. A shell-free image that previously worked with argv-only `EXEC` is now refused because it cannot serve that capability's full contract; file-only Docker workloads remain supported.

Successful command checks are remembered per physical sandbox, independently of its image reference. A warm acquire runs only newly required checks; replacement instances are checked again. The new command checks share a deadline of at most ten seconds per acquire, shortened by the backend's configured command/read timeout. Failed or interrupted checks are not cached; failures raise `SandboxCapabilityNotSupported`, while cancellation propagates. Docker and WSLC retain refused containers in their existing lifecycle tracking for retry or host disposal. ACAS disposes a newly created sandbox on a capability refusal and retains a warm one, as for its removal gate. These checks create no scratch files. Existing identity, creation and ACAS removal probes keep their own costs and bounds.

`RECLAIM` remains outside this spec-requested command gate. It is now a cleanup declaration that a spec cannot require, and the router resolves cleanup from the static declaration. Per-instance cleanup narrowing requires its own contract; it cannot be achieved by changing one backend's shared declaration after probing an image.

**Probe by observation, and keep the claim narrow.** For ACAS removal compatibility, stdout and a successful exit are insufficient: the file plane must confirm that the planted file is gone and its scratch directory remains. This observes what that command did to that path, not what other workload code can do elsewhere. Each actual removal also needs its own observation, and a successful probe must never unlock a more privileged removal mechanism.

**Cost.** A completed probe is cached per sandbox, so warm reuse pays no further probe cost. Each cold acquire that needs the probe measures the sandbox it booted, even when another sandbox used the same image reference. ACAS pays file-plane preparation, a guest command, file-plane observation and cleanup for each probe; a spec requiring none of the probed capabilities pays nothing. **Incomplete probes are retried:** a timeout or failed file-plane request records no new verdict, so repeated failures cost another bounded probe on each acquire. A repointed reference can add a billable create: an earlier positive image hint permits a create, but a completed inconclusive removal probe refuses a spec requiring `FILES_DELETE` and the newly created sandbox is discarded. Each image hint carries a generation that advances on every write, even when the result is unchanged. A completed inconclusive probe replaces the hint only if its generation has not changed since the probe began, so an uncontested result stops further creates until expiry while an overlapping measurement is preserved. Each written hint expires after 60 seconds; repeated refusals do not renew it, and the next cold acquire after expiry creates and probes again. Each sandbox keeps its own verdict regardless of which probe updates the hint.

## Decision 4 — there is no filesystem axis

Every candidate field for a `FilesystemTraits` declaration dissolves on inspection, and building one would create a second home for facts that belong in the protocol's own contracts.

- **Case sensitivity** — already solved by construction, though not by one function: `_nfc` normalises, `_collision_key` lowercases rather than casefolds, and `_check_declared_names` refuses a case-only collision — none of them asking the guest. `portable_file_name` is a separate, opt-in helper that rewrites Windows-invalid segments. And it cannot be derived from an OS name anyway, per Decision 1.
- **Symlink semantics** — already mapped. `EntryKind` folds junctions and reparse points into `SYMLINK`. What is genuinely open is that confinement is checked and not held — a path validated by the filesystem path check can change before the operation that relied on it — and that is a contract and ordering problem, not a trait a backend could declare.
- **Whether the working directory exists** is a postcondition. `SandboxBackend.acquire` prepares the spec's base for workloads requiring `EXEC` or file capabilities, including warm reuse. Creation uses the backend's file plane; existing contents and permissions are preserved, and an obstructed or unreadable path fails acquire. Runtime-only workloads owe no filesystem. This establishes the base on return, not its survival after guest mutation, its write permissions, or per-call children: the host-tools launcher still prepares its own `work` child. Backend allocation of the base and relative addressing remain the separate storage-ownership design.
- **Permissions, atomicity, persistence** — nothing in this stack branches on any of them today, so a member for any of them would be decorative on arrival.

**The settled position: filesystem behaviour is expressed as protocol contracts and conservative construction, never as a declared trait set.**

## Decision 5 — local hypervisors, and what the rungs mean there

[`policy-isolation.md`](policy-isolation.md) defines the top two rungs as `microvm` — a hypervisor boundary with a minimal or absent guest OS — and `vm`, a dedicated full VM. Both have so far meant remote infrastructure. A backend over a *local* hypervisor is the case that makes the whole platform axis load-bearing, because it is the first one where the guest OS is genuinely variable.

| Host | Hypervisor | Guest families it can serve |
| --- | --- | --- |
| Windows | Hyper-V, Windows Hypervisor Platform | Windows and POSIX |
| Linux | KVM, QEMU, libvirt | POSIX and Windows; also the only host where gVisor-class and Kata-class runtimes exist at all |
| macOS, Apple silicon | Virtualization.framework, Parallels, VMware Fusion, UTM | POSIX only in practice — an x86 Windows guest needs emulation |

Four consequences, all settled here.

**The rung is earned, unlike the one a plain container backend must refuse.** [`research/docker-backend-exploration.md`](research/docker-backend-exploration.md) rejects rounding a desktop container up to `microvm` because one VM hosts every container and the boundary between two sandboxes is namespaces. One VM per workload does not have that problem, so a local-hypervisor backend claims a hypervisor rung honestly.

**Which of the two rungs it claims depends on the image, and the standing rule is that a declared rung is a constant no configuration raises.** The same backend booting a stripped Linux image is `microvm`; booting a full Windows Server guest it is `vm`. Under the constant-rung rule one instance must declare the lower of the two, which strands a host that asked for `vm`.

**Fixing the guest per backend instance resolves the rung and the family together.** One instance, one template family: `os_families` is a constant, `isolation` is a constant, and the image is bound at construction — which is also the cheap branch of Decision 3's binding-time fork. A deployment serving both registers two instances. This is one rule doing three jobs, and it is the strongest argument for the per-instance discipline.

**The host OS becomes a constructor precondition rather than a documentation caveat.** Nothing in this stack reads the host's OS today outside one publishing script; the served-host matrix lives entirely in prose. A local-hypervisor backend must resolve at construction what its host can actually offer, because that determines its `os_families` — the same package cannot serve `WINDOWS` on Apple silicon and can on Linux.

## What this does not solve

**The host-tool transport over exec is the largest remaining item, and it is larger than this axis.** It cannot be raised into a protocol method, because it is a launcher and a supervisor rather than a primitive: `#!/bin/sh`, `command -v setsid`, `nohup sh -c`, `kill -KILL`. Its own docstring hands the problem to backends and no backend has taken it. Its cleanup is the one piece that does come out, as `reclaim`, which is the measure of how little of a launcher a primitive can carry. Making a second shim possible means first splitting the guest shim from the supervisor and giving the transport a way to be negotiated rather than assumed. **A Windows guest is not reachable until that work is done, whatever this axis declares.** What this axis buys in the meantime is that the mismatch is refused at attach instead of failing in the middle of a tool call.

**A probe is a fact at acquire, not a guarantee at exec.** It narrows the window rather than closing it, in the same way the reclaim surface already concedes for removal. A guest can lose a command between acquire and use, and confinement remains checked rather than held.

**Two refusal times are not one.** In the per-spec branch, a capability backed by a guest command is refused at acquire rather than at attach. That is inherent to the image not existing earlier, not a shortcoming of the design, but it does mean host developers see some misconfigurations one step later than others.

## Alternatives considered

**A three-value OS enum — `linux` / `macos` / `windows` — instead of a family.** Rejected. Once command availability is routed elsewhere, nothing left branches on Linux versus macOS; the one candidate, case sensitivity, is a filesystem property no OS name implies. A `linux` member would be reached for in order to reason about commands and would not answer that question, since distroless is Linux and has almost nothing in it. If an OS name is ever genuinely needed it arrives as a separate field, not as new members of this one — collapsing the two is what would force the expensive redefinition later.

**A scalar `platform` rather than a set.** Rejected. Correct for every backend shipping today and wrong for the first local-hypervisor backend, and widening it later is a redefinition rather than an addition.

**Declaring available commands on the backend.** Rejected twice over: no backend can honestly make the claim, because the same instance serves many images, and the router would be acting on an unverified string. The classification in Decision 2 removes the need.

**Probing instead of declaring.** Rejected as a replacement, adopted as a complement. A probe cannot answer at attach, because the image is not running; a declaration cannot answer per image. The two compose — the declaration is the ceiling and the probe narrows it — and neither substitutes for the other.

**Having kinds probe the guest at first use.** Rejected. It turns a configuration error into a runtime one and pays the cost inside a tool call. A probe at acquire is still before any tool call and amortises across a warm-reused sandbox.

**A `FilesystemTraits` declaration.** Rejected — Decision 4.

**Letting a configured hardened runtime or a booted image raise the declared rung.** Rejected, consistent with the existing ruling in [`backends/docker.md`](backends/docker.md). Per-instance backends give a deployment the same expressiveness without a rung that varies.

## Status

Decision 1 has shipped, and every real backend declares against it — `docker` off its daemon, `acas` and `wslc` as constants. The rows are in the order the work should be done, and the umbrella that carried all of them closed with the first, so each remaining row is pinned on its own.

| Decision | State | Tracking |
|---|---|---|
| `OsFamily`, the backend attribute, the spec field, and the `ensure_can_serve` clause | shipped exactly as designed — two members, a `frozenset` per backend instance, `requires_os_family` defaulting to `None`, `SandboxOsFamilyNotSupported` raised at attach and again in `acquire`; additive, and the neutrality test now pins a Windows-shaped `work_dir` as served. Released in `maf-sandbox` 0.20.0 | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) (closed) by [#532](https://github.com/sokolaidev/maf-extensions/pull/532) (merged); release [#542](https://github.com/sokolaidev/maf-extensions/pull/542) (merged) |
| The Docker backend reads its daemon's `OSType` at construction | shipped, with two amendments the row did not anticipate. The read is an `async` `create` factory rather than `__init__`, which makes no engine calls and could not do a bounded one on the caller's loop. And only `linux` is translated: a `windows` daemon declares `frozenset()`, since that backend's `sh -c`, its `rm -rf` and core's backslash-refusing path arithmetic could not serve a Windows guest whatever it declared. The declaration being a snapshot — the client re-resolves `DOCKER_HOST` and the context per invocation — is answered by re-asking on the create path | [#587](https://github.com/sokolaidev/maf-extensions/issues/587) by [#747](https://github.com/sokolaidev/maf-extensions/pull/747) |
| ACAS and wslc declare `POSIX` instead of asserting it in a comment | shipped — both state `{POSIX}` in their declarations, as constants: the service boots Linux microVMs and `wslc` runs Linux containers, so neither has a daemon to ask the way docker does. The `shlex.join` docstring now points at the declaration | [#588](https://github.com/sokolaidev/maf-extensions/issues/588) (closed) by [#946](https://github.com/sokolaidev/maf-extensions/pull/946) (merged) |
| Infrastructure commands raised into protocol methods, `reclaim` first | partial — `Sandbox.reclaim` ships and core spells `rm -rf` in neither place now. **The write-path filesystem path check is not a command core spells**, and the earlier reading of this row that said so was wrong: it is core's rule in `maf_sandbox.paths` over a stat each backend supplies, and three of the four answer it out of their own engine — a tar header, a data-plane stat, a dict lookup. One backend answers it inside the guest, which is the `maf-sandbox-wslc` row above and is a backend's claim rather than core's. The spec base is prepared at acquire through backend file planes. Per-call child creation is still a command, `mkdir -p` in `launcher_script` | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) for the reclaim; the vocabulary, the shared helpers, the contract and the guide have all landed, so the umbrella closes with the guide ([#585](https://github.com/sokolaidev/maf-extensions/issues/585) (closed), its last item [#735](https://github.com/sokolaidev/maf-extensions/issues/735) (closed)); the guest-answered case stays open at [#495](https://github.com/sokolaidev/maf-extensions/issues/495) (open) — whose third branch, keeping the probe and saying so, is now a supported spelling core ships rather than one a backend invents ([#733](https://github.com/sokolaidev/maf-extensions/issues/733) (closed)); base-directory preparation is shipped for [#466](https://github.com/sokolaidev/maf-extensions/issues/466) (closed) by [#1086](https://github.com/sokolaidev/maf-extensions/pull/1086) (merged); storage ownership and relative addressing are shipped in [#480](https://github.com/sokolaidev/maf-extensions/issues/480) (closed) by [#1090](https://github.com/sokolaidev/maf-extensions/pull/1090) (merged) |
| The static ceiling, then the acquire-time probe | shipped — backend-owned command checks narrow requested capabilities at acquire on Docker, WSLC and ACAS; successful checks are cached per physical sandbox. ACAS retains its observed-removal gate and capability-specific inconclusive-result policy. Cleanup capability negotiation remains separate | [#586](https://github.com/sokolaidev/maf-extensions/issues/586) (closed) by [#1089](https://github.com/sokolaidev/maf-extensions/pull/1089) (merged) |
| Splitting the guest shim from the supervisor, and negotiating the transport | shipped (the split) — the guest shim is a real, linted module with a tested wire-format contract; negotiation stays deferred until a second transport exists | [#357](https://github.com/sokolaidev/maf-extensions/issues/357) (closed) by [#590](https://github.com/sokolaidev/maf-extensions/pull/590) (merged), negotiation [#369](https://github.com/sokolaidev/maf-extensions/issues/369) (open) |
| A local-hypervisor backend | open — the consumer that makes every row above refuse something real | untracked; the nearest live candidate, [#382](https://github.com/sokolaidev/maf-extensions/issues/382) (open), is runtime-shaped and takes the branch that declares no family |
| There is no filesystem axis | settled — nothing to build | — |
