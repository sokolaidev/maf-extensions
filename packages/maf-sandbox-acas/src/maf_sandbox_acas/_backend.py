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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from maf_sandbox import (
    Capability,
    DisposalFailure,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    Sandbox,
    SandboxBackend,
    SandboxCapabilityNotSupported,
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
    confine_guest_path,
    confine_guest_write_path,
    guest_path_relative_to,
    refuse_symlinked_parents,
)

from ._config import AcasSandboxConfig
from ._images import (
    names_a_prebuilt_image,
    qualify_image_reference,
    resolve_disk_image_id,
    resolve_prebuilt_image_name,
)

logger = logging.getLogger(__name__)

__all__ = ["BACKEND_NAME", "AcasEntryPayloadIncomplete", "AcasSandboxBackend"]

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
    return {
        _LABEL_SCOPE: _label_value(key.scope),
        _LABEL_THREAD: _label_value(key.thread_id),
        _LABEL_AGENT: _label_value(key.agent_dir),
        _LABEL_KIND: _label_value(spec.kind),
        **{k: _label_value(v) for k, v in spec.labels.items()},
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

#: What makes the guest's uid worth reading at all. `EXEC` earns a warning rather than a
#: refusal: a command whose whole result is its stdout runs fine as any user.
_PROBE_WHEN_REQUIRED = _NEEDS_A_WRITING_GUEST | {Capability.EXEC}

#: How the guest's uid is read, once per image on a cold acquire.
_GUEST_UID_COMMAND = "id -u"

#: Where that runs. Never `spec.work_dir`, which nothing has created yet at acquire: an exec
#: into a directory that does not exist fails for a reason unrelated to the answer.
_GUEST_PROBE_WORKING_DIRECTORY = "/"

#: How long the probe gets. Its own bound rather than `read_timeout_seconds`, which is 120 by
#: default and describes a read that never returns: a guest that has not answered `id -u` in 30
#: seconds is not going to, and this runs on the way to a cold acquire.
_PROBE_TIMEOUT_S = 30.0


def _image_identity(spec: SandboxSpec) -> tuple[str, str]:
    """What a spec names its image by — the key the guest's uid is remembered under.

    Both fields, because ``image_id`` skips resolution entirely: two specs sharing an ``image``
    can still boot different artefacts.
    """
    return (spec.image_id or "", spec.image or "")


def _image_label(spec: SandboxSpec) -> str:
    """How a message names the image :func:`_image_identity` keys on."""
    return spec.image or spec.image_id or "the configured image"


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


def _confined(path: str, working_directory: str) -> tuple[str, str]:
    """Resolve ``path`` against ``working_directory``: the guest path, and the relative one.

    Paired because every caller here wants the relative half for a
    :class:`~maf_sandbox.SandboxEntry` as soon as the absolute one is confined.  That half is
    never ``None`` — :func:`~maf_sandbox.paths.confine_guest_path` has already refused anything
    outside — so the ``or ""`` narrows a type rather than covering a case.
    """
    resolved = confine_guest_path(path, working_directory)
    return resolved, guest_path_relative_to(resolved, working_directory) or ""


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
    resolved, relative = _confined(reported, working_directory)
    if posixpath.dirname(resolved) != listed:
        raise AcasEntryPayloadIncomplete(
            f"the sandbox service listed {reported!r} as an entry of {listed!r}, which is not "
            "its parent, and a listing enumerates one level only"
        )
    return relative


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

    async def write_file(self, path: str, content: str | bytes, *, working_directory: str) -> None:
        # `create_dirs=True` is the SDK's own default, and it is passed explicitly anyway.
        # A workload may hand us a nested path — `infra/main.bicep` is the example in the
        # bicep tool's own description — and without it every such write fails on a missing
        # parent. The file API docs do not mention the behaviour at all, so it is the SDK
        # signature that is load-bearing here; relying silently on a `0.1.0bN` default is how
        # `DiskImage.image` got missed. Stating it costs nothing and pins the intent.
        guest = await confine_guest_write_path(
            lambda p: self._stat_guest(p, p), path, working_directory
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
        :func:`shlex.join` first.  ``shlex.join`` produces POSIX quoting, which is correct
        here because every sandbox this backend hands out is Linux.
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
        *parents* are walked first, exactly as a read walks them: no byte of ``/etc`` crosses
        when ``out -> /etc`` is statted through, but its type and size do, and that is
        metadata from outside the boundary.

        The **final** component is described rather than refused: a link reported as
        :data:`~maf_sandbox.EntryKind.SYMLINK` is how a caller learns it is one.
        """
        guest, relative = _confined(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        return await self._stat_guest(guest, relative)

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
        """Delete ``path`` through the data plane's own ``delete_file`` — no shell, no ``rm``.

        The service unlinks a final symlink component, but resolves symlinked parents, so the
        parent walk remains refused before the delete. A directory is refused without
        ``recursive`` whatever it holds: the rule is on the entry's kind, because a backend
        that cannot enumerate cannot tell empty from full.
        """
        from azure.core.exceptions import ResourceNotFoundError

        guest = confine_guest_path(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        if posixpath.normpath(guest) == posixpath.normpath(working_directory):
            raise ValueError(
                f"refusing to remove the working directory itself: {working_directory}"
            )
        planted = await self._stat_guest(guest, posixpath.normpath(path))
        if planted is not None and planted.kind is EntryKind.DIRECTORY and not recursive:
            raise OSError(f"refusing to remove a directory without recursive: {path}")
        try:
            # Bounded like every other call on this data plane: this one runs from a `finally`,
            # where a wedged service would otherwise hold the caller's turn open with the run's
            # own failure still unreported.
            await asyncio.wait_for(
                self._sc.delete_file(guest, recursive=recursive),
                timeout=self._read_timeout,
            )
        except ResourceNotFoundError:
            return
        except TimeoutError:
            raise
        except Exception as refused:
            # `azure.core` raises its own hierarchy, and `HttpResponseError` is no `OSError`.
            # Chained rather than interpolated: `error_detail` enriches with the response body,
            # and this one is raised at a caller rather than logged.
            raise OSError(f"could not remove {path}: {type(refused).__name__}") from refused

    async def reclaim(self, directory: str, *, working_directory: str, timeout: float) -> None:
        """Remove ``directory`` through the data plane's ``delete_file``, which acts as the
        host rather than as the image's ``USER`` — so a file the file plane wrote as root is
        removable on an image whose guest is not root, where ``rm`` over ``exec`` could not.

        Reach: a guest that swaps ``directory`` itself gains nothing — this mechanism unlinks a
        directly-named link instead of following it — but a swapped *ancestor* **is followed**,
        so the argument there rests on who owns the component. The full argument, and the
        launcher-created residual, is [`acas.md`](../../../../docs/sandbox/backends/acas.md)'s
        to carry; the guards below refuse what this backend cannot place.
        """
        from azure.core.exceptions import ResourceNotFoundError

        del working_directory
        # Both refusals stand on their own rather than trusting the caller's, because this
        # removal is recursive, irreversible, and now runs with the host's authority.
        if not directory.startswith("/"):
            raise ValueError(f"refusing to reclaim a path that is not absolute: {directory}")
        target = posixpath.normpath(directory)
        if len([part for part in target.split("/") if part]) < 2:
            raise ValueError(f"refusing to reclaim recursively that close to the root: {target}")
        try:
            # Bounded like every other call on this data plane: this one runs from a `finally`,
            # where a wedged service would otherwise hold the caller's turn open with the
            # caller's own failure still unreported.
            await asyncio.wait_for(self._sc.delete_file(target, recursive=True), timeout=timeout)
        except ResourceNotFoundError:
            # A directory already gone is success — `reclaim` runs from a `finally`, and a
            # no-op cleanup must not bury the error that brought the caller here.
            return
        except TimeoutError:
            raise
        except Exception as refused:
            # `azure.core` raises its own hierarchy, and `HttpResponseError` is no `OSError`.
            # Translated the way `remove` translates, so a caller catching what the docstring
            # says catches a transport failure too. `error_detail` carries the response body,
            # which `str()` drops — `ReclaimFailure.reason` serializes this wrapper without
            # traversing `__cause__`, so the detail has to ride in the message itself.
            raise OSError(f"could not reclaim {directory}: {error_detail(refused)}") from refused

    async def _stat_guest(self, guest: str, relative: str) -> SandboxEntry | None:
        """Stat an absolute guest path, with no confinement check of its own.

        Split out because the component walk stats the working directory's own ancestors, which
        by definition sit outside it — confining here would refuse the very check being made.
        """
        from azure.core.exceptions import ResourceNotFoundError

        try:
            payload = await self._files_payload(_STAT_ROUTE, guest)
        except ResourceNotFoundError:
            return None
        return _stat_from_payload(payload, relative)

    async def _refuse_symlinked_parents(
        self, guest: str, *, working_directory: str, include_guest: bool = False
    ) -> None:
        """The protocol's component walk, over this backend's own unconfined stat.

        A symlinked *parent* is invisible in the final entry's stat — with
        ``/maf-sandbox/work/out -> /etc``, ``out/hostname`` stats as a regular 12-byte file and
        reads ``/etc/hostname`` — and this API offers no no-follow read and no realpath to
        settle it in one call, so it costs one stat per component.  ``include_guest`` is what
        :meth:`list_dir` needs: the service enumerates through a symlinked directory as readily
        as it reads through one.
        """
        await refuse_symlinked_parents(
            lambda directory: self._stat_guest(directory, directory),
            guest,
            working_directory,
            include_self=include_guest,
        )

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

        guest, relative = _confined(path, working_directory)
        await self._refuse_symlinked_parents(guest, working_directory=working_directory)
        # `_stat_guest` rather than `stat_file`, which would walk the same parents a second time.
        entry = await self._stat_guest(guest, relative)
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

        guest, _ = _confined(path, working_directory)
        await self._refuse_symlinked_parents(
            guest, working_directory=working_directory, include_guest=True
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
        self._registry: dict[tuple[str, str, str, str], str] = {}
        #: Sandbox ids a delete could not remove, by key prefix. Apart from the registry,
        #: which `acquire` resumes from and `dispose` pops, so a failed delete is retried and
        #: never served. An entry lives only while its delete keeps failing.
        self._undeleted: dict[tuple[str, str, str], set[str]] = {}
        # Group clients cached per event loop. An azure-core async client binds its transport
        # to the loop that created it, and this host runs some work on a dedicated background
        # loop, so one shared client would be a cross-loop hazard; one per call would leak a
        # connection pool per tool invocation.
        self._clients: dict[asyncio.AbstractEventLoop, tuple[Any, Any]] = {}
        #: The uid `exec` runs as, per image a spec named — `None` where the image could not
        #: say. A property of the image rather than of the sandbox booted from it, so one probe
        #: answers for every sandbox after; membership, not the value, is what says it was
        #: asked, because an image with no `id` must not be re-probed on every tool call.
        self._guest_uids: dict[tuple[str, str], int | None] = {}
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
    def egress_modes(self) -> frozenset[Egress]:
        # `_egress_policy` builds a Deny-default allowlist: named hosts resolve as ALLOWLIST,
        # an empty allowlist as CLOSED (deny all). Never UNRESTRICTED — the group's policy
        # denies by default and cannot be told to allow everything.
        return frozenset({Egress.ALLOWLIST, Egress.CLOSED})

    @property
    def capabilities(self) -> frozenset[Capability]:
        # FILES_LIST as well as FILES_OUT, which is the split's own test — name the backend
        # that lacks it. Enumeration is native here and unavailable on the backends that
        # transport a named path only.
        #
        # HOST_TOOLS is the one member with no method behind it, so what it asserts here is
        # narrower than the others and worth stating: `exec` **detaches**. A process started by
        # one call outlives it and is observable from the next, because the sandbox is a microVM
        # the group keeps between calls rather than a session torn down per `exec` call — which
        # is what `host_tool_calls_over_exec` is built on, its launcher returning at once and the
        # exit-code file being the run's only witness. This is the backend where that could not
        # be taken on faith, since every call is an HTTP round trip to a remote control plane, so
        # `test_acas_e2e.py` measures it against the service rather than against a reading of the
        # SDK.
        #
        # It is *not* a claim about the image. The shipped launcher wants `sh`, `nohup`,
        # `printf`, `mv`, `mkdir`, `rm` and `kill`, and `setsid` where the image has it; a
        # kind wants whatever interpreter it names — codeact wants `python3` —
        # none of which this backend chooses, since `spec.image` does. That gap is #111's axis,
        # and it is the same gap `EXEC` already has: a kind execing `python3` against an image
        # without Python fails inside the sandbox today.
        #
        # The image does narrow one thing, and `acquire` is where it lands rather than here: a
        # guest that is not root can create nothing inside a directory the file plane made, so
        # this pair is refused there for such an image (#722).
        return frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
                Capability.FILES_DELETE,
                Capability.HOST_TOOLS,
            }
        )

    @property
    def limits(self) -> SandboxLimits:
        return _LIMITS

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
                ``HOST_TOOLS`` and the image's guest is not root, which
                :meth:`_refuse_or_warn_where_the_guest_cannot_write` explains.
        """
        async with self._acquire_lock((key.scope, key.thread_id, key.agent_dir, spec.kind)):
            return await self._get_or_create(key, spec)

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
        gc = self._group_client()
        registry_key = (key.scope, key.thread_id, key.agent_dir, spec.kind)
        # From what an earlier acquire measured, so the second workload to meet a refused image
        # is refused before this one pays for a create.
        await self._refuse_or_warn_where_the_guest_cannot_write(spec)

        sandbox_id = self._registry.get(registry_key)
        if sandbox_id is not None:
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
                await self._refuse_or_warn_where_the_guest_cannot_write(spec, reused)
                logger.info(
                    "sandbox reused: id=%s kind=%s thread=%s agent=%s",
                    sandbox_id,
                    spec.kind,
                    key.thread_id,
                    key.agent_dir,
                )
                return reused
            self._registry.pop(registry_key, None)

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
        self._registry[registry_key] = sc.sandbox_id
        try:
            await self._configure(sc)
        except Exception:  # noqa: BLE001
            # Non-fatal: the sandbox runs with SDK default policies. No best-effort delete —
            # the auto-delete timer reclaims it, and failing the caller here would turn a
            # policy hiccup into a lost turn.
            logger.warning(
                "acas backend: failed to configure lifecycle policy for sandbox %s; "
                "it will be reclaimed by the auto-delete timer",
                sc.sandbox_id,
            )
        created = _AcasSandbox(sc, self._config.read_timeout_seconds)
        try:
            await self._refuse_or_warn_where_the_guest_cannot_write(spec, created)
        except SandboxCapabilityNotSupported:
            self._registry.pop(registry_key, None)
            await self._release_the_refused(gc, key, sc.sandbox_id)
            raise
        return created

    async def _release_the_refused(self, gc: Any, key: SandboxKey, sandbox_id: str) -> None:
        """Delete a sandbox this acquire created and then refused; remember it if that fails.

        An acquire that raises is handed to nobody, and the framework's per-call cleanup
        disposes only what it was handed — so without this the microVM runs on, billable and
        unusable, until the auto-delete timer or the conversation's purge reaches it.  The
        caller has already dropped it from the registry.
        """
        prefix = (key.scope, key.thread_id, key.agent_dir)
        # Before the await, the way `dispose` records it: the registry no longer holds this id
        # and there is no listing to fall back on, so a delete that fails is retried only here.
        self._undeleted[prefix] = self._undeleted.get(prefix, set()) | {sandbox_id}
        if (await self._delete(gc, sandbox_id)).failure is not None:
            return
        logger.info(
            "sandbox released: id=%s thread=%s agent=%s",
            sandbox_id,
            key.thread_id,
            key.agent_dir,
        )
        # Read from the live map, not from what this call wrote: a disposal running beside it
        # may have recorded ids of its own.
        left = self._undeleted.get(prefix, set()) - {sandbox_id}
        if left:
            self._undeleted[prefix] = left
        else:
            self._undeleted.pop(prefix, None)

    async def _refuse_or_warn_where_the_guest_cannot_write(
        self, spec: SandboxSpec, sandbox: _AcasSandbox | None = None
    ) -> None:
        """Refuse a spec whose guest could not create the files it collects; warn one that only
        runs commands.

        The two planes act as two principals: the file plane writes as root, so every directory
        it creates lands ``0:0 0755``, while ``exec`` runs as the image's ``USER`` and the SDK
        exposes no selector to raise it.  On an image whose guest is not root, a guest program
        can therefore create nothing inside a directory the file plane made — neither a declared
        output beside the files it was given nor the host-tool transport's own markers, which go
        into a call directory the launcher's own upload created.

        Called twice by :meth:`_get_or_create`: once with no sandbox, which answers only from
        what an earlier acquire measured and so refuses before paying for a create, and once
        with the sandbox the probe runs in — where a refused **create** is deleted by the caller
        and a refused **reuse** stays registered for the key's own disposal.  The warning is
        emitted once per image and kind rather than on both of those calls, and rather than on
        every acquire — this method runs on every tool call.

        An image whose uid cannot be read is **served**, and asked only once.  Refusing on an
        unreadable probe would take a working root image off a deployment, where serving it
        costs no more than the failure that happens today; re-probing one would put an `exec`
        round trip, and its timeout, in front of every tool call.

        Raises:
            SandboxCapabilityNotSupported: when the spec requires a capability only a guest that
                can write is able to back.
        """
        if not spec.requires & _PROBE_WHEN_REQUIRED:
            return
        identity = _image_identity(spec)
        if identity in self._guest_uids:
            uid = self._guest_uids[identity]
        elif sandbox is None:
            return
        else:
            uid = await self._probe_guest_uid(sandbox, spec)
        if uid is None or uid == 0:
            return

        image = _image_label(spec)
        refused = spec.requires & _NEEDS_A_WRITING_GUEST
        if refused:
            raise SandboxCapabilityNotSupported(
                f"sandbox backend {BACKEND_NAME!r} cannot serve "
                f"{', '.join(sorted(refused))} to the {spec.kind!r} workload from {image}: its "
                f"guest runs as uid {uid}, and every directory this backend's file plane creates "
                "belongs to root and is writable by nobody else, so the guest program can create "
                "neither a declared output beside the files it was given nor the host-tool "
                "transport's own markers. Refused here rather than inside the tool call, where "
                "it arrives as a shell's 'Permission denied'. Serve this workload on an image "
                "whose USER is root, or narrow what it requires."
            )
        already_warned = (_image_identity(spec), spec.kind)
        if already_warned in self._warned_about_the_guest:
            return
        self._warned_about_the_guest.add(already_warned)
        logger.warning(
            "acas: %s runs its guest as uid %s, so a program the %s workload execs can read "
            "what write_file placed but cannot create any file of its own beside it — every "
            "directory the file plane makes belongs to root. An exec whose whole result is its "
            "stdout is unaffected; anything the guest has to write is not.",
            image,
            uid,
            spec.kind,
        )

    async def _probe_guest_uid(self, sandbox: _AcasSandbox, spec: SandboxSpec) -> int | None:
        """The uid ``exec`` runs as, remembered per image, ``None`` when the guest cannot say.

        The uid belongs to the artefact a reference names rather than to the sandbox booted
        from it, so one round trip on one cold acquire answers for every sandbox after it.  The
        memo is this backend instance's, not the module's — unlike ``_images``' disk-image cache
        — so a host that builds a backend per request pays one probe per request.

        **A failure is remembered too**, as ``None``: an image with no ``id`` would otherwise be
        asked again on every acquire, including the warm reuse the probe used to skip, and each
        ask is a round trip bounded by :data:`_PROBE_TIMEOUT_S`.

        **A failure never displaces an answer.**  Two cold acquires for one image can be in
        flight together — different keys, one backend — so a probe that fails late would
        otherwise overwrite the uid the other established, and ``None`` is served rather than
        refused.  Both failure paths record through ``setdefault`` and return what the memo
        holds, so a late failure yields to the measurement instead of disabling the gate.
        """
        image = _image_label(spec)
        try:
            answered = await sandbox.exec(
                _GUEST_UID_COMMAND,
                working_directory=_GUEST_PROBE_WORKING_DIRECTORY,
                timeout=_PROBE_TIMEOUT_S,
            )
        except Exception as unreachable:  # noqa: BLE001 - an acquire must not fail over this
            logger.debug(
                "acas: %s did not answer %r (%s)",
                image,
                _GUEST_UID_COMMAND,
                error_detail(unreachable),
            )
            return self._guest_uids.setdefault(_image_identity(spec), None)
        reported = answered.stdout.strip()
        if answered.exit_code != 0 or not reported.isdecimal():
            logger.debug(
                "acas: %s answered %r with exit %s and %r, so its guest's uid is unknown",
                image,
                _GUEST_UID_COMMAND,
                answered.exit_code,
                reported,
            )
            return self._guest_uids.setdefault(_image_identity(spec), None)
        uid = int(reported)
        self._guest_uids[_image_identity(spec)] = uid
        return uid

    async def dispose(self, key: SandboxKey) -> DisposalFailure | None:
        """Delete every kind's sandbox for ``key`` that this process knows of.

        Every kind's, because the key may own one sandbox per kind and this method takes no
        kind — a caller releasing a key means all of it.

        Never raises, and reports the reason a sandbox may still be there. Reaching the group
        is part of the delete: a client this process cannot build has deleted nothing. Ids a
        delete could not remove are kept for the next attempt, apart from the registry, which
        :meth:`acquire` resumes from — a sandbox whose delete failed is retried, never served.
        """
        prefix = (key.scope, key.thread_id, key.agent_dir)
        mine = [k for k in list(self._registry) if k[:3] == prefix]
        wanted = list(
            dict.fromkeys(
                [
                    *(sid for sid in (self._registry.pop(k, None) for k in mine) if sid),
                    *sorted(self._undeleted.get(prefix, ())),
                ]
            )
        )
        if not wanted:
            return None
        # Before the first await: the registry no longer holds these and there is no listing
        # to fall back on, so a retry finds them only here. Over-retaining is safe — an id
        # already deleted drops out next attempt. Merged, not assigned: teardown is not
        # serialized.
        self._undeleted[prefix] = self._undeleted.get(prefix, set()) | set(wanted)
        try:
            gc = self._group_client()
        except Exception as exc:  # noqa: BLE001 - disposal must never raise
            logger.warning("acas backend: could not reach the sandbox group: %s", error_detail(exc))
            return DisposalFailure(
                "unreachable", f"could not reach the sandbox group: {error_detail(exc)}"
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
        # Read from the live map, not `wanted`, so an id another disposal recorded survives.
        # No await between the read and the write.
        still = set(undeleted)
        left = (self._undeleted.get(prefix, set()) | still) - (set(wanted) - still)
        if left:
            self._undeleted[prefix] = left
        else:
            self._undeleted.pop(prefix, None)
        reported = fold_disposal_failures(list(undeleted.values()))
        if reported is not None:
            return reported
        if left:
            # A disposal still in flight wrote these ahead of its own await. `None` would
            # clear the refusal on a delete nobody confirmed; `unknown` and a count, since
            # neither the outcome nor the ids are this attempt's to describe.
            return DisposalFailure(
                "unknown", f"another disposal has not yet reported on {len(left)} sandbox(es)"
            )
        return None

    async def dispose_scope(self, scope: str, thread_id: str) -> ScopePurge:
        """Delete every sandbox labelled ``(scope, thread_id)``: how many, and what stayed.

        The registry is consulted first but is **not** the source of truth: it only knows
        what this process created, so a conversation delete served by another replica — or
        by this one after a redeploy — would otherwise leave the sandbox running until the
        auto-delete timer fires.  Labels close that gap by making the service itself, not
        this process's memory, the durable record of which sandboxes belong to a thread.

        Registry entries are dropped up front whether or not the delete succeeds: a stale
        entry pointing at a sandbox that may already be gone is worse than no entry, since
        the next acquire would try to resume it.  An id a previous :meth:`dispose` could not
        delete is swept here too, and stops being owed a retry once this takes it away.

        A listing that failed is reported, not just logged.  The registry still names what this
        process created, so those are deleted — but a purge that could not read the labels
        cannot claim to have reached a sandbox another replica created, which is the very gap
        the labels exist to close.
        """
        known = [
            (k, sandbox_id)
            for k, sandbox_id in list(self._registry.items())
            if k[0] == scope and k[1] == thread_id
        ]
        for k, _ in known:
            self._registry.pop(k, None)

        try:
            gc = self._group_client()
        except Exception as exc:  # noqa: BLE001 - purge must never fail
            logger.warning("acas backend: could not reach the sandbox group: %s", exc)
            # The registry entries are gone by now, so these ids live here or nowhere.
            for key, sandbox_id in known:
                prefix = (key[0], key[1], key[2])
                self._undeleted[prefix] = self._undeleted.get(prefix, set()) | {sandbox_id}
            return ScopePurge(
                0,
                DisposalFailure(
                    "unreachable", f"could not reach the sandbox group: {error_detail(exc)}"
                ),
            )

        retained = {
            p: set(names)
            for p, names in self._undeleted.items()
            if p[0] == scope and p[1] == thread_id
        }
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
        # Merge-only against the *live* map: a `dispose` for one of these keys can land
        # mid-sweep, and indexing what it removed would raise out of a method that never does.
        for prefix, before in retained.items():
            left = self._undeleted.get(prefix, set()) - (before - undeleted)
            if left:
                self._undeleted[prefix] = left
            else:
                self._undeleted.pop(prefix, None)
        # The listing does not say which key owns a failed id, so it is recorded against the
        # registry keys this purge popped rather than lost.
        for key, sandbox_id in known:
            if sandbox_id in undeleted:
                prefix = (key[0], key[1], key[2])
                self._undeleted[prefix] = self._undeleted.get(prefix, set()) | {sandbox_id}
        return ScopePurge(count, fold_disposal_failures(undisposed))

    # -- internals ----------------------------------------------------------------

    def _egress_policy(self, spec: SandboxSpec) -> Any:
        """Deny by default, allow only the hosts the spec names."""
        from azure.containerapps.sandbox import EgressHostRule, EgressPolicy

        return EgressPolicy(
            default_action="Deny",
            traffic_inspection="Full",
            host_rules=[EgressHostRule(pattern=host, action="Allow") for host in spec.egress_allow],
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
            await group_client.get_sandbox_client(sandbox_id).begin_delete()
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
