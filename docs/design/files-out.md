# `FILES_OUT`: getting artefacts out of a sandbox

> **Status: PROPOSED** — tracking issue [#109](https://github.com/sokolaidev/maf-extensions/issues/109). The baseline is [`sandbox-architecture.md`](sandbox-architecture.md); this document specifies rollout item 4 of [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md), whose `FILES_OUT` paragraph is a sketch and is superseded here.

## What is missing, and why it is not symmetrical with `FILES_IN`

A workload today can put files into a sandbox and run a command; its only way back is `ExecResult.stdout`. Every kind whose product is an artefact — a rendered image, a generated archive, a transformed dataset — is either unwritable or forced into a base64-on-stdout convention, and such a convention does not stay a workaround: the tool description teaches it, the model depends on it, and it outlives the protocol gap by years.

The obvious framing — "`FILES_IN` already works, do the same thing backwards" — is wrong in all three of the ways that matter.

| | `FILES_IN` | `FILES_OUT` |
|---|---|---|
| Who chooses the **name** | The host: only a path present in `WorkspaceContext.list_files` is ever substituted, which is the injection-pinning boundary | The **guest**: the name is whatever ran inside, which for a CodeAct-class kind means whatever the model wrote |
| Who chooses the **content** | The host: bytes already inside the trust boundary | The guest: bytes produced by the thing the sandbox exists to contain |
| Where it **ends up** | Inside the sandbox, which is disposable | Host state — a transcript, a workspace, a blob container — which is not |

So `FILES_OUT` is the first channel in this stack where guest-chosen names and guest-produced bytes reach host state. Everything below follows from that sentence.

## Two flows, not one

A single capability creates two information flows that answer to different legs of a host's policy, and conflating them is the mistake to avoid:

- **Reading bytes the kind itself consumes** — a SARIF file parsed into diagnostics, a JSON result summarised into the tool result — is a **source**. The question it raises is *integrity*: is what came back trustworthy? It is already answered by `sandbox_tool_declarations(source_integrity=...)`, and a kind that runs model-written code answers it by declaring nothing, so the untrusted default applies.
- **Landing an artefact in host state** is a **sink**. The question it raises is *confidentiality*: may a conversation this sensitive cause bytes to be written where they are going? Nothing answers that today.

A kind may do either, both, or neither. `read_file` is the protocol primitive behind both; the sink exists only for the second.

## The protocol

### The pull pair

```python
class EntryKind(StrEnum):
    FILE = "file"           # a regular file, the only kind that may be collected
    DIRECTORY = "directory"
    OTHER = "other"         # symlink, device, socket, fifo — never collected, see confinement

@dataclass(frozen=True)
class SandboxEntry:
    path: str               # relative to the listed directory
    kind: EntryKind
    size_bytes: int         # 0 for anything that is not a FILE

class Sandbox(Protocol):
    async def list_dir(self, path: str) -> tuple[SandboxEntry, ...]: ...
    async def read_file(self, path: str) -> bytes: ...
```

Three decisions in that block, each paying for itself:

- **`list_dir`, not `list_files`.** `WorkspaceContext.list_files` is the host's allowlist — the injection-pinning boundary, the most trusted enumeration in the system. A sandbox-side listing is the least trusted one. Both are in scope inside a kind's tool body; giving them the same name makes confusing them a security bug that reads perfectly well.
- **Typed entries, not bare paths.** `size_bytes` is what makes a cap enforceable *before* the transfer rather than after it, and `kind` is what makes confinement structural rather than a string check.
- **`bytes`, not `str`.** Artefacts will not stay text; decoding is the kind's job. This makes the protocol deliberately asymmetric — `write_file` takes `str` — and that asymmetry is a known debt, not an oversight: see [Open questions](#open-questions).

### Confinement, and why a path prefix check is not it

Reads are confined to `work_dir`. The confinement that matters is not the one on the argument string:

```python
os.symlink("/", "/work/out/root")           # inside the guest, one line of the program
```

A collector that walks the output directory and reads what it finds now reads whatever the *reader* can see through that link. On a backend that streams from inside the guest, that is the guest's filesystem — bad. On a sync-mount backend, where the output directory is a host directory the guest writes into, it is the **host's** filesystem — much worse, and this is precisely the backend shape [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) says the output-subdir design "maps to naturally".

The rule is therefore structural, and it lives at the backend:

1. **Only `EntryKind.FILE` is ever read.** A symlink is reported as `OTHER` and refused, whether or not its target would have resolved somewhere legitimate. There is no case where a kind needs to follow a link that a plain file would not serve, and "the target happens to be inside `work_dir`" is a judgement the backend makes with the wrong filesystem in view.
2. **A backend that reads through the host filesystem resolves before it reads** — `realpath`, then a containment check against the realpath of `work_dir` — because step 1 depends on the backend's own `lstat` being the one that classified the entry.
3. **A backend that reads through the guest** (a tar stream, an in-guest reader) refuses non-regular entries at the stream level, where the type bit is already present.

### Caps: bytes, count, and total

One frozen value type, used in three places, rather than six loose fields:

```python
@dataclass(frozen=True)
class TransferLimits:
    max_bytes_per_file: int
    max_total_bytes: int
    max_files: int

    def within(self, ceiling: "TransferLimits") -> bool:
        """Every field at or below ``ceiling``'s — the match ``ensure_can_serve`` applies."""
```

`SandboxSpec` carries one per direction (`files_in`, `files_out`), defaulted to named constants. A byte cap alone does not bound a collection: ten thousand files one byte under the per-file ceiling costs what the ceiling was written to prevent, and a listing of a million entries costs before a single read. All three are needed, and `max_files` bounds the listing as well as the collection.

### Backend maxima, and which silence rule they follow

A backend declares its own ceilings as `limits: SandboxLimits` (a `TransferLimits` per direction); `ensure_can_serve` refuses a spec asking above them, and the backend enforces the *spec's* cap at runtime — the spec is the workload's property, the maxima are the backend's, and the stricter of the two applies.

The interesting question is what an *undeclared* `limits` means, and the answer is not the one `capabilities` uses. `Capability` silence is read charitably (`DEFAULT_CAPABILITIES`) because it is a functionality claim: a backend that never heard of the vocabulary still honestly does what `Sandbox` obligates. `Egress` silence is read as `UNRESTRICTED` and refused, because it is a safety claim: a backend written before the property existed cannot have been enforcing an allowlist it never read. **Transfer limits are a safety claim** — a backend that never heard of them is not enforcing them — so silence resolves to conservative named defaults and a spec asking for more is refused. Nothing regresses in practice: no shipped backend declares `FILES_OUT`, so none is selected for a spec requiring it until it has been written to.

This is the third optional backend declaration read with `getattr` (`capabilities`, `egress`, now `limits`), for the reason recorded on `SandboxBackend`: `runtime_checkable` enforces member *presence*, so a new Protocol member would stop every backend written before it from being a `SandboxBackend` at all. Three is where the pattern should stop. A fourth declaration is the signal to collapse all of them into one optional declarations object rather than to add another `getattr`.

## Where artefacts land

### The library cannot know, so the host says

A workspace store, a blob container, a UI artifacts panel, a scratch directory, nothing at all — the destination is a property of the application, and `maf_sandbox` is stdlib-only by design. The host supplies a callable, which is the pattern `WorkspaceContext` already is:

```python
@dataclass(frozen=True)
class OutputSink:
    """Where a kind's artefacts land, and how confidential that destination is."""

    deliver: Callable[[str, bytes], Awaitable[str]]
    max_allowed_confidentiality: str | None = None
```

`deliver` receives a name **the glue has already validated** and returns the host-facing reference — a workspace path, an artifact id, a URL — which is what the kind puts in the tool result. Two consequences worth stating: no host re-solves traversal or overwrite (the validation is in one place, not once per application), and **artefact bytes never enter the transcript**, which is what makes a non-UTF-8 artefact a question that does not have to be answered.

Absent a sink, nothing is collected, `FILES_OUT` is not required, and the tool description says nothing about artefacts. Silence is the closed configuration, as it is for `egress_allow`.

### The sink is structural, and that is why it is not declared

It is tempting to make the landing callback carry a role decoration like a dispatched host tool, or to stamp it automatically as a sink when the host runs information-flow enforcement. Both are wrong, for reasons this repository has already written down.

A dispatched host tool's role is genuinely ambiguous — source, sink, both, or neither — which is why [`two-axis-sandbox-policy.md`](two-axis-sandbox-policy.md) makes every leg explicit and why `require_declared` refuses an unstamped one. **A landing callback has no ambiguity**: data moves guest → host state, always. It cannot be a source. There is nothing to infer, so nothing to auto-infer, and making it decorable only creates a way to declare it as something it is not.

What is *not* structural is how confidential the destination is, and that is exactly the value `sandbox_tool_declarations` already refuses to invent: writing a confidentiality key "participates in a policy leg that may be dormant in the host … the host's decision to make with its own classification in hand, never a default a library picks". The integrity leg takes a library default; the confidentiality leg does not. A sink declaration is the confidentiality leg. So the host names the grade when it supplies the sink — one place, one decision — and the library's job is to make the *flow* visible, never to grade it.

### The `spec.egress_allow` guard becomes wrong

`sandbox_tool_declarations` writes the confidentiality cap only when the spec permits egress, because "a sandbox with no network cannot carry anything out of the conversation, so capping it would gate calls for a flow that does not exist".

**A landing sink falsifies that premise.** With closed egress and a sink, bytes leave the sandbox for host state, and the flow the guard was checking for exists again. The condition has to become "the spec permits egress **or** a sink is attached", and the effective cap is the strictest of the two — the same strictest-over-sinks fold the `HOST_TOOLS` registry uses. This is a one-line change that no test will demand: the code keeps working and the suite stays green, which is why it is written down here rather than left to be noticed.

### What `FILES_OUT` is not

**It is not `HOST_TOOLS`.** Nothing inside the sandbox dispatches the landing callback; the kind calls it host-side after the run, with bytes it pulled. The empty-dispatch property a CodeAct-class kind rests on survives intact. The shapes look similar enough that a reader will assume `denied_capabilities={HOST_TOOLS}` blocks artefact landing — it does not, and a host that wants that hard stop denies `FILES_OUT`.

**It is not a second write path into the workspace.** A kind that lands artefacts where the agent's own file tools write has given model-written code an unapproved `file_access_write`, and one that can overwrite an existing file has given it a way to influence a *different* kind on the next call — a `.bicep` a validator compiles, a config another tool reads. `(key, kind)` sandbox identity ([#84](https://github.com/sokolaidev/maf-extensions/issues/84)) separates sandboxes; it does nothing about host state they both touch. The obligations that follow are on the glue, not on each host: a dedicated artefact namespace, never overwrite, and a landing name derived from the guest's only by sanitisation.

## Writing a kind that collects artefacts

The glue supplies `collect_outputs(sandbox, spec, sink)`; a kind's tool body calls it after `exec` and formats the references it returns. Five things a kind author has to get right, four of which the glue can carry:

1. **Per-run output directory.** `acquire` is get-or-create and `work_dir` survives across calls, so a fixed output directory collects the *previous* run's artefacts and reports them as fresh. This is the same trap `execute_code` already handles for its program file by rewriting one fixed name every call. The glue takes the run-scoped subdir approach — a fresh directory per collection — rather than growing a delete primitive that would need a guest-OS assumption; the disk it leaves behind is bounded by the sandbox's own lifecycle.
2. **Tell the model where to write.** The output directory has to appear in the tool's description, because a program that writes its PNG next to itself produces nothing collectable and no error. State the directory and state that only files written there come back.
3. **Do not put bytes in the result.** Return the references `deliver` gave you. A kind that base64s an artefact into the tool result has reintroduced exactly the convention this capability exists to remove.
4. **Require the capability only when you will use it.** Adding `FILES_OUT` to a spec's `requires` makes `ensure_can_serve` refuse every backend that lacks it, and `sandboxed_tool` attaches `[]` for an unconfigured host — so a kind that requires it unconditionally *silently un-attaches its own tool* for every host on a backend that has not implemented it yet. Grow `requires` from whether a sink was supplied.
5. **Sanitised failures still apply.** A host's `deliver` can raise with a connection string or a container URL in the message. It goes through `error_detail` into the log like every other await in the ladder, and the model gets the fixed sentence.

## Implementing `FILES_OUT` in a backend

- **Declare it honestly or not at all.** `FILES_OUT` in `capabilities` is a claim the router trusts; there is no router-side emulation, and there must not be, because a laundered capability makes every downstream refusal meaningless.
- **ACA Sandboxes** have a native file API and are the reference implementation: read, list and stat exist, so entries carry real sizes and the cap is enforced at the door.
- **wslc** should do **tar-out**, not base64-over-exec: its `write_file` is already a one-entry tar on stdin, and the reverse direction is binary-safe, streams, and carries the type bit that makes confinement structural. Base64 through `exec` is the fallback, and a poor one — `ExecResult.stdout` is `str`, so an 8 MiB artefact becomes ~11 MiB of text through a pipe, interleavable with anything else the command printed. It stays available for backends with no file channel at all, opt-in, with its own lower maxima.
- **The micro-VM standard gains a clause.** Leg 4 admits "files in, results out" as declared channels; for a backend claiming `microvm` *and* `FILES_OUT`, the declared channel is reads confined to `work_dir` with non-regular entries refused. A backend whose read path can be walked out of `work_dir` does not meet leg 4, whatever its hypervisor does.

## Worked example: a diagram kind

A kind with one tool, `render_diagram(dot: str)`: the model writes Graphviz DOT, the sandbox renders it, a PNG comes back as a reference. It is the smallest workload that exercises every part of this document — input the model authored, a binary artefact, a real backend, and nothing the model could have produced by reasoning.

```python
def diagram_sandbox_spec(image: str | None = None) -> SandboxSpec:
    return SandboxSpec(
        kind="diagram-generator",
        image=image,
        egress_allow=(),                                                   # renders, does not fetch
        work_dir=_WORK_DIR,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
        files_out=TransferLimits(max_bytes_per_file=_PNG_CAP, max_total_bytes=_PNG_CAP, max_files=1),
    )
```

What it demonstrates, in the order a reader meets it: the source file goes in through `FILES_IN` as file content, so no shell sees model text and the `exec` stays a fixed argv; `egress_allow=()` holds because rendering is computation; `max_files=1` is a workload property, not a guess — one invocation renders one graph, and a cap of one is the honest statement of that; the artefact leaves through the host's sink and the model receives a reference, so a corrupt PNG cannot become a wall of transcript; and `dot` exiting non-zero on malformed input is a diagnostic the model can act on, which is the T2-over-T0 framing the Bicep kind established.

## Rollout

Sequenced so that each step is releasable and nothing ships a claim it cannot honour:

1. **Protocol** — `EntryKind`, `SandboxEntry`, `TransferLimits`, `SandboxLimits`, the pull pair on `Sandbox`, the two cap fields on `SandboxSpec`, and the `ensure_can_serve` match. No backend declares `FILES_OUT` yet, so nothing changes behaviourally.
2. **Glue** — `OutputSink`, landing-name validation, `collect_outputs`, the run-scoped output directory, and the `sandbox_tool_declarations` fix so a sink counts as an outbound flow.
3. **One backend** — ACAS, natively. This is where the capability becomes real, and where the confinement rules get their tests.
4. **One kind and one sample** — the diagram kind above, on ACAS, end to end.
5. **wslc** — tar-out, so the portability claim samples make ("the same kind, both backends") holds for artefact-producing kinds too.

## Open questions

- **Does `write_file` grow a `bytes` path?** The in-door is `str` and cannot carry a PNG or a spreadsheet. It does not block anything here, but it blocks the first kind that wants to send a *binary* input in, and settling it while the file surface is already being changed is cheaper than a second breaking change later.
- **Should artefact landing be approval-gated by default?** `sandboxed_tool` already takes `approval_mode`, and a kind that writes host state has a better claim on `always_require` than one that returns text. Today a kind hardcodes the value; it may need to follow the sink instead.
- **Is one `deliver` per artefact the right granularity?** A collection of forty files is forty awaits and forty references. A batch form is friendlier to a store with a transaction, and worse for a store that streams. Left per-artefact until a host argues otherwise.
