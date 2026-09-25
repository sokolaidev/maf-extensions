"""Live tests against a real ``wslc`` and a real container image.

Skipped unless ``wslc`` is on ``PATH`` and ``MAF_SANDBOX_WSLC_E2E_IMAGE`` names an image to
run — the offline suite already pins every command line, and what is left to prove is that
those commands do what this backend believes when a real container is on the other end.
The image is read from the environment rather than written down here so a local tag never
becomes a committed one; any Linux image with ``sh`` and ``sleep`` will do.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _fixture_probe import _the_image_ships
from maf_sandbox import (
    Capability,
    Cleanup,
    Egress,
    EgressRule,
    Isolation,
    OsFamily,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_call_scope_conformance,
    assert_egress_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_reach_conformance,
    assert_reclaim_conformance,
)

from maf_sandbox_wslc import WslcReapResult, WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _container_name, _proxy_name
from maf_sandbox_wslc._reap import listing_rows

_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_IMAGE")
_PROXY_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE")
#: A non-root image without work_dir: the file plane must create it for the guest.
_NONROOT_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_NONROOT_IMAGE")
_GUEST_OWNED_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE")

_WORK = "/maf-sandbox/work"


@pytest.mark.parametrize("image", [_IMAGE, _NONROOT_IMAGE], ids=["root", "nonroot"])
def test_acquire_prepares_base_before_exec_and_repairs_warm_reuse(image):
    if not image:
        pytest.skip("needs the corresponding WSLC E2E image")

    async def scenario():
        backend = WslcSandboxBackend(WslcSandboxConfig())
        key = _key("acquire-work-dir-" + uuid.uuid4().hex[:10])
        spec = SandboxSpec(
            kind="base",
            image=image,
            work_dir="/maf-sandbox/acquire-test",
            requires=frozenset({Capability.EXEC}),
        )
        try:
            sandbox = await backend.acquire(key, spec)
            first_id = sandbox.instance_id
            for state in ("cold", "warm", "repaired", "restarted"):
                if state == "repaired":
                    removed = await backend._wslc(
                        "container",
                        "exec",
                        "--user",
                        "0",
                        "-w",
                        "/",
                        sandbox.container_name,
                        "rm",
                        "-rf",
                        "--",
                        spec.work_dir,
                        timeout=30,
                    )
                    assert removed.returncode == 0
                if state == "restarted":
                    stopped = await backend._wslc(
                        "container", "stop", sandbox.container_name, timeout=30
                    )
                    assert stopped.returncode == 0
                if state != "cold":
                    sandbox = await backend.acquire(key, spec)
                assert sandbox.instance_id == first_id
                result = await sandbox.exec(
                    "printf ok > marker; cat marker",
                    working_directory=spec.work_dir,
                    timeout=30,
                )
                assert result.exit_code == 0, (state, result)
                assert result.stdout == "ok"
        finally:
            assert await backend.dispose(key, kind=spec.kind) is None

    asyncio.run(scenario())


pytestmark = pytest.mark.skipif(
    shutil.which("wslc") is None or not _IMAGE,
    reason="needs wslc on PATH and MAF_SANDBOX_WSLC_E2E_IMAGE naming a runnable image",
)


def _spec() -> SandboxSpec:
    return SandboxSpec(kind="e2e", image=_IMAGE)


def _key(scope: str) -> SandboxKey:
    return SandboxKey(scope=scope, thread_id="thread-1", agent_id="devops-engineer")


def test_write_checks_remove_private_host_copies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "-"
    sentinel.write_bytes(b"host content")
    copies = tmp_path / "copies"
    copies.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(copies))
    backend = WslcSandboxBackend(WslcSandboxConfig())
    key = _key("e2e-stat-copy-" + uuid.uuid4().hex)
    copied = []
    run = backend._wslc

    async def observe(*args, **kwargs):
        result = await run(*args, **kwargs)
        if args[:2] == ("container", "cp") and args[2] != "-":
            destination = Path(args[-1])
            assert destination.is_absolute() and destination.parent.parent == copies
            if destination.exists():
                copied.append(destination.stat().st_size)
        return result

    monkeypatch.setattr(backend, "_wslc", observe)

    async def scenario():
        try:
            sandbox = await backend.acquire(key, _spec())
            prepared = await sandbox.exec(
                "printf hello > small; dd if=/dev/zero of=large bs=1048576 count=64; "
                "ln -s /tmp linked-parent; ln -s /tmp/out linked-leaf",
                working_directory=_WORK,
                timeout=30,
            )
            assert prepared.exit_code == 0, prepared.stderr
            for guest in ("small", "large", "missing"):
                await sandbox.write_file(guest, b"replacement", working_directory=_WORK)
                assert not list(copies.iterdir())
            for guest in ("linked-parent/out", "linked-leaf", "small/out"):
                with pytest.raises((ValueError, NotADirectoryError)):
                    await sandbox.write_file(guest, b"refused", working_directory=_WORK)
                assert not list(copies.iterdir())
            assert 5 in copied and 64 * 1024 * 1024 in copied and 0 in copied
            assert sentinel.read_bytes() == b"host content"
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())
    assert not list(copies.iterdir())
    assert not _names_on_the_machine(_container_name(key, _spec().kind))


def _service_listening(container: str, *ports: int) -> None:
    """Wait until ``container`` listens on every one of ``ports``.

    Reads the kernel's socket tables instead of connecting: a one-shot ``nc -l`` would spend
    itself answering the probe.
    """
    wanted = {f"{port:04X}" for port in ports}
    deadline = time.monotonic() + 30
    while True:
        tables = subprocess.run(
            [
                "wslc",
                "container",
                "exec",
                container,
                "/bin/busybox",
                "cat",
                "/proc/net/tcp",
                "/proc/net/tcp6",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        listening = {
            fields[1].rsplit(":", 1)[-1]
            for fields in (line.split() for line in tables.splitlines())
            if len(fields) > 3 and fields[3] == "0A"
        }
        if wanted <= listening:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"{container} is not listening on {sorted(ports)}")
        time.sleep(0.2)


def _http_stub(port: int, response: str, *, ipv6: bool = False) -> str:
    """Shell serving ``response`` (``printf`` escapes) on ``port`` once each request head is read.

    A Go HTTP client drops a reply that arrives before its request is written.
    """
    handler = (
        'cr=$(printf "\\r"); while IFS= read -r line && [ "$line" != "$cr" ]; do :; done; '
        f"printf {shlex.quote(response)}"
    )
    bind = " -s ::" if ipv6 else ""
    return f"/bin/busybox nc -lk -p {port}{bind} -e /bin/sh -c {shlex.quote(handler)}"


def _listed_name(row: dict) -> str:
    """The name a listing row carries, under either field the CLI has used for it.

    Raises rather than returning nothing, because the callers below assert on absence: a row
    this cannot read has to fail the test, not answer it.
    """
    value = row.get("Name", row.get("Names"))
    if not isinstance(value, str) or not value:
        raise AssertionError(f"a listing row carries no name: {row}")
    return value


def _names_on_the_machine(name: str) -> list[str]:
    """Every container currently named ``name``, read with wslc rather than the backend.

    Read as JSON: the table view truncates the NAME column, so scanning it can miss a
    container that is still there and pass an emptiness assertion that should fail.
    """
    listing = subprocess.run(
        ["wslc", "container", "list", "-a", "--format", "json", "--filter", f"name={name}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return [n for row in listing_rows(listing) if (n := _listed_name(row)) == name]


@pytest.mark.parametrize(
    "command,capability",
    [("sh", Capability.EXEC), ("test", Capability.FILES_IN), ("mv", Capability.FILES_IN)],
)
def test_command_probe_refusal_retries_after_the_guest_command_is_restored(command, capability):
    backend = WslcSandboxBackend(WslcSandboxConfig())
    key = _key(f"e2e-command-{uuid.uuid4()}")
    spec = replace(_spec(), requires=frozenset())
    required = replace(spec, requires=frozenset({capability}))
    backup = "/tmp/maf-command-backup"

    async def scenario():
        try:
            sandbox = await backend.acquire(key, spec)
            changed = await sandbox.exec(
                f"cp /bin/{command} {backup} && rm -f /bin/{command} /usr/bin/{command}",
                working_directory="/",
                timeout=30,
            )
            assert changed.exit_code == 0, changed.stderr
            for _ in range(2):
                with pytest.raises(SandboxCapabilityNotSupported, match=command):
                    await backend.acquire(key, required)
            weaker = await backend.acquire(key, spec)
            assert weaker.instance_id == sandbox.instance_id

            # WSLC argv execution can restore sh even while string execution is unavailable.
            restored = await sandbox.exec(
                ["cp", backup, f"/bin/{command}"], working_directory="/", timeout=30
            )
            assert restored.exit_code == 0, restored.stderr
            recovered = await backend.acquire(key, required)
            assert recovered.instance_id == sandbox.instance_id
            assert (await backend.acquire(key, required)).instance_id == sandbox.instance_id
            if capability == Capability.FILES_IN:
                await recovered.write_file("marker", b"restored", working_directory=_WORK)
                ran = await recovered.exec(["cat", "marker"], working_directory=_WORK, timeout=30)
            else:
                ran = await recovered.exec("printf restored", working_directory="/", timeout=30)
            assert ran.exit_code == 0 and ran.stdout == "restored", ran.stderr
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())
    assert not _names_on_the_machine(_container_name(key, spec.kind))


@pytest.mark.parametrize("confined", [False, True])
def test_router_disposes_each_call_and_preserves_another_kind(confined):
    scope = f"e2e-{uuid.uuid4()}"
    backend = WslcSandboxBackend(WslcSandboxConfig())
    router = SandboxRouter(backends=[backend], min_isolation=Isolation.CONTAINER)
    spec = replace(_spec(), confined_to_guest_call_path=confined)
    key = _key(scope)

    async def scenario() -> None:
        sibling = await backend.acquire(key, replace(spec, kind="sibling"))
        for owner in ("first", "second"):
            admission = await router.enter_call(key, spec, owner=owner)
            assert admission.rung is Cleanup.DISPOSE
            sandbox = await router.acquire(key, spec, _admission=admission)
            result = await sandbox.exec(
                ["test", "!", "-e", "/tmp/call-residue"], working_directory="/", timeout=30
            )
            assert result.exit_code == 0
            result = await sandbox.exec(
                ["touch", "/tmp/call-residue"], working_directory="/", timeout=30
            )
            assert result.exit_code == 0
            assert (
                await router.finish_call(
                    key, spec, admission=admission, sandbox=sandbox, owner=owner, timeout=30
                )
                is None
            )
            assert _names_on_the_machine(_container_name(key, spec.kind)) == []
            assert _names_on_the_machine(sibling.container_name) == [sibling.container_name]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(backend.dispose_scope(scope, key.thread_id))


@pytest.mark.parametrize(
    ("allowlist", "leftover"),
    [(False, "workload"), (True, "workload"), (True, "proxy-network"), (True, "network")],
)
def test_reap_after_creator_process_exits(allowlist, leftover):
    assert _IMAGE is not None
    if allowlist and not _PROXY_IMAGE:
        pytest.skip("needs MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE for infrastructure cleanup")
    scope = f"e2e-reap-{uuid.uuid4()}"
    backend = WslcSandboxBackend(
        WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE if allowlist else None)
    )
    spec = SandboxSpec(
        kind="e2e",
        image=_IMAGE,
        egress=Egress.ALLOWLIST if allowlist else Egress.CLOSED,
        egress_allow=("mcr.microsoft.com",) if allowlist else (),
    )
    names = [
        _container_name(
            SandboxKey(
                scope=scope + "-other" if agent == "unrelated" else scope,
                thread_id="thread-1",
                agent_id=agent,
            ),
            spec.kind,
            backend._egress_id(spec),
        )
        for agent in ("old", "running", "fresh", "unrelated")
    ]
    creator = """
import asyncio, json, os, sys
from maf_sandbox import Egress, SandboxKey, SandboxSpec
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
async def main():
    backend = WslcSandboxBackend(WslcSandboxConfig(egress_proxy_image=sys.argv[3] or None))
    names = []
    for agent in ('old', 'running', 'fresh', 'unrelated'):
        sandbox = await backend.acquire(
            SandboxKey(scope=sys.argv[1] + '-other' if agent == 'unrelated' else sys.argv[1],
                       thread_id='thread-1', agent_id=agent),
            SandboxSpec(kind='e2e', image=sys.argv[2],
                        egress=Egress.ALLOWLIST if sys.argv[3] else Egress.CLOSED,
                        egress_allow=('mcr.microsoft.com',) if sys.argv[3] else ()))
        names.append(sandbox.container_name)
    print(json.dumps(names), flush=True)
    os._exit(0)
asyncio.run(main())
"""
    cleaner = """
import asyncio, json, sys
from dataclasses import asdict
from datetime import timedelta
from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
backend = WslcSandboxBackend(WslcSandboxConfig())
assert not backend._registry
print(json.dumps(asdict(asyncio.run(backend.reap(timedelta(seconds=30), scope=sys.argv[1])))))
"""

    def command(*args):
        return subprocess.run(
            ["wslc", *args], capture_output=True, text=True, check=True, timeout=60
        ).stdout

    def inspect(name):
        response = subprocess.run(
            ["wslc", "container", "inspect", name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if response.returncode:
            assert response.returncode == 1 and json.loads(response.stdout) == []
            return None
        return json.loads(response.stdout)[0]

    try:
        started = datetime.now(UTC)
        created = subprocess.run(
            [
                sys.executable,
                "-c",
                creator,
                scope,
                _IMAGE,
                (_PROXY_IMAGE or "") if allowlist else "",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=240,
        )
        assert json.loads(created.stdout) == names
        old, running, fresh, unrelated = names
        command("container", "stop", unrelated)
        stop_requested_at = datetime.now(UTC)
        command("container", "stop", old)
        metadata = inspect(old)
        assert metadata is not None
        assert metadata["State"]["Running"] is False
        assert metadata["State"]["Status"] == "exited"
        created_at = datetime.fromisoformat(metadata["Created"])
        stopped_at = datetime.fromisoformat(metadata["State"]["FinishedAt"])
        # WSLC lifecycle timestamps and the operator can read different host/VM clocks.
        skew = timedelta(seconds=10)
        assert started - skew <= created_at <= stopped_at <= datetime.now(UTC) + skew
        listing = listing_rows(
            command("container", "list", "-a", "--format", "json", "--filter", f"name={old}")
        )
        # Only the two fields the reaper reads back off a listing. Every other one has changed
        # type or gone between CLI versions, and nothing here consumes them.
        listed = next(row for row in listing if _listed_name(row) == old)
        assert str(listed.get("Id", listed.get("ID"))).startswith(metadata["Id"][:12])
        assert stop_requested_at - skew <= stopped_at <= datetime.now(UTC) + skew
        print(
            json.dumps(
                {
                    "leftover": leftover,
                    "allowlist": allowlist,
                    "created": metadata["Created"],
                    "finished": metadata["State"]["FinishedAt"],
                    "operator_now": datetime.now(UTC).isoformat(),
                    "listed": listed,
                }
            )
        )
        if allowlist:
            network = json.loads(command("network", "inspect", old + "-net"))[0]
            assert network["Internal"] is True
            requested_at = datetime.fromisoformat(
                network["Labels"]["maf-sandbox.network-created-at"]
            )
            assert started <= requested_at <= datetime.now(UTC)
        if leftover != "workload":
            command("container", "remove", old)
        if leftover == "network":
            command("container", "remove", "-f", old + "-proxy")
        expires_at = stopped_at + timedelta(seconds=30)
        time.sleep(max(0, (expires_at - datetime.now(UTC)).total_seconds()) + 1)
        command("container", "stop", fresh)
        cleaned = subprocess.run(
            [sys.executable, "-c", cleaner, scope],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
        result = json.loads(cleaned.stdout)
        assert result == {
            "disposed": int(leftover == "workload"),
            "proxies_removed": int(allowlist and leftover != "network"),
            "networks_removed": int(allowlist),
            "failures": [],
        }
        assert inspect(old) is None
        running_metadata = inspect(running)
        assert running_metadata is not None and running_metadata["State"]["Running"] is True
        assert inspect(fresh) is not None
        assert inspect(unrelated) is not None
        if allowlist:
            assert inspect(old + "-proxy") is None
            for retained in (running, fresh, unrelated):
                assert inspect(retained + "-proxy") is not None
                assert json.loads(command("network", "inspect", retained + "-net"))
            absent = subprocess.run(
                ["wslc", "network", "inspect", old + "-net"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert absent.returncode == 1 and json.loads(absent.stdout) == []
        repeated = subprocess.run(
            [sys.executable, "-c", cleaner, scope],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
        assert json.loads(repeated.stdout) == {
            "disposed": 0,
            "proxies_removed": 0,
            "networks_removed": 0,
            "failures": [],
        }
    finally:

        async def cleanup():
            for name in names:
                await backend._remove(name + "-proxy")
                await backend._remove(name)
                await backend._remove_network(name + "-net")

        asyncio.run(cleanup())


@pytest.mark.skipif(not _PROXY_IMAGE, reason="needs MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE")
@pytest.mark.parametrize("stage", ["inspect", "remove"])
def test_reap_continues_after_a_real_proxy_disappears(stage, monkeypatch):
    scope = f"e2e-reap-{uuid.uuid4()}"
    backend = WslcSandboxBackend(WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE))

    async def scenario():
        try:
            sandbox = await backend.acquire(
                _key(scope),
                SandboxSpec(
                    kind="e2e",
                    image=_IMAGE,
                    egress=Egress.ALLOWLIST,
                    egress_allow=("mcr.microsoft.com",),
                ),
            )
            name = sandbox.container_name
            command = backend._wslc
            stopped = await command("container", "stop", name)
            assert stopped.returncode == 0
            metadata = await command("container", "inspect", name)
            finished = datetime.fromisoformat(
                json.loads(metadata.stdout_text)[0]["State"]["FinishedAt"]
            )
            wait = (finished + timedelta(seconds=1) - datetime.now(UTC)).total_seconds()
            assert wait < 15, "WSL and Windows clocks must be within the probe's wait budget"
            await asyncio.sleep(max(0, wait) + 1)
            proxy = await command("container", "inspect", name + "-proxy")
            proxy_id = json.loads(proxy.stdout_text)[0]["Id"]
            reads = 0
            removed = False

            async def disappear(*args, **kwargs):
                nonlocal reads, removed
                if args == ("container", "inspect", proxy_id):
                    reads += 1
                trigger = args[:2] == ("container", stage) and args[-1] == proxy_id
                if trigger and not removed and (stage == "remove" or reads == 2):
                    response = await command("container", "remove", "-f", proxy_id)
                    assert response.returncode == 0
                    removed = True
                return await command(*args, **kwargs)

            monkeypatch.setattr(backend, "_wslc", disappear)
            result = await backend.reap(timedelta(seconds=1), scope=scope)
            assert removed
            assert result == WslcReapResult(1, 0, 1)
            assert _names_on_the_machine(name) == []
            assert _names_on_the_machine(name + "-proxy") == []
            assert not _network_present(name + "-net")
        finally:
            await backend.dispose_scope(scope, "thread-1")

    asyncio.run(scenario())


class TestALiveContainer:
    def test_a_file_written_survives_into_exec_and_the_container_is_reused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file(
                "/maf-sandbox/work/nested/deep/main.bicep",
                "param naïve string\n",
                working_directory="/maf-sandbox/work",
            )

            read_back = await sandbox.exec(
                ["cat", "nested/deep/main.bicep"], working_directory="/maf-sandbox/work", timeout=60
            )
            assert read_back.exit_code == 0, read_back.stderr
            assert read_back.stdout == "param naïve string\n"

            failing = await sandbox.exec(
                "exit 7", working_directory="/maf-sandbox/work", timeout=60
            )
            assert failing.exit_code == 7

            warm = await backend.acquire(_key(scope), _spec())
            assert warm.container_name == sandbox.container_name
            still_there = await warm.exec(
                ["cat", "nested/deep/main.bicep"], working_directory="/maf-sandbox/work", timeout=60
            )
            assert still_there.stdout == "param naïve string\n"

            await backend.dispose(_key(scope))
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_dispose_scope_finds_the_container_by_its_labels(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> str:
            sandbox = await backend.acquire(_key(scope), _spec())
            # Purged through a second backend: the labels on the container, not this
            # process's memory, are what a conversation delete has to find.
            purged = await WslcSandboxBackend(WslcSandboxConfig()).dispose_scope(scope, "thread-1")
            assert purged.disposed >= 1
            assert purged.undisposed is None
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


def test_resource_limits_reach_the_workload_cgroup():
    scope = f"e2e-{uuid.uuid4()}"
    backend = WslcSandboxBackend(WslcSandboxConfig(memory="256M", cpus=0.5))

    async def scenario() -> None:
        sandbox = await backend.acquire(_key(scope), _spec())
        limits = await sandbox.exec(
            ["cat", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/cpu.max"],
            working_directory="/",
            timeout=60,
        )
        assert limits.exit_code == 0, limits.stderr
        assert limits.stdout.splitlines() == [str(256 * 1024 * 1024), "50000 100000"]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestTheDeclaredGuestFamilyAgainstARealContainer:
    """The constant `os_families` states, backed by a container rather than matched on paper.

    The offline suite pins the declaration and the router's refusal; what needs a live `wslc`
    is that the guest on the other end really does take POSIX argv and a `/`-rooted path.
    """

    def test_a_workload_requiring_posix_is_served_and_runs(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)
        spec = SandboxSpec(kind="e2e", image=_IMAGE, requires_os_family=OsFamily.POSIX)

        async def scenario() -> None:
            router.ensure_can_serve(spec)
            sandbox = await router.acquire(_key(scope), spec)
            # At `/` rather than `_WORK`: what this asserts is the guest's grammar and argv,
            # which every Linux image answers for, not a directory some of them carry.
            ran = await sandbox.exec(
                ["sh", "-c", "printf posix"], working_directory="/", timeout=60
            )
            assert ran.exit_code == 0, ran.stderr
            assert ran.stdout == "posix"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


def _network_present(name: str) -> bool:
    """Whether a network named ``name`` exists, read with wslc (the JSON list, not the table)."""
    listing = subprocess.run(
        ["wslc", "network", "list", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return any(_listed_name(row) == name for row in listing_rows(listing))


@pytest.mark.skipif(
    not _PROXY_IMAGE,
    # `curl` has to be in the *image*, which we cannot check from here; the Bicep sandbox has it.
    reason="needs MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE naming a built proxy image (and curl in the image)",
)
class TestAllowlistEgress:
    """The whole point of ALLOWLIST: an allowed host is reachable and a denied one is not.

    This exercises the topology the offline tests only assert the command lines for — an
    internal network with no route out except through a filtering proxy — so it needs a real
    ``wslc`` and an image with ``curl`` (the Bicep sandbox image has one).
    """

    def _config(self) -> WslcSandboxConfig:
        return WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE)

    def test_oversized_keys_acquire_and_drain_with_the_callers_key(self):
        key = SandboxKey(scope=f"e2e-{uuid.uuid4()}", thread_id="thread", agent_id="x" * 150_000)
        creator = WslcSandboxBackend(self._config())
        reader = WslcSandboxBackend(self._config())
        events = []
        reader.observe_egress(events.append)

        async def scenario():
            try:
                sandbox = await creator.acquire(
                    key, replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("example.com",))
                )
                result = await sandbox.exec(
                    ["curl", "-I", "--max-time", "10", "https://blocked.invalid"],
                    working_directory="/",
                    timeout=20,
                )
                assert result.exit_code != 0
                await reader._drain_attributed_proxy(sandbox.container_name)
                assert events == []
                creator.observe_egress(events.append)
                assert (await creator.dispose(key)) is None
                assert [event.key for event in events] == [key]
                assert [d.host for event in events for d in event.decisions] == ["blocked.invalid"]
            finally:
                await creator.dispose_scope(key.scope, key.thread_id)

        asyncio.run(scenario())

    def test_a_conversations_disposal_files_a_calls_window_under_that_call(self):
        """A disposal selects on scope, thread and agent, so it reaches a sandbox a call inside
        the conversation acquired — and the window is that call's, not the caller's."""
        scope = f"e2e-{uuid.uuid4()}"
        call = SandboxKey(scope=scope, thread_id="thread", agent_id="agent", call_id="call-1")
        conversation = SandboxKey(scope=scope, thread_id="thread", agent_id="agent")
        creator = WslcSandboxBackend(self._config())
        reader = WslcSandboxBackend(self._config())
        events = []
        reader.observe_egress(events.append)

        async def scenario():
            try:
                sandbox = await creator.acquire(
                    call, replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("example.com",))
                )
                result = await sandbox.exec(
                    ["curl", "-I", "--max-time", "10", "https://blocked.invalid"],
                    working_directory="/",
                    timeout=20,
                )
                assert result.exit_code != 0
                assert (await reader.dispose(conversation)) is None
                assert [event.key for event in events] == [call]
                assert [d.host for event in events for d in event.decisions] == ["blocked.invalid"]
            finally:
                await creator.dispose_scope(scope, conversation.thread_id)

        asyncio.run(scenario())

    @pytest.mark.parametrize("orphan", [False, True])
    def test_a_fresh_backend_reports_proxy_decisions_with_lossless_attribution(self, orphan):
        key = SandboxKey(
            scope=f"e2e-{uuid.uuid4()} / \u2603",
            thread_id="thread / 1",
            agent_id="agent" * 30,
        )
        creator = WslcSandboxBackend(self._config())
        reader = WslcSandboxBackend(self._config())
        events = []
        reader.observe_egress(events.append)

        async def scenario():
            try:
                sandbox = await creator.acquire(
                    key, replace(_spec(), egress=Egress.ALLOWLIST, egress_allow=("example.com",))
                )
                result = await sandbox.exec(
                    ["curl", "-I", "--max-time", "10", "https://blocked.invalid"],
                    working_directory="/",
                    timeout=20,
                )
                assert result.exit_code != 0
                if orphan:
                    assert (await creator._remove(sandbox.container_name)).removed
                assert (await reader.dispose_scope(key.scope, key.thread_id)).undisposed is None
                assert [event.key for event in events] == [key]
                assert [d.host for event in events for d in event.decisions] == ["blocked.invalid"]
                assert events[0].unreadable is None
            finally:
                await creator.dispose_scope(key.scope, key.thread_id)

        asyncio.run(scenario())

    def _curl_status(
        self,
        sandbox,
        url: str,
        *,
        method: str | None = None,
        force_proxy: bool = False,
        follow_redirects: bool = False,
    ) -> tuple[int, str]:
        args = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "25"]
        if follow_redirects:
            args.append("--location")
        if method == "HEAD":
            args.append("--head")
        elif method is not None:
            args += ["-X", method]
        if force_proxy:
            args += ["--noproxy", "", "--proxy", f"http://{sandbox.container_name}-proxy:3128"]
        args.append(url)
        result = asyncio.run(
            sandbox.exec(
                args,
                working_directory="/maf-sandbox/work",
                timeout=45,
            )
        )
        return result.exit_code, result.stdout.strip()

    def test_an_allowed_host_answers_a_denied_one_does_not_and_teardown_leaves_nothing(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e", image=_IMAGE, egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",)
        )

        # Acquire before the try so a failure here surfaces as itself, not as an
        # `UnboundLocalError` from the teardown assertions that follow.
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        net = sandbox.container_name + "-net"
        try:
            assert _network_present(net)
            subject = PosixGuestSubject(
                sandbox=sandbox,
                working_directory=_WORK,
                capabilities=backend.declarations.capabilities,
            )
            asyncio.run(
                assert_egress_conformance(
                    subject,
                    allowed_url="https://mcr.microsoft.com/v2/",
                    denied_url="https://pypi.org/simple/",
                )
            )
            # A rejected CONNECT has no origin HTTP response for curl to report.
            _, denied_status = self._curl_status(sandbox, "https://pypi.org/simple/")
            assert denied_status == "000", denied_status
        finally:
            purged = asyncio.run(backend.dispose_scope(scope, "thread-1")).disposed
        assert purged == 1
        assert _names_on_the_machine(sandbox.container_name) == []
        assert not _network_present(net)

    def test_resource_limits_reach_the_proxy_and_it_still_serves(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(replace(self._config(), memory="256M", cpus=0.5))
        spec = SandboxSpec(
            kind="e2e", image=_IMAGE, egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",)
        )
        try:
            sandbox = asyncio.run(backend.acquire(_key(scope), spec))
            limits = asyncio.run(
                backend._wslc(
                    "container",
                    "exec",
                    _proxy_name(sandbox.container_name),
                    "cat",
                    "/sys/fs/cgroup/memory.max",
                    "/sys/fs/cgroup/cpu.max",
                    timeout=30,
                )
            )
            assert limits.returncode == 0, limits.stderr_text
            assert limits.stdout_text.splitlines() == [str(256 * 1024 * 1024), "50000 100000"]
            _, allowed_status = self._curl_status(sandbox, "https://mcr.microsoft.com/v2/")
            assert allowed_status != "000", allowed_status
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_container_on_the_outbound_network_cannot_use_the_proxy(self):
        """The tunnel listens on the sandbox network only; its outbound address refuses."""
        assert _IMAGE is not None
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e", image=_IMAGE, egress=Egress.ALLOWLIST, egress_allow=("mcr.microsoft.com",)
        )

        def wslc(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["wslc", *args], capture_output=True, text=True, timeout=120, check=False
            )

        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        proxy = sandbox.container_name + "-proxy"
        try:
            inspected = json.loads(wslc("container", "inspect", proxy).stdout)[0]
            legs = inspected["NetworkSettings"]["Networks"]
            internal = legs[sandbox.container_name + "-net"]["IPAddress"]
            outbound = legs["bridge"]["IPAddress"]
            logs = wslc("container", "logs", proxy)
            assert f'"addr":"{internal}:3128"' in logs.stdout + logs.stderr
            assert self._curl_status(sandbox, "https://mcr.microsoft.com/v2/") == (0, "200")
            # A refusal rather than a timeout: the probe reaches the proxy and nothing listens.
            probe = wslc(
                "container",
                "run",
                "--rm",
                "--network",
                "bridge",
                "--entrypoint",
                "curl",
                _IMAGE,
                "-sv",
                "-o",
                "/dev/null",
                "--max-time",
                "10",
                "--proxy",
                f"http://{outbound}:3128",
                "https://mcr.microsoft.com/v2/",
            )
            assert probe.returncode == 7 and "Connection refused" in probe.stderr, probe
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_tls_method_path_and_plaintext_controls(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("mcr.microsoft.com", methods=("GET",), paths=("/v2/",)),),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            allowed_code, allowed_status = self._curl_status(
                sandbox, "https://mcr.microsoft.com/v2/"
            )
            assert allowed_code == 0 and allowed_status == "200"
            assert (
                self._curl_status(sandbox, "https://mcr.microsoft.com/v2/", method="POST")[1]
                == "403"
            )
            inner_connect = asyncio.run(
                sandbox.exec(
                    [
                        "curl",
                        "--http1.1",
                        "-s",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "--max-time",
                        "25",
                        "-X",
                        "CONNECT",
                        "--request-target",
                        "mcr.microsoft.com:443",
                        "https://mcr.microsoft.com/v2/",
                    ],
                    working_directory=_WORK,
                    timeout=45,
                )
            )
            assert inner_connect.stdout.strip() == "403", inner_connect
            assert self._curl_status(sandbox, "https://mcr.microsoft.com/other")[1] == "403"
            assert (
                self._curl_status(sandbox, "http://mcr.microsoft.com/v2/", force_proxy=True)[1]
                == "502"
            )
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_literal_star_method_does_not_allow_get(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("mcr.microsoft.com", methods=("*",), paths=("/v2/",)),),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            assert self._curl_status(sandbox, "https://mcr.microsoft.com/v2/")[1] == "403"
            assert (
                self._curl_status(sandbox, "https://mcr.microsoft.com/v2/", method="*")[1] != "403"
            )
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    @pytest.mark.skipif(not _NONROOT_IMAGE, reason="needs a non-root WSLC E2E image")
    def test_existing_root_owned_work_dir_accepts_the_proxy_ca(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e",
            image=_NONROOT_IMAGE,
            work_dir="/",
            requires=frozenset({Capability.EXEC}),
            egress=Egress.ALLOWLIST,
            egress_allow=("mcr.microsoft.com",),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            result = asyncio.run(
                sandbox.exec(
                    ["test", "-r", "/maf-sandbox-proxy-ca.crt"], working_directory="/", timeout=30
                )
            )
            assert result.exit_code == 0, result.stderr
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_websocket_refusal_is_observed_as_a_denial(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        events = []
        backend.observe_egress(events.append)
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=(EgressRule("mcr.microsoft.com", methods=("GET",), paths=("/v2/",)),),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            result = asyncio.run(
                sandbox.exec(
                    [
                        "curl",
                        "--http1.1",
                        "-s",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "-H",
                        "Connection: Upgrade",
                        "-H",
                        "Upgrade: websocket",
                        "https://mcr.microsoft.com/v2/",
                    ],
                    working_directory=_WORK,
                    timeout=45,
                )
            )
            assert result.exit_code == 0 and result.stdout.strip() == "403"
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
        assert [(d.decision, d.host, d.port) for e in events for d in e.decisions] == [
            ("DENY", "mcr.microsoft.com", 443)
        ]

    def test_public_tls_on_another_port_and_overlapping_wildcard_rules(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=(
                EgressRule("*.badssl.com", methods=("GET",), paths=("/",)),
                EgressRule("tls-v1-2.badssl.com", methods=("HEAD",), paths=("/",)),
            ),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            url = "https://tls-v1-2.badssl.com:1012/"
            assert self._curl_status(sandbox, url) == (0, "200")
            assert self._curl_status(sandbox, url, method="HEAD") == (0, "200")
            assert self._curl_status(sandbox, url, method="POST")[1] == "403"
            assert self._curl_status(sandbox, "https://badssl.com/")[1] == "000"
            assert self._curl_status(sandbox, "https://self-signed.badssl.com/")[1] == "502"
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_private_plaintext_requires_the_host_opt_in_and_private_address(self):
        assert _PROXY_IMAGE is not None
        scope = f"e2e-{uuid.uuid4()}"
        service = f"maf-private-service-{uuid.uuid4().hex[:12]}"
        allowed = WslcSandboxBackend(
            WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE, allow_private_http=True)
        )
        default = WslcSandboxBackend(WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE))
        key = _key(scope)
        try:
            subprocess.run(
                [
                    "wslc",
                    "container",
                    "run",
                    "-d",
                    "--name",
                    service,
                    "--network",
                    "bridge",
                    "--entrypoint",
                    "/bin/sh",
                    _PROXY_IMAGE,
                    "-c",
                    _http_stub(8080, "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok")
                    + " & "
                    + _http_stub(
                        8081,
                        "HTTP/1.1 302 Found\\r\\nLocation: http://mcr.microsoft.com/v2/\\r\\n"
                        "Content-Length: 0\\r\\n\\r\\n",
                    ),
                ],
                check=True,
                capture_output=True,
            )
            _service_listening(service, 8080, 8081)
            inspected = json.loads(
                subprocess.check_output(["wslc", "container", "inspect", service])
            )
            private_ip = inspected[0]["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
            spec = SandboxSpec(
                kind="e2e",
                image=_IMAGE,
                egress=Egress.ALLOWLIST,
                egress_allow=(private_ip, "mcr.microsoft.com"),
            )
            sandbox = asyncio.run(allowed.acquire(key, spec))
            assert self._curl_status(sandbox, f"http://{private_ip}:8080/", force_proxy=True) == (
                0,
                "200",
            )
            assert (
                self._curl_status(sandbox, "http://mcr.microsoft.com/v2/", force_proxy=True)[1]
                == "502"
            )
            assert (
                self._curl_status(sandbox, "http://mcr.microsoft.com:443/v2/", force_proxy=True)[1]
                == "502"
            )
            assert (
                self._curl_status(
                    sandbox,
                    f"http://{private_ip}:8081/",
                    force_proxy=True,
                    follow_redirects=True,
                )[1]
                == "502"
            )
            sandbox = asyncio.run(default.acquire(key, spec))
            assert (
                self._curl_status(sandbox, f"http://{private_ip}:8080/", force_proxy=True)[1]
                == "502"
            )
        finally:
            asyncio.run(allowed.dispose_scope(scope, "thread-1"))
            asyncio.run(default.dispose_scope(scope, "thread-1"))
            subprocess.run(
                ["wslc", "container", "remove", "-f", service], check=False, capture_output=True
            )

    def test_private_tls_validates_the_upstream_certificate(self):
        assert _PROXY_IMAGE is not None
        scope = f"e2e-{uuid.uuid4()}"
        network = f"maf-private-{uuid.uuid4().hex[:12]}"
        service = f"maf-tls-relay-{uuid.uuid4().hex[:12]}"
        upstream = socket.gethostbyname("mcr.microsoft.com")
        relay = (
            f"printf '#!/bin/sh\\nexec /bin/busybox nc {upstream} 443\\n' >/tmp/relay; "
            "chmod +x /tmp/relay; exec /bin/busybox nc -lk -p 8443 -e /tmp/relay"
        )
        backend = WslcSandboxBackend(self._config())
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=("mcr.microsoft.com",),
        )
        subprocess.run(["wslc", "network", "create", network], check=True, capture_output=True)
        try:
            subprocess.run(
                [
                    "wslc",
                    "container",
                    "run",
                    "-d",
                    "--name",
                    service,
                    "--network",
                    network,
                    "--network-alias",
                    "mcr.microsoft.com",
                    "--entrypoint",
                    "/bin/sh",
                    _PROXY_IMAGE,
                    "-c",
                    relay,
                ],
                check=True,
                capture_output=True,
            )
            _service_listening(service, 8443)
            sandbox = asyncio.run(backend.acquire(_key(scope), spec))
            subprocess.run(
                ["wslc", "network", "connect", network, sandbox.container_name + "-proxy"],
                check=True,
                capture_output=True,
            )
            assert self._curl_status(sandbox, "https://mcr.microsoft.com:8443/v2/") == (0, "200")
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
            subprocess.run(
                ["wslc", "container", "remove", "-f", service], check=False, capture_output=True
            )
            subprocess.run(["wslc", "network", "remove", network], check=False, capture_output=True)

    @pytest.mark.parametrize("allow_private_http", [False, True], ids=["tls-only", "dev-http"])
    def test_selected_private_ipv6_enforces_transport_methods_paths_and_isolation(
        self, allow_private_http
    ):
        assert _PROXY_IMAGE is not None
        suffix = uuid.uuid4().hex[:12]
        supplied_network = os.environ.get("MAF_SANDBOX_WSLC_E2E_IPV6_NETWORK")
        network = supplied_network or f"maf-ipv6-{suffix}"
        service = f"maf-ipv6-service-{suffix}"
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(
            WslcSandboxConfig(
                egress_proxy_image=_PROXY_IMAGE, allow_private_http=allow_private_http
            )
        )
        events = []
        backend.observe_egress(events.append)

        def command(*args, **kwargs):
            return subprocess.run(
                ["wslc", *args], check=True, capture_output=True, text=True, timeout=60, **kwargs
            )

        if not supplied_network:
            command("network", "create", "--subnet", f"fd42:1407:{suffix[:4]}::/64", network)
        service_created = False
        try:
            inspected = json.loads(command("network", "inspect", network).stdout)[0]
            if not supplied_network and inspected.get("EnableIPv6") is False:
                version = command("--version").stdout.strip()
                pytest.skip(
                    f"{version} created the IPv6 subnet with EnableIPv6=false; "
                    "private IPv6 HTTP/TLS is unverified (#1407)"
                )
            assert inspected.get("EnableIPv6") is True, inspected
            upstream = socket.gethostbyname("mcr.microsoft.com")
            relay = (
                f"printf '#!/bin/sh\\nexec /bin/busybox nc {upstream} 443\\n' >/tmp/relay; "
                "chmod +x /tmp/relay; "
                "/bin/busybox nc -lk -p 8443 -s :: -e /tmp/relay & "
                + _http_stub(
                    8080, "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok", ipv6=True
                )
            )
            command(
                "container",
                "run",
                "-d",
                "--name",
                service,
                "--network",
                network,
                "--entrypoint",
                "/bin/sh",
                _PROXY_IMAGE,
                "-c",
                relay,
            )
            service_created = True
            _service_listening(service, 8080, 8443)

            def ipv6_address(name):
                inspected = json.loads(command("network", "inspect", network).stdout)[0]
                endpoints = [e for e in inspected["Containers"].values() if e["Name"] == name]
                assert len(endpoints) == 1, inspected
                address = endpoints[0].get("IPv6Address")
                assert address, endpoints[0]
                ip = ipaddress.ip_interface(address).ip
                assert ip in ipaddress.ip_network("fc00::/7")
                return str(ip)

            address = ipv6_address(service)
            spec = SandboxSpec(
                kind="e2e",
                image=_IMAGE,
                egress=Egress.ALLOWLIST,
                egress_allow=(
                    EgressRule("private.test", methods=("GET",), paths=("/allowed",)),
                    EgressRule("mcr.microsoft.com", methods=("GET",), paths=("/v2/",)),
                    "interface.test",
                ),
            )
            sandbox = asyncio.run(backend.acquire(_key(scope), spec))
            proxy = sandbox.container_name + "-proxy"
            command("network", "connect", network, proxy)
            proxy_address = ipv6_address(proxy)
            command(
                "container",
                "exec",
                "-i",
                "-u",
                "0",
                proxy,
                "/bin/sh",
                "-c",
                "cat >> /etc/hosts",
                input=f"{address} private.test mcr.microsoft.com\n{proxy_address} interface.test\n",
            )
            assert self._curl_status(
                sandbox, "http://private.test:8080/allowed", force_proxy=True
            ) == (0, "200" if allow_private_http else "502")
            assert self._curl_status(sandbox, "https://mcr.microsoft.com:8443/v2/") == (0, "200")
            for url in ("http://private.test:8080/other", "https://mcr.microsoft.com:8443/other"):
                assert self._curl_status(sandbox, url, force_proxy=True) == (0, "403")
            assert self._curl_status(
                sandbox, "https://mcr.microsoft.com:8443/v2/", method="POST"
            ) == (0, "403")
            assert self._curl_status(sandbox, "http://interface.test:8080/", force_proxy=True) == (
                0,
                "502",
            )
            direct = asyncio.run(
                sandbox.exec(
                    [
                        "curl",
                        "-s",
                        "--noproxy",
                        "*",
                        "--max-time",
                        "3",
                        f"http://[{address}]:8080/allowed",
                    ],
                    working_directory=_WORK,
                    timeout=15,
                )
            )
            assert direct.exit_code != 0, direct
        finally:
            try:
                assert asyncio.run(backend.dispose_scope(scope, "thread-1")).undisposed is None
            finally:
                try:
                    if service_created:
                        command("container", "remove", "-f", service)
                finally:
                    if not supplied_network:
                        command("network", "remove", network)
        decisions = {(d.decision, d.host) for event in events for d in event.decisions}
        assert decisions >= {
            ("ALLOW", "mcr.microsoft.com"),
            ("DENY", "mcr.microsoft.com"),
            ("ALLOW" if allow_private_http else "DENY", "private.test"),
            ("DENY", "interface.test"),
        }

    def test_ipv6_loopback_link_local_and_metadata_addresses_are_denied(self):
        assert _PROXY_IMAGE is not None
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(
            WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE, allow_private_http=True)
        )
        events = []
        backend.observe_egress(events.append)
        addresses = {
            "loopback.test": "::1",
            "linklocal.test": "fe80::1",
            "metadata.test": "fd00:ec2::254",
        }
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=(*addresses, "bridge-gateway.test", "internal-gateway.test"),
        )
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        try:
            proxy = sandbox.container_name + "-proxy"
            for network, host in (
                ("bridge", "bridge-gateway.test"),
                (sandbox.container_name + "-net", "internal-gateway.test"),
            ):
                inspected = json.loads(
                    subprocess.check_output(["wslc", "network", "inspect", network])
                )[0]
                gateways = [
                    entry["Gateway"]
                    for entry in inspected["IPAM"]["Config"]
                    if entry.get("Gateway")
                ]
                if network == "bridge":
                    assert gateways
                if gateways:
                    addresses[host] = gateways[0]
            subprocess.run(
                [
                    "wslc",
                    "container",
                    "exec",
                    "-i",
                    "-u",
                    "0",
                    proxy,
                    "/bin/sh",
                    "-c",
                    "cat >> /etc/hosts",
                ],
                input="".join(f"{address} {host}\n" for host, address in addresses.items()),
                text=True,
                check=True,
                capture_output=True,
            )
            for host in addresses:
                assert (
                    self._curl_status(sandbox, f"http://{host}:8080/", force_proxy=True)[1] == "502"
                )
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
        assert {(d.decision, d.host) for e in events for d in e.decisions} >= {
            ("DENY", host) for host in addresses
        }

    def test_private_http_exception_is_rechecked_after_dns_changes(self):
        assert _PROXY_IMAGE is not None
        scope = f"e2e-{uuid.uuid4()}"
        private_network = f"maf-private-{uuid.uuid4().hex[:12]}"
        other_network = f"maf-rebind-{uuid.uuid4().hex[:12]}"
        first_service = f"maf-private-service-{uuid.uuid4().hex[:12]}"
        second_service = f"maf-rebound-service-{uuid.uuid4().hex[:12]}"
        response = _http_stub(8080, "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok")

        def start_service(name: str, network: str) -> None:
            assert _PROXY_IMAGE is not None
            subprocess.run(
                [
                    "wslc",
                    "container",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    network,
                    "--network-alias",
                    "private.test",
                    "--entrypoint",
                    "/bin/sh",
                    _PROXY_IMAGE,
                    "-c",
                    response,
                ],
                check=True,
                capture_output=True,
            )
            _service_listening(name, 8080)

        backend = WslcSandboxBackend(
            WslcSandboxConfig(egress_proxy_image=_PROXY_IMAGE, allow_private_http=True)
        )
        spec = SandboxSpec(
            kind="e2e",
            image=_IMAGE,
            egress=Egress.ALLOWLIST,
            egress_allow=("private.test",),
        )
        try:
            subprocess.run(
                ["wslc", "network", "create", private_network], check=True, capture_output=True
            )
            subprocess.run(
                ["wslc", "network", "create", "--subnet", "203.0.113.0/24", other_network],
                check=True,
                capture_output=True,
            )
            start_service(first_service, private_network)
            sandbox = asyncio.run(backend.acquire(_key(scope), spec))
            proxy = sandbox.container_name + "-proxy"
            subprocess.run(
                ["wslc", "network", "connect", private_network, proxy],
                check=True,
                capture_output=True,
            )
            assert self._curl_status(sandbox, "http://private.test:8080/", force_proxy=True) == (
                0,
                "200",
            )
            subprocess.run(
                ["wslc", "network", "connect", other_network, proxy],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["wslc", "container", "remove", "-f", first_service],
                check=True,
                capture_output=True,
            )
            start_service(second_service, other_network)
            for _ in range(5):
                status = self._curl_status(sandbox, "http://private.test:8080/", force_proxy=True)
                logs = subprocess.check_output(["wslc", "container", "logs", proxy]).decode()
                if "plaintext HTTP requires a private upstream address" in logs:
                    assert status[1] == "502"
                    break
                time.sleep(1)
            else:
                pytest.fail("the proxy did not classify the newly resolved address")
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
            for service in (first_service, second_service):
                subprocess.run(
                    ["wslc", "container", "remove", "-f", service],
                    check=False,
                    capture_output=True,
                )
            for network in (private_network, other_network):
                subprocess.run(
                    ["wslc", "network", "remove", network], check=False, capture_output=True
                )


class TestTheSharedConformanceSuites:
    """Live file and exec conformance, with deletion and reclamation refused."""

    def test_it_answers_the_files_in_probes(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_files_in_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.declarations.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_it_answers_the_exec_probes_on_its_own_sandbox(self):
        """On a fresh container: the timeout probe discards it, exactly as on docker.

        `dispose_scope` afterwards has to stay clean over a container the timeout already
        removed — teardown reaching a name that is already gone is half the assertion.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            results = await assert_exec_conformance(
                PosixGuestSubject(
                    sandbox=sandbox,
                    working_directory=_WORK,
                    capabilities=backend.declarations.capabilities,
                )
            )
            assert not [r for r in results if r.skipped]

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_reclaim_suite_refuses_an_undeclared_capability(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            with pytest.raises(ValueError, match="declares no RECLAIM"):
                await assert_reclaim_conformance(
                    PosixGuestSubject(
                        sandbox=sandbox,
                        working_directory=_WORK,
                        capabilities=backend.declarations.capabilities,
                    )
                )

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_delete_suite_refuses_an_undeclared_capability(self):
        """The gated runner refuses a subject with no FILES_DELETE — and that refusal is the answer.

        The whole-suite gate raises rather than skipping, so a run against this backend cannot
        report probe results at all; asserting the refusal keeps the call honest (it is what
        the coverage wiring looks for) without pretending skips that the runner never emits.
        The capability itself is withheld structurally — `remove` raises NotImplementedError.
        Not for want of the check, which the engine now answers for every ancestor a delete
        would descend through (#495), but because nothing here implements a removal and no
        branch of that stat reports an owner. So unlike acas there is nothing to measure:
        no mechanism exists behind the gate.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            with pytest.raises(ValueError, match="declares no FILES_DELETE"):
                await assert_files_delete_conformance(
                    PosixGuestSubject(
                        sandbox=sandbox,
                        working_directory=_WORK,
                        capabilities=backend.declarations.capabilities,
                    )
                )

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _NONROOT_IMAGE,
    reason="needs MAF_SANDBOX_WSLC_E2E_NONROOT_IMAGE naming an image whose USER is not root",
)
class TestAGuestThatIsNotRoot:
    """Inputs and the guest's own files are both cleaned by container disposal."""

    def _spec(self, image: str | None = None) -> SandboxSpec:
        return SandboxSpec(kind="e2e-nonroot", image=image or _NONROOT_IMAGE, work_dir=_WORK)

    def test_disposal_removes_a_call_directory_the_file_plane_wrote(self):
        """Inputs are removed with their container."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            with pytest.raises(NotImplementedError, match="RECLAIM"):
                await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)
            assert await backend.dispose(_key(scope), kind=self._spec().kind) is None
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_guest_command_still_runs_as_the_image_user(self):
        """The half that must not move: ``exec`` is the guest program's own."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            whoami = await sandbox.exec(["id", "-u"], working_directory="/", timeout=60)
            assert whoami.exit_code == 0, whoami.stderr
            assert whoami.stdout.strip() not in ("", "0")
            assert sandbox.guest_principal == "unprivileged"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_guest_can_modify_inputs_and_create_outputs(self):
        """The file plane's inputs and missing directories belong to the image's user."""
        assert not _the_image_ships(_WORK, str(_NONROOT_IMAGE)), (
            f"this fixture must not ship {_WORK}: what the ownership checks below read is "
            "what acquire created"
        )
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            prepared = await sandbox.exec(["test", "-d", _WORK], working_directory="/", timeout=60)
            assert prepared.exit_code == 0, "acquire must prepare work_dir before writing inputs"
            planted = f"{_WORK}/call-a1b2c3/host_note"
            await sandbox.write_file(planted, "# the host wrote this\n", working_directory=_WORK)
            result = await sandbox.exec(
                [
                    "sh",
                    "-ec",
                    f"""
                    test "$(id -u)" != 0
                    for path in . call-a1b2c3 {planted}; do
                        test "$(stat -c %u:%g "$path")" = "$(id -u):$(id -g)"
                    done
                    echo appended >> {planted}
                    echo output > result.txt
                    mkdir sub
                    cat {planted}
                    echo rewritten > {planted}
                    rm {planted}
                """,
                ],
                working_directory=_WORK,
                timeout=60,
            )
            assert result.exit_code == 0, result.stderr
            assert result.stdout == "# the host wrote this\nappended\n"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_disposal_removes_a_tree_the_two_principals_share(self):
        """Disposal removes both the host's files and the guest's output."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/program.py", "print(1)\n", working_directory=_WORK
            )
            wrote = await sandbox.exec(
                [
                    "sh",
                    "-ec",
                    f"mkdir {call_directory}/work; echo mine > {call_directory}/work/output.txt",
                ],
                working_directory="/",
                timeout=60,
            )
            assert wrote.exit_code == 0, wrote.stderr

            assert await backend.dispose(_key(scope), kind=self._spec().kind) is None
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.skipif(
    not _GUEST_OWNED_IMAGE,
    reason="needs MAF_SANDBOX_WSLC_E2E_GUEST_OWNED_IMAGE with a non-root USER owning work_dir",
)
def test_a_guest_owned_work_dir_answers_the_reach_probe():
    scope = f"e2e-{uuid.uuid4()}"
    backend = WslcSandboxBackend(WslcSandboxConfig())
    spec = SandboxSpec(kind="e2e-guest-owned", image=_GUEST_OWNED_IMAGE, work_dir=_WORK)
    assert _the_image_ships(_WORK, str(_GUEST_OWNED_IMAGE)), (
        f"this fixture must ship a guest-owned {_WORK}: preserving a directory that is "
        "already there is what the stat comparison below measures"
    )

    async def scenario() -> None:
        sandbox = await backend.acquire(_key(scope), spec)
        before = await sandbox.exec(
            ["sh", "-ec", f'test "$(id -u)" != 0; test -w {_WORK}; stat -c %u:%g:%a {_WORK}'],
            working_directory="/",
            timeout=60,
        )
        assert before.exit_code == 0, before.stderr
        results = await assert_reach_conformance(
            PosixGuestSubject(
                sandbox=sandbox,
                working_directory=_WORK,
                capabilities=backend.declarations.capabilities,
            )
        )
        assert len([result for result in results if not result.skipped]) == 1
        after = await sandbox.exec(
            ["stat", "-c", "%u:%g:%a", _WORK], working_directory="/", timeout=60
        )
        assert after.exit_code == 0, after.stderr
        assert after.stdout == before.stdout

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(backend.dispose_scope(scope, "thread-1"))


@pytest.mark.parametrize("network", [False, True])
def test_instance_disposal_conforms_against_engine_inventory(network):
    from maf_sandbox.conformance import assert_instance_disposal_conformance

    from maf_sandbox_wslc._backend import _network_name, _sandbox_labels

    backend = WslcSandboxBackend(WslcSandboxConfig())
    key = _key(f"e2e-instance-{uuid.uuid4()}")
    spec = _spec()

    async def create(kind, variant):
        selected = SandboxSpec(kind=kind, image=_IMAGE)
        name = _container_name(key, kind, variant)
        if network:
            await backend._ensure_network(_network_name(name), key, selected)
        args = [
            "container",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            _network_name(name) if network else "none",
        ]
        for label, value in _sandbox_labels(key, selected).items():
            args += ["--label", f"{label}={value}"]
        args += [str(_IMAGE), "sleep", "infinity"]
        created = await backend._wslc(*args, timeout=60)
        assert created.returncode == 0
        identity = created.stdout_text.strip()
        inspected = await backend._inspect_disposal_target(identity)
        assert inspected is not None
        return str(inspected["Id"])

    async def exists(identity):
        return await backend._inspect_disposal_target(identity) is not None

    async def scenario():
        try:
            target = await create(spec.kind, "target")
            same_kind = await create(spec.kind, "sibling")
            other_kind = await create("sibling-kind", "")
            await assert_instance_disposal_conformance(
                backend,
                key,
                spec.kind,
                target,
                [same_kind, other_kind],
                exists,
            )
            if network:
                removed = await backend._wslc(
                    "network",
                    "inspect",
                    _network_name(_container_name(key, spec.kind, "target")),
                    timeout=60,
                )
                assert (
                    removed.returncode != 0 and "network not found" in removed.stderr_text.lower()
                )
                for kind, variant in ((spec.kind, "sibling"), ("sibling-kind", "")):
                    sibling_network = await backend._wslc(
                        "network",
                        "inspect",
                        _network_name(_container_name(key, kind, variant)),
                        timeout=60,
                    )
                    assert sibling_network.returncode == 0
            replacement = await create(spec.kind, "target")
            assert replacement != target
            assert await backend.dispose(key, kind=spec.kind, instance_id=target) is None
            assert await exists(replacement)
            assert await exists(same_kind) and await exists(other_kind)
            if network:
                replacement_network = await backend._wslc(
                    "network",
                    "inspect",
                    _network_name(_container_name(key, spec.kind, "target")),
                    timeout=60,
                )
                assert replacement_network.returncode == 0
        finally:
            failure = await backend.dispose(key)
            if network:
                for kind, variant in (
                    (spec.kind, "target"),
                    (spec.kind, "sibling"),
                    ("sibling-kind", ""),
                ):
                    await backend._remove_network(
                        _network_name(_container_name(key, kind, variant))
                    )
            assert failure is None

    asyncio.run(scenario())


@pytest.mark.parametrize("override", [None, "/image/custom-base"])
def test_relative_storage_base_conformance(override):
    from maf_sandbox.conformance import assert_storage_base_conformance

    async def scenario():
        backend = WslcSandboxBackend(WslcSandboxConfig())
        key = _key("storage-base-" + uuid.uuid4().hex[:10])
        spec = SandboxSpec(kind="storage", image=_IMAGE, work_dir=override)
        try:
            sandbox = await backend.acquire(key, spec)
            await assert_storage_base_conformance(sandbox, backend.declarations.capabilities)
            await sandbox.write_file("kept", b"warm", working_directory=".")
            stopped = await backend._wslc("container", "stop", sandbox.instance_id, timeout=30)
            assert stopped.returncode == 0
            resumed = WslcSandboxBackend(WslcSandboxConfig())
            with pytest.raises(ValueError, match="storage base"):
                await resumed.acquire(key, replace(spec, work_dir="/other/base"))
            assert not await resumed._is_listed(sandbox.container_name, all_states=False)
            second = await resumed.acquire(key, spec)
            assert second.instance_id == sandbox.instance_id
            kept = await second.exec(["cat", "kept"], working_directory=".", timeout=10)
            assert kept.exit_code == 0 and kept.stdout == "warm"
        finally:
            assert await backend.dispose(key, kind=spec.kind) is None

    asyncio.run(scenario())


class TestTheCallScopeAgainstARealEngine:
    """`maf_sandbox.conformance`'s CALL_SCOPE suite — the half a declaration cannot prove.

    This backend declares `IsolationScope.CALL`, and what entitles it to is that the key's
    `call_id` reaches the container name, the registry entry and the disposal's label filter.
    The offline tests answer a listing from what the fake is holding and read no
    `--filter label=` at all, so that two acquires differing only in `call_id` are two
    containers, and that ending one leaves the other running, is measured here or nowhere.
    """

    def test_it_answers_the_call_scope_probes(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())
        spec = _spec()
        first = replace(_key(scope), call_id="call-a")
        second = replace(_key(scope), call_id="call-b")

        def subject_over(sandbox) -> PosixGuestSubject:
            return PosixGuestSubject(
                sandbox=sandbox,
                working_directory=_WORK,
                capabilities=backend.declarations.capabilities,
            )

        async def scenario() -> None:
            sandbox = await backend.acquire(first, spec)

            async def acquire_another() -> PosixGuestSubject:
                return subject_over(await backend.acquire(second, spec))

            async def dispose_this_call() -> None:
                failure = await backend.dispose(first, kind=spec.kind)
                if failure is not None:
                    raise AssertionError(f"the call's own container was not deleted: {failure}")

            async def dispose_the_other() -> None:
                await backend.dispose(second, kind=spec.kind)

            results = await assert_call_scope_conformance(
                subject_over(sandbox), acquire_another, dispose_this_call, dispose_the_other
            )
            # This backend declares neither FILES_OUT nor FILES_LIST, so the two probes that
            # read a sandbox back skip; the three that decide the boundary must have run.
            skipped = {r.probe.name for r in results if r.skipped}
            assert skipped == {
                "the-same-name-holds-this-calls-bytes",
                "the-listing-holds-only-this-calls-files",
            }

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
