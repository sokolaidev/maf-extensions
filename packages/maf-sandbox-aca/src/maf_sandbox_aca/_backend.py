"""The ACA Sandboxes backend: :class:`~maf_sandbox.SandboxBackend` on Azure.

Everything provider-specific lives here — the group client, disk-image resolution, the
egress policy, the lifecycle policy, the sandbox registry and label-based purge.  A workload
above the router sees only ``write_file`` and ``exec``.

Isolation is :data:`~maf_sandbox.Isolation.VM`: execution leaves the host process
entirely, the host keeps the control-plane credential and never puts one inside, and egress
is Deny-default with a per-spec allowlist.  That declaration is what the router checks
before permitting this backend in a deployed environment.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from maf_sandbox import Egress, ExecResult, Isolation, SandboxKey, SandboxSpec, error_detail

from ._config import AcaSandboxConfig
from ._images import qualify_image_reference, resolve_disk_image_id

logger = logging.getLogger(__name__)

__all__ = ["AcaSandboxBackend"]

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
        **{k: _label_value(v) for k, v in spec.labels.items()},
    }


# Sandbox labels.  Written at create time and read back on purge, so the *service* — not
# this process's memory — is the durable record of which sandboxes belong to a thread.
_LABEL_SCOPE = "scope"
_LABEL_THREAD = "thread"
_LABEL_AGENT = "agent"

# How long to wait for a warm sandbox to come back from suspension before giving up on it
# and creating a fresh one.
#
# 120 to match the value the lifecycle documentation uses in its own example
# (`wait_for_running(timeout=120)`). It was 60, which is the wrong direction to be wrong in:
# the timeout does not fail the call, it abandons a healthy suspended sandbox and pays a
# cold create instead — slower for the user and more expensive, with nothing in the logs
# saying why. Waiting longer costs only the wait.
_RESUME_TIMEOUT_S = 120


class _AcaSandbox:
    """A running ACA sandbox, narrowed to what a workload is allowed to do with it."""

    def __init__(self, sandbox_client: Any) -> None:
        self._sc = sandbox_client

    @property
    def sandbox_id(self) -> str:
        return self._sc.sandbox_id

    async def write_file(self, path: str, content: str) -> None:
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


class AcaSandboxBackend:
    """Hands out VM-isolated sandboxes from an Azure Container Apps sandbox group."""

    def __init__(self, config: AcaSandboxConfig) -> None:
        self._config = config
        # (scope, thread_id, agent_dir) -> sandbox_id, for this process only.
        # Keyed on scope so sandboxes from one user's session cannot be reused or deleted by
        # a request in another's.  `dispose_scope` treats this as a fast path, never as the
        # source of truth — see its docstring.
        self._registry: dict[tuple[str, str, str], str] = {}
        # Group clients cached per event loop. An azure-core async client binds its transport
        # to the loop that created it, and this host runs some work on a dedicated background
        # loop, so one shared client would be a cross-loop hazard; one per call would leak a
        # connection pool per tool invocation.
        self._clients: dict[asyncio.AbstractEventLoop, tuple[Any, Any]] = {}
        # One get-or-create lock per (loop, registry key) — see `_acquire_lock`.
        self._acquire_locks: dict[
            tuple[asyncio.AbstractEventLoop, tuple[str, str, str]], asyncio.Lock
        ] = {}

    @property
    def name(self) -> str:
        return "aca"

    @property
    def isolation(self) -> str:
        return Isolation.VM

    @property
    def egress(self) -> str:
        # True because `_egress_policy` builds it: Deny default, one Allow per named host.
        return Egress.ALLOWLIST

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
                    logger.debug("aca backend: error closing %s: %s", type(closeable).__name__, exc)
        self._clients.clear()

    # -- SandboxBackend -----------------------------------------------------------

    async def acquire(self, key: SandboxKey, spec: SandboxSpec) -> _AcaSandbox:
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
        async with self._acquire_lock((key.scope, key.thread_id, key.agent_dir)):
            return await self._get_or_create(key, spec)

    def _acquire_lock(self, registry_key: tuple[str, str, str]) -> asyncio.Lock:
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

    async def _get_or_create(self, key: SandboxKey, spec: SandboxSpec) -> _AcaSandbox:
        """:meth:`acquire`'s body, run under that key's lock."""
        gc = self._group_client()
        registry_key = (key.scope, key.thread_id, key.agent_dir)

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
                return _AcaSandbox(sc)
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
                "aca backend: failed to configure lifecycle policy for sandbox %s; "
                "it will be reclaimed by the auto-delete timer",
                sc.sandbox_id,
            )
        return _AcaSandbox(sc)

    async def dispose(self, key: SandboxKey) -> None:
        """Delete the sandbox for ``key``, if this process knows of one."""
        sandbox_id = self._registry.pop((key.scope, key.thread_id, key.agent_dir), None)
        if sandbox_id is None:
            return
        if await self._delete(self._group_client(), sandbox_id):
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
            logger.warning("aca backend: could not reach the sandbox group: %s", exc)
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
                "aca backend: failed to delete sandbox %s: %s", sandbox_id, error_detail(exc)
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
                "aca backend: could not list sandboxes for thread %s: %s",
                thread_id,
                error_detail(exc),
            )
        return ids
