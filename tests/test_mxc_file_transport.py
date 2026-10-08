"""Reject forged or malformed native completion before durable publication."""

from __future__ import annotations

import base64
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    transport = importlib.import_module("scripts.experiments.mxc_files_patch.transport")
    shared = importlib.import_module("scripts.experiments.mxc_files_patch.shared_call")
    storage = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def native(tmp_path):
    report = tmp_path / "native.json"
    control = {
        "format": transport.FORMAT,
        "completed": True,
        "checkpoint": True,
        "limit_bytes": transport.STREAM_LIMIT,
        "streams": {},
        "artifacts": [{"name": "out.bin", "offset": 0, "bytes": 3}],
        "workspace_bytes": 0,
        "workspace_files": 0,
    }
    for name in ("stdout", "stderr"):
        control["streams"][name] = {
            "retained_bytes": 0,
            "omitted_bytes": 0,
            "omitted_bytes_saturated": False,
        }
        report.with_suffix(f".{name}.bin").write_bytes(b"")
    report.with_suffix(".artifacts.bin").write_bytes(b"\x00\xffA")
    report.write_text(json.dumps(control), encoding="utf-8")
    return report, control


def request():
    model = importlib.import_module("scripts.experiments.mxc_files_patch.request")
    return model.Request(b"pass", (), ("out.bin",), model.FileLimits())


def test_artifacts_replay_after_reopen_without_native_helper(native, tmp_path):
    report, _ = native
    req = request()
    result = transport.read_result(report, True, req)
    assert base64.b64decode(json.loads(result)["artifacts"][0]["base64"]) == b"\x00\xffA"
    limits = storage.Limits(
        10**8, 10**8, checkpoint_bytes=1024, files=3, result_bytes=transport.result_limit(req)
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "state").write_bytes(b"state")
    (candidate / "workspace.bin").write_bytes(b"files")
    with storage.SharedStore(
        tmp_path / "store", "one", {"format": transport.FORMAT}, limits
    ) as store:
        store.begin("one", req.identity())
        store.commit("one", candidate, result)
    with storage.SharedStore(
        tmp_path / "store", "one", {"format": transport.FORMAT}, limits
    ) as store:
        assert (
            shared.call(
                store,
                "one",
                req,
                tmp_path / "absent",
                tmp_path / "absent",
                storage.ScratchLimits(1, 1, 1),
            )
            == result
        )
        store.audit_usage()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(completed=1),
        lambda c: c.update(workspace_bytes=True),
        lambda c: c.update(workspace_files=257),
        lambda c: c.update(artifacts=[]),
        lambda c: c["artifacts"][0].update(name="../out"),
        lambda c: c["artifacts"][0].update(offset=1),
        lambda c: c["artifacts"][0].update(bytes=2),
        lambda c: c["artifacts"][0].update(bytes=True),
        lambda c: c["artifacts"].append({"name": "extra", "offset": 3, "bytes": 0}),
        lambda c: c["streams"]["stdout"].update(omitted_bytes=1),
    ],
)
def test_malformed_completion_never_authorizes_publication(native, mutate):
    report, control = native
    mutate(control)
    report.write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(transport.Refused):
        transport.read_result(report, True, request())


def test_budget_refusal_precedes_admission(tmp_path, monkeypatch):
    req = request()
    limits = storage.Limits(10**8, 10**8, checkpoint_bytes=1024, files=3)
    monkeypatch.setattr(shared, "execute", lambda *a, **kw: pytest.fail("unexpected helper"))
    for limits, scratch, reason in (
        (limits, storage.ScratchLimits(10**8, 10000, 10**8), "result allowance"),
        (
            replace(limits, result_bytes=transport.result_limit(req)),
            storage.ScratchLimits(1024, 10000, 10**8),
            "scratch allowance",
        ),
    ):
        with storage.SharedStore(
            tmp_path / reason, "one", {"format": transport.FORMAT}, limits
        ) as store:
            before = store.usage()
            with pytest.raises(transport.Refused, match=reason):
                shared.call(store, "one", req, tmp_path / "absent", tmp_path / "absent", scratch)
            assert store.usage() == before
            assert store.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


def test_maximum_file_envelope_fits_explicit_store_ceiling():
    model = importlib.import_module("scripts.experiments.mxc_files_patch.request")
    req = model.Request(b"pass", (), (), model.FileLimits(artifact_bytes=64 * 1024**2))
    assert transport.result_limit(req) <= storage.MAX_SHARED_RESULT
    assert storage.Limits(10**9, 10**9).result_bytes == 2 * 1024**2


@pytest.mark.parametrize(
    "old,new,replace,valid",
    [
        ("keep", "keep", False, False),
        ("keep", "keep", True, True),
        ("KEEP", "keep", True, False),
        ("Straße", "STRASSE", True, False),
        ("é", "é", True, True),
        ("e\u0301", "é", True, False),
        ("dir/file", "dir", True, False),
        ("file", "file/child", True, False),
        ("keep", "new", False, True),
    ],
)
def test_restored_upload_authority_uses_portable_exact_names(tmp_path, old, new, replace, valid):
    model = importlib.import_module("scripts.experiments.mxc_files_patch.request")
    req = model.Request(
        b"pass", (model.Input(new, b"", "session", replace),), (), model.FileLimits()
    )
    (tmp_path / "workspace.json").write_text(
        json.dumps({"files": [{"name": "session/" + old}]}), encoding="utf-8"
    )
    assert transport.uploads_valid(tmp_path, req) is valid


def test_prepare_keeps_replacement_refusal_inside_identified_helper(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    transport.prepare(work, request(), True, base)
    assert json.loads((work / "request.json").read_text())["uploads_valid"] is False
