"""Live tests of the refusals and faults that depend on host-wide ``sbx`` state.

Skipped unless ``MAF_SANDBOX_SBX_E2E=1``.  A test that has to change that state — SSH agent
forwarding, an MCP registration, the login, the daemon's engine socket — also needs
``MAF_SANDBOX_SBX_E2E_HOST=1``, and restores what it changed.  Set it only on a host no one
else is using: while a test runs, every other sandbox on the host sees the changed state.
Logging back in reads ``DOCKER_USERNAME`` and ``DOCKER_PAT``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_docker_sbx import (
    SbxDaemonFault,
    SbxHostNotConfined,
    SbxLoginRequired,
    SbxSandboxBackend,
    SbxSandboxConfig,
)
from maf_sandbox_docker_sbx._backend import sandbox_name

_SBX = os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx")
_MAY_CHANGE_HOST = os.environ.get("MAF_SANDBOX_SBX_E2E_HOST") == "1"
_FORWARDING = "ssh.agentForwardingEnabled"

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_SANDBOX_SBX_E2E") != "1",
    reason="set MAF_SANDBOX_SBX_E2E=1 to run against a real sbx",
)


def _sbx(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([_SBX, *args], input=stdin, capture_output=True)


def _checked(*args: str, stdin: bytes | None = None) -> bytes:
    result = _sbx(*args, stdin=stdin)
    assert result.returncode == 0, (args, result.stderr.decode(errors="replace"))
    return result.stdout


def _forwarding() -> str:
    return _checked("settings", "get", _FORWARDING).decode().strip()


def _needs_host_changes(what: str) -> None:
    if not _MAY_CHANGE_HOST:
        pytest.skip(f"{what}; set MAF_SANDBOX_SBX_E2E_HOST=1 to let the test change it")


def _backend(tmp_path: Path) -> SbxSandboxBackend:
    return SbxSandboxBackend(
        SbxSandboxConfig(sbx_path=_SBX, workspace_root=tmp_path / "workspaces", cpus=2)
    )


def _key(label: str) -> SandboxKey:
    return SandboxKey(scope=f"e2e-{label}-{uuid.uuid4().hex[:10]}", thread_id="t", agent_id="a")


def _spec() -> SandboxSpec:
    return SandboxSpec(kind="e2e", requires=frozenset({Capability.EXEC, Capability.FILES_IN}))


def _listed(name: str) -> bool:
    out = _checked("ls", "--json")
    return any(row.get("name") == name for row in json.loads(out).get("sandboxes") or [])


@contextlib.contextmanager
def _forwarding_set(value: str) -> Iterator[None]:
    """Hold SSH agent forwarding at ``value``, restarting the daemon so sandboxes get it."""
    before = _forwarding()
    if before == value:
        yield
        return
    _needs_host_changes(f"SSH agent forwarding is {before} on this host")
    try:
        _checked("settings", "set", _FORWARDING, value)
        _checked("daemon", "restart")
        yield
    finally:
        _checked("settings", "set", _FORWARDING, before)
        _checked("daemon", "restart")


def test_a_host_forwarding_its_ssh_agent_is_refused(tmp_path):
    with _forwarding_set("true"):
        backend = _backend(tmp_path)
        key = _key("refused")
        with pytest.raises(SbxHostNotConfined, match="ssh.agentForwardingEnabled"):
            asyncio.run(backend.acquire(key, _spec()))
        assert not _listed(sandbox_name("maf", key, "e2e"))


def test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path):
    with _forwarding_set("true"):
        backend = _backend(tmp_path)

        async def accepted() -> None:
            return None

        backend.check_host = accepted  # type: ignore[method-assign]
        key = _key("agent")
        with pytest.raises(SbxHostNotConfined, match="ssh-agent.sock"):
            asyncio.run(backend.acquire(key, _spec()))
        assert not _listed(sandbox_name("maf", key, "e2e"))


def test_a_registered_mcp_server_is_refused(tmp_path):
    _needs_host_changes("the test registers an MCP server")
    with _forwarding_set("false"):
        listed = json.loads(_checked("mcp", "ls", "--json"))
        if listed.get("servers"):
            pytest.skip("an MCP server is already registered on this host")
        server = f"maf-e2e-{uuid.uuid4().hex[:8]}"
        # A stdio command is registered without being started, so no network is involved.
        _checked("mcp", "add", server, "--command", "true")
        try:
            backend = _backend(tmp_path)
            key = _key("mcp")
            with pytest.raises(SbxHostNotConfined, match="sbx mcp rm"):
                asyncio.run(backend.acquire(key, _spec()))
            assert not _listed(sandbox_name("maf", key, "e2e"))
        finally:
            _checked("mcp", "rm", "--force", server)


def test_a_lapsed_login_names_sbx_login(tmp_path):
    _needs_host_changes("the test logs sbx out")
    username, token = os.environ.get("DOCKER_USERNAME"), os.environ.get("DOCKER_PAT")
    if not username or not token:
        pytest.skip("logging back in needs DOCKER_USERNAME and DOCKER_PAT")
    with _forwarding_set("false"):
        try:
            _checked("logout", "--yes")
            backend = _backend(tmp_path)
            with pytest.raises(SbxLoginRequired, match="Run `sbx login`") as refused:
                asyncio.run(backend.acquire(_key("login"), _spec()))
            print(f"refusal: {refused.value}")
        finally:
            _checked("login", "--username", username, "--password-stdin", stdin=token.encode())


def _engine_socket() -> Path:
    status = json.loads(_checked("daemon", "status", "--json"))
    engine = Path(status["socket"]).with_name("docker.sock")
    try:
        is_socket = stat.S_ISSOCK(engine.lstat().st_mode)
    except FileNotFoundError:
        is_socket = False
    if not is_socket:
        pytest.skip(f"no engine socket at {engine} on this host")
    return engine


def test_a_lost_engine_is_a_fault_and_not_an_absent_sandbox(tmp_path):
    _needs_host_changes("the test hides the daemon's engine socket")
    with _forwarding_set("false"):
        backend = _backend(tmp_path)
        key = _key("engine")

        async def scenario() -> None:
            try:
                sandbox = await backend.acquire(key, _spec())
                workspace = tmp_path / "workspaces" / sandbox.name
                engine = _engine_socket()
                hidden = engine.with_name(f"{engine.name}.hidden")
                engine.rename(hidden)
                try:
                    # A daemon without its engine lists no sandboxes at all.
                    assert not _listed(sandbox.name)
                    with pytest.raises(SbxDaemonFault, match="sbx daemon restart"):
                        await backend.acquire(key, _spec())
                    for failure in (
                        await backend.dispose(key, instance_id=sandbox.instance_id),
                        await backend.dispose(key),
                    ):
                        assert failure is not None and failure.code == "unreachable", failure
                    assert workspace.is_dir()
                finally:
                    hidden.rename(engine)
                assert _listed(sandbox.name)
                again = await backend.acquire(key, _spec())
                assert again.instance_id == sandbox.instance_id
            finally:
                assert await backend.dispose(key) is None

        asyncio.run(scenario())
