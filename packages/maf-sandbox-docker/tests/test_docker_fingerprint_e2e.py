"""Run the same observer checks against a rootful or rootless Docker context."""

import asyncio
import os
import shutil
import uuid

import pytest
from maf_sandbox import SandboxKey, SandboxSpec
from maf_sandbox.conformance import ConformanceFailure, assert_nothing_left_behind

from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig
from maf_sandbox_docker._backend import _DockerSandbox
from maf_sandbox_docker.conformance import DockerFingerprintSubject

_IMAGE = os.environ.get("MAF_SANDBOX_DOCKER_E2E_IMAGE", "")
_OBSERVER = os.environ.get("MAF_SANDBOX_DOCKER_OBSERVER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not _IMAGE or not _OBSERVER or not shutil.which("docker"),
    reason="needs Docker, MAF_SANDBOX_DOCKER_E2E_IMAGE and MAF_SANDBOX_DOCKER_OBSERVER_IMAGE",
)


@pytest.mark.parametrize(
    "script, expected",
    [
        ("true", None),
        (
            "mkdir /tmp/confined-call; echo input > /tmp/confined-call/file; rm -rf /tmp/confined-call",
            None,
        ),
        ("echo residue > /tmp/residue", "/tmp/residue"),
        ("echo residue > /dev/shm/residue", "/dev/shm/residue"),
        ("echo residue > /dev/residue", "/dev/residue"),
        ("echo residue >> /etc/hosts", "/etc/hosts"),
        ("touch /etc/hosts", "/etc/hosts"),
        ("chmod 600 /etc/hosts", "/etc/hosts"),
        ("chown 123:123 /etc/hosts", "/etc/hosts"),
        ("chmod 600 /etc/hosts; chmod 644 /etc/hosts", None),
        ("touch /dev/shm", "/dev/shm"),
        ("touch -h /dev/stdout", "/dev/stdout"),
        ("ln -s /etc/passwd /dev/shm/residue", "/dev/shm/residue"),
        ("sleep 120 >/dev/null 2>&1 &", "processes added"),
    ],
)
def test_engine_observes_residue(script, expected):
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig())
        key = SandboxKey(
            scope="fingerprint-" + uuid.uuid4().hex, thread_id="test", agent_dir="test"
        )
        try:
            sandbox = await backend.acquire(
                key, SandboxSpec(kind="fingerprint", image=_IMAGE, work_dir="/")
            )
            subject = DockerFingerprintSubject(sandbox, observer_image=_OBSERVER)

            async def call():
                result = await sandbox.exec(["sh", "-c", script], working_directory="/", timeout=30)
                assert result.exit_code == 0, result.stderr

            if expected is None:
                assert all(r.passed for r in await assert_nothing_left_behind(subject, call))
            else:
                with pytest.raises(ConformanceFailure, match=expected):
                    await assert_nothing_left_behind(subject, call)
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["stat", "read", "write_reclaim"])
def test_file_plane_leaves_no_mounted_file_residue(operation):
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig())
        key = SandboxKey(
            scope="fingerprint-files-" + uuid.uuid4().hex, thread_id="test", agent_dir="test"
        )
        try:
            sandbox = await backend.acquire(
                key, SandboxSpec(kind="fingerprint", image=_IMAGE, work_dir="/")
            )
            subject = DockerFingerprintSubject(sandbox, observer_image=_OBSERVER)

            async def call():
                if operation == "write_reclaim":
                    await sandbox.write_file(
                        "tmp/confined-call/input", b"input", working_directory="/"
                    )
                    await sandbox.reclaim("/tmp/confined-call", working_directory="/", timeout=30)
                else:
                    for path in ("etc/hostname", "etc/hosts", "etc/resolv.conf"):
                        if operation == "stat":
                            assert await sandbox.stat_file(path, working_directory="/") is not None
                        else:
                            assert await sandbox.read_file(
                                path, working_directory="/", max_bytes=65536
                            )

            assert all(r.passed for r in await assert_nothing_left_behind(subject, call))
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


def test_observer_limit_refuses_instead_of_truncating():
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig())
        key = SandboxKey(
            scope="fingerprint-" + uuid.uuid4().hex, thread_id="test", agent_dir="test"
        )
        try:
            sandbox = await backend.acquire(
                key, SandboxSpec(kind="fingerprint", image=_IMAGE, work_dir="/")
            )
            subject = DockerFingerprintSubject(sandbox, observer_image=_OBSERVER, max_bytes=1)
            with pytest.raises(RuntimeError, match="byte limit exceeded"):
                await subject.fingerprint()
        finally:
            assert await backend.dispose(key) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "flags, path",
    [
        (["--tmpfs", "/tmp"], "/tmp/residue"),
        (["--user", "10001:10001", "--cap-drop=ALL"], "/dev/shm/residue"),
    ],
)
def test_host_tmpfs_and_nonroot_workloads(flags, path):
    async def scenario():
        backend = DockerSandboxBackend(DockerSandboxConfig())
        name = "maf-fingerprint-fixture-" + uuid.uuid4().hex
        try:
            created = await backend._docker(
                "run",
                "-d",
                "--name",
                name,
                "--network=none",
                *flags,
                "--entrypoint=sleep",
                _IMAGE,
                "infinity",
                timeout=60,
            )
            assert created.returncode == 0, created.stderr
            sandbox = _DockerSandbox(
                backend._docker,
                name,
                60,
                instance_id=created.stdout.decode().strip(),
                freeze=backend._freeze(name),
            )
            subject = DockerFingerprintSubject(sandbox, observer_image=_OBSERVER)

            async def call():
                result = await sandbox.exec(
                    ["sh", "-c", f"echo residue > {path}"], working_directory="/", timeout=30
                )
                assert result.exit_code == 0, result.stderr

            with pytest.raises(ConformanceFailure, match=path):
                await assert_nothing_left_behind(subject, call)
        finally:
            removed = await backend._docker("rm", "-f", name, timeout=30)
            assert removed.returncode == 0, removed.stderr

    asyncio.run(scenario())
