"""A native success or guest marker alone cannot qualify the output experiment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "scripts/experiments/mxc_session_patch"
SPEC = importlib.util.spec_from_file_location("mxc_descriptor_probe", ROOT / "descriptor_probe.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.fixture
def evidence(tmp_path):
    results = json.loads((ROOT / "windows-descriptor-result.json").read_text())["results"]
    for name, data in {
        "bypass.stdout": b"MXC_BYPASS\r\n",
        "file_limit.stdout": b"Unsupported resource 1",
        "timeout.stderr": b"native state probe failed: TimedOut\n",
        "file_restore.stdout": b"MXC_RESTORED\r\n",
        "pipe_restore.stdout": b"MXC_RESTORED\r\n",
    }.items():
        (tmp_path / name).write_bytes(data)
    return results, tmp_path


def test_observed_positive_controls_still_conclude_no_host_enforcement(evidence):
    results, state = evidence
    assessment = probe.validate(results, state)
    assert assessment["cooperative_pipe_fidelity"]
    assert not assessment["satisfies_host_enforced_output_contract"]


@pytest.mark.parametrize("case", ["fidelity", "file_restore", "pipe_restore"])
def test_guest_measurements_cannot_replace_native_completion(evidence, case):
    results, state = evidence
    results[case]["control"] = None
    with pytest.raises(RuntimeError, match="native success missing"):
        probe.validate(results, state)


def test_supervisor_kill_does_not_count_as_native_timeout(evidence):
    results, state = evidence
    results["timeout"]["supervisor_killed"] = True
    with pytest.raises(RuntimeError, match="supervisor fallback"):
        probe.validate(results, state)


def test_successful_guest_with_corrupt_output_cannot_pass_fidelity(evidence):
    results, state = evidence
    results["fidelity"]["guest_observation"][0]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="byte fidelity"):
        probe.validate(results, state)


def test_missing_bypass_evidence_does_not_prove_the_counterexample(evidence):
    results, state = evidence
    (state / "bypass.stdout").write_bytes(b"")
    with pytest.raises(RuntimeError, match="bypass counterexample"):
        probe.validate(results, state)


def test_missing_case_is_inconclusive(evidence):
    results, state = evidence
    del results["file_limit"]
    with pytest.raises(RuntimeError, match="incomplete"):
        probe.validate(results, state)
