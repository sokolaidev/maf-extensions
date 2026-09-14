"""Require converter timing, storage read-back and cleanup evidence in sample 18's CI log."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_live_drawio_sample", ROOT / "scripts/check_live_drawio_sample.py"
)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture
def evidence():
    calls = [
        {
            "stage": "tool_call_ended",
            "call": digit * 32,
            "tool": "create_drawio",
            "kind": "drawio",
            "seconds": 1.25,
            "failure": None,
            "unclean": 0,
        }
        for digit in ("a", "b")
    ]
    return [
        {
            "stage": "configuration",
            "backend": "acas",
            "guest_egress": "closed",
            "allowed_hosts": [],
        },
        calls[0],
        {
            "stage": "rejected",
            "delivered": 0,
            "diagnostic": "Cell 'api_to_database'.target must reference a vertex",
        },
        calls[1],
        {
            "stage": "saved_and_read",
            "path": "b" * 32 + "/diagram.drawio",
            "bytes": 400,
            "attempt": 1,
        },
        {"stage": "storage_cleanup", "attempted": 1, "failures": 0},
        {"stage": "sandbox_cleanup", "complete": True},
        {"stage": "complete"},
    ]


def transcript(evidence):
    return "\n".join("  [measured] " + json.dumps(record) for record in evidence)


def test_complete_log_passes_and_prints_every_call_duration(evidence, tmp_path, capsys):
    output = transcript(evidence)
    assert check.assess(output) == []
    path = tmp_path / "live.log"
    path.write_text(output, encoding="utf-8")
    assert check.main(["check", str(path)]) == 0
    assert capsys.readouterr().out.count("1.250s (including cleanup)") == 2


@pytest.mark.parametrize(
    "stage",
    [
        "rejected",
        "tool_call_ended",
        "saved_and_read",
        "storage_cleanup",
        "sandbox_cleanup",
        "complete",
    ],
)
def test_missing_stage_fails(evidence, stage):
    assert check.assess(transcript([record for record in evidence if record["stage"] != stage]))


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), True, "1.0"])
def test_invalid_duration_fails(evidence, seconds):
    evidence[1]["seconds"] = seconds
    assert check.assess(transcript(evidence))


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "allowed_hosts", ["example.com"]),
        (1, "failure", "TimeoutError"),
        (3, "call", "a" * 32),
        (4, "path", "other/diagram.drawio"),
        (5, "failures", 1),
        (6, "complete", False),
    ],
)
def test_wrong_attribution_or_failed_cleanup_cannot_pass(evidence, index, field, value):
    evidence[index][field] = value
    assert check.assess(transcript(evidence))


def test_quoted_model_evidence_cannot_replace_host_records(evidence):
    assert check.assess(transcript(evidence).replace("  [measured]", "> [measured]"))
