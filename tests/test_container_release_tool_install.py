"""Release tool installation has a short deadline and propagates package-manager failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.workflow


def install_step(job):
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-image-release.yml").read_text())
    (step,) = [step for step in workflow["jobs"][job]["steps"] if "apt-get" in step.get("run", "")]
    return step


@pytest.mark.parametrize("job", ["prepare", "promote"])
def test_tool_install_has_its_own_deadline(job):
    assert 0 < install_step(job)["timeout-minutes"] <= 10


@pytest.mark.skipif(sys.platform != "linux", reason="release tool installation uses Linux Bash")
@pytest.mark.parametrize("job", ["prepare", "promote"])
@pytest.mark.parametrize("failure", ["", "update", "install"])
def test_tool_install_bounds_downloads_and_stops_on_failure(tmp_path, job, failure):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands.jsonl"
    for name, source in {
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "apt-get": (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            'with Path(os.environ["COMMAND_LOG"]).open("a") as stream:\n'
            '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'sys.exit(100 if os.environ["FAIL_OPERATION"] in sys.argv[1:] else 0)\n'
        ),
        "python3": '#!/bin/sh\nprintf \'["scanners"]\\n\' >> "$COMMAND_LOG"\n',
    }.items():
        target = tools / name
        target.write_text(source)
        target.chmod(0o755)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", install_step(job)["run"]],
        env=os.environ
        | {
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "COMMAND_LOG": str(log),
            "FAIL_OPERATION": failure,
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (100 if failure else 0), result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(commands) == {"update": 1, "install": 2, "": 3}[failure]
    for command in commands[: 2 if failure != "update" else 1]:
        for option in (
            "Acquire::Retries=3",
            "Acquire::http::Timeout=30",
            "Acquire::https::Timeout=30",
        ):
            assert command[command.index(option) - 1] == "-o"
    assert "update" in commands[0] and "--error-on=any" in commands[0]
    if not failure:
        assert commands[1][-3:] == ["install", "--yes", "skopeo"]
        assert commands[-1] == ["scanners"]
