"""Bubblewrap broker path semantics against the core confinement contract."""

from pathlib import PurePosixPath

import pytest
from maf_sandbox.paths import confine_resolve_guest_path, resolve_guest_working_directory

from maf_sandbox_bubblewrap._guest import BASE, parts


@pytest.mark.parametrize(
    ("path", "directory"),
    [
        ("a/../result", "."),
        ("./a/../../sub/result", "sub"),
        ("result", "a/../sub"),
        ("/maf-sandbox/work/a/../result", "."),
        ("result", "/tmp/a/../work"),
        (".", "/"),
        ("result", "//tmp/work"),
        ("../escape", "."),
        ("result", "../escape"),
        ("/maf-sandbox/work/sub2/result", "sub"),
        ("//maf-sandbox/work/result", "."),
        ("a\\result", "."),
    ],
)
def test_paths_match_core_confinement(path: str, directory: str) -> None:
    try:
        working = resolve_guest_working_directory(directory, BASE)
        expected = confine_resolve_guest_path(path, working)
    except ValueError:
        with pytest.raises(ValueError):
            parts(path, directory)
    else:
        assert parts(path, directory) == PurePosixPath(expected).parts[1:]


@pytest.mark.parametrize(("path", "directory"), [("a\x00b", "."), ("result", "a\x00b")])
def test_paths_refuse_nul(path: str, directory: str) -> None:
    with pytest.raises(ValueError):
        parts(path, directory)
