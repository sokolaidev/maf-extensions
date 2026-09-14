"""Real Linux cgroup memory, process-tree cleanup and ownership checks without a VM."""

from __future__ import annotations

import asyncio
import json
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


def spawn(job: _linux.Job, command: list[str]) -> subprocess.Popen[bytes]:
    return job.spawn(command, environment=dict(os.environ), cwd=os.getcwd(), cleanup_timeout=3)


def test_cgroup_rejects_regular_filesystem(tmp_path: Path):
    with pytest.raises(HyperlightWorkerError, match="cgroup v2 filesystem"):
        _linux.Job(64 * 1024**2, str(tmp_path), 3)
    assert not list(tmp_path.iterdir())


def test_cgroup_memory_limit_kills_native_allocation():
    if sys.platform != "linux":
        pytest.skip("Linux signal API")
    job = _linux.Job(64 * 1024**2, cgroup_root(), 3)
    process = spawn(
        job,
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import sys; sys.stdin.readline(); print('allocating',flush=True); data=bytearray(256*1024**2); print('unbounded')",
        ],
    )
    handle = None
    try:
        job.ready(deadline=time.monotonic() + 3)
        assert job._worker_pid is not None
        handle = os.pidfd_open(job._worker_pid)
        os.kill(process.pid, signal.SIGSTOP)
        group = Path(cgroup_root()) / job._name
        assert (group / "memory.max").read_text().strip() == str(64 * 1024**2)
        assert (group / "memory.swap.max").read_text().strip() == "0"
        assert process.stdin is not None
        process.stdin.write(b"start\n")
        process.stdin.flush()
        assert line(process).strip() == b"allocating"
        exited(handle)
        events = dict(row.split() for row in (group / "memory.events").read_text().splitlines())
        assert int(events["oom_kill"]) >= 1
        os.kill(process.pid, signal.SIGCONT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert not stdout
    finally:
        with suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGCONT)
        job.close()
        process.wait(timeout=5)
        if handle is not None:
            os.close(handle)
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
    worker = spawn(
        job,
        [sys.executable, "-I", "-u", "-c", TREE],
    )
    child_fd = None
    try:
        job.ready(deadline=time.monotonic() + 3)
        assert worker.stdin is not None
        worker.stdin.write(b"start\n")
        worker.stdin.flush()
        child_fd = os.pidfd_open(int(line(worker)))
        if worker_dies:
            assert job._worker_pid is not None
            os.kill(job._worker_pid, signal.SIGKILL)
            exited(child_fd)
        job.close()
        exited(child_fd)
        assert worker.wait(timeout=5) == 0
        assert job._watcher is not None and job._watcher.poll() is not None
    finally:
        job.close()
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)
        if child_fd is not None:
            os.close(child_fd)


OWNER = """import os, subprocess, sys, time
from maf_sandbox_hyperlight import _linux
_linux._LOCK_PATH = sys.argv[1]
_linux.claim_host()
job = _linux.Job(128*1024**2, os.environ['MAF_HYPERLIGHT_CGROUP_ROOT'], 3)
worker = job.spawn([sys.executable, '-I', '-u', '-c', sys.argv[2]], environment=dict(os.environ), cwd=os.getcwd(), cleanup_timeout=3)
job.ready(deadline=time.monotonic() + 3)
worker.stdin.write(b'start\\n')
worker.stdin.flush()
child = int(worker.stdout.readline())
print(job._worker_pid, child, job._watcher.pid, job._name, flush=True)
sys.stdin.readline()
os._exit(0)
"""


@pytest.mark.parametrize("termination", ["exit", "kill", "group"])
def test_owner_death_kills_workers_and_retains_lock_until_cleanup(tmp_path: Path, termination: str):
    if sys.platform != "linux":
        pytest.skip("Linux pidfd and signal APIs")
    lock = str(tmp_path / "owner.lock")
    owner = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", OWNER, lock, TREE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    watcher = None
    handles: list[int] = []
    try:
        worker_text, child_text, watcher_text, name = line(owner).decode().split()
        worker, child, watcher = map(int, (worker_text, child_text, watcher_text))
        handles = [os.pidfd_open(pid) for pid in (worker, child, watcher)]
        os.kill(watcher, signal.SIGSTOP)
        if termination == "group":
            os.killpg(owner.pid, signal.SIGTERM)
        elif termination == "kill":
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


STARTUP_OWNER = """import asyncio, json, os, subprocess, sys, time
from maf_sandbox import Capability, SandboxKey, SandboxSpec
from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig, _linux
_linux._LOCK_PATH=sys.argv[1]
phase, bootstrap, retain = sys.argv[2:5]
original = subprocess.Popen
workers = []
def holder():
    if retain != 'yes':
        return None
    child = os.fork()
    if child == 0:
        os.setsid()
        time.sleep(60)
        os._exit(0)
    return child
def start(command, **kwargs):
    if 'maf_sandbox_hyperlight._linux_watch' in command:
        if phase == 'before':
            print(json.dumps({'watcher': None, 'workers': workers, 'holder': holder()}), flush=True)
            time.sleep(60)
        command = [sys.executable, '-I', '-u', '-c', bootstrap, *command[5:]]
        process = original(command, **kwargs)
        print(json.dumps({'watcher': process.pid, 'workers': workers, 'holder': holder()}), flush=True)
        return process
    process = original(command, **kwargs)
    workers.append(process.pid)
    return process
subprocess.Popen = start
config = HyperlightSandboxConfig(linux_cgroup_root=os.environ['MAF_HYPERLIGHT_CGROUP_ROOT'], startup_timeout=30)
backend = HyperlightSandboxBackend(config)
spec = SandboxSpec(kind='python', work_dir=None, requires=frozenset({Capability.RUN_CODE}))
asyncio.run(backend.acquire(SandboxKey('startup','thread','agent'), spec))
"""


def scope_is_clean(lock: Path) -> bool:
    script = """import asyncio, sys
from maf_sandbox_hyperlight import HyperlightSandboxBackend, _linux
_linux._LOCK_PATH=sys.argv[1]
result=asyncio.run(HyperlightSandboxBackend().dispose_scope('startup','thread'))
print('clean' if result.undisposed is None else 'unclean')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(lock)], capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() == b"clean"


@pytest.mark.parametrize(
    ("phase", "retain_stdin"),
    [("before", False), ("before", True), ("cgroup", True), ("worker", True), ("entry", True)],
)
def test_owner_death_during_startup_cannot_leave_a_clean_purge_with_orphans(
    tmp_path: Path, phase: str, retain_stdin: bool
):
    if sys.platform != "linux":
        pytest.skip("Linux pidfd, process-group and cgroup APIs")
    lock, marker = tmp_path / "owner.lock", tmp_path / "paused.json"
    bootstrap = f"""import json, os, runpy, signal, subprocess, sys
from pathlib import Path
from maf_sandbox_hyperlight import _linux_watch
marker = Path({str(marker)!r})
phase = {phase!r}
def pause(worker):
    marker.write_text(json.dumps({{'worker': worker}}))
    os.kill(os.getpid(), signal.SIGSTOP)
write = _linux_watch.write_control
paused = False
def control(*args):
    global paused
    write(*args)
    if not paused:
        paused = True
        pause(None)
original = subprocess.Popen
def start(command, **kwargs):
    if phase == 'entry':
        entry = "import json,os,runpy,signal; from pathlib import Path; Path(" + repr(str(marker)) + ").write_text(json.dumps({{'worker':os.getpid()}})); os.kill(os.getpid(),signal.SIGSTOP); runpy.run_module('maf_sandbox_hyperlight._linux_entry',run_name='__main__')"
        command = [sys.executable, '-I', '-u', '-c', entry, *command[5:]]
    process = original(command, **kwargs)
    if phase == 'worker':
        pause(process.pid)
    return process
if phase == 'cgroup':
    _linux_watch.write_control = control
subprocess.Popen = start
_linux_watch.main()
"""
    root = Path(cgroup_root())
    before = set(root.glob("worker-*"))
    owner = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            STARTUP_OWNER,
            str(lock),
            phase,
            bootstrap,
            "yes" if retain_stdin else "no",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    handles: list[int] = []
    watcher = None
    try:
        info = json.loads(line(owner))
        native = list(info["workers"])
        watcher = info["watcher"]
        if info["holder"] is not None:
            handles.append(os.pidfd_open(info["holder"]))
        if watcher is not None:
            handles.append(os.pidfd_open(watcher))
            deadline = time.monotonic() + 5
            while not marker.exists():
                assert time.monotonic() < deadline, "startup did not reach the selected phase"
                time.sleep(0.01)
            paused = json.loads(marker.read_text())
            if paused["worker"] is not None:
                native.append(paused["worker"])
            if phase == "entry":
                os.kill(watcher, signal.SIGSTOP)
        native_handles = [os.pidfd_open(pid) for pid in native]
        handles.extend(native_handles)
        owner.kill()
        owner.wait(timeout=5)
        if retain_stdin:
            poller = select.poll()
            poller.register(handles[0], select.POLLIN)
            assert not poller.poll(0), "the pipe-holding fork child must remain alive"
        if watcher is None:
            remaining = set(root.glob("worker-*")) - before
            assert not (scope_is_clean(lock) and remaining), "startup cgroup survived a clean purge"
        else:
            assert not scope_is_clean(lock), "ownership passed before startup cleanup"
            os.kill(watcher, signal.SIGCONT)
            exited(handles[1])
        for handle in native_handles:
            exited(handle)
        assert set(root.glob("worker-*")) == before
        assert scope_is_clean(lock)
    finally:
        if watcher is not None:
            with suppress(ProcessLookupError):
                os.kill(watcher, signal.SIGCONT)
        if owner.poll() is None:
            owner.kill()
        for handle in handles:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(handle, signal.SIGKILL)
            os.close(handle)
        owner.communicate(timeout=5)
        for group in set(root.glob("worker-*")) - before:
            with suppress(FileNotFoundError):
                (group / "cgroup.kill").write_text("1")
                deadline = time.monotonic() + 5
                while "populated 1" in (group / "cgroup.events").read_text():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                group.rmdir()


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
os.write(sys.stdout.fileno(), b'1')
_linux_watch._cleanup(0,0,'unused',0.01)
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
            command = [
                sys.executable,
                "-I",
                "-u",
                "-c",
                "import runpy,time; time.sleep(0.8); runpy.run_module('maf_sandbox_hyperlight._linux_watch',run_name='__main__')",
                *command[5:],
            ]
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
        assert len(processes) == 1 and all(process.poll() is not None for process in processes)
        assert not backend._sandboxes
    finally:
        asyncio.run(backend.aclose())


def test_watcher_startup_can_outlast_the_cleanup_allowance(monkeypatch: pytest.MonkeyPatch):
    from maf_sandbox import Capability, SandboxKey, SandboxSpec

    from maf_sandbox_hyperlight import HyperlightSandboxBackend, HyperlightSandboxConfig, _process

    original = subprocess.Popen

    def start(command, **kwargs):
        if "maf_sandbox_hyperlight._linux_watch" in command:
            command = [
                sys.executable,
                "-I",
                "-u",
                "-c",
                "import runpy,time; time.sleep(0.3); runpy.run_module('maf_sandbox_hyperlight._linux_watch',run_name='__main__')",
                *command[5:],
            ]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(
        _process.Worker,
        "command",
        staticmethod(
            lambda: [
                sys.executable,
                "-I",
                "-u",
                str(Path(__file__).with_name("worker_fixture.py")),
                "normal",
            ]
        ),
    )
    backend = HyperlightSandboxBackend(
        HyperlightSandboxConfig(
            linux_cgroup_root=cgroup_root(), startup_timeout=3, cleanup_timeout=0.1
        )
    )
    spec = SandboxSpec(kind="python", work_dir=None, requires=frozenset({Capability.RUN_CODE}))
    try:
        sandbox = asyncio.run(backend.acquire(SandboxKey("linux", "watcher", "agent"), spec))
        assert (asyncio.run(sandbox.run_code("ready", timeout=1))).stdout == "ready"
    finally:
        asyncio.run(backend.aclose())
