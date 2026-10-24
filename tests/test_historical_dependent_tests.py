"""Historical corrections cannot conceal changed sources or unrelated failures."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import historical_dependent_tests as historical


def _record(tmp_path, monkeypatch, source="value = 'before'\n"):
    tests = tmp_path / "tests"
    tests.mkdir()
    path = tests / "test_example.py"
    path.write_bytes(source.encode("utf-8"))
    records = tmp_path / "records.json"
    records.write_text(
        json.dumps(
            {
                "example==1.0": {
                    "reason": "reviewed test correction",
                    "files": [
                        {
                            "name": path.name,
                            "sha256": hashlib.sha256(source.encode()).hexdigest(),
                            "replacements": [["'before'", "'after'"]],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(historical, "_RECORDS", records)
    return tests, path


def test_only_the_recorded_version_is_corrected(tmp_path, monkeypatch):
    tests, path = _record(tmp_path, monkeypatch)
    assert historical.prepare_tests("example", "1.1", tests) == ()
    assert "before" in path.read_text()
    assert historical.prepare_tests("example", "1.0", tests) == ()
    assert path.read_text() == "value = 'after'\n"


def test_changed_historical_test_fails_before_editing(tmp_path, monkeypatch):
    tests, path = _record(tmp_path, monkeypatch)
    source = "value = 'before'\n# unexpected change\n"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        historical.prepare_tests("example", "1.0", tests)
    assert path.read_text() == source


def test_an_ambiguous_correction_fails_before_editing(tmp_path, monkeypatch):
    tests, path = _record(tmp_path, monkeypatch, "value = ('before', 'before')\n")
    with pytest.raises(ValueError, match="ambiguous"):
        historical.prepare_tests("example", "1.0", tests)
    assert path.read_text() == "value = ('before', 'before')\n"


@pytest.mark.parametrize(
    "behavior",
    ["retained", "released", "assertion", "error", "skipped", "late_assertion", "late_error"],
)
def test_acas_exception_accepts_only_the_named_retention_failure(tmp_path, behavior):
    record = json.loads(historical._RECORDS.read_text())["maf-sandbox-acas==0.31.0"]
    replacements = record["files"][0]["replacements"]
    decorator = replacements[0][1]
    assertion = textwrap.indent(textwrap.dedent(replacements[1][1]), "    ")
    setup = {
        "retained": "references = [lambda: object()]",
        "released": "references = [lambda: None]",
        "assertion": "assert False, 'different assertion'",
        "error": "raise RuntimeError('different failure')",
        "skipped": "pytest.skip('did not execute')",
        "late_assertion": "references = [lambda: object()]",
        "late_error": "references = [lambda: object()]",
    }[behavior]
    late = {
        "late_assertion": "    assert False, 'later assertion'\n",
        "late_error": "    raise RuntimeError('later error')\n",
    }.get(behavior, "")
    verdict = replacements[3][1].removeprefix("    asyncio.run(scenario())\n")
    (tmp_path / "test_acas_client_pool.py").write_text(
        "import pytest\nfrom types import SimpleNamespace\n"
        "sys = SimpleNamespace(version_info=(3, 14))\n"
        + decorator
        + "\ndef test_completed_capacity_waiters_release_context_before_capacity_changes(outcome):\n"
        + "    retention_failures = []\n"
        + f"    {setup}\n"
        + assertion
        + "\n"
        + late
        + verdict,
        encoding="utf-8",
    )
    report = tmp_path / "results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "--junitxml", str(report)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"},
    )
    error = historical.verify_expected_failures(report, tuple(record["expected_failures"]))
    if behavior == "retained":
        assert result.returncode == 0, result.stdout + result.stderr
        assert error is None
    elif behavior == "skipped":
        assert result.returncode == 0
        assert error is not None
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert error is not None


@pytest.mark.parametrize(
    "xml",
    [
        "<broken",
        "<testsuite/>",
        '<testsuite><testcase classname="wrong_file" name="expected"><skipped type="pytest.xfail"/></testcase></testsuite>',
        '<testsuite><testcase classname="correct" name="expected"><skipped type="pytest.xfail"/></testcase><testcase classname="correct" name="expected"><skipped type="pytest.xfail"/></testcase></testsuite>',
    ],
)
def test_missing_malformed_or_wrong_exception_evidence_fails(tmp_path, xml):
    report = tmp_path / "results.xml"
    report.write_text(xml, encoding="utf-8")
    assert historical.verify_expected_failures(report, ("correct::expected",)) is not None


def test_missing_report_fails(tmp_path):
    assert historical.verify_expected_failures(tmp_path / "missing.xml", ("expected",)) is not None


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_junit_dtd_is_rejected_before_entity_expansion(tmp_path, encoding):
    report = tmp_path / "results.xml"
    report.write_bytes(
        (
            f'<?xml version="1.0" encoding="{encoding}"?>'
            '<!DOCTYPE testsuite [<!ENTITY injected "pytest.xfail">]>'
            '<testsuite><testcase classname="correct" name="expected">'
            '<skipped type="&injected;"/></testcase></testsuite>'
        ).encode(encoding)
    )
    assert "must not contain a DTD" in (
        historical.verify_expected_failures(report, ("correct::expected",)) or ""
    )
