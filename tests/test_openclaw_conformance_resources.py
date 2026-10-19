"""Regression controls for bounded output and exact best-effort reclamation."""

from __future__ import annotations

import importlib
import json as json_module
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
runner = importlib.import_module("openclaw_conformance.__main__")
resources = importlib.import_module("openclaw_conformance.resources")


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize(
    "relative",
    ["gateway/gateway.log", "gateway/worker.log", "gateway/provider.jsonl", "gateway.log"],
)
def test_auxiliary_output_is_bounded_even_when_child_exits_between_polls(
    tmp_path, monkeypatch, finished, relative
):
    class Processes:
        stopped = False

        def __init__(self, root):
            polls = iter([0] if finished else [None, 0])
            self.children = [(SimpleNamespace(poll=lambda: next(polls, 0), returncode=0), None)]

        def start(self, *args):
            (tmp_path / "gateway.log").write_bytes(b"")
            (tmp_path / "gateway").mkdir(exist_ok=True)
            (tmp_path / "gateway/observations.json").write_text("{}")
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 33)

        def close(self):
            self.stopped = True
            return []

    fake_agent = ModuleType("agent")
    setattr(fake_agent, "Processes", Processes)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(runner.LIMITS, "log", 32)
    monkeypatch.setitem(runner.LIMITS, "events", 32)
    with pytest.raises(runner.Refused, match="stage_log_limit"):
        runner.stage("gateway", tmp_path, tmp_path, tmp_path / "candidate.json")


@pytest.mark.parametrize("failure", ["sentinel", "observe", "remove", "owner"])
def test_cleanup_keeps_attempting_all_scopes_and_control_resources(tmp_path, monkeypatch, failure):
    expected = resources.inventory("a" * 32)
    owners = ["1" * 32, "2" * 32, "3" * 32, "4" * 32]
    for relative, owner in zip(
        (
            "ownership/owner/owner",
            "gateway/owner/owner",
            "policy/refused-owner/owner",
            "policy/input-owner/owner",
        ),
        owners,
        strict=True,
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text("bad" if failure == "owner" and owner == owners[0] else owner)
    calls = []
    live = {"owned-" + o for o in owners} | set(expected["names"].values())
    unrelated = "unrelated-container"
    live.add(unrelated)

    def command(args, *rest, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            if failure == "sentinel":
                raise OSError("unavailable")
            return "true"
        if args[1:3] == ["rm", "-f"]:
            if failure == "remove" and args[-1] == "owned-" + owners[0]:
                raise OSError("refused")
            live.discard(args[-1])
            return ""
        label = next((a for a in args if a.startswith("label=maf-sandbox.scope=")), None)
        if label:
            owner = label.rsplit("-", 1)[-1]
            if failure == "observe" and owner == owners[0]:
                raise OSError("unavailable")
            target = "owned-" + owner
        else:
            target = next(a for a in args if a.startswith("name=^/"))[7:-1]
        return target if target in live else ""

    monkeypatch.setattr(runner, "command", command)
    result = runner.cleanup(tmp_path, tmp_path, expected, False)
    assert result["errors"]
    assert result["processes_stopped"] is False
    assert result["emergency_cleanup"] is True
    assert unrelated in live
    assert all(name not in live for name in expected["names"].values())
    assert all("owned-" + owner not in live for owner in owners[1:])
    assert any(
        args[1:3] == ["rm", "-f"] and args[-1] == expected["names"]["sentinel"] for args in calls
    )


def test_control_inventory_is_persisted_before_docker_creation(tmp_path, monkeypatch):
    m = importlib.import_module("test_openclaw_conformance").manifest()
    path = tmp_path / "candidate.json"
    runner.write(path, m)
    output = tmp_path / "campaign"
    monkeypatch.setattr(runner, "check_prepared", lambda *args: None)
    observed = []

    def command(args, *rest, **kwargs):
        if args[1] == "run":
            saved = runner.read(output / "resources.json")
            assert args[args.index("--name") + 1] == saved["names"]["sentinel"]
            observed.append(saved)
        raise OSError("Docker command interrupted")

    monkeypatch.setattr(runner, "command", command)
    assert runner.run(tmp_path / "prepared", path, output)["outcome"] == "failed"
    assert len(observed) == 1
    summary = runner.read(output / "evidence/summary.json")
    assert [r["group"] for r in summary["attempts"]] == ["cleanup"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_setup_output_limit_terminates_early(tmp_path, monkeypatch, stream):
    install = importlib.import_module("openclaw_conformance.install")
    marker = tmp_path / "continued"
    monkeypatch.setitem(install.LIMITS, "log", 4096)
    monkeypatch.setitem(install.LIMITS, "events", 4096)
    code = (
        f"import sys,time; from pathlib import Path; sys.{stream}.write('x'*8192); "
        f"sys.{stream}.flush(); time.sleep(2); Path({str(marker)!r}).touch()"
    )
    with pytest.raises(runner.Refused, match="setup_(output|log)_limit"):
        install.command([sys.executable, "-I", "-B", "-c", code], tmp_path, 10)
    assert not marker.exists()
    assert (tmp_path / "setup.log").stat().st_size <= 4096


def test_cleanup_commands_work_after_setup_diagnostics_overflow(tmp_path, monkeypatch):
    install = importlib.import_module("openclaw_conformance.install")
    monkeypatch.setitem(install.LIMITS, "log", 32)
    (tmp_path / "temp").mkdir()
    (tmp_path / "setup.log").write_bytes(b"x" * 33)
    expected = resources.inventory("a" * 32)
    unrelated = "unrelated-container"
    live = set(expected["names"].values()) | {unrelated}

    def observe(args, root, timeout, **kwargs):
        if args[1] == "inspect":
            output = "true"
        elif args[1:3] == ["rm", "-f"]:
            live.remove(args[-1])
            output = ""
        else:
            target = next(a for a in args if a.startswith("name=^/"))[7:-1]
            output = target if target in live else ""
        return install.command(
            [sys.executable, "-I", "-B", "-c", f"print({output!r})"], root, timeout, **kwargs
        )

    monkeypatch.setattr(runner, "command", observe)
    value = runner.cleanup(tmp_path, tmp_path, expected, True)
    assert live == {unrelated}
    assert value["owned_empty"] is True
    assert value["sentinel_survived"] is True
    assert value["sentinel_removed"] is True
    assert value["errors"] == ["setup_log_limit"]
    assert (tmp_path / "setup.log").read_bytes() == b"x" * 33


@pytest.mark.parametrize("mode", ["stdout", "stderr", "timeout", "exit", "inherited_exit"])
def test_setup_stops_descendants_and_preserves_unrelated_process(tmp_path, monkeypatch, mode):
    import subprocess
    import time

    install = importlib.import_module("openclaw_conformance.install")
    alive = importlib.import_module("test_openclaw_e2e_sample").alive
    (tmp_path / "temp").mkdir()
    monkeypatch.setitem(install.LIMITS, "log", 4096)
    monkeypatch.setitem(install.LIMITS, "events", 4096)
    pidfile = tmp_path / "descendant.pid"
    child = f"import os,time; from pathlib import Path; p=Path({str(pidfile)!r}); pending=p.with_suffix('.pending'); pending.touch(); time.sleep(.05); pending.write_text(str(os.getpid())); pending.replace(p); time.sleep(120)"
    action = (
        f"sys.{mode}.write('x'*8192); sys.{mode}.flush(); time.sleep(120)"
        if mode in {"stdout", "stderr"}
        else "time.sleep(120)"
        if mode == "timeout"
        else "print('completed'); sys.stderr.write('diagnostic\\n'); sys.stderr.flush()"
    )
    streams = (
        "" if mode == "inherited_exit" else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    )
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable,'-I','-B','-c',{child!r}]{streams}); "
        f"p=Path({str(pidfile)!r}); "
        "exec('while not p.exists(): time.sleep(.01)'); " + action
    )
    sentinel = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(120)"])
    descendant = None
    try:
        if mode in {"exit", "inherited_exit"}:
            assert (
                install.command([sys.executable, "-I", "-B", "-c", code], tmp_path, 10)
                == "completed"
            )
            assert "diagnostic" in (tmp_path / "setup.log").read_text()
        else:
            reason = "setup_command_timeout" if mode == "timeout" else "setup_(output|log)_limit"
            with pytest.raises(runner.Refused, match=reason):
                install.command(
                    [sys.executable, "-I", "-B", "-c", code],
                    tmp_path,
                    10,
                )
        descendant = int(pidfile.read_text())
        end = time.monotonic() + 5
        while alive(descendant) and time.monotonic() < end:
            time.sleep(0.01)
        assert not alive(descendant)
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=10)
        if descendant is None and pidfile.exists():
            descendant = int(pidfile.read_text())
        if descendant is not None and alive(descendant):
            import os
            import signal

            os.kill(descendant, signal.SIGTERM)


@pytest.mark.parametrize("probe", ["discovery", "policy"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_probe_output_is_bounded_before_exit(tmp_path, monkeypatch, probe, stream):
    import subprocess

    checks = importlib.import_module("openclaw_conformance.checks")
    marker = tmp_path / "continued"
    monkeypatch.setitem(runner.LIMITS, "events", 4096)
    monkeypatch.setitem(runner.LIMITS, "log", 4096)
    code = (
        f"import sys,time; from pathlib import Path; sys.{stream}.write(' '*8192); "
        f"sys.{stream}.flush(); time.sleep(2); Path({str(marker)!r}).touch(); "
        "print('{\"side_effect_attempts\":[]}'); sys.exit(3 if len(sys.argv)>1 else 0)"
    )
    if probe == "discovery":
        monkeypatch.setattr(checks, "DISCOVERY", code)
    else:
        original = subprocess.Popen

        def launch(args, *a, **kw):
            args = list(args)
            index = next(
                (
                    i
                    for i, arg in enumerate(args)
                    if str(arg).endswith(("maf-bicep-mcp.exe", "maf-bicep-mcp"))
                ),
                None,
            )
            if index is not None:
                args[index:] = [sys.executable, "-I", "-B", "-c", code, "refuse"]
            return original(args, *a, **kw)

        monkeypatch.setattr(subprocess, "Popen", launch)

        async def inputs(*args):
            return True

        monkeypatch.setattr(checks, "_input_refusals", inputs)
    with pytest.raises(runner.Refused, match="setup_(output|log)_limit"):
        if probe == "discovery":
            checks.discovery(tmp_path)
        else:
            checks.policy(tmp_path, tmp_path, {"image": "sha256:" + "a" * 64})
    assert not marker.exists()
    assert (tmp_path / "setup.log").stat().st_size <= 4096


@pytest.mark.parametrize("codes,accepted", [([0, 3], True), ([0, 0], False), ([3, 3], False)])
def test_discovery_preserves_expected_exit_statuses(tmp_path, monkeypatch, codes, accepted):
    import subprocess

    checks = importlib.import_module("openclaw_conformance.checks")
    statuses = iter(codes)
    monkeypatch.setattr(
        checks,
        "capture",
        lambda args, *a, **kw: subprocess.CompletedProcess(
            args, next(statuses), b'{"side_effect_attempts":[]}'
        ),
    )
    if accepted:
        assert checks.discovery(tmp_path)["late_guard_control"] is True
    else:
        with pytest.raises(runner.Refused, match="late_guard_control"):
            checks.discovery(tmp_path)


@pytest.mark.parametrize("status", [0, 1, 2, 3])
def test_policy_credits_only_nonzero_refusals(tmp_path, monkeypatch, status):
    import subprocess

    checks = importlib.import_module("openclaw_conformance.checks")
    monkeypatch.setattr(
        checks, "capture", lambda args, *a, **kw: subprocess.CompletedProcess(args, status, b"")
    )

    async def inputs(*args):
        return True

    monkeypatch.setattr(checks, "_input_refusals", inputs)
    result = checks.policy(tmp_path, tmp_path, {"image": "sha256:" + "a" * 64})
    assert all(
        result[name] is (status != 0)
        for name in ("missing_config", "oversized_config", "mutable_image")
    )


@pytest.mark.parametrize(
    "late", [b'{"late":true}\n', b'{"scenario":"wrong","scenario":"valid"}\n', b'{"partial":']
)
def test_gateway_final_snapshot_follows_provider_shutdown(tmp_path, monkeypatch, late):
    from contextlib import nullcontext

    checks = importlib.import_module("openclaw_conformance.checks")
    worker = importlib.import_module("openclaw_installed_worker_check")
    server = importlib.import_module("maf_sandbox_mcp.server")
    agent = importlib.import_module("agent")
    evidence = tmp_path / "provider.jsonl"
    evidence.write_bytes(b'{"initial":true}\n')
    (tmp_path / "owner").mkdir()
    (tmp_path / "owner/owner").write_text("a" * 32)

    class Processes:
        stopped = False

        def __init__(self, root):
            pass

        def start(self, *args):
            pass

        def ready(self, *args):
            pass

        def close(self):
            self.stopped = True
            return []

    class Provider:
        server_port = 1234
        daemon_threads = True

        def __init__(self, *args):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            assert self.daemon_threads is False
            with evidence.open("ab") as stream:
                stream.write(late)

    ports = iter([1235, 1236])
    monkeypatch.setattr(agent, "Processes", Processes)
    monkeypatch.setattr(agent, "free_port", lambda: next(ports))
    monkeypatch.setattr(checks, "ThreadingHTTPServer", Provider)
    monkeypatch.setattr(checks, "command", lambda *args, **kwargs: "")
    monkeypatch.setattr(server, "ownership", lambda *args: nullcontext())
    monkeypatch.setattr(
        worker, "console_gateway", lambda args: {"provider_observations": [{"initial": True}]}
    )
    manifest = {"image": "sha256:" + "b" * 64, "versions": {}}
    if late == b'{"late":true}\n':
        value = checks.gateway(tmp_path, tmp_path, manifest)
        assert value["provider_observations"] == [{"initial": True}, {"late": True}]
    else:
        with pytest.raises((RuntimeError, ValueError)):
            checks.gateway(tmp_path, tmp_path, manifest)


@pytest.mark.parametrize(
    "tail",
    [
        b'{"late":true}\n',
        b'{"event":"wrong","event":"retired"}\n',
        b'{"partial":',
        b'{"time_ns":NaN}\n',
    ],
)
def test_ownership_final_snapshot_after_check_returns(tmp_path, monkeypatch, tail):
    checks = importlib.import_module("openclaw_conformance.checks")
    worker = importlib.import_module("openclaw_installed_worker_check")
    ownership = importlib.import_module("openclaw_bicep_ownership_check")
    (tmp_path.parent / "resources.json").write_text("{}")
    evidence = tmp_path / "transport.jsonl"
    raw = b'{"initial":true}\n' + tail

    async def check(args):
        evidence.write_bytes(raw)
        return {"transport": [{"initial": True}]}

    monkeypatch.setattr(ownership, "check", check)
    monkeypatch.setattr(worker, "installed_environment", lambda *args: None)
    manifest = {"image": "sha256:" + "b" * 64, "versions": {}}
    if tail == b'{"late":true}\n':
        assert checks.run("ownership", tmp_path, tmp_path, manifest)["transport"] == [
            {"initial": True},
            {"late": True},
        ]
    else:
        with pytest.raises(RuntimeError):
            checks.run("ownership", tmp_path, tmp_path, manifest)
    assert evidence.read_bytes() == raw


@pytest.mark.parametrize(
    ("phase", "malformed"),
    [
        (phase, malformed)
        for phase in ("initialize", "refusal", "positive")
        for malformed in (
            None,
            "duplicate",
            "NaN",
            "Infinity",
            "1e400",
            "rpc_error",
            "null_error",
            "wrong_version",
            "missing_version",
            "missing_id",
            "wrong_id",
            "float_id",
            "string_id",
            "boolean_id",
            "http_error",
        )
    ]
    + [
        ("positive", flag)
        for flag in (
            "tool_error",
            "null_flag",
            "zero_flag",
            "mirror_mismatch",
            "mirror_boolean",
            "mirror_duplicate",
            "mirror_nonfinite",
            "mirror_missing",
            "mirror_image",
            "mirror_multiple",
        )
    ],
)
def test_policy_input_responses_require_strict_json(tmp_path, monkeypatch, phase, malformed):
    import asyncio
    from contextlib import nullcontext

    import httpx
    from maf_sandbox_docker import DockerSandboxBackend
    from maf_sandbox_mcp import server, workload_http

    checks = importlib.import_module("openclaw_conformance.checks")
    (tmp_path / "bicepconfig.json").write_text("{}")

    class Host:
        started = True
        should_exit = False

        async def serve(self, **kwargs):
            while not self.should_exit:
                await asyncio.sleep(0)

    host = Host()

    async def application(*args):
        return object()

    async def invoke(*args, **kwargs):
        return None

    class Client:
        def __init__(self, **kwargs):
            self.headers = kwargs["headers"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def delete(self, *args):
            return httpx.Response(200)

        async def post(self, path, *, json):
            if json["method"] == "notifications/initialized":
                return httpx.Response(202)
            request_id = json["id"]
            current = {1: "initialize", 2: "refusal", 3: "positive"}[request_id]
            if current == "positive":
                await DockerSandboxBackend._invoke(object.__new__(DockerSandboxBackend), "create")
                structured = {"completed": True, "verdict": "valid", "cleanup": "confirmed"}
                text = json_module.dumps(structured, separators=(",", ":"))
                content = [{"type": "text", "text": text}]
                if malformed == "mirror_mismatch":
                    content[0]["text"] = json_module.dumps(dict(structured, cleanup="failed"))
                elif malformed == "mirror_boolean":
                    content[0]["text"] = text.replace("true", "1")
                elif malformed == "mirror_duplicate":
                    content[0]["text"] = '{"completed":false,' + text[1:]
                elif malformed == "mirror_nonfinite":
                    content[0]["text"] = '{"extra":NaN,' + text[1:]
                elif malformed == "mirror_missing":
                    content = []
                elif malformed == "mirror_image":
                    content[0]["type"] = "image"
                elif malformed == "mirror_multiple":
                    content *= 2
                result = json_module.dumps(
                    {"isError": False, "structuredContent": structured, "content": content},
                    separators=(",", ":"),
                )
            else:
                result = "{}" if current == "initialize" else '{"isError":true}'
            raw = '{"jsonrpc":"2.0","id":' + str(request_id) + ',"result":' + result + "}"
            status = 200
            if current == phase and malformed:
                if malformed == "duplicate":
                    raw = raw.replace('"jsonrpc":"2.0"', '"jsonrpc":"1.0","jsonrpc":"2.0"')
                elif malformed in ("NaN", "Infinity", "1e400"):
                    raw = raw[:-1] + ',"ignored":' + malformed + "}"
                elif malformed in ("rpc_error", "null_error"):
                    error = (
                        "null"
                        if malformed == "null_error"
                        else '{"code":-32603,"message":"failed"}'
                    )
                    raw = raw[:-1] + ',"error":' + error + "}"
                elif malformed == "wrong_version":
                    raw = raw.replace('"jsonrpc":"2.0"', '"jsonrpc":"1.0"')
                elif malformed == "missing_version":
                    raw = raw.replace('"jsonrpc":"2.0",', "")
                elif malformed == "missing_id":
                    raw = raw.replace('"id":' + str(request_id) + ",", "")
                elif malformed in ("wrong_id", "float_id", "string_id", "boolean_id"):
                    changed = {
                        "wrong_id": "99",
                        "float_id": str(request_id) + ".0",
                        "string_id": '"' + str(request_id) + '"',
                        "boolean_id": "true",
                    }[malformed]
                    raw = raw.replace('"id":' + str(request_id), '"id":' + changed)
                elif malformed == "http_error":
                    status = 500
                elif not malformed.startswith("mirror_"):
                    flag = {"tool_error": "true", "null_flag": "null", "zero_flag": "0"}[malformed]
                    raw = raw.replace('"isError":false', '"isError":' + flag)
            return httpx.Response(
                status, headers={"mcp-session-id": "session"}, content=raw.encode()
            )

    monkeypatch.setattr(server, "ownership", lambda *args: nullcontext("test-scope"))
    monkeypatch.setattr(server, "http_application", application)
    monkeypatch.setattr(workload_http, "server", lambda app: host)
    monkeypatch.setattr(DockerSandboxBackend, "_invoke", invoke)
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(checks, "command", lambda *args, **kwargs: "")
    probe = checks._input_refusals(tmp_path, tmp_path, {"image": "sha256:" + "a" * 64})
    if malformed is None:
        assert asyncio.run(probe) is True
    else:
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(probe)
    assert host.should_exit is True
