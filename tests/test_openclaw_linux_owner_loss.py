"""Keep Linux process ownership and actor-loss acceptance fail-closed."""

from __future__ import annotations

import copy
import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests/fixtures")]
profile = importlib.import_module("openclaw_conformance.linux_owner_loss")
actors = importlib.import_module("openclaw_conformance.linux_actor")
old = importlib.import_module("test_openclaw_owner_loss")


def observation(name):
    candidate = old.manifest()
    value = old.observation(name, candidate)
    value["independent_cleanup"]["removed"] = []
    final = profile.one(value["events"], "harness_cleanup")
    value["events"].append(
        {"event": "harness_leftovers", "mono_ns": final["mono_ns"] - 1, "resources": []}
    )
    value["events"].sort(key=lambda row: row["mono_ns"])
    if name != "control":
        fault = profile.one(value["events"], "actor_killed")
        fault["job_membership_verified"] = False
        fault["linux_identity"] = {
            "pid": 101,
            "pgrp": 100,
            "session": 100,
            "start": 77,
            "supervisor_pid": 100,
            "pidfd_bound": True,
            "signal": "SIGKILL",
        }
    return candidate, value


@pytest.mark.parametrize("name", profile.CASES)
def test_linux_cases_reuse_full_correlated_recovery_contract(name):
    candidate, value = observation(name)
    profile.verify_case(value, name, candidate)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("pidfd_bound", False),
        ("pgrp", 999),
        ("session", 999),
        ("pid", True),
        ("start", 0),
        ("signal", "SIGTERM"),
    ],
)
def test_actor_loss_requires_linux_identity(field, bad):
    candidate, value = observation("worker-active")
    profile.one(value["events"], "actor_killed")["linux_identity"][field] = bad
    with pytest.raises(ValueError, match="linux_fault_containment"):
        profile.verify_case(value, "worker-active", candidate)


@pytest.mark.parametrize("layer", ["independent", "harness"])
def test_emergency_cleanup_cannot_pass_recovery(layer):
    candidate, value = observation("worker-active")
    if layer == "independent":
        value["independent_cleanup"]["removed"] = ["orphan"]
    else:
        profile.one(value["events"], "harness_leftovers")["resources"] = ["orphan"]
    with pytest.raises(ValueError, match="emergency_cleanup_required|harness_removed_orphans"):
        profile.verify_case(value, "worker-active", candidate)


def test_linux_evidence_does_not_satisfy_windows_profile():
    candidate, value = observation("worker-active")
    with pytest.raises(ValueError, match="fault_containment"):
        old.fault.verify(value, "worker-active", candidate)


def test_linux_plan_rejects_candidate_upgrade():
    candidate = {
        "host": {"platform": "Linux"},
        "wheel_sha256": profile.WHEEL,
        "versions": dict(profile.VERSIONS),
    }
    assert len(profile.plan(candidate)["cases"]) == 13
    candidate["versions"]["mcp"] = "2.2.0"
    with pytest.raises(ValueError, match="worker_versions"):
        profile.plan(candidate)


def test_runtime_snapshot_includes_added_and_modified_assets(tmp_path):
    for name in ("worker", "openclaw"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "module.py").write_bytes(b"original")
    before = profile.runtime(tmp_path)
    (tmp_path / "worker/module.py").write_bytes(b"changed")
    assert profile.runtime(tmp_path) != before
    (tmp_path / "worker/module.py").write_bytes(b"original")
    (tmp_path / "openclaw/extra.js").write_bytes(b"extra")
    assert profile.runtime(tmp_path) != before


@pytest.mark.parametrize(
    "mutation", ["none", "plan", "missing_case", "runtime", "transplant", "failed_case"]
)
def test_collector_rejects_incomplete_or_transplanted_campaign(tmp_path, monkeypatch, mutation):
    candidate, _ = observation("control")
    candidate.update(
        host={"platform": "Linux"}, wheel_sha256=profile.WHEEL, versions=dict(profile.VERSIONS)
    )
    root = tmp_path / "campaign"
    root.mkdir()
    profile.write(root / "candidate.json", candidate)
    plan = profile.plan(candidate)
    if mutation == "plan":
        plan["cases"] = ["control"]
    profile.write(root / "plan.json", plan)
    summary = {"completed": list(profile.CASES), "runtime_unchanged": mutation != "runtime"}
    if mutation == "missing_case":
        summary["completed"].pop()
    profile.write(root / "summary.json", summary)
    for name in profile.CASES:
        _, value = observation(name)
        if mutation == "failed_case" and name == "worker-active":
            value["outcome"] = "fail"
        profile.write(root / (name + ".json"), value)
    selected = copy.deepcopy(candidate)
    if mutation == "transplant":
        selected["image"] = "sha256:" + "f" * 64
    monkeypatch.setattr(profile, "baseline", lambda _: selected)
    reporter = importlib.import_module("report_openclaw_hosted")
    monkeypatch.setattr(reporter, "command", lambda *a: "{}")
    monkeypatch.setattr(profile, "cleanup", lambda *a: {"confirmed": True, "removed": []})
    assert profile.collect(tmp_path, root)["qualified"] is (mutation == "none")


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux pidfds and procfs")
def test_pidfd_fault_targets_only_owned_actor():
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    ) as child:
        actor = actors.LinuxActor(SimpleNamespace(children=[(child, None)]), child.pid)
        try:
            assert actor.alive()
            assert actor.evidence["pgrp"] == actor.evidence["session"] == child.pid
            actor.kill()
            assert not actor.alive()
            assert child.wait(timeout=5) != 0
        finally:
            actor.close()
            if child.poll() is None:
                child.kill()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux pidfds and procfs")
def test_unrelated_actor_is_refused_without_signal():
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    ) as child:
        try:
            with pytest.raises(ValueError, match="actor_not_owned"):
                actors.LinuxActor(SimpleNamespace(children=[(child, None)]), os.getpid())
            assert child.poll() is None
        finally:
            child.kill()


def test_owner_loss_workflow_uses_fixed_installed_baseline_and_markdown_only():
    import yaml

    live = yaml.safe_load((ROOT / ".github/workflows/docker-live.yml").read_text())
    job = live["jobs"]["openclaw-owner-loss"]
    assert (
        job["if"]
        == "github.event_name == 'workflow_dispatch' && inputs.openclaw_bicep && inputs.openclaw_mode == 'owner-loss'"
    )
    assert job["runs-on"] == "ubuntu-24.04"
    assert "environment" not in job and "permissions" not in job
    steps = {s.get("name"): s for s in job["steps"]}
    assert profile.WHEEL in steps["Download verified published worker and prepared compiler"]["run"]
    assert "mcp==1.30.0" in steps["Run a fresh installed deterministic baseline"]["run"]
    run = steps["Qualify the fixed Linux actor-loss cases"]["run"]
    assert 'worker/bin/python" -I -B' in run and "linux_owner_loss" in run
    assert steps["Re-evaluate and retain bounded actor-loss evidence"]["if"] == "always()"
    upload = steps["Retain bounded Markdown evidence"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/openclaw-owner-loss.md"
    assert "openclaw-owner-loss" in live["jobs"]["report-failure"]["needs"]
