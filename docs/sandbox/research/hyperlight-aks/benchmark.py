"""Measure pinned Linux SDK latency and memory without changing container limits."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import platform
import resource
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

CGROUP = Path("/sys/fs/cgroup")
HELLO = "value = 42; print(value)"


def emit(stage: str, **values: Any) -> None:
    """Write one machine-readable measurement."""
    print(json.dumps({"stage": stage, **values}), flush=True)


def counters(path: Path) -> dict[str, int]:
    """Read whitespace-delimited kernel counters."""
    rows = (line.split() for line in path.read_text().splitlines())
    return {name: int(value) for name, value in rows}


def memory(stage: str) -> None:
    """Record process residency separately from container-wide accounting."""
    smaps = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "kB":
            smaps[fields[0].removesuffix(":")] = int(fields[1]) * 1024
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "kB":
            status[fields[0].removesuffix(":")] = int(fields[1]) * 1024
    emit(
        "memory",
        point=stage,
        process_bytes={
            "rss": smaps["Rss"],
            "pss": smaps["Pss"],
            "private": smaps["Private_Clean"] + smaps["Private_Dirty"],
            "virtual": status["VmSize"],
            "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        },
        container_current_bytes=int((CGROUP / "memory.current").read_text()),
        container_peak_bytes=int((CGROUP / "memory.peak").read_text()),
        container_stat=counters(CGROUP / "memory.stat"),
        memory_events=counters(CGROUP / "memory.events"),
    )


def timed(operation: Any, *args: Any) -> tuple[Any, dict[str, float]]:
    """Exclude validation, memory inspection and output from the measured interval."""
    wall = time.perf_counter_ns()
    cpu = time.process_time_ns()
    result = operation(*args)
    return result, {
        "wall_ms": (time.perf_counter_ns() - wall) / 1_000_000,
        "cpu_ms": (time.process_time_ns() - cpu) / 1_000_000,
    }


def require_result(result: Any, expected: str = "42") -> None:
    """Refuse to count failed guest execution as a timing sample."""
    if result.exit_code != 0 or result.stdout.strip() != expected or result.stderr:
        raise RuntimeError("guest result did not match the benchmark assertion")


def kvm_handles() -> int:
    """Count VM and vCPU descriptors; the shared device handle does not retain a VM."""
    count = 0
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        count += target.startswith("anon_inode:kvm-")
    return count


def initialize() -> Any:
    """Load the native SDK and materialize the guest before warm-process measurements."""
    sdk, duration = timed(lambda: importlib.import_module("hyperlight_sandbox"))
    emit("latency", operation="sdk_import", **duration)
    _, duration = timed(
        lambda: importlib.import_module("hyperlight_sandbox_backend_wasm._native_wasm")
    )
    emit("latency", operation="native_import", **duration)
    module, duration = timed(lambda: importlib.import_module("python_guest.path").get_module_path())
    emit("latency", operation="guest_materialization", **duration)
    emit("artifact", guest_bytes=Path(module).stat().st_size)
    return sdk.Sandbox


def latency(constructor: Any, count: int) -> None:
    """Compare a fresh VM in a warm process with restored and retained guest state."""
    initial_handles = kvm_handles()
    before = counters(CGROUP / "cpu.stat")
    for sample in range(count):
        sandbox, create = timed(constructor)
        result, first = timed(sandbox.run, HELLO)
        require_result(result)
        emit(
            "latency",
            operation="create_first_run",
            sample=sample,
            create=create,
            first_run=first,
            wall_ms=create["wall_ms"] + first["wall_ms"],
            cpu_ms=create["cpu_ms"] + first["cpu_ms"],
        )
        del sandbox
        gc.collect()
        if kvm_handles() != initial_handles:
            raise RuntimeError("KVM descriptors survived fresh-sandbox disposal")
    sandbox = constructor()
    require_result(sandbox.run("pass"), "")
    baseline, snapshot_time = timed(sandbox.snapshot)
    emit("latency", operation="snapshot", **snapshot_time)
    for sample in range(count):
        # Keep the first restore visible: it can have different page-allocation costs.
        _, reset = timed(sandbox.restore, baseline)
        result, execute = timed(
            sandbox.run, "print('value' in globals()); value = 42; print(value)"
        )
        require_result(result, "False\n42")
        emit(
            "latency",
            operation="restore_first_run",
            sample=sample,
            restore=reset,
            first_run=execute,
            wall_ms=reset["wall_ms"] + execute["wall_ms"],
            cpu_ms=reset["cpu_ms"] + execute["cpu_ms"],
        )
    for sample in range(count):
        result, duration = timed(sandbox.run, "print(value)")
        require_result(result)
        emit("latency", operation="retained_run", sample=sample, **duration)
    del baseline, sandbox
    gc.collect()
    if kvm_handles() != initial_handles:
        raise RuntimeError("KVM descriptors survived snapshot-sandbox disposal")
    after = counters(CGROUP / "cpu.stat")
    emit("cpu", delta={key: value - before.get(key, 0) for key, value in after.items()})


def footprint(constructor: Any) -> None:
    """Sample one guest, its snapshot, touched variables and normal release."""
    initial_handles = kvm_handles()
    memory("sdk_loaded")
    sandbox = constructor()
    memory("constructed")
    require_result(sandbox.run("pass"), "")
    memory("python_initialized")
    baseline = sandbox.snapshot()
    memory("baseline_snapshot_retained")
    for mib in (1, 16, 64):
        code = f"payload = bytearray({mib} * 1024 * 1024)\n"
        code += "for i in range(0, len(payload), 4096): payload[i] = 1\n"
        code += "print(len(payload))"
        require_result(sandbox.run(code), str(mib * 1024 * 1024))
        memory(f"guest_payload_{mib}_mib")
        sandbox.restore(baseline)
        require_result(sandbox.run("print('payload' in globals())"), "False")
        memory(f"restored_after_{mib}_mib")
    del baseline
    gc.collect()
    memory("snapshot_released")
    del sandbox
    gc.collect()
    memory("sandbox_released")
    if kvm_handles() != initial_handles:
        raise RuntimeError("KVM descriptors survived memory-probe disposal")
    emit("cleanup", result="pass", remaining_kvm_handles=initial_handles)


def main() -> None:
    """Run one bounded experiment inside an already prepared application container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("latency", "memory", "cold"), required=True)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("iterations must be between 1 and 100")
    if platform.system() != "Linux" or os.getuid() != 65534:
        raise RuntimeError("requires the non-root Linux proof container")
    versions = {
        name: version(name)
        for name in (
            "hyperlight-sandbox",
            "hyperlight-sandbox-backend-wasm",
            "hyperlight-sandbox-python-guest",
        )
    }
    if set(versions.values()) != {"0.7.0"}:
        raise RuntimeError("requires the pinned 0.7.0 SDK trio")
    emit(
        "configuration",
        mode=args.mode,
        iterations=args.iterations,
        python=platform.python_version(),
        kernel=platform.release(),
        versions=versions,
        heap_mib=400,
        stack_mib=200,
        container_limit_bytes=int((CGROUP / "memory.max").read_text()),
        cpu_max=(CGROUP / "cpu.max").read_text().strip(),
        guest_cache_exists=(
            Path(os.environ["XDG_CACHE_HOME"]) / "hyperlight_sandbox_guests/python_guest"
        ).exists(),
    )
    if args.mode == "memory":
        memory("host_python")
    sdk_constructor = initialize()

    def constructor() -> Any:
        return sdk_constructor(
            backend="wasm", module="python_guest.path", heap_size="400Mi", stack_size="200Mi"
        )

    if args.mode == "latency":
        latency(constructor, args.iterations)
    elif args.mode == "memory":
        footprint(constructor)
    else:
        sandbox, create = timed(constructor)
        result, execute = timed(sandbox.run, HELLO)
        require_result(result)
        emit("latency", operation="cold_create", **create)
        emit("latency", operation="cold_first_run", **execute)
        del sandbox
        gc.collect()
    emit("complete", result="pass")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("failure", error_type=type(error).__name__, detail=str(error)[:512])
        sys.exit(1)
