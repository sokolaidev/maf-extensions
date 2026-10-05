"""Pin storage overlays, generated builds and the bounded native command contract."""

from __future__ import annotations

import hashlib
import importlib
import json
import tomllib
from pathlib import Path

import pytest

storage = importlib.import_module("scripts.experiments.mxc_session_patch.storage_patch")
output = importlib.import_module("scripts.experiments.mxc_session_patch.output_patch")
store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
shared = importlib.import_module("scripts.experiments.mxc_session_patch.shared_call")
ROOT = Path(__file__).parents[1] / "scripts/experiments/mxc_session_patch"


def test_retained_overlays_match_their_pins():
    metadata = json.loads((ROOT / "storage-patch.json").read_text(encoding="utf-8"))
    assert (
        hashlib.sha256((ROOT / "output-patch.json").read_bytes()).hexdigest()
        == metadata["prerequisite_sha256"]
    )
    for label, item in metadata["patches"].items():
        assert (
            hashlib.sha256((ROOT / f"storage-{label}.patch").read_bytes()).hexdigest()
            == item["sha256"]
        )


@pytest.mark.parametrize("bounded_storage", [False, True])
def test_generated_manifest_and_lock_preserve_versions_and_one_crate_identity(
    tmp_path, bounded_storage
):
    sources = {key: tmp_path / "caf\u00e9" / key for key in ("session", "runtime", "host")}
    build = tmp_path / "build"
    if bounded_storage:
        storage.configure_storage(sources, build)
    else:
        output.configure(sources, build)
    manifest = tomllib.loads((build / "Cargo.toml").read_text(encoding="utf-8"))
    expected = ["bounded-output", "bounded-storage"] if bounded_storage else ["bounded-output"]
    assert manifest["features"]["default"] == expected
    assert manifest["features"]["bounded-storage"] == []
    patched = {"hyperlight-unikraft"} | (
        {"hyperlight-host", "hyperlight-common"} if bounded_storage else set()
    )
    assert set(manifest["patch"]["crates-io"]) == patched
    assert (
        manifest["patch"]["crates-io"]["hyperlight-unikraft"]["path"]
        == sources["runtime"].as_posix()
    )
    if bounded_storage:
        for crate in ("host", "common"):
            assert (
                manifest["patch"]["crates-io"][f"hyperlight-{crate}"]["path"]
                == (sources["host"] / f"src/hyperlight_{crate}").as_posix()
            )
    original = tomllib.loads((ROOT / "Cargo.lock").read_text(encoding="utf-8"))["package"]
    generated = tomllib.loads((build / "Cargo.lock").read_text(encoding="utf-8"))["package"]
    assert len(original) == len(generated)
    for before, after in zip(original, generated, strict=True):
        if before["name"] in patched:
            assert after == {
                key: value for key, value in before.items() if key not in ("source", "checksum")
            }
        else:
            assert after == before
    with pytest.raises(FileExistsError):
        storage.configure_storage(sources, build)


@pytest.mark.parametrize("undersized", ["bytes", "entries"])
def test_insufficient_scratch_refuses_before_reserving(tmp_path, undersized):
    limits = store.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=1024, files=2)
    with store.SharedStore(tmp_path / "db", "one", {}, limits) as db:
        scratch = store.ScratchLimits(
            1 if undersized == "bytes" else 4 * store.CHUNK,
            1 if undersized == "entries" else 2048,
            8 * store.CHUNK,
        )
        with pytest.raises(store.Refused, match="scratch allowance"):
            shared.call(db, "a", b"code", tmp_path / "helper", tmp_path / "startup", scratch, 100)
        assert db.db.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        assert db.db.execute("SELECT count(*) FROM launches").fetchone()[0] == 0


def test_storage_prerequisites_match_the_pinned_output_overlay():
    storage_metadata = json.loads((ROOT / "storage-patch.json").read_text(encoding="utf-8"))
    output_metadata = json.loads((ROOT / "output-patch.json").read_text(encoding="utf-8"))
    for label, item in output_metadata["patches"].items():
        for name, hashes in item["files"].items():
            assert storage_metadata["patches"][label]["files"][name]["before"] == hashes["after"]
