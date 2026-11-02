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
CAMPAIGN = "a" * 32


def observation(name):
    candidate = old.manifest()
    value = old.observation(name, candidate)
    value.update(campaign_id=CAMPAIGN, candidate_sha256=profile.digest(candidate))
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
            "pid": 1234,
            "pgrp": 100,
            "session": 100,
            "start": 77,
            "supervisor_pid": 100,
            "pidfd_bound": True,
            "signal": "SIGKILL",
        }
        role = name.split("-", 1)[0]
        value["worker"] = [{"event": "actor_started", "mono_ns": 10, "pid": 1234}]
        value["events"].append(
            {
                "event": "actor_bound",
                "mono_ns": 220 if role == "client" else 20,
                "actor": role,
                "pid": 1234,
                "linux_identity": dict(fault["linux_identity"]),
            }
        )
        value["events"].sort(key=lambda row: row["mono_ns"])
    return candidate, value


@pytest.mark.parametrize("name", profile.CASES)
def test_linux_cases_reuse_full_correlated_recovery_contract(name):
    candidate, value = observation(name)
    profile.verify_case(value, name, candidate, CAMPAIGN)


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
        profile.verify_case(value, "worker-active", candidate, CAMPAIGN)


@pytest.mark.parametrize("role", ["client", "worker", "gateway"])
@pytest.mark.parametrize("field", ["pid", "start"])
def test_killed_identity_must_match_bound_actor(role, field):
    name = role + "-active"
    candidate, value = observation(name)
    profile.one(value["events"], "actor_killed")["linux_identity"][field] += 1
    with pytest.raises(ValueError, match="linux_actor_identity"):
        profile.verify_case(value, name, candidate, CAMPAIGN)


@pytest.mark.parametrize("role", ["client", "worker"])
def test_killed_identity_must_match_process_startup(role):
    name = role + "-active"
    candidate, value = observation(name)
    profile.one(value[role], "actor_started")["pid"] += 1
    with pytest.raises(ValueError, match="linux_actor_startup"):
        profile.verify_case(value, name, candidate, CAMPAIGN)


@pytest.mark.parametrize("layer", ["independent", "harness"])
def test_emergency_cleanup_cannot_pass_recovery(layer):
    candidate, value = observation("worker-active")
    if layer == "independent":
        value["independent_cleanup"]["removed"] = ["orphan"]
    else:
        profile.one(value["events"], "harness_leftovers")["resources"] = ["orphan"]
    with pytest.raises(ValueError, match="emergency_cleanup_required|harness_removed_orphans"):
        profile.verify_case(value, "worker-active", candidate, CAMPAIGN)


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
    assert len(profile.plan(candidate, CAMPAIGN)["cases"]) == 13
    candidate["versions"]["mcp"] = "2.2.0"
    with pytest.raises(ValueError, match="worker_versions"):
        profile.plan(candidate, CAMPAIGN)


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
    "mutation",
    [
        "none",
        "plan",
        "missing_case",
        "runtime",
        "transplant",
        "failed_case",
        "campaign",
        "source",
        "case_hash",
        "summary",
    ],
)
def test_collector_rejects_incomplete_or_transplanted_campaign(tmp_path, monkeypatch, mutation):
    candidate, _ = observation("control")
    candidate.update(
        host={"platform": "Linux"}, wheel_sha256=profile.WHEEL, versions=dict(profile.VERSIONS)
    )
    root = tmp_path / "campaign"
    root.mkdir()
    profile.write(root / "candidate.json", candidate)
    plan = profile.plan(candidate, CAMPAIGN)
    if mutation == "plan":
        plan["cases"] = ["control"]
    profile.write(root / "plan.json", plan)
    summary = {
        "completed": list(profile.CASES),
        "runtime_unchanged": mutation != "runtime",
        "plan_sha256": profile.digest(plan),
        "case_hashes": {},
    }
    if mutation == "missing_case":
        summary["completed"].pop()
    if mutation == "summary":
        summary["plan_sha256"] = "f" * 64
    for name in profile.CASES:
        _, value = observation(name)
        value["candidate_sha256"] = profile.digest(candidate)
        if name == "worker-active":
            if mutation == "campaign":
                value["campaign_id"] = "b" * 32
            if mutation == "source":
                value["candidate_sha256"] = profile.digest(dict(candidate, source_commit="other"))
        if mutation == "failed_case" and name == "worker-active":
            value["outcome"] = "fail"
        path = root / (name + ".json")
        profile.write(path, value)
        summary["case_hashes"][name] = profile.file_hash(path)
        if mutation == "case_hash" and name == "worker-active":
            value["duration"] += 1
            profile.write(path, value)
    profile.write(root / "summary.json", summary)
    selected = copy.deepcopy(candidate)
    if mutation == "transplant":
        selected["image"] = "sha256:" + "f" * 64
    monkeypatch.setattr(profile, "baseline", lambda _: selected)
    reporter = importlib.import_module("report_openclaw_hosted")
    monkeypatch.setattr(reporter, "command", lambda *a: "{}")
    monkeypatch.setattr(profile, "cleanup", lambda *a: {"confirmed": True, "removed": []})
    report = profile.collect(tmp_path, root)
    assert report["qualified"] is (mutation == "none")
    if mutation in {"source", "campaign", "case_hash"}:
        row = next(r for r in report["cases"] if r["case"] == "worker-active")
        assert row["rejection"] == (
            "case_artifact_digest" if mutation == "case_hash" else "case_campaign_binding"
        )


def test_incomplete_campaign_retains_failure_and_cleanup_diagnostics(tmp_path, monkeypatch):
    candidate, value = observation("control")
    candidate.update(
        host={"platform": "Linux"}, wheel_sha256=profile.WHEEL, versions=dict(profile.VERSIONS)
    )
    expected = profile.plan(candidate, CAMPAIGN)
    value.update(candidate_sha256=profile.digest(candidate), parent_failure="fault_case_deadline")
    value["failure"] = "private log with token and path"
    value["independent_cleanup"]["confirmed"] = False
    profile.write(tmp_path / "candidate.json", candidate)
    profile.write(tmp_path / "plan.json", expected)
    profile.write(tmp_path / "control.json", value)
    profile.write(
        tmp_path / "summary.json",
        {
            "plan_sha256": profile.digest(expected),
            "completed": ["control"],
            "case_hashes": {"control": profile.file_hash(tmp_path / "control.json")},
            "runtime_unchanged": False,
        },
    )
    monkeypatch.setattr(profile, "baseline", lambda _: candidate)
    reporter = importlib.import_module("report_openclaw_hosted")
    monkeypatch.setattr(reporter, "command", lambda *a: "{}")
    report = profile.collect(tmp_path, tmp_path)
    assert report["qualified"] is False
    assert report["rejection"] == "case_inventory"
    assert len(report["cases"]) == 13
    row = report["cases"][0]
    assert row["parent_failure"] == "fault_case_deadline"
    assert row["failure"] == "redacted"
    assert row["independent_cleanup"] is False
    assert row["rejection"] == "parent_supervisor_failure"
    assert all(r["rejection"] == "missing_or_oversized_record" for r in report["cases"][1:])


def test_interrupted_case_stops_campaign_after_evidence_and_cleanup(tmp_path, monkeypatch):
    prepared = tmp_path / "prepared"
    root = tmp_path / "faults"
    candidate = {
        "host": {"platform": "Linux"},
        "wheel_sha256": profile.WHEEL,
        "versions": dict(profile.VERSIONS),
    }
    calls = []

    def interrupted_case(case_root, _prepared, _candidate, name):
        calls.append(("case", name))
        return {
            "case": name,
            "outcome": "fail",
            "failure": "KeyboardInterrupt",
            "events": [{"event": "harness_cleanup", "processes_stopped": True}],
        }

    def cleanup(case_root, name):
        calls.append(("cleanup", name))
        return {"confirmed": True, "remaining": [], "removed": []}

    cases = importlib.import_module("openclaw_conformance.owner_loss_cases")
    monkeypatch.setattr(cases, "case", interrupted_case)
    monkeypatch.setattr(profile, "cleanup", cleanup)
    monkeypatch.setattr(profile, "baseline", lambda _: candidate)
    monkeypatch.setattr(profile.sys, "platform", "linux")
    monkeypatch.setattr(profile.sys, "prefix", str(prepared / "worker"))
    monkeypatch.setattr(profile.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(profile.signal, "signal", lambda *a: None)
    monkeypatch.setattr(profile.signal, "alarm", lambda *a: None, raising=False)
    with pytest.raises(ValueError, match="interrupted"):
        profile.run(prepared, root)
    assert calls == [("case", "control"), ("cleanup", "control")]
    summary = profile.read(root / "summary.json")
    assert summary["completed"] == ["control"]
    assert summary["runtime_unchanged"] is False
    saved = root / "control.json"
    assert summary["case_hashes"]["control"] == profile.file_hash(saved)
    assert profile.read(saved)["failure"] == "KeyboardInterrupt"


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
