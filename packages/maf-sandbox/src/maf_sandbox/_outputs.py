"""Landing an artifact from a sandbox in host state — the sink half of ``FILES_OUT``.

This package never writes anything itself: a file store, a blob container, a UI panel or
a scratch directory is a property of the application, so the host supplies a callback and this
module decides only *what* reaches it and *when*.  Two of those decisions are load-bearing:

- **Nothing is delivered until the whole collection is in hand.**  A push callback cannot be
  un-called, so a cap breach or an absent required output is refusable as a whole only while
  nothing has been delivered yet.  There is therefore no streaming to the sink, and
  ``max_total_bytes`` bounds host memory as far as the backend lets it: over-cap bytes are
  never delivered, but a backend that buffers a whole response internally has already spent
  the memory by the time this module sees a byte.
- **Both the name and the bytes are the guest's.**  This is the first channel where either
  reaches host state, so every declared name is held to a narrow invariant — checked in the
  spelling that will actually be delivered — and case-only collisions within one collection
  are refused before the host sees either half.  What is *legal* at the destination stays the
  host's own rule — :func:`portable_name` helps with the Windows part of it, and is never
  applied for you.
"""

from __future__ import annotations

import contextlib
import posixpath
import unicodedata
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ._protocol import (
    DeclaredOutput,
    EntryKind,
    OutputDisposition,
    Sandbox,
    SandboxSpec,
    TransferLimits,
)

__all__ = [
    "MAX_ARTIFACT_NAME_BYTES",
    "Artifact",
    "LandedArtifact",
    "NameNormalization",
    "OutputSink",
    "SandboxArtifactNameCollision",
    "SandboxArtifactNameInvalid",
    "SandboxLandingNotConfined",
    "SandboxOutputError",
    "SandboxOutputMissing",
    "SandboxOutputNotConfined",
    "SandboxOutputNotRegular",
    "SandboxOutputSinkRequired",
    "SandboxOutputSizeUnknown",
    "SandboxOutputUnreachable",
    "SandboxTransferCapExceeded",
    "collect_outputs",
    "landing_outputs",
    "make_file_system_sink",
    "missing_sink_refusal",
    "portable_name",
    "spec_lands_artifacts",
    "validate_artifact_name",
]


#: The protocol's one path grammar, POSIX-shaped whatever the guest and the host each run.
_SEPARATOR = "/"
_TRAVERSAL = ".."
_CURRENT_DIRECTORY = "."
_BACKSLASH = "\\"

#: Segments naming no directory of their own. ``a//b`` and ``a/./b`` are the same file as
#: ``a/b`` to every filesystem, so a declaration spelling it either way would deliver a second
#: name for one file.
_NON_NAMING_SEGMENTS: frozenset[str] = frozenset({"", _CURRENT_DIRECTORY})

#: The bound the narrow invariant enforces, counted in UTF-8 bytes rather than in characters
#: because the destinations that impose a limit count bytes.
MAX_ARTIFACT_NAME_BYTES: int = 255

#: The device names Windows reserves in every directory, exactly as its own naming rules list
#: them — nothing beyond, because a guess here mangles a legitimate name.
_RESERVED_DEVICES = ("CON", "PRN", "AUX", "NUL")
_PORT_PREFIXES = ("COM", "LPT")
_PORT_DIGITS = "123456789"
#: Windows reads the ISO/IEC 8859-1 superscript digits as digits, so ``COM¹`` is as reserved as
#: ``COM1`` is: ``echo test > COM¹`` fails to create a file.  ⁴ and up are not.
_SUPERSCRIPT_PORT_DIGITS = "¹²³"
_RESERVED_STEMS: frozenset[str] = frozenset(
    set(_RESERVED_DEVICES)
    | {
        f"{prefix}{digit}"
        for prefix in _PORT_PREFIXES
        for digit in _PORT_DIGITS + _SUPERSCRIPT_PORT_DIGITS
    }
)
_FORBIDDEN_CHARACTERS = '<>:"|?*'
#: ASCII 0-31. Microsoft's naming rules list them in the same breath as the punctuation above,
#: and no filesystem anywhere accepts one — which is why these are refused by the narrow
#: invariant as well as rewritten by the opt-in helper, where the punctuation is only rewritten.
_CONTROL_CHARACTERS: frozenset[str] = frozenset(map(chr, range(32)))
_UNPORTABLE_CHARACTERS: frozenset[str] = _CONTROL_CHARACTERS | frozenset(_FORBIDDEN_CHARACTERS)
_TRAILING_CHARACTERS = ". "
_REPLACEMENT = "_"


class SandboxOutputError(RuntimeError):
    """A declared output could not be collected. Base of the whole refusal family.

    A kind that only needs to tell the model "the artifacts did not come back" catches this;
    the members below are for a caller that wants to name what went wrong.  Every refusal
    :func:`collect_outputs` can raise is one of them, including the ones a backend raises in
    its own vocabulary — those are translated on the way out, so the family is exhaustive
    rather than merely typical.
    """


class SandboxOutputMissing(SandboxOutputError):
    """A declared output marked ``required`` was not there when the run finished."""


class SandboxOutputNotConfined(SandboxOutputError):
    """A declared output's path resolved outside the sandbox's working directory."""


class SandboxOutputNotRegular(SandboxOutputError):
    """A declared output is a directory, a symlink, or anything else that is never read."""


class SandboxOutputSizeUnknown(SandboxOutputError):
    """A declared output's size could not be determined, so no cap could be applied to it."""


class SandboxOutputUnreachable(SandboxOutputError):
    """A declared output, or the sandbox holding it, went away while it was being collected."""


class SandboxTransferCapExceeded(SandboxOutputError):
    """A collection asks to move more than the workload's own ``files_out`` caps allow."""


class SandboxLandingNotConfined(SandboxOutputError):
    """A sink refused a destination that resolved outside the root it lands under.

    The host-side counterpart to :class:`SandboxOutputNotConfined`, and a separate name
    because the remediation is not the same: that one says the *guest* named a path outside
    its working directory, this one says the host's own landing directory has something in it
    — a symlink, most likely — carrying a write elsewhere.
    """


class SandboxOutputSinkRequired(SandboxOutputError):
    """A spec declares an output that lands, and no sink was supplied to land it in."""


class SandboxArtifactNameInvalid(SandboxOutputError):
    """A landing name breaks the narrow invariant every name crossing this boundary must meet."""


class SandboxArtifactNameCollision(SandboxOutputError):
    """Two landing names in one collection are one file at the destination.

    Either identical — two guest paths given the same ``name`` — or differing only by case or
    by Unicode form, which is one file on Windows and default macOS and two on Linux.
    """


@dataclass(frozen=True)
class Artifact:
    """One file pulled out of a sandbox, on its way to the host.

    ``name`` is validated and relative, and spelled as the sink asked: it is the declared
    output's ``name`` when the kind gave one, and its ``path`` otherwise — the two come apart
    for a kind writing into a per-call directory, whose guest path carries a run id the host
    has no use for.  ``kind`` is the spec's, so a host can route by workload without being told
    twice.  ``media_type`` is whatever the kind declared, never sniffed: sniffing would let
    guest-produced content decide how the host handles it.
    """

    name: str
    content: bytes
    kind: str
    media_type: str | None


@dataclass(frozen=True)
class LandedArtifact:
    """Where the host put one artifact, and what the model may be told about it.

    The split between ``display`` and ``handle`` is a security property rather than tidiness.
    A host that returned one string, and a kind that put it in the tool result, could between
    them persist a SAS URL with a bearer token in its query string into the transcript, to be
    replayed on every subsequent turn.  ``display`` is what the model sees; ``handle`` is the
    host's own reference, and nothing in this library renders it anywhere.
    """

    name: str
    display: str
    handle: str | None = None


class NameNormalization(StrEnum):
    """What a sink wants done to a name before it sees it."""

    #: Compose to NFC, the one form that survives all three filesystems recognisably.
    NFC = "nfc"
    #: Byte-exact, for a content-addressed store or a Linux-only deployment. It disables
    #: **only** the rewrite: the narrow invariant still applies, and collision detection still
    #: compares normalized forms.
    NONE = "none"


@dataclass(frozen=True)
class OutputSink:
    """Where a host lands artifacts, and how it wants their names spelled.

    ``deliver`` refuses by raising.  Returning ``None`` would be a silent drop, and a "refused"
    flag on :class:`LandedArtifact` would be a check every consumer has to remember to make,
    and one will not.

    There is deliberately **no confidentiality cap here**.  That value is an opaque
    host-vocabulary string with no ordering, so nothing in a library can rank two of them:
    there is one value from one source — the host's outbound cap, supplied once where the
    workload's tool is built — and a second one on the sink would exist only to be folded.
    """

    deliver: Callable[[Artifact], Awaitable[LandedArtifact]]
    normalization: NameNormalization = NameNormalization.NFC


def _landed_display(artifact: Artifact, _destination: Path) -> str:
    """The default line a model sees for a landed artifact: its name and its size."""
    return f"{artifact.name} ({len(artifact.content)} bytes)"


def make_file_system_sink(
    root: Path, *, display: Callable[[Artifact, Path], str] = _landed_display
) -> OutputSink:
    """An :class:`OutputSink` that writes each artifact under ``root``, refusing any that would
    land outside it.

    The refusal is why this exists; the writing is three lines.
    :func:`validate_artifact_name` is **lexical** — it bounds the name and says nothing about
    what is already sitting at the path that name resolves to — so a symlink in ``root`` carries
    a write straight out of it, the host-side twin of the symlinked ancestor a guest path is
    checked for.  Every host that lands to a filesystem has to make this check.

    It stays a check rather than a guarantee: resolving and writing are two calls, so a
    destination replaced in between is followed, and a host landing genuinely hostile output
    wants no-follow primitives underneath.  What this closes is the standing case — something
    already in the way when the run started.

    ``display`` is the one line the model is allowed to see, and a kind that introduces its
    artifacts in its own words supplies its own; ``handle`` is always the host path, which
    nothing renders into the transcript.

    Raises:
        SandboxLandingNotConfined: when a destination resolves outside ``root``.
    """
    confined_root = root.resolve()

    async def deliver(artifact: Artifact) -> LandedArtifact:
        destination = (confined_root / artifact.name).resolve()
        if not destination.is_relative_to(confined_root):
            # Neither path is named. This family is the one a kind may show the model
            # verbatim — `maf-sandbox-codeact` interpolates it straight into the tool result
            # — and every other member says only `name!r`, which the guest supplied and
            # already knows. A host path here would reach the transcript, which is the leak
            # `LandedArtifact.handle` exists to prevent. A host wanting the resolution has
            # `root` and the name.
            raise SandboxLandingNotConfined(
                f"artifact {artifact.name!r} resolves outside the directory it lands in. "
                "Something on that path leaves it — a link, most likely — and writing "
                "would follow it."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(artifact.content)
        return LandedArtifact(
            name=artifact.name,
            display=display(artifact, destination),
            handle=str(destination),
        )

    return OutputSink(deliver)


def _nfc(name: str) -> str:
    """``name`` in NFC — the one Unicode form all three filesystems recognise alike."""
    return unicodedata.normalize("NFC", name)


def validate_artifact_name(name: str) -> None:
    """Refuse ``name`` unless it meets the narrow invariant, naming the rule that refused it.

    Relative, no traversal segment, no backslash, no segment that names nothing (``a//b``,
    ``a/./b``), no control character, at most :data:`MAX_ARTIFACT_NAME_BYTES` bytes of valid
    UTF-8 — and deliberately nothing further.  What is *legal* at the destination differs
    between a blob container, NTFS and a file store, so a library that guessed would be
    wrong for two of the three; hosts still own their own namespace rules.

    Call it on the spelling that will actually be **delivered**: NFC is not
    length-non-increasing, so a name checked before normalization is not the name the host
    receives.

    Raises:
        SandboxArtifactNameInvalid: naming which of the rules the name broke.
    """
    if not name:
        raise SandboxArtifactNameInvalid("an artifact name must be a non-empty relative path")
    if _BACKSLASH in name:
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} contains a backslash. The protocol has one path grammar "
            "and '\\' is not a separator in it, whatever the guest or the host runs."
        )
    if name.startswith(_SEPARATOR):
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} is absolute, and a landing name is relative: where it "
            "lands is the host's to decide, not the guest's"
        )
    segments = name.split(_SEPARATOR)
    if _TRAVERSAL in segments:
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} contains a {_TRAVERSAL!r} traversal segment"
        )
    if not _NON_NAMING_SEGMENTS.isdisjoint(segments):
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} contains an empty or {_CURRENT_DIRECTORY!r} path segment, "
            "which names no directory of its own. It is the same file as the spelling without "
            "it, and two spellings of one file would land as two artifacts."
        )
    control = sorted(_CONTROL_CHARACTERS.intersection(name))
    if control:
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} contains a control character (ASCII "
            f"{', '.join(str(ord(character)) for character in control)}). No filesystem "
            "accepts one, so this is a narrow-invariant rule rather than a guess about the "
            "destination's own namespace."
        )
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} is not valid UTF-8, which is the interchange form for "
            "every name crossing this boundary"
        ) from exc
    if len(encoded) > MAX_ARTIFACT_NAME_BYTES:
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} is {len(encoded)} bytes of UTF-8, over the "
            f"{MAX_ARTIFACT_NAME_BYTES}-byte ceiling"
        )


def portable_name(name: str) -> str:
    """Rewrite ``name`` into one Windows will accept — opt-in, and never applied for you.

    Per path segment: Windows's reserved device names (``CON``, ``PRN``, ``AUX``, ``NUL``, and
    ``COM`` or ``LPT`` followed by ``1``-``9`` or by a superscript ``¹``, ``²`` or ``³``, with
    or without an extension), the ``< > : " | ? *`` set, ASCII 0-31, and trailing dots and
    spaces.  A helper rather than a rule, because a library that rewrote every name for the
    strictest destination would be wrong for the other two — and because a rewritten name is
    no longer the name the workload said it produced.
    """
    return _SEPARATOR.join(_portable_segment(segment) for segment in name.split(_SEPARATOR))


def _portable_segment(segment: str) -> str:
    """One path segment made portable; an empty result becomes the replacement character."""
    cleaned = "".join(
        _REPLACEMENT if character in _UNPORTABLE_CHARACTERS else character for character in segment
    ).rstrip(_TRAILING_CHARACTERS)
    stem, dot, extension = cleaned.partition(".")
    if stem.upper() in _RESERVED_STEMS:
        stem += _REPLACEMENT
    return (stem + dot + extension) or _REPLACEMENT


@dataclass
class _Tally:
    """The three caps, applied to one collection as it is measured."""

    limits: TransferLimits
    files: int = 0
    total_bytes: int = 0

    @property
    def remaining_bytes(self) -> int:
        """What is left of ``max_total_bytes`` — the budget everything still to come shares."""
        return max(self.limits.max_total_bytes - self.total_bytes, 0)

    def add(self, path: str, size_bytes: int) -> None:
        """Count one file, refusing the whole collection when it puts any cap over."""
        if size_bytes > self.limits.max_bytes_per_file:
            raise SandboxTransferCapExceeded(
                f"declared output {path!r} is {size_bytes} bytes, over this workload's "
                f"max_bytes_per_file of {self.limits.max_bytes_per_file}. The whole collection "
                "is refused and nothing is delivered: a partial artifact set reported as "
                "success is worse than none, because the model cannot tell what it did not get."
            )
        self.files += 1
        if self.files > self.limits.max_files:
            raise SandboxTransferCapExceeded(
                f"declared output {path!r} is number {self.files} of a collection this workload "
                f"capped at max_files={self.limits.max_files}"
            )
        self.total_bytes += size_bytes
        if self.total_bytes > self.limits.max_total_bytes:
            raise SandboxTransferCapExceeded(
                f"declared output {path!r} brings the collection to {self.total_bytes} bytes, "
                f"over this workload's max_total_bytes of {self.limits.max_total_bytes}"
            )


def landing_outputs(spec: SandboxSpec) -> tuple[DeclaredOutput, ...]:
    """``spec``'s declared outputs that reach a host sink — the subset that needs one.

    Written once and read from both sides of the boundary: :func:`collect_outputs` needs it to
    know what to deliver, and ``sandboxed_tool`` needs the same answer at attach time to refuse
    a spec that lands something with nowhere to land it.

    It answers for ``spec.declared_outputs`` only, so a spec setting
    ``outputs_named_at_call_time`` can land something and still be answered ``()`` here.  Every
    caller therefore reads :func:`spec_lands_artifacts` instead when the question is whether a
    sink is needed at all.
    """
    return _landing(spec.declared_outputs)


def _landing(outputs: tuple[DeclaredOutput, ...]) -> tuple[DeclaredOutput, ...]:
    """The subset of ``outputs`` that reaches a sink."""
    return tuple(declared for declared in outputs if declared.disposition is OutputDisposition.LAND)


def spec_lands_artifacts(spec: SandboxSpec) -> bool:
    """Whether ``spec`` sends anything to a host sink — named in advance or at call time."""
    return bool(landing_outputs(spec)) or spec.outputs_named_at_call_time


def missing_sink_refusal(
    spec: SandboxSpec, landing: tuple[DeclaredOutput, ...], *, asked_by: str
) -> SandboxOutputSinkRequired:
    """The refusal for a spec that lands something when no sink was supplied to land it in.

    Returned rather than raised so each caller keeps its own control flow visible; the wording
    lives here so the two sites cannot drift into telling a host two different stories.  An
    empty ``landing`` is the ``outputs_named_at_call_time`` case, where the spec says it lands
    something and cannot yet say what.
    """
    declares = (
        f"declares {', '.join(repr(declared.path) for declared in landing)} as landing outputs"
        if landing
        else "lands artifacts it names at call time"
    )
    return SandboxOutputSinkRequired(
        f"the {spec.kind!r} workload {declares} and {asked_by} was given no output sink, so "
        "the tool cannot honour its own spec"
    )


def _delivered_name(declared: DeclaredOutput, sink: OutputSink | None) -> str:
    """The exact spelling the host will be handed — what the invariant has to judge.

    ``name`` when the kind gave one, else the guest path, since for most kinds they are the
    same string.  ``NameNormalization.NONE`` disables the rewrite and nothing else; with no
    sink at all nothing is delivered, and the declaration is the only spelling there is.
    """
    name = declared.name if declared.name is not None else declared.path
    if sink is None or sink.normalization is NameNormalization.NONE:
        return name
    return _nfc(name)


def _collision_key(path: str) -> str:
    """The one file two declared outputs must not both name, however each is spelled.

    ``str.lower`` rather than ``str.casefold``: casefolding maps ``ß`` to ``ss`` and ``ﬁ`` to
    ``fi``, which are distinct files on Linux, NTFS and case-insensitive APFS alike, so folding
    them together would fail a whole collection that no destination has a problem with.
    ``normpath`` answers the other half of the same question — and keying on it means the
    answer does not depend on :func:`validate_artifact_name` continuing to refuse the
    spellings it collapses.  ``NameNormalization.NONE`` disables the rewrite, never this.
    """
    return posixpath.normpath(_nfc(path).lower())


def _check_declared_names(outputs: tuple[DeclaredOutput, ...], sink: OutputSink | None) -> None:
    """Settle what the declaration alone decides, before the sandbox is touched at all.

    Every declared **path** meets the narrow invariant whatever its disposition — a ``CONSUME``
    path is still a path this library hands to a backend, and one that traverses would come
    back as that backend's own exception rather than as a refusal a kind can catch.  A landing
    output's delivered **name** is checked as well, in the spelling the sink will receive,
    because the two are no longer always the same string.  Only landing names can collide,
    because only they are delivered anywhere.

    Both rules are properties of the declaration rather than of the run, so a kind whose
    outputs could never land is refused the same way whatever the guest happened to produce.
    """
    seen: dict[str, tuple[DeclaredOutput, str]] = {}
    for declared in outputs:
        validate_artifact_name(declared.path)
        if declared.disposition is not OutputDisposition.LAND:
            continue
        delivered = _delivered_name(declared, sink)
        if delivered != declared.path:
            validate_artifact_name(delivered)
        key = _collision_key(delivered)
        if key in seen:
            raise _collision(seen[key], (declared, delivered))
        seen[key] = (declared, delivered)


def _collision(
    first: tuple[DeclaredOutput, str], second: tuple[DeclaredOutput, str]
) -> SandboxArtifactNameCollision:
    """Two declarations landing as one file, saying which of the two ways it happened.

    Since a landing name can be given apart from the guest path, the two spellings are no
    longer necessarily *variants* of each other: two paths may ask for the identical name.
    Reporting that as "differing only by case" would describe a difference the reader can see
    is not there.
    """
    (earlier, earlier_name), (later, later_name) = first, second
    how = (
        f"both land as {earlier_name!r}"
        if earlier_name == later_name
        else (
            f"land as {earlier_name!r} and {later_name!r}, which are one file on Windows and "
            "default macOS and two on Linux, differing only by case or by Unicode form"
        )
    )
    return SandboxArtifactNameCollision(
        f"declared outputs {earlier.path!r} and {later.path!r} {how}. Refused with the whole "
        "declaration in view, because the host receives artifacts one at a time and could "
        "never see the collision."
    )


@contextlib.contextmanager
def _backend_refusals(path: str) -> Generator[None, None, None]:
    """Translate what a backend raises out of the pull surface into this module's family.

    A backend answers in its own vocabulary — a bare ``ValueError`` for a path that resolved
    outside the working directory, a bare ``FileNotFoundError`` for a file the guest deleted
    between the stat and the read — and a kind told to catch :class:`SandboxOutputError` would
    never see either.
    """
    try:
        yield
    except ValueError as exc:
        raise SandboxOutputNotConfined(
            f"the backend refused declared output {path!r}: it does not resolve to a path "
            "inside the sandbox's working directory. Reads are confined there because the "
            "alternative is answering with whichever filesystem the reader can see."
        ) from exc
    except OSError as exc:
        raise SandboxOutputUnreachable(
            f"declared output {path!r} could not be reached: the file, or the sandbox holding "
            "it, is gone. A stat is a promise about a filesystem the guest is still free to "
            "change underneath the reader."
        ) from exc


async def _stat_and_cap(
    sandbox: Sandbox, spec: SandboxSpec, declared_outputs: tuple[DeclaredOutput, ...]
) -> tuple[tuple[DeclaredOutput, int], ...]:
    """Stat every declared output and settle every refusal the filesystem decides.

    Reads nothing and delivers nothing; returns the landing outputs that are actually there
    with their stat-ed sizes, in declaration order.
    """
    tally = _Tally(limits=spec.files_out)
    present: list[tuple[DeclaredOutput, int]] = []
    for declared in declared_outputs:
        with _backend_refusals(declared.path):
            entry = await sandbox.stat_file(declared.path, working_directory=spec.work_dir)
        if entry is None:
            if declared.required:
                raise SandboxOutputMissing(
                    f"the {spec.kind!r} workload declares {declared.path!r} as a required "
                    "output and it is not there. A workload for which an absence is normal — a "
                    "renderer exiting non-zero produces no file — declares required=False and "
                    "gets that absence as a diagnostic instead of a transfer error."
                )
            continue
        if entry.kind is not EntryKind.FILE:
            raise SandboxOutputNotRegular(
                f"declared output {declared.path!r} is a {str(entry.kind)!r} entry, and only a "
                "regular file is ever read. A symlink is refused whether or not its target "
                "would have resolved somewhere legitimate: that judgement is made with the "
                "guest's filesystem in view and answered with whichever one the reader sees."
            )
        if entry.size_bytes is None:
            raise SandboxOutputSizeUnknown(
                f"declared output {declared.path!r} has no determinable size, so no cap can be "
                "applied to it. Refused rather than read: coercing an unknown size to zero "
                "would make every cap read the one file it cannot measure as free."
            )
        # Counted whatever its disposition: `files_out` bounds the collection the spec
        # declared, not the subset of it that happens to land.
        tally.add(declared.path, entry.size_bytes)
        if declared.disposition is not OutputDisposition.LAND:
            continue
        present.append((declared, entry.size_bytes))
    return tuple(present)


async def _read_all(
    sandbox: Sandbox,
    spec: SandboxSpec,
    outputs: tuple[tuple[DeclaredOutput, int], ...],
    sink: OutputSink,
) -> tuple[Artifact, ...]:
    """Read every landing output into memory, re-applying the caps to the bytes that arrived.

    The second pass over the caps is not redundant: a stat is a promise about a file the guest
    is still free to rewrite, and only what was actually read bounds what reaches the host.
    Each read carries the smaller of that promise and what the collection has left, so a
    backend able to stop early does — and one whose SDK buffers the whole response internally
    cannot, which is why the count below stays rather than trusting the bound it just passed.
    """
    tally = _Tally(limits=spec.files_out)
    artifacts: list[Artifact] = []
    for declared, stat_bytes in outputs:
        with _backend_refusals(declared.path):
            content = await sandbox.read_file(
                declared.path,
                working_directory=spec.work_dir,
                max_bytes=min(stat_bytes, tally.remaining_bytes),
            )
        tally.add(declared.path, len(content))
        artifacts.append(
            Artifact(
                name=_delivered_name(declared, sink),
                content=content,
                kind=spec.kind,
                media_type=declared.media_type,
            )
        )
    return tuple(artifacts)


async def collect_outputs(
    sandbox: Sandbox,
    spec: SandboxSpec,
    *,
    sink: OutputSink | None = None,
    outputs: tuple[DeclaredOutput, ...] = (),
) -> tuple[LandedArtifact, ...]:
    """Pull the declared outputs and land the ones that land, in declaration order.

    The order of the phases is the contract rather than an implementation detail: what the
    declaration alone decides — a sink for anything that lands, a valid name for every declared
    output, no two landing names that collide — is settled before the sandbox is touched, then
    every declared output is stat-ed and capped, then every landing one is read, and only then
    is anything delivered.
    Delivery is a push nothing can take back, so a refusal arriving after the first ``deliver``
    could not leave the host as it found it.  The one residue is a ``deliver`` that itself
    raises part-way: whatever it already accepted stays accepted, and the exception propagates.

    A ``CONSUME`` output is stat-ed and **counted against every cap** like any other, and then
    left alone: ``spec.files_out`` bounds the collection the spec declared, not the subset of
    it that lands.  Its bytes are the kind's own :meth:`~maf_sandbox.Sandbox.read_file` call,
    they never reach the sink, and bounding that read is the kind's own responsibility.

    Args:
        sandbox: The running sandbox to pull from.
        spec: The workload's spec. ``work_dir``, ``files_out`` and ``kind`` all come from it,
            so no caller can pair one workload's outputs with another's caps.
        sink: Where landing artifacts go. Required as soon as any output declares
            :data:`~maf_sandbox.OutputDisposition.LAND`, because a tool that declares something
            that lands and has nowhere to land it cannot honour its own spec.
        outputs: Declarations settled at call time, collected **in addition to**
            ``spec.declared_outputs`` and counted against the same caps. This is the road for
            a workload whose artifact names are not knowable when its tool is built; it is
            refused unless the spec set ``outputs_named_at_call_time``, so a kind cannot
            quietly collect paths its attach-time declarations never admitted to.

    Raises:
        ValueError: when ``outputs`` is passed and the spec does not admit call-time names.
        SandboxOutputSinkRequired: when an output lands and no sink was supplied.
        SandboxOutputMissing: when a ``required`` output is not there, naming it.
        SandboxOutputNotConfined: when a declared path resolves outside ``spec.work_dir``.
        SandboxOutputNotRegular: when a declared output is not a regular file.
        SandboxOutputSizeUnknown: when a declared output's size could not be determined.
        SandboxOutputUnreachable: when an output, or the sandbox, went away mid-collection.
        SandboxTransferCapExceeded: when the collection is over one of ``spec.files_out``'s
            three caps, naming the cap and the file that breached it.
        SandboxArtifactNameInvalid: when a declared name breaks the narrow invariant.
        SandboxArtifactNameCollision: when two landing names are one file at the destination —
            identical, or differing only by case or by Unicode form.
    """
    if outputs and not spec.outputs_named_at_call_time:
        raise ValueError(
            f"the {spec.kind!r} workload passed {len(outputs)} call-time output(s) to "
            "collect_outputs and its spec does not set outputs_named_at_call_time. That flag "
            "is what makes the tool's attach-time declarations honest about landing anything "
            "at all — without it the tool was attached with no sink required and no outbound "
            "confidentiality cap, and collecting here would land artifacts behind both checks."
        )
    declared_outputs = spec.declared_outputs + outputs
    landing = _landing(declared_outputs)
    if landing and sink is None:
        raise missing_sink_refusal(spec, landing, asked_by=collect_outputs.__name__)
    _check_declared_names(declared_outputs, sink)

    to_read = await _stat_and_cap(sandbox, spec, declared_outputs)
    if not to_read:
        return ()
    assert sink is not None  # non-empty only if something lands, which was refused above

    landed: list[LandedArtifact] = []
    for artifact in await _read_all(sandbox, spec, to_read, sink):
        landed.append(await sink.deliver(artifact))
    return tuple(landed)
