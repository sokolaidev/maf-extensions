"""Fresh group admission checks and ownership of both SDK pipelines."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, cast

from azure.containerapps.sandbox import SandboxGroup
from maf_sandbox import SandboxAttachedIdentityNotPermitted

from ._config import AcasSandboxConfig
from ._credentials import AcasClientCloseError


class AcasIdentityVerificationError(SandboxAttachedIdentityNotPermitted):
    """The group could not be verified as ready and free of attached identity."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GroupClients:
    """One leased credential owns data access and lazy management inspection."""

    def __init__(self, data: Any, create_management: Callable[[], Any]) -> None:
        self.data = data
        self.management: Any = None
        self._create_management = create_management

    def __getattr__(self, name: str) -> Any:
        return getattr(self.data, name)

    async def get_group(self, name: str) -> SandboxGroup:
        if self.management is None:
            self.management = self._create_management()
        return await self.management.get_group(name)

    async def close(self) -> None:
        async def close_one(attribute: str) -> None:
            resource = getattr(self, attribute)
            if resource is not None:
                await resource.close()
                setattr(self, attribute, None)

        results = await asyncio.gather(
            close_one("data"), close_one("management"), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise AcasClientCloseError("ACAS SDK pipeline closure is incomplete")


def _check_group(group: object, config: AcasSandboxConfig) -> None:
    expected = (
        f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}"
        f"/providers/Microsoft.App/sandboxGroups/{config.sandbox_group}"
    )
    if (
        not all((config.subscription_id, config.resource_group, config.sandbox_group))
        or not isinstance(group, SandboxGroup)
        or not isinstance(group.id, str)
        or group.id.casefold() != expected.casefold()
        or not isinstance(cast(object, group.name), str)
        or group.name.casefold() != config.sandbox_group.casefold()
        or not isinstance(cast(object, group.properties), Mapping)
        or group.properties.get("provisioningState") != "Succeeded"
    ):
        raise AcasIdentityVerificationError(
            "ACAS group identity response is incomplete or not ready"
        )
    identity = group.identity
    if identity is None:
        return
    if not isinstance(cast(object, identity), Mapping) or identity.get("type") != "None":
        raise AcasIdentityVerificationError("ACAS group has attached or unrecognized identity")
    if (
        set(identity) - {"type", "principalId", "tenantId", "userAssignedIdentities"}
        or identity.get("principalId") is not None
        or identity.get("tenantId") is not None
        or identity.get("userAssignedIdentities", {}) != {}
    ):
        raise AcasIdentityVerificationError("ACAS group identity response is inconsistent")


async def verify_group_identity(client: Any, config: AcasSandboxConfig) -> None:
    """Read under the acquire grant; retain neither successful snapshots nor provider errors."""
    try:
        async with asyncio.timeout(config.identity_check_seconds):
            group = await client.get_group(config.sandbox_group)
            _check_group(group, config)
    except AcasIdentityVerificationError:
        raise
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        raise AcasIdentityVerificationError(
            "ACAS group identity verification failed or timed out; acquisition refused",
            status_code=status if isinstance(status, int) else None,
        ) from None
