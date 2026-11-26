"""Saved references and replacement authority must remain independently attributable."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_credential_recovery")


class Message(NS):
    def SerializeToString(self, **kwargs):
        return repr(vars(self)).encode()


def test_saved_reference_survives_launch_environment_change_without_disclosure(probe, tmp_path):
    saved = tmp_path / "reference"
    old = "openshell:resolve:synthetic-original"
    new = "openshell:resolve:synthetic-replacement"

    def run(value, **kwargs):
        return subprocess.run(
            [sys.executable, "-c", probe.reference_program("TEST_REFERENCE", str(saved), **kwargs)],
            env={**os.environ, "TEST_REFERENCE": value},
            capture_output=True,
            text=True,
            timeout=10,
        )

    captured = run(old, capture=True)
    assert captured.returncode == 0 and not captured.stderr
    assert json.loads(captured.stdout) == {
        "reference_sha256": hashlib.sha256(old.encode()).hexdigest()
    }
    observed = run(new)
    assert observed.returncode == 0 and observed.stdout == captured.stdout
    assert old not in observed.stdout and new not in observed.stdout
    overwritten = run(new, capture=True)
    assert overwritten.returncode != 0
    assert saved.read_text() == old
    saved.write_text("unwrapped-secret")
    invalid = run(new)
    assert invalid.returncode != 0
    assert "unwrapped-secret" not in invalid.stdout + invalid.stderr


@pytest.mark.parametrize(
    "fault", [None, "missing", "extra", "host", "token", "body", "expired", "transport"]
)
def test_only_exact_unexpired_issuer_token_and_origin_echo_can_pass(probe, fault):
    request = {
        "host": "owned.test:8443",
        "port": 8443,
        "method": "GET",
        "path": "/provider?case=owned",
        "authorization_sha256": "",
    }
    mint = {"mode": "ok", "authorization_sha256": "issued", "expires_at": time.time() + 300}
    hit = {**request, "authorization_sha256": "issued"}
    observed = {"status": 200, "body": json.dumps(hit)}
    events = [dict(hit)]
    if fault == "missing":
        events.clear()
    elif fault == "extra":
        events.append(dict(hit))
    elif fault == "host":
        events[0]["host"] = "foreign.test:8443"
    elif fault == "token":
        events[0]["authorization_sha256"] = "unknown"
    elif fault == "body":
        observed["body"] = "{}"
    elif fault == "expired":
        mint["expires_at"] = time.time() + 10
    elif fault == "transport":
        observed = {"error": "URLError", "errno": -5}
    if fault:
        with pytest.raises(RuntimeError):
            probe.classify(observed, events, request, [mint])
    else:
        assert probe.classify(observed, events, request, [mint]) == "issued"


@pytest.mark.parametrize("refusal", ["credential_unavailable", "credential_endpoint_mismatch"])
def test_explicit_refusal_requires_empty_origin(probe, refusal):
    response = {
        "status": 500 if refusal == "credential_unavailable" else 403,
        "body": json.dumps({"error": refusal}),
    }
    request = {"authorization_sha256": ""}
    assert probe.classify(response, [], request, []) == refusal
    with pytest.raises(RuntimeError):
        probe.classify(response, [{"unexpected": "traffic"}], request, [])
    with pytest.raises(RuntimeError):
        probe.classify({"status": 500, "body": "{}"}, [], request, [])


def test_raw_authority_payloads_are_fingerprinted_recursively(probe):
    secret = b"synthetic-material"
    raw = {"request_hex": secret.hex(), "items": [{"status_hex": secret.hex()}]}
    result = probe.fingerprint_rows(raw)
    assert result["request_sha256"] == hashlib.sha256(secret).hexdigest()
    assert result["items"][0]["status_size"] == len(secret)
    assert secret.decode() not in json.dumps(result)
    assert secret.hex() not in json.dumps(result)
    assert raw["request_hex"] == secret.hex()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "preexited",
        "shutdown",
        "intent",
        "continuity",
        "replacement",
        "original_descriptor",
        "descriptor",
        "policy",
        "receipt",
        "attest",
    ],
)
def test_restart_requires_physical_replacement_before_new_authority(probe, monkeypatch, failure):
    events, records = [], []
    original = {"runtime": "old"}
    replacement = {"runtime": "new"}
    spec = Message(policy="owned-policy")
    before = Message(observed=NS(process_instance_id="old-boundary"))
    receipt = NS(receipt_id="old-receipt")
    restored = NS(receipt_id="old-receipt" if failure == "receipt" else "new-receipt")
    state = {"restarted": False, "code": 1 if failure == "preexited" else None}
    fixture = NS(owners=[{"sandbox_id": "owned", "name": "owned-name"}], save=lambda: None)
    client = fixture.client = NS(
        scope="workspace",
        pb=NS(
            UpdateConfigRequest=Message,
            GetSandboxProviderStatusRequest=Message,
            ProviderReadinessState=NS(Name=lambda state: state),
        ),
    )
    client.rpc = lambda *args: NS(status=Message(state="PENDING", synthetic="private-payload"))
    authority = probe.CredentialRecovery(
        fixture,
        NS(provider="provider"),
        "192.0.2.1",
        lambda kind, row: records.append((kind, probe.fingerprint_rows(row))),
    )
    authority.wait_provider = lambda *args: before

    def current():
        value = Message(policy="foreign") if failure == "intent" and state["restarted"] else spec
        return NS(spec=value, metadata=NS(resource_version=2))

    authority.current = current

    def stop():
        events.append("stop")
        state["code"] = 9 if failure == "shutdown" else 0

    def start():
        events.append("start")
        state["restarted"] = True
        state["code"] = None

    gateway = NS(stop=stop, start=start, process=NS(poll=lambda: state["code"]))
    monkeypatch.setattr(probe, "wait_gateway", lambda client: events.append("ready"))
    monkeypatch.setattr(
        probe, "snapshot", lambda identity: replacement if state["restarted"] else original
    )

    def descriptor(identity, runtime):
        if runtime == replacement and failure == "descriptor":
            raise RuntimeError("descriptor mismatch")
        return {
            "session_id": "invalid"
            if failure == "original_descriptor"
            else runtime["runtime"] + "-boundary"
        }

    monkeypatch.setattr(probe, "boundary_descriptor", descriptor)

    def continuity(identity, old, new):
        events.append("physical-proof")
        assert (identity, old, new) == ("owned", original, replacement)
        if failure == "replacement":
            raise RuntimeError("old process survived")
        return failure == "continuity"

    monkeypatch.setattr(probe, "runtime_continuity", continuity)

    def submit(method, request):
        events.append("policy")
        assert request.policy == "owned-policy"
        if failure == "policy":
            raise RuntimeError("policy failed")
        return "response"

    authority.submit = submit
    authority.wait_policy = lambda response: events.append("policy-installed")
    authority.provider = lambda attached: (events.append("provider"), restored)[1]

    def attest(*args, **kwargs):
        events.append("attest")
        assert args[:4] == (restored, True, before, replacement)
        assert kwargs["boundary"] == {"session_id": "new-boundary"}
        if failure == "attest":
            raise RuntimeError("stale authority")
        return "accepted"

    authority.attest = attest
    if failure:
        with pytest.raises(RuntimeError):
            authority.replace(gateway, receipt)
        assert not any(row.get("completed") for _, row in records)
    else:
        result = authority.replace(gateway, receipt)
        assert result[-1] == "accepted"
        assert events == [
            "stop",
            "start",
            "ready",
            "physical-proof",
            "policy",
            "policy-installed",
            "provider",
            "attest",
        ]
        assert records[-1][1]["completed"]
    if failure in ("preexited", "shutdown"):
        assert "start" in events and "policy" not in events
    if failure in ("intent", "continuity", "replacement", "descriptor", "original_descriptor"):
        assert "policy" not in events
    text = json.dumps(records)
    assert "private-payload" not in text and b"private-payload".hex() not in text


@pytest.mark.parametrize("failure", [False, True])
def test_mutation_evidence_has_no_serialized_fields_even_on_rpc_failure(probe, failure):
    records = []
    secret = "synthetic-sensitive-field"
    request, response = Message(material=secret), Message(material=secret)

    def rpc(*args):
        if failure:
            raise RuntimeError("RPC failed")
        return response

    fixture = NS(client=NS(rpc=rpc), owners=[{"name": "owned", "sandbox_id": "identity"}])
    authority = probe.CredentialRecovery(
        fixture, NS(), "192.0.2.1", lambda kind, row: records.append(copy.deepcopy(row))
    )
    if failure:
        with pytest.raises(RuntimeError):
            authority.submit("mutation", request)
    else:
        assert authority.submit("mutation", request) is response
    assert records[0]["state"] == "pending"
    assert len(records) == (1 if failure else 2)
    assert secret not in json.dumps(records)
    assert secret.encode().hex() not in json.dumps(records)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "saved_retired",
        "reference_changed",
        "expired",
        "origin",
        "replacement",
        "attestation",
        "detach",
        "cleanup",
    ],
)
def test_complete_matrix_preserves_reference_binding_and_cleanup(
    probe, monkeypatch, tmp_path, fault
):
    state = {"epoch": 0, "attached": True, "replaced": False, "sequence": 0}
    events, hits, mints, snapshots = [], [], [], []
    fixture = NS(
        client=NS(image="image"), owners=[{"name": "owned", "sandbox_id": "identity"}], report={}
    )
    fixture.save = lambda: snapshots.append(copy.deepcopy(fixture.report))

    class Issuer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("issuer-closed")

        def snapshot(self):
            return dict(mints[-1])

        def journal(self):
            return list(mints)

    class Profile:
        env_key = "TEST_RECOVERY_REFERENCE"

        def __init__(self, issuer):
            self.issuer = issuer

        def prepare_workspace(self, client):
            self.configure_refresh(client)
            self.rotate(client)

        def configure_refresh(self, client):
            state["epoch"] += 1
            monkeypatch.setenv(self.env_key, "openshell:resolve:epoch-" + str(state["epoch"]))

        def rotate(self, client):
            mints.append(
                {
                    "mode": "ok",
                    "generation": len(mints) + 1,
                    "authorization_sha256": "token-" + str(len(mints) + 1),
                    "expires_at": time.time() + 300,
                }
            )

        def refresh_status(self, client):
            expires = mints[-1]["expires_at"]
            return Message(status="refreshed", expiration_time=NS(seconds=int(expires), nanos=0))

        def endpoint(self, *args):
            pass

        def cleanup_workspace(self, client):
            events.append("provider-cleanup")
            if fault == "cleanup":
                raise RuntimeError("cleanup failed")

    class Response:
        status = 200

        def __init__(self, hit):
            self.hit = hit

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return json.dumps(self.hit).encode()

    def urlopen(request, **kwargs):
        url = urlsplit(request.full_url)
        reference = request.get_header("Authorization", "").removeprefix("Bearer ")
        refusal = None
        if reference:
            if not state["attached"] or reference != "openshell:resolve:epoch-" + str(
                state["epoch"]
            ):
                refusal = "credential_unavailable"
            elif url.path == "/outside":
                refusal = "credential_endpoint_mismatch"
        if refusal:
            raise urllib.error.HTTPError(
                request.full_url,
                500 if refusal == "credential_unavailable" else 403,
                "refused",
                {},
                io.BytesIO(json.dumps({"error": refusal}).encode()),
            )
        hit = {
            "method": "GET",
            "host": url.netloc,
            "port": url.port,
            "path": url.path + "?" + url.query,
            "authorization_sha256": mints[-1]["authorization_sha256"] if reference else None,
        }
        hits.append(hit)
        if fault == "origin" and state["replaced"]:
            hits.append(dict(hit))
        return Response(hit)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    class Sandbox:
        owner = fixture.owners[0]

        async def exec(self, command, **kwargs):
            code = command[-1].replace("/sandbox/", tmp_path.as_posix() + "/")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(code, {})  # nosec B102 - repository-generated fixture program only
            return NS(exit_code=0, stderr="", stdout=output.getvalue())

        async def dispose(self):
            events.append("physical-cleanup")

    sandbox = Sandbox()

    async def acquire(**kwargs):
        return sandbox

    fixture.acquire = acquire

    class Authority:
        def __init__(self, *args):
            pass

        def current(self):
            return NS(spec=Message(policy="owned"))

        def provider(self, attached):
            state["attached"] = attached
            events.append("attach" if attached else "detach")
            if not attached and fault == "detach":
                raise RuntimeError("detach failed")
            return NS(receipt_id="receipt")

        def replace(self, *args):
            events.append("replace")
            state["replaced"] = True
            if fault == "replacement":
                raise RuntimeError("replacement not proved")
            if fault == "saved_retired":
                state["epoch"] += 1
                monkeypatch.setenv(
                    Profile.env_key, "openshell:resolve:epoch-" + str(state["epoch"])
                )
            if fault == "reference_changed":
                next(tmp_path.glob("*-saved")).write_text("openshell:resolve:foreign")
            if fault == "expired":
                mints[-1]["expires_at"] = time.time() + 1
            return NS(receipt_id="new"), "before", "runtime", "spec", "boundary", 0

        def attest(self, receipt, attached, before, runtime, spec, accepted, **kwargs):
            events.append("attest")
            assert accepted == state["sequence"]
            state["sequence"] += 1
            if fault == "attestation":
                raise RuntimeError("stale authority")
            return state["sequence"]

        def rotation_target(self, receipt):
            events.append("rotation-target")
            return "pinned-target"

        def attest_current(self, target, before, runtime, spec, accepted, **kwargs):
            assert target == "pinned-target"
            return self.attest(None, True, before, runtime, spec, accepted, **kwargs)

    def control(host, path, **kwargs):
        hit = {
            "method": "GET",
            "path": path,
            "host": host + ":8443",
            "port": 8443,
            "authorization_sha256": None,
        }
        hits.append(hit)
        return hit

    monkeypatch.setattr(probe, "TokenIssuer", Issuer)
    monkeypatch.setattr(probe, "HTTPSRefreshProfile", Profile)
    monkeypatch.setattr(probe, "CredentialRecovery", Authority)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("192.0.2.1", 8443))]
    )
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(probe, "control_https", control)
    monkeypatch.setattr(probe, "origin_get", lambda *args: list(hits))

    def rule():
        return NS(binaries=NS(add=lambda **kwargs: None))

    monkeypatch.setattr(
        probe, "fixture_spec", lambda image: NS(policy=NS(network_policies=defaultdict(rule)))
    )
    expected_failure = fault not in (None, "saved_retired")
    if expected_failure:
        with pytest.raises(RuntimeError):
            asyncio.run(probe.measure_credential_recovery(fixture, "192.0.2.1", "ca.pem", object()))
    else:
        asyncio.run(probe.measure_credential_recovery(fixture, "192.0.2.1", "ca.pem", object()))
    report = fixture.report["credential_recovery"]
    assert report["qualification_passed"] is False
    assert (
        "physical-cleanup" in events
        and "provider-cleanup" in events
        and events[-1] == "issuer-closed"
    )
    assert events.index("physical-cleanup") > events.index("detach")
    assert report["provider_and_profile_cleanup_verified"] is (fault != "cleanup")
    if fault not in ("cleanup",):
        assert report["characterization_completed"] is (not expected_failure)
    if not expected_failure:
        assert len(report["cases"]) == 12 and all(row["passed"] for row in report["cases"])
        saved = next(
            row for row in report["cases"] if row["case"] == "saved_reference_after_restoration"
        )
        assert saved["attempts"][0]["result"] == ("credential_unavailable" if fault else "token-2")
        assert len(report["references"]) == 3
    assert "openshell:resolve:" not in json.dumps(snapshots)


def desired(revision=2):
    return Message(
        sandbox_id="owned",
        sandbox="owned-name",
        provider_id="provider-id",
        attachment_epoch="epoch",
        provider_env_revision=revision,
        provider_resource_version=revision,
        config_revision=7,
        policy_hash="policy",
    )


@pytest.mark.parametrize(
    "fault",
    [None, "attachment_epoch", "config_revision", "policy_hash", "provider_id", "unchanged"],
)
def test_rotation_pins_only_a_new_environment_target(probe, monkeypatch, fault):
    records = []
    authority = probe.CredentialRecovery(
        NS(client=NS(), owners=[{"name": "owned-name", "sandbox_id": "owned"}]),
        NS(),
        "192.0.2.1",
        lambda kind, row: records.append(row),
    )
    previous, current = desired(1), desired(1 if fault == "unchanged" else 2)
    if fault in ("attachment_epoch", "config_revision", "policy_hash", "provider_id"):
        setattr(current, fault, "foreign")
    authority.current_status = lambda: NS(receipt=NS(desired=current))
    ticks = iter([0, 31])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    if fault:
        with pytest.raises(RuntimeError):
            authority.rotation_target(NS(desired=previous))
    else:
        assert authority.rotation_target(NS(desired=previous)) == current
        assert records[0]["previous_env_revision"] == 1
        assert records[0]["current_env_revision"] == 2


@pytest.mark.parametrize(
    "fault", [None, "target", "receipt", "sequence", "identity", "descriptor", "runtime"]
)
def test_current_target_requires_exact_installed_and_advancing_evidence(probe, monkeypatch, fault):
    target = desired()
    receipt = NS(desired=target)

    def status(prefix, sequence):
        return Message(
            receipt=receipt,
            state="READY",
            network_instance_id=prefix + "-network",
            observed=NS(
                session_id=prefix + "-session",
                process_instance_id="boundary",
                sequence=sequence,
                attachment_epoch=target.attachment_epoch,
                provider_env_revision=target.provider_env_revision,
                config_revision=target.config_revision,
                policy_hash=target.policy_hash,
                credentials_installed=True,
                policy_active=True,
                launch_environment_installed=True,
            ),
        )

    before, accepted, current = status("old", 1), status("new", 2), status("new", 3)
    current.receipt = copy.deepcopy(receipt)
    if fault == "target":
        current.receipt.desired.provider_env_revision = 9
        current.observed.provider_env_revision = 9
    elif fault == "receipt":
        current.observed.provider_env_revision = 1
    elif fault == "sequence":
        current.observed.sequence = 2
    elif fault == "identity":
        current.network_instance_id = "foreign"
    fixture = NS(
        client=NS(pb=NS(PROVIDER_READINESS_STATE_READY="READY")),
        owners=[{"name": "owned-name", "sandbox_id": "owned"}],
    )
    records = []
    authority = probe.CredentialRecovery(
        fixture, NS(), "192.0.2.1", lambda kind, row: records.append(row)
    )
    authority.current_status = lambda: current

    def stable(*args):
        if fault == "runtime":
            raise RuntimeError("replacement changed")

    authority.stable = stable
    boundary = {"session_id": "boundary"}
    monkeypatch.setattr(
        probe, "boundary_descriptor", lambda *args: {} if fault == "descriptor" else boundary
    )
    ticks = iter([0, 31])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    if fault:
        with pytest.raises(RuntimeError):
            authority.attest_current(target, before, {}, "spec", accepted, boundary=boundary)
    else:
        assert (
            authority.attest_current(target, before, {}, "spec", accepted, boundary=boundary)
            is current
        )
    assert records and "status_hex" not in records[0]
    assert records[0]["accepted_sequence"] == 2


@pytest.mark.parametrize(
    "fault", [None, "workspace", "provider", "sandbox_id", "sandbox", "provider_id"]
)
def test_current_status_selector_binds_owned_identity(probe, fault):
    target = desired()
    receipt = NS(workspace="workspace", provider="provider", desired=target)
    if fault:
        setattr(receipt if fault in ("workspace", "provider") else target, fault, "foreign")

    def rpc(method, request):
        assert method == "GetSandboxProviderStatus"
        assert vars(request) == {
            "sandbox": "owned-name",
            "provider": "provider",
            "workspace_scope": scope,
        }
        return NS(status=NS(receipt=receipt))

    scope = NS(workspace="workspace")
    fixture = NS(
        client=NS(rpc=rpc, scope=scope, pb=NS(GetSandboxProviderStatusRequest=NS)),
        owners=[{"name": "owned-name", "sandbox_id": "owned"}],
    )
    authority = probe.CredentialRecovery(
        fixture, NS(provider="provider", provider_id="provider-id"), "192.0.2.1", lambda *args: None
    )
    authority.current = lambda: None
    if fault:
        with pytest.raises(RuntimeError):
            authority.current_status()
    else:
        assert authority.current_status().receipt is receipt
