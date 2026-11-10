"""Replacement authority needs fresh session evidence on both sides of observed traffic."""

from __future__ import annotations

import asyncio
import copy
import importlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


class Message(NS):
    def SerializeToString(self, **kwargs):
        return repr(vars(self)).encode()


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_restart_probe")


def status(prefix="new", sequence=1):
    return Message(
        network_instance_id=prefix + "-network",
        observed=NS(
            session_id=prefix + "-session",
            process_instance_id=prefix + "-process",
            sequence=sequence,
        ),
    )


@pytest.mark.parametrize("field", ["network", "session", "process"])
@pytest.mark.parametrize("value", ["old", ""])
def test_every_authority_identity_must_be_new_and_nonempty(probe, field, value):
    before, current = status("old"), status()
    target, name = {
        "network": (current, "network_instance_id"),
        "session": (current.observed, "session_id"),
        "process": (current.observed, "process_instance_id"),
    }[field]
    setattr(target, name, value + "-" + field if value else "")
    assert not probe.fresh_authority(before, current)
    assert probe.fresh_authority(before, status())
    setattr(target, name, "")
    assert not probe.fresh_authority(current, status("third"))


@pytest.mark.parametrize("sequence", [0, 1, 2, 3])
def test_reports_must_advance_within_the_accepted_session(probe, sequence):
    before, accepted = status("old"), status(sequence=2)
    assert probe.fresh_authority(before, status(sequence=sequence), accepted) is (sequence > 2)
    assert not probe.fresh_authority(before, status("third", sequence=4), accepted)
    assert not probe.fresh_authority(before, status(sequence=0))


@pytest.mark.parametrize("attached", [True, False])
@pytest.mark.parametrize("version", ["0.1.2", "0.1.7"])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "preserved",
        "stale",
        "intent",
        "runtime",
        "identity",
        "guest",
        "after",
        "cached_after",
        "retained",
        "policy",
        "provider",
        "receipt",
    ],
)
def test_recovery_brackets_traffic_with_fresh_stable_authority(
    probe, monkeypatch, attached, failure, version
):
    saved, events, persisted, cached_before_guest = [], [], [], []
    spec = Message(policy="expected")
    row = {
        "runtime_continuity": failure == "preserved",
        "after": {},
        "after_spec_hex": spec.SerializeToString().hex(),
        "completed": False,
        "before": {},
        "original_supervisor_absent": True,
        "original_processes_retired": True,
    }
    fixture = NS(
        client=NS(expected_version=version), owners=[{"name": "owned", "sandbox_id": "physical"}]
    )
    fixture.save = lambda: persisted.append(copy.deepcopy(saved))

    def retained(method, request):
        assert method == "GetSandboxProviderStatus" and request.receipt_id == "original"
        events.append("retained")
        if failure == "retained":
            raise RuntimeError("retained status unavailable")
        return NS(status=Message(state="SUPERSEDED"))

    fixture.client.scope = "workspace"
    fixture.client.rpc = retained
    fixture.client.pb = NS(
        GetSandboxProviderStatusRequest=NS, ProviderReadinessState=NS(Name=lambda value: value)
    )

    def record(kind, value):
        saved.append((kind, value))

    transition = probe.RestartAuthority(
        fixture, NS(provider_host="fixture", provider="provider"), "192.0.2.1", record
    )
    monkeypatch.setattr(transition, "restart_runtime", lambda *args: (row, status("old")))
    monkeypatch.setattr(
        transition,
        "current",
        lambda: NS(spec=Message(policy="wrong") if failure == "intent" else spec),
    )
    monkeypatch.setattr(probe, "snapshot", lambda identity: {})
    monkeypatch.setattr(probe, "boundary_descriptor", lambda *args: {"session_id": "old-process"})
    monkeypatch.setattr(probe, "runtime_continuity", lambda *args: failure != "runtime")
    tick = iter(range(0, 10000, 31))
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(tick))
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)

    def policy(enabled):
        assert enabled is attached
        events.append("policy")
        if failure == "policy":
            raise RuntimeError("policy intent failed")

    def provider(enabled):
        assert enabled is attached
        events.append("provider")
        if failure == "provider":
            raise RuntimeError("provider intent failed")
        return Message(receipt_id="original" if failure == "receipt" else "restored")

    monkeypatch.setattr(transition, "policy", policy)
    monkeypatch.setattr(transition, "provider", provider)

    def wait(*args):
        assert args[0].receipt_id == "restored"
        sequence = len([event for event in events if event == "status"]) + 1
        events.append("status")
        prefix = "old" if failure == "stale" else "new"
        if failure == "identity" and sequence == 2:
            prefix = "third"
        if failure == "after" and sequence == 3:
            sequence = 2
        if failure == "cached_after" and sequence >= 3:
            return cached_before_guest[-1]
        result = status(prefix, sequence)
        if version == "0.1.7":
            result.observed.process_instance_id = "old-process"
        return result

    def observe(client, name, ip, record, label, host, path, expected, method):
        cached_before_guest.append(status(sequence=3))
        if version == "0.1.7":
            cached_before_guest[-1].observed.process_instance_id = "old-process"
        events.append("guest")
        result = {"case": label, "passed": failure != "guest"}
        record("cases", result)
        if failure == "guest":
            raise RuntimeError("origin observation failed")
        assert expected == ((403 if path == "/blocked" else 200) if attached else "EACCES")
        return result

    monkeypatch.setattr(transition, "wait_provider", wait)
    monkeypatch.setattr(probe, "observe_native", observe)
    if failure:
        with pytest.raises(RuntimeError):
            transition.recover(None, Message(receipt_id="original"), attached)
        assert not row["completed"]
        if failure not in ("guest", "after", "cached_after"):
            assert "guest" not in events
        if failure in ("after", "cached_after"):
            case = next(value for kind, value in saved if kind == "cases")
            assert case["passed"] and not case["authority_verified"]
    else:
        transition.recover(None, Message(receipt_id="original"), attached)
        assert row["completed"]
        assert events == ["retained", "policy", "provider", "status"] + [
            "status",
            "guest",
            "status",
            "status",
        ] * (3 if attached else 2)
        assert row["retained_receipt_state"] == "SUPERSEDED"
        assert row["restoration_state"] == "installed"
        cases = [value for kind, value in saved if kind == "cases"]
        assert all(case["authority_verified"] for case in cases)
        assert all(case["authority_before_hex"] != case["authority_after_hex"] for case in cases)
        assert all(
            case["authority_post_traffic_floor_hex"] != case["authority_after_hex"]
            for case in cases
        )
    assert persisted


@pytest.mark.parametrize(
    "failure", [None, "prepare", "create", "attest", "partial", "authority", "cleanup"]
)
def test_matrix_retains_unrun_cases_and_cleanup_without_qualification(probe, monkeypatch, failure):
    report, cleaned, snapshots = {}, [], []
    fixture = NS(client=NS(image="image"), owners=[], report=report)
    fixture.save = lambda: snapshots.append(copy.deepcopy(report))

    async def acquire(**kwargs):
        if failure == "create":
            raise RuntimeError("uncertain create")
        fixture.owners.append({"name": "owned", "sandbox_id": "physical"})

    async def dispose(owner):
        cleaned.append(owner["sandbox_id"])

    fixture.acquire, fixture.dispose = acquire, dispose

    class Profile:
        profile_id, provider, provider_id, provider_host = "profile", "provider", "id", "fixture"

        def preflight(self, ip):
            pass

        def prepare_workspace(self, client):
            if failure == "prepare":
                raise RuntimeError("prepare failed")

        def cleanup_workspace(self, client):
            cleaned.append("profile")
            if failure == "cleanup":
                raise RuntimeError("cleanup failed")

    class Matrix:
        def __init__(self, fixture, profile, ip, record):
            self.record = record

        def policy(self, attached):
            pass

        def provider(self, attached):
            return Message()

        def check(self, label, *args, blocked=False):
            for suffix in (
                ["get_provider", "post_provider", "get_blocked"]
                if blocked
                else ["get_provider", "post_provider"]
            ):
                self.record("cases", {"case": label + "_" + suffix, "passed": True})

        def recover(self, gateway, receipt, attached):
            if failure == "attest":
                raise RuntimeError("attestation failed")
            label = "recovered_attached" if attached else "recovered_detached"
            for suffix in (
                ["get_provider", "post_provider", "get_blocked"]
                if attached
                else ["get_provider", "post_provider"]
            ):
                case = label + "_" + suffix
                self.record("case_attempts", {"case": case})
                self.record(
                    "cases",
                    {"case": case, "passed": True, "authority_verified": failure != "authority"},
                )
                if failure == "partial":
                    raise RuntimeError("partial stage")

    monkeypatch.setattr(probe, "CompositionProfile", Profile)
    monkeypatch.setattr(probe, "RestartAuthority", Matrix)
    monkeypatch.setattr(probe, "fixture_policy", lambda *args: None)
    monkeypatch.setattr(probe, "native_origin_ip", lambda ip: ip)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("192.0.2.1", 8000))]
    )
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(probe.measure_restart_authority(fixture, "192.0.2.1", None))
    else:
        asyncio.run(probe.measure_restart_authority(fixture, "192.0.2.1", None))
    result = report["restart_authority"]
    assert result["qualification_passed"] is False
    assert result["runtime_continuity_qualified"] is False
    assert result["characterization_completed"] is (failure in (None, "cleanup"))
    assert result["provider_and_profile_cleanup_verified"] is (
        failure not in ("prepare", "cleanup")
    )
    assert cleaned == (["profile"] if failure in ("prepare", "create") else ["physical", "profile"])
    assert len(result["unrun_cases"]) == (
        5 if failure in ("prepare", "create", "attest") else 4 if failure == "partial" else 0
    )
    for saved in snapshots:
        evidence = saved["restart_authority"]
        assert not {row["case"] for row in evidence["case_attempts"]} & set(evidence["unrun_cases"])
