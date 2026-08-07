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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ExecResult",
    "Isolation",
    "Sandbox",
    "SandboxBackend",
    "SandboxKey",
    "SandboxSpec",
    "WorkspaceContext",
]


class Isolation:
    """How strong a backend's boundary is. Declared by the backend, checked by the router.

    This is not documentation: :class:`~maf_sandbox._router.SandboxRouter` refuses to
    select anything below :data:`VM` in a deployed environment.  A security review put
    execution in a VM-isolated sandbox precisely because a container shares the host kernel
    and sits next to whatever credentials the host process holds, and the security posture
    the deployed-isolation rule enforces rests on that conclusion.  A backend that lies here
    defeats the check, so the value belongs with the backend that knows the truth about
    itself.
    """

    #: Hardware/hypervisor boundary, no ambient identity reachable from inside (ACA Sandboxes).
    VM = "vm"
    #: Shared-kernel container. Fine on a developer machine, never in a deployed environment.
    CONTAINER = "container"
    #: Same process as the host. Tests only.
    PROCESS = "process"


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

    ``kind`` names the workload (``"bicep"`` today).  ``image`` is normally
    ``repository:tag`` — *where* images live is a property of the deployment, so the backend
    qualifies it with its own registry; a fully-qualified reference is passed through
    untouched.  The ACA backend then resolves the result to an imported disk image, while a
    Docker backend would hand it to ``docker run``.  ``image_id`` is an escape hatch for a
    backend-native pinned id that skips resolution entirely.

    ``egress_allow`` is an allowlist of hostnames — **everything not listed is denied**, so
    an empty tuple means no network at all.  Stating it positively is deliberate: a spec that
    forgets to mention egress gets the closed configuration, not the open one.
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
    key across calls, because a workload's fix-round loop would otherwise pay a cold start
    every iteration.

    ``dispose_scope`` exists separately from ``dispose`` because a conversation delete has to
    reach sandboxes this process never created — a multi-replica host serves the delete
    wherever it lands.  A backend that only consults its own memory there leaves billable
    sandboxes running.
    """

    @property
    def name(self) -> str:
        """Short identifier used in configuration, e.g. ``"aca"``."""
        ...

    @property
    def isolation(self) -> str:
        """One of the :class:`Isolation` constants. Read by the router's deployed check."""
        ...

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> Sandbox:
        """Return a running sandbox for ``key``, creating one if needed."""
        ...

    async def dispose(self, key: SandboxKey) -> None:
        """Delete the sandbox for ``key``, if any. Best-effort: never raises."""
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
