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
        {"stage": "authored", "sha256": "1" * 64},
        {
            "stage": "corrupted",
            "edge": "api_to_database",
            "target": "missing_database",
            "sha256": "3" * 64,
        },
        calls[0],
        {
            "stage": "validation",
            "call": "a" * 32,
            "sha256": "3" * 64,
            "delivered": 0,
            "diagnostic": "Result: refused\nError: Cell 'api_to_database'.target must reference a vertex",
        },
        {
            "stage": "rejected",
            "delivered": 0,
            "diagnostic": "Result: refused\nError: Cell 'api_to_database'.target must reference a vertex",
        },
        {
            "stage": "repair",
            "attempt": 1,
            "sha256": "2" * 64,
            "diagnostic": "Result: refused\nError: Cell 'api_to_database'.target must reference a vertex",
        },
        calls[1],
        {
            "stage": "validation",
            "call": "b" * 32,
            "sha256": "2" * 64,
            "diagnostic": "b" * 32 + "/diagram.drawio",
            "delivered": 1,
        },
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
        "configuration",
        "authored",
        "corrupted",
        "validation",
        "rejected",
        "repair",
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
    next(record for record in evidence if record["stage"] == "tool_call_ended")["seconds"] = seconds
    assert check.assess(transcript(evidence))


@pytest.mark.parametrize(
    "stage,field,value",
    [
        ("configuration", "allowed_hosts", ["example.com"]),
        ("tool_call_ended", "failure", "TimeoutError"),
        ("tool_call_ended", "call", "b" * 32),
        ("saved_and_read", "path", "other/diagram.drawio"),
        ("storage_cleanup", "failures", 1),
        ("sandbox_cleanup", "complete", False),
        ("corrupted", "target", "database"),
        ("validation", "diagnostic", "Result: refused\nError: a different error"),
        ("validation", "call", "b" * 32),
        ("validation", "sha256", "f" * 64),
        ("authored", "sha256", ""),
        ("repair", "attempt", 2),
    ],
)
def test_wrong_attribution_or_failed_cleanup_cannot_pass(evidence, stage, field, value):
    next(record for record in evidence if record["stage"] == stage)[field] = value
    assert check.assess(transcript(evidence))


def test_quoted_model_evidence_cannot_replace_host_records(evidence):
    assert check.assess(transcript(evidence).replace("  [measured]", "> [measured]"))


def test_later_cleanup_success_cannot_hide_an_earlier_failure(evidence):
    cleanup = next(record for record in evidence if record["stage"] == "storage_cleanup")
    evidence.insert(evidence.index(cleanup), {**cleanup, "failures": 1})
    assert check.assess(transcript(evidence))


def test_cleanup_before_readback_cannot_pass(evidence):
    cleanup = next(record for record in evidence if record["stage"] == "storage_cleanup")
    evidence.remove(cleanup)
    evidence.insert(0, cleanup)
    assert check.assess(transcript(evidence))


@pytest.mark.parametrize("attempts", [2, 3])
@pytest.mark.parametrize("tamper", ["attempt", "retry_result", "call", "xml", "prompt"])
def test_retries_require_each_failed_repair_diagnostic(evidence, attempts, tamper):
    repair = next(record for record in evidence if record["stage"] == "repair")
    call = evidence[evidence.index(repair) + 1]
    saved = next(record for record in evidence if record["stage"] == "saved_and_read")
    index = evidence.index(repair)
    for number in range(1, attempts):
        evidence[index:index] = [
            {**repair, "attempt": number},
            {**call, "call": str(number) * 32},
            {
                "stage": "validation",
                "call": str(number) * 32,
                "sha256": repair["sha256"],
                "diagnostic": f"Result: refused\nError: Invalid XML {number}",
                "delivered": 0,
            },
            {
                "stage": "repair_rejected",
                "attempt": number,
                "diagnostic": f"Result: refused\nError: Invalid XML {number}",
            },
        ]
        index += 4
    repair["attempt"] = saved["attempt"] = attempts
    validations = [record for record in evidence if record["stage"] == "validation"]
    repairs = [record for record in evidence if record["stage"] == "repair"]
    for index, record in enumerate(repairs):
        record["diagnostic"] = validations[index]["diagnostic"]
    assert check.assess(transcript(evidence)) == []
    rejected = next(record for record in evidence if record["stage"] == "repair_rejected")
    if tamper == "attempt":
        rejected["attempt"] = attempts
    elif tamper == "retry_result":
        rejected["diagnostic"] = "Result: refused\nError: a stale result"
    elif tamper == "call":
        validations[1]["call"] = "a" * 32
    elif tamper == "xml":
        validations[1]["sha256"] = "0" * 64
    else:
        repairs[1]["diagnostic"] = validations[0]["diagnostic"]
    assert check.assess(transcript(evidence))
