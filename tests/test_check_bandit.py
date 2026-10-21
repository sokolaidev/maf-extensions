"""Exercise the Bandit gate's baseline boundaries and real scanner failures."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_bandit  # noqa: E402

pytestmark = pytest.mark.workflow


def finding(**changes):
    return {
        "filename": "./scripts/example.py",
        "test_id": "B102",
        "issue_severity": "MEDIUM",
        "issue_confidence": "HIGH",
        "code": "10 exec(value)\n",
        "line_number": 10,
        "issue_text": "Use of exec detected.",
        **changes,
    }


def report(*findings):
    return {"errors": [], "metrics": {"_totals": {"loc": 1}}, "results": list(findings)}


def baseline(item):
    return [dict(zip(check_bandit.FIELDS, check_bandit.finding_key(item)), count=1)]


def test_baseline_tolerates_line_shifts_and_windows_paths():
    before = finding()
    after = finding(filename="scripts\\example.py", code="25 exec(value)\r\n", line_number=25)
    assert check_bandit.check_report(report(after), baseline(before)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "scripts/another.py"},
        {"code": "10 exec(other_value)\n"},
        {"test_id": "B307"},
        {"issue_severity": "HIGH"},
        {"issue_confidence": "MEDIUM"},
    ],
)
def test_changed_finding_is_not_hidden_by_baseline(change):
    assert check_bandit.check_report(report(finding(**change)), baseline(finding())) == 1


def test_second_identical_finding_in_same_file_fails():
    item = finding()
    duplicate = finding(code="30 exec(value)\n", line_number=30)
    assert check_bandit.check_report(report(item, duplicate), baseline(item)) == 1


def test_removed_findings_do_not_block_cleanup():
    assert check_bandit.check_report(report(), baseline(finding())) == 0


@pytest.mark.parametrize(
    "count,severity",
    [(0, "MEDIUM"), (-1, "MEDIUM"), (1.5, "MEDIUM"), (True, "MEDIUM"), (1, "HIGH")],
)
def test_invalid_allowances_are_refused(count, severity):
    entries = baseline(finding())
    entries[0].update(count=count, issue_severity=severity)
    with pytest.raises(ValueError):
        check_bandit.check_report(report(), entries)


@pytest.mark.parametrize("broken", [{"errors": [{"reason": "syntax error"}]}, {"metrics": {}}])
def test_incomplete_scan_fails(broken):
    assert check_bandit.check_report({**report(), **broken}, []) == 2


@pytest.mark.parametrize(
    "source,expected",
    [("value = 1\n", 0), ("exec(value)\n", 1), ("def broken(\n", 2)],
)
def test_real_scanner_retains_report_and_fails_closed(tmp_path, monkeypatch, source, expected):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "example.py").write_text(source, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.bandit]\n", encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(check_bandit, "ROOT", tmp_path)
    monkeypatch.setattr(check_bandit, "BASELINE", baseline_path)
    monkeypatch.setattr(check_bandit, "TARGETS", ("source",))
    output = tmp_path / "report.json"
    assert check_bandit.main(["--report", str(output)]) == expected
    saved = json.loads(output.read_text(encoding="utf-8"))
    if expected == 1:
        assert saved["results"][0]["test_id"] == "B102"
    elif expected == 2:
        assert saved["errors"]


def test_scanner_execution_failure_cannot_reuse_old_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text(json.dumps(report()), encoding="utf-8")
    monkeypatch.setattr(
        check_bandit.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 2)
    )
    assert check_bandit.scan(output) == 2


def test_malformed_baseline_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "baseline.json"
    path.write_text("invalid JSON", encoding="utf-8")
    output = tmp_path / "report.json"
    output.write_text(json.dumps(report()), encoding="utf-8")
    monkeypatch.setattr(check_bandit, "BASELINE", path)
    monkeypatch.setattr(
        check_bandit.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    assert check_bandit.main(["--report", str(output)]) == 2


def test_every_tracked_python_root_is_scanned():
    paths = subprocess.check_output(
        ["git", "ls-files", "--", "*.py"], cwd=ROOT, text=True
    ).splitlines()
    assert paths
    assert {path.split("/")[0] for path in paths} <= set(check_bandit.TARGETS)


def test_ci_retains_full_report_after_failure_and_joins_security_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    steps = jobs["security"]["steps"]
    gate = next(step for step in steps if "check_bandit.py" in step.get("run", ""))
    assert gate["run"] == "uv run python scripts/check_bandit.py --report bandit-report.json"
    assert not gate.get("continue-on-error")
    upload = next(step for step in steps if "upload-artifact@" in step.get("uses", ""))
    assert "always()" in upload["if"]
    assert upload["with"]["path"] == "bandit-report.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert "security" in jobs["tests"]["needs"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["poe"]["tasks"]["security"] == "python scripts/check_bandit.py"


def test_baseline_has_no_duplicate_keys_or_blank_context():
    entries = json.loads(check_bandit.BASELINE.read_text(encoding="utf-8"))
    keys = [tuple(entry[field] for field in check_bandit.FIELDS) for entry in entries]
    assert len(keys) == len(set(keys))
    assert all(entry["code"].strip() for entry in entries)
    check_bandit.check_report(deepcopy(report()), entries)
