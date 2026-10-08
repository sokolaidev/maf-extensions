"""Keep file file_requests bounded and replay identity sensitive to host policy."""

from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    files = importlib.import_module("scripts.experiments.mxc_files_patch.request")
    storage = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))


@pytest.fixture
def file_request():
    return files.Request(
        b"print('analysis')",
        (files.Input("data/input.csv", b"value\n1\n"),),
        ("chart.png",),
        files.FileLimits(2, 16, 2, 1024, 16),
    )


def test_defaults_and_arbitrary_bytes(file_request):
    upload = files.Input("binary", bytes(range(256)))
    assert upload.lifecycle == "call"
    assert upload.replace is False
    assert files.Input("state", b"", "session", True).replace is True
    result = replace(
        file_request,
        inputs=(upload,),
        limits=replace(file_request.limits, input_bytes=256, file_bytes=256),
    )
    assert b'"bytes":256' in result.identity()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "../escape",
        "nested/../escape",
        "a//b",
        "a/./b",
        "a/",
        "a\\b",
        "C:/file",
        "file:stream",
        "nul",
        "CON.txt",
        "CON .txt",
        "COM¹",
        "LPT².txt",
        "dir/LPT1",
        "trailing.",
        "trailing ",
        "a\x00b",
        "a\nb",
        "a?b",
        "a" * 513,
        "a/" * 16 + "b",
        "e\u0301",
        "\ud800",
    ],
)
def test_unsafe_names_refused(path):
    with pytest.raises(files.Refused):
        files.Input(path, b"")


@pytest.mark.parametrize("paths", [("a", "a"), ("a", "A"), ("a", "a/b"), ("A", "a/b")])
def test_aliases_and_parent_collisions_refused(file_request, paths):
    with pytest.raises(files.Refused):
        replace(file_request, inputs=tuple(files.Input(path, b"") for path in paths))
    with pytest.raises(files.Refused):
        replace(file_request, artifacts=paths)


def test_transfer_budgets_refuse_before_admission(file_request):
    exact = replace(
        file_request, inputs=(files.Input("one", b"a" * 8), files.Input("two", b"b" * 8))
    )
    assert exact.identity()
    for inputs in (
        (files.Input("one", b"a" * 17),),
        (files.Input("one", b"a" * 9), files.Input("two", b"b" * 8)),
        tuple(files.Input(str(i), b"") for i in range(3)),
    ):
        with pytest.raises(files.Refused):
            replace(file_request, inputs=inputs)
    with pytest.raises(files.Refused):
        replace(file_request, artifacts=("one", "two", "three"))


@pytest.mark.parametrize(
    "field", ["input_files", "input_bytes", "artifact_files", "artifact_bytes", "file_bytes"]
)
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, 2**64])
def test_limits_refuse_invalid_values(file_request, field, value):
    with pytest.raises(files.Refused):
        replace(file_request.limits, **{field: value})


def test_mutable_payload_or_inventory_refused(file_request):
    with pytest.raises(files.Refused):
        files.Input("input", bytearray(b"data"))
    with pytest.raises(files.Refused):
        replace(file_request, inputs=list(file_request.inputs))
    with pytest.raises(files.Refused):
        replace(file_request, artifacts=list(file_request.artifacts))
    with pytest.raises(files.Refused):
        files.Input("input", b"", lifecycle="forever")
    with pytest.raises(files.Refused):
        files.Input("input", b"", replace=1)
    for code in (b"a" * 65537, b"\xff", "pass"):
        with pytest.raises(files.Refused):
            replace(file_request, code=code)


def test_retry_after_reopen_binds_file_content_lifecycle_authority_and_limits(
    file_request, tmp_path
):
    limits = storage.Limits(10**7, 10**7, checkpoint_bytes=1024, files=2)
    root = tmp_path / "store"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "state").write_bytes(b"state")
    with storage.SharedStore(root, "session", {"format": files.FORMAT}, limits) as store:
        assert store.begin("one", file_request.identity()) is None
        store.commit("one", candidate, b"saved result")
    with storage.SharedStore(root, "session", {"format": files.FORMAT}, limits) as store:
        assert store.begin("one", file_request.identity()) == b"saved result"
        changes = (
            replace(file_request, code=b"other code"),
            replace(file_request, inputs=(replace(file_request.inputs[0], data=b"other"),)),
            replace(file_request, inputs=(replace(file_request.inputs[0], name="other.csv"),)),
            replace(file_request, inputs=(replace(file_request.inputs[0], lifecycle="session"),)),
            replace(file_request, inputs=(replace(file_request.inputs[0], replace=True),)),
            replace(file_request, artifacts=("other.png",)),
            replace(file_request, limits=replace(file_request.limits, artifact_bytes=512)),
        )
        for changed in changes:
            with pytest.raises(files.Refused, match="different request"):
                store.begin("one", changed.identity())
        store.audit_usage()


def test_inventory_order_does_not_change_retry_identity(file_request):
    original = replace(
        file_request, inputs=(files.Input("a", b"a"), files.Input("b", b"b")), artifacts=("x", "y")
    )
    reordered = replace(
        original,
        inputs=tuple(reversed(original.inputs)),
        artifacts=tuple(reversed(original.artifacts)),
    )
    assert original.identity() == reordered.identity()
