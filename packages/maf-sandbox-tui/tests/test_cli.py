"""Non-interactive MST command behavior."""

from __future__ import annotations

import json

import pytest

from maf_sandbox_tui.cli import main

_READY_INSTANCE = "f2ecba87b2ce44659a66fd28fd0a1002"


def test_list_accepts_options_after_the_command_and_filters_json(capsys):
    main(["list", "--demo", "--json", "--state", "ready"])
    payload = json.loads(capsys.readouterr().out)
    assert [item["instance_id"] for item in payload] == [_READY_INSTANCE]


def test_legacy_json_flag_still_prints_an_inventory(capsys):
    main(["--demo", "--json"])
    assert len(json.loads(capsys.readouterr().out)) == 3


def test_hosts_reports_the_opted_in_demo_process(capsys):
    main(["--demo", "hosts", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["source_id"] == "mst-demo"
    assert payload[0]["status"] == "healthy"


def test_demo_host_identifier_filters_its_reported_inventory(capsys):
    main(["list", "--demo", "--host", "mst-demo", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert len(payload) == 3
    assert {item["source_id"] for item in payload} == {"mst-demo"}


def test_show_prints_one_exact_record(capsys):
    main(["show", _READY_INSTANCE, "--demo", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["instance_id"] == _READY_INSTANCE
    assert payload["thread_id"] == "forecast-042"


def test_show_uses_a_distinct_not_found_exit_code(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["show", "missing", "--demo", "--json"])
    assert raised.value.code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "not_found"


def test_delete_requires_noninteractive_confirmation(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["delete", _READY_INSTANCE, "--demo", "--json"])
    assert raised.value.code == 2
    assert "pass --yes" in capsys.readouterr().err


def test_delete_prints_the_disposal_result(capsys):
    main(["delete", _READY_INSTANCE, "--demo", "--yes", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "disposed",
        "instance_id": _READY_INSTANCE,
        "message": "Sandbox disposed.",
    }


def test_purge_thread_prints_the_aggregate_result(capsys):
    main(
        [
            "purge-thread",
            "--demo",
            "--scope",
            "tenant-labs",
            "--thread",
            "forecast-042",
            "--yes",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "purged"
    assert payload["disposed"] == 1


def test_watch_can_emit_one_bounded_jsonl_snapshot(capsys):
    main(["watch", "--demo", "--count", "1", "--jsonl", "--kind", "codeact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert len(payload["sandboxes"]) == 2
    assert {item["kind"] for item in payload["sandboxes"]} == {"codeact"}
