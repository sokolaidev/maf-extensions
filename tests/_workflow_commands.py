"""Read Python command arguments from the production workflow's thin shell wrappers."""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-packages.yml"
LIVE_WORKFLOW = ROOT / ".github/workflows/verify-live.yml"


def command_arguments(block: str, script: str, variables: dict[str, str]) -> list[str]:
    """Expand only named environment variables, without interpreting any shell code."""
    commands = block.replace("\\\n", " ").splitlines()
    command = next(
        line for line in commands if script in line and not line.lstrip().startswith("#")
    )
    tokens = shlex.split(command)

    def expand(match: re.Match[str]) -> str:
        return variables[match[1] or match[2]]

    return [re.sub(r"\$\{(\w+)\}|\$(\w+)", expand, token) for token in tokens[2:]]


def run_release(tmp_path: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute the production release entry point with isolated Actions output files."""
    for name in ("out.txt", "summary.md"):
        (tmp_path / name).touch()
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_workflow.py"), *arguments],
        capture_output=True,
        cwd=tmp_path,
        env=os.environ
        | {
            "PYTHONUTF8": "1",
            "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
    )


def run_block(workflow: Path, step_name: str) -> str:
    """Return a step's dedented script as received by the runner's shell."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == f"- name: {step_name}"
    )
    run = next(index for index, line in enumerate(lines[start:], start) if line.strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:])
    return "\n".join(body)


def execute_release_step(
    tmp_path: Path,
    step: str,
    stdout: str,
    *,
    stderr: str = "",
    status: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    """Run the production release wrapper against a controlled native checker process."""
    arguments = command_arguments(
        run_block(PUBLISH_WORKFLOW, step),
        "release_workflow.py",
        {"PACKAGE": "maf-sandbox", "VERSION": "0.13.0"},
    )
    stub = tmp_path / "checker with spaces.py"
    stub.write_text(
        f"import sys\nsys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\n"
        f"raise SystemExit({status})\n",
        encoding="utf-8",
    )
    arguments = arguments[: arguments.index("--") + 1] + [sys.executable, str(stub)]
    return run_release(tmp_path, arguments)


def release_outputs(tmp_path: Path) -> tuple[str, str]:
    return (
        (tmp_path / "out.txt").read_text("utf-8"),
        (tmp_path / "summary.md").read_text("utf-8"),
    )


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("check_live_fix_loop_sample")
_HOST_TOOLS = _load("check_live_host_tools_call_sample")


@dataclasses.dataclass(frozen=True)
class Retrying:
    """A live step that spends a second attempt on its model, and the check it keys on.

    `marks` finds the step by its profile: sample 15 has two backend legs.
    """

    label: str
    marks: str
    check: ModuleType
    readme: Path


#: Every step allowed to loop, and the claim `test_no_other_live_sample_retries` holds the
#: workflow to.
RETRYING = (
    Retrying(
        "sample 13",
        "retry_live_sample.py sample13 ",
        check,
        ROOT / "samples" / "13_bicep_fix_loop" / "README.md",
    ),
    Retrying(
        "sample 15",
        "retry_live_sample.py sample15 ",
        _HOST_TOOLS,
        ROOT / "samples" / "15_acas_codeact_host_tools" / "README.md",
    ),
    Retrying(
        "sample 15 on docker",
        "retry_live_sample.py sample15-docker ",
        _HOST_TOOLS,
        ROOT / "samples" / "15_acas_codeact_host_tools" / "README.md",
    ),
)


def retry_step(retrying: Retrying = RETRYING[0]) -> dict:
    """The step that runs a sample, found by what it runs rather than by its name."""
    workflow = yaml.safe_load(LIVE_WORKFLOW.read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if retrying.marks in step.get("run", "")
    ]
    assert len(steps) == 1, f"expected one step for {retrying.label}, found {len(steps)}"
    return steps[0]


def isolated_retry_block(retrying: Retrying, output: Path) -> str:
    """Keep the production Bash command, redirecting its log into the test directory."""
    block = retry_step(retrying)["run"]
    rewritten, count = re.subn(
        r"(--output\s+)\S+", lambda m: m[1] + shlex.quote(str(output)), block
    )
    assert count == 1
    return rewritten
