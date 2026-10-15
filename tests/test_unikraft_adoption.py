"""Keep the adoption decision separate from host-fixture and native qualification evidence."""

import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    adoption = importlib.import_module("scripts.experiments.unikraft_output.adoption")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


def test_incompatible_native_metadata_preserves_committed_checkpoint_and_replay(tmp_path):
    result = adoption.publication(
        tmp_path / "host",
        {
            "case": "byte_prefix",
            "retained_bytes": 1048575,
            "omitted_bytes": 3,
            "limit_bytes": 1048576,
        },
    )
    assert result["refusals"] == {
        "guest-control-text": "invalid native completion",
        "unicode-byte-prefix": "invalid stream metadata",
    }
    assert result["saved_result_replayed"]
    assert result["previous_checkpoint_preserved"]
    assert result["interrupted_retry_refused"]
    assert "synthetic" in result["scope"]


def test_changed_native_observation_cannot_reuse_expected_decision(tmp_path):
    with pytest.raises(ValueError, match="unexpected native"):
        adoption.publication(tmp_path / "host", {"case": "byte_prefix"})
    assert not (tmp_path / "host").exists()


def test_failed_assessment_retains_unqualified_report(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("native recovery failed")

    monkeypatch.setattr(adoption, "assess", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "adoption",
            "--source",
            str(tmp_path),
            "--output",
            str(tmp_path),
            "--root",
            str(tmp_path / "retained"),
        ],
    )
    with pytest.raises(RuntimeError, match="native recovery"):
        adoption.main()
    report = json.loads((tmp_path / "adoption-result.json").read_text())
    assert report["status"] == "unqualified"
    assert "adoption" not in report


def test_old_run_measurement_refused_before_build(tmp_path, monkeypatch):
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "source_commit": "old",
                "run_id": "old",
                "run_attempt": "1",
                "status": "measured",
                "upstream_commit": adoption.PIN,
            }
        )
    )
    with pytest.raises(ValueError, match="same-run"):
        adoption.assess(
            tmp_path,
            tmp_path,
            tmp_path / "retained",
            {
                "source_commit": "new",
                "run_id": "new",
                "run_attempt": "1",
            },
        )
    assert not (tmp_path / "retained").exists()


@pytest.mark.workflow
def test_native_assessment_runs_on_both_hosts_and_excludes_checkpoint_payloads():
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/unikraft-output.yml").read_text()
    )
    job = workflow["jobs"]["native"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-24.04", "windows-latest"}
    steps = job["steps"]
    step = next(
        s
        for s in steps
        if s.get("name") == "Assess retained checkpoint recovery and publication boundaries"
    )
    assert "--root retained-mxc" in step["run"]
    assert "continue-on-error" not in step
    upload = steps[-1]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "evidence/"
