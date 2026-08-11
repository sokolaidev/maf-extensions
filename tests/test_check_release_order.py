"""The release-order gate: which titles cut a minor, and which ceilings still exclude it.

`scripts/check_release_order.py` runs on every pull request and refuses a maf-sandbox change
that would publish a version the dependents cannot adopt. Its `assess` is a pure function of a
title, a list of changed paths and a tree, so all of it is tested here rather than discovered
on the one pull request it fires against.

Two of these matter more than the rest: a breaking change below 1.0.0 cuts a *minor*, not a
major, so `fix!:` crosses a ceiling that `fix:` does not; and `packages/maf-sandbox-acas/` must
not read as the core package, which a prefix match would get wrong in the direction that lets
the release through.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_order.py"
_spec = importlib.util.spec_from_file_location("check_release_order", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_CORE_FILE = "packages/maf-sandbox/src/maf_sandbox/_protocol.py"


def _repo(tmp_path: Path, version: str, ceilings: dict[str, str]) -> Path:
    """A tree with maf-sandbox at `version` and one dependent per entry in `ceilings`."""
    core = tmp_path / "packages" / "maf-sandbox"
    core.mkdir(parents=True)
    (core / "pyproject.toml").write_text(
        f'[project]\nname = "maf-sandbox"\nversion = "{version}"\ndependencies = []\n',
        "utf-8",
    )
    for name, ceiling in ceilings.items():
        package = tmp_path / "packages" / name
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
            f'dependencies = ["maf-sandbox>=0.6.0,<{ceiling}"]\n',
            "utf-8",
        )
    return tmp_path


class TestWhichTitlesCutWhichRelease:
    """The type-to-bump mapping release-please applies, read from the title alone."""

    @pytest.mark.parametrize("title", ["feat: a thing", "feat(core): a thing"])
    def test_a_feat_cuts_a_minor(self, title: str):
        assert check.next_version((0, 6, 1), title) == (0, 7, 0)

    @pytest.mark.parametrize(
        "title", ["fix: a thing", "perf: a thing", "docs: a thing"]
    )
    def test_the_patch_types_cut_a_patch(self, title: str):
        assert check.next_version((0, 6, 1), title) == (0, 6, 2)

    @pytest.mark.parametrize(
        "title", ["chore: a thing", "ci: a thing", "refactor: a thing"]
    )
    def test_the_silent_types_cut_nothing(self, title: str):
        assert check.next_version((0, 6, 1), title) is None

    def test_a_breaking_change_below_one_cuts_a_minor_not_a_major(self):
        assert check.next_version((0, 6, 1), "fix!: a thing") == (0, 7, 0)

    def test_a_breaking_change_at_or_above_one_cuts_a_major(self):
        assert check.next_version((1, 2, 3), "fix!: a thing") == (2, 0, 0)

    def test_a_title_that_is_not_conventional_cuts_nothing(self):
        assert check.next_version((0, 6, 1), "update exec") is None


class TestWhatCountsAsTouchingTheCore:
    """Attribution is by directory, the way release-please does it."""

    def test_a_core_source_file_counts(self):
        assert check.touches_core([_CORE_FILE])

    def test_a_sibling_sharing_the_prefix_does_not(self):
        assert not check.touches_core(
            ["packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py"]
        )

    def test_a_windows_separator_still_counts(self):
        assert check.touches_core([_CORE_FILE.replace("/", "\\")])

    @pytest.mark.parametrize(
        "path", ["docs/design/files-out.md", "samples/07_docker_diagram/agent.py"]
    )
    def test_a_path_outside_packages_does_not(self, path: str):
        assert not check.touches_core([path])


class TestAssess:
    """The whole gate: refuse only when a real minor meets a ceiling that excludes it."""

    def test_a_core_feat_is_refused_while_a_ceiling_excludes_it(self, tmp_path: Path):
        repo = _repo(
            tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7", "maf-sandbox-wslc": "0.7"}
        )
        problems = check.assess("feat: a thing", [_CORE_FILE], repo)
        assert problems, "a 0.7.0 release under a <0.7 ceiling must be refused"
        assert "maf-sandbox-acas, maf-sandbox-wslc" in problems[0]
        assert "0.7.0" in problems[0]

    def test_it_names_only_the_dependents_that_exclude_it(self, tmp_path: Path):
        repo = _repo(
            tmp_path, "0.6.1", {"maf-sandbox-acas": "0.8", "maf-sandbox-wslc": "0.7"}
        )
        problems = check.assess("feat: a thing", [_CORE_FILE], repo)
        assert "maf-sandbox-wslc" in problems[0]
        assert "maf-sandbox-acas" not in problems[0]

    def test_a_widened_ceiling_lets_it_through(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.8"})
        assert check.assess("feat: a thing", [_CORE_FILE], repo) == []

    def test_a_patch_never_crosses_a_minor_ceiling(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.assess("docs: a thing", [_CORE_FILE], repo) == []

    def test_a_breaking_patch_does_cross_it(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.assess("fix!: a thing", [_CORE_FILE], repo) != []

    def test_a_feat_that_touches_no_core_file_is_not_this_gate_s_business(
        self, tmp_path: Path
    ):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        changed = ["packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py"]
        assert check.assess("feat: a thing", changed, repo) == []

    def test_a_chore_on_the_core_releases_nothing_and_passes(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.assess("chore: a thing", [_CORE_FILE], repo) == []

    def test_the_refusal_says_what_to_do_about_it(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        problems = check.assess("feat: a thing", [_CORE_FILE], repo)
        assert "RELEASING.md" in problems[1]


class TestTheVersionThisTreeDeclaresIsOneEveryDependentAdmits:
    """The backstop, on the real tree — and the one that fires on a Release PR.

    The gate above reads a title, so it can only predict what a merge would release, and a
    `BREAKING CHANGE:` footer added in the squash box at merge time is invisible to it. Here
    the prediction is over: a Release PR carries the bumped version in the tree, so a ceiling
    that excludes it is an ordinary fact about two files. Since a Release PR cannot merge
    without the required check reporting, this is the assertion the reversed order cannot get
    past.
    """

    def test_no_dependent_excludes_it(self):
        repo_root = Path(__file__).resolve().parent.parent
        declared = check.core_version(repo_root)
        bounds = check.ceilings(repo_root)
        assert bounds, "expected at least one package to declare a maf-sandbox ceiling"
        shown = ".".join(str(part) for part in declared)
        for package, ceiling in sorted(bounds.items()):
            assert check.admits(declared, ceiling), (
                f"{package} caps maf-sandbox below {shown}, the version this tree declares; "
                "widen the ceilings first (RELEASING.md, step 1 of a maf-sandbox release)"
            )
