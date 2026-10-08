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
    transport = importlib.import_module("scripts.experiments.mxc_streams_patch.transport")
    shared = importlib.import_module("scripts.experiments.mxc_streams_patch.shared_call")
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
    }
    for name in ("stdout", "stderr"):
        control["streams"][name] = {
            "retained_bytes": 0,
            "omitted_bytes": 0,
            "omitted_bytes_saturated": False,
        }
        report.with_suffix(f".{name}.bin").write_bytes(b"")
    report.write_text(json.dumps(control), encoding="utf-8")
    return report, control


def test_full_independent_binary_streams_fit_reserved_result_and_survive_reopen(native, tmp_path):
    report, control = native
    data = bytes(range(256)) * (transport.STREAM_LIMIT // 256)
    for name in ("stdout", "stderr"):
        report.with_suffix(f".{name}.bin").write_bytes(data)
        control["streams"][name].update(retained_bytes=len(data), omitted_bytes=11)
    report.write_text(json.dumps(control), encoding="utf-8")
    result = transport.read_result(report, True)
    assert 2 * transport.STREAM_LIMIT < len(result) < transport.RESULT_LIMIT
    for name in ("stdout", "stderr"):
        assert base64.b64decode(json.loads(result)["streams"][name]["base64"]) == data
    limits = storage.Limits(
        10**8, 10**8, checkpoint_bytes=1024, files=2, result_bytes=transport.RESULT_LIMIT
    )
    checkpoint = tmp_path / "candidate"
    checkpoint.mkdir()
    (checkpoint / "state").write_bytes(b"checkpoint")
    root = tmp_path / "store"
    with storage.SharedStore(root, "session", {"format": transport.FORMAT}, limits) as store:
        assert store.begin("one", b"code") is None
        store.commit("one", checkpoint, result)
        store.audit_usage()
    with storage.SharedStore(root, "session", {"format": transport.FORMAT}, limits) as store:
        assert store.begin("one", b"code") == result
        store.audit_usage()
    assert storage.Limits(10**8, 10**8).result_bytes == 2 * transport.STREAM_LIMIT
    with pytest.raises(transport.Refused):
        replace(limits, result_bytes=storage.MAX_SHARED_RESULT + 1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(completed=1),
        lambda c: c.update(checkpoint=1),
        lambda c: c.update(format="other"),
        lambda c: c.update(limit_bytes=True),
        lambda c: c.update(unexpected="value"),
        lambda c: c["streams"].update(extra={}),
        lambda c: c["streams"]["stdout"].update(retained_bytes=True),
        lambda c: c["streams"]["stdout"].update(retained_bytes=-1),
        lambda c: c["streams"]["stdout"].update(retained_bytes=transport.STREAM_LIMIT + 1),
        lambda c: c["streams"]["stderr"].update(omitted_bytes=-1),
        lambda c: c["streams"]["stderr"].update(omitted_bytes=2**64),
        lambda c: c["streams"]["stderr"].update(omitted_bytes=1),
        lambda c: c["streams"]["stderr"].update(omitted_bytes_saturated=True),
        lambda c: c["streams"]["stderr"].update(omitted_bytes_saturated=0),
        lambda c: c["streams"].update(stdout=[]),
    ],
)
def test_invalid_control_is_refused(native, mutate):
    report, control = native
    mutate(control)
    report.write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(transport.Refused):
        transport.read_result(report, True)


@pytest.mark.parametrize(
    "raw", [b"{", b"\xff", b"{}" * 600, b'{"completed":false,"completed":true}', b"[]", b"null"]
)
def test_malformed_duplicate_or_oversized_control_is_refused(native, raw):
    report, _ = native
    report.write_bytes(raw)
    with pytest.raises(transport.Refused):
        transport.read_result(report, True)


def test_guest_json_cannot_substitute_for_native_completion(native):
    report, control = native
    guest = json.dumps(control).encode()
    report.unlink()
    report.with_suffix(".stdout.bin").write_bytes(guest)
    with pytest.raises(FileNotFoundError):
        transport.read_result(report, True)


def test_payload_size_and_checkpoint_mode_are_checked(native):
    report, _ = native
    with pytest.raises(transport.Refused):
        transport.read_result(report, False)
    report.with_suffix(".stderr.bin").write_bytes(b"unexpected bytes")
    with pytest.raises(transport.Refused):
        transport.read_result(report, True)


def test_insufficient_result_allowance_refuses_before_launch_or_reservation(tmp_path, monkeypatch):
    limits = storage.Limits(10**8, 10**8, checkpoint_bytes=1024, files=2)
    monkeypatch.setattr(shared, "execute", lambda *a, **kw: pytest.fail("must not launch"))
    with storage.SharedStore(
        tmp_path / "store", "session", {"format": transport.FORMAT}, limits
    ) as store:
        before = store.usage()
        with pytest.raises(transport.Refused, match="result allowance"):
            shared.call(
                store,
                "one",
                b"pass",
                tmp_path / "absent",
                tmp_path / "absent",
                storage.ScratchLimits(10**7, 2000, 2 * 10**7),
            )
        assert store.usage() == before
        assert store.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


def test_scratch_reserves_both_payloads_diagnostics_and_control_before_launch(
    tmp_path, monkeypatch
):
    limits = storage.Limits(
        10**8, 10**8, checkpoint_bytes=1024, files=2, result_bytes=transport.RESULT_LIMIT
    )
    minimum = (
        2 * limits.checkpoint_bytes
        + 4 * transport.STREAM_LIMIT
        + len(b"pass")
        + 2 * transport.CONTROL_LIMIT
    )
    monkeypatch.setattr(shared, "execute", lambda *a, **kw: pytest.fail("must not launch"))
    with storage.SharedStore(
        tmp_path / "store", "session", {"format": transport.FORMAT}, limits
    ) as store:
        before = store.usage()
        with pytest.raises(transport.Refused, match="scratch allowance"):
            shared.call(
                store,
                "one",
                b"pass",
                tmp_path / "absent",
                tmp_path / "absent",
                storage.ScratchLimits(minimum - 1, 2000, 2 * minimum),
            )
        assert store.usage() == before
        assert store.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
