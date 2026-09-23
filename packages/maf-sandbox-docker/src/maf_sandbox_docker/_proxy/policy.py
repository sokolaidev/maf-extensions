"""Translate the core egress contract into the pinned iron-proxy configuration."""

from __future__ import annotations

import base64
import ipaddress
import json
from collections.abc import Sequence
from typing import cast

from maf_sandbox import EgressDecision, EgressDecisionCode, SandboxSpec

__all__ = ["encoded_policy", "network_gateways", "read_decisions"]

_UPSTREAM_DENY_CIDRS = (
    "0.0.0.0/8",
    "127.0.0.0/8",
    "100.100.100.200/32",
    "168.63.129.16/32",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "::/128",
    "::1/128",
    "fe80::/10",
    "ff00::/8",
    "fd00:ec2::254/128",
    "fd20:ce::254/128",
)


def network_gateways(ipam: object) -> tuple[str, ...]:
    """Read the gateway addresses from an inspected network's IPAM configuration."""
    if not isinstance(ipam, list):
        raise ValueError("network IPAM configuration is not a list")
    addresses: list[str] = []
    for item in cast("list[object]", ipam):
        if not isinstance(item, dict):
            raise ValueError("network IPAM entry is not an object")
        entry = cast("dict[str, object]", item)
        gateway = entry.get("Gateway")
        if gateway is None or gateway == "":
            # Engine 28 omits the gateway it allocated for a subnet the caller named, so deny
            # the default IPAM pick. An unaddressed bridge only gains a spare deny.
            subnet = entry.get("Subnet")
            if not isinstance(subnet, str) or subnet == "":
                continue
            gateway = str(next(ipaddress.ip_network(subnet, strict=False).hosts()))
        if not isinstance(gateway, str):
            raise ValueError("network gateway is not an address")
        addresses.append(str(ipaddress.ip_address(gateway)))
    return tuple(addresses)


def encoded_policy(spec: SandboxSpec, *, control_addresses: Sequence[str] = ()) -> str:
    """Return the per-sandbox default-deny policy as base64 encoded JSON."""
    control_cidrs = tuple(
        str(ipaddress.ip_network(f"{address}/{ipaddress.ip_address(address).max_prefixlen}"))
        for address in control_addresses
    )
    domains: list[str] = []
    rules: list[dict[str, object]] = []
    for entry in spec.egress_allow:
        if isinstance(entry, str):
            domains.append(entry)
            continue
        if entry.authority is not None:
            raise ValueError("this backend cannot enforce attached egress authority")
        rule: dict[str, object] = {"host": entry.host}
        if entry.methods is not None:
            rule["methods"] = list(entry.methods)
        if entry.paths is not None:
            rule["paths"] = list(entry.paths)
        rules.append(rule)
    policy = {
        "dns": {"enabled": False},
        "proxy": {
            "http_listen": "127.0.0.1:18080",
            "https_listen": "127.0.0.1:18443",
            "tunnel_listen": ":3128",
            "upstream_deny_cidrs": (*_UPSTREAM_DENY_CIDRS, *control_cidrs),
        },
        "tls": {
            "mode": "mitm",
            "ca_cert": "/run/maf-proxy/ca.crt",
            "ca_key": "/run/maf-proxy/ca.key",
        },
        "transforms": [{"name": "allowlist", "config": {"domains": domains, "rules": rules}}],
        "metrics": {"listen": "127.0.0.1:19090"},
        "log": {"level": "info"},
    }
    return base64.b64encode(json.dumps(policy, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )


def read_decisions(text: str, limit: int) -> tuple[tuple[EgressDecision, ...], bool]:
    """Read bounded iron-proxy audit records, skipping startup and tunnel handshakes."""
    lines = text.splitlines()
    decisions: list[EgressDecision] = []
    for line in lines:
        try:
            parsed: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        record = cast("dict[str, object]", parsed)
        if record.get("msg") != "request":
            continue
        audit_data = record.get("audit")
        if not isinstance(audit_data, dict):
            continue
        audit = cast("dict[str, object]", audit_data)
        action, method, target = audit.get("action"), audit.get("method"), audit.get("host")
        if not isinstance(target, str) or not target or action not in ("allow", "reject", "error"):
            continue
        tunnel_data = record.get("tunnel")
        tunnel = cast("dict[str, object]", tunnel_data) if isinstance(tunnel_data, dict) else {}
        if method == "CONNECT" and action == "allow" and not isinstance(tunnel_data, dict):
            continue
        tunnel_target = tunnel.get("target")
        if target.startswith("[") and "]:" in target:
            host, port_text = target[1:].rsplit("]:", 1)
        elif ":" in target and target.rsplit(":", 1)[1].isdigit():
            host, port_text = target.rsplit(":", 1)
        else:
            host = target
            port_text = "443" if audit.get("sni") else "80"
            if isinstance(tunnel_target, str) and ":" in tunnel_target:
                port_text = tunnel_target.rsplit(":", 1)[1]
        if not port_text.isdigit() or not 0 < int(port_text) <= 65535:
            continue
        if action == "allow":
            code = "ALLOW"
        elif action == "reject":
            code = "DENY"
        elif any(
            reason in str(record.get("error", ""))
            for reason in ("plaintext HTTP", "upstream_deny_cidrs", "proxy interface address")
        ):
            code = "DENY"
        else:
            code = "UNREACHABLE"
        decisions.append(
            EgressDecision(decision=cast(EgressDecisionCode, code), host=host, port=int(port_text))
        )
    return tuple(decisions[-limit:]), len(lines) > limit
