"""Continuous credential evidence rejects process replacement, replay and expiry ambiguity."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    return importlib.import_module("openshell_credential_lifecycle")


def identity():
    return {"pid": 123, "start": "456", "nonce": "owned-process"}


def test_protobuf_evidence_keeps_only_digest_and_byte_length(probe):
    payload = b"synthetic-secret-material\x00\xff"
    result = probe.protobuf_evidence(NS(SerializeToString=lambda: payload), "profile")
    assert result == {
        "profile_sha256": hashlib.sha256(payload).hexdigest(),
        "profile_size": len(payload),
    }


@pytest.mark.parametrize(
    "fault",
    [None, "old", "late", "owner", "host", "port", "generation", "message", "malformed"],
)
def test_generation_evidence_is_exact_scoped_and_secret_free(probe, fault):
    from datetime import datetime

    stamp = int(datetime.fromisoformat("2026-11-15T13:38:46+00:00").timestamp()) * 10**9
    event = {
        "container": {"uid": "owned"},
        "dst_endpoint": {"domain": "owned.test", "port": 8443},
        "message": "L7 tunnel closed before inspection because policy changed: "
        "policy generation is stale [captured_generation:2 current_generation:3]",
        "unmapped": {"authorization": "Bearer private-token", "hostname": "internal.example"},
    }
    if fault == "owner":
        event["container"]["uid"] = "replacement"
    elif fault == "host":
        event["dst_endpoint"]["domain"] = "other.test"
    elif fault == "port":
        event["dst_endpoint"]["port"] = 443
    elif fault == "generation":
        event["message"] = event["message"].replace("current_generation:3", "current_generation:2")
    elif fault == "message":
        event["message"] += " private-token"
    elif fault == "malformed":
        event["container"] = None
    log = "2026-11-15T13:38:46.662804935Z 2026-11-15T13:38:46.662Z OCSF-JSON " + json.dumps(event)
    started = stamp + (700_000_000 if fault == "old" else 600_000_000)
    ended = stamp + (650_000_000 if fault == "late" else 800_000_000)
    signals = probe.generation_closures(log, "owned", "owned.test", started, ended)
    assert bool(signals) is (fault is None)
    if fault is None:
        assert signals == [
            {
                "signal": "relay_closed_for_policy_generation_change",
                "time_unix_ns": stamp + 662_804_935,
                "captured_generation": 2,
                "current_generation": 3,
            }
        ]
    assert "private-token" not in json.dumps(signals)
    assert "internal.example" not in json.dumps(signals)
    assert probe.generation_closures("OCSF-JSON {bad}", "owned", "owned.test", started, ended) == []


@pytest.mark.parametrize("fault", [None, "replacement", "missing", "duplicate", "exit", "timeout"])
def test_generation_capture_requires_owned_supervisor_and_redacts_failures(
    probe, monkeypatch, fault
):
    owner = {"sandbox_id": "owned", "containers": ["supervisor"]}
    container = {
        "Id": "replacement" if fault == "replacement" else "supervisor",
        "Labels": {"openshell.ai/isolation-role": "supervisor"},
    }
    monkeypatch.setattr(
        probe,
        "snapshot",
        lambda identity: {
            "containers": []
            if fault == "missing"
            else [container] * (2 if fault == "duplicate" else 1)
        },
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, 10, output="private-token")
        return NS(returncode=1 if fault == "exit" else 0, stdout="private-token", stderr="")

    monkeypatch.setattr(probe.subprocess, "run", run)
    if fault is None:
        assert probe.generation_interruption(owner, "owned.test", 0, 1) == []
    else:
        with pytest.raises(RuntimeError) as caught:
            probe.generation_interruption(owner, "owned.test", 0, 1)
        assert "private-token" not in str(caught.value)
    assert len(calls) == (0 if fault in ("replacement", "missing", "duplicate") else 1)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "baseline",
        "scope",
        "rejected",
        "version",
        "changed",
        "readback",
        "readback_scope",
        "lost_ack",
    ],
)
def test_profile_path_edit_is_conditional_exact_and_never_replayed(probe, fault):
    from copy import deepcopy

    class Message(NS):
        def CopyFrom(self, other):
            vars(self).update(deepcopy(vars(other)))

        def SerializeToString(self):
            return repr(self).encode()

    before = Message(
        id="profile",
        description="synthetic-secret-material",
        resource_version=7,
        source="user",
        scope="workspace",
        endpoints=[NS(path="/provider/**", rules=[NS(allow=NS(path="/provider/**"))])],
        credentials=[NS(name="synthetic", auth_style="bearer")],
    )
    state = NS(stored=deepcopy(before), calls=[], records=[])
    if fault == "baseline":
        state.stored.endpoints[0].path = "/replacement"
    if fault == "scope":
        state.stored.scope = "platform"

    def rpc(method, request):
        state.calls.append((method, deepcopy(request)))
        if method == "GetProviderProfile":
            result = deepcopy(state.stored)
            result.source = "user"
            result.scope = "platform" if fault == "scope" else "workspace"
            if fault == "readback" and len(state.calls) > 1:
                result.resource_version += 1
            if fault == "readback_scope" and len(state.calls) > 1:
                result.scope = "platform"
            return NS(profile=result)
        assert method == "UpdateProviderProfiles"
        assert request.expected_resource_version == 7
        assert request.id == request.profile.profile.id == "profile"
        assert request.request_id
        assert request.workspace_scope == "owned"
        assert request.profile.profile.source == request.profile.profile.scope == ""
        state.stored = deepcopy(request.profile.profile)
        state.stored.resource_version = 7 if fault == "version" else 8
        if fault == "changed":
            state.stored.credentials[0].auth_style = "basic"
        if fault == "lost_ack":
            raise ConnectionError("committed edit acknowledgement lost")
        return Message(updated=fault != "rejected", profile=deepcopy(state.stored))

    client = NS(
        pb=NS(
            GetProviderProfileRequest=Message,
            UpdateProviderProfilesRequest=Message,
            ProviderProfileImportItem=Message,
        ),
        scope="owned",
        rpc=rpc,
    )
    profile = object.__new__(probe.HTTPSRefreshProfile)
    profile.profile_id, profile.endpoint_path = "profile", "/provider/**"

    def record(kind, row):
        state.records.append((kind, deepcopy(row)))

    if fault is None:
        profile.update_path(client, "/provider/other", record)
        assert state.stored.endpoints[0].path == "/provider/other"
        assert state.stored.endpoints[0].rules[0].allow.path == "/provider/other"
        assert state.stored.credentials == before.credentials
        assert before.endpoints[0].path == "/provider/**"
        assert profile.endpoint_path == "/provider/other"
        assert state.records[-1][1]["stored_profile_revision"] == 8
        assert not state.records[-1][1]["guest_installation_confirmed"]
    else:
        with pytest.raises((RuntimeError, ConnectionError)):
            profile.update_path(client, "/provider/other", record)
        assert profile.endpoint_path == "/provider/**"
        assert not any("stored_profile_revision" in row for _, row in state.records)
    assert sum(method == "UpdateProviderProfiles" for method, _ in state.calls) == (
        0 if fault in ("baseline", "scope") else 1
    )
    retained = json.dumps(state.records)
    assert "synthetic-secret-material" not in retained
    assert b"synthetic-secret-material".hex() not in retained
    assert "_hex" not in retained
    if fault is None:
        pending, acknowledged, stored = (row for _, row in state.records)
        request = state.calls[1][1].SerializeToString()
        assert pending["request_sha256"] == hashlib.sha256(request).hexdigest()
        assert pending["request_size"] == len(request)
        assert len(acknowledged["profile_update_acknowledged"]["response_sha256"]) == 64
        assert acknowledged["profile_update_acknowledged"]["response_size"] > 0
        assert len(stored["profile_sha256"]) == 64
        assert stored["profile_size"] > 0


@pytest.mark.parametrize("change", ["pid", "start", "nonce", "sequence", "reference_sha256"])
def test_replacement_replay_or_reference_change_cannot_prove_continuity(probe, change):
    ready = {"identity": identity(), "reference_sha256": "old-reference"}
    result = {**ready, "identity": identity(), "sequence": 3}
    assert probe.continuous_result(ready, result, 3, "old-reference")
    if change in identity():
        result["identity"][change] = "replacement"
    else:
        result[change] = "replacement"
    assert not probe.continuous_result(ready, result, 3, "old-reference")


@pytest.mark.parametrize("remaining", [-1, 0, 34, 35])
def test_expiry_cannot_be_misattributed_to_revocation(probe, remaining):
    with pytest.raises(RuntimeError, match="expiry"):
        probe.unexpired({"expires_at": 100 + remaining}, now=100)
    probe.unexpired({"expires_at": 136}, now=100)


@pytest.mark.parametrize(
    "after", [[], [{"path": "replacement"}], [{"path": "new"}, {"path": "old"}]]
)
def test_truncated_or_reordered_origin_journal_never_proves_no_hit(probe, after):
    with pytest.raises(RuntimeError, match="continuity"):
        probe.journal_delta([{"path": "old"}], after)


def test_propagation_only_accepts_attributed_issuer_generations(probe):
    request = {"method": "GET", "path": "/provider/ok?case=1", "host": "owned:8443", "port": 8443}
    event = {**request, "authorization_sha256": "old-generation"}
    observed = {"status": 200, "body": json.dumps(event)}
    minted = [{"mode": "ok", "authorization_sha256": "old-generation"}]
    assert probe.propagation_observation(observed, [event], request, minted)
    assert not probe.propagation_observation(observed, [event], request, [])
    assert not probe.propagation_observation(observed, [event, event], request, minted)
    assert not probe.propagation_observation(
        observed, [{**event, "path": "/wrong"}], request, minted
    )
    refused = {"status": 500, "body": '{"error":"credential_unavailable"}'}
    assert probe.propagation_observation(refused, [], request, minted)
    assert not probe.propagation_observation(refused, [event], request, minted)
    assert not probe.propagation_observation({"error": "TimeoutError"}, [], request, minted)


@pytest.mark.parametrize("uncertain_write", [False, True])
def test_real_guest_keeps_launch_reference_across_new_launches(probe, tmp_path, uncertain_write):
    """Exercise generated guest code and file controls in a real subprocess, without OpenShell."""
    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            digest = hashlib.sha256(self.headers.get("Authorization", "").encode()).hexdigest()
            events.append(digest)
            body = json.dumps({"authorization_sha256": digest}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    launch = {**os.environ, "QUALIFICATION_TOKEN": "openshell:resolve:first"}
    processes = []
    failures = []
    retries = []

    class LocalSandbox:
        async def exec(self, command, **kwargs):
            code = command[-1]
            # This checks the file protocol on Windows, not Linux process attestation.
            if sys.platform == "win32":
                code = code.replace(
                    "Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]",
                    "'local-test-start'",
                )
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                env=dict(launch),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(process)
            try:
                out, err = await asyncio.to_thread(process.communicate, timeout=20)
                return NS(exit_code=process.returncode, stdout=out.decode(), stderr=err.decode())
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.communicate)

        async def read_file(self, path, **kwargs):
            return Path(path).read_bytes()

        async def write_file(self, path, content, **kwargs):
            Path(path).write_text(content, encoding="utf-8")
            if uncertain_write and not failures:
                failures.append(content)
                raise EOFError("write acknowledgement lost")

    async def scenario():
        guest = probe.ContinuousGuest(LocalSandbox(), "QUALIFICATION_TOKEN", record=retries.append)
        guest.directory = str(tmp_path / "guest")
        try:
            ready = await guest.start()
            url = f"http://127.0.0.1:{server.server_port}/provider/ok"
            first = await guest.request(url)
            launch["QUALIFICATION_TOKEN"] = "openshell:resolve:second"
            fresh = await guest.capture_current()
            second = await guest.request(url)
            explicit = await guest.request(url, "current")
            assert (
                first["identity"] == second["identity"] == explicit["identity"] == ready["identity"]
            )
            assert first["reference_sha256"] == second["reference_sha256"] != fresh
            assert explicit["reference_sha256"] == fresh
            assert events[0] == events[1] != events[2]
            launch["QUALIFICATION_TOKEN"] = "openshell:resolve:third"
            edited = await guest.capture_current("edited")
            preserved = await guest.request(url, "current")
            narrowed = await guest.request(url, "edited")
            original = await guest.request(url)
            assert preserved["reference_sha256"] == fresh != edited
            assert narrowed["reference_sha256"] == edited
            assert original["reference_sha256"] == first["reference_sha256"]
            assert all(
                row["identity"] == ready["identity"] for row in (preserved, narrowed, original)
            )
            assert events[2] == events[3] != events[4]
            assert events[0] == events[5]
            assert len(processes) == 3
            assert len(events) == 6
            assert retries == (
                [{"operation": "write", "sequence": 1, "error": "EOFError"}]
                if uncertain_write
                else []
            )
            evidence = json.dumps([ready, first, second, explicit, preserved, narrowed, original])
            assert "openshell:resolve:" not in evidence
        finally:
            await guest.close()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert all(process.poll() == 0 for process in processes)


def test_early_guest_exit_cannot_be_read_as_a_refusal(probe):
    class Sandbox:
        async def exec(self, *args, **kwargs):
            return NS(exit_code=1, stdout="", stderr="missing reference")

        async def read_file(self, *args, **kwargs):
            raise FileNotFoundError

    async def scenario():
        guest = probe.ContinuousGuest(Sandbox(), "TOKEN")
        with pytest.raises(RuntimeError, match="exited before observation"):
            await guest.start()
        with pytest.raises(RuntimeError, match="guest failed"):
            await guest.close()

    asyncio.run(scenario())


def test_issuer_journal_is_an_independent_copy(probe):
    with probe.TokenIssuer() as issuer:
        issuer.events.append({"mode": "ok", "generation": 1})
        retained = issuer.journal()
        retained[0]["generation"] = 999
        retained.append({"mode": "unrelated"})
        assert issuer.journal() == [{"mode": "ok", "generation": 1}]


@pytest.mark.parametrize("lost_detach_ack", [False, True])
def test_transition_journal_and_cleanup_survive_guest_start_failure(
    probe, monkeypatch, tmp_path, lost_detach_ack
):
    from collections import defaultdict

    cleanup = []
    origin = []
    host = "allowed.10.23.45.67.nip.io"
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(0, 0, 0, 0, ("10.23.45.67", 8443))]
    )
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())

    def control(host, path, **kwargs):
        row = {
            "method": "GET",
            "path": path,
            "host": host + ":8443",
            "port": 8443,
            "authorization_sha256": None,
        }
        origin.append(row)
        return row

    monkeypatch.setattr(probe, "control_https", control)
    monkeypatch.setattr(probe, "origin_get", lambda *args: list(origin))

    def rule():
        return NS(binaries=NS(add=lambda **kwargs: None))

    monkeypatch.setattr(
        probe, "fixture_spec", lambda image: NS(policy=NS(network_policies=defaultdict(rule)))
    )

    class Profile:
        def __init__(self, issuer):
            self.profile_id, self.provider, self.provider_id, self.env_key = (
                "profile",
                "provider",
                "physical-provider",
                "TOKEN",
            )

        def prepare_workspace(self, client):
            pass

        def endpoint(self, *args):
            pass

        def cleanup_workspace(self, client):
            cleanup.append("provider-profile")

    class Transition:
        def __init__(self, fixture, profile, ip, record):
            self.record = record
            self.identity = "physical-sandbox"

        def provider(self, attached):
            method = "AttachSandboxProvider" if attached else "DetachSandboxProvider"
            row = {"method": method, "state": "pending"}
            self.record("mutations", row)
            row["state"] = "acknowledged"
            self.record("barriers", {"provider_state": "READY" if attached else "REVOKED"})
            if not attached:
                cleanup.append("detach")
                if lost_detach_ack:
                    row["state"] = "unknown"
                    raise RuntimeError("detach acknowledgement lost")

    class Guest:
        def __init__(self, sandbox, key, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("guest startup refused")

        async def close(self):
            cleanup.append("guest")

    owner = {"sandbox_id": "physical-sandbox"}

    async def acquire(**kwargs):
        async def delete():
            await dispose(owner)

        return NS(owner=owner, dispose=delete)

    async def dispose(value):
        assert value == owner
        cleanup.append("sandbox")

    monkeypatch.setattr(probe, "HTTPSRefreshProfile", Profile)
    monkeypatch.setattr(probe, "Transitions", Transition)
    monkeypatch.setattr(probe, "ContinuousGuest", Guest)
    fixture = NS(
        report={},
        save=lambda: None,
        client=NS(image="image"),
        owners=[owner],
        disposed=set(),
        acquire=acquire,
        dispose=dispose,
    )
    failure = "detach acknowledgement lost" if lost_detach_ack else "guest startup refused"
    with pytest.raises(RuntimeError, match=failure):
        asyncio.run(probe.measure_credentials(fixture, "10.23.45.67", tmp_path / "ca.pem"))
    evidence = fixture.report["credentials"]
    assert evidence["controls"][0]["host"] == host + ":8443"
    assert evidence["mutations"] == [
        {"method": "AttachSandboxProvider", "state": "acknowledged"},
        {
            "method": "DetachSandboxProvider",
            "state": "unknown" if lost_detach_ack else "acknowledged",
        },
    ]
    assert evidence["barriers"] == [{"provider_state": "READY"}, {"provider_state": "REVOKED"}]
    assert not evidence["characterization_completed"]
    assert not evidence["qualification_passed"]
    assert evidence["provider_and_profile_cleanup_verified"]
    assert cleanup == ["detach", "guest", "sandbox", "provider-profile"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_automatic_and_final_cleanup_share_one_detach_and_delete(probe, uncertain):
    events = []

    def detach(attached):
        assert attached is False
        events.append("detach")
        if uncertain:
            raise RuntimeError("detach acknowledgement lost")

    async def delete():
        events.append("delete")

    async def scenario():
        sandbox = NS(dispose=delete)
        cleanup = probe.ProviderCleanup(sandbox, NS(provider=detach))
        results = await asyncio.gather(sandbox.dispose(), cleanup.dispose(), return_exceptions=True)
        if uncertain:
            assert all(isinstance(result, RuntimeError) for result in results)
            with pytest.raises(RuntimeError, match="acknowledgement lost"):
                await cleanup.detach()
        else:
            assert results == [None, None]
            await cleanup.detach()

    asyncio.run(scenario())
    assert events == ["detach", "delete"]


def test_control_retry_budget_is_bounded_and_retained(probe, monkeypatch):
    times = iter([0, 1, 16])
    records = []
    monkeypatch.setattr(probe, "time", NS(monotonic=lambda: next(times)))

    async def failure():
        raise EOFError("control unavailable")

    async def pause(seconds):
        pass

    async def scenario():
        guest = probe.ContinuousGuest(None, "TOKEN", record=records.append)
        monkeypatch.setattr(probe.asyncio, "sleep", pause)
        with pytest.raises(EOFError):
            await guest.control_io("read", failure)

    asyncio.run(scenario())
    assert records == [{"operation": "read", "sequence": 0, "error": "EOFError"}] * 2


def test_control_files_use_one_owned_session_and_bound_paths_and_reads(probe, monkeypatch):
    import io
    from contextlib import contextmanager

    calls = []

    class Session:
        def open(self, path, mode):
            calls.append((path, mode))
            return io.BytesIO(b'{"ready":true}')

    @contextmanager
    def connection(client, name, identity, *, seconds):
        calls.append((name, identity, seconds))
        try:
            yield Session()
        finally:
            calls.append("closed")

    monkeypatch.setattr(probe, "native_sftp", connection)
    directory = "/sandbox/credential-" + "a" * 32
    sandbox = NS(client=object(), owner={"name": "owned", "sandbox_id": "physical"})
    files = probe.ControlFiles(sandbox, directory)

    async def scenario():
        for _ in range(2):
            assert (
                await files.read_file(
                    directory + "/ready.json", working_directory="/sandbox", max_bytes=100
                )
                == b'{"ready":true}'
            )
        with pytest.raises(RuntimeError, match="capture budget"):
            await files.read_file(
                directory + "/ready.json", working_directory="/sandbox", max_bytes=1
            )
        for path in (
            directory + "/../escape",
            directory + "-other/ready.json",
            "/sandbox/elsewhere/ready.json",
        ):
            with pytest.raises(ValueError, match="outside"):
                await files.read_file(path, working_directory="/sandbox", max_bytes=100)
        await files.write_file(directory + "/command-1.json", "{}", working_directory="/sandbox")

    try:
        asyncio.run(scenario())
    finally:
        files.close()
    assert calls[0] == ("owned", "physical", 620)
    assert calls[-1] == "closed"
    assert len(calls) == 6


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_persistent_control_session_requires_finite_budget(probe, seconds):
    with pytest.raises(ValueError, match="positive and finite"):
        with probe.native_sftp(None, "owned", "physical", seconds=seconds):
            pytest.fail("invalid lifetime admitted")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "reset",
        "unattributed",
        "final",
        "expiry_before",
        "expiry_after",
        "profile_no_revocation",
        "profile_transport",
        "profile_expiry_before",
        "profile_expiry_after",
        "profile_retained_old_reference",
        "profile_same_reference",
        "restore_no_revive",
        "restore_narrowed_reference",
        "generation",
        "generation_missing",
        "generation_multiple",
        "generation_origin",
        "generation_timeout",
        "generation_late_success",
        "generation_expiry",
    ],
)
def test_measurement_reconciles_idle_intervals_and_brackets_old_epoch_expiry(
    probe, monkeypatch, tmp_path, fault
):
    from collections import defaultdict
    from urllib.parse import urlsplit

    origin, cleanup = [], []
    state = NS(
        now=1000,
        clock=0,
        attached=False,
        attaches=0,
        epoch=1,
        requests=0,
        path="/provider/**",
        pending=False,
        restoring=False,
    )
    monkeypatch.setattr(probe.time, "time", lambda: state.now)
    monotonic = probe.time.monotonic
    monkeypatch.setattr(probe.time, "monotonic", lambda: monotonic() + state.clock)
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *a: [(0, 0, 0, 0, ("10.23.45.67", 8443))]
    )
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kw: object())
    monkeypatch.setattr(probe, "origin_get", lambda *a: list(origin))
    evidence_reads = []

    def generation_interruption(owner, host, started, ended):
        evidence_reads.append((started, ended))
        if fault == "generation_timeout":
            state.clock += 31
        if fault == "generation_expiry":
            state.now = state.issuer.latest["expires_at"] - 34
        return (
            []
            if fault == "generation_missing"
            else [{"signal": "closure"}] * (2 if fault == "generation_multiple" else 1)
        )

    monkeypatch.setattr(probe, "generation_interruption", generation_interruption)
    monkeypatch.setattr(probe, "snapshot", lambda identity: {"owned": identity})
    monkeypatch.setattr(
        probe, "runtime_continuity", lambda identity, before, after: before == after
    )
    monkeypatch.setattr(probe, "session_sample", lambda *args: sample_session("one"))

    def control(host, path, **kwargs):
        row = dict(
            method="GET", path=path, host=host + ":8443", port=8443, authorization_sha256=None
        )
        origin.append(row)
        return row

    monkeypatch.setattr(probe, "control_https", control)
    monkeypatch.setattr(
        probe,
        "fixture_spec",
        lambda image: NS(
            policy=NS(
                network_policies=defaultdict(
                    lambda: NS(name="", binaries=NS(add=lambda **kw: None))
                )
            )
        ),
    )

    class Retryable(Exception):
        def code(self):
            return "unavailable"

    class Issuer:
        def __init__(self):
            self.events, self.latest, self.mode = [], {}, "ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def configure(self, *, mode="ok", **kwargs):
            self.mode = mode

        def snapshot(self):
            generation = self.latest.get("generation", 0) + 1
            self.latest = dict(
                generation=generation,
                mode="ok",
                at=state.now + generation,
                expires_at=state.now + 300,
                authorization_sha256=str(generation),
            )
            self.events.append(self.latest)
            if generation == 4:
                state.recovered = self.latest
                if fault == "reset":
                    origin.clear()
                elif fault == "unattributed":
                    origin.append({"path": "/unexpected"})
            return dict(self.latest)

        def journal(self):
            return list(self.events)

    class Profile:
        def __init__(self, issuer):
            state.issuer = issuer
            self.profile_id, self.provider, self.provider_id, self.env_key = (
                "profile",
                "provider",
                "id",
                "TOKEN",
            )

        def prepare_workspace(self, client):
            pass

        def endpoint(self, *args):
            pass

        def configure_refresh(self, client):
            state.epoch += 1

        def rotate(self, client):
            if state.issuer.mode != "ok":
                state.issuer.events.append(dict(mode=state.issuer.mode, at=state.now))
                raise Retryable()

        def refresh_status(self, client):
            return NS(
                status="refreshed",
                expiration_time=NS(seconds=state.issuer.latest["expires_at"], nanos=0),
                failure_code="oauth_token_endpoint_retryable",
                HasField=lambda name: True,
                SerializeToString=lambda: b"synthetic-refresh-error-material",
            )

        def cleanup_workspace(self, client):
            cleanup.append("profile")

        def update_path(self, client, path, record):
            state.path = path
            state.pending = fault is None
            state.restoring = path == "/provider/**"
            if fault == "profile_expiry_before":
                state.now = state.issuer.latest["expires_at"] - 34

    class Transition:
        def __init__(self, *args):
            self.identity = "sandbox"

        def current(self):
            pass

        def provider(self, attached):
            state.attached = attached
            if attached:
                state.attaches += 1
                if state.attaches == 2 and fault == "expiry_before":
                    state.now = state.recovered["expires_at"] - 34
            else:
                cleanup.append("detach")

    class Guest:
        def __init__(self, *args, **kwargs):
            self.ready = dict(identity=identity(), reference_sha256="original")

        async def start(self):
            return self.ready

        async def capture_current(self, slot="current"):
            return "current" if fault == "profile_same_reference" else slot

        async def request(self, url, source):
            state.requests += 1
            target = urlsplit(url)
            narrowing = state.path == "/provider/other" and target.path == "/provider/ok"
            restoring = state.restoring
            delayed = state.pending
            state.pending = False
            if narrowing and fault == "profile_expiry_after":
                state.now = state.issuer.latest["expires_at"] - 34
            if narrowing and fault == "profile_transport":
                return dict(guest={"error": "URLError", "errno": -5})
            if narrowing and str(fault).startswith("generation") and not evidence_reads:
                if fault == "generation_origin":
                    origin.append({"path": "/unexpected"})
                return dict(guest={"error": "RemoteDisconnected", "errno": None})
            if narrowing and fault == "generation_late_success" and evidence_reads:
                state.clock += 31
            if (
                (narrowing and fault == "profile_no_revocation")
                or (restoring and fault == "restore_no_revive")
                or (
                    fault == "profile_retained_old_reference"
                    and state.path == "/provider/other"
                    and target.path == "/provider/other"
                    and source == "current"
                )
            ):
                state.clock += 31
            if source != "none" and (
                not state.attached or source == "original" and state.epoch > 1
            ):
                if state.attaches == 2 and fault == "expiry_after":
                    state.now = state.recovered["expires_at"] - 34
                response = dict(status=500, body='{"error":"credential_unavailable"}')
            elif target.path == "/outside":
                response = dict(status=403, body='{"error":"credential_endpoint_mismatch"}')
            elif source != "none" and (
                narrowing
                and not delayed
                and fault != "profile_no_revocation"
                or restoring
                and (delayed or fault == "restore_no_revive")
            ):
                response = dict(status=403, body='{"error":"credential_endpoint_mismatch"}')
            elif (
                source == "current"
                and state.path == "/provider/other"
                and fault not in ("profile_no_revocation", "profile_retained_old_reference")
                or source == "edited"
                and restoring
                and fault != "restore_narrowed_reference"
            ):
                response = dict(status=500, body='{"error":"credential_unavailable"}')
            else:
                row = dict(
                    method="GET",
                    path=target.path + "?" + target.query,
                    host=target.netloc,
                    port=8443,
                    authorization_sha256=None
                    if source == "none"
                    else state.issuer.latest["authorization_sha256"],
                )
                origin.append(row)
                response = dict(status=200, body=json.dumps(row))
            return dict(guest=response)

        async def close(self):
            cleanup.append("guest")
            if fault == "final":
                origin.clear()

    owner = {"sandbox_id": "sandbox"}

    async def delete():
        cleanup.append("sandbox")

    async def acquire(**kwargs):
        return NS(owner=owner, dispose=delete)

    async def dispose(owner):
        await delete()

    for name, value in (
        ("TokenIssuer", Issuer),
        ("HTTPSRefreshProfile", Profile),
        ("Transitions", Transition),
        ("ContinuousGuest", Guest),
    ):
        monkeypatch.setattr(probe, name, value)
    fixture = NS(
        report={},
        save=lambda: None,
        client=NS(
            image="image", grpc=NS(RpcError=Retryable, StatusCode=NS(UNAVAILABLE="unavailable"))
        ),
        owners=[owner],
        disposed=set(),
        acquire=acquire,
        dispose=dispose,
    )
    if fault is None or fault == "generation":
        asyncio.run(probe.measure_credentials(fixture, "10.23.45.67", tmp_path / "ca.pem"))
        assert state.requests == (23 if fault == "generation" else 24)
        assert fixture.report["credentials"]["characterization_completed"]
        assert (
            fixture.report["credentials"]["worker_refresh_session_window"]["outcome"]
            == "no_disconnect_observed"
        )
        retained = json.dumps(fixture.report["credentials"])
        assert "synthetic-refresh-error-material" not in retained
        assert b"synthetic-refresh-error-material".hex() not in retained
        statuses = [
            row for row in fixture.report["credentials"]["barriers"] if "status_sha256" in row
        ]
        assert len(statuses) >= 5
        assert all(
            row["status_sha256"] == hashlib.sha256(b"synthetic-refresh-error-material").hexdigest()
            and row["status_size"] == len(b"synthetic-refresh-error-material")
            for row in statuses
        )
        assert any("retryable_failure" in row for row in statuses)
        cases = {row["case"]: row for row in fixture.report["credentials"]["cases"]}
        for name in (
            "profile_narrowing_revokes_removed_path",
            "profile_restoration_revives_same_current_reference",
        ):
            expected = (
                [True]
                if fault == "generation"
                and name == "profile_restoration_revives_same_current_reference"
                else [False, True]
            )
            assert [attempt["matched"] for attempt in cases[name]["attempts"]] == expected
        if fault == "generation":
            assert len(evidence_reads) == 1
            assert cases["profile_narrowing_revokes_removed_path"]["attempts"][0][
                "generation_interruptions"
            ] == [{"signal": "closure"}]
    else:
        match = (
            "expiry"
            if "expiry" in fault
            else "did not change the reference"
            if fault == "profile_same_reference"
            else "unexpected continuous"
            if fault
            in (
                "profile_no_revocation",
                "profile_retained_old_reference",
                "restore_no_revive",
                "restore_narrowed_reference",
                "generation_timeout",
                "generation_late_success",
            )
            else "continuity|unattributed"
        )
        with pytest.raises(RuntimeError, match=match):
            asyncio.run(probe.measure_credentials(fixture, "10.23.45.67", tmp_path / "ca.pem"))
        assert not fixture.report["credentials"]["characterization_completed"]
        if fault == "expiry_before":
            assert state.requests == 11
        elif fault == "expiry_after":
            assert state.requests == 12
    assert cleanup[-3:] == ["detach", "sandbox", "profile"] or cleanup[-4:] == [
        "detach",
        "guest",
        "sandbox",
        "profile",
    ]
    assert fixture.report["credentials"]["provider_and_profile_cleanup_verified"]


def sample_session(session, sequence=1, *, reason="none", installed=True):
    return dict(
        session=session,
        sequence=sequence,
        network="network",
        process="process",
        reason=reason,
        installed=installed,
    )


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (None, "reconnected"),
        ("same", "disconnect_without_new_session"),
        ("no_gap", "session_changed_without_observed_disconnect"),
        ("lease", "session_changed_without_observed_disconnect"),
        ("before", "installation_unconfirmed"),
        ("process", "runtime_identity_changed"),
        ("request", "session_changed_around_guest_request"),
        ("cached", "fresh_report_unconfirmed"),
        ("old_after_gap", "multiple_sessions_observed"),
        ("single_advance", "fresh_report_unconfirmed"),
        ("repeated_advance", "fresh_report_unconfirmed"),
        ("regression", "session_changed_around_guest_request"),
        ("second_session", "session_changed_around_guest_request"),
        ("second_runtime", "runtime_identity_changed"),
        ("second_pending", "installation_unconfirmed"),
    ],
)
def test_disconnect_requires_owned_runtime_and_observed_session_transition(probe, fault, expected):
    first = sample_session("old")
    gap = sample_session(
        "", installed=False, reason="PROVIDER_READINESS_REASON_SUPERVISOR_DISCONNECTED"
    )
    last = sample_session("old" if fault == "same" else "new", 2)
    after = {**last, "sequence": 2 if fault == "cached" else 3}
    if fault == "before":
        first["installed"] = False
    if fault == "process":
        after["process"] = "replacement"
    if fault == "request":
        after["session"] = "another"
    if fault == "lease":
        gap["reason"] = "PROVIDER_READINESS_REASON_SUPERVISOR_LEASE_EXPIRED"
    samples = [first, *([] if fault == "no_gap" else [gap])]
    if fault == "old_after_gap":
        samples.append(first)
    samples.append(last)
    post = [after]
    if fault != "single_advance":
        final = {**after, "sequence": after["sequence"] + 1}
        if fault == "repeated_advance":
            final["sequence"] = after["sequence"]
        if fault == "regression":
            final["sequence"] -= 2
        if fault == "second_session":
            final["session"] = "other"
        if fault == "second_runtime":
            final["network"] = "other"
        if fault == "second_pending":
            final["installed"] = False
        post.append(final)
    assert probe.session_outcome(samples, post) == expected


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (None, "reconnected"),
        ("late", "post_request_window_elapsed"),
        ("cached", "fresh_report_unconfirmed"),
    ],
)
def test_post_request_confirmation_waits_only_for_timely_advances(
    probe, monkeypatch, fault, expected
):
    before = [
        sample_session("old"),
        sample_session(
            "", installed=False, reason="PROVIDER_READINESS_REASON_SUPERVISOR_DISCONNECTED"
        ),
        sample_session("new", 2),
    ]
    after = []
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    def read(*args):
        if len(after) and fault == "late":
            sleep(16)
        return sample_session("new", 3 if not after or fault == "cached" else 4)

    monkeypatch.setattr(probe, "session_sample", read)
    monkeypatch.setattr(probe, "time", NS(monotonic=lambda: clock[0], sleep=sleep))
    assert probe.observe_session_after_request(None, None, None, before, after) == expected
    assert len(after) >= 2
