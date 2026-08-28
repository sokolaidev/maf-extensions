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
from typing import TYPE_CHECKING

from ._outputs import SandboxTransferCapExceeded
from ._protocol import (
    BackendDeclarations,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxEntry,
    SandboxKey,
    SandboxSpec,
    ScopePurge,
)
from .paths import (
    confine_guest_path,
    confine_guest_write_path,
    guest_path_relative_to,
    refuse_symlinked_parents,
)

__all__ = [
    "FAKE_BACKEND_DECLARATIONS",
    "InMemoryStore",
    "InProcessSandbox",
    "InProcessSandboxBackend",
]


def _child_name(entry_rel: str | None, directory_rel: str) -> str | None:
    """The immediate child of ``directory_rel`` that ``entry_rel`` sits under, if any.

    A grandchild names its own parent — ``list_dir`` enumerates one level, never the whole
    subtree — and the caller classifies that name, so a seeded link with something beneath it
    is not reported as the directory its children would make it look like.
    """
    if entry_rel is None or entry_rel == directory_rel:
        return None
    prefix = "" if directory_rel == "" else directory_rel + "/"
    if not entry_rel.startswith(prefix):
        return None
    name, _, _nested = entry_rel[len(prefix) :].partition("/")
    return prefix + name


class InProcessSandbox:
    """A :class:`~maf_sandbox.Sandbox` that lives in this process's memory — no container, no VM.

    Args:
        outputs: Marker-keyed scripted stdout. On ``exec``, the first key found as a
            substring of the (possibly joined — see below) command is returned as
            ``ExecResult.stdout``. ``None`` means no scripting at all.
        raises: When set, every ``exec``, ``run_code`` and ``reclaim`` call raises this instead
            of doing its work — for exercising a dead or unresponsive sandbox.

    Keyword Args:
        default_stdout: What ``exec`` returns when no marker in ``outputs`` matches. Left to
            the caller rather than baked in: a kind that speaks SARIF wants an empty-but-valid
            SARIF document here, a kind that does not wants ``""`` (the default) — this fake
            has no opinion about either.
        seed_files: Pre-populates the read surface before any ``write_file`` call. A name
            distinct from ``outputs``, which already means scripted stdout — the same reason
            the design doc spells ``declared_outputs`` apart from it. A ``str`` value is
            UTF-8 encoded like ``write_file``'s; ``bytes`` is stored as given;
            :data:`~maf_sandbox.EntryKind.SYMLINK` declares the path a link and
            :data:`~maf_sandbox.EntryKind.OTHER` any other non-regular entry — neither has
            content, neither is readable, and only a link is refused as an escape.

    Storage is bytes, and :attr:`contents` **is** that store, keyed by normalised absolute guest
    paths — the place a caller reading or
    seeding binary content looks. ``write_file`` UTF-8-encodes ``str`` content on the way in.
    :attr:`symlinks` and :attr:`non_regular` are the stores for entries that have no content:
    sets of absolute guest paths, seedable above or writable directly to plant one mid-test.
    :attr:`files` is a read-only, UTF-8-decoded view of :attr:`contents`, kept for callers
    written against the fake's original shape, which only ever wrote text: a write to it
    raises rather than vanishing, and reading it raises ``UnicodeDecodeError`` if anything
    stored is not text — asking for text that was never written is worth an error rather than
    a replacement character. ``exec`` records ``(command, working_directory, timeout)`` tuples
    into :attr:`commands`, and ``reclaim`` records ``(directory, working_directory, timeout)``
    into :attr:`reclaims` and really removes.

    ``stat_file``, ``read_file`` and ``list_dir`` confine every ``path`` to the
    ``working_directory`` a call names: a backslash or a resolved path outside it raises
    ``ValueError``. ``read_file`` serves only :data:`~maf_sandbox.EntryKind.FILE`, raising
    ``FileNotFoundError`` for nothing there, ``IsADirectoryError`` for a directory and
    ``OSError`` for a seeded non-regular entry — and it **refuses** rather than truncates a
    file over its ``max_bytes``, as the protocol requires.

    All four also run :func:`~maf_sandbox.paths.refuse_symlinked_parents` over the components,
    so a seeded link standing where a directory was expected is refused here as it is on a real
    backend. What this fake cannot model is the **escape** itself: a seeded link has no target,
    so nothing reads through one and a test going green here has asserted shape, not safety.
    Both backend suites carry their own premise test for that reason.

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
        self.symlinks: set[str] = set()
        self.non_regular: set[str] = set()
        self.directories: set[str] = set()
        for path, value in (seed_files or {}).items():
            # EntryKind is itself a str subclass, so this must be checked before isinstance(str).
            if value is EntryKind.SYMLINK:
                self.symlinks.add(path)
            elif value is EntryKind.DIRECTORY:
                # Kept apart from `non_regular`: a declared directory is the one entry a
                # removal has to refuse without `recursive`, and an empty one has no children
                # to infer it from.
                self.directories.add(path)
            elif isinstance(value, EntryKind):
                self.non_regular.add(path)
            elif isinstance(value, str):
                self.contents[path] = value.encode("utf-8")
            else:
                self.contents[path] = value
        self.commands: list[tuple[str, str, float]] = []
        #: Every ``reclaim`` call, as ``(directory, working_directory, timeout)``.
        self.reclaims: list[tuple[str, str, float]] = []
        #: Every ``run_code`` call, as ``(code, timeout)``. Separate from :attr:`commands`
        #: because a test asserting a program was evaluated should not match a shell command
        #: that happens to contain the same text.
        self.programs: list[tuple[str, float]] = []
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

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        full_path = await confine_guest_write_path(self._stat_unconfined, path, working_directory)
        self.contents[full_path] = content.encode("utf-8") if isinstance(content, str) else content

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

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Record the program and return scripted output, on the same rules as :meth:`exec`.

        Scripted rather than raising, because a fake that refused would make every kind
        written against ``run_code`` untestable without a real backend — which is the one
        thing this class exists to avoid. Nothing is evaluated: ``outputs`` is matched against
        ``code`` as a substring, exactly as it is matched against a command line.
        """
        self.programs.append((code, timeout))
        if self._raises is not None:
            raise self._raises
        for marker, output in self._outputs.items():
            if marker in code:
                return ExecResult(stdout=output)
        return ExecResult(stdout=self._default_stdout)

    def _stored(self) -> tuple[str, ...]:
        """Every path this fake holds, whatever kind it is.

        One list because the consumers below each walk all of them, and a store added to the
        constructor and to only two of the three is a silent hole.
        """
        return (*self.contents, *self.symlinks, *self.non_regular, *self.directories)

    def _has_children(self, full_path: str) -> bool:
        prefix = full_path + "/"
        return any(p.startswith(prefix) for p in self._stored())

    def _kind_at(self, full_path: str) -> tuple[EntryKind, int | None] | None:
        """What is stored at an absolute guest path, unconfined and following nothing.

        Unconfined because the component walk classifies the working directory's own ancestors,
        which sit outside it by definition.
        """
        if full_path in self.contents:
            return EntryKind.FILE, len(self.contents[full_path])
        if full_path in self.symlinks:
            return EntryKind.SYMLINK, None
        if full_path in self.non_regular:
            return EntryKind.OTHER, None
        if full_path in self.directories or self._has_children(full_path):
            return EntryKind.DIRECTORY, None
        return None

    async def _stat_unconfined(self, full_path: str) -> SandboxEntry | None:
        found = self._kind_at(full_path)
        if found is None:
            return None
        kind, size_bytes = found
        return SandboxEntry(path=full_path, kind=kind, size_bytes=size_bytes)

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        full_path = confine_guest_path(path, working_directory)
        await refuse_symlinked_parents(self._stat_unconfined, full_path, working_directory)
        rel = guest_path_relative_to(full_path, posixpath.normpath(working_directory))
        assert rel is not None  # confine_guest_path already refused anything outside it
        found = self._kind_at(full_path)
        if found is None:
            return None
        kind, size_bytes = found
        return SandboxEntry(path=rel, kind=kind, size_bytes=size_bytes)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        full_path = confine_guest_path(path, working_directory)
        await refuse_symlinked_parents(self._stat_unconfined, full_path, working_directory)
        if full_path in self.contents:
            content = self.contents[full_path]
            if len(content) > max_bytes:
                # Refused, never truncated: a short read returned as success is an artifact
                # the host cannot tell from a whole one.
                raise SandboxTransferCapExceeded(
                    f"{path!r} is {len(content)} bytes and the caller allowed {max_bytes}"
                )
            return content
        if full_path in self.symlinks or full_path in self.non_regular:
            raise OSError(f"{path!r} is not a regular file and is refused")
        if full_path in self.directories or self._has_children(full_path):
            raise IsADirectoryError(f"{path!r} is a directory")
        raise FileNotFoundError(f"no such file: {path!r}")

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        full_path = confine_guest_path(path, working_directory)
        # `include_self=False`: a link named here is the thing being removed, and removing it
        # is the one operation on the pull surface that must *not* resolve it. Its parents are
        # walked exactly as a read walks them.
        await refuse_symlinked_parents(
            self._stat_unconfined, full_path, working_directory, include_self=False
        )
        base = posixpath.normpath(working_directory)
        if posixpath.normpath(full_path) == base:
            raise ValueError(
                f"refusing to remove the working directory itself: {working_directory}"
            )
        # A link's children are the target's, so a link has none here — otherwise `recursive`
        # would follow one, in the fake that exists to forbid it.
        children = (
            []
            if full_path in self.symlinks
            else [
                stored
                for stored in self._stored()
                if guest_path_relative_to(stored, full_path) not in (None, "")
            ]
        )
        if (children or full_path in self.directories) and not recursive:
            raise OSError(f"refusing to remove a directory without recursive: {path}")
        for stored in (*children, full_path):
            self.contents.pop(stored, None)
            self.symlinks.discard(stored)
            self.non_regular.discard(stored)
            self.directories.discard(stored)

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Remove the directory for real, and record the call.

        No confinement check and no depth guard: the contract leaves both with the caller.
        ``directory`` is absolute, so ``working_directory`` plays no part.
        """
        self.reclaims.append((directory, working_directory, timeout))
        if self._raises is not None:
            raise self._raises
        full_path = posixpath.normpath(directory)
        # A link is unlinked, not followed.
        stored_paths = (full_path,) if full_path in self.symlinks else self._stored()
        for stored in [p for p in stored_paths if guest_path_relative_to(p, full_path) is not None]:
            self.contents.pop(stored, None)
            self.symlinks.discard(stored)
            self.non_regular.discard(stored)
            self.directories.discard(stored)

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        full_path = confine_guest_path(path, working_directory)
        await refuse_symlinked_parents(
            self._stat_unconfined, full_path, working_directory, include_self=True
        )
        base = posixpath.normpath(working_directory)
        directory_rel = guest_path_relative_to(full_path, base)
        assert directory_rel is not None  # confine_guest_path already refused anything outside it

        names: set[str] = set()
        for stored_path in self._stored():
            child = _child_name(guest_path_relative_to(stored_path, base), directory_rel)
            if child is not None:
                names.add(child)
        entries: list[SandboxEntry] = []
        for child_path in sorted(names):
            found = self._kind_at(posixpath.join(base, child_path))
            if found is not None:
                entries.append(SandboxEntry(path=child_path, kind=found[0], size_bytes=found[1]))
        return tuple(entries)


#: What :class:`InProcessSandboxBackend` declares unless a test says otherwise.  One field
#: departs from :data:`~maf_sandbox.DEFAULT_BACKEND_DECLARATIONS`: ``egress_modes`` is stated,
#: because the router's silence rule there refuses every spec, and an offline suite that has to
#: opt out of the attach refusal in every test is measuring the fake rather than the workload.
FAKE_BACKEND_DECLARATIONS = BackendDeclarations(
    egress_modes=frozenset({Egress.ALLOWLIST, Egress.CLOSED})
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
            :class:`~maf_sandbox.Isolation` rung, not only ``NONE``.
        declarations: Returned by the :attr:`declarations` property. Defaults to
            :data:`FAKE_BACKEND_DECLARATIONS`, which differs from
            :data:`~maf_sandbox.DEFAULT_BACKEND_DECLARATIONS` in one field: ``egress_modes`` is
            ``{ALLOWLIST, CLOSED}`` so a workload under test attaches as it would against a
            proxy-capable live backend, rather than every offline test becoming a test of the
            attach refusal. A test of that refusal passes a narrower set (``frozenset()`` for
            "enforces nothing", ``{UNRESTRICTED}`` for the no-confinement backend). The other
            three fields keep the router's own silence rules, so leaving them unset and stating
            them explicitly serve one spec identically — which is why ``capabilities`` still
            defaults to :data:`~maf_sandbox.DEFAULT_CAPABILITIES` even though this sandbox
            genuinely implements the pull surface: a test that wants it asks for it.

            **Override with** ``dataclasses.replace(FAKE_BACKEND_DECLARATIONS, ...)``, never
            with a bare :class:`~maf_sandbox.BackendDeclarations`: constructing one resets
            ``egress_modes`` to the router's silence rule, which enforces nothing, and every
            attach then fails with :class:`~maf_sandbox.SandboxEgressNotEnforced` about a field
            the test never named.
        acquire_error: When set, ``acquire`` raises this instead of returning the sandbox —
            for exercising a kind's "sandbox unavailable" degrade path.
        dispose_error: When set, ``dispose`` records the key and then raises this — for
            exercising what a host sees when the framework cannot dispose a sandbox it
            could not clean.
        dispose_failure: When set, ``dispose`` records the key and returns this
            :class:`~maf_sandbox.DisposalFailure` — a delete that failed *without* raising,
            which is what a real backend does, since ``dispose`` is contractually best-effort.
            Fires after ``dispose_error``, so a test setting both sees the raise.
        purge_failure: The same for ``dispose_scope``, returned as ``ScopePurge.undisposed``.

    Every ``acquire`` records ``key`` into :attr:`keys` and ``spec`` into :attr:`specs`
    (skipped when ``acquire_error`` fires — a failed acquire acquired nothing). Every
    ``dispose`` records ``key`` into :attr:`disposed` (before ``dispose_error`` fires). Every
    ``dispose_scope`` records
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
        isolation: Isolation = Isolation.NONE,
        declarations: BackendDeclarations = FAKE_BACKEND_DECLARATIONS,
        acquire_error: BaseException | None = None,
        dispose_error: BaseException | None = None,
        dispose_failure: DisposalFailure | None = None,
        purge_failure: DisposalFailure | None = None,
    ) -> None:
        self.sandbox = sandbox if sandbox is not None else InProcessSandbox()
        self._name = name
        self._isolation = isolation
        self._declarations = declarations
        self.acquire_error = acquire_error
        self.dispose_error = dispose_error
        self.dispose_failure = dispose_failure
        self.purge_failure = purge_failure
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
    def declarations(self) -> BackendDeclarations:
        return self._declarations

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> InProcessSandbox:
        if self.acquire_error is not None:
            raise self.acquire_error
        self.keys.append(key)
        self.specs.append(spec)
        return self.sandbox

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        self.disposed.append(key)
        if self.dispose_error is not None:
            raise self.dispose_error
        return self.dispose_failure

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        self.purged.append((scope, thread_id))
        return ScopePurge(self.purge_count, self.purge_failure)


class InMemoryStore:
    """The slice of ``AgentFileStore`` a sandbox kind's tests need — duck-typed, zero imports.

    Args:
        files: The initial ``{store path: content}`` mapping. Copied on construction, so
            mutating the caller's original dict afterward does not reach into the store.

    ``read`` mirrors ``AgentFileStore.read``'s real contract: a miss returns ``None`` rather
    than raising, because a kind is expected to handle "listed but has no content" as data,
    not as an exception.

    ``list`` is deliberately ``async def list(self) -> list[str]`` — no other parameter — so
    the *unbound* method matches :class:`~maf_sandbox.CallerContext`'s
    ``list_files: Callable[[Any], Awaitable[list[str]]]`` exactly.
    ``list_files=InMemoryStore.list`` works with no wrapper.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self.files: dict[str, str] = dict(files)

    async def read(self, name: str) -> str | None:
        return self.files.get(name)

    async def list(self) -> list[str]:
        return list(self.files)


# Holds this module's backend and sandbox to the protocols they implement, inside this
# package's own strict pyright pass — the annotation is what fails the build when a protocol
# method goes missing or a signature narrows.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (
        InProcessSandboxBackend(),
        InProcessSandbox,
    )
