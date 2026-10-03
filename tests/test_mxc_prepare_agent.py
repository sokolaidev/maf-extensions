"""Pinned MXC agent extraction refuses ambiguous or unverified runtime input."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "scripts/experiments/mxc_session_patch/prepare_agent.py"
SPEC = importlib.util.spec_from_file_location("mxc_prepare_agent", MODULE)
assert SPEC and SPEC.loader
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def layer(path: Path, names: list[str], *, symlink: bool = False) -> str:
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "outside"
                archive.addfile(member)
            else:
                member.size = 4
                archive.addfile(member, io.BytesIO(b"cpio"))
    return agent.digest(path)


def test_checksum_precedes_archive_parsing(tmp_path):
    archive = tmp_path / "invalid.tar"
    archive.write_bytes(b"not an archive")
    with pytest.raises(ValueError, match="layer checksum"):
        agent.extract(archive, tmp_path / "initrd", "0" * 64, "0" * 64)
    assert not (tmp_path / "initrd").exists()


@pytest.mark.parametrize("names", [["initrd.cpio"], ["./initrd.cpio"]])
def test_extract_only_verified_initrd(tmp_path, names):
    archive = tmp_path / "layer.tar.gz"
    checksum = layer(archive, [*names, "../outside"])
    output = tmp_path / "initrd"
    agent.extract(archive, output, checksum, hashlib.sha256(b"cpio").hexdigest())
    assert output.read_bytes() == b"cpio"
    assert not (tmp_path.parent / "outside").exists()
    with pytest.raises(FileExistsError):
        agent.extract(archive, output, checksum, hashlib.sha256(b"cpio").hexdigest())


@pytest.mark.parametrize(
    ("names", "symlink"),
    [
        (["../initrd.cpio"], False),
        (["initrd.cpio", "./initrd.cpio"], False),
        (["initrd.cpio"], True),
    ],
)
def test_refuse_ambiguous_or_linked_member(tmp_path, names, symlink):
    archive = tmp_path / "layer.tar.gz"
    checksum = layer(archive, names, symlink=symlink)
    with pytest.raises(ValueError, match="one bounded regular initrd"):
        agent.extract(archive, tmp_path / "initrd", checksum, "0" * 64)
    assert not (tmp_path / "initrd").exists()


def test_refuse_wrong_initrd_digest(tmp_path):
    archive = tmp_path / "layer.tar.gz"
    checksum = layer(archive, ["initrd.cpio"])
    with pytest.raises(ValueError, match="initrd checksum"):
        agent.extract(archive, tmp_path / "initrd", checksum, "0" * 64)
