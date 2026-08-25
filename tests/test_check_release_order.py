"""The release-order gate, whose `assess` is a pure function and so is tested in full here.

Two cases carry the rest: below 1.0.0 a breaking change cuts a *minor*, so `fix!:` crosses a
ceiling that `fix:` does not; and `packages/maf-sandbox-acas/` must not read as the core
package, which a prefix match missing the trailing separator gets wrong.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_order.py"
_spec = importlib.util.spec_from_file_location("check_release_order", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_CORE_FILE = "packages/maf-sandbox/src/maf_sandbox/_protocol.py"


class _Response:
    """The slice of an HTTP response ``json.load`` reads: a ``read()`` returning JSON bytes."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, object],
) -> None:
    """Route a URL to a payload dict (success) or an ``HTTPError`` (raise).

    Keys are matched as substrings, longest first, so a per-version segment like
    ``bicep/0.10.0/json`` is not shadowed by the top-level ``bicep/json``.
    """

    def fake_urlopen(url: str | urllib.request.Request, timeout: int | None = None) -> _Response:
        target = url.full_url if isinstance(url, urllib.request.Request) else url
        for key in sorted(routes, key=len, reverse=True):
            if key in target:
                result = routes[key]
                if isinstance(result, urllib.error.HTTPError):
                    raise result
                return _Response(result)
        raise AssertionError(f"unexpected url {target}")

    monkeypatch.setattr(check.urllib.request, "urlopen", fake_urlopen)


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
        "title",
        ["fix: a thing", "perf: a thing", "revert: a thing", "docs: a thing"],
    )
    def test_the_patch_types_cut_a_patch(self, title: str):
        assert check.next_version((0, 6, 1), title) == (0, 6, 2)

    def test_the_patch_types_are_the_releasing_types_that_are_not_feat(self):
        sections = json.loads(
            (Path(__file__).resolve().parent.parent / "release-please-config.json").read_text(
                "utf-8"
            )
        )["changelog-sections"]
        releasing = {s["type"] for s in sections if not s.get("hidden")}
        assert check._PATCH_TYPES == releasing - {"feat"}, (
            "the types this script treats as a patch have drifted from the ones "
            "release-please actually releases"
        )

    @pytest.mark.parametrize("title", ["chore: a thing", "ci: a thing", "refactor: a thing"])
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
        "path", ["docs/sandbox/research/files-out.md", "samples/07_docker_diagram/agent.py"]
    )
    def test_a_path_outside_packages_does_not(self, path: str):
        assert not check.touches_core([path])


class TestConsequences:
    """What it says, and that saying it is all it does."""

    def test_a_core_feat_past_a_ceiling_is_reported(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7", "maf-sandbox-wslc": "0.7"})
        notices = check.consequences("feat: a thing", [_CORE_FILE], repo)
        assert notices, "a 0.7.0 release under a <0.7 ceiling has something to say"
        assert "maf-sandbox-acas, maf-sandbox-wslc" in notices[0]
        assert "0.7.0" in notices[0]

    def test_it_names_only_the_dependents_that_exclude_it(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.8", "maf-sandbox-wslc": "0.7"})
        notices = check.consequences("feat: a thing", [_CORE_FILE], repo)
        assert "maf-sandbox-wslc" in notices[0]
        assert "maf-sandbox-acas" not in notices[0]

    def test_a_widened_ceiling_has_nothing_to_report(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.8"})
        assert check.consequences("feat: a thing", [_CORE_FILE], repo) == []

    def test_a_patch_never_crosses_a_minor_ceiling(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.consequences("docs: a thing", [_CORE_FILE], repo) == []

    def test_a_breaking_patch_does_cross_it(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.consequences("fix!: a thing", [_CORE_FILE], repo) != []

    def test_a_feat_that_touches_no_core_file_is_not_this_gate_s_business(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        changed = ["packages/maf-sandbox-acas/src/maf_sandbox_acas/_backend.py"]
        assert check.consequences("feat: a thing", changed, repo) == []

    def test_a_chore_on_the_core_releases_nothing_and_says_nothing(self, tmp_path: Path):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        assert check.consequences("chore: a thing", [_CORE_FILE], repo) == []

    def test_it_names_what_follows_rather_than_a_thing_to_do_first(self, tmp_path: Path):
        """It names what follows, not a thing to do first: the order is not this to decide."""
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        notices = check.consequences("feat: a thing", [_CORE_FILE], repo)
        assert "no published dependent resolves it" in notices[1]
        assert "the live samples" in notices[1]


class TestItFailsSoTheConsequenceIsRead:
    """A consequence nobody opens is not delivered: this exits non-zero to force the read.

    Not a verdict on the release — a version outside every ceiling is permitted, and for a
    breaking release it is the point. Merging over the red is the expected move, and the
    override is what records that the consequence was read. What asks whether the release is
    *sound* is `check_core_against_dependents.py` at the moment of release; this asks only
    that somebody looked.

    Reporting and exiting zero puts the notice in the log of a green job, where nothing sends
    anyone to read it — so the exit code is the whole delivery mechanism.
    """

    def test_a_release_past_every_ceiling_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(_CORE_FILE))
        monkeypatch.setattr(check.Path, "resolve", lambda self: repo / "scripts" / "x.py")
        assert check.main(["prog", "feat: a thing"]) == 1

    def test_a_release_every_ceiling_admits_still_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The other half, and the one that keeps this from failing every core pull request."""
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.8"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(_CORE_FILE))
        monkeypatch.setattr(check.Path, "resolve", lambda self: repo / "scripts" / "x.py")
        assert check.main(["prog", "feat: a thing"]) == 0

    def test_a_change_that_releases_no_core_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Most pull requests. A non-releasing title reaches no ceiling, so there is nothing
        to read and nothing to override."""
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(_CORE_FILE))
        monkeypatch.setattr(check.Path, "resolve", lambda self: repo / "scripts" / "x.py")
        assert check.main(["prog", "ci: a workflow tweak"]) == 0

    def test_it_says_that_merging_over_it_is_the_move(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """A red with no instruction reads as "you did something wrong", which this is not."""
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(_CORE_FILE))
        monkeypatch.setattr(check.Path, "resolve", lambda self: repo / "scripts" / "x.py")
        check.main(["prog", "feat: a thing"])
        assert "merge over this" in capsys.readouterr().out

    def test_what_it_printed_went_to_stdout_not_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """The exit code carries the failure; the text is a consequence to read, and the log
        shows both streams anyway. Keeping it on stdout keeps a local run readable."""
        repo = _repo(tmp_path, "0.6.1", {"maf-sandbox-acas": "0.7"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(_CORE_FILE))
        monkeypatch.setattr(check.Path, "resolve", lambda self: repo / "scripts" / "x.py")
        check.main(["prog", "feat: a thing"])
        captured = capsys.readouterr()
        assert "maf-sandbox-acas" in captured.out
        assert captured.err == ""


class TestPublishedVersionsAreSortedSemantically:
    """Newest-first, by numeric value, never lexically."""

    def test_0_10_0_sorts_after_0_9_0(self, monkeypatch: pytest.MonkeyPatch):
        _patch_urlopen(
            monkeypatch,
            {"simple/maf-sandbox-bicep/": {"versions": ["0.6.0", "0.10.0", "0.9.0"]}},
        )
        assert check.fetch_published_versions("maf-sandbox-bicep") == ["0.10.0", "0.9.0", "0.6.0"]

    def test_an_unsorted_multi_part_order_is_preserved_by_value(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_urlopen(
            monkeypatch,
            {"simple/maf-sandbox-bicep/": {"versions": ["1.2.4", "1.2.10", "1.3.0", "1.2.3"]}},
        )
        assert check.fetch_published_versions("maf-sandbox-bicep") == [
            "1.3.0",
            "1.2.10",
            "1.2.4",
            "1.2.3",
        ]


class TestTheVersionThisTreeDeclaresIsOneEveryDependentAdmits:
    """The backstop: on a Release PR the version is in the tree rather than read from a title.

    This is what catches a `BREAKING CHANGE:` footer added in the squash box, which no title
    check can see.
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


def test_the_usage_line_matches_the_workflow_that_runs_it():
    """Three dots in both, so the comparison starts at the merge base wherever the base is.

    A maintainer reproducing a red check follows the docstring, and two dots against a base
    that has moved on hands this every commit the base gained since.
    """
    workflow = (_SCRIPT.parent.parent / ".github" / "workflows" / "pr-title.yml").read_text(
        encoding="utf-8"
    )
    assert 'git diff --name-only "$BASE_SHA...HEAD"' in workflow
    usage = check.__doc__ or ""
    assert "git diff --name-only <base>...HEAD" in usage
