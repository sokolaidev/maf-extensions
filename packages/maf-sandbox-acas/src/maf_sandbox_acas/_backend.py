"""The ACA Sandboxes backend: :class:`~maf_sandbox.SandboxBackend` on Azure.

Everything provider-specific lives here — the group client, disk-image resolution, the
egress policy, the lifecycle policy, the sandbox registry and label-based purge.  A workload
above the router sees ``write_file``, ``exec`` and the pull surface (``stat_file``,
``read_file``, ``list_dir``).

Isolation is :data:`~maf_sandbox.Isolation.MICROVM` — the router's default floor, so a host
that configures nothing already permits this backend.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shlex
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from maf_sandbox import (
    BackendDeclarations,
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    OsFamily,
    Sandbox,
    SandboxBackend,
    SandboxCapabilityNotSupported,
    SandboxEgressNotEnforced,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxOutputError,
    SandboxOutputSizeUnknown,
    SandboxSpec,
    SandboxTransferCapExceeded,
    ScopePurge,
    TransferLimits,
    error_detail,
    fold_disposal_failures,
)
from maf_sandbox.paths import (
    confine_resolve_guest_delete_path,
    confine_resolve_guest_list_path,
    confine_resolve_guest_path,
    confine_resolve_guest_read_path,
    confine_resolve_guest_write_path,
    ensure_guest_work_dir,
    guest_path_relative_to,
    posix_work_dir_ancestors,
)

from ._config import AcasSandboxConfig
from ._images import (
    names_a_prebuilt_image,
    qualify_image_reference,
    resolve_disk_image_id,
    resolve_prebuilt_image_name,
)
from ._probes import probe_commands

logger = logging.getLogger(__name__)

__all__ = [
    "BACKEND_NAME",
    "AcasEgressPolicyConflict",
    "AcasEntryPayloadIncomplete",
    "AcasSandboxBackend",
]

#: The name :attr:`AcasSandboxBackend.name` answers to, and the value
#: :class:`~maf_sandbox.SandboxRouter`'s ``selected=`` matches on.
#:
#: Public because a host choosing a backend from its own configuration needs the value before
#: it has a backend to read it off, and building one to learn a constant is a lot of machinery
#: for a fixed string (#411) — more here than anywhere else, since constructing this backend
#: means a subscription, a credential and a resource group. The property below returns this,
#: so the two cannot disagree.
#:
#: Import it qualified or aliased when more than one backend package is in play. Every backend
#: exports this same symbol, so two `from … import BACKEND_NAME` lines shadow each other and
#: the second wins silently. Either `import maf_sandbox_acas` and reach it as
#: `maf_sandbox_acas.BACKEND_NAME`, or alias at the import:
#: `from maf_sandbox_acas import BACKEND_NAME as ACAS_BACKEND`.
BACKEND_NAME = "acas"


class AcasEgressPolicyConflict(SandboxEgressNotEnforced):
    """This key and kind hold a sandbox created with a different egress policy."""


class AcasEntryPayloadIncomplete(SandboxOutputError):
    """The service described an entry without a field this backend needs to classify it.

    Deliberately not a ``ValueError``: ``collect_outputs`` reads that as a confinement failure,
    so a renamed wire field would reach a kind as path traversal and mask the tripwire.
    """


#: The service's limit on a label value. Exceeding it fails the whole create with
#: ``400 … Label value for key 'scope' exceeds 63 characters``.
_LABEL_VALUE_MAX = 63


def _label_value(raw: str) -> str:
    """A label value that fits the service's limit, deterministically.

    An authenticated scope is ``user-<base64url(provider:accountId)>``, which for an Entra
    id runs to 79 characters and fails the create outright.  Anonymous scopes are short
    UUIDs, so this only appears once someone signs in — which is why it looked intermittent.

    Short values pass through unchanged, because a readable label is worth having when
    looking at the group; longer ones become a digest.  Truncation is deliberately **not**
    used: two users whose scopes share a 63-character prefix would land on the same label,
    and these labels are what :meth:`dispose_scope` selects on, so a collision would let one
    conversation's purge delete another's sandboxes.  A 192-bit digest makes that
    impossible in practice where a prefix makes it merely unlikely.

    The mapping must stay identical on both sides: labels are written at create and matched
    at list.  Transform one and not the other and purge quietly selects nothing — the
    sandboxes keep running, billable, and nothing reports an error.
    """
    if len(raw) <= _LABEL_VALUE_MAX:
        return raw
    return "sha256-" + sha256(raw.encode("utf-8")).hexdigest()[:48]


def _sandbox_labels(key: SandboxKey, spec: SandboxSpec) -> dict[str, str]:
    """The labels a sandbox is created with — the same ones `dispose_scope` selects on."""
    reserved = {_LABEL_SCOPE, _LABEL_THREAD, _LABEL_AGENT, _LABEL_KIND}
    collisions = reserved.intersection(spec.labels)
    if collisions:
        raise ValueError(f"reserved sandbox labels: {', '.join(sorted(collisions))}")
    return {
        **{k: _label_value(v) for k, v in spec.labels.items()},
        _LABEL_SCOPE: _label_value(key.scope),
        _LABEL_THREAD: _label_value(key.thread_id),
        _LABEL_AGENT: _label_value(key.agent_dir),
        _LABEL_KIND: _label_value(spec.kind),
    }


# Sandbox labels.  Written at create time and read back on purge, so the *service* — not
# this process's memory — is the durable record of which sandboxes belong to a thread.
_LABEL_SCOPE = "scope"
_LABEL_THREAD = "thread"
_LABEL_AGENT = "agent"
_LABEL_KIND = "kind"

# How long to wait for a warm sandbox to come back from suspension before giving up on it
# and creating a fresh one.
#
# 120 to match the value the lifecycle documentation uses in its own example
# (`wait_for_running(timeout=120)`). It was 60, which is the wrong direction to be wrong in:
# the timeout does not fail the call, it abandons a healthy suspended sandbox and pays a
# cold create instead — slower for the user and more expensive, with nothing in the logs
# saying why. Waiting longer costs only the wait.
_RESUME_TIMEOUT_S = 120

#: What no guest that cannot write is able to back. `FILES_OUT` because a declared output is
#: created by the program that ran, `HOST_TOOLS` because the transport's launcher writes its own
#: pid, exit and session markers into a directory the file plane made.
_NEEDS_A_WRITING_GUEST = frozenset({Capability.FILES_OUT, Capability.HOST_TOOLS})

#: Deletion requires a completed compatibility check even when output collection fails open.
_NEEDS_OBSERVED_REMOVAL = frozenset({Capability.FILES_DELETE})

#: EXEC earns a warning rather than a refusal: stdout-only commands need no writing guest.
_PROBE_WHEN_REQUIRED = _NEEDS_A_WRITING_GUEST | _NEEDS_OBSERVED_REMOVAL | {Capability.EXEC}

#: The probe must not depend on a workload directory that does not exist at acquire.
_GUEST_PROBE_WORKING_DIRECTORY = "/"

#: One bound for preparation, exec and observation; cleanup has its own equal bound.
_PROBE_TIMEOUT_S = 30.0

#: Bound stale pre-create refusals without paying for a sandbox on every rejected acquire.
_REMOVAL_HINT_TTL_S = 60.0


def _image_identity(spec: SandboxSpec) -> tuple[str, str]:
    """What a spec names its image by — the key the guest's removal result is remembered under.

    Both fields, because ``image_id`` skips resolution entirely: two specs sharing an ``image``
    can still boot different artefacts.
    """
    return (spec.image_id or "", spec.image or "")


def _image_label(spec: SandboxSpec) -> str:
    """How a message names the image :func:`_image_identity` keys on.

    ``image_id`` first, and both when both are set: an id wins at create, so naming ``image``
    alone would point a refusal at an artefact that never booted.
    """
    if spec.image_id and spec.image:
        return f"{spec.image_id} (pinned over {spec.image})"
    return spec.image_id or spec.image or "the configured image"


#: So the ceilings below read as sizes rather than as eight-digit literals.
_MIB = 1024 * 1024

# This backend's own transfer ceilings, per direction. The byte ones stay well under what a
# streaming backend could offer because this one cannot stream: the SDK's `read_file` buffers
# the whole response, so a per-file ceiling bounds host memory rather than transfer cost.
# `max_files` is higher because a FILES_LIST kind fetches each file in a round trip of its own.
_FILES_LIMITS = TransferLimits(
    max_bytes_per_file=32 * _MIB, max_total_bytes=128 * _MIB, max_files=128
)
_LIMITS = SandboxLimits(files_in=_FILES_LIMITS, files_out=_FILES_LIMITS)

# What the router reads off this backend. The four fields stated here are constants — the
# sandbox group's egress policy, the data plane's own surface and the guest's shape are all
# fixed before a spec arrives.
#
# `os_families`: POSIX, and no input to this backend can change it. The service boots Linux
# microVMs, whether from its own prebuilt catalogue or from a disk image imported into the
# group, so there is nothing to ask an engine and nothing to probe in the guest. That is the
# constant side of the binding-time fork in `docs/sandbox/guest-platform-and-commands.md`, and
# it is what `exec`'s `shlex.join` quoting and this module's `posixpath` arithmetic rest on.
#
# `egress_modes`: `_egress_policy` builds a Deny-default allowlist — named hosts resolve as
# ALLOWLIST, an empty allowlist as CLOSED (deny all). Never UNRESTRICTED, because the group's
# policy denies by default and cannot be told to allow everything.
#
# `capabilities`: FILES_LIST as well as FILES_OUT, which is the split's own test — name the
# backend that lacks it. Enumeration is native here and unavailable on the backends that
# transport a named path only.
#
# HOST_TOOLS is the one member with no method behind it, so what it asserts is narrower than the
# others and worth stating: `exec` **detaches**. A process started by one call outlives it and is
# observable from the next, because the sandbox is a microVM the group keeps between calls rather
# than a session torn down per `exec` call — which is what `host_tool_calls_over_exec` is built
# on, its launcher returning at once and the exit-code file being the run's only witness. This is
# the backend where that could not be taken on faith, since every call is an HTTP round trip to a
# remote control plane, so `test_acas_e2e.py` measures it against the service rather than against
# a reading of the SDK.
#
# It is *not* a claim about the image. The shipped launcher wants `sh`, `nohup`, `printf`, `mv`,
# `mkdir`, `rm` and `kill`, and `setsid` where the image has it; a kind wants whatever interpreter
# it names — codeact wants `python3` — none of which this backend chooses, since `spec.image`
# does. That gap is #111's axis, and it is the same gap `EXEC` already has: a kind execing
# `python3` against an image without Python fails inside the sandbox today.
#
# Three capabilities are a ceiling: acquire checks removal from a file-plane directory before
# serving FILES_DELETE and conservatively refuses writing workloads on a completed failure.
# FILES_IN stays, with the residual write_file states (#951).
_DECLARATIONS = BackendDeclarations(
    capabilities=frozenset(
        {
            Capability.EXEC,
            Capability.FILES_IN,
            Capability.FILES_OUT,
            Capability.FILES_LIST,
            Capability.FILES_DELETE,
            Capability.HOST_TOOLS,
        }
    ),
    limits=_LIMITS,
    egress_modes=frozenset({Egress.ALLOWLIST, Egress.CLOSED}),
    os_families=frozenset({OsFamily.POSIX}),
)

# The data-plane routes and payload fields the pull surface reads for itself, rather than
# through the SDK's typed models — see `_AcasSandbox._files_payload`.
_STAT_ROUTE = "files/stat"
_LIST_ROUTE = "files/list"
_QUERY_PATH = "path"
_QUERY_API_VERSION = "api-version"
_FIELD_PATH = "path"
_FIELD_ENTRIES = "entries"
_FIELD_SIZE = "size"
_FIELD_IS_DIR = "isDir"
_FIELD_IS_SYMLINK = "isSymlink"


def _relative(guest: str, working_directory: str) -> str:
    """The relative half of an already-confined guest path, for a :class:`SandboxEntry`.

    Never ``None``: the bundle that produced ``guest`` has already refused anything outside, so
    the ``or ""`` narrows a type rather than covering a case.
    """
    return guest_path_relative_to(guest, working_directory) or ""


def _stat_from_payload(payload: Mapping[str, Any], relative_path: str) -> SandboxEntry:
    """One raw stat payload as a :class:`~maf_sandbox.SandboxEntry`.

    A payload missing either type flag is **refused**, never read as a regular file: those two
    booleans are the whole of this backend's symlink refusal, so a service that stops sending
    them has to break the read loudly rather than degrade confinement to nothing.  ``mode`` is
    not consulted — it carries permission bits only, with no ``S_IFLNK`` or ``S_IFDIR`` in it.
    """
    is_symlink: Any = payload.get(_FIELD_IS_SYMLINK)
    is_dir: Any = payload.get(_FIELD_IS_DIR)
    if not isinstance(is_symlink, bool) or not isinstance(is_dir, bool):
        raise AcasEntryPayloadIncomplete(
            f"the sandbox service described {relative_path!r} without both {_FIELD_IS_SYMLINK!r} "
            f"and {_FIELD_IS_DIR!r}, so what it is cannot be told. Refused rather than assumed "
            "to be a regular file: this backend's read follows a symlink to whatever it points "
            "at, so an unknown type is a read of the wrong file."
        )
    if is_symlink:
        return SandboxEntry(path=relative_path, kind=EntryKind.SYMLINK, size_bytes=None)
    if is_dir:
        return SandboxEntry(path=relative_path, kind=EntryKind.DIRECTORY, size_bytes=None)
    return SandboxEntry(path=relative_path, kind=EntryKind.FILE, size_bytes=_size_bytes(payload))


def _size_bytes(payload: Mapping[str, Any]) -> int | None:
    """A regular file's size, or ``None`` when the service reported none it can be trusted on.

    ``None`` fails closed upstream, so an absent, non-integer or negative ``size`` is passed
    through as unknown rather than taken at face value, which would make every cap read that
    one file as free — or, for a negative, as *less* than free: it clears the pre-read cap
    check and is then subtracted from the collection's running total.  Only a regular file is
    measured at all: a symlink's ``size`` is the length of the target string, not of anything
    readable.
    """
    size: Any = payload.get(_FIELD_SIZE)
    # `bool` is an `int`, and `True` would otherwise report as a one-byte file.
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size


def _listed_entries(payload: Mapping[str, Any], path: str) -> tuple[Mapping[str, Any], ...]:
    """The entries of a listing, refusing a container this backend cannot read.

    Not defaulted to empty: the service sends an explicit empty list for an empty directory, so
    an absent or renamed key means the shape changed — and defaulting would hide every output
    behind a listing that looks legitimately empty.
    """
    entries: Any = payload.get(_FIELD_ENTRIES)
    if not isinstance(entries, list):
        raise AcasEntryPayloadIncomplete(
            f"the sandbox service listed {path!r} without a {_FIELD_ENTRIES!r} list, so a "
            "changed payload cannot be told from an empty directory"
        )
    listed = cast("list[Any]", entries)
    for entry in listed:
        if not isinstance(entry, Mapping):
            raise AcasEntryPayloadIncomplete(
                f"the sandbox service listed an entry of type {type(entry).__name__}, not an "
                "object, so its type and size cannot be read"
            )
    return tuple(cast("list[Mapping[str, Any]]", listed))


def _listed_entry_path(payload: Mapping[str, Any], *, listed: str, working_directory: str) -> str:
    """Where one listed entry sits, relative to the working directory the call named.

    Confined to ``working_directory`` *and* required to be a direct child of ``listed``: the
    protocol's listing enumerates one level, so a sibling or a grandchild in the response is a
    payload this backend cannot read, not a path a caller asked to traverse — hence
    :class:`AcasEntryPayloadIncomplete` rather than the ``ValueError`` a traversal raises.
    """
    reported: Any = payload.get(_FIELD_PATH)
    if not isinstance(reported, str) or not reported:
        raise AcasEntryPayloadIncomplete(
            f"the sandbox service listed an entry with no {_FIELD_PATH!r}, so where it sits "
            "cannot be told"
        )
    # The file name check alone: this path came back *from* a listing whose own directory was
    # confined by component, so its ancestors have been classified already.
    resolved = confine_resolve_guest_path(reported, working_directory)
    relative = _relative(resolved, working_directory)
    if posixpath.dirname(resolved) != listed:
        raise AcasEntryPayloadIncomplete(
            f"the sandbox service listed {reported!r} as an entry of {listed!r}, which is not "
            "its parent, and a listing enumerates one level only"
        )
    return relative


@dataclass
class _Held:
    """A sandbox this backend is holding, and its guest removal compatibility result.

    The verdict lives on the entry so it cannot outlive the sandbox it describes.
    ``probed`` separates an inconclusive completed probe from one that must be retried.
    """

    sandbox_id: str
    removal: bool | None = None
    probed: bool = False
    commands: set[str] = field(default_factory=set[str])
    egress: tuple[Egress, frozenset[str]] = field(kw_only=True)


def _egress_key(spec: SandboxSpec) -> tuple[Egress, frozenset[str]]:
    """The supported policy's identity, independent of host spelling and order."""
    if Capability.EGRESS_METHODS in spec.required_capabilities:
        raise SandboxCapabilityNotSupported(
            "ACAS cannot enforce literal, case-sensitive egress methods; "
            "method-scoped policy is refused."
        )
    return spec.egress, frozenset(str(host).lower() for host in spec.egress_allow)


@dataclass(frozen=True)
class _RemovalHint:
    """An image result with a monotonic deadline and a generation for ordering updates."""

    removal: bool | None
    expires_at: float
    generation: int = 0


@dataclass(frozen=True)
class _Deletion:
    """What one delete did: whether a sandbox went away, and why one did not.

    Both, because a sandbox the service no longer has is neither — nothing was deleted, and
    nothing is wrong.
    """

    deleted: bool
    failure: DisposalFailure | None = None


class _AcasSandbox:
    """A running ACA sandbox, narrowed to what a workload is allowed to do with it."""

    def __init__(self, sandbox_client: Any, read_timeout: float) -> None:
        self._sc = sandbox_client
        self._read_timeout = read_timeout

    @property
    def sandbox_id(self) -> str:
        return self._sc.sandbox_id

    @property
    def instance_id(self) -> str:
        return self.sandbox_id

    async def prepare_work_dir(self, spec: SandboxSpec) -> None:
        """Establish the spec's base through the data plane."""
        await ensure_guest_work_dir(
            spec, self._unconfined_stat, self._create_directories, resolve=posix_work_dir_ancestors
        )

    async def _create_directories(self, directories: tuple[str, ...]) -> None:
        """Create each missing parent through the data plane."""
        for directory in directories:
            await self._sc.mkdir(directory)

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        """Write ``content`` at ``path`` through the data plane, which lands it as ``0:0``.

        **The residual to know before choosing this backend for a non-root image.**  The
        confinement check and this write are separate calls, and the service resolves a
        symlinked parent, so a guest that replaces a checked component in between has the write
        followed — and this plane acts as the host, so the bytes land root-owned at a path the
        guest could not have written itself.  On a root image that is a confinement failure and
        nothing more, since the guest was already root.  On a non-root one it is more than the
        guest had, which is the reach rule :meth:`~maf_sandbox.Sandbox.reclaim` states.

        Stated rather than refused, where :meth:`remove` is refused: the protocol states the
        reach rule for removals and says nothing yet about writes (#951), and withholding
        ``FILES_IN`` would leave this backend no in-door at all on such an image.  Closing it
        properly needs ownership in the data plane's stat payload, the same upstream read #710
        needs — see ``docs/sandbox/backends/acas.md``.
        """
        # `create_dirs=True` is the SDK's own default, and it is passed explicitly anyway.
        # A workload may hand us a nested path — `infra/main.bicep` is the example in the
        # bicep tool's own description — and without it every such write fails on a missing
        # parent. The file API docs do not mention the behaviour at all, so it is the SDK
        # signature that is load-bearing here; relying silently on a `0.1.0bN` default is how
        # `DiskImage.image` got missed. Stating it costs nothing and pins the intent.
        guest = await confine_resolve_guest_write_path(
            self._unconfined_stat, path, working_directory
        )
        await self._sc.write_file(guest, content, create_dirs=True)

    async def exec(
        self, command: str | Sequence[str], *, working_directory: str, timeout: float
    ) -> ExecResult:
        """Run ``command``, bounded by ``timeout``.

        The bound is applied here rather than left to the SDK: a sandbox that stops
        answering would otherwise hold the caller's turn open indefinitely.  ``TimeoutError``
        propagates so the workload can report it as a diagnostic rather than as a hang.

        The SDK's own ``exec`` takes a string only, so a sequence is quoted into one with
        :func:`shlex.join` first — POSIX quoting, which is the guest shape this backend
        declares in ``declarations.os_families`` and the router matches a spec against.
        """
        cmd = command if isinstance(command, str) else shlex.join(command)
        result = await asyncio.wait_for(
            self._sc.exec(cmd, working_directory=working_directory), timeout=timeout
        )
        return ExecResult(
            stdout=getattr(result, "stdout", "") or "",
            stderr=getattr(result, "stderr", "") or "",
            exit_code=getattr(result, "exit_code", 0) or 0,
        )

    async def probe_guest_removal(self) -> bool | None:
        """Check guest removal compatibility; this cannot establish workload authority."""
        from azure.core.exceptions import ResourceNotFoundError

        guest_directory = f"/.maf-authority-{uuid4().hex}"
        guest_file = f"{guest_directory}/probe"
        removal: bool | None = None
        cleaned = True
        try:
            async with asyncio.timeout(_PROBE_TIMEOUT_S):
                await self._sc.write_file(guest_file, b"probe", create_dirs=True)
                planted = await self.stat_file(guest_file, working_directory="/")
                if planted is None or planted.kind is not EntryKind.FILE:
                    raise OSError("the file plane did not create the removal probe file")
                answered = await self.exec(
                    ["rm", "--", guest_file],
                    working_directory=_GUEST_PROBE_WORKING_DIRECTORY,
                    timeout=_PROBE_TIMEOUT_S,
                )
                parent = await self.stat_file(guest_directory, working_directory="/")
                remaining = await self.stat_file(guest_file, working_directory="/")
                if parent is not None and parent.kind is EntryKind.DIRECTORY:
                    if remaining is None and answered.exit_code == 0:
                        removal = True
                    elif remaining is not None and remaining.kind is EntryKind.FILE:
                        if answered.exit_code == 1:
                            removal = False
        finally:
            try:
                async with asyncio.timeout(_PROBE_TIMEOUT_S):
                    # The random directory is a direct child of /; cleanup never follows
                    # a guest-controlled intermediate component or a final symlink.
                    await self._sc.delete_file(guest_directory, recursive=True)
            except ResourceNotFoundError:
                # An absent scratch directory already satisfies cleanup.
                pass
            except Exception as cleanup_failed:  # noqa: BLE001 - keep the probe failure
                logger.warning(
                    "acas: could not clean removal probe %s in sandbox %s: %s",
                    guest_directory,
                    self.sandbox_id,
                    error_detail(cleanup_failed),
                )
                cleaned = False
        if not cleaned:
            raise OSError("the removal probe could not be cleaned up")
        return removal

    # -- the pull surface ---------------------------------------------------------

    async def _files_payload(self, route: str, guest_path: str) -> Mapping[str, Any]:
        """One ``files/`` data-plane GET, as the **raw** payload the service sent.

        The only place that reaches past the SDK's typed ``FileInfo``, which cannot express an
        entry's type at all: no ``isSymlink``, and an ``isDirectory`` the service never sends.
        Removal gate — this helper and the field constants go when the typed surface carries the
        type: `#136 <https://github.com/sokolaidev/maf-extensions/issues/136>`_.
        """
        sc = self._sc
        payload: Mapping[str, Any] = await sc._dp_get(
            f"{sc._sbx_path}/{route}",
            params={_QUERY_PATH: guest_path, _QUERY_API_VERSION: sc._api_version},
        )
        return payload

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Describe ``path``, or return ``None`` when nothing is there.

        Stat is ``lstat``-like: a symlink is described as itself, never as its target.  Its
        *ancestors* are checked first, exactly as a read checks them: no byte of ``/etc`` crosses
        when ``out -> /etc`` is statted through, but its type and size do, and that is
        metadata from outside the boundary.

        The **final** component is described rather than refused: a link reported as
        :data:`~maf_sandbox.EntryKind.SYMLINK` is how a caller learns it is one.
        """
        guest = await confine_resolve_guest_read_path(
            self._unconfined_stat, path, working_directory
        )
        return await self._stat_guest(guest, _relative(guest, working_directory))

    async def run_code(self, code: str, *, timeout: float) -> ExecResult:
        """Not supported: this backend declares no :data:`~maf_sandbox.Capability.RUN_CODE`.

        Not for want of an interpreter — the image may well carry one — but because *which*
        runtime an image carries is a property of the image, and this backend is handed image
        references it does not parse. Declaring the capability would be a claim about someone
        else's artefact. A workload that wants a runtime by name invokes it through
        :meth:`exec` and owns that assumption itself.
        """
        raise NotImplementedError(
            "the acas backend does not support RUN_CODE: evaluating code without a shell "
            "means knowing which runtime the guest carries, and this backend resolves an "
            "image reference without looking inside it. Run the interpreter through exec, or "
            "register a backend that declares RUN_CODE."
        )

    async def remove(self, path: str, *, working_directory: str, recursive: bool = False) -> None:
        """Remove as the guest and verify absence through the file plane.

        The image controls its commands, so a successful probe cannot authorize a host-plane
        delete. Parent checks are not held; a redirected command still runs as the guest.
        """
        async with asyncio.timeout(self._read_timeout):
            try:
                guest = await confine_resolve_guest_delete_path(
                    self._unconfined_stat, path, working_directory
                )
                planted = await self._stat_guest(guest, posixpath.normpath(path))
                if planted is None:
                    return
                if planted.kind is EntryKind.DIRECTORY and not recursive:
                    raise OSError(f"refusing to remove a directory without recursive: {path}")
                answered = await self.exec(
                    ["rm", "-rf" if recursive else "-f", "--", guest],
                    working_directory="/",
                    timeout=self._read_timeout,
                )
                if answered.exit_code != 0:
                    raise OSError(
                        f"could not remove {path}: guest rm exited {answered.exit_code}"
                        f"{f' — {answered.stderr.strip()}' if answered.stderr else ''}"
                    )
                if await self.stat_file(guest, working_directory=working_directory) is not None:
                    raise OSError(
                        f"could not remove {path}: the file plane still reports the entry"
                    )
            except (OSError, ValueError):
                raise
            except Exception as refused:
                raise OSError(f"could not remove {path}: {type(refused).__name__}") from refused

    async def reset(self, *, timeout: float) -> None:
        """Unsupported: this backend does not declare Capability.SNAPSHOT."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support reset; dispose its sandbox instead."
        )

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Unsupported: safe ancestry for a host-authority delete cannot be established."""
        raise NotImplementedError(
            "the acas backend does not support RECLAIM: the data plane resolves linked "
            "parents but cannot establish that the guest could not replace them. "
            "Dispose the sandbox instead."
        )

    async def _stat_guest(self, guest: str, relative: str) -> SandboxEntry | None:
        """Stat an absolute guest path, with no confinement check of its own.

        Split out because the filesystem path check stats the working directory's own
        ancestors, which by definition sit outside it — confining here would refuse the
        very check being made.
        """
        from azure.core.exceptions import ResourceNotFoundError

        try:
            payload = await self._files_payload(_STAT_ROUTE, guest)
        except ResourceNotFoundError:
            return None
        return _stat_from_payload(payload, relative)

    async def _unconfined_stat(self, directory: str) -> SandboxEntry | None:
        """The unconfined, no-follow stat used by the confinement bundles."""
        return await self._stat_guest(directory, directory)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Read the regular file at ``path``, refusing anything over ``max_bytes``.

        Stat-before-read is the confinement rule rather than an optimisation: **this backend's
        read follows symlinks**, in the parents as much as in the final component, so every one
        of them is classified before a byte moves.

        ``max_bytes`` is a refusal, never a truncation, and it is checked again against what
        arrived, because the SDK buffers the whole response rather than exposing an incremental
        hook and a stat is only a promise about a file the guest may still rewrite.

        That promise is also the residual this cannot close: a guest that swaps the stat-ed file
        for a symlink between the two calls wins, since the service follows it and this API has
        no no-follow read.  An atomic no-follow read, or a frozen guest filesystem, would close
        it; nothing available here does.
        """
        from azure.core.exceptions import ResourceNotFoundError

        guest = await confine_resolve_guest_read_path(
            self._unconfined_stat, path, working_directory
        )
        # `_stat_guest` rather than `stat_file`, which would check the same ancestors a second time.
        entry = await self._stat_guest(guest, _relative(guest, working_directory))
        if entry is None:
            raise FileNotFoundError(f"no such file: {path!r}")
        if entry.kind is not EntryKind.FILE:
            raise OSError(
                f"{path!r} is a {str(entry.kind)!r} entry and only a regular file is ever read. "
                "A symlink is refused whether or not its target would have resolved somewhere "
                "legitimate, because this backend's read would follow it."
            )
        if entry.size_bytes is None:
            raise SandboxOutputSizeUnknown(
                f"the sandbox service reported no size for {path!r}, so no cap can be applied "
                "to it. Refused rather than read."
            )
        if entry.size_bytes > max_bytes:
            raise SandboxTransferCapExceeded(
                f"{path!r} is {entry.size_bytes} bytes and the caller allowed {max_bytes}"
            )
        try:
            content: bytes = await asyncio.wait_for(
                self._sc.read_file(guest), timeout=self._read_timeout
            )
        except TimeoutError as exc:
            # Not merely slow: a FIFO is reported exactly as an empty regular file — same mode,
            # both type flags false — so the classification above cannot refuse one, and the
            # read never returns. A bound turns hanging the caller's turn into a refusal.
            raise TimeoutError(
                f"reading {path!r} did not return within {self._read_timeout}s; a guest can "
                "make an entry the service reports as a regular file but never serves"
            ) from exc
        except ResourceNotFoundError as exc:
            # Same translation `list_dir` does: a file the guest deleted after the stat must not
            # reach a kind as an azure-core type.
            raise FileNotFoundError(f"no such file: {path!r}") from exc
        if len(content) > max_bytes:
            raise SandboxTransferCapExceeded(
                f"{path!r} read back as {len(content)} bytes and the caller allowed {max_bytes}"
            )
        return content

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Enumerate the entries directly under ``path``.

        Native here, which is why this is the only backend that declares
        :data:`~maf_sandbox.Capability.FILES_LIST`.  Every listed entry is confined the same way
        a declared path is, and must additionally be a direct child of the directory that was
        listed: one naming something else fails the listing rather than being reported as a path
        a caller may go on to read.  ``path`` itself is confined by component, the directory
        listed included — the service enumerates through a symlinked directory as readily as it
        reads through one.
        """
        from azure.core.exceptions import ResourceNotFoundError

        guest = await confine_resolve_guest_list_path(
            self._unconfined_stat, path, working_directory
        )
        try:
            payload = await self._files_payload(_LIST_ROUTE, guest)
        except ResourceNotFoundError as exc:
            # Translated out of the SDK's vocabulary: a kind catching this would otherwise have
            # to import azure-core to name what it caught.
            raise FileNotFoundError(f"no such directory: {path!r}") from exc
        return tuple(
            _stat_from_payload(
                entry, _listed_entry_path(entry, listed=guest, working_directory=working_directory)
            )
            for entry in _listed_entries(payload, path)
        )


class AcasSandboxBackend:
    """Hands out microVM-isolated sandboxes from an Azure Container Apps sandbox group."""

    def __init__(self, config: AcasSandboxConfig) -> None:
        self._config = config
        # (scope, thread_id, agent_dir, kind) -> sandbox_id, for this process only.
        # Keyed on scope so sandboxes from one user's session cannot be reused or deleted by
        # a request in another's, and on kind so two workloads on one agent never share a
        # sandbox — the first spec to arrive would decide the image and egress for both.
        # `dispose_scope` treats this as a fast path, never as the source of truth — see its
        # docstring.
        self._registry: dict[tuple[str, str, str, str], _Held] = {}
        #: Sandbox ids a delete could not remove, by key prefix. Apart from the registry,
        #: which `acquire` resumes from and `dispose` pops, so a failed delete is retried and
        #: never served. An entry lives only while its delete keeps failing.
        self._undeleted: dict[tuple[str, str, str], set[str]] = {}
        self._undeleted_kinds: dict[tuple[str, str, str], dict[str, str]] = {}
        self._disposal_tokens: dict[tuple[str, str, str], dict[str, object]] = {}
        self._disposal_guard = threading.Lock()
        # Group clients cached per event loop. An azure-core async client binds its transport
        # to the loop that created it, and this host runs some work on a dedicated background
        # loop, so one shared client would be a cross-loop hazard; one per call would leak a
        # connection pool per tool invocation.
        self._clients: dict[asyncio.AbstractEventLoop, tuple[Any, Any]] = {}
        #: An image-level hint for the pre-create refusal, never proof for another sandbox.
        self._guest_removals: dict[tuple[str, str], _RemovalHint] = {}
        self._guest_removals_guard = threading.Lock()
        #: Which (image, kind) pairs have already been warned about. `acquire` runs on every
        #: tool call, and a warning per call is noise rather than a signal.
        self._warned_about_the_guest: set[tuple[tuple[str, str], str]] = set()
        # One get-or-create lock per (loop, registry key) — see `_acquire_lock`.
        self._acquire_locks: dict[
            tuple[asyncio.AbstractEventLoop, tuple[str, str, str, str]], asyncio.Lock
        ] = {}

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def isolation(self) -> Isolation:
        return Isolation.MICROVM

    @property
    def declarations(self) -> BackendDeclarations:
        return _DECLARATIONS

    # -- client -------------------------------------------------------------------

    def _group_client(self) -> Any:
        """The group client for the running loop, created on first use."""
        from azure.containerapps.sandbox.aio import SandboxGroupClient
        from azure.identity.aio import DefaultAzureCredential

        loop = asyncio.get_running_loop()
        existing = self._clients.get(loop)
        if existing is not None:
            return existing[0]

        credential = DefaultAzureCredential()
        cfg = self._config
        client = SandboxGroupClient(
            endpoint=cfg.endpoint,
            credential=credential,
            subscription_id=cfg.subscription_id,
            resource_group=cfg.resource_group,
            sandbox_group=cfg.sandbox_group,
        )
        self._clients[loop] = (client, credential)
        return client

    async def aclose(self) -> None:
        """Close every cached client and credential. Errors are logged, never raised."""
        for client, credential in list(self._clients.values()):
            for closeable in (client, credential):
                close = getattr(closeable, "close", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001 - teardown must not raise
                    logger.debug(
                        "acas backend: error closing %s: %s", type(closeable).__name__, exc
                    )
        self._clients.clear()

    # -- SandboxBackend -----------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _AcasSandbox:
        """Return a running sandbox for ``key``, reusing a warm one when there is one.

        The three outcomes — reused, replaced, created — are logged at INFO rather than
        left to be inferred.  Whether a sandbox was started is the difference between a
        seconds-long call and a minutes-long one, and between one billable sandbox and
        several; none of that is visible in the tool's output, which reports compiler
        diagnostics either way.

        Get-or-create is serialised per key, because a create names no sandbox and the
        service therefore has nothing to recognise a duplicate by.  The function calls in one
        assistant message are executed concurrently, so two acquires for one key can be in
        flight at once; unserialised, both miss the registry and each is handed a running,
        billable sandbox, of which only one stays registered.

        Raises:
            SandboxCapabilityNotSupported: when the spec requires ``FILES_OUT`` or
                ``HOST_TOOLS`` and the removal compatibility probe completed with failure,
                or ``FILES_DELETE`` without a successful removal observation. An inconclusive
                probe serves the writing capabilities but refuses deletion; a successful
                probe does not establish that the guest is root. Method-scoped egress
                is also refused because the service matches methods case-insensitively.
            AcasEgressPolicyConflict: when this key and kind already hold a different
                egress policy. Successfully dispose the kind through the router or this
                backend before changing it, or use another key.
        """
        _sandbox_labels(key, spec)
        async with self._acquire_lock((key.scope, key.thread_id, key.agent_dir, spec.kind)):
            sandbox = await self._get_or_create(key, spec)
            async with asyncio.timeout(self._config.read_timeout_seconds):
                await sandbox.prepare_work_dir(spec)
            return sandbox

    def _acquire_lock(self, registry_key: tuple[str, str, str, str]) -> asyncio.Lock:
        """The get-or-create lock for one key on the running loop.

        Per loop as well as per key: an :class:`asyncio.Lock` binds to the first loop a
        caller has to *wait* on it and raises on every other one after that, and this backend
        is reachable from more than one loop (see ``_clients``).  Per key rather than one lock
        for the backend, so a cold create for one conversation never queues behind another's.
        """
        lock_key = (asyncio.get_running_loop(), registry_key)
        lock = self._acquire_locks.get(lock_key)
        if lock is None:
            lock = self._acquire_locks[lock_key] = asyncio.Lock()
        return lock

    async def _get_or_create(self, key: SandboxKey, spec: SandboxSpec) -> _AcasSandbox:
        """:meth:`acquire`'s body, run under that key's lock."""
        egress = _egress_key(spec)
        registry_key = (key.scope, key.thread_id, key.agent_dir, spec.kind)
        held = self._registry.get(registry_key)
        if held is not None and held.egress != egress:
            # Replacement could delete an instance another caller is still using.
            raise AcasEgressPolicyConflict(
                "ACAS already holds a different egress policy for this key and kind. "
                "Successfully dispose the kind with SandboxRouter.dispose_kind or "
                "AcasSandboxBackend.dispose before changing policy, or use a different key."
            )
        gc = self._group_client()
        if held is not None:
            sandbox_id = held.sandbox_id
            try:
                sc = gc.get_sandbox_client(sandbox_id)
                await sc.ensure_running(timeout=_RESUME_TIMEOUT_S)
                reused = _AcasSandbox(sc, self._config.read_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - a dead sandbox is replaced, not reported
                # Not a warning: a sandbox reclaimed by its auto-delete timer between rounds
                # is the expected path, not a fault. But it does mean the next call pays for
                # a cold create, so the reason is worth a line rather than a silent `pass`.
                logger.info(
                    "sandbox %s did not resume (%s); creating a replacement",
                    sandbox_id,
                    error_detail(exc),
                )
            else:
                # Outside the `try`, because a refusal is this acquire's answer rather than a
                # sandbox that failed to resume, and the handler above would swallow it into a
                # replacement create. Before the log, so a refused acquire does not report one
                # of the three outcomes `acquire` promises to name.
                await self._refuse_or_warn_on_guest_removal(spec, reused, held=held)
                await self._probe_commands(spec, reused, held)
                logger.info(
                    "sandbox reused: id=%s kind=%s thread=%s agent=%s",
                    sandbox_id,
                    spec.kind,
                    key.thread_id,
                    key.agent_dir,
                )
                return reused
            self._registry.pop(registry_key, None)

        # The hint answers here and nowhere earlier, because here is where a create is about to
        # be paid for: the second workload to meet a refused image is refused without one. A
        # warm sandbox never reaches this, and must not — it has a verdict of its own, where
        # the hint is whatever some other guest last answered for the same reference.
        await self._refuse_or_warn_on_guest_removal(spec)

        # Two namespaces, and `image` says which by whether it carries a tag: a bare name is
        # one the service prebuilt, anything else is repository:tag for an image this
        # deployment imported, which the configured registry qualifies. `image_id` still
        # skips both, exactly as the field promises — pinning an id means resolution is not
        # wanted, and that is as true of the catalogue as of the imported list.
        if spec.image_id is None and names_a_prebuilt_image(spec.image or ""):
            booted_from = await resolve_prebuilt_image_name(gc, spec.image or "")
            # `disk` and `disk_id` are the two keywords the SDK reads the namespace from, and
            # it refuses them together — so the source is one key, chosen here, not two
            # arguments one of which is None.
            source: dict[str, str] = {"disk": booted_from}
        else:
            image = qualify_image_reference(self._config.registry, spec.image or "")
            booted_from = await resolve_disk_image_id(gc, spec.image_id, image or None)
            source = {"disk_id": booted_from}
        poller = await gc.begin_create_sandbox(
            **source,
            labels=_sandbox_labels(key, spec),
            egress_policy=self._egress_policy(spec),
        )
        sc = await poller.result()
        logger.info(
            "sandbox created: id=%s kind=%s disk_image=%s thread=%s agent=%s",
            sc.sandbox_id,
            spec.kind,
            booted_from,
            key.thread_id,
            key.agent_dir,
        )
        # Register immediately, so the sandbox is reachable by purge even if configure fails.
        held = self._registry[registry_key] = _Held(sc.sandbox_id, egress=egress)
        try:
            await self._configure(sc)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: the sandbox runs with SDK default policies. The service default is not
            # known to include auto-delete, so recovery is operator-owned from here.
            logger.warning(
                "acas backend: failed to configure lifecycle policy for sandbox %s; "
                "it is labelled for recovery but no auto-delete timer was confirmed: %s",
                sc.sandbox_id,
                error_detail(exc),
            )
        created = _AcasSandbox(sc, self._config.read_timeout_seconds)
        try:
            await self._refuse_or_warn_on_guest_removal(
                spec, created, held=held, freshly_created=True
            )
            await self._probe_commands(spec, created, held)
        except SandboxCapabilityNotSupported:
            self._registry.pop(registry_key, None)
            await self._release_the_refused(gc, key, sc.sandbox_id, kind=spec.kind)
            raise
        return created

    async def _probe_commands(self, spec: SandboxSpec, sandbox: _AcasSandbox, held: _Held) -> None:
        deadline = asyncio.get_running_loop().time() + min(10.0, self._config.read_timeout_seconds)

        async def run(argv: tuple[str, ...], as_root: bool) -> int:
            assert not as_root
            async with asyncio.timeout_at(deadline):
                result = await sandbox.exec(
                    argv,
                    working_directory="/",
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
            return result.exit_code

        await probe_commands(spec, held.commands, run)

    def _retain_disposals(
        self, prefix: tuple[str, str, str], names: Sequence[str], kinds: Mapping[str, str]
    ) -> dict[str, object]:
        """Reserve retry records while holding the disposal guard."""
        if not names:
            return {}
        tokens = {name: object() for name in names}
        self._disposal_tokens.setdefault(prefix, {}).update(tokens)
        self._undeleted.setdefault(prefix, set()).update(names)
        self._undeleted_kinds.setdefault(prefix, {}).update(kinds)
        return tokens

    def _finish_disposals(
        self,
        prefix: tuple[str, str, str],
        attempted: Mapping[str, object],
        failed: Sequence[str],
        kinds: Mapping[str, str],
    ) -> None:
        """Reconcile this attempt while holding the disposal guard."""
        self._retain_disposals(prefix, failed, {n: kinds[n] for n in failed if n in kinds})
        tokens = self._disposal_tokens.get(prefix, {})
        names = self._undeleted.get(prefix, set())
        attributed = self._undeleted_kinds.get(prefix, {})
        for name, token in attempted.items():
            if tokens.get(name) is token:
                tokens.pop(name)
                names.discard(name)
                attributed.pop(name, None)
        if not tokens:
            self._disposal_tokens.pop(prefix, None)
        if not names:
            self._undeleted.pop(prefix, None)
        if not attributed:
            self._undeleted_kinds.pop(prefix, None)

    async def _release_the_refused(
        self, gc: Any, key: SandboxKey, sandbox_id: str, *, kind: str
    ) -> None:
        """Delete a refused fresh sandbox, retaining its kind for retries if deletion fails."""
        prefix = (key.scope, key.thread_id, key.agent_dir)
        kinds = {sandbox_id: kind}
        with self._disposal_guard:
            attempted = self._retain_disposals(prefix, [sandbox_id], kinds)
        deletion = await self._delete(gc, sandbox_id)
        with self._disposal_guard:
            self._finish_disposals(
                prefix, attempted, [sandbox_id] if deletion.failure is not None else [], kinds
            )
        if deletion.failure is not None:
            return
        logger.info(
            "sandbox released: id=%s thread=%s agent=%s",
            sandbox_id,
            key.thread_id,
            key.agent_dir,
        )

    async def _refuse_or_warn_on_guest_removal(
        self,
        spec: SandboxSpec,
        sandbox: _AcasSandbox | None = None,
        *,
        held: _Held | None = None,
        freshly_created: bool = False,
    ) -> None:
        """Require observed removal compatibility for deletes; warn an exec-only workload.

        A completed removal failure also refuses workloads that need a writing guest.
        An inconclusive probe serves that functional set, but never FILES_DELETE.
        """
        if not spec.requires & _PROBE_WHEN_REQUIRED:
            return
        identity = _image_identity(spec)
        if sandbox is None or held is None:
            # Nothing running to ask, so the hint answers or the caller creates one. This is
            # the refusal that spares the second workload a create.
            hint = self._guest_removals.get(identity)
            if hint is None or monotonic() >= hint.expires_at:
                return
            removal = hint.removal
        elif held.probed:
            # This sandbox's own answer, never the image hint: another sandbox booted from the
            # same reference may have moved that since, and it describes a different guest.
            removal = held.removal
        else:
            removal = await self._probe_guest_removal(sandbox, spec, held)
        if removal is True:
            return

        image = _image_label(spec)
        unbackable: frozenset[Capability] = (
            spec.requires & _NEEDS_A_WRITING_GUEST if removal is not None else frozenset()
        )
        unproven = spec.requires & _NEEDS_OBSERVED_REMOVAL
        refused = unbackable | unproven
        if refused:
            reasons: list[str] = []
            if unbackable:
                reasons.append(
                    f"{', '.join(sorted(unbackable))} because every directory this backend's file "
                    "plane creates belongs to root and is writable by nobody else, so the guest "
                    "program can create neither a declared output beside the files it was given "
                    "nor the host-tool transport's own markers — inside the tool call that "
                    "arrives as a shell's 'Permission denied'"
                )
            if unproven:
                reasons.append(
                    f"{', '.join(sorted(unproven))} because its guest rm did not demonstrate "
                    "removal of the file plane's probe file. Each requested removal runs as "
                    "the guest and must also be confirmed through the file plane"
                )
            if removal is False:
                whose_guest = "its guest could not remove the file plane's probe file"
                remedy = "Serve this workload on an image whose USER is root and can run rm"
            else:
                whose_guest = "its guest's removal probe was inconclusive"
                remedy = (
                    "Serve this workload on an image whose guest can run rm and whose "
                    "file plane confirms the removal — a root USER alone does not prove it"
                )
            if sandbox is not None and not freshly_created:
                recovery = (
                    "This warm sandbox retains its own verdict. Dispose it before acquiring "
                    "from a repaired image; repointing a reference cannot change a running guest."
                    if held is not None and held.probed
                    else "No cached refusal blocks a retry; the next acquire probes this warm "
                    "sandbox again."
                )
            else:
                hint = self._guest_removals.get(identity)
                remaining = (
                    max(0.0, hint.expires_at - monotonic())
                    if hint is not None and hint.removal is not True
                    else 0.0
                )
                recovery = (
                    f"The cached refusal expires in {remaining:.1f} seconds. After expiry, "
                    "the next cold acquire creates and probes again. Repeated refusals do "
                    "not extend the deadline; no process restart is needed."
                    if remaining > 0
                    else "No cached refusal blocks a retry; the next cold acquire asks again."
                )
            raise SandboxCapabilityNotSupported(
                f"sandbox backend {BACKEND_NAME!r} cannot serve "
                f"{', '.join(sorted(refused))} to the {spec.kind!r} workload from {image}: "
                f"{whose_guest}, and it refuses {'; and '.join(reasons)}. Refused here "
                f"rather than inside the tool call. {remedy}, or narrow what it requires. "
                f"{recovery}"
            )
        if removal is None or sandbox is None:
            # Served, and silently. An unreadable probe would otherwise warn about a wall this
            # image may not have, on every acquire. And the pre-create path is reading a hint
            # from whatever the reference last resolved to, so warning here would describe the
            # wrong artefact *and* mark the pair warned, silencing the accurate one the
            # post-create call is about to be able to make.
            return
        already_warned = (_image_identity(spec), spec.kind)
        if already_warned in self._warned_about_the_guest:
            return
        self._warned_about_the_guest.add(already_warned)
        logger.warning(
            "acas: %s could not remove the file plane's probe file, so a program the %s "
            "workload execs can read "
            "what write_file placed but cannot create any file of its own beside it — every "
            "directory the file plane makes belongs to root. An exec whose whole result is its "
            "stdout is unaffected; anything the guest has to write is not.",
            image,
            spec.kind,
        )

    async def _probe_guest_removal(
        self, sandbox: _AcasSandbox, spec: SandboxSpec, held: _Held
    ) -> bool | None:
        """Cache completed probes for this sandbox, preserving measurements on failure."""
        identity = _image_identity(spec)
        generation = self._guest_removals.get(identity, _RemovalHint(None, 0)).generation
        try:
            removal = await sandbox.probe_guest_removal()
        except Exception as unreachable:  # noqa: BLE001 - an acquire must not fail over this
            logger.debug(
                "acas: removal probe for %s did not complete (%s); the next acquire retries",
                _image_label(spec),
                error_detail(unreachable),
            )
            return held.removal if held.probed else None
        if removal is None:
            held.probed = True
        else:
            held.removal, held.probed = removal, True
        # The comparison and write must be atomic across the host's event loops. Even a
        # repeated measurement advances the generation so an overlapping unknown stands down.
        with self._guest_removals_guard:
            current = self._guest_removals.get(identity, _RemovalHint(None, 0))
            if removal is not None or (held.removal is None and current.generation == generation):
                self._guest_removals[identity] = _RemovalHint(
                    removal, monotonic() + _REMOVAL_HINT_TTL_S, current.generation + 1
                )
        return held.removal

    async def dispose(
        self, key: SandboxKey, *, kind: str | None = None, instance_id: str | None = None
    ) -> DisposalFailure | None:
        """Delete this key's sandboxes, narrowed to kind when given.

        Service labels discover ownership; retained IDs cover a failed sweep listing.
        Failed deletions are retained per kind for retries and reported without raising."""
        prefix = (key.scope, key.thread_id, key.agent_dir)
        with self._disposal_guard:
            mine = [
                k
                for k in list(self._registry)
                if k[:3] == prefix
                and (kind is None or k[3] == kind)
                and (instance_id is None or self._registry[k].sandbox_id == instance_id)
            ]
            attributed = self._undeleted_kinds.setdefault(prefix, {})
            remembered: list[str] = []
            for entry in mine:
                held = self._registry.pop(entry)
                remembered.append(held.sandbox_id)
                attributed[held.sandbox_id] = entry[3]
            retained = sorted(
                name
                for name in self._undeleted.get(prefix, ())
                if (kind is None or attributed.get(name) == kind)
                and (instance_id is None or name == instance_id)
            )
            wanted = list(
                dict.fromkeys(
                    [
                        *remembered,
                        *retained,
                    ]
                )
            )
            attempted_kinds = {name: attributed[name] for name in wanted if name in attributed}
            attempted = self._retain_disposals(prefix, wanted, attempted_kinds)
        try:
            gc = self._group_client()
        except Exception as exc:  # noqa: BLE001 - disposal must never raise
            logger.warning("acas backend: could not reach the sandbox group: %s", error_detail(exc))
            return DisposalFailure(
                "unreachable", f"could not reach the sandbox group: {error_detail(exc)}"
            )
        labels = {
            _LABEL_SCOPE: _label_value(key.scope),
            _LABEL_THREAD: _label_value(key.thread_id),
            _LABEL_AGENT: _label_value(key.agent_dir),
        }
        if kind is not None:
            labels[_LABEL_KIND] = _label_value(kind)
        listed: list[str] | None = []
        try:
            async for sandbox in gc.list_sandboxes(labels=labels):
                sandbox_id = getattr(sandbox, "id", None)
                if not isinstance(sandbox_id, str) or not sandbox_id:
                    raise ValueError("the service returned no sandbox ID")
                if instance_id is None or sandbox_id == instance_id:
                    listed.append(sandbox_id)
        except Exception as exc:  # noqa: BLE001 - a failed listing is never an empty inventory
            logger.warning(
                "acas backend: could not discover disposal targets: %s", error_detail(exc)
            )
            listed = None
        if instance_id is not None:
            # A local record cannot prove current engine ownership of a supplied ID.
            if listed is None:
                return DisposalFailure("unlisted", "could not verify sandbox ownership")
            wanted = listed
        elif listed is not None:
            wanted = list(dict.fromkeys([*wanted, *listed]))
        with self._disposal_guard:
            if kind is not None:
                attempted_kinds.update(dict.fromkeys(wanted, kind))
            attempted.update(
                self._retain_disposals(
                    prefix, [name for name in wanted if name not in attempted], attempted_kinds
                )
            )
        undeleted: dict[str, DisposalFailure] = {}
        for sandbox_id in wanted:
            deletion = await self._delete(gc, sandbox_id)
            if deletion.deleted:
                logger.info(
                    "sandbox released: id=%s thread=%s agent=%s",
                    sandbox_id,
                    key.thread_id,
                    key.agent_dir,
                )
            if deletion.failure is not None:
                undeleted[sandbox_id] = deletion.failure
        with self._disposal_guard:
            self._finish_disposals(prefix, attempted, list(undeleted), attempted_kinds)
            left = self._undeleted.get(prefix, set())
            attributed = self._undeleted_kinds.get(prefix, {})
            outstanding = {
                name
                for name in left
                if (kind is None or attributed.get(name) == kind)
                and (instance_id is None or name == instance_id)
            }
        reported = fold_disposal_failures(
            [
                *undeleted.values(),
                *(
                    []
                    if listed is not None
                    else [DisposalFailure("unlisted", "sandbox sweep may be partial")]
                ),
            ]
        )
        if reported is not None:
            return reported
        if outstanding:
            # Pending attempts cannot yet certify cleanup.
            return DisposalFailure(
                "unknown",
                f"another disposal has not yet reported on {len(outstanding)} sandbox(es)",
            )
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete sandboxes labelled ``(scope, thread_id)`` and report what stayed.

        Labels reach sandboxes created elsewhere; registry and retry records cover failed
        listings, which are still reported. Registry entries are dropped before deletion.
        """
        with self._disposal_guard:
            known = [
                (k, entry.sandbox_id)
                for k, entry in list(self._registry.items())
                if k[0] == scope and k[1] == thread_id
            ]
            for k, _ in known:
                self._registry.pop(k, None)
            for entry, sandbox_id in known:
                self._undeleted_kinds.setdefault(entry[:3], {})[sandbox_id] = entry[3]
                self._undeleted.setdefault(entry[:3], set()).add(sandbox_id)

            retained = {
                p: set(names)
                for p, names in self._undeleted.items()
                if p[0] == scope and p[1] == thread_id
            }
            attempted_kinds = {
                p: {
                    name: kind
                    for name, kind in self._undeleted_kinds.get(p, {}).items()
                    if name in names
                }
                for p, names in retained.items()
            }
            attempted = {
                prefix: self._retain_disposals(prefix, list(names), attempted_kinds[prefix])
                for prefix, names in retained.items()
            }
        try:
            gc = self._group_client()
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning("acas backend: could not reach the sandbox group: %s", exc)
            return ScopePurge(
                0,
                DisposalFailure(
                    "unreachable", f"could not reach the sandbox group: {error_detail(exc)}"
                ),
            )

        undisposed: list[DisposalFailure] = []
        ids = {sandbox_id for _, sandbox_id in known}
        ids.update(sandbox_id for names in retained.values() for sandbox_id in names)
        listed = await self._list_thread_sandbox_ids(gc, scope, thread_id)
        if listed is None:
            undisposed.append(
                DisposalFailure(
                    "unlisted",
                    "could not list the thread's sandboxes, so the sweep may be partial",
                )
            )
        else:
            ids.update(listed)

        count = 0
        undeleted: set[str] = set()
        for sandbox_id in sorted(ids):
            deletion = await self._delete(gc, sandbox_id)
            if deletion.deleted:
                logger.info(
                    "sandbox released: id=%s thread=%s (scope purge)", sandbox_id, thread_id
                )
                count += 1
            if deletion.failure is not None:
                undeleted.add(sandbox_id)
                undisposed.append(deletion.failure)
        with self._disposal_guard:
            for prefix, tokens in attempted.items():
                self._finish_disposals(
                    prefix, tokens, list(tokens.keys() & undeleted), attempted_kinds[prefix]
                )
        return ScopePurge(count, fold_disposal_failures(undisposed))

    # -- internals ----------------------------------------------------------------

    def _egress_policy(self, spec: SandboxSpec) -> Any:
        """Deny by default, allow only the hosts the spec names."""
        from azure.containerapps.sandbox import EgressHostRule, EgressPolicy

        _egress_key(spec)
        return EgressPolicy(
            default_action="Deny",
            traffic_inspection="Full",
            host_rules=[
                EgressHostRule(pattern=str(host), action="Allow") for host in spec.egress_allow
            ],
        )

    async def _configure(self, sandbox_client: Any) -> None:
        """Apply the lifecycle policy to a freshly created sandbox."""
        from azure.containerapps.sandbox import (
            AutoDeletePolicy,
            AutoSuspendPolicy,
            LifecyclePolicy,
        )

        await sandbox_client.set_lifecycle_policy(
            LifecyclePolicy(
                auto_suspend=AutoSuspendPolicy(
                    enabled=True,
                    interval=self._config.auto_suspend_seconds,
                    mode="Memory",
                ),
                auto_delete=AutoDeletePolicy(
                    enabled=True,
                    delete_interval_seconds=self._config.auto_delete_seconds,
                ),
            )
        )

    async def _delete(self, group_client: Any, sandbox_id: str) -> _Deletion:
        """Best-effort delete. Never raises; reports what it did.

        A sandbox the service no longer has is a delete with nothing to do, not a failure: the
        auto-delete timer reclaiming one between rounds is the expected path, the same reading
        the resume above takes of it.
        """
        from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

        try:
            poller = await group_client.get_sandbox_client(sandbox_id).begin_delete()
            await poller.result()
            return _Deletion(deleted=True)
        except ResourceNotFoundError:
            return _Deletion(deleted=False)
        except ServiceRequestError as exc:
            # The request never reached the service, so the sandbox was never asked about.
            logger.warning(
                "acas backend: failed to delete sandbox %s: %s", sandbox_id, error_detail(exc)
            )
            return _Deletion(
                deleted=False,
                failure=DisposalFailure("unreachable", f"{sandbox_id}: {error_detail(exc)}"),
            )
        except Exception as exc:  # noqa: BLE001
            # The service answered and the sandbox is still there — a role the principal lacks
            # far more often than anything transient.
            logger.warning(
                "acas backend: failed to delete sandbox %s: %s", sandbox_id, error_detail(exc)
            )
            return _Deletion(
                deleted=False,
                failure=DisposalFailure("refused", f"{sandbox_id}: {error_detail(exc)}"),
            )

    async def _list_thread_sandbox_ids(
        self, group_client: Any, scope: str, thread_id: str
    ) -> list[str] | None:
        """Sandbox ids labelled ``(scope, thread_id)``, or ``None`` when the query failed.

        Told apart, because the sentence below is otherwise the whole record: a listing that
        failed and a conversation with nothing in it both come back empty, and only one of
        them means the purge covered everything.
        """
        ids: list[str] = []
        try:
            # `_label_value` on both sides, always: these have to be the same strings the
            # create wrote, or the query matches nothing and every sandbox for the deleted
            # conversation keeps running until its auto-delete timer fires — silently, since
            # "found none to delete" and "there were none" are the same result here.
            async for sandbox in group_client.list_sandboxes(
                labels={
                    _LABEL_SCOPE: _label_value(scope),
                    _LABEL_THREAD: _label_value(thread_id),
                }
            ):
                sandbox_id = getattr(sandbox, "id", None)
                if sandbox_id:
                    ids.append(sandbox_id)
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning(
                "acas backend: could not list sandboxes for thread %s: %s",
                thread_id,
                error_detail(exc),
            )
            return None
        return ids


# The package's strict pyright pass type-checks this assignment. ``runtime_checkable`` tests
# member *presence* only, so a narrowed signature or a missing method passes `isinstance` and
# fails here instead — in the package where the divergence would be introduced.
if TYPE_CHECKING:
    _: tuple[SandboxBackend, type[Sandbox]] = (
        AcasSandboxBackend(AcasSandboxConfig(endpoint="https://sandbox.invalid")),
        _AcasSandbox,
    )
