"""``FileStoreProvenance``: what is known about the integrity of content in an agent file store.

A kind reading the agent's file store reads content the framework can no longer label, so what
this records is the one fact still recoverable at the tool-call boundary: that an agent-driven
call wrote a path.  Three invariants hold it together, and each is a property rather than a
convention a caller could break.

* **Every entry is untrusted**, because :meth:`FileStoreProvenance.record` takes no integrity
  argument.  Recording twice records the same thing, so recording only ever lowers — and
  :meth:`FileStoreProvenance.forget` is the one thing that does not, which is why a reader takes
  :meth:`FileStoreProvenance.state_of` either side of its read rather than a value after it.
* **An entry is about the path**, not a version of its bytes, and it stands until
  :meth:`FileStoreProvenance.forget`.
* **The floor applies only to a path with no recorded entry**, so an entry always beats it and
  a trusted floor can never lift bytes the model wrote — and a *trusted* floor is refused
  outright unless a middleware was built against the record, since without one there are no
  entries for it to lose to.

``docs/sandbox/hosts.md`` carries why the boundary is the tool call rather than the store, and
``docs/sandbox/research/labelling-the-file-store.md`` the measurements behind it.
"""

from __future__ import annotations

import re
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

#: One pass over the string, rather than a loop that rescans it for each pair it removes.
_REPEATED_SEPARATORS = re.compile(r"/+")


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
    return _REPEATED_SEPARATORS.sub("/", path.strip().replace("\\", "/"))


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

    **A reader wants what this record said *throughout* its read, and two readings cannot give
    it that.**  :meth:`record` only adds, but :meth:`forget` removes, so the value can move away
    and back inside one read and leave both readings identical.  Take :meth:`state_of`, which
    samples the value and the count under one lock: equal counts either side mean nothing
    happened in between and the reading describes the whole interval, and a changed count means
    the reader must not claim anything.  The two accessors called in turn would answer about two
    instants instead, and a write between them is the one arrangement the count cannot catch.
    :meth:`~maf_sandbox.maf.SandboxToolSession.read_file` does exactly that, given the record.

    **One residue is not closable from here.**  A write is recorded once the writing tool call
    *returns*, so bytes already written by a call still in flight are not yet in the record.  A
    ``TRUSTED`` floor is therefore still a claim about that residue, though no longer about the
    whole span between a listing and a read.  ``None`` and ``UNTRUSTED`` floors have nowhere
    weaker to fall and are unaffected.
    """

    def __init__(self, *, floor: SourceIntegrity | None = None) -> None:
        self._floor = None if floor is None else SourceIntegrity(str(floor))
        # Guarded because a synchronous tool body runs on a `asyncio.to_thread` pool thread
        # while the middleware recording writes runs on the event loop.
        self._lock = threading.Lock()
        self._entries: dict[str, SourceIntegrity] = {}
        #: How many times anything in this record has changed.  Only ever counts up, which is
        #: what lets a reader tell "unchanged" from "changed back" — see :meth:`state_of`.  One
        #: counter rather than one per path: a per-path count cannot be dropped when a path is
        #: forgotten without losing the very history it exists to carry, so it would grow with
        #: every temporary file a long-lived host ever writes.
        self._epoch = 0
        self._observed = False

    def _note_observer(self) -> None:
        """Mark that a middleware has been built against this record.

        Private because constructing :func:`~maf_sandbox.file_store_provenance_middleware` is the
        only supported way to lift the refusal in :meth:`integrity_of` — a caller that could set
        this directly could clear the refusal without restoring the observation it stands for.
        """
        with self._lock:
            self._observed = True

    @property
    def floor(self) -> SourceIntegrity | None:
        """What a path with no recorded entry is worth, as the host declared it."""
        return self._floor

    def record(self, path: str) -> None:
        """Record that an agent-driven call wrote ``path``.

        ``path`` is keyed through :func:`store_key`, here and in :meth:`integrity_of` alike, so a
        record filed under one spelling is found under every spelling of the same file.

        **There is no integrity argument, so recording only ever lowers.**  Every entry it can
        hold is untrusted, so recording twice is recording the same thing and no caller — not
        this package's middleware, not a host's own — can *record* a path back up to trusted.
        Dropping one does raise it, which is :meth:`forget`'s whole purpose and the reason a
        reader brackets its read rather than trusting a single later look.

        **An entry is about the path, not about a version of its content.**  It records that the
        model has written here, which stays true of every later version: nothing the model writes
        afterwards makes the file host-authored again.  Binding the entry to a digest of the bytes
        would say the opposite — a path whose content changed would stop matching and fall to the
        floor, and a trusted floor would then answer for a file the model demonstrably wrote.
        """
        with self._lock:
            key = store_key(path)
            if key not in self._entries:
                self._entries[key] = SourceIntegrity.UNTRUSTED
                self._epoch += 1

    def forget(self, path: str) -> None:
        """Drop any entry for ``path``, returning it to :attr:`floor`.

        For a host that has **established** the file is gone.  Nothing in this package calls it:
        the file-store tools answer a failed delete with a sentence rather than an exception, so
        an observer cannot tell a delete that removed the file from one that did not, and
        forgetting on the strength of a call having been made would return a path to a trusted
        floor while the model's bytes were still in it.

        **This is the one method that can raise what a path is worth**, which is why a reader
        consults this record either side of its read rather than after it — see
        :meth:`~maf_sandbox.maf.SandboxToolSession.read_file`.  A call to this racing a read is
        then harmless rather than something a host has to serialise against.
        """
        with self._lock:
            if self._entries.pop(store_key(path), None) is not None:
                self._epoch += 1

    def _sample(self, path: str) -> tuple[SourceIntegrity | None, bool, int]:
        """This path's entry, whether anything observes writes, and its count — under one lock.

        One acquisition rather than three, because a reader needs the entry and the count to
        describe the *same* instant: taken separately, a write landing between them yields the
        integrity from before it and the count from after, which reads as a still interval and
        labels the bytes with the value the write replaced.
        """
        with self._lock:
            return self._entries.get(store_key(path)), self._observed, self._epoch

    def state_of(self, path: str) -> tuple[SourceIntegrity | None, int]:
        """What ``path`` is worth, and how many times this record has changed, sampled together.

        The pair :meth:`~maf_sandbox.maf.SandboxToolSession.read_file` takes either side of a
        read: an unchanged count means nothing moved in between, so the value describes the whole
        interval rather than one instant of it.  Both come from one lock, because two calls would
        answer about two instants and a write between them is the one arrangement a count cannot
        rescue.

        **The count is the record's, not the path's.**  A mutation anywhere ends every interval
        in flight, so a busy store costs concurrent reads their label rather than their
        correctness — and the alternative is a per-path count that can never be discarded,
        because discarding it is what lets a path look untouched after being written and
        forgotten.

        Raises:
            ValueError: as :meth:`integrity_of` does, and for the same reason.
        """
        entry, observed, generation = self._sample(path)
        return self._answer(entry, observed), generation

    def integrity_of(self, path: str) -> SourceIntegrity | None:
        """What ``path`` is worth, or ``None`` where nothing here establishes it.

        An entry answers for as long as it stands; only :meth:`forget` removes one.  A path with
        no entry takes :attr:`floor`.

        Raises:
            ValueError: where the floor is :data:`~maf_sandbox.SourceIntegrity.TRUSTED` and no
                middleware was ever built against this record.  The
                floor is a claim about the paths *no tool call wrote*, and with nothing observing
                the calls there is no such thing as a path a tool call wrote: every path would
                answer trusted, model-written ones included.
        """
        entry, observed, _ = self._sample(path)
        return self._answer(entry, observed)

    def _answer(self, entry: SourceIntegrity | None, observed: bool) -> SourceIntegrity | None:
        """What a snapshot of this record means, without taking another one.

        Separate from :meth:`_sample` so :meth:`state_of` can reach the same verdict from the
        snapshot it already holds.  Looking again to resolve the floor would put a second
        instant into a pair whose whole purpose is to describe one.
        """
        if entry is not None:
            return entry
        if self._floor is SourceIntegrity.TRUSTED and not observed:
            raise ValueError(
                "FileStoreProvenance was given floor=SourceIntegrity.TRUSTED and no "
                "file_store_provenance_middleware was ever built against it, so nothing records "
                "what the model writes and every path would answer trusted — model-written files "
                "included. Wire file_store_provenance_middleware(record) into the agent's "
                "middleware beside the information-flow middleware, or drop floor= and let an "
                "unwritten path stay unestablished."
            )
        return self._floor

    def __len__(self) -> int:
        """How many paths carry an entry. For a host's own assertions and this suite's."""
        with self._lock:
            return len(self._entries)
