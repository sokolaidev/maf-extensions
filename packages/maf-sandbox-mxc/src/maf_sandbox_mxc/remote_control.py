"""Trusted generation-fencing endpoint, deployed separately from the worker API."""

from __future__ import annotations

import re
import ssl
from collections.abc import Callable, Mapping
from typing import Any

from maf_sandbox_mxc._records import is_record

from .host_store import Refused
from .remote_api import Client, _fields, _text  # pyright: ignore[reportPrivateUsage]
from .remote_authority import Authority, Fence, Owner
from .remote_host import Host


class Controller:
    """Map controller certificates to explicitly provisioned generation journals."""

    def __init__(
        self,
        hosts: Mapping[str, Host],
        grants: Mapping[str, frozenset[str]],
        *,
        enabled: bool = False,
        automatic_shutdown: bool = False,
        boundary: Callable[[str], None] = lambda _: None,
    ):
        if enabled is not True:
            raise Refused("host controller requires explicit opt-in")
        if type(automatic_shutdown) is not bool:
            raise Refused("automatic shutdown requires an explicit boolean")
        if not hosts or not grants or len(hosts) > 128 or len(grants) > 128:
            raise Refused("invalid controller configuration size")
        if any(host.owner is None or token != host.owner.token for token, host in hosts.items()):
            raise Refused("controller requires generation-bound journals")
        if len({host.root.resolve() for host in hosts.values()}) != len(hosts):
            raise Refused("controller generations require separate journal roots")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", key) or not tokens or not tokens <= hosts.keys()
            for key, tokens in grants.items()
        ):
            raise Refused("invalid controller certificate grant")
        self.hosts = dict(hosts)
        self.grants = {key: frozenset(tokens) for key, tokens in grants.items()}
        self.automatic_shutdown = automatic_shutdown
        self.boundary = boundary

    def dispatch(
        self, fingerprint: str, request: dict[str, Any], connection: ssl.SSLSocket | None = None
    ) -> dict[str, Any]:
        """Authorize the exact owner before touching its local journal."""
        _fields(request, {"version", "operation", "session", "arguments"})
        if type(request["version"]) is not int or request["version"] != 1:
            raise Refused("unsupported controller API version")
        if request["operation"] != "fence":
            raise Refused("unsupported controller operation")
        args = request["arguments"]
        if not is_record(args):
            raise Refused("controller arguments must be an object")
        _fields(args, {"owner", "fence"})
        owner = Authority._decode_owner(_text(args["owner"]))  # pyright: ignore[reportPrivateUsage]
        fence = Authority._decode_fence(_text(args["fence"]))  # pyright: ignore[reportPrivateUsage]
        if owner.token not in self.grants.get(fingerprint, frozenset()):
            raise Refused("certificate is not authorized for this generation")
        host = self.hosts[owner.token]
        if host.owner != owner or request["session"] != owner.session:
            raise Refused("controller generation binding differs")
        if fence.automatic and not self.automatic_shutdown:
            raise Refused("automatic shutdown is disabled")
        if host.fence(fence, self.boundary) is not True:
            raise Refused("generation termination is not confirmed")
        return {"owner": Authority._encode(owner), "fence": Authority._encode(fence)}  # pyright: ignore[reportPrivateUsage]


class Verifier:
    """Use a configured TLS endpoint and require its complete owner/request binding."""

    def __init__(self, client: Client, owner: Owner):
        self.client, self.owner = client, owner

    def __call__(self, request: Fence) -> bool:
        if request.resource != self.owner.resource or request.sessions != (self.owner.session,):
            raise Refused("controller request differs from configured owner")
        expected = {"owner": Authority._encode(self.owner), "fence": Authority._encode(request)}  # pyright: ignore[reportPrivateUsage]
        response = self.client.call("fence", self.owner.session, **expected)
        if response != expected:
            raise Refused("controller response binding differs")
        return True
