"""The origin-identifier guard, whose `scan` is a pure function and so is tested in full here.

The two shapes that carry the rest: the committed pattern must fire on an internal hostname
without touching the public content this tree legitimately carries — the suite's own
``*.azurecr.io`` registry host, the ``C:/Users/…`` path-normalisation fixtures, and the
samples' retail codes — and the CLI must judge the *staged* content, not the worktree, or a
leak that is fixed on disk but still in the index would sail through.
"""

from __future__ import annotations

import importlib.util
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
        "host", ["db.orders.internal", "api.corp.intranet", "relay-2.internal"]
    )
    def test_internal_hostnames_fire(self, host: str):
        assert any("internal hostname" in finding for finding in guard.scan(f"see {host}"))

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
    (tmp_path / name).write_text(text, "utf-8")
    subprocess.run(["git", "add", name], cwd=tmp_path, check=True)


def _main_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> int:
    """Run ``guard.main`` as if the scratch repository ``tmp_path`` were the working repo."""
    monkeypatch.setattr(guard, "_REPO_ROOT", tmp_path)
    return guard.main(["check_no_origin_identifiers.py", *args])


class TestCli:
    def test_staged_leak_is_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        _stage(tmp_path, "notes.md", "connect to db.orders.internal\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "notes.md") == 1

    def test_worktree_fix_does_not_clear_a_staged_leak(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _init_repo(tmp_path)
        _stage(tmp_path, "notes.md", "connect to db.orders.internal\n")
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
        _stage(tmp_path, "leak.md", "see relay-2.internal\n")
        assert _main_in(monkeypatch, tmp_path, "--staged", "clean.md", "leak.md") == 1
        assert _main_in(monkeypatch, tmp_path, "--staged", "clean.md") == 0

    def test_binary_stage_is_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01db.orders.internal\x02")
        subprocess.run(["git", "add", "blob.bin"], cwd=tmp_path, check=True)
        assert _main_in(monkeypatch, tmp_path, "--staged", "blob.bin") == 0

    def test_commit_msg_form(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        bad = tmp_path / "msg.txt"
        bad.write_text("ci: hook the checks\n\nvia relay-2.internal\n", "utf-8")
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

    def test_the_guards_own_source_is_exempt(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # A file at the guard's own paths carries fixtures by construction, so it is never
        # judged — otherwise a test that proves the rule fires could not itself be committed.
        _init_repo(tmp_path)
        (tmp_path / "tests").mkdir()
        leaky = "tests/test_check_no_origin_identifiers.py"
        (tmp_path / leaky).write_text("db.orders.internal and api.corp.intranet\n", "utf-8")
        subprocess.run(["git", "add", leaky], cwd=tmp_path, check=True)
        assert _main_in(monkeypatch, tmp_path, "--staged", leaky) == 0

    def test_a_different_file_is_not_exempt(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The exemption is exact-path, not a substring: a near name is still judged.
        _init_repo(tmp_path)
        (tmp_path / "other").mkdir()
        _stage(tmp_path, "other/test_check_no_origin_identifiers.py", "db.orders.internal\n")
        assert (
            _main_in(monkeypatch, tmp_path, "--staged", "other/test_check_no_origin_identifiers.py")
            == 1
        )
