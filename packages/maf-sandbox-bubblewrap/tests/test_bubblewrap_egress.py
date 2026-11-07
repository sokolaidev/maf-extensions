"""Native egress admission and host/guest trust separation."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from maf_sandbox import Capability, Egress, EgressRule, HttpMethod, SandboxSpec

from maf_sandbox_bubblewrap import BubblewrapSandboxBackend, BubblewrapSandboxConfig
from maf_sandbox_bubblewrap._backend import _arguments
from maf_sandbox_bubblewrap._egress import Proxy, policy


def test_allowlist_requires_explicit_proxy_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    config = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    closed = BubblewrapSandboxBackend(config)
    assert closed.declarations.egress_modes == {Egress.CLOSED}
    assert Capability.EGRESS_METHODS not in closed.declarations.capabilities
    enabled = BubblewrapSandboxBackend(
        replace(config, egress_proxy=tmp_path / "proxy", egress_proxy_sha256="a" * 64)
    )
    assert Egress.ALLOWLIST in enabled.declarations.egress_modes
    assert Capability.EGRESS_METHODS in enabled.declarations.capabilities
    assert Capability.EGRESS_PATHS in enabled.declarations.capabilities


@pytest.mark.parametrize(
    "changes",
    [
        {"egress_proxy": Path("/proxy")},
        {"egress_proxy_sha256": "a" * 64},
        {"egress_proxy": Path("proxy"), "egress_proxy_sha256": "a" * 64},
        {"egress_proxy": Path("/proxy"), "egress_proxy_sha256": "not-a-digest"},
    ],
)
def test_incomplete_proxy_identity_is_refused(tmp_path, changes):
    with pytest.raises(ValueError):
        BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path, **changes)


def test_native_policy_preserves_host_methods_paths_and_denies_host_addresses(tmp_path):
    spec = SandboxSpec(
        kind="pricing",
        egress=Egress.ALLOWLIST,
        egress_allow=(
            EgressRule("prices.azure.com", methods=(HttpMethod.GET,), paths=("/api/retail/*",)),
        ),
    )
    result = policy(spec, tmp_path, [1, 2, 3])
    assert result["transforms"] == [
        {
            "name": "allowlist",
            "config": {
                "domains": [],
                "rules": [
                    {"host": "prices.azure.com", "methods": ["GET"], "paths": ["/api/retail/*"]},
                ],
            },
        }
    ]
    proxy = result["proxy"]
    tls = result["tls"]
    assert isinstance(proxy, dict) and isinstance(tls, dict)
    assert "127.0.0.0/8" in proxy["upstream_deny_cidrs"]
    assert "169.254.0.0/16" in proxy["upstream_deny_cidrs"]
    assert tls["mode"] == "mitm"


def test_only_public_proxy_files_enter_the_guest(tmp_path):
    config = BubblewrapSandboxConfig(tmp_path, tmp_path, tmp_path)
    proxy = Proxy(tmp_path / "private")
    args = _arguments(config, proxy)
    assert "--unshare-net" in args
    assert "--share-net" not in args
    assert str(proxy.directory / "public") in args
    assert str(proxy.directory) not in args
    assert not any("ca.key" in arg for arg in args)
    assert "SSL_CERT_FILE" in args
    assert "HTTPS_PROXY" not in _arguments(config)


def test_proxy_digest_mismatch_precedes_any_process_or_state(tmp_path):
    binary = tmp_path / "proxy"
    binary.write_bytes(b"incorrect executable")
    proxy = Proxy(tmp_path / "state")
    with pytest.raises(ValueError, match="SHA-256"):
        asyncio.run(proxy.start(binary, "0" * 64, SandboxSpec(kind="pricing"), tmp_path))
    assert not proxy.directory.exists()
    assert proxy.process is None
