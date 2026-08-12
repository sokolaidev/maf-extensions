"""The attacks a backend serving ``FILES_OUT`` has to survive, written once for all of them.

    await assert_files_out_conformance(MySubject(sandbox, "/work"))

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

Nothing here imports a test framework: this module ships in the wheel.  A failure raises
:class:`ConformanceFailure` naming every probe that failed rather than the first.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from ._protocol import Capability, EntryKind, Sandbox

__all__ = [
    "FILES_OUT_PROBES",
    "ConformanceFailure",
    "ConformancePaths",
    "ConformanceSubject",
    "PosixGuestSubject",
    "Probe",
    "ProbeResult",
    "assert_files_out_conformance",
    "plant_layout",
    "run_files_out_probes",
]

#: What the read probes allow. Large enough that nothing here is refused for its size — every
#: refusal these probes assert is a confinement refusal, and a cap breach would mask one.
_READ_CAP = 1 << 20

_SECRET = b"the guest must not reach this\n"
_INSIDE = b"a legitimate output\n"


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

    ``outside`` is a **sibling** rather than a child of the root: ``/work-outside`` shares a
    string prefix with ``/work`` and is still outside it, so a backend comparing prefixes
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
        """A working directory one level *inside* the link — the ``/acas -> /`` case.

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
    """Raised by :func:`assert_files_out_conformance`, naming every probe that failed.

    An :class:`AssertionError` so a test framework reports it as a failed assertion rather than
    an error, without this module importing one.
    """

    def __init__(self, results: tuple[ProbeResult, ...]) -> None:
        self.results = results
        self.failures = tuple(r for r in results if r.failure is not None)
        lines = [f"{len(self.failures)} of {len(results)} FILES_OUT conformance probes failed:"]
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
            "stats straight through them. This is the /acas -> / case, and it is the one the "
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
    """
    paths = await plant_layout(subject)
    declared = subject.capabilities
    results: list[ProbeResult] = []
    for probe in FILES_OUT_PROBES:
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


async def assert_files_out_conformance(subject: ConformanceSubject) -> tuple[ProbeResult, ...]:
    """Run the probes and raise :class:`ConformanceFailure` if any failed.

    Returns the results on success so a caller can assert on what was *skipped*: a backend that
    silently stopped declaring ``FILES_LIST`` would otherwise go green on three fewer probes.
    """
    results = await run_files_out_probes(subject)
    if any(result.failure is not None for result in results):
        raise ConformanceFailure(results)
    return results
