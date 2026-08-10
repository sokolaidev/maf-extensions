"""Live tests against a real ``docker`` engine and a real container image.

Skipped unless the ``docker`` client is on ``PATH`` and ``MAF_SANDBOX_DOCKER_E2E_IMAGE`` names
an image to run — but unlike the wslc live suite, those gates are *satisfiable on this
repository's own CI runners*, where Docker is preinstalled, so ``tests.yml`` sets the variables
and this module runs on every pull request. It is the acceptance gate for the ``FILES_OUT``
protocol: the offline suite pins every command line, and what is left to prove is that a real
engine does what this backend believes — that a declared output written by a workload stats and
comes back byte-identical, that an over-cap output is refused before its content moves, and that
a symlinked output is refused on the tar entry's type bit.

Deliberately lightweight: a tiny image, no model, seconds not minutes. The image is read from
the environment rather than written down here so a local tag never becomes a committed one; any
Linux image with ``sh``, ``sleep`` and a way to write a file will do.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid

import pytest
from maf_sandbox import (
    Capability,
    EntryKind,
    Isolation,
    SandboxKey,
    SandboxRouter,
    SandboxSpec,
    SandboxTransferCapExceeded,
    TransferLimits,
    collect_outputs,
)

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_IMAGE")
_PROXY_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE")

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or not _IMAGE,
    reason="needs the docker client on PATH and MAF_SANDBOX_DOCKER_E2E_IMAGE naming a runnable image",
)

_WORK = "/work"


def _spec(**kw) -> SandboxSpec:
    return SandboxSpec(kind="e2e", image=_IMAGE, work_dir=_WORK, **kw)


def _key(scope: str) -> SandboxKey:
    return SandboxKey(scope=scope, thread_id="thread-1", agent_dir="devops-engineer")


def _names_on_the_machine(name: str) -> list[str]:
    """Every container currently named ``name``, read with docker rather than the backend."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line == name]


class TestALiveContainer:
    def test_write_exec_reuse_and_dispose_round_trip(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/work/nested/deep/main.txt", "param naïve string\n")

            read_back = await sandbox.exec(
                ["cat", "nested/deep/main.txt"], working_directory=_WORK, timeout=60
            )
            assert read_back.exit_code == 0, read_back.stderr
            assert read_back.stdout == "param naïve string\n"

            failing = await sandbox.exec("exit 7", working_directory=_WORK, timeout=60)
            assert failing.exit_code == 7

            warm = await backend.acquire(_key(scope), _spec())
            assert warm.container_name == sandbox.container_name

            await backend.dispose(_key(scope))
            assert _names_on_the_machine(sandbox.container_name) == []

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_dispose_scope_finds_the_container_by_its_labels(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> str:
            sandbox = await backend.acquire(_key(scope), _spec())
            # Purged through a second backend: the labels on the container, not this process's
            # memory, are what a conversation delete has to find.
            purged = await DockerSandboxBackend(DockerSandboxConfig()).dispose_scope(
                scope, "thread-1"
            )
            assert purged >= 1
            return sandbox.container_name

        try:
            name = asyncio.run(scenario())
            assert _names_on_the_machine(name) == []
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


class TestFilesOutAgainstARealEngine:
    """The acceptance gate: stat, read, cap refusal and symlink refusal on a live tar stream."""

    def test_a_written_output_stats_and_reads_back_byte_identical(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/work/out.png", payload)

            entry = await sandbox.stat_file("out.png", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.FILE
            assert entry.size_bytes == len(payload)

            got = await sandbox.read_file("out.png", working_directory=_WORK, max_bytes=1 << 20)
            assert got == payload

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_an_over_cap_output_is_refused(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            await sandbox.write_file("/work/big.bin", b"x" * 5000)
            with pytest.raises(SandboxTransferCapExceeded):
                await sandbox.read_file("big.bin", working_directory=_WORK, max_bytes=100)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_a_symlinked_output_is_refused_at_stat_and_at_read(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())

        async def scenario() -> None:
            sandbox = await backend.acquire(_key(scope), _spec())
            # A kind's first write creates the working directory (docker cp makes the parents);
            # exec's -w needs it to exist, exactly as it does for a real workload.
            await sandbox.write_file("/work/.keep", "")
            made = await sandbox.exec(
                ["ln", "-s", "/etc/passwd", "/work/link"], working_directory=_WORK, timeout=60
            )
            assert made.exit_code == 0, made.stderr

            entry = await sandbox.stat_file("link", working_directory=_WORK)
            assert entry is not None
            assert entry.kind is EntryKind.OTHER  # never FILE

            with pytest.raises(OSError):
                await sandbox.read_file("link", working_directory=_WORK, max_bytes=1 << 20)

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))

    def test_collect_outputs_lands_a_declared_output_through_the_router(self):
        """The whole pull surface, end to end: a kind declares an output and it lands.

        Exercises `collect_outputs` — the glue over `stat_file`/`read_file` — against a real
        engine, which is the shape a diagram-style kind uses.
        """
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(DockerSandboxConfig())
        router = SandboxRouter([backend], min_isolation=Isolation.CONTAINER)

        from maf_sandbox import Artifact, DeclaredOutput, LandedArtifact, OutputSink

        landed: list[Artifact] = []

        async def deliver(artifact: Artifact) -> LandedArtifact:
            landed.append(artifact)
            return LandedArtifact(name=artifact.name, display=artifact.name)

        spec = _spec(
            requires=frozenset({Capability.EXEC, Capability.FILES_IN, Capability.FILES_OUT}),
            declared_outputs=(DeclaredOutput(path="result.txt", media_type="text/plain"),),
            files_out=TransferLimits(
                max_bytes_per_file=1 << 20, max_total_bytes=1 << 20, max_files=4
            ),
        )

        async def scenario() -> None:
            sandbox = await router.acquire(_key(scope), spec)
            # Seed the working directory the way a real kind does — its first write_file is what
            # creates /work before any exec -w into it.
            await sandbox.write_file("/work/.keep", "")
            await sandbox.exec(
                ["sh", "-c", "echo rendered > result.txt"], working_directory=_WORK, timeout=60
            )
            results = await collect_outputs(sandbox, spec, sink=OutputSink(deliver=deliver))
            assert [r.name for r in results] == ["result.txt"]
            assert landed[0].content == b"rendered\n"
            assert landed[0].media_type == "text/plain"

        try:
            asyncio.run(scenario())
        finally:
            asyncio.run(backend.dispose_scope(scope, "thread-1"))


def _network_present(name: str) -> bool:
    out = subprocess.run(
        ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return name in out.splitlines()


@pytest.mark.skipif(
    not _PROXY_IMAGE,
    reason="needs MAF_SANDBOX_DOCKER_E2E_PROXY_IMAGE naming a built proxy image (and curl in the image)",
)
class TestAllowlistEgress:
    """An allowed host is reachable, a denied one is not, and a DNS lookup for a denied host
    does not leak — the negative controls the design requires include the DNS case.
    """

    def _config(self) -> DockerSandboxConfig:
        return DockerSandboxConfig(egress_proxy_image=_PROXY_IMAGE)

    def _curl_status(self, sandbox, url: str) -> tuple[int, str]:
        result = asyncio.run(
            sandbox.exec(
                ["sh", "-c", f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 25 {url}"],
                working_directory=_WORK,
                timeout=45,
            )
        )
        return result.exit_code, result.stdout.strip()

    def test_allowed_reachable_denied_not_and_teardown_leaves_nothing(self):
        scope = f"e2e-{uuid.uuid4()}"
        backend = DockerSandboxBackend(self._config())
        spec = _spec(egress_allow=("mcr.microsoft.com",))

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
