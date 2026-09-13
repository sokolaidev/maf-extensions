"""Platform configuration and refusal before native execution."""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from maf_sandbox import Capability, SandboxKey, SandboxSpec

from maf_sandbox_hyperlight import (
    HyperlightSandboxBackend,
    HyperlightSandboxConfig,
    HyperlightWorkerError,
    _backend,
    _linux,
)


@pytest.mark.parametrize("root", ["relative", "", b"/sys/fs/cgroup/test", True, 3, "/bad\x00path"])
def test_cgroup_configuration_requires_an_absolute_text_path(root):
    with pytest.raises(ValueError, match="absolute Linux path"):
        HyperlightSandboxConfig(linux_cgroup_root=root)


@pytest.mark.parametrize(
    "system,architecture", [("darwin", "x86_64"), ("linux", "aarch64"), ("win32", "ARM64")]
)
def test_unvalidated_host_family_refuses(system: str, architecture: str):
    with (
        patch.object(sys, "platform", system),
        patch.object(platform, "machine", return_value=architecture),
        pytest.raises(HyperlightWorkerError, match="x86-64"),
    ):
        _backend.check_host()


@pytest.mark.parametrize(
    "release", ["6.8.0-101-generic", "6.12.0-azure", "4.4.0-19041-Microsoft", "6.6.0-custom"]
)
def test_unvalidated_linux_refuses_before_ownership_or_worker(release: str):
    backend = HyperlightSandboxBackend()
    key = SandboxKey("tenant", "conversation", "agent")
    spec = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))
    with (
        patch.object(sys, "platform", "linux"),
        patch.object(platform, "machine", return_value="x86_64"),
        patch.object(platform, "release", return_value=release),
        patch.object(_linux, "claim_host") as claim,
        patch.object(_backend, "Worker", side_effect=AssertionError("worker started")) as worker,
    ):
        with pytest.raises(HyperlightWorkerError, match="standard WSL2 kernel"):
            asyncio.run(backend.acquire(key, spec))
        failure = asyncio.run(backend.dispose(key))
        assert failure is not None and "standard WSL2 kernel" in failure.detail
        purge = asyncio.run(backend.dispose_scope(key.scope, key.thread_id))
        assert purge.undisposed is not None and "standard WSL2 kernel" in purge.undisposed.detail
        claim.assert_not_called()
        worker.assert_not_called()


@pytest.mark.parametrize(
    "release", ["6.18.40.1-microsoft-standard-WSL2", "5.15.167.4-microsoft-standard-WSL2"]
)
def test_standard_wsl2_admission_still_requires_ownership(release: str):
    with (
        patch.object(sys, "platform", "linux"),
        patch.object(platform, "machine", return_value="x86_64"),
        patch.object(platform, "release", return_value=release),
        patch.object(
            _linux, "claim_host", side_effect=HyperlightWorkerError("owner busy")
        ) as claim,
        pytest.raises(HyperlightWorkerError, match="owner busy"),
    ):
        _backend.check_host()
    claim.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux device access")
def test_kvm_permission_failure_has_an_actionable_error():
    from maf_sandbox_hyperlight import _linux

    with (
        patch.object(os, "stat", side_effect=FileNotFoundError),
        patch.object(os, "open", side_effect=PermissionError),
        pytest.raises(HyperlightWorkerError, match="grant /dev/kvm access"),
    ):
        _linux.check_kvm()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux device selection")
def test_mshv_host_does_not_silently_inherit_kvm_validation():
    from maf_sandbox_hyperlight import _linux

    with patch.object(os, "stat"), pytest.raises(HyperlightWorkerError, match="MSHV"):
        _linux.check_kvm()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux persistent ownership")
def test_existing_owner_lock_does_not_require_create_access(tmp_path: Path):
    lock = tmp_path / "owner.lock"
    lock.touch()
    script = """import os, sys
from maf_sandbox_hyperlight import _linux
_linux._LOCK_PATH=sys.argv[1]
original=os.open
def open_existing(path, flags, *args, **kwargs):
    if flags & os.O_CREAT:
        raise PermissionError('creation denied for an existing cross-user lock')
    return original(path, flags, *args, **kwargs)
os.open=open_existing
_linux.claim_host()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(lock)], capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
