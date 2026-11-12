"""Live tests of the refusals and faults that depend on host-wide ``sbx`` state.

Skipped unless ``MAF_SANDBOX_SBX_E2E=1``.  A test that has to change that state — SSH agent
forwarding, an MCP registration, the login, or the daemon API socket — also needs
``MAF_SANDBOX_SBX_E2E_HOST=1``, and restores what it changed.  Set it only on a host no one
else is using: while a test runs, every other sandbox on the host sees the changed state.
Logging back in reads ``DOCKER_USERNAME`` and ``DOCKER_PAT``. Build and load the shell fixture
in ``tests/images`` first, or name a prepared template with ``MAF_SANDBOX_SBX_E2E_IMAGE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _sbx_e2e_faults import engine_fault_responses
from _sbx_e2e_helpers import listed_rows
from maf_sandbox import Capability, Egress, SandboxKey, SandboxSpec

from maf_sandbox_docker_sbx import (
    SbxDaemonFault,
    SbxHostNotConfined,
    SbxLoginRequired,
    SbxSandboxBackend,
    SbxSandboxConfig,
)
from maf_sandbox_docker_sbx._backend import sandbox_name

_SBX = os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx")
_IMAGE = os.environ.get("MAF_SANDBOX_SBX_E2E_IMAGE", "maf-sbx-e2e-shell:local")
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
    return SandboxSpec(
        kind="e2e",
        image=os.environ.get("MAF_SANDBOX_SBX_E2E_HOST_IMAGE", _IMAGE),
        requires=frozenset({Capability.EXEC, Capability.FILES_IN}),
    )


def _listed(name: str) -> bool:
    out = _checked("ls", "--json")
    return any(row.get("name") == name for row in listed_rows(out, "sandboxes"))


@contextlib.contextmanager
def _setting_set(setting: str, value: str) -> Iterator[None]:
    """Hold a host setting at ``value``, restarting the daemon on each change."""
    before = _checked("settings", "get", setting).decode().strip()
    if before == value:
        yield
        return
    _needs_host_changes(f"{setting} is {before} on this host")
    try:
        _checked("settings", "set", setting, value)
        _checked("daemon", "restart")
        yield
    finally:
        _checked("settings", "set", setting, before)
        _checked("daemon", "restart")


def test_a_host_forwarding_its_ssh_agent_is_refused(tmp_path):
    with _setting_set(_FORWARDING, "true"):
        backend = _backend(tmp_path)
        key = _key("refused")
        with pytest.raises(SbxHostNotConfined, match="ssh.agentForwardingEnabled"):
            asyncio.run(backend.acquire(key, _spec()))
        assert not _listed(sandbox_name("maf", key, "e2e"))


async def _prove_forwarded_agent(backend: SbxSandboxBackend, name: str, tmp_path: Path) -> None:
    public_key_path = os.environ.get("MAF_SANDBOX_SBX_E2E_AGENT_PUBLIC_KEY")
    assert public_key_path, (
        "set MAF_SANDBOX_SBX_E2E_AGENT_PUBLIC_KEY to an ephemeral agent's .pub file"
    )
    public_key = Path(public_key_path).read_text(encoding="utf-8").split()
    assert len(public_key) >= 2 and public_key[0] == "ssh-ed25519", public_key_path
    challenge = f"maf-sbx-forwarding-{uuid.uuid4().hex}"
    print(
        json.dumps(
            {
                "forwarded_agent_host": {
                    "sandbox": name,
                    "version": _checked("version").decode().strip(),
                    "forwarding": _checked("settings", "get", _FORWARDING).decode().strip(),
                    "socket_selection": _checked("settings", "get", "ssh.agentSocketPath")
                    .decode()
                    .strip(),
                    "daemon": json.loads(_checked("daemon", "status", "--json")),
                    "sandboxes": listed_rows(_checked("ls", "--json"), "sandboxes"),
                }
            }
        ),
        flush=True,
    )
    script = Path(__file__).with_name("_sbx_e2e_agent.py").read_text(encoding="utf-8")
    result = await backend._sbx(
        "exec",
        name,
        "python3",
        "-c",
        script,
        "/run/ssh-agent.sock",
        public_key[1],
        challenge,
        timeout=30,
    )
    print(f"forwarded_agent_guest: {result.stdout.decode(errors='replace')}", flush=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    report = json.loads(result.stdout)
    assert report["socket_present"] is True and report["socket_is_socket"] is True, report
    signature = tmp_path / "agent.sig"
    signature.write_text(report["signature"], encoding="utf-8", newline="\n")
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"fixture {' '.join(public_key[:2])}\n", encoding="utf-8", newline="\n")
    verified = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(allowed),
            "-I",
            "fixture",
            "-n",
            "git",
            "-s",
            str(signature),
        ],
        input=challenge.encode(),
        capture_output=True,
        timeout=10,
    )
    assert verified.returncode == 0, (verified.stdout, verified.stderr)
    print(json.dumps({"forwarded_agent_signature_verified": name}), flush=True)


def test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path, monkeypatch):
    with _setting_set(_FORWARDING, "true"):
        backend = _backend(tmp_path)

        async def accepted() -> None:
            return None

        backend.check_host = accepted  # type: ignore[method-assign]
        sbx = backend._sbx

        async def with_agent(*args, **kwargs):
            # V3 kits receive no agent unless they request the runtime capability.
            if args[0] == "create":
                kit = Path(__file__).parent / "images" / "ssh-agent"
                args = (*args[:-1], "--kit", str(kit), args[-1])
            return await sbx(*args, **kwargs)

        monkeypatch.setattr(backend, "_sbx", with_agent)
        check_sandbox = backend.check_sandbox

        async def proved_then_checked(name: str) -> None:
            await _prove_forwarded_agent(backend, name, tmp_path)
            await check_sandbox(name)

        monkeypatch.setattr(backend, "check_sandbox", proved_then_checked)
        key = _key("agent")
        try:
            with pytest.raises(SbxHostNotConfined, match="ssh-agent.sock"):
                asyncio.run(backend.acquire(key, _spec()))
            assert not _listed(sandbox_name("maf", key, "e2e"))
            print(json.dumps({"forwarded_agent_refused_and_discarded": True}), flush=True)
        finally:
            assert asyncio.run(backend.dispose(key)) is None
            assert not _listed(sandbox_name("maf", key, "e2e"))


def test_a_registered_mcp_server_is_refused(tmp_path):
    _needs_host_changes("the test registers an MCP server")
    with _setting_set(_FORWARDING, "false"):
        listed = listed_rows(_checked("mcp", "ls", "--json"), "servers")
        if listed:
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
    with _setting_set(_FORWARDING, "false"):
        try:
            help_text = _checked("logout", "--help")
            flag = "--force" if b"--force" in help_text else "--yes"
            assert flag.encode() in help_text, "sbx logout has no known confirmation flag"
            _checked("logout", flag)
            backend = _backend(tmp_path)
            with pytest.raises(SbxLoginRequired, match="Run `sbx login`") as refused:
                asyncio.run(backend.acquire(_key("login"), _spec()))
            print(f"refusal: {refused.value}")
        finally:
            _checked("login", "--username", username, "--password-stdin", stdin=token.encode())


def test_engine_fault_responses_do_not_mean_an_absent_sandbox(tmp_path):
    _needs_host_changes("the test substitutes responses at the daemon API socket")
    with _setting_set(_FORWARDING, "false"):
        backend = _backend(tmp_path)
        key = _key("engine")

        async def scenario() -> None:
            try:
                sandbox = await backend.acquire(key, _spec())
                workspace = tmp_path / "workspaces" / sandbox.name
                status = json.loads(_checked("daemon", "status", "--json"))
                with engine_fault_responses(Path(status["socket"])) as observed:
                    assert not _listed(sandbox.name)
                    with pytest.raises(SbxDaemonFault, match="sbx daemon restart"):
                        await backend.acquire(key, _spec())
                    for failure in (
                        await backend.dispose(key, instance_id=sandbox.instance_id),
                        await backend.dispose(key),
                    ):
                        assert failure is not None and failure.code == "unreachable", failure
                    assert workspace.is_dir()
                    assert "listing" in observed
                    target = f"/sandbox/{sandbox.name}"
                    paths = [request.partition(" ")[2] for request in observed]
                    assert any(path == target or path.startswith(target + "/") for path in paths), (
                        observed
                    )
                print(json.dumps({"engine_fault_injection": "daemon-api", "requests": observed}))
                assert _listed(sandbox.name)
                again = await backend.acquire(key, _spec())
                assert again.instance_id == sandbox.instance_id
            finally:
                assert await backend.dispose(key) is None

        asyncio.run(scenario())


def _allowlist(*hosts: str) -> SandboxSpec:
    return SandboxSpec(
        kind="e2e",
        image=os.environ.get("MAF_SANDBOX_SBX_E2E_HOST_IMAGE", _IMAGE),
        requires=frozenset({Capability.EXEC}),
        egress=Egress.ALLOWLIST,
        egress_allow=hosts,
    )


def test_a_global_allow_added_later_retires_an_allowlisted_sandbox(tmp_path):
    _needs_host_changes("the test adds a global allow rule")
    late = f"maf-e2e-{uuid.uuid4().hex[:8]}.test"
    backend = _backend(tmp_path)
    key = _key("drift")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _allowlist("example.com"))
            ran = await sandbox.exec(["true"], working_directory=".", timeout=60)
            assert ran.exit_code == 0, ran
            _checked("policy", "allow", "network", late)
            try:
                checked = json.loads(
                    _sbx(
                        "policy", "check", "network", "--json", "--sandbox", sandbox.name, late
                    ).stdout
                )
                # The drift check matters only because a later global allow reaches a running
                # sandbox; if sbx stops doing that, this test must say so rather than pass.
                assert checked.get("allowed") is True, checked
                with pytest.raises(SbxHostNotConfined, match="retired"):
                    await sandbox.exec(["true"], working_directory=".", timeout=60)
                assert sandbox.instance_id in backend.retired
                replacement = await backend.acquire(key, _allowlist("example.com"))
                assert replacement.instance_id != sandbox.instance_id
                checked = json.loads(
                    _sbx(
                        "policy", "check", "network", "--json", "--sandbox", replacement.name, late
                    ).stdout
                )
                assert checked.get("allowed") is False, checked
            finally:
                _checked("policy", "rm", "network", "--resource", late, "--force")
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_a_custom_secret_for_an_allowed_host_is_refused(tmp_path):
    _needs_host_changes("the test stores a custom secret")
    placeholder = f"maf-e2e-{uuid.uuid4().hex[:12]}"
    _checked(
        *("secret", "set-custom", "--host", "api.example.com", "--env", "MAF_E2E_KEY"),
        *("--value", "not-a-secret", "--placeholder", placeholder),
    )
    try:
        backend = _backend(tmp_path)
        key = _key("secret")
        with pytest.raises(SbxHostNotConfined, match="MAF_E2E_KEY"):
            asyncio.run(backend.acquire(key, _allowlist("*.example.com")))
        assert not _listed(sandbox_name("maf", key, "e2e"))
    finally:
        _checked("secret", "rm", "--placeholder", placeholder, "--force")
