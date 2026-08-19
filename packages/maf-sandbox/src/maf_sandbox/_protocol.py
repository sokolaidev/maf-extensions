"""The backend-neutral vocabulary every sandbox provider and workload speaks.

Nothing here knows what a sandbox *is* — that is the point.  A workload (``bicep_validate``
today; a GitHub Copilot agent or an Azure CLI surface later) asks for a sandbox and runs a
command; a backend (ACA Sandboxes today; local Docker or an in-process fake later) decides
what actually boots.

The split is what lets the same tool run against any of them unchanged, and it is what makes
"sandboxed" a claim with a *matrix* behind it rather than one uniform guarantee — see
:class:`Isolation`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_CAPABILITIES",
    "DEFAULT_SANDBOX_LIMITS",
    "DEFAULT_TRANSFER_LIMITS",
    "INTEGRITY_RANK",
    "ISOLATION_RANK",
    "Capability",
    "DeclaredOutput",
    "Egress",
    "EntryKind",
    "ExecResult",
    "Identity",
    "Isolation",
    "OutputDisposition",
    "Sandbox",
    "SandboxBackend",
    "SandboxEntry",
    "SandboxKey",
    "SandboxLimits",
    "SandboxSpec",
    "SourceIntegrity",
    "TransferLimits",
    "CallerContext",
    "meets_floor",
]


class Isolation(StrEnum):
    """How strong a backend's boundary is. Declared by the backend, checked by the router.

    This is not documentation: the members are a ladder ordered by :data:`ISOLATION_RANK`,
    and :class:`~maf_sandbox._router.SandboxRouter` refuses anything below the declared floor.
    """

    #: No boundary at all: the workload runs in the host process, with the host's authority.
    #: Tests and local fakes. Named for what it provides rather than for where it runs — the
    #: rung this replaces was ``PROCESS``, which reads as a real boundary and meant the absence
    #: of one. :data:`PROCESS` now names a real one, two ranks above this; it took the
    #: attribute back but deliberately not the string, so a declaration written against the
    #: old meaning still fails rather than being re-ranked upward in silence.
    NONE = "none"
    #: A software boundary inside the host process — a restricted interpreter, a WASM
    #: runtime's fault isolation.
    RUNTIME = "runtime"
    #: A separate OS process: a kernel-enforced address space, sharing the host's kernel and
    #: filesystem with no namespaces. Stronger than a software boundary drawn inside the host's
    #: own address space, where an escape lands beside the host's memory and credentials;
    #: weaker than :data:`CONTAINER`, which is this plus namespaces and cgroups.
    #:
    #: No backend in this package provides it. It is vocabulary for one that does — without it
    #: a backend running untrusted code in a subprocess has no honest rung to declare, and must
    #: either understate itself as :data:`RUNTIME` or overstate itself as :data:`CONTAINER`.
    #:
    #: The value is ``"os_process"`` rather than ``"process"``, and that is the point. This
    #: attribute named the bottom rung until 0.14 and meant the absence of a boundary. Reusing
    #: the name is safe because it is resolved when the code is written; reusing the string
    #: would not be, because a declaration crosses into this vocabulary through
    #: ``Isolation(raw)`` at run time, out of configuration nobody re-reads. So
    #: ``Isolation("process")`` keeps raising :exc:`ValueError` and the old spelling is refused
    #: rather than promoted two ranks into a claim it never made.
    PROCESS = "os_process"
    #: Shared-kernel namespaces and cgroups: the host kernel is in the attack surface.
    CONTAINER = "container"
    #: Syscall interception in a userspace kernel (gVisor-class), between namespaces and hardware.
    HARDENED_CONTAINER = "hardened_container"
    #: A hypervisor boundary with a minimal or absent guest OS, and no ambient identity
    #: reachable from inside (ACA Sandboxes, Firecracker, Kata as configured).
    MICROVM = "microvm"
    #: A dedicated, full VM provisioned for this workload on remote infrastructure.
    VM = "vm"


#: The ladder's order, written down exactly once — every comparison of two rungs goes
#: through it.
ISOLATION_RANK: Mapping[Isolation, int] = {
    level: rank
    for rank, level in enumerate(
        (
            Isolation.NONE,
            Isolation.RUNTIME,
            Isolation.PROCESS,
            Isolation.CONTAINER,
            Isolation.HARDENED_CONTAINER,
            Isolation.MICROVM,
            Isolation.VM,
        )
    )
}


def meets_floor(declared: Isolation, floor: Isolation) -> bool:
    """Whether ``declared`` sits at or above ``floor`` on the ladder."""
    return ISOLATION_RANK[declared] >= ISOLATION_RANK[floor]


class Egress(StrEnum):
    """How precisely a backend can confine what a sandbox reaches. Declared by the backend.

    Backends differ in how much of a spec's allowlist they can express, and the direction they
    miss by is not symmetrical: confining **less** than the spec asks silently widens what the
    workload was designed to reach, while confining **more** only makes the workload fail,
    loudly, at whatever it could not fetch.  So only the first is refused.
    """

    #: Deny by default, allow exactly the hosts a spec names.
    ALLOWLIST = "allowlist"
    #: All or nothing: can deny everything, cannot allow one host and not another.
    CLOSED = "closed"
    #: Cannot confine egress at all — whatever the host can reach, the sandbox can reach.
    UNRESTRICTED = "unrestricted"


class Capability(StrEnum):
    """What a sandbox can *do* — declared by a backend, required by a spec, matched at attach.

    Unlike :class:`Egress`, silence here is a functionality claim rather than a safety one —
    see :data:`DEFAULT_CAPABILITIES`.
    """

    #: Run a command line or argv.
    EXEC = "exec"
    #: Evaluate code in a language runtime, without going through a shell.
    RUN_CODE = "run_code"
    #: Dispatch host-registered functions from inside the sandbox.
    HOST_TOOLS = "host_tools"
    #: Write files into the sandbox before execution.
    FILES_IN = "files_in"
    #: Stat and read files back out, at the paths a spec declared. Reading only, never
    #: discovery.
    FILES_OUT = "files_out"
    #: Enumerate a directory. Split from :data:`FILES_OUT` because Docker has no engine-level
    #: primitive for it, which is also why a declared output is a literal path and not a glob.
    FILES_LIST = "files_list"
    #: Delete what a workload put there. Split from :data:`FILES_IN` because writing and
    #: removing are different powers to grant, and a backend can honestly offer one without
    #: the other. Absent from :data:`DEFAULT_CAPABILITIES`, so a spec asks for it: nothing in
    #: the protocol deleted before this, and a workload that never cleans up is not broken by
    #: a capability it does not require.
    FILES_DELETE = "files_delete"
    #: Any egress at all — how precisely it is confined stays in :class:`Egress`.
    NETWORK = "network"
    #: Snapshot and restore a sandbox for reuse.
    SNAPSHOT = "snapshot"
    #: A platform-attached identity scoped to the sandbox itself.
    ATTACHED_IDENTITY = "attached_identity"


#: What every :class:`Sandbox` already obligates.
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})


class SourceIntegrity(StrEnum):
    """How much a host may trust data a dispatched host tool brings *in* — its source leg.

    The vocabulary MAF's information-flow module already speaks (``source_integrity`` on a
    tool's ``additional_properties``), promoted to an enum here because the host-tools
    aggregate has to *fold* it: ``execute_code``'s own result integrity is the weakest tier
    over every registered source, and "weakest" needs an ordering, which this repository
    requires to be data — see :data:`INTEGRITY_RANK`.
    """

    #: Attacker-influenceable content: fetched documents, search results, anything a model or
    #: the open network shaped. The tracker's own default for an undeclared tool.
    UNTRUSTED = "untrusted"
    #: Deterministic first-party output the host would trust from its own code.
    TRUSTED = "trusted"


#: The integrity ordering, weakest first, written down exactly once — the aggregate's fold
#: goes through it, and an exhaustiveness test asserts every member is ranked.
INTEGRITY_RANK: Mapping[SourceIntegrity, int] = {
    level: rank for rank, level in enumerate((SourceIntegrity.UNTRUSTED, SourceIntegrity.TRUSTED))
}


class Identity(StrEnum):
    """Whose authority a dispatched host tool's body exercises. Its declared identity leg.

    A dispatched body runs **in the host process** and carries whatever authority that
    process carries, so this leg is what makes the surface honest: it is declared per tool,
    aggregated per registry, and deniable per router (``denied_identities``).

    **:data:`APP` is not the safe option, only the declared one.**  It is the application's
    full authority — for a deployed host, its workload identity with every grant it holds —
    and the only real bounds on it are the emptiness of the registry and the dispatch cap.
    Least privilege for dispatched tools comes from what a host registers, never from what it
    declares.

    :data:`USER` is **declarable but not servable**: declaring it must be possible so a
    registry can be written honestly and refused loudly, and serving it must not be until
    per-run token minting, an audience-within-egress check, and the ephemeral ``exec`` env
    channel exist — or a host ships model-orchestrated user authority before anything bounds
    it.  Registering a ``USER`` tool raises the whole ``execute_code`` surface to
    approval-gated; dispatching one is refused with the prerequisites named.
    """

    #: The host application's own authority — everything its process can already do.
    APP = "app"
    #: The end user's delegated authority (on-behalf-of). Declarable, refused at dispatch.
    USER = "user"


class EntryKind(StrEnum):
    """What a path inside a sandbox is — a typed field rather than a mode string to parse.

    No two backends report type the same way: ACAS carries ``is_directory``, a two-way split,
    and leaves everything else in ``mode: str | None``, while Docker's stat carries a Go
    ``ModeSymlink`` bit and an explicit link target.  One vocabulary covers both.

    The four members are the four answers
    :func:`~maf_sandbox.paths.refuse_symlinked_parents` needs per ancestor: keep walking, or
    refuse — as an escape for :data:`SYMLINK`, as ``ENOTDIR`` for anything else non-regular.
    Both are refused either way, so what the split buys is the *reason*, which is the part a
    caller above the backend cannot reconstruct.  A backend that cannot recognise a link
    reports :data:`OTHER` and stays honest, losing only the precision; both shipped backends
    can, so neither does.
    """

    #: A regular file — the only kind :meth:`Sandbox.read_file` will serve.
    FILE = "file"
    DIRECTORY = "directory"
    #: Anything a reader would follow elsewhere. A Windows junction or reparse point belongs
    #: here rather than in :data:`OTHER`: for confinement it is an escape like any other link.
    SYMLINK = "symlink"
    #: A device, socket or fifo — or a link a backend cannot recognise. Never read.
    OTHER = "other"


@dataclass(frozen=True)
class SandboxEntry:
    """One path inside a sandbox, as :meth:`Sandbox.stat_file` and :meth:`Sandbox.list_dir` see it.

    ``path`` is relative to the working directory the call was made against.  ``size_bytes``
    is ``None`` when the backend could not determine it, and **``None`` fails closed**: an
    entry of unknown size is refused rather than read, because coercing it to ``0`` would make
    every size cap read the one file it cannot measure as free.

    A link's ``size_bytes`` is ``None`` for a second reason: what a stat reports for one is
    the length of the target *string*, not of anything readable.
    """

    path: str
    kind: EntryKind
    size_bytes: int | None


class OutputDisposition(StrEnum):
    """Where a declared output's bytes are meant to go. The two flows, kept apart in the spec.

    They answer to different legs of a host's policy — landing is a *sink* and the question is
    confidentiality, while an output the kind parses is a *source* and the question is
    integrity — so the distinction is declared rather than left to convention.
    """

    #: Delivered to the host's output sink; the model gets a reference, never the bytes.
    LAND = "land"
    #: Parsed by the kind that asked for it, and never delivered anywhere.
    CONSUME = "consume"


@dataclass(frozen=True)
class DeclaredOutput:
    """One artifact a workload says it produces, as a literal path this library will resolve.

    That is the whole of what makes a declaration one — not *when* it was written down.  Most
    live in ``SandboxSpec.declared_outputs``, fixed when the tool is built; a workload whose
    names are not knowable then passes the same type to ``collect_outputs(outputs=...)``, which
    for a guest-authored manifest is after the run that produced them.

    ``path`` is **literal** and relative to the sandbox's working directory.  A glob would
    have to be resolved by enumerating a directory, which is the primitive
    :data:`Capability.FILES_LIST` exists to gate, so patterns belong to a kind that requires
    that capability and nowhere else.

    ``media_type`` is declared rather than sniffed: sniffing would let guest-produced content
    decide how the host handles it, and a kind knows what it renders.  ``required=False`` is
    how a workload says an absence is normal — a renderer exiting non-zero produces no file,
    and the model needs that diagnostic rather than a transfer error stacked on top of it.

    ``name`` is the spelling the artifact **lands** under, and it defaults to ``path`` because
    for most kinds the two are the same string.  They come apart as soon as a kind writes into
    a per-call directory — which warm sandbox reuse forces on any kind whose outputs would
    otherwise persist into the next round — since the guest path then carries a run id the
    host has no use for.
    """

    path: str
    disposition: OutputDisposition = OutputDisposition.LAND
    media_type: str | None = None
    required: bool = True
    name: str | None = None


#: So the ceilings below read as sizes rather than as eight-digit literals.
_MIB = 1024 * 1024


@dataclass(frozen=True)
class TransferLimits:
    """How much may cross the boundary in one direction, for one collection.

    All three fields are load-bearing: a byte ceiling alone does not bound a collection, since
    ten thousand files one byte under the per-file cap cost exactly what the cap was written to
    prevent.  ``max_total_bytes`` also bounds host memory as far as the backend allows it to:
    a collection is pulled whole before any of it is delivered, so the cap is what the host
    holds at once — but a backend whose SDK buffers a whole response internally has already
    spent that memory before the bytes are handed over, and no caller-side ceiling can
    retract it.  Over-cap bytes are never *delivered*; the memory bound is best-effort.
    """

    max_bytes_per_file: int
    max_total_bytes: int
    max_files: int

    def within(self, ceiling: TransferLimits) -> bool:
        """Whether every field sits at or below ``ceiling``'s — the match the router applies."""
        return (
            self.max_bytes_per_file <= ceiling.max_bytes_per_file
            and self.max_total_bytes <= ceiling.max_total_bytes
            and self.max_files <= ceiling.max_files
        )


#: One constant for both sides of the match — a spec that says nothing asks for exactly what a
#: backend that says nothing allows, so :meth:`TransferLimits.within` holds by equality and no
#: spec written before this axis existed starts being refused at attach.
DEFAULT_TRANSFER_LIMITS: TransferLimits = TransferLimits(
    max_bytes_per_file=8 * _MIB, max_total_bytes=32 * _MIB, max_files=64
)


@dataclass(frozen=True)
class SandboxLimits:
    """A backend's transfer ceilings, one :class:`TransferLimits` per direction."""

    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS
    files_out: TransferLimits = DEFAULT_TRANSFER_LIMITS


#: What a backend declaring no ``limits`` is read as.  Silence here follows :class:`Egress`
#: rather than :data:`DEFAULT_CAPABILITIES`: a cap is a safety claim, so a spec asking for more
#: than this is refused rather than assumed to be fine.
DEFAULT_SANDBOX_LIMITS: SandboxLimits = SandboxLimits()


@dataclass(frozen=True)
class SandboxKey:
    """Identifies the one sandbox a caller may reach.

    ``scope`` is the host's user/tenant scope and ``thread_id`` the conversation; both come
    from the host's request context and never from model input, because a model-supplied
    value here would let one conversation address another's sandbox.  ``agent_dir`` keys the
    sandbox to a single agent, so two agents in one conversation do not share a
    filesystem.
    """

    scope: str
    thread_id: str
    agent_dir: str


@dataclass(frozen=True)
class SandboxSpec:
    """What a sandbox of a given kind needs, in terms no backend is privileged by.

    ``kind`` names the workload (``"bicep"`` today), and it is **part of the sandbox's
    identity, not a display label**: a backend must never serve two kinds from one sandbox,
    because the first spec to arrive would decide the image and the egress policy for both —
    see :meth:`SandboxBackend.acquire`.  ``image`` is a reference the **backend** resolves,
    and nothing here parses it.  ``repository:tag`` is the usual shape, and *where* images live
    is a property of the deployment, so a backend may complete an unqualified reference from
    its own configuration.  But which namespace a given string names is the backend's rule to
    state, in the backend's own documentation — a Docker backend hands it to ``docker run``;
    the ACAS backend resolves it against its sandbox group.  This field holding no opinion is
    what lets a backend read a second namespace out of it without changing it, and what keeps
    the promise in this docstring's first line.  ``image_id`` is an escape hatch for a
    backend-native pinned id that skips resolution entirely.

    ``egress_allow`` is an allowlist of hostnames — **everything not listed is denied**, so
    an empty tuple means no network at all.  Stating it positively is deliberate: a spec that
    forgets to mention egress gets the closed configuration, not the open one.

    ``work_dir`` is the guest-side directory a workload's paths resolve against, and it is
    **guest-native**: the host states it to suit the image it configured, and nothing rewrites
    it.  Not because translating would be undesirable but because it is not possible — a kind
    derives absolute paths from this field and passes them into :meth:`Sandbox.exec`'s argv,
    and a backend cannot find a path inside an opaque argv without parsing arbitrary command
    lines.  An argv *sequence* protects against quoting, not against paths within the
    arguments.  ``/maf-sandbox/work`` is a default, not a requirement.  A workload must not read the
    guest's platform *out* of this field, and nothing here validates it against one — that a
    kind can depend on its guest's OS, and that no axis yet declares or matches it, is a gap
    kept deliberately additive so a platform axis lands without a breaking change (issue
    #111).

    ``requires`` names the capabilities the workload cannot run without, and ``min_isolation``
    the weakest boundary it accepts anywhere.  A spec may **raise** the host's floor and never
    lower it, and ``None`` means no opinion — which is not :data:`Isolation.NONE`, however
    alike the two now read.  ``None`` declines to constrain the floor at all; ``Isolation.NONE``
    constrains it to the bottom rung, which is the weakest opinion there is rather than the
    absence of one.

    ``declared_outputs`` names the artifacts the workload produces, literally and in advance;
    it is spelled long because ``outputs=`` already means marker-keyed scripted stdout on the
    in-process fake, and the two would meet in one expression in every kind's tests.
    ``files_in`` and ``files_out`` are the workload's own transfer caps per direction — a
    backend declares its own ceilings, and the router refuses a spec asking above them.

    ``outputs_named_at_call_time`` says this workload lands artifacts it cannot name here.
    Setting it obliges the same three things a declared ``LAND`` output does — a sink, the
    outbound confidentiality cap, and :data:`Capability.FILES_OUT` in ``requires`` — and is
    what ``collect_outputs(..., outputs=...)`` refuses to run without.  It composes with
    ``declared_outputs`` rather than replacing it, and ``files_out`` caps the union.

    ``identities`` names whose authority the workload's dispatched host tools exercise —
    derived from its registry (:attr:`~maf_sandbox.HostToolAggregate.identities`, which seals
    that registry as it answers), never invented.  Declared on the spec so the router can
    refuse it at attach
    (``denied_identities``), the same moment every other posture question is answered; a
    workload that dispatches nothing declares nothing.
    """

    kind: str
    image: str | None = None
    image_id: str | None = None
    egress_allow: tuple[str, ...] = ()
    work_dir: str = "/maf-sandbox/work"
    # `dict[str, str]` rather than a bare `dict` as the factory: the bare builtin gives a
    # strict type checker `dict[Unknown, Unknown]` to work with, and this package's own
    # pyright config is strict. The subscripted form is callable and constructs the
    # identical empty dict.
    labels: dict[str, str] = field(default_factory=dict[str, str])
    requires: frozenset[Capability] = DEFAULT_CAPABILITIES
    min_isolation: Isolation | None = None
    declared_outputs: tuple[DeclaredOutput, ...] = ()
    files_in: TransferLimits = DEFAULT_TRANSFER_LIMITS
    files_out: TransferLimits = DEFAULT_TRANSFER_LIMITS
    # Appended rather than grouped with `declared_outputs`, where it reads better: this is a
    # public dataclass and is not keyword-only, so inserting a field rebinds every positional
    # argument after it — a caller's `files_in` would silently become this flag.
    outputs_named_at_call_time: bool = False
    identities: frozenset[Identity] = frozenset()


@dataclass(frozen=True)
class ExecResult:
    """The result of one command run inside a sandbox."""

    stdout: str
    stderr: str = ""
    exit_code: int = 0


@runtime_checkable
class Sandbox(Protocol):
    """A running sandbox a workload can put files into, run commands in, and read back out of.

    The pull surface — :meth:`stat_file`, :meth:`read_file`, :meth:`list_dir` — is gated by
    :data:`Capability.FILES_OUT` and :data:`Capability.FILES_LIST`, and a backend declaring
    neither may raise from all three.  The attach gate refuses such a spec before the workload
    ever runs — ``sandboxed_tool`` refuses a spec that declares outputs without requiring
    :data:`Capability.FILES_OUT`, and the router's capability match refuses a backend that
    cannot serve it — so no kind has to feature-detect here.

    :meth:`remove` is gated by :data:`Capability.FILES_DELETE` and is **not** covered by that
    last sentence: no spec field implies a removal, so a kind that cleans up must put the
    capability in ``requires`` itself.  Omit it and the router may hand back a backend whose
    ``remove`` raises :class:`NotImplementedError` — from a ``finally``, over whatever the run
    was already reporting.

    ``working_directory`` is a parameter on those three exactly as it is on :meth:`exec`,
    because no sandbox object knows the spec's ``work_dir``: it arrives per call or not at all,
    and a pull surface without it would assign the confinement duty to a layer with no way to
    discharge it.  Their ``path`` is POSIX-shaped and relative to it, and one resolving outside
    it is refused.  :meth:`write_file` is the residual asymmetry — it takes an absolute guest
    path and has no path grammar — and unifying the two is a larger change than this.

    **Confinement is a duty of all three, and it is not a check on the argument string.**  A
    path whose *parent* is a link satisfies every lexical test and still reads outside: with
    ``out -> /etc``, ``out/hostname`` stats as a regular 12-byte file.  Discharge it with
    :func:`~maf_sandbox.paths.refuse_symlinked_parents` rather than by writing the walk again —
    it is where the two refusals a caller must be able to tell apart are defined, and
    :mod:`maf_sandbox.conformance` is the same duty as probes, for holding a backend that
    writes its own.
    """

    async def write_file(self, path: str, content: str | bytes) -> None:
        """Write ``content`` to ``path`` inside the sandbox.

        ``str`` means UTF-8 whatever the host's locale says; ``bytes`` is written as given, and
        is what an in-door carrying a PNG or a spreadsheet needs.
        """
        ...

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command`` inside the sandbox, bounded by ``timeout`` seconds.

        **A** :class:`TimeoutError` **from this method means that bound expired, and nothing
        else.** A backend with an independent, shorter ceiling of its own must not surface it
        as one — raise something else, or let ``timeout`` govern. Callers derive the bound
        they pass from a budget they own, so they read its expiry as their own budget running
        out; a backend borrowing the same exception for a different limit makes that reading
        false, and the caller has no way to tell.

        ``command`` accepts two shapes, and they are not interchangeable:

        - A **sequence** (``["bicep", "build", path]``) is quoted for you before it reaches
          a shell, and is the safe default whenever any element could contain whitespace or
          a shell metacharacter — a file store path, in particular, is not agent-controlled
          but is still text neither side should have to prove is free of ``;`` or ``$()``.
        - A **string** (``"bicep build … 2>&1 || true"``) is a shell command line, evaluated
          by a shell inside the sandbox. Use it only when the command genuinely needs shell
          features a sequence cannot express — redirection, ``||``, ``&&`` — and every part
          of it is a fixed template with nothing but an already-validated path interpolated.
        """
        ...

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Describe ``path``, or return ``None`` when nothing is there.

        Stat is the contract, not an optimisation: a caller stats, refuses anything over its
        cap or whose ``size_bytes`` came back ``None``, and only then reads.  The alternative —
        counting bytes as they stream — is unavailable on a backend whose SDK buffers the whole
        response internally, which the reference one does.

        Stat is ``lstat``-like: the **final** component is described rather than refused —
        :data:`EntryKind.SYMLINK` is how a caller learns it is a link.  Its *parents* are still
        walked, because a stat through one reports a type and a size from outside the working
        directory even though no byte crosses.
        """
        ...

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Read the regular file at ``path``, refusing anything over ``max_bytes``.

        Bytes, never text: decoding here would corrupt every artifact that is not text, and the
        caller already declared the media type.  Only :data:`EntryKind.FILE` is served — a
        symlink is refused whether or not its target would have resolved somewhere legitimate,
        because that judgement is made with the guest's filesystem in view and answered with
        whichever one the reader can actually see.  Anything else is refused with an
        :class:`OSError`, and every parent is walked first.

        ``max_bytes`` is a **refusal, never a truncation**: half a PNG returned as success is
        an artifact the host cannot tell from a whole one.  Refuse with
        ``SandboxTransferCapExceeded``.  It is the caller's own ceiling handed down so a
        backend that can stop early does — the stat-ed size clamped by what the collection has
        left — and a backend whose SDK buffers the whole response before returning it can only
        refuse after the fact, which is why the caller re-counts what actually arrived.
        """
        ...

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Delete ``path``, and everything under it when ``recursive``.

        Three rules a caller depends on. A path that is not there is **success** — cleanup runs
        in a ``finally`` and must not report a second failure over the first. A link is
        **removed, never followed**, since resolving one would unlink a target outside the
        boundary. A *directory* is refused without ``recursive``, empty or not: a backend with
        no enumeration primitive cannot tell an empty one from a full one.

        Raises:
            ValueError: A path outside ``working_directory``, one reached through a link, or
                the working directory itself — the confinement refusal the pull surface makes.
            OSError: A directory without ``recursive``, or a removal the guest refused.
            NotImplementedError: The backend does not declare
                :data:`Capability.FILES_DELETE`. Require it rather than catching this.
        """
        ...

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Enumerate the entries directly under ``path``.

        Named apart from :attr:`CallerContext.list_files` on purpose: that is the host's
        allowlist and the most trusted enumeration in the system, this is the least trusted
        one, and both are in scope inside a kind's tool body.

        The walk runs one component deeper here — ``include_self`` — because an enumeration
        passes through a link as readily as a read does.  A listed link is reported as
        :data:`EntryKind.SYMLINK`, not hidden: a name handed back with its type erased is a
        name read without the warning.
        """
        ...


@runtime_checkable
class SandboxBackend(Protocol):
    """A provider that can hand out sandboxes.

    ``acquire`` is get-or-create: a backend is expected to reuse a warm sandbox for the same
    key **and kind** across calls, because a workload's fix-round loop would otherwise pay a
    cold start every iteration.  A sandbox's identity is ``(key, spec.kind)`` — two specs
    with different kinds must never share one, whatever their key: a sandbox created for one
    kind carries that kind's image and egress policy, and handing it to another kind would
    run the second workload under the first one's network policy.

    ``dispose_scope`` exists separately from ``dispose`` because a conversation delete has to
    reach sandboxes this process never created — a multi-replica host serves the delete
    wherever it lands.  A backend that only consults its own memory there leaves billable
    sandboxes running.

    A backend may also declare ``capabilities: frozenset[Capability]``, matched by the router
    against a spec's ``requires``, and ``limits: SandboxLimits``, the transfer ceilings a spec
    may not ask above.  Neither is a member of this Protocol, deliberately:
    :func:`~typing.runtime_checkable` enforces member *presence*, so declaring them here would
    stop every backend written before them from being a ``SandboxBackend`` at all.  With
    :attr:`egress` that makes three optional declarations read by ``getattr``; a fourth is the
    signal to collapse all of them into one declarations object.
    """

    @property
    def name(self) -> str:
        """Short identifier used in configuration, e.g. ``"acas"``."""
        ...

    @property
    def isolation(self) -> Isolation:
        """The rung this backend sits on, checked against the host's floor at construction.

        A value outside :class:`Isolation` is refused rather than ranked: the router cannot
        tell whether an unknown boundary is stronger or weaker than the one required.
        """
        ...

    @property
    def egress(self) -> Egress:
        """One of the :class:`Egress` members, read before a workload's tool is attached.

        Not declaring it is read as :data:`Egress.UNRESTRICTED`, and refused.
        """
        ...

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Two acquires for one key can be in flight at once: the function calls in a single
        assistant message are executed concurrently, so a workload's tool body runs twice
        over.  An unguarded read-then-create then hands out two sandboxes where the caller
        expects one, and only one of them is remembered.  Serialise the get-or-create, or
        derive a name the provider will reject a duplicate of.
        """
        ...

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key``, if any. Best-effort: never raises.

        Every kind's, because a key may own one sandbox per kind and this method takes no
        kind: a caller releasing a key means all of it.
        """
        ...

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every sandbox for ``(scope, thread_id)``, returning how many. Never raises."""
        ...


@dataclass(frozen=True)
class CallerContext:
    """How the host identifies the caller and enumerates the files it may act on.

    ``current_scope`` and ``current_thread_id`` are **callables read at call time** (they are
    typically ``ContextVar`` lookups) rather than values, which is what keeps the
    :class:`SandboxKey` a property of the host's request context instead of something a
    caller — or a model — can supply.

    ``list_files`` receives the file store and returns the paths the caller may act on.
    Workloads use it as their injection-pinning boundary: only a name present in that listing
    is ever substituted into a command.
    """

    current_scope: Callable[[], str]
    current_thread_id: Callable[[], str | None]
    list_files: Callable[[Any], Awaitable[list[str]]]
