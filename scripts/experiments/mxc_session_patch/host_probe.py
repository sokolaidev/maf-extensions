"""Measure real MXC recovery across local checkpoint publication crash boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from host_call import digest

SEED = """
import base64, hashlib
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
counter = 1
Path('/tmp/input.csv').write_text('name,value\\na,3\\nb,7\\n')
frame = pd.read_csv('/tmp/input.csv')
frame.plot.bar(x='name', y='value')
plt.savefig('/tmp/chart.png', format='png')
plt.close('all')
chart = Path('/tmp/chart.png').read_bytes()
for offset in range(0, len(chart), 384):
    print('MAF_CHART:' + str(offset // 384) + ':' + base64.b64encode(chart[offset:offset+384]).decode('ascii'))
print('MAF_CHART_END:' + str(len(chart)) + ':' + hashlib.sha256(chart).hexdigest())
print('COUNTER:' + str(counter))
"""


def main() -> int:
    """Require fresh evidence and independently kill only owned host-call processes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--startup", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=False)
    host = Path(__file__).with_name("host_call.py")
    sequence = 0

    def call(call_id: str, code: str, fault: str | None = None, refuse: bool = False):
        nonlocal sequence
        sequence += 1
        prefix = state / f"step-{sequence}"
        program = prefix.with_suffix(".py")
        program.write_text(code, encoding="utf-8")
        report = prefix.with_suffix(".json")
        command = [
            sys.executable,
            str(host),
            "--helper",
            str(args.helper.resolve()),
            "--startup",
            str(args.startup.resolve()),
            "--store",
            str(state / "store"),
            "--work",
            str(prefix),
            "--code",
            str(program),
            "--call-id",
            call_id,
            "--session-id",
            "publication-probe",
            "--report",
            str(report),
        ]
        if fault:
            command.extend(["--fault", fault])
        with prefix.with_suffix(".log").open("wb") as log:
            child = subprocess.Popen(command, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 240
                while child.poll() is None and not report.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"host call {sequence} exceeded deadline")
                    time.sleep(0.1)
                if fault:
                    if not report.exists() or json.loads(report.read_text()) != {"paused": fault}:
                        raise RuntimeError(f"host did not reach {fault}; inspect step {sequence}")
                    child.kill()
                    child.wait(timeout=15)
                    return None
                child.wait(timeout=15)
                if refuse:
                    if child.returncode == 0 or report.exists():
                        raise RuntimeError("interrupted call was replayed")
                    return None
                if child.returncode != 0:
                    raise RuntimeError(f"host failed; inspect step {sequence}")
                data = json.loads(report.read_text())
                if data["redelivered"] and (prefix / "candidate").exists():
                    raise RuntimeError("redelivery executed the guest")
                return data
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=15)

    seed = call("seed", SEED)
    assert seed and seed["result"]["artifacts"]["chart.png"]
    call("uncommitted", "counter += 100", "before_commit")
    call("uncommitted", "counter += 100", refuse=True)
    recovered = call("verify-rollback", "assert counter == 1\nprint('COUNTER:1')")
    assert recovered and "COUNTER:1" in recovered["result"]["combined_output"]
    redelivery: dict[str, bool] = {}
    for point, expected in [("after_commit", 2), ("before_ack", 3)]:
        code = f"counter += 1\nassert counter == {expected}\nprint('COUNTER:{expected}')"
        call(point, code, point)
        saved = call(point, code)
        assert saved and saved["redelivered"]
        assert f"COUNTER:{expected}" in saved["result"]["combined_output"]
        repeated = call(point, code)
        assert repeated and saved["result_sha256"] == repeated["result_sha256"]
        verified = call(f"verify-{point}", f"assert counter == {expected}")
        assert verified
        redelivery[point] = True
    artifact_retry = call("seed", SEED)
    assert artifact_retry and artifact_retry["redelivered"]
    assert artifact_retry["result_sha256"] == seed["result_sha256"]
    result = {
        "helper_sha256": digest(args.helper),
        "startup_index_sha256": digest(args.startup / "index.json"),
        "sqlite_sha256": digest(state / "store/state.sqlite"),
        "before_commit_keeps_previous_checkpoint": True,
        "interrupted_call_refuses_replay": True,
        "committed_result_redelivery_without_execution": redelivery,
        "csv_to_chart": True,
        "artifact_redelivery_identical": True,
        "seed_result_sha256": seed["result_sha256"],
        "chart_encoded_sha256": hashlib.sha256(
            seed["result"]["artifacts"]["chart.png"].encode()
        ).hexdigest(),
        "scope": "local process crash; no power-loss or remote-recovery qualification",
    }
    (state / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS: MXC checkpoint/result transactions and lost acknowledgments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
