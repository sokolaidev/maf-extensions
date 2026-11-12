"""Offline regression checks for the live suites' CLI observations."""

from __future__ import annotations

import contextlib
import json
import subprocess

import pytest
import test_sbx_e2e as live
import test_sbx_e2e_host as host


def test_prepared_image_reaches_every_live_spec(monkeypatch):
    image = "prepared-shell:test"
    monkeypatch.setattr(live, "_IMAGE", image)
    monkeypatch.setattr(host, "_IMAGE", image)
    assert live._spec().image == image
    assert live._spec(kind="second").image == image
    assert host._spec().image == image
    assert host._allowlist("example.com").image == image
    assert live._spec(image="other:test").image == "other:test"


@pytest.mark.parametrize(
    "mode", ["idle", "async", "sync", "timeout", "cancel", "sibling", "stop", "dispose"]
)
def test_host_tool_lifecycle_uses_the_prepared_image(monkeypatch, tmp_path, mode):
    observed = []

    async def scenario(path, image, selected_mode):
        observed.append((path, image, selected_mode))

    monkeypatch.setattr(live, "_IMAGE", "prepared-shell:test")
    monkeypatch.setattr(live, "_host_tools_case", scenario)
    live.test_host_tools_lifecycle(tmp_path, mode)
    assert observed == [(tmp_path, "prepared-shell:test", mode)]


@pytest.mark.parametrize("failure", [None, "body", "restart"])
def test_host_setting_is_restored_after_success_or_failure(monkeypatch, failure):
    setting, before = host._FORWARDING, "true"
    commands = []

    def checked(*args):
        commands.append(args)
        if args == ("settings", "get", setting):
            return (before + "\n").encode()
        if failure == "restart" and len(commands) == 3:
            raise RuntimeError("restart failed")
        return b""

    monkeypatch.setattr(host, "_MAY_CHANGE_HOST", True)
    monkeypatch.setattr(host, "_checked", checked)
    expected = pytest.raises(RuntimeError) if failure else contextlib.nullcontext()
    with expected, host._setting_set(setting, "false"):
        assert commands[-1] == ("daemon", "restart")
        if failure == "body":
            raise RuntimeError("fault assertion failed")
    assert commands == [
        ("settings", "get", setting),
        ("settings", "set", setting, "false"),
        ("daemon", "restart"),
        ("settings", "set", setting, before),
        ("daemon", "restart"),
    ]


@pytest.mark.parametrize("before", ["false", "true"])
def test_host_setting_without_permission_never_mutates(monkeypatch, before):
    commands = []

    def checked(*args):
        commands.append(args)
        return before.encode()

    monkeypatch.setattr(host, "_MAY_CHANGE_HOST", False)
    monkeypatch.setattr(host, "_checked", checked)
    expected = (
        pytest.raises(pytest.skip.Exception) if before == "true" else contextlib.nullcontext()
    )
    with expected, host._setting_set(host._FORWARDING, "false"):
        assert before == "false"
    assert commands == [("settings", "get", host._FORWARDING)]


@pytest.mark.parametrize("module", [live, host], ids=["suite", "host"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["array", "legacy"])
@pytest.mark.parametrize("names, expected", [([], False), (["other"], False), (["target"], True)])
def test_sandbox_listing_observes_both_formats(monkeypatch, module, wrapped, names, expected):
    rows = [{"name": name} for name in names]
    payload = {"sandboxes": rows} if wrapped else rows

    def run(args, **kwargs):
        assert args == [module._SBX, "ls", "--json"]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert module._listed("target") is expected


@pytest.mark.parametrize("module", [live, host], ids=["suite", "host"])
@pytest.mark.parametrize("payload", [{}, {"sandboxes": None}, {"sandboxes": {}}, None, [None]])
def test_unknown_sandbox_listing_cannot_prove_absence(monkeypatch, module, payload):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(payload).encode(), b""
        ),
    )
    with pytest.raises(AssertionError):
        module._listed("target")


@pytest.mark.parametrize("wrapped", [False, True], ids=["array", "legacy"])
@pytest.mark.parametrize("registered", [False, True], ids=["empty", "registered"])
def test_mcp_preflight_observes_both_formats(monkeypatch, tmp_path, wrapped, registered):
    rows = [{"name": "existing"}] if registered else []
    payload = {"servers": rows} if wrapped else rows
    commands = []

    def checked(*args, **kwargs):
        commands.append(args)
        return json.dumps(payload).encode() if args == ("mcp", "ls", "--json") else b""

    class RefusingBackend:
        async def acquire(self, key, spec):
            raise host.SbxHostNotConfined("remove registered servers with sbx mcp rm")

    monkeypatch.setattr(host, "_MAY_CHANGE_HOST", True)
    monkeypatch.setattr(host, "_setting_set", lambda setting, value: contextlib.nullcontext())
    monkeypatch.setattr(host, "_checked", checked)
    monkeypatch.setattr(host, "_backend", lambda path: RefusingBackend())
    monkeypatch.setattr(host, "_listed", lambda name: False)
    if registered:
        with pytest.raises(pytest.skip.Exception, match="already registered"):
            host.test_a_registered_mcp_server_is_refused(tmp_path)
        assert commands == [("mcp", "ls", "--json")]
    else:
        host.test_a_registered_mcp_server_is_refused(tmp_path)
        assert commands[0] == ("mcp", "ls", "--json")
        assert commands[1][:2] == ("mcp", "add")
        server = commands[1][2]
        assert commands[2:] == [("mcp", "rm", "--force", server)]


@pytest.mark.parametrize("payload", [{}, {"servers": None}, {"servers": {}}, None, [None]])
def test_unknown_mcp_listing_stops_before_registration(monkeypatch, tmp_path, payload):
    commands = []

    def checked(*args, **kwargs):
        commands.append(args)
        return json.dumps(payload).encode()

    monkeypatch.setattr(host, "_MAY_CHANGE_HOST", True)
    monkeypatch.setattr(host, "_setting_set", lambda setting, value: contextlib.nullcontext())
    monkeypatch.setattr(host, "_checked", checked)
    with pytest.raises(AssertionError):
        host.test_a_registered_mcp_server_is_refused(tmp_path)
    assert commands == [("mcp", "ls", "--json")]


@pytest.mark.parametrize("flag", ["--force", "--yes"])
def test_lapsed_login_uses_supported_confirmation_and_restores_login(monkeypatch, tmp_path, flag):
    commands = []

    def checked(*args, **kwargs):
        commands.append(args)
        if args == ("logout", "--help"):
            return f"  {flag}   Skip confirmation prompt".encode()
        if args[0] == "logout":
            assert args == ("logout", flag)
        return b""

    class LoggedOutBackend:
        async def acquire(self, key, spec):
            raise host.SbxLoginRequired("Run `sbx login`")

    monkeypatch.setenv("DOCKER_USERNAME", "test-user")
    monkeypatch.setenv("DOCKER_PAT", "test-token")
    monkeypatch.setattr(host, "_MAY_CHANGE_HOST", True)
    monkeypatch.setattr(host, "_setting_set", lambda setting, value: contextlib.nullcontext())
    monkeypatch.setattr(host, "_checked", checked)
    monkeypatch.setattr(host, "_backend", lambda path: LoggedOutBackend())
    host.test_a_lapsed_login_names_sbx_login(tmp_path)
    assert commands == [
        ("logout", "--help"),
        ("logout", flag),
        ("login", "--username", "test-user", "--password-stdin"),
    ]


@pytest.mark.parametrize("outcome", ["refused", "accepted", "broken-agent", "leftover"])
def test_forwarded_agent_probe_requests_a_kit_and_cleans_up(monkeypatch, tmp_path, outcome):
    commands = []
    disposed = []
    observed = []

    async def prove(backend, name, path):
        observed.append("probe")
        if outcome == "broken-agent":
            raise AssertionError("agent cannot sign")

    class AgentBackend:
        async def check_host(self):
            raise AssertionError("the probe must bypass the host preflight")

        async def _sbx(self, *args, **kwargs):
            commands.append(args)

        async def check_sandbox(self, name):
            observed.append("check")
            if outcome != "accepted":
                raise host.SbxHostNotConfined("ssh-agent.sock")

        async def acquire(self, key, spec):
            await self.check_host()
            await self._sbx("create", "shell", "--template", spec.image, "/workspace")
            await self._sbx("exec", "probe", "true")
            await self.check_sandbox("probe")

        async def dispose(self, key):
            disposed.append(key)

    monkeypatch.setattr(host, "_setting_set", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(host, "_backend", lambda path: AgentBackend())
    monkeypatch.setattr(host, "_prove_forwarded_agent", prove)
    monkeypatch.setattr(host, "_listed", lambda name: outcome == "leftover" and not disposed)
    if outcome == "refused":
        host.test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path, monkeypatch)
    elif outcome == "accepted":
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            host.test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path, monkeypatch)
    else:
        with pytest.raises(AssertionError):
            host.test_a_sandbox_the_running_daemon_gave_the_agent_is_refused(tmp_path, monkeypatch)
    kit = host.Path(host.__file__).parent / "images" / "ssh-agent"
    assert (kit / "kit.yaml").is_file()
    assert commands == [
        ("create", "shell", "--template", host._IMAGE, "--kit", str(kit), "/workspace"),
        ("exec", "probe", "true"),
    ]
    assert len(disposed) == 1
    assert observed == (["probe"] if outcome == "broken-agent" else ["probe", "check"])
