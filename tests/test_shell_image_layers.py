"""Shell release checks inspect saved layers before starting consumer sandboxes."""

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import shell_qualification as shell  # noqa: E402

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


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_device_in_lower_layer_is_rejected_even_with_a_later_whiteout(tmp_path, kind):
    path = tmp_path / "image.tar"
    saved_image(path, kind)
    with pytest.raises(ValueError, match="device node: dev/null"):
        shell.check_layer_archive(path)


def test_regular_files_are_not_devices(tmp_path):
    path = tmp_path / "image.tar"
    saved_image(path, tarfile.REGTYPE)
    assert shell.check_layer_archive(path) == 2


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
