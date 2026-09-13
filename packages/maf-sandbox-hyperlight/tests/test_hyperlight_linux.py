"""Real Linux cgroup memory, process-tree cleanup and ownership checks without a VM."""

from __future__ import annotations

import asyncio
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from maf_sandbox_hyperlight import HyperlightWorkerError, _linux

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MAF_HYPERLIGHT_LINUX_TESTS") != "1",
    reason="requires opt-in and a writable delegated cgroup v2 root",
)


def cgroup_root() -> str:
    return os.environ["MAF_HYPERLIGHT_CGROUP_ROOT"]


def line(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready, "worker did not answer"
    value = process.stdout.readline()
    assert value, "worker closed before answering"
    return value


def exited(fd: int) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux pidfd API")
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(5000), "process survived tree cleanup"


def test_cgroup_rejects_regular_filesystem(tmp_path: Path):
    with pytest.raises(HyperlightWorkerError, match="cgroup v2 filesystem"):
        _linux.Job(64 * 1024**2, str(tmp_path), 3)
    assert not list(tmp_path.iterdir())


def test_cgroup_memory_limit_kills_native_allocation():
    if sys.platform != "linux":
        pytest.skip("Linux signal API")
    job = _linux.Job(64 * 1024**2, cgroup_root(), 3)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import sys; sys.stdin.readline(); data=bytearray(256*1024**2); print('unbounded')",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        job.assign(process.pid)
        job.ready()
        assert _linux._read(job._directory, "memory.max").strip() == str(64 * 1024**2)
        assert _linux._read(job._directory, "memory.swap.max").strip() == "0"
        stdout, stderr = process.communicate(b"start\n", timeout=10)
        assert process.returncode == -signal.SIGKILL, stderr
        assert not stdout
    finally:
        job.close()
        process.wait(timeout=5)
    assert not (Path(cgroup_root()) / job._name).exists()


TREE = """import os, subprocess, sys, time
sys.stdin.readline()
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
sys.stdin.readline()
"""


@pytest.mark.parametrize("worker_dies", [False, True])
def test_tree_cleanup_includes_a_child_in_another_session(worker_dies: bool):
    if sys.platform != "linux":
        pytest.skip("Linux pidfd and signal APIs")
    job = _linux.Job(128 * 1024**2, cgroup_root(), 3)
    worker = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", TREE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    child_fd = None
    try:
        job.assign(worker.pid)
        job.ready()
        assert worker.stdin is not None
        worker.stdin.write(b"start\n")
        child_fd = os.pidfd_open(int(line(worker)))
        if worker_dies:
            worker.kill()
            exited(child_fd)
        job.close()
        exited(child_fd)
        assert worker.wait(timeout=5) == -signal.SIGKILL
        assert job._watcher is not None and job._watcher.poll() is not None
    finally:
        job.close()
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)
        if child_fd is not None:
            os.close(child_fd)


OWNER = """import os, subprocess, sys
from maf_sandbox_hyperlight import _linux
_linux._LOCK_PATH = sys.argv[1]
_linux.claim_host()
job = _linux.Job(128*1024**2, os.environ['MAF_HYPERLIGHT_CGROUP_ROOT'], 3)
worker = subprocess.Popen([sys.executable, '-I', '-u', '-c', sys.argv[2]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
job.assign(worker.pid)
job.ready()
worker.stdin.write(b'start\\n')
child = int(worker.stdout.readline())
print(worker.pid, child, job._watcher.pid, job._name, flush=True)
sys.stdin.readline()
os._exit(0)
"""


@pytest.mark.parametrize("kill_owner", [False, True])
def test_owner_death_kills_workers_and_retains_lock_until_cleanup(tmp_path: Path, kill_owner: bool):
    if sys.platform != "linux":
        pytest.skip("Linux pidfd and signal APIs")
    lock = str(tmp_path / "owner.lock")
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", OWNER, lock, TREE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    watcher = None
    handles: list[int] = []
    try:
        worker_text, child_text, watcher_text, name = line(owner).decode().split()
        worker, child, watcher = map(int, (worker_text, child_text, watcher_text))
        handles = [os.pidfd_open(pid) for pid in (worker, child, watcher)]
        os.kill(watcher, signal.SIGSTOP)
        if kill_owner:
            owner.kill()
        else:
            assert owner.stdin is not None
            owner.stdin.write(b"exit\n")
        owner.wait(timeout=5)
        refuse = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH=sys.argv[1]; _linux.claim_host()",
                lock,
            ],
            capture_output=True,
            timeout=5,
        )
        assert refuse.returncode != 0 and b"another process owns Hyperlight" in refuse.stderr
        os.kill(watcher, signal.SIGCONT)
        for handle in handles:
            exited(handle)
        assert not (Path(cgroup_root()) / name).exists()
        accepted = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH=sys.argv[1]; _linux.claim_host()",
                lock,
            ],
            capture_output=True,
            timeout=5,
        )
        assert accepted.returncode == 0, accepted.stderr
    finally:
        if watcher is not None:
            with suppress(ProcessLookupError):
                os.kill(watcher, signal.SIGCONT)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        for handle in handles:
            os.close(handle)


def test_foreign_owner_scope_purge_reports_failure(tmp_path: Path):
    lock = str(tmp_path / "owner.lock")
    script = "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH=sys.argv[1]; _linux.claim_host(); print('ready',flush=True); sys.stdin.readline()"
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", script, lock],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert line(owner).strip() == b"ready"
        check = """import asyncio, sys
from maf_sandbox_hyperlight import HyperlightSandboxBackend, _linux
_linux._LOCK_PATH=sys.argv[1]
result=asyncio.run(HyperlightSandboxBackend().dispose_scope('scope','thread'))
assert result.undisposed is not None, result
print(result.undisposed)
"""
        refused = subprocess.run(
            [sys.executable, "-I", "-c", check, lock],
            capture_output=True,
            timeout=5,
        )
        assert refused.returncode == 0, refused.stderr
        assert b"another process owns Hyperlight" in refused.stdout
    finally:
        owner.communicate(b"exit\n", timeout=5)


def test_missing_cgroup_root_refuses_before_spawning(tmp_path: Path):
    from maf_sandbox_hyperlight import HyperlightSandboxConfig, _process

    with pytest.raises(HyperlightWorkerError, match="delegated cgroup v2"):
        _process.Worker(HyperlightSandboxConfig(linux_cgroup_root=str(tmp_path / "absent")))


def test_watcher_keeps_ownership_while_cleanup_retries(tmp_path: Path):
    lock, attempted, release = (tmp_path / name for name in ("owner.lock", "attempted", "release"))
    script = """import os, subprocess, sys
from pathlib import Path
from maf_sandbox_hyperlight import HyperlightWorkerError, _linux, _linux_watch
_linux._LOCK_PATH=sys.argv[1]
_linux.claim_host()
attempted, release = Path(sys.argv[2]), Path(sys.argv[3])
child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'])
worker=os.pidfd_open(child.pid)
owner=os.pidfd_open(os.getpid())
child.kill()
child.wait()
def cleanup(*args):
    attempted.write_text('retry')
    if not release.exists():
        raise HyperlightWorkerError('cleanup not confirmed')
_linux_watch.kill_group=cleanup
sys.argv=['watch',str(owner),str(worker),'0','0','unused','0.01']
_linux_watch.main()
"""
    watcher = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", script, str(lock), str(attempted), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert watcher.stdout is not None
        ready, _, _ = select.select([watcher.stdout], [], [], 5)
        assert ready and watcher.stdout.read(1) == b"1"
        deadline = time.monotonic() + 5
        while not attempted.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        refused = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; from maf_sandbox_hyperlight import _linux; _linux._LOCK_PATH=sys.argv[1]; _linux.claim_host()",
                str(lock),
            ],
            capture_output=True,
            timeout=5,
        )
        assert refused.returncode != 0 and b"another process owns Hyperlight" in refused.stderr
        assert watcher.poll() is None
        release.touch()
        _, stderr = watcher.communicate(timeout=5)
        assert watcher.returncode == 0, stderr
    finally:
        release.touch()
        watcher.communicate(timeout=5)


def test_slow_watcher_readiness_consumes_startup_deadline(monkeypatch: pytest.MonkeyPatch):
    from maf_sandbox import Capability, SandboxKey, SandboxSpec

    from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig

    original = subprocess.Popen
    processes: list[subprocess.Popen[bytes] | subprocess.Popen[str]] = []

    def start(command, **kwargs):
        if "maf_sandbox_hyperlight._linux_watch" in command:
            command = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
        process = original(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            linux_cgroup_root=cgroup_root(),
            startup_timeout=0.5,
            cleanup_timeout=1,
            max_worker_memory_bytes=128 * 1024**2,
        )
    )
    spec = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(backend.acquire(SandboxKey("linux", "readiness", "agent"), spec))
        assert len(processes) == 2 and all(process.poll() is not None for process in processes)
        assert not backend._sandboxes
    finally:
        asyncio.run(backend.aclose())
