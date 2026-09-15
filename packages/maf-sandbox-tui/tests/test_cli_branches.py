"""CLI outcomes that require unavailable, ambiguous or failing hosts."""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import cast

import pytest

import maf_sandbox_tui.cli as cli_module
from maf_sandbox_tui import (
    ControlEndpointError,
    DisposalResult,
    DisposalStatus,
    EndpointManifest,
    HttpControl,
    MemoryControl,
    PurgeResult,
    PurgeStatus,
    SandboxRecord,
    SandboxState,
)


def _record(instance_id: str = "instance") -> SandboxRecord:
    return SandboxRecord(
        source_id="host",
        backend="hyperlight",
        scope="scope",
        thread_id="thread",
        agent_id="agent",
        call_id="",
        kind="python",
        instance_id=instance_id,
        state=SandboxState.READY,
        created_at=1,
        last_activity_at=2,
    )


def test_an_explicit_empty_endpoint_never_falls_back_to_local_discovery(monkeypatch, capsys):
    def unexpected_discovery() -> tuple[EndpointManifest, ...]:
        raise AssertionError("explicit endpoint must not discover another host")

    monkeypatch.setattr(cli_module, "read_manifests", unexpected_discovery)
    with pytest.raises(SystemExit) as raised:
        cli_module.main(["--endpoint", "", "list"])
    assert raised.value.code == 1
    assert "loopback" in capsys.readouterr().err

    with pytest.raises(SystemExit) as conflicting:
        cli_module.main(["--demo", "--endpoint", "", "list"])
    assert conflicting.value.code == 2


def _arguments(**values: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "instance_id": "instance",
        "json": True,
        "yes": True,
        "timeout": 0.1,
        "scope": "scope",
        "thread": "thread",
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def test_snapshot_and_find_retain_runtime_host_errors():
    class FailingClient:
        async def list_sandboxes(self):
            raise RuntimeError("inventory failed")

        async def get_sandbox(self, _instance_id):
            raise RuntimeError("lookup failed")

    manifest = EndpointManifest("host", "http://127.0.0.1:1", 1)
    probe = cli_module._HostProbe(manifest, cast(HttpControl, FailingClient()))

    records, list_errors = asyncio.run(cli_module._snapshot((probe,)))
    matches, find_errors = asyncio.run(cli_module._find((probe,), "instance"))

    assert records == () and matches == ()
    assert list_errors == ("host: inventory failed",)
    assert find_errors == ("host: lookup failed",)


@pytest.mark.parametrize("operation", ["show", "delete"])
def test_exact_lookup_without_discovered_hosts_is_unavailable(operation, capsys):
    arguments = _arguments()
    result = (
        asyncio.run(cli_module._show((), arguments))
        if operation == "show"
        else asyncio.run(cli_module._delete((), arguments))
    )

    assert result == 1
    captured = capsys.readouterr()
    assert '"status": "unavailable"' in captured.out
    assert "no opted-in MAF hosts were discovered" in captured.err


def test_empty_table_warning_and_long_age_rendering(monkeypatch, capsys):
    cli_module._table(("A",), (), empty="Nothing here.")
    cli_module._print_errors(("host failed",))
    monkeypatch.setattr(time, "time", lambda: 200_000.0)

    assert cli_module._age(196_000) == "1h"
    assert cli_module._age(100_000) == "1d"
    captured = capsys.readouterr()
    assert captured.out == "Nothing here.\n"
    assert captured.err == "mst: warning: host failed\n"


def test_plain_not_found_distinguishes_certain_and_uncertain(capsys):
    assert cli_module._not_found("gone", as_json=False, uncertain=False) == 3
    assert cli_module._not_found("maybe", as_json=False, uncertain=True) == 1
    errors = capsys.readouterr().err
    assert "already gone" in errors
    assert "hosts were unavailable" in errors


@pytest.mark.parametrize("operation", ["show", "delete"])
def test_duplicate_instance_reports_are_rejected(operation, monkeypatch):
    record = _record()
    matches = ((object(), record), (object(), record))

    async def find(*_args):
        return matches, ()

    monkeypatch.setattr(cli_module, "_find", find)

    with pytest.raises(ControlEndpointError, match="several hosts"):
        if operation == "show":
            asyncio.run(cli_module._show((), _arguments()))
        else:
            asyncio.run(cli_module._delete((), _arguments()))


def test_delete_reports_an_instance_that_is_already_gone(monkeypatch, capsys):
    async def find(*_args):
        return (), ()

    monkeypatch.setattr(cli_module, "_find", find)

    assert asyncio.run(cli_module._delete((), _arguments())) == 3
    assert '"status": "not_found"' in capsys.readouterr().out


def test_show_returns_an_error_when_another_host_is_unavailable(monkeypatch, capsys):
    async def find(*_args):
        return ((object(), _record()),), ("other-host: connection refused",)

    monkeypatch.setattr(cli_module, "_find", find)

    assert asyncio.run(cli_module._show((), _arguments())) == 1
    captured = capsys.readouterr()
    assert '"instance_id": "instance"' in captured.out
    assert "other-host: connection refused" in captured.err


def test_delete_refuses_when_single_ownership_is_uncertain(monkeypatch, capsys):
    disposed = False

    class Client:
        async def dispose_sandbox(self, instance_id, *, timeout):
            del instance_id, timeout
            nonlocal disposed
            disposed = True
            raise AssertionError("unreachable")

    async def find(*_args):
        return ((Client(), _record()),), ("other-host: connection refused",)

    monkeypatch.setattr(cli_module, "_find", find)

    assert asyncio.run(cli_module._delete((), _arguments())) == 1
    assert not disposed
    assert "other-host: connection refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(DisposalStatus.NOT_FOUND, 3), (DisposalStatus.FAILED, 1)],
)
def test_delete_propagates_non_success_disposal_status(status, exit_code, monkeypatch, capsys):
    class Control:
        async def dispose_sandbox(self, instance_id, *, timeout):
            assert instance_id == "instance" and timeout == 0.1
            return DisposalResult(status, instance_id, "outcome")

    async def find(*_args):
        return ((object(), _record()),), ()

    monkeypatch.setattr(cli_module, "_find", find)
    monkeypatch.setattr(cli_module, "_control", lambda _probes: Control())

    assert asyncio.run(cli_module._delete((), _arguments())) == exit_code
    assert status.value in capsys.readouterr().out


def test_purge_requires_confirmation_in_noninteractive_json(capsys):
    arguments = _arguments(yes=False)
    assert asyncio.run(cli_module._purge((), arguments)) == 2
    assert "pass --yes" in capsys.readouterr().err


def test_unavailable_host_makes_an_otherwise_successful_purge_partial(monkeypatch, capsys):
    class Control:
        async def purge_thread(self, scope, thread, *, timeout):
            assert (scope, thread, timeout) == ("scope", "thread", 0.1)
            return PurgeResult(PurgeStatus.PURGED, scope, thread, 2, "done")

    manifest = EndpointManifest("missing", "http://127.0.0.1:1", 1)
    probe = cli_module._HostProbe(
        manifest,
        cast(HttpControl, object()),
        "connection refused",
    )
    monkeypatch.setattr(cli_module, "_control", lambda _probes: Control())

    assert asyncio.run(cli_module._purge((probe,), _arguments())) == 1
    payload = capsys.readouterr()
    assert '"status": "partial"' in payload.out
    assert "connection refused" in payload.err


def test_plain_watch_separates_multiple_snapshots(monkeypatch, capsys):
    async def sleep(_interval):
        return None

    monkeypatch.setattr(asyncio, "sleep", sleep)
    arguments = cli_module._parser().parse_args(
        ["watch", "--demo", "--count", "2", "--interval", "0.01"]
    )

    async def probe(_manifests):
        return ()

    monkeypatch.setattr(cli_module, "_probe", probe)

    assert asyncio.run(cli_module._watch(lambda: (), arguments)) == 0
    assert "\n\nSnapshot 2" in capsys.readouterr().out


def test_no_command_opens_the_tui(monkeypatch):
    ran: list[bool] = []

    class Console:
        def __init__(self, _control):
            pass

        async def run_async(self):
            ran.append(True)

    monkeypatch.setattr(cli_module, "SandboxConsole", Console)
    arguments = cli_module._parser().parse_args([])

    assert asyncio.run(cli_module._dispatch(arguments, lambda: ())) == 0
    assert ran == [True]


def test_tui_control_reloads_discovery_for_each_operation(monkeypatch):
    manifest = EndpointManifest("host", "http://127.0.0.1:1", 1)
    memory = MemoryControl.demo(now=1_000)
    loads = 0

    def load():
        nonlocal loads
        loads += 1
        return (manifest,)

    async def probe(manifests):
        assert manifests == (manifest,)
        return (cli_module._HostProbe(manifest, cast(HttpControl, memory)),)

    monkeypatch.setattr(cli_module, "_probe", probe)

    async def check() -> None:
        control = cli_module._ReloadingControl(load)
        records = await control.list_sandboxes()
        assert await control.get_sandbox(records[0].instance_id) == records[0]
        assert (
            await control.dispose_sandbox(records[0].instance_id)
        ).status is DisposalStatus.DISPOSED
        purged = await control.purge_thread(records[1].scope, records[1].thread_id)
        assert purged.status is PurgeStatus.PURGED

    asyncio.run(check())
    assert loads == 4


def test_internal_unknown_command_is_an_assertion():
    with pytest.raises(AssertionError, match="unknown command"):
        asyncio.run(cli_module._dispatch(argparse.Namespace(command="other"), lambda: ()))


def test_default_discovery_loader_is_used_without_demo_or_endpoint(monkeypatch):
    marker = EndpointManifest("found", "http://127.0.0.1:1", 1)
    monkeypatch.setattr(cli_module, "read_manifests", lambda: (marker,))

    async def dispatch(_arguments, loader):
        assert loader() == (marker,)
        return 0

    monkeypatch.setattr(cli_module, "_dispatch", dispatch)
    arguments = cli_module._parser().parse_args(["hosts"])

    assert asyncio.run(cli_module._run(arguments)) == 0


def test_local_discovery_failure_uses_the_stable_cli_error_path(monkeypatch, tmp_path, capsys):
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("MAF_SANDBOX_TUI_RUNTIME_DIR", str(occupied))

    with pytest.raises(SystemExit) as raised:
        cli_module.main(["hosts"])

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert error.startswith("mst: sandbox discovery path is not a directory:")
    assert "Traceback" not in error


def test_keyboard_interrupt_uses_shell_exit_code_130(monkeypatch):
    async def interrupted(_arguments):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_run", interrupted)

    with pytest.raises(SystemExit) as raised:
        cli_module.main(["hosts"])

    assert raised.value.code == 130
