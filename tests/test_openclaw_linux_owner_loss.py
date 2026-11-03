"""Keep Linux process ownership and actor-loss acceptance fail-closed."""

from __future__ import annotations

import copy
import importlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests/fixtures")]
profile = importlib.import_module("openclaw_conformance.linux_owner_loss")
actors = importlib.import_module("openclaw_conformance.linux_actor")
docker_faults = importlib.import_module("openclaw_conformance.linux_docker_failures")
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
    if name in {"worker-unavailable", "worker-denied"}:
        for event, at in (("docker_fault_blocked", 850), ("docker_fault_restored", 950)):
            value["events"].append(
                {
                    "event": event,
                    "mono_ns": at,
                    "kind": name,
                    "resources": ["a" * 64],
                    "sentinel_survived": True,
                    "engine": "e" * 64,
                    "context_private": True,
                    "endpoint_reserved": True,
                    "probe_returncode": 1 if at == 850 else 0,
                    "context_same": True,
                }
            )
        value["events"].sort(key=lambda row: row["mono_ns"])
        if name == "worker-denied":
            denied = value["denial"]
            denied["image"] = docker_faults.DENIAL_IMAGE
            removal = {
                "event": "docker_removal",
                "mono_ns": 875,
                "returncode": 1,
                "container": "a" * 64,
                "target": "owned",
            }
            allowed = dict(removal, mono_ns=975, returncode=0)
            denied.update(
                removals=[removal],
                allowed_removals=[allowed],
                restored_decisions=[
                    *denied["decisions"],
                    dict(denied["decisions"][0], sequence=2, allowed=True, denied=False, nonce=""),
                ],
            )
            value["transport"].extend([removal, allowed])
            value["transport"].sort(key=lambda row: row["mono_ns"])
            value["daemon"] = {
                "registration_sha256": "d" * 64,
                "control_sha256": "e" * 64,
                "daemon_image": docker_faults.DENIAL_IMAGE,
                "daemon_version": "29.8.2",
                "daemon_storage_driver": "overlay2",
                "daemon_tls": True,
                "daemon_unchanged": True,
                "plugin_source_sha256": docker_faults.DENIAL_SOURCE,
                "plugin_binary_sha256": "b" * 64,
                "go_version": "go version go1.27.3 linux/amd64",
            }
    value["transport"][0]["mono_ns"] = 2
    value["transport"].extend(
        dict(r, event="startup_complete", mono_ns=r["mono_ns"] - 1)
        for r in list(value["transport"])
        if r["event"] == "startup_ready"
    )
    value["transport"].sort(key=lambda r: r["mono_ns"])
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
    assert len(profile.plan(candidate, CAMPAIGN)["cases"]) == 15
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


def test_baseline_reporter_participates_in_source_identity_and_dirty_check(tmp_path, monkeypatch):
    install = importlib.import_module("openclaw_conformance.install")
    runner = tmp_path / "scripts/openclaw_conformance/install.py"
    reporter = tmp_path / "scripts/report_openclaw_hosted.py"
    plugin = tmp_path / "tests/fixtures/openclaw_docker_authz/main.go"
    for path in (runner, reporter, plugin, tmp_path / "tests/test_mcp_workload_service.py"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
    monkeypatch.setattr(install, "__file__", str(runner))
    monkeypatch.setattr(install, "REPO", tmp_path)
    monkeypatch.setattr(install, "FIXTURES", tmp_path / "tests/fixtures")
    monkeypatch.setattr(install, "SAMPLE", tmp_path / "sample")
    before = install.runner_hash()
    reporter.write_text("changed acceptance", encoding="utf-8")
    assert install.runner_hash() != before
    before_plugin = install.runner_hash()
    plugin.write_text("changed authorization", encoding="utf-8")
    assert install.runner_hash() != before_plugin

    def dirty(command, **kwargs):
        assert "status" in command
        assert "scripts/report_openclaw_hosted.py" in command
        return b" M scripts/report_openclaw_hosted.py\n"

    monkeypatch.setattr(install.subprocess, "check_output", dirty)
    with pytest.raises(ValueError, match="uncommitted_runner"):
        install.runner_revision()


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
        "survivor",
        "collection_interrupt",
        "registration",
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
        if name == "worker-denied":
            (root / name).mkdir()
            for file, key in (
                ("daemon-resources.json", "registration_sha256"),
                ("daemon-control.json", "control_sha256"),
            ):
                profile.write(root / name / file, {"test": "registered"})
                value["daemon"][key] = profile.file_hash(root / name / file)
            if mutation == "registration":
                profile.write(root / name / "daemon-resources.json", {"test": "different scope"})
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

    def inventory(*args, **kwargs):
        assert kwargs == {"remove": False}
        return {"confirmed": True, "removed": [], "remaining": []}

    def failed_inventory(*args, **kwargs):
        assert kwargs == {"remove": False}
        if mutation == "collection_interrupt":
            raise KeyboardInterrupt
        return {"confirmed": False, "removed": [], "remaining": ["orphan"]}

    monkeypatch.setattr(profile, "cleanup", inventory)
    if mutation in {"survivor", "collection_interrupt"}:
        monkeypatch.setattr(profile, "cleanup", failed_inventory)
    if mutation == "collection_interrupt":
        with pytest.raises(KeyboardInterrupt):
            profile.collect(tmp_path, root)
        monkeypatch.setattr(profile, "cleanup", inventory)
    report = profile.collect(tmp_path, root)
    assert report["qualified"] is (mutation == "none")
    if mutation == "none":
        assert profile.collect(tmp_path, root)["qualified"] is True
    if mutation in {"source", "campaign", "case_hash"}:
        row = next(r for r in report["cases"] if r["case"] == "worker-active")
        assert row["rejection"] == (
            "case_artifact_digest" if mutation == "case_hash" else "case_campaign_binding"
        )
    if mutation == "registration":
        assert report["cases"][-1]["rejection"] == "daemon_registration_changed"
    if mutation in {"survivor", "collection_interrupt"}:
        monkeypatch.setattr(profile, "cleanup", inventory)
        repeated = profile.collect(tmp_path, root)
        assert repeated["qualified"] is False
        assert repeated["cases"][0]["rejection"] == "postflight_previous_failure"


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
    assert len(report["cases"]) == 15
    row = report["cases"][0]
    assert row["parent_failure"] == "fault_case_deadline"
    assert row["failure"] == "redacted"
    assert row["independent_cleanup"] is False
    assert row["rejection"] == "parent_supervisor_failure"
    assert all(r["rejection"] == "missing_or_oversized_record" for r in report["cases"][1:])


@pytest.mark.parametrize(
    "processes_stopped,cleanup_interrupted", [(True, False), (False, False), (True, True)]
)
def test_interrupted_case_stops_campaign_after_evidence_and_cleanup(
    tmp_path, monkeypatch, processes_stopped, cleanup_interrupted
):
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
            "failure": None if cleanup_interrupted else "KeyboardInterrupt",
            "events": [{"event": "harness_cleanup", "processes_stopped": processes_stopped}],
        }

    def cleanup(case_root, name):
        calls.append(("cleanup", name))
        if cleanup_interrupted:
            raise KeyboardInterrupt
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
    with pytest.raises(
        ValueError,
        match="interrupted"
        if processes_stopped and not cleanup_interrupted
        else "cleanup_unconfirmed",
    ):
        profile.run(prepared, root)
    assert calls == [
        ("case", "control"),
        *(([("cleanup", "control")]) if processes_stopped else []),
    ]
    summary = profile.read(root / "summary.json")
    assert summary["completed"] == ["control"]
    assert summary["runtime_unchanged"] is False
    saved = root / "control.json"
    assert summary["case_hashes"]["control"] == profile.file_hash(saved)
    value = profile.read(saved)
    if cleanup_interrupted:
        assert value["failure"] is None
        assert value["parent_failure"] == "KeyboardInterrupt"
        assert value["independent_cleanup"] == {"confirmed": False, "error": "KeyboardInterrupt"}
    else:
        assert value["failure"] == "KeyboardInterrupt"


def test_process_teardown_disarms_deadline_and_retries_failed_close(monkeypatch):
    calls = []
    handlers = {}
    child = SimpleNamespace(pid=1234, wait=lambda **kw: calls.append("wait"))

    def close():
        calls.append("close")
        handlers[actors.signal.SIGINT](actors.signal.SIGINT, None)
        return ["Refused"]

    def handler(signum, callback):
        handlers[signum] = callback
        return actors.signal.SIG_DFL

    proc = SimpleNamespace(
        children=[(child, None)],
        stopped=False,
        close=close,
        wait_empty=lambda *a: True,
    )
    monkeypatch.setattr(actors.sys, "platform", "linux")
    monkeypatch.setattr(actors.signal, "alarm", lambda n: calls.append(("alarm", n)), raising=False)
    monkeypatch.setattr(actors.signal, "signal", handler)
    monkeypatch.setattr(actors.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        actors.os, "killpg", lambda pid, sig: calls.append(("kill", pid)), raising=False
    )
    errors = actors.stop_processes([proc])
    assert calls == [("alarm", 0), "close", ("kill", 1234), "wait"]
    assert errors == ["KeyboardInterrupt", "Refused"]
    assert proc.stopped is True
    assert handlers[actors.signal.SIGINT] == actors.signal.SIG_DFL


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux process groups and alarms")
def test_pending_deadline_cannot_bypass_kill_of_term_resistant_actor(tmp_path, monkeypatch):
    agent = importlib.import_module("test_openclaw_e2e_sample").agent
    proc = agent.Processes(tmp_path)
    marker = tmp_path / "ready"
    code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(marker)!r}).touch(); time.sleep(60)"
    )
    original_wait = proc.wait_empty
    monkeypatch.setattr(
        proc, "wait_empty", lambda child, seconds: original_wait(child, 0.3 if seconds == 30 else 5)
    )

    def expired(*_args):
        raise profile.Refused("fault_case_deadline")

    previous = actors.signal.signal(actors.signal.SIGALRM, expired)
    try:
        proc.start("resistant", [sys.executable, "-c", code])
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        actors.signal.setitimer(actors.signal.ITIMER_REAL, 0.1)
        assert actors.stop_processes([proc]) == []
        assert proc.stopped and not proc.group_exists(proc.children[0][0])
        assert proc.children[0][0].returncode == -actors.signal.SIGKILL
    finally:
        actors.signal.alarm(0)
        actors.signal.signal(actors.signal.SIGALRM, previous)
        if not proc.stopped:
            actors.stop_processes([proc])


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
    assert (
        docker_faults.DENIAL_REFERENCE
        in steps["Download verified published worker and prepared compiler"]["run"]
    )
    go = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/setup-go@"))
    assert go["with"] == {"go-version": "1.27.3", "cache": False}


@pytest.mark.parametrize("name", ["worker-unavailable", "worker-denied"])
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_block",
        "reordered_restore",
        "orphan_changed",
        "sentinel_lost",
        "engine_changed",
        "ready_while_blocked",
        "emergency_cleanup",
    ],
)
def test_docker_failure_controls_cannot_be_omitted_or_transplanted(name, mutation):
    candidate, value = observation(name)
    if mutation == "missing_block":
        value["events"] = [r for r in value["events"] if r["event"] != "docker_fault_blocked"]
    elif mutation == "ready_while_blocked":
        value["transport"].append(
            {"event": "startup_ready", "mono_ns": 890, "boot": "f" * 32, "owner_empty": True}
        )
        value["transport"].sort(key=lambda r: r["mono_ns"])
    elif mutation == "emergency_cleanup":
        value["independent_cleanup"]["removed"] = ["daemon"]
    else:
        restored = profile.one(value["events"], "docker_fault_restored")
        field, bad = {
            "reordered_restore": ("mono_ns", 800),
            "orphan_changed": ("resources", []),
            "sentinel_lost": ("sentinel_survived", False),
            "engine_changed": ("engine", "f" * 64),
        }[mutation]
        restored[field] = bad
    with pytest.raises(ValueError):
        profile.verify_case(value, name, candidate, CAMPAIGN)


@pytest.mark.parametrize(
    "field,bad", [("context_private", False), ("endpoint_reserved", False), ("probe_returncode", 0)]
)
def test_connection_loss_requires_an_independently_failed_private_probe(field, bad):
    candidate, value = observation("worker-unavailable")
    profile.one(value["events"], "docker_fault_blocked")[field] = bad
    with pytest.raises(ValueError, match="connection_fault_controls"):
        profile.verify_case(value, "worker-unavailable", candidate, CAMPAIGN)


def test_connection_probe_uses_server_inventory_even_when_formatted_info_would_succeed(monkeypatch):
    environment = {"DOCKER_CONTEXT": "private", "DOCKER_CONFIG": "isolated"}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["env"] is environment and kwargs["timeout"] == 15
        return SimpleNamespace(returncode=1 if command == ["docker", "ps", "-aq"] else 0)

    monkeypatch.setattr(docker_faults.subprocess, "run", run)
    assert docker_faults.connection_probe(environment) == 1
    assert commands == [["docker", "ps", "-aq"]]


@pytest.mark.parametrize(
    "mutation",
    [
        "image",
        "plugin",
        "tls",
        "restarted",
        "missing_allow",
        "wrong_container",
        "different_command",
        "late_removal",
    ],
)
def test_denial_requires_exact_daemon_and_real_restored_removal(mutation):
    candidate, value = observation("worker-denied")
    denial = value["denial"]
    if mutation in {"image", "plugin", "tls", "restarted"}:
        field, bad = {
            "image": ("daemon_image", "sha256:" + "f" * 64),
            "plugin": ("plugin_source_sha256", "f" * 64),
            "tls": ("daemon_tls", False),
            "restarted": ("daemon_unchanged", False),
        }[mutation]
        value["daemon"][field] = bad
    elif mutation == "missing_allow":
        denial["restored_decisions"].pop()
    elif mutation == "wrong_container":
        denial["restored_decisions"][-1]["target"] = "other"
    elif mutation == "different_command":
        denial["allowed_removals"] = [dict(denial["allowed_removals"][0], target="other")]
    else:
        denial["allowed_removals"][0]["mono_ns"] = 1001
        value["transport"].sort(key=lambda r: r["mono_ns"])
    with pytest.raises((ValueError, RuntimeError)):
        profile.verify_case(value, "worker-denied", candidate, CAMPAIGN)


@pytest.mark.parametrize("survivor", ["none", "daemon", "volume", "parent_changed"])
def test_dedicated_postflight_is_read_only_and_checks_retained_volume_identity(
    tmp_path, monkeypatch, survivor
):
    host = importlib.import_module("openclaw_docker_denial_check")
    client, daemon = "a" * 32, "b" * 64
    profile.write(
        tmp_path / "daemon-control.json",
        {"client": client, "name": "qualification-authz-" + client, "host": "unix:///fixture.sock"},
    )
    profile.write(
        tmp_path / "daemon-resources.json",
        {"client": client, "daemon": daemon, "parent": "original", "volumes": ["owned-volume"]},
    )
    calls = []

    def outer(endpoint, *args):
        calls.append(args)
        if args[0] == "info":
            return "changed" if survivor == "parent_changed" else "original"
        if args[:2] == ("volume", "ls"):
            return "owned-volume" if survivor == "volume" else "unrelated-volume"
        assert args[0] == "ps"
        return daemon if survivor == "daemon" else ""

    monkeypatch.setattr(host, "outer_docker", outer)
    monkeypatch.setattr(
        host, "cleanup_daemon", lambda *a: pytest.fail("collector removed a survivor")
    )
    for _ in range(2):
        if survivor == "parent_changed":
            with pytest.raises(ValueError, match="parent_daemon_changed"):
                docker_faults.inventory(tmp_path, remove=False)
        else:
            result = docker_faults.inventory(tmp_path, remove=False)
            assert result["confirmed"] is (survivor == "none")
            assert result["removed"] == []
    assert all(args[0] in {"info", "ps", "volume"} for args in calls)


def test_fallback_does_not_report_a_surviving_orphan_volume_as_removed(tmp_path, monkeypatch):
    host = importlib.import_module("openclaw_docker_denial_check")
    client = "a" * 32
    profile.write(
        tmp_path / "daemon-control.json",
        {"client": client, "name": "qualification-authz-" + client, "host": "unix:///fixture.sock"},
    )
    profile.write(
        tmp_path / "daemon-resources.json",
        {"client": client, "daemon": "b" * 64, "parent": "original", "volumes": ["orphan-volume"]},
    )

    def outer(endpoint, *args):
        if args[0] == "info":
            return "original"
        return "orphan-volume" if args[0] == "volume" else ""

    monkeypatch.setattr(host, "outer_docker", outer)
    monkeypatch.setattr(host, "cleanup_daemon", lambda *a: None)
    result = docker_faults.inventory(tmp_path, remove=True)
    assert result["confirmed"] is False
    assert result["remaining"] == ["orphan-volume"]
    assert result["removed"] == []


@pytest.mark.parametrize(
    "failure", [RuntimeError("Dedicated daemon volume survived cleanup"), KeyboardInterrupt()]
)
def test_dedicated_daemon_failure_retains_observed_case(tmp_path, monkeypatch, failure):
    cases = importlib.import_module("openclaw_conformance.owner_loss_cases")
    host = importlib.import_module("openclaw_docker_denial_check")
    denial = importlib.import_module("openclaw_docker_denial")
    candidate, value = observation("worker-denied")
    root = tmp_path / "denied"

    def run(args, *, qualifier):
        assert args.classic_store is True
        assert args.daemon_image == docker_faults.DENIAL_REFERENCE
        root.mkdir()
        qualifier(
            SimpleNamespace(
                authz_outer_host="unix:///fixture.sock",
                authz_daemon="a" * 64,
                authz_client="b" * 32,
            )
        )
        raise failure

    monkeypatch.setattr(host, "run", run)
    monkeypatch.setattr(denial, "DockerRemovalDenial", lambda *a: object())
    monkeypatch.setattr(cases, "case", lambda *a: value)
    observed = docker_faults.denied_case(root, tmp_path, candidate)
    assert observed["events"] == value["events"]
    assert observed["outcome"] == "fail"
    assert observed["failure"] == (
        "KeyboardInterrupt" if isinstance(failure, KeyboardInterrupt) else "daemon_volume_cleanup"
    )
    assert profile.read(root / "result.json") == observed


@pytest.mark.parametrize("name", ["worker-unavailable", "worker-denied"])
def test_product_startup_attempt_cannot_be_hidden_by_observer_failure(tmp_path, monkeypatch, name):
    import asyncio

    observer = importlib.import_module("openclaw_http_observer")
    candidate, value = observation(name)
    observed = []

    class App:
        async def __call__(self, scope, receive, send):
            await send({"type": "lifespan.startup.complete"})

    class Observer(observer.ObserveHTTP):
        def record(self, **fields):
            observed.append({"mono_ns": 860, "boot": "f" * 32, **fields})

    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["docker", "ps"])

    monkeypatch.setattr(observer.subprocess, "check_output", unavailable)
    app = Observer(App(), tmp_path / "unused", owned_scope="owned")

    async def receive():
        return {}

    async def send(message):
        pytest.fail("The failed probe must not forward startup completion")

    with pytest.raises(subprocess.CalledProcessError):
        asyncio.run(app({"type": "lifespan"}, receive, send))
    value["transport"].extend(observed)
    value["transport"].sort(key=lambda r: r["mono_ns"])
    with pytest.raises(ValueError, match="blocked_startup"):
        profile.verify_case(value, name, candidate, CAMPAIGN)


@pytest.mark.parametrize("phase", ["removals", "allowed_removals"])
@pytest.mark.parametrize("field,bad", [("container", "b" * 64), ("target", "unrelated")])
def test_denial_rejects_jointly_changed_command_copies(phase, field, bad):
    candidate, value = observation("worker-denied")
    # The fixture shares each command object with the raw transport observation.
    command = value["denial"][phase][0]
    assert any(row is command for row in value["transport"])
    command[field] = bad
    with pytest.raises((ValueError, RuntimeError)):
        profile.verify_case(value, "worker-denied", candidate, CAMPAIGN)


@pytest.mark.parametrize("name", ["worker-unavailable", "worker-denied"])
@pytest.mark.parametrize("mutation", ["missing", "wrong_boot", "late"])
def test_docker_faults_require_positive_startup_observer_controls(name, mutation):
    candidate, value = observation(name)
    completed = [r for r in value["transport"] if r["event"] == "startup_complete"]
    if mutation == "missing":
        value["transport"].remove(completed[-1])
    elif mutation == "wrong_boot":
        completed[-1]["boot"] = "f" * 32
    else:
        completed[-1]["mono_ns"] = 1001
        value["transport"].sort(key=lambda r: r["mono_ns"])
    with pytest.raises(ValueError, match="startup_completion_observation"):
        profile.verify_case(value, name, candidate, CAMPAIGN)
