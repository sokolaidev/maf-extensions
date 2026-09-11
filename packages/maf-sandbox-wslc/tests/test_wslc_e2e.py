"""Live tests against a real ``wslc`` and a real container image.

Skipped unless ``wslc`` is on ``PATH`` and ``MAF_SANDBOX_WSLC_E2E_IMAGE`` names an image to
run — the offline suite already pins every command line, and what is left to prove is that
those commands do what this backend believes when a real container is on the other end.
The image is read from the environment rather than written down here so a local tag never
becomes a committed one; any Linux image with ``sh`` and ``sleep`` will do.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from _fixture_probe import _the_image_ships
from maf_sandbox import (
    Capability,
    Cleanup,
    Egress,
    Isolation,
    OsFamily,
    SandboxCapabilityNotSupported,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
)
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_egress_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_reach_conformance,
    assert_reclaim_conformance,
)

from maf_sandbox_wslc import WslcReapResult, WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _container_name

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
    return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")


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
    rows = json.loads(listing) if listing.strip() else []
    return [row["Name"] for row in rows if row.get("Name") == name]


@pytest.mark.parametrize(
    "command,capability", [("sh", Capability.EXEC), ("test", Capability.FILES_IN)]
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
                agent_dir=agent,
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
                       thread_id='thread-1', agent_dir=agent),
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
        listing = json.loads(
            command("container", "list", "-a", "--format", "json", "--filter", f"name={old}")
        )
        listed = next(row for row in listing if row["Name"] == old)
        assert listed["CreatedAt"] == int(created_at.timestamp())
        # The state-change event and inspected process exit have separate timestamps.
        listed_stop = datetime.fromtimestamp(listed["StateChangedAt"], UTC)
        assert stop_requested_at - skew <= listed_stop <= datetime.now(UTC) + skew
        print(
            json.dumps(
                {
                    "leftover": leftover,
                    "allowlist": allowlist,
                    "created": metadata["Created"],
                    "finished": metadata["State"]["FinishedAt"],
                    "operator_now": datetime.now(UTC).isoformat(),
                    "listed_created": listed["CreatedAt"],
                    "listed_state_changed": listed["StateChangedAt"],
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
    rows = json.loads(listing) if listing.strip() else []
    return any(row.get("Name") == name for row in rows)


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
        key = SandboxKey(scope=f"e2e-{uuid.uuid4()}", thread_id="thread", agent_dir="x" * 150_000)
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

    @pytest.mark.parametrize("orphan", [False, True])
    def test_a_fresh_backend_reports_proxy_decisions_with_lossless_attribution(self, orphan):
        key = SandboxKey(
            scope=f"e2e-{uuid.uuid4()} / \u2603",
            thread_id="thread / 1",
            agent_dir="agent" * 30,
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

    def _curl_status(self, sandbox, url: str) -> tuple[int, str]:
        result = asyncio.run(
            sandbox.exec(
                ["sh", "-c", f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 25 {url}"],
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
            # wslc-specific, stronger than the shared contract: the deny is L3, so curl cannot
            # open the tunnel and reports `000`, not an L7 proxy's HTTP answer.
            _, denied_status = self._curl_status(sandbox, "https://pypi.org/simple/")
            assert denied_status == "000", denied_status
        finally:
            purged = asyncio.run(backend.dispose_scope(scope, "thread-1")).disposed
        assert purged == 1
        assert _names_on_the_machine(sandbox.container_name) == []
        assert not _network_present(net)


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
    """The root file plane and non-root guest are both cleaned by container disposal."""

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
