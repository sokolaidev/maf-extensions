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
            assert len(processes) == 2
            assert len(events) == 3
            assert retries == (
                [{"operation": "write", "sequence": 1, "error": "EOFError"}]
                if uncertain_write
                else []
            )
            evidence = json.dumps([ready, first, second, explicit])
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


def test_transition_journal_and_cleanup_survive_guest_start_failure(probe, monkeypatch, tmp_path):
    from collections import defaultdict

    cleanup = []
    host = "allowed.10.23.45.67.nip.io"
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args: [(0, 0, 0, 0, ("10.23.45.67", 8443))]
    )
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(
        probe,
        "control_https",
        lambda host, path, **kwargs: {
            "method": "GET",
            "path": path,
            "host": host + ":8443",
            "port": 8443,
            "authorization_sha256": None,
        },
    )

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

        def provider(self, attached):
            row = {"method": "AttachSandboxProvider", "state": "pending"}
            self.record("mutations", row)
            row["state"] = "acknowledged"
            self.record("barriers", {"provider_state": "READY"})

    class Guest:
        def __init__(self, sandbox, key, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("guest startup refused")

        async def close(self):
            cleanup.append("guest")

    owner = {"sandbox_id": "physical-sandbox"}

    async def acquire(**kwargs):
        return NS(owner=owner)

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
        acquire=acquire,
        dispose=dispose,
    )
    with pytest.raises(RuntimeError, match="guest startup refused"):
        asyncio.run(probe.measure_credentials(fixture, "10.23.45.67", tmp_path / "ca.pem"))
    evidence = fixture.report["credentials"]
    assert evidence["controls"][0]["host"] == host + ":8443"
    assert evidence["mutations"] == [{"method": "AttachSandboxProvider", "state": "acknowledged"}]
    assert evidence["barriers"] == [{"provider_state": "READY"}]
    assert not evidence["characterization_completed"]
    assert not evidence["qualification_passed"]
    assert evidence["provider_and_profile_cleanup_verified"]
    assert cleanup == ["guest", "sandbox", "provider-profile"]


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
