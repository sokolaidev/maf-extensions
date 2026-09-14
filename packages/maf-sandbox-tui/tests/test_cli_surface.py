"""Complete command-line surface and entry-point coverage for MST."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from importlib.metadata import version

import pytest

import maf_sandbox_tui.cli as cli_module
from maf_sandbox_tui import MemoryControl, SandboxControlServer
from maf_sandbox_tui._update import Installation, InstallationKind

_READY_INSTANCE = "f2ecba87b2ce44659a66fd28fd0a1002"

_EXPECTED_OPTIONS = {
    None: {"-h", "--help", "--demo", "--endpoint", "--source", "--json"},
    "version": {"-h", "--help", "--json"},
    "update": {"-h", "--help", "--check", "--to", "--prerelease", "--timeout", "--json"},
    "hosts": {"-h", "--help", "--demo", "--endpoint", "--source", "--json"},
    "list": {
        "-h",
        "--help",
        "--demo",
        "--endpoint",
        "--source",
        "--json",
        "--host",
        "--backend",
        "--scope",
        "--thread",
        "--kind",
        "--state",
        "--older-than",
    },
    "show": {"-h", "--help", "--demo", "--endpoint", "--source", "--json"},
    "watch": {
        "-h",
        "--help",
        "--demo",
        "--endpoint",
        "--source",
        "--json",
        "--jsonl",
        "--interval",
        "--count",
        "--host",
        "--backend",
        "--scope",
        "--thread",
        "--kind",
        "--state",
        "--older-than",
    },
    "delete": {
        "-h",
        "--help",
        "--demo",
        "--endpoint",
        "--source",
        "--yes",
        "--timeout",
        "--json",
    },
    "purge-thread": {
        "-h",
        "--help",
        "--demo",
        "--endpoint",
        "--source",
        "--scope",
        "--thread",
        "--yes",
        "--timeout",
        "--json",
    },
}


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions  # pyright: ignore[reportPrivateUsage]
        for option in action.option_strings
    }


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        action
        for action in parser._actions  # pyright: ignore[reportPrivateUsage]
        if action.dest == "command"
    )
    return dict(action.choices)  # type: ignore[attr-defined]


def test_cli_surface_manifest_covers_every_command_and_option():
    parser = cli_module._parser()
    actual: dict[str | None, set[str]] = {None: _options(parser)}
    actual.update({name: _options(command) for name, command in _subcommands(parser).items()})

    assert actual == _EXPECTED_OPTIONS


@pytest.mark.parametrize("help_option", ["-h", "--help"])
@pytest.mark.parametrize(
    "command",
    [None, "version", "update", "hosts", "list", "show", "watch", "delete", "purge-thread"],
)
def test_every_help_option_exits_successfully(command, help_option, capsys):
    arguments = [help_option] if command is None else [command, help_option]

    with pytest.raises(SystemExit) as raised:
        cli_module.main(arguments)

    assert raised.value.code == 0
    assert "usage: mst" in capsys.readouterr().out


def test_list_accepts_every_filter_and_duration_unit(capsys):
    cli_module.main(
        [
            "list",
            "--demo",
            "--json",
            "--host",
            "research-agent",
            "--backend",
            "hyperlight",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
            "--kind",
            "codeact",
            "--state",
            "ready",
            "--older-than",
            "1m",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert [record["instance_id"] for record in payload] == [_READY_INSTANCE]


@pytest.mark.parametrize(
    ("age", "expected"),
    [("30s", 1), ("0.01h", 1), ("0.0001d", 2), ("60", 1)],
)
def test_older_than_accepts_every_documented_unit(age, expected, capsys):
    cli_module.main(["list", "--demo", "--older-than", age, "--json"])
    assert len(json.loads(capsys.readouterr().out)) == expected


def test_watch_accepts_every_filter_timing_option_and_json_alias(capsys):
    cli_module.main(
        [
            "watch",
            "--demo",
            "--json",
            "--interval",
            "0.001",
            "--count",
            "2",
            "--host",
            "research-agent",
            "--backend",
            "hyperlight",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
            "--kind",
            "codeact",
            "--state",
            "ready",
            "--older-than",
            "1m",
        ]
    )

    snapshots = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(snapshots) == 2
    assert all(item["sandboxes"][0]["instance_id"] == _READY_INSTANCE for item in snapshots)


def test_plain_output_is_available_for_every_read_command(capsys, monkeypatch, tmp_path):
    installation = Installation(InstallationKind.VIRTUAL_ENVIRONMENT, tmp_path / ".venv")
    monkeypatch.setattr(cli_module, "inspect_installation", lambda: installation)

    cli_module.main(["version"])
    assert "mst " in capsys.readouterr().out

    cli_module.main(["hosts", "--demo"])
    assert "SOURCE" in capsys.readouterr().out

    cli_module.main(["list", "--demo"])
    assert "INSTANCE" in capsys.readouterr().out

    cli_module.main(["show", _READY_INSTANCE, "--demo"])
    assert _READY_INSTANCE in capsys.readouterr().out

    cli_module.main(["watch", "--demo", "--count", "1"])
    assert "Snapshot 1" in capsys.readouterr().out


def test_direct_endpoint_and_source_options_reach_the_named_host(capsys, tmp_path):
    async def check() -> None:
        async with SandboxControlServer(
            MemoryControl.demo(),
            source_id="server-label",
            manifest_directory=tmp_path,
        ) as server:
            await asyncio.to_thread(
                cli_module.main,
                [
                    "hosts",
                    "--endpoint",
                    f"{server.endpoint}/",
                    "--source",
                    "direct-label",
                    "--json",
                ],
            )

    asyncio.run(check())

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["source_id"] == "direct-label"
    assert payload[0]["status"] == "healthy"
    assert not payload[0]["endpoint"].endswith("/")


def test_delete_accepts_timeout_and_plain_output(capsys):
    cli_module.main(["delete", _READY_INSTANCE, "--demo", "--yes", "--timeout", "0.5"])
    assert f"disposed: Sandbox disposed. ({_READY_INSTANCE})" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "kept"),
    [
        (["delete", _READY_INSTANCE, "--demo"], "Sandbox kept."),
        (
            [
                "purge-thread",
                "--demo",
                "--scope",
                "tenant-labs",
                "--thread",
                "forecast-042",
            ],
            "Conversation kept.",
        ),
    ],
)
def test_interactive_destructive_commands_can_be_declined(
    command: Sequence[str], kept: str, monkeypatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(SystemExit) as raised:
        cli_module.main(command)

    assert raised.value.code == 4
    assert kept in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        ["delete", _READY_INSTANCE, "--demo"],
        [
            "purge-thread",
            "--demo",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
        ],
    ],
)
def test_interactive_destructive_commands_accept_yes(command, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    cli_module.main(command)

    output = capsys.readouterr().out.lower()
    assert "disposed" in output or "purged" in output


def test_purge_accepts_timeout_and_plain_output(capsys):
    cli_module.main(
        [
            "purge-thread",
            "--demo",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
            "--yes",
            "--timeout",
            "0.5",
        ]
    )
    assert "purged: Conversation purged across 1 host(s). Disposed: 1." in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["watch", "--demo", "--interval", "0"],
        ["watch", "--demo", "--interval", "nan"],
        ["watch", "--demo", "--count", "-1"],
        ["watch", "--demo", "--count", "one"],
        ["list", "--demo", "--older-than", "-1"],
        ["list", "--demo", "--older-than", "forever"],
        ["list", "--demo", "--state", "unknown"],
        ["delete", _READY_INSTANCE, "--demo", "--timeout", "inf"],
        ["update", "--timeout", "zero"],
        ["update", "--check", "--to", "1.0.0"],
    ],
)
def test_invalid_option_values_are_rejected(arguments):
    with pytest.raises(SystemExit) as raised:
        cli_module.main(arguments)
    assert raised.value.code == 2


def _entry_point(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "maf_sandbox_tui", *arguments],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def test_every_command_runs_through_the_real_module_entry_point():
    installed = version("maf-sandbox-tui")
    commands = {
        "version": ["version", "--json"],
        "update": ["update", "--to", installed, "--json"],
        "hosts": ["hosts", "--demo", "--json"],
        "list": ["list", "--demo", "--json"],
        "show": ["show", _READY_INSTANCE, "--demo", "--json"],
        "watch": ["watch", "--demo", "--count", "1", "--jsonl"],
        "delete": ["delete", _READY_INSTANCE, "--demo", "--yes", "--json"],
        "purge-thread": [
            "purge-thread",
            "--demo",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
            "--yes",
            "--json",
        ],
    }

    for name, arguments in commands.items():
        completed = _entry_point(*arguments)
        assert completed.returncode == 0, f"{name}: {completed.stdout}\n{completed.stderr}"
        assert json.loads(completed.stdout)
