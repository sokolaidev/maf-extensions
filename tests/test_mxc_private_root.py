"""Real filesystem controls for retained-state access permissions."""

from __future__ import annotations

import importlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    store = importlib.import_module("scripts.experiments.mxc_session_patch.shared_store")
    private = importlib.import_module("scripts.experiments.mxc_session_patch.private_root")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

LIMITS = store.Limits(200_000, 500_000, checkpoint_bytes=1024, result_bytes=128, files=2)


def _public(root):
    if sys.platform == "win32":
        subprocess.run(
            [
                "icacls.exe",
                str(root),
                "/grant",
                "*S-1-1-0:" + ("(OI)(CI)" if root.is_dir() else "") + "(RX)",
            ],
            check=True,
            capture_output=True,
        )
    else:
        root.chmod(0o755)


def test_new_root_is_private_even_under_public_parent(tmp_path):
    parent = tmp_path / "public"
    parent.mkdir()
    _public(parent)
    root = parent / "db"
    previous = os.umask(0o022)
    try:
        with store.SharedStore(root, "one", {}, LIMITS):
            if sys.platform == "win32":
                private._windows_access(root)
                private._windows_access(root / "shared.sqlite")
            else:
                assert stat.S_IMODE(root.stat().st_mode) == 0o700
                assert root.stat().st_uid == os.geteuid()
    finally:
        os.umask(previous)
    with store.SharedStore(root, "one", {}, LIMITS):
        pass


@pytest.mark.parametrize("retained", [False, True])
def test_broad_existing_root_refuses_before_opening_database(tmp_path, retained):
    root = tmp_path / "db"
    if retained:
        with store.SharedStore(root, "one", {}, LIMITS):
            pass
    else:
        root.mkdir(mode=0o700)
    database = root / "shared.sqlite"
    before = database.read_bytes() if retained else None
    _public(root)
    with pytest.raises(store.Refused, match="private|DACL"):
        store.SharedStore(root, "one", {}, LIMITS)
    assert (database.read_bytes() if database.exists() else None) == before
    if not retained:
        assert not list(root.iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX ownership")
def test_wrong_owner_refuses_before_database_access(tmp_path, monkeypatch):
    root = tmp_path / "db"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(private.os, "geteuid", lambda: root.stat().st_uid + 1)
    with pytest.raises(store.Refused, match="owner"):
        store.SharedStore(root, "one", {}, LIMITS)
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "dacl",
    [
        "",
        "D:NO_ACCESS_CONTROL",
        "D:",
        "D:(A;;FA;;;WD)",
        "D:(A;OICIIO;FA;;;BU)",
        "D:(XA;;FA;;;OW;(x))",
        "D:(D;;FA;;;OW)",
    ],
)
def test_unverifiable_or_public_windows_dacl_refuses(dacl):
    with pytest.raises(store.Refused, match="DACL"):
        private._check_dacl(dacl, "S-1-5-21-123")


def test_windows_owner_and_privileged_os_access_is_supported():
    private._check_dacl("D:P(A;OICI;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)", "S-1-5-21-123")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows child ACL independent of traversal")
def test_public_database_below_private_root_refuses(tmp_path):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", {}, LIMITS):
        pass
    path = root / "shared.sqlite"
    before = path.read_bytes()
    _public(path)
    private._windows_access(root)
    with pytest.raises(store.Refused, match="DACL"):
        store.SharedStore(root, "one", {}, LIMITS)
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["shared.sqlite", "shared.sqlite-journal", "initialize.lock"])
def test_redirected_store_file_refuses_without_modifying_target(tmp_path, name):
    root = tmp_path / "db"
    with store.SharedStore(root, "one", {}, LIMITS):
        pass
    path = root / name
    outside = tmp_path / "outside"
    if path.exists():
        outside.hardlink_to(path)
    else:
        outside.write_bytes(b"outside")
        path.hardlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(store.Refused, match="private regular file"):
        store.SharedStore(root, "one", {}, LIMITS)
    assert outside.read_bytes() == before
