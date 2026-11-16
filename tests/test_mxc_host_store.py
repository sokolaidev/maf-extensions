"""Crash boundaries and refusal controls for the experimental MXC host store."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    host_call = importlib.import_module("scripts.experiments.mxc_session_patch.host_call")
    store = importlib.import_module("scripts.experiments.mxc_session_patch.host_store")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

PROFILE = {"runtime": "pinned", "policy": "closed", "machine": "local", "session": "one"}


@pytest.mark.parametrize("bounded", [False, True])
def test_probe_supervision_preserves_legacy_and_bounded_payloads(tmp_path, monkeypatch, bounded):
    import base64

    real_popen = subprocess.Popen
    program = r"""
import base64, hashlib, json, sys
from pathlib import Path
assert sys.stdin.buffer.read(8) == b'MXCOWN1\n'
assert Path(sys.argv[4]).read_bytes() == b'code'
report = Path(sys.argv[5])
if sys.argv[1] == 'call-bounded':
    assert sys.argv[6:] == ['100']
    report.with_suffix('.output').write_bytes(b'ok')
    report.write_text(json.dumps({'captured': True, 'output': {
        'limit_bytes': 100, 'retained_bytes': 2, 'omitted_bytes': 0,
        'omitted_bytes_saturated': False, 'truncated': False}}))
else:
    assert sys.argv[1] == 'call-owned' and len(sys.argv) == 6
    data = b'\x89PNG\r\n\x1a\nchart'
    print('MAF_CHART:0:' + base64.b64encode(data).decode())
    print(f'MAF_CHART_END:{len(data)}:{hashlib.sha256(data).hexdigest()}')
    report.write_text(json.dumps({'captured': True}))
print('diagnostic', file=sys.stderr)
"""

    def spawn(command, **kwargs):
        return real_popen([sys.executable, "-c", program, *command[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    result = json.loads(
        host_call.execute(
            tmp_path / "helper", tmp_path / "startup", tmp_path, b"code", 100 if bounded else None
        )
    )
    if bounded:
        assert base64.b64decode(result["console_base64"]) == b"ok"
        assert result["output"]["limit_bytes"] == 100
    else:
        assert base64.b64decode(result["artifacts"]["chart.png"]) == b"\x89PNG\r\n\x1a\nchart"
        assert "MAF_CHART_END:" in result["combined_output"]
        assert result["native_stderr"].strip() == "diagnostic"


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
import json, os, sys
from scripts.experiments.mxc_session_patch import host_store as m
from pathlib import Path
with m.Store(Path(sys.argv[1]),json.loads(sys.argv[3])) as db:
    db.begin('new',b'new')
    def crash(point):
        if point==sys.argv[4]: os._exit(73)
    db.commit('new',Path(sys.argv[2]),b'new-result',crash)
"""
    import json

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
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
        with pytest.raises((store.Refused, zlib.error)):
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


def test_chart_chunks_detect_loss_and_duplicates():
    import base64
    import hashlib

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


@pytest.mark.parametrize("corrupt", [b"invalid-zlib", zlib.compress(b"wrong-state")])
def test_publication_replaces_corrupt_historical_chunk(tmp_path, corrupt):
    root = tmp_path / "db"
    old = b"old-state"
    chunk_hash = hashlib.sha256(old).hexdigest()
    with store.Store(root, PROFILE) as db:
        db.begin("old", b"old")
        db.commit("old", candidate(tmp_path / "old", old), b"old-result")
        db.begin("current", b"current")
        db.commit("current", candidate(tmp_path / "current", b"current-state"), b"current-result")
        db.db.execute("UPDATE chunks SET data=? WHERE hash=?", (corrupt, chunk_hash))
        db.db.commit()
        assert db.restore(tmp_path / "before") == "current"
        db.begin("new", b"new")
        db.commit("new", candidate(tmp_path / "new", old), b"new-result")
    with store.Store(root, PROFILE) as db:
        assert db.begin("new", b"new") == b"new-result"
        assert db.restore(tmp_path / "restored") == "new"
        assert (tmp_path / "restored/index.json").read_bytes() == old
        assert db.db.execute("SELECT count(*) FROM chunks").fetchone() == (2,)
        assert db.begin("old", b"old") == b"old-result"


@pytest.mark.parametrize("fault", ["checkpoint_stored", "before_commit"])
def test_chunk_replacement_rolls_back_with_failed_publication(tmp_path, fault):
    root = tmp_path / "db"
    old = b"old-state"
    chunk_hash = hashlib.sha256(old).hexdigest()
    with store.Store(root, PROFILE) as db:
        db.begin("old", b"old")
        db.commit("old", candidate(tmp_path / "old", old), b"old-result")
        db.begin("current", b"current")
        db.commit("current", candidate(tmp_path / "current", b"current-state"), b"current-result")
        db.db.execute("UPDATE chunks SET data=? WHERE hash=?", (b"corrupt", chunk_hash))
        db.db.commit()
        db.begin("new", b"new")

        def fail(point):
            if point == fault:
                raise RuntimeError("publication interrupted")

        with pytest.raises(RuntimeError, match="publication interrupted"):
            db.commit("new", candidate(tmp_path / "new", old), b"new-result", fail)
    with store.Store(root, PROFILE) as db:
        assert db.restore(tmp_path / "restored") == "current"
        assert (tmp_path / "restored/index.json").read_bytes() == b"current-state"
        assert db.db.execute("SELECT data FROM chunks WHERE hash=?", (chunk_hash,)).fetchone() == (
            b"corrupt",
        )
        with pytest.raises(store.Refused, match="explicit recovery"):
            db.begin("new", b"new")
        assert db.begin("current", b"current") == b"current-result"
