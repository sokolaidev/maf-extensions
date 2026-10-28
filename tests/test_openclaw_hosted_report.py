"""Hosted evidence excludes private state and refuses incomplete cleanup."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "report_openclaw_hosted",
    Path(__file__).resolve().parents[1] / "scripts/report_openclaw_hosted.py",
)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def sample():
    return {
        "passed": True,
        "owned_empty": True,
        "owner_lock_released": True,
        "processes_stopped": True,
        "mode": "smoke",
        "actual_model": False,
        "cleanup_errors": [],
        "turns": [
            {"calls": 1, "completed": True, "verdict": verdict, "cleanup": "confirmed"}
            for verdict in ("invalid", "valid")
        ],
        "artifact": {
            "versions": {"maf-sandbox-mcp": "0.2.0"},
            "modules": {"server.py": "a" * 64},
            "wheel_sha256": "b" * 64,
        },
    }


def test_only_allowlisted_evidence_is_published():
    value = sample()
    value["worker_token"] = "PRIVATE_SENTINEL"
    value["error"] = "PRIVATE_SENTINEL"
    value["artifact"]["private_path"] = "PRIVATE_SENTINEL"
    value["turns"][0]["source"] = "PRIVATE_SENTINEL"
    public, accepted = report.observations(value)
    assert accepted
    assert "PRIVATE_SENTINEL" not in json.dumps(public)


@pytest.mark.parametrize(
    "field", ["passed", "owned_empty", "owner_lock_released", "processes_stopped"]
)
def test_missing_cleanup_evidence_refuses(field):
    value = sample()
    value.pop(field)
    assert not report.observations(value)[1]


@pytest.mark.parametrize("change", ["model", "cleanup", "turn", "verdict", "wheel"])
def test_non_smoke_or_incomplete_results_refuse(change):
    value = copy.deepcopy(sample())
    if change == "model":
        value["actual_model"] = True
    elif change == "cleanup":
        value["cleanup_errors"] = ["PRIVATE_SENTINEL"]
    elif change == "turn":
        value["turns"].pop()
    elif change == "verdict":
        value["turns"][0]["verdict"] = "valid"
    else:
        value["artifact"].pop("wheel_sha256")
    public, accepted = report.observations(value)
    assert not accepted
    assert "PRIVATE_SENTINEL" not in json.dumps(public)


def test_independent_inventory_refuses_leftovers(tmp_path: Path, monkeypatch):
    (tmp_path / "report.json").write_text(json.dumps(sample()))
    (tmp_path / "tool-evidence.jsonl").write_text("private evidence")
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)
    commands = []

    def command(*args):
        commands.append(args)
        return "owned-container"

    monkeypatch.setattr(report, "command", command)
    public, accepted = report.collect(tmp_path)
    assert not accepted
    assert public["independent_owned_container_count"] == 1
    assert commands == [
        ("docker", "ps", "-aq", "--filter", "label=maf-sandbox.scope=openclaw-bicep-" + "a" * 32)
    ]


def model_sample():
    value = sample()
    value.update(mode="model", actual_model=True)
    value["turns"][1]["calls"] = 3
    value["provider"] = {
        "requests": 6,
        "forwarded_responses": 6,
        "error_count": 0,
        "upstream_failures": [],
        "endpoint_sha256": "c" * 64,
        "requested_model_sha256": "d" * 64,
        "response_model_sha256": ["e" * 64],
        "max_requests": 32,
        "max_completion_tokens": 4096,
        "reasoning_effort": "none",
    }
    return value


def test_setup_failure_preserves_bounded_diagnostics_without_owner(tmp_path: Path):
    value = {
        "passed": False,
        "phase": "worker-install",
        "error_type": "CalledProcessError",
        "setup_exit_code": 1,
        "setup_error_codes": ["No solution found"],
        "error": "PRIVATE_SENTINEL",
    }
    (tmp_path / "report.json").write_text(json.dumps(value))
    public, accepted = report.collect(tmp_path, "model")
    assert not accepted
    assert public["phase"] == "worker-install"
    assert public["setup_exit_code"] == 1
    assert public["collection_error_type"] == "FileNotFoundError"
    assert "PRIVATE_SENTINEL" not in json.dumps(public)


def test_model_profile_allows_verified_repairs_without_weakening_smoke():
    value = model_sample()
    value["provider"]["key"] = "PRIVATE_SENTINEL"
    value["provider"]["endpoint"] = "PRIVATE_SENTINEL"
    public, accepted = report.observations(value, "model")
    assert accepted and public["turns"][1]["calls"] == 3
    assert "PRIVATE_SENTINEL" not in json.dumps(public)
    assert not report.observations(value)[1]
    assert not report.observations(sample(), "model")[1]
    value = sample()
    value["turns"][1]["calls"] = 3
    assert not report.observations(value)[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("requests", 0),
        ("requests", 33),
        ("requests", True),
        ("forwarded_responses", 5),
        ("error_count", 1),
        ("upstream_failures", [{"status": 400}]),
        ("max_requests", 64),
        ("max_completion_tokens", 8192),
        ("reasoning_effort", "provider-default"),
        ("response_model_sha256", []),
        ("requested_model_sha256", None),
        ("endpoint_sha256", None),
    ],
)
def test_model_profile_requires_complete_bounded_forwarding(field, value):
    result = model_sample()
    result["provider"][field] = value
    assert not report.observations(result, "model")[1]


@pytest.mark.parametrize(
    "field,value",
    [
        ("calls", 0),
        ("calls", True),
        ("calls", -1),
        ("calls", 33),
        ("completed", False),
        ("cleanup", "unknown"),
        ("verdict", "invalid"),
    ],
)
def test_model_profile_refuses_incomplete_turns(field, value):
    result = model_sample()
    result["turns"][1][field] = value
    assert not report.observations(result, "model")[1]
