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
``InProcessSandbox`` is bytes-backed, so it can now stand in for a real pull surface too:
``stat_file``, ``read_file`` and ``list_dir`` confine every read to the ``working_directory``
a call names, the same rule a real backend enforces against its own guest filesystem.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ._outputs import SandboxTransferCapExceeded
from ._protocol import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SANDBOX_LIMITS,
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxSpec,
)

__all__ = ["InMemoryStore", "InProcessSandbox", "InProcessSandboxBackend"]


def _resolve(path: str, working_directory: str) -> str:
    """POSIX-join ``path`` onto ``working_directory`` and refuse anything that escapes it.

    A backslash is refused outright: the protocol has one path grammar, and ``\\`` is not a
    separator in it, whatever the host OS.  ``posixpath`` only, never ``os.path``.
    """
    if "\\" in path:
        raise ValueError(f"path {path!r} contains a backslash, which is not a valid separator")
    base = posixpath.normpath(working_directory)
    resolved = posixpath.normpath(posixpath.join(base, path))
    if _relative(resolved, base) is None:
        raise ValueError(f"path {path!r} resolves outside working directory {working_directory!r}")
    return resolved


def _relative(full_path: str, base: str) -> str | None:
    """``full_path`` relative to ``base``, or ``None`` when it does not sit inside ``base``.

    Compares against ``base + "/"``, not ``base``, so a sibling that merely shares a string
    prefix — ``/work/sub2`` under ``/work/sub`` — is not mistaken for a descendant.
    """
    if full_path == base:
        return ""
    prefix = base if base.endswith("/") else base + "/"
    if not full_path.startswith(prefix):
        return None
    return full_path[len(prefix) :]


def _record_child(
    children: dict[str, tuple[EntryKind, int | None]],
    entry_rel: str | None,
    directory_rel: str,
    kind: EntryKind,
    size_bytes: int | None,
) -> None:
    """Fold one stored entry into ``children`` if it sits under ``directory_rel``.

    A grandchild collapses into a single ``DIRECTORY`` entry for its immediate parent —
    ``list_dir`` enumerates one level, never the whole subtree.
    """
    if entry_rel is None or entry_rel == directory_rel:
        return
    prefix = "" if directory_rel == "" else directory_rel + "/"
    if not entry_rel.startswith(prefix):
        return
    name, _, nested = entry_rel[len(prefix) :].partition("/")
    child_path = prefix + name
    if nested:
        children[child_path] = (EntryKind.DIRECTORY, None)
    else:
        children.setdefault(child_path, (kind, size_bytes))


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
        seed_files: Pre-populates the read surface before any ``write_file`` call. A name
            distinct from ``outputs``, which already means scripted stdout — the same reason
            the design doc spells ``declared_outputs`` apart from it. A ``str`` value is
            UTF-8 encoded like ``write_file``'s; ``bytes`` is stored as given;
            :data:`~maf_sandbox.EntryKind.OTHER` declares the path as a non-regular entry —
            no content, never readable — the only way this fake can exercise the
            symlink-refusal rule.

    Storage is bytes, and :attr:`contents` **is** that store — the place a caller reading or
    seeding binary content looks. ``write_file`` UTF-8-encodes ``str`` content on the way in.
    :attr:`files` is a read-only, UTF-8-decoded view of the same store, kept for callers
    written against the fake's original shape, which only ever wrote text: a write to it
    raises rather than vanishing, and reading it raises ``UnicodeDecodeError`` if anything
    stored is not text — asking for text that was never written is worth an error rather than
    a replacement character. ``exec`` records ``(command, working_directory, timeout)`` tuples
    into :attr:`commands`.

    ``stat_file``, ``read_file`` and ``list_dir`` confine every ``path`` to the
    ``working_directory`` a call names: a backslash or a resolved path outside it raises
    ``ValueError``. ``read_file`` serves only :data:`~maf_sandbox.EntryKind.FILE`, raising
    ``FileNotFoundError`` for nothing there, ``IsADirectoryError`` for a directory and
    ``OSError`` for a seeded non-regular entry — and it **refuses** rather than truncates a
    file over its ``max_bytes``, as the protocol requires.

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
        seed_files: Mapping[str, str | bytes | EntryKind] | None = None,
    ) -> None:
        self.contents: dict[str, bytes] = {}
        self._non_regular: set[str] = set()
        for path, value in (seed_files or {}).items():
            # EntryKind is itself a str subclass, so this must be checked before isinstance(str).
            if isinstance(value, EntryKind):
                self._non_regular.add(path)
            elif isinstance(value, str):
                self.contents[path] = value.encode("utf-8")
            else:
                self.contents[path] = value
        self.commands: list[tuple[str, str, float]] = []
        self._outputs = outputs or {}
        self._raises = raises
        self._default_stdout = default_stdout

    @property
    def files(self) -> Mapping[str, str]:
        """Read-only UTF-8 view over :attr:`contents` — the shape older callers read.

        A proxy rather than a plain dict because it is computed: a write to a fresh dict would
        be discarded silently, and a test seeding through it would pass while asserting
        nothing. Write to :attr:`contents` instead, in bytes.
        """
        return MappingProxyType(
            {path: content.decode("utf-8") for path, content in self.contents.items()}
        )

    async def write_file(self, path: str, content: str | bytes) -> None:
        self.contents[path] = content.encode("utf-8") if isinstance(content, str) else content

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

    def _has_children(self, full_path: str) -> bool:
        prefix = full_path + "/"
        return any(p.startswith(prefix) for p in self.contents) or any(
            p.startswith(prefix) for p in self._non_regular
        )

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        full_path = _resolve(path, working_directory)
        rel = _relative(full_path, posixpath.normpath(working_directory))
        assert rel is not None  # _resolve already refused anything outside working_directory
        if full_path in self.contents:
            return SandboxEntry(
                path=rel, kind=EntryKind.FILE, size_bytes=len(self.contents[full_path])
            )
        if full_path in self._non_regular:
            return SandboxEntry(path=rel, kind=EntryKind.OTHER, size_bytes=None)
        if self._has_children(full_path):
            return SandboxEntry(path=rel, kind=EntryKind.DIRECTORY, size_bytes=None)
        return None

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        full_path = _resolve(path, working_directory)
        if full_path in self.contents:
            content = self.contents[full_path]
            if len(content) > max_bytes:
                # Refused, never truncated: a short read returned as success is an artifact
                # the host cannot tell from a whole one.
                raise SandboxTransferCapExceeded(
                    f"{path!r} is {len(content)} bytes and the caller allowed {max_bytes}"
                )
            return content
        if full_path in self._non_regular:
            raise OSError(f"{path!r} is not a regular file and is refused")
        if self._has_children(full_path):
            raise IsADirectoryError(f"{path!r} is a directory")
        raise FileNotFoundError(f"no such file: {path!r}")

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        full_path = _resolve(path, working_directory)
        base = posixpath.normpath(working_directory)
        directory_rel = _relative(full_path, base)
        assert directory_rel is not None  # _resolve already refused anything outside it

        children: dict[str, tuple[EntryKind, int | None]] = {}
        for stored_path, content in self.contents.items():
            _record_child(
                children, _relative(stored_path, base), directory_rel, EntryKind.FILE, len(content)
            )
        for stored_path in self._non_regular:
            _record_child(
                children, _relative(stored_path, base), directory_rel, EntryKind.OTHER, None
            )
        return tuple(
            SandboxEntry(path=child_path, kind=kind, size_bytes=size_bytes)
            for child_path, (kind, size_bytes) in sorted(children.items())
        )


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
            router's minimum-isolation floor is exercised against fakes claiming every
            :class:`~maf_sandbox.Isolation` rung, not only ``PROCESS``.
        egress: Returned by the :attr:`egress` property. Defaults to
            :data:`~maf_sandbox.Egress.ALLOWLIST` so a workload under test attaches as it
            would against a live backend, rather than every offline test becoming a test of
            the attach refusal.
        capabilities: Returned by the :attr:`capabilities` property. Still defaults to
            :data:`~maf_sandbox.DEFAULT_CAPABILITIES` even though the sandbox now genuinely
            implements the pull surface: widening the default would change what a bare
            ``InProcessSandboxBackend()`` attaches against for every existing caller that
            never asked for :data:`~maf_sandbox.Capability.FILES_OUT` or
            :data:`~maf_sandbox.Capability.FILES_LIST`. A test that wants the pull surface
            asks for it explicitly.
        limits: Returned by the :attr:`limits` property. Defaults to
            :data:`~maf_sandbox.DEFAULT_SANDBOX_LIMITS` — the same constant the router assumes
            for a backend that declares nothing, so leaving this unset and setting it
            explicitly serve one spec identically.
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
        isolation: Isolation = Isolation.PROCESS,
        egress: Egress = Egress.ALLOWLIST,
        capabilities: frozenset[Capability] = DEFAULT_CAPABILITIES,
        limits: SandboxLimits = DEFAULT_SANDBOX_LIMITS,
        acquire_error: BaseException | None = None,
    ) -> None:
        self.sandbox = sandbox if sandbox is not None else InProcessSandbox()
        self._name = name
        self._isolation = isolation
        self._egress = egress
        self._capabilities = capabilities
        self._limits = limits
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
    def isolation(self) -> Isolation:
        return self._isolation

    @property
    def egress(self) -> Egress:
        return self._egress

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    @property
    def limits(self) -> SandboxLimits:
        return self._limits

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
