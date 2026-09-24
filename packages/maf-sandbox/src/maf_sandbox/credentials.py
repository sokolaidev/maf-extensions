"""Host-authorized, finite credential grants for external HTTP gateways.

The provider runs in the trusted host. It must authorize the complete request context;
neither a model-supplied audience nor possession of a sandbox key is authorization.
"""

from __future__ import annotations

import ipaddress
import json
import math
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ._protocol import (
    AttachedIdentity,
    AuthorityChannel,
    EgressRule,
    IdentityScope,
    SandboxKey,
    SandboxSpec,
)


@dataclass(frozen=True)
class CredentialRequest:
    """One trusted ownership boundary and its newly created runtime generation.

    ``key.scope`` must include both tenant and user when those are distinct boundaries.
    ``expires_at`` is the absolute Unix deadline for this nonrenewable generation.
    """

    key: SandboxKey
    kind: str
    instance_id: str
    generation: str
    audiences: frozenset[str]
    expires_at: float


@dataclass(frozen=True)
class CredentialGrant:
    """A bearer token usable only at one exact HTTPS origin until a Unix deadline.

    No token is installed in guest files, environment variables, or process arguments.
    The authorized upstream receives it and must be trusted not to disclose it.
    """

    audience: str
    origin: str
    token: str = field(repr=False)
    expires_at: float

    def __post_init__(self) -> None:
        url = urlsplit(self.origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or url.port == 0
        ):
            raise ValueError("a credential origin must be an exact HTTPS origin")
        if not self.audience or any(c.isspace() for c in self.audience):
            raise ValueError("a credential audience must be nonempty without whitespace")
        if (
            not self.token
            or len(self.token) > 16384
            or any(ord(c) < 33 or ord(c) > 126 for c in self.token)
        ):
            raise ValueError("a credential must be a bounded printable bearer token")
        if not math.isfinite(self.expires_at) or self.expires_at <= time.time():
            raise ValueError("a credential must have a future finite expiry")


@dataclass(frozen=True)
class CredentialGateway:
    """Opt a backend into host-issued bearer credentials with a hard orphan lifetime.

    The provider authorizes each user, agent, call, audience, and runtime instance afresh.
    Each authority rule needs a unique audience, with exactly one grant per audience.
    No cache, refresh credential, or cross-replica grant store is shared by this library.
    Expiry requires a new tool call.
    """

    provider: Callable[[CredentialRequest], Awaitable[Sequence[CredentialGrant]]] = field(
        repr=False
    )
    max_lifetime_seconds: int = 300

    def __post_init__(self) -> None:
        if not callable(self.provider):
            raise TypeError("credential provider must be callable")
        if type(self.max_lifetime_seconds) is not int or not 1 <= self.max_lifetime_seconds <= 3600:
            raise ValueError("credential lifetime must be an integer from 1 to 3600 seconds")

    @property
    def attached_identity(self) -> AttachedIdentity:
        """The authority and orphan bound this configuration requires a host to permit."""
        return AttachedIdentity(
            IdentityScope.PER_SANDBOX,
            self.max_lifetime_seconds,
            frozenset({AuthorityChannel.EGRESS_HEADER}),
        )


@dataclass(frozen=True)
class GatewayLease:
    """Backend helper for a fresh, nonrenewable gateway generation."""

    generation: str
    expires_at: float

    @classmethod
    def start(
        cls, gateway: CredentialGateway | None, key: SandboxKey, spec: SandboxSpec
    ) -> GatewayLease | None:
        """Refuse unsupported authority before provisioning any resources."""
        if not spec.authority_channels:
            return None
        audiences = [
            entry.authority
            for entry in spec.egress_allow
            if isinstance(entry, EgressRule) and entry.authority is not None
        ]
        if len(set(audiences)) != len(audiences):
            raise ValueError("credential authority rules require a unique audience per rule")
        if gateway is None:
            raise ValueError("attached egress authority requires a credential gateway")
        if not all((key.scope, key.thread_id, key.agent_id, key.call_id)):
            raise ValueError("credential gateways require a trusted scope, agent, and call key")
        if (
            spec.max_identity_retention_seconds is None
            or gateway.max_lifetime_seconds > spec.max_identity_retention_seconds
        ):
            raise ValueError("credential gateway lifetime exceeds the workload's bound")
        return cls(secrets.token_hex(16), time.time() + gateway.max_lifetime_seconds)

    async def payload(
        self,
        gateway: CredentialGateway,
        key: SandboxKey,
        spec: SandboxSpec,
        instance_id: str,
        peer: str,
        boot: str,
    ) -> bytes:
        """Authorize this instance and serialize its least-privilege proxy installation."""
        ipaddress.ip_address(peer)
        if not instance_id or len(boot) != 48 or any(c not in "0123456789abcdef" for c in boot):
            raise ValueError("gateway installation requires a verified instance and boot identity")
        rules = tuple(
            entry
            for entry in spec.egress_allow
            if isinstance(entry, EgressRule) and entry.authority is not None
        )
        audiences = frozenset(entry.authority for entry in rules if entry.authority is not None)
        request = CredentialRequest(
            key, spec.kind, instance_id, self.generation, audiences, self.expires_at
        )
        try:
            grants = tuple(await gateway.provider(request))
        except Exception:
            raise RuntimeError("credential provider refused the gateway grant") from None
        by_audience = {grant.audience: grant for grant in grants}
        if len(by_audience) != len(grants) or by_audience.keys() != audiences:
            raise ValueError("credential provider must return exactly the requested audiences")
        entries: list[dict[str, object]] = []
        for rule in rules:
            assert rule.authority is not None
            grant = by_audience[rule.authority]
            origin = urlsplit(grant.origin)
            if origin.hostname != rule.host.lower():
                raise ValueError("credential origin does not match its authority rule")
            expires = min(grant.expires_at, self.expires_at)
            if expires <= time.time():
                raise ValueError("credential expired during gateway provisioning")
            entries.append(
                {
                    "host": rule.host.lower(),
                    "port": origin.port or 443,
                    "methods": sorted(rule.methods) if rule.methods is not None else [],
                    "paths": list(rule.paths) if rule.paths is not None else [],
                    "token": grant.token,
                    "expires_at": expires,
                }
            )
        payload = json.dumps(
            {"boot": boot, "peer": peer, "expires_at": self.expires_at, "entries": entries},
            separators=(",", ":"),
        ).encode()
        if len(payload) > 1048576:
            raise ValueError("credential grant exceeds the gateway installation limit")
        return payload
