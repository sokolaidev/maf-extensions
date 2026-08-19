"""The attacks a backend serving ``FILES_OUT`` has to survive, written once for all of them.

    await assert_files_out_conformance(MySubject(sandbox, "/maf-sandbox/work"))

**Run it against a real instance.**  A backend's own suite fakes its provider seam, and a faked
seam agrees with whatever its author believed; these probes plant a hostile layout through the
public surface and attack it there, so what passes is the provider's real behaviour.
:class:`ConformanceSubject` is the seam — a sandbox, plus the two planting operations the
protocol has no word for and never will.

Two things it does not do.  It does not prove the *premise*, that the provider really resolves
through a link and the refusals are refusing something reachable: that means looking under a
backend's own public surface, at its unconfined stat or its raw payload, and only that backend
can, so each keeps that test at home.  And it grades nothing — a backend that cannot recognise
a link still refuses every path here and fails the two probes about *naming* what it refused.

The same shape covers the other capabilities, each as its own suite:
:func:`assert_files_in_conformance` (:data:`~maf_sandbox.Capability.FILES_IN`),
:func:`assert_exec_conformance` (:data:`~maf_sandbox.Capability.EXEC`), and
:func:`assert_files_delete_conformance`
(:data:`~maf_sandbox.Capability.FILES_DELETE`).  The FILES_IN, EXEC and FILES_DELETE probes
verify through :meth:`Sandbox.exec` — ``cat``, ``test``, ``printf``, ``pwd``, ``sleep`` —
rather than the pull surface, because a backend with no pull surface still owes those
capabilities, and their command needs are those of ``PosixGuestSubject``'s own ``ln``.  What
those suites assert is measured against the guest the image ships, which for the suites that
run in CI is the image the workflow names.

**The EXEC suite may not leave the sandbox alive.**  Its last probe asserts the
``TimeoutError`` contract, and two backends discard the whole sandbox when a call times out —
that is the documented recovery, not a defect.  A caller sharing one sandbox across suites runs
EXEC last, and a caller that wants what comes after acquires a second sandbox.

**The EXEC suite plants its own working directory.**  ``working_directory`` does not exist
after ``acquire`` — no backend creates ``spec.work_dir`` and the protocol does not promise it —
so the suite writes a marker file first: the caller-creates rule, with the reasoning and the
open question of whether ``acquire`` should owe it filed as #466.  A subject whose sandbox has
no ``write_file`` cannot run the suite.

Nothing here imports a test framework: this module ships in the wheel.  A failure raises
:class:`ConformanceFailure` naming every probe that failed rather than the first.
"""

from __future__ import annotations

import posixpath
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from ._protocol import Capability, EntryKind, Sandbox

__all__ = [
    "EXEC_PROBES",
    "FILES_DELETE_PROBES",
    "FILES_IN_PROBES",
    "FILES_OUT_PROBES",
    "ConformanceFailure",
    "ConformancePaths",
    "ConformanceSubject",
    "PosixGuestSubject",
    "Probe",
    "ProbeResult",
    "assert_exec_conformance",
    "assert_files_delete_conformance",
    "assert_files_in_conformance",
    "assert_files_out_conformance",
    "measure_files_delete_probes",
    "plant_layout",
    "run_exec_probes",
    "run_files_delete_probes",
    "run_files_in_probes",
    "run_files_out_probes",
]

#: What the read probes allow. Large enough that nothing here is refused for its size — every
#: refusal these probes assert is a confinement refusal, and a cap breach would mask one.
_READ_CAP = 1 << 20

_SECRET = b"the guest must not reach this\n"
_INSIDE = b"a legitimate output\n"

#: High codepoints and no NUL: a payload every UTF-8 decoder agrees on, so the probe asserts
#: what the protocol states (``stdout: str``) and nothing further. The bytes that *cannot*
#: survive a decode — the ones a ``errors="replace"`` transport turns into U+FFFD — are a
#: contract the protocol does not state yet; the issue filed alongside this suite carries the
#: proposal to state it, and the probe narrows rather than guesses.
_BINARY = "ünïcödé→payload".encode() + bytes(range(1, 128))


class ConformanceSubject(Protocol):
    """The seam a backend fills in so the probes can plant their layout and attack it.

    Planting is a subject method because creating a link is the *guest's* move — a sandbox that
    offered it would hand the attacker the tool — so each backend plants however its guest
    allows.  ``capabilities`` decides which probes run: a backend that never claimed
    :data:`~maf_sandbox.Capability.FILES_LIST` skips the ones attacking
    :meth:`Sandbox.list_dir` rather than failing them.
    """

    @property
    def sandbox(self) -> Sandbox:
        """The sandbox under attack, already acquired."""
        ...

    @property
    def working_directory(self) -> str:
        """The absolute guest path every probe's relative path resolves against."""
        ...

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What the backend claims — read to skip the probes it never promised to pass."""
        ...

    async def plant_file(self, path: str, content: bytes) -> None:
        """Create a regular file at an absolute guest ``path``, parents included."""
        ...

    async def plant_symlink(self, path: str, target: str) -> None:
        """Create a link at an absolute guest ``path`` pointing at ``target``."""
        ...


@dataclass(frozen=True)
class PosixGuestSubject:
    """A :class:`ConformanceSubject` for any backend whose guest is Linux and has ``ln``.

    Both shipped backends fill the seam this way.  ``ln`` is a requirement of this harness and
    not of the protocol — a Windows guest or a distroless image writes its own subject and runs
    the same probes unchanged.
    """

    sandbox: Sandbox
    working_directory: str
    capabilities: frozenset[Capability]
    exec_timeout: float = 60.0

    async def plant_file(self, path: str, content: bytes) -> None:
        await self.sandbox.write_file(path, content)

    async def plant_symlink(self, path: str, target: str) -> None:
        result = await self.sandbox.exec(
            ["ln", "-sfn", target, path],
            working_directory=self.working_directory,
            timeout=self.exec_timeout,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"could not plant {path!r} -> {target!r} in the guest "
                f"(exit {result.exit_code}): {result.stderr.strip()}"
            )


@dataclass(frozen=True)
class ConformancePaths:
    """The hostile layout, derived from a subject's working directory.

    ``outside`` is a **sibling** rather than a child of the root: ``/maf-sandbox/work-outside`` shares a
    string prefix with ``/maf-sandbox/work`` and is still outside it, so a backend comparing prefixes
    without the separator fails here rather than in production.  Every path is absolute,
    because planting goes through :meth:`Sandbox.write_file`; the probes attack with paths
    relative to ``work``.
    """

    work: str
    outside: str

    @classmethod
    def under(cls, working_directory: str) -> ConformancePaths:
        work = posixpath.normpath(working_directory)
        return cls(work=work, outside=f"{work}-outside")

    @property
    def secret(self) -> str:
        """A file outside the working directory. No probe may ever come back with its bytes."""
        return f"{self.outside}/secret.txt"

    @property
    def nested_secret(self) -> str:
        """The same, one directory deeper — what a listing through a link would enumerate."""
        return f"{self.outside}/sub/leaf.txt"

    @property
    def inside(self) -> str:
        """A legitimate nested output: the positive control every refusal is measured against."""
        return f"{self.work}/real/inside.txt"

    @property
    def plain(self) -> str:
        """A regular file, planted to stand where a directory is expected."""
        return f"{self.work}/plain.txt"

    @property
    def linked_directory(self) -> str:
        """``work/link-dir -> outside``: the escape, as a parent component."""
        return f"{self.work}/link-dir"

    @property
    def under_linked_directory(self) -> str:
        """A working directory one level *inside* the link — the ``/maf-sandbox -> /`` case.

        Distinct from making the work dir itself the link: an implementation whose walk starts
        at the work dir passes that one and still reads straight through this.
        """
        return f"{self.linked_directory}/sub"

    @property
    def linked_file(self) -> str:
        """``work/link-file -> outside/secret.txt``: the escape, as a final component."""
        return f"{self.work}/link-file"


@dataclass(frozen=True)
class Probe:
    """One attack, with the reason it is in the suite.

    ``why`` is printed with a failure: a probe whose point is not written down is a probe
    someone deletes when it fails.
    """

    name: str
    why: str
    requires: frozenset[Capability]
    run: Callable[[ConformanceSubject, ConformancePaths], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class ProbeResult:
    """What one probe did. ``skipped`` and ``failure`` are never both set."""

    probe: Probe
    failure: str | None = None
    skipped: str | None = None

    @property
    def passed(self) -> bool:
        return self.failure is None and self.skipped is None


class ConformanceFailure(AssertionError):
    """Raised by the ``assert_*_conformance`` functions, naming every probe that failed.

    An :class:`AssertionError` so a test framework reports it as a failed assertion rather than
    an error, without this module importing one.
    """

    def __init__(self, results: tuple[ProbeResult, ...], suite: str = "FILES_OUT") -> None:
        self.results = results
        self.failures = tuple(r for r in results if r.failure is not None)
        lines = [f"{len(self.failures)} of {len(results)} {suite} conformance probes failed:"]
        for result in self.failures:
            lines.append(f"  - {result.probe.name}: {result.failure}")
            lines.append(f"    why it is in the suite: {result.probe.why}")
        super().__init__("\n".join(lines))


async def _refused_with(
    expected: type[BaseException], what: str, call: Awaitable[object]
) -> BaseException:
    """Await ``call``, insisting it raises exactly the family ``expected``.

    The wrong exception is a failure, not a pass: a suite that accepted any refusal could not
    tell an escape from an ``ENOTDIR``, which is the distinction it exists to check.
    """
    try:
        await call
    except expected as caught:
        return caught
    except Exception as wrong:
        # Reported, not swallowed: the wrong refusal is the finding. `Exception` rather than
        # `BaseException` so a cancellation on the way out stays a cancellation.
        raise AssertionError(
            f"{what} raised {type(wrong).__name__} ({wrong}), not {expected.__name__}"
        ) from wrong
    raise AssertionError(f"{what} returned instead of raising {expected.__name__}")


async def _probe_a_legitimate_read_still_works(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    entry = await subject.sandbox.stat_file(
        "real/inside.txt", working_directory=subject.working_directory
    )
    if entry is None:
        raise AssertionError("stat of the planted file returned None — the layout did not land")
    if entry.kind is not EntryKind.FILE:
        raise AssertionError(f"the planted regular file stats as {str(entry.kind)!r}")
    if entry.size_bytes != len(_INSIDE):
        raise AssertionError(f"the planted file stats as {entry.size_bytes} bytes")
    content = await subject.sandbox.read_file(
        "real/inside.txt", working_directory=subject.working_directory, max_bytes=_READ_CAP
    )
    if content != _INSIDE:
        raise AssertionError(f"the planted file read back as {content!r}")


async def _probe_a_link_is_named_a_link(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    entry = await subject.sandbox.stat_file(
        "link-file", working_directory=subject.working_directory
    )
    if entry is None:
        raise AssertionError("stat of the planted link returned None — the layout did not land")
    if entry.kind is not EntryKind.SYMLINK:
        raise AssertionError(f"the link stats as {str(entry.kind)!r}, not 'symlink'")
    if entry.size_bytes is not None:
        raise AssertionError(
            f"the link reports {entry.size_bytes} bytes — that is the length of the target "
            "string, not of anything readable, and passing it on answers a size question "
            "about a file nobody measured"
        )


async def _probe_a_link_is_never_read(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    await _refused_with(
        OSError,
        "reading a link",
        subject.sandbox.read_file(
            "link-file", working_directory=subject.working_directory, max_bytes=_READ_CAP
        ),
    )


async def _probe_stat_through_a_linked_parent(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "stat through a linked parent",
        subject.sandbox.stat_file(
            "link-dir/secret.txt", working_directory=subject.working_directory
        ),
    )


async def _probe_read_through_a_linked_parent(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "read through a linked parent",
        subject.sandbox.read_file(
            "link-dir/secret.txt",
            working_directory=subject.working_directory,
            max_bytes=_READ_CAP,
        ),
    )


async def _probe_a_linked_working_directory(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "stat against a working directory that is itself a link",
        subject.sandbox.stat_file("secret.txt", working_directory=paths.linked_directory),
    )
    await _refused_with(
        ValueError,
        "read against a working directory that is itself a link",
        subject.sandbox.read_file(
            "secret.txt", working_directory=paths.linked_directory, max_bytes=_READ_CAP
        ),
    )


async def _probe_a_linked_ancestor_of_the_working_directory(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "stat under a working directory whose own ancestor is a link",
        subject.sandbox.stat_file("leaf.txt", working_directory=paths.under_linked_directory),
    )
    await _refused_with(
        ValueError,
        "read under a working directory whose own ancestor is a link",
        subject.sandbox.read_file(
            "leaf.txt", working_directory=paths.under_linked_directory, max_bytes=_READ_CAP
        ),
    )


async def _probe_listing_under_a_linked_ancestor(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "listing a working directory whose own ancestor is a link",
        subject.sandbox.list_dir(".", working_directory=paths.under_linked_directory),
    )


async def _probe_a_plain_parent_is_not_an_escape(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        NotADirectoryError,
        "stat through a regular file standing where a directory was expected",
        subject.sandbox.stat_file(
            "plain.txt/deeper.txt", working_directory=subject.working_directory
        ),
    )


async def _probe_listing_a_linked_directory(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "listing a linked directory",
        subject.sandbox.list_dir("link-dir", working_directory=subject.working_directory),
    )


async def _probe_listing_through_a_linked_parent(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await _refused_with(
        ValueError,
        "listing through a linked parent",
        subject.sandbox.list_dir("link-dir/sub", working_directory=subject.working_directory),
    )


async def _probe_a_listing_names_its_links(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    entries = await subject.sandbox.list_dir(".", working_directory=subject.working_directory)
    by_path = {entry.path: entry for entry in entries}
    link = by_path.get("link-file")
    if link is None:
        raise AssertionError(
            f"the working directory's listing does not name 'link-file' at all: "
            f"{sorted(by_path)}. Hiding a link leaves a caller a name to read with no warning "
            "attached to it"
        )
    if link.kind is not EntryKind.SYMLINK:
        raise AssertionError(f"'link-file' is listed as {str(link.kind)!r}, not 'symlink'")


#: The probes, in the order a reader should meet them: the positive control first, then what a
#: backend can say about a link, then the component walk, then enumeration.
FILES_OUT_PROBES: tuple[Probe, ...] = (
    Probe(
        name="a-legitimate-read-still-works",
        why=(
            "every other probe asserts a refusal, and a backend that refused everything would "
            "pass all of them. This one fails if the hostile layout never landed."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_legitimate_read_still_works,
    ),
    Probe(
        name="a-link-is-named-a-link",
        why=(
            "a caller above the backend has to tell an escape from a fifo, and EntryKind.OTHER "
            "cannot say which. This is the discriminator the walk below is written against."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_link_is_named_a_link,
    ),
    Probe(
        name="a-link-is-never-read",
        why=(
            "a link is refused whether or not its target would have resolved somewhere "
            "legitimate: that judgement is made with the guest's filesystem in view and "
            "answered with whichever one the reader can actually see."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_link_is_never_read,
    ),
    Probe(
        name="stat-through-a-linked-parent",
        why=(
            "the natural, wrong reading of the rule: with out -> /etc, out/hostname stats as a "
            "regular 12-byte file. No byte crosses, but a type and a size from outside the "
            "working directory do."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_stat_through_a_linked_parent,
    ),
    Probe(
        name="read-through-a-linked-parent",
        why="the same escape with the bytes attached, which is the one that empties the guest.",
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_read_through_a_linked_parent,
    ),
    Probe(
        name="a-linked-working-directory",
        why=(
            "the working directory is a component too. An implementation that classifies only "
            "the parents of the path it was handed, relative to the work dir, never looks at "
            "the work dir it resolved against."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_linked_working_directory,
    ),
    Probe(
        name="a-linked-ancestor-of-the-working-directory",
        why=(
            "the walk starts above the working directory, not at it: a nested work dir has "
            "ancestors the guest can replace, and an implementation beginning at the work dir "
            "stats straight through them. This is the /maf-sandbox -> / case, and it is the one the "
            "probe above does not reach."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_linked_ancestor_of_the_working_directory,
    ),
    Probe(
        name="a-plain-parent-is-not-an-escape",
        why=(
            "the refusal has to name the right fault. A non-directory standing where a "
            "directory was expected is an ordinary ENOTDIR, and reporting it as a confinement "
            "failure accuses the guest of an attack it did not make."
        ),
        requires=frozenset({Capability.FILES_OUT}),
        run=_probe_a_plain_parent_is_not_an_escape,
    ),
    Probe(
        name="listing-a-linked-directory",
        why=(
            "enumeration passes through a link as readily as a read does, so list_dir walks "
            "one component deeper than the other two — the directory being listed included."
        ),
        requires=frozenset({Capability.FILES_OUT, Capability.FILES_LIST}),
        run=_probe_listing_a_linked_directory,
    ),
    Probe(
        name="listing-through-a-linked-parent",
        why="the duty lives at all three entry points, and a listing is the one that hands "
        "back names a caller goes on to read.",
        requires=frozenset({Capability.FILES_OUT, Capability.FILES_LIST}),
        run=_probe_listing_through_a_linked_parent,
    ),
    Probe(
        name="listing-under-a-linked-ancestor",
        why="the duty at the third entry point, for the ancestor case as well as the path's own.",
        requires=frozenset({Capability.FILES_OUT, Capability.FILES_LIST}),
        run=_probe_listing_under_a_linked_ancestor,
    ),
    Probe(
        name="a-listing-names-its-links",
        why=(
            "a link inside the working directory is reported rather than hidden: the caller is "
            "being told what is there, and a name handed back with its type erased is a name "
            "read without the warning."
        ),
        requires=frozenset({Capability.FILES_OUT, Capability.FILES_LIST}),
        run=_probe_a_listing_names_its_links,
    ),
)


async def plant_layout(subject: ConformanceSubject) -> ConformancePaths:
    """Build the hostile layout the probes attack, and return where everything is.

    Public so a backend's own premise test can attack the same layout with its unconfined stat.
    """
    paths = ConformancePaths.under(subject.working_directory)
    await subject.plant_file(paths.inside, _INSIDE)
    await subject.plant_file(paths.plain, b"not a directory\n")
    await subject.plant_file(paths.secret, _SECRET)
    await subject.plant_file(paths.nested_secret, _SECRET)
    await subject.plant_symlink(paths.linked_directory, paths.outside)
    await subject.plant_symlink(paths.linked_file, paths.secret)
    return paths


async def run_files_out_probes(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Plant the layout and run every probe, returning what each one did.

    Every probe runs even after one fails, so a backend is told everything wrong with it at
    once.  One requiring a capability the subject does not declare is skipped, not failed.

    Anything a probe raises is a failure of that probe and nothing else: a backend that answers
    a positive control with its own ``RuntimeError`` would otherwise take the whole run down
    and report none of the refusals that did work.

    A subject that does not declare :data:`~maf_sandbox.Capability.FILES_OUT` is refused rather
    than run.  Skipping is right for a capability a backend never claimed — ``FILES_LIST`` is
    the case it exists for — but skipping *everything* and returning success is a green run
    that attacked nothing, which is worse than no run at all.
    """
    return await _run_suite(subject, Capability.FILES_OUT, plant_layout, FILES_OUT_PROBES)


async def assert_files_out_conformance(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the probes and raise :class:`ConformanceFailure` if any failed.

    Returns the results on success so a caller can assert on what was *skipped*: a backend that
    silently stopped declaring ``FILES_LIST`` would otherwise go green on three fewer probes.
    """
    return _assert_conformance(await run_files_out_probes(subject), "FILES_OUT")


# ---------------------------------------------------------------------------
# FILES_IN — what a declared write owes
# ---------------------------------------------------------------------------


async def _probe_a_write_lands_and_reads_back(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.sandbox.write_file(f"{paths.work}/real/written.txt", "written")
    result = await subject.sandbox.exec(
        ["test", "-f", "real/written.txt"],
        working_directory=subject.working_directory,
        timeout=60,
    )
    if result.exit_code != 0:
        raise AssertionError("the written file is not visible at its guest path")
    back = await subject.sandbox.exec(
        ["cat", "real/written.txt"], working_directory=subject.working_directory, timeout=60
    )
    if back.stdout != "written":
        raise AssertionError(f"the written file reads back as {back.stdout!r}, not 'written'")


async def _probe_bytes_survive_the_round_trip(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.sandbox.write_file(f"{paths.work}/binary.bin", _BINARY)
    back = await subject.sandbox.exec(
        ["cat", "binary.bin"], working_directory=subject.working_directory, timeout=60
    )
    # What is asserted is the protocol's own promise — `stdout: str` — over a payload every
    # UTF-8 decoder agrees on: multi-byte sequences and control bytes that a text-shaped hop
    # in the transport (CRLF translation, a decode/encode pair, a truncation) would alter.
    # Non-UTF-8 bytes are deliberately out of scope: whether exec must carry them losslessly
    # (surrogateescape) is a contract the protocol does not state, backends disagree on today,
    # and the issue filed with this suite proposes to state.
    decoded = _BINARY.decode("utf-8")
    if back.stdout != decoded:
        raise AssertionError(
            f"{len(_BINARY)} bytes went in and came back altered — a text-shaped hop in the "
            "transport translated or truncated them"
        )


async def _probe_str_content_is_utf8(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    await subject.sandbox.write_file(f"{paths.work}/text.txt", "naïve")
    back = await subject.sandbox.exec(
        ["cat", "text.txt"], working_directory=subject.working_directory, timeout=60
    )
    if back.stdout != "naïve":
        raise AssertionError(f"'naïve' went in as str and {back.stdout!r} came back")


async def _probe_a_second_write_replaces(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.sandbox.write_file(f"{paths.work}/overwritten.txt", "first")
    await subject.sandbox.write_file(f"{paths.work}/overwritten.txt", "second")
    back = await subject.sandbox.exec(
        ["cat", "overwritten.txt"], working_directory=subject.working_directory, timeout=60
    )
    if back.stdout != "second":
        raise AssertionError(f"an overwritten file reads back as {back.stdout!r}, not 'second'")


async def _probe_parents_are_created(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    await subject.sandbox.write_file(f"{paths.work}/deep/er/est/leaf.txt", "deep")
    result = await subject.sandbox.exec(
        ["test", "-f", "deep/er/est/leaf.txt"],
        working_directory=subject.working_directory,
        timeout=60,
    )
    if result.exit_code != 0:
        raise AssertionError("a write under unannounced parents did not create them")


FILES_IN_PROBES: tuple[Probe, ...] = (
    Probe(
        name="a-write-lands-and-reads-back",
        why=(
            "every other FILES_IN probe reads a file back out, so one that never landed would "
            "fail all of them for the wrong reason. This names the landing itself."
        ),
        requires=frozenset({Capability.FILES_IN}),
        run=_probe_a_write_lands_and_reads_back,
    ),
    Probe(
        name="bytes-survive-the-round-trip",
        why=(
            "FILES_IN carries bytes — an in-door with a PNG or a spreadsheet — and any "
            "text-shaped hop in the transport corrupts them in ways a caller cannot detect: "
            "half a PNG returned as success is indistinguishable from a whole one. Asserted "
            "over UTF-8-representable bytes, which is the protocol's stated `stdout: str`; "
            "whether exec must carry non-UTF-8 bytes losslessly is an unstated contract, "
            "proposed separately rather than guessed here."
        ),
        requires=frozenset({Capability.FILES_IN}),
        run=_probe_bytes_survive_the_round_trip,
    ),
    Probe(
        name="str-content-is-utf8",
        why=(
            "the protocol promises UTF-8 for str whatever the host's locale says; a backend "
            "encoding with the host's default answers every non-ASCII write with mojibake on a "
            "machine whose locale is not UTF-8."
        ),
        requires=frozenset({Capability.FILES_IN}),
        run=_probe_str_content_is_utf8,
    ),
    Probe(
        name="a-second-write-replaces",
        why=(
            "append is a choice a caller makes, not a default a transport imposes: a fix-loop "
            "writes the same path twice and the second answer must be the file's whole content."
        ),
        requires=frozenset({Capability.FILES_IN}),
        run=_probe_a_second_write_replaces,
    ),
    Probe(
        name="parents-are-created",
        why=(
            "a declared output's path names directories the workload never made, and a write "
            "that refuses them pushes directory creation onto every kind, which the protocol "
            "gives them no way to do."
        ),
        requires=frozenset({Capability.FILES_IN}),
        run=_probe_parents_are_created,
    ),
)


async def _plant_files_in_layout(subject: ConformanceSubject) -> ConformancePaths:
    """Derive the paths the FILES_IN probes share.

    Each probe writes its own fixture through the surface under test — that surface is the
    point of the suite, and a probe that verified a write made elsewhere would report on state
    it did not create. It also keeps a write that raises (implicit parents unsupported, a
    refused path) inside its own probe's handling, so the run reports it as that probe's
    failure rather than aborting with a raw exception and no results.
    """
    return ConformancePaths.under(subject.working_directory)


async def run_files_in_probes(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the FILES_IN probes. Same contract as :func:`run_files_out_probes`."""
    return await _run_suite(subject, Capability.FILES_IN, _plant_files_in_layout, FILES_IN_PROBES)


async def assert_files_in_conformance(
    subject: ConformanceSubject,
) -> tuple[ProbeResult, ...]:
    """Run the FILES_IN probes and raise :class:`ConformanceFailure` if any failed."""
    return _assert_conformance(await run_files_in_probes(subject), "FILES_IN")


# ---------------------------------------------------------------------------
# EXEC — what a declared exec owes
# ---------------------------------------------------------------------------


async def _probe_an_argv_sequence_runs(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    result = await subject.sandbox.exec(
        ["printf", "ok-ran"], working_directory=subject.working_directory, timeout=60
    )
    if result.exit_code != 0:
        raise AssertionError(
            f"a plain argv sequence exited {result.exit_code}: {result.stderr.strip()}"
        )
    if result.stdout != "ok-ran":
        raise AssertionError(f"stdout came back as {result.stdout!r}, not 'ok-ran'")


async def _probe_exit_code_fidelity(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    # A string command, because `exit` is a shell builtin: the string form is the protocol's
    # own shell-command shape, and this doubles as the positive control for it.
    result = await subject.sandbox.exec(
        "exit 7", working_directory=subject.working_directory, timeout=60
    )
    if result.exit_code != 7:
        raise AssertionError(f"'exit 7' came back as exit code {result.exit_code}")


async def _probe_argv_is_quoted(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    hostile = "a b$(echo injected)c"
    # `printf '%s\n'` over the argument, then `wc -l`: the count is 1 only if the argument
    # arrived as one word. A backend joining argv unquoted runs the substitution and the
    # split, and the count — or the injected marker — gives the failure two ways to show.
    result = await subject.sandbox.exec(
        ["sh", "-c", "printf '%s\\n' \"$1\" | wc -l", "probe", hostile],
        working_directory=subject.working_directory,
        timeout=60,
    )
    if result.exit_code != 0:
        raise AssertionError(f"the quoting probe exited {result.exit_code}")
    count = result.stdout.strip()
    if count != "1":
        raise AssertionError(
            f"the argument containing spaces and a $( ) arrived as {count} words — it was "
            "joined into the command line unquoted, which is the injection the sequence form "
            "exists to prevent"
        )
    if "injected" in result.stdout:
        raise AssertionError("the $( ) in the argument was evaluated: the argv was injected")


async def _probe_working_directory_is_honoured(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    result = await subject.sandbox.exec(
        ["pwd"], working_directory=subject.working_directory, timeout=60
    )
    # `pwd -P` is not asked for: the contract is the directory the call named, and a logical
    # `pwd` answering it through a shell variable is as conformant as a physical one.
    if result.stdout.strip() != posixpath.normpath(subject.working_directory):
        raise AssertionError(
            f"pwd answered {result.stdout.strip()!r} against a working directory of "
            f"{subject.working_directory!r}"
        )


async def _probe_a_timeout_raises_timeout_error(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    # Last in EXEC_PROBES by design: the protocol lets a backend discard the whole sandbox to
    # stop a hung command, and two shipped ones do, so the sandbox this probe returns from may
    # be gone by the time the caller sees the result.
    started = time.monotonic()
    try:
        await subject.sandbox.exec(
            ["sleep", "30"], working_directory=subject.working_directory, timeout=1
        )
    except TimeoutError:
        # The bound must actually have elapsed — and only the bound. A backend borrowing the
        # exception for its own shorter ceiling — or raising it eagerly on any call — passes a
        # bare type check while breaking the caller's reading of its own budget running out;
        # one that ignores the argument and raises at a longer ceiling of its own makes the
        # caller's bound a lie from the other side. The tolerances are scheduling allowance,
        # not precision: the claim is "roughly between half the bound and four times it".
        elapsed = time.monotonic() - started
        if elapsed < 0.5:
            raise AssertionError(
                "TimeoutError arrived in under half the bound — it is the backend's own "
                "ceiling firing, not the caller's timeout expiring"
            ) from None
        if elapsed > 4.0:
            raise AssertionError(
                f"TimeoutError arrived after {elapsed:.1f}s against a 1s bound — the backend "
                "ignored the caller's timeout and fired its own, so the call was not bounded "
                "by the argument as exec promises"
            ) from None
        return
    except Exception as wrong:
        raise AssertionError(
            f"a call that overran its timeout raised {type(wrong).__name__} ({wrong}), not "
            "TimeoutError — the protocol reserves that exception for the bound expiring and "
            "nothing else"
        ) from wrong
    raise AssertionError("a call over its timeout returned instead of raising TimeoutError")


EXEC_PROBES: tuple[Probe, ...] = (
    Probe(
        name="an-argv-sequence-runs",
        why=(
            "the positive control: everything else here asserts a refusal or a fidelity, and a "
            "backend that ran nothing would pass all of them."
        ),
        requires=frozenset({Capability.EXEC}),
        run=_probe_an_argv_sequence_runs,
    ),
    Probe(
        name="exit-code-fidelity",
        why=(
            "a kind's whole diagnostic is the exit code — the fix-loop reads it to decide "
            "whether to try again — and a backend normalising every failure to 1 turns a "
            "compiler's '7 errors' into 'something went wrong'."
        ),
        requires=frozenset({Capability.EXEC}),
        run=_probe_exit_code_fidelity,
    ),
    Probe(
        name="argv-is-quoted",
        why=(
            "the sequence form is the safe default precisely because the backend quotes it; a "
            "backend that joins argv into a command line unquoted hands every argument to the "
            "shell, and an argument with spaces or $() is an injection."
        ),
        requires=frozenset({Capability.EXEC}),
        run=_probe_argv_is_quoted,
    ),
    Probe(
        name="working-directory-is-honoured",
        why=(
            "paths a kind passes are relative to the work dir it declared, so an exec ignoring "
            "working_directory reads and writes somewhere the caller cannot predict."
        ),
        requires=frozenset({Capability.EXEC}),
        run=_probe_working_directory_is_honoured,
    ),
    Probe(
        name="a-timeout-raises-timeout-error",
        why=(
            "the protocol states it in bold: TimeoutError from exec means the bound expired "
            "and nothing else. A caller reads it as its own budget running out; a backend "
            "borrowing the exception for another limit, or returning from an overrun, makes "
            "that reading false."
        ),
        requires=frozenset({Capability.EXEC}),
        run=_probe_a_timeout_raises_timeout_error,
    ),
)


async def _plant_nothing(subject: ConformanceSubject) -> ConformancePaths:
    """Plant the working directory the probes exec in — see the module docstring for why."""
    paths = ConformancePaths.under(subject.working_directory)
    await subject.sandbox.write_file(f"{paths.work}/.probe-cwd", b"")
    return paths


async def run_exec_probes(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the EXEC probes. Same contract as :func:`run_files_out_probes`.

    The subject's sandbox may not survive this suite — see the module docstring.
    """
    return await _run_suite(subject, Capability.EXEC, _plant_nothing, EXEC_PROBES)


async def assert_exec_conformance(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the EXEC probes and raise :class:`ConformanceFailure` if any failed."""
    return _assert_conformance(await run_exec_probes(subject), "EXEC")


# ---------------------------------------------------------------------------
# FILES_DELETE — what a declared remove owes
# ---------------------------------------------------------------------------


async def _assert_present(sandbox: Sandbox, path: str, working_directory: str, what: str) -> None:
    """``path`` still exists — the half of every refusal probe that a bare ``raises`` omits."""
    result = await sandbox.exec(
        ["test", "-e", path], working_directory=working_directory, timeout=60
    )
    if result.exit_code != 0:
        raise AssertionError(f"{what} did not survive: {path!r} is gone")


async def _probe_a_removal_removes(subject: ConformanceSubject, paths: ConformancePaths) -> None:
    await subject.sandbox.write_file(f"{paths.work}/doomed.txt", b"to be removed\n")
    await subject.sandbox.write_file(f"{paths.work}/bystander.txt", b"not the target\n")
    await subject.sandbox.remove("doomed.txt", working_directory=subject.working_directory)
    result = await subject.sandbox.exec(
        ["test", "-e", "doomed.txt"], working_directory=subject.working_directory, timeout=60
    )
    if result.exit_code == 0:
        raise AssertionError("the removed file is still there")
    # A removal that also took the working directory, or a neighbour, is not a removal of
    # ``path``: the method promises that path and nothing else.
    await _assert_present(
        subject.sandbox, "bystander.txt", subject.working_directory, "a bystander file"
    )


async def _probe_a_missing_path_is_success(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    # Idempotence is the two-call shape a finally-based cleanup actually runs: remove the
    # same path twice, the second call from nothing. A backend that succeeds on never-seen
    # paths but raises on the repeat breaks exactly the second call.
    await subject.sandbox.write_file(f"{paths.work}/was-here.txt", b"gone after the first call\n")
    await subject.sandbox.remove("was-here.txt", working_directory=subject.working_directory)
    await subject.sandbox.remove("was-here.txt", working_directory=subject.working_directory)


async def _probe_a_link_is_removed_never_followed(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.plant_file(f"{paths.outside}/target.txt", b"outside\n")
    await subject.plant_symlink(f"{paths.work}/link-out", f"{paths.outside}/target.txt")
    await subject.sandbox.remove("link-out", working_directory=subject.working_directory)
    link = await subject.sandbox.exec(
        ["test", "-e", "link-out"], working_directory=subject.working_directory, timeout=60
    )
    target = await subject.sandbox.exec(
        ["test", "-f", "../work-outside/target.txt"],
        working_directory=subject.working_directory,
        timeout=60,
    )
    if link.exit_code == 0:
        raise AssertionError("the removed link is still there")
    if target.exit_code != 0:
        raise AssertionError("the link's target went with it: the removal followed the link")


async def _probe_a_path_through_a_linked_parent_is_refused(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.plant_file(f"{paths.outside}/target.txt", b"outside\n")
    await subject.plant_symlink(paths.linked_directory, paths.outside)
    await _refused_with(
        ValueError,
        "removing through a linked parent",
        subject.sandbox.remove("link-dir/target.txt", working_directory=subject.working_directory),
    )
    # The refusal is only worth its exception type if nothing crossed: a backend that followed
    # the link, deleted the target, and then raised would pass the check above and have done
    # the exact damage the probe exists to catch.
    await _assert_present(
        subject.sandbox,
        "../work-outside/target.txt",
        subject.working_directory,
        "the target behind a refused linked-parent removal",
    )


async def _probe_a_directory_needs_recursive(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.plant_file(f"{paths.work}/dir/keeps.txt", b"a directory\n")
    await _refused_with(
        OSError,
        "removing a non-empty directory without recursive",
        subject.sandbox.remove("dir", working_directory=subject.working_directory),
    )
    await _assert_present(
        subject.sandbox, "dir/keeps.txt", subject.working_directory, "the refused directory"
    )


async def _probe_an_empty_directory_needs_recursive(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    # The protocol refuses empty directories too — no enumeration primitive to tell them
    # apart, so the rule has to be one every backend can keep — and an empty one is planted
    # through `exec`, the guest's own move, because `plant_file` cannot make an empty
    # anything: every path it names gains a file.
    await subject.sandbox.exec(
        ["mkdir", "-p", "empty"], working_directory=subject.working_directory, timeout=60
    )
    await _refused_with(
        OSError,
        "removing an empty directory without recursive",
        subject.sandbox.remove("empty", working_directory=subject.working_directory),
    )
    await _assert_present(
        subject.sandbox, "empty", subject.working_directory, "the refused empty directory"
    )


async def _probe_recursive_removes_the_tree(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.plant_file(f"{paths.work}/tree/leaf.txt", b"in the tree\n")
    await subject.plant_file(f"{paths.work}/next-to-tree.txt", b"not in the tree\n")
    await subject.sandbox.remove(
        "tree", working_directory=subject.working_directory, recursive=True
    )
    for path in ("tree", "tree/leaf.txt"):
        result = await subject.sandbox.exec(
            ["test", "-e", path], working_directory=subject.working_directory, timeout=60
        )
        if result.exit_code == 0:
            raise AssertionError(f"{path!r} survived a recursive removal of the tree")
    # Recursive scopes to the tree: a backend that cleared the working directory whole would
    # pass the loop above and delete a file nothing asked about.
    await _assert_present(
        subject.sandbox, "next-to-tree.txt", subject.working_directory, "a file beside the tree"
    )


async def _probe_a_link_inside_a_recursive_removal_is_unlinked_not_followed(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    """A link *inside* a recursively removed tree is unlinked, never resolved.

    `recursive-removes-the-tree` proves the tree goes; this proves nothing outside it went
    with it. A service-side recursive delete that resolves interior links deletes targets
    outside the working directory — the same escape as the final-component one, reachable
    only through this shape, because the only other link the suite plants is the removal
    target itself.
    """
    await subject.plant_file(f"{paths.outside}/interior-target.txt", b"outside the tree\n")
    await subject.plant_file(f"{paths.work}/linked-tree/leaf.txt", b"in the tree\n")
    await subject.plant_symlink(
        f"{paths.work}/linked-tree/inside-link", f"{paths.outside}/interior-target.txt"
    )
    await subject.sandbox.remove(
        "linked-tree", working_directory=subject.working_directory, recursive=True
    )
    tree = await subject.sandbox.exec(
        ["test", "-e", "linked-tree"], working_directory=subject.working_directory, timeout=60
    )
    if tree.exit_code == 0:
        raise AssertionError("the recursively removed tree is still there")
    target = await subject.sandbox.exec(
        ["test", "-f", "../work-outside/interior-target.txt"],
        working_directory=subject.working_directory,
        timeout=60,
    )
    if target.exit_code != 0:
        raise AssertionError(
            "the target of a link inside the recursively removed tree went with it: the "
            "removal followed an interior link, deleting a file outside the working directory"
        )


async def _probe_the_working_directory_is_refused(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.sandbox.write_file(f"{paths.work}/ground.txt", b"still standing\n")
    await _refused_with(
        ValueError,
        "removing the working directory itself",
        subject.sandbox.remove(".", working_directory=subject.working_directory),
    )
    # A backend that removed the working directory and then raised would pass the refusal
    # check above having taken the next run's ground with it.
    await _assert_present(
        subject.sandbox, "ground.txt", subject.working_directory, "a file in the refused work dir"
    )


async def _probe_a_path_outside_is_refused(
    subject: ConformanceSubject, paths: ConformancePaths
) -> None:
    await subject.plant_file(f"{paths.outside}/target.txt", b"outside\n")
    await _refused_with(
        ValueError,
        "removing a path outside the working directory",
        subject.sandbox.remove(
            "../work-outside/target.txt", working_directory=subject.working_directory
        ),
    )
    await _assert_present(
        subject.sandbox,
        "../work-outside/target.txt",
        subject.working_directory,
        "the target of a refused outside-the-work-dir removal",
    )


FILES_DELETE_PROBES: tuple[Probe, ...] = (
    Probe(
        name="a-removal-removes",
        why="the positive control: a backend that removed nothing would pass every refusal "
        "probe here.",
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_removal_removes,
    ),
    Probe(
        name="a-missing-path-is-success",
        why=(
            "cleanup runs in a finally, after whatever went wrong already went wrong — a "
            "missing path raising a second failure over the first buries the real error and "
            "breaks every finally-based cleanup."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_missing_path_is_success,
    ),
    Probe(
        name="a-link-is-removed-never-followed",
        why=(
            "a removal that resolved a link would delete a target the guest chose, from "
            "outside the working directory, and no byte has to come back for the damage to be "
            "done — unlinking outside is the escape when nothing is read."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_link_is_removed_never_followed,
    ),
    Probe(
        name="a-path-through-a-linked-parent-is-refused",
        why="the same walk the pull surface keeps: a path whose parent is a link satisfies "
        "every lexical test and still reaches outside the working directory.",
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_path_through_a_linked_parent_is_refused,
    ),
    Probe(
        name="a-directory-needs-recursive",
        why=(
            "recursive is a word the caller has to say, because the alternative is an "
            "irreversible operation that reads like a single-file delete at the call site — "
            "and empty is not carved out, since a backend without enumeration cannot tell."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_directory_needs_recursive,
    ),
    Probe(
        name="an-empty-directory-needs-recursive",
        why=(
            "the empty case stated on its own, because it is the one a backend could quietly "
            "carve out — an implicit rmdir where a refused removal belongs — and no other "
            "probe plants a directory with nothing in it."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_an_empty_directory_needs_recursive,
    ),
    Probe(
        name="recursive-removes-the-tree",
        why="the capability's other half: recursive that refused anyway would be FILES_DELETE "
        "in name only.",
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_recursive_removes_the_tree,
    ),
    Probe(
        name="a-link-inside-a-recursive-removal-is-unlinked-not-followed",
        why=(
            "the escape the tree probe cannot see: a service-side recursive delete that "
            "resolves an interior link deletes a target outside the working directory, and no "
            "byte has to come back for the damage to be done. The only link elsewhere in this "
            "suite is the removal target itself, so this shape is the one that reaches it."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_link_inside_a_recursive_removal_is_unlinked_not_followed,
    ),
    Probe(
        name="the-working-directory-is-refused",
        why=(
            "the working directory is the confinement root; a workload that removes it takes "
            "the next run's ground with it."
        ),
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_the_working_directory_is_refused,
    ),
    Probe(
        name="a-path-outside-is-refused",
        why="the boundary itself: a removal that reached outside needs no link to do its damage.",
        requires=frozenset({Capability.FILES_DELETE}),
        run=_probe_a_path_outside_is_refused,
    ),
)


async def _plant_files_delete_layout(subject: ConformanceSubject) -> ConformancePaths:
    """Derive the paths the delete probes share. Each probe plants and removes its own fixtures.

    Self-contained probes are what make :func:`measure_files_delete_probes` possible: a probe
    that planted nothing and verified a removal made elsewhere would report on state it did
    not create, and a measurement run — which may stop at the first refusal — could not run
    the probes that follow it.
    """
    return ConformancePaths.under(subject.working_directory)


async def run_files_delete_probes(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the FILES_DELETE probes. Same contract as :func:`run_files_out_probes`."""
    return await _run_suite(
        subject, Capability.FILES_DELETE, _plant_files_delete_layout, FILES_DELETE_PROBES
    )


async def assert_files_delete_conformance(
    subject: ConformanceSubject,
) -> tuple[ProbeResult, ...]:
    """Run the FILES_DELETE probes and raise :class:`ConformanceFailure` if any failed."""
    return _assert_conformance(await run_files_delete_probes(subject), "FILES_DELETE")


async def measure_files_delete_probes(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run every FILES_DELETE probe with no declaration gate and no verdict.

    Measurement, not conformance: the entry point for a backend that **implements** ``remove``
    and does not **declare** :data:`~maf_sandbox.Capability.FILES_DELETE` — where a failure is
    a finding about the backend's mechanism rather than a broken promise, and the question is
    *what the probes say*, not *whether they pass*. A declared backend has no business here;
    ``assert_files_delete_conformance`` is the contract.

    The distinction exists because a capability is how the router refuses a spec up front, and
    a capability may only be declared once its mechanism passes these probes — so someone has
    to be able to run the probes against an undeclared mechanism or the gate can never open.
    The result is the citable artefact: each probe passed, failed (with what it found), or
    raised (with what the mechanism answered).
    """
    paths = ConformancePaths.under(subject.working_directory)
    results: list[ProbeResult] = []
    for probe in FILES_DELETE_PROBES:
        try:
            await probe.run(subject, paths)
        except AssertionError as failed:
            results.append(ProbeResult(probe=probe, failure=str(failed)))
        except Exception as raised:
            results.append(
                ProbeResult(probe=probe, failure=f"raised {type(raised).__name__}: {raised}")
            )
        else:
            results.append(ProbeResult(probe=probe))
    return tuple(results)


# ---------------------------------------------------------------------------
# the runner all four suites share
# ---------------------------------------------------------------------------


async def _run_suite(
    subject: ConformanceSubject,
    gate: Capability,
    plant: Callable[[ConformanceSubject], Awaitable[ConformancePaths]],
    probes: tuple[Probe, ...],
) -> tuple[ProbeResult, ...]:
    declared = subject.capabilities
    if gate not in declared:
        raise ValueError(
            f"this subject declares no {str(gate).upper()}, so every probe would be skipped "
            "and the run would report success having attacked nothing. Pass the backend's own "
            "`capabilities` — the frozenset the router reads — rather than a narrower set."
        )
    paths = await plant(subject)
    results: list[ProbeResult] = []
    for probe in probes:
        missing = probe.requires - declared
        if missing:
            names = ", ".join(sorted(str(capability) for capability in missing))
            results.append(ProbeResult(probe=probe, skipped=f"backend does not declare {names}"))
            continue
        try:
            await probe.run(subject, paths)
        except AssertionError as failed:
            results.append(ProbeResult(probe=probe, failure=str(failed)))
        except Exception as raised:
            results.append(
                ProbeResult(probe=probe, failure=f"raised {type(raised).__name__}: {raised}")
            )
        else:
            results.append(ProbeResult(probe=probe))
    return tuple(results)


def _assert_conformance(results: tuple[ProbeResult, ...], suite: str) -> tuple[ProbeResult, ...]:
    if any(result.failure is not None for result in results):
        raise ConformanceFailure(results, suite)
    return results
