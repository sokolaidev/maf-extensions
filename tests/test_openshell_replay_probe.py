"""The retention fixture must alter exactly one create receipt in a stopped isolated gateway."""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def injection(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/experiments"))
    probe = importlib.import_module("openshell_replay_probe")
    database = tmp_path / "gateway.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE objects (id TEXT, object_type TEXT, payload BLOB, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        for row_id, kind, owner in (
            ("match", "mutation_admission_v1", "original"),
            ("other", "mutation_admission_v1", "other"),
            ("outside", "another_type", "original"),
        ):
            payload = {
                "success": {"Ordinary": {"Sandbox": {"id": owner}}},
                "completed_at_ms": 123,
                "payload_hash": "unchanged",
            }
            db.execute(
                "INSERT INTO objects VALUES (?,?,?,?,?)",
                (row_id, kind, json.dumps(payload).encode(), 123, 123),
            )
    db.close()
    stopped = []
    started = []
    gateway = SimpleNamespace(
        stop=lambda: stopped.append(True),
        start=lambda: started.append(True),
        process=SimpleNamespace(poll=lambda: 0 if stopped else None),
    )
    return probe, {"database": database, "gateway": gateway}, stopped, started


def rows(control):
    with sqlite3.connect(control["database"]) as db:
        result = db.execute("SELECT * FROM objects ORDER BY id").fetchall()
    db.close()
    return {row[0]: row for row in result}


@pytest.mark.parametrize("mode", ["expired_success", "unresolved"])
def test_injection_changes_only_selected_create_receipt(injection, mode):
    probe, control, stopped, started = injection
    before = rows(control)
    probe.inject_admission(control, "original", mode)
    after = rows(control)
    assert stopped == started == [True]
    assert after["other"] == before["other"] and after["outside"] == before["outside"]
    payload = json.loads(after["match"][2])
    assert payload["payload_hash"] == "unchanged"
    assert abs(after["match"][3] - (int(time.time() * 1000) - 25 * 60 * 60 * 1000)) < 2000
    if mode == "expired_success":
        assert payload["success"] == json.loads(before["match"][2])["success"]
        assert payload["completed_at_ms"] == after["match"][3]
    else:
        assert payload["success"] is None and payload["completed_at_ms"] is None


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_receipt_never_changes_database(injection, count):
    probe, control, _, started = injection
    with sqlite3.connect(control["database"]) as db:
        if count == 0:
            db.execute("DELETE FROM objects WHERE id='match'")
        else:
            db.execute(
                "INSERT INTO objects SELECT 'duplicate',object_type,payload,created_at_ms,updated_at_ms FROM objects WHERE id='match'"
            )
    db.close()
    before = rows(control)
    with pytest.raises(RuntimeError, match="expected one"):
        probe.inject_admission(control, "original", "expired_success")
    assert rows(control) == before and started == [True]


def test_unknown_injection_never_changes_database(injection):
    probe, control, _, started = injection
    before = rows(control)
    with pytest.raises(ValueError, match="unknown fault"):
        probe.inject_admission(control, "original", "invalid")
    assert rows(control) == before and started == [True]


def test_running_gateway_refuses_database_injection(injection):
    probe, control, _, started = injection
    control["gateway"].process.poll = lambda: None
    before = rows(control)
    with pytest.raises(RuntimeError, match="did not stop"):
        probe.inject_admission(control, "original", "expired_success")
    assert rows(control) == before and not started
