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
@pytest.mark.parametrize("loser_status", ["ABORTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "OK"])
def test_conflicts_require_two_known_outcomes(probe, monkeypatch, schedule, loser_status):
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
                winner = not calls
                calls.append((method, request))
            if not winner and loser_status != "OK":
                raise RpcError(loser_status)
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

    if loser_status != "ABORTED":
        with pytest.raises(RuntimeError, match="one success"):
            probe.competing_calls(client, requests, schedule, record)
    else:
        result = probe.competing_calls(client, requests, schedule, record)
        assert sum(response is not None for response in result.values()) == 1
        assert saved[0][1]["passed"]
        if schedule == "concurrent":
            assert saved[0][1]["rpc_intervals_overlap"]
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
    with pytest.raises(RuntimeError, match="one success"):
        probe.competing_calls(client, requests, "concurrent", lambda kind, row: saved.append(row))
    assert [row["status"] for row in saved[0]["outcomes"]] == ["OSError", "OSError"]
    assert not saved[0]["passed"]


def test_ordered_control_must_accept_first_intent(probe, monkeypatch):
    calls = []

    class Peer:
        def __init__(self, *args):
            pass

        def rpc(self, method, request):
            calls.append(method)
            if len(calls) == 1:
                raise RpcError("ABORTED")
            return Message(accepted=method)

        def close(self):
            pass

    monkeypatch.setattr(probe, "Client", Peer)
    client = NS(scope=NS(workspace="w"), token="token", image="image", grpc=NS(RpcError=RpcError))
    requests = [(method, Message(expected_resource_version=7)) for method in ("policy", "detach")]
    with pytest.raises(RuntimeError, match="first intent"):
        probe.competing_calls(client, requests, "policy_first", lambda *args: None)


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


@pytest.mark.parametrize(
    "failure", [None, "container", "pid", "missing_pid", "intent", "receipt", "origin", "stop"]
)
@pytest.mark.parametrize("attached", [True, False])
def test_restart_requires_same_intent_physical_instance_and_observed_access(
    probe, monkeypatch, failure, attached
):
    calls = []
    transition, saved = make_transitions(probe, None)
    before = {"containers": [{"Id": "workload"}, {"Id": "supervisor"}], "processes": {"123": "456"}}
    after = copy.deepcopy(before)
    if failure == "container":
        after["containers"][0]["Id"] = "replacement"
    if failure == "pid":
        after["processes"]["123"] = "789"
    if failure == "missing_pid":
        before["processes"]["123"] = None
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
    gateway = NS(
        stop=lambda: calls.append("stop"),
        start=lambda: calls.append("start"),
        process=NS(poll=lambda: None if failure == "stop" else 0),
    )
    if failure:
        with pytest.raises(RuntimeError):
            transition.restart(gateway, receipt(attached), attached)
        assert not saved[0][1]["completed"]
    else:
        transition.restart(gateway, receipt(attached), attached)
        assert saved[0][1]["completed"]
        assert calls == ["receipt", "stop", "start", "health", "receipt", "origin"]
    assert "start" in calls
    if failure in ("container", "pid", "missing_pid", "intent", "receipt", "stop"):
        assert "origin" not in calls


@pytest.mark.parametrize("winner", ["UpdateConfig", "DetachSandboxProvider"])
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
        lambda *args: {
            method: NS(receipt="receipt") if method == winner else None
            for method in ("UpdateConfig", "DetachSandboxProvider")
        },
    )
    transition.race("concurrent")
    assert calls[:3] == [("policy", True), ("provider", True), ("check", "concurrent_before")]
    if winner == "UpdateConfig":
        assert calls[3:6] == [
            ("barrier", "policy"),
            ("check", "concurrent_policy_won"),
            ("provider", False),
        ]
    else:
        assert calls[3:6] == [
            ("barrier", "detach"),
            ("check", "concurrent_detach_won"),
            ("policy", False),
        ]
    assert calls[-1] == ("check", "concurrent_converged")


@pytest.mark.parametrize(
    "failure", [None, "prepare", "acquire", "race", "restart", "dispose", "cleanup"]
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
            report["transitions"]["cases"].extend([{"passed": True}] * (3 if attached else 2))
            fail("restart")

    monkeypatch.setattr(probe, "CompositionProfile", lambda: profile)
    monkeypatch.setattr(probe, "Transitions", Matrix)
    monkeypatch.setattr(probe, "fixture_policy", lambda *args: None)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("172.18.0.2", 8000))]
    )
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            asyncio.run(probe.measure_transitions(Fixture(), "172.18.0.2", None))
    else:
        asyncio.run(probe.measure_transitions(Fixture(), "172.18.0.2", None))
    evidence = report["transitions"]
    assert evidence["qualification_passed"] is False
    assert evidence["characterization_completed"] == (failure in (None, "dispose", "cleanup"))
    assert evidence["provider_and_profile_cleanup_verified"] == (
        failure not in ("prepare", "dispose", "cleanup")
    )
    assert "cleanup" in calls or failure == "dispose"
