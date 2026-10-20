"""Rejection controls for the bounded owner-loss evidence profile."""

from __future__ import annotations

import copy
import importlib
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests/fixtures")]
fault = importlib.import_module("openclaw_conformance.owner_loss_evidence")
e = importlib.import_module("openclaw_conformance.evidence")


def manifest():
    path = (
        ROOT
        / "scripts/experiments/openclaw_conformance_evidence/2026-10-17-windows-v4/candidate.json"
    )
    if not path.exists():
        path = path.with_name("expected-candidate.json")
    return e.read(path)


def result(m):
    sources = importlib.import_module("openclaw_sources")

    return {
        "completed": True,
        "verdict": "valid",
        "status": "ok",
        "cleanup": "confirmed",
        "image": m["image"],
        "config_sha256": m["config_sha256"],
        "source_sha256": sources.source_digest(sources.SIMPLE_SOURCE),
        "diagnostics": "",
    }


def mirrored(value):
    return {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def observation(case, m):
    """Construct contract data; these synthetic records are not live qualification."""
    sources = importlib.import_module("openclaw_sources")

    old_boot, new_boot = "1" * 32, "2" * 32
    identity = {"boot": old_boot, "session": "3" * 64, "request": "4" * 64}
    value = {
        "case": case,
        "outcome": "pass",
        "failure": None,
        "parent_failure": None,
        "image": m["image"],
        "config_sha256": m["config_sha256"],
        "duration": 100,
        "events": [],
        "transport": [
            {"event": "startup_ready", "mono_ns": 1, "boot": old_boot, "owner_empty": True},
            {
                "event": "binding_started",
                "mono_ns": 50,
                "boot": old_boot,
                "session": "5" * 64,
                "request": "6" * 64,
            },
        ],
        "independent_cleanup": {"confirmed": True, "remaining": []},
    }
    rows = value["events"]
    transport = value["transport"]

    def add(at, event, **fields):
        rows.append({"event": event, "mono_ns": at, **fields})

    def wire(at, event, **fields):
        transport.append({"event": event, "mono_ns": at, **fields})

    add(100, "control_result", result=result(m))
    add(200, "lock_contender", refused=True)
    if case == "control":
        add(700, "client_settled", status=200)
    else:
        actor, phase = case.split("-", 1)
        phase = "active" if phase in {"unavailable", "denied"} else phase
        resources = ["a" * 64] if phase in {"active", "cleanup"} else []
        wire(250, "target_request", **identity)
        add(300, "target_identity", request_ns=250, turn="c" * 32, **identity)
        value["client"] = [
            {"event": "actor_started", "mono_ns": 210, "pid": 1234},
            {"event": "request_sent", "mono_ns": 230, "turn": "c" * 32},
        ]
        if actor != "client":
            value["client"].append(
                {
                    "event": "client_outcome",
                    "mono_ns": 650,
                    "status": None,
                    "body_sha256": None,
                    "error": "ConnectionResetError",
                }
            )
        value["provider"] = [
            {
                "turn": "c" * 32,
                "session": "d" * 32,
                "scenario": "cancel" if phase == "active" else "valid",
                "advertised_tools": ["tool_call"],
            }
        ]
        count = 0 if actor == "worker" and phase == "pre_admission" else 1
        if phase != "pre_admission":
            wire(255, "request", method="tools/call", exchange="7" * 32, **identity)
        if count and phase != "pre_admission":
            wire(260, "binding_started", **identity)
        if phase != "active":
            fields = {"container": resources[0]} if phase == "cleanup" else {}
            if phase == "response":
                fields["result"] = result(m)
            wire(350, "barrier", phase=phase, **identity, **fields)
        add(
            400,
            "barrier_observed",
            phase=phase,
            target_started_ns=201,
            resources=resources,
            compiler_running=True,
            paused=True,
            result=result(m),
        )
        add(
            500,
            "actor_killed",
            actor=actor,
            exited=True,
            others_alive=True,
            job_membership_verified=True,
        )
        if count and phase == "pre_admission":
            wire(540, "request", method="tools/call", exchange="7" * 32, **identity)
            wire(550, "binding_started", **identity)
        settlement = {}
        if actor != "worker":
            wire(
                570,
                "settled",
                exchange="7" * 32,
                method="POST",
                active=False,
                poisoned=False,
                **identity,
            )
            wire(580, "registry_snapshot", boot=old_boot, active=False, poisoned=False)
            settlement = {"exchange": "7" * 32, "terminal_ns": 570, "snapshot_ns": 580}
        add(
            600,
            "service_observed",
            state="dead_readiness_unconfirmed" if actor == "worker" else "settled",
            resources=resources if actor == "worker" else [],
            sentinel_survived=True,
            **settlement,
        )
        add(
            700,
            "client_settled",
            exit_code=86 if actor == "client" else 1,
            outcome="killed" if actor == "client" else "transport_error",
            status=None,
            error=None if actor == "client" else "ConnectionResetError",
        )
        add(800, "target_dispatches", count=count)
        if actor == "worker":
            if case.endswith(("unavailable", "denied")):
                add(900, "recovery_refused", exit_code=1, ready=False, resources=resources)
            wire(1000, "startup_ready", boot=new_boot, owner_empty=True)
            wire(1050, "binding_started", boot=new_boot, session="8" * 64, request="4" * 64)
            fresh = result(m)
            fresh["source_sha256"] = sources.source_digest(sources.LARGE_SOURCE)
            wire(
                1100,
                "binding_result",
                boot=new_boot,
                session="8" * 64,
                request="4" * 64,
                result=mirrored(copy.deepcopy(fresh)),
            )
            add(
                1200,
                "replacement_ready",
                resources=[],
                owner_unchanged=True,
                boot_changed=True,
                boot=new_boot,
                ready_ns=1000,
                old_token_status=401,
                old_session_status=404,
                stale_cancel_status=404,
                stale_delete_status=404,
                fresh_call_survived=True,
                fresh_result=fresh,
            )
        wire(
            1300,
            "binding_started",
            boot=new_boot if actor == "worker" else old_boot,
            session="9" * 64,
            request="4" * 64,
        )
        wire(
            1350,
            "binding_result",
            boot=new_boot if actor == "worker" else old_boot,
            session="9" * 64,
            request="4" * 64,
            result=mirrored(result(m)),
        )
        add(1400, "sibling_result", result=result(m))
        if case.endswith("denied"):
            value["denial"] = {
                "daemon_removed": True,
                "parent_unchanged": True,
                "image": fault.DENIAL_IMAGE,
                "container": resources[0],
                "target": "owned",
                "client": "b" * 32,
                "nonce": "c" * 32,
                "removals": [{"returncode": 1}],
                "decisions": [
                    {
                        "event": "docker_authz",
                        "sequence": 1,
                        "method": "DELETE",
                        "allowed": False,
                        "denied": True,
                        "client": "b" * 32,
                        "nonce": "c" * 32,
                        "target": "owned",
                    }
                ],
            }
    add(
        1600,
        "harness_cleanup",
        processes_stopped=True,
        remaining=[],
        errors=[],
        sentinel_survived=True,
        sentinel_removed=True,
    )
    return value


@pytest.mark.parametrize("case", fault.CASES)
def test_all_selected_case_contracts(case):
    m = manifest()
    fault.verify(observation(case, m), case, m)


@pytest.mark.parametrize(
    "event,field,bad",
    [
        ("actor_killed", "actor", "client"),
        ("actor_killed", "exited", False),
        ("actor_killed", "others_alive", False),
        ("actor_killed", "job_membership_verified", 1),
        ("barrier_observed", "phase", "response"),
        ("barrier_observed", "compiler_running", False),
        ("barrier_observed", "paused", False),
        ("target_dispatches", "count", 2),
        ("target_dispatches", "count", True),
        ("service_observed", "state", "ready"),
        ("service_observed", "resources", []),
        ("service_observed", "sentinel_survived", False),
        ("client_settled", "outcome", "killed"),
        ("client_settled", "exit_code", None),
        ("replacement_ready", "owner_unchanged", False),
        ("replacement_ready", "boot_changed", False),
        ("replacement_ready", "resources", ["leftover"]),
        ("replacement_ready", "old_token_status", 200),
        ("replacement_ready", "old_session_status", 200),
        ("replacement_ready", "stale_cancel_status", 202),
        ("replacement_ready", "stale_delete_status", 200),
        ("replacement_ready", "fresh_call_survived", False),
        ("harness_cleanup", "processes_stopped", False),
        ("harness_cleanup", "remaining", ["orphan"]),
        ("harness_cleanup", "errors", ["denied"]),
        ("harness_cleanup", "sentinel_survived", False),
        ("harness_cleanup", "sentinel_removed", False),
    ],
)
def test_unsubstantiated_fault_claims_refuse(event, field, bad):
    m = manifest()
    value = observation("worker-active", m)
    fault.one(value["events"], event)[field] = bad
    with pytest.raises(e.Refused):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "event",
    [
        "actor_killed",
        "barrier_observed",
        "target_dispatches",
        "service_observed",
        "client_settled",
        "replacement_ready",
        "sibling_result",
        "harness_cleanup",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered"])
def test_missing_duplicate_and_reordered_events_refuse(event, mutation):
    m = manifest()
    value = observation("worker-active", m)
    selected = fault.one(value["events"], event)
    if mutation == "missing":
        value["events"].remove(selected)
    elif mutation == "duplicate":
        value["events"].append(copy.deepcopy(selected))
    else:
        selected["mono_ns"] = 0
    with pytest.raises(e.Refused):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("ready", True),
        ("exit_code", 0),
        ("exit_code", True),
        ("exit_code", False),
        ("resources", []),
    ],
)
@pytest.mark.parametrize("case", ["worker-unavailable", "worker-denied"])
def test_failed_recovery_cannot_establish_readiness(case, field, bad):
    m = manifest()
    value = observation(case, m)
    fault.one(value["events"], "recovery_refused")[field] = bad
    with pytest.raises(e.Refused):
        fault.verify(value, case, m)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("target", "foreign"),
        ("nonce", "d" * 32),
        ("client", "d" * 32),
        ("denied", False),
        ("allowed", True),
        ("sequence", False),
    ],
)
def test_daemon_denial_requires_matching_real_decision(field, bad):
    m = manifest()
    value = observation("worker-denied", m)
    value["denial"]["decisions"][0][field] = bad
    with pytest.raises((e.Refused, RuntimeError)):
        fault.verify(value, "worker-denied", m)


def bundle(root):
    m = manifest()
    baseline = {
        "result": {"outcome": "qualified_for_profile"},
        "plan_sha256": "a" * 64,
        "summary_sha256": "b" * 64,
    }
    expected = fault.plan(m, uuid.uuid4().hex, baseline)
    summary = {
        "schema": 1,
        "campaign_id": expected["campaign_id"],
        "candidate_sha256": e.digest(m),
        "plan_sha256": e.digest(expected),
        "outcome": "qualified_for_profile",
        "attempts": [],
        "installation_checks": {"before": e.digest(m), "after": e.digest(m)},
    }
    for case in fault.CASES:
        aid = uuid.uuid4().hex
        value = {
            "campaign_id": expected["campaign_id"],
            "candidate_sha256": e.digest(m),
            "attempt_id": aid,
            "observations": observation(case, m),
        }
        path = root / (case + ".json")
        e.write(path, value)
        summary["attempts"].append(
            {
                "case": case,
                "attempt_id": aid,
                "sha256": e.file_hash(path),
                "bytes": path.stat().st_size,
                "outcome": "pass",
            }
        )
    e.write(root / "summary.json", summary)
    return expected, m, baseline


def test_complete_bundle_binds_independent_plan(tmp_path):
    expected, m, baseline = bundle(tmp_path)
    assert fault.evaluate(tmp_path, expected, m, baseline)["cases"] == 15


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_case",
        "narrow_plan",
        "deadline",
        "candidate",
        "baseline",
        "failed",
        "duplicate_attempt",
        "extra_file",
        "changed_bytes",
    ],
)
def test_bundle_cannot_select_its_own_acceptance(tmp_path, mutation):
    expected, m, baseline = bundle(tmp_path)
    summary_path = tmp_path / "summary.json"
    summary = e.read(summary_path)
    if mutation == "missing_case":
        (tmp_path / "worker-denied.json").unlink()
    elif mutation == "narrow_plan":
        expected["cases"].remove("worker-denied")
    elif mutation == "deadline":
        expected["deadlines"] = {**expected["deadlines"], "case": 99999}
    elif mutation == "candidate":
        m["runner_commit"] = "f" * 40
    elif mutation == "baseline":
        baseline["plan_sha256"] = "f" * 64
    elif mutation == "failed":
        summary["attempts"][0]["outcome"] = "fail"
    elif mutation == "duplicate_attempt":
        summary["attempts"][1]["attempt_id"] = summary["attempts"][0]["attempt_id"]
    elif mutation == "extra_file":
        (tmp_path / "extra.json").write_text("{}")
    else:
        (tmp_path / "worker-active.json").write_text(json.dumps({"outcome": "pass"}))
    e.write(summary_path, summary)
    with pytest.raises(e.Refused):
        fault.evaluate(tmp_path, expected, m, baseline)


@pytest.mark.parametrize("case", ["worker-active", "gateway-active", "client-pre_admission"])
def test_late_dispatch_cannot_hide_after_count_snapshot(case):
    m = manifest()
    value = observation(case, m)
    target = fault.one(value["transport"], "target_request")
    value["transport"].append(
        {
            "event": "binding_started",
            "mono_ns": 1500,
            **{key: target[key] for key in ("boot", "session", "request")},
        }
    )
    with pytest.raises(e.Refused, match="late_or_missing_dispatch"):
        fault.verify(value, case, m)


@pytest.mark.parametrize("mutation", ["missing", "wrong_boot", "nonempty"])
def test_startup_requires_matching_recovered_owner(mutation):
    m = manifest()
    value = observation("worker-active", m)
    startup = value["transport"][0]
    if mutation == "missing":
        value["transport"].remove(startup)
    elif mutation == "wrong_boot":
        startup["boot"] = "f" * 32
    else:
        startup["owner_empty"] = False
    with pytest.raises(e.Refused):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize("field", ["image", "config_sha256", "source_sha256"])
def test_completed_response_cannot_use_another_candidate(field):
    m = manifest()
    value = observation("worker-response", m)
    bad = "sha256:" + "f" * 64 if field == "image" else "f" * 64
    fault.one(value["events"], "barrier_observed")["result"][field] = bad
    fault.one(value["transport"], "barrier")["result"][field] = bad
    with pytest.raises(e.Refused, match="response_candidate"):
        fault.verify(value, "worker-response", m)


@pytest.mark.parametrize("field", ["boot", "session", "request", "phase"])
def test_worker_barrier_must_belong_to_the_target(field):
    m = manifest()
    value = observation("worker-cleanup", m)
    fault.one(value["transport"], "barrier")[field] = "foreign"
    with pytest.raises(e.Refused, match="barrier_correlation"):
        fault.verify(value, "worker-cleanup", m)


@pytest.mark.parametrize(
    "mutation", ["missing_terminal", "wrong_exchange", "active", "poisoned", "early_snapshot"]
)
@pytest.mark.parametrize("case", ["client-pre_admission", "gateway-active"])
def test_empty_owner_is_not_a_settled_target(case, mutation):
    m = manifest()
    value = observation(case, m)
    terminal = fault.one(value["transport"], "settled")
    snapshot = fault.one(value["transport"], "registry_snapshot")
    if mutation == "missing_terminal":
        value["transport"].remove(terminal)
    elif mutation == "wrong_exchange":
        terminal["exchange"] = "foreign"
    elif mutation == "early_snapshot":
        snapshot["mono_ns"] = terminal["mono_ns"] - 1
        fault.one(value["events"], "service_observed")["snapshot_ns"] = snapshot["mono_ns"]
        value["transport"].sort(key=lambda row: row["mono_ns"])
    else:
        snapshot[mutation] = True
    with pytest.raises(e.Refused, match="service_terminal"):
        fault.verify(value, case, m)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("cleanup", "unconfirmed"),
        ("status", "cancelled"),
        ("image", "sha256:" + "f" * 64),
        ("config_sha256", "f" * 64),
        ("source_sha256", "f" * 64),
    ],
)
def test_fresh_generation_requires_its_actual_result(field, bad):
    m = manifest()
    value = observation("worker-active", m)
    fault.one(value["events"], "replacement_ready")["fresh_result"][field] = bad
    with pytest.raises(e.Refused, match="fresh_generation_result|workload_result"):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "mutation", ["missing", "wrong_request", "error", "mismatched_mirror", "wrong_result"]
)
def test_pending_fresh_call_does_not_prove_success(mutation):
    m = manifest()
    value = observation("worker-active", m)
    ending = next(row for row in value["transport"] if row["event"] == "binding_result")
    if mutation == "missing":
        value["transport"].remove(ending)
    elif mutation == "wrong_request":
        ending["request"] = "foreign"
    elif mutation == "error":
        ending["result"]["isError"] = True
    elif mutation == "mismatched_mirror":
        ending["result"]["content"][0]["text"] = "{}"
    else:
        different = copy.deepcopy(ending["result"]["structuredContent"])
        different["source_sha256"] = "f" * 64
        ending["result"] = mirrored(different)
    with pytest.raises((e.Refused, RuntimeError)):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "failure", ["fault_case_exit", "fault_case_deadline", "case_supervisor_cleanup"]
)
def test_child_pass_cannot_hide_parent_failure(failure):
    m = manifest()
    value = observation("worker-active", m)
    value["parent_failure"] = failure
    with pytest.raises(e.Refused, match="parent_supervisor_failure"):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize("phase", ["pre_admission", "response", "cleanup"])
@pytest.mark.parametrize("event", ["barrier_released", "barrier_ended", "settled"])
def test_phase_must_remain_held_until_actor_loss(phase, event):
    m = manifest()
    value = observation("worker-" + phase, m)
    identity = fault.one(value["transport"], "target_request")
    if event == "settled" and phase == "pre_admission":
        value["transport"].append(
            {
                "event": "request",
                "method": "tools/call",
                "exchange": "7" * 32,
                "mono_ns": 360,
                **{key: identity[key] for key in ("boot", "session", "request")},
            }
        )
    value["transport"].append(
        {
            "event": event,
            "exchange": "7" * 32,
            "mono_ns": 450,
            "phase": phase,
            **{key: identity[key] for key in ("boot", "session", "request")},
        }
    )
    value["transport"].sort(key=lambda row: row["mono_ns"])
    with pytest.raises(e.Refused, match="barrier_not_held_at_fault"):
        fault.verify(value, "worker-" + phase, m)


@pytest.mark.parametrize("mutation", ["old_boot", "old_session", "before_ready"])
def test_replacement_controls_require_fresh_ready_authority(mutation):
    m = manifest()
    value = observation("worker-active", m)
    target = fault.one(value["transport"], "target_request")
    selected = [row for row in value["transport"] if row["mono_ns"] in (1050, 1100)]
    if mutation == "before_ready":
        selected[0]["mono_ns"] = 950
        selected[1]["mono_ns"] = 975
        value["transport"].sort(key=lambda row: row["mono_ns"])
    else:
        key = "boot" if mutation == "old_boot" else "session"
        for row in selected:
            row[key] = target[key]
    with pytest.raises(e.Refused, match="replacement_control_identity"):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "missing_outcome",
        "duplicate_outcome",
        "wrong_turn",
        "wrong_status",
        "wrong_error",
        "early_outcome",
        "zero_error_exit",
    ],
)
def test_client_summary_requires_its_recorded_terminal_outcome(mutation):
    m = manifest()
    value = observation("worker-active", m)
    client = value["client"]
    if mutation == "empty":
        client.clear()
    elif mutation == "missing_outcome":
        client.pop()
    elif mutation == "duplicate_outcome":
        client.append({**client[-1], "mono_ns": 660})
    elif mutation == "wrong_turn":
        client[1]["turn"] = "f" * 32
    elif mutation == "early_outcome":
        client[-1]["mono_ns"] = 390
    elif mutation == "zero_error_exit":
        fault.one(value["events"], "client_settled")["exit_code"] = 0
    else:
        client[-1]["status" if mutation == "wrong_status" else "error"] = (
            200 if mutation == "wrong_status" else "OtherError"
        )
    with pytest.raises(e.Refused):
        fault.verify(value, "worker-active", m)


@pytest.mark.parametrize(
    "mutation",
    ["empty", "wrong_turn", "wrong_scenario", "result_first", "repeated_dispatch", "wrong_session"],
)
def test_provider_turn_must_correlate_with_target_caller(mutation):
    m = manifest()
    value = observation("worker-active", m)
    provider = value["provider"]
    if mutation == "empty":
        provider.clear()
    elif mutation == "wrong_turn":
        provider[0]["turn"] = "f" * 32
    elif mutation == "wrong_scenario":
        provider[0]["scenario"] = "valid"
    elif mutation == "result_first":
        provider[0]["tool_result"] = "{}"
    elif mutation == "repeated_dispatch":
        provider.append(copy.deepcopy(provider[0]))
    else:
        provider.append({**provider[0], "tool_result": "{}", "session": "f" * 32})
    with pytest.raises(e.Refused, match="provider_target_turn"):
        fault.verify(value, "worker-active", m)


def test_killed_client_cannot_claim_a_delivered_outcome():
    m = manifest()
    value = observation("client-active", m)
    value["client"].append(
        {
            "event": "client_outcome",
            "mono_ns": 650,
            "status": 200,
            "body_sha256": "a" * 64,
            "error": None,
        }
    )
    with pytest.raises(e.Refused, match="killed_client_outcome"):
        fault.verify(value, "client-active", m)


def test_correlated_response_and_provider_result_are_allowed():
    m = manifest()
    value = observation("gateway-response", m)
    value["client"][-1].update(status=200, body_sha256="a" * 64, error=None)
    fault.one(value["events"], "client_settled").update(
        outcome="response", status=200, error=None, exit_code=0
    )
    value["provider"].append({**value["provider"][0], "tool_result": "{}"})
    fault.verify(value, "gateway-response", m)


@pytest.mark.parametrize("case", ["gateway-response", "client-cleanup", "worker-pre_admission"])
def test_same_session_get_terminal_does_not_end_held_post(case):
    m = manifest()
    value = observation(case, m)
    target = fault.one(value["transport"], "target_request")
    value["transport"].append(
        {
            "event": "settled",
            "method": "GET",
            "exchange": "e" * 32,
            "mono_ns": 450,
            "boot": target["boot"],
            "session": target["session"],
        }
    )
    value["transport"].sort(key=lambda row: row["mono_ns"])
    fault.verify(value, case, m)


@pytest.mark.parametrize("case", ["gateway-response", "client-cleanup"])
def test_target_post_terminal_before_fault_refuses(case):
    m = manifest()
    value = observation(case, m)
    request = next(row for row in value["transport"] if row["event"] == "request")
    value["transport"].append(
        {
            "event": "settled",
            "method": "POST",
            "exchange": request["exchange"],
            "mono_ns": 450,
            "boot": request["boot"],
            "session": request["session"],
        }
    )
    value["transport"].sort(key=lambda row: row["mono_ns"])
    with pytest.raises(e.Refused, match="barrier_not_held_at_fault"):
        fault.verify(value, case, m)


def test_retained_owner_loss_campaign_qualifies_exact_candidate():
    runner = importlib.import_module("openclaw_conformance.owner_loss")
    record = ROOT / "scripts/experiments/openclaw_owner_loss_evidence/2026-10-19-windows-v1"
    candidate = e.read(record / "candidate.json")
    baseline = runner.baseline(
        record / "baseline/evidence", record / "baseline/plan.json", candidate
    )
    result = fault.evaluate(
        record / "faults/evidence", e.read(record / "faults/plan.json"), candidate, baseline
    )
    assert result == {
        "outcome": "qualified_for_profile",
        "profile": "owner-loss-windows-v1",
        "campaign_id": "48d6fda50c654d76acc81494eb733401",
        "candidate_sha256": "01c302cfa02f341cb149da075a86d28285cfd8e0a04e372103c5c3bad4a1eeb0",
        "cases": 15,
    }


@pytest.mark.parametrize("retained", [False, True], ids=["synthetic", "retained"])
@pytest.mark.parametrize(
    "case", [f"{actor}-{phase}" for actor in ("gateway", "client") for phase in fault.PHASES]
)
@pytest.mark.parametrize(
    "mutation",
    [
        "unrelated_get_pre_fault",
        "unrelated_get_post_fault",
        "terminal_boot",
        "terminal_session",
        "snapshot_boot",
        "terminal_before_fault",
        "terminal_active",
        "terminal_poisoned",
        "request_after_terminal",
    ],
)
def test_service_settlement_requires_target_exchange_and_runtime(case, mutation, retained):
    if retained:
        record = ROOT / "scripts/experiments/openclaw_owner_loss_evidence/2026-10-19-windows-v1"
        m = e.read(record / "candidate.json")
        value = e.read(record / "faults/evidence" / (case + ".json"))["observations"]
    else:
        m = manifest()
        value = observation(case, m)
    observed = fault.one(value["events"], "service_observed")
    target = fault.one(value["transport"], "target_request")
    terminal = next(
        row
        for row in value["transport"]
        if row["event"] == "settled" and row.get("exchange") == observed["exchange"]
    )
    request = next(
        row
        for row in value["transport"]
        if row["event"] == "request" and row.get("exchange") == observed["exchange"]
    )
    snapshot = next(
        row
        for row in value["transport"]
        if row["event"] == "registry_snapshot" and row["mono_ns"] == observed["snapshot_ns"]
    )
    if mutation.startswith("unrelated_get"):
        terminal.update(method="GET", exchange="e" * 32)
        observed["exchange"] = terminal["exchange"]
        if mutation.endswith("pre_fault"):
            terminal["mono_ns"] = target["mono_ns"] - 2
            observed["terminal_ns"] = terminal["mono_ns"]
    elif mutation == "terminal_before_fault":
        terminal["mono_ns"] = target["mono_ns"] - 2
        observed["terminal_ns"] = terminal["mono_ns"]
    elif mutation == "request_after_terminal":
        request["mono_ns"] = terminal["mono_ns"] + 1
    elif mutation == "snapshot_boot":
        snapshot["boot"] = "f" * 32
    else:
        field = mutation.removeprefix("terminal_")
        terminal[field] = (
            True if field in {"active", "poisoned"} else "f" * (32 if field == "boot" else 64)
        )
    value["transport"].sort(key=lambda row: row["mono_ns"])
    with pytest.raises(e.Refused):
        fault.verify(value, case, m)


@pytest.mark.parametrize("case", ["gateway-active", "client-active"])
@pytest.mark.parametrize("field", ["boot", "session", "request", "method", "exchange"])
def test_service_settlement_request_must_be_target(case, field):
    m = manifest()
    value = observation(case, m)
    request = fault.one(value["transport"], "request")
    request[field] = "foreign"
    with pytest.raises(e.Refused, match="service_terminal"):
        fault.verify(value, case, m)


@pytest.mark.parametrize("event", ["request", "settled"])
@pytest.mark.parametrize("case", ["gateway-active", "client-active"])
def test_service_settlement_exchange_must_be_unambiguous(case, event):
    m = manifest()
    value = observation(case, m)
    duplicate = copy.deepcopy(fault.one(value["transport"], event))
    duplicate["mono_ns"] += 1
    value["transport"].append(duplicate)
    value["transport"].sort(key=lambda row: row["mono_ns"])
    with pytest.raises(e.Refused, match="service_terminal"):
        fault.verify(value, case, m)
