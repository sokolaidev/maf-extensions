"""No workflow expands a `${{ }}` expression inside a `run:` script.

The runner substitutes an expression into the script's text before the shell parses it, so a
value carrying a quote or `$(...)` runs as code. Passed through the step's `env:`, the same value
reaches the shell as data.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
#: A `run:` with a value on its line; `defaults: run:` is a mapping, not a script.
RUN_KEY = re.compile(r"^[ \t]*(?:- )?run:[ \t]*\S", re.MULTILINE)


def _run_blocks(path: Path) -> list[tuple[str, str]]:
    workflow = yaml.safe_load(path.read_text("utf-8")) or {}
    return [
        (f"{path.name}: {job_name}: {step.get('name', f'step {index}')}", str(step["run"]))
        for job_name, job in (workflow.get("jobs") or {}).items()
        for index, step in enumerate(job.get("steps") or [])
        if "run" in step
    ]


def _workflows() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_the_scan_reaches_every_run_key_in_the_files():
    """The check below passes vacuously on any step the parse walks past."""
    paths = _workflows()
    assert paths
    for path in paths:
        assert len(_run_blocks(path)) == len(RUN_KEY.findall(path.read_text("utf-8"))), path.name


def test_no_run_block_expands_an_expression():
    offending = [
        where for path in _workflows() for where, block in _run_blocks(path) if "${{" in block
    ]
    assert not offending, (
        "these steps expand an expression inside `run:`; pass the value through the step's "
        f'`env:` and read it as "$NAME": {offending}'
    )
