"""Qualify bounded native capture and durable truncation metadata on a real helper."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from host_call import digest


def main() -> int:
    """Measure fixed programs and require native outcomes rather than supervisor fallbacks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    host = Path(__file__).with_name("host_call.py")
    sequence = 0
    measurements = {}

    def call(
        name: str,
        code: str,
        limit: int = 1024 * 1024,
        *,
        fault: str | None = None,
        fail: bool = False,
    ):
        nonlocal sequence
        sequence += 1
        prefix = state / f"step-{sequence}"
        source = prefix.with_suffix(".py")
        source.write_text(code, encoding="utf-8")
        report = prefix.with_suffix(".json")
        command = [
            sys.executable,
            str(host),
            "--helper",
            str(args.helper.resolve()),
            "--startup",
            str(args.startup.resolve()),
            "--store",
            str(state / f"store-{limit}"),
            "--work",
            str(prefix),
            "--code",
            str(source),
            "--call-id",
            name,
            "--session-id",
            "bounded-output-probe",
            "--report",
            str(report),
            "--bounded-output",
            "--output-limit",
            str(limit),
        ]
        if fault:
            command.extend(["--fault", fault])
        with prefix.with_suffix(".log").open("wb") as log:
            child = subprocess.Popen(command, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 180
                while child.poll() is None:
                    if fault and report.exists():
                        assert json.loads(report.read_bytes()) == {"paused": fault}
                        child.kill()
                        child.wait(timeout=15)
                        return None
                    if time.monotonic() > deadline:
                        raise RuntimeError("supervisor deadline is inconclusive")
                    time.sleep(0.05)
                if fail:
                    assert child.returncode != 0 and not report.exists()
                    assert not (prefix / "native.json").exists()
                    error = (prefix / "native.stderr").read_bytes()
                    assert (b"TimedOut" if name == "timeout" else b"GuestExit(1)") in error
                    return None
                assert child.returncode == 0, f"inspect step {sequence}"
                data = json.loads(report.read_bytes())
                if data["redelivered"]:
                    assert not (prefix / "candidate").exists()
                else:
                    assert (prefix / "native.stdout").read_bytes() == b""
                return data
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=15)

    def check(name: str, code: str, expected: bytes, omitted: int, limit: int = 1024 * 1024):
        data = call(name, code, limit)
        assert data and not data["redelivered"]
        payload = base64.b64decode(data["result"]["console_base64"], validate=True)
        assert payload == expected
        metadata = data["result"]["output"]
        assert metadata == {
            "limit_bytes": limit,
            "retained_bytes": len(expected),
            "omitted_bytes": omitted,
            "omitted_bytes_saturated": False,
            "truncated": omitted > 0,
        }
        measurements[name] = {"output": metadata, "sha256": hashlib.sha256(payload).hexdigest()}
        return data

    check("empty", "pass", b"", 0)
    check("exact", "import os\nfor _ in range(1024): os.write(1, b'X' * 1024)", b"X" * 1048576, 0)
    code = (
        "import os\nfor _ in range(1025): os.write(1, b'X' * 1024)\ncontinued_after_overflow = 73"
    )
    check("overflow", code, b"X" * 1048576, 1024)
    check(
        "continued", "assert continued_after_overflow == 73\nimport os\nos.write(2,b'OK')", b"OK", 0
    )
    check("unicode", "import os\nos.write(1,'a€'.encode())\nos.write(2,b'z')", b"a", 4, 3)
    check("zero", "import os\nos.write(1,b'xyz')", b"", 3, 0)
    check(
        "bypass",
        "import os\nr,w=os.pipe()\nos.dup2(w,1)\nfd=os.open('/dev/stdout',os.O_WRONLY)\nos.write(fd,b'B'*64)",
        b"B" * 8,
        56,
        8,
    )
    # A completed call with a truncated prefix is still committed before acknowledgment.
    crash_code = "import os\nfor _ in range(1025): os.write(1,b'R'*1024)\nrecovered_counter = 19"
    call("lost-ack", crash_code, fault="after_commit")
    saved = call("lost-ack", crash_code)
    repeated = call("lost-ack", crash_code)
    assert saved and repeated and saved["redelivered"] and repeated["redelivered"]
    assert saved["result"] == repeated["result"]
    assert saved["result"]["output"]["truncated"]
    assert saved["result"]["output"]["omitted_bytes"] == 1024
    assert base64.b64decode(saved["result"]["console_base64"]) == b"R" * 1048576
    check("recovered", "assert recovered_counter == 19\nimport os\nos.write(1,b'NEXT')", b"NEXT", 0)
    call(
        "guest-error",
        "import os\nos.write(1,b'{\"captured\":true}')\nraise ValueError('expected')",
        fail=True,
    )
    call("timeout", "import os\nwhile True: os.write(1,b'T'*1024)", fail=True)
    check("after-failure", "assert recovered_counter == 19", b"", 0)
    result = {
        "helper_sha256": digest(args.helper),
        "startup_index_sha256": digest(args.startup / "index.json"),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "host_python": platform.python_version(),
        "measurements": measurements,
        "lost_ack_redelivery_identical": True,
        "checkpoint_after_truncation": True,
        "guest_failure_and_timeout_refuse_publication": True,
        "scope": "bounded HostPrint text capture; no full stream or binary fidelity qualification",
    }
    (state / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS: bounded native console capture and durable truncation metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
