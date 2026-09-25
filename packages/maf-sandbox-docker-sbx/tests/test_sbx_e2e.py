"""Live tests against a real ``sbx`` and real microVMs.

Skipped unless ``MAF_SANDBOX_SBX_E2E=1``.  ``MAF_SANDBOX_SBX_PATH`` names the CLI when it is
not on ``PATH``.  The host must pass the backend's own checks — SSH agent forwarding off, no MCP
server registered — except on a host whose settings the tester may not change, where
``MAF_SANDBOX_SBX_E2E_ACCEPT_HOST=1`` skips those two checks for every test but the one that
asserts them.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from maf_sandbox import Capability, EntryKind, SandboxKey, SandboxSpec
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reach_conformance,
    assert_reclaim_conformance,
    assert_storage_base_conformance,
)

from maf_sandbox_docker_sbx import SbxHostNotConfined, SbxSandboxBackend, SbxSandboxConfig
from maf_sandbox_docker_sbx._backend import sandbox_name
from maf_sandbox_docker_sbx._plane import WorkspacePlane

_SBX = os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx")
_ACCEPT_HOST = os.environ.get("MAF_SANDBOX_SBX_E2E_ACCEPT_HOST") == "1"
_WORK = "/maf-sandbox/work"
_NO_SYMLINK_PRIVILEGE = 1314

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_SANDBOX_SBX_E2E") != "1",
    reason="set MAF_SANDBOX_SBX_E2E=1 to run against a real sbx",
)


def _backend(tmp_path: Path, *, accept_host: bool = _ACCEPT_HOST) -> SbxSandboxBackend:
    backend = SbxSandboxBackend(
        SbxSandboxConfig(sbx_path=_SBX, workspace_root=tmp_path / "workspaces", cpus=2)
    )
    if accept_host:

        async def accepted() -> None:
            return None

        async def accepted_sandbox(_name: str) -> None:
            return None

        backend.check_host = accepted  # type: ignore[method-assign]
        backend.check_sandbox = accepted_sandbox  # type: ignore[method-assign]
    return backend


def _key(label: str) -> SandboxKey:
    return SandboxKey(scope=f"e2e-{label}-{uuid.uuid4().hex[:10]}", thread_id="t", agent_id="a")


def _spec(kind: str = "e2e", **overrides: object) -> SandboxSpec:
    requires = frozenset(
        {
            Capability.EXEC,
            Capability.FILES_IN,
            Capability.FILES_OUT,
            Capability.FILES_LIST,
            Capability.FILES_DELETE,
        }
    )
    return SandboxSpec(kind=kind, requires=requires, **overrides)  # type: ignore[arg-type]


def _listed(name: str) -> bool:
    out = subprocess.run([_SBX, "ls", "--json"], capture_output=True, check=True).stdout
    return any(row.get("name") == name for row in json.loads(out).get("sandboxes") or [])


@dataclass(frozen=True)
class SbxSubject(PosixGuestSubject):
    """Plants a link in the guest where it can, and on the host side of the mount where not.

    The plane acts on the host side, so a host-made link is the same thing to it as a
    guest-made one.  ``guest_links`` records which the guest managed.
    """

    plane: WorkspacePlane | None = None
    guest_links: list[bool] | None = None

    async def plant_symlink(self, path: str, target: str) -> None:
        assert self.plane is not None and self.guest_links is not None
        await self.sandbox.exec(
            ["ln", "-sfn", target, path],
            working_directory=self.working_directory,
            timeout=self.exec_timeout,
        )
        parts = self.plane.parts(path)
        assert parts, path
        host = self.plane.host_root.joinpath(*parts)
        made = host.is_symlink() or host.is_junction()
        self.guest_links.append(made)
        if made:
            return
        if host.is_dir() and not host.is_symlink():
            host.rmdir()
        try:
            os.symlink(target, host)
        except OSError as error:
            if getattr(error, "winerror", None) != _NO_SYMLINK_PRIVILEGE:
                raise
            # Without the symlink privilege a junction is the only link Windows makes. It takes a
            # directory, so it aims at the target's; the plane refuses every reparse point alike.
            aimed = self.plane.parts(target) if target.startswith("/") else None
            destination = self.plane.host_root.joinpath(*aimed) if aimed else None
            while destination is not None and not destination.is_dir():
                destination = destination.parent
            if destination is None:
                raise
            import _winapi

            # Windows-only, so absent from the stubs a Linux type check reads.
            getattr(_winapi, "CreateJunction")(str(destination), str(host))


def _subject(backend: SbxSandboxBackend, sandbox, links: list[bool]) -> SbxSubject:
    return SbxSubject(
        sandbox=sandbox,
        working_directory=_WORK,
        capabilities=backend.declarations.capabilities,
        exec_timeout=60.0,
        exec_cleanup_timeout=backend.config.exec_cleanup_timeout_seconds,
        plane=sandbox.plane,
        guest_links=links,
    )


def test_file_suites_hold_against_a_real_sandbox(tmp_path):
    backend = _backend(tmp_path)
    key = _key("files")
    links: list[bool] = []

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            subject = _subject(backend, sandbox, links)
            await assert_storage_base_conformance(sandbox, backend.declarations.capabilities)
            await assert_files_in_conformance(subject)
            await assert_files_out_conformance(subject)
            await assert_files_delete_conformance(subject)
            await assert_reclaim_conformance(subject)
            await assert_reach_conformance(subject)
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())
    print(f"guest-made links in the workspace: {links.count(True)} of {len(links)}")


def test_explicit_storage_base_and_warm_reuse(tmp_path):
    backend = _backend(tmp_path)
    key = _key("base")
    spec = _spec(work_dir="/srv/maf/base")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, spec)
            await assert_storage_base_conformance(sandbox, backend.declarations.capabilities)
            await sandbox.write_file("kept.txt", b"kept", working_directory=".")
            stopped = subprocess.run([_SBX, "stop", sandbox.name], capture_output=True)
            assert stopped.returncode == 0, stopped.stderr
            again = await backend.acquire(key, spec)
            assert again.instance_id == sandbox.instance_id
            started = time.monotonic()
            result = await again.exec(["cat", "kept.txt"], working_directory=".", timeout=60)
            print(f"exec on a stopped sandbox: {time.monotonic() - started:.1f} s")
            assert (result.exit_code, result.stdout) == (0, "kept")
            with pytest.raises(ValueError, match="work_dir"):
                await backend.acquire(key, _spec(work_dir="/srv/maf/other"))
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_exec_suite_and_the_process_group_deadline(tmp_path):
    backend = _backend(tmp_path)
    key = _key("exec")
    links: list[bool] = []

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await sandbox.exec(
                    "sleep 300 & sleep 300; echo done", working_directory=".", timeout=3
                )
            elapsed = time.monotonic() - started
            left = await sandbox.exec(
                "ps -eo args | grep -c '^sleep 300$'", working_directory=".", timeout=30
            )
            assert left.stdout.strip() == "0", left
            assert elapsed < 3 + backend.config.exec_cleanup_timeout_seconds, elapsed
            missing = await sandbox.exec(["pwd"], working_directory="/nowhere", timeout=30)
            assert missing.exit_code == 125 and "nowhere" in missing.stderr, missing
            await assert_exec_conformance(_subject(backend, sandbox, links))
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_egress_is_closed_by_content(tmp_path):
    backend = _backend(tmp_path)
    key = _key("egress")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            for url, content in (
                ("https://example.com/", "Example Domain"),
                ("http://example.com/", "Example Domain"),
                ("https://1.1.1.1/", "Cloudflare"),
            ):
                result = await sandbox.exec(
                    ["curl", "-sS", "--max-time", "15", url], working_directory=".", timeout=60
                )
                print(f"{url}: exit {result.exit_code}, {result.stdout.strip()[:80]!r}")
                assert content not in result.stdout, result
                assert result.exit_code != 0 or "Blocked" in result.stdout, result
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_the_guest_cannot_plant_a_link_the_plane_follows(tmp_path):
    backend = _backend(tmp_path)
    key = _key("links")
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            made = await sandbox.exec(
                ["sh", "-c", 'ln -s "$1" out-link; ln -s / root-link; true', "_", str(secret)],
                working_directory=".",
                timeout=60,
            )
            assert made.exit_code == 0, made
            for name in ("out-link", "root-link"):
                entry = await sandbox.stat_file(name, working_directory=".")
                print(f"guest `ln -s` {name}: {entry.kind if entry else 'nothing created'}")
                if entry is not None:
                    assert entry.kind is EntryKind.SYMLINK
                    with pytest.raises(OSError):
                        await sandbox.read_file(name, working_directory=".", max_bytes=100)
                    with pytest.raises(ValueError):
                        await sandbox.write_file(f"{name}/x", b"no", working_directory=".")
            assert secret.read_text() == "host secret"
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_disposal_removes_the_sandbox_and_its_workspace(tmp_path):
    backend = _backend(tmp_path)
    key = _key("dispose")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            await backend.acquire(key, _spec(kind="second"))
            workspace = tmp_path / "workspaces" / sandbox.name
            assert workspace.is_dir() and _listed(sandbox.name)
            purge = await backend.dispose_scope(key.scope, key.thread_id)
            assert purge.undisposed is None and purge.disposed == 2, purge
            assert not workspace.exists() and not _listed(sandbox.name)
            assert not _listed(sandbox_name("maf", key, "second"))
        finally:
            await backend.dispose(key)

    asyncio.run(scenario())


def test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path):
    forwarding = subprocess.run(
        [_SBX, "settings", "get", "ssh.agentForwardingEnabled"], capture_output=True, text=True
    ).stdout.strip()
    if forwarding != "true":
        pytest.skip("SSH agent forwarding is off on this host")
    backend = _backend(tmp_path, accept_host=False)

    async def accepted() -> None:
        return None

    backend.check_host = accepted  # type: ignore[method-assign]
    key = _key("agent")
    with pytest.raises(SbxHostNotConfined, match="ssh-agent.sock"):
        asyncio.run(backend.acquire(key, _spec()))
    assert not _listed(sandbox_name("maf", key, "e2e"))


def test_a_host_forwarding_its_ssh_agent_is_refused(tmp_path):
    forwarding = subprocess.run(
        [_SBX, "settings", "get", "ssh.agentForwardingEnabled"], capture_output=True, text=True
    ).stdout.strip()
    if forwarding != "true":
        pytest.skip("SSH agent forwarding is already off on this host")
    backend = _backend(tmp_path, accept_host=False)
    key = _key("refused")
    with pytest.raises(SbxHostNotConfined, match="ssh.agentForwardingEnabled"):
        asyncio.run(backend.acquire(key, _spec()))
    assert not _listed(sandbox_name("maf", key, "e2e"))
