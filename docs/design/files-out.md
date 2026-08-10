# `FILES_OUT`: getting artifacts out of a sandbox

> **Status: PROPOSED** — tracking issue [#109](https://github.com/sokolaidev/maf-extensions/issues/109). The baseline is [`sandbox-architecture.md`](sandbox-architecture.md); this document specifies rollout item 4 of [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md), whose `FILES_OUT` paragraph is a sketch and is superseded here.

## What is missing, and why it is not symmetrical with `FILES_IN`

A workload today can put files into a sandbox and run a command; its only way back is `ExecResult.stdout`. Every kind whose product is an artifact — a rendered image, a generated archive, a transformed dataset — is either unwritable or forced into a base64-on-stdout convention, and such a convention does not stay a workaround: the tool description teaches it, the model depends on it, and it outlives the protocol gap by years.

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
- **Landing an artifact in host state** is a **sink**. The question is *confidentiality*: may a conversation this sensitive cause bytes to be written where they are going? Nothing answers that today.

The two are distinguished in the spec, not left to convention — see `OutputDisposition` below.

## Two capabilities, not one

`FILES_OUT` is **stat and read a path the spec declared**. Open-ended *enumeration* — "tell me what is in this directory" — is a separate capability, `FILES_LIST`, and the split is the load-bearing decision in this document.

The reason is a backend, not a preference. **Docker has no engine-level primitive for enumerating a directory.** `docker cp <name>:<path> -` streams a tar out and works against an image with no shell, and `HEAD /containers/{id}/archive` stats a *named* path — but discovering what names exist requires either an in-container `ls`/`find`, which depends on the image having one while the copy path does not, or tarring the whole directory to read its entry headers, which transfers every byte including the one a size cap exists to refuse. Requiring enumeration would make the backend most people run locally and in CI either fragile or cap-hostile.

ACA Sandboxes, by contrast, expose `list_files` natively. So the two capabilities describe a real difference between real backends, which is the test worth applying to any future member: **name the backend that lacks it.** If none does, it is a comment, not a capability. (This is why widening `write_file` to `str | bytes` — below — gets no capability of its own: every backend can already do it.)

One consequence decides the API shape: **globs require enumeration.** `*.png` cannot be resolved without listing a directory, so a glob-accepting spec would silently reintroduce the primitive Docker lacks. Declared outputs are therefore **literal relative paths**; patterns are available only to a kind that also requires `FILES_LIST`.

| | `FILES_OUT` | `FILES_LIST` |
|---|---|---|
| Surface | `stat_file`, `read_file` | `list_dir` |
| What a kind must know | The names of its outputs, in advance | Nothing |
| ACAS | Native (`stat_file`, `read_file`) | Native (`list_files`) |
| Docker | Native (`HEAD /archive`, `docker cp`) | Not without an in-image shell |
| wslc | **Not served** — `cp` has no container-to-stdout form, so there is no tar to read a header from ([#125](https://github.com/sokolaidev/maf-extensions/issues/125)) | Not without an in-image shell |
| Who needs it | Every artifact-producing kind | Kinds whose output names are unpredictable — the CodeAct shape |

## The protocol

### Adding to `Sandbox` is a breaking change, and that is the choice

`Sandbox` is `@runtime_checkable`, and `SandboxBackend`'s docstring already records what that costs: `runtime_checkable` enforces member *presence*, which is why `capabilities` is read with `getattr` rather than declared as a Protocol member. The same reasoning applies to `Sandbox`, and this document adds three members to it anyway.

That is a decision, not an oversight, and it is safe here for a reason worth stating: **nothing in this repository ever calls `isinstance(x, Sandbox)`** — the only protocol `isinstance` checks are against `SandboxBackend`, in four tests. The members go on the Protocol because a sandbox's file surface is what the type exists to describe, and hiding it behind `getattr` would make every kind feature-detect.

It is nonetheless **breaking for out-of-tree implementers**: an existing `Sandbox` stops satisfying the protocol the day this lands. It ships as `feat!` at 0.x, and "no backend declares the new capabilities yet, so nothing changes behaviourally" is true of the router's capability match and **false of the protocol**.

### `FILES_OUT` — declared outputs

```python
class OutputDisposition(StrEnum):
    LAND = "land"        # goes to the host's OutputSink; the model gets a reference
    CONSUME = "consume"  # goes to the kind, which parses it; never reaches the sink

@dataclass(frozen=True)
class DeclaredOutput:
    path: str                                            # literal, relative to the working directory; no globs
    disposition: OutputDisposition = OutputDisposition.LAND
    media_type: str | None = None                        # declared by the kind, never sniffed
    required: bool = True                                # missing required is an error; missing optional is absence

class Sandbox(Protocol):
    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None: ...
    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes: ...
```

**`working_directory` is a parameter, exactly as it is on `exec`.** This is the correction that matters most: no sandbox object knows the spec's `work_dir` — `_AcasSandbox` holds an SDK client, `_WslcSandbox` holds a runner and a container name, `InProcessSandbox` holds a dict — and `work_dir` reaches a sandbox exactly once per call, as `exec(..., working_directory=...)`. A pull pair without it assigns the confinement duty to a layer with no way to discharge it. `path` resolves against `working_directory`, and a resolved path outside it is refused.

Four more decisions, each paying for itself:

- **`disposition` puts the two flows in the spec.** A PNG lands; a SARIF file is parsed by the kind and must never reach the sink, because it is the tool's own input rather than its product. Putting it in the spec is what lets the glue apply the sink's declaration to `LAND` outputs only, rather than to everything the kind touches. It is a *routing* distinction and nothing more: a `CONSUME` output is stat-ed, capped and counted exactly like a landing one — see the caps below.
- **`media_type` is declared, not sniffed.** Sniffing lets guest-produced content decide how the host handles it. A kind knows what it renders.
- **`required` separates transport failure from workload failure.** `dot` exiting non-zero and producing no PNG is the *normal* path a model has to recover from; if collection raises on top of it, the model gets a transfer error where it should get a diagnostic.
- **The spec field is `declared_outputs`, not `outputs`.** `InProcessSandbox.__init__` already takes `outputs=` meaning marker-keyed scripted stdout, and the two would appear in one expression in every kind's tests.

### `FILES_LIST` — enumeration

```python
class EntryKind(StrEnum):
    FILE = "file"           # a regular file, the only kind that may be read
    DIRECTORY = "directory"
    OTHER = "other"         # symlink, junction, reparse point, device, socket, fifo — never read

@dataclass(frozen=True)
class SandboxEntry:
    path: str               # relative to the working directory
    kind: EntryKind
    size_bytes: int | None  # None when the backend cannot report it — see below

class Sandbox(Protocol):
    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]: ...
```

`list_dir`, not `list_files`: `WorkspaceContext.list_files` is the host's allowlist and the most trusted enumeration in the system, while this is the least trusted one, and both are in scope inside a kind's tool body. Giving them one name makes confusing them a security bug that reads perfectly well.

`SandboxEntry` is shared with `FILES_OUT`, because `stat_file` returns one.

`size_bytes` is `int | None`, and **`None` fails closed**: ACAS's `FileInfo.size` is `int | None` and an omitted field yields `None`, so coercing unknown to `0` would make a size cap read that file as free. An entry whose size cannot be determined is refused, never read.

`kind` is a **typed field**, not a mode string to parse. ACAS's `FileInfo` carries `is_directory` — a two-way split with no symlink flag — and leaves type information in `mode: str | None`; Docker's stat carries both a Go `ModeSymlink` bit and an explicit `linkTarget`. `OTHER` also absorbs Windows junctions and reparse points, so the vocabulary survives a non-POSIX guest.

### Caps

```python
@dataclass(frozen=True)
class TransferLimits:
    max_bytes_per_file: int
    max_total_bytes: int
    max_files: int

    def within(self, ceiling: "TransferLimits") -> bool:
        """Every field at or below ``ceiling``'s — the match ``ensure_can_serve`` applies."""

DEFAULT_TRANSFER_LIMITS: TransferLimits = TransferLimits(...)   # one constant, both sides
```

`SandboxSpec` carries one per direction (`files_in`, `files_out`), and a backend declares its own ceilings. All three fields are needed: a byte cap alone does not bound a collection, since ten thousand files one byte under the per-file ceiling cost exactly what the ceiling was written to prevent.

**`files_out` bounds the collection the spec *declared*, not the subset of it that lands.** A `CONSUME` output is stat-ed, capped and counted against all three fields exactly as a landing one is — it is the same guest, the same filesystem and the same bytes leaving the sandbox, and the only difference is where they go afterwards. Exempting them would have made every cap opt-out: a spec declaring everything `CONSUME` would be uncapped. What `collect_outputs` does *not* do for a `CONSUME` output is read it, so **a kind reading its own `CONSUME` output is responsible for that read's own bound** — it passes `max_bytes` to `read_file` like any other caller, and the collection-wide accounting above cannot see bytes it never handled.

**The invariant that keeps this change inert: the spec-side default and the backend-side silent default are the same constant.** `within()` is then satisfied by equality, and nothing already in the repository starts being refused. Get this wrong in the other direction — a spec default above what a silent backend is assumed to allow — and *every* spec fails at attach, including `SandboxSpec(kind="smoke")` in the published-wheel smoke test. A test asserts `DEFAULT_TRANSFER_LIMITS.within(DEFAULT_TRANSFER_LIMITS)` so the invariant cannot drift apart.

**Enforcement is pre-stat where stat exists, and stream-counting is the fallback.** This is the reverse of what an earlier draft said, and the evidence is on both real backends: the ACAS SDK's `read_file` does `await response.read()` internally — fully buffered, no incremental hook — so stat is the *only* enforcement available there; Docker's `HEAD /archive` returns a size before any byte moves, and can also count tar bytes on the way out as a second line. The contract is therefore: **stat, refuse if over cap or unknown, then read**; a backend that can additionally abort mid-transfer should, but no backend is required to.

`read_file` therefore takes **`max_bytes`**, and the caller passes the stat-ed size clamped by what the collection has left. A backend that can stop early stops early; one that cannot refuses afterwards. It is a **refusal, never a truncation** — half a PNG returned as success is an artifact the host cannot tell from a whole one — and the caller re-counts what actually arrived regardless, because a bound handed to the guest's own backend is not a bound the guest cannot beat.

**The caps are re-applied to the bytes actually read, not only to the stat-ed sizes.** A stat is a promise about a file the guest can still rewrite before the read reaches it, and the guest is the thing the sandbox exists to contain. Checking once would make the whole cap advisory against exactly the adversary it is written for.

A breach **fails the whole collection with no partial delivery**. A partial artifact set reported as success is worse than none, because the model cannot tell what it did not get.

### Backend maxima, and which silence rule they follow

A backend declares ceilings as `limits: SandboxLimits` (a `TransferLimits` per direction), read with `getattr` like `capabilities` and `egress`; `ensure_can_serve` refuses a spec asking above them, and the backend enforces the *spec's* cap at runtime.

What an *undeclared* `limits` means is not the rule `capabilities` uses. `Capability` silence is read charitably because it is a functionality claim: a backend that never heard of the vocabulary still honestly does what `Sandbox` obligates. `Egress` silence is read as `UNRESTRICTED` and refused, because it is a safety claim. **Transfer limits are a safety claim**, so silence resolves to `DEFAULT_TRANSFER_LIMITS` and a bigger ask is refused.

This is the third optional backend declaration read with `getattr`. Three is where the pattern stops; a fourth is the signal to collapse all of them into one optional declarations object.

### `write_file` widens; no capability for it

`write_file` becomes `(path: str, content: str | bytes)`, with `str` continuing to mean UTF-8. The in-door otherwise cannot carry a PNG or a spreadsheet, and this release already has every backend's file code open.

It gets no capability, by the test above: ACAS's SDK signature is already `content: str | bytes`, and Docker and wslc both transport via tar, which is binary-native. No backend lacks it.

Note the residual asymmetry, stated rather than hidden: `write_file` takes an **absolute** guest path and has no path grammar today, while the read surface takes a path relative to a `working_directory` and validates it. Unifying them is a larger change than this one and is not attempted here.

### Error taxonomy

Named exceptions under one base, so backends do not diverge and a kind can map failures to messages: a declared output that is absent, a path resolving outside the working directory, a non-regular entry refused, a size that cannot be determined, a cap breached (naming which cap and which file), a sandbox that has gone away, and — decided by the declaration rather than by the run — a name breaking the narrow invariant, two names colliding, and something that lands with no sink to land it in. Provider and transport detail stays in the log under `error_detail`, never in the tool result.

**The base class is a promise about coverage, not a family resemblance.** A backend answers in its own vocabulary — a bare `ValueError` for a path it would not resolve, a bare `FileNotFoundError` for a file the guest deleted between the stat and the read — and a kind told to catch one base class would never see either. `collect_outputs` translates what the pull surface raises into the family, keeping the original as `__cause__`. Enumerating the members anywhere is how the list drifts; the code states no count.

## Confinement

Reads are confined to the working directory, and the confinement that matters is not the one on the argument string:

```python
os.symlink("/", "/work/out/root")           # inside the guest, one line of the program
```

A reader that follows that link reads whatever *it* can see — on a backend that streams from inside the guest, the guest's filesystem; on a sync-mount backend, where the output directory is a host directory the guest writes into, the **host's**.

Only regular files are ever read. A symlink is refused whether or not its target would have resolved somewhere legitimate: there is no case where a kind needs to follow a link that a plain file would not serve, and "the target is inside the working directory" is a judgement made with the wrong filesystem in view. **How** that rule is honoured differs by backend:

| Backend | Mechanism | Strength |
|---|---|---|
| Docker | `HEAD /archive` returns a Go `ModeSymlink` bit **and** an explicit `linkTarget`; `docker cp` without `-L` additionally tars the link *entry* rather than the target's bytes | Strongest — two independent mechanisms, both verified against a live engine |
| wslc | **None available.** `cp` reports success and writes a **0-byte file** for a symlink — neither preserved, nor followed, nor refused, and indistinguishable from a legitimately empty artifact | Cannot meet the rule; the backend does not serve `FILES_OUT` |
| ACAS | `stat_file` returns `is_directory` (a two-way split) and `mode: str \| None`, so the type must be parsed out of the mode string | Weakest — **fail closed when `mode` is `None`** |

The reference backend is the weak one, and its defence is a parse of an undocumented preview-SDK field. That is acceptable only stated plainly, and it should be raised upstream: `FileInfo` ought to carry the entry type as a field rather than leaving it in a mode string.

## Cross-platform rules

Every shipped backend runs a Linux guest today. That is an observation, not a protocol assumption — Windows-guest backends are plausible ([#111](https://github.com/sokolaidev/maf-extensions/issues/111)), so the protocol states one grammar and **backends translate to whatever their guest actually is**. This is the same shape as `exec` taking an argv *sequence* rather than a command line.

- **Paths are POSIX-shaped in the protocol**, and a backslash in a declared path is refused — not because the guest is Linux, but because the protocol has one grammar and `\` is not a separator in it. Nothing builds a guest path with `os.path` or `pathlib`; `posixpath` only.
- **UTF-8 is the interchange form for names.** Linux filenames are byte strings and can be invalid UTF-8; such a name is refused rather than round-tripped. This only arises on the `FILES_LIST` road — declared paths are authored by the kind.
- **`str` content means UTF-8, always**, independent of host locale. Any code path that reaches a platform default encoding is a mojibake bug waiting for a Windows host.
- **No newline translation, in either direction, ever.** A host must write artifact content in **binary** mode: `open(path, "w")` on Windows turns `\n` into `\r\n` and corrupts a PNG that was byte-exact when it left the sandbox.

## Where artifacts land

### The host writes; the library never does

A workspace store, a blob container, a UI artifacts panel, a scratch directory — the destination is a property of the application, and `maf_sandbox` is stdlib-only by design. The host supplies a callback, which is the pattern `WorkspaceContext` already is:

```python
@dataclass(frozen=True)
class Artifact:
    """One file pulled out of a sandbox, on its way to the host."""
    name: str                    # validated; relative; derived from the declared output
    content: bytes
    kind: str                    # spec.kind, so a host can route by workload
    media_type: str | None       # as declared by the kind

@dataclass(frozen=True)
class LandedArtifact:
    """Where the host put it."""
    name: str                    # echoes what was asked for
    display: str                 # safe for the transcript
    handle: str | None = None    # host-internal: URL, blob key, id — never auto-rendered

class NameNormalization(StrEnum):
    NFC = "nfc"                  # default
    NONE = "none"                # byte-exact

@dataclass(frozen=True)
class OutputSink:
    deliver: Callable[[Artifact], Awaitable[LandedArtifact]]
    normalization: NameNormalization = NameNormalization.NFC
```

`collect_outputs(...) -> tuple[LandedArtifact, ...]`, frozen and ordered — a tuple, as `egress_allow` is.

**The typed return is a security property, not tidiness.** If `deliver` returned one string and the kind put it in the tool result, whatever the host handed back would enter the transcript verbatim — and for a blob container that may be a SAS URL with a bearer token in the query string, persisted and replayed on every subsequent turn. `display` is what the model sees; `handle` is the host's own reference and is never rendered anywhere by this library.

**Everything is pulled and capped before the first `deliver`.** A push callback that writes to host state cannot be un-called, so "no partial delivery" is only achievable by collecting the whole set first. One consequence is normative: there is no streaming to the sink.

The second consequence is weaker than an earlier draft claimed, and the difference is worth stating plainly rather than discovering. **What is normative is that over-cap bytes are never *delivered*.** `max_total_bytes` as a *peak host-memory* bound is **best-effort and backend-dependent**: it is passed down as `read_file(max_bytes=...)`, so a backend that can stop reading early does — but a backend whose SDK buffers the whole response internally before returning it, which the reference one does (`await response.read()`), has already allocated the file by the time this library can look at its length. **Such a backend cannot provide the memory bound at all**; it can only refuse afterwards, which is a delivery guarantee, not a memory one. A host that needs a hard memory ceiling gets it from a backend that streams, or from the per-file cap it chooses.

**One residue survives that ordering, and it is a property of the shape rather than a defect.** If `deliver` itself raises on the third of five artifacts, the first two are already in the host's store and nothing can retract them; the exception propagates and the host is left holding a partial set. Everything the library decides — a bad name, a collision, a breached cap, a missing required output — is settled before any delivery, so this is the *only* path to a partial landing. It is also the strongest argument for the batch form in the open questions below: a single call taking the whole collection would remove the residue entirely, where the per-artifact shape cannot.

**A spec with `LAND` outputs requires a sink.** This is structural rather than a per-kind judgement: if the spec declares something that lands and no sink was supplied, the tool cannot honour its own spec. The check lives **inside `sandboxed_tool`, after the attach gate** — a host with no sandbox configured at all still gets `[]`, because the unconfigured-host escape wins, exactly as it does today. A kind where artifacts are incidental declares no `LAND` outputs when it was given no sink.

**`deliver` refuses by raising.** Returning `None` is a silent drop, and a "refused" flag on `LandedArtifact` means every consumer must remember to check it, and one will not.

### Names: what the library guarantees, and what it does not

The library enforces a **narrow invariant** and no more: relative, no traversal, no separators beyond the declared output's own, no segment that names nothing (`a//b` and `a/./b` are `a/b`, and delivering both spellings would land two artifacts for one file), no control character, bounded length, valid UTF-8. It does not guarantee the name is legal at the destination, because it cannot: legal names differ between a blob container, NTFS, and a workspace store. **Hosts still own their own namespace rules.**

Control characters are the one rule that reaches past the guest's own grammar, and it is not overreach: no filesystem anywhere accepts a NUL or a newline in a name, so refusing them decides nothing a destination might have decided differently. The length bound is checked against the name **as it will be delivered**, after normalization — NFC is not length-non-increasing (85 × U+0958 is 255 bytes decomposed and 510 composed), so checking the declared spelling would be checking a different name.

Windows-hostility is real and worth helping with, so `portable_name()` ships as an **opt-in helper** covering Microsoft's authoritative set and nothing beyond it: `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, **and the ISO/IEC 8859-1 superscript variants `COM¹ COM² COM³ LPT¹ LPT² LPT³`** — Windows reads those superscripts as digits, so `COM¹` is a device and `echo test > COM¹` fails to create a file. All of them reserved with an extension too (`NUL.tar.gz` is `NUL`), plus the forbidden `< > : " | ? *` set, **ASCII 0–31, which Microsoft's naming rules list in the same breath as that punctuation**, and trailing dots and spaces.

The superscripts are the entry that justifies shipping this at all: no host writing its own list will think of them. The set stops exactly at the documented one — `COM⁴` (U+2074) is not a digit to Windows and is a legitimate filename, and a helper that guesses beyond the spec starts mangling names that were fine.

Names are normalized to **NFC** before `deliver` sees them, because it is the one form that survives all three filesystems recognisably. A host with a content-addressed store or a Linux-only deployment opts out with `NameNormalization.NONE` — which disables **only** the rewrite. The narrow invariant still applies, and collision detection still **compares** normalized forms. We compare normalized and write what was asked for.

**Case-only collisions are refused across one collection.** `Diagram.png` and `diagram.png` are two files on Linux and one on Windows and default macOS. `collect_outputs` has every name in hand before it delivers any of them; the host receives artifacts one at a time and can never see the collision.

The comparison is **lowercase, not casefold**, and the difference is a refusal nobody would understand. Casefolding maps `ß` to `ss` and `ﬁ` (U+FB01) to `fi`, so `Straße.png` + `Strasse.png` and `ﬁle.png` + `file.png` would each be refused as one file — and they are two files on Linux, NTFS and case-insensitive APFS alike. A cap that fails a whole collection has to be right about why.

**Never-overwrite is a contract clause, not a library guarantee.** Because the host does the writing, the library cannot enforce it.

### The sink is structural, and that is why it is not declared

It is tempting to give the landing callback a role decoration like a dispatched host tool, or to stamp it as a sink automatically when the host runs information-flow enforcement. Both are wrong.

A dispatched host tool's role is genuinely ambiguous — source, sink, both, or neither — which is why [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) *proposes* making every leg explicit. **A landing callback has no ambiguity**: data moves guest → host state, always. It cannot be a source. There is nothing to infer, so nothing to auto-infer.

What is *not* structural is how confidential the destination is — exactly the value `sandbox_tool_declarations` already refuses to invent: writing a confidentiality key "participates in a policy leg that may be dormant in the host … the host's decision to make with its own classification in hand, never a default a library picks".

### The confidentiality cap: one value, one source, no fold

`sandbox_tool_declarations` writes the cap only when the spec permits egress, because "a sandbox with no network cannot carry anything out of the conversation".

**A landing sink falsifies that premise.** With closed egress and a sink, bytes leave for host state and the flow the guard was checking for exists again. The condition becomes "the spec permits egress **or** the spec declares an output that lands *and* a sink is attached".

Both halves of that second clause are load-bearing, and the shorter version — "a sink is attached" — reintroduces the bug it was written to fix. A sink is ordinarily *one object handed to every sandbox tool a host builds*, so its presence says nothing about whether this particular workload sends anything down it. A spec that declares no outputs, or only `CONSUME` ones, carries nothing to host state however many sinks it was given, and capping it would gate calls for a flow that does not exist — the exact thing the condition exists to avoid.

An earlier draft said the effective cap is "the strictest of the two, the same fold the `HOST_TOOLS` registry uses". **Both halves of that were wrong.** The cap is an opaque host-vocabulary string with no ordering — this repository requires orderings to be data with exhaustiveness tests, as `ISOLATION_RANK` is — so a library cannot rank two of them. And the cited precedent does not exist: the `HOST_TOOLS` registry, `require_declared` and the strictest-over-sinks fold are unimplemented rollout item 5 of the two-axis proposal, described there in the future tense.

So there is **one value from one source**: the parameter is renamed `outbound_max_confidentiality` (it no longer describes only egress) and the host supplies it once. `OutputSink` carries no cap of its own.

One further hazard, which is the same shape as the bug this section fixes: `sandboxed_tool` honours an explicit `declarations=` mapping verbatim, and `maf-sandbox-codeact` already uses that escape hatch — so a kind passing `declarations=` would silently keep a derivation that knows nothing about its sink. **Supplying both a sink and an explicit `declarations=` is refused** rather than resolved by precedence.

### What `FILES_OUT` is not

**It is not `HOST_TOOLS`.** Nothing inside the sandbox dispatches the landing callback; the kind calls it host-side after the run. The empty-dispatch property a CodeAct-class kind rests on survives intact. A host wanting a hard stop denies `FILES_OUT`.

**It is not a second write path into the workspace.** A kind that lands artifacts where the agent's own file tools write has given model-written code an unapproved `file_access_write`, and one that can overwrite has given it a way to influence a *different* kind on the next call. `(key, kind)` sandbox identity ([#84](https://github.com/sokolaidev/maf-extensions/issues/84)) separates sandboxes; it does nothing about host state they both touch.

**It does not change backend selection.** `SandboxRouter` resolves a backend by name or by position and checks only the isolation floor; the capability match happens afterwards and **raises `SandboxCapabilityNotSupported`** rather than choosing a different backend. Capability-based selection is a separate, unimplemented proposal. So a host whose only backend lacks `FILES_OUT` gets an exception out of its agent factory, not a quietly-unattached tool.

## Writing a kind that collects artifacts

1. **Declare your outputs.** Literal relative paths, with a disposition, a media type, and `required` set honestly.
2. **Tell the model where to write.** The output path has to appear in the tool's description: a program that saves its PNG somewhere else produces nothing collectable and no error.
3. **Do not put bytes in the result.** Return the references `deliver` gave you.
4. **Require `FILES_LIST` only if you truly cannot name your outputs.** It is refused on Docker and wslc; a kind that requires it without needing it has made itself ACAS-only.
5. **Grow `requires` from what you declare.** A spec with no declared outputs should not require `FILES_OUT` at all — and one that declares any output, of either disposition, is **refused** without it: the capability match is the only thing standing between that spec and a backend with no pull surface, and it only runs on what `requires` names.
6. **Do not combine a sink with an explicit `declarations=`** — it is refused, because the two disagree about what the tool's flow is.

## Implementing `FILES_OUT` in a backend

- **Declare it honestly or not at all.** There is no router-side emulation and there must not be.
- **Docker** implements stat as `HEAD /containers/{id}/archive` (`X-Docker-Container-Path-Stat`: name, size, Go-mode, mtime, `linkTarget`) and reads as `docker cp <name>:<path> -`, a tar on stdout that works against an image with no shell. It does **not** declare `FILES_LIST`.
- **ACAS** reads natively and is the only backend that can serve `FILES_LIST`. Its obligation is the symlink `mode` parse above, failing closed on `None`.
- **wslc does not serve `FILES_OUT`, and the tar-out plan this document used to describe does not exist.** `wslc container cp` has three forms — local→container, container→local, and stdin→container — and **no container→stdout form**, so there is no reverse of the one-entry tar it writes on the way in. Container→local writes a raw file to a host path, with no tar header and therefore no type or size ahead of the content. Worse, a symlink source exits 0, writes nothing to stderr, and produces a 0-byte file. Serving the capability here would mean an `exec`-based `stat` before every read, which requires the image to contain a shell — the dependency the `FILES_LIST` split exists to avoid. Deferred in [#125](https://github.com/sokolaidev/maf-extensions/issues/125); filed upstream as [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) (the symlink bug) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310) (the missing stdout form), either of which would reopen the question.
- **base64-over-exec is opt-in convenience, never the contract.** It depends on `base64` and a shell existing in the image, which the native copy paths do not. If it ships it is **one reviewed implementation in `maf_sandbox`**, never a parse per backend, and it carries its own lower maxima.
- **The micro-VM standard gains a clause.** For a backend claiming `microvm` *and* `FILES_OUT`, the declared channel is reads confined to the working directory with non-regular entries refused.
- **`maf_sandbox.testing` grows the same surface.** The in-process fake implements stat, read and list, and declares both capabilities configurably, or no kind can be tested.

## Worked example: a diagram kind

One tool, `render_diagram(dot: str)`: the model writes Graphviz DOT, the sandbox renders it with `dot`, a PNG comes back as a reference. The smallest workload that exercises every part of this document.

```python
def diagram_sandbox_spec(image: str | None = None) -> SandboxSpec:
    return SandboxSpec(
        kind="diagram-generator",
        image=image,
        egress_allow=(),                                            # renders, does not fetch
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        declared_outputs=(
            DeclaredOutput(path=_PNG_NAME, media_type="image/png", required=False),
        ),
        files_out=TransferLimits(max_bytes_per_file=_PNG_CAP, max_total_bytes=_PNG_CAP, max_files=1),
    )
```

The DOT source goes in through `FILES_IN` as file content, so no shell sees model text and the `exec` stays a fixed argv; `egress_allow=()` holds because rendering is computation; no `FILES_LIST`, because the kind names its own output — which is why it runs unchanged on every backend that serves `FILES_OUT` at all, rather than only on the one with the richest file API; `max_files=1` is a workload property, since one invocation renders one graph; `required=False` because `dot` failing on malformed input is a diagnostic the model should act on, not a transport error.

## Rollout

1. **Protocol** — the vocabulary above, the pull surface on `Sandbox`, the two capabilities, the spec fields, the widened `write_file`, the error taxonomy, the `ensure_can_serve` match, and the `maf_sandbox.testing` fake. Two existing tests are tripwires that must be updated deliberately rather than silenced: `TestModuleInventory` requires every `maf_sandbox` module to be classified, and `TestSpecDefaults` asserts each spec field's default one by one.
2. **Glue** — `Artifact`, `LandedArtifact`, `OutputSink`, name validation and normalization, the case-collision check, `collect_outputs`, and the declarations fix. `collect_outputs` belongs in the **stdlib-only core**, not in `maf_sandbox.maf`: it needs nothing from `agent_framework`, and registering it in the protocol-module set makes `TestZeroDependencies` enforce that automatically.
3. **Docker, first and as the acceptance gate.** The backend declares `FILES_OUT` from the day it exists, and its e2e is what proves the protocol: write, exec, stat, read back, cap refusal, symlink refusal. It goes first because it is the only suite that runs on a GitHub runner with no subscription, no login and no disk-image import — and the only backend a contributor can exercise on the machine in front of them.
4. **ACAS** — natively, including the symlink `mode` parse. It remains the *reference* for shape, since it is the only backend that can serve `FILES_LIST`; it follows Docker because verifying it requires infrastructure a pull request cannot assume.
5. **The diagram kind and `samples/07`** — end to end.
6. ~~**wslc** — the bytes seam first, then tar-out.~~ **Deferred** ([#125](https://github.com/sokolaidev/maf-extensions/issues/125)): there is no tar-out to reach. See the backend section above; the two upstream gaps that would reopen it are [microsoft/WSL#41309](https://github.com/microsoft/WSL/issues/41309) and [microsoft/WSL#41310](https://github.com/microsoft/WSL/issues/41310).

Reference and gate are deliberately different roles. ACAS defines what the surface should look like; Docker decides whether it actually works, because it is the one that runs everywhere. Splitting them keeps the richest backend from quietly setting requirements the portable ones cannot meet — the mistake the `FILES_LIST` split already corrected once.

## Open questions

- **Is one `deliver` per artifact the right granularity?** A collection of forty files is forty awaits. Since everything is buffered before the first delivery anyway, a batch form is now the more natural shape; left per-artifact until a host argues otherwise.
- **The error taxonomy's exact members** are named above by role, not yet as types. They should be settled in the protocol PR, where they can be checked.
