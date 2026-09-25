"""The workspace plane on the real host filesystem: mapping, names, links and caps."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from maf_sandbox import EntryKind, SandboxTransferCapExceeded

from maf_sandbox_docker_sbx._plane import WorkspacePlane, refuse_names_the_host_changes

ROOT = "/maf-sandbox"


@pytest.fixture
def plane(tmp_path: Path) -> WorkspacePlane:
    host = tmp_path / "ws"
    host.mkdir()
    return WorkspacePlane(host, ROOT)


def _link(link: Path, target: Path) -> None:
    """A host link the plane must refuse: a symlink, or a junction without the privilege."""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except OSError:
        if sys.platform != "win32" or not target.is_dir():
            pytest.skip("this host cannot create the link")
        import _winapi

        # Windows-only, so absent from the stubs a Linux type check reads.
        getattr(_winapi, "CreateJunction")(str(target), str(link))


class TestMapping:
    def test_paths_under_the_root_map_and_the_root_itself_is_empty(self, plane):
        assert plane.parts("/maf-sandbox/work/a/b") == ("work", "a", "b")
        assert plane.parts("/maf-sandbox") == ()

    def test_an_ancestor_is_above_and_anything_else_is_outside(self, plane):
        assert plane.parts("/") is None
        with pytest.raises(ValueError, match="above"):
            plane.list("/")
        entry = plane.lstat("/")
        assert entry is not None and entry.kind is EntryKind.DIRECTORY
        for outside in ("/etc/passwd", "/maf-sandbox-outside/x"):
            with pytest.raises(ValueError, match="outside"):
                plane.parts(outside)


class TestFiles:
    def test_a_write_creates_parents_and_reads_back_exactly(self, plane):
        data = bytes(range(256))
        plane.write("/maf-sandbox/work/deep/file.bin", data)
        assert plane.read("/maf-sandbox/work/deep/file.bin", 256) == data
        entry = plane.lstat("/maf-sandbox/work/deep/file.bin")
        assert entry is not None and (entry.kind, entry.size_bytes) == (EntryKind.FILE, 256)
        listed = plane.list("/maf-sandbox/work/deep")
        assert [(name, entry.kind) for name, entry in listed] == [("file.bin", EntryKind.FILE)]

    def test_a_second_write_replaces_and_leaves_no_part_file(self, plane):
        plane.write("/maf-sandbox/f", b"first")
        plane.write("/maf-sandbox/f", b"second")
        assert plane.read("/maf-sandbox/f", 10) == b"second"
        assert [name for name, _ in plane.list("/maf-sandbox")] == ["f"]

    def test_a_read_over_the_cap_is_refused(self, plane):
        plane.write("/maf-sandbox/big", b"x" * 11)
        with pytest.raises(SandboxTransferCapExceeded):
            plane.read("/maf-sandbox/big", 10)
        assert plane.read("/maf-sandbox/big", 11) == b"x" * 11

    def test_a_directory_is_not_read(self, plane):
        plane.make_directories("/maf-sandbox/dir")
        with pytest.raises(OSError):
            plane.read("/maf-sandbox/dir", 10)

    def test_a_missing_path_stats_as_none(self, plane):
        assert plane.lstat("/maf-sandbox/absent") is None
        assert plane.lstat("/maf-sandbox/absent/deeper") is None


class TestLinks:
    def test_a_link_is_named_and_never_read_or_passed_through(self, plane, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"secret")
        _link(plane.host_root / "link", outside)
        entry = plane.lstat("/maf-sandbox/link")
        assert entry is not None and entry.kind is EntryKind.SYMLINK and entry.size_bytes is None
        with pytest.raises(ValueError, match="link"):
            plane.read("/maf-sandbox/link/secret", 100)
        with pytest.raises(ValueError, match="link"):
            plane.write("/maf-sandbox/link/planted", b"no")
        with pytest.raises(ValueError, match="link"):
            plane.list("/maf-sandbox/link")
        assert sorted(os.listdir(outside)) == ["secret"]
        assert dict(plane.list("/maf-sandbox"))["link"].kind is EntryKind.SYMLINK

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor-relative plane")
    def test_a_write_over_a_final_link_replaces_the_link(self, plane, tmp_path):
        target = tmp_path / "target"
        target.write_bytes(b"host")
        os.symlink(target, plane.host_root / "leaf")
        plane.write("/maf-sandbox/leaf", b"guest")
        assert target.read_bytes() == b"host"
        assert not (plane.host_root / "leaf").is_symlink()
        os.symlink(target, plane.host_root / "leaf2")
        with pytest.raises(OSError):
            plane.read("/maf-sandbox/leaf2", 100)

    def test_a_regular_file_as_a_parent_is_not_a_directory(self, plane):
        plane.write("/maf-sandbox/plain", b"x")
        with pytest.raises(NotADirectoryError):
            plane.read("/maf-sandbox/plain/deeper", 10)


class TestNames:
    @pytest.mark.parametrize(
        "name",
        ["a:b", "q?x", "pipe|", "trail.", "trail ", "CON", "nul.txt", "COM1", "lpt¹", "a\x01b"],
    )
    def test_windows_refuses_names_it_would_change_or_hide(self, name):
        with pytest.raises(ValueError):
            refuse_names_the_host_changes([name], windows=True)
        if "\x01" not in name:
            refuse_names_the_host_changes([name], windows=False)

    def test_ordinary_names_pass_everywhere(self):
        refuse_names_the_host_changes(["a.b", "config", "con-fig", ".hidden"], windows=True)

    def test_a_nul_is_refused_everywhere(self):
        with pytest.raises(ValueError):
            refuse_names_the_host_changes(["a\0b"], windows=False)

    def test_a_case_variant_on_a_folding_host_is_refused(self, plane):
        plane.write("/maf-sandbox/Upper", b"x")
        if not (plane.host_root / "upper").exists():
            pytest.skip("this host filesystem is case-sensitive")
        with pytest.raises(ValueError, match="verbatim"):
            plane.write("/maf-sandbox/upper", b"y")
        with pytest.raises(ValueError, match="verbatim"):
            plane.lstat("/maf-sandbox/upper")
        with pytest.raises(ValueError, match="verbatim"):
            plane.read("/maf-sandbox/upper", 10)
        assert plane.read("/maf-sandbox/Upper", 10) == b"x"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor-relative plane")
class TestDescriptors:
    def test_every_operation_returns_its_descriptors(self, plane, tmp_path):
        if not Path("/proc/self/fd").is_dir():
            pytest.skip("needs /proc/self/fd to count descriptors")
        (tmp_path / "outside").mkdir()
        os.symlink(tmp_path / "outside", plane.host_root / "link")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            plane.write("/maf-sandbox/a/b/c", b"x")
            plane.read("/maf-sandbox/a/b/c", 10)
            plane.list("/maf-sandbox/a/b")
            plane.lstat("/maf-sandbox/a/missing/deeper")
            with pytest.raises(ValueError):
                plane.read("/maf-sandbox/link/x", 10)
        assert len(os.listdir("/proc/self/fd")) == before

    def test_a_failing_close_is_not_retried_on_the_same_descriptor(self, plane, monkeypatch):
        if not Path("/proc/self/fd").is_dir():
            pytest.skip("needs /proc/self/fd to count descriptors")
        plane.make_directories("/maf-sandbox/a")
        before = len(os.listdir("/proc/self/fd"))
        closed: list[int] = []
        real_close = os.close

        def close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)
            if len(closed) == 1:
                raise OSError("close reported an error after releasing the descriptor")

        monkeypatch.setattr(os, "close", close)
        with pytest.raises(OSError, match="close reported"):
            plane.lstat("/maf-sandbox/a/x")
        monkeypatch.undo()
        assert len(closed) == len(set(closed)), closed
        assert len(os.listdir("/proc/self/fd")) == before, "a descriptor leaked"
