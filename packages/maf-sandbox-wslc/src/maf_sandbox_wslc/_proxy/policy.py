"""Translate the core egress contract into the pinned iron-proxy configuration."""

from __future__ import annotations

import base64
import json
from typing import cast

from maf_sandbox import EgressDecision, EgressDecisionCode, SandboxSpec

__all__ = ["encoded_policy", "read_decisions"]

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


def encoded_policy(spec: SandboxSpec) -> str:
    """Return the per-sandbox default-deny policy as base64 encoded JSON."""
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
            "upstream_deny_cidrs": _UPSTREAM_DENY_CIDRS,
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
        if method == "CONNECT" and action == "allow":
            continue
        tunnel_data = record.get("tunnel")
        tunnel = cast("dict[str, object]", tunnel_data) if isinstance(tunnel_data, dict) else {}
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
