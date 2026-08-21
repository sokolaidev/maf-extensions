# Capabilities

> What a sandbox can *do*: the vocabulary, how it is declared and matched, and the full semantics of the file surface. Sources of record: [`research/files-out.md`](research/files-out.md) and [`research/two-axis-sandbox-policy.md`](research/two-axis-sandbox-policy.md).

## The vocabulary

`Capability` is a `StrEnum` with ten members. It is the second of the two axes — [`policy-isolation.md`](policy-isolation.md) holds the first — and it answers a different question: not *how strong is the boundary*, but *what is behind it*. Where the axis sits in the stack is [`architecture.md`](architecture.md); what a kind does with it is [`kinds/README.md`](kinds/README.md); how each backend implements it is [`backends/README.md`](backends/README.md).

| Member | What it gates | Declared today by |
|---|---|---|
| `EXEC` | Run a command line or argv — `Sandbox.exec` | docker, acas, wslc |
| `RUN_CODE` | Evaluate code in a language runtime, without going through a shell | nobody |
| `HOST_TOOLS` | Dispatch host-registered functions from inside the sandbox | docker, acas |
| `FILES_IN` | Write files in before execution — `Sandbox.write_file` | docker, acas, wslc |
| `FILES_OUT` | Stat and read back the paths a spec declared — `stat_file`, `read_file` | docker, acas |
| `FILES_LIST` | Enumerate a directory — `list_dir` | acas |
| `FILES_DELETE` | Delete a path and everything under it — `remove` | docker |
| `NETWORK` | Any egress at all; how precisely it is confined stays in `Egress` | nobody |
| `SNAPSHOT` | Snapshot and restore a sandbox for reuse | nobody |
| `ATTACHED_IDENTITY` | A platform-attached identity scoped to the sandbox itself | nobody |

`InProcessSandboxBackend` declares whatever a test claims, defaulting to the set below.

```python
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})
```

That is what every `Sandbox` already obligates, which is why silence resolves to it rather than to nothing.

**The admission test for a new member: name the backend that lacks it.** If none does, it is a comment, not a capability. Three times it has been asked, and once the answer was no:

- **`FILES_LIST` is split out of `FILES_OUT`** because **Docker has no engine-level primitive for enumerating a directory**. `docker cp <name>:<path> -` streams a tar and works against an image with no shell, and its first header block stats a *named* path — but discovering what names exist needs either an in-container `ls`/`find`, which depends on the image having one where the copy path does not, or tarring the whole directory to read its entry headers, which moves every byte a size cap exists to refuse. ACA Sandboxes enumerate natively. Two real backends differ, so there are two capabilities. One consequence decides the API shape: **globs require enumeration**, so a declared output is a literal path and patterns belong only to a kind that also requires `FILES_LIST`.
- **`FILES_DELETE` is split out of `FILES_IN`** because writing and removing are different powers, and a backend can honestly offer one without the other — docker declares it, acas implements the mechanism and withholds the promise, wslc has neither.
- **Widening `write_file` to `str | bytes` gets no capability**, because no backend lacks it: the ACAS SDK signature is already `content: str | bytes`, and docker and wslc both transport via tar, which is binary-native.

The two file-read capabilities side by side, since a kind chooses between them and the choice decides its portability:

| | `FILES_OUT` | `FILES_LIST` |
|---|---|---|
| Surface | `stat_file`, `read_file` | `list_dir` |
| What a kind must know | The names of its outputs, in advance | Nothing |
| ACAS | Native (`stat_file`, `read_file`) | Native (`list_files`) |
| Docker | Native — stat from the first tar header of `docker cp`, read from the same stream | Not without an in-image shell |
| wslc | Not served — `cp` has no container-to-stdout form, so there is no tar to read a header from | Not without an in-image shell |
| Who needs it | Every artifact-producing kind | A kind that must **discover** names nothing told it — which the CodeAct shape is not |

## Declared, required, matched

A backend declares `capabilities: frozenset[Capability]`; a spec declares `requires: frozenset[Capability]`; `SandboxRouter.ensure_can_serve` refuses `spec.requires - capabilities` with `SandboxCapabilityNotSupported`. The same check runs inside `acquire`, so a caller who skipped `ensure_can_serve` is refused too rather than served behind a capability set the spec never agreed to.

**Silence is read charitably here, and that is a claim about which kind of claim it is.** `capabilities` is read with `getattr(backend, "capabilities", DEFAULT_CAPABILITIES)`: a backend that never heard of the vocabulary still honestly does what `Sandbox` obligates, so the default is a *functionality* claim and costs nothing. `Egress` silence and `limits` silence are *safety* claims and resolve the other way — `Egress.UNDEFINED` is refused, and an undeclared ceiling is the default ceiling with a bigger ask refused.

**What ships is a match, not a search.** `SandboxRouter._resolve` runs once at construction: it takes the backend named by `selected`, or the first registered one, and checks the isolation floor. Nothing else participates in choosing. The host's outright denials, the effective floor, the capability match, the transfer ceilings and the egress rule then run **per spec** in `_refuse_unless_backend_can_serve`, in that order, and every one of them **raises**. So a host whose only backend lacks `FILES_OUT` gets an exception out of its agent factory rather than a quietly-unattached tool — and a host with two backends does not get the second one tried. The generalization two-axis proposes — `_resolve` picking the first backend satisfying floor ∧ capabilities ∧ egress, so one router could hold an in-process `RUN_CODE` backend beside a remote VM one — is **not implemented**; it is [#328](https://github.com/sokolaidev/maf-extensions/issues/328).

`denied_capabilities` is the posture counterpart, and it is not the same refusal: `SandboxCapabilityDenied` says *this host will not*, whatever a backend declares, and no backend property softens it. See [`hosts.md`](hosts.md) for what a host denies and why.

## The file surface

### Two flows, not one

A single capability creates two information flows that answer to different legs of a host's policy, and the spec keeps them apart rather than leaving it to convention:

- **Reading bytes the kind itself consumes** — a SARIF file parsed into diagnostics, a JSON result summarised into the tool result — is a **source**, and the question is *integrity*.
- **Landing an artifact in host state** is a **sink**, and the question is *confidentiality*: may a conversation this sensitive cause bytes to be written where they are going?

The source leg is already answered by the tool's own integrity declaration, and a kind running model-written code answers it by declaring nothing, so the untrusted default applies. Nothing answered the sink leg before this capability existed.

`OutputDisposition.CONSUME` and `OutputDisposition.LAND` are that distinction, declared. It is a *routing* distinction and nothing more — a `CONSUME` output is stat-ed, capped and counted exactly like a landing one. What follows from the sink leg — where artifacts land, name validation, the outbound confidentiality cap — is [`hosts.md`](hosts.md)'s.

**Two things `FILES_OUT` is not.** It is **not `HOST_TOOLS`**: nothing inside the sandbox dispatches the landing callback, the kind calls it host-side after the run, and the empty-dispatch property a CodeAct-class kind rests on survives intact — a host wanting a hard stop denies `FILES_OUT`. And it is **not a second write path into the file store**: a kind landing artifacts where the agent's own file tools write has handed model-written code an unapproved write, and one that can overwrite has given it a way to influence a *different* kind on the next call.

### Unpredictable output names do not need enumeration

The CodeAct shape looks like `FILES_LIST`'s constituency — a kind running model-written code cannot know what that code will write — and it is not. **A name unknown when the tool is *built* can still be known before the collection *runs*.** Two channels supply one, neither a directory listing: the model names its files in the tool call, or the program writes a manifest the kind reads at a path it chose itself. Both end in literal paths, which is all `FILES_OUT` needs. `FILES_LIST` is for a kind that must discover a name **nothing told it**, and a kind requiring it without needing it has made itself ACAS-only in the worst direction — refused at attach on a developer's Docker machine, attached in production.

The declaring channel also closes a trade the fixed-slot shape has to document: a program writing an artifact somewhere other than a declared path produces nothing collectable *and no error*, while a name declared before the run and absent after it is a diagnostic the kind hands back verbatim.

Two fields pay for it. `SandboxSpec.outputs_named_at_call_time` says *this workload lands artifacts it cannot name here*, and it is what keeps such a workload honest at attach: every attach-time question — is a sink required, does the outbound cap apply, must the backend serve `FILES_OUT` — is answered from the declarations, and a workload landing artifacts while declaring none would answer all three wrongly. `collect_outputs(outputs=...)` is refused without it. `DeclaredOutput.name` exists because `acquire` is get-or-create: a kind whose outputs would otherwise persist into the next round needs a per-call directory, and the guest path then carries a run id the host has no use for. `path` is what the backend reads; `name` is what the sink receives.

### The protocol

```python
class Sandbox(Protocol):
    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None: ...
    async def exec(self, command: str | Sequence[str], *, working_directory: str, timeout: float) -> ExecResult: ...
    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None: ...
    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes: ...
    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None: ...
    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]: ...
```

**`working_directory` is a parameter, exactly as it is on `exec`.** No sandbox object knows the spec's `work_dir` — the ACAS sandbox holds an SDK client, the wslc one a runner and a container name, `InProcessSandbox` a dict — and `work_dir` reaches a sandbox once per call or not at all. A pull surface without it would assign the confinement duty to a layer with no way to discharge it. `path` is POSIX-shaped and relative to `working_directory`; one resolving outside it is refused.

`stat_file` is **`lstat`-like**: the final component is described rather than refused, since `EntryKind.SYMLINK` is how a caller learns it is a link, and its parents are still walked because a stat through one reports a type and a size from outside the working directory even though no byte crosses. `list_dir` walks one component deeper than the others, because an enumeration passes through a link as readily as a read does, and a listed link is reported as `SYMLINK` rather than hidden — a name handed back with its type erased is a name read without the warning.

```python
@dataclass(frozen=True)
class DeclaredOutput:
    path: str                                            # literal, relative to work_dir; no globs
    disposition: OutputDisposition = OutputDisposition.LAND
    media_type: str | None = None                        # declared by the kind, never sniffed
    required: bool = True                                # missing required is an error
    name: str | None = None                              # the landing name; defaults to `path`
```

Each field earns itself. `path` is **literal** because a glob would have to be resolved by enumerating a directory, the one primitive Docker lacks. `media_type` is **declared, not sniffed**, because sniffing lets guest-produced content decide how the host handles it, and a kind knows what it renders. `required=False` **separates transport failure from workload failure**: a renderer exiting non-zero and producing no PNG is the normal path a model recovers from, and collection raising on top of it hands the model a transfer error where a diagnostic belongs. The spec field is `declared_outputs`, not `outputs`, because `InProcessSandbox.__init__` already takes `outputs=` meaning marker-keyed scripted stdout and the two would meet in one expression in every kind's tests; `outputs_named_at_call_time` is appended last on `SandboxSpec` rather than grouped where it reads better, because the dataclass is public and not keyword-only, so inserting a field would silently rebind a caller's positional `files_in` to a boolean.

```python
class EntryKind(StrEnum):
    FILE = "file"           # a regular file, the only kind read_file serves
    DIRECTORY = "directory"
    SYMLINK = "symlink"     # a link, junction or reparse point — never read, never walked through
    OTHER = "other"         # device, socket, fifo — or a link a backend cannot recognise

@dataclass(frozen=True)
class SandboxEntry:
    path: str               # relative to the working directory
    kind: EntryKind
    size_bytes: int | None  # None fails closed
```

`kind` is a **typed field, not a mode string to parse**: ACAS carries `is_directory` and nothing else about type, Docker's stat reads a tar header carrying the entry's real type flag and, for a link, the target name, and one vocabulary covers both. **`SYMLINK` is split out of `OTHER` because the parent walk needs a four-way answer** — regular file, directory (keep walking), link (an escape), anything else non-regular (an ordinary `ENOTDIR`). Both are refusals either way, so what the split buys is the *reason*, and the reason is what made the walk shareable: while the signal lived in a private per-backend flag the walk could only be written once per backend, which is exactly how two copies of it shipped. A Windows junction or reparse point maps to `SYMLINK`, not `OTHER` — for confinement it is an escape like any other link, and leaving it in `OTHER` would reintroduce the bug on a non-POSIX guest while looking correct. Two rejected shapes: `link_target` invites a reader to reason about where the link *goes*, a judgement made with the guest's filesystem in view; `is_symlink: bool = False` is a defaulted boolean that can simply not be read, and makes `kind=FILE, is_symlink=True` representable.

**`size_bytes: None` fails closed.** ACAS's stat payload reports size as an optional integer, and coercing unknown to `0` would make a size cap read that file as free, while passing on a negative would clear the pre-read check and then *reduce* the collection's running total. An entry whose size cannot be determined is refused rather than read. A link's size is `None` for a second reason: what a stat reports for one is the length of the target *string*, not of anything readable.

### Adding to `Sandbox` is a breaking change, and that is the choice

`Sandbox` is `@runtime_checkable`, which enforces member *presence* — the reason `capabilities` is read off a backend with `getattr` rather than declared as a Protocol member. Four members went onto `Sandbox` anyway, and it is safe here for a stated reason: **no production path in this repository calls `isinstance(x, Sandbox)`**; three tests do, deliberately, and the only other protocol `isinstance` checks are against `SandboxBackend`. The members belong on the Protocol because a sandbox's file surface is what the type exists to describe, and hiding them behind `getattr` would make every kind feature-detect. It is nonetheless **breaking for out-of-tree implementers** — an existing `Sandbox` stops satisfying the protocol the day it lands — so it ships as `feat!` at 0.x, and "no backend declares the new capabilities yet, so nothing changes behaviourally" is true of the router's match and false of the protocol.

## Caps

```python
@dataclass(frozen=True)
class TransferLimits:
    max_bytes_per_file: int
    max_total_bytes: int
    max_files: int

    def within(self, ceiling: TransferLimits) -> bool: ...   # every field at or below

DEFAULT_TRANSFER_LIMITS = TransferLimits(max_bytes_per_file=8 * MiB, max_total_bytes=32 * MiB, max_files=64)
```

**All three fields are load-bearing**: a byte ceiling alone does not bound a collection, since ten thousand files one byte under the per-file cap cost exactly what the cap was written to prevent. A spec carries one `TransferLimits` per direction (`files_in`, `files_out`); a backend declares a `SandboxLimits`, which is the pair.

**The invariant that keeps the axis inert: the spec-side default and the backend-side silent default are the same constant.** `within()` is then satisfied by equality and nothing already written starts being refused. Get it wrong in the other direction — a spec default above what a silent backend is assumed to allow — and *every* spec fails at attach, including the published-wheel smoke test's. A test asserts `DEFAULT_TRANSFER_LIMITS.within(DEFAULT_TRANSFER_LIMITS)` so the two cannot drift apart. A `limits` this package cannot read at all is refused rather than guessed at, with the adjacent mistake named in the message: `TransferLimits` is one direction and `SandboxLimits` is the pair, and the wrong one used to surface as a bare `AttributeError` out of a host's agent factory.

**Enforcement is stat-then-read, with stream counting as the fallback rather than the rule.** The ACAS SDK's read does `await response.read()` internally — fully buffered, no incremental hook — so stat is the only enforcement available there; Docker's first tar header returns a size before any content byte moves and can also count tar bytes on the way out as a second line. So: **stat, refuse if over cap or unknown, then read**; a backend that can additionally abort mid-transfer should, and none is required to. `read_file` takes `max_bytes`, and the caller passes the stat-ed size clamped by what the collection has left. It is a **refusal, never a truncation** — half a PNG returned as success is an artifact the host cannot tell from a whole one.

**The caps are re-applied to the bytes that actually arrived, not only to the stat-ed sizes.** A stat is a promise about a file the guest can still rewrite before the read reaches it, and the guest is the thing the sandbox exists to contain; checking once would make the whole cap advisory against exactly the adversary it is written for. A breach **fails the whole collection with no partial delivery**, because a partial artifact set reported as success is worse than none — the model cannot tell what it did not get.

**`files_out` bounds the collection the spec *declared*, not the subset that lands.** A `CONSUME` output is counted against all three fields: same guest, same filesystem, same bytes leaving the sandbox, and exempting them would make every cap opt-out, since a spec declaring everything `CONSUME` would be uncapped. What collection does *not* do for one is read it — a kind reading its own `CONSUME` output passes `max_bytes` itself and owns that read's bound.

**Backend maxima follow the safety-claim silence rule**, not the capability one: an undeclared `limits` resolves to `DEFAULT_SANDBOX_LIMITS` and a bigger ask is refused with `SandboxTransferLimitsNotPermitted`. That makes `limits` the third optional backend declaration read with `getattr`, beside `capabilities` and `egress`. **Three is where the pattern stops**; a fourth is the signal to collapse all of them into one optional declarations object.

## `write_file` widens; no capability for it

`write_file` takes `(path: str, content: str | bytes, *, working_directory: str)`, with `str` continuing to mean UTF-8 whatever the host's locale says. The in-door otherwise cannot carry a PNG or a spreadsheet. It gets no capability by the admission test above. It uses the same POSIX grammar and `working_directory` confinement as the read surface, including refusal of lexical escapes, symlinked parents, and a link at the leaf; parent directories are created as needed, and a missing component ends the walk, so nothing this call creates can be a link. The walk and the write are not atomic on any shipped backend: a guest that turns a checked component into a link in between wins.

## `FILES_DELETE`

`Sandbox.remove(path, *, working_directory, recursive=False)` deletes `path` and, when `recursive`, everything under it. Three rules a caller depends on, and the confinement duty of the pull surface:

- **A path that is not there is success.** Cleanup runs in a `finally` and must not report a second failure over the first.
- **A link is removed, never followed.** Resolving one would unlink a target outside the boundary, and no byte has to come back for the damage to be done.
- **A directory is refused without `recursive`, empty or not.** `recursive` is a word the caller has to say, because the alternative is an irreversible operation that reads like a single-file delete at the call site — and the empty case is not carved out, because a backend with no enumeration primitive cannot tell an empty directory from a full one.
- `ValueError` for a path outside `working_directory`, one reached through a link, or the working directory itself; `OSError` for a directory without `recursive` or a removal the guest refused; `NotImplementedError` when the backend does not declare the capability — **require it rather than catching it**.

`conformance.FILES_DELETE_PROBES` is the de facto spec, and what it obligates is legible from what it attacks. Ten probes: a removal removes (the positive control — a backend that removed nothing would pass every refusal probe) *and* leaves a bystander file standing; a missing path is success, run as the two-call shape a `finally` cleanup actually produces; a link is removed and not followed, on both flag values because `recursive` may select a different operation entirely; a path through a linked parent is refused, the same walk the pull surface keeps; a directory needs `recursive`, and **an empty directory needs it too**, stated as its own probe because it is the case a backend could quietly carve out as an implicit `rmdir`; recursive removes the tree; **a link *inside* a recursive removal is unlinked, not followed**, which is the escape the tree probe cannot see — a service-side tree delete that resolves an interior link deletes a file outside the working directory; the working directory itself is refused, with a file in it asserted still present afterwards, because a backend that removed it and *then* raised would pass a bare refusal check having taken the next run's ground with it; and a path outside is refused, the boundary needing no link to be crossed.

Who declares it: **docker yes; acas no; wslc no.** The ACAS backend implements `remove` through the data plane's own `delete_file` and withholds the capability, because it *refuses* a link where the protocol says a link is removed — the service follows a link in the final component as much as in the parents, and an HTTP `DELETE` promises nothing else, so removing one could delete what the guest pointed at. `conformance.measure_files_delete_probes` exists for exactly that gap: it runs every probe with no declaration gate and no verdict, so a mechanism can be measured before anything declares it — otherwise the gate could never open, since every conformance entry point refuses an undeclared subject. Its live suite runs the measurement and classifies each probe into expected-pass or expected-fail, so an unclassified finding cannot sit green.

**No shipped kind requires `FILES_DELETE`,** and the framework's own call-directory reclaim does not use `remove`. `maf_sandbox._reclaim.reclaim_guest_path` runs `rm -rf` over `EXEC`, because that is the one removal every backend serving a kind can do today. It carries two independent guards, since a recursive delete is irreversible and neither should depend on the caller having derived the path correctly: the working directory itself is refused, and so is any path with fewer than two components. It runs with `working_directory="/"`, since the target is absolute and no backend creates a spec's work dir. Which mechanism belongs there is [#477](https://github.com/sokolaidev/maf-extensions/issues/477).

## Confinement

Reads are confined to the working directory, and the confinement that matters is not the one on the argument string:

```python
os.symlink("/", "/maf-sandbox/work/out/root")           # inside the guest, one line of the program
```

A reader that follows that link reads whatever *it* can see — on a backend streaming from inside the guest, the guest's filesystem; on a sync-mount backend, the **host's**. So **only regular files are ever read, and a symlink is refused whether or not its target would have resolved somewhere legitimate**: there is no case where a kind needs to follow a link a plain file would not serve, and "the target is inside the working directory" is a judgement made with the wrong filesystem in view.

| Backend | Mechanism | Strength |
|---|---|---|
| Docker | The first tar header of `docker cp` carries the entry's real type flag **and**, for a link, the target name; `docker cp` without `-L` then tars the link *entry* rather than the target's bytes | Strongest — two independent signals read from the same tar stream, both verified against a live engine |
| ACAS | The data plane's stat payload carries `isSymlink` and `symlinkTarget`; the SDK's typed `FileInfo` drops both, so the backend reads the raw payload | Middling — one explicit flag, from an undocumented preview shape, and the read follows links |
| wslc | **None available.** `cp` reports success and writes a **0-byte file** for a symlink — neither preserved, followed, nor refused, and indistinguishable from a legitimately empty artifact | Cannot meet the rule; the backend does not serve `FILES_OUT` |

A payload omitting either flag is refused rather than assumed regular. That the reference backend's defence rests on an undocumented shape it reads past its own SDK to reach is acceptable only stated plainly, and it is filed upstream — see the status table.

**Classifying the last component is not enough on any of them.** A symlinked *parent* is invisible in the final entry's stat: with `out -> /etc`, `out/hostname` is a regular 12-byte file. That rule failed twice as prose — two backends independently shipped the same escape — so it is a function: **`maf_sandbox.paths.refuse_symlinked_parents` is the walk itself**, taking a backend's own **unconfined, no-follow** stat — those two properties are the trap, since a confined stat cannot reach the work dir's ancestors and a following one describes the target instead of the link. It walks from the filesystem root down, above the working directory rather than at it, because a nested work dir has ancestors the guest can replace. Only a link is a confinement failure; any other non-directory is an ordinary `ENOTDIR`. Neither API offers a no-follow read, so a guest that swaps a stat-ed component between the walk and the read is followed; that residual is stated rather than closed.

### Refusing a symlink is not the same as proving a regular file

A backend that identifies links and directories has narrowed an entry to *not one of those* — FIFOs, sockets and device nodes are none of the three. Docker can prove regularity: the tar header carries the real type, so a FIFO is `OTHER` and refused. **ACAS cannot.** Its payload has `isDir` and `isSymlink` and nothing else, and `mode` is permission bits with the type bits stripped, so a FIFO is reported *identically* to an empty regular file and is classified `FILE`. Verified live, that is not merely a mislabelling: `read_file` on a FIFO **never returns**, so a guest putting one where a declared output belongs would hold the caller's turn open indefinitely. Nothing available closes it — `exec` with `test -f` would reintroduce the in-image shell dependency the `FILES_LIST` split exists to avoid, on exactly the minimal images where the file API is most useful — so that backend **bounds the read**, turning an indefinite hang into a refusal in the output-error family. Stated plainly because a kind author is entitled to know it: **on ACAS, `EntryKind.FILE` means "not a directory and not a symlink"**, and a read of one can fail on a timeout no cap or size predicted.

### `maf_sandbox.conformance` is the executable spec

The rule above is also a suite. The probes plant a hostile layout through a backend's own public surface — a link to a sibling of the working directory, a link as a final component, a regular file standing where a directory was expected — and attack it at `stat_file`, `read_file` and `list_dir`, since the duty lives at all three. **Every probe carries the reason it exists**; a failure names every probe that failed rather than the first; and a probe requiring a capability the backend never declared is **skipped rather than failed**. Planting is a subject method, because creating a link is the guest's move and a sandbox offering one would hand the attacker the tool. It imports no test framework: the module ships in the wheel.

Two things it deliberately does not do. It does not prove the **premise** — that the provider really resolves through a link, so the refusals are refusing something reachable — because establishing that means looking under a backend's own public surface, and only that backend can; each keeps that test at home. And it **grades nothing**: a backend that cannot recognise a link still refuses every path attacked and fails only the two probes about *naming* what it refused. That is a gap, not a tier.

Where each backend answers, stated because the legs are not equal:

| Leg | Where it runs | What a green means |
|---|---|---|
| docker | Against a **real engine**, in the `docker-e2e` job, on every pull request | The provider's real behaviour, on the backend a contributor can also run locally |
| acas | Against the **real service**, in `verify-live.yml`'s ACAS job — on demand and after a release, since every sandbox is a billable resource — plus a fake on every pull request | The only place the four `FILES_LIST` probes have ever met a real provider; the suite asserts none of them skipped, because a run that skipped them reports the same success as one that ran them |
| in-process fake | The core suite | **Shape, not safety.** It runs the shared walk and refuses a seeded link standing where a directory was expected, but a seeded link has no target and nothing reads through one |

## Cross-platform rules

Every shipped backend runs a Linux guest today. That is an observation, not a protocol assumption. The tempting claim — the protocol states one grammar and backends translate to whatever their guest is, by analogy with `exec` taking an argv *sequence* — **does not hold**: a sequence protects against quoting, not against paths inside the arguments, and a kind derives absolute guest paths from `work_dir` and passes them straight into argv. A backend can translate a path it is given *as* a path; it cannot find one buried in an opaque argv without parsing arbitrary command lines.

- **`work_dir` is guest-native, not protocol-normalized.** The host states it to match the image it configured and nothing rewrites it. `/maf-sandbox/work` is a default, not a requirement.
- **Declared output paths and artifact names are POSIX-shaped**, and a backslash in one is refused — not because the guest is Linux, but because these are the paths the library itself resolves and it has one grammar. Nothing builds a guest path with `os.path` or `pathlib`; `posixpath` only.
- **UTF-8 is the interchange form for names.** Linux filenames are byte strings and can be invalid UTF-8; such a name is refused rather than round-tripped. This only arises on the `FILES_LIST` road, since declared paths are authored by the kind.
- **`str` content means UTF-8, always**, independent of host locale. Any path reaching a platform default encoding is a mojibake bug waiting for a Windows host.
- **No newline translation, in either direction, ever.** A host writes artifact content in binary mode: `open(path, "w")` on Windows turns `\n` into `\r\n` and corrupts a PNG that was byte-exact when it left the sandbox.

That a kind can depend on its guest's OS while no axis declares or matches one is a real gap, kept deliberately additive so a platform axis lands without a breaking change.

## The rest of the vocabulary

**`RUN_CODE`** — evaluate code in a language runtime without going through a shell, the CodeAct verb. No shipped backend serves it, and today it gates no method; the contract is fixed in [`research/hyperlight-backend-proposal.md`](research/hyperlight-backend-proposal.md) as `run_code(code: str, *, timeout: float) -> ExecResult`, with a backend that raises from `exec` outright. The matcher question is the sharper one: `maf-sandbox-codeact` hard-requires `EXEC`, so a `RUN_CODE`-only backend cannot serve it however complete it is. Whether that is answered by a disjunction in the match or by a second spec is [#425](https://github.com/sokolaidev/maf-extensions/issues/425).

**`NETWORK`** — declared by no backend and required by no spec anywhere in the repository. Its one live mention is a comment in `samples/09_inprocess_bicep`: a backend with no boundary honestly cannot confine egress, and the router refuses `Egress.UNRESTRICTED` for every workload today, so that sample declares `Egress.CLOSED` to pass a gate it does not enforce. The intended use is the gate that would let it declare `UNRESTRICTED` honestly — permitted for workloads that do *not* require `NETWORK`. Until then the member is vocabulary with no reader, and [`network.md`](network.md) holds the axis that does the work.

**`SNAPSHOT`** — snapshot and restore for reuse. No shipped backend; the hyperlight proposal declares it and it is load-bearing there, as both the warm-reuse mechanism and the recovery from a poisoned sandbox.

**`ATTACHED_IDENTITY`** — the vocabulary shipped with the enum; the plumbing did not. See [`hosts.md`](hosts.md) for the identity axis and what a spec carrying it would owe.

## Error taxonomy

Named exceptions under **one base**, `SandboxOutputError`, so backends do not diverge and a kind can map failures to messages. A kind that only needs to tell the model "the artifacts did not come back" catches the base; one that wants to name what went wrong catches a member.

**The base class is a promise about coverage, not a family resemblance.** A backend answers in its own vocabulary — a bare `ValueError` for a path it would not resolve, a bare `FileNotFoundError` for a file the guest deleted between the stat and the read — and a kind told to catch one base class would never see either. So the collection **translates** what the pull surface raises into the family, keeping the original as `__cause__`: a `ValueError` becomes `SandboxOutputNotConfined`, an `OSError` becomes `SandboxOutputUnreachable`. That is what makes the family exhaustive rather than merely typical. **The code states no count** — enumerating the members anywhere is how the list drifts.

## Status

| Decision | State | Tracking |
|---|---|---|
| Ten-member `Capability`; `DEFAULT_CAPABILITIES = {EXEC, FILES_IN}` | shipped | — |
| `FILES_OUT` rollout: protocol, glue, docker, acas, `samples/07_docker_diagram`, codeact file store | shipped (items 1–5b) | [#109](https://github.com/sokolaidev/maf-extensions/issues/109) open |
| wslc serves `FILES_OUT` | deferred — no container-to-stdout form to read a tar header from | [#125](https://github.com/sokolaidev/maf-extensions/issues/125) open; upstream [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309), [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310), both open |
| Selection is name-or-position + floor; the capability match raises | shipped | — |
| `_resolve` picks a backend per spec (floor ∧ capabilities ∧ egress) | open | [#328](https://github.com/sokolaidev/maf-extensions/issues/328) open |
| `FILES_DELETE` vocabulary, `remove` contract, and the ten probes | shipped | — |
| Which mechanism reclaims a call directory — `rm -rf` over `EXEC` today | open | [#477](https://github.com/sokolaidev/maf-extensions/issues/477) open |
| ACAS declares `FILES_DELETE` | open — the mechanism refuses a link where the protocol removes it; measured without a verdict in the live suite | untracked |
| `RUN_CODE` gates a method | open | [#381](https://github.com/sokolaidev/maf-extensions/issues/381) open |
| codeact on a `RUN_CODE`-only backend: matcher disjunction or a second spec | open | [#425](https://github.com/sokolaidev/maf-extensions/issues/425) open |
| A backend serving `RUN_CODE` and `SNAPSHOT` | open | [#382](https://github.com/sokolaidev/maf-extensions/issues/382) open |
| ACAS reads the raw stat payload because the SDK's `FileInfo` drops `isSymlink` | open, upstream | [#136](https://github.com/sokolaidev/maf-extensions/issues/136) open |
| On ACAS `EntryKind.FILE` means not-a-directory-and-not-a-symlink; a FIFO read is bounded rather than refused | open, upstream — no signal available to close it | upstream [microsoft/azure-container-apps#1807](https://github.com/microsoft/azure-container-apps/issues/1807) open |
| `NETWORK` becomes matchable — `Egress.UNRESTRICTED` for specs that do not require it | open | [#265](https://github.com/sokolaidev/maf-extensions/issues/265) open |
| `ATTACHED_IDENTITY` plumbing behind the vocabulary | open | [#395](https://github.com/sokolaidev/maf-extensions/issues/395) (open), [#396](https://github.com/sokolaidev/maf-extensions/issues/396) (open), body in [`hosts.md`](hosts.md) |
| A guest-OS axis, declared and matched | open | [#111](https://github.com/sokolaidev/maf-extensions/issues/111) open |
| Error taxonomy members as types under one base | shipped — settled in code, not by role | untracked |
| Batch `deliver` granularity instead of one call per artifact | open | untracked |
