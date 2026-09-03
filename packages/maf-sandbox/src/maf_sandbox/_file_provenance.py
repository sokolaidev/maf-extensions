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

**Everything else is the host's to declare.** Content placed before the run, or written past
the store object by another process, is not a tool call and is not observed here.  A host that
knows how its store is fed says so once with ``floor=``, and a recorded entry always beats it —
so a trusted floor can never lift bytes the model wrote.
"""

from __future__ import annotations

import hashlib
import threading

from ._protocol import SourceIntegrity

__all__ = [
    "DELETE_TOOL",
    "FILE_STORE_WRITE_TOOLS",
    "PATH_ARGUMENT",
    "WHOLE_CONTENT_ARGUMENT",
    "FileStoreProvenance",
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

#: The argument carrying the whole of a file's new content, on the one tool that has it.
#: ``file_access_replace`` and ``file_access_replace_lines`` describe an *edit*, so what the
#: path ends up holding is not in their arguments and an entry for them carries no digest.
WHOLE_CONTENT_ARGUMENT = "content"

DELETE_TOOL = "file_access_delete"


def _digest(content: str) -> str:
    """A content digest, for binding an entry to the bytes it describes rather than to a path.

    Not a security primitive and not defending against a chosen-prefix attack: both sides of
    every comparison are content this process already holds, and the question asked is "are
    these the same bytes", never "did someone forge these bytes".
    """
    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()


class FileStoreProvenance:
    """What a host knows about the integrity of the content in one agent file store.

    Held by the host, passed to :func:`file_store_provenance_middleware` so agent-driven writes
    are recorded into it, and read by a kind — or by the host's own listing callable — to answer
    what a named file is worth.

    **One store.**  A path is the whole key, so a host wiring two providers over two stores
    needs one of these each; the tools carry no store identity for this to key on.  Reading a
    kind against the wrong one would answer about a file it never read.

    Args:
        floor: What applies to a path with no recorded entry — content placed before the run,
            or written past the store object by something that is not a tool call. ``None``,
            the default, means *unestablished*: this host has not said, and a caller must treat
            the answer as it treats any source the framework has not established.
    """

    def __init__(self, *, floor: SourceIntegrity | None = None) -> None:
        self._floor = None if floor is None else SourceIntegrity(str(floor))
        # Guarded because a synchronous tool body runs on a `asyncio.to_thread` pool thread
        # while the middleware recording writes runs on the event loop.
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[SourceIntegrity, str | None]] = {}

    @property
    def floor(self) -> SourceIntegrity | None:
        """What a path with no recorded entry is worth, as the host declared it."""
        return self._floor

    def record(self, path: str, *, integrity: SourceIntegrity, content: str | None = None) -> None:
        """Record that ``path`` holds content of ``integrity``.

        ``content`` binds the entry to the bytes it describes: where it is given, the entry is
        served only while the path still holds those bytes, so an overwrite this never saw
        cannot keep serving the old answer. Where it is not — an edit, whose result is not in
        the call that made it — the entry has no digest and is served for the path outright,
        which is the conservative direction for the untrusted entries this records.
        """
        with self._lock:
            self._entries[path] = (
                SourceIntegrity(str(integrity)),
                None if content is None else _digest(content),
            )

    def forget(self, path: str) -> None:
        """Drop any entry for ``path`` — what a delete leaves behind.

        The path then falls to :attr:`floor`, which is right: the file the entry described is
        gone, and anything later found under that name arrived by a route this never saw.
        """
        with self._lock:
            self._entries.pop(path, None)

    def integrity_of(self, path: str, content: str | None = None) -> SourceIntegrity | None:
        """What ``path`` is worth, or ``None`` where nothing here establishes it.

        ``content`` is what the caller just read. Pass it wherever you have it: an entry
        carrying a digest answers only while the bytes still match, and falls to the floor when
        they do not, so a path rewritten by something that is not a tool call cannot go on
        being answered from a record of what it used to hold.
        """
        with self._lock:
            entry = self._entries.get(path)
        if entry is None:
            return self._floor
        integrity, digest = entry
        if digest is not None and content is not None and _digest(content) != digest:
            return self._floor
        return integrity

    def __len__(self) -> int:
        """How many paths carry an entry. For a host's own assertions and this suite's."""
        with self._lock:
            return len(self._entries)
