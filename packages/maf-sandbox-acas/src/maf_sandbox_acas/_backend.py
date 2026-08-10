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
from hashlib import sha256
from typing import Any

from maf_sandbox import (
    Capability,
    Egress,
    EntryKind,
    ExecResult,
    Isolation,
    SandboxEntry,
    SandboxKey,
    SandboxLimits,
    SandboxOutputSizeUnknown,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    error_detail,
)

from ._config import AcasSandboxConfig
from ._images import qualify_image_reference, resolve_disk_image_id

logger = logging.getLogger(__name__)

__all__ = ["AcasSandboxBackend"]

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

#: So the ceilings below read as sizes rather than as eight-digit literals.
_MIB = 1024 * 1024

# This backend's own transfer ceilings, per direction: a spec may not ask above them, and the
# router refuses one that does. Set above the protocol's spec-side defaults so a spec that
# says nothing is admitted with room to spare, and well below what a streaming backend can
# offer, because this one cannot stream: the SDK's `read_file` buffers the whole response
# internally, so every byte is host memory before this package sees it and a per-file ceiling
# is a memory statement rather than a transfer-cost one. `max_files` is the field raised
# furthest — this is the only backend that can serve FILES_LIST, so it is where a kind whose
# output names are unpredictable runs, and each of those files is one round trip rather than
# one shared stream.
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

#: The protocol's one path grammar, whatever the guest and the host each run.
_SEPARATOR = "/"
_BACKSLASH = "\\"


def _confined(path: str, working_directory: str) -> tuple[str, str]:
    """Resolve ``path`` against ``working_directory``: the guest path, and the relative one.

    ``posixpath`` only, never ``os.path`` — the protocol has one path grammar and a Windows
    host must not resolve a guest path with its own.  A backslash, or a ``..`` that climbs out
    of ``working_directory``, raises :class:`ValueError`, which ``maf_sandbox`` translates into
    ``SandboxOutputNotConfined`` for the caller.
    """
    if _BACKSLASH in path:
        raise ValueError(f"path {path!r} contains a backslash, which is not a valid separator")
    base = posixpath.normpath(working_directory)
    resolved = posixpath.normpath(posixpath.join(base, path))
    relative = _relative_path(resolved, base)
    if relative is None:
        raise ValueError(f"path {path!r} resolves outside working directory {working_directory!r}")
    return resolved, relative


def _relative_path(guest_path: str, base: str) -> str | None:
    """``guest_path`` relative to ``base``, or ``None`` when it does not sit inside it.

    Compared against ``base + "/"`` rather than ``base``, so a sibling sharing a string prefix
    — ``/work/sub2`` under ``/work/sub`` — is not mistaken for a descendant.
    """
    if guest_path == base:
        return ""
    prefix = base if base.endswith(_SEPARATOR) else base + _SEPARATOR
    if not guest_path.startswith(prefix):
        return None
    return guest_path[len(prefix) :]


def _entry_from_payload(payload: Mapping[str, Any], relative_path: str) -> SandboxEntry:
    """One raw stat payload as a :class:`~maf_sandbox.SandboxEntry`.

    A payload missing either type flag is **refused**, never read as a regular file: those two
    booleans are the whole of this backend's symlink refusal, so a service that stops sending
    them has to break the read loudly rather than degrade confinement to nothing.  ``mode`` is
    not consulted — it carries permission bits only, with no ``S_IFLNK`` or ``S_IFDIR`` in it.
    """
    is_symlink: Any = payload.get(_FIELD_IS_SYMLINK)
    is_dir: Any = payload.get(_FIELD_IS_DIR)
    if not isinstance(is_symlink, bool) or not isinstance(is_dir, bool):
        raise ValueError(
            f"the sandbox service described {relative_path!r} without both {_FIELD_IS_SYMLINK!r} "
            f"and {_FIELD_IS_DIR!r}, so what it is cannot be told. Refused rather than assumed "
            "to be a regular file: this backend's read follows a symlink to whatever it points "
            "at, so an unknown type is a read of the wrong file."
        )
    if is_symlink:
        return SandboxEntry(path=relative_path, kind=EntryKind.OTHER, size_bytes=None)
    if is_dir:
        return SandboxEntry(path=relative_path, kind=EntryKind.DIRECTORY, size_bytes=None)
    return SandboxEntry(path=relative_path, kind=EntryKind.FILE, size_bytes=_size_bytes(payload))


def _size_bytes(payload: Mapping[str, Any]) -> int | None:
    """A regular file's size, or ``None`` when the service reported none.

    ``None`` fails closed upstream, so an absent or non-integer ``size`` is passed through as
    unknown rather than coerced to zero, which would make every cap read that one file as free.
    Only a regular file is measured at all: a symlink's ``size`` is the length of the target
    string, not of anything readable.
    """
    size: Any = payload.get(_FIELD_SIZE)
    # `bool` is an `int`, and `True` would otherwise report as a one-byte file.
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    return size


def _listed_entry_path(payload: Mapping[str, Any], working_directory: str) -> str:
    """Where one listed entry sits, relative to the working directory the call named."""
    reported: Any = payload.get(_FIELD_PATH)
    if not isinstance(reported, str) or not reported:
        raise ValueError("the sandbox service listed an entry with no path")
    _, relative = _confined(reported, working_directory)
    return relative


class _AcasSandbox:
    """A running ACA sandbox, narrowed to what a workload is allowed to do with it."""

    def __init__(self, sandbox_client: Any) -> None:
        self._sc = sandbox_client

    @property
    def sandbox_id(self) -> str:
        return self._sc.sandbox_id

    async def write_file(self, path: str, content: str | bytes) -> None:
        # `create_dirs=True` is the SDK's own default, and it is passed explicitly anyway.
        # A workload may hand us a nested path — `infra/main.bicep` is the example in the
        # bicep tool's own description — and without it every such write fails on a missing
        # parent. The file API docs do not mention the behaviour at all, so it is the SDK
        # signature that is load-bearing here; relying silently on a `0.1.0bN` default is how
        # `DiskImage.image` got missed. Stating it costs nothing and pins the intent.
        await self._sc.write_file(path, content, create_dirs=True)

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

        The SDK's typed ``FileInfo`` is bypassed, and this is the only place that reaches past
        it: it exposes no ``isSymlink`` and no ``symlinkTarget``, and reads ``isDirectory``, a
        key the service does not send — so through the typed surface a symlink and a directory
        alike come back looking like a regular file, and this backend's confinement rule would
        be a no-op.  Tracked as
        `#136 <https://github.com/sokolaidev/maf-extensions/issues/136>`_ and filed upstream as
        `Azure/azure-sdk-for-python#48523
        <https://github.com/Azure/azure-sdk-for-python/issues/48523>`_; when the typed surface
        carries the entry type, this helper and the field constants go with it.
        """
        sc = self._sc
        payload: Mapping[str, Any] = await sc._dp_get(
            f"{sc._sbx_path}/{route}",
            params={_QUERY_PATH: guest_path, _QUERY_API_VERSION: sc._api_version},
        )
        return payload

    async def stat_file(self, path: str, *, working_directory: str) -> SandboxEntry | None:
        """Describe ``path``, or return ``None`` when nothing is there.

        Stat is ``lstat``-like: a symlink is described as itself, never as its target.
        """
        from azure.core.exceptions import ResourceNotFoundError

        guest, relative = _confined(path, working_directory)
        try:
            payload = await self._files_payload(_STAT_ROUTE, guest)
        except ResourceNotFoundError:
            return None
        return _entry_from_payload(payload, relative)

    async def read_file(self, path: str, *, working_directory: str, max_bytes: int) -> bytes:
        """Read the regular file at ``path``, refusing anything over ``max_bytes``.

        It stats first, and that ordering is the confinement rule rather than an optimisation:
        **this backend's read follows symlinks** — reading a path that links to ``/etc/hostname``
        returns that file's contents — so a non-regular entry has to be refused before any byte
        moves, not classified afterwards when the damage is done.

        ``max_bytes`` is a refusal, never a truncation, and it is applied twice.  The stat is a
        promise about a file the guest is still free to rewrite, and the SDK buffers the whole
        response internally rather than exposing an incremental hook, so the second check —
        against what actually arrived — is the only one that bounds what this returns.
        """
        guest, _ = _confined(path, working_directory)
        entry = await self.stat_file(path, working_directory=working_directory)
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
        content: bytes = await self._sc.read_file(guest)
        if len(content) > max_bytes:
            raise SandboxTransferCapExceeded(
                f"{path!r} read back as {len(content)} bytes and the caller allowed {max_bytes}"
            )
        return content

    async def list_dir(self, path: str, *, working_directory: str) -> tuple[SandboxEntry, ...]:
        """Enumerate the entries directly under ``path``.

        Native here, which is why this is the only backend that declares
        :data:`~maf_sandbox.Capability.FILES_LIST`.  Every listed entry is confined the same way
        a declared path is: one naming something outside ``working_directory`` fails the
        listing rather than being reported as a path a caller may go on to read.
        """
        from azure.core.exceptions import ResourceNotFoundError

        guest, _ = _confined(path, working_directory)
        try:
            payload = await self._files_payload(_LIST_ROUTE, guest)
        except ResourceNotFoundError as exc:
            # Translated out of the SDK's vocabulary: a kind catching this would otherwise have
            # to import azure-core to name what it caught.
            raise FileNotFoundError(f"no such directory: {path!r}") from exc
        entries: Sequence[Any] = payload.get(_FIELD_ENTRIES) or ()
        return tuple(
            _entry_from_payload(entry, _listed_entry_path(entry, working_directory))
            for entry in entries
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
        # Group clients cached per event loop. An azure-core async client binds its transport
        # to the loop that created it, and this host runs some work on a dedicated background
        # loop, so one shared client would be a cross-loop hazard; one per call would leak a
        # connection pool per tool invocation.
        self._clients: dict[asyncio.AbstractEventLoop, tuple[Any, Any]] = {}
        # One get-or-create lock per (loop, registry key) — see `_acquire_lock`.
        self._acquire_locks: dict[
            tuple[asyncio.AbstractEventLoop, tuple[str, str, str, str]], asyncio.Lock
        ] = {}

    @property
    def name(self) -> str:
        return "acas"

    @property
    def isolation(self) -> Isolation:
        return Isolation.MICROVM

    @property
    def egress(self) -> Egress:
        # True because `_egress_policy` builds it: Deny default, one Allow per named host.
        return Egress.ALLOWLIST

    @property
    def capabilities(self) -> frozenset[Capability]:
        # FILES_LIST as well as FILES_OUT, which is the split's own test — name the backend
        # that lacks it. Enumeration is native here and unavailable on the backends that
        # transport a named path only.
        return frozenset(
            {
                Capability.EXEC,
                Capability.FILES_IN,
                Capability.FILES_OUT,
                Capability.FILES_LIST,
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

        sandbox_id = self._registry.get(registry_key)
        if sandbox_id is not None:
            try:
                sc = gc.get_sandbox_client(sandbox_id)
                await sc.ensure_running(timeout=_RESUME_TIMEOUT_S)
                logger.info(
                    "sandbox reused: id=%s kind=%s thread=%s agent=%s",
                    sandbox_id,
                    spec.kind,
                    key.thread_id,
                    key.agent_dir,
                )
                return _AcasSandbox(sc)
            except Exception as exc:  # noqa: BLE001 - a dead sandbox is replaced, not reported
                # Not a warning: a sandbox reclaimed by its auto-delete timer between rounds
                # is the expected path, not a fault. But it does mean the next call pays for
                # a cold create, so the reason is worth a line rather than a silent `pass`.
                logger.info(
                    "sandbox %s did not resume (%s); creating a replacement",
                    sandbox_id,
                    error_detail(exc),
                )
            self._registry.pop(registry_key, None)

        # The spec carries repository:tag; this backend knows which registry holds it.
        image = qualify_image_reference(self._config.registry, spec.image or "")
        disk_id = await resolve_disk_image_id(gc, spec.image_id, image or None)
        poller = await gc.begin_create_sandbox(
            disk_id=disk_id,
            labels=_sandbox_labels(key, spec),
            egress_policy=self._egress_policy(spec),
        )
        sc = await poller.result()
        logger.info(
            "sandbox created: id=%s kind=%s disk_image=%s thread=%s agent=%s",
            sc.sandbox_id,
            spec.kind,
            disk_id,
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
        return _AcasSandbox(sc)

    async def dispose(self, key: SandboxKey) -> None:
        """Delete every kind's sandbox for ``key`` that this process knows of.

        Every kind's, because the key may own one sandbox per kind and this method takes no
        kind — a caller releasing a key means all of it.
        """
        prefix = (key.scope, key.thread_id, key.agent_dir)
        mine = [k for k in list(self._registry) if k[:3] == prefix]
        if not mine:
            return
        gc = self._group_client()
        for registry_key in mine:
            sandbox_id = self._registry.pop(registry_key, None)
            if sandbox_id is None:
                continue
            if await self._delete(gc, sandbox_id):
                logger.info(
                    "sandbox released: id=%s thread=%s agent=%s",
                    sandbox_id,
                    key.thread_id,
                    key.agent_dir,
                )

    async def dispose_scope(self, scope: str, thread_id: str) -> int:
        """Delete every sandbox labelled ``(scope, thread_id)``; returns how many.

        The registry is consulted first but is **not** the source of truth: it only knows
        what this process created, so a conversation delete served by another replica — or
        by this one after a redeploy — would otherwise leave the sandbox running until the
        auto-delete timer fires.  Labels close that gap by making the service itself, not
        this process's memory, the durable record of which sandboxes belong to a thread.

        Registry entries are dropped up front whether or not the delete succeeds: a stale
        entry pointing at a sandbox that may already be gone is worse than no entry, since
        the next acquire would try to resume it.
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
            return 0

        ids = {sandbox_id for _, sandbox_id in known}
        ids.update(await self._list_thread_sandbox_ids(gc, scope, thread_id))

        count = 0
        for sandbox_id in sorted(ids):
            if await self._delete(gc, sandbox_id):
                logger.info(
                    "sandbox released: id=%s thread=%s (scope purge)", sandbox_id, thread_id
                )
                count += 1
        return count

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

    async def _delete(self, group_client: Any, sandbox_id: str) -> bool:
        """Best-effort delete. Returns whether it succeeded; never raises."""
        try:
            await group_client.get_sandbox_client(sandbox_id).begin_delete()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "acas backend: failed to delete sandbox %s: %s", sandbox_id, error_detail(exc)
            )
            return False

    async def _list_thread_sandbox_ids(
        self, group_client: Any, scope: str, thread_id: str
    ) -> list[str]:
        """Sandbox ids labelled ``(scope, thread_id)``, read from the service."""
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
        return ids
