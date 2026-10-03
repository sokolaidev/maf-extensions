"""Qualify native owner-pipe loss and cancellation using a live fixed guest program."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

OWNER = """
import sys, time
sys.stdout.buffer.write(b'MXCOWN1\\n')
sys.stdout.buffer.flush()
command = sys.stdin.buffer.read(1)
if command:
    sys.stdout.buffer.write(command)
    sys.stdout.buffer.flush()
while True:
    time.sleep(1)
"""


def digest(path: Path) -> str:
    """Identify the native executable measured by this probe."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    """Require a new evidence directory and terminate only processes owned by this probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    helper = args.helper.resolve(strict=True)
    startup = args.startup.resolve(strict=True)
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    source = state / "code.py"
    source.write_text("print('MXC_OWNER_READY', flush=True)\nwhile True: pass\n", encoding="utf-8")
    results = {}
    for name, command, expected in [
        ("owner_death", None, 74),
        ("cancel", b"C", 74),
        ("invalid_command", b"X", 75),
    ]:
        report = state / f"{name}.json"
        candidate = state / name
        log = state / f"{name}.stdout"
        with (
            log.open("wb") as stdout,
            (state / f"{name}.stderr").open("wb") as stderr,
            subprocess.Popen(
                [sys.executable, "-u", "-c", OWNER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as owner,
        ):
            assert owner.stdout is not None and owner.stdin is not None
            try:
                with subprocess.Popen(
                    [
                        str(helper),
                        "call-owned",
                        str(startup),
                        str(candidate),
                        str(source),
                        str(report),
                    ],
                    stdin=owner.stdout,
                    stdout=stdout,
                    stderr=stderr,
                ) as native:
                    owner.stdout.close()
                    try:
                        deadline = time.monotonic() + 90
                        while b"MXC_OWNER_READY" not in log.read_bytes():
                            if native.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError(f"{name}: guest did not become ready")
                            time.sleep(0.02)
                        started = time.monotonic()
                        if command is None:
                            owner.kill()
                            owner.wait(timeout=5)
                        else:
                            owner.stdin.write(command)
                            owner.stdin.flush()
                        native.wait(timeout=5)
                        elapsed = time.monotonic() - started
                        if native.returncode != expected or report.exists() or candidate.exists():
                            raise RuntimeError(f"{name}: native execution was not retired safely")
                        results[name] = {"exit_code": native.returncode, "stop_seconds": elapsed}
                    finally:
                        if native.poll() is None:
                            native.kill()
                            native.wait(timeout=5)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
    for name, header, expected in [
        ("missing_owner", b"", 74),
        ("invalid_header", b"INVALID\n", 75),
    ]:
        report = state / f"{name}.json"
        result = subprocess.run(
            [
                str(helper),
                "call-owned",
                str(state / "absent"),
                str(state / name),
                str(source),
                str(report),
            ],
            input=header,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != expected or report.exists():
            raise RuntimeError(f"{name}: execution was not refused before restore")
        results[name] = {"exit_code": result.returncode}
    record = {
        "helper_sha256": digest(helper),
        "startup_index_sha256": digest(startup / "index.json"),
        "controls": results,
        "scope": "Native process retirement through its private owner pipe; no descendant or distributed fencing qualification",
    }
    (state / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(
        "PASS: owner death, cancellation and malformed ownership controls retire native execution"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
