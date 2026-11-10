"""PR and main scans must upload matching CodeQL language configurations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import codeql_languages as selector  # noqa: E402

pytestmark = pytest.mark.workflow


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule", "workflow_dispatch"])
def test_workflow_outputs_all_configurations_without_a_diff(tmp_path, monkeypatch, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.delenv("BASE_SHA", raising=False)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    monkeypatch.chdir(tmp_path)
    selector.main()
    output = (tmp_path / "output").read_text(encoding="utf-8")
    matrix = json.loads(output.removeprefix("matrix="))
    assert matrix == [
        {"language": "actions", "build-mode": "none"},
        {"language": "javascript-typescript", "build-mode": "none"},
        {"language": "python", "build-mode": "none"},
        {"language": "rust", "build-mode": "none"},
        {"language": "go", "build-mode": "manual"},
    ]
    assert (tmp_path / "summary").read_text(encoding="utf-8") == (
        "CodeQL languages: actions, javascript-typescript, python, rust, go\n"
    )


def test_unsupported_event_fails():
    with pytest.raises(ValueError, match="Unsupported CodeQL event"):
        selector.analysis_matrix("pull_request_target")


def test_workflow_analyzes_every_configuration_and_reports_failures():
    workflow = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert "paths" not in events["pull_request"]
    assert "paths-ignore" not in events["pull_request"]
    jobs = workflow["jobs"]
    select = jobs["select"]
    assert select["outputs"]["matrix"] == "${{ steps.select.outputs.matrix }}"
    _, step = select["steps"]
    assert step["id"] == "select"
    assert step["run"] == "python3 scripts/codeql_languages.py"
    assert jobs["analyze"]["needs"] == "select"
    assert "if" not in jobs["analyze"]
    assert (
        jobs["analyze"]["strategy"]["matrix"]["include"]
        == "${{ fromJSON(needs.select.outputs.matrix) }}"
    )
    assert jobs["report-failure"]["needs"] == ["select", "analyze"]
    assert jobs["report-failure"]["if"] == (
        "always() && (needs.select.result == 'failure' || needs.analyze.result == 'failure')"
    )
