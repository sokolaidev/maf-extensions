"""Linux ownership and shared deadlines without requiring KVM or cgroup delegation."""

from __future__ import annotations

import errno
import os
import select
import signal
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from maf_sandbox_hyperlight import HyperlightSandboxConfig, HyperlightWorkerError, _linux, _process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process ownership")

CLAIM = "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH=sys.argv[1]; _linux.claim_host()"


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_nonregular_owner_lock_refuses_without_blocking(tmp_path: Path, kind: str):
    if sys.platform != "linux":
        pytest.skip("Linux FIFO API")
    lock = tmp_path / "owner.lock"
    if kind == "fifo":
        os.mkfifo(lock)
    else:
        lock.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-c", CLAIM, str(lock)], capture_output=True, timeout=3
    )
    assert result.returncode != 0
    assert b"regular, persistent owner lock" in result.stderr


@pytest.mark.parametrize("mask", ["022", "077"])
def test_owner_lock_readability_is_independent_of_umask(tmp_path: Path, mask: str):
    lock = tmp_path / "owner.lock"
    script = "import os; os.umask(int(__import__('sys').argv[2],8)); " + CLAIM
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(lock), mask], capture_output=True, timeout=3
    )
    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE(lock.stat().st_mode)
    assert mode & 0o444 == 0o444
    assert mode & 0o222 == 0
    assert lock.read_bytes() == b""


@pytest.mark.parametrize("failure", ["exception", "exit"])
def test_failed_lock_preparation_leaves_successor_free_to_publish(tmp_path: Path, failure: str):
    lock = tmp_path / "owner.lock"
    script = """import os, sys
from maf_sandbox_hyperlight import _linux
_linux._LOCK_PATH=sys.argv[1]
def fail(fd, mode):
    if sys.argv[2] == 'exit':
        os._exit(73)
    raise PermissionError('mode preparation refused')
os.fchmod=fail
_linux.claim_host()
"""
    creator = subprocess.run(
        [sys.executable, "-I", "-c", script, str(lock), failure], capture_output=True, timeout=3
    )
    assert creator.returncode != 0
    assert not lock.exists(), "an incomplete inode must never occupy the shared lock path"
    if failure == "exception":
        assert not list(tmp_path.iterdir())
    successor = subprocess.run(
        [sys.executable, "-I", "-c", CLAIM, str(lock)], capture_output=True, timeout=3
    )
    assert successor.returncode == 0, successor.stderr
    assert stat.S_IMODE(lock.stat().st_mode) == 0o444
    assert lock.stat().st_nlink == 1
    assert lock.read_bytes() == b""


def test_late_lock_publisher_preserves_the_winning_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lock = tmp_path / "owner.lock"
    lock.write_bytes(b"operator-managed")
    lock.chmod(0o640)
    before = lock.stat()
    monkeypatch.setattr(_linux, "_LOCK_PATH", str(lock))
    _linux._publish_owner_lock()
    after = lock.stat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
    )
    assert lock.read_bytes() == b"operator-managed"
    assert list(tmp_path.iterdir()) == [lock]


def test_unrelated_fork_child_does_not_keep_ownership_after_owner_exit(tmp_path: Path):
    if sys.platform != "linux":
        pytest.skip("Linux fork and pidfd APIs")
    lock = str(tmp_path / "owner.lock")
    script = """import os, sys, time
from maf_sandbox_hyperlight import _linux
_linux._LOCK_PATH=sys.argv[1]
_linux.claim_host()
child=os.fork()
if child == 0:
    for fd in (0,1,2):
        os.close(fd)
    time.sleep(30)
    os._exit(0)
print(child, flush=True)
sys.stdin.readline()
os._exit(0)
"""
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", script, lock],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_fd = None
    try:
        assert owner.stdout is not None
        ready, _, _ = select.select([owner.stdout], [], [], 5)
        assert ready
        child = int(owner.stdout.readline())
        child_fd = os.pidfd_open(child)
        refused = subprocess.run(
            [sys.executable, "-I", "-c", CLAIM, lock], capture_output=True, timeout=3
        )
        assert refused.returncode != 0 and b"another process owns Hyperlight" in refused.stderr
        _, stderr = owner.communicate(b"exit\n", timeout=3)
        assert owner.returncode == 0, stderr
        poller = select.poll()
        poller.register(child_fd, select.POLLIN)
        assert not poller.poll(0), "the unrelated child must remain alive during succession"
        accepted = subprocess.run(
            [sys.executable, "-I", "-c", CLAIM, lock], capture_output=True, timeout=3
        )
        assert accepted.returncode == 0, accepted.stderr
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=3)
        if child_fd is not None:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            os.close(child_fd)


def test_fork_child_still_refuses_inherited_backend_use(tmp_path: Path):
    script = """import os, sys
from maf_sandbox_hyperlight import HyperlightWorkerError, _linux
_linux._LOCK_PATH=sys.argv[1]
_linux.claim_host()
child=os.fork()
if child == 0:
    try:
        _linux.claim_host()
    except HyperlightWorkerError:
        os._exit(0)
    os._exit(1)
_, status=os.waitpid(child, 0)
raise SystemExit(os.waitstatus_to_exitcode(status))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path / "owner.lock")],
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_admission_and_supervisor_wait_share_one_deadline(monkeypatch: pytest.MonkeyPatch):
    now = [0.0]

    def sleep(seconds: float):
        now[0] += seconds

    class ContendedLock:
        def acquire(self, *, timeout: float):
            sleep(0.16)
            return True

        def release(self):
            pass

    class WaitingWatcher:
        def poll(self):
            return None

        def wait(self, *, timeout):
            sleep(timeout)
            raise subprocess.TimeoutExpired("watcher", timeout)

    job = _linux.Job.__new__(_linux.Job)
    job._pid = os.getpid()
    job._closed = False
    job._timeout = 0.2
    job._control = -1
    monkeypatch.setattr(job, "_watcher", WaitingWatcher(), raising=False)
    worker = _process.Worker.__new__(_process.Worker)
    worker._owner_pid = os.getpid()
    worker._config = HyperlightSandboxConfig(cleanup_timeout=0.2)
    monkeypatch.setattr(worker, "_closing", ContendedLock(), raising=False)
    worker._closed = False
    worker._job = job
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: now[0])
        patch.setattr(time, "sleep", sleep)
        patch.setattr(os, "write", lambda *args: 1)
        with pytest.raises(HyperlightWorkerError, match="cleanup expired"):
            worker.close()
    assert now[0] == pytest.approx(0.2)


@pytest.mark.parametrize("remaining", [-0.1, 0.05])
def test_readiness_deadline_does_not_depend_on_watcher_termination(remaining: float):
    watcher = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(2)"], stdout=subprocess.PIPE
    )
    job = _linux.Job.__new__(_linux.Job)
    job._watcher = watcher
    job._ready = False
    assert watcher.stdout is not None
    job._readiness = watcher.stdout.fileno()
    try:
        with pytest.raises(TimeoutError, match="startup deadline"):
            job.ready(deadline=time.monotonic() + remaining)
        assert watcher.poll() is None
    finally:
        watcher.kill()
        watcher.wait(timeout=3)
        assert watcher.stdout is not None
        watcher.stdout.close()


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ENODEV])
@pytest.mark.parametrize("exists", [False, True])
def test_missing_cgroup_control_requires_the_group_to_be_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int, exists: bool
):
    if sys.platform != "linux":
        pytest.skip("Linux directory descriptors")
    if exists:
        (tmp_path / "worker").mkdir()

    def unavailable(*args):
        raise OSError(error_number, "cgroup control unavailable")

    monkeypatch.setattr(_linux, "write_control", unavailable)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if exists:
            with pytest.raises(OSError) as raised:
                _linux.kill_group(-1, parent, "worker", time.monotonic() + 1)
            assert raised.value.errno == error_number
        else:
            _linux.kill_group(-1, parent, "worker", time.monotonic() + 1)
    finally:
        os.close(parent)
