"""Conflict and restart evidence must not turn uncertainty into revocation proof."""

from __future__ import annotations

import asyncio
import copy
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_transition_probe")


class Message(NS):
    def SerializeToString(self, **kwargs):
        return repr(vars(self)).encode()


class RpcError(Exception):
    def code(self):
        return NS(name=str(self))


@pytest.mark.parametrize("schedule", ["policy_first", "detach_first", "concurrent"])
@pytest.mark.parametrize("policy_status", ["ABORTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "OK"])
@pytest.mark.parametrize("detach_status", ["OK", "ABORTED"])
def test_conflicts_require_two_known_outcomes(
    probe, monkeypatch, schedule, policy_status, detach_status
):
    calls, closed, saved = [], [], []
    entered = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    client = NS(
        scope=NS(workspace="workspace"), token="token", image="image", grpc=NS(RpcError=RpcError)
    )

    class Peer:
        def __init__(self, *args):
            assert args == ("workspace", "token", "image")

        def rpc(self, method, request):
            if schedule == "concurrent":
                entered.wait()
            with lock:
                calls.append((method, request))
            status = policy_status if method == "UpdateConfig" else detach_status
            if status != "OK":
                raise RpcError(status)
            return Message(accepted=method)

        def close(self):
            closed.append(self)

    monkeypatch.setattr(probe, "Client", Peer)
    requests = [
        (method, Message(expected_resource_version=7, request_id=method))
        for method in ("UpdateConfig", "DetachSandboxProvider")
    ]

    def record(kind, row):
        saved.append((kind, row))

    allowed = detach_status == "OK" and (
        policy_status == "OK"
        and schedule in ("policy_first", "concurrent")
        or policy_status == "ABORTED"
        and schedule in ("detach_first", "concurrent")
    )
    if not allowed:
        with pytest.raises(RuntimeError, match="unexpected"):
            probe.competing_calls(client, requests, schedule, record)
    else:
        result = probe.competing_calls(
            client, requests, schedule, record, observe_version=lambda: 7
        )
        assert sum(response is not None for response in result.values()) == 1 + int(
            policy_status == "OK"
        )
        assert saved[0][1]["passed"]
        if schedule == "concurrent":
            assert saved[0][1]["rpc_intervals_overlap"]
        else:
            assert saved[0][1]["resource_version_after_first"] == 7
    assert len(closed) == 2
    assert len(saved[0][1]["outcomes"]) == 2
    if schedule != "concurrent":
        assert calls == (list(reversed(requests)) if schedule == "detach_first" else requests)


@pytest.mark.parametrize("versions", [(0, 0), (7, 8)])
def test_unconditional_or_different_versions_never_dispatch(probe, monkeypatch, versions):
    monkeypatch.setattr(probe, "Client", lambda *args: pytest.fail("must not dispatch"))
    requests = [("method", Message(expected_resource_version=v)) for v in versions]
    with pytest.raises(ValueError, match="same nonzero"):
        probe.competing_calls(None, requests, "concurrent", lambda *args: None)


def test_unknown_failure_is_retained_without_a_retry(probe, monkeypatch):
    saved = []

    def unavailable(*args):
        raise OSError("cannot create channel")

    monkeypatch.setattr(probe, "Client", unavailable)
    client = NS(scope=NS(workspace="w"), token="token", image="image", grpc=NS(RpcError=RpcError))
    requests = [("method", Message(expected_resource_version=7)) for _ in range(2)]
    with pytest.raises(RuntimeError, match="unexpected"):
        probe.competing_calls(client, requests, "concurrent", lambda kind, row: saved.append(row))
    assert [row["status"] for row in saved[0]["outcomes"]] == ["OSError", "OSError"]
    assert not saved[0]["passed"]


def make_transitions(probe, rpc):
    client = NS(
        scope=NS(workspace="workspace"),
        image="image",
        rpc=rpc,
        pb=NS(
            GetSandboxRequest=Message,
            GetSandboxProviderStatusRequest=Message,
            GetSandboxPolicyStatusRequest=Message,
            PROVIDER_MUTATION_KIND_ATTACH=1,
            PROVIDER_MUTATION_KIND_DETACH=2,
            PROVIDER_READINESS_STATE_READY=3,
            PROVIDER_READINESS_STATE_REVOKED=5,
            POLICY_STATUS_LOADED=2,
        ),
    )
    saved = []
    fixture = NS(
        client=client, owners=[{"name": "sandbox", "sandbox_id": "physical"}], save=lambda: None
    )
    profile = NS(provider="provider", provider_id="provider-id", provider_host="allowed")
    transition = probe.Transitions(
        fixture, profile, "172.18.0.2", lambda kind, row: saved.append((kind, row))
    )
    return transition, saved


def receipt(attached):
    return Message(
        receipt_id="receipt",
        provider="provider",
        workspace="workspace",
        kind=1 if attached else 2,
        desired=NS(
            sandbox_id="physical", sandbox="sandbox", provider_id="provider-id" if attached else ""
        ),
    )


@pytest.mark.parametrize(
    "field", ["receipt_id", "provider", "workspace", "kind", "sandbox_id", "sandbox", "provider_id"]
)
@pytest.mark.parametrize("attached", [True, False])
def test_provider_receipt_cannot_cross_identity_boundary(probe, field, attached):
    transition, _ = make_transitions(
        probe, lambda *args: pytest.fail("wrong receipt must not poll")
    )
    value = receipt(attached)
    target = value.desired if field in ("sandbox_id", "sandbox", "provider_id") else value
    setattr(target, field, "" if field == "receipt_id" else "wrong")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        transition.wait_provider(value, attached)


@pytest.mark.parametrize("barrier", ["policy", "provider"])
def test_replacement_is_rejected_before_readiness_poll(probe, barrier):
    calls = []

    def rpc(method, request):
        calls.append(method)
        return NS(sandbox=NS(metadata=NS(id="replacement")))

    transition, _ = make_transitions(probe, rpc)
    with pytest.raises(RuntimeError, match="replaced"):
        if barrier == "policy":
            transition.wait_policy(NS(version=2))
        else:
            transition.wait_provider(receipt(True), True)
    assert calls == ["GetSandbox"]


def physical_snapshot():
    return {
        "containers": [
            {
                "Id": role,
                "State": "running",
                "Labels": {
                    "openshell.ai/isolation-role": role,
                    "openshell.ai/sandbox-id": "physical",
                },
            }
            for role in ("sandbox", "supervisor")
        ],
        "processes": {"123": "456"},
    }


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "old_supervisor",
        "old_process",
        "unknown_owner",
        "stopped",
        "missing_role",
        "missing_process",
    ],
)
def test_runtime_replacement_requires_independent_retirement_evidence(probe, monkeypatch, failure):
    before = physical_snapshot()
    after = copy.deepcopy(before)
    after["containers"][1]["Id"] = "new-supervisor"
    after["processes"] = {"789": "101112"}
    monkeypatch.setattr(
        probe, "docker_get", lambda path: {} if failure == "old_supervisor" else None
    )
    monkeypatch.setattr(
        probe, "process_identity", lambda pid: "456" if failure == "old_process" else None
    )
    if failure == "unknown_owner":
        after["containers"][1]["Labels"]["openshell.ai/sandbox-id"] = "replacement"
    elif failure == "stopped":
        after["containers"][0]["State"] = "exited"
    elif failure == "missing_role":
        after["containers"].pop()
    elif failure == "missing_process":
        after["processes"] = {}
    if failure:
        with pytest.raises(RuntimeError):
            probe.runtime_continuity("physical", before, after)
    else:
        assert probe.runtime_continuity("physical", before, after) is False


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "container",
        "pid",
        "missing_pid",
        "intent",
        "receipt",
        "origin",
        "stop",
        "forced_kill",
        "terminated",
        "abnormal_exit",
        "already_stopped",
        "stop_error",
        "runtime_restarted",
    ],
)
@pytest.mark.parametrize("attached", [True, False])
def test_restart_requires_same_intent_physical_instance_and_observed_access(
    probe, monkeypatch, failure, attached
):
    calls = []
    transition, saved = make_transitions(probe, None)
    persisted = []
    monkeypatch.setattr(transition.fixture, "save", lambda: persisted.append(copy.deepcopy(saved)))
    before = physical_snapshot()
    after = copy.deepcopy(before)
    if failure == "container":
        after["containers"][0]["Id"] = "replacement"
    if failure == "pid":
        after["processes"]["123"] = "789"
    if failure == "missing_pid":
        before["processes"]["123"] = None
    if failure == "runtime_restarted":
        after["containers"][1]["Id"] = "new-supervisor"
        after["processes"] = {"789": "101112"}
    monkeypatch.setattr(probe, "docker_get", lambda path: None)
    monkeypatch.setattr(probe, "process_identity", lambda pid: None)
    snapshots = iter([before, after])
    monkeypatch.setattr(probe, "snapshot", lambda identity: next(snapshots))
    intents = iter(["original", "changed" if failure == "intent" else "original"])
    monkeypatch.setattr(transition, "current", lambda: NS(spec=Message(policy=next(intents))))
    monkeypatch.setattr(probe, "wait_gateway", lambda client: calls.append("health"))

    def wait(value, state):
        if failure == "receipt" and "health" in calls:
            raise RuntimeError("receipt not installed")
        calls.append("receipt")

    def check(*args, **kwargs):
        calls.append("origin")
        if failure == "origin":
            raise RuntimeError("origin mismatch")

    monkeypatch.setattr(transition, "wait_provider", wait)
    monkeypatch.setattr(transition, "check", check)

    def stop():
        calls.append("stop")
        if failure == "stop_error":
            raise RuntimeError("stop failed")

    exit_codes = iter(
        [
            0 if failure == "already_stopped" else None,
            {"stop": None, "forced_kill": -9, "terminated": -15, "abnormal_exit": 1}.get(
                failure, 0
            ),
        ]
    )
    gateway = NS(
        stop=stop,
        start=lambda: calls.append("start"),
        process=NS(poll=lambda: next(exit_codes)),
    )
    if failure:
        with pytest.raises(
            probe.RuntimeRestarted if failure == "runtime_restarted" else RuntimeError
        ):
            transition.restart(gateway, receipt(attached), attached)
        assert not saved[0][1]["completed"]
        if failure == "runtime_restarted":
            assert saved[0][1]["runtime_continuity"] is False
    else:
        transition.restart(gateway, receipt(attached), attached)
        assert saved[0][1]["completed"]
        assert calls == ["receipt", "stop", "start", "health", "receipt", "origin"]
    assert "start" in calls
    assert persisted[0][0][1]["completed"] is False
    shutdown_failed = failure in (
        "stop",
        "forced_kill",
        "terminated",
        "abnormal_exit",
        "already_stopped",
        "stop_error",
    )
    assert saved[0][1]["graceful_shutdown"] is not shutdown_failed
    assert persisted[0][0][1]["graceful_shutdown"] is not shutdown_failed
    if shutdown_failed:
        assert "health" not in calls
    if failure == "already_stopped":
        assert "stop" not in calls
    if failure in (
        "container",
        "pid",
        "missing_pid",
        "intent",
        "receipt",
        "stop",
        "forced_kill",
        "terminated",
        "abnormal_exit",
        "already_stopped",
        "stop_error",
        "runtime_restarted",
    ):
        assert "origin" not in calls


@pytest.mark.parametrize("winner", ["both", "DetachSandboxProvider"])
def test_loser_is_resubmitted_only_after_winner_barrier_and_traffic_checks(
    probe, monkeypatch, winner
):
    transition, _ = make_transitions(probe, None)
    calls = []
    monkeypatch.setattr(transition, "current", lambda: NS(metadata=NS(resource_version=7)))
    monkeypatch.setattr(transition, "policy", lambda enabled: calls.append(("policy", enabled)))
    monkeypatch.setattr(transition, "provider", lambda enabled: calls.append(("provider", enabled)))
    monkeypatch.setattr(
        transition,
        "policy_request",
        lambda version, enabled: Message(expected_resource_version=version),
    )
    monkeypatch.setattr(
        transition,
        "provider_request",
        lambda version, enabled: (
            "DetachSandboxProvider",
            Message(expected_resource_version=version),
        ),
    )
    monkeypatch.setattr(
        transition, "check", lambda label, *args, **kwargs: calls.append(("check", label))
    )
    monkeypatch.setattr(
        transition, "wait_policy", lambda response: calls.append(("barrier", "policy"))
    )
    monkeypatch.setattr(
        transition, "wait_provider", lambda *args: calls.append(("barrier", "detach"))
    )
    monkeypatch.setattr(
        probe,
        "competing_calls",
        lambda *args, **kwargs: {
            method: NS(receipt="receipt") if winner == "both" or method == winner else None
            for method in ("UpdateConfig", "DetachSandboxProvider")
        },
    )
    transition.race("concurrent")
    assert calls[:3] == [("policy", True), ("provider", True), ("check", "concurrent_before")]
    if winner == "both":
        assert calls[3:6] == [
            ("barrier", "policy"),
            ("barrier", "detach"),
            ("check", "concurrent_both_accepted"),
        ]
    else:
        assert calls[3:6] == [
            ("barrier", "detach"),
            ("check", "concurrent_detach_won"),
            ("policy", False),
        ]
    assert calls[-1] == ("check", "concurrent_converged")


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "prepare",
        "acquire",
        "race",
        "restart",
        "restart_detached",
        "dispose",
        "cleanup",
        "continuity",
    ],
)
def test_measurement_preserves_partial_evidence_and_cleanup_limits(probe, monkeypatch, failure):
    calls = []
    report = {}

    def fail(stage):
        calls.append(stage)
        if stage == failure:
            raise RuntimeError(stage)

    profile = NS(
        provider_host="allowed",
        profile_id="profile",
        provider="provider",
        provider_id="provider-id",
        preflight=lambda ip: None,
        prepare_workspace=lambda client: fail("prepare"),
        cleanup_workspace=lambda client: fail("cleanup"),
    )

    class Fixture:
        client = NS(image="image")
        owners = []

        def __init__(self):
            self.report, self.owners = report, []

        def save(self):
            pass

        async def acquire(self, **kwargs):
            self.owners.append({"name": "sandbox", "sandbox_id": "physical"})
            fail("acquire")

        async def dispose(self, owner):
            fail("dispose")

    class Matrix:
        def __init__(self, *args):
            pass

        def race(self, schedule):
            report["transitions"]["cases"].extend([{"passed": True}] * 8)
            fail("race")

        def policy(self, enabled):
            pass

        def provider(self, enabled):
            return "receipt"

        def restart(self, gateway, value, attached):
            if failure == "continuity":
                raise probe.RuntimeRestarted("runtime replaced")
            report["transitions"]["cases"].extend([{"passed": True}] * (3 if attached else 2))
            fail("restart" if attached else "restart_detached")

    monkeypatch.setattr(probe, "CompositionProfile", lambda: profile)
    monkeypatch.setattr(probe, "Transitions", Matrix)
    monkeypatch.setattr(probe, "fixture_policy", lambda *args: None)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("172.18.0.2", 8000))]
    )
    if failure and failure != "continuity":
        with pytest.raises(RuntimeError, match=failure):
            asyncio.run(probe.measure_transitions(Fixture(), "172.18.0.2", None))
    else:
        asyncio.run(probe.measure_transitions(Fixture(), "172.18.0.2", None))
    evidence = report["transitions"]
    assert evidence["qualification_passed"] is False
    assert evidence["characterization_completed"] == (
        failure in (None, "dispose", "cleanup", "continuity")
    )
    assert evidence["restart_continuity_blocked"] == (failure == "continuity")
    assert len(evidence["unrun_cases"]) == {
        "continuity": 5,
        "restart": 5,
        "restart_detached": 2,
    }.get(failure, 0)
    assert evidence["provider_and_profile_cleanup_verified"] == (
        failure not in ("prepare", "dispose", "cleanup")
    )
    assert "cleanup" in calls or failure == "dispose"
