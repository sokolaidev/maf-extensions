"""``FileStoreProvenance``: what is known about the integrity of content in an agent file store.

A kind that reads the agent's file store reads content whose provenance the framework has
already lost.  ``AgentFileStore`` holds a ``str`` and returns a ``str``, and the information-flow
middleware expands a variable reference into the bytes it stands for *before* the tool body
that writes them runs — so by the time anything reaches the store, nothing anywhere says what
it was worth (#841, measured in ``docs/sandbox/research/labelling-the-file-store.md``).

What *is* recoverable is **who wrote it**, and that is recoverable at the tool-call boundary
rather than at the store.  A write through ``FileAccessProvider``'s tools is a tool call, and a
tool call is the unambiguous signal that the model drove it.  So this records one fact per
path — an agent-driven write happened here — and that fact settles the integrity question in
the only direction that matters, because every route by which the model can put bytes into the
store is a route through the model:

* content behind a ``[var_id]`` reference is there because the middleware **hid** it, and it
  hides what is untrusted;
* content typed into ``content=`` literally was authored by the model.

Neither is trusted, so an observed write records :data:`~maf_sandbox.SourceIntegrity.UNTRUSTED`
without this module resolving anything.  That reading does not depend on the framework's
``hide_threshold`` staying where it is: a host that moves it makes the *first* bullet less
precise, and this still records untrusted, which is the fail-safe direction.

**Everything else is the host's to declare.** Content placed before the agent started, or
**Everything else is the host's to declare.** Content placed before the agent started, or
written past the store object by another process, is not a tool call and is not observed here.
so a trusted floor can never lift bytes the model wrote.
"""

from __future__ import annotations

import threading

from ._protocol import SourceIntegrity

__all__ = [
    "FILE_STORE_WRITE_TOOLS",
    "PATH_ARGUMENT",
    "FileStoreProvenance",
    "store_key",
]

#: The ``FileAccessProvider`` tools that change what a path holds.  Named here rather than
#: imported, because importing them would make this module require the framework's harness to
#: be installed, and a host may wire an equivalent surface of its own under other names — which
#: is what ``also_observes`` is for.  Checked against the framework's own ``_WRITE_TOOL_NAMES``
#: by a divergence alarm in the suite, so a tool added upstream fails there rather than going
#: silently unobserved here.
FILE_STORE_WRITE_TOOLS = frozenset(
    {
        "file_access_write",
        "file_access_replace",
        "file_access_replace_lines",
        "file_access_delete",
    }
)

#: The argument every one of those tools names its path with.
PATH_ARGUMENT = "file_name"


def store_key(path: str) -> str:
    """``path`` as the store will hold it, so a record files under the key a read will use.

    ``FileAccessProvider`` normalises before it writes — it trims surrounding whitespace, turns
    backslashes into forward slashes and collapses repeated separators — so a record filed under
    the argument as the model spelled it is filed under a key nothing ever reads.  That is the
    one failure this whole record exists to prevent: the lookup misses, the path falls to the
    host's floor, and a trusted floor answers for bytes the model wrote.

    **It mirrors behaviour rather than a published contract**, as ``_reduced_form`` does: the
    rule lives in ``agent_framework._harness._file_access._normalize_relative_path``, which is
    private and promises nothing.  A spelling this stops matching is a path whose entry a read
    cannot find, so the suite checks the two against each other rather than assuming.  Only the
    *collapsing* half is mirrored; the rejections that raise are the provider's to make, and a
    path it refuses is one it never wrote.
    """
    collapsed = path.strip().replace("\\", "/")
    while "//" in collapsed:
        collapsed = collapsed.replace("//", "/")
    return collapsed


class FileStoreProvenance:
    """What a host knows about the integrity of the content in one agent file store.

    Held by the host, passed to :func:`file_store_provenance_middleware` so agent-driven writes
    are recorded into it, and read by a kind — or by the host's own listing callable — to answer
    what a named file is worth.

    **One store.**  A path is the whole key, so a host wiring two providers over two stores
    needs one of these each; the tools carry no store identity for this to key on.  Reading a
    kind against the wrong one would answer about a file it never read.

    Args:
        floor: What applies to a path with no recorded entry — content placed before the agent
            started, or written past the store object by something that is not a tool call.
            ``None``, the default, means *unestablished*: this host has not said, and a caller
            must treat the answer as it treats any source the framework has not established.
    """

    def __init__(self, *, floor: SourceIntegrity | None = None) -> None:
        self._floor = None if floor is None else SourceIntegrity(str(floor))
        # Guarded because a synchronous tool body runs on a `asyncio.to_thread` pool thread
        # while the middleware recording writes runs on the event loop.
        self._lock = threading.Lock()
        self._entries: dict[str, SourceIntegrity] = {}

    @property
    def floor(self) -> SourceIntegrity | None:
        """What a path with no recorded entry is worth, as the host declared it."""
        return self._floor

    def record(self, path: str) -> None:
        """Record that an agent-driven call wrote ``path``.

        ``path`` is keyed through :func:`store_key`, here and in :meth:`integrity_of` alike, so a
        record filed under one spelling is found under every spelling of the same file.

        **There is no integrity argument, and that is what makes the record monotone.**  Every
        entry it can hold is untrusted, so recording twice is recording the same thing and no
        caller — not this package's middleware, not a host's own — can raise a path that was
        written by the model back to trusted.  A record whose entries could be raised would give
        the concurrency and floor guarantees below nothing to stand on.

        **An entry is about the path, not about a version of its content.**  It records that the
        model has written here, which stays true of every later version: nothing the model writes
        afterwards makes the file host-authored again.  Binding the entry to a digest of the bytes
        would say the opposite — a path whose content changed would stop matching and fall to the
        floor, and a trusted floor would then answer for a file the model demonstrably wrote.
        """
        with self._lock:
            self._entries[store_key(path)] = SourceIntegrity.UNTRUSTED

    def forget(self, path: str) -> None:
        """Drop any entry for ``path``, returning it to :attr:`floor`.

        For a host that has **established** the file is gone.  Nothing in this package calls it:
        the file-store tools answer a failed delete with a sentence rather than an exception, so
        an observer cannot tell a delete that removed the file from one that did not, and
        forgetting on the strength of a call having been made would return a path to a trusted
        floor while the model's bytes were still in it.
        """
        with self._lock:
            self._entries.pop(store_key(path), None)

    def integrity_of(self, path: str) -> SourceIntegrity | None:
        """What ``path`` is worth, or ``None`` where nothing here establishes it.

        An entry answers for as long as it stands; only :meth:`forget` removes one.  A path with
        no entry takes :attr:`floor`.
        """
        with self._lock:
            entry = self._entries.get(store_key(path))
        return self._floor if entry is None else entry

    def __len__(self) -> int:
        """How many paths carry an entry. For a host's own assertions and this suite's."""
        with self._lock:
            return len(self._entries)
