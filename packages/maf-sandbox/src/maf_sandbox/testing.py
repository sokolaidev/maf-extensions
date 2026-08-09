"""In-process fakes for testing code that depends on this package.

Two hand-rolled fakes had accumulated independently — one in the router's own test suite,
shaped for policy tests (a configurable name and isolation, recording every call); one in the
bicep kind's test suite, shaped for scripting a compiler's output (marker-keyed stdout,
``raises``).  Promoting a single, supported superset here means a future kind's tests do not
write a third one, and a fake nobody outside this package tests is a fake that drifts — see
this module's own test suite for the assertions that keep it honest.

Nothing here is a mock in the unittest.mock sense: every class is a plain, real implementation
of the protocols in :mod:`maf_sandbox._protocol`, so ``isinstance(..., SandboxBackend)`` holds
and a workload under test cannot tell it apart from a live backend except by what it does.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from ._protocol import Egress, ExecResult, Isolation, SandboxKey, SandboxSpec

__all__ = ["InMemoryStore", "InProcessSandbox", "InProcessSandboxBackend"]


class InProcessSandbox:
    """A :class:`~maf_sandbox.Sandbox` that lives in this process's memory — no container, no VM.

    Args:
        outputs: Marker-keyed scripted stdout. On ``exec``, the first key found as a
            substring of the (possibly joined — see below) command is returned as
            ``ExecResult.stdout``. ``None`` means no scripting at all.
        raises: When set, every ``exec`` call raises this instead of returning a result —
            for exercising a dead or unresponsive sandbox.

    Keyword Args:
        default_stdout: What ``exec`` returns when no marker in ``outputs`` matches. Left to
            the caller rather than baked in: a kind that speaks SARIF wants an empty-but-valid
            SARIF document here, a kind that does not wants ``""`` (the default) — this fake
            has no opinion about either.

    ``write_file`` records into :attr:`files`, keyed by path. ``exec`` records
    ``(command, working_directory, timeout)`` tuples into :attr:`commands`.

    ``exec`` accepts a plain string or an argv sequence (mirroring
    :meth:`~maf_sandbox.Sandbox.exec`). A sequence is joined with :func:`shlex.join` *before*
    it is recorded and *before* marker matching runs, so ``commands`` always holds the string
    form and a marker written against a string command matches an equivalent argv command the
    same way. The backends this fake stands in for are Linux, so POSIX quoting (what
    :func:`shlex.join` produces) is the correct join.
    """

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        raises: BaseException | None = None,
        *,
        default_stdout: str = "",
    ) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, str, float]] = []
        self._outputs = outputs or {}
        self._raises = raises
        self._default_stdout = default_stdout

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        joined = command if isinstance(command, str) else shlex.join(command)
        self.commands.append((joined, working_directory, timeout))
        if self._raises is not None:
            raise self._raises
        for marker, output in self._outputs.items():
            if marker in joined:
                return ExecResult(stdout=output)
        return ExecResult(stdout=self._default_stdout)


class InProcessSandboxBackend:
    """A :class:`~maf_sandbox.SandboxBackend` that hands out one :class:`InProcessSandbox`.

    Args:
        sandbox: The sandbox every ``acquire`` returns. Defaults to a fresh
            ``InProcessSandbox()`` when omitted.

    Keyword Args:
        name: Returned by the :attr:`name` property — configurable because router tests need
            to distinguish several registered backends (``"first"``, ``"aca"``, ``"docker"``)
            by name.
        isolation: Returned by the :attr:`isolation` property — configurable because the
            router's deployed-isolation rule is exercised against fakes claiming every
            :class:`~maf_sandbox.Isolation` level, not only ``PROCESS``.
        egress: Returned by the :attr:`egress` property. Defaults to
            :data:`~maf_sandbox.Egress.ALLOWLIST` so a workload under test attaches as it
            would against a live backend, rather than every offline test becoming a test of
            the attach refusal.
        acquire_error: When set, ``acquire`` raises this instead of returning the sandbox —
            for exercising a kind's "sandbox unavailable" degrade path.

    Every ``acquire`` records ``key`` into :attr:`keys` and ``spec`` into :attr:`specs`
    (skipped when ``acquire_error`` fires — a failed acquire acquired nothing). Every
    ``dispose`` records ``key`` into :attr:`disposed`. Every ``dispose_scope`` records
    ``(scope, thread_id)`` into :attr:`purged` and returns :attr:`purge_count`, settable per
    test to simulate more than one sandbox reclaimed.

    A deliberate simplification: every ``acquire`` returns the same sandbox whatever the key
    or the spec's kind, where a real backend keys sandboxes by ``(key, kind)``. Tests that
    care which kind asked read :attr:`specs`; a test that needs two genuinely distinct
    sandboxes registers two backends.
    """

    def __init__(
        self,
        sandbox: InProcessSandbox | None = None,
        *,
        name: str = "in-process",
        isolation: str = Isolation.PROCESS,
        egress: str = Egress.ALLOWLIST,
        acquire_error: BaseException | None = None,
    ) -> None:
        self.sandbox = sandbox if sandbox is not None else InProcessSandbox()
        self._name = name
        self._isolation = isolation
        self._egress = egress
        self.acquire_error = acquire_error
        self.keys: list[SandboxKey] = []
        self.specs: list[SandboxSpec] = []
        self.disposed: list[SandboxKey] = []
        self.purged: list[tuple[str, str]] = []
        self.purge_count = 1

    @property
    def name(self) -> str:
        return self._name

    @property
    def isolation(self) -> str:
        return self._isolation

    @property
    def egress(self) -> str:
        return self._egress

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> InProcessSandbox:
        if self.acquire_error is not None:
            raise self.acquire_error
        self.keys.append(key)
        self.specs.append(spec)
        return self.sandbox

    async def dispose(self, key: SandboxKey) -> None:
        self.disposed.append(key)

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        self.purged.append((scope, thread_id))
        return self.purge_count


class InMemoryStore:
    """The slice of ``AgentFileStore`` a sandbox kind's tests need — duck-typed, zero imports.

    Args:
        files: The initial ``{workspace path: content}`` mapping. Copied on construction, so
            mutating the caller's original dict afterward does not reach into the store.

    ``read`` mirrors ``AgentFileStore.read``'s real contract: a miss returns ``None`` rather
    than raising, because a kind is expected to handle "listed but has no content" as data,
    not as an exception.

    ``list`` is deliberately ``async def list(self) -> list[str]`` — no other parameter — so
    the *unbound* method matches :class:`~maf_sandbox.WorkspaceContext`'s
    ``list_files: Callable[[Any], Awaitable[list[str]]]`` exactly.
    ``list_files=InMemoryStore.list`` works with no wrapper.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self.files: dict[str, str] = dict(files)

    async def read(self, name: str) -> str | None:
        return self.files.get(name)

    async def list(self) -> list[str]:
        return list(self.files)
