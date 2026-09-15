"""HTTP client and local discovery for cooperative MAF control endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ._control import SandboxControl
from ._models import DisposalResult, DisposalStatus, PurgeResult, PurgeStatus, SandboxRecord
from ._server import (
    PROTOCOL_VERSION,
    EndpointManifest,
    ensure_private_runtime_directory,
    runtime_directory,
)

_CONTROL_SETTLEMENT_GRACE = 0.1


class ControlEndpointError(RuntimeError):
    """A discovered endpoint could not answer a control request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PartialInventoryError(ControlEndpointError):
    """Some control endpoints failed while others returned inventory."""

    def __init__(self, records: Sequence[SandboxRecord], errors: Sequence[str]) -> None:
        super().__init__(f"inventory is incomplete on {len(errors)} host(s)")
        self.records = tuple(records)
        self.errors = tuple(errors)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Keep a loopback request from being redirected to another origin."""
        return None


_LOCAL_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())
_TRANSPORT_GRACE = 5.0


class HttpControl:
    """Client for one version one loopback control endpoint."""

    def __init__(self, manifest: EndpointManifest, *, timeout: float = 5.0) -> None:
        self.manifest = manifest
        self._endpoint = manifest.endpoint.removesuffix("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, *, timeout: float | None = None) -> object:
        request = Request(
            f"{self._endpoint}{path}",
            method=method,
            headers={"Accept": "application/json"},
        )
        try:
            with _LOCAL_OPENER.open(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                return cast("object", json.loads(response.read()))
        except HTTPError as error:
            try:
                body = cast("dict[object, object]", json.loads(error.read()))
                message = body.get("error") or body.get("message")
            except (ValueError, AttributeError):
                message = None
            raise ControlEndpointError(
                str(message) if message else f"control endpoint returned HTTP {error.code}",
                status_code=error.code,
            ) from error
        except (OSError, URLError, ValueError) as error:
            raise ControlEndpointError(f"control endpoint is unavailable: {error}") from error

    async def _joined_request(
        self, method: str, path: str, *, timeout: float | None = None
    ) -> object:
        """Run one blocking request without abandoning a mutating transport on cancellation."""
        task = asyncio.create_task(asyncio.to_thread(self._request, method, path, timeout=timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled to_thread await does not stop urllib; join it before reporting completion.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise

    async def health(self) -> None:
        """Require a compatible local endpoint."""
        value = await asyncio.to_thread(self._request, "GET", "/v1/health")
        if not isinstance(value, dict):
            raise ControlEndpointError("control endpoint returned an incompatible health record")
        data = cast("dict[object, object]", value)
        version = data.get("protocol_version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != PROTOCOL_VERSION
            or (
                self.manifest.process_id is not None
                and data.get("source_id") != self.manifest.source_id
            )
        ):
            raise ControlEndpointError("control endpoint returned an incompatible health record")

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Read the endpoint's current authoritative snapshot."""
        value = await asyncio.to_thread(
            self._request,
            "GET",
            f"/v1/sandboxes?timeout={self.timeout}",
            timeout=self.timeout + _TRANSPORT_GRACE,
        )
        if not isinstance(value, dict):
            raise ControlEndpointError("control endpoint returned an invalid inventory")
        data = cast("dict[object, object]", value)
        inventory = data.get("sandboxes")
        if not isinstance(inventory, list):
            raise ControlEndpointError("control endpoint returned an invalid inventory")
        return tuple(SandboxRecord.from_json(item) for item in cast("list[object]", inventory))

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        """Read one exact physical instance from this endpoint."""
        try:
            value = await asyncio.to_thread(
                self._request,
                "GET",
                f"/v1/sandboxes/{quote(instance_id, safe='')}?timeout={self.timeout}",
                timeout=self.timeout + _TRANSPORT_GRACE,
            )
        except ControlEndpointError as error:
            if error.status_code == 404:
                return None
            raise
        record = SandboxRecord.from_json(value)
        if record.instance_id != instance_id:
            raise ControlEndpointError("control endpoint returned a different sandbox instance")
        return record

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Ask the owning MAF process to dispose one exact instance."""
        try:
            value = await self._joined_request(
                "DELETE",
                f"/v1/sandboxes/{quote(instance_id, safe='')}?timeout={timeout}",
                timeout=timeout + _TRANSPORT_GRACE,
            )
        except ControlEndpointError as error:
            return DisposalResult(DisposalStatus.FAILED, instance_id, str(error))
        result = DisposalResult.from_json(value)
        if result.instance_id != instance_id:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                "Control endpoint returned a result for a different sandbox instance.",
            )
        return result

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        """Ask this endpoint to purge every sandbox for one conversation."""
        value = await self._joined_request(
            "DELETE",
            (
                f"/v1/scopes/{quote(scope, safe='')}/threads/"
                f"{quote(thread_id, safe='')}?timeout={timeout}"
            ),
            timeout=timeout + _TRANSPORT_GRACE,
        )
        result = PurgeResult.from_json(value)
        if (result.scope, result.thread_id) != (scope, thread_id):
            raise ControlEndpointError(
                "control endpoint returned a result for another conversation"
            )
        return result


class CompositeControl:
    """Combine several independently owned MAF processes for one console."""

    def __init__(
        self,
        controls: Sequence[SandboxControl],
        initial_errors: Sequence[str] = (),
    ) -> None:
        self._controls = tuple(controls)
        self._initial_errors = tuple(initial_errors)
        self._unsettled_operations: set[asyncio.Task[Any]] = set()

    def _finish_operation(self, task: asyncio.Task[Any]) -> None:
        self._unsettled_operations.discard(task)
        # Observe late failures so the event loop does not report lost task exceptions.
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    async def _wait_bounded(
        self, tasks: tuple[asyncio.Task[Any], ...], *, timeout: float
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        if not tasks:
            return set(), set()
        for task in tasks:
            self._unsettled_operations.add(task)
            task.add_done_callback(self._finish_operation)
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=_CONTROL_SETTLEMENT_GRACE)
            return done, pending
        except BaseException:
            for task in tasks:
                task.cancel()
            raise

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Return every responsive endpoint's current inventory."""
        snapshots = await asyncio.gather(
            *(control.list_sandboxes() for control in self._controls), return_exceptions=True
        )
        records: list[SandboxRecord] = []
        errors = list(self._initial_errors)
        for snapshot in snapshots:
            if isinstance(snapshot, BaseException):
                errors.append(str(snapshot))
            else:
                records.extend(snapshot)
        ordered = tuple(
            sorted(records, key=lambda item: (item.source_id, item.logical_name, item.kind))
        )
        if errors:
            raise PartialInventoryError(ordered, errors)
        return ordered

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        """Find one exact instance across responsive endpoints."""
        matches = await asyncio.gather(
            *(control.get_sandbox(instance_id) for control in self._controls),
            return_exceptions=True,
        )
        records = [item for item in matches if isinstance(item, SandboxRecord)]
        errors = list(self._initial_errors)
        errors.extend(str(item) for item in matches if isinstance(item, BaseException))
        if len(records) > 1:
            raise ControlEndpointError(f"instance id {instance_id!r} is reported by several hosts")
        if not records and errors:
            raise ControlEndpointError(
                f"sandbox presence could not be confirmed on {len(errors)} unavailable host(s)"
            )
        return records[0] if records else None

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Route exact-instance disposal to the endpoint currently reporting it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        locating_timeout = DisposalResult(
            DisposalStatus.FAILED,
            instance_id,
            "Disposal timed out while locating the owning host.",
        )
        probes = tuple(asyncio.create_task(control.list_sandboxes()) for control in self._controls)
        _, pending = await self._wait_bounded(probes, timeout=timeout)
        if pending:
            return locating_timeout
        owners: list[SandboxControl] = []
        errors = list(self._initial_errors)
        for control, probe in zip(self._controls, probes, strict=True):
            try:
                snapshot = probe.result()
            except (asyncio.CancelledError, Exception) as error:
                errors.append(str(error))
            else:
                if any(item.instance_id == instance_id for item in snapshot):
                    owners.append(control)
        if len(owners) > 1:
            raise ControlEndpointError(f"instance id {instance_id!r} is reported by several hosts")
        if not owners:
            if errors:
                return DisposalResult(
                    DisposalStatus.FAILED,
                    instance_id,
                    f"Sandbox owner could not be confirmed on {len(errors)} unavailable host(s).",
                )
            return DisposalResult(
                DisposalStatus.NOT_FOUND,
                instance_id,
                "The sandbox is already gone or its generation changed.",
            )
        if errors:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                f"Sandbox ownership could not be confirmed on {len(errors)} unavailable host(s).",
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            return locating_timeout

        async def dispose_before_deadline() -> DisposalResult:
            owner_remaining = deadline - loop.time()
            if owner_remaining <= 0:
                return locating_timeout
            return await owners[0].dispose_sandbox(instance_id, timeout=owner_remaining)

        disposing = asyncio.create_task(dispose_before_deadline())
        _, pending = await self._wait_bounded((disposing,), timeout=remaining)
        if pending:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                "Disposal timed out while invoking the owning host.",
            )
        try:
            return disposing.result()
        except TimeoutError:
            return DisposalResult(
                DisposalStatus.FAILED,
                instance_id,
                "Disposal timed out while invoking the owning host.",
            )

    async def purge_thread(
        self, scope: str, thread_id: str, *, timeout: float = 10.0
    ) -> PurgeResult:
        """Ask every responsive endpoint to purge one conversation."""
        if not self._controls:
            return PurgeResult(
                PurgeStatus.PARTIAL,
                scope,
                thread_id,
                0,
                "No responsive MAF host could confirm the purge.",
            )
        tasks = tuple(
            asyncio.create_task(control.purge_thread(scope, thread_id, timeout=timeout))
            for control in self._controls
        )
        done, pending = await self._wait_bounded(tasks, timeout=timeout)
        results: list[PurgeResult | BaseException] = []
        for task in done:
            try:
                results.append(task.result())
            except (asyncio.CancelledError, Exception) as error:
                results.append(error)
        disposed = sum(item.disposed for item in results if isinstance(item, PurgeResult))
        failures = list(self._initial_errors)
        for item in results:
            if isinstance(item, BaseException):
                failures.append(str(item))
            elif item.status is PurgeStatus.PARTIAL:
                failures.append(item.message)
        if pending:
            suffix = f"; {len(failures)} other host failure(s) were reported" if failures else ""
            return PurgeResult(
                PurgeStatus.PARTIAL,
                scope,
                thread_id,
                disposed,
                f"Conversation purge timed out before {len(pending)} host(s) responded{suffix}.",
            )
        if failures:
            return PurgeResult(
                PurgeStatus.PARTIAL,
                scope,
                thread_id,
                disposed,
                f"Conversation purge was incomplete on {len(failures)} host(s).",
            )
        return PurgeResult(
            PurgeStatus.PURGED,
            scope,
            thread_id,
            disposed,
            f"Conversation purged across {len(results)} host(s).",
        )


def read_manifests(directory: Path | None = None) -> tuple[EndpointManifest, ...]:
    """Read valid discovery records without trusting filenames or stale content."""
    root = directory or runtime_directory()
    if not ensure_private_runtime_directory(root, create=False):
        return ()
    manifests: list[EndpointManifest] = []
    for path in sorted(root.glob("*.json")):
        try:
            manifest = EndpointManifest.from_json(json.loads(path.read_text("utf-8")))
            if manifest.process_id is not None:
                manifests.append(manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return tuple(manifests)


async def discover_controls(directory: Path | None = None) -> CompositeControl:
    """Return clients for responsive local endpoints explicitly enabled by their hosts."""
    clients = [HttpControl(manifest) for manifest in read_manifests(directory)]
    healthy: list[HttpControl] = []
    errors: list[str] = []
    results = await asyncio.gather(*(client.health() for client in clients), return_exceptions=True)
    for client, result in zip(clients, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"{client.manifest.source_id}: {result}")
        else:
            healthy.append(client)
    return CompositeControl(cast("Sequence[SandboxControl]", healthy), errors)
