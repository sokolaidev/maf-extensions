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
from datetime import UTC, datetime, timedelta

import pytest
from maf_sandbox import Egress, Isolation, OsFamily, SandboxKey, SandboxRouter, SandboxSpec
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_egress_conformance,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
)

# Feature-detected, not floored: the published-cores gate runs this suite against every
# core the range admits, and cores before 0.23 have no Sandbox.reclaim to conform to.
try:
    from maf_sandbox.conformance import assert_reclaim_conformance
except ImportError:
    assert_reclaim_conformance = None

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig
from maf_sandbox_wslc._backend import _container_name

_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_IMAGE")
_PROXY_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE")
#: An image whose ``USER`` is not root. Every image this suite otherwise runs is root's, which is
#: what keeps the two-principal split invisible: the file plane writes as root and so does the
#: guest. A non-root image separates them, and is what ``reclaim``'s ``--user 0`` raise exists for.
_NONROOT_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_NONROOT_IMAGE")

_WORK = "/maf-sandbox/work"

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
        assert listed["StateChangedAt"] == int(stopped_at.timestamp())
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
    """`maf_sandbox.conformance`'s FILES_IN, EXEC, FILES_DELETE and RECLAIM suites, against a
    real guest.

    This is the backend the FILES_IN and EXEC suites exist for: it declares exactly those two
    and no pull surface, so before them nothing held it to anything (#450). The suites verify
    through `exec`, which this backend has — the probes are why that shape was chosen.

    FILES_DELETE is **called and refused**: the suite gate raises before any probe runs,
    because this backend declares no such capability — the filesystem path check a removal owes
    is answered inside the guest for the kinds that decide an escape (#495). The call is
    the wiring; the refusal is the answer, and there are no results to skip.

    RECLAIM is answered in full, unlike FILES_DELETE: it is gated by no capability, so this is
    the backend `reclaim` exists to prove — a mandatory removal served honestly beside a
    `remove` that keeps refusing (see `test_wslc_backend.py`'s `TestReclaim` for that refusal
    pinned offline).
    """

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

    def test_it_answers_the_reclaim_probes(self):
        if assert_reclaim_conformance is None:
            pytest.skip("this maf-sandbox predates Sandbox.reclaim (< 0.23)")
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            # The narrowing does not cross into this closure; the assert re-establishes it,
            # and the coverage check wants the suite called by name.
            assert assert_reclaim_conformance is not None
            results = await assert_reclaim_conformance(
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

    def test_the_delete_suite_refuses_an_undeclared_capability(self):
        """The gated runner refuses a subject with no FILES_DELETE — and that refusal is the answer.

        The whole-suite gate raises rather than skipping, so a run against this backend cannot
        report probe results at all; asserting the refusal keeps the call honest (it is what
        the coverage wiring looks for) without pretending skips that the runner never emits.
        The capability itself is withheld structurally — `remove` raises NotImplementedError,
        since the check a deletion owes is answered inside the guest (#495) — so unlike acas
        there is nothing to measure: no mechanism exists behind the gate.
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
    """The file plane against an image whose ``USER`` is not root.

    Every other image this suite runs is root's, so the file plane (``write_file`` writes as the
    host authority, root) and the guest (``exec`` runs as the image's ``USER``) are the same
    principal and the split is invisible. A non-root image separates them, and is what
    ``reclaim``'s ``--user 0`` raise is for: the guest cannot remove what the file plane wrote, so
    ``reclaim`` raises authority to root to take the call directory back.
    """

    def _spec(self, image: str | None = None) -> SandboxSpec:
        return SandboxSpec(kind="e2e-nonroot", image=image or _NONROOT_IMAGE, work_dir=_WORK)

    def _as_root(self, container: str, script: str) -> str:
        """One command in the container with root's authority, from outside the backend."""
        done = subprocess.run(
            ["wslc", "container", "exec", "--user", "0", container, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def test_reclaim_removes_a_call_directory_the_file_plane_wrote(self):
        """The file plane writes as root; ``reclaim`` raises to ``--user 0`` to remove it."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/note", "left behind\n", working_directory=_WORK
            )

            # The framework's own ``finally`` member, now raised to root. Without the raise this
            # raises OSError (rm exits 1, the directory leaks); with it the tree is gone.
            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            assert (
                self._as_root(
                    sandbox.container_name, f"test -d {call_directory} && echo yes || echo no"
                )
                == "no"
            )

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

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_the_guest_cannot_rewrite_what_the_host_wrote(self):
        """A file the file plane planted as root stays the host's: the non-root guest can
        neither rewrite nor delete it, so a call's inputs survive the guest that read them.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            planted = f"{_WORK}/call-a1b2c3/host_note"
            await sandbox.write_file(planted, "# the host wrote this\n", working_directory=_WORK)

            rewritten = await sandbox.exec(
                ["sh", "-c", f"echo '# tampered' > {planted}"], working_directory="/", timeout=60
            )
            assert rewritten.exit_code != 0

            deleted = await sandbox.exec(
                ["sh", "-c", f"rm -f {planted}"], working_directory="/", timeout=60
            )
            assert deleted.exit_code != 0

            # The guest can still *read* what it could not rewrite or delete (the file is the
            # host's, mode 0644), which is the half that makes the call's inputs survivable.
            intact = await sandbox.exec(["cat", planted], working_directory="/", timeout=60)
            assert intact.exit_code == 0, intact.stderr
            assert intact.stdout == "# the host wrote this\n"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_reclaim_removes_a_tree_the_two_principals_share(self):
        """The host's files beside the guest's, under one directory, removed in one walk."""
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), self._spec())
            call_directory = f"{_WORK}/abc123def456"
            await sandbox.write_file(
                f"{call_directory}/program.py", "print(1)\n", working_directory=_WORK
            )
            guest = await sandbox.exec(["id", "-u"], working_directory="/", timeout=60)
            guest_uid = guest.stdout.strip()
            chown = (
                f"mkdir -p {call_directory}/work && "
                f"chown -R {guest_uid}:{guest_uid} {call_directory}/work"
            )
            self._as_root(sandbox.container_name, chown)
            wrote = await sandbox.exec(
                ["sh", "-c", f"echo mine > {call_directory}/work/output.txt"],
                working_directory="/",
                timeout=60,
            )
            assert wrote.exit_code == 0, wrote.stderr

            await sandbox.reclaim(call_directory, working_directory=_WORK, timeout=60)

            assert (
                self._as_root(
                    sandbox.container_name, f"test -d {call_directory} && echo yes || echo no"
                )
                == "no"
            )

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
