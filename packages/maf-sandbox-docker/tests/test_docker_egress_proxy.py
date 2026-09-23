"""Proxy audit translation tests for Docker."""

from __future__ import annotations

import json

from maf_sandbox_docker._proxy.policy import read_decisions


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
