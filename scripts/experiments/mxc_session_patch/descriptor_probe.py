"""Measure descriptor redirection without treating guest claims as trusted completion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

LIMIT = 1024 * 1024
ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    """Hash an artifact without buffering it in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def execute(
    helper: Path,
    startup: Path,
    state: Path,
    name: str,
    code: str,
    *,
    capture: bool = False,
    cancel: bool = False,
) -> dict[str, object]:
    """Bound supervisor retention and time for each fixed native experiment."""
    source = state / f"{name}.py"
    source.write_text(code, encoding="utf-8")
    report = state / f"{name}.control.json"
    outputs = [bytearray(), bytearray()]
    overflow = threading.Event()
    ready = threading.Event()
    with subprocess.Popen(
        [
            str(helper),
            "call-owned" if capture else "execute-owned",
            str(startup),
            str(state / name),
            str(source),
            str(report),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        assert child.stdin is not None
        child.stdin.write(b"MXCOWN1\n")
        child.stdin.flush()

        def drain(index: int) -> None:
            stream = child.stdout if index == 0 else child.stderr
            assert stream is not None
            while data := os.read(stream.fileno(), 4096):
                room = LIMIT - len(outputs[index])
                outputs[index].extend(data[:room])
                if len(data) > room:
                    overflow.set()
                if index == 0 and b"MXC_OUTPUT_READY" in outputs[0]:
                    ready.set()

        readers = [threading.Thread(target=drain, args=(index,), daemon=True) for index in (0, 1)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + 75
        cancelled = False
        killed = False
        try:
            while child.poll() is None:
                if cancel and ready.is_set() and not cancelled:
                    child.stdin.write(b"C")
                    child.stdin.flush()
                    cancelled = True
                if overflow.is_set() or time.monotonic() > deadline:
                    child.kill()
                    killed = True
                    break
                time.sleep(0.02)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise RuntimeError("native output reader did not stop")
        control = None
        if report.exists():
            if report.stat().st_size > 128:
                raise RuntimeError("oversized native control")
            control = json.loads(report.read_bytes())
            if child.returncode != 0 or control != (
                {"captured": True} if capture else {"executed": True}
            ):
                raise RuntimeError("unexpected native control")
        for suffix, data in zip(("stdout", "stderr"), outputs):
            (state / f"{name}.{suffix}").write_bytes(data)
        return {
            "exit_code": child.returncode,
            "control": control,
            "supervisor_killed": killed,
            "supervisor_overflow": overflow.is_set(),
            "cancel_sent": cancelled,
            "stdout_bytes": len(outputs[0]),
            "stdout_sha256": hashlib.sha256(outputs[0]).hexdigest(),
            "stderr_bytes": len(outputs[1]),
            "stderr_sha256": hashlib.sha256(outputs[1]).hexdigest(),
        }


def observation(state: Path, name: str) -> object:
    """Read a fixed guest's measurement, never a native completion signal."""
    lines = (state / f"{name}.stdout").read_bytes().splitlines()
    records = [
        line.removeprefix(b"MXC_OBSERVATION:")
        for line in lines
        if line.startswith(b"MXC_OBSERVATION:")
    ]
    if len(records) != 1:
        raise RuntimeError(f"{name}: missing or duplicate guest observation")
    return json.loads(records[0])


def validate(results: dict[str, Any], state: Path) -> dict[str, bool]:
    """Require the measured positive controls and the counterexamples before drawing a conclusion."""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise RuntimeError(message)

    expected = {
        "console_4095",
        "console_4096",
        "console_4097",
        "fidelity",
        "exact",
        "overflow",
        "bypass",
        "file_limit",
        "timeout",
        "cancel",
        "file_seed",
        "file_restore",
        "pipe_seed",
        "pipe_restore",
    }
    require(set(results) == expected, "incomplete descriptor experiment")
    for name, result in results.items():
        require(
            not result["supervisor_killed"] and not result["supervisor_overflow"],
            f"{name}: supervisor fallback is not a runtime result",
        )
        if name not in {"file_limit", "timeout", "cancel"}:
            require(
                result["exit_code"] == 0
                and result["control"]
                == ({"captured": True} if name.endswith("_seed") else {"executed": True}),
                f"{name}: native success missing",
            )
    for size in (4095, 4096, 4097):
        result = results[f"console_{size}"]
        data = b"X" * min(size, 4096)
        require(
            result["stdout_bytes"] == len(data)
            and result["stdout_sha256"] == hashlib.sha256(data).hexdigest(),
            "console boundary changed",
        )

    def stream(data: bytes) -> dict[str, object]:
        return {
            "total": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "retained_bytes": min(len(data), LIMIT),
            "retained_sha256": hashlib.sha256(data[:LIMIT]).hexdigest(),
            "overflow": len(data) > LIMIT,
        }

    fidelity = [
        stream(
            bytes(range(256)) * 32
            + ("\u20ac\U0001f642\n" * 1000).encode()
            + bytes([fd]) * 4097
            + b"DUP\x00\xffVEC\x00\xffNATIVE\x00\xffPYTHON\n"
        )
        for fd in (1, 2)
    ]
    require(
        results["fidelity"]["guest_observation"] == fidelity, "fixed guest byte fidelity differs"
    )
    for name, count in (("exact", LIMIT), ("overflow", LIMIT + 1)):
        require(
            results[name]["guest_observation"] == [stream(b"Q" * count), stream(b"")],
            f"{name}: retention/overflow observation differs",
        )
    require(
        results["bypass"]["guest_observation"] == [stream(b""), stream(b"")]
        and b"MXC_BYPASS\r\n" in (state / "bypass.stdout").read_bytes(),
        "console-bypass counterexample changed",
    )
    require(
        results["file_limit"]["exit_code"] == 1
        and results["file_limit"]["control"] is None
        and b"Unsupported resource 1" in (state / "file_limit.stdout").read_bytes(),
        "file-size-limit refusal changed",
    )
    require(
        results["timeout"]["exit_code"] == 1
        and results["timeout"]["control"] is None
        and b"TimedOut" in (state / "timeout.stderr").read_bytes(),
        "native deadline did not retire the pipe workload",
    )
    require(
        results["cancel"]["exit_code"] == 74
        and results["cancel"]["control"] is None
        and results["cancel"]["cancel_sent"],
        "owner cancellation failed",
    )
    for kind in ("file", "pipe"):
        require(
            (state / f"{kind}_restore.stdout").read_bytes() == b"MXC_RESTORED\r\n",
            f"{kind}: restored descriptor content differs",
        )
    return {
        "console_truncation_reproduced": True,
        "cooperative_pipe_fidelity": True,
        "cooperative_retention_limit": True,
        "guest_overflow_does_not_fail_native_execution": True,
        "console_bypasses_redirected_descriptors": True,
        "file_size_rlimit_unsupported": True,
        "file_and_buffered_pipe_restore": True,
        "timeout_and_cancellation_retire_native": True,
        "satisfies_host_enforced_output_contract": False,
    }


def console_program(size: int) -> str:
    """Generate the single-write console boundary workload."""
    return (
        f"import os\nwritten = os.write(1, b'X' * {size})\n"
        f"if written != {size}:\n"
        f"    raise RuntimeError(f'console write returned {{written}}, expected {size}')\n"
    )


def main() -> int:
    """Record independent outcomes for the pinned descriptor alternatives."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    helper = args.helper.resolve(strict=True)
    startup = args.startup.resolve(strict=True)
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    guest = (ROOT / "descriptor_guest.py").read_text(encoding="utf-8-sig")
    results = {}
    for name in (
        "console_4095",
        "console_4096",
        "console_4097",
        "fidelity",
        "exact",
        "overflow",
        "bypass",
        "file_limit",
        "timeout",
        "cancel",
    ):
        code = f"MXC_CASE = {name!r}\n" + guest
        if name.startswith("console_"):
            size = int(name.split("_")[1])
            code = console_program(size)
        result = execute(helper, startup, state, name, code, cancel=name == "cancel")
        if result["control"] is not None and not name.startswith("console_"):
            result["guest_observation"] = observation(state, name)
        results[name] = result
        print(f"{name}: {json.dumps(result)}", flush=True)
    for kind in ("file", "pipe"):
        seed = """import os, sys
sys.stdout.flush()
mxc_saved_stdout = os.dup(1)
"""
        if kind == "file":
            seed += (
                "mxc_sink = os.open('/tmp/mxc-persistent-output', os.O_CREAT | os.O_RDWR, 0o600)\n"
            )
        else:
            seed += "mxc_read, mxc_sink = os.pipe()\n"
        seed += "os.dup2(mxc_sink, 1)\nos.write(1, b'before\\x00')\n"
        name = f"{kind}_seed"
        results[name] = execute(helper, startup, state, name, seed, capture=True)
        if results[name]["control"] is not None:
            restore = "os.write(1, b'after\\xff')\nos.dup2(mxc_saved_stdout, 1)\nos.close(mxc_saved_stdout)\n"
            if kind == "file":
                restore += "os.lseek(mxc_sink, 0, 0)\ndata = os.read(mxc_sink, 13)\n"
            else:
                restore += "data = os.read(mxc_read, 13)\n"
            restore += "assert data == b'before\\x00after\\xff', repr(data)\nprint('MXC_RESTORED', flush=True)\n"
            results[f"{kind}_restore"] = execute(
                helper, state / name, state, f"{kind}_restore", restore
            )
    record = {
        "platform": platform.system(),
        "architecture": platform.machine(),
        "helper_sha256": digest(helper),
        "startup_index_sha256": digest(startup / "index.json"),
        "guest_sha256": digest(ROOT / "descriptor_guest.py"),
        "results": results,
        "scope": "Fixed-workload observations; guest collectors are not a host-enforced output boundary",
    }
    (state / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    record["assessment"] = validate(results, state)
    (state / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
