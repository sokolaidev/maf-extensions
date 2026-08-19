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
import uuid

import pytest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox.conformance import (
    PosixGuestSubject,
    assert_exec_conformance,
    assert_files_delete_conformance,
    assert_files_in_conformance,
)

from maf_sandbox_wslc import WslcSandboxBackend, WslcSandboxConfig

_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_IMAGE")
_PROXY_IMAGE = os.environ.get("MAF_SANDBOX_WSLC_E2E_PROXY_IMAGE")

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


class TestALiveContainer:
    def test_a_file_written_survives_into_exec_and_the_container_is_reused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = WslcSandboxBackend(WslcSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file(
                "/maf-sandbox/work/nested/deep/main.bicep", "param naïve string\n"
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
            assert purged >= 1
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
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
        spec = SandboxSpec(kind="e2e", image=_IMAGE, egress_allow=("mcr.microsoft.com",))

        # Acquire before the try so a failure here surfaces as itself, not as an
        # `UnboundLocalError` from the teardown assertions that follow.
        sandbox = asyncio.run(backend.acquire(_key(scope), spec))
        net = sandbox.container_name + "-net"
        try:
            assert _network_present(net)
            allowed_rc, allowed_status = self._curl_status(sandbox, "https://mcr.microsoft.com/v2/")
            _, denied_status = self._curl_status(sandbox, "https://pypi.org/simple/")

            assert allowed_rc == 0 and allowed_status.startswith("2"), allowed_status
            # curl exits non-zero and reports 000 when the proxy refuses the tunnel.
            assert denied_status == "000", denied_status
        finally:
            purged = asyncio.run(backend.dispose_scope(scope, "thread-1"))
        assert purged == 1
        assert _names_on_the_machine(sandbox.container_name) == []
        assert not _network_present(net)


class TestTheSharedConformanceSuites:
    """`maf_sandbox.conformance`'s FILES_IN, EXEC and FILES_DELETE suites, against a real guest.

    This is the backend the FILES_IN and EXEC suites exist for: it declares exactly those two
    and no pull surface, so before them nothing held it to anything (#450). The suites verify
    through `exec`, which this backend has — the probes are why that shape was chosen.

    FILES_DELETE is **called and refused**: the suite gate raises before any probe runs,
    because this backend declares no such capability — confining a removal needs the component
    walk its absent pull surface cannot provide (#125 carries the pull surface). The call is
    the wiring; the refusal is the answer, and there are no results to skip.
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
                    capabilities=backend.capabilities,
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
                    capabilities=backend.capabilities,
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
        confining a deletion needs the pull surface #125 carries — so unlike acas there is
        nothing to measure: no mechanism exists behind the gate.
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
                        capabilities=backend.capabilities,
                    )
                )

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))
