"""The policy handed to the pinned iron-proxy image."""

from __future__ import annotations

import base64
import json

from maf_sandbox import Egress, EgressRule, SandboxSpec

from maf_sandbox_wslc._proxy import build_context
from maf_sandbox_wslc._proxy.policy import encoded_policy, network_gateways, read_decisions


def test_scoped_rules_are_serialized_for_iron_proxy() -> None:
    spec = SandboxSpec(
        kind="test",
        image="image",
        egress=Egress.ALLOWLIST,
        egress_allow=(
            "mcr.microsoft.com",
            EgressRule("api.example.com", methods=("GET",), paths=("/v1/*",)),
        ),
    )
    policy = json.loads(base64.b64decode(encoded_policy(spec)))
    allowlist = policy["transforms"][0]["config"]
    assert allowlist == {
        "domains": ["mcr.microsoft.com"],
        "rules": [{"host": "api.example.com", "methods": ["GET"], "paths": ["/v1/*"]}],
    }
    assert policy["tls"] == {
        "mode": "mitm",
        "ca_cert": "/run/maf-proxy/ca.crt",
        "ca_key": "/run/maf-proxy/ca.key",
    }
    assert "169.254.0.0/16" in policy["proxy"]["upstream_deny_cidrs"]
    assert "100.100.100.200/32" in policy["proxy"]["upstream_deny_cidrs"]
    assert "168.63.129.16/32" in policy["proxy"]["upstream_deny_cidrs"]
    assert "::1/128" in policy["proxy"]["upstream_deny_cidrs"]


def test_inspected_gateways_are_denied_without_denying_all_private_addresses() -> None:
    addresses = network_gateways([{"Gateway": "172.17.0.1"}, {"Gateway": "fd42:1407::1"}])
    spec = SandboxSpec(kind="test", image="image", egress=Egress.ALLOWLIST)
    policy = json.loads(base64.b64decode(encoded_policy(spec, control_addresses=addresses)))
    denied = policy["proxy"]["upstream_deny_cidrs"]
    assert "172.17.0.1/32" in denied
    assert "fd42:1407::1/128" in denied
    assert "172.17.0.0/16" not in denied


def test_a_named_subnet_without_a_reported_gateway_denies_its_default_gateway() -> None:
    # Engine 28.0.4 reports a caller-named subnet without the gateway its bridge holds.
    ipam = [{"Subnet": "fd42:1407:abcd::/64"}, {"Subnet": "172.19.0.0/16", "Gateway": "172.19.0.1"}]
    assert network_gateways(ipam) == ("fd42:1407:abcd::1", "172.19.0.1")


def test_public_plaintext_error_is_a_denial() -> None:
    record = {
        "msg": "request",
        "audit": {"host": "example.com", "method": "GET", "action": "error"},
        "error": "plaintext HTTP is disabled",
    }
    decisions, truncated = read_decisions(json.dumps(record), 10)
    assert [(item.decision, item.host, item.port) for item in decisions] == [
        ("DENY", "example.com", 80)
    ]
    assert not truncated


def test_audit_reader_keeps_request_ports_and_classifies_address_denials() -> None:
    records = [
        {
            "msg": "request",
            "audit": {"host": "example.com:1012", "method": "CONNECT", "action": "allow"},
        },
        {
            "msg": "request",
            "audit": {"host": "example.com:1012", "method": "GET", "action": "allow"},
        },
        {
            "msg": "request",
            "audit": {"host": "private.test:8443", "method": "GET", "action": "error"},
            "error": "proxy interface address is not an upstream",
        },
    ]
    decisions, truncated = read_decisions("\n".join(map(json.dumps, records)), 10)
    assert [(item.decision, item.host, item.port) for item in decisions] == [
        ("ALLOW", "example.com", 1012),
        ("DENY", "private.test", 8443),
    ]
    assert not truncated


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


def test_packaged_context_pins_the_proxy_and_carries_its_patch() -> None:
    context = build_context()
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "5bd11abeb95ca734c767cfc992ea9be862700614" in dockerfile
    assert "COPY iron.patch" in dockerfile
    assert (context / "iron.patch").is_file()
    assert (context / "entrypoint.sh").is_file()
