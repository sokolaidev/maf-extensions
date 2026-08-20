"""The origin-identifier guard, whose ``scan`` is a pure function and so is tested in full here.

The CLI tests stage through a real scratch git repository: the index is what is judged, so a
leak fixed on disk but still staged is refused, and every path is judged like any other.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_no_origin_identifiers.py"
_spec = importlib.util.spec_from_file_location("check_no_origin_identifiers", _SCRIPT)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class TestScan:
    def test_clean_text_passes(self):
        text = "fix the router's minimum isolation floor\nsee packages/maf-sandbox\n"
        assert guard.scan(text) == []

    @pytest.mark.parametrize(
        "host", ["db.orders." + "internal", "api.corp." + "intranet", "relay-2." + "internal"]
    )
    def test_internal_hostnames_fire(self, host: str):
        assert any("internal hostname" in finding for finding in guard.scan(f"see {host}"))

    @pytest.mark.parametrize(
        "host",
        [
            "service." + "internal" + ".example.com",  # a public name after the word
            "host." + "internal" + "-name.example",  # the suffix is not the name's end
            "relay-2." + "internal" + ".example",  # a public name after the root dot
            "shop." + "internal" + ".example.com",  # same shape as the registry hosts above
        ],
    )
    def test_public_continuations_do_not_fire(self, host: str):
        assert guard.scan(f"see {host}") == []

    def test_public_content_does_not_fire(self):
        text = (
            "registry acr.azurecr.io/bicep-sandbox:0.46.1\n"  # the suite's own public registry
            "path C:/Users/x/AppData/Local/Temp/no-isolation-ab/main.bicep\n"  # a fixture
            "code STO-202, label WA-1896.25\n"  # the samples' retail identifiers
            "host shop.example.corp.com\n"  # a public domain ending in .corp
        )
        assert guard.scan(text) == []


class TestLocalNames:
    def test_absent_list_is_empty(self, tmp_path: Path):
        assert guard.load_local_names(tmp_path) == ()

    def test_list_reads_names_and_skips_comments(self, tmp_path: Path):
        (tmp_path / guard.LOCAL_LIST_NAME).write_text(
            "# the origin, never named in this repo\norigin-repo\n/path/with/identifier\n\n",
            "utf-8",
        )
        assert guard.load_local_names(tmp_path) == ("origin-repo", "/path/with/identifier")

    def test_local_name_matches_case_insensitively(self):
        assert guard.scan("extracted from Origin-Repo once", ("origin-repo",)) != []
        assert guard.scan("nothing to see here", ("origin-repo",)) == []


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)


def _stage(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True)


def _main_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> int:
    """Run ``guard.main`` as if the scratch repository ``tmp_path`` were the working repo."""
    monkeypatch.setattr(guard, "_REPO_ROOT", tmp_path)
    return guard.main(["check_no_origin_identifiers.py", *args])


class TestCli:
    def test_staged_leak_is_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        _stage(tmp_path, "notes.md", "connect to db.orders." + "internal\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "notes.md") == 1

    def test_worktree_fix_does_not_clear_a_staged_leak(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _init_repo(tmp_path)
        _stage(tmp_path, "notes.md", "connect to db.orders." + "internal\n")
        # Fixed on disk, but the index still carries the leak — that is what would commit.
        (tmp_path / "notes.md").write_text("connect to the database\n", "utf-8")
        assert _main_in(monkeypatch, tmp_path, "--staged", "notes.md") == 1

    def test_clean_stage_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        _stage(tmp_path, "notes.md", "connect to the database\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "notes.md") == 0

    def test_several_staged_files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        _stage(tmp_path, "clean.md", "nothing to see\n")
        _stage(tmp_path, "leak.md", "see relay-2." + "internal\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "clean.md", "leak.md") == 1
        assert _main_in(monkeypatch, tmp_path, "--staged", "clean.md") == 0

    def test_binary_stage_is_refused_when_it_carries_an_identifier(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _init_repo(tmp_path)
        blob = b"\x00\x01" + b"db.orders." + b"internal" + b"\x02"
        (tmp_path / "blob.bin").write_bytes(blob)
        subprocess.run(["git", "add", "blob.bin"], cwd=tmp_path, check=True)
        assert _main_in(monkeypatch, tmp_path, "--staged", "blob.bin") == 1

    def test_wide_encodings_are_decoded_too(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # An identifier can be committed in a wide encoding; the lossy single-encoding decode
        # used to interleave NULs and let it past both rules.
        for encoding in ("utf-16-le", "utf-32-le"):
            blob = ("connect to db.orders." + "internal\n").encode(encoding)
            _init_repo(tmp_path)
            (tmp_path / "wide.txt").write_bytes(blob)
            subprocess.run(["git", "add", "wide.txt"], cwd=tmp_path, check=True)
            assert _main_in(monkeypatch, tmp_path, "--staged", "wide.txt") == 1

    def test_a_staged_symlink_is_judged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # A symlink's staged blob is its stored target — a path, which is content the local
        # list must reach. pre-commit's `types: [file]` default would keep symlinks away from
        # the hook; the config sets `types: []`.
        _init_repo(tmp_path)
        (tmp_path / guard.LOCAL_LIST_NAME).write_text("origin-repo\n", "utf-8")
        try:
            os.symlink("origin-repo" + ".md", tmp_path / "link.md")
            subprocess.run(["git", "add", "link.md"], cwd=tmp_path, check=True)
        except OSError:
            pytest.skip("no symlink support on this platform")
        assert _main_in(monkeypatch, tmp_path, "--staged", "link.md") == 1

    def test_clean_binary_stage_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
        subprocess.run(["git", "add", "blob.bin"], cwd=tmp_path, check=True)
        assert _main_in(monkeypatch, tmp_path, "--staged", "blob.bin") == 0

    def test_commit_msg_form(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        bad = tmp_path / "msg.txt"
        bad.write_text("ci: hook the checks\n\nvia relay-2." + "internal\n", "utf-8")
        assert _main_in(monkeypatch, tmp_path, "--commit-msg", str(bad)) == 1
        good = tmp_path / "msg2.txt"
        good.write_text("ci: hook the checks before commit and push\n", "utf-8")
        assert _main_in(monkeypatch, tmp_path, "--commit-msg", str(good)) == 0

    def test_local_list_is_read_from_the_working_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _init_repo(tmp_path)
        (tmp_path / guard.LOCAL_LIST_NAME).write_text("origin-repo\n", "utf-8")
        _stage(tmp_path, "notes.md", "extracted from origin-repo once\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "notes.md") == 1

    def test_no_targets_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        assert _main_in(monkeypatch, tmp_path) == 2

    def test_unknown_flag_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        assert _main_in(monkeypatch, tmp_path, "--bogus") == 2

    def test_a_path_carrying_a_local_name_is_refused(self, monkeypatch, tmp_path: Path):
        # A file or directory name is part of the tree too: a leak in the name alone is refused.
        _init_repo(tmp_path)
        (tmp_path / guard.LOCAL_LIST_NAME).write_text("origin-repo\n", "utf-8")
        (tmp_path / "origin-repo").mkdir()
        _stage(tmp_path, "origin-repo/notes.md", "nothing to see\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "origin-repo/notes.md") == 1

    def test_the_guards_own_paths_are_judged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The guard's files are not special: clean content at their own paths passes, so the
        # suite cannot hide a leak behind the guard's file name.
        _init_repo(tmp_path)
        _stage(tmp_path, "scripts/check_no_origin_identifiers.py", "def scan(text):\n    ...\n")
        _stage(tmp_path, "tests/test_check_no_origin_identifiers.py", "def test_ok():\n    ...\n")
        assert (
            _main_in(
                monkeypatch,
                tmp_path,
                "--staged",
                "scripts/check_no_origin_identifiers.py",
                "tests/test_check_no_origin_identifiers.py",
            )
            == 0
        )

    def test_a_near_name_at_a_guard_path_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Same file name in a different tree: the identifier in the content is caught.
        _init_repo(tmp_path)
        (tmp_path / "other").mkdir()
        _stage(
            tmp_path, "other/test_check_no_origin_identifiers.py", "see db.orders." + "internal\n"
        )
        assert (
            _main_in(monkeypatch, tmp_path, "--staged", "other/test_check_no_origin_identifiers.py")
            == 1
        )
