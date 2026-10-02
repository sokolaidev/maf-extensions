"""Crash boundaries and refusal controls for the experimental MXC host store."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "scripts/experiments/mxc_session_patch/host_store.py"
SPEC = importlib.util.spec_from_file_location("mxc_host_store", MODULE)
assert SPEC and SPEC.loader
store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store)
PROFILE = {"runtime": "pinned", "policy": "closed", "machine": "local", "session": "one"}


def candidate(root: Path, content: bytes = b"checkpoint") -> Path:
    root.mkdir()
    (root / "index.json").write_bytes(content)
    return root


def test_publication_result_retry_and_latest_state(tmp_path):
    with store.Store(tmp_path / "db", PROFILE) as db:
        assert db.begin("a", b"first") is None
        db.commit("a", candidate(tmp_path / "a"), b"first-result")
        assert db.begin("b", b"second") is None
        db.commit("b", candidate(tmp_path / "b", b"new-state"), b"second-result")
    with store.Store(tmp_path / "db", PROFILE) as db:
        assert db.begin("a", b"first") == b"first-result"
        assert db.restore(tmp_path / "restored") == "b"
        assert (tmp_path / "restored/index.json").read_bytes() == b"new-state"
        with pytest.raises(store.Refused, match="different request"):
            db.begin("a", b"different")


@pytest.mark.parametrize("point", ["checkpoint_stored", "before_commit", "after_commit"])
def test_real_process_death_preserves_atomic_checkpoint_and_result(tmp_path, point):
    root = tmp_path / "db"
    with store.Store(root, PROFILE) as db:
        db.begin("old", b"old")
        db.commit("old", candidate(tmp_path / "old", b"old-state"), b"old-result")
    new = candidate(tmp_path / "new", b"new-state")
    script = """
import importlib.util, json, os, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('store',sys.argv[1])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with m.Store(Path(sys.argv[2]),json.loads(sys.argv[4])) as db:
    db.begin('new',b'new')
    def crash(point):
        if point==sys.argv[5]: os._exit(73)
    db.commit('new',Path(sys.argv[3]),b'new-result',crash)
"""
    import json

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(MODULE),
            str(root),
            str(new),
            json.dumps(PROFILE),
            point,
        ],
        check=False,
        timeout=20,
    )
    assert child.returncode == 73
    with store.Store(root, PROFILE) as db:
        committed = point == "after_commit"
        assert db.restore(tmp_path / "restored") == ("new" if committed else "old")
        assert (tmp_path / "restored/index.json").read_bytes() == (
            b"new-state" if committed else b"old-state"
        )
        if committed:
            assert db.begin("new", b"new") == b"new-result"
        else:
            with pytest.raises(store.Refused, match="explicit recovery"):
                db.begin("new", b"new")
        assert db.begin("old", b"old") == b"old-result"


def test_pending_calls_cannot_continue_or_replay(tmp_path):
    with store.Store(tmp_path / "db", PROFILE) as db:
        db.begin("a", b"code")
        with pytest.raises(store.Refused, match="explicit recovery"):
            db.begin("b", b"code")
    with store.Store(tmp_path / "db", PROFILE) as db:
        with pytest.raises(store.Refused, match="explicit recovery"):
            db.begin("a", b"code")
        assert db.begin("b", b"new request after explicit recovery") is None


def test_exclusive_owner_profile_and_generation(tmp_path):
    with store.Store(tmp_path / "db", PROFILE) as db:
        with pytest.raises(store.Refused, match="owner"):
            store.Store(tmp_path / "db", PROFILE)
        db.db.execute("UPDATE meta SET generation=generation+1")
        db.db.commit()
        with pytest.raises(store.Refused, match="generation"):
            db.begin("a", b"code")
    with pytest.raises(store.Refused, match="profile"):
        store.Store(tmp_path / "db", PROFILE | {"policy": "changed"})


@pytest.mark.parametrize("corruption", ["chunk", "file", "path", "result", "missing"])
def test_corruption_refuses_without_fresh_fallback(tmp_path, corruption):
    with store.Store(tmp_path / "db", PROFILE) as db:
        db.begin("a", b"code")
        db.commit("a", candidate(tmp_path / "a"), b"result")
        sql = {
            "chunk": "UPDATE chunks SET data=x'00'",
            "file": "UPDATE files SET hash='wrong'",
            "path": "UPDATE files SET path='../outside'",
            "result": "UPDATE calls SET result=x'00'",
            "missing": "DELETE FROM chunks",
        }[corruption]
        db.db.execute(sql)
        db.db.commit()
        with pytest.raises((store.Refused, store.zlib.error)):
            if corruption == "result":
                db.begin("a", b"code")
            else:
                db.restore(tmp_path / "restored")
    assert not (tmp_path / "outside").exists()


def test_failed_capture_preserves_previous_checkpoint(tmp_path, monkeypatch):
    with store.Store(tmp_path / "db", PROFILE) as db:
        db.begin("old", b"old")
        db.commit("old", candidate(tmp_path / "old"), b"old-result")
        db.begin("new", b"new")
        monkeypatch.setattr(store, "MAX_CHECKPOINT", 3)
        with pytest.raises(store.Refused, match="limit"):
            db.commit("new", candidate(tmp_path / "new"), b"result")
        monkeypatch.setattr(store, "MAX_CHECKPOINT", 1024)
        assert db.restore(tmp_path / "restored") == "old"


def test_hard_links_refuse(tmp_path):
    checkpoint = candidate(tmp_path / "candidate")
    os.link(checkpoint / "index.json", checkpoint / "alias.json")
    with store.Store(tmp_path / "db", PROFILE) as db:
        db.begin("a", b"code")
        with pytest.raises(store.Refused, match="non-private"):
            db.commit("a", checkpoint, b"result")


def test_missing_session_metadata_cannot_create_fresh_state(tmp_path):
    with store.Store(tmp_path / "db", PROFILE) as db:
        db.begin("a", b"code")
        db.commit("a", candidate(tmp_path / "a"), b"result")
        db.db.execute("DELETE FROM meta")
        db.db.commit()
    with pytest.raises(store.Refused, match="no session metadata"):
        store.Store(tmp_path / "db", PROFILE)


def test_chart_chunks_detect_loss_and_duplicates(monkeypatch):
    import base64
    import hashlib

    monkeypatch.setitem(sys.modules, "host_store", store)
    spec = importlib.util.spec_from_file_location("host_call", MODULE.with_name("host_call.py"))
    assert spec and spec.loader
    host_call = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host_call)
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 8000
    chunks = [data[i : i + 384] for i in range(0, len(data), 384)]
    lines = [f"MAF_CHART:{i}:{base64.b64encode(chunk).decode()}" for i, chunk in enumerate(chunks)]
    trailer = f"MAF_CHART_END:{len(data)}:{hashlib.sha256(data).hexdigest()}"
    encoded = "\n".join([*lines, trailer])
    assert base64.b64decode(host_call.chart_data(encoded)["chart.png"]) == data
    for bad in [
        "\n".join(lines),
        "\n".join([lines[0], *lines, trailer]),
        "\n".join([*lines[1:], trailer]),
        encoded.replace(trailer, trailer + "0"),
    ]:
        with pytest.raises(host_call.Refused):
            host_call.chart_data(bad)
