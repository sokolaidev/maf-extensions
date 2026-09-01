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
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_BACKEND_DECLARATIONS",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_SANDBOX_LIMITS",
    "DEFAULT_TRANSFER_LIMITS",
    "INTEGRITY_RANK",
    "ISOLATION_RANK",
    "ISOLATION_SCOPE_RANK",
    "BackendDeclarations",
    "Capability",
    "DeclaredOutput",
    "DisposalCode",
    "DisposalFailure",
    "Egress",
    "EntryKind",
    "ExecResult",
    "HostToolAggregate",
    "Identity",
    "Isolation",
    "IsolationScope",
    "OsFamily",
    "OutputDisposition",
    "Sandbox",
    "SandboxBackend",
    "SandboxEntry",
    "SandboxKey",
    "SandboxLimits",
    "SandboxQueuedTimeout",
    "SandboxSpec",
    "ScopePurge",
    "SourceIntegrity",
    "TransferLimits",
    "fold_disposal_failures",
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


class IsolationScope(StrEnum):
    """How much of a conversation one sandbox serves. Asked for by a spec, enforced by a backend.

    A different axis from :class:`Isolation`, which says how strong the boundary is: a microVM
    serving a whole conversation and a container serving one call answer different questions,
    and a workload can need either answer.  Ordered by :data:`ISOLATION_SCOPE_RANK`, and a spec
    may raise the host's floor and never lower it — the rule :attr:`SandboxSpec.min_isolation`
    follows.
    """

    #: One sandbox per ``(key, kind)``, reused across a conversation's calls — the get-or-create
    #: :meth:`SandboxBackend.acquire` describes.  What a call leaves behind outlives it, bounded
    #: by what the reclaim removes.
    CONVERSATION = "conversation"
    #: A sandbox created for one tool call and destroyed when it returns.  Two calls in one
    #: assistant message then share no filesystem, and a reclaim that fails leaves data no later
    #: call can address.  Paid for with a cold start per call.
    CALL = "call"


#: The order, least separated first, written down exactly once — every comparison of two scopes
#: goes through it, and an exhaustiveness test asserts every member is ranked.
ISOLATION_SCOPE_RANK: Mapping[IsolationScope, int] = {
    scope: rank for rank, scope in enumerate((IsolationScope.CONVERSATION, IsolationScope.CALL))
}


class Egress(StrEnum):
    """A network posture on one axis, least-isolated to most: ``UNRESTRICTED``, ``ALLOWLIST``,
    ``CLOSED``.

    It is both what a **workload runs in** (:attr:`SandboxSpec.egress`, one mode, default
    ``CLOSED``) and what a **backend can enforce** (:attr:`BackendDeclarations.egress_modes`, a
    set).
    The router serves a workload iff its mode is in the backend's set, and refuses otherwise —
    never substituting a different mode.  Confining **less** than asked silently widens what the
    workload reaches; confining **more** hands it a posture it was not built for; so neither is
    done in place of the other.  See ``docs/sandbox/research/egress-resolution.md``.
    """

    #: Reach anything the host can — no confinement. The least isolated.
    UNRESTRICTED = "unrestricted"
    #: Deny by default, allow exactly the hosts a spec names (:attr:`SandboxSpec.egress_allow`).
    ALLOWLIST = "allowlist"
    #: No network at all. The most isolated, and the default a spec gets if it says nothing.
    CLOSED = "closed"


class Capability(StrEnum):
    """What a sandbox can *do* — declared by a backend, required by a spec, matched at attach.

    Unlike :class:`Egress`, silence here is a functionality claim rather than a safety one —
    see :data:`DEFAULT_CAPABILITIES`.
    """

    #: Run a command line or argv.
    EXEC = "exec"
    #: Evaluate code in a language runtime, without going through a shell.
    RUN_CODE = "run_code"
    #: Call host-registered functions from inside the sandbox.
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
    #: Snapshot and restore a sandbox for reuse.
    SNAPSHOT = "snapshot"
    #: A platform-attached identity scoped to the sandbox itself.
    ATTACHED_IDENTITY = "attached_identity"


#: What every :class:`Sandbox` already obligates.
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})


class OsFamily(StrEnum):
    """A guest's path grammar and argv quoting. **Not its operating system, and not what is
    installed in it.**

    Two members rather than three, because only this split decides anything here: argv
    quoting, the separator and root shape, the reserved device names, and what a junction or
    reparse point reports as. Nothing in this package branches on Linux against macOS once
    command availability is set aside, and the one candidate — case sensitivity — is a
    property of a filesystem rather than of an OS, so no OS name would imply it.

    **Reaching for this to decide whether a command exists is the mistake it cannot catch.**
    A distroless image is POSIX and has no shell; a macOS guest is POSIX and has no
    ``setsid``. What a guest has installed is a property of its image, which one backend may
    be handed many of — see ``docs/sandbox/guest-platform-and-commands.md``, which settles
    where that question is answered instead.
    """

    #: A ``/`` root, POSIX quoting: Linux, macOS, the BSDs.
    POSIX = "posix"
    #: Drive letters and UNC, Windows argv quoting rules.
    WINDOWS = "windows"


class SourceIntegrity(StrEnum):
    """How much a host may trust data a host tool brings *in* — its source leg.

    The vocabulary MAF's information-flow module already speaks (``source_integrity`` on a
    tool's ``additional_properties``), promoted to an enum here because the host-tools
    aggregate has to *fold* it: ``execute_code``'s own result integrity is the weakest level
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
    """Whose authority a host tool's body exercises. Its declared identity leg.

    A host tool's body runs **in the host process** and carries whatever authority that
    process carries, so this leg is what makes the surface honest: it is declared per tool,
    aggregated per registry, and deniable per router (``denied_identities``).

    **:data:`APP` is not the safe option, only the declared one.**  It is the application's
    full authority — for a deployed host, its workload identity with every grant it holds —
    and the only real bounds on it are the emptiness of the registry and the host-tool-call cap.
    Least privilege for host tools comes from what a host registers, never from what it
    declares.

    :data:`USER` is **served only where a host mints it**.  A registry given
    ``mint_user_identity`` asks it when a ``USER`` call is reached and passes what it answers
    to the body as ``user_identity``, keeping the first usable answer for the rest of the run
    and asking again after one that failed; a registry without one keeps refusing the call,
    which is what lets a
    registry be written honestly on a host that serves no user authority at all.  Registering
    a ``USER`` tool raises the whole ``execute_code`` surface to approval-gated either way.

    What the library enforces is *where* the authority comes from and *how often* it is asked
    for — not what it is worth.  A callback free to answer with the same long-lived credential
    every time satisfies every check here, so scoping the authority to the run it is asked for
    is the host's to keep, and ``run_id`` is passed so it can.
    """

    #: The host application's own authority — everything its process can already do.
    APP = "app"
    #: The end user's delegated authority (on-behalf-of). Declarable always; served only by a
    #: registry that mints it, and refused at call time by one that does not.
    USER = "user"


class EntryKind(StrEnum):
    """What a path inside a sandbox is — a typed field rather than a mode string to parse.

    No two backends report type the same way: ACAS carries ``is_directory``, a two-way split,
    and leaves everything else in ``mode: str | None``, while Docker's stat carries a Go
    ``ModeSymlink`` bit and an explicit link target.  One vocabulary covers both.

    The four members are the four answers
    :func:`~maf_sandbox.paths.refuse_symlinked_ancestors` needs per ancestor: keep checking, or
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

    ``call_id`` is empty for a conversation-scoped sandbox and names one tool call for an
    :data:`IsolationScope.CALL` one, where get-or-create is bypassed rather than warmed.  A
    backend folds it into whatever names a sandbox — the container name, the label set, its own
    registry — and one that does not must not declare that scope: two calls would resolve to one
    sandbox, which is the sharing the scope exists to end.
    """

    scope: str
    thread_id: str
    agent_dir: str
    #: Defaulted, so a key written before this axis existed constructs unchanged and still means
    #: the conversation-scoped sandbox it always meant.
    call_id: str = ""


@dataclass(frozen=True)
class HostToolAggregate:
    """What a host-tool registry's contents mean for the one model-facing tool that serves them.

    Lives here rather than beside the registry because :class:`SandboxSpec` carries one: an
    annotation a spec cannot resolve at runtime breaks ``typing.get_type_hints``, and every field
    below is already this module's vocabulary.  :mod:`maf_sandbox._host_tools` builds it and
    re-exports the name.

    Derived per leg, over the relevant subset, never replacing the host's classification of the
    tool itself as an exec sink under untrusted taint — refining it:

    - ``result_integrity`` is the weakest level over *sources only* — a sink-only or pure tool
      must not drag the result to untrusted, and a registry with no sources has no integrity
      opinion at all (``None``): the workload's own default stands.
    - ``outbound_caps`` is every declared sink cap, verbatim and unfolded.  Confidentiality
      values are opaque host vocabulary with no ordering, and this repository requires an
      ordering to be data before anything ranks by it — so more than one distinct cap is the
      host's to reconcile, never this package's to guess between.
    - ``identities`` and ``requires_approval``: any :data:`Identity.USER` tool raises the whole
      surface to approval-gated, because a single host-tool call may exercise the user's delegated
      authority.
    - ``has_undeclared`` marks a registry serving unstamped tools (the gate off).  Each such
      tool already failed safe into the folds above — an untrusted source, an
      :data:`Identity.APP` identity — and the flag is how a host notices the degrade without
      diffing the folds.
    - ``response_limits`` and ``max_host_tool_calls_per_run`` are the registry's own ceilings,
      carried verbatim so the router can fold the transport's worst case into the transfer-limit
      match when it serves the spec — reported policy, not a fold performed here.  The count is
      load-bearing there and not only the bytes: it is what turns "one response" into "how many
      files, and how many refusals nothing debits".
    """

    result_integrity: SourceIntegrity | None
    outbound_caps: frozenset[str]
    identities: frozenset[Identity]
    requires_approval: bool
    has_undeclared: bool
    response_limits: TransferLimits
    max_host_tool_calls_per_run: int


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

    ``egress`` is the one network posture the workload runs in — an :class:`Egress` mode,
    default :data:`Egress.CLOSED` (no network).  The router serves it only on a backend that can
    enforce that exact mode and refuses otherwise, never substituting another; see
    ``docs/sandbox/research/egress-resolution.md``.  ``egress_allow`` is the payload of an
    :data:`Egress.ALLOWLIST` run — the hostnames reached, **everything not listed denied** — and
    is consulted only in that mode.  A non-empty ``egress_allow`` therefore requires
    ``egress is Egress.ALLOWLIST``, refused here otherwise: naming hosts with no network to reach
    them on is incoherent, not resolved into a surprise.  The ``CLOSED`` default keeps the
    fail-closed property: a spec that says nothing about egress gets no network.

    ``work_dir`` is the guest-side directory a workload's paths resolve against, and it is
    **guest-native**: the host states it to suit the image it configured, and nothing rewrites
    it.  Not because translating would be undesirable but because it is not possible — a kind
    derives absolute paths from this field and passes them into :meth:`Sandbox.exec`'s argv,
    and a backend cannot find a path inside an opaque argv without parsing arbitrary command
    lines.  An argv *sequence* protects against quoting, not against paths within the
    arguments.  ``/maf-sandbox/work`` is a default, not a requirement.  A workload must not read the
    guest's platform *out* of this field, and nothing here validates it against one: the axis
    that declares and matches a guest's shape is :class:`OsFamily`, stated by a spec in
    :attr:`requires_os_family` and by a backend in ``os_families``.  Reading it out of a path
    instead would infer a fact the field never promised — a ``/``-rooted ``work_dir`` says
    nothing about the guest, because the host typed it.

    ``requires_os_family`` is the shape this workload's commands and scripts are written for,
    and the router refuses a backend whose ``os_families`` does not hold it.  ``None`` — the
    default — asks nothing and is refused by nothing, which is what keeps every spec written
    before this axis existed serving exactly as it did.  It says nothing about what is
    installed in the guest; :class:`OsFamily` carries why.

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

    ``identities`` names whose authority the workload's host tools exercise, read off
    ``host_tools`` (:attr:`~maf_sandbox.HostToolAggregate.identities`, which seals that registry
    as it answers) and settable nowhere.  The router refuses a denied one at attach
    (``denied_identities``), the same moment every other posture question is answered; a
    workload that wires nothing declares nothing.

    ``host_tools`` is the sealed surface a wired registry answers with
    (:meth:`~maf_sandbox.HostToolRegistry.aggregate`), or ``None`` when nothing is callable.
    The router folds its ceilings into the transfer-limit match transiently, mutating nothing
    here.  It is where ``identities`` comes from, so those two cannot disagree; what a spec
    still owes is :data:`Capability.HOST_TOOLS` in ``requires``, refused below otherwise,
    because the router reads that half of its posture from the field rather than the surface.

    ``isolation_scope`` is how much of a conversation one sandbox serves.  The default shares one
    across every call in the conversation; :data:`IsolationScope.CALL` asks for one created for
    this call and destroyed when it returns, which is what a workload handling labelled data
    needs — two calls in a single assistant message otherwise run in one filesystem, and a
    reclaim that fails leaves what the first wrote where the second can read it.  A floor rather
    than a setting, like ``min_isolation``: a host raises it for every workload it serves, a spec
    raises it further, and the router refuses a backend that cannot enforce what the two resolve
    to.  It buys that with a cold start per call, which is the whole of its cost and the reason
    it is not the default.  Like ``egress`` it is normalised on construction, so a plain string
    serves exactly as the member does and anything else raises here.
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
    # Appended, like the two above, so it cannot rebind a positional caller's argument.
    egress: Egress = Egress.CLOSED
    # Appended for the same reason, and *after* ``egress`` because that field was here first.
    # Which of two defaulted fields comes last is arbitrary to a keyword caller and
    # load-bearing to a positional one, so the order follows arrival rather than taste.
    requires_os_family: OsFamily | None = None
    # Appended last, like the defaulted fields above, so it cannot rebind a positional caller's
    # argument.
    host_tools: HostToolAggregate | None = None
    # Appended after it, for that same reason.
    isolation_scope: IsolationScope = IsolationScope.CONVERSATION

    @property
    def identities(self) -> frozenset[Identity]:
        """Whose authority this workload's host tools exercise — the surface's own, or none.

        Derived rather than declared, so a spec cannot name a posture its surface does not
        carry: that disagreement is what ``denied_identities`` would have been read past.
        """
        return self.host_tools.identities if self.host_tools is not None else frozenset()

    def __post_init__(self) -> None:
        # Coerced before anything reads them, the way `HostToolDeclaration` coerces its own
        # identity: a `StrEnum` member equals its string, so a caller passing ``"call"`` satisfies
        # every ``==`` and fails every ``is`` — and the two checks that make a per-call sandbox a
        # boundary, the key's call id and the router's refusal, are both ``is``. A value that is
        # not a member raises here rather than degrading to a shared sandbox somewhere later.
        object.__setattr__(self, "egress", Egress(str(self.egress)))
        object.__setattr__(self, "isolation_scope", IsolationScope(str(self.isolation_scope)))
        if self.egress_allow and self.egress is not Egress.ALLOWLIST:
            hosts = ", ".join(self.egress_allow)
            raise ValueError(
                f"egress_allow names hosts ({hosts}) but egress is {str(self.egress)!r}: a host "
                f"list is the payload of an {str(Egress.ALLOWLIST)!r} run and has no meaning "
                "without it. Set egress=Egress.ALLOWLIST, or drop the hosts."
            )
        if self.host_tools is None:
            return
        # Only the capability half can disagree: `identities` is read off the surface, so a host
        # denying one meets it whatever the spec says, while `requires` is the spec's own word
        # and a surface it does not ask for would slip past `denied_capabilities`.
        if Capability.HOST_TOOLS not in self.requires:
            raise ValueError(
                "host_tools carries a callable surface but requires does not include "
                f"{str(Capability.HOST_TOOLS)!r}, so a host denying that capability "
                "(denied_capabilities) would serve this workload anyway. Add it to requires."
            )


@dataclass(frozen=True)
class ExecResult:
    """The result of one command run inside a sandbox."""

    stdout: str
    stderr: str = ""
    exit_code: int = 0


class SandboxQueuedTimeout(TimeoutError):
    """A :meth:`Sandbox.run_code` deadline expired before the program ever started.

    Its own type because the caller's next move differs: a program that overran should be made
    smaller, and one that never started should be retried unchanged.  A caller cannot tell
    those apart from a message, and a kind reporting the wrong one to a model sends it to
    rewrite working code.
    """


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
    last sentence. A kind that calls :meth:`remove` directly must put the capability in
    ``requires`` itself; omit it and the router may hand back a backend whose ``remove`` raises
    :class:`NotImplementedError` — from a ``finally``, over whatever the run was already
    reporting. :meth:`reclaim` is behind no capability: every backend implements it.

    ``working_directory`` is a parameter on those four — the pull surface and :meth:`remove` —
    exactly as it is on :meth:`exec`,
    because no sandbox object knows the spec's ``work_dir``: it arrives per call or not at all,
    and a pull surface without it would assign the confinement duty to a layer with no way to
    discharge it.  Their ``path`` is POSIX-shaped and relative to it, and one resolving outside it
    is refused.

    **Confinement is a duty of all five, and it is not a check on the argument string.**  A
    path whose *parent* is a link passes the file name check and still reads outside: with
    ``out -> /etc``, ``out/hostname`` stats as a regular 12-byte file.  The filesystem path
    check is what catches it, so discharge both halves through the bundle for the policy your
    method owes: :func:`~maf_sandbox.paths.confine_resolve_guest_write_path` for
    :meth:`write_file`, :func:`~maf_sandbox.paths.confine_resolve_guest_read_path` for
    :meth:`stat_file` and :meth:`read_file` both,
    :func:`~maf_sandbox.paths.confine_resolve_guest_list_path` for :meth:`list_dir`, and
    :func:`~maf_sandbox.paths.confine_resolve_guest_delete_path` for :meth:`remove`.  Each
    carries what its own policy owes about the final component and about the working directory,
    and the two refusals a caller must be able to tell apart are defined there.
    :mod:`maf_sandbox.conformance` is the same duty as probes, for holding a backend that writes
    its own.  **The stat you hand it must not be answered by the guest** wherever your engine
    offers anything else: a workload asked to describe its own filesystem can answer falsely,
    and that answer is the one this trusts.  :meth:`reclaim` is outside the count of five, for
    the reason its own docstring gives.
    """

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        """Write ``content`` to ``path`` inside the sandbox.

        ``path`` is POSIX-shaped and relative to ``working_directory``; an absolute path resolving
        inside it is accepted. ``str`` means UTF-8 whatever the host's locale says; ``bytes`` is
        written as given, and is what an in-door carrying a PNG or a spreadsheet needs. Parent
        directories are created as needed. A missing component ends the filesystem path check,
        so nothing created by this call can be a link.

        Raises:
            ValueError: If the path is outside, passes through a link, names a link, or is the
                working directory itself.
            NotADirectoryError: If a parent is neither a directory nor a link.

        The filesystem path check and the write are not atomic on any shipped backend; a guest
        that turns a checked component into a link in between wins.  The file name check cannot
        race: it is text arithmetic over arguments nothing else can reach.
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

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Evaluate ``code`` in the guest's language runtime, bounded by ``timeout`` seconds.

        The method :data:`Capability.RUN_CODE` names, as :meth:`exec` is the method
        :data:`Capability.EXEC` names.  A backend declaring that capability implements this;
        one declaring neither may raise :exc:`NotImplementedError`, because the router refuses
        a spec requiring it before any caller arrives — no kind feature-detects.

        There is no ``working_directory``: this surface takes a program, not a path, and a
        backend offering it may have no filesystem to resolve one against.

        **``timeout`` is wall-clock from this call, not from the moment the program starts.**
        A backend that serialises calls on one sandbox — a warm-reuse backend under concurrent
        tool calls, a worker actor — spends part of the budget queued, and that time is the
        caller's just as much as execution is.  The alternative bounds only the running half
        and leaves the waiting half unbounded, which is the failure a timeout exists to
        prevent.

        Raises:
            SandboxQueuedTimeout: the deadline expired while queued; the program never ran.
                Distinct from :exc:`TimeoutError` on purpose — see that class.
            TimeoutError: the program started and overran ``timeout``. As on :meth:`exec`,
                this means that bound expired and nothing else; a backend with a shorter
                ceiling of its own must not surface it as this.

        What the runtime promises a program is the backend's to state, in the backend's own
        documentation, because it differs and this protocol cannot make it uniform: whether
        the value of the last expression comes back or only what the program printed, and what
        is importable inside.  A kind that assumes one shape works on one backend by accident.
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
        checked, because a stat through one reports a type and a size from outside the working
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
        :class:`OSError`, and every ancestor is checked first.

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

        ``path`` is **model-supplied**, which is what buys the confinement duty and the
        capability gate. :meth:`reclaim` is the other half of that split and is neither.

        The reach rule stated on :meth:`reclaim` binds here too, and harder: ``path`` names
        components a guest program may own, and a removal resolves every parent even where it
        unlinks its own operand.

        Raises:
            ValueError: A path outside ``working_directory``, one reached through a link, or
                the working directory itself — the confinement refusal the pull surface makes.
            OSError: A directory without ``recursive``, or a removal the guest refused.
            NotImplementedError: The backend does not declare
                :data:`Capability.FILES_DELETE`. Require it rather than catching this.
        """
        ...

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Remove ``directory`` and everything under it, within ``timeout`` seconds.

        The framework's cleanup. Mandatory, and behind no :class:`Capability`.

        Three rules. The caller created ``directory`` under ``working_directory`` with an
        unguessable name, so no filesystem path check is owed — stated, not checked. The premise is
        not stable: the **guest program** — the payload a kind ran, not the transport files a
        backend put beside it — can have swapped the path, or a parent, for a link before the
        call returned.

        What the contract holds is **reach**, not the mechanism that bounds it: a swap must
        not let the removal delete anything that program could not have deleted itself.
        Removing as the principal the program ran under satisfies that everywhere. Removing
        with more authority satisfies it only where no component of the path was writable by
        that program, and a backend that does so owes the argument for why. What more the
        contract promises is #584's question.

        A directory that is not there is success: this runs in a ``finally``. Anything else
        raises.

        ``directory`` is absolute. Run the removal from ``/``, not from ``working_directory``,
        which may not exist. Not :meth:`remove`: that takes a model-supplied path, owes
        confinement, and sits behind :data:`Capability.FILES_DELETE`.

        Raises:
            ValueError: A path that is not absolute, or fewer than two components from the
                root — a backend refusing a path it cannot place. The guards in this module
                refuse the same shapes, and a backend that repeats them stands on its own:
                this removal is recursive and irreversible, and neither guard should depend
                on the caller having derived the path correctly.
            OSError: The removal was refused or failed.
            TimeoutError: ``timeout`` expired. A subclass of :class:`OSError`.
        """
        ...

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Enumerate the entries directly under ``path``.

        Named apart from :attr:`CallerContext.list_files` on purpose: that is the host's
        allowlist and the most trusted enumeration in the system, this is the least trusted
        one, and both are in scope inside a kind's tool body.

        The filesystem path check runs one component deeper here — ``include_self``, which the
        file name check has no equivalent of — because an enumeration
        passes through a link as readily as a read does.  A listed link is reported as
        :data:`EntryKind.SYMLINK`, not hidden: a name handed back with its type erased is a
        name read without the warning.
        """
        ...


#: Why a disposal did not land, in a word a caller may branch on.
#:
#: ``"unreachable"``: never reached, so nothing was asked of it. ``"timeout"``: unfinished,
#: so whether it landed is not known. ``"refused"``: it answered, and the sandbox is still
#: there. ``"unlisted"``: the query enumerating what to delete failed, so the sweep may be
#: partial. ``"unknown"``: the backend cannot classify it, and may always say so.
DisposalCode = Literal["unreachable", "timeout", "refused", "unlisted", "unknown"]

#: Which code survives when one disposal hits several, most actionable first: ``"unreachable"``
#: outranks ``"refused"`` because it is the one worth retrying.
_DISPOSAL_PRECEDENCE: tuple[DisposalCode, ...] = (
    "unreachable",
    "timeout",
    "refused",
    "unlisted",
    "unknown",
)


@dataclass(frozen=True)
class DisposalFailure:
    """Why a sandbox may still be there: a code to branch on, and the detail to log.

    The code is the part a caller acts on and the only part kept stable. ``detail`` is the
    backend's own sentence, for a log rather than for parsing.
    """

    code: DisposalCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def fold_disposal_failures(failures: Sequence[DisposalFailure]) -> DisposalFailure | None:
    """The one failure that stands for several, or ``None`` when there are none.

    Lives here because all three shipped backends fold, and a rule three packages implement
    separately drifts. The code is the most actionable reported; the detail keeps every
    sentence. A code from outside the vocabulary folds to ``unknown``, which is also how a
    lone failure is normalised.
    """
    if not failures:
        return None
    codes: set[DisposalCode] = {failure.code for failure in failures}
    # Nothing enforces a `Literal` at run time, so default rather than raise on an odd code.
    worst: DisposalCode = "unknown"
    for candidate in _DISPOSAL_PRECEDENCE:
        if candidate in codes:
            worst = candidate
            break
    if len(failures) == 1:
        # Identity for a code of ours; a lone unrecognised one is normalised like several.
        only = failures[0]
        return only if only.code == worst else DisposalFailure(worst, only.detail)
    # `detail`, not `str(failure)`: one shape whether one backend reported or three.
    return DisposalFailure(worst, "; ".join(failure.detail for failure in failures))


@dataclass(frozen=True)
class ScopePurge:
    """What one conversation's purge did: how many sandboxes went, and why any is still there.

    ``undisposed`` follows :meth:`SandboxBackend.dispose`'s reading — a
    :class:`DisposalFailure`, or ``None`` for nothing reported, which a backend unable to check
    also returns.

    It replaced a bare ``int``, so a caller that only ever wanted the count now reads
    :attr:`disposed`. Watch for ``if await purger.purge_scoped_thread(...)``: an instance is
    always truthy, where the count it replaced was not.
    """

    disposed: int = 0
    undisposed: DisposalFailure | None = None


@dataclass(frozen=True)
class BackendDeclarations:
    """What a backend tells the router about itself, in one object read with one ``getattr``.

    Every field's default **is** its silence rule, so a backend that omits one is read exactly
    as a backend that declared nothing at all.  The rules differ and are not
    interchangeable: :attr:`capabilities` is a functionality claim read charitably,
    :attr:`limits` is a safety claim read conservatively, and the two sets are the *absence of
    an answer* — which refuses every ask on :attr:`egress_modes`, where a backend enforcing no
    mode can serve none, and only an asking spec on :attr:`os_families`.

    The router reads this synchronously, before any sandbox exists, so it must be settled by
    the time it asks: a plain attribute or a property over configuration, never an ``async``
    query and never something only a running guest could answer.

    ``isolation`` is not here.  It is a member of :class:`SandboxBackend` itself, because a
    backend with no rung at all cannot be placed against a floor, and stating it twice would
    give a reader two places to look.
    """

    #: What the backend can do, matched against a spec's ``requires``.  Silence is read
    #: charitably: a backend that never heard of the vocabulary still does what
    #: :class:`Sandbox` obligates.
    capabilities: frozenset[Capability] = DEFAULT_CAPABILITIES
    #: The transfer ceilings a spec may not ask above.  Silence is read conservatively — an
    #: undeclared ceiling is the default ceiling, and a bigger ask is refused.
    limits: SandboxLimits = DEFAULT_SANDBOX_LIMITS
    #: The modes the backend can *enforce*, resolved against a spec's
    #: :attr:`SandboxSpec.egress` (see ``docs/sandbox/research/egress-resolution.md``).  Empty
    #: refuses every spec, which is the honest reading: a backend declaring no mode enforces
    #: none.
    egress_modes: frozenset[Egress] = frozenset()
    #: The guest shapes the backend hands out, matched against a spec's
    #: :attr:`SandboxSpec.requires_os_family`.  Empty refuses a spec that asks for a family and
    #: leaves every spec that does not exactly as it was: a backend with no guest in the
    #: operating-system sense — a language runtime, a data-plane API — has no answer to give.
    os_families: frozenset[OsFamily] = frozenset()
    #: How much of a conversation the backend can serve from one sandbox, resolved against a
    #: spec's :attr:`SandboxSpec.isolation_scope`.  The one field whose silence is a claim
    #: rather than the absence of one: empty reads as ``{IsolationScope.CONVERSATION}``, which
    #: is what every backend written before this axis already did.
    #: :data:`IsolationScope.CALL` belongs here only once the backend folds
    #: :attr:`SandboxKey.call_id` into the name it gives a sandbox.
    isolation_scopes: frozenset[IsolationScope] = frozenset()


#: What a backend declaring no ``declarations`` is read as: every field at its own silence rule.
DEFAULT_BACKEND_DECLARATIONS: BackendDeclarations = BackendDeclarations()


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

    A backend may also declare ``declarations: BackendDeclarations`` — what it can do, what it
    will carry, which egress modes it enforces and which guest shapes it hands out.  It is not
    a member of this Protocol, deliberately: :func:`~typing.runtime_checkable` enforces member
    *presence*, so declaring it here would stop every backend written before it from being a
    ``SandboxBackend`` at all.  Declaring neither it nor any of the four
    attributes it replaced is read as :data:`DEFAULT_BACKEND_DECLARATIONS`, and
    :class:`BackendDeclarations` is where each field's silence rule is written down.

    ``declarations`` replaced four separate attributes — ``capabilities``, ``limits``,
    ``egress_modes`` and ``os_families`` — and a backend still carrying any of them is refused
    when the router resolves it, rather than read as silent.  Nothing in the type system can
    catch that migration: none of the four was ever a member here, so ``isinstance`` still
    holds either way, and an unnoticed ``egress_modes`` would turn a working backend into one
    that refuses every spec.
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

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed.

        Two acquires for one key can be in flight at once: the function calls in a single
        assistant message are executed concurrently, so a workload's tool body runs twice
        over.  An unguarded read-then-create then hands out two sandboxes where the caller
        expects one, and only one of them is remembered.  Serialise the get-or-create, or
        derive a name the provider will reject a duplicate of.

        ``key`` may carry a :attr:`SandboxKey.call_id`, and it is part of a sandbox's identity
        exactly as the other three fields are.  A backend deriving its name from three of the
        four hands one sandbox to two calls that asked not to share, so fold the whole key —
        and declare :data:`IsolationScope.CALL` only once it is folded.
        """
        ...

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        """Delete every kind's sandbox for ``key``, if any. Best-effort: never raises.

        Every kind's, because a key may own one sandbox per kind and this method takes no
        kind: a caller releasing a key means all of it.

        **Return a :class:`DisposalFailure` when a sandbox may still be there, or ``None``.**
        ``None`` is read as disposed, and a backend with no way to check returns it too — the
        conflation is with success, because refusing every key served by a backend that cannot
        answer is the wrong direction to fail in.

        The :data:`DisposalCode` is what a caller branches on and the only half kept stable;
        ``detail`` is yours and reaches a log. Reach for ``"unknown"`` rather than guessing.

        A record of what could not be deleted is retry bookkeeping, not a guard on
        :meth:`acquire` — refusing to serve is the router's ledger. The three in this
        repository each carry their own copy of it, so a change to one is owed to the others.
        """
        ...

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete every sandbox for ``(scope, thread_id)``. Never raises.

        Returns how many went and, like :meth:`dispose`, why any is still there. A conversation
        delete that silently deleted nothing would otherwise read as a clean sweep, and the
        router would reopen every key it had refused for that conversation.
        """
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
