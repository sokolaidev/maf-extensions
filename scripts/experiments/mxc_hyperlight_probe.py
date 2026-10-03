"""Reproduce the MXC v0.9.0 one-shot controls for spike #1649."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main() -> int:
    """Record fixed one-shot controls without claiming persistent-session support."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--setup", action="store_true", help="Download and warm the agent image")
    parser.add_argument("--run", action="store_true", help="Execute the fixed guest controls")
    args = parser.parse_args()
    executor = args.executor.resolve(strict=True)
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    home = state / "home"
    home.mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(home),
            "APPDATA": str(home),
            "TMP": str(state),
            "TEMP": str(state),
            "TMPDIR": str(state),
            "MXC_HYPERLIGHT_HOME": str(state / "images"),
        }
    )
    results: dict[str, object] = {
        "mxc_ref": "86fb3d2abaf9c431556692037bff881830b543a5",
        "executor_sha256": hashlib.sha256(executor.read_bytes()).hexdigest(),
        "controls": [],
    }
    controls: list[dict[str, object]] = []
    results["controls"] = controls

    def execute(name: str, command: list[str], timeout: int = 60) -> int | None:
        with (
            (state / f"{name}.stdout").open("wb") as stdout,
            (state / f"{name}.stderr").open("wb") as stderr,
        ):
            try:
                result = subprocess.run(
                    [str(executor), *command],
                    cwd=state,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout,
                    check=False,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = None
        controls.append({"name": name, "exit_code": code, "timed_out": code is None})
        (state / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"{name}: exit={code}", flush=True)
        return code

    def request(
        name: str, code: str, *, dry_run: bool = False, legacy_network: bool = False
    ) -> None:
        payload: dict[str, object] = {
            "version": "0.10.0-alpha",
            "containment": "hyperlight",
            "process": {"commandLine": code, "timeout": 30000},
            "hyperlight": {"runtime": "agent"},
        }
        if legacy_network:
            payload["network"] = {"allowedHosts": ["example.com"]}
        config = state / f"{name}.json"
        config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        command = ["--experimental"]
        if dry_run:
            command.append("--dry-run")
        execute(name, [*command, str(config)])

    execute("help", ["--help"])
    execute("probe", ["--probe"])
    request("closed_schema", "print('mxc-spike')", dry_run=True)
    request(
        "legacy_allowlist_schema", "print('must not execute')", dry_run=True, legacy_network=True
    )
    if args.setup and execute("setup_agent", ["--setup-hyperlight=agent"], timeout=600) != 0:
        return 1
    if args.run:
        request(
            "rich_python",
            "import sys, numpy as np, pandas as pd\nprint(sys.version)\nprint(np.arange(4).sum())\nprint(pd.DataFrame({'x': [1, 2]}).sum().to_dict())",
        )
        request(
            "streams", "import sys\nprint('guest-stdout')\nprint('guest-stderr', file=sys.stderr)"
        )
        request("guest_exception", "raise ValueError('mxc-spike-controlled-error')")
        request("fresh_seed", "mxc_spike_value = 73\nprint(mxc_spike_value)")
        request("fresh_read", "print(globals().get('mxc_spike_value', 'absent'))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
