"""Proxy audit translation tests for Docker."""

from __future__ import annotations

import base64
import json

import pytest
from maf_sandbox import Egress, SandboxSpec

from maf_sandbox_docker._proxy.policy import (
    encoded_policy,
    ipv4_subnets,
    network_gateways,
    read_decisions,
)


def test_audit_reader_keeps_inner_connect_but_skips_tunnel_setup() -> None:
    records = [
        {
            "msg": "request",
            "audit": {"host": "example.com:443", "method": "CONNECT", "action": "allow"},
        },
        {
            "msg": "request",
            "audit": {"host": "example.com:443", "method": "CONNECT", "action": "allow"},
            "tunnel": {"target": "example.com:443"},
        },
        {
            "msg": "request",
            "audit": {"host": "other.test:443", "method": "CONNECT", "action": "reject"},
        },
    ]
    decisions, _ = read_decisions("\n".join(map(json.dumps, records)), 10)
    assert [(item.decision, item.host, item.port) for item in decisions] == [
        ("ALLOW", "example.com", 443),
        ("DENY", "other.test", 443),
    ]


def test_a_named_subnet_without_a_reported_gateway_denies_its_default_gateway() -> None:
    # Engine 28.0.4 reports a caller-named subnet without the gateway its bridge holds.
    ipam = [{"Subnet": "fd42:1407:abcd::/64"}, {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]
    assert network_gateways(ipam) == ("fd42:1407:abcd::1", "172.19.0.1")


def test_only_the_ipv4_subnets_of_the_sandbox_network_are_read() -> None:
    ipam = [{"Subnet": "fd42:1454::/64"}, {"Subnet": "172.20.0.0/16", "Gateway": "172.20.0.1"}]
    assert ipv4_subnets(ipam) == ("172.20.0.0/16",)
    with pytest.raises(ValueError, match="subnet"):
        ipv4_subnets([{"Gateway": "172.20.0.1"}])


def test_the_policy_leaves_the_tunnel_to_the_entrypoint() -> None:
    """An image whose entrypoint does not bind the sandbox network starts no tunnel at all."""
    spec = SandboxSpec(kind="test", image="image", egress=Egress.ALLOWLIST)
    assert "tunnel_listen" not in json.loads(base64.b64decode(encoded_policy(spec)))["proxy"]
