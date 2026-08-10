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
    "ISOLATION_RANK",
    "Capability",
    "Egress",
    "ExecResult",
    "Isolation",
    "Sandbox",
    "SandboxBackend",
    "SandboxKey",
    "SandboxSpec",
    "WorkspaceContext",
    "meets_floor",
]


class Isolation(StrEnum):
    """How strong a backend's boundary is. Declared by the backend, checked by the router.

    This is not documentation: the members are a ladder ordered by :data:`ISOLATION_RANK`,
    and :class:`~maf_sandbox._router.SandboxRouter` refuses anything below the declared floor.
    """

    #: Same process as the host: no boundary at all. Tests and local fakes.
    PROCESS = "process"
    #: A software boundary inside the host process — a restricted interpreter, a WASM
    #: runtime's fault isolation.
    RUNTIME = "runtime"
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
            Isolation.PROCESS,
            Isolation.RUNTIME,
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
    #: Read files back out after execution.
    FILES_OUT = "files_out"
    #: Any egress at all — how precisely it is confined stays in :class:`Egress`.
    NETWORK = "network"
    #: Snapshot and restore a sandbox for reuse.
    SNAPSHOT = "snapshot"
    #: A platform-attached identity scoped to the sandbox itself.
    ATTACHED_IDENTITY = "attached_identity"


#: What every :class:`Sandbox` already obligates.
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.EXEC, Capability.FILES_IN})


@dataclass(frozen=True)
class SandboxKey:
    """Identifies the one sandbox a caller may reach.

    ``scope`` is the host's user/tenant scope and ``thread_id`` the conversation; both come
    from the host's request context and never from model input, because a model-supplied
    value here would let one conversation address another's sandbox.  ``agent_dir`` keys the
    sandbox to a single agent's workspace, so two agents in one conversation do not share a
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
    see :meth:`SandboxBackend.acquire`.  ``image`` is normally
    ``repository:tag`` — *where* images live is a property of the deployment, so the backend
    qualifies it with its own registry; a fully-qualified reference is passed through
    untouched.  The ACAS backend then resolves the result to an imported disk image, while a
    Docker backend would hand it to ``docker run``.  ``image_id`` is an escape hatch for a
    backend-native pinned id that skips resolution entirely.

    ``egress_allow`` is an allowlist of hostnames — **everything not listed is denied**, so
    an empty tuple means no network at all.  Stating it positively is deliberate: a spec that
    forgets to mention egress gets the closed configuration, not the open one.

    ``requires`` names the capabilities the workload cannot run without, and ``min_isolation``
    the weakest boundary it accepts anywhere.  A spec may **raise** the host's floor and never
    lower it, and ``None`` means no opinion — not the same as :data:`Isolation.PROCESS`, which
    would be the weakest opinion there is.
    """

    kind: str
    image: str | None = None
    image_id: str | None = None
    egress_allow: tuple[str, ...] = ()
    work_dir: str = "/work"
    # `dict[str, str]` rather than a bare `dict` as the factory: the bare builtin gives a
    # strict type checker `dict[Unknown, Unknown]` to work with, and this package's own
    # pyright config is strict. The subscripted form is callable and constructs the
    # identical empty dict.
    labels: dict[str, str] = field(default_factory=dict[str, str])
    requires: frozenset[Capability] = DEFAULT_CAPABILITIES
    min_isolation: Isolation | None = None


@dataclass(frozen=True)
class ExecResult:
    """The result of one command run inside a sandbox."""

    stdout: str
    stderr: str = ""
    exit_code: int = 0


@runtime_checkable
class Sandbox(Protocol):
    """A running sandbox a workload can put files into and run commands in."""

    async def write_file(self, path: str, content: str) -> None:
        """Write ``content`` to ``path`` inside the sandbox."""
        ...

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command`` inside the sandbox, bounded by ``timeout`` seconds.

        ``command`` accepts two shapes, and they are not interchangeable:

        - A **sequence** (``["bicep", "build", path]``) is quoted for you before it reaches
          a shell, and is the safe default whenever any element could contain whitespace or
          a shell metacharacter — a workspace path, in particular, is not agent-controlled
          but is still text neither side should have to prove is free of ``;`` or ``$()``.
        - A **string** (``"bicep build … 2>&1 || true"``) is a shell command line, evaluated
          by a shell inside the sandbox. Use it only when the command genuinely needs shell
          features a sequence cannot express — redirection, ``||``, ``&&`` — and every part
          of it is a fixed template with nothing but an already-validated path interpolated.
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
    against a spec's ``requires``.  It is deliberately not a member of this Protocol:
    :func:`~typing.runtime_checkable` enforces member *presence*, so declaring it here would
    stop every backend written before it from being a ``SandboxBackend`` at all.
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
class WorkspaceContext:
    """How the host identifies the caller and enumerates the files it may act on.

    ``current_scope`` and ``current_thread_id`` are **callables read at call time** (they are
    typically ``ContextVar`` lookups) rather than values, which is what keeps the
    :class:`SandboxKey` a property of the host's request context instead of something a
    caller — or a model — can supply.

    ``list_files`` receives the workspace store and returns the paths the caller may act on.
    Workloads use it as their injection-pinning boundary: only a name present in that listing
    is ever substituted into a command.
    """

    current_scope: Callable[[], str]
    current_thread_id: Callable[[], str | None]
    list_files: Callable[[Any], Awaitable[list[str]]]
