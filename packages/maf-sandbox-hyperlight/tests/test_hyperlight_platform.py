"""Platform configuration and refusal before native execution."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from maf_sandbox_hyperlight import HyperlightSandboxConfig, HyperlightWorkerError, _backend


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
