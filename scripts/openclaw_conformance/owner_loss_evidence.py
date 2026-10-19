"""Finite actor-loss profile and strict evidence contract for installed Windows candidates."""

from __future__ import annotations

import importlib
import math
import re
from pathlib import Path
from typing import Any

from .evidence import LIMITS, candidate, digest, encode, file_hash, keys, read, require

PROFILE = "owner-loss-windows-v1"
PHASES = ("pre_admission", "active", "response", "cleanup")
CASES = (
    "control",
    *(f"{actor}-{phase}" for actor in ("gateway", "client", "worker") for phase in PHASES),
    "worker-unavailable",
    "worker-denied",
)
DEADLINES = {
    "case": 720,
    "barrier": 90,
    "client": 270,
    "settlement": 90,
    "readiness": 120,
    "supervision": 240,
}
DENIAL_SOURCE = "086f2ddbf7d081a9e18e2d8e7d9abc5cd9dccd2b47d09aa5b67b2dc231f780c4"
DENIAL_IMAGE = "sha256:1e08cdb63405ca788aea94ef35b792d1e299607c667d33d7bcbcae1fd2611ced"


def plan(manifest: dict[str, Any], campaign: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """Freeze required cases and recovery policy before observing any fault."""
    candidate(manifest)
    require(isinstance(campaign, str) and re.fullmatch(r"[0-9a-f]{32}", campaign), "campaign_id")
    return {
        "schema": 1,
        "profile": PROFILE,
        "campaign_id": campaign,
        "candidate_sha256": digest(manifest),
        "baseline_sha256": digest(baseline),
        "cases": list(CASES),
        "deadlines": DEADLINES,
        "limits": LIMITS,
        "denial_image": DENIAL_IMAGE,
        "denial_source_sha256": DENIAL_SOURCE,
        "fault_unit": "identified_actor_process",
        "client_route": "external_gateway_http",
        "replacement_policy": "same_locked_owner_operator_rotates_token",
    }


def events(rows: Any) -> None:
    """Require strictly ordered monotonic observations."""
    require(isinstance(rows, list) and rows, "missing_events")
    previous = 0
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("event"), str), "event_shape")
        current = row.get("mono_ns")
        require(type(current) is int and current > previous, "event_order")
        assert isinstance(current, int)
        previous = current


def one(rows: list[dict[str, Any]], event: str) -> dict[str, Any]:
    """Require exactly one observation of a named event."""
    matching = [r for r in rows if r.get("event") == event]
    require(len(matching) == 1, "event_inventory:" + event)
    return matching[0]


def verify(value: dict[str, Any], case: str, manifest: dict[str, Any]) -> None:
    """Correlate actor, phase, workload and recovery evidence for one required case."""
    require(value.get("parent_failure") is None, "parent_supervisor_failure")
    require(value.get("case") == case and value.get("outcome") == "pass", "case_failed")
    require(
        value.get("independent_cleanup", {}).get("confirmed") is True
        and value["independent_cleanup"].get("remaining") == [],
        "independent_cleanup",
    )
    rows = value["events"]
    events(rows)
    transport = value["transport"]
    events(transport)
    require(
        type(value["duration"]) in (int, float)
        and math.isfinite(value["duration"])
        and 0 < value["duration"] <= DEADLINES["case"],
        "case_deadline",
    )
    require(
        value["image"] == manifest["image"] and value["config_sha256"] == manifest["config_sha256"],
        "case_candidate",
    )
    final = one(rows, "harness_cleanup")
    require(
        final["processes_stopped"] is True
        and final["remaining"] == []
        and final["errors"] == []
        and final["sentinel_survived"] is True
        and final["sentinel_removed"] is True,
        "harness_cleanup",
    )
    require(value["failure"] is None, "retained_failure")
    control = one(rows, "control_result")
    from .evidence import result

    result(control["result"])
    require(
        control["result"]["image"] == manifest["image"]
        and control["result"]["config_sha256"] == manifest["config_sha256"],
        "control_identity",
    )
    require(one(rows, "lock_contender")["refused"] is True, "owner_lock")
    startups = [r for r in transport if r["event"] == "startup_ready"]
    require(startups and all(r["owner_empty"] is True for r in startups), "startup_recovery")
    if case == "control":
        require(one(rows, "client_settled")["status"] == 200, "control_delivery")
        return
    actor, phase = case.split("-", 1)
    phase = "active" if phase in {"unavailable", "denied"} else phase
    fault = one(rows, "actor_killed")
    barrier = one(rows, "barrier_observed")
    require(barrier["phase"] == phase and barrier["mono_ns"] < fault["mono_ns"], "fault_phase")
    require(
        fault["actor"] == actor
        and fault["exited"] is True
        and fault["others_alive"] is True
        and fault["job_membership_verified"] is True,
        "fault_containment",
    )
    if phase == "active":
        require(
            barrier["compiler_running"] is True and barrier["paused"] is True, "active_compiler"
        )
    identity = one(rows, "target_identity")
    target = one(transport, "target_request")
    require(
        all(target[k] == identity[k] for k in ("boot", "session", "request"))
        and target["mono_ns"] == identity["request_ns"]
        and target["mono_ns"] < fault["mono_ns"],
        "target_identity",
    )
    require(
        startups[0]["boot"] == target["boot"] and startups[0]["mono_ns"] < target["mono_ns"],
        "target_startup",
    )
    LARGE_SOURCE = importlib.import_module("openclaw_sources").LARGE_SOURCE
    SIMPLE_SOURCE = importlib.import_module("openclaw_sources").SIMPLE_SOURCE
    source_digest = importlib.import_module("openclaw_sources").source_digest
    held: dict[str, Any] = {}
    if phase != "active":
        held = one(transport, "barrier")
        require(
            all(held[k] == target[k] for k in ("boot", "session", "request"))
            and held["phase"] == phase
            and target["mono_ns"] <= held["mono_ns"] < barrier["mono_ns"],
            "barrier_correlation",
        )
        requests = [
            row
            for row in transport
            if row["event"] == "request"
            and row.get("method") == "tools/call"
            and all(row.get(key) == target[key] for key in ("boot", "session", "request"))
        ]
        require(
            len(requests) <= 1
            and (phase == "pre_admission" or len(requests) == 1)
            and all(isinstance(row.get("exchange"), str) and row["exchange"] for row in requests),
            "target_exchange_inventory",
        )
        exchanges = {row["exchange"] for row in requests}
        require(
            not any(
                row.get("boot") == target["boot"]
                and held["mono_ns"] <= row["mono_ns"] < fault["mono_ns"]
                and (
                    row["event"] in {"barrier_released", "barrier_ended"}
                    and row.get("phase") == phase
                    or row["event"] == "settled"
                    and row.get("exchange") in exchanges
                    or phase == "pre_admission"
                    and row["event"] == "binding_started"
                    and row.get("session") == target["session"]
                    and row.get("request") == target["request"]
                )
                for row in transport
            ),
            "barrier_not_held_at_fault",
        )
        if phase == "cleanup":
            require(barrier["resources"] == [held["container"]], "cleanup_barrier_resource")
    if phase == "response":
        result(barrier["result"])
        require(
            encode(held["result"]) == encode(barrier["result"])
            and barrier["result"]["image"] == manifest["image"]
            and barrier["result"]["config_sha256"] == manifest["config_sha256"]
            and barrier["result"]["source_sha256"] == source_digest(SIMPLE_SOURCE),
            "response_candidate",
        )
    if phase in {"active", "cleanup"}:
        require(len(barrier["resources"]) == 1, "fault_resource")
    dispatch = one(rows, "target_dispatches")
    require(type(dispatch["count"]) is int, "dispatch_type")
    expected = (
        {0}
        if actor == "worker" and phase == "pre_admission"
        else {0, 1}
        if phase == "pre_admission"
        else {1}
    )
    require(dispatch["count"] in expected, "automatic_replay")
    actual = [
        r
        for r in transport
        if r["event"] == "binding_started"
        and r["mono_ns"] >= barrier["target_started_ns"]
        and r["mono_ns"] <= dispatch["mono_ns"]
    ]
    require(
        len(actual) == dispatch["count"]
        and all(all(r[k] == target[k] for k in ("boot", "session", "request")) for r in actual),
        "dispatch_correlation",
    )
    complete = [
        r
        for r in transport
        if r["event"] == "binding_started" and r["mono_ns"] >= barrier["target_started_ns"]
    ]
    require(
        len(complete) == dispatch["count"] + (2 if actor == "worker" else 1),
        "late_or_missing_dispatch",
    )
    client = one(rows, "client_settled")
    require(client["mono_ns"] > fault["mono_ns"], "client_order")
    require(
        type(client["exit_code"]) is int
        and client["outcome"] in {"response", "transport_error", "killed"},
        "client_outcome",
    )
    require((client["outcome"] == "killed") is (actor == "client"), "client_actor")
    client_rows = value["client"]
    events(client_rows)
    started = one(client_rows, "actor_started")
    sent = one(client_rows, "request_sent")
    require(
        type(started.get("pid")) is int
        and started["pid"] > 0
        and started["mono_ns"] < sent["mono_ns"] < fault["mono_ns"]
        and sent.get("turn") == identity.get("turn")
        and isinstance(sent.get("turn"), str)
        and re.fullmatch(r"[0-9a-f]{32}", sent["turn"]),
        "client_request_identity",
    )
    outcomes = [row for row in client_rows if row["event"] == "client_outcome"]
    if actor == "client":
        require(not outcomes and client["exit_code"] != 0, "killed_client_outcome")
    else:
        require(len(outcomes) == 1, "client_terminal_inventory")
        outcome = outcomes[0]
        require(
            barrier["mono_ns"] < outcome["mono_ns"] <= client["mono_ns"]
            and outcome.get("status") == client.get("status")
            and outcome.get("error") == client.get("error"),
            "client_terminal_correlation",
        )
        if client["outcome"] == "response":
            require(
                type(outcome.get("status")) is int
                and 100 <= outcome["status"] <= 599
                and outcome.get("error") is None
                and client["exit_code"] == 0
                and isinstance(outcome.get("body_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", outcome["body_sha256"]),
                "client_response",
            )
        else:
            require(
                outcome.get("status") is None
                and outcome.get("body_sha256") is None
                and isinstance(outcome.get("error"), str)
                and outcome["error"]
                and client["exit_code"] != 0,
                "client_transport_error",
            )
    provider = value["provider"]
    require(
        isinstance(provider, list) and all(isinstance(row, dict) for row in provider),
        "provider_records",
    )
    turns = [row for row in provider if row.get("turn") == identity["turn"]]
    require(
        1 <= len(turns) <= 2
        and "tool_result" not in turns[0]
        and all(
            row.get("scenario") == ("cancel" if phase == "active" else "valid") for row in turns
        )
        and isinstance(turns[0].get("session"), str)
        and re.fullmatch(r"[0-9a-f]{32}", turns[0]["session"])
        and all(row.get("session") == turns[0]["session"] for row in turns)
        and (len(turns) == 1 or isinstance(turns[1].get("tool_result"), str)),
        "provider_target_turn",
    )
    settled = one(rows, "service_observed")
    require(
        settled["mono_ns"] > fault["mono_ns"] and settled["sentinel_survived"] is True,
        "service_observation",
    )
    recovered: dict[str, Any] = {}
    if actor == "worker":
        require(settled["state"] == "dead_readiness_unconfirmed", "death_is_not_readiness")
        require(settled["resources"] == barrier["resources"], "orphan_inventory")
        recovered = one(rows, "replacement_ready")
        require(
            recovered["mono_ns"] > settled["mono_ns"]
            and recovered["resources"] == []
            and recovered["owner_unchanged"] is True
            and recovered["boot_changed"] is True,
            "recovery_readiness",
        )
        require(
            recovered["old_token_status"] == 401
            and recovered["old_session_status"] == 404
            and recovered["stale_cancel_status"] == 404
            and recovered["stale_delete_status"] == 404
            and recovered["fresh_call_survived"] is True,
            "stale_authority",
        )
        ready = [
            r
            for r in startups
            if r["boot"] == recovered["boot"] and r["mono_ns"] == recovered["ready_ns"]
        ]
        require(
            len(ready) == 1
            and ready[0]["mono_ns"] > fault["mono_ns"]
            and ready[0]["boot"] != target["boot"],
            "replacement_startup",
        )
        fresh = recovered["fresh_result"]
        result(fresh, "incomplete" if fresh.get("status") == "incomplete" else "valid")
        require(
            fresh["cleanup"] == "confirmed"
            and fresh["status"] in {"ok", "incomplete"}
            and fresh["image"] == manifest["image"]
            and fresh["config_sha256"] == manifest["config_sha256"]
            and fresh["source_sha256"] == source_digest(LARGE_SOURCE),
            "fresh_generation_result",
        )
        if case.endswith(("unavailable", "denied")):
            refused = one(rows, "recovery_refused")
            require(
                settled["mono_ns"] < refused["mono_ns"] < recovered["mono_ns"]
                and type(refused["exit_code"]) is int
                and refused["exit_code"] != 0
                and refused["ready"] is False
                and refused["resources"] == settled["resources"],
                "failed_recovery",
            )
            if case.endswith("denied"):
                denial = value["denial"]
                require(
                    denial["daemon_removed"] is True
                    and denial["parent_unchanged"] is True
                    and denial["image"] == DENIAL_IMAGE,
                    "real_denial",
                )
                verify_authorization = importlib.import_module(
                    "openclaw_docker_denial"
                ).verify_authorization

                verify_authorization(
                    denial["decisions"],
                    denial["removals"],
                    target=denial["target"],
                    container=denial["container"],
                    client=denial["client"],
                    nonce=denial["nonce"],
                    denied=True,
                )
                require(denial["container"] in settled["resources"], "denial_orphan_binding")
    else:
        require(settled["state"] == "settled" and settled["resources"] == [], "service_settlement")
        terminal = [
            r
            for r in transport
            if r["event"] == "settled"
            and r.get("exchange") == settled["exchange"]
            and r["mono_ns"] == settled["terminal_ns"]
        ]
        snapshot = [
            r
            for r in transport
            if r["event"] == "registry_snapshot" and r["mono_ns"] == settled["snapshot_ns"]
        ]
        require(
            len(terminal) == len(snapshot) == 1
            and terminal[0]["mono_ns"] < snapshot[0]["mono_ns"] < settled["mono_ns"]
            and snapshot[0]["active"] is False
            and snapshot[0]["poisoned"] is False,
            "service_terminal",
        )
    sibling = one(rows, "sibling_result")
    result(sibling["result"])
    workload_result = importlib.import_module("openclaw_gateway_http_check").workload_result
    require(
        sibling["result"]["image"] == manifest["image"]
        and sibling["result"]["config_sha256"] == manifest["config_sha256"]
        and sibling["result"]["source_sha256"] == source_digest(SIMPLE_SOURCE),
        "sibling_identity",
    )
    controls = [(complete[-1], sibling["result"])]
    if actor == "worker":
        controls.insert(0, (complete[-2], recovered["fresh_result"]))
    old_sessions = {
        row["session"]
        for row in transport
        if row.get("boot") == target["boot"] and isinstance(row.get("session"), str)
    }
    for start, expected_result in controls:
        if actor == "worker":
            require(
                start.get("boot") == recovered["boot"]
                and recovered["ready_ns"] < start["mono_ns"]
                and isinstance(start.get("session"), str)
                and start["session"]
                and start["session"] not in old_sessions,
                "replacement_control_identity",
            )
        next_start = min(
            (r["mono_ns"] for r in complete if r["mono_ns"] > start["mono_ns"]),
            default=final["mono_ns"],
        )
        endings = [
            r
            for r in transport
            if r["event"] == "binding_result"
            and start["mono_ns"] < r["mono_ns"] < next_start
            and all(r[k] == start[k] for k in ("boot", "session", "request"))
        ]
        require(len(endings) == 1, "control_binding_completion")
        observed = workload_result(endings[0]["result"])
        require(
            all(
                encode(observed.get(k)) == encode(v)
                for k, v in expected_result.items()
                if k != "projected_content_items"
            ),
            "control_binding_result",
        )
    require(sibling["mono_ns"] > settled["mono_ns"], "sibling_order")


def evaluate(
    bundle: Path,
    expected: dict[str, Any],
    manifest: dict[str, Any],
    baseline: dict[str, Any],
    *,
    pending: bool = False,
) -> dict[str, Any]:
    """Refuse incomplete or transplanted evidence against independently supplied inputs."""
    require(
        encode(expected) == encode(plan(manifest, expected["campaign_id"], baseline)),
        "fault_plan_narrowed",
    )
    require(
        {p.name for p in bundle.iterdir()} == {"summary.json", *(case + ".json" for case in CASES)},
        "fault_inventory",
    )
    require(
        not any(p.is_symlink() or not p.is_file() for p in bundle.iterdir()),
        "fault_artifact_escape",
    )
    summary = read(bundle / "summary.json")
    keys(
        summary,
        {
            "schema",
            "campaign_id",
            "candidate_sha256",
            "plan_sha256",
            "outcome",
            "attempts",
            "installation_checks",
        },
    )
    require(
        summary["schema"] == 1
        and type(summary["schema"]) is int
        and summary["campaign_id"] == expected["campaign_id"]
        and summary["candidate_sha256"] == digest(manifest)
        and summary["plan_sha256"] == digest(expected),
        "fault_campaign_binding",
    )
    require(
        summary["outcome"] == ("unqualified" if pending else "qualified_for_profile"),
        "fault_campaign_outcome",
    )
    require(
        isinstance(summary["attempts"], list) and len(summary["attempts"]) == len(CASES),
        "fault_attempt_inventory",
    )
    require(
        encode(summary["installation_checks"])
        == encode({"before": digest(manifest), "after": digest(manifest)}),
        "installation_checks",
    )
    seen = set()
    total = 0
    for attempt, case in zip(summary["attempts"], CASES, strict=True):
        keys(attempt, {"case", "attempt_id", "sha256", "bytes", "outcome"})
        aid = attempt["attempt_id"]
        require(
            isinstance(aid, str) and re.fullmatch(r"[0-9a-f]{32}", aid) and aid not in seen,
            "fault_attempt_identity",
        )
        seen.add(aid)
        path = bundle / (case + ".json")
        total += path.stat().st_size
        require(
            type(attempt["bytes"]) is int
            and path.stat().st_size == attempt["bytes"]
            and file_hash(path) == attempt["sha256"],
            "fault_artifact_digest",
        )
        require(attempt["case"] == case and attempt["outcome"] == "pass", "fault_case_failed")
        artifact = read(path, LIMITS["events"])
        keys(artifact, {"campaign_id", "candidate_sha256", "attempt_id", "observations"})
        require(
            artifact["campaign_id"] == expected["campaign_id"]
            and artifact["candidate_sha256"] == digest(manifest)
            and artifact["attempt_id"] == aid,
            "fault_artifact_binding",
        )
        verify(artifact["observations"], case, manifest)
    require(
        total <= LIMITS["events"]
        and total + (bundle / "summary.json").stat().st_size <= LIMITS["bundle"],
        "fault_bundle_limit",
    )
    return {
        "outcome": "qualified_for_profile",
        "profile": PROFILE,
        "campaign_id": expected["campaign_id"],
        "candidate_sha256": digest(manifest),
        "cases": len(CASES),
    }
