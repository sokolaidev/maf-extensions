"""HTTP client and local discovery for cooperative MAF control endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ._control import SandboxControl
from ._models import DisposalResult, DisposalStatus, PurgeResult, PurgeStatus, SandboxRecord
from ._server import EndpointManifest, runtime_directory


class ControlEndpointError(RuntimeError):
    """A discovered endpoint could not answer a control request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Keep a loopback request from being redirected to another origin."""
        return None


_LOCAL_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


class HttpControl:
    """Client for one version one loopback control endpoint."""

    def __init__(self, manifest: EndpointManifest, *, timeout: float = 5.0) -> None:
        self.manifest = manifest
        self.timeout = timeout

    def _request(self, method: str, path: str, *, timeout: float | None = None) -> object:
        request = Request(
            f"{self.manifest.endpoint}{path}",
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

    async def health(self) -> None:
        """Require a compatible local endpoint."""
        value = await asyncio.to_thread(self._request, "GET", "/v1/health")
        if not isinstance(value, dict):
            raise ControlEndpointError("control endpoint returned an incompatible health record")
        data = cast("dict[object, object]", value)
        if data.get("protocol_version") != 1 or (
            self.manifest.process_id != 0 and data.get("source_id") != self.manifest.source_id
        ):
            raise ControlEndpointError("control endpoint returned an incompatible health record")

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Read the endpoint's current authoritative snapshot."""
        value = await asyncio.to_thread(self._request, "GET", "/v1/sandboxes")
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
                f"/v1/sandboxes/{quote(instance_id, safe='')}",
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
            value = await asyncio.to_thread(
                self._request,
                "DELETE",
                f"/v1/sandboxes/{quote(instance_id, safe='')}?timeout={timeout}",
                timeout=timeout + 5.0,
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
        value = await asyncio.to_thread(
            self._request,
            "DELETE",
            (
                f"/v1/scopes/{quote(scope, safe='')}/threads/"
                f"{quote(thread_id, safe='')}?timeout={timeout}"
            ),
            timeout=timeout + 5.0,
        )
        result = PurgeResult.from_json(value)
        if (result.scope, result.thread_id) != (scope, thread_id):
            raise ControlEndpointError(
                "control endpoint returned a result for another conversation"
            )
        return result


class CompositeControl:
    """Combine several independently owned MAF processes for one console."""

    def __init__(self, controls: Sequence[SandboxControl]) -> None:
        self._controls = tuple(controls)

    async def list_sandboxes(self) -> tuple[SandboxRecord, ...]:
        """Return every responsive endpoint's current inventory."""
        if not self._controls:
            return ()
        snapshots = await asyncio.gather(
            *(control.list_sandboxes() for control in self._controls), return_exceptions=True
        )
        records: list[SandboxRecord] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, BaseException):
                records.extend(snapshot)
        return tuple(
            sorted(records, key=lambda item: (item.source_id, item.logical_name, item.kind))
        )

    async def get_sandbox(self, instance_id: str) -> SandboxRecord | None:
        """Find one exact instance across responsive endpoints."""
        matches = await asyncio.gather(
            *(control.get_sandbox(instance_id) for control in self._controls),
            return_exceptions=True,
        )
        records = [item for item in matches if isinstance(item, SandboxRecord)]
        if len(records) > 1:
            raise ControlEndpointError(f"instance id {instance_id!r} is reported by several hosts")
        return records[0] if records else None

    async def dispose_sandbox(self, instance_id: str, *, timeout: float = 10.0) -> DisposalResult:
        """Route exact-instance disposal to the endpoint currently reporting it."""
        for control in self._controls:
            try:
                if any(item.instance_id == instance_id for item in await control.list_sandboxes()):
                    return await control.dispose_sandbox(instance_id, timeout=timeout)
            except ControlEndpointError:
                continue
        return DisposalResult(
            DisposalStatus.NOT_FOUND,
            instance_id,
            "The sandbox is already gone or its owner is unavailable.",
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
        results = await asyncio.gather(
            *(
                control.purge_thread(scope, thread_id, timeout=timeout)
                for control in self._controls
            ),
            return_exceptions=True,
        )
        disposed = sum(item.disposed for item in results if isinstance(item, PurgeResult))
        failures: list[str] = []
        for item in results:
            if isinstance(item, BaseException):
                failures.append(str(item))
            elif item.status is PurgeStatus.PARTIAL:
                failures.append(item.message)
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
    if not root.is_dir():
        return ()
    manifests: list[EndpointManifest] = []
    for path in sorted(root.glob("*.json")):
        try:
            manifests.append(EndpointManifest.from_json(json.loads(path.read_text("utf-8"))))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return tuple(manifests)


async def discover_controls(directory: Path | None = None) -> CompositeControl:
    """Return clients for responsive local endpoints explicitly enabled by their hosts."""
    clients = [HttpControl(manifest) for manifest in read_manifests(directory)]
    healthy: list[HttpControl] = []
    results = await asyncio.gather(*(client.health() for client in clients), return_exceptions=True)
    for client, result in zip(clients, results, strict=True):
        if not isinstance(result, BaseException):
            healthy.append(client)
    return CompositeControl(cast("Sequence[SandboxControl]", healthy))
