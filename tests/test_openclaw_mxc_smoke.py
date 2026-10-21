"""Reject fabricated, repeated, and cross-turn native smoke observations."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
Refused = importlib.import_module("openclaw_conformance.evidence").Refused
Provider = importlib.import_module("openclaw_conformance.mxc_smoke_provider").Provider


@pytest.fixture
def provider():
    value = Provider()
    value.select("a" * 32, "exec", {"command": "fixed"})
    yield value
    value.close()


def body(*, tools: tuple[str, ...] = ("exec",)) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "smoke-turn=" + "a" * 32}],
        "tools": [{"function": {"name": name}} for name in tools],
    }


def first_delta(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode().splitlines()[0].removeprefix("data: "))["choices"][0]["delta"]


@pytest.mark.parametrize("wrapper", [False, True])
def test_fixed_call_and_only_matching_gateway_result(provider: Any, wrapper: bool):
    request = body(tools=("tool_call",) if wrapper else ("exec",))
    call = first_delta(provider.reply(request))["tool_calls"][0]
    assert provider.results == []
    assert call["id"] == "call" + "a" * 32
    assert call["function"]["name"] == ("tool_call" if wrapper else "exec")
    arguments = json.loads(call["function"]["arguments"])
    assert arguments == (
        {"id": "exec", "args": {"command": "fixed"}} if wrapper else {"command": "fixed"}
    )
    request["messages"].append({"role": "assistant", "tool_calls": [call]})
    request["messages"].append(
        {"role": "tool", "tool_call_id": call["id"], "content": "actual Gateway failure"}
    )
    assert first_delta(provider.reply(request))["content"] == "Smoke call settled."
    assert provider.results == ["actual Gateway failure"]
    assert provider.calls == 1
    with pytest.raises(Refused, match="provider_request_budget"):
        provider.reply(request)


@pytest.mark.parametrize("tool", ["read", "tool_search", "wait"])
def test_missing_tool_cannot_be_fabricated(provider: Any, tool: str):
    with pytest.raises(Refused, match="provider_tool_unavailable"):
        provider.reply(body(tools=(tool,)))
    assert provider.calls == 0


@pytest.mark.parametrize("matches", [0, 2])
def test_missing_or_duplicate_result_refused(provider: Any, matches: int):
    request = body()
    provider.reply(request)
    request["messages"].extend(
        {"role": "tool", "tool_call_id": "call" + "a" * 32, "content": "untrusted"}
        for _ in range(matches)
    )
    with pytest.raises(Refused, match="provider_result_identity"):
        provider.reply(request)
    assert provider.results == []


def test_other_turn_result_is_not_accepted(provider: Any):
    request = body()
    provider.reply(request)
    request["messages"].append(
        {"role": "tool", "tool_call_id": "call" + "b" * 32, "content": "wrong turn"}
    )
    with pytest.raises(Refused, match="provider_result_identity"):
        provider.reply(request)


def test_later_user_cannot_reuse_prior_turn(provider: Any):
    request = body()
    request["messages"].append({"role": "user", "content": "smoke-turn=" + "b" * 32})
    with pytest.raises(Refused, match="provider_turn"):
        provider.reply(request)


@pytest.mark.parametrize("change", ["name", "arguments"])
def test_provider_rejects_rewritten_call(provider: Any, change: str):
    request = body()
    call = first_delta(provider.reply(request))["tool_calls"][0]
    call["function"][change] = "write" if change == "name" else '{"command":"different"}'
    request["messages"].extend(
        [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": "untrusted"},
        ]
    )
    with pytest.raises(Refused, match="provider_call_changed"):
        provider.reply(request)
    assert provider.results == []


smoke = importlib.import_module("openclaw_conformance.mxc_smoke")
process = importlib.import_module("openclaw_conformance.mxc_smoke_process")


@pytest.mark.parametrize("mode", smoke.MODES)
def test_native_success_requires_process_and_file_evidence(tmp_path: Path, mode: str):
    active = tmp_path / "active"
    active.mkdir()
    agent = tmp_path / "agent"
    agent.mkdir()
    agent_write = agent / "agent-write.txt"
    agent_write.write_text("nonce" if mode == "rw" else "before")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-before")
    target = active / "native-write.txt"
    target.write_text("nonce" if mode == "rw" else "before")
    result = "\n".join(
        [
            "smoke-start=nonce",
            "guest-pid=123",
            "active-read=nonce",
            "agent-read=denied" if mode == "none" else "agent-read=nonce",
            "active-write=allowed" if mode == "rw" else "active-write=denied",
            "agent-write=allowed" if mode == "rw" else "agent-write=denied",
            "outside-write=denied",
            "smoke-end=nonce",
        ]
    )
    assert smoke.native_passed(result, {123}, active, agent, outside, mode, "nonce")
    assert not smoke.native_passed(result, {456}, active, agent, outside, mode, "nonce")
    assert not smoke.native_passed(
        '{"status":"error"}', {123}, active, agent, outside, mode, "nonce"
    )
    outside.write_text("modified")
    assert not smoke.native_passed(result, {123}, active, agent, outside, mode, "nonce")
    outside.write_text("outside-before")
    agent_write.write_text("wrong-state")
    assert not smoke.native_passed(result, {123}, active, agent, outside, mode, "nonce")
    agent_write.write_text("nonce" if mode == "rw" else "before")
    wrong_result = (
        result.replace("agent-write=denied", "agent-write=allowed")
        if mode != "rw"
        else result.replace("agent-write=allowed", "agent-write=denied")
    )
    assert not smoke.native_passed(wrong_result, {123}, active, agent, outside, mode, "nonce")
    target.write_text("wrong-state")
    assert not smoke.native_passed(result, {123}, active, agent, outside, mode, "nonce")


@pytest.mark.parametrize("mode", smoke.MODES)
def test_cli_workspace_must_match_selected_mode_and_owned_root(tmp_path: Path, mode: str):
    path = tmp_path / ("agent" if mode == "rw" else "private/session")
    explanation = {
        "sandbox": {
            "backend": "mxc",
            "sessionIsSandboxed": True,
            "workspaceAccess": mode,
            "scope": "agent",
            "effectiveHostWorkspaceRoot": str(path),
        }
    }
    assert smoke.workspace(explanation, tmp_path, mode) == path
    for key, value in (
        ("backend", "docker"),
        ("sessionIsSandboxed", False),
        ("scope", "session"),
        ("effectiveHostWorkspaceRoot", str(tmp_path.parent)),
    ):
        changed = {"sandbox": {**explanation["sandbox"], key: value}}
        with pytest.raises(Refused):
            smoke.workspace(changed, tmp_path, mode)


def test_native_ancestor_requires_distinct_matching_executor():
    rows = {
        1: {"parent": 0, "executor": True, "sha256": "expected"},
        2: {"parent": 1},
        3: {"parent": 2},
    }
    assert process.native_ancestor(rows, 3, "expected")
    assert not process.native_ancestor(rows, 1, "expected")
    assert not process.native_ancestor(rows, 3, "changed")
    rows[2]["parent"] = 3
    assert not process.native_ancestor(rows, 3, "expected")


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 job and process-image observation")
def test_real_owned_process_snapshot(tmp_path: Path):
    import time

    sys.path.insert(0, str(smoke.SAMPLE))
    helpers = importlib.import_module("agent")
    owned = helpers.Processes(tmp_path)
    try:
        owned.start("probe", [sys.executable, "-I", "-c", "import time; time.sleep(10)"])
        job = next(iter(owned.jobs.values()))
        executable = Path(sys.executable).resolve()
        expected = smoke.file_hash(executable)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            rows = process.sample(job, executable)
            if any(process.native_ancestor(rows, pid, expected) for pid in rows):
                break
            time.sleep(0.05)
        else:
            pytest.fail("No verified executor ancestor in the owned job")
    finally:
        cleanup = owned.close()
        assert cleanup == []
        assert owned.stopped


def test_configuration_keeps_native_candidate_and_policy_explicit(tmp_path: Path, provider: Any):
    plugin = tmp_path / "installed-plugin"
    config = smoke.configuration(tmp_path, plugin, "none", 12345, "test-token", provider)
    settings = config["plugins"]["entries"]["mxc"]["config"]
    assert (
        Path(settings["mxcBinaryPath"])
        == plugin / "node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe"
    )
    assert settings["containment"] == "process"
    assert settings["network"] == "none"
    assert "mxcPolicyPaths" not in settings
    assert config["tools"]["exec"]["host"] == "sandbox"
    assert config["tools"]["elevated"]["enabled"] is False
    assert config["tools"]["codeMode"]["enabled"] is False
    assert config["plugins"]["slots"]["memory"] == "none"


def test_provider_http_rejects_missing_auth_before_selecting_tool(provider: Any):
    import http.client

    provider.start()
    connection = http.client.HTTPConnection("127.0.0.1", provider.server.server_port, timeout=5)
    try:
        connection.request(
            "POST", "/v1/chat/completions", json.dumps(body()), {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        assert response.status == 502
        response.read()
        assert provider.calls == 0
        assert provider.results == []
        assert provider.errors == ["Refused"]
    finally:
        connection.close()


def test_report_cannot_pass_missing_cases_or_failed_cleanup(tmp_path: Path):
    campaign = smoke.Campaign(tmp_path, tmp_path / "installation", "none")
    try:
        campaign.rows = [("native", True, "native result")]
        passed = campaign.report(None)
        assert not passed
        campaign.rows = [(str(i), True, "ok") for i in range(4)]
        passed = campaign.report(None)
        assert passed
        passed = campaign.report("candidate_changed")
        assert not passed
        campaign.cleanup.append("still-running")
        passed = campaign.report(None)
        assert not passed
        assert list(tmp_path.glob("*.json")) == []
        assert "unqualified" in (tmp_path / "report.md").read_text()
    finally:
        campaign.provider.close()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell fixture prerequisite")
def test_owned_powershell_fixture_reaches_every_unrestricted_target(tmp_path: Path):
    import os

    active = tmp_path / "active workspace"
    agent = tmp_path / "agent's workspace"
    active.mkdir()
    agent.mkdir()
    outside = tmp_path / "outside.txt"
    (active / "marker.txt").write_text("nonce")
    (agent / "agent-marker.txt").write_text("nonce")
    (agent / "agent-write.txt").write_text("before")
    outside.write_text("outside-before")
    script = active / "smoke.ps1"
    script.write_text(smoke.native_script(active, agent, outside, "nonce"), encoding="utf-8-sig")
    shell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = smoke.capture(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        tmp_path,
        smoke.environment(tmp_path),
        30,
        diagnostics=True,
    )
    assert result.returncode == 0
    assert b"smoke-end=nonce" in result.stdout
    assert b"active-read=nonce" in result.stdout
    assert b"agent-read=nonce" in result.stdout
    assert b"agent-write=allowed" in result.stdout
    assert b"outside-write=allowed" in result.stdout
    assert outside.read_text() == "nonce"
    assert (active / "native-write.txt").read_text() == "nonce"
    assert (agent / "agent-write.txt").read_text() == "nonce"


@pytest.mark.parametrize(
    "partial", [b"", b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"]
)
def test_provider_shutdown_with_incomplete_headers(monkeypatch: pytest.MonkeyPatch, partial: bytes):
    import socket
    import threading

    module = importlib.import_module("openclaw_conformance.mxc_smoke_provider")
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.1)
    value = Provider()
    accepted = threading.Event()
    finished = threading.Event()
    errors = []
    get_request = value.server.get_request

    def accept():
        connection = get_request()
        accepted.set()
        return connection

    def close():
        try:
            value.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(value.server, "get_request", accept)
    value.start()
    connection = socket.create_connection(("127.0.0.1", value.server.server_port), timeout=3)
    closer = threading.Thread(target=close, daemon=True)
    try:
        if partial:
            connection.sendall(partial)
        assert accepted.wait(3)
        closer.start()
        assert finished.wait(3), "Provider shutdown blocked on request parsing"
        assert not errors
        assert not value.thread.is_alive()
        assert value.calls == 0
    finally:
        connection.close()
        if closer.ident is not None:
            closer.join(timeout=3)
        value.close()


@pytest.mark.parametrize("mode", smoke.MODES)
@pytest.mark.parametrize("agent_allowed", [False, True])
def test_fixture_control_requires_and_restores_agent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, agent_allowed: bool
):
    from types import SimpleNamespace

    (tmp_path / "state").mkdir()
    agent = tmp_path / "agent"
    agent.mkdir()
    active = agent if mode == "rw" else tmp_path / "private/session"
    monkeypatch.setattr(smoke, "identities", lambda _: {})
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "node")
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
    campaign = smoke.Campaign(tmp_path, tmp_path / "installation", mode)

    def capture(command, *args, **kwargs):
        if "explain" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "sandbox": {
                            "backend": "mxc",
                            "sessionIsSandboxed": True,
                            "workspaceAccess": mode,
                            "scope": "agent",
                            "effectiveHostWorkspaceRoot": str(active),
                        }
                    }
                ).encode(),
            )
        nonce = (active / "marker.txt").read_text()
        (active / "native-write.txt").write_text(nonce)
        (tmp_path / "outside.txt").write_text(nonce)
        if agent_allowed:
            (agent / "agent-write.txt").write_text(nonce)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"smoke-end={nonce}\nactive-read={nonce}\nagent-read={nonce}\n"
                "active-write=allowed\noutside-write=allowed\n"
                + ("agent-write=allowed" if agent_allowed else "agent-write=denied")
            ).encode(),
        )

    def before_gateway():
        assert (agent / "agent-write.txt").read_text() == "before"
        assert (active / "native-write.txt").read_text() == "before"
        assert (tmp_path / "outside.txt").read_text() == "outside-before"
        raise Refused("stop_after_control")

    monkeypatch.setattr(smoke, "capture", capture)
    monkeypatch.setattr(campaign.provider, "start", before_gateway)
    try:
        with pytest.raises(
            Refused, match="stop_after_control" if agent_allowed else "host_fixture_prerequisite"
        ):
            campaign.execute()
        assert campaign.rows[0][1] is agent_allowed
        assert (agent / "agent-write.txt").read_text() == "before"
    finally:
        campaign.provider.close()
