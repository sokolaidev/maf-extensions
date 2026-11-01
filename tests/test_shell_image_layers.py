"""Saved layers are checked before release retention or shell execution."""

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_container_release as preparation  # noqa: E402
import shell_qualification as shell  # noqa: E402
from image_layer_archive import check_layer_archive  # noqa: E402

IMAGE = "sha256:" + "a" * 64


def saved_image(path, kind):
    with tarfile.open(path, "w") as archive:
        for index, entry in enumerate(("dev/null", "dev/.wh.null")):
            layer = io.BytesIO()
            with tarfile.open(fileobj=layer, mode="w") as contents:
                member = tarfile.TarInfo(entry)
                member.type = kind if index == 0 else tarfile.REGTYPE
                contents.addfile(member)
            data = layer.getvalue()
            member = tarfile.TarInfo(f"layer{index}.tar")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        data = json.dumps([{"Layers": ["layer0.tar", "layer1.tar"]}]).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, b"s"])
def test_device_in_lower_layer_is_rejected_even_with_a_later_whiteout(tmp_path, kind):
    path = tmp_path / "image.tar"
    saved_image(path, kind)
    with pytest.raises(ValueError, match="special file:.*dev/null"):
        check_layer_archive(path)


@pytest.mark.parametrize(
    "kind", [tarfile.REGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE]
)
def test_regular_files_and_links_are_not_special_files(tmp_path, kind):
    path = tmp_path / "image.tar"
    saved_image(path, kind)
    assert check_layer_archive(path) == 2


def test_saved_image_identity_and_layer_evidence(monkeypatch):
    outputs = []

    def save(command, **kwargs):
        assert command[:4] == ["docker", "image", "save", "--output"]
        assert command[-1] == IMAGE
        assert kwargs == {"check": True, "timeout": 180}
        path = Path(command[4])
        outputs.append(path)
        saved_image(path, tarfile.REGTYPE)

    monkeypatch.setattr(shell.subprocess, "run", save)
    assert shell.check_image_layers(IMAGE) == {"layersChecked": 2, "deviceNodesPresent": False}
    assert not outputs[0].exists()


def test_layer_failure_stops_before_acquiring_a_sandbox(monkeypatch):
    def refuse(_):
        raise ValueError("device node")

    create = AsyncMock()
    monkeypatch.setattr(shell, "check_image_layers", refuse)
    monkeypatch.setattr(shell.DockerSandboxBackend, "create", create)
    with pytest.raises(ValueError, match="device node"):
        asyncio.run(shell.check(IMAGE))
    create.assert_not_awaited()


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, b"s"])
def test_release_refuses_special_files_before_retaining_candidate(tmp_path, monkeypatch, kind):
    def run(command, **kwargs):
        assert command[:3] == ["docker", "save", "--output"]
        assert command[-1] == IMAGE
        saved_image(Path(command[3]), kind)

    monkeypatch.setattr(preparation, "run", run)
    with pytest.raises(ValueError, match="special file"):
        preparation.retain(IMAGE, tmp_path, acas=True)
    assert not (tmp_path / "image-layers.json").exists()
    assert not (tmp_path / "oci").exists()


def test_release_retains_layer_evidence_before_conversion(tmp_path, monkeypatch):
    def run(command, **kwargs):
        if command[:3] == ["docker", "save", "--output"]:
            saved_image(Path(command[3]), tarfile.REGTYPE)
        else:
            assert command[:4] == ["skopeo", "copy", "--format", "oci"]
            assert preparation.read(tmp_path / "image-layers.json") == {
                "builtImageId": IMAGE,
                "layersChecked": 2,
                "specialFilesPresent": False,
            }
            raise RuntimeError("conversion intercepted")

    monkeypatch.setattr(preparation, "run", run)
    with pytest.raises(RuntimeError, match="conversion intercepted"):
        preparation.retain(IMAGE, tmp_path, acas=True)


@pytest.mark.parametrize("layers", [[], None, "layer0.tar"])
def test_empty_or_invalid_layer_list_is_rejected(tmp_path, layers):
    path = tmp_path / "image.tar"
    with tarfile.open(path, "w") as archive:
        data = json.dumps([{"Layers": layers}]).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="filesystem layers"):
        check_layer_archive(path)
