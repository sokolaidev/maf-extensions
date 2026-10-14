"""Acceptance must come from the Gateway's tool results, never assistant claims."""

from __future__ import annotations

import argparse
import copy
import http.client
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

SAMPLE = Path(__file__).resolve().parents[1] / "samples/experimental/openclaw_bicep_e2e"


def load_sample() -> Any:
    saved = {name: sys.modules.get(name) for name in ("_evidence", "_provider", "_windows_job")}
    sys.path.insert(0, str(SAMPLE))
    try:
        for name in saved:
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("openclaw_e2e_agent", SAMPLE / "agent.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SAMPLE))
        for name, previous in saved.items():
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous


agent = load_sample()
evidence = agent.check_turn.__globals__
TURN = "a" * 32
IMAGE = "sha256:" + "b" * 64
CONFIG = "c" * 64


def record(*, repaired: bool = False) -> dict[str, Any]:
    source = evidence["VALID"] if repaired else evidence["INVALID"]
    return {
        "turn": TURN,
        "arguments": {"files": [{"path": "main.bicep", "content": source}]},
        "projected": {
            "tool": {
                "id": "mcp:bicep:bicep__bicep_validate",
                "name": "bicep__bicep_validate",
                "source": "mcp",
            },
            "result": {
                "content": [{"type": "text", "text": "Compiler diagnostics"}],
                "details": {
                    "structuredContent": {
                        "completed": True,
                        "status": "ok",
                        "verdict": "valid" if repaired else "invalid",
                        "cleanup": "confirmed",
                        "image": IMAGE,
                        "config_sha256": CONFIG,
                        "source_sha256": evidence["digest"](source),
                    }
                },
            },
        },
    }


def check(records: list[dict[str, Any]], *, repaired: bool = False) -> dict[str, Any]:
    return agent.check_turn(records, TURN, repaired=repaired, image=IMAGE, config_hash=CONFIG)


@pytest.mark.parametrize("repaired", [False, True])
def test_compiler_evidence_accepts_expected_turn(repaired: bool) -> None:
    assert check([record(repaired=repaired)], repaired=repaired)["calls"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("completed", False),
        ("status", "error"),
        ("verdict", "valid"),
        ("cleanup", "unknown"),
        ("image", "wrong"),
        ("config_sha256", "wrong"),
        ("source_sha256", "wrong"),
    ],
)
def test_refuse_wrong_compiler_evidence(field: str, value: Any) -> None:
    item = record()
    item["projected"]["result"]["details"]["structuredContent"][field] = value
    with pytest.raises(RuntimeError):
        check([item])


@pytest.mark.parametrize("mutation", ["other_turn", "other_tool", "error", "missing", "prose"])
def test_no_success_without_this_tools_result(mutation: str) -> None:
    item = record()
    if mutation == "other_turn":
        item["turn"] = "d" * 32
    elif mutation == "other_tool":
        item["projected"]["tool"]["name"] = "exec"
    elif mutation == "error":
        item["projected"]["result"]["isError"] = True
    elif mutation == "prose":
        item["projected"]["result"] = {"content": [{"text": "It passed. Cleanup confirmed."}]}
    with pytest.raises((RuntimeError, KeyError)):
        check([] if mutation == "missing" else [item])


@pytest.mark.parametrize(
    "source",
    [
        "",
        "output greeting int = 1",
        "output other string = 'hello'",
        "output greeting string = 'bye'",
        "output greeting int = 'hello'",
    ],
)
def test_repair_must_preserve_requested_value(source: str) -> None:
    item = record(repaired=True)
    item["arguments"]["files"][0]["content"] = source
    item["projected"]["result"]["details"]["structuredContent"]["source_sha256"] = evidence[
        "digest"
    ](source)
    with pytest.raises(RuntimeError):
        check([item], repaired=True)


def test_last_repair_must_validate() -> None:
    failed = record(repaired=True)
    failed["projected"]["result"]["details"]["structuredContent"]["verdict"] = "invalid"
    with pytest.raises(RuntimeError, match="Last repair"):
        check([record(repaired=True), failed], repaired=True)


def messages(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Validate sample-turn=" + item["turn"]},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_" + item["turn"],
                    "function": {
                        "name": "tool_call",
                        "arguments": json.dumps(
                            {"id": evidence["TOOL"], "args": item["arguments"]}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_" + item["turn"],
            "content": "Tool result:\n" + json.dumps(item["projected"]),
        },
    ]


@pytest.fixture
def provider(tmp_path: Path):
    instance = agent.Provider(tmp_path / "evidence.jsonl", "", "", "")
    instance.start()
    try:
        yield instance
    finally:
        instance.close()


def test_observe_deduplicates_history_and_keeps_original_turn(provider: Any) -> None:
    first = record()
    second = record(repaired=True)
    second["turn"] = "e" * 32
    provider.observe({"messages": messages(first) + messages(second)})
    provider.observe({"messages": messages(first) + messages(second)})
    assert provider.records == [first, second]
    assert len(provider.evidence.read_text().splitlines()) == 2
    altered = copy.deepcopy(first)
    altered["arguments"]["files"][0]["content"] = "changed"
    with pytest.raises(RuntimeError, match="identity was reused"):
        provider.observe({"messages": messages(altered)})


def test_provider_requires_current_turn_marker(provider: Any) -> None:
    with pytest.raises(RuntimeError, match="Missing turn"):
        provider.observe({"messages": [{"role": "user", "content": "It passed"}]})


def post(provider: Any, body: dict[str, Any], *, token: str | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", provider.server.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            json.dumps(body),
            {"Authorization": "Bearer " + (provider.token if token is None else token)},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_deterministic_provider_emits_tool_call_and_observes_result(provider: Any) -> None:
    body = {"messages": messages(record())[:1]}
    status, payload = post(provider, body)
    assert status == 200 and b'"name": "tool_call"' in payload
    assert b"[DONE]" in payload
    status, payload = post(provider, {"messages": messages(record())})
    assert status == 200 and b"Compiler result received" in payload
    assert check(provider.records)["verdict"] == "invalid"


def test_relay_forwards_to_configured_model_with_its_own_credentials(
    provider: Any, tmp_path: Path
) -> None:
    relay = agent.Provider(tmp_path / "relay.jsonl", provider.url, "chosen-model", provider.token)
    relay.start()
    captured = []
    original = provider.smoke

    def capture(body: dict[str, Any], turn: str, repair: bool) -> bytes:
        captured.append(body)
        return original(body, turn, repair)

    provider.smoke = capture
    try:
        status, payload = post(relay, {"model": "sample", "messages": messages(record())[:1]})
        assert status == 200 and b"tool_calls" in payload
        assert captured[0]["model"] == "chosen-model"
        assert not relay.errors
    finally:
        relay.close()


def test_provider_refuses_bad_auth_and_exhausted_budget(provider: Any) -> None:
    body = {"messages": messages(record())[:1]}
    assert post(provider, body, token="wrong")[0] == 502
    assert provider.requests == 0
    provider.requests = 32
    assert post(provider, body)[0] == 502
    assert not provider.records


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://user:secret@example.test/v1",
        "https://example.test/v1?key=secret",
        "https://example.test/v1#secret",
    ],
)
def test_model_configuration_refuses_unsafe_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    monkeypatch.setenv("OPENAI_MODEL", "model")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    with pytest.raises(RuntimeError):
        agent.model_settings("model")
    assert agent.model_settings("smoke") == ("", "", "")


def test_cleanup_continues_when_provider_shutdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenProvider:
        def __init__(self, *args: Any):
            pass

        def start(self) -> None:
            raise RuntimeError("startup failed")

        def close(self) -> None:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(agent, "Provider", BrokenProvider)
    monkeypatch.setattr(agent, "program", lambda name: name)
    monkeypatch.setattr(agent, "install", lambda *args: (tmp_path / "python", tmp_path, {}))
    monkeypatch.setattr(agent, "execute", lambda *args, **kwargs: json.dumps([{"Id": IMAGE}]))
    args = argparse.Namespace(mode="smoke", output=tmp_path / "private", image=IMAGE)
    result = agent.run(args)
    assert not result["passed"]
    assert result["cleanup_errors"] == ["Provider cleanup: RuntimeError"]
    assert result["owned_empty"] is True
    assert json.loads((args.output / "report.json").read_text())["error"] == "startup failed"


def test_provider_cleanup_before_thread_start_does_not_wait(tmp_path: Path) -> None:
    instance = agent.Provider(tmp_path / "evidence.jsonl", "", "", "")
    closer = threading.Thread(target=instance.close, daemon=True)
    closer.start()
    try:
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert instance.server.socket.fileno() == -1
    finally:
        instance.server.server_close()


def test_worker_does_not_inherit_source_path_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "source-tree")
    monkeypatch.setenv("PYTHONHOME", "other-runtime")
    monkeypatch.setenv("DOCKER_CONTEXT", "selected-daemon")
    environment = agent.worker_environment()
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["DOCKER_CONTEXT"] == "selected-daemon"


@pytest.mark.parametrize("prefix,suffix", [(" ", ""), ("", "\n"), ("\n", "\t")])
def test_first_turn_must_match_prompt_bytes(prefix: str, suffix: str) -> None:
    item = record()
    source = prefix + evidence["INVALID"] + suffix
    item["arguments"]["files"][0]["content"] = source
    item["projected"]["result"]["details"]["structuredContent"]["source_sha256"] = evidence[
        "digest"
    ](source)
    with pytest.raises(RuntimeError, match="First turn changed"):
        check([item])


def test_repair_may_change_whitespace() -> None:
    item = record(repaired=True)
    source = "\n  output greeting string = 'hello'\n"
    item["arguments"]["files"][0]["content"] = source
    item["projected"]["result"]["details"]["structuredContent"]["source_sha256"] = evidence[
        "digest"
    ](source)
    assert check([item], repaired=True)["verdict"] == "valid"


def wait_file(path: Path) -> str:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError("Child did not publish readiness")


def alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        api.OpenProcess.restype = ctypes.wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
        api.WaitForSingleObject.restype = ctypes.wintypes.DWORD
        api.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("leader_exits", [False, True])
def test_owned_descendant_is_removed_even_if_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leader_exits: bool
) -> None:
    controller = agent.Processes(tmp_path)
    original_wait = controller.wait_empty
    monkeypatch.setattr(
        controller,
        "wait_empty",
        lambda child, timeout: original_wait(child, 0.3 if timeout == 30 else 5),
    )
    marker = tmp_path / "descendant"
    child_code = (
        "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path("
        + repr(str(marker))
        + ").write_text(str(os.getpid())); time.sleep(120)"
    )
    leader_code = (
        "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',"
        + repr(child_code)
        + "]); marker=Path("
        + repr(str(marker))
        + ");\nwhile not marker.exists(): time.sleep(.01)\n"
        + ("" if leader_exits else "time.sleep(120)")
    )
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], creationflags=agent.FLAGS
    )
    try:
        controller.start("tree", [sys.executable, "-c", leader_code])
        descendant = int(wait_file(marker))
        assert alive(descendant)
        leader = controller.children[0][0]
        if leader_exits:
            leader.wait(timeout=15)
        assert controller.group_exists(leader)
        close_errors = controller.close()
        assert close_errors == []
        assert controller.stopped
        assert not alive(descendant)
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)
        if not controller.stopped:
            controller.close()
        if os.name == "nt":
            for job in controller.jobs.values():
                if job.handle:
                    job.terminate()
                    job.close()
        if os.name != "nt" and controller.children:
            try:
                os.killpg(controller.children[0][0].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Successful cleanup already removed the process group.


def test_failed_group_observation_never_confirms_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = agent.Processes(tmp_path)
    controller.start("short", [sys.executable, "-c", "pass"])
    controller.children[0][0].wait(timeout=15)

    def refused(child: Any) -> bool:
        raise OSError("Cannot inspect owned processes")

    monkeypatch.setattr(controller, "group_exists", refused)
    close_errors = controller.close()
    assert close_errors == ["OSError"]
    assert not controller.stopped


@pytest.mark.skipif(os.name != "nt", reason="Windows job admission")
def test_failed_job_assignment_never_starts_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = agent.Processes(tmp_path)
    marker = tmp_path / "must-not-start"

    def refuse(*args: Any) -> None:
        raise OSError("Job assignment refused")

    monkeypatch.setattr(agent.Job, "assign", refuse)
    with pytest.raises(OSError, match="assignment refused"):
        controller.start(
            "gated",
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()",
            ],
        )
    close_errors = controller.close()
    assert close_errors == []
    assert controller.stopped
    assert not marker.exists()


def test_missing_docker_retains_failure_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    args = argparse.Namespace(mode="smoke", output=tmp_path / "private")
    result = agent.run(args)
    retained = json.loads((args.output / "report.json").read_text())
    assert retained == result
    assert not result["passed"]
    assert result["error"] == "Install docker and place it on PATH"
    assert result["cleanup_errors"] == []


def test_gateway_temp_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    root = tmp_path / "private"
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "shared"))

    class Services:
        stopped = True

        def __init__(self, directory: Path) -> None:
            pass

        def start(self, name: str, command: list[str], env: dict[str, str]) -> None:
            if name == "worker":
                (root / "owner").mkdir()
                (root / "owner/owner").write_text("a" * 32)
                return
            captured.update(env)
            raise RuntimeError("captured gateway environment")

        def ready(self, *args: Any) -> None:
            pass

        def close(self) -> list[str]:
            return []

    monkeypatch.setattr(agent, "Processes", Services)
    monkeypatch.setattr(agent, "program", lambda name: name)
    monkeypatch.setattr(agent, "install", lambda *args: (tmp_path / "python", tmp_path, {}))
    monkeypatch.setattr(
        agent,
        "execute",
        lambda command, *args, **kwargs: (
            json.dumps([{"Id": IMAGE}]) if "inspect" in command else ""
        ),
    )
    args = argparse.Namespace(mode="smoke", output=root, image=IMAGE)
    result = agent.run(args)
    assert result["error"] == "captured gateway environment"
    private_temp = root / "temp"
    assert private_temp.is_dir()
    assert {captured[name] for name in ("TEMP", "TMP", "TMPDIR")} == {str(private_temp)}
    if os.name != "nt":
        assert private_temp.stat().st_mode & 0o777 == 0o700
    resolved = subprocess.run(
        [sys.executable, "-I", "-c", "import tempfile; print(tempfile.gettempdir())"],
        env=captured,
        check=True,
        capture_output=True,
        text=True,
        creationflags=agent.FLAGS,
    )
    assert Path(resolved.stdout.strip()) == private_temp
