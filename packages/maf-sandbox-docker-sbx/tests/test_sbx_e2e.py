"""Live tests against a real ``sbx`` and real microVMs.

Skipped unless ``MAF_SANDBOX_SBX_E2E=1``.  ``MAF_SANDBOX_SBX_PATH`` names the CLI when it is
not on ``PATH``.  The host must pass the backend's own checks — SSH agent forwarding off, no MCP
server registered — except on a host whose settings the tester may not change, where
``MAF_SANDBOX_SBX_E2E_ACCEPT_HOST=1`` skips those two checks.  ``test_sbx_e2e_host.py`` asserts
them.  ``MAF_SANDBOX_SBX_E2E_IMAGES`` names templates, comma-separated, that the file, exec and
egress tests also run on, beside Docker's default one.  The allowlist tests set rules scoped to
their own sandboxes only, and refuse on a host whose global rules or secrets reach the hosts they
allow.
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
from maf_sandbox import (
    Capability,
    Egress,
    EgressRule,
    EntryKind,
    HostToolRegistry,
    HostToolRun,
    HttpMethod,
    Identity,
    SandboxKey,
    SandboxProgramTimeout,
    SandboxSpec,
    SourceIntegrity,
    guest_run_layout,
    host_tool_calls_over_exec,
    host_tool_shim,
    sandbox_tool,
)
from maf_sandbox.bounded_exec import SandboxExecOutputLimitExceeded
from maf_sandbox.conformance import (
    ExecEgressMethodsSubject,
    PosixGuestSubject,
    assert_egress_conformance,
    assert_egress_methods_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reach_conformance,
    assert_reclaim_conformance,
    assert_storage_base_conformance,
)
from maf_sandbox.run_activity import SandboxRunActivityLost

from maf_sandbox_docker_sbx import SbxSandboxBackend, SbxSandboxConfig
from maf_sandbox_docker_sbx._backend import sandbox_name
from maf_sandbox_docker_sbx._plane import WorkspacePlane

_SBX = os.environ.get("MAF_SANDBOX_SBX_PATH", "sbx")
_ACCEPT_HOST = os.environ.get("MAF_SANDBOX_SBX_E2E_ACCEPT_HOST") == "1"
_WORK = "/maf-sandbox/work"
_NO_SYMLINK_PRIVILEGE = 1314
_IMAGES = [None, *(i for i in os.environ.get("MAF_SANDBOX_SBX_E2E_IMAGES", "").split(",") if i)]
_BY_IMAGE = pytest.mark.parametrize("image", _IMAGES, ids=lambda image: image or "default")

pytestmark = pytest.mark.skipif(
    os.environ.get("MAF_SANDBOX_SBX_E2E") != "1",
    reason="set MAF_SANDBOX_SBX_E2E=1 to run against a real sbx",
)


def _backend(tmp_path: Path) -> SbxSandboxBackend:
    backend = SbxSandboxBackend(
        SbxSandboxConfig(sbx_path=_SBX, workspace_root=tmp_path / "workspaces", cpus=2)
    )
    if _ACCEPT_HOST:

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


@_BY_IMAGE
def test_file_suites_hold_against_a_real_sandbox(tmp_path, image):
    backend = _backend(tmp_path)
    key = _key("files")
    links: list[bool] = []

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec(image=image))
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


@_BY_IMAGE
def test_explicit_storage_base_and_warm_reuse(tmp_path, image):
    backend = _backend(tmp_path)
    key = _key("base")
    spec = _spec(work_dir="/srv/maf/base", image=image)

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
                await backend.acquire(key, _spec(work_dir="/srv/maf/other", image=image))
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


@_BY_IMAGE
def test_exec_suite_the_deadline_and_the_output_bound(tmp_path, image):
    backend = _backend(tmp_path)
    key = _key("exec")
    links: list[bool] = []

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec(image=image))
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
            with pytest.raises(SandboxExecOutputLimitExceeded):
                await sandbox.exec("sleep 300 & yes", working_directory=".", timeout=120)
            left = await sandbox.exec(
                "ps -eo args | grep -cE '^(yes|sleep 300)$'", working_directory=".", timeout=30
            )
            assert left.stdout.strip() == "0", left
            assert sandbox.instance_id not in backend.retired
            small = await sandbox.exec_bounded(
                ["echo", "within"], working_directory=".", timeout=30, max_output_bytes=4096
            )
            assert (small.exit_code, small.stdout) == (0, "within\n"), small
            started = time.monotonic()
            with pytest.raises(SandboxExecOutputLimitExceeded):
                await sandbox.exec_bounded(
                    "sleep 300 & yes", working_directory=".", timeout=120, max_output_bytes=4096
                )
            # A 4 KiB budget ends the flood long before the 8 MiB default would.
            assert time.monotonic() - started < 30, time.monotonic() - started
            left = await sandbox.exec(
                "ps -eo args | grep -cE '^(yes|sleep 300)$'", working_directory=".", timeout=30
            )
            assert left.stdout.strip() == "0", left
            ids = "id -u; id -g"
            wrapped = await sandbox.exec(ids, working_directory=".", timeout=30)
            direct = subprocess.run(
                [_SBX, "exec", sandbox.name, "sh", "-c", ids], capture_output=True
            )
            assert wrapped.stdout.split() == direct.stdout.decode().split(), (wrapped, direct)
            assert len(wrapped.stdout.split()) == 2, wrapped
            # A user namespace never reaches the guest's real root, Docker's template included.
            sudo = await sandbox.exec("sudo -n true", working_directory=".", timeout=30)
            assert sudo.exit_code != 0, sudo
            # The namespace maps the caller's own ids and no others.
            uid = wrapped.stdout.split()[0]
            owner = await sandbox.exec(
                ["stat", "-c", "%u", "/etc/passwd"], working_directory=".", timeout=30
            )
            assert owner.stdout.strip() == ("0" if uid == "0" else "65534"), owner
            given = await sandbox.exec(
                "touch given && chown 4321 given", working_directory=".", timeout=30
            )
            print(f"uid {uid}: /etc/passwd owner {owner.stdout.strip()}, chown {given.exit_code}")
            assert given.exit_code != 0, given
            missing = await sandbox.exec(["pwd"], working_directory="/nowhere", timeout=30)
            assert missing.exit_code == 125 and "nowhere" in missing.stderr, missing
            await assert_exec_conformance(_subject(backend, sandbox, links))
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


@_BY_IMAGE
def test_egress_is_closed_by_content(tmp_path, image):
    backend = _backend(tmp_path)
    key = _key("egress")

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec(image=image))
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


async def _fetch(sandbox, url: str) -> str:
    result = await sandbox.exec(
        ["sh", "-c", 'curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$0"', url],
        working_directory=".",
        timeout=60,
    )
    return result.stdout.strip()


@_BY_IMAGE
def test_an_allowlist_admits_its_hosts_and_nothing_else(tmp_path, image):
    backend = _backend(tmp_path)
    key = _key("allow")
    spec = _spec(
        image=image,
        egress=Egress.ALLOWLIST,
        egress_allow=(EgressRule("example.com", paths=("/",)), "*.python.org"),
    )

    async def scenario():
        try:
            sandbox = await backend.acquire(key, spec)
            await assert_egress_conformance(
                _subject(backend, sandbox, []),
                allowed_url="https://example.com/",
                denied_url="https://pypi.org/simple/",
            )
            # `*.python.org` admits subdomains at any depth and never the bare name.
            assert (await _fetch(sandbox, "https://www.python.org/"))[0] == "2"
            assert await _fetch(sandbox, "https://python.org/") == "403"
            # A path rule admits that path alone.
            assert await _fetch(sandbox, "https://example.com/other") == "403"
            again = await backend.acquire(key, spec)
            assert again.instance_id == sandbox.instance_id
            assert (await _fetch(again, "https://example.com/"))[0] == "2"
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_method_and_path_rules_are_enforced(tmp_path):
    backend = _backend(tmp_path)
    scoped_key, control_key = _key("methods"), _key("control")
    paths = ("/anything/*",)
    get_only = EgressRule("httpbin.org", methods=(HttpMethod.GET,), paths=paths)
    # Every verb the backend declares, so sbx is seen to accept each one.
    every = EgressRule("httpbin.org", methods=tuple(HttpMethod), paths=paths)

    async def scenario():
        try:
            capabilities = backend.declarations.capabilities
            scoped = await backend.acquire(
                scoped_key, _spec(egress=Egress.ALLOWLIST, egress_allow=(get_only,))
            )
            control = await backend.acquire(
                control_key, _spec(egress=Egress.ALLOWLIST, egress_allow=(every,))
            )
            await assert_egress_methods_conformance(
                ExecEgressMethodsSubject(scoped, capabilities, "."),
                ExecEgressMethodsSubject(control, capabilities, "."),
                allowed_url="https://httpbin.org/anything/probe",
            )
            assert (await _fetch(scoped, "https://httpbin.org/anything"))[0] == "2"
            assert await _fetch(scoped, "https://httpbin.org/get") == "403"
        finally:
            assert await backend.dispose(scoped_key) is None
            assert await backend.dispose(control_key) is None

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


@_BY_IMAGE
def test_host_tools_answer_on_each_template(tmp_path, image):
    asyncio.run(_host_tools_case(tmp_path, image, "short"))


@pytest.mark.parametrize(
    "mode", ["idle", "async", "sync", "timeout", "cancel", "sibling", "stop", "dispose"]
)
def test_host_tools_lifecycle(tmp_path, mode):
    asyncio.run(_host_tools_case(tmp_path, None, mode))


async def _host_tools_case(tmp_path, image, mode):
    backend = _backend(tmp_path)
    key = _key("host-tools")
    effects = []
    registry = HostToolRegistry()
    entered = asyncio.Event()
    tasks = []

    @sandbox_tool(source=SourceIntegrity.TRUSTED, sink=None, identity=Identity.APP)
    async def probe() -> str:
        entered.set()
        if mode == "sync":
            time.sleep(45)
        elif mode in {"async", "stop", "dispose"}:
            await asyncio.sleep(45)
        effects.append("effect")
        return "answer"

    registry.register(probe)
    layout = guest_run_layout(_WORK + "/run")
    spec = SandboxSpec(kind="host-tools", image=image, egress=Egress.CLOSED)
    started = time.monotonic()
    try:
        sandbox = await backend.acquire(key, spec)

        async def prepare(where, delay):
            await sandbox.write_file(
                where.program,
                "import os,time\nfrom pathlib import Path\nimport maf_host_tools\n"
                f"Path('{where.work}/pid').write_text(str(os.getpid()))\n"
                f"time.sleep({delay})\n"
                "print('received:' + maf_host_tools.call('probe'), flush=True)\n",
                working_directory=".",
            )
            await sandbox.write_file(
                where.shim, host_tool_shim(call_timeout=90), working_directory="."
            )
            await sandbox.write_file(where.work + "/prepared", "", working_directory=".")

        await prepare(
            layout, 45 if mode == "idle" else 120 if mode in {"timeout", "cancel", "sibling"} else 0
        )
        transport = asyncio.create_task(
            host_tool_calls_over_exec(
                sandbox,
                HostToolRun(registry, key=key),
                layout,
                timeout=6 if mode == "timeout" else 80,
                interpreter="python3",
            )
        )
        tasks.append(transport)
        sibling = None
        if mode == "sibling":
            other = guest_run_layout(_WORK + "/other")
            await prepare(other, 12)
            sibling = asyncio.create_task(
                host_tool_calls_over_exec(
                    sandbox,
                    HostToolRun(registry, key=key),
                    other,
                    timeout=40,
                    interpreter="python3",
                )
            )
            tasks.append(sibling)
        if mode in {"cancel", "sibling"}:
            await asyncio.sleep(6)
            transport.cancel()
        if mode in {"stop", "dispose"}:
            await asyncio.wait_for(entered.wait(), timeout=20)
            if mode == "stop":
                stopped = await backend._sbx("stop", sandbox.name)
                assert stopped.returncode == 0
            else:
                assert await backend.dispose(key, kind=spec.kind) is None
        if mode == "timeout":
            with pytest.raises(SandboxProgramTimeout):
                await transport
        elif mode in {"cancel", "sibling"}:
            with pytest.raises(asyncio.CancelledError):
                await transport
        elif mode in {"stop", "dispose"}:
            with pytest.raises(SandboxRunActivityLost, match="effect may have completed"):
                await transport
            assert effects == ["effect"]
            assert backend.retirement_reason(sandbox.instance_id)
        else:
            result = await transport
            assert result.exit_code == 0, result
            assert "received:answer" in result.stdout
            assert effects == ["effect"]
        if sibling is not None:
            result = await sibling
            assert result.exit_code == 0 and "received:answer" in result.stdout
            assert effects == ["effect"]
        if mode not in {"stop", "dispose"}:
            assert await sandbox.stat_file(layout.calls, working_directory=".") is None
            pid = (
                await sandbox.read_file(layout.work + "/pid", working_directory=".", max_bytes=64)
            ).decode()
            gone = await sandbox.exec(
                ["sh", "-c", f"test ! -e /proc/{int(pid)}"], working_directory=".", timeout=10
            )
            assert gone.exit_code == 0
            leftovers = await sandbox.exec(
                ["sh", "-c", 'set -- /tmp/maf-sbx-*.pgid; test "$#" -eq 1'],
                working_directory=".",
                timeout=10,
            )
            # The inspection exec owns its own pid file while it runs.
            assert leftovers.exit_code == 0, leftovers
        print(
            json.dumps(
                {
                    "host_tools": mode,
                    "image": image or "default",
                    "effects": len(effects),
                    "seconds": round(time.monotonic() - started, 3),
                }
            ),
            flush=True,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert await backend.dispose(key, kind=spec.kind) is None
