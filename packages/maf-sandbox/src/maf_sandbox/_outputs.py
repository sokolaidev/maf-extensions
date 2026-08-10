"""Landing an artifact from a sandbox in host state — the sink half of ``FILES_OUT``.

This package never writes anything itself: a workspace store, a blob container, a UI panel or
a scratch directory is a property of the application, so the host supplies a callback and this
module decides only *what* reaches it and *when*.  Two of those decisions are load-bearing:

- **Nothing is delivered until the whole collection is in hand.**  A push callback cannot be
  un-called, so a cap breach or an absent required output is refusable as a whole only while
  nothing has been delivered yet.  ``max_total_bytes`` is therefore the peak host memory one
  collection costs, and there is no streaming to the sink.
- **Both the name and the bytes are the guest's.**  This is the first channel where either
  reaches host state, so a landing name is held to a narrow invariant and case-only collisions
  within one collection are refused before the host sees either half.  What is *legal* at the
  destination stays the host's own rule — :func:`portable_name` helps with the Windows part of
  it, and is never applied for you.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

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
    "SandboxOutputError",
    "SandboxOutputMissing",
    "SandboxOutputNotRegular",
    "SandboxOutputSinkRequired",
    "SandboxOutputSizeUnknown",
    "SandboxTransferCapExceeded",
    "collect_outputs",
    "portable_name",
    "validate_artifact_name",
]


#: The protocol's one path grammar, POSIX-shaped whatever the guest and the host each run.
_SEPARATOR = "/"
_TRAVERSAL = ".."
_BACKSLASH = "\\"

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
_TRAILING_CHARACTERS = ". "
_REPLACEMENT = "_"


class SandboxOutputError(RuntimeError):
    """A declared output could not be collected. Base of the whole refusal family.

    A kind that only needs to tell the model "the artifacts did not come back" catches this;
    the members below are for a caller that wants to say which of the six things went wrong.
    """


class SandboxOutputMissing(SandboxOutputError):
    """A declared output marked ``required`` was not there when the run finished."""


class SandboxOutputNotRegular(SandboxOutputError):
    """A declared output is a directory, a symlink, or anything else that is never read."""


class SandboxOutputSizeUnknown(SandboxOutputError):
    """A landing output's size could not be determined, so no cap could be applied to it."""


class SandboxTransferCapExceeded(SandboxOutputError):
    """A collection asks to move more than the workload's own ``files_out`` caps allow."""


class SandboxOutputSinkRequired(SandboxOutputError):
    """A spec declares an output that lands, and no sink was supplied to land it in."""


class SandboxArtifactNameInvalid(SandboxOutputError):
    """A landing name breaks the narrow invariant every name crossing this boundary must meet."""


class SandboxArtifactNameCollision(SandboxOutputError):
    """Two landing names in one collection differ only by case or by Unicode form."""


@dataclass(frozen=True)
class Artifact:
    """One file pulled out of a sandbox, on its way to the host.

    ``name`` is validated and relative, derived from the declared output's path and spelled as
    the sink asked.  ``kind`` is the spec's, so a host can route by workload without being told
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


def _nfc(name: str) -> str:
    """``name`` in NFC — the one Unicode form all three filesystems recognise alike."""
    return unicodedata.normalize("NFC", name)


def validate_artifact_name(name: str) -> None:
    """Refuse ``name`` unless it meets the narrow invariant, naming the rule that refused it.

    Relative, no traversal segment, no backslash, at most :data:`MAX_ARTIFACT_NAME_BYTES` bytes
    of valid UTF-8 — and deliberately nothing further.  What is *legal* at the destination
    differs between a blob container, NTFS and a workspace store, so a library that guessed
    would be wrong for two of the three; hosts still own their own namespace rules.

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
    if _TRAVERSAL in name.split(_SEPARATOR):
        raise SandboxArtifactNameInvalid(
            f"artifact name {name!r} contains a {_TRAVERSAL!r} traversal segment"
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
    or without an extension), the ``< > : " | ? *`` set, and trailing dots and spaces.  A
    helper rather than a rule, because a library that rewrote every name for the strictest
    destination would be wrong for the other two — and because a rewritten name is no longer
    the name the workload said it produced.
    """
    return _SEPARATOR.join(_portable_segment(segment) for segment in name.split(_SEPARATOR))


def _portable_segment(segment: str) -> str:
    """One path segment made portable; an empty result becomes the replacement character."""
    cleaned = "".join(
        _REPLACEMENT if character in _FORBIDDEN_CHARACTERS else character for character in segment
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


def _check_landing_names(landing: tuple[DeclaredOutput, ...]) -> None:
    """Settle what the spec alone decides, before the sandbox is touched at all.

    Both rules are properties of the declaration rather than of the run, so a kind whose
    outputs could never land is refused the same way whatever the guest happened to produce.
    """
    seen: dict[str, str] = {}
    for declared in landing:
        validate_artifact_name(declared.path)
        # NameNormalization.NONE disables the rewrite, never this comparison.
        key = _nfc(declared.path).casefold()
        if key in seen:
            raise SandboxArtifactNameCollision(
                f"declared outputs {seen[key]!r} and {declared.path!r} differ only by case or "
                "by Unicode form, which is two files on Linux and one on Windows and default "
                "macOS. Refused with the whole declaration in view, because the host receives "
                "artifacts one at a time and could never see the collision itself."
            )
        seen[key] = declared.path


async def _stat_and_cap(sandbox: Sandbox, spec: SandboxSpec) -> tuple[DeclaredOutput, ...]:
    """Stat every declared output and settle every refusal the filesystem decides.

    Reads nothing and delivers nothing; returns the landing outputs that are actually there,
    in declaration order.
    """
    tally = _Tally(limits=spec.files_out)
    present: list[DeclaredOutput] = []
    for declared in spec.declared_outputs:
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
        if entry.kind != EntryKind.FILE:
            raise SandboxOutputNotRegular(
                f"declared output {declared.path!r} is a {str(entry.kind)!r} entry, and only a "
                "regular file is ever read. A symlink is refused whether or not its target "
                "would have resolved somewhere legitimate: that judgement is made with the "
                "guest's filesystem in view and answered with whichever one the reader sees."
            )
        # A CONSUME output is the kind's own `read_file` call, so nothing below applies to it —
        # but `required` above means the same thing for both dispositions.
        if declared.disposition != OutputDisposition.LAND:
            continue
        if entry.size_bytes is None:
            raise SandboxOutputSizeUnknown(
                f"declared output {declared.path!r} has no determinable size, so no cap can be "
                "applied to it. Refused rather than read: coercing an unknown size to zero "
                "would make every cap read the one file it cannot measure as free."
            )
        tally.add(declared.path, entry.size_bytes)
        present.append(declared)
    return tuple(present)


async def _read_all(
    sandbox: Sandbox,
    spec: SandboxSpec,
    outputs: tuple[DeclaredOutput, ...],
    normalization: NameNormalization,
) -> tuple[Artifact, ...]:
    """Read every landing output into memory, re-applying the caps to the bytes that arrived.

    The second pass over the caps is not redundant: a stat is a promise about a file the guest
    is still free to rewrite, and only what was actually read bounds what reaches the host.
    """
    tally = _Tally(limits=spec.files_out)
    artifacts: list[Artifact] = []
    for declared in outputs:
        content = await sandbox.read_file(declared.path, working_directory=spec.work_dir)
        tally.add(declared.path, len(content))
        name = declared.path if normalization == NameNormalization.NONE else _nfc(declared.path)
        artifacts.append(
            Artifact(name=name, content=content, kind=spec.kind, media_type=declared.media_type)
        )
    return tuple(artifacts)


async def collect_outputs(
    sandbox: Sandbox, spec: SandboxSpec, *, sink: OutputSink | None = None
) -> tuple[LandedArtifact, ...]:
    """Pull ``spec``'s declared outputs and land the ones that land, in declaration order.

    The order of the phases is the contract rather than an implementation detail: what the
    spec alone decides — a sink for anything that lands, a valid landing name, no two landing
    names that collide — is settled before the sandbox is touched, then every declared output
    is stat-ed and capped, then every landing one is read, and only then is anything delivered.
    Delivery is a push nothing can take back, so a refusal arriving after the first ``deliver``
    could not leave the host as it found it.  The one residue is a ``deliver`` that itself
    raises part-way: whatever it already accepted stays accepted, and the exception propagates.

    A ``CONSUME`` output is stat-ed like any other, and then left alone: its bytes are the
    kind's own :meth:`~maf_sandbox.Sandbox.read_file` call and never reach the sink.

    Args:
        sandbox: The running sandbox to pull from.
        spec: The workload's spec. ``declared_outputs``, ``work_dir``, ``files_out`` and
            ``kind`` all come from it, so no caller can pair one workload's outputs with
            another's caps.
        sink: Where landing artifacts go. Required as soon as any output declares
            :data:`~maf_sandbox.OutputDisposition.LAND`, because a tool that declares something
            that lands and has nowhere to land it cannot honour its own spec.

    Raises:
        SandboxOutputSinkRequired: when an output lands and no sink was supplied.
        SandboxOutputMissing: when a ``required`` output is not there, naming it.
        SandboxOutputNotRegular: when a declared output is not a regular file.
        SandboxOutputSizeUnknown: when a landing output's size could not be determined.
        SandboxTransferCapExceeded: when the collection is over one of ``spec.files_out``'s
            three caps, naming the cap and the file that breached it.
        SandboxArtifactNameInvalid: when a landing name breaks the narrow invariant.
        SandboxArtifactNameCollision: when two landing names differ only by case or by Unicode
            form.
    """
    landing = tuple(
        declared
        for declared in spec.declared_outputs
        if declared.disposition == OutputDisposition.LAND
    )
    if landing and sink is None:
        raise SandboxOutputSinkRequired(
            f"the {spec.kind!r} workload declares "
            f"{', '.join(repr(declared.path) for declared in landing)} as landing outputs and "
            "no output sink was supplied, so the tool cannot honour its own spec"
        )
    _check_landing_names(landing)

    to_read = await _stat_and_cap(sandbox, spec)
    if not to_read:
        return ()
    assert sink is not None  # non-empty only if something lands, which was refused above

    landed: list[LandedArtifact] = []
    for artifact in await _read_all(sandbox, spec, to_read, sink.normalization):
        landed.append(await sink.deliver(artifact))
    return tuple(landed)
