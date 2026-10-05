"""Opt-in live qualification of all 32 supported Linux capability combinations."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import replace

import pytest
from maf_sandbox import (
    CallerContext,
    Capability,
    Egress,
    Isolation,
    IsolationScope,
    SandboxKey,
    SandboxObserver,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_call_scope_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
)
from maf_sandbox.maf import sandboxed_tool
from maf_sandbox.testing import InMemoryStore

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerResult

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_CAPABILITY_IMAGE", "")
_PROXY = os.environ.get("MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE", "")
_GRANTS = ("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL")
_BITS = {"CHOWN": 0, "DAC_OVERRIDE": 1, "KILL": 5, "SETGID": 6, "SETUID": 7}
_COMBINATIONS = [tuple(g for i, g in enumerate(_GRANTS) if mask & (1 << i)) for mask in range(32)]
_WORK = "/maf-sandbox/work"
pytestmark = pytest.mark.skipif(
    not _IMAGE or not shutil.which("docker"),
    reason="needs MAF_SANDBOX_DOCKER_CAPABILITY_IMAGE with Python, sh, sleep and ln",
)


def _docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def observer():
    """Observe host PID identities independently of the workload's filesystem and commands."""
    name = "maf-capability-observer-" + uuid.uuid4().hex
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--pid",
        "host",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--memory",
        "128m",
        "--pids-limit",
        "64",
        _IMAGE,
        "sleep",
        "3600",
    )
    try:
        yield name
    finally:
        _docker("rm", "-f", name)


def _processes(observer, pids):
    script = """import json, sys
from pathlib import Path
found = {}
for pid in json.loads(sys.argv[1]):
    try:
        fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
    except FileNotFoundError:
        continue
    found[str(pid)] = fields[19]
print(json.dumps(found))
"""
    return json.loads(_docker("exec", observer, "python3", "-c", script, json.dumps(pids)))


def _snapshot(observer, container):
    pids = [int(line.strip()) for line in _docker("top", container, "-eo", "pid").splitlines()[1:]]
    result = _processes(observer, pids)
    assert len(result) >= 2, "independent observer did not see the detached guest process"
    return result


def _assert_gone(observer, snapshot):
    remaining = _processes(observer, list(snapshot))
    assert not {pid for pid, start in snapshot.items() if remaining.get(pid) == start}


def _spec():
    return SandboxSpec(
        kind="capability-qualification",
        image=_IMAGE,
        isolation_scope=IsolationScope.CALL,
        requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
    )


@pytest.mark.parametrize("grants", _COMBINATIONS, ids=lambda g: "+".join(g) or "none")
def test_capability_file_network_and_call_boundaries(grants):
    async def scenario():
        backend = await DockerSandboxBackend.create(DockerSandboxConfig(cap_add=grants))
        scope = "capability-files-" + uuid.uuid4().hex
        first = SandboxKey(scope, "thread", "agent", call_id="first")
        second = replace(first, call_id="second")
        spec = _spec()
        try:
            sandbox, sibling = await asyncio.gather(
                backend.acquire(first, spec), backend.acquire(second, spec)
            )
            assert sandbox.instance_id != sibling.instance_id
            status = await sandbox.exec(
                ["cat", "/proc/self/status"], working_directory=_WORK, timeout=10
            )
            assert status.exit_code == 0
            fields = dict(line.split(":", 1) for line in status.stdout.splitlines())
            mask = sum(1 << _BITS[g] for g in grants)
            for field in ("CapEff", "CapPrm", "CapBnd"):
                assert int(fields[field], 16) == mask
            assert int(fields["CapInh"], 16) == int(fields["CapAmb"], 16) == 0
            assert int(fields["NoNewPrivs"]) == 1
            settings = json.loads(_docker("inspect", sandbox.instance_id))[0]
            assert settings["HostConfig"]["NetworkMode"] == "none"
            assert not settings["Mounts"]
            network = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    (
                        "import socket; assert not open('/proc/net/route').read().splitlines()[1:]; "
                        "s=socket.socket(); s.settimeout(1); assert s.connect_ex(('1.1.1.1',443)) != 0"
                    ),
                ],
                working_directory=_WORK,
                timeout=10,
            )
            assert network.exit_code == 0, network.stderr

            def subject(value):
                return PosixGuestSubject(value, _WORK, backend.declarations.capabilities)

            await assert_files_in_conformance(subject(sandbox))
            await assert_files_out_conformance(subject(sandbox))
            with pytest.raises(NotImplementedError):
                await sandbox.remove("child", working_directory=_WORK)
            with pytest.raises(NotImplementedError):
                await sandbox.reclaim("child", working_directory=_WORK, timeout=1)

            async def another():
                return subject(sibling)

            async def dispose_first():
                assert await backend.dispose(first, kind=spec.kind) is None

            async def dispose_second():
                assert await backend.dispose(second, kind=spec.kind) is None

            results = await assert_call_scope_conformance(
                subject(sandbox), another, dispose_first, dispose_second
            )
            assert {r.probe.name for r in results if r.skipped} == {
                "the-listing-holds-only-this-calls-files"
            }
        finally:
            assert not (await backend.dispose_scope(scope, "thread")).undisposed

    asyncio.run(scenario())


@pytest.mark.parametrize("grants", _COMBINATIONS, ids=lambda g: "+".join(g) or "none")
def test_capability_disposal_on_every_exit(grants, observer):
    async def scenario():
        backend = await DockerSandboxBackend.create(DockerSandboxConfig(cap_add=grants))
        scope = "capability-lifecycle-" + uuid.uuid4().hex
        events = []
        failures = []

        async def record_failure(failure):
            failures.append(failure)

        class Records(SandboxObserver):
            def tool_call_ended(self, event):
                events.append(event)

        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER, observer=Records())
        spec = _spec()
        seen = []
        ready = asyncio.Event()
        snapshots = []
        original = backend._docker
        refuse_disposal = False

        async def controlled(*args, **kwargs):
            if refuse_disposal and args[:2] == ("rm", "-f"):
                return _DockerResult(1, b"", "injected disposal refusal")
            return await original(*args, **kwargs)

        backend._docker = controlled

        def build(session):
            async def run(mode: str) -> str:
                key = session.key()
                assert not isinstance(key, str)
                sandbox = await session.acquire(key)
                assert not isinstance(sandbox, str), sandbox
                seen.append((key, sandbox.instance_id))
                await sandbox.write_file("residue", mode, working_directory="/tmp")
                result = await sandbox.exec(
                    [
                        "python3",
                        "-c",
                        (
                            "import subprocess; subprocess.Popen(['sleep','300'], "
                            "start_new_session=True, stdin=subprocess.DEVNULL, "
                            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
                        ),
                    ],
                    working_directory=_WORK,
                    timeout=10,
                )
                assert result.exit_code == 0, result.stderr
                snapshots.append(_snapshot(observer, sandbox.instance_id))
                ready.set()
                if mode == "failure":
                    raise RuntimeError("injected body failure")
                if mode == "timeout":
                    await sandbox.exec(["sleep", "30"], working_directory=_WORK, timeout=0.2)
                if mode == "cancel":
                    await asyncio.Event().wait()
                return "finished"

            return run

        tool = sandboxed_tool(
            build,
            router=router,
            spec=spec,
            name="qualify",
            on_reclaim_failure=record_failure,
            agent_id="agent",
            context=CallerContext(
                current_scope=lambda: scope,
                current_thread_id=lambda: "thread",
                list_files=InMemoryStore.list,
            ),
        )[0]
        try:
            for mode in ("success", "failure", "timeout", "cancel", "disposal-refused"):
                ready.clear()
                refuse_disposal = mode == "disposal-refused"
                task = asyncio.create_task(tool.func(mode=mode))
                if mode == "cancel":
                    await asyncio.wait_for(ready.wait(), timeout=60)
                    task.cancel()
                if mode in {"failure", "timeout", "cancel"}:
                    error = {
                        "failure": RuntimeError,
                        "timeout": TimeoutError,
                        "cancel": asyncio.CancelledError,
                    }[mode]
                    with pytest.raises(error):
                        await task
                else:
                    await task
                key, instance = seen[-1]
                if mode == "disposal-refused":
                    assert failures[-1].disposal == "failed"
                    assert failures[-1].key == key
                    assert _processes(observer, list(snapshots[-1]))
                    next_key = replace(key, call_id=uuid.uuid4().hex)
                    next_sandbox = await router.acquire(next_key, spec)
                    assert next_sandbox.instance_id != instance
                    assert await next_sandbox.stat_file("residue", working_directory="/tmp") is None
                    refuse_disposal = False
                    assert await router.dispose(next_key) is None
                    assert await router.dispose(key) is None
                else:
                    assert events[-1].unclean == 0
                _assert_gone(observer, snapshots[-1])
                assert not _docker("ps", "-aq", "--filter", "id=" + instance)
            assert len({instance for _, instance in seen}) == 5
        finally:
            refuse_disposal = False
            backend._docker = original
            assert not (await router.dispose_scope(scope, "thread")).undisposed

    asyncio.run(scenario())


@pytest.mark.skipif(not _PROXY, reason="needs MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE")
@pytest.mark.parametrize("grants", _COMBINATIONS, ids=lambda g: "+".join(g) or "none")
def test_capability_allowlist_enforcement(grants):
    async def scenario():
        backend = await DockerSandboxBackend.create(
            DockerSandboxConfig(cap_add=grants, egress_proxy_image=_PROXY)
        )
        key = SandboxKey("capability-egress-" + uuid.uuid4().hex, "thread", "agent", call_id="call")
        spec = replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",))
        request = """import sys, urllib.request, urllib.error
try:
    with urllib.request.urlopen(sys.argv[1], timeout=20) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
except urllib.error.URLError as error:
    if str(error.reason) != 'Tunnel connection failed: 403 Forbidden':
        raise
    print(403)
"""
        try:
            sandbox = await backend.acquire(key, spec)
            for url, status in (
                ("https://mcr.microsoft.com/v2/", "200"),
                ("https://pypi.org/simple/", "403"),
            ):
                result = await sandbox.exec(
                    ["python3", "-c", request, url], working_directory=_WORK, timeout=30
                )
                assert result.exit_code == 0, result.stderr
                assert result.stdout.strip() == status
            proxy = json.loads(_docker("inspect", sandbox.container_name + "-proxy"))[0]
            assert proxy["HostConfig"]["CapDrop"] == ["ALL"]
            assert not proxy["HostConfig"]["CapAdd"]
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())
