"""Measure Linux hypervisor access and the pinned Python guest in disposable children.

This is a hosting probe, not backend conformance. Native children have a 45-second deadline;
the container's memory limit bounds native allocations. No host files or tools reach the guest.
Exit zero requires complete child observations, not successful Hyperlight guest execution.
"""

from __future__ import annotations

import argparse
import errno
import importlib
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path

PACKAGES = (
    "hyperlight-sandbox",
    "hyperlight-sandbox-backend-wasm",
    "hyperlight-sandbox-python-guest",
)
DIAGNOSTIC_LIMIT = 64 * 1024


def emit(stage: str, **values: object) -> None:
    """Emit one bounded observation without the application's environment or host identifiers."""
    print(json.dumps({"stage": stage, "uid": os.geteuid(), **values}), flush=True)


def failure(stage: str, error: BaseException, **values: object) -> None:
    """Keep the failed operation distinct from its cause."""
    code = getattr(error, "errno", None)
    emit(
        stage,
        status="error",
        error_type=type(error).__name__,
        errno=code,
        errno_name=errno.errorcode.get(code) if isinstance(code, int) else None,
        detail=str(error)[:2048],
        **values,
    )


def devices() -> None:
    """Inspect and open both Linux devices; only a usable KVM descriptor admits VM creation."""
    import fcntl

    for device in ("/dev/kvm", "/dev/mshv"):
        try:
            info = os.stat(device)
            emit(
                "device_stat",
                device=device,
                character=stat.S_ISCHR(info.st_mode),
                mode=oct(stat.S_IMODE(info.st_mode)),
                owner=info.st_uid,
                group=info.st_gid,
                major=os.major(info.st_rdev),
                minor=os.minor(info.st_rdev),
            )
        except OSError as error:
            failure("device_stat", error, device=device)
        try:
            descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            failure("device_open", error, device=device)
            continue
        try:
            emit("device_open", status="ok", device=device)
            if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                emit("vm_create", status="skipped", reason="not_character_device", device=device)
                continue
            if device != "/dev/kvm":
                emit("vm_create", status="deferred_to_sdk", device=device)
                continue
            try:
                api = fcntl.ioctl(descriptor, 0xAE00, 0)  # KVM_GET_API_VERSION
                emit("kvm_api", version=api)
            except OSError as error:
                failure("kvm_api", error)
                continue
            if api != 12:
                emit("vm_create", status="skipped", reason="unsupported_kvm_api", device=device)
                continue
            try:
                vm = fcntl.ioctl(descriptor, 0xAE01, 0)  # KVM_CREATE_VM, default machine type
            except OSError as error:
                failure("vm_create", error, device=device)
            else:
                os.close(vm)
                emit("vm_create", status="ok", device=device)
        finally:
            os.close(descriptor)


def guest() -> None:
    """Attempt native initialization even when device discovery failed."""
    stage = "sdk_versions"
    try:
        versions = {name: version(name) for name in PACKAGES}
        emit(stage, versions=versions)
        if set(versions.values()) != {"0.7.0"}:
            raise RuntimeError("the SDK/backend/guest must all be exactly 0.7.0")
        stage = "sdk_import"
        sdk = importlib.import_module("hyperlight_sandbox")
        emit(stage, status="ok")
        stage = "guest_cache"
        module = importlib.import_module("python_guest.path")
        emit(stage, status="ok", bytes=Path(module.MODULE_PATH).stat().st_size)
        stage = "sdk_construct"
        emit(stage, status="starting")
        sandbox = sdk.Sandbox(
            backend="wasm", module="python_guest.path", heap_size="400Mi", stack_size="200Mi"
        )
        emit(stage, status="ok")
        stage = "guest_run"
        # The SDK can defer actual VM creation until the first run.
        emit(stage, status="starting")
        result = sandbox.run("answer=6*7; print(answer)")
        emit(stage, stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code)
        if result.exit_code != 0 or result.stdout.strip() != "42":
            raise RuntimeError("unexpected Python guest result")
        stage = "snapshot_restore"
        baseline = sandbox.snapshot()
        changed = sandbox.run("answer=99")
        if changed.exit_code != 0:
            raise RuntimeError("guest state change failed")
        sandbox.restore(baseline)
        restored = sandbox.run("print(answer)")
        if restored.exit_code != 0 or restored.stdout.strip() != "42":
            raise RuntimeError("snapshot did not restore Python state")
        emit(stage, status="ok", stdout=restored.stdout)
    except Exception as error:
        failure(stage, error)


def run_child(stage: str, uid: int) -> bool:
    """Drain diagnostics concurrently and kill the child's process group at the deadline."""
    with tempfile.TemporaryDirectory(prefix="hyperlight-aca-") as directory:
        os.chmod(directory, 0o700)
        if os.geteuid() == 0:
            os.chown(directory, uid, uid)
        environment = {
            "PATH": os.defpath,
            "HOME": directory,
            "XDG_CACHE_HOME": directory,
            "TMPDIR": directory,
            "HYPERLIGHT_MAX_SURROGATES": "0",
        }
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(Path(__file__).resolve()), "--child", stage],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=directory,
            user=uid,
            group=uid,
            extra_groups=[],
            start_new_session=True,
        )
        captured: dict[str, bytes] = {}

        def drain(label: str, stream) -> None:
            retained = bytearray()
            while chunk := stream.read(8192):
                retained.extend(chunk[: max(0, DIAGNOSTIC_LIMIT - len(retained))])
            captured[label] = bytes(retained)

        readers = [
            threading.Thread(target=drain, args=(label, stream), daemon=True)
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Closing the whole group also covers a descendant retaining a diagnostic pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The child may have already exited with its entire process group.
                pass
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise RuntimeError("child diagnostic pipes did not close after process-group kill")
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        stdout = captured.get("stdout", b"").decode("utf-8", errors="replace")
        try:
            completion = json.loads(stdout.splitlines()[-1])
        except (IndexError, ValueError):
            completion = None
        complete = (
            not timed_out
            and process.returncode == 0
            and captured.keys() == {"stdout", "stderr"}
            and completion == {"stage": "child_complete", "uid": uid, "child_stage": stage}
        )
        emit(
            "child_result",
            child_stage=stage,
            child_uid=uid,
            returncode=process.returncode,
            timed_out=timed_out,
            collection_complete=complete,
            stdout=stdout,
            stderr=captured.get("stderr", b"").decode("utf-8", errors="replace"),
        )
        return complete


def main() -> None:
    """Run fixed probes once, optionally remaining alive for ACA console-log collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("devices", "guest"))
    parser.add_argument("--hold-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.child:
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith(("CapEff:", "NoNewPrivs:", "Seccomp:"))
        )
        emit(
            "worker_identity",
            gid=os.getegid(),
            groups=os.getgroups(),
            security={key: value.strip() for key, value in status.items()},
        )
        {"devices": devices, "guest": guest}[args.child]()
        emit("child_complete", child_stage=args.child)
        return
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise SystemExit("this probe requires Linux x86-64")
    cpuinfo = Path("/proc/cpuinfo").read_text()
    emit(
        "environment",
        python=platform.python_version(),
        architecture=platform.machine(),
        kernel=platform.release(),
        distribution=platform.freedesktop_os_release().get("PRETTY_NAME"),
        cpu_flags={flag: flag in cpuinfo.split() for flag in ("vmx", "svm", "hypervisor")},
    )
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/cpu.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            emit("cgroup", path=path, value=Path(path).read_text().strip())
        except OSError as error:
            failure("cgroup", error, path=path)
    identities = (0, 65534) if os.geteuid() == 0 else (os.geteuid(),)
    complete = True
    for uid in identities:
        for stage in ("devices", "guest"):
            try:
                if not run_child(stage, uid):
                    complete = False
            except Exception as error:
                failure("child_launch_or_cleanup", error, child_stage=stage, child_uid=uid)
                complete = False
    if not complete:
        emit("probe_incomplete")
        raise SystemExit(1)
    emit("probe_complete")
    time.sleep(max(0, min(args.hold_seconds, 1800)))


if __name__ == "__main__":
    main()
