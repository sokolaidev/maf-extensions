"""Offline regression checks for the live suites' CLI observations."""

from __future__ import annotations

import contextlib
import json
import subprocess

import pytest
import test_sbx_e2e as live
import test_sbx_e2e_host as host


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
    monkeypatch.setattr(host, "_forwarding_set", lambda value: contextlib.nullcontext())
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
    monkeypatch.setattr(host, "_forwarding_set", lambda value: contextlib.nullcontext())
    monkeypatch.setattr(host, "_checked", checked)
    with pytest.raises(AssertionError):
        host.test_a_registered_mcp_server_is_refused(tmp_path)
    assert commands == [("mcp", "ls", "--json")]
