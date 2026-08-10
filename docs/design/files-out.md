# `FILES_OUT`: getting artefacts out of a sandbox

> **Status: PROPOSED** — tracking issue [#109](https://github.com/sokolaidev/maf-extensions/issues/109). The baseline is [`sandbox-architecture.md`](sandbox-architecture.md); this document specifies rollout item 4 of [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md), whose `FILES_OUT` paragraph is a sketch and is superseded here.

## What is missing, and why it is not symmetrical with `FILES_IN`

A workload today can put files into a sandbox and run a command; its only way back is `ExecResult.stdout`. Every kind whose product is an artefact — a rendered image, a generated archive, a transformed dataset — is either unwritable or forced into a base64-on-stdout convention, and such a convention does not stay a workaround: the tool description teaches it, the model depends on it, and it outlives the protocol gap by years.

The obvious framing — "`FILES_IN` already works, do the same thing backwards" — is wrong in all three of the ways that matter.

| | `FILES_IN` | `FILES_OUT` |
|---|---|---|
| Who chooses the **name** | The host: only a path present in `WorkspaceContext.list_files` is ever substituted, which is the injection-pinning boundary | The **guest**: whatever ran inside decided, which for a CodeAct-class kind means the model decided |
| Who chooses the **content** | The host: bytes already inside the trust boundary | The guest: bytes produced by the thing the sandbox exists to contain |
| Where it **ends up** | Inside the sandbox, which is disposable | Host state — a transcript, a workspace, a blob container — which is not |

So this is the first channel in the stack where guest-chosen names and guest-produced bytes reach host state. Everything below follows from that sentence.

## Two flows, not one

A single capability creates two information flows that answer to different legs of a host's policy, and conflating them is the mistake this document is arranged to prevent:

- **Reading bytes the kind itself consumes** — a SARIF file parsed into diagnostics, a JSON result summarised into the tool result — is a **source**. The question is *integrity*: is what came back trustworthy? Already answered by `sandbox_tool_declarations(source_integrity=...)`, and a kind running model-written code answers it by declaring nothing, so the untrusted default applies.
- **Landing an artefact in host state** is a **sink**. The question is *confidentiality*: may a conversation this sensitive cause bytes to be written where they are going? Nothing answers that today.

The two are distinguished in the spec, not left to convention — see `OutputDisposition` below.

## Two capabilities, not one

`FILES_OUT` is **read a path the spec declared**. Open-ended discovery — "tell me what is in this directory" — is a *separate* capability, `FILES_LIST`, and the split is the load-bearing decision in this document.

The reason is a backend, not a preference. **Docker has no engine-level primitive for listing or stat.** `docker cp <name>:<path> -` streams a tar out and works against an image with no shell at all, but finding out *what* is in a directory requires either an in-container `ls`/`find` — which depends on the image having one, while the copy path does not — or tarring the whole directory to read its entry headers, which transfers every byte including the one the size cap exists to refuse. Requiring discovery would make the backend most people run locally and in CI either fragile or cap-hostile.

ACA Sandboxes, by contrast, expose `list_files` and `stat_file` natively. So the two capabilities describe a real difference between real backends, which is the test worth applying to any future member: **name the backend that lacks it.** If none does, it is a comment, not a capability. (This is why widening `write_file` to `str | bytes` — below — gets no capability of its own: every backend can already do it.)

One consequence decides the API shape: **globs require listing.** `*.png` cannot be resolved without enumerating a directory, so a glob-accepting spec would silently reintroduce the primitive Docker lacks. Declared outputs are therefore **literal relative paths**; patterns are available only to a kind that also requires `FILES_LIST`.

| | `FILES_OUT` | `FILES_LIST` |
|---|---|---|
| Surface | `read_file(path) -> bytes` | `list_dir(path) -> tuple[SandboxEntry, ...]` |
| What a kind must know | The names of its outputs, in advance | Nothing |
| ACAS | Native | Native |
| Docker | Native (`docker cp`) | Not without an in-image shell |
| wslc | Native (tar-out) | Not without an in-image shell |
| Who needs it | Every artefact-producing kind | Kinds whose output names are unpredictable — the CodeAct shape |

## The protocol

### `FILES_OUT` — declared outputs

```python
class OutputDisposition(StrEnum):
    LAND = "land"        # goes to the host's OutputSink; the model gets a reference
    CONSUME = "consume"  # goes to the kind, which parses it; never reaches the sink

@dataclass(frozen=True)
class DeclaredOutput:
    path: str                                            # literal, relative to work_dir; no globs
    disposition: OutputDisposition = OutputDisposition.LAND
    media_type: str | None = None                        # declared by the kind, never sniffed
    required: bool = True                                # missing required is an error; missing optional is absence

class Sandbox(Protocol):
    async def read_file(self, path: str) -> bytes: ...
```

Four decisions, each paying for itself:

- **`disposition` puts the two flows in the spec.** A PNG lands; a SARIF file is parsed by the kind and must never reach the sink, because it is the tool's own input rather than its product. Putting it in the spec is what lets the glue apply the sink's confidentiality declaration to `LAND` outputs only, rather than to everything the kind touches.
- **`media_type` is declared, not sniffed.** Sniffing lets guest-produced content decide how the host handles it. A kind knows what it renders.
- **`required` separates transport failure from workload failure.** `dot` exiting non-zero and producing no PNG is the *normal* path a model has to recover from; if collection raises on top of it, the model gets a transfer error where it should get a diagnostic. An artefact-producing kind marks its output optional for exactly this reason.
- **Paths are relative to `work_dir`**, not to a dedicated output subdirectory. A separate subdir isolates outputs from inputs but breaks the common case where a compiler writes its report beside the source.

### `FILES_LIST` — discovery

```python
class EntryKind(StrEnum):
    FILE = "file"           # a regular file, the only kind that may be read
    DIRECTORY = "directory"
    OTHER = "other"         # symlink, junction, reparse point, device, socket, fifo — never read

@dataclass(frozen=True)
class SandboxEntry:
    path: str               # relative to the listed directory
    kind: EntryKind
    size_bytes: int         # 0 for anything that is not a FILE

class Sandbox(Protocol):
    async def list_dir(self, path: str) -> tuple[SandboxEntry, ...]: ...
```

`list_dir`, not `list_files`: `WorkspaceContext.list_files` is the host's allowlist and the most trusted enumeration in the system, while this is the least trusted one, and both are in scope inside a kind's tool body. Giving them one name makes confusing them a security bug that reads perfectly well.

`kind` is a **typed field**, not a mode string to parse. That matters beyond tidiness: ACAS's `FileInfo` carries `is_directory` — a two-way split with no symlink flag — and leaves type information in `mode: str | None`, which is Unix-shaped and can be absent. `OTHER` also absorbs Windows junctions and reparse points, so the vocabulary survives a non-POSIX guest.

### Caps

```python
@dataclass(frozen=True)
class TransferLimits:
    max_bytes_per_file: int
    max_total_bytes: int
    max_files: int

    def within(self, ceiling: "TransferLimits") -> bool:
        """Every field at or below ``ceiling``'s — the match ``ensure_can_serve`` applies."""
```

`SandboxSpec` carries one per direction (`files_in`, `files_out`), defaulted to named constants. All three fields are needed: a byte cap alone does not bound a collection, since ten thousand files one byte under the per-file ceiling cost exactly what the ceiling was written to prevent.

Enforcement is **stream-counted and fail-closed**. On the `FILES_OUT` road there is no listing and therefore no size known in advance, so a backend counts bytes as they arrive and aborts past the cap — Docker's case is literally "count tar bytes and kill the `cp` subprocess". Two consequences follow, and both are deliberate: the cap binds **transfer** bytes rather than content bytes (a tar is larger than what it carries, and requiring exact content accounting up front would demand a stat that Docker does not have), and a breach **fails the whole collection with no partial delivery**. A partial artefact set reported as success is worse than none, because the model cannot tell what it did not get. Where `stat` exists, pre-checking is an optimisation, never the contract.

### Backend maxima, and which silence rule they follow

A backend declares ceilings as `limits: SandboxLimits` (a `TransferLimits` per direction); `ensure_can_serve` refuses a spec asking above them, and the backend enforces the *spec's* cap at runtime.

What an *undeclared* `limits` means is not the rule `capabilities` uses. `Capability` silence is read charitably (`DEFAULT_CAPABILITIES`) because it is a functionality claim: a backend that never heard of the vocabulary still honestly does what `Sandbox` obligates. `Egress` silence is read as `UNRESTRICTED` and refused, because it is a safety claim: a backend written before the property existed cannot have been enforcing an allowlist it never read. **Transfer limits are a safety claim**, so silence resolves to conservative named defaults and a bigger ask is refused. Nothing regresses: no shipped backend declares `FILES_OUT`, so none is selected for a spec requiring it until it has been written to.

This is the third optional backend declaration read with `getattr` (`capabilities`, `egress`, now `limits`), for the reason recorded on `SandboxBackend`: `runtime_checkable` enforces member *presence*, so a new Protocol member would stop every backend written before it from being a `SandboxBackend` at all. Three is where the pattern stops. A fourth is the signal to collapse all of them into one optional declarations object.

### `write_file` widens; no capability for it

`write_file` becomes `(path: str, content: str | bytes)`, with `str` continuing to mean UTF-8. The in-door otherwise cannot carry a PNG or a spreadsheet, and this release already has every backend's file code open — a second breaking change to the same member is what is worth avoiding.

It gets no capability, by the test above: ACAS's SDK signature is already `content: str | bytes`, and Docker and wslc both transport via tar, which is binary-native. No backend lacks it.

### Error taxonomy

Named exceptions, so backends do not diverge and a kind can map failures to messages: a declared output that is absent, a path resolving outside `work_dir`, a non-regular entry refused, a cap breached (naming which cap and which file), and a sandbox that has gone away. Provider and transport detail stays in the log under `error_detail`, never in the tool result.

## Confinement

Reads are confined to `work_dir`, and the confinement that matters is not the one on the argument string:

```python
os.symlink("/", "/work/out/root")           # inside the guest, one line of the program
```

A reader that follows that link reads whatever *it* can see — on a backend that streams from inside the guest, the guest's filesystem; on a sync-mount backend, where the output directory is a host directory the guest writes into, the **host's**.

Only regular files are ever read. A symlink is refused whether or not its target would have resolved somewhere legitimate: there is no case where a kind needs to follow a link that a plain file would not serve, and "the target is inside `work_dir`" is a judgement made with the wrong filesystem in view. **How** that rule is honoured differs by backend, and the difference is not cosmetic:

| Backend | Mechanism | Strength |
|---|---|---|
| Docker | `docker cp` without `-L` tars the link *entry*, not the target's bytes; refuse on the tar type bit | Structural — safe by default |
| wslc | Tar-out, same type bit | Structural — safe by default |
| ACAS | `read_file(path)` has no non-following variant and returns bytes with no type, so the backend must `stat_file` first and parse `mode` | Weaker — **fail closed when `mode` is `None`** |

The reference backend is the weak one, and its defence is a parse of an undocumented preview-SDK field. That is acceptable only stated plainly and paid for with the extra round trip; it should also be raised upstream, because `FileInfo` ought to carry the entry type as a field rather than leaving it in a mode string.

## Cross-platform rules

Every shipped backend runs a Linux guest today. That is an observation, not a protocol assumption — Windows-guest backends are plausible, so the protocol states one grammar and **backends translate to whatever their guest actually is**. This is the same shape as `exec` taking an argv *sequence* rather than a command line: the protocol states intent, the backend does the platform-correct thing (ACAS's `shlex.join` is correct precisely because it is a backend-scoped claim about a Linux guest).

- **Paths are POSIX-shaped in the protocol**, and a backslash in a declared path is refused — not because the guest is Linux, but because the protocol has one grammar and `\` is not a separator in it. Nothing builds a guest path with `os.path` or `pathlib`; `posixpath` or plain joins only. Kinds refer to outputs relatively wherever they can, which keeps `work_dir`'s value out of their business and makes a non-POSIX guest a translation layer rather than a protocol change.
- **UTF-8 is the interchange form for names.** Linux filenames are byte strings and can be invalid UTF-8; such a name is refused rather than round-tripped, since it cannot survive a JSON data plane in any case. This only arises on the `FILES_LIST` road — declared paths are authored by the kind.
- **`str` content means UTF-8, always**, independent of host locale. Any code path that reaches a platform default encoding is a mojibake bug waiting for a Windows host.
- **No newline translation, in either direction, ever.** A host must write `Artefact.content` in **binary** mode: `open(path, "w")` on Windows turns `\n` into `\r\n` and corrupts a PNG that was byte-exact when it left the sandbox.

A spec-level platform axis — so a kind that execs `python3` is refused on a Windows guest rather than failing at exec — is **deliberately not here**. By the capability test, no backend lacks it yet. The obligation this document takes on is narrower: keep the normative text platform-neutral so the axis can be added without a breaking change.

## Where artefacts land

### The host writes; the library never does

A workspace store, a blob container, a UI artifacts panel, a scratch directory — the destination is a property of the application, and `maf_sandbox` is stdlib-only by design. The host supplies a callback, which is the pattern `WorkspaceContext` already is:

```python
@dataclass(frozen=True)
class Artefact:
    """One file pulled out of a sandbox, on its way to the host."""
    name: str                    # validated; relative; derived from the declared output
    content: bytes
    kind: str                    # spec.kind, so a host can route by workload
    media_type: str | None       # as declared by the kind

@dataclass(frozen=True)
class LandedArtefact:
    """Where the host put it."""
    name: str                    # echoes what was asked for
    display: str                 # safe for the transcript
    handle: str | None = None    # host-internal: URL, blob key, id — never auto-rendered

class NameNormalization(StrEnum):
    NFC = "nfc"                  # default
    NONE = "none"                # byte-exact

@dataclass(frozen=True)
class OutputSink:
    deliver: Callable[[Artefact], Awaitable[LandedArtefact]]
    max_allowed_confidentiality: str | None = None
    normalization: NameNormalization = NameNormalization.NFC
```

`collect_outputs(...) -> tuple[LandedArtefact, ...]`, frozen and ordered, matching `egress_allow` and the entry listing.

**The typed return is a security property, not tidiness.** If `deliver` returned one string and the kind put it in the tool result, whatever the host handed back would enter the transcript verbatim — and for a blob container that may be a SAS URL with a bearer token in the query string, persisted and replayed on every subsequent turn. `display` is what the model sees; `handle` is the host's own reference and is never rendered anywhere by this library.

**A missing sink refuses, for a kind whose product is the artefact.** This is the distinction `sandboxed_tool` already draws: nothing-configured attaches `[]` quietly, but cannot-honour-the-spec raises. A diagram kind with no sink is the second case. A kind where artefacts are incidental should make the sink optional and degrade — the kind's factory decides, not a library-wide rule.

**`deliver` refuses by raising.** Returning `None` is a silent drop, and a "refused" flag on `LandedArtefact` means every consumer must remember to check it, and one will not.

### Names: what the library guarantees, and what it does not

The library enforces a **narrow invariant** and no more: relative, no traversal, no separators beyond the declared output's own, bounded length, valid UTF-8. It does not guarantee the name is legal at the destination, because it cannot: legal names differ between a blob container, NTFS, and a workspace store, and a library that mangled `report:v2.png` into `report_v2.png` for a Linux host or an object store that was perfectly happy would be overreaching. **Hosts still own their own namespace rules**, and the doc says so rather than implying the problem is solved.

Windows-hostility is real and worth helping with, so `portable_name()` ships as an **opt-in helper** — reserved device names (`CON`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, including with extensions), the forbidden `< > : " | ? *` set, trailing dots and spaces. Every host that needs it would otherwise reinvent that list, badly.

Names are normalized to **NFC** before `deliver` sees them, because it is the one form that survives all three filesystems recognisably: macOS normalizes to NFD on the filesystem, Linux stores bytes verbatim, Windows is NFC-shaped, and without normalization `LandedArtefact.name` and the name actually on disk disagree on exactly one platform. A host with a content-addressed store or a Linux-only deployment opts out with `NameNormalization.NONE` — which disables **only** the rewrite. The narrow invariant still applies, and collision detection still **compares** normalized forms, so two names differing only in NFC/NFD still collide. We compare normalized and write what was asked for.

**Case-only collisions are refused across one collection.** `Diagram.png` and `diagram.png` are two files on Linux and one on Windows and default macOS, so a case-sensitive check silently loses one of them on two platforms out of three. `collect_outputs` has every name in hand before it delivers any of them; the host receives artefacts one at a time and can never see the collision.

**Never-overwrite is a contract clause, not a library guarantee.** Because the host does the writing, the library cannot enforce it. The obligations on a host are: a dedicated artefact namespace, never overwrite, and treat the name as guest-derived data.

### The sink is structural, and that is why it is not declared

It is tempting to give the landing callback a role decoration like a dispatched host tool, or to stamp it as a sink automatically when the host runs information-flow enforcement. Both are wrong, for reasons this repository has already written down.

A dispatched host tool's role is genuinely ambiguous — source, sink, both, or neither — which is why [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) makes every leg explicit and why `require_declared` refuses an unstamped one. **A landing callback has no ambiguity**: data moves guest → host state, always. It cannot be a source. There is nothing to infer, so nothing to auto-infer, and making it decorable only creates a way to declare it as something it is not.

What is *not* structural is how confidential the destination is — exactly the value `sandbox_tool_declarations` already refuses to invent: writing a confidentiality key "participates in a policy leg that may be dormant in the host … the host's decision to make with its own classification in hand, never a default a library picks". The integrity leg takes a library default; the confidentiality leg does not. So the host names the grade when it supplies the sink, and the library's job is to make the *flow* visible, never to grade it.

### The `spec.egress_allow` guard becomes wrong

`sandbox_tool_declarations` writes the confidentiality cap only when the spec permits egress, because "a sandbox with no network cannot carry anything out of the conversation, so capping it would gate calls for a flow that does not exist".

**A landing sink falsifies that premise.** With closed egress and a sink, bytes leave for host state and the flow the guard was checking for exists again. The condition becomes "the spec permits egress **or** a sink is attached", and the effective cap is the strictest of the two — the same strictest-over-sinks fold the `HOST_TOOLS` registry uses. No test will demand this: the code keeps working and the suite stays green, which is why it is written down here.

### Approval

`never_require` remains the default for landing. The meaningful control is that artefacts land in a dedicated namespace that never overwrites, not a prompt a user learns to click through. But a kind's factory must expose `approval_mode`, so a host whose sink writes somewhere consequential can turn it on without forking the kind.

### What `FILES_OUT` is not

**It is not `HOST_TOOLS`.** Nothing inside the sandbox dispatches the landing callback; the kind calls it host-side after the run, with bytes it pulled. The empty-dispatch property a CodeAct-class kind rests on survives intact. The shapes look similar enough that a reader will assume `denied_capabilities={HOST_TOOLS}` blocks artefact landing — it does not, and a host wanting that hard stop denies `FILES_OUT`.

**It is not a second write path into the workspace.** A kind that lands artefacts where the agent's own file tools write has given model-written code an unapproved `file_access_write`, and one that can overwrite has given it a way to influence a *different* kind on the next call — a `.bicep` a validator compiles, a config another tool reads. `(key, kind)` sandbox identity ([#84](https://github.com/sokolaidev/maf-extensions/issues/84)) separates sandboxes; it does nothing about host state they both touch.

## Writing a kind that collects artefacts

1. **Declare your outputs.** Literal relative paths, with a disposition, a media type, and `required` set honestly. A render that legitimately produces nothing on failure declares its artefact optional.
2. **Tell the model where to write.** The output path has to appear in the tool's description: a program that saves its PNG somewhere else produces nothing collectable and no error.
3. **Do not put bytes in the result.** Return the references `deliver` gave you. A kind that base64s an artefact into the tool result has reintroduced the convention this capability exists to remove.
4. **Require `FILES_LIST` only if you truly cannot name your outputs.** It is refused on Docker and wslc; a kind that requires it without needing it has made itself ACAS-only.
5. **Grow `requires` from what you declare.** A spec with no `LAND` outputs and no sink should not require `FILES_OUT` at all.
6. **Sanitised failures still apply.** A host's `deliver` can raise with a connection string in the message; it goes through `error_detail` into the log, and the model gets the fixed sentence.

## Implementing `FILES_OUT` in a backend

- **Declare it honestly or not at all.** There is no router-side emulation and there must not be: a laundered capability makes every downstream refusal meaningless.
- **ACA Sandboxes** are the reference: `read_file` returns bytes natively, `list_files`/`stat_file` back `FILES_LIST`, and `write_file` already accepts `str | bytes`. Its obligation is the symlink `stat` above.
- **Docker** implements reads as `docker cp <name>:<path> -`, streaming a tar out, which works against an image with no shell. It does **not** declare `FILES_LIST`.
- **wslc** should do **tar-out**, the reverse of the one-entry tar on stdin it already writes: binary-safe, streaming, and it carries the type bit that makes confinement structural.
- **base64-over-exec is opt-in convenience, never the contract.** It depends on `base64` and a shell existing in the image, which the native copy paths do not, and its entry-type reporting is a parse of command output. If it ships it is **one reviewed implementation in `maf_sandbox`**, never a parse per backend, and it carries its own lower maxima.
- **The micro-VM standard gains a clause.** Leg 4 admits "files in, results out" as declared channels; for a backend claiming `microvm` *and* `FILES_OUT`, the declared channel is reads confined to `work_dir` with non-regular entries refused. A read path that can be walked out of `work_dir` does not meet leg 4, whatever the hypervisor does.
- **`maf_sandbox.testing` grows the same surface.** The in-process fake implements the pull pair and declares both capabilities configurably, or no kind can be tested and no declaration test can assert the router match in both directions.

## Worked example: a diagram kind

One tool, `render_diagram(dot: str)`: the model writes Graphviz DOT, the sandbox renders it, a PNG comes back as a reference. The smallest workload that exercises every part of this document — model-authored input, a binary artefact, a real backend, and nothing the model could have produced by reasoning.

```python
def diagram_sandbox_spec(image: str | None = None) -> SandboxSpec:
    return SandboxSpec(
        kind="diagram-generator",
        image=image,
        egress_allow=(),                                            # renders, does not fetch
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        outputs=(DeclaredOutput(path=_PNG_NAME, media_type="image/png", required=False),),
        files_out=TransferLimits(max_bytes_per_file=_PNG_CAP, max_total_bytes=_PNG_CAP, max_files=1),
    )
```

What it demonstrates, in the order a reader meets it: the DOT source goes in through `FILES_IN` as file content, so no shell sees model text and the `exec` stays a fixed argv; `egress_allow=()` holds because rendering is computation; no `FILES_LIST`, because the kind names its own output — which is why it runs unchanged on ACAS, Docker and wslc; `max_files=1` is a workload property rather than a guess, since one invocation renders one graph; `required=False` because `dot` failing on malformed input is a diagnostic the model should act on, not a transport error; and the PNG leaves through the host's sink so a corrupt render cannot become a wall of transcript.

## Rollout

1. **Protocol** — `OutputDisposition`, `DeclaredOutput`, `EntryKind`, `SandboxEntry`, `TransferLimits`, `SandboxLimits`, `read_file`, `list_dir`, the two capabilities, the spec fields, the widened `write_file`, the error taxonomy, and the `maf_sandbox.testing` fake. No backend declares the capabilities yet, so nothing changes behaviourally.
2. **Glue** — `Artefact`, `LandedArtefact`, `OutputSink`, name validation and normalization, the case-collision check, `collect_outputs`, and the `sandbox_tool_declarations` fix so a sink counts as an outbound flow. `collect_outputs` belongs in the **stdlib-only core**, not in `maf_sandbox.maf`: it needs nothing from `agent_framework`, and putting it in the MAF module would deny it to protocol-only consumers for no reason.
3. **Docker, first and as the acceptance gate.** The backend declares `FILES_OUT` from the day it exists rather than shipping without it and adding it later, and its e2e is what proves the protocol: write, exec, read back, cap breach with a mid-transfer abort, symlink refusal. It goes first because it is the only suite that runs on a GitHub runner with no subscription, no login and no disk-image import — and because it is the only backend a contributor can exercise on the machine in front of them. `docker cp` covers reads natively, and it does **not** declare `FILES_LIST`.
4. **ACAS** — natively, including the symlink `stat`. It remains the *reference* for shape, since its file API is the richest and it is the only backend that can serve `FILES_LIST`; it follows Docker because verifying it requires infrastructure that a pull request cannot assume.
5. **The diagram kind and `samples/07`** — end to end.
6. **wslc** — tar-out, so "the same kind, both backends" holds for artefact-producing kinds too.

Reference and gate are deliberately different roles. ACAS defines what the surface *should* look like because it has the most complete file API; Docker decides whether the surface actually *works*, because it is the one that runs everywhere. Splitting them is what keeps the richest backend from quietly setting requirements the portable ones cannot meet — which is the mistake the `FILES_LIST` split already corrected once.

## Open questions

- **Is one `deliver` per artefact the right granularity?** A collection of forty files is forty awaits. A batch form is friendlier to a store with a transaction and worse for one that streams. Left per-artefact until a host argues otherwise.
- **The error taxonomy's exact members** are named above by role, not yet as types. They should be settled in the protocol PR rather than here, where nothing can check them.
