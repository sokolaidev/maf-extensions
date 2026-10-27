"""Installed-policy evidence must match the exact desired generation and runtime acknowledgement."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_composition_probe")


def receipt_fixture():
    desired = NS(
        attachment_epoch="epoch-a",
        provider_env_revision=31,
        config_revision=17,
        policy_hash="hash-a",
    )
    receipt = NS(receipt_id="receipt-a", desired=desired)
    observed = NS(
        session_id="session-a",
        process_instance_id="process-a",
        **vars(desired),
        credentials_installed=True,
        policy_active=True,
        launch_environment_installed=True,
    )
    return receipt, NS(receipt=copy.deepcopy(receipt), state=3, observed=observed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("attachment_epoch", "epoch-b"),
        ("provider_env_revision", 32),
        ("config_revision", 18),
        ("policy_hash", "hash-b"),
        ("credentials_installed", False),
        ("policy_active", False),
        ("launch_environment_installed", False),
        ("session_id", ""),
        ("process_instance_id", ""),
    ],
)
def test_partial_or_different_installation_is_not_ready(probe, field, value):
    receipt, status = receipt_fixture()
    setattr(status.observed, field, value)
    assert not probe.receipt_installed(status, receipt, 3)


@pytest.mark.parametrize("state", [0, 1, 2, 4, 5, 6, 7])
def test_saved_pending_failed_or_superseded_receipt_is_not_ready(probe, state):
    receipt, status = receipt_fixture()
    status.state = state
    assert not probe.receipt_installed(status, receipt, 3)


def test_different_receipt_cannot_substitute_for_original(probe):
    receipt, status = receipt_fixture()
    status.receipt.receipt_id = "receipt-b"
    assert not probe.receipt_installed(status, receipt, 3)


def test_exact_installation_and_revocation_controls(probe):
    receipt, status = receipt_fixture()
    assert probe.receipt_installed(status, receipt, 3)
    status.state = 5
    assert probe.receipt_installed(status, receipt, 5)


@pytest.mark.parametrize("field,value", [("version", 3), ("policy_hash", "hash-b"), ("status", 1)])
def test_revision_requires_exact_loaded_version_and_hash(probe, field, value):
    response = NS(version=2, policy_hash="hash-a")
    status = NS(active_version=2, revision=NS(version=2, policy_hash="hash-a", status=2))
    setattr(status.revision, field, value)
    assert not probe.revision_installed(status, response, 2)


def test_historical_loaded_revision_is_not_current(probe):
    response = NS(version=2, policy_hash="hash-a")
    status = NS(active_version=3, revision=NS(version=2, policy_hash="hash-a", status=2))
    assert not probe.revision_installed(status, response, 2)
    status.active_version = 2
    assert probe.revision_installed(status, response, 2)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "timeout",
        "dns",
        "reset",
        "wrong_errno_phase",
        "side_effect",
        "journal",
        "exit",
        "stderr",
        "control",
    ],
)
def test_native_observer_requires_control_and_whole_origin_delta(probe, monkeypatch, failure):
    routes = importlib.import_module("openshell_routes_probe")
    journal, saved = [], []
    request = {}

    def direct(ip, port, wire):
        method, target, _ = wire.split("\r\n")[0].split(" ")
        request.update(method=method, path=target, host="allowed:8000", port=8000)
        journal.append(dict(request))
        return {"status": 200, "body": json.dumps(request)}

    def execute(*args):
        observed = {"error": "PermissionError", "errno": 13, "phase": "connect"}
        if failure in ("timeout", "dns", "reset"):
            observed = {"error": failure, "phase": "connect"}
        elif failure == "wrong_errno_phase":
            observed["phase"] = "response"
        elif failure == "side_effect":
            journal.append({**request, "path": "/unrelated"})
        elif failure == "journal":
            journal.clear()
        return (
            int(failure == "exit"),
            json.dumps(observed).encode(),
            b"error" if failure == "stderr" else b"",
        )

    monkeypatch.setattr(routes, "raw_request", direct)
    monkeypatch.setattr(routes, "guest_code", lambda *args: "guest")
    monkeypatch.setattr(probe, "origin_get", lambda *args: copy.deepcopy(journal))
    monkeypatch.setattr(probe, "execute", execute)
    if failure == "control":
        monkeypatch.setattr(routes, "raw_request", lambda *args: {"status": 500})

    def call():
        return probe.observe_native(
            None,
            "sandbox",
            "172.18.0.2",
            lambda kind, row: saved.append((kind, row)),
            "closed",
            "allowed",
            "/provider",
            "EACCES",
            "GET",
        )

    if failure:
        with pytest.raises(RuntimeError):
            call()
        assert all(not row.get("passed", False) for _, row in saved)
    else:
        assert call()["observation"] == "connect_permission_refusal"
    assert saved[0][0] == "controls"
    if failure != "control":
        assert saved[-1][0] == "cases"


@pytest.mark.parametrize(
    "failure", [None, "matrix", "acquire", "dispose", "provider_cleanup", "prepare"]
)
def test_native_fixture_retains_limits_and_cleanup_failures(probe, monkeypatch, failure):
    egress = importlib.import_module("openshell_egress_probe")
    calls = []
    ip = "172.18.0.2"

    class Profile:
        provider_host, base_host = "provider", "base"
        profile_id, provider, provider_id = "profile", "provider", "provider-id"

        def preflight(self, ip):
            pass

        def prepare_workspace(self, client):
            if failure == "prepare":
                raise RuntimeError("prepare")

        def cleanup_workspace(self, client):
            calls.append("provider_cleanup")
            if failure == "provider_cleanup":
                raise RuntimeError("provider_cleanup")

        def _run_cases(self, *args, observe, record):
            row = {"passed": failure != "matrix"}
            record("cases", row)
            if failure == "matrix":
                raise RuntimeError("matrix")
            return [row] * 25

    class Fixture:
        client = NS(image="image")

        def __init__(self):
            self.report, self.owners, self.saved = {}, [], []

        def save(self):
            self.saved.append(copy.deepcopy(self.report))

        async def acquire(self, **kwargs):
            self.owners.append({"name": "sandbox", "sandbox_id": "physical"})
            if failure == "acquire":
                raise RuntimeError("acquire")

        async def dispose(self, owner):
            calls.append("dispose")
            if failure == "dispose":
                raise RuntimeError("dispose")

    monkeypatch.setattr(probe, "CompositionProfile", Profile)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, (ip, 8000))]
    )
    monkeypatch.setattr(egress, "fixture_policy", lambda *args: None)
    fixture = Fixture()
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            asyncio.run(probe.measure_native_composition(fixture, ip))
    else:
        asyncio.run(probe.measure_native_composition(fixture, ip))
    evidence = fixture.saved[-1]["composition"]
    assert evidence["qualification_passed"] is False
    assert evidence["characterization_completed"] == (
        failure in (None, "dispose", "provider_cleanup")
    )
    assert evidence["provider_and_profile_cleanup_verified"] == (
        failure in (None, "matrix", "acquire")
    )
    assert calls == (
        ["provider_cleanup"]
        if failure == "prepare"
        else ["dispose"]
        if failure == "dispose"
        else ["dispose", "provider_cleanup"]
    )


@pytest.mark.parametrize("wrong_provider", [False, True])
def test_native_matrix_gates_requests_and_composes_overlapping_rules(
    probe, monkeypatch, wrong_provider
):
    from collections import defaultdict

    class Message(NS):
        def SerializeToString(self):
            return b"fixture-protobuf"

    class Aborted(Exception):
        def code(self):
            return "ABORTED"

    profile = object.__new__(probe.CompositionProfile)
    profile.provider, profile.provider_id = "provider", "provider-id"
    profile.provider_host, profile.base_host = "allowed", "denied"
    profile.endpoint = lambda rule, host, path, methods=("GET",): vars(rule).update(
        host=host, path=path, methods=methods
    )
    monkeypatch.setattr(
        probe,
        "fixture_spec",
        lambda image: NS(
            policy=NS(network_policies=defaultdict(lambda: NS(binaries=NS(add=lambda **kw: None))))
        ),
    )
    version, epoch, active, loaded = 1, 0, False, True
    base_rule, receipt, installed = None, None, True
    seen, transitions = [], []

    def rpc(method, request):
        nonlocal version, epoch, active, loaded, base_rule, receipt, installed
        if method == "GetSandbox":
            return NS(sandbox=NS(metadata=NS(id="sandbox-id", resource_version=version)))
        if method in ("AttachSandboxProvider", "DetachSandboxProvider"):
            assert request.expected_resource_version == version
            active = method == "AttachSandboxProvider"
            epoch += 1
            version += 1
            installed = False
            desired = NS(
                sandbox_id="sandbox-id",
                provider_id="wrong" if wrong_provider else "provider-id",
                attachment_epoch=str(epoch),
                provider_env_revision=version,
                config_revision=version,
                policy_hash=str(version),
            )
            receipt = Message(receipt_id=str(epoch), desired=desired)
            return NS(receipt=receipt)
        if method == "GetSandboxProviderStatus":
            if request.receipt_id != receipt.receipt_id:
                return NS(status=Message(state=6))
            installed = True
            observed = NS(
                **{
                    k: v
                    for k, v in vars(receipt.desired).items()
                    if k not in ("sandbox_id", "provider_id")
                },
                session_id="session",
                process_instance_id="process",
                credentials_installed=True,
                policy_active=True,
                launch_environment_installed=True,
            )
            return NS(status=Message(receipt=receipt, state=3 if active else 5, observed=observed))
        if method == "UpdateConfig":
            if request.expected_resource_version != version:
                raise Aborted()
            version += 1
            loaded = False
            base_rule = request.policy.network_policies.get("base_fixture")
            return Message(version=version, policy_hash=str(version))
        if method == "GetSandboxPolicyStatus":
            assert request.version == version
            loaded = True
            return Message(
                active_version=version,
                revision=NS(version=version, policy_hash=str(version), status=2),
            )
        raise AssertionError(method)

    pb = NS(
        **{
            name: Message
            for name in (
                "GetSandboxRequest",
                "AttachSandboxProviderRequest",
                "DetachSandboxProviderRequest",
                "GetSandboxProviderStatusRequest",
                "UpdateConfigRequest",
                "GetSandboxPolicyStatusRequest",
            )
        },
        PROVIDER_READINESS_STATE_READY=3,
        PROVIDER_READINESS_STATE_REVOKED=5,
        PROVIDER_READINESS_STATE_SUPERSEDED=6,
        POLICY_STATUS_LOADED=2,
        ProviderReadinessState=NS(Name=str),
    )
    client = NS(
        pb=pb,
        rpc=rpc,
        image="image",
        scope="workspace",
        grpc=NS(RpcError=Aborted, StatusCode=NS(ABORTED="ABORTED")),
    )

    def observe(label, host, path, expected, method):
        assert installed and loaded
        host_allowed = (active and host == "allowed") or (
            base_rule is not None and host == base_rule.host
        )
        request_allowed = (
            active and host == "allowed" and path == "/provider" and method == "GET"
        ) or (
            base_rule is not None
            and host == base_rule.host
            and path == base_rule.path
            and method in base_rule.methods
        )
        assert expected == (200 if request_allowed else 403 if host_allowed else "EACCES")
        seen.append(label)
        return {"passed": True}

    if wrong_provider:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            profile._run_cases(
                client, "sandbox", {"id": "sandbox-id"}, "ip", "composition", observe=observe
            )
        assert len(seen) == 2
    else:
        results = profile._run_cases(
            client,
            "sandbox",
            {"id": "sandbox-id"},
            "ip",
            "composition",
            observe=observe,
            record=lambda kind, row: transitions.append(row),
        )
        assert len(results) == len(set(seen)) == 25
        assert "overlap_base_adds_post" in seen
        assert "base_clear_retains_provider_authority" in seen
        assert sum("provider_transition" in row for row in transitions) == 4
        assert sum("policy_loaded" in row for row in transitions) == 5
        assert {"stale_policy_update": "ABORTED"} in transitions
