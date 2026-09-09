"""Mounted-file comparisons retain final state and reject inconsistent reads."""

import hashlib
import io
import json
import stat
from types import SimpleNamespace

import pytest

from maf_sandbox_docker import _observer

_ID = "a" * 64


@pytest.fixture
def mounted_file(monkeypatch):
    state = SimpleNamespace(
        path="/etc/hosts",
        root=f"/docker/containers/{_ID}/hosts",
        content=b"127.0.0.1 localhost\n",
        metadata=dict(
            st_dev=1,
            st_ino=2,
            st_mode=stat.S_IFREG | 0o644,
            st_nlink=1,
            st_uid=0,
            st_gid=0,
            st_rdev=0,
            st_size=len(b"127.0.0.1 localhost\n"),
            st_mtime_ns=100,
            st_ctime_ns=200,
        ),
        during_read=False,
        overmount=False,
    )

    class Contents(io.BytesIO):
        def fileno(self):
            return 10

        def read(self, size=-1):
            if state.during_read:
                state.metadata["st_ctime_ns"] += 1
            return super().read(size)

    def open_mountinfo(path, *args, **kwargs):
        assert path == "/proc/1/mountinfo"
        mounts = f"2 1 0:1 {state.root} {state.path} rw - ext4 /dev/test rw\n"
        if state.overmount:
            mounts += f"3 1 0:1 /custom/hosts {state.path} rw - ext4 /dev/test rw\n"
        return io.StringIO(mounts)

    monkeypatch.setattr(_observer, "_processes", lambda: ["1:100"])
    monkeypatch.setattr(_observer, "open", open_mountinfo, raising=False)
    monkeypatch.setattr(
        _observer,
        "os",
        SimpleNamespace(
            O_DIRECTORY=1,
            O_NOFOLLOW=2,
            O_NONBLOCK=4,
            O_RDONLY=0,
            open=lambda *args, **kwargs: 10,
            dup=lambda fd: fd,
            close=lambda fd: None,
            stat=lambda *args, **kwargs: SimpleNamespace(**state.metadata),
            fstat=lambda fd: SimpleNamespace(**state.metadata),
            fdopen=lambda *args: Contents(state.content),
            readlink=lambda *args, **kwargs: "/other/hosts",
        ),
    )

    def measure(verified=True):
        roots = {"/etc/hosts": f"/{_ID}/hosts"} if verified else {}
        entries = _observer.measure(1024, 10, roots)["entries"]
        assert isinstance(entries, dict)
        return entries[state.path]

    return state, measure


def test_network_file_compares_sha256_and_final_metadata_without_ctime(mounted_file):
    state, measure = mounted_file
    before = measure()
    metadata, digest = json.loads(before)
    assert digest == hashlib.sha256(state.content).hexdigest()
    assert metadata == list(state.metadata.values())[:-1]
    state.metadata["st_ctime_ns"] += 1
    assert measure() == before


@pytest.mark.parametrize(
    "field",
    [
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_rdev",
        "st_size",
        "st_mtime_ns",
        "content",
    ],
)
def test_network_file_retains_other_changes(mounted_file, field):
    state, measure = mounted_file
    before = measure()
    if field == "content":
        state.content = b"127.0.0.2 localhost\n"
    else:
        state.metadata[field] += 1
    assert measure() != before


@pytest.mark.parametrize("unverified", ["inspection", "mount_root", "path", "symlink", "overmount"])
def test_other_files_retain_ctime(mounted_file, unverified):
    state, measure = mounted_file
    if unverified == "mount_root":
        state.root = "/custom/hosts"
    elif unverified == "path":
        state.path = "/other/hosts"
    elif unverified == "symlink":
        state.metadata["st_mode"] = stat.S_IFLNK | 0o777
    elif unverified == "overmount":
        state.overmount = True
    before = measure(verified=unverified != "inspection")
    state.metadata["st_ctime_ns"] += 1
    assert measure(verified=unverified != "inspection") != before


def test_ctime_change_during_network_file_read_fails(mounted_file):
    state, measure = mounted_file
    state.during_read = True
    with pytest.raises(RuntimeError, match="entry changed during observation"):
        measure()
